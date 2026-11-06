"""The typed grammar: expressions, types and operators.

Ported from Arithmetic.scala in htoyll8/bottom-up-enumerative-synthesis. One
OPS row holds what Scala spreads over the case class, Value.*, evalUtil, tyOf,
aryOf, init, mkCode and astSize. Values are plain Python values, so there are
no NumV or StringV wrappers.
"""

import operator
from dataclasses import dataclass
from typing import Any, Callable

# ======================================================================
# Expressions
# ======================================================================


@dataclass(frozen=True)
class Var:
    name: str


@dataclass(frozen=True)
class Const:
    value: Any


@dataclass(frozen=True)
class Node:
    op: str
    children: tuple


# ======================================================================
# Types, as strings so a grammar can be written to JSON
# ======================================================================

BASE = (
    "int",
    "float",
    "bool",
    "str",
    "bytes",
    "list[str]",
    "list[int]",
    "dict",
)


def typeof(v):
    """A recorded value's type, or None if the grammar doesn't cover it."""
    if v is None:
        return "none"
    if type(v) in (int, float, bool, str, bytes):
        return type(v).__name__
    if type(v) is dict and all(isinstance(k, str) for k in v):
        return "dict"
    if type(v) in (list, tuple) and v:
        inner = {type(x) for x in v}
        if inner == {str}:
            return "list[str]"
        if inner == {int}:
            return "list[int]"
    return None


# ======================================================================
# Operators
# ======================================================================


@dataclass(frozen=True)
class Op:
    # ((child types), result type) pairs, so an operator can have several.
    sigs: tuple
    fn: Callable
    code: str  # Python text, with {0}, {1}, ... for the children
    ok: Callable = None  # the "is it defined" part of Scala's Verifier


def _sigs(*pairs):
    return tuple((tuple(args), out) for args, out in pairs)


OPS = {
    # Arithmetic
    "PLUS": Op(_sigs((("int", "int"), "int")), operator.add, "({0} + {1})"),
    "MINUS": Op(_sigs((("int", "int"), "int")), operator.sub, "({0} - {1})"),
    "TIMES": Op(_sigs((("int", "int"), "int")), operator.mul, "({0} * {1})"),
    # String
    "LENGTH": Op(
        _sigs(
            (("str",), "int"),
            (("bytes",), "int"),
            (("list[str]",), "int"),
            (("list[int]",), "int"),
            (("dict",), "int"),
        ),
        len,
        "len({0})",
    ),
    "STRINDEX": Op(
        _sigs((("str", "int"), "str")),
        lambda s, i: s[i],
        "{0}[{1}]",
        ok=lambda s, i: -len(s) <= i < len(s),
    ),
    "INDEXOF": Op(_sigs((("str", "str"), "int")), str.find, "{0}.find({1})"),
    "CONCAT": Op(_sigs((("str", "str"), "str")), operator.add, "({0} + {1})"),
    "LOWER": Op(_sigs((("str",), "str")), str.lower, "{0}.lower()"),
    "UPPER": Op(_sigs((("str",), "str")), str.upper, "{0}.upper()"),
    "TITLE": Op(_sigs((("str",), "str")), str.title, "{0}.title()"),
    "STRSUBSTR": Op(
        _sigs((("str", "int", "int"), "str")),
        lambda s, i, j: s[i:j],
        "{0}[{1}:{2}]",
    ),
    "REVERSESTRSUBSTR": Op(
        _sigs((("str", "int"), "str")), lambda s, i: s[i:], "{0}[{1}:]"
    ),
    "SPLIT": Op(
        _sigs((("str", "str"), "list[str]")),
        str.split,
        "{0}.split({1})",
        ok=lambda s, sep: sep != "",
    ),
    "STRREPLACE": Op(
        _sigs((("str", "str", "str"), "str")),
        str.replace,
        "{0}.replace({1}, {2})",
    ),
    "STRTOLIST": Op(_sigs((("str",), "list[str]")), list, "list({0})"),
    # Boolean
    "EQ": Op(
        _sigs(*(((t, t), "bool") for t in BASE)), operator.eq, "({0} == {1})"
    ),
    "STARTSWITH": Op(
        _sigs((("str", "str"), "bool")), str.startswith, "{0}.startswith({1})"
    ),
    "ENDSWITH": Op(
        _sigs((("str", "str"), "bool")), str.endswith, "{0}.endswith({1})"
    ),
    "ISALPHA": Op(_sigs((("str",), "bool")), str.isalpha, "{0}.isalpha()"),
    "ISDIGIT": Op(_sigs((("str",), "bool")), str.isdigit, "{0}.isdigit()"),
    # List
    "ARRINDEX": Op(
        _sigs((("list[str]", "int"), "str"), (("list[int]", "int"), "int")),
        lambda a, i: a[i],
        "{0}[{1}]",
        ok=lambda a, i: -len(a) <= i < len(a),
    ),
    "ARRAPPEND": Op(
        _sigs((("list[str]", "str"), "list[str]")),
        lambda a, x: list(a) + [x],
        "({0} + [{1}])",
    ),
    "ARRJOIN": Op(
        _sigs((("str", "list[str]"), "str")), str.join, "{0}.join({1})"
    ),
    # Conditional
    "IFTHENELSE": Op(
        _sigs(*((("bool", t, t), t) for t in BASE)),
        lambda c, a, b: a if c else b,
        "({1} if {0} else {2})",
    ),
    # Added for mining, not in the Scala: comparisons and connectives, so
    # rules can be stated.
    "NE": Op(
        _sigs(*(((t, t), "bool") for t in BASE)), operator.ne, "({0} != {1})"
    ),
    "LT": Op(
        _sigs((("int", "int"), "bool"), (("float", "float"), "bool")),
        operator.lt,
        "({0} < {1})",
    ),
    "LE": Op(
        _sigs((("int", "int"), "bool"), (("float", "float"), "bool")),
        operator.le,
        "({0} <= {1})",
    ),
    "GT": Op(
        _sigs((("int", "int"), "bool"), (("float", "float"), "bool")),
        operator.gt,
        "({0} > {1})",
    ),
    "GE": Op(
        _sigs((("int", "int"), "bool"), (("float", "float"), "bool")),
        operator.ge,
        "({0} >= {1})",
    ),
    "IN": Op(
        _sigs(
            (("str", "str"), "bool"),
            (("str", "list[str]"), "bool"),
            (("str", "dict"), "bool"),
        ),
        lambda x, s: x in s,
        "({0} in {1})",
    ),
    "NOT": Op(_sigs((("bool",), "bool")), operator.not_, "(not {0})"),
    "AND": Op(
        _sigs((("bool", "bool"), "bool")),
        lambda a, b: a and b,
        "({0} and {1})",
    ),
    "OR": Op(
        _sigs((("bool", "bool"), "bool")), lambda a, b: a or b, "({0} or {1})"
    ),
}

