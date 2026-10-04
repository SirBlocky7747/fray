/* io.c — non-blocking file and socket I/O for fray coroutines.
 *
 * Why a worker pool and not epoll: epoll_ctl(2) returns EPERM for a regular
 * file — anything without poll semantics cannot be registered. So a purely
 * readiness-driven design could make sockets non-blocking but could never make
 * fileReadAsync non-blocking, and the phase asks for both. This module
 * therefore offloads *every* operation to a small pool of worker threads that
 * perform the blocking syscall into a plain malloc'd buffer, then post the
 * completion back to the coroutine's own event loop. One mechanism covers
 * files, pipes and sockets, and it reuses the ready-queue push the loop
 * already documents as its cross-thread wakeup seam.
 *
 * GC discipline: a worker never touches a FrayValue. It fills a C buffer and
 * the parked coroutine converts that into a value on its own GC space after
 * it resumes. This matters because fibers of a loop run on the loop thread's
 * per-thread GC space; a worker allocating into it would be a cross-space
 * write. The hand-off is therefore (Coro* token, job*) and nothing else
 * crosses the thread boundary.
 *
 * From a plain thread — a module-level `readFileAsync` — there is no fiber to
 * park, so the job runs inline and the call is simply the blocking syscall.
 * The language surface is the same either way, which is what keeps the oracle
 * (thread-per-coroutine) and the compiled engine in agreement.
 */

#include "runtime.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <io.h>
#define fray_close_fd(fd) _close(fd)
#else
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#define fray_close_fd(fd) close(fd)
#endif

/* ── Jobs ── */

typedef enum {
    IOJ_READ_FILE,     /* slurp a whole file                        */
    IOJ_WRITE_FILE,    /* replace a whole file                      */
    IOJ_READ_FD,       /* read(fd, buf, n)                          */
    IOJ_WRITE_FD,      /* write(fd, buf, len)                       */
    IOJ_ACCEPT,        /* accept(server_fd) -> new fd               */
    IOJ_CONNECT        /* connect(host, port) -> new fd             */
} IoKind;

typedef struct IoJob {
    IoKind      kind;
    int         fd;          /* READ/WRITE_FD, ACCEPT                */
    char       *path;        /* READ_FILE, WRITE_FILE                */
    const char *host;        /* CONNECT                              */
    int         port;
    char       *buf;         /* READ_FD dst / WRITE_FD src           */
    size_t      len;         /* WRITE_FD bytes, READ_FD capacity     */
    ssize_t     done;        /* result: bytes moved, or the new fd    */
    int         err;         /* errno, 0 on success                  */
    const char *errmsg;      /* fray diagnostic, NULL on success     */
    void       *token;       /* the parked coroutine                 */
    struct IoJob *next;
} IoJob;

/* ── The pool ──
 *
 * The pool grows on demand. A fixed-size blocking pool deadlocks as soon as
 * more operations are outstanding than there are workers AND they depend on
 * each other: a worker parked in read() waiting for a peer's write() cannot
 * finish, and that peer's write() never gets a worker to run on. Two
 * coroutines echoing over one socket already need both an outstanding read
 * and an outstanding write; a pool of four falls over at five conversations.
 * So the pool starts small and adds a worker whenever outstanding work
 * exceeds the number started, up to a cap. (This is the same cliff
 * asyncio's default ThreadPoolExecutor has — its cap is min(32, cpu+4).)
 * The real fix is readiness-based I/O for sockets, which needs no worker at
 * all; until then, growing is what makes the pattern correct. */

#define IO_WORKERS_MIN 4
#define IO_WORKERS_MAX 256

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cv = PTHREAD_COND_INITIALIZER;
static IoJob         *g_head, *g_tail;
static int            g_started;       /* workers running                   */
static int            g_outstanding;   /* jobs queued or running            */
static bool           g_stopping;
static pthread_t      g_workers[IO_WORKERS_MAX];

static void io_job_free(IoJob *j) {
    if (!j) return;
    free(j->path);
    free(j->buf);
    free((void *)j->host);
    free(j);
}

