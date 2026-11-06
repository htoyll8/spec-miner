"""Makes small bugs, mutants, in the fixed code, to score specs by.

For each changed file, from <run>/1_traces_fixed/<task>/src/, every possible
one-place change of these kinds, in every function the tests called (from
the fixed trace's calls.pkl):

  compare    == and !=, < and >=, <= and >, in and not in, is and is not
  negate     if x / while x / assert x  ->  ... not (x)
  boolop     and and or
  constant   True and False, n -> n + 1, a non-empty string -> ''
  return     return x -> return None
  delete     a one-line statement -> pass

and first, one mutant per piece of the gold patch, undone on its own:

  revert     a run of the patch's removed and added lines, undone: its
             removed lines put back and its added lines taken out, with the
             rest of the fix left in place. These are half-fixed versions,
             tied to the issue.

Only the changed code is replaced, so the rest of the file keeps its exact
text. Mutant m000 changes nothing: no spec should catch it. The LLM never
sees any of this.

Writes <run>/6_mutants/<task>/: m001.py, ... and index.json, which says for
each mutant its file, function, line, kind, the code before and after, and
whether the gold patch changed that function (in_fix).

    python -m check.mutate --run RUN --task ID
"""

import argparse
import ast
import copy
import json
import pickle
import re
import warnings
from pathlib import Path

FIXED_TRACES = "1_traces_fixed"
MUTANTS_DIR = "6_mutants"

FLIP = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


def fix_lines(patch, path):
    """Line numbers in the fixed file that the gold patch adds, or next to
    where it removes, for one file. Context lines don't count."""
    out, current, n = set(), None, 0
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/", line)
        if m:
            current, n = m.group(1), 0
            continue
        m = re.match(r"^@@ -\S+ \+(\d+)", line)
        if m:
            n = int(m.group(1))
            continue
        if current != path or not n or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            out.add(n)
            n += 1
        elif line.startswith("-"):
            out.add(n)
        elif not line.startswith("\\"):
            n += 1
    return out


def functions(tree):
    """{qualified name: node} for every function in a module."""
    out = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + child.name] = child
                walk(child, prefix + child.name + ".<locals>.")

    walk(tree, "")
    return out


def segment(src, node):
    return ast.get_source_segment(src, node)


def offsets(src):
    """The character offset where each line starts."""
    starts, total = [0], 0
    for line in src.splitlines(keepends=True):
        total += len(line)
        starts.append(total)
    return starts


def span(starts, node):
    begin = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
    return begin, end


def is_docstring(node, parents):
    p = parents.get(node)
    return (
        isinstance(p, ast.Expr)
        and isinstance(parents.get(p), (ast.FunctionDef, ast.ClassDef))
        and parents[p].body[0] is p
    )


def mutations(src, fn):
    """(node, kind, replacement text) for every mutation inside fn."""
    parents = {c: p for p in ast.walk(fn) for c in ast.iter_child_nodes(p)}
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in FLIP:
                    new = copy.deepcopy(node)
                    new.ops[i] = FLIP[type(op)]()
                    out.append((node, "compare", ast.unparse(new)))
        elif isinstance(node, (ast.If, ast.While, ast.Assert)):
            text = segment(src, node.test)
            out.append((node.test, "negate", f"not ({text})"))
        elif isinstance(node, ast.BoolOp):
            new = copy.deepcopy(node)
            new.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            out.append((node, "boolop", f"({ast.unparse(new)})"))
        elif isinstance(node, ast.Constant) and not is_docstring(
            node, parents
        ):
            v = node.value
            if isinstance(v, bool):
                out.append((node, "constant", repr(not v)))
            elif isinstance(v, int):
                out.append((node, "constant", repr(v + 1)))
            elif isinstance(v, str) and v:
                out.append((node, "constant", "''"))
        elif isinstance(node, ast.Return) and node.value is not None:
            if not (
                isinstance(node.value, ast.Constant)
                and node.value.value is None
            ):
                out.append((node.value, "return", "None"))
        if (
            isinstance(node, (ast.Expr, ast.Assign, ast.AugAssign))
            and node.lineno == node.end_lineno
            and not is_docstring(getattr(node, "value", None), parents)
        ):
            out.append((node, "delete", "pass"))
    return out


