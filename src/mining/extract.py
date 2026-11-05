"""Reads a function's vocabulary from its source with tree-sitter.

The counterpart of SpecFuzzer's GrammarExtractor, but it reads the function's
source instead of the class's field types.
"""

import ast
from pathlib import Path

from .grammar import Const, Var, methods_named
from .vocab import Vocab, save

GRAMMAR_DIR = "2_grammar"

# Comparisons and connectives are always on, since every rule needs them.
ALWAYS = ("EQ", "NE", "LT", "LE", "GT", "GE", "NOT", "AND", "OR")
# A method or builtin called in the source -> the operators it turns on.
METHOD_OPS = {
    "len": ("LENGTH",),
    "find": ("INDEXOF",),
    "index": ("INDEXOF",),
    "lower": ("LOWER",),
    "upper": ("UPPER",),
    "title": ("TITLE",),
    "split": ("SPLIT",),
    "replace": ("STRREPLACE",),
    "startswith": ("STARTSWITH",),
    "endswith": ("ENDSWITH",),
    "isalpha": ("ISALPHA",),
    "isdigit": ("ISDIGIT",),
    "join": ("ARRJOIN",),
    "append": ("ARRAPPEND",),
    "list": ("STRTOLIST",),
}
# Syntax in the source -> the operators it turns on.
SYNTAX_OPS = {
    "+": ("PLUS", "CONCAT"),
    "-": ("MINUS",),
    "*": ("TIMES",),
    "in": ("IN",),
    "subscript": ("STRINDEX", "ARRINDEX"),
    "slice": ("STRSUBSTR", "REVERSESTRSUBSTR"),
    "conditional_expression": ("IFTHENELSE",),
}


def _walk(n):
    yield n
    for c in n.children:
        yield from _walk(c)


def _add_const(leaves, v):
    """Add Const(v) unless it's there. Types count, since 1 == 1.0."""
    if not any(
        isinstance(x, Const) and type(x.value) is type(v) and x.value == v
        for x in leaves
    ):
        leaves.append(Const(v))


def vocab_for(src, fn_node, names):
    """Build a function's Vocab, its leaves and operators, from its source.

    Leaves: a Var for each name, plus the string, bytes, integer and float
    literals in the source. names are the variables the trace recorded for the
    function, such as its inputs and result.

    Operators: the comparisons and connectives in ALWAYS, plus the ones the
    source turns on through METHOD_OPS (calls like .split), SYNTAX_OPS
    (syntax like +, x[i] and slicing), and any other method it calls on a
    type in METHODS, like .strip (see methods_named).
    """

    def text(n):
        start, end = n.start_byte, n.end_byte
        return src[start:end].decode(errors="replace")

    # Turn every variable name into a leaf.
    leaves = [Var(x) for x in names]
    ops = dict.fromkeys(ALWAYS, 0.0)

    for n in _walk(fn_node):
        # A string alone on a line, like a docstring, is never used as a
        # value: Python evaluates it and throws it away.
        if n.type == "string" and n.parent.type != "expression_statement":
            # Python parses the literal: prefixes, quotes and escapes.
            try:
                s = ast.literal_eval(text(n))
            except (ValueError, SyntaxError):
                continue  # an f-string, which has no fixed value
            if isinstance(s, (str, bytes)):
                _add_const(leaves, s)
        elif n.type in ("integer", "float"):
            # Python parses the number, so 0x10 and 1_000 are kept too.
            _add_const(leaves, ast.literal_eval(text(n)))
        elif n.type == "call":
            f = n.child_by_field_name("function")
            name = (
                text(f.child_by_field_name("attribute"))
                if f.type == "attribute"
                else text(f)
            )
            if name in METHOD_OPS:
                ops.update(dict.fromkeys(METHOD_OPS[name], 0.0))
            else:
                # Any other method in METHODS, like .strip, as in proto4.py.
                ops.update(dict.fromkeys(methods_named(name), 0.0))
        elif n.type in ("binary_operator", "comparison_operator"):
            for c in n.children:
                ops.update(dict.fromkeys(SYNTAX_OPS.get(c.type, ()), 0.0))
        elif n.type in SYNTAX_OPS:
            ops.update(dict.fromkeys(SYNTAX_OPS[n.type], 0.0))
    return Vocab(leaves, ops)


def grammar_path(run_dir, task, function):
    """Where a function's grammar goes: <run>/2_grammar/<task>/<fn>.json."""
    return Path(run_dir) / GRAMMAR_DIR / task / f"{function}.json"


def write_grammar(run_dir, task, function, vocab):
    """Save vocab in the run's results folder and return the file's path."""
    path = grammar_path(run_dir, task, function)
    save(vocab, path, task=task, function=function)
    return path