/* Slurp a file into a fresh buffer. Returns the length, or -1 with errno set.
 * The buffer is sized from the file's length, which is what makes the bulk
 * read cheap; see fray_slurp_stream in builtins.c. */
static ssize_t slurp(const char *path, char **out) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    size_t len = 0;
    char *buf = fray_slurp_stream(f, &len);
    int failed = ferror(f);
    fclose(f);
    if (failed) { free(buf); errno = EIO; return -1; }
    *out = buf;
    return (ssize_t)len;
}

static void job_execute(IoJob *j) {
    switch (j->kind) {
    case IOJ_READ_FILE: {
        ssize_t n = slurp(j->path, &j->buf);
        if (n < 0) j->errmsg = "ValueError: no such file";
        j->done = n;
        break;
    }
    case IOJ_WRITE_FILE: {
        FILE *f = fopen(j->path, "wb");
        if (!f) { j->errmsg = "ValueError: cannot open file for writing"; break; }
        size_t wrote = j->len ? fwrite(j->buf, 1, j->len, f) : 0;
        int rc = fclose(f);
        if (wrote != j->len || rc != 0) j->errmsg = "RuntimeError: write failed";
        else j->done = (ssize_t)j->len;
        break;
    }
    case IOJ_READ_FD: {
        ssize_t n = read(j->fd, j->buf, j->len);
        if (n < 0) {
            j->errmsg = (errno == EBADF) ? "ValueError: not a socket"
                                         : "OSError: read failed";
        }
        j->done = n;
        break;
    }
    case IOJ_WRITE_FD: {
        size_t off = 0;
        while (off < j->len) {
            ssize_t n = write(j->fd, j->buf + off, j->len - off);
            if (n < 0) {
                if (errno == EINTR) continue;
                j->errmsg = (errno == EBADF) ? "ValueError: not a socket"
                                             : "OSError: write failed";
                j->done = -1;
                return;
            }
            off += (size_t)n;
        }
        j->done = (ssize_t)off;
        break;
    }
    case IOJ_ACCEPT: {
        int fd = accept(j->fd, NULL, NULL);
        if (fd < 0) j->errmsg = "OSError: accept failed";
        j->done = fd;
        break;
    }
    case IOJ_CONNECT:
#ifdef _WIN32
        j->errmsg = "OSError: sockets are not supported on this platform";
        j->done = -1;
#else
    {
        struct addrinfo hints, *res = NULL, *it;
        char portstr[16];
        snprintf(portstr, sizeof portstr, "%d", j->port);
        memset(&hints, 0, sizeof hints);
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        int gai = getaddrinfo(j->host, portstr, &hints, &res);
        if (gai != 0 || !res) {
            j->errmsg = "ValueError: cannot resolve host";
            j->done = -1;
            break;
        }
        int fd = -1, last = ECONNREFUSED;
        for (it = res; it; it = it->ai_next) {
            fd = socket(it->ai_family, it->ai_socktype, it->ai_protocol);
            if (fd < 0) { last = errno; continue; }
            if (connect(fd, it->ai_addr, it->ai_addrlen) == 0) break;
            last = errno;
            fray_close_fd(fd);
            fd = -1;
        }
        freeaddrinfo(res);
        if (fd < 0) {
            j->errmsg = (last == ECONNREFUSED) ? "ValueError: connection refused"
                                               : "ValueError: connection failed";
            j->done = -1;
            break;
        }
        int one = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        j->done = fd;
    }
#endif
    break;
    }
}

static void *io_worker(void *unused) {
    (void)unused;
    for (;;) {
        pthread_mutex_lock(&g_lock);
        while (!g_head && !g_stopping) pthread_cond_wait(&g_cv, &g_lock);
        if (g_stopping && !g_head) { pthread_mutex_unlock(&g_lock); return NULL; }
        IoJob *j = g_head;
        g_head = j->next;
        if (!g_head) g_tail = NULL;
        pthread_mutex_unlock(&g_lock);

        job_execute(j);
        /* Hands the job to the loop thread, which resumes the coroutine and
         * takes ownership. Never touches a FrayValue here. */
        fray_coro_post_io(j->token, j);
        pthread_mutex_lock(&g_lock);
        g_outstanding--;
        pthread_mutex_unlock(&g_lock);
    }
}

