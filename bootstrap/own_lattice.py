"""Bootstrapping helper for the compiler passes.

inference.py needs a type-node registry (Identifier → TypeNode) to key its
signatures; codegen.py is the module that actually manages variable slots
and must query the inference results. Defining TypeNode here breaks the
inference → codegen → inference import cycle while keeping a single
instance of the class.
"""


class TypeNode:
    """A variable or expression's compile-time type.

    VOID is a placeholder for "not yet inferred"; it must not leak into
    monomorphic decisions.
    """

    __slots__ = ("tag",)

    def __init__(self, tag: str = "VOID"):
        self.tag = tag

    def __repr__(self) -> str:
        return f"TypeNode({self.tag})"


REGISTRY: dict = {}


def intern(tag: str) -> TypeNode:
    """Intern a type node by tag so equality is pointer equality."""
    node = REGISTRY.get(tag)
    if node is None:
        node = TypeNode(tag)
        REGISTRY[tag] = node
    return node


# Canonical interned nodes.
INT = intern("INT")
FLOAT = intern("FLOAT")
BOOL = intern("BOOL")
STRING = intern("STRING")
LIST = intern("LIST")
TUPLE = intern("TUPLE")
SET = intern("SET")
NONE = intern("NONE")
UNKNOWN = intern("UNKNOWN")
VOID = intern("VOID")


# Ground tags for switch tables.
GROUND_TAGS = {"INT", "FLOAT", "BOOL", "STRING", "LIST", "TUPLE", "SET", "NONE"}


def ground(tag: str) -> TypeNode:
    """Intern a tag if it is a known ground type, else UNKNOWN."""
    return REGISTRY.get(tag, UNKNOWN) if tag in GROUND_TAGS else UNKNOWN