def make_mutants(src, path, wanted, patch):
    """[(mutant source, info)] for the functions in wanted."""
    tree = ast.parse(src)
    starts = offsets(src)
    changed = fix_lines(patch, path)
    out = []
    for name, fn in sorted(functions(tree).items()):
        if name not in wanted:
            continue
        in_fix = any(fn.lineno <= n <= fn.end_lineno for n in changed)
        for node, kind, text in mutations(src, fn):
            begin, end = span(starts, node)
            mutant = src[:begin] + text + src[end:]
            try:
                with warnings.catch_warnings():
                    # The repo's own code can warn, like "is" with a literal.
                    warnings.simplefilter("ignore")
                    compile(mutant, path, "exec")
            except SyntaxError:
                continue
            if mutant == src:
                continue
            info = {
                "file": path,
                "function": name,
                "line": node.lineno,
                "kind": kind,
                "before": src[begin:end],
                "after": text,
                "in_fix": in_fix,
            }
            out.append((mutant, info))
    return out


def called_functions(trace):
    """{file: {qualified name}} for the changed files' functions the fixed
    code's tests called."""
    with open(trace / "calls.pkl", "rb") as f:
        calls = pickle.load(f)["calls"]
    out = {}
    for c in calls:
        if c["role"] == "changed":
            path, _, name = c["function"]
            out.setdefault(path, set()).add(name)
    return out


def patch_pieces(patch, path):
    """The pieces of the gold patch for one file: each run of removed and
    added lines, as (line in the fixed file where its added lines start,
    removed lines, added lines)."""
    pieces, current, n, piece = [], None, 0, None

    def close():
        nonlocal piece
        if piece is not None:
            pieces.append(piece)
            piece = None

    for line in patch.splitlines(keepends=True):
        m = re.match(r"^diff --git a/(\S+) b/", line)
        if m:
            close()
            current, n = m.group(1), 0
            continue
        m = re.match(r"^@@ -\S+ \+(\d+)", line)
        if m:
            close()
            n = int(m.group(1))
            continue
        if current != path or not n or line.startswith(("+++", "---")):
            continue
        if line.startswith(("-", "+")):
            if piece is None:
                piece = (n, [], [])
            if line.startswith("-"):
                piece[1].append(line[1:])
            else:
                piece[2].append(line[1:])
                n += 1
        elif not line.startswith("\\"):
            close()
            n += 1
    close()
    return pieces


def revert_mutants(src, path, patch):
    """[(mutant source, info)], one per piece of the gold patch, undone."""
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    out = []
    for start, removed, added in patch_pieces(patch, path):
        i = start - 1
        rest = i + len(added)
        mutant = "".join(lines[:i] + removed + lines[rest:])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(mutant, path, "exec")
        except SyntaxError:
            continue
        where = [
            name
            for name, fn in functions(tree).items()
            if fn.lineno <= start <= fn.end_lineno
        ]
        info = {
            "file": path,
            "function": max(where, key=len) if where else None,
            "line": start,
            "kind": "revert",
            "before": "".join(added),
            "after": "".join(removed),
            "in_fix": True,
        }
        out.append((mutant, info))
    return out


def write_mutants(run, task, patch):
    trace = Path(run) / FIXED_TRACES / task
    out = Path(run) / MUTANTS_DIR / task
    out.mkdir(parents=True, exist_ok=True)
    index = [{"id": "m000", "kind": "none", "file": None, "in_fix": None}]
    n = 0
    for path, wanted in sorted(called_functions(trace).items()):
        src = (trace / "src" / path).read_text()
        made = revert_mutants(src, path, patch)
        made += make_mutants(src, path, wanted, patch)
        for mutant, info in made:
            n += 1
            mid = f"m{n:03d}"
            (out / f"{mid}.py").write_text(mutant)
            index.append({"id": mid, **info})
    (out / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    return out, index


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        out, index = write_mutants(args.run, t, tasks[t]["patch"])
        in_fix = sum(1 for m in index if m.get("in_fix"))
        print(f"{t}: {len(index) - 1} mutants, {in_fix} in the fix -> {out}")


if __name__ == "__main__":
    main()
