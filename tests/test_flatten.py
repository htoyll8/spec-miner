from tracing.flatten import DROP, flatten_call, plain
from tracing.tracer import save


class Request:
    def __init__(self, method, headers):
        self.method = method
        self.headers = headers


class Headers(dict):
    """Like requests' case-insensitive dict: a dict subclass."""


def test_plain():
    assert plain(None) is None
    assert plain(b"x") == b"x"
    assert plain((1, "a")) == (1, "a")
    assert plain(Headers({"A": "1"})) == {"A": "1"}
    assert type(plain(Headers({"A": "1"}))) is dict
    assert plain(Request("GET", {})) is DROP
    assert plain([1, object()]) is DROP
    assert plain({1: "a"}) is DROP  # keys must be strings


def test_flatten_call_expands_before_after_and_result():
    before = Request("GET", Headers())
    after = Request("GET", Headers({"Content-Length": "0"}))
    call = {
        "function": ["requests/models.py", 388, "prepare_content_length"],
        "role": "changed",
        "test": "t.py::test_get",
        "phase": "call",
        "before": {"self": save(before), "body": save(None)},
        "after": {"self": save(after), "body": save(None)},
        "result": save(None),
        "raised": None,
    }
    flat = flatten_call(call, depth=1)
    assert flat["before"] == {
        "body": None,
        "self.headers": {},
        "self.method": "GET",
    }
    assert flat["after"]["self.headers"] == {"Content-Length": "0"}
    assert flat["result"] == {"result": None}
    assert flat["test"] == "t.py::test_get"


def test_flatten_call_skips_unsaved_and_raised():
    call = {
        "function": ["m.py", 1, "f"],
        "role": "changed",
        "test": None,
        "phase": None,
        "before": {"x": {"unsaved": "TypeError"}, "y": save(2)},
        "after": None,
        "result": None,
        "raised": "ValueError",
    }
    flat = flatten_call(call, depth=1)
    assert flat["before"] == {"y": 2}
    assert flat["after"] == {}
    assert flat["result"] == {}
    assert flat["raised"] == "ValueError"
