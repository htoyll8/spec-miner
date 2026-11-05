"""Checks that tracer.py works on this Python, before a real task is traced.

Builds a small fake repo, with a library outside it, runs both passes through
pytest the way a real task runs, and checks what was recorded. Prints each
problem and exits 1 if anything is wrong, so the runner stops early.

Standalone and Python 3.6+, like tracer.py. The tests reuse its fake repo.

    python selfcheck.py
"""

import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
TRACER = os.path.join(HERE, "tracer.py")
FLATTEN = os.path.join(HERE, "flatten.py")

FILES = {
    "fakerepo/__init__.py": "",
    "fakerepo/changed.py": """
        import fakelib
        from fakerepo.helpers import clean, shout

        class Grid:
            def __init__(self):
                self.cells = []

            def add(self, x):
                self.cells.append(clean(x))
                return len(self.cells)

            def fail(self):
                raise ValueError("no")

            def safe(self):
                try:
                    raise KeyError("k")
                except KeyError:
                    return "caught"

        def keep(f):
            return f

        def apply(x):
            # A callback through a library: shout is still a callee.
            return fakelib.call(shout, x)
    """,
    "fakerepo/helpers.py": """
        def clean(x):
            return x.strip()

        def shout(x):
            return x.upper()

        def unused():
            return 1
    """,
    "fakerepo/api.py": """
        import fakelib
        from fakerepo.changed import Grid

        def build(xs):
            g = Grid()
            for x in xs:
                g.add(x)
            return g

        def other():
            return 2

        def via_lib(x):
            # Calls Grid.add through a library: still a caller.
            return fakelib.call(Grid().add, x)
    """,
    "tests/test_api.py": """
        from fakerepo.api import build, other, via_lib
        from fakerepo.changed import Grid, apply, keep

        def test_all():
            assert len(build([" a ", "b"]).cells) == 2
            assert other() == 2
            assert Grid().safe() == "caught"
            keep(lambda: 1)
            assert via_lib("c") == 1
            assert apply("d") == "D"
    """,
}

# A library outside the repo, which calls whatever it's given.
LIBRARY = """
    def call(f, *args):
        return f(*args)
"""

# What both passes should find in the fake repo.
EXPECTED_ROLES = {
    ("fakerepo/api.py", "build"): "caller",
    ("fakerepo/api.py", "via_lib"): "caller",
    ("fakerepo/helpers.py", "clean"): "callee",
    ("fakerepo/helpers.py", "shout"): "callee",
}
EXPECTED_CALLS = {"build", "via_lib", "clean", "shout", "Grid.add", "keep"}
TEST_ID = "tests/test_api.py::test_all"


def write_fake_repo(base):
    """Write the fake repo and library under base. Returns (root, lib)."""
    root, lib = os.path.join(base, "repo"), os.path.join(base, "lib")
    for name, text in FILES.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(textwrap.dedent(text))
    os.makedirs(lib)
    with open(os.path.join(lib, "fakelib.py"), "w") as f:
        f.write(textwrap.dedent(LIBRARY))
    return root, lib


def run(cmd, root, lib):
    """Run cmd in the fake repo, raising with its output if it fails."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env["PYTHONPATH"] = os.pathsep.join([root, lib])
    r = subprocess.run(
        cmd,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if r.returncode != 0:
        raise RuntimeError(cmd[2] + " failed:\n" + r.stdout[-2000:])


def run_passes(root, lib):
    """Run discover, record and flatten on the fake repo.

    Returns (keys, calls, flat).
    """
    keys = os.path.join(root, "keys.json")
    calls = os.path.join(root, "calls.pkl")
    flat = os.path.join(root, "flat.pkl")
    changed = os.path.join(root, "fakerepo", "changed.py")
    tests = ["tests/test_api.py", "-q", "-p", "no:cacheprovider"]
    for args in (
        ["discover", root, changed, keys],
        ["record", root, changed, keys, calls],
    ):
        run([sys.executable, TRACER] + args + ["--"] + tests, root, lib)
    run([sys.executable, FLATTEN, calls, "1", flat], root, lib)
    out = []
    with open(keys) as f:
        out.append(json.load(f))
    for path in (calls, flat):
        with open(path, "rb") as f:
            out.append(pickle.load(f))
    return tuple(out)


def problems():
    """Everything wrong with the tracer on this Python, or [] if nothing."""
    base = tempfile.mkdtemp()
    try:
        root, lib = write_fake_repo(base)
        try:
            found, data, flat = run_passes(root, lib)
        except RuntimeError as e:
            return [str(e)]
        out = []
        roles = {(k[0], k[2]): k[3] for k in found}
        if roles != EXPECTED_ROLES:
            out.append("discover found " + repr(roles))
        recorded = {c["function"][2] for c in data["calls"]}
        missing = EXPECTED_CALLS - recorded
        if missing:
            out.append("record missed " + repr(sorted(missing)))
        tests = {c["test"] for c in data["calls"]}
        if tests != {TEST_ID}:
            out.append("calls were tagged with " + repr(tests))
        adds = [c for c in data["calls"] if c["function"][2] == "Grid.add"]
        # add appends a cell, so self after the call differs from before.
        if adds and adds[0]["after"]["self"] == adds[0]["before"]["self"]:
            out.append("Grid.add's after snapshot didn't change")
        flat_adds = [
            c for c in flat["calls"] if c["function"][2] == "Grid.add"
        ]
        first = flat_adds[0] if flat_adds else None
        # flatten turns self into its fields, before and after.
        if not first or (
            first["before"].get("self.cells"),
            first["after"].get("self.cells"),
            first["result"].get("result"),
        ) != ([], ["a"], 1):
            out.append("flatten gave " + repr(first))
        return out
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    found = problems()
    for p in found:
        print("SELFCHECK:", p)
    if found:
        sys.exit(1)
    print("SELFCHECK: ok on Python " + sys.version.split()[0])


if __name__ == "__main__":
    main()
