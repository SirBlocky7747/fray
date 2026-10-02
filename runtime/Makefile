# runtime/Makefile — build libfrayrt and run the C test suite.
#
# Primary development target: Linux (gcc/clang). Also works on macOS and
# MinGW-w64. The fray compiler driver (bootstrap/codegen.py) compiles the
# same translation units directly; this Makefile exists for standalone
# builds, CI, and release packaging.

CC      ?= gcc
# gnu11 (not strict c11): the runtime uses POSIX calls (strdup, nanosleep,
# ucontext) that strict mode hides on glibc.
CFLAGS  ?= -O2 -g -std=gnu11 -Wall -Wextra -Werror -pthread
# -lm: glibc keeps libm separate (MinGW links it into libc).
LDLIBS  ?= -lm
AR      ?= ar

RUNTIME_SOURCES = objects.c cycles.c ops.c printing.c builtins.c \
                  threads.c atomics.c coroutine.c io.c structs.c maps.c \
                  option_result.c
RUNTIME_OBJECTS = $(RUNTIME_SOURCES:.c=.o)

.PHONY: all test asan clean

all: libfrayrt.a

libfrayrt.a: $(RUNTIME_OBJECTS)
	$(AR) rcs $@ $^

%.o: %.c runtime.h
	$(CC) $(CFLAGS) -c -o $@ $<

# Memory-management + thread-stress test suite (test.c includes main()).
runtime_test: test.c $(RUNTIME_OBJECTS)
	$(CC) $(CFLAGS) -o $@ test.c $(RUNTIME_OBJECTS) $(LDLIBS)

# The same suite under AddressSanitizer. The runtime owns every object a fray
# program allocates, so a leak here is a leak in every compiled program — this
# target has to stay clean, and CI runs it as a gate. Skips (rather than
# fails) where the compiler cannot link an instrumented binary, so the suite
# is still runnable on toolchains without ASan support.
SANFLAGS = -fsanitize=address -fno-omit-frame-pointer -O1 -g -std=gnu11 -Wall
asan: test.c $(RUNTIME_SOURCES) runtime.h
	@printf 'int main(void){return 0;}\n' > .asan_probe.c; \
	if ! $(CC) $(SANFLAGS) -o .asan_probe .asan_probe.c 2>/dev/null; then \
		rm -f .asan_probe.c .asan_probe; \
		echo "asan: $(CC) cannot link with AddressSanitizer - skipping"; \
		exit 0; \
	fi; \
	rm -f .asan_probe.c .asan_probe; \
	$(CC) $(SANFLAGS) -o runtime_test_asan test.c $(RUNTIME_SOURCES) $(LDLIBS) -pthread && \
	ASAN_OPTIONS=detect_leaks=1 ./runtime_test_asan

# Coroutine event-loop driver.
coro_drv: coro_drv.c $(RUNTIME_OBJECTS)
	$(CC) $(CFLAGS) -o $@ coro_drv.c $(RUNTIME_OBJECTS) $(LDLIBS)

test: runtime_test
	./runtime_test

clean:
	rm -f $(RUNTIME_OBJECTS) libfrayrt.a runtime_test runtime_test_asan coro_drv
