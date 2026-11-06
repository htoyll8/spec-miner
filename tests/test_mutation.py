import json

from check.mutate import fix_lines, make_mutants, patch_pieces, revert_mutants
from check.mutation import score, script

SRC = '''class R:
    def length(self, body):
        """Docstrings aren't mutated."""
        if body is not None:
            self.n = len(body)
        return self.n > 0

    def other(self):
        return True
'''

PATCH = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -3,3 +3,3 @@
         \"\"\"Docstrings aren't mutated.\"\"\"
-        if body:
+        if body is not None:
             self.n = len(body)
"""


def test_fix_lines_skip_context():
    assert fix_lines(PATCH, "m.py") == {4}
    assert fix_lines(PATCH, "other.py") == set()


def test_make_mutants():
    mutants = make_mutants(SRC, "m.py", {"R.length", "R.other"}, PATCH)
    found = {
        (i["function"], i["kind"], i["before"], i["after"]) for _, i in mutants
    }
    assert ("R.length", "compare", "body is not None", "body is None") in found
    assert (
        "R.length",
        "negate",
        "body is not None",
        "not (body is not None)",
    ) in found
    assert ("R.length", "delete", "self.n = len(body)", "pass") in found
    assert ("R.length", "constant", "0", "1") in found
    assert ("R.length", "return", "self.n > 0", "None") in found
    assert ("R.other", "constant", "True", "False") in found
    # The docstring is left alone.
    assert not any("Docstrings" in b for _, _, b, _ in found)
    # Only R.length contains a changed line.
    assert all(
        i["in_fix"] == (i["function"] == "R.length") for _, i in mutants
    )
    # Everything outside the change keeps its exact text.
    for mutant, _ in mutants:
        assert mutant.count("\n") == SRC.count("\n")


def test_only_wanted_functions_are_mutated():
    mutants = make_mutants(SRC, "m.py", {"R.other"}, PATCH)
    assert {i["function"] for _, i in mutants} == {"R.other"}


def test_script_runs_each_mutant_and_restores_the_file():
    task = {
        "patch": "diff --git a/m.py b/m.py\n",
        "F2P": ["t.py::test_a"],
        "P2P": [],
    }
    index = [
        {"id": "m000", "file": None},
        {"id": "m001", "file": "m.py"},
    ]
    text = script(task, index, "baseline", 30)
    assert "git apply /mut/test.patch /mut/gold.patch" in text
    assert "cp /mut/m001.py m.py" in text
    assert "timeout 30 python /check/live.py /mut/baseline/spec.py" in text
    assert "/mut/baseline/m001.json /testbed m -- t.py::test_a" in text
    assert text.index("cp /mut/m001.py") < text.index("cp /tmp/fixed/m.py")


def clause(text, by_test):
    """A clause's result, from {test: (held, violated)}."""
    return {
        "function": "f",
        "text": text,
        "violated": sum(v for _, v in by_test.values()),
        "violated_by": {t: v for t, (_, v) in by_test.items() if v},
        "by_test": {
            t: {"held": h, "violated": v} for t, (h, v) in by_test.items()
        },
    }


def write(folder, reports):
    for mid, (clauses, failed) in reports.items():
        report = {"clauses": clauses, "failed_tests": failed}
        (folder / f"{mid}.json").write_text(json.dumps(report))


def test_score(tmp_path):
    write(
        tmp_path,
        {
            "m000": ([clause("Ensures(x)", {"t1": (1, 0)})], []),
            "m001": ([clause("Ensures(x)", {"t1": (0, 1)})], []),  # spec only
            "m002": ([clause("Ensures(x)", {"t1": (1, 0)})], ["t.py::a"]),
        },
    )
    index = [
        {"id": "m000", "in_fix": None},
        {"id": "m001", "in_fix": True},
        {"id": "m002", "in_fix": False},
        {"id": "m003", "in_fix": False},  # no result: broken
    ]
    s = score(index, tmp_path)
    assert s["control"]["spec"] is False
    assert s["groups"]["all"] == {
        "mutants": 3,
        "broken": 1,
        "spec": 1,
        "tests": 1,
        "spec_not_tests": 1,
        "tests_not_spec": 1,
    }
    assert s["groups"]["in the fix"]["spec"] == 1
    assert s["groups"]["outside the fix"]["tests"] == 1


