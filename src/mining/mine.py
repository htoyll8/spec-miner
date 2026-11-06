"""Mines candidate rules for each traced function: step 3 of a run.

Reads <run>/1_traces/<task>/ (flat.pkl, src/, tests.json), and for each
function in the changed files: reads its grammar from the source (the
function's own code, or its whole file, by --scope), saves it to 2_grammar/,
enumerates terms over its calls, and keeps the rules that hold.

The variables for one call are its arguments and their fields before the
call, by name (self.method); the ones that changed on some call, after it,
as after(self.headers); and result.

Rules:
  pre      a condition on the inputs, true on every call
  post     a condition using after(...) or result, true on every call that
           returned
  implies  a => b, where a is a condition on the inputs that's true on at
           least min_support calls but not all of them, and b is true on all
           of those calls but not on every call. b is "raises E" if those
           calls all raised E.

Writes <run>/3_candidates/<task>.json.

    python -m mining.mine --run RUN --task ID --max-size N --min-support N
        --f2p include|exclude --scope function|file
"""

import argparse
import json
import pickle
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Parser

from .enumerate import enumerate_terms
from .extract import vocab_for, write_grammar
from .grammar import Node, Var, code
from .vocab import Vocab

PARSER = Parser(Language(tree_sitter_python.language()))
TRACES_DIR = "1_traces"
CANDIDATES_DIR = "3_candidates"
MISSING = object()


def after(name):
    return f"after({name})"


def is_output(name):
    """Whether a variable is about the call's outcome, not its inputs."""
    return name.startswith("after(") or name.split(".")[0] == "result"


def uses_output(e):
    if isinstance(e, Var):
        return is_output(e.name)
    return isinstance(e, Node) and any(uses_output(c) for c in e.children)


def contexts(calls):
    """One set of variables per call, and the names that changed.

    A variable gets an after(...) version only if it changed on some call.
    """
    changed = sorted(
        {
            k
            for c in calls
            for k, v in c["after"].items()
            if c["before"].get(k, MISSING) != v
        }
    )
    envs = []
    for c in calls:
        env = dict(c["before"])
        for k in changed:
            if k in c["after"]:
                env[after(k)] = c["after"][k]
        env.update(c["result"])
        envs.append(env)
    return envs, changed


def function_nodes(src):
    """{line: tree-sitter node} for every function in src.

    Each function is keyed by its def line and, if decorated, its first
    decorator's line, since the tracer's line can be either.
    """
    out = {}

    def walk(n):
        if n.type == "function_definition":
            out[n.start_point[0] + 1] = n
            if (
                n.parent is not None
                and n.parent.type == "decorated_definition"
            ):
                out[n.parent.start_point[0] + 1] = n
        for c in n.children:
            walk(c)

    root = PARSER.parse(src).root_node
    walk(root)
    out["file"] = root
    return out


def is_f2p(test, f2p):
    """Whether a call's test is one of the F2P tests. Sympy's F2P names are
    bare, like test_a, so a test ending in ::test_a matches too."""
    return test is not None and any(
        test == t or test.endswith("::" + t) for t in f2p
    )


def bools(bank, keep):
    """The true-or-false terms in a bank that keep accepts."""
    return [
        t
        for level in bank.values()
        for t in level
        if t.type == "bool" and keep(t.expr)
    ]


