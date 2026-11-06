"""Writes a Nagini spec for a task with one LLM call: step 4 of a run.

The prompt is the issue, the changed files as they were before the fix and,
with --candidates, every rule mined in step 3, all in one message. No tools.

Writes <run>/4_specs/<task>/<arm>.md (the answer), <arm>.prompt.txt, and
<arm>.json (model, tokens, cost). The arm is "baseline" (the issue and the
code) or "candidates" (the same plus the rules). It won't start a call once
the run has spent --budget dollars, counting every LLM step (llm/call.py).

    python -m llm.write_spec --tasks TASKS.json --run RUN --task ID
        --model MODEL --budget USD [--max-tokens N] [--candidates]
"""

import argparse
import json
from pathlib import Path

from .call import ask, spent

TRACES_DIR = "1_traces"
CANDIDATES_DIR = "3_candidates"
SPECS_DIR = "4_specs"

# The run 7 system prompt, without "and its fix", since there's no fix here.
SYSTEM = (
    "You are building a formally verified software-engineering benchmark in "
    "the style of SWE-Proof. Given a real GitHub issue, write a formal "
    "specification of the behavior the fixed code must have."
)
# Nagini specs are usually written by annotating the real code: contracts
# at the top of each function, above its unchanged body.
JOB = (
    "## Your job\n"
    "Annotate the code above with Nagini contracts (nagini_contracts: "
    "Requires, Ensures, Old, Result, Implies) describing the behavior the "
    "fixed code must have. Put the contracts at the top of each function you "
    "annotate, and keep its name, parameters and body unchanged. Return the "
    "annotated functions, inside their classes, in one Python code block."
)
CANDIDATES_INTRO = (
    "## Candidate rules\n"
    "These were mined by running the repo's tests on the code before the "
    "fix, so they describe that code, not the fixed code. A pre or post rule "
    "was true on every recorded call of its function, and an implies rule on "
    "every call where its left side held. A plain name like self.headers is "
    "its value when the call starts. after(self.headers) is its value when "
    "the same call returns. Some rules may be irrelevant."
)


def issue_text(task_id, task):
    return f"# Task {task_id}\n\n## Issue\n{task['problem_statement']}\n"


def code_text(trace):
    """The changed files as they were before the fix."""
    parts = ["## Code before the fix"]
    for path in sorted((trace / "src").rglob("*.py")):
        rel = path.relative_to(trace / "src")
        parts.append(f"`{rel}`\n```python\n{path.read_text()}\n```")
    return "\n\n".join(parts) + "\n"


def candidates_text(candidates):
    """Every mined rule, grouped by function."""
    parts = [CANDIDATES_INTRO]
    for name, f in candidates["functions"].items():
        rules = f.get("rules")
        if not rules:
            continue
        lines = [f"### {name}"]
        lines += [
            f"{r['kind']}: {r['rule']}   [{r['support']}]" for r in rules
        ]
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def build_prompt(task_id, task, trace, candidates=None):
    parts = [issue_text(task_id, task), code_text(trace)]
    if candidates is not None:
        parts.append(candidates_text(candidates))
    parts.append(JOB)
    return "\n".join(parts)


def write_spec(
    tasks, run, task_id, model, max_tokens, budget, with_candidates
):
    run = Path(run)
    candidates = None
    if with_candidates:
        path = run / CANDIDATES_DIR / f"{task_id}.json"
        candidates = json.loads(path.read_text())
    arm = "candidates" if with_candidates else "baseline"
    trace = run / TRACES_DIR / task_id
    prompt = build_prompt(task_id, tasks[task_id], trace, candidates)
    out = run / SPECS_DIR / task_id
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{arm}.prompt.txt").write_text(prompt)
    text, info = ask(run, model, budget, SYSTEM, prompt, max_tokens)
    (out / f"{arm}.md").write_text(text)
    (out / f"{arm}.json").write_text(json.dumps(info, indent=1) + "\n")
    print(f"{task_id} {arm}: ${info['usd']:.3f}, run total ${spent(run):.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--model", required=True)
    ap.add_argument(
        "--max-tokens",
        type=int,
        help="longest answer; if left out, litellm's default is used",
    )
    ap.add_argument(
        "--budget", type=float, required=True, help="dollars for the run"
    )
    ap.add_argument("--candidates", action="store_true")
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        write_spec(
            tasks,
            args.run,
            t,
            args.model,
            args.max_tokens,
            args.budget,
            args.candidates,
        )


if __name__ == "__main__":
    main()
