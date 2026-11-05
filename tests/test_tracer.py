import importlib
import os
import pickle
import sys
import textwrap
from pathlib import Path

import pytest

from tracing.selfcheck import FILES, LIBRARY, problems
from tracing.tracer import Discover, Record, Repo, qualnames, show

TRACER = Path(__file__).parent.parent / "src" / "tracing" / "tracer.py"


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


def test_selfcheck_passes():
    """Both passes through pytest, as the runner runs them in a container."""
    assert problems() == []


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
    # A class body runs as a call when imported, but isn't a function.
    body = compile(
        "class C:\n    x = 1", str(repo / "fakerepo/api.py"), "exec"
    )
    assert r.kind(body.co_consts[0]) is None
    # Not a real file, even though the path would resolve inside the repo.
    frozen = compile("def f(): pass", "<frozen importlib._bootstrap>", "exec")
    assert r.kind(frozen.co_consts[0]) == "library"


def test_several_changed_files(repo):
    r = Repo(repo, [repo / "fakerepo/changed.py", repo / "fakerepo/api.py"])
    api = importlib.import_module("fakerepo.api")
    helpers = importlib.import_module("fakerepo.helpers")
    assert r.kind(api.build.__code__) == "changed"
    assert r.kind(helpers.clean.__code__) == "repo"


def test_qualnames_from_source(tmp_path):
    """How older Pythons, without co_qualname, name functions."""
    path = tmp_path / "m.py"
    path.write_text(
        textwrap.dedent(
            """
            class Grid:
                def add(self):
                    def inner():
                        pass

                @staticmethod
                def make():
                    pass

            def top():
                pass
            """
        )
    )
    names = qualnames(str(path))
    assert names[3] == "Grid.add"
    assert names[4] == "Grid.add.<locals>.inner"
    assert names[7] == names[8] == "Grid.make"  # decorator line and def line
    assert names[11] == "top"
