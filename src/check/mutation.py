"""Scores a spec by the mutants it catches: step 6 of a run.

For one spec (<run>/4_specs/<task>/<arm>.md) and the mutants from mutate.py
(<run>/6_mutants/<task>/), runs every mutant in one container: put the
mutant in place of the fixed file, run live.py (the spec's referee) over the
F2P and P2P tests, then put the fixed file back. m000, the unchanged file,
runs the same way as a control.

A mutant is caught by the spec if any clause is violated on it, and caught
by the tests if one of the repo's tests fails. With --inputs, the referee
also runs the calls from llm/write_inputs.py, so the spec is checked on more
calls, but only the repo's tests count as the tests catching a mutant. If
live.py crashes, for example because the mutant breaks importing, it counts
as broken, not caught.

Writes <run>/6_mutants/<task>/<arm>/, or <arm>+inputs/ with --inputs: one
JSON per mutant, run.sh (what ran in the container), score.json and
score.txt.

    python -m check.mutation --tasks TASKS.json --run RUN --task ID
        --arm ARM --mutant-timeout S [--inputs]
        --run-timeout S --pull-timeout S --docker-timeout S
"""

import argparse
import json
import shlex
from pathlib import Path

from check.run import SPECS_DIR, spec_code
from llm.write_inputs import INPUTS_DIR
from tracing.run import (
    ACTIVATE,
    add_timeouts,
    apply_patches,
    changed_files,
    pytest_args,
    run_in_image,
    timeouts_from,
)

CHECK_DIR = Path(__file__).parent
MUTANTS_DIR = "6_mutants"


CALLS = "calls.py"


def script(task, index, arm, mutant_timeout, inputs=False):
    """The shell script that runs every mutant in the container. With
    inputs, the referee also runs the generated calls."""
    files = changed_files(task["patch"])
    modules = ",".join(f[:-3].replace("/", ".") for f in files)
    if inputs:
        modules += f" /mut/{CALLS}"
    tests = " ".join(shlex.quote(a) for a in pytest_args(task))
    live = (
        "PYTHONPATH=/testbed PYTHONDONTWRITEBYTECODE=1"
        f" timeout {mutant_timeout} python /check/live.py"
        f" /mut/{arm}/spec.py /mut/{arm}/{{id}}.json /testbed {modules}"
        f" -- {tests} -q -p no:cacheprovider -o addopts="
    )
    lines = [
        ACTIVATE,
        "cd /testbed",
        apply_patches("fixed", "/mut"),
        "(python -c 'import pytest' 2>/dev/null"
        " || python -m pip install -q pytest)",
        # The fixed files, to put back after each mutant.
        "mkdir -p /tmp/fixed && cp --parents "
        + " ".join(map(shlex.quote, files))
        + " /tmp/fixed/",
    ]
    for m in index:
        mid = m["id"]
        lines.append(f"echo {mid}")
        if m["file"] is not None:
            lines.append(f"cp /mut/{mid}.py {shlex.quote(m['file'])}")
        lines.append(live.format(id=mid) + " > /dev/null 2>&1")
        if m["file"] is not None:
            lines.append(f"cp /tmp/fixed/{m['file']} {shlex.quote(m['file'])}")
    return "\n".join(lines) + "\n"


def clause_name(c):
    return f"{c['function']}: {c['text']}"


def load_report(path):
    return json.loads(path.read_text()) if path.exists() else None


def new_failures(clause, control):
    """The calls where clause failed but the same clause didn't fail on the
    control (the unchanged fixed code)."""
    before = control.get("by_test", {}) if control else {}
    return [
        test
        for test, per in clause.get("by_test", {}).items()
        if per.get("violated", 0) > 0
        and before.get(test, {}).get("violated", 0) == 0
    ]


def outcome(report, control):
    """What happened to one mutant: caught by the spec, by the tests, which
    clauses caught it, or broken.

    The spec catches a mutant when a clause fails on a call where it didn't
    fail on the control. Failures already on the control don't count, since
    they'd count on every mutant, but the clause still counts on its other
    calls.
    """
    if report is None:
        return {"broken": True}
    controls = {clause_name(c): c for c in (control or {}).get("clauses", [])}
    caught_by = [
        clause_name(c)
        for c in report["clauses"]
        if new_failures(c, controls.get(clause_name(c)))
    ]
    return {
        "broken": False,
        "spec": bool(caught_by),
        "tests": bool(report.get("failed_tests", [])),
        "clauses": caught_by,
    }


