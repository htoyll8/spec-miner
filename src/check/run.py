"""Checks a spec on a task's buggy or fixed code: step 5 of a run.

Takes the spec's code block from <run>/4_specs/<task>/<arm>.md and checks
its clauses in the task's container, in one of two modes:
  recorded  checker.py, on the calls recorded earlier (1_traces for buggy
            code, 1_traces_fixed for fixed), with the same patches applied
            so the recorded objects load
  live      live.py, which wraps the spec's functions and runs the F2P and
            P2P tests, checking each call as it happens

A good clause holds on the fixed code and is violated on some buggy call.

Writes <run>/5_check/<task>/<arm>.<code>.<mode>.json, and a readable .txt.

    python -m check.run --tasks TASKS.json --run RUN --task ID --arm ARM
        --code buggy|fixed --mode recorded|live
        --run-timeout S --pull-timeout S --docker-timeout S
"""

import argparse
import json
import re
import shlex
from pathlib import Path

from tracing.run import (
    ACTIVATE,
    TRACES_DIRS,
    add_timeouts,
    apply_patches,
    changed_files,
    pytest_args,
    run_in_image,
    timeouts_from,
)

CHECK_DIR = Path(__file__).parent
SPECS_DIR = "4_specs"
RESULTS_DIR = "5_check"


def spec_code(markdown):
    """The first ```python block in an answer, or None."""
    m = re.search(r"```python\n(.*?)```", markdown, re.S)
    return m.group(1) if m else None


def inner_command(code, spec_name, out_name):
    """The recorded check: checker.py on the saved calls."""
    return " && ".join(
        [
            ACTIVATE,
            "cd /testbed",
            # The patches saved with the trace, mounted at /calls.
            apply_patches(code, "/calls"),
            "PYTHONPATH=/testbed python /check/checker.py"
            f" /work/{spec_name} /calls/calls.pkl /work/{out_name}",
        ]
    )


def live_command(task, code, spec_name, out_name):
    """The live check: live.py wraps the spec's functions, then runs the
    tests. The changed files' modules are imported first, so their
    functions can be found."""
    modules = ",".join(
        f[:-3].replace("/", ".") for f in changed_files(task["patch"])
    )
    tests = " ".join(shlex.quote(a) for a in pytest_args(task))
    return " && ".join(
        [
            ACTIVATE,
            "cd /testbed",
            # The patches, written next to the spec, mounted at /work.
            apply_patches(code, "/work"),
            "(python -c 'import pytest' 2>/dev/null"
            " || python -m pip install -q pytest)",
            "PYTHONPATH=/testbed python /check/live.py"
            f" /work/{spec_name} /work/{out_name} /testbed {modules}"
            f" -- {tests} -q -p no:cacheprovider -o addopts=",
        ]
    )


def summary(report):
    """The report as text: each function, then each clause's counts."""
    lines = []
    for name, f in report["functions"].items():
        note = f.get("problem") or f"{f['calls']} calls"
        lines.append(f"{name}: {note}")
    lines.append("")
    lines.append("held violated vacuous unresolved  clause")
    for c in report["clauses"]:
        n = "  ".join(
            f"{c[k]:>{len(k)}}"
            for k in ("held", "violated", "vacuous", "unresolved")
        )
        lines.append(f"{n}  {c['function']}: {c['text']}")
        for reason, count in c["reasons"].items():
            lines.append(f"      unresolved {count}x: {reason}")
        if c["example"]:
            lines.append(f"      first violation: {c['example']['test']}")
    return "\n".join(lines) + "\n"


def check(task_id, task, run, arm, code, mode, timeouts):
    run = Path(run).resolve()
    out = run / RESULTS_DIR / task_id
    out.mkdir(parents=True, exist_ok=True)
    spec = spec_code((run / SPECS_DIR / task_id / f"{arm}.md").read_text())
    if spec is None:
        print(task_id, arm, "has no python code block")
        return
    name = f"{arm}.{code}.{mode}"
    spec_name, out_name = f"{arm}.spec.py", f"{name}.json"
    (out / spec_name).write_text(spec)
    if mode == "recorded":
        trace = run / TRACES_DIRS[code] / task_id
        mounts = {CHECK_DIR: "/check:ro", trace: "/calls:ro", out: "/work"}
        inner = inner_command(code, spec_name, out_name)
    else:
        (out / "test.patch").write_text(task["test_patch"])
        (out / "gold.patch").write_text(task["patch"])
        mounts = {CHECK_DIR: "/check:ro", out: "/work"}
        inner = live_command(task, code, spec_name, out_name)
    log = run_in_image(task["image"], mounts, inner, timeouts)
    (out / f"{name}.log").write_text(log)
    result = out / out_name
    if not result.exists():
        print(task_id, name, "FAILED, see", f"{name}.log")
        return
    text = summary(json.loads(result.read_text()))
    (out / f"{name}.txt").write_text(text)
    print(f"{task_id} {name}\n{text}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--arm", required=True, help="the spec's arm name")
    ap.add_argument("--code", choices=("buggy", "fixed"), required=True)
    ap.add_argument("--mode", choices=("recorded", "live"), required=True)
    add_timeouts(ap)
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        check(
            t,
            tasks[t],
            args.run,
            args.arm,
            args.code,
            args.mode,
            timeouts_from(args),
        )


if __name__ == "__main__":
    main()
