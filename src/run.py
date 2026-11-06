"""Runs the whole pipeline over a config's tasks.

For each task, in order, skipping any step whose output already exists, so
a stopped run picks up where it left off and never pays for an LLM call
twice:

  trace    the buggy and the fixed code (tracing/run.py)
  mine     candidate rules (mining/mine.py)
  specs    one per arm (llm/write_spec.py)
  inputs   generated calls (llm/write_inputs.py)
  check    each spec on buggy and fixed code, with and without the inputs,
           then compared call by call (check/run.py, check/compare.py)
  mutants  made from the fixed code, then each spec scored on them, with
           and without the inputs (check/mutate.py, check/mutation.py)

A task's image is pulled once, used by every step, and removed after. Each
step's time goes in timings.json, and summary.txt has a table for all tasks.
Once the LLM budget is reached, steps that need an LLM are skipped.

    python -m run CONFIG.toml                    # results/<name>_<time>
    python -m run CONFIG.toml --resume RUN_DIR   # carry on with a run
"""

import argparse
import json
import shutil
import time
import tomllib
from datetime import datetime
from pathlib import Path

from check import compare, mutate, mutation
from check import run as check_run
from llm import write_inputs, write_spec
from llm.call import spent
from mining import mine
from tracing import run as tracing_run

MODES = ((False, "live"), (True, "live+inputs"))


class BudgetReached(Exception):
    pass


def load_config(path):
    config = tomllib.loads(Path(path).read_text())
    config["tasks_file"] = str(Path(config["tasks_file"]).expanduser())
    return config


def timeouts(config):
    return dict(config["timeouts"])


def outputs(run, task, config):
    """Each step's outputs: the step is done when all of them exist."""
    arms = config["specs"]["arms"]
    check = run / check_run.RESULTS_DIR / task
    mut = run / mutation.MUTANTS_DIR / task
    return {
        "trace": [
            run / tracing_run.TRACES_DIRS[code] / task / "flat.pkl"
            for code in ("buggy", "fixed")
        ],
        "mine": [run / mine.CANDIDATES_DIR / f"{task}.json"],
        "specs": [run / write_spec.SPECS_DIR / task / f"{a}.md" for a in arms],
        "inputs": [run / write_inputs.INPUTS_DIR / task / "calls.py"],
        "check": [
            check / f"{a}.{mode}.compare.txt"
            for a in arms
            for _, mode in MODES
        ],
        "mutants": [
            mut / (a + ("+inputs" if inputs else "")) / "score.json"
            for a in arms
            for inputs, _ in MODES
        ],
    }


def step_trace(run, task_id, task, config):
    for code in ("buggy", "fixed"):
        tracing_run.trace(
            task_id,
            task,
            run,
            code,
            config["trace"]["depth"],
            timeouts(config),
            dry_run=False,
        )


def step_mine(run, task_id, task, config):
    m = config["mine"]
    mine.mine_task(
        run,
        task_id,
        m["max_size"],
        m["min_support"],
        m["f2p"] == "include",
        m["scope"],
    )


def step_specs(run, task_id, task, config, tasks):
    for arm in config["specs"]["arms"]:
        if (run / write_spec.SPECS_DIR / task_id / f"{arm}.md").exists():
            continue
        llm(
            write_spec.write_spec,
            tasks,
            run,
            task_id,
            config["llm"]["model"],
            None,
            config["llm"]["budget"],
            arm == "candidates",
        )


def step_inputs(run, task_id, task, config, tasks):
    llm(
        write_inputs.write_inputs,
        tasks,
        run,
        task_id,
        config["llm"]["model"],
        config["llm"]["budget"],
        None,
    )


def llm(f, *args):
    try:
        f(*args)
    except SystemExit as e:
        if "budget reached" in str(e):
            raise BudgetReached(str(e)) from None
        raise


def step_check(run, task_id, task, config):
    for arm in config["specs"]["arms"]:
        for inputs, mode in MODES:
            for code in ("buggy", "fixed"):
                check_run.check(
                    task_id,
                    task,
                    run,
                    arm,
                    code,
                    "live",
                    timeouts(config),
                    inputs,
                )
            compare.compare_files(run, task_id, arm, mode)


def step_mutants(run, task_id, task, config):
    folder = run / mutation.MUTANTS_DIR / task_id
    if not (folder / "index.json").exists():
        mutate.write_mutants(run, task_id, task["patch"])
    for arm in config["specs"]["arms"]:
        for inputs, _ in MODES:
            name = arm + ("+inputs" if inputs else "")
            if (folder / name / "score.json").exists():
                continue
            mutation.run_mutants(
                task_id,
                task,
                run,
                arm,
                config["mutants"]["mutant_timeout"],
                timeouts(config),
                inputs,
            )


