"""Bottom-up enumeration by size, ported from Enumeration.scala.

Level 1 is the leaves. Level n combines smaller expressions whose sizes add
up to n - 1, since the operator itself counts 1. The Scala's ranking and top-k
parts are left out: they look for one program that hits a target output, and
here every expression is kept for mining.
"""

from dataclasses import dataclass
from itertools import product

from .evaluate import evaluate
from .grammar import (
    ANY,
    FROM_VALUES,
    Node,
    Var,
    code,
    get_op,
    type_context,
    type_of,
    typeof,
)


@dataclass(frozen=True)
class Term:
    expr: object
    type: str
    values: tuple  # its value on each recorded call


def _has_var(e):
    return isinstance(e, Var) or (
        isinstance(e, Node) and any(_has_var(c) for c in e.children)
    )


def cost_combinations(total, arity):
    """Every way to split total into arity sizes of at least 1, in order.

    Scala's costCombinations. For example, cost_combinations(3, 2) is
    [(1, 2), (2, 1)].
    """
    if arity == 1:
        return [(total,)] if total >= 1 else []
    return [
        (first,) + rest
        for first in range(1, total)
        for rest in cost_combinations(total - first, arity - 1)
    ]


def apply(op, child_values):
    """The new values on every call, or None if op is undefined on any of them.

    Scala's verifyMultipleCtx.
    """
    out = []
    for args in zip(*child_values, strict=True):
        if op.ok and not op.ok(*args):
            return None
        try:
            out.append(op.fn(*args))
        except Exception:
            return None
    return tuple(out)


def enumerate_terms(vocab, ctxs, limit, prune="self_loop"):
    """Every well-typed expression up to size limit, as Terms by size.

    prune is "self_loop" (Scala's filter: drop a term equal to one of its
    children on every call) or "none". Terms are never merged just because
    their values match, since that would throw away rules.
    """
    type_ctx = type_context(ctxs)

    # initProgramBank
    bank = {1: []}
    for leaf in vocab.leaves:
        t = type_of(type_ctx, leaf)
        if t is not None:
            bank[1].append(
                Term(leaf, t, tuple(evaluate(env, leaf) for env in ctxs))
            )

    # newPrograms, one level at a time
    for level in range(2, limit + 1):
        bank[level] = []
        for name in vocab.ops:
            op = get_op(name)
            for child_types, out_type in op.sigs:
                for sizes in cost_combinations(level - 1, len(child_types)):
                    pools = [
                        [c for c in bank[s] if t in (ANY, c.type)]
                        for s, t in zip(sizes, child_types, strict=True)
                    ]
                    for children in product(*pools):
                        # Scala's CONCAT check, for every operator: a term
                        # with no variable says nothing about the program.
                        if not any(_has_var(c.expr) for c in children):
                            continue
                        values = apply(op, [c.values for c in children])
                        if values is None:
                            continue
                        out = out_type
                        if out == FROM_VALUES:
                            # A method: one supported type on every call.
                            types = {typeof(v) for v in values}
                            if len(types) != 1 or None in types:
                                continue
                            out = types.pop()
                        if prune != "none" and any(
                            c.type == out and c.values == values
                            for c in children
                        ):
                            continue
                        bank[level].append(
                            Term(
                                Node(name, tuple(c.expr for c in children)),
                                out,
                                values,
                            )
                        )
    return bank


def show(bank):
    """Every term as one line: size, type, code and its value on each call."""
    lines = []
    for level, terms in bank.items():
        lines.append(f"--- size {level}: {len(terms)} terms")
        for t in terms:
            lines.append(f"{t.type:10} {code(t.expr):35} {list(t.values)}")
    return "\n".join(lines)
