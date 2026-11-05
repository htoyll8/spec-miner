import re

import pytest

from mining.evaluate import evaluate
from mining.grammar import (
    OPS,
    Const,
    Node,
    Var,
    code,
    methods_named,
    size,
    type_context,
    type_of,
    typeof,
)

# One sample value per type, used to try every operator.
SAMPLE = {
    "int": 1,
    "float": 1.5,
    "bool": True,
    "str": "a-b",
    "bytes": b"a-b",
    "list[str]": ["a", "b"],
    "list[int]": [1, 2],
}


def all_signatures():
    for name, op in OPS.items():
        for args, out in op.sigs:
            yield name, args, out


@pytest.mark.parametrize("name,args,out", list(all_signatures()))
def test_code_runs_as_python_and_matches_evaluate(name, args, out):
    """A row's code and fn agree, so a rule's text means what was checked."""
    env = {f"x{i}": SAMPLE[t] for i, t in enumerate(args)}
    e = Node(name, tuple(Var(f"x{i}") for i in range(len(args))))
    op = OPS[name]
    if op.ok and not op.ok(*env.values()):
        pytest.skip("undefined on the sample values")
    assert eval(code(e), {}, dict(env)) == evaluate(env, e)


@pytest.mark.parametrize("name,args,out", list(all_signatures()))
def test_arity_matches_code(name, args, out):
    """The {i} slots in code match the arity, unlike Scala's two tables."""
    slots = {int(i) for i in re.findall(r"\{(\d+)\}", OPS[name].code)}
    assert slots == set(range(len(args)))


@pytest.mark.parametrize("name,args,out", list(all_signatures()))
def test_result_type_matches_value(name, args, out):
    env = {f"x{i}": SAMPLE[t] for i, t in enumerate(args)}
    e = Node(name, tuple(Var(f"x{i}") for i in range(len(args))))
    op = OPS[name]
    if op.ok and not op.ok(*env.values()):
        pytest.skip("undefined on the sample values")
    assert type_of({k: typeof(v) for k, v in env.items()}, e) == out
    # None, since an empty list has no element type.
    assert typeof(evaluate(env, e)) in (out, None)


def test_typeof():
    assert typeof(3) == "int"
    # bool, not int, although bool is a subclass of int.
    assert typeof(True) == "bool"
    assert typeof("x") == "str"
    assert typeof(b"x") == "bytes"
    assert typeof(["a"]) == "list[str]"
    assert typeof([1]) == "list[int]"
    assert typeof([]) is None
    assert typeof({"a": 1}) is None


def test_type_of_rejects_wrong_types():
    ctx = {"s": "str", "n": "int"}
    assert type_of(ctx, Node("LENGTH", (Var("s"),))) == "int"
    assert type_of(ctx, Node("LENGTH", (Var("n"),))) is None
    assert type_of(ctx, Node("STRINDEX", (Var("s"), Var("n")))) == "str"
    assert type_of(ctx, Node("STRINDEX", (Var("n"), Var("s")))) is None


def test_type_context_leaves_out_mixed_types():
    calls = [{"a": 1, "b": "x"}, {"a": 2, "b": 3}]
    assert type_context(calls) == {"a": "int"}


def test_size_counts_every_node():
    assert size(Var("s")) == 1
    assert size(Const(0)) == 1
    assert size(Node("LENGTH", (Var("s"),))) == 2
    e = Node("STRSUBSTR", (Var("s"), Const(0), Node("LENGTH", (Var("s"),))))
    assert size(e) == 5


def test_code():
    e = Node("ARRINDEX", (Node("SPLIT", (Var("str"), Var("sep"))), Const(0)))
    assert code(e) == "str.split(sep)[0]"


def test_methods_named():
    assert methods_named("strip") == [
        "str.strip/0",
        "str.strip/1",
        "bytes.strip/0",
        "bytes.strip/1",
    ]
    # count exists on str, bytes and, through tuple, on lists.
    assert methods_named("count") == [
        "str.count/0",
        "str.count/1",
        "bytes.count/0",
        "bytes.count/1",
        "list.count/0",
        "list.count/1",
    ]
    # append would change a list in place, so lists don't get it.
    assert methods_named("append") == []
    assert methods_named("__len__") == []
    assert methods_named("no_such_method") == []


def test_method_code_matches_evaluate():
    env = {"s": " a-b ", "sep": "-"}
    for name, kids, expected in (
        ("str.strip/0", (Var("s"),), "a-b"),
        ("str.count/1", (Var("s"), Var("sep")), 1),
    ):
        e = Node(name, kids)
        assert evaluate(env, e) == expected
        assert eval(code(e), {}, dict(env)) == expected
    assert code(Node("str.strip/0", (Var("s"),))) == "s.strip()"


def test_method_result_type_is_not_known_statically():
    e = Node("str.strip/0", (Var("s"),))
    assert type_of({"s": "str"}, e) is None
    assert type_of({"s": "int"}, e) is None  # and wrong receivers don't fit