STEPS = (
    ("trace", step_trace, False),
    ("mine", step_mine, False),
    ("specs", step_specs, True),
    ("inputs", step_inputs, True),
    ("check", step_check, False),
    ("mutants", step_mutants, False),
)


def run_task(run, task_id, task, config, tasks, timings, state):
    """Every step for one task, with its image pulled once."""
    t = timeouts(config)
    times = timings.setdefault(task_id, {})
    todo = outputs(run, task_id, config)
    if all(p.exists() for paths in todo.values() for p in paths):
        for name, _, _ in STEPS:
            times.setdefault(name, "done before")
        return
    image = task["image"]
    had = tracing_run.sh(["docker", "image", "inspect", image], t["docker"])
    had = had.returncode == 0
    if not had:
        tracing_run.sh(["docker", "pull", "-q", image], t["pull"])
    try:
        for name, f, needs_llm in STEPS:
            if all(p.exists() for p in outputs(run, task_id, config)[name]):
                times.setdefault(name, "done before")
                continue
            if needs_llm and state["budget_reached"]:
                times[name] = "skipped: budget reached"
                continue
            start = time.time()
            try:
                if needs_llm:
                    f(run, task_id, task, config, tasks)
                else:
                    f(run, task_id, task, config)
                times[name] = round(time.time() - start, 1)
            except BudgetReached as e:
                state["budget_reached"] = True
                times[name] = f"skipped: {e}"
            except Exception as e:
                times[name] = f"error: {type(e).__name__}: {str(e)[:200]}"
            (run / "timings.json").write_text(json.dumps(timings, indent=1))
            print(f"{task_id} {name}: {times[name]}", flush=True)
    finally:
        if not had:
            tracing_run.sh(["docker", "rmi", "-f", image], t["docker"])


def summary(run, config):
    """One table for all tasks: each spec's mutant scores and clauses."""
    lines = [
        f"run: {run.name}   spent: ${spent(run):.2f}",
        "",
        "task / spec              mutants  in fix  outside  tests  clauses",
        "                         caught   caught  caught   caught catch/fail",
    ]
    for task in config["tasks"]:
        lines.append(task)
        folder = run / mutation.MUTANTS_DIR / task
        for arm in config["specs"]["arms"]:
            for inputs, mode in MODES:
                name = arm + ("+inputs" if inputs else "")
                score = folder / name / "score.json"
                cmp = run / check_run.RESULTS_DIR / task / f"{arm}.{mode}"
                row = f"  {name:22}"
                if score.exists():
                    g = json.loads(score.read_text())["groups"]
                    a, i, o = g["all"], g["in the fix"], g["outside the fix"]
                    row += (
                        f" {a['spec']:3}/{a['mutants']:<4}"
                        f" {i['spec']:3}/{i['mutants']:<3}"
                        f" {o['spec']:3}/{o['mutants']:<4}"
                        f" {a['tests']:5}"
                    )
                else:
                    row += " (no mutant scores)" + " " * 18
                row += "  " + clause_counts(cmp)
                lines.append(row)
    lines.append("")
    lines.append(
        "clauses catch/fail: how many clauses catch the bug on some call,"
    )
    lines.append("and how many fail on the fixed code on some call.")
    return "\n".join(lines) + "\n"


def clause_counts(prefix):
    buggy = Path(f"{prefix}.json".replace(".live", ".buggy.live"))
    fixed = Path(f"{prefix}.json".replace(".live", ".fixed.live"))
    if not (buggy.exists() and fixed.exists()):
        return "-"
    cells = compare.compare(compare.clauses(buggy), compare.clauses(fixed))
    catch = sum(bool(c["catches the bug"]) for c in cells.values())
    fail = sum(
        bool(c["disagrees with the fix"] or c["fails on both"])
        for c in cells.values()
    )
    return f"{catch}/{fail} of {len(cells)}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("config", help="an experiment config, like exp/configs/x")
    ap.add_argument("--resume", help="a run folder to carry on with")
    args = ap.parse_args()
    config = load_config(args.config)
    if args.resume:
        run = Path(args.resume)
    else:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        run = Path("results") / f"{config['name']}_{stamp}"
        run.mkdir(parents=True)
        shutil.copy(args.config, run / "config.toml")
    run = run.resolve()
    tasks = json.loads(Path(config["tasks_file"]).read_text())
    timings_path = run / "timings.json"
    timings = (
        json.loads(timings_path.read_text()) if timings_path.exists() else {}
    )
    state = {"budget_reached": False}
    for task_id in config["tasks"]:
        run_task(run, task_id, tasks[task_id], config, tasks, timings, state)
        (run / "summary.txt").write_text(summary(run, config))
    print((run / "summary.txt").read_text())


if __name__ == "__main__":
    main()
