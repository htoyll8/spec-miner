from mining.enumerate import cost_combinations, enumerate_terms, show
from mining.grammar import Const, Var, code, size, type_context, type_of
from mining.vocab import Vocab


def all_terms(bank):
    return [t for level in bank.values() for t in level]


def run(vocab, ctxs, limit, prune="self_loop"):
    """Enumerate and print every term. See the output with pytest -s."""
    bank = enumerate_terms(vocab, ctxs, limit, prune)
    print(show(bank))
    return bank


def test_cost_combinations():
    assert cost_combinations(1, 1) == [(1,)]
    assert cost_combinations(3, 2) == [(1, 2), (2, 1)]
    assert cost_combinations(4, 3) == [(1, 1, 2), (1, 2, 1), (2, 1, 1)]
    assert cost_combinations(1, 2) == []


def test_first_four_chars():
    """The Scala test "First four chars" finds the same two programs."""
    ctxs = [
        {"str": s, "sep": "-"}
        for s in [
            "EC1A-1BB-AC12",
            "DN55-IPT-AB34",
            "ASCN-1ZZ-CD89",
            "STHL-1ZZ-P065",
        ]
    ]
    vocab = Vocab(
        [Var("str"), Var("sep"), Const(0), Const(1), Const(2), Const(4)],
        dict.fromkeys(
            ["SPLIT", "STRSUBSTR", "LENGTH", "STRINDEX", "ARRINDEX"], 0.0
        ),
    )
    bank = run(vocab, ctxs, 5)
    target = ("EC1A", "DN55", "ASCN", "STHL")
    found = {code(t.expr) for t in all_terms(bank) if t.values == target}
    assert found == {"str.split(sep)[0]", "str[0:4]"}


def test_each_term_has_its_level_size_and_type():
    ctxs = [{"s": "Ab", "n": 1}, {"s": "cd-e", "n": 2}]
    vocab = Vocab(
        [Var("s"), Var("n"), Const("-"), Const(0)],
        dict.fromkeys(["LENGTH", "STRINDEX", "PLUS", "EQ", "SPLIT"], 0.0),
    )
    bank = run(vocab, ctxs, 4)
    tctx = type_context(ctxs)
    for level, terms in bank.items():
        for t in terms:
            assert size(t.expr) == level
            assert type_of(tctx, t.expr) == t.type


def test_no_constant_only_terms():
    ctxs = [{"s": "ab"}, {"s": "cd"}]
    vocab = Vocab(
        [Var("s"), Const(1), Const(2)],
        dict.fromkeys(["PLUS", "EQ", "LT"], 0.0),
    )
    for t in all_terms(run(vocab, ctxs, 3)):
        assert "s" in code(t.expr) or t.expr in vocab.leaves


def test_terms_with_equal_values_are_all_kept():
    """No merging by value: both always-true conditions stay."""
    ctxs = [{"s": "ab", "n": 1}, {"s": "cd", "n": 2}]
    vocab = Vocab(
        [Var("s"), Var("n"), Const(0), Const("")],
        dict.fromkeys(["NE", "GT"], 0.0),
    )
    found = {code(t.expr) for t in all_terms(run(vocab, ctxs, 3))}
    assert "(n > 0)" in found
    assert "(s != '')" in found


def test_self_loop_filter():
    """lower() of a lowercase string is dropped, unless pruning is off."""
    ctxs = [{"s": "ab"}, {"s": "cd"}]
    vocab = Vocab([Var("s")], {"LOWER": 0.0})
    pruned = {code(t.expr) for t in all_terms(run(vocab, ctxs, 2))}
    kept = {code(t.expr) for t in all_terms(run(vocab, ctxs, 2, prune="none"))}
    assert "s.lower()" not in pruned
    assert "s.lower()" in kept


def test_undefined_on_any_call_is_dropped():
    """s[2] fails on "ab", so it's left out, like Scala's verifyMultipleCtx."""
    ctxs = [{"s": "abc"}, {"s": "ab"}]
    vocab = Vocab([Var("s"), Const(1), Const(2)], {"STRINDEX": 0.0})
    found = {code(t.expr) for t in all_terms(run(vocab, ctxs, 3))}
    assert "s[1]" in found
    assert "s[2]" not in found


def test_methods_take_their_type_from_values():
    """s.strip() is a str, s.isupper() a bool, s.encode() bytes."""
    ctxs = [{"s": " AB "}, {"s": "c"}]
    vocab = Vocab(
        [Var("s"), Const("A")],
        dict.fromkeys(
            ["str.strip/0", "str.isupper/0", "str.count/1", "str.encode/0"],
            0.0,
        ),
    )
    found = {
        code(t.expr): (t.type, t.values)
        for t in all_terms(run(vocab, ctxs, 3))
    }
    assert found["s.strip()"] == ("str", ("AB", "c"))
    assert found["s.isupper()"] == ("bool", (True, False))
    assert found["s.count('A')"] == ("int", (1, 0))
    assert found["s.encode()"] == ("bytes", (b" AB ", b"c"))