def mine_function(calls, vocab, max_size, min_support):
    """The pre, post and implies rules for one function's calls."""
    envs, _ = contexts(calls)
    n = len(calls)
    rules = []

    # Conditions on the inputs, over every call.
    inputs = Vocab([x for x in vocab.leaves if not uses_output(x)], vocab.ops)
    conds = bools(enumerate_terms(inputs, envs, max_size), lambda e: True)
    for a in conds:
        if all(v is True for v in a.values):
            rules.append({"kind": "pre", "rule": code(a.expr), "support": n})

    # Outcomes, over the calls that returned.
    returned = [i for i, c in enumerate(calls) if not c["raised"]]
    outcomes = []
    if returned:
        bank = enumerate_terms(vocab, [envs[i] for i in returned], max_size)
        outcomes = bools(bank, uses_output)
    position = {i: j for j, i in enumerate(returned)}
    for b in outcomes:
        if all(v is True for v in b.values):
            rule = {"kind": "post", "rule": code(b.expr)}
            rules.append({**rule, "support": len(returned)})

    # a => b, where a holds on some calls but not all.
    for a in conds:
        on = [i for i, v in enumerate(a.values) if v is True]
        if len(on) < min_support or len(on) == n:
            continue
        raised = {calls[i]["raised"] for i in on}
        if len(raised) == 1 and None not in raised:
            rule = f"{code(a.expr)} => raises {raised.pop()}"
            rules.append({"kind": "implies", "rule": rule, "support": len(on)})
            continue
        if any(calls[i]["raised"] for i in on):
            continue
        for b in outcomes:
            if all(v is True for v in b.values):
                continue  # already a post rule
            if all(b.values[position[i]] is True for i in on):
                rule = f"{code(a.expr)} => {code(b.expr)}"
                rules.append(
                    {"kind": "implies", "rule": rule, "support": len(on)}
                )
    return rules


def mine_task(run, task, max_size, min_support, include_f2p, scope):
    """scope is "function" (the grammar comes from the function's own code)
    or "file" (from its whole file). The variables are the function's either
    way."""
    trace = Path(run) / TRACES_DIR / task
    with open(trace / "flat.pkl", "rb") as f:
        flat = pickle.load(f)
    f2p = json.loads((trace / "tests.json").read_text())["F2P"]
    calls = [
        c for c in flat["calls"] if include_f2p or not is_f2p(c["test"], f2p)
    ]
    by_function = {}
    for c in calls:
        by_function.setdefault(tuple(c["function"]), []).append(c)

    sources = {}
    out = {}
    for (path, line, name), fcalls in sorted(by_function.items()):
        entry = {"file": path, "line": line, "calls": len(fcalls)}
        out[name] = entry
        src_path = trace / "src" / path
        if not src_path.exists():
            entry["skipped"] = "no source: only the changed files are copied"
            continue
        if len(fcalls) < min_support:
            entry["skipped"] = f"fewer than {min_support} calls"
            continue
        if path not in sources:
            src = src_path.read_bytes()
            sources[path] = (src, function_nodes(src))
        src, nodes = sources[path]
        if line not in nodes:
            entry["skipped"] = "function not found in the source"
            continue
        envs, changed = contexts(fcalls)
        names = sorted({k for env in envs for k in env})
        node = nodes[line] if scope == "function" else nodes["file"]
        vocab = vocab_for(src, node, names)
        write_grammar(run, task, name, vocab)
        entry["variables"] = names
        entry["changed"] = changed
        entry["rules"] = mine_function(fcalls, vocab, max_size, min_support)

    result = {
        "task": task,
        "settings": {
            "max_size": max_size,
            "min_support": min_support,
            "f2p": "include" if include_f2p else "exclude",
            "scope": scope,
        },
        "functions": out,
    }
    path = Path(run) / CANDIDATES_DIR / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, default=repr) + "\n")
    return path, result


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--max-size", type=int, required=True)
    ap.add_argument("--min-support", type=int, required=True)
    ap.add_argument("--f2p", choices=("include", "exclude"), required=True)
    ap.add_argument("--scope", choices=("function", "file"), required=True)
    args = ap.parse_args()
    for task in args.task.split(","):
        path, result = mine_task(
            args.run,
            task,
            args.max_size,
            args.min_support,
            args.f2p == "include",
            args.scope,
        )
        print(path)
        for name, f in result["functions"].items():
            rules = f.get("rules")
            if rules is None:
                print(f"  {name}: skipped, {f['skipped']}")
                continue
            kinds = {k: sum(r["kind"] == k for r in rules) for k in KINDS}
            print(f"  {name}: {f['calls']} calls, {kinds}")


KINDS = ("pre", "post", "implies")

if __name__ == "__main__":
    main()
