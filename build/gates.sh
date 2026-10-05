#!/bin/bash
# Canonical gates after the try/return fix: the driver is build/frayc_driver.
set -x
cd "$(dirname "$0")/.." || exit 1

echo "##### native-gate"
.venv/bin/python tools/check_frontend.py --native-gate build/frayc_driver || echo "NATIVE_GATE_FAIL"

echo "##### stage2 (fixed point)"
.venv/bin/python tools/check_frontend.py --stage2 --driver build/frayc_driver || echo "STAGE2_FAIL"

echo "##### pipeline run (interpreter + self-hosted codegen, output diffed)"
.venv/bin/python tools/check_frontend.py --run || echo "RUN_FAIL"

echo "##### memory gate (driver)"
.venv/bin/python tools/check_memory.py --driver build/frayc_driver || echo "MEMORY_FAIL"

# The cross-thread memory errors ASan cannot see. ASan reported the
# loop_push_ready use-after-free 0 times in 240 runs across six ASAN_OPTIONS
# settings, because the window is a couple of instructions wide and never
# interleaves at native speed; memcheck does interleave it. Separate from the
# line above because that one runs the self-hosted driver over the golden
# cases, and the programs that expose this live in benchmarks/. Adds about a
# minute.
echo "##### memory gate (cross-thread, valgrind memcheck)"
.venv/bin/python tools/check_memory.py --valgrind || echo "MEMORY_VG_FAIL"

echo "##### python-free chain (sh-only golden runner)"
sh tools/check_cases.sh || echo "CHECK_CASES_FAIL"

echo "##### oracle + bootstrap codegen"
.venv/bin/python tools/run_tests.py || echo "RUN_TESTS_FAIL"

echo "##### syntax reference audit (fray-layout.md against the compiler)"
.venv/bin/python tools/check_fray_txt.py || echo "FRAY_TXT_FAIL"

echo "##### tutorial audit (docs/fray_by_example.md against the compiler)"
.venv/bin/python tools/check_tutorial.py || echo "TUTORIAL_FAIL"

echo "##### block-scope audit (names that escape the block they are bound in)"
.venv/bin/python tools/check_scope.py || echo "SCOPE_FAIL"

echo "GATES_DONE"