static void io_shutdown(void) {
    pthread_mutex_lock(&g_lock);
    if (!g_started) { pthread_mutex_unlock(&g_lock); return; }
    g_stopping = true;
    int n = g_started;
    pthread_cond_broadcast(&g_cv);
    pthread_mutex_unlock(&g_lock);
    for (int i = 0; i < n; i++) pthread_join(g_workers[i], NULL);
    g_started = 0;
}

static void io_ensure_pool(void) {
    pthread_mutex_lock(&g_lock);
    if (g_started) { pthread_mutex_unlock(&g_lock); return; }
    g_stopping = false;
    pthread_mutex_unlock(&g_lock);
    int n = 0;
    for (; n < IO_WORKERS_MIN; n++) {
        if (pthread_create(&g_workers[n], NULL, io_worker, NULL) != 0) break;
    }
    pthread_mutex_lock(&g_lock);
    g_started = n;             /* only the ones that actually started   */
    pthread_mutex_unlock(&g_lock);
    atexit(io_shutdown);
}

/* ── Submit-and-park ── */

/* Run `job`, parking the calling coroutine if there is one. Returns the job
 * once it has completed, or NULL if the coroutine was resumed for some other
 * reason (which cannot happen today, but the loop is written not to assume
 * it). The caller owns the returned job. */
static IoJob *io_await(IoJob *job) {
    void *token = fray_coro_current_token();
    if (!token) {
        job_execute(job);            /* plain thread: just do it       */
        return job;
    }
    job->token = token;
    fray_coro_hold(token);           /* alive for the flight          */
    io_ensure_pool();
    pthread_mutex_lock(&g_lock);
    job->next = NULL;
    if (g_tail) g_tail->next = job; else g_head = job;
    g_tail = job;
    g_outstanding++;
    /* Every job in flight gets a worker of its own until the cap, so a job
     * that is waiting on a peer can never starve the peer that unblocks it. */
    if (g_outstanding > g_started && g_started < IO_WORKERS_MAX) {
        if (pthread_create(&g_workers[g_started], NULL, io_worker, NULL) == 0) {
            g_started++;
        }
    }
    pthread_cond_signal(&g_cv);
    pthread_mutex_unlock(&g_lock);

    fray_coro_park(token);           /* resumes inside fray_coro_post_io */

    IoJob *done = (IoJob *)fray_coro_take_io(token);
    fray_coro_release_token(token);  /* unref on the loop thread      */
    return done;
}

/* Turn a finished job into a value, or throw. Consumes the job. */
static FrayValue io_result_to_value(IoJob *j) {
    if (!j) {
        fray_throw(FRAY_EXC_RUNTIME, "RuntimeError: I/O completion lost");
        return fray_none();
    }
    const char *err = j->errmsg;
    if (err) {
        io_job_free(j);
        if (strcmp(err, "OSError: read failed") == 0 ||
            strcmp(err, "OSError: write failed") == 0 ||
            strcmp(err, "OSError: accept failed") == 0) {
            fray_throw(FRAY_EXC_RUNTIME, err);
        } else {
            fray_throw(FRAY_EXC_VALUE, err);
        }
        return fray_none();
    }
    switch (j->kind) {
    case IOJ_READ_FILE: {
        size_t n = j->done < 0 ? 0 : (size_t)j->done;
        char *owned = j->buf;
        j->buf = NULL;              /* the string takes it; do not free it */
        io_job_free(j);
        return fray_string_take(owned, n);
    }
    case IOJ_WRITE_FILE: {
        ssize_t n = j->done;
        io_job_free(j);
        return fray_int(n);
    }
    case IOJ_READ_FD: {
        /* done == 0 means the peer closed: the caller sees an empty string,
         * which is what a read of a drained socket should look like. */
        size_t n = j->done < 0 ? 0 : (size_t)j->done;
        char *owned = j->buf;
        j->buf = NULL;
        io_job_free(j);
        return fray_string_take(owned, n);
    }
    case IOJ_WRITE_FD: {
        ssize_t n = j->done;
        io_job_free(j);
        return fray_int(n);
    }
    case IOJ_ACCEPT:
    case IOJ_CONNECT: {
        ssize_t fd = j->done;
        io_job_free(j);
        return fray_int(fd);
    }
    }
    io_job_free(j);
    return fray_none();
}

