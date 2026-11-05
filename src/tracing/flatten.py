"""Turns recorded calls into plain values that mining can load anywhere.

calls.pkl holds the repo's own objects, which only load where the repo is,
so this runs inside the task's container. For each call it loads the
arguments before and after, and the result, expands objects into fields with
objects.expand (self becomes self.method, self.headers, ...), and keeps only
plain values: None, bools, numbers, strings, bytes, and lists, tuples and
dicts of those. A dict subclass, like a case-insensitive dict, becomes a
plain dict. Everything else is dropped.

The output is a pickle of builtins only, so it loads on any Python.

Standalone and Python 3.6+, like tracer.py.

    python flatten.py CALLS.pkl DEPTH FLAT.pkl
"""

import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from objects import expand  # noqa: E402

DROP = object()


def plain(v):
    """v as builtins only, or DROP if it can't be."""
    if v is None or isinstance(v, (bool, int, float, str, bytes)):
        return v
    if isinstance(v, (list, tuple)):
        items = [plain(x) for x in v]
        if any(x is DROP for x in items):
            return DROP
        return items if isinstance(v, list) else tuple(items)
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            px = plain(x)
            if not isinstance(k, str) or px is DROP:
                return DROP
            out[k] = px
        return out
    return DROP


def load(saved):
    """A saved value, or DROP if it wasn't saved or won't load."""
    if not saved or "pickle" not in saved:
        return DROP
    try:
        return pickle.loads(saved["pickle"])
    except Exception:
        return DROP


def flat_values(env, depth):
    """{name: plain value} for env and its objects' fields."""
    loaded = {k: load(v) for k, v in env.items()}
    loaded = {k: v for k, v in loaded.items() if v is not DROP}
    out = {}
    for k, v in expand(loaded, depth).items():
        p = plain(v)
        if p is not DROP:
            out[k] = p
    return out


def flatten_call(call, depth):
    out = {k: call[k] for k in ("function", "role", "test", "phase")}
    out["raised"] = call["raised"]
    out["before"] = flat_values(call["before"], depth)
    out["after"] = flat_values(call["after"] or {}, depth)
    # The result goes in as a variable named result, with its fields.
    result = {} if call["raised"] else {"result": call["result"]}
    out["result"] = flat_values(result, depth)
    return out


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    calls_path, depth, out_path = argv[0], int(argv[1]), argv[2]
    with open(calls_path, "rb") as f:
        data = pickle.load(f)
    flat = {
        "changed": data["changed"],
        "depth": depth,
        "calls": [flatten_call(c, depth) for c in data["calls"]],
    }
    with open(out_path, "wb") as f:
        pickle.dump(flat, f, protocol=2)


if __name__ == "__main__":
    main(sys.argv[1:])