# Any argument type, and a result type read from the values.
ANY = "*"
FROM_VALUES = "?"

OPS["IS_NONE"] = Op(
    _sigs(((ANY,), "bool")), lambda x: x is None, "({0} is None)"
)

# Types whose methods can be called on recorded values: they can't change in
# place, so a call has no side effects. Lists get tuple's methods, count and
# index, for the same reason. Like METHODS in proto4.py.
METHODS = {
    "str": (str, ("str",)),
    "bytes": (bytes, ("bytes",)),
    "int": (int, ("int",)),
    "float": (float, ("float",)),
    "list": (tuple, ("list[str]", "list[int]")),
}


def methods_named(name):
    """The method operators for a method name, like str.strip/0, str.strip/1.

    /0 calls it with no argument and /1 with one.
    """
    return [
        f"{kind}.{name}/{n}"
        for kind, (py, _) in METHODS.items()
        if not name.startswith("_") and callable(getattr(py, name, None))
        for n in (0, 1)
    ]


def get_op(name):
    """An operator by name: a row of OPS, or a method like str.strip/1."""
    if name in OPS:
        return OPS[name]
    kind, rest = name.split(".", 1)
    method, n = rest.rsplit("/", 1)
    receivers = METHODS[kind][1]
    if n == "0":
        sigs = tuple(((r,), FROM_VALUES) for r in receivers)
        code_ = f"{{0}}.{method}()"
    else:
        sigs = tuple(((r, ANY), FROM_VALUES) for r in receivers)
        code_ = f"{{0}}.{method}({{1}})"
    return Op(sigs, lambda x, *args: getattr(x, method)(*args), code_)


# ======================================================================
# Typechecking, code and size
# ======================================================================


def type_of(type_ctx, e):
    """Scala's tyOf. None plays the part of ErrTy."""
    if isinstance(e, Var):
        return type_ctx.get(e.name)
    if isinstance(e, Const):
        return typeof(e.value)
    kids = tuple(type_of(type_ctx, c) for c in e.children)
    for args, out in get_op(e.op).sigs:
        if all(a in (ANY, k) for a, k in zip(args, kids, strict=True)):
            # A method's result type is only known from its values.
            return None if out == FROM_VALUES else out
    return None


def code(e):
    """Scala's mkCode."""
    if isinstance(e, Var):
        return e.name
    if isinstance(e, Const):
        return repr(e.value)
    return get_op(e.op).code.format(*(code(c) for c in e.children))


def size(e):
    """Scala's astSize: every node counts 1."""
    return (
        1 if not isinstance(e, Node) else 1 + sum(size(c) for c in e.children)
    )


def merge_types(types):
    """One type for a set of observed types, or None if they conflict.

    A type seen alongside "none" is that type, since the value can be None:
    {"str", "none"} is "str". Operators that fail on None just drop the term.
    """
    types = set(types)
    if None in types:
        return None
    if len(types) > 1:
        types.discard("none")
    return types.pop() if len(types) == 1 else None


def type_context(ctxs):
    """The type of each variable, from the recorded calls.

    A variable whose type varies between calls is left out, except that it
    may also be None.
    """
    seen = {}
    for env in ctxs:
        for k, v in env.items():
            seen.setdefault(k, set()).add(typeof(v))
    out = {k: merge_types(ts) for k, ts in seen.items()}
    return {k: t for k, t in out.items() if t is not None}
