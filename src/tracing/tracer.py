"""Records calls, with whole objects, while a task's tests run.

Recorded: every function in the changed files, plus their direct callers
and callees anywhere in the repo. Test files and libraries are left out, but
calls are seen through them: a repo function that calls a changed function
by way of a library still counts as its caller.

It runs inside a task's container, on that container's Python, so it is one
file with no imports from this package and nothing newer than Python 3.6.

Two passes over the same tests:
  discover  finds the repo functions that call, or are called by, a function
            in the changed file. It copies nothing, so it's fast. A caller has
            to be known before it starts, to take its before snapshot.
  record    snapshots each call to those functions and the changed file's own:
            the arguments at entry and at exit, and the result or exception.

Each call is tagged with the test that made it, from pytest's
PYTEST_CURRENT_TEST, so calls from some tests can be left out later.

Each value is saved with pickle.dumps the moment it's seen, which copies it,
so later changes by the program can't reach the snapshot. A value pickle
can't save is stored as {"unsaved": reason}.

    python tracer.py discover ROOT CHANGED KEYS.json -- PYTEST_ARGS...
    python tracer.py record ROOT CHANGED KEYS.json CALLS.pkl -- PYTEST_ARGS...

CHANGED is one path, or several separated by commas.
"""

import ast
import json
import os
import pickle
import sys

# Code flags for generators and coroutines, which pause and resume. Their
# call and return events don't match one call, so they're skipped for now.
PAUSING = 0x20 | 0x80 | 0x200
# Functions have the CO_OPTIMIZED flag. Module and class bodies, which also
# run as calls, don't.
OPTIMIZED = 0x01
# Comprehensions are functions to Python before 3.12, but not to a reader.
NOT_FUNCTIONS = (
    "<module>",
    "<listcomp>",
    "<dictcomp>",
    "<setcomp>",
    "<genexpr>",
)


def is_test_file(path):
    """pytest's default test file names, test_*.py and *_test.py, and its
    conftest.py."""
    name = os.path.basename(path)
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


class Repo:
    """Decides which functions belong to the repo and names them."""

    def __init__(self, root, changed):
        """changed is a path, or a list of paths."""
        if isinstance(changed, (str, os.PathLike)):
            changed = [changed]
        self.root = os.path.realpath(root)
        self.changed = {os.path.realpath(c) for c in changed}
        self._kinds = {}
        self._keys = {}
        self._names = {}  # file -> {line: qualified name}

    def kind(self, code):
        """'changed', 'repo', 'test' or 'library', or None for code that
        isn't a function, like a module or class body, or a comprehension.

        library is everything outside the repo, and anything installed in
        site-packages: Python's own modules, pytest, numpy and so on.
        """
        if not code.co_flags & OPTIMIZED or code.co_name in NOT_FUNCTIONS:
            return None
        f = code.co_filename
        if f not in self._kinds:
            p = os.path.realpath(f)
            if p in self.changed:
                self._kinds[f] = "changed"
            elif (
                f.startswith("<")  # not a file, like <frozen importlib>
                or not p.startswith(self.root + os.sep)
                or "site-packages" in p
            ):
                self._kinds[f] = "library"
            elif is_test_file(p):
                self._kinds[f] = "test"
            else:
                self._kinds[f] = "repo"
        return self._kinds[f]

    def nearest(self, frame):
        """The first frame above frame that isn't library code or a
        non-function, with its kind. Libraries are seen through, so a call
        made by way of one still counts as direct. Tests are not."""
        f = frame.f_back
        while f is not None:
            k = self.kind(f.f_code)
            if k not in ("library", None):
                return f, k
            f = f.f_back
        return None, None

    def key(self, code):
        """(file relative to the root, first line, qualified name)."""
        if code not in self._keys:
            rel = os.path.relpath(
                os.path.realpath(code.co_filename), self.root
            )
            self._keys[code] = (rel, code.co_firstlineno, self.qualname(code))
        return self._keys[code]

    def qualname(self, code):
        """The function's full name, like Grid.add.

        Python 3.11 gives it as co_qualname. Before that, it's read from the
        source: the function whose def or decorator is on co_firstlineno.
        """
        if hasattr(code, "co_qualname"):
            return code.co_qualname
        names = self._names.get(code.co_filename)
        if names is None:
            names = self._names[code.co_filename] = qualnames(code.co_filename)
        return names.get(code.co_firstlineno, code.co_name)


def qualnames(path):
    """{line: qualified name} for every function in a file, keyed by its def
    line and each decorator's line, since co_firstlineno is one of them."""
    try:
        with open(path) as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return {}
    out = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            defs = (ast.FunctionDef, ast.AsyncFunctionDef)
            if isinstance(child, defs + (ast.ClassDef,)):
                name = prefix + child.name
                if isinstance(child, defs):
                    for n in [child] + child.decorator_list:
                        out[n.lineno] = name
                    walk(child, name + ".<locals>.")
                else:
                    walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


