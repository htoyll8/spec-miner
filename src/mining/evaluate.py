"""Running an expression on one recorded call.

Scala's evalUtil, from Arithmetic.scala in
htoyll8/bottom-up-enumerative-synthesis.
"""

from .grammar import Const, Var, get_op


def evaluate(env, e):
    """Scala's evalUtil."""
    if isinstance(e, Var):
        return env[e.name]
    if isinstance(e, Const):
        return e.value
    return get_op(e.op).fn(*(evaluate(env, c) for c in e.children))
