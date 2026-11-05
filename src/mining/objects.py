"""Turns recorded objects into variables, by reflection with dir().

The runtime counterpart of SpecFuzzer's type graph. A call recorded as
{"self": grid} also gets "self.width", "self.cells" and so on, so the fields
become leaves like any input. Reading uses inspect.getattr_static, which runs
no code: methods and properties are skipped, so nothing can change state.
"""

import inspect


def fields(obj):
    """The data attributes dir() finds on obj, read without running any
    code. Dunder names like __dict__, methods, properties and other
    descriptors are left out. Private fields like _cells are kept."""
    out = {}
    for name in dir(obj):
        if name.startswith("__") and name.endswith("__"):
            continue  # Python's own machinery, like __class__
        try:
            v = inspect.getattr_static(obj, name)
        except AttributeError:
            continue
        if callable(v) or hasattr(type(v), "__get__"):
            continue  # a method, property or other descriptor
        out[name] = v
    return out


def expand(env, depth):
    """env plus a dotted variable for each field, following fields of fields
    up to depth levels. An object already seen on the path is not followed
    again, so cycles like a linked list's next stop."""
    out = dict(env)

    def walk(prefix, obj, level, path):
        if level > depth or id(obj) in path or _plain(obj):
            return
        for name, v in fields(obj).items():
            key = f"{prefix}.{name}"
            out[key] = v
            walk(key, v, level + 1, path | {id(obj)})

    for name, v in env.items():
        walk(name, v, 1, frozenset())
    return out


def _plain(v):
    """Values the grammar reads directly, whose attributes aren't fields."""
    return v is None or isinstance(
        v, (int, float, bool, str, bytes, list, tuple, dict, set)
    )