class Discover:
    """Pass 1: finds the direct callers and callees of the changed file,
    seeing through library code between them."""

    def __init__(self, repo):
        self.repo = repo
        self.found = {}  # key -> "caller" or "callee"

    def hook(self, frame, event, arg):
        if event != "call":
            return None
        here = self.repo.kind(frame.f_code)
        if here not in ("changed", "repo"):
            return None
        up, back = self.repo.nearest(frame)
        if here == "changed" and back == "repo":
            self.found.setdefault(self.repo.key(up.f_code), "caller")
        elif here == "repo" and back == "changed":
            self.found.setdefault(self.repo.key(frame.f_code), "callee")
        return None  # no line events, so this pass stays fast


def current_test():
    """The running test's ID and phase, like ("t.py::test_a", "call").

    pytest sets PYTEST_CURRENT_TEST to "t.py::test_a (call)" while a test
    runs, and "setup" or "teardown" around it.
    """
    s = os.environ.get("PYTEST_CURRENT_TEST")
    if not s:
        return None, None
    test, _, phase = s.rpartition(" ")
    return test, phase.strip("()")


def save(value):
    try:
        return {"pickle": pickle.dumps(value)}
    except Exception as e:
        return {"unsaved": type(e).__name__ + ": " + str(e)[:200]}


def arg_names(code):
    """The parameter names, including *args and **kwargs."""
    n = code.co_argcount + code.co_kwonlyargcount
    if code.co_flags & 0x04:
        n += 1
    if code.co_flags & 0x08:
        n += 1
    return code.co_varnames[:n]


def snapshot(frame, names):
    return {n: save(frame.f_locals[n]) for n in names if n in frame.f_locals}


class Record:
    """Pass 2: snapshots each call to the functions to record."""

    def __init__(self, repo, roles):
        self.repo = repo
        self.roles = roles  # key -> "caller" or "callee", from Discover
        self.calls = []

    def hook(self, frame, event, arg):
        if event != "call":
            return None
        code = frame.f_code
        if code.co_flags & PAUSING:
            return None
        kind = self.repo.kind(code)
        if kind == "changed":
            role = "changed"
        elif kind == "repo":
            role = self.roles.get(self.repo.key(code))
        else:
            role = None
        if role is None:
            return None
        names = arg_names(code)
        test, phase = current_test()
        call = {
            "function": list(self.repo.key(code)),
            "role": role,
            "test": test,
            "phase": phase,
            "before": snapshot(frame, names),
            "after": None,
            "result": None,
            "raised": None,
        }
        self.calls.append(call)
        state = {"exception": None}

        def local(frame, event, arg):
            if event == "exception":
                state["exception"] = arg[0].__name__
            elif event == "line":
                state["exception"] = None  # it was caught
            elif event == "return":
                call["after"] = snapshot(frame, names)
                if state["exception"] is None:
                    call["result"] = save(arg)
                else:
                    call["raised"] = state["exception"]
            return local

        return local


def show(calls):
    """Every recorded call as text: function, role, before, after, result.

    Loading the values needs their classes importable, so run this where the
    repo's code is.
    """

    def text(saved):
        if saved is None:
            return "-"
        if "unsaved" in saved:
            return "<unsaved: " + saved["unsaved"] + ">"
        value = pickle.loads(saved["pickle"])
        fields = getattr(value, "__dict__", None)
        if fields is not None and not isinstance(value, type):
            return type(value).__name__ + repr(fields)
        return repr(value)

    def args(snap):
        return ", ".join(k + "=" + text(v) for k, v in snap.items())

    lines = []
    for c in calls:
        path, line, name = c["function"]
        lines.append(
            name + "  (" + c["role"] + ", " + path + ":" + str(line) + ")"
        )
        if c.get("test"):
            lines.append("  test:   " + c["test"] + " (" + c["phase"] + ")")
        lines.append("  before: " + args(c["before"]))
        lines.append("  after:  " + args(c["after"] or {}))
        if c["raised"]:
            lines.append("  raised: " + c["raised"])
        else:
            lines.append("  result: " + text(c["result"]))
    return "\n".join(lines)


def run_tests(hook, pytest_args):
    import pytest

    sys.settrace(hook)
    try:
        return pytest.main(pytest_args)
    finally:
        sys.settrace(None)


def main(argv):
    if "--" not in argv:
        sys.exit(__doc__)
    i = argv.index("--")
    mine, pytest_args = argv[:i], argv[i:][1:]
    if mine[0] == "discover":
        root, changed, keys_path = mine[1:4]
        d = Discover(Repo(root, changed.split(",")))
        run_tests(d.hook, pytest_args)
        with open(keys_path, "w") as f:
            json.dump([list(k) + [r] for k, r in d.found.items()], f, indent=1)
    elif mine[0] == "record":
        root, changed, keys_path, out_path = mine[1:5]
        with open(keys_path) as f:
            roles = {(k[0], k[1], k[2]): k[3] for k in json.load(f)}
        r = Record(Repo(root, changed.split(",")), roles)
        run_tests(r.hook, pytest_args)
        with open(out_path, "wb") as f:
            data = {"changed": changed.split(","), "calls": r.calls}
            pickle.dump(data, f)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
