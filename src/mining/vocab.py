"""A function's vocabulary: the leaves and operators enumeration may use.

Ported from Vocab.scala in htoyll8/bottom-up-enumerative-synthesis.
"""

import json
from dataclasses import dataclass

from .grammar import Const, Var, get_op


@dataclass
class Vocab:
    leaves: list
    ops: dict  # operator name -> weight, unused for now


def to_json(vocab):
    """A readable dict. Each operator also lists its typings and code."""
    leaves = [
        {"var": x.name} if isinstance(x, Var) else {"const": x.value}
        for x in vocab.leaves
    ]
    ops = {
        name: {
            "weight": w,
            "code": get_op(name).code,
            "sigs": [[list(args), out] for args, out in get_op(name).sigs],
        }
        for name, w in vocab.ops.items()
    }
    return {"leaves": leaves, "ops": ops}


def from_json(d):
    leaves = [
        Var(x["var"]) if "var" in x else Const(x["const"]) for x in d["leaves"]
    ]
    return Vocab(leaves, {name: op["weight"] for name, op in d["ops"].items()})


def save(vocab, path, **info):
    """Write vocab to path as JSON, with any extra info, like the task."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({**info, **to_json(vocab)}, indent=1, default=repr)
    path.write_text(text + "\n")


def load(path):
    return from_json(json.loads(path.read_text()))
