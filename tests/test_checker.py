"""The checker on a small spec, with recorded calls built here.

The classes below play the repo's code: the checker imports this module by
the recorded file name, test_checker.py, so they're the "real" classes.
"""

from check.checker import check, is_stub, match_params
from tracing.tracer import save


class Headers(dict):
    pass


class Prepared:
    def __init__(self, method, headers):
        self.method = method
        self.headers = headers


SPEC = '''
from nagini_contracts.contracts import *

class Headers(dict):
    """A stub the spec defines, which must not replace the real one."""

def no_body(b):
    return b is None

def abstract(x) -> bool:
    ...

class Prepared:
    def prepare_content_length(self, body):
        Requires(self.headers is not None)
        Ensures(Implies(no_body(body), 'Content-Length' not in self.headers))
        Ensures(Implies(self.method == 'POST',
                        'Content-Length' in self.headers))
        Ensures(isinstance(self.headers, Headers))
        Ensures(abstract(body))
        Ensures(len(self.headers) == len(Old(self.headers)) + 1)
        Ensures(Result() is None)
'''


def call(before_headers, after_headers, body=None, raised=None):
    return {
        "function": ["test_checker.py", 1, "Prepared.prepare_content_length"],
        "test": "t.py::test_get",
        "before": {
            "self": save(Prepared("GET", Headers(before_headers))),
            "body": save(body),
        },
        "after": {
            "self": save(Prepared("GET", Headers(after_headers))),
            "body": save(body),
        },
        "result": save(None),
        "raised": raised,
    }


def clauses(report):
    return {c["text"]: c for c in report["clauses"]}


def counts(c):
    keys = ("held", "violated", "vacuous", "unresolved")
    return tuple(c[k] for k in keys)


def test_each_kind_of_outcome():
    # The buggy behavior: a GET with no body gets Content-Length.
    report = check(SPEC, [call({}, {"Content-Length": "0"})])
    assert report["functions"]["Prepared.prepare_content_length"] == {
        "matched": "Prepared.prepare_content_length",
        "calls": 1,
        "helper_problems": {},
    }
    c = clauses(report)
    #                                         held violated vacuous unresolved
    assert counts(c["Requires(self.headers is not None)"]) == (1, 0, 0, 0)
    text = (
        "Ensures(Implies(no_body(body), 'Content-Length' not in self.headers))"
    )
    assert counts(c[text]) == (0, 1, 0, 0)
    assert c[text]["example"]["test"] == "t.py::test_get"
    post = [t for t in c if "self.method == 'POST'" in t][0]
    assert counts(c[post]) == (0, 0, 1, 0)  # it's a GET, so vacuous
    # The real Headers class, not the spec's stub.
    assert counts(c["Ensures(isinstance(self.headers, Headers))"]) == (
        1,
        0,
        0,
        0,
    )
    stub = c["Ensures(abstract(body))"]
    assert counts(stub) == (0, 0, 0, 1)
    assert "abstract" in list(stub["reasons"])[0]
    old = "Ensures(len(self.headers) == len(Old(self.headers)) + 1)"
    assert counts(c[old]) == (1, 0, 0, 0)
    assert counts(c["Ensures(Result() is None)"]) == (1, 0, 0, 0)


def test_fixed_behavior_holds():
    report = check(SPEC, [call({}, {})])
    c = clauses(report)
    text = (
        "Ensures(Implies(no_body(body), 'Content-Length' not in self.headers))"
    )
    assert counts(c[text]) == (1, 0, 0, 0)


def test_ensures_is_skipped_on_calls_that_raised():
    report = check(SPEC, [call({}, {"Content-Length": "0"}, raised="Boom")])
    c = clauses(report)
    assert c["Ensures(Result() is None)"]["skipped_raised"] == 1
    assert counts(c["Requires(self.headers is not None)"]) == (1, 0, 0, 0)


def test_unmatched_function_is_reported():
    spec = "def missing(x):\n    Ensures(x)\n"
    report = check(spec, [call({}, {})])
    assert report["functions"]["missing"]["problem"] == "no recorded calls"


def test_is_stub():
    import ast

    tree = ast.parse(
        "def a():\n    '''doc'''\n    ...\n"
        "def b(x):\n    Ensures(x)\n    pass\n"
        "def c(x):\n    return x\n"
    )
    assert [is_stub(f) for f in tree.body] == [True, True, False]


def test_match_params():
    assert match_params(["self", "body"], ["self", "body"]) == {
        "self": "self",
        "body": "body",
    }
    # Different names, same count: by position.
    assert match_params(["self", "b"], ["self", "body"]) == {
        "self": "self",
        "b": "body",
    }
    # Names that all match still work, even if the spec has fewer.
    assert match_params(["self"], ["self", "body"]) == {"self": "self"}
    # Different names and a different count can't be matched.
    assert match_params(["self", "x", "y"], ["self", "body"]) is None


def test_spec_code_takes_the_first_python_block():
    from check.run import spec_code

    md = "Here it is:\n```python\nx = 1\n```\nand\n```python\ny = 2\n```"
    assert spec_code(md) == "x = 1\n"
    assert spec_code("no code") is None


def test_check_inner_command_applies_the_same_patches():
    from check.run import inner_command

    buggy = inner_command("buggy", "issue.spec.py", "issue.buggy.json")
    fixed = inner_command("fixed", "issue.spec.py", "issue.fixed.json")
    assert "git apply /calls/test.patch &&" in buggy
    assert "git apply /calls/test.patch /calls/gold.patch &&" in fixed
    assert "/check/checker.py /work/issue.spec.py /calls/calls.pkl" in buggy


def test_live_command_imports_changed_modules_and_runs_tests():
    from check.run import live_command

    task = {
        "patch": "diff --git a/requests/models.py b/requests/models.py\n",
        "F2P": ["test_requests.py::T::test_a"],
        "P2P": ["test_requests.py::T::test_b"],
    }
    cmd = live_command(task, "fixed", "issue.spec.py", "issue.fixed.live.json")
    assert "git apply /work/test.patch /work/gold.patch &&" in cmd
    assert (
        "/check/live.py /work/issue.spec.py /work/issue.fixed.live.json"
        " /testbed requests.models -- test_requests.py::T::test_a" in cmd
    )