/* ── Argument helpers ── */

static const char *io_path_arg(FrayValue v, const char *who) {
    if (!fray_is_string(v)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: path must be a string");
        return NULL;
    }
    (void)who;
    return v->as.str.data;
}

static int64_t io_int_arg(FrayValue v, const char *who) {
    if (!fray_is_int(v)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: argument must be an integer");
        return -1;
    }
    (void)who;
    return v->as.i;
}

/* ── The language surface ── */

FrayValue fray_io_read_file(FrayValue path) {
    const char *p = io_path_arg(path, "readFileAsync");
    if (!p) return fray_none();
    IoJob *j = (IoJob *)calloc(1, sizeof(IoJob));
    j->kind = IOJ_READ_FILE;
    j->path = strdup(p);
    return io_result_to_value(io_await(j));
}

FrayValue fray_io_write_file(FrayValue path, FrayValue text) {
    const char *p = io_path_arg(path, "writeFileAsync");
    if (!p) return fray_none();
    if (!fray_is_string(text)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: writeFileAsync() text must be a string");
        return fray_none();
    }
    IoJob *j = (IoJob *)calloc(1, sizeof(IoJob));
    j->kind = IOJ_WRITE_FILE;
    j->path = strdup(p);
    /* Copy the bytes before parking: the coroutine frame owns the string and
     * a worker must never see a FrayValue. */
    j->len = text->as.str.len;
    j->buf = (char *)malloc(j->len + 1);
    if (j->buf && j->len) memcpy(j->buf, text->as.str.data, j->len);
    return io_result_to_value(io_await(j));
}

FrayValue fray_io_read_fd(FrayValue fd, FrayValue count) {
    int64_t f = io_int_arg(fd, "readAsync");
    if (f < 0) return fray_none();
    int64_t want = io_int_arg(count, "readAsync");
    if (want <= 0) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: readAsync() count must be positive");
        return fray_none();
    }
    if (want > (int64_t)1 << 24) want = (int64_t)1 << 24;   /* 16 MiB ceiling */
    IoJob *j = (IoJob *)calloc(1, sizeof(IoJob));
    j->kind = IOJ_READ_FD;
    j->fd = (int)f;
    j->len = (size_t)want;
    /* One byte over the request: the result string adopts this buffer and
     * writes a terminating NUL at the length it read, which is `want` when
     * the socket hands over everything we asked for. */
    j->buf = (char *)malloc(j->len + 1);
    return io_result_to_value(io_await(j));
}

FrayValue fray_io_write_fd(FrayValue fd, FrayValue text) {
    int64_t f = io_int_arg(fd, "writeAsync");
    if (f < 0) return fray_none();
    if (!fray_is_string(text)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: writeAsync() text must be a string");
        return fray_none();
    }
    IoJob *j = (IoJob *)calloc(1, sizeof(IoJob));
    j->kind = IOJ_WRITE_FD;
    j->fd = (int)f;
    j->len = text->as.str.len;
    j->buf = (char *)malloc(j->len + 1);
    if (j->buf && j->len) memcpy(j->buf, text->as.str.data, j->len);
    return io_result_to_value(io_await(j));
}

FrayValue fray_io_accept(FrayValue server_fd) {
    int64_t f = io_int_arg(server_fd, "tcpAccept");
    if (f < 0) return fray_none();
    IoJob *j = (IoJob *)calloc(1, sizeof(IoJob));
    j->kind = IOJ_ACCEPT;
    j->fd = (int)f;
    return io_result_to_value(io_await(j));
}

