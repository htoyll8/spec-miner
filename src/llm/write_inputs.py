"""Asks an LLM for extra inputs to run the code on: part of step 5.

The repo's tests only make a few calls, so a spec that's wrong on other
inputs can't be caught. This asks an LLM, given only the issue and the code
before the fix, for calls to the code with many different inputs. It never
sees the fix, the tests or any spec.

The answer is a list of calls, one per line, after any imports. The spec's
referee (check/live.py) runs each call after the tests, so it checks the
spec on these inputs too.

Writes <run>/4_inputs/<task>/: calls.py (the imports, then one valid call per
line), answer.md (the whole answer), prompt.txt and info.json.

    python -m llm.write_inputs --tasks TASKS.json --run RUN --task ID
        --model MODEL --budget USD [--max-tokens N]
"""

import argparse
import ast
import json
import re
from pathlib import Path

from .call import ask, spent
from .write_spec import TRACES_DIR, code_text, issue_text

INPUTS_DIR = "4_inputs"
SYSTEM = "You write test inputs for Python code."
JOB = """## Your job
List calls to the code this issue is about, with many different inputs: the
cases the issue describes, and the cases around them that a fix could get
wrong. Write one call per line, after any imports they need. Don't use the
network or write files. Return them in one Python code block. For example,
for an issue about a `parse_date` function in `mylib/dates.py`:
```python
from mylib.dates import parse_date
parse_date('2024-01-31')
parse_date('2024-02-30')
parse_date('31/01/2024')
parse_date('')
parse_date(None)
```"""


def build_prompt(task_id, task, trace):
    return "\n".join([issue_text(task_id, task), code_text(trace), JOB])


def split_calls(code):
    """(imports, calls): the import lines, and every other line that's a
    valid statement on its own. Comments and blank lines are dropped."""
    imports, calls = [], []
    for line in code.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ast.parse(line)
        except SyntaxError:
            continue
        (imports if line.startswith(("import ", "from ")) else calls).append(
            line
        )
    return imports, calls


def write_inputs(tasks, run, task_id, model, budget, max_tokens):
    run = Path(run)
    trace = run / TRACES_DIR / task_id
    prompt = build_prompt(task_id, tasks[task_id], trace)
    out = run / INPUTS_DIR / task_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(prompt)
    text, info = ask(run, model, budget, SYSTEM, prompt, max_tokens)
    (out / "answer.md").write_text(text)
    m = re.search(r"```python\n(.*?)```", text, re.S)
    imports, calls = split_calls(m.group(1) if m else "")
    info["calls"] = len(calls)
    (out / "calls.py").write_text("\n".join(imports + calls) + "\n")
    (out / "info.json").write_text(json.dumps(info, indent=1) + "\n")
    print(
        f"{task_id}: {len(calls)} calls, ${info['usd']:.3f},"
        f" run total ${spent(run):.3f}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tasks", required=True, help="the task list JSON")
    ap.add_argument("--run", required=True, help="the run's results folder")
    ap.add_argument("--task", required=True, help="task IDs, comma separated")
    ap.add_argument("--model", required=True)
    ap.add_argument(
        "--budget", type=float, required=True, help="dollars for the run"
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        help="longest answer; if left out, litellm's default is used",
    )
    args = ap.parse_args()
    tasks = json.loads(Path(args.tasks).read_text())
    for t in args.task.split(","):
        write_inputs(
            tasks, args.run, t, args.model, args.budget, args.max_tokens
        )


if __name__ == "__main__":
    main()
