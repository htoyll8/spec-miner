"""Groups a spec's clauses by how they did on the buggy and fixed code.

From one spec's two check results in <run>/5_check/<task>/ (its buggy and
fixed runs, in the same mode), each clause's calls are compared one by one,
a call being a test or a generated line. Each call lands in one cell:

                     holds on buggy        fails on buggy
  holds on fixed     says nothing          catches the bug
  fails on fixed     disagrees with fix    fails on both

So one clause can catch the bug on some calls and disagree with the fix on
others. A call that disagrees means the spec or the fix is wrong there, so
those calls are listed for someone to judge. Calls checked on only one side
(vacuous, unresolved or not run on the other) are counted apart.

Writes <run>/5_check/<task>/<arm>.<mode>.compare.txt.

    python -m check.compare --run RUN --task ID --arm ARM --mode MODE

MODE is the results' suffix, like live, recorded or live+inputs.
"""

import argparse
import json
from pathlib import Path

from check.mutation import call_text, clause_name, values_text
from check.run import RESULTS_DIR
from llm.write_inputs import INPUTS_DIR


def clauses(path):
    report = json.loads(path.read_text())
    return {clause_name(c): c for c in report["clauses"]}


CELLS = (
    "says nothing",
    "catches the bug",
    "disagrees with the fix",
    "fails on both",
)


def side(per):
    """ "holds", "fails", or None if the call wasn't checked."""
    if per.get("violated", 0) > 0:
        return "fails"
    return "holds" if per.get("held", 0) > 0 else None


def examples_of(buggy, fixed):
    """{clause: {"buggy": {test: example}, "fixed": {...}}}."""
    return {
        name: {
            "buggy": buggy.get(name, {}).get("examples", {}),
            "fixed": f.get("examples", {}),
        }
        for name, f in fixed.items()
    }


def compare(buggy, fixed):
    """{clause: {cell: [calls]}}, comparing two reports' clauses call by
    call. The extra cell "one side only" holds calls checked on one side."""
    out = {}
    for name, f in fixed.items():
        b = buggy.get(name, {})
        fb, bb = f.get("by_test", {}), b.get("by_test", {})
        cells = {c: [] for c in CELLS + ("one side only",)}
        for test in sorted(set(fb) | set(bb)):
            on_fixed, on_buggy = side(fb.get(test, {})), side(bb.get(test, {}))
            if on_fixed is None or on_buggy is None:
                cell = "one side only"
            else:
                cell = CELLS[(on_fixed == "fails") * 2 + (on_buggy == "fails")]
            cells[cell].append(test)
        out[name] = cells
    return out


def compare_text(clauses, examples, calls=None):
    lines = [
        "Calls per cell: nothing = holds on both, catches = holds on fixed",
        "and fails on buggy, disagrees = fails on fixed and holds on buggy,",
        "both = fails on both, one side = checked on only one side.",
        "",
        "nothing catches disagrees both one-side  clause",
    ]
    for name, cells in clauses.items():
        n = [len(cells[c]) for c in CELLS + ("one side only",)]
        lines.append(f"{n[0]:7} {n[1]:7} {n[2]:9} {n[3]:4} {n[4]:8}  {name}")
        for cell in (
            "catches the bug",
            "disagrees with the fix",
            "fails on both",
        ):
            for test in cells[cell]:
                lines.append(f"      {cell}: {call_text(test, calls)}")
                # The values from the side where it failed: buggy for a
                # catch, fixed otherwise.
                side = "buggy" if cell == "catches the bug" else "fixed"
                e = examples[name][side].get(test)
                lines += values_text(e, "          ")
    return "\n".join(lines) + "\n"


def compare_files(run, task, arm, mode):
    """Compare one spec's buggy and fixed results, write compare.txt, and
    return its text."""
    run = Path(run)
    folder = run / RESULTS_DIR / task
    buggy = clauses(folder / f"{arm}.buggy.{mode}.json")
    fixed = clauses(folder / f"{arm}.fixed.{mode}.json")
    calls = None
    calls_path = run / INPUTS_DIR / task / "calls.py"
    if "+inputs" in mode and calls_path.exists():
        calls = [x.strip() for x in calls_path.read_text().splitlines()]
        calls = [x for x in calls if x]
    text = compare_text(
        compare(buggy, fixed), examples_of(buggy, fixed), calls
    )
    (folder / f"{arm}.{mode}.compare.txt").write_text(text)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--arm", required=True, help="the spec's arm name")
    ap.add_argument("--mode", required=True, help="like live or live+inputs")
    args = ap.parse_args()
    for t in args.task.split(","):
        text = compare_files(args.run, t, args.arm, args.mode)
        print(f"{t} {args.arm} {args.mode}\n{text}")


if __name__ == "__main__":
    main()
