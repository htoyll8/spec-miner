import json

import tree_sitter_python
from tree_sitter import Language, Parser

from mining.extract import vocab_for, write_grammar
from mining.grammar import Const, Var
from mining.vocab import Vocab, load


def test_vocab_for_reads_the_function():
    src = b'''
def short(amount, sep="-"):
    """Docstrings are skipped."""
    if len(amount) > 3 and amount.startswith("$"):
        return amount.split(sep)[0] + "k"
    if amount == "a string well over twenty characters":
        return ""
    return amount
'''
    fn = (
        Parser(Language(tree_sitter_python.language()))
        .parse(src)
        .root_node.children[0]
    )
    v = vocab_for(src, fn, ["amount", "sep", "result"])
    assert v.leaves == [
        Var("amount"),
        Var("sep"),
        Var("result"),
        Const("-"),
        Const(3),
        Const("$"),
        Const(0),
        Const("k"),
        Const("a string well over twenty characters"),
        Const(""),
    ]
    for op in (
        "LENGTH",
        "STARTSWITH",
        "SPLIT",
        "STRINDEX",
        "ARRINDEX",
        "PLUS",
        "CONCAT",
    ):
        assert op in v.ops
    for op in ("LOWER", "STRREPLACE", "IFTHENELSE"):
        assert op not in v.ops


def test_write_grammar_round_trips(tmp_path):
    vocab = Vocab(
        [Var("s"), Const("-"), Const(3), Const(True)],
        {"LENGTH": 0.0, "SPLIT": 0.5},
    )
    path = write_grammar(tmp_path, "psf__requests-1142", "short", vocab)
    assert path == tmp_path / "2_grammar/psf__requests-1142/short.json"
    d = json.loads(path.read_text())
    assert d["task"] == "psf__requests-1142"
    assert d["function"] == "short"
    assert d["ops"]["LENGTH"]["code"] == "len({0})"
    assert load(path) == vocab


def test_string_literals_are_parsed_by_python():
    src = b"""
def f(s, x):
    if s == "" or s == r"\\d" or s == "a\\tb" or s == f"{x}!":
        return s.split(b"-")
"""
    fn = (
        Parser(Language(tree_sitter_python.language()))
        .parse(src)
        .root_node.children[0]
    )
    consts = [x.value for x in vocab_for(src, fn, ["s"]).leaves[1:]]
    # The empty string is kept, escapes are decoded, and the raw string
    # keeps its backslash. The f-string is skipped, and bytes are kept.
    assert consts == ["", "\\d", "a\tb", b"-"]


def test_number_literals_are_parsed_by_python():
    src = b"""
def f(x):
    \"\"\"A docstring, which is skipped.\"\"\"
    sql = \"\"\"select\"\"\"
    return x in (0x10, 1_000, 0.5, 1, 1.0, 16)
"""
    fn = (
        Parser(Language(tree_sitter_python.language()))
        .parse(src)
        .root_node.children[0]
    )
    consts = [x.value for x in vocab_for(src, fn, ["x"]).leaves[1:]]
    # 0x10 is 16, so the later 16 is a duplicate. 1 and 1.0 are both kept.
    # A triple-quoted string that isn't a docstring is kept.
    assert consts == ["select", 16, 1000, 0.5, 1, 1.0]
    assert [type(c) for c in consts[-2:]] == [int, float]


def test_other_methods_become_operators():
    src = b"def f(s):\n    return s.strip().count('-') + s.rjust(3)\n"
    fn = (
        Parser(Language(tree_sitter_python.language()))
        .parse(src)
        .root_node.children[0]
    )
    ops = vocab_for(src, fn, ["s"]).ops
    for name in ("str.strip/0", "str.strip/1", "str.count/1", "list.count/1"):
        assert name in ops
    assert "str.rjust/1" in ops
