"""Checks a Nagini spec live, while the tests run, inside the container.

Puts a referee at the door of every repo function the spec has rules for: a
wrapper that replaces the function before the tests start. When the function
is called, the referee checks Requires on the arguments and copies what
Old(...) asks about. Then the real function runs. When it returns, the
referee checks Ensures on the arguments as they are now and on Result().
Every outcome is counted, and nothing the referee does can stop or change the
call: a check that fails only becomes a count.

Clauses are handled exactly as in checker.py (Implies, Old, Result, empty
helpers, parameter names). The report has the same shape.

Standalone and Python 3.6+, like tracing/tracer.py.

    python live.py SPEC.py OUT.json ROOT MODULE[,MODULE...] [CALLS.py]
        -- PYTEST_ARGS...

ROOT is the repo, and MODULE the modules to import first, like
requests.models, so their functions can be found and wrapped. CALLS.py, from
llm/write_inputs.py, is extra inputs: after the tests, with the referee still
in place, its import lines run, then each other line runs on its own, tagged
generated::<line number>. An error in a call is caught and counted.
"""

import ast
import builtins
import functools
import importlib
import inspect
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checker import (  # noqa: E402
    Prepared,
    describe,
    helpers,
    match_params,
    new_counts,
    shims,
    spec_functions,
    tally,
)

# True while the referee is checking, so calls a check makes itself, like a
# rule that calls a repo function, aren't checked in turn.
_busy = threading.local()


def current_test():
    s = os.environ.get("PYTEST_CURRENT_TEST")
    return s.rpartition(" ")[0] if s else None


def repo_modules(root):
    """The loaded modules whose files are in the repo, outside tests."""
    root = os.path.realpath(root) + os.sep
    out = []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if not f:
            continue
        p = os.path.realpath(f)
        name = os.path.basename(p)
        if (
            p.startswith(root)
            and "site-packages" not in p
            and not name.startswith("test_")
            and name != "conftest.py"
        ):
            out.append(m)
    return out


def functions_in(modules):
    """{qualified name: [(owner, attribute, raw)]} for every function defined
    in modules, at module level or in a class. raw is what the owner holds,
    which may be a staticmethod or classmethod."""
    out = {}

    def add(owner, attr, raw):
        fn = getattr(raw, "__func__", raw)
        name = getattr(fn, "__qualname__", None)
        if inspect.isfunction(fn) and name:
            out.setdefault(name, []).append((owner, attr, raw))

    for m in modules:
        for attr, v in list(vars(m).items()):
            if getattr(v, "__module__", None) != m.__name__:
                continue
            if inspect.isclass(v):
                for cattr, raw in list(vars(v).items()):
                    add(v, cattr, raw)
            else:
                add(m, attr, v)
    return out


def find(name, found):
    """The (owner, attribute, raw) for a spec function's name: by exact name,
    or by its last part if exactly one function has it."""
    if len(found.get(name, [])) == 1:
        return found[name][0]
    last = name.split(".")[-1]
    options = [
        hit
        for qual, hits in found.items()
        if qual.split(".")[-1] == last
        for hit in hits
    ]
    return options[0] if len(options) == 1 else None


class Referee:
    """The wrapper for one function, and its clauses' counts."""

    def __init__(self, fn, names, preps, base, info):
        self.fn = fn
        self.sig = inspect.signature(fn)
        self.names = names  # spec parameter -> real parameter
        self.preps = preps  # [(Prepared, counts)]
        self.base = base
        self.info = info

    def arguments(self, args, kwargs):
        bound = self.sig.bind(*args, **kwargs)
        bound.apply_defaults()
        real = bound.arguments
        return {s: real[r] for s, r in self.names.items() if r in real}

    def enter(self, args, kwargs):
        """Check Requires, and copy what Old(...) needs. Returns what the
        exit check needs."""
        live = self.arguments(args, kwargs)
        env = dict(self.base, **live)
        test = current_test()
        # The arguments as text now, since the call may change them. Only
        # kept if a clause fails on this call.
        before = {k: describe(v) for k, v in live.items()}

        def example():
            return {"test": test, "before": before}

        olds = {}
        for prep, counts in self.preps:
            if prep.kind == "pre":
                outcome, reason = prep.judge(env, ([], []))
                tally(counts, outcome, reason, example, test)
            else:
                olds[id(prep)] = prep.capture(env, copy_values=True)
        return live, olds, test, before

    def leave(self, entry, result):
        live, olds, test, before = entry
        env = dict(self.base, **live)
        env["__result__"] = result

        def example():
            return {
                "test": test,
                "before": before,
                "after": {k: describe(v) for k, v in live.items()},
                "result": describe(result),
            }

        for prep, counts in self.preps:
            if prep.kind != "post":
                continue
            captured, problem = olds[id(prep)]
            if problem:
                tally(counts, "unresolved", problem, test=test)
                continue
            outcome, reason = prep.judge(env, captured)
            tally(counts, outcome, reason, example, test)

    def skip_posts(self):
        for prep, counts in self.preps:
            if prep.kind == "post":
                counts["skipped_raised"] += 1

    def wrapper(self):
        fn = self.fn

        @functools.wraps(fn)
        def referee(*args, **kwargs):
            if getattr(_busy, "on", False):
                return fn(*args, **kwargs)
            self.info["calls"] += 1
            entry = self._quietly(self.enter, args, kwargs)
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                self._quietly(self.skip_posts)
                raise
            if entry is not None:
                self._quietly(self.leave, entry, result)
            return result

        return referee

    def _quietly(self, f, *args):
        """Run a check so it can only count, never stop or change the call."""
        _busy.on = True
        try:
            return f(*args)
        except Exception as e:
            problems = self.info.setdefault("referee_errors", {})
            key = type(e).__name__ + ": " + str(e)[:100]
            problems[key] = problems.get(key, 0) + 1
            return None
        finally:
            _busy.on = False


