import importlib
import json
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tracing.tracer import Discover, Record, Repo, show

TRACER = Path(__file__).parent.parent / "src" / "tracing" / "tracer.py"

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


@pytest.fixture
def repo(tmp_path):
    root, lib = tmp_path / "repo", tmp_path / "lib"
    for name, text in FILES.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    lib.mkdir()
    (lib / "fakelib.py").write_text(textwrap.dedent(LIBRARY))
    sys.path[:0] = [str(root), str(lib)]
    yield root
    sys.path.remove(str(root))
    sys.path.remove(str(lib))
    for m in [m for m in sys.modules if m.startswith(("fakerepo", "fakelib"))]:
        del sys.modules[m]


def run_under(hook, fn):
    sys.settrace(hook)
    try:
        return fn()
    finally:
        sys.settrace(None)


def drive():
    """What the fake test does, plus a call that raises."""
    api = importlib.import_module("fakerepo.api")
    changed = importlib.import_module("fakerepo.changed")
    api.build([" a ", "b"])
    api.other()
    changed.Grid().safe()
    changed.keep(lambda: 1)
    api.via_lib("c")
    changed.apply("d")
    try:
        changed.Grid().fail()
    except ValueError:
        pass


def names(found):
    return {(k[0], k[2]): role for k, role in found.items()}


def test_discover_finds_direct_callers_and_callees(repo):
    d = Discover(Repo(repo, repo / "fakerepo/changed.py"))
    run_under(d.hook, drive)
    # build calls Grid; Grid.add calls clean. via_lib and shout go through
    # the library. other and unused are left out, and so is the test file.
    assert names(d.found) == {
        ("fakerepo/api.py", "build"): "caller",
        ("fakerepo/api.py", "via_lib"): "caller",
        ("fakerepo/helpers.py", "clean"): "callee",
        ("fakerepo/helpers.py", "shout"): "callee",
    }


def record(repo):
    r = Repo(repo, repo / "fakerepo/changed.py")
    d = Discover(r)
    run_under(d.hook, drive)
    rec = Record(r, d.found)
    run_under(rec.hook, drive)
    print(show(rec.calls))
    return rec.calls


def load(saved):
    return pickle.loads(saved["pickle"])


def test_record_snapshots_before_and_after(repo):
    calls = record(repo)
    adds = [c for c in calls if c["function"][2] == "Grid.add"]
    first = adds[0]
    assert first["role"] == "changed"
    assert load(first["before"]["self"]).cells == []
    assert load(first["after"]["self"]).cells == ["a"]
    assert load(first["result"]) == 1
    # The second call starts from the first call's state.
    assert load(adds[1]["before"]["self"]).cells == ["a"]


def test_record_roles(repo):
    roles = {(c["function"][2], c["role"]) for c in record(repo)}
    assert ("build", "caller") in roles
    assert ("clean", "callee") in roles
    assert ("Grid.__init__", "changed") in roles
    assert not any(name in ("other", "unused") for name, _ in roles)


def test_record_exceptions(repo):
    calls = {c["function"][2]: c for c in record(repo)}
    assert calls["Grid.fail"]["raised"] == "ValueError"
    assert calls["Grid.fail"]["result"] is None
    # An exception caught inside the function isn't reported.
    assert calls["Grid.safe"]["raised"] is None
    assert load(calls["Grid.safe"]["result"]) == "caught"


def test_values_pickle_cant_save_are_marked(repo):
    keep = next(c for c in record(repo) if c["function"][2] == "keep")
    assert "unsaved" in keep["before"]["f"]


def test_command_line_runs_both_passes(repo):
    """The two passes as they'd run in a container, through pytest."""
    keys, out = repo / "keys.json", repo / "calls.pkl"
    changed = str(repo / "fakerepo/changed.py")
    path = os.pathsep.join([str(repo), str(repo.parent / "lib")])
    env = {**os.environ, "PYTHONPATH": path}
    env_args = {"cwd": repo, "env": env, "check": True, "capture_output": True}
    for args in (
        ["discover", str(repo), changed, str(keys)],
        ["record", str(repo), changed, str(keys), str(out)],
    ):
        cmd = [sys.executable, str(TRACER), *args]
        subprocess.run([*cmd, "--", "tests/test_api.py", "-q"], **env_args)
    found = {(k[0], k[2]): k[3] for k in json.loads(keys.read_text())}
    assert found == {
        ("fakerepo/api.py", "build"): "caller",
        ("fakerepo/api.py", "via_lib"): "caller",
        ("fakerepo/helpers.py", "clean"): "callee",
        ("fakerepo/helpers.py", "shout"): "callee",
    }
    data = pickle.loads(out.read_bytes())
    recorded = {c["function"][2] for c in data["calls"]}
    expected = {"build", "via_lib", "clean", "shout", "Grid.add", "keep"}
    assert expected <= recorded
    assert "<module>" not in recorded


def test_kinds(repo):
    r = Repo(repo, repo / "fakerepo/changed.py")
    lib = importlib.import_module("fakelib")
    api = importlib.import_module("fakerepo.api")
    assert r.kind(api.build.__code__) == "repo"
    assert r.kind(lib.call.__code__) == "library"
    assert r.kind(os.path.join.__code__) == "library"
    test_file = repo / "tests" / "test_api.py"
    code = compile("def f(): pass", str(test_file), "exec")
    assert r.kind(code.co_consts[0]) == "test"
    # Not a real file, even though the path would resolve inside the repo.
    frozen = compile("def f(): pass", "<frozen importlib._bootstrap>", "exec")
    assert r.kind(frozen.co_consts[0]) == "library"
