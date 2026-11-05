from mining.enumerate import enumerate_terms, show
from mining.grammar import Var, code
from mining.vocab import Vocab
from tracing.objects import expand, fields


class Node:
    def __init__(self, value, next=None):
        self.value = value
        self.next = next


class Grid:
    size = 10  # a class attribute, which dir() also finds

    def __init__(self, width, cells):
        self.width = width
        self.cells = cells
        self._hidden = 1
        self.__mangled = 2

    @property
    def area(self):
        raise AssertionError("properties must not run")

    def add(self, x):
        self.cells.append(x)


def test_fields_reads_data_only():
    g = Grid(3, ["a"])
    # Private and name-mangled fields are kept. Dunder names are not.
    assert fields(g) == {
        "_Grid__mangled": 2,
        "_hidden": 1,
        "cells": ["a"],
        "size": 10,
        "width": 3,
    }


def test_expand_adds_dotted_fields():
    env = {"self": Grid(3, ["a"]), "x": "b"}
    out = expand(env, depth=1)
    assert out["self.width"] == 3
    assert out["self.cells"] == ["a"]
    assert out["x"] == "b"
    assert "self.area" not in out
    assert "self.add" not in out


def test_expand_follows_fields_up_to_depth():
    env = {"head": Node(1, Node(2, Node(3)))}
    assert "head.next.value" not in expand(env, depth=1)
    out = expand(env, depth=2)
    assert out["head.next.value"] == 2
    assert "head.next.next.value" not in out


def test_expand_stops_at_cycles():
    a = Node(1)
    a.next = a
    out = expand({"a": a}, depth=5)
    assert out["a.value"] == 1
    assert "a.next.value" not in out


def test_fields_become_leaves():
    """A rule about a field, found from two recorded calls."""
    calls = [
        expand({"self": Grid(3, ["a", "b", "c"])}, depth=1),
        expand({"self": Grid(2, ["d", "e"])}, depth=1),
    ]
    vocab = Vocab(
        [Var("self.width"), Var("self.cells")],
        dict.fromkeys(["LENGTH", "EQ"], 0.0),
    )
    bank = enumerate_terms(vocab, calls, 4)
    print(show(bank))
    found = {code(t.expr) for level in bank.values() for t in level}
    assert "(len(self.cells) == self.width)" in found