def install(spec_source, root, imports):
    """Wrap every repo function the spec has rules for. Returns the report,
    which fills in as the tests run."""
    for m in imports:
        importlib.import_module(m)
    tree = ast.parse(spec_source)
    functions = spec_functions(tree, spec_source.splitlines())
    modules = repo_modules(root)
    found = functions_in(modules)
    report = {"functions": {}, "clauses": []}
    for name, spec in functions.items():
        info = {"matched": None, "calls": 0}
        report["functions"][name] = info
        hit = find(name, found)
        if hit is None:
            info["problem"] = "not found in the repo"
            continue
        owner, attr, raw = hit
        fn = getattr(raw, "__func__", raw)
        info["matched"] = fn.__module__ + ":" + fn.__qualname__
        names = match_params(
            spec["params"], list(inspect.signature(fn).parameters)
        )
        if names is None:
            info["problem"] = "parameters %s don't match %s" % (
                spec["params"],
                list(inspect.signature(fn).parameters),
            )
            continue
        base = dict(vars(builtins), **vars(sys.modules[fn.__module__]))
        base.update(shims())
        info["helper_problems"] = helpers(tree, base)
        preps = []
        for clause in spec["clauses"]:
            counts = new_counts(clause)
            counts["function"] = name
            report["clauses"].append(counts)
            preps.append((Prepared(clause), counts))
        wrapped = Referee(fn, names, preps, base, info).wrapper()
        if isinstance(raw, (staticmethod, classmethod)):
            setattr(owner, attr, type(raw)(wrapped))
        else:
            setattr(owner, attr, wrapped)
            # Other modules may already hold the function by name, from
            # "from module import f". Point them at the referee too.
            for m in modules:
                for k, v in list(vars(m).items()):
                    if v is fn:
                        setattr(m, k, wrapped)
    return report


class FailedTests:
    """A pytest plugin that lists the tests that failed, so generated input
    cases can be told apart from the repo's own tests."""

    def __init__(self, report):
        self.failed = report.setdefault("failed_tests", [])

    def pytest_runtest_logreport(self, report):
        if report.failed and report.nodeid not in self.failed:
            self.failed.append(report.nodeid)


def run_calls(path, report):
    """Run the generated calls, each on its own, under the referee."""
    with open(path) as f:
        lines = [x.strip() for x in f if x.strip()]
    namespace = {"__name__": "generated"}
    stats = report.setdefault("generated_calls", {"run": 0, "raised": 0})
    for n, line in enumerate(lines, 1):
        is_import = line.startswith(("import ", "from "))
        if not is_import:
            os.environ["PYTEST_CURRENT_TEST"] = "generated::%d (call)" % n
            stats["run"] += 1
        try:
            exec(line, namespace)
        except Exception:
            if not is_import:
                stats["raised"] += 1
    os.environ.pop("PYTEST_CURRENT_TEST", None)


def main(argv):
    if "--" not in argv:
        sys.exit(__doc__)
    i = argv.index("--")
    mine, pytest_args = argv[:i], argv[i:][1:]
    calls_path = mine[4] if len(mine) == 5 else None
    spec_path, out_path, root, modules = mine[:4]
    with open(spec_path) as f:
        spec_source = f.read()
    report = install(spec_source, root, modules.split(","))
    import pytest

    rc = pytest.main(pytest_args, plugins=[FailedTests(report)])
    report["pytest_exit"] = int(rc)
    if calls_path:
        run_calls(calls_path, report)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
