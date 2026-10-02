"""
target.py — host/target platform detection for the fray toolchain.

Every platform-specific decision (LLVM triple, data layout, executable
suffix, C compiler discovery) lives here so codegen, the test runner, the
REPL, the benchmarks and the release packaging all stay consistent.

The toolchain builds and runs natively on Linux (primary development
target: Linux Mint / Ubuntu) and still supports Windows via MinGW.
"""

from __future__ import annotations
import os
import platform
import shutil
import sys


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def exe_suffix() -> str:
    """Suffix for native executables the toolchain produces."""
    return ".exe" if is_windows() else ""


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64"):
        return "x86_64"
    if m in ("arm64", "aarch64"):
        return "aarch64"
    return m


def host_triple() -> str:
    """LLVM target triple for this machine.

    Override with the FRAY_TARGET_TRIPLE environment variable to
    cross-emit for another target (object emission only; linking still
    happens with the host toolchain).
    """
    override = os.environ.get("FRAY_TARGET_TRIPLE")
    if override:
        return override
    arch = _arch()
    if is_windows():
        return f"{arch}-pc-windows-msvc"
    if is_macos():
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-gnu"


def host_data_layout() -> str:
    """Explicit LLVM data layout for the host triple, or '' to let LLVM
    derive the default from the triple (preferred — stays correct across
    LLVM versions). The Windows/MSVC string is pinned because the MinGW
    linking path was tuned against it."""
    if is_windows() and _arch() == "x86_64":
        return ("e-m:w-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128"
                "-f80:128-n8:16:32:64-S128")
    return ""


def find_c_compiler() -> str:
    """Locate a C compiler used to compile libfrayrt and link binaries.

    Prefers gcc (the linking flags were validated with it), then clang,
    then cc. On Windows, falls back to the standard MSYS2 locations.
    """
    for name in ("gcc", "clang", "cc"):
        found = shutil.which(name)
        if found:
            return found
    if is_windows():
        for path in ("C:/msys64/ucrt64/bin/gcc.exe",
                     "C:/msys64/mingw64/bin/gcc.exe"):
            if os.path.exists(path):
                return path
    return ""


def describe() -> str:
    """Human-readable one-liner for banners and diagnostics."""
    return (f"{host_triple()} | {platform.python_implementation()} "
            f"{platform.python_version()} | exe suffix '{exe_suffix()}'")