def score(index, folder):
    """Counts for the spec and the tests, overall, in the fix and outside.

    Clauses that fail on the control, the unchanged fixed code, disagree
    with the fix there. They're listed with the calls that failed, for
    someone to judge whether the spec or the fix is wrong.
    """
    control = load_report(folder / f"{index[0]['id']}.json")
    disagreements = []
    if control is not None:
        disagreements = [
            {
                "clause": clause_name(c),
                "violated_by": c.get("violated_by", {}),
                "examples": c.get("examples", {}),
            }
            for c in control["clauses"]
            if c["violated"] > 0
        ]
    rows = [
        {**m, **outcome(load_report(folder / f"{m['id']}.json"), control)}
        for m in index
    ]
    out = {
        "control": rows[0],
        "disagreements": disagreements,
        "groups": {},
    }
    groups = {
        "all": rows[1:],
        "in the fix": [r for r in rows[1:] if r["in_fix"]],
        "outside the fix": [r for r in rows[1:] if not r["in_fix"]],
    }
    for name, group in groups.items():
        ok = [r for r in group if not r["broken"]]
        out["groups"][name] = {
            "mutants": len(group),
            "broken": len(group) - len(ok),
            "spec": sum(r["spec"] for r in ok),
            "tests": sum(r["tests"] for r in ok),
            "spec_not_tests": sum(r["spec"] and not r["tests"] for r in ok),
            "tests_not_spec": sum(r["tests"] and not r["spec"] for r in ok),
        }
    out["mutants"] = rows
    return out


def values_text(example, indent):
    """An example's values as lines: before, after and result."""
    if not example:
        return []
    out = []
    for key in ("before", "after", "result"):
        if key not in example:
            continue
        v = example[key]
        if isinstance(v, dict):
            v = ", ".join(f"{k}={x}" for k, x in v.items())
        out.append(f"{indent}{key}: {v}")
    return out


def call_text(test, calls):
    """A violating call: a test's name, or a generated call's line."""
    if test and test.startswith("generated::") and calls:
        n = int(test.split("::")[1])
        if 0 < n <= len(calls):
            return f"{test}  {calls[n - 1]}"
    return str(test)


def score_text(s, calls=None):
    """The score as text. calls are the generated calls' lines, if any."""
    lines = []
    if s["control"].get("broken"):
        lines.append("CONTROL BROKEN: the unchanged fixed code didn't run")
    if s["disagreements"]:
        lines.append(
            "Disagrees with the fix (fails on the unchanged fixed code)."
        )
        lines.append("Those calls don't count as catching mutants:")
        for d in s["disagreements"]:
            lines.append(f"  {d['clause']}")
            for test, n in d["violated_by"].items():
                lines.append(f"      {n}x {call_text(test, calls)}")
                example = d.get("examples", {}).get(str(test))
                lines += values_text(example, "          ")
    else:
        lines.append("Every clause holds on the unchanged fixed code.")
    lines.append("")
    lines.append(
        "group             mutants broken  spec tests  spec-only tests-only"
    )
    for name, g in s["groups"].items():
        lines.append(
            f"{name:17} {g['mutants']:7} {g['broken']:6} {g['spec']:5}"
            f" {g['tests']:5} {g['spec_not_tests']:10}"
            f" {g['tests_not_spec']:10}"
        )
    return "\n".join(lines) + "\n"


def rescore(folder, calls_path=None):
    """Score a finished run again from its saved results, with no rerun."""
    index = json.loads((folder.parent / "index.json").read_text())
    calls = None
    if calls_path and calls_path.exists():
        calls = [x.strip() for x in calls_path.read_text().splitlines()]
        calls = [x for x in calls if x]
    s = score(index, folder)
    (folder / "score.json").write_text(json.dumps(s, indent=1) + "\n")
    text = score_text(s, calls)
    (folder / "score.txt").write_text(text)
    return text


def run_mutants(task_id, task, run, arm, mutant_timeout, timeouts, inputs):
    run = Path(run).resolve()
    folder = run / MUTANTS_DIR / task_id
    index = json.loads((folder / "index.json").read_text())
    name = f"{arm}+inputs" if inputs else arm
    out = folder / name
    out.mkdir(parents=True, exist_ok=True)
    md = (run / SPECS_DIR / task_id / f"{arm}.md").read_text()
    spec = spec_code(md)
    if spec is None:
        print(task_id, arm, "has no python code block")
        return
    (out / "spec.py").write_text(spec)
    (folder / "test.patch").write_text(task["test_patch"])
    (folder / "gold.patch").write_text(task["patch"])
    if inputs:
        calls = run / INPUTS_DIR / task_id / CALLS
        (folder / CALLS).write_text(calls.read_text())
    sh = script(task, index, name, mutant_timeout, inputs)
    (out / "run.sh").write_text(sh)
    mounts = {CHECK_DIR: "/check:ro", folder: "/mut"}
    log = run_in_image(
        task["image"], mounts, f"bash /mut/{name}/run.sh", timeouts
    )
    (out / "run.log").write_text(log)
    calls = folder / CALLS if inputs else None
    print(f"{task_id} {name}\n{rescore(out, calls)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--arm", required=True, help="the spec's arm name")
    ap.add_argument(
        "--mutant-timeout",
        type=int,
        required=True,
        help="seconds for one mutant's tests",
    )
    ap.add_argument(
        "--inputs",
        action="store_true",
        help="also run the generated inputs from llm/write_inputs.py",
    )
    add_timeouts(ap)
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        run_mutants(
            t,
            tasks[t],
            args.run,
            args.arm,
            args.mutant_timeout,
            timeouts_from(args),
            args.inputs,
        )


if __name__ == "__main__":
    main()