FrayValue fray_io_connect(FrayValue host, FrayValue port) {
    if (!fray_is_string(host)) {
        fray_throw(FRAY_EXC_TYPE, "TypeError: tcpConnect() host must be a string");
        return fray_none();
    }
    int64_t p = io_int_arg(port, "tcpConnect");
    if (p < 0) return fray_none();
    IoJob *j = (IoJob *)calloc(1, sizeof(IoJob));
    j->kind = IOJ_CONNECT;
    j->host = strdup(host->as.str.data);
    j->port = (int)p;
    return io_result_to_value(io_await(j));
}

FrayValue fray_io_listen(FrayValue port) {
    int64_t p = io_int_arg(port, "tcpListen");
    if (p < 0) return fray_none();
    if (p < 0 || p > 65535) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: port must be 0..65535");
        return fray_none();
    }
#ifdef _WIN32
    fray_throw(FRAY_EXC_RUNTIME, "OSError: sockets are not supported on this platform");
    return fray_none();
#else
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        fray_throw(FRAY_EXC_RUNTIME, "OSError: cannot create socket");
        return fray_none();
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);   /* loopback only */
    addr.sin_port = htons((uint16_t)p);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0 ||
        listen(fd, 16) != 0) {
        fray_close_fd(fd);
        fray_throw(FRAY_EXC_VALUE, "ValueError: cannot listen on port");
        return fray_none();
    }
    return fray_int(fd);
#endif
}

void fray_io_close_fd(FrayValue fd) {
    int64_t f = io_int_arg(fd, "closeSocket");
    if (f < 0) return;
    fray_close_fd((int)f);
}

/* The port a listener actually bound. `tcpListen(0)` asks the kernel for an
 * ephemeral port, and there is no other way for the program to learn which one
 * it got — without this a socket test would have to hardcode a port and
 * collide with whatever else the host is running. */
FrayValue fray_io_port(FrayValue fd) {
    int64_t f = io_int_arg(fd, "tcpPort");
    if (f < 0) return fray_none();
#ifdef _WIN32
    fray_throw(FRAY_EXC_RUNTIME, "OSError: sockets are not supported on this platform");
    return fray_none();
#else
    struct sockaddr_in addr;
    socklen_t alen = sizeof addr;
    memset(&addr, 0, sizeof addr);
    if (getsockname((int)f, (struct sockaddr *)&addr, &alen) != 0) {
        fray_throw(FRAY_EXC_VALUE, "ValueError: not a socket");
        return fray_none();
    }
    return fray_int((int64_t)ntohs(addr.sin_port));
#endif
}

/* Boxed shims, matching the convention every other builtin uses. The shims
 * exist so codegen calls one uniform signature, not because ownership moves:
 * the CALLER owns the arguments. Measured at the shim boundary, an argument
 * arrives with refcount 2 -- the caller's binding plus codegen's retain for
 * the call -- and the caller releases its own reference afterwards. Releasing
 * here as well frees the object while it is still live, which shows up as
 * `ValueError: not a socket` on the next use of the same fd.
 *
 * The original comment here said the callee consumes the arguments, which is
 * backwards, and cost a long investigation to disprove. */
FrayValue fray_io_read_file_boxed(FrayValue path) {
    return fray_io_read_file(path);
}

FrayValue fray_io_write_file_boxed(FrayValue path, FrayValue text) {
    return fray_io_write_file(path, text);
}

FrayValue fray_io_read_fd_boxed(FrayValue fd, FrayValue count) {
    return fray_io_read_fd(fd, count);
}

FrayValue fray_io_write_fd_boxed(FrayValue fd, FrayValue text) {
    return fray_io_write_fd(fd, text);
}

FrayValue fray_io_accept_boxed(FrayValue server_fd) {
    return fray_io_accept(server_fd);
}

FrayValue fray_io_connect_boxed(FrayValue host, FrayValue port) {
    return fray_io_connect(host, port);
}

FrayValue fray_io_listen_boxed(FrayValue port) {
    return fray_io_listen(port);
}

FrayValue fray_io_close_fd_boxed(FrayValue fd) {
    fray_io_close_fd(fd);
    return fray_none();
}

FrayValue fray_io_port_boxed(FrayValue fd) {
    return fray_io_port(fd);
}