def test_mutants_are_caught_call_by_call(tmp_path):
    """A clause that already fails on the control, on call t2, still counts
    when it newly fails on another call, t1. Failing again on t2 doesn't."""
    write(
        tmp_path,
        {
            "m000": ([clause("Ensures(a)", {"t1": (1, 0), "t2": (0, 1)})], []),
            "m001": ([clause("Ensures(a)", {"t1": (1, 0), "t2": (0, 1)})], []),
            "m002": ([clause("Ensures(a)", {"t1": (0, 1), "t2": (0, 1)})], []),
        },
    )
    index = [
        {"id": "m000", "in_fix": None},
        {"id": "m001", "in_fix": True},
        {"id": "m002", "in_fix": True},
    ]
    s = score(index, tmp_path)
    assert s["disagreements"] == [
        {"clause": "f: Ensures(a)", "violated_by": {"t2": 1}, "examples": {}}
    ]
    assert [m["spec"] for m in s["mutants"][1:]] == [False, True]


def test_compare_puts_each_call_in_one_cell():
    from check.compare import compare

    fixed = {
        "c": clause("c", {"get": (1, 0), "post": (0, 1), "x": (1, 0)}),
    }
    buggy = {
        "c": clause("c", {"get": (0, 1), "post": (1, 0), "y": (1, 0)}),
    }
    cells = compare(buggy, fixed)["c"]
    assert cells["catches the bug"] == ["get"]
    assert cells["disagrees with the fix"] == ["post"]
    assert cells["one side only"] == ["x", "y"]


def test_call_text_shows_generated_lines():
    from check.mutation import call_text

    calls = ["import x", "x.f(1)", "x.f(None)"]
    assert call_text("generated::3", calls) == "generated::3  x.f(None)"
    assert call_text("t.py::test_a", calls) == "t.py::test_a"


def test_script_with_inputs_passes_the_calls_to_the_referee():
    task = {
        "patch": "diff --git a/m.py b/m.py\n",
        "F2P": ["t.py::test_a"],
        "P2P": [],
    }
    index = [{"id": "m000", "file": None}]
    text = script(task, index, "baseline+inputs", 30, inputs=True)
    assert "/testbed m /mut/calls.py -- t.py::test_a -q" in text
    assert "/mut/baseline+inputs/spec.py" in text


TWO_PIECES = """diff --git a/models.py b/models.py
--- a/models.py
+++ b/models.py
@@ -1,6 +1,7 @@
 def prepare_content_length(self, body):
-    self.headers['Content-Length'] = '0'
     if body is not None:
         self.headers['Content-Length'] = str(len(body))
+    elif self.method not in ('GET', 'HEAD'):
+        self.headers['Content-Length'] = '0'
     return None
"""

FIXED = """def prepare_content_length(self, body):
    if body is not None:
        self.headers['Content-Length'] = str(len(body))
    elif self.method not in ('GET', 'HEAD'):
        self.headers['Content-Length'] = '0'
    return None
"""


def test_patch_pieces():
    pieces = patch_pieces(TWO_PIECES, "models.py")
    assert pieces == [
        (2, ["    self.headers['Content-Length'] = '0'\n"], []),
        (
            4,
            [],
            [
                "    elif self.method not in ('GET', 'HEAD'):\n",
                "        self.headers['Content-Length'] = '0'\n",
            ],
        ),
    ]


def test_revert_mutants_undo_one_piece_each():
    (first, info1), (second, info2) = revert_mutants(
        FIXED, "models.py", TWO_PIECES
    )
    # Undo the removal: the line at the top is back, the elif stays.
    assert first.splitlines()[1] == "    self.headers['Content-Length'] = '0'"
    assert "elif self.method not in" in first
    # Undo the addition: no line at the top, and no elif.
    assert "elif" not in second
    assert second.count("'0'") == 0
    assert info1["kind"] == info2["kind"] == "revert"
    assert info1["function"] == "prepare_content_length"
    assert info2["after"] == ""  # nothing to put back
