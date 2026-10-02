"""
fray module resolution: plain modules, packages and relative imports.

A module path resolves under a root directory to one of two files:

    a.b.c  ->  a/b/c.fray             a plain module
            or a/b/c/__init__.fray    a package initializer

The initializer names the package: `pkg/__init__.fray` is the module `pkg`, and
its definitions (and the names it imports) are what `import pkg` and
`from pkg import name` see. A package is *split* across files by putting
siblings beside the initializer (`pkg/util.fray` is the module `pkg.util`) and
re-exporting from it (`from .util import twice`).

Every module has a *package*, which is where a relative import starts from:

    module         file                    package   `.sibling` means
    __main__       the program             ""        (an error)
    pkg            pkg/__init__.fray       pkg       pkg.sibling
    pkg.sub        pkg/sub.fray            pkg       pkg.sibling
    pkg.sub        pkg/sub/__init__.fray   pkg.sub   pkg.sub.sibling

A leading dot means "the current package"; each extra dot goes up one level
(`..other` inside `pkg.sub` is `other`, at the top level).
"""

from __future__ import annotations

import os

INIT_STEM = "__init__"


def stdlib_root() -> str:
    """Where the standard library lives: ``$FRAY_STDLIB`` when it is set (a
    package can be installed anywhere), else ``stdlib/`` beside ``bootstrap/``
    in this checkout. It may not exist — a checkout without a standard library
    is a normal state — and every caller treats a missing one as empty.
    """
    override = os.environ.get("FRAY_STDLIB")
    if override:
        return os.path.normpath(override)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, os.pardir, "stdlib"))


def find_module_file(root: str, dotted: str):
    """Return ``(path, is_package)`` for module ``dotted`` under ``root``.

    A plain module file wins over a package directory of the same name, so
    ``pkg.fray`` takes precedence over ``pkg/__init__.fray``. A module the
    program's own root does not hold is looked for in the standard library, so
    `import random` means the same thing from any directory — and the
    program's own module of that name still wins. Returns ``(None, False)``
    when neither root has it.
    """
    rel = dotted.replace(".", os.sep)
    roots = [root]
    stdlib = stdlib_root()
    if stdlib and os.path.normpath(stdlib) != os.path.normpath(root):
        roots.append(stdlib)
    for candidate in roots:
        plain = os.path.join(candidate, rel + ".fray")
        if os.path.isfile(plain):
            return os.path.normpath(plain), False
        package = os.path.join(candidate, rel, INIT_STEM + ".fray")
        if os.path.isfile(package):
            return os.path.normpath(package), True
    return None, False


def package_of(dotted: str, is_package: bool) -> str:
    """The package a module's relative imports resolve against."""
    if is_package:
        return dotted
    i = dotted.rfind(".")
    return dotted[:i] if i >= 0 else ""


def resolve_relative(module: str, level: int, package: str):
    """Resolve a relative import to an absolute module name.

    ``level`` is the number of leading dots (0 for an ordinary import), and
    ``package`` is the importing module's package (``""`` for the main
    program). Returns ``None`` when the import has no package to resolve
    against — a relative import in the main program, or one that climbs past
    the top level.
    """
    if level <= 0:
        return module
    if not package:
        return None
    base = package
    for _ in range(level - 1):
        if not base:
            return None
        i = base.rfind(".")
        base = base[:i] if i >= 0 else ""
    if not module:
        return base or None
    return f"{base}.{module}" if base else module
