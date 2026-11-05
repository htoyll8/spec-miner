"""Traces tasks in their SWE-bench containers, on the buggy code.

For each task: start its image, apply the test patch (but not the gold
patch), check the tracer works on the container's Python (selfcheck.py), and
run tracer.py's two passes over the F2P and P2P tests. Each call
is tagged with its test, and tests.json says which are F2P, so calls from the
fix's tests can be left out later.

Writes <run>/1_traces/<task>/: keys.json, calls.pkl, calls.txt (readable),
flat.pkl (plain values, loadable anywhere), src/ (the changed files),
tests.json and trace.log.

    python -m tracing.run --tasks TASKS.json --run RUN_DIR --task ID[,ID...]
        --trace-timeout S --pull-timeout S --docker-timeout S --depth N
    python -m tracing.run ... --dry-run   # print the commands, run nothing
"""

import argparse
import json
import re
import shlex
import subprocess
import uuid
from pathlib import Path

TRACER_DIR = Path(__file__).parent
ACTIVATE = "source /opt/miniconda3/bin/activate && conda activate testbed"
TRACES_DIR = "1_traces"


def changed_files(patch):
    """The files a patch changes, from its diff --git lines."""
    return re.findall(r"^diff --git a/(\S+) b/", patch, re.M)


def pytest_args(task):
    """The F2P and P2P tests, as pytest arguments.

    Most repos give pytest IDs, like file.py::test_a. Sympy gives bare
    names, like test_a, so those run as the test files with -k.
    """
    tests = task["F2P"] + task["P2P"]
    if all("::" in t for t in tests):
        return tests
    return list(task["test_patch_files"]) + ["-k", " or ".join(tests)]


def inner_command(task, depth):
    """The shell command that runs inside the container.

    depth is how many levels of fields flatten follows into objects.
    """
    files = changed_files(task["patch"])
    changed = ",".join("/testbed/" + f for f in files)
    tests = " ".join(shlex.quote(a) for a in pytest_args(task))
    opts = "-q -p no:cacheprovider -o addopts="
    tracer = "python /trace/tracer.py"
    return " && ".join(
        [
            ACTIVATE,
            "cd /testbed",
            "git apply /out/test.patch",
            "(python -c 'import pytest' 2>/dev/null"
            " || python -m pip install -q pytest)",
            "python /trace/selfcheck.py",
            f"{tracer} discover /testbed {changed} /out/keys.json"
            f" -- {tests} {opts}",
            f"{tracer} record /testbed {changed} /out/keys.json"
            f" /out/calls.pkl -- {tests} {opts}",
            # A readable copy, made here since loading needs the repo.
            f"{tracer} show /out/calls.pkl > /out/calls.txt",
            f"python /trace/flatten.py /out/calls.pkl {depth} /out/flat.pkl",
            # The changed files' source, which the grammar is read from.
            "mkdir -p /out/src",
            "cp --parents " + " ".join(map(shlex.quote, files)) + " /out/src/",
        ]
    )


def docker_command(task, out, name, depth):
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "-v",
        f"{TRACER_DIR}:/trace:ro",
        "-v",
        f"{out}:/out",
        task["image"],
        "bash",
        "-lc",
        inner_command(task, depth),
    ]


def sh(cmd, timeout):
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def trace(task_id, task, run_dir, timeouts, depth, dry_run):
    """Trace one task. timeouts holds seconds for each step: "trace" (the
    container run), "pull" (downloading the image), and "docker" (the
    short docker commands: checking, removing a container or an image)."""
    out = Path(run_dir).resolve() / TRACES_DIR / task_id
    name = "trace_" + uuid.uuid4().hex[:10]
    cmd = docker_command(task, out, name, depth)
    if dry_run:
        print(shlex.join(cmd))
        return
    out.mkdir(parents=True, exist_ok=True)
    (out / "test.patch").write_text(task["test_patch"])
    tests = {"F2P": task["F2P"], "P2P": task["P2P"]}
    (out / "tests.json").write_text(json.dumps(tests, indent=1))
    image = task["image"]
    had = sh(["docker", "image", "inspect", image], timeouts["docker"])
    had = had.returncode == 0
    if not had:
        pull = sh(["docker", "pull", "-q", image], timeouts["pull"])
        if pull.returncode != 0:
            (out / "trace.log").write_text("PULL FAILED\n" + pull.stderr)
            print(task_id, "FAILED to pull the image", flush=True)
            return
    try:
        r = sh(cmd, timeouts["trace"])
        log = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        log = "TIMEOUT"
    finally:
        sh(["docker", "rm", "-f", name], timeouts["docker"])
        if not had:
            sh(["docker", "rmi", "-f", image], timeouts["docker"])
    (out / "trace.log").write_text(log)
    done = (out / "calls.pkl").exists()
    print(task_id, "done" if done else "FAILED, see trace.log", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    for step, help_ in (
        ("trace", "seconds for one task's container run"),
        ("pull", "seconds to download an image"),
        ("docker", "seconds for each short docker command"),
    ):
        ap.add_argument(
            f"--{step}-timeout", type=int, required=True, help=help_
        )
    ap.add_argument(
        "--depth",
        type=int,
        required=True,
        help="levels of fields to follow into recorded objects",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    timeouts = {
        "trace": args.trace_timeout,
        "pull": args.pull_timeout,
        "docker": args.docker_timeout,
    }
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        trace(t, tasks[t], args.run, timeouts, args.depth, args.dry_run)


if __name__ == "__main__":
    main()
