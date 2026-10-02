#include "runtime.h"
#include <stdio.h>
extern void fray_coro_result_store(FrayValue v);
static void tramp(FrayValue self){ (void)self;
    printf("T: calling sleep\n"); fflush(stdout);
    FrayValue five = fray_int(5);
    fray_sleep_boxed(five);
    fray_release(five);
    printf("T: back from sleep\n"); fflush(stdout);
}
int main(void){
    FrayValue fn = fray_function("m", (void*)tramp, 0);
    FrayValue h = fray_coro_start(fn);
    fray_release(h);
    fray_release(fn);
    printf("M: draining\n"); fflush(stdout);
    fray_coro_run_until_complete();
    printf("M: drained\n"); fflush(stdout);
    return 0;
}
