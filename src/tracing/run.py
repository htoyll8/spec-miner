"""Traces tasks in their SWE-bench containers, on the buggy or fixed code.

For each task: start its image, apply the test patch (and, with --code fixed,
the gold patch), check the tracer works on the container's Python
(selfcheck.py), and run tracer.py's two passes over the F2P and P2P tests.
Each call is tagged with its test, and tests.json says which are F2P, so
calls from the fix's tests can be left out later.

Writes <run>/1_traces/<task>/ for buggy code, or <run>/1_traces_fixed/<task>/
for fixed code: keys.json, calls.pkl, calls.txt (readable), flat.pkl (plain
values, loadable anywhere), src/ (the changed files), tests.json, trace.log.

    python -m tracing.run --tasks TASKS.json --run RUN_DIR --task ID[,ID...]
        --code buggy|fixed --depth N
        --trace-timeout S --pull-timeout S --docker-timeout S
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
TRACES_DIRS = {"buggy": "1_traces", "fixed": "1_traces_fixed"}


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


def apply_patches(code, folder="/out"):
    """The command that sets up buggy or fixed code with the fix's tests,
    from the patches saved in folder."""
    if code == "fixed":
        return f"git apply {folder}/test.patch {folder}/gold.patch"
    return f"git apply {folder}/test.patch"


def inner_command(task, code, depth):
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
            apply_patches(code),
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


def docker_command(image, mounts, inner, name):
    """docker run for image, with mounts as {host path: "/path[:ro]"}."""
    cmd = ["docker", "run", "--rm", "--name", name]
    for host, inside in mounts.items():
        cmd += ["-v", f"{host}:{inside}"]
    return cmd + [image, "bash", "-lc", inner]


def sh(cmd, timeout):
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def run_in_image(image, mounts, inner, timeouts):
    """Pull image if needed, run inner in it, clean up. Returns the log.

    timeouts holds seconds for each step: "run" (the container), "pull"
    (downloading the image), and "docker" (the short docker commands:
    checking, removing a container or an image). An image this pulled is
    removed afterwards, so the disk doesn't fill up.
    """
    name = "spec_" + uuid.uuid4().hex[:10]
    had = sh(["docker", "image", "inspect", image], timeouts["docker"])
    had = had.returncode == 0
    if not had:
        pull = sh(["docker", "pull", "-q", image], timeouts["pull"])
        if pull.returncode != 0:
            return "PULL FAILED\n" + pull.stderr
    try:
        r = sh(docker_command(image, mounts, inner, name), timeouts["run"])
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    finally:
        sh(["docker", "rm", "-f", name], timeouts["docker"])
        if not had:
            sh(["docker", "rmi", "-f", image], timeouts["docker"])


def trace(task_id, task, run_dir, code, depth, timeouts, dry_run):
    out = Path(run_dir).resolve() / TRACES_DIRS[code] / task_id
    mounts = {TRACER_DIR: "/trace:ro", out: "/out"}
    inner = inner_command(task, code, depth)
    if dry_run:
        print(shlex.join(docker_command(task["image"], mounts, inner, "x")))
        return
    out.mkdir(parents=True, exist_ok=True)
    (out / "test.patch").write_text(task["test_patch"])
    if code == "fixed":
        (out / "gold.patch").write_text(task["patch"])
    tests = {"F2P": task["F2P"], "P2P": task["P2P"]}
    (out / "tests.json").write_text(json.dumps(tests, indent=1))
    log = run_in_image(task["image"], mounts, inner, timeouts)
    (out / "trace.log").write_text(log)
    done = (out / "calls.pkl").exists()
    print(task_id, code, "done" if done else "FAILED, see trace.log")


def add_timeouts(ap):
    for step, help_ in (
        ("run", "seconds for one task's container run"),
        ("pull", "seconds to download an image"),
        ("docker", "seconds for each short docker command"),
    ):
        ap.add_argument(
            f"--{step}-timeout", type=int, required=True, help=help_
        )


def timeouts_from(args):
    return {
        "run": args.run_timeout,
        "pull": args.pull_timeout,
        "docker": args.docker_timeout,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--code", choices=("buggy", "fixed"), required=True)
    ap.add_argument(
        "--depth",
        type=int,
        required=True,
        help="levels of fields to follow into recorded objects",
    )
    add_timeouts(ap)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        trace(
            t,
            tasks[t],
            args.run,
            args.code,
            args.depth,
            timeouts_from(args),
            args.dry_run,
        )


if __name__ == "__main__":
    main()
