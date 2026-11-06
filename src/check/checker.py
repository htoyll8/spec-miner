"""Checks a Nagini spec's clauses on recorded calls, inside the container.

For each function in the spec with Requires or Ensures (methods named
Class.method), each clause is evaluated on every recorded call of that
function: Requires on the arguments before the call, Ensures on them after
it, with Old(x) read before the call and Result() the recorded result.
Ensures is skipped on calls that raised. Nagini is never imported: the spec
is read with ast.

Each clause gets counts:
  held        true on a call
  violated    false on a call; the first such call is kept as an example
  vacuous     Implies(a, b) where a was false, so it said nothing
  unresolved  couldn't be evaluated, with the reasons

Names in a clause are, in order: the call's arguments, helper functions the
spec defines, and the repo module the function lives in. So a class like
CaseInsensitiveDict is the repo's real one, never a stub the spec defines.
A helper whose body is only ..., pass or a docstring has no behavior, so a
clause that calls it is unresolved, not violated.

Standalone and Python 3.6+, like tracing/tracer.py.

    python checker.py SPEC.py CALLS.pkl OUT.json
"""

import ast
import builtins
import copy
import importlib
import json
import pickle
import sys

CONTRACTS = {"Requires": "pre", "Ensures": "post"}
# Contract calls that say nothing about values, so they're no-ops here.
NO_OPS = (
    "Invariant",
    "Assert",
    "Assume",
    "Fold",
    "Unfold",
    "Inhale",
    "Exhale",
)


class StubHelper(Exception):
    """A spec helper with no body was called."""


def shims():
    """Stand-ins for nagini_contracts, for evaluating clauses as Python."""

    def nothing(*args, **kwargs):
        return None

    def same(f=None, *args, **kwargs):
        return f

    out = {name: nothing for name in tuple(CONTRACTS) + NO_OPS}
    out.update(
        Old=lambda x: x,
        Implies=lambda a, b: (not a) or b,
        Pure=same,
        Predicate=same,
        Acc=lambda *args: True,
        Rd=lambda *args: True,
        Forall=lambda domain, f: all(f(x) for x in domain),
        Exists=lambda domain, f: any(f(x) for x in domain),
        Unfolding=lambda p, e: e,
    )
    return out


def call_name(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _is_doc_or_dots(v):
    """A docstring or ..., in any Python from 3.6."""
    if isinstance(v, getattr(ast, "Constant", ())):
        return v.value is Ellipsis or isinstance(v.value, str)
    old = tuple(
        getattr(ast, n) for n in ("Str", "Ellipsis") if hasattr(ast, n)
    )
    return isinstance(v, old)


def is_stub(fn):
    """Whether a helper's body has no behavior: only a docstring, ..., pass
    or contract calls."""
    for stmt in fn.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr):
            v = stmt.value
            if _is_doc_or_dots(v):
                continue
            if call_name(v) in tuple(CONTRACTS) + NO_OPS:
                continue
        return False
    return True


def clause_text(lines, node):
    """The clause's source, from its first line until brackets balance."""
    text, depth = [], 0
    start = node.lineno - 1
    for line in lines[start:]:
        text.append(line.strip())
        depth += line.count("(") - line.count(")")
        if depth <= 0:
            break
    return " ".join(text)


def spec_functions(tree, lines):
    """{qualified name: {"params": [...], "clauses": [...]}} for every
    function in the spec with at least one Requires or Ensures."""
    out = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.FunctionDef):
                clauses = []
                for stmt in child.body:
                    kind = CONTRACTS.get(
                        call_name(getattr(stmt, "value", None))
                    )
                    if kind and stmt.value.args:
                        expr = stmt.value.args[0]
                        clauses.append(
                            {
                                "kind": kind,
                                "expr": expr,
                                "text": clause_text(lines, stmt),
                            }
                        )
                if clauses:
                    params = [a.arg for a in child.args.args]
                    for extra in (child.args.vararg, child.args.kwarg):
                        if extra is not None:
                            params.append(extra.arg)
                    out[prefix + child.name] = {
                        "params": params,
                        "clauses": clauses,
                    }

    walk(tree, "")
    return out


def helpers(tree, namespace):
    """The spec's top-level functions, defined in namespace. A stub helper
    becomes a function that raises StubHelper. Returns {name: problem} for
    helpers that couldn't be defined."""
    problems = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if is_stub(node):

            def stub(*args, _name=node.name, **kwargs):
                raise StubHelper(_name)

            namespace[node.name] = stub
            continue
        module = ast.Module(body=[node])
        module.type_ignores = []  # needed from Python 3.8
        try:
            exec(compile(module, "<spec>", "exec"), namespace)
        except Exception as e:
            problems[node.name] = type(e).__name__ + ": " + str(e)
    return problems


class Rewrite(ast.NodeTransformer):
    """Turns Old(x) into a name read before the call, Result() into the
    recorded result, and Implies(a, b) into (not a) or b."""

    def __init__(self):
        self.olds = []

    def visit_Call(self, node):
        name = call_name(node)
        if name == "Old" and len(node.args) == 1:
            self.olds.append(node.args[0])
            return ast.Name(
                id="__old%d" % (len(self.olds) - 1), ctx=ast.Load()
            )
        if name == "Result" and not node.args:
            return ast.Name(id="__result__", ctx=ast.Load())
        node = self.generic_visit(node)
        if name == "Implies" and len(node.args) == 2:
            a, b = node.args
            return ast.BoolOp(
                op=ast.Or(), values=[ast.UnaryOp(op=ast.Not(), operand=a), b]
            )
        return node


def compiled(expr):
    """(code, [code for each Old(x)]) for a clause expression."""
    r = Rewrite()
    body = r.visit(copy.deepcopy(expr))
    return _compile(body), [_compile(copy.deepcopy(x)) for x in r.olds]


def _compile(node):
    tree = ast.Expression(body=node)
    return compile(ast.fix_missing_locations(tree), "<clause>", "eval")


def load(saved):
    """A recorded value, or a reason it isn't available."""
    if not saved or "pickle" not in saved:
        return None, "not saved"
    try:
        return pickle.loads(saved["pickle"]), None
    except Exception as e:
        return None, "can't load: " + type(e).__name__


def values(snapshot, names):
    """{spec parameter: value} from a recorded snapshot, renamed by
    names, which maps each spec parameter to the recorded one."""
    out = {}
    for spec_name, recorded in names.items():
        v, problem = load((snapshot or {}).get(recorded))
        if problem is None:
            out[spec_name] = v
    return out


def match_params(spec_params, recorded):
    """{spec parameter: recorded parameter}, by name, or else by position
    when the counts agree. None if neither works."""
    if all(p in recorded for p in spec_params):
        return {p: p for p in spec_params}
    if len(spec_params) == len(recorded):
        return {p: recorded[i] for i, p in enumerate(spec_params)}
    return None


def module_globals(path):
    """The globals of the repo module a recorded function lives in."""
    name = path[:-3].replace("/", ".") if path.endswith(".py") else path
    if name.endswith(".__init__"):
        name = name.rsplit(".", 1)[0]
    try:
        return dict(vars(importlib.import_module(name))), None
    except Exception as e:
        return {}, "can't import %s: %s" % (name, type(e).__name__)


def evaluate(code, env):
    """(True/False, None) or (None, reason)."""
    try:
        return bool(eval(code, env)), None
    except StubHelper as e:
        return None, "calls helper %s, which has no body" % e
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:100])


class Prepared:
    """A clause compiled once, for checking on many calls.

    olds are its Old(...) parts, read before a call. For Implies(a, b), a is
    compiled on its own too, to tell a vacuous call from one that held.
    """

    def __init__(self, clause):
        self.kind = clause["kind"]
        self.text = clause["text"]
        expr = clause["expr"]
        self.code, self.olds = compiled(expr)
        self.cond = None
        if call_name(expr) == "Implies" and len(expr.args) == 2:
            self.cond = compiled(expr.args[0])

    def capture(self, env, copy_values=False):
        """The Old(...) values, read from env before the call, as
        ([for the clause], [for a]), or (None, reason). A live check copies
        them, since the call may change them."""
        out = []
        cond_olds = self.cond[1] if self.cond else []
        for codes in (self.olds, cond_olds):
            vals = []
            for c in codes:
                v, problem = _eval_value(c, env)
                if problem is None and copy_values:
                    try:
                        v = copy.deepcopy(v)
                    except Exception as e:
                        problem = "can't copy: " + type(e).__name__
                if problem:
                    return None, "Old(...): " + problem
                vals.append(v)
            out.append(vals)
        return out, None

    def judge(self, env, olds):
        """("held" | "violated" | "vacuous" | "unresolved", reason)."""
        if self.cond is not None:
            ok, _ = evaluate(self.cond[0], _with_olds(env, olds[1]))
            if ok is False:
                return "vacuous", None
        ok, reason = evaluate(self.code, _with_olds(env, olds[0]))
        if ok is None:
            return "unresolved", reason
        return ("held" if ok else "violated"), None


def _with_olds(env, vals):
    out = dict(env)
    out.update({"__old%d" % k: v for k, v in enumerate(vals)})
    return out


def new_counts(clause):
    return {
        "kind": clause["kind"],
        "text": clause["text"],
        "held": 0,
        "violated": 0,
        "vacuous": 0,
        "unresolved": 0,
        "skipped_raised": 0,
        "reasons": {},
        "example": None,
        # Every call that violated the clause, by test, with counts.
        "violated_by": {},
        # Every outcome, by test: {test: {"held": n, "violated": n, ...}},
        # so two runs can be compared call by call.
        "by_test": {},
        # The first violating call of each test, with its values.
        "examples": {},
    }


def tally(counts, outcome, reason=None, example=None, test=None):
    """Add one call's outcome to a clause's counts. test is the call's test,
    or generated::<line>. example is called on each violation, to say more
    about the call."""
    counts[outcome] += 1
    per = counts["by_test"].setdefault(str(test), {})
    per[outcome] = per.get(outcome, 0) + 1
    if reason:
        counts["reasons"][reason] = counts["reasons"].get(reason, 0) + 1
    if outcome == "violated" and example:
        e = example()
        if counts["example"] is None:
            counts["example"] = e
        by = counts["violated_by"]
        by[e.get("test")] = by.get(e.get("test"), 0) + 1
        counts["examples"].setdefault(str(test), e)


def check_clause(clause, calls, names, base):
    """One clause's counts over recorded calls."""
    prep = Prepared(clause)
    out = new_counts(clause)
    for call in calls:
        if prep.kind == "post" and call["raised"]:
            out["skipped_raised"] += 1
            continue
        before = values(call["before"], names)
        now = before if prep.kind == "pre" else values(call["after"], names)
        env = dict(base)
        env.update(now)
        if prep.kind == "post":
            result, problem = load(call["result"])
            if problem is None:
                env["__result__"] = result
        olds, problem = prep.capture(dict(base, **before))
        if problem:
            tally(out, "unresolved", problem, test=call.get("test"))
            continue
        outcome, reason = prep.judge(env, olds)
        tally(
            out,
            outcome,
            reason,
            lambda call=call, before=before: {
                "test": call.get("test"),
                "before": {k: _short(v) for k, v in before.items()},
            },
            test=call.get("test"),
        )
    return out


def _eval_value(code, env):
    try:
        return eval(code, env), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:100])


def _short(v):
    return describe(v)


def describe(v, limit=300):
    """A value as short text. An object shows its fields, like
    PreparedRequest{'method': 'POST', 'headers': {...}}, since its own repr
    often hides them."""
    try:
        fields = getattr(v, "__dict__", None)
        if isinstance(fields, dict) and not isinstance(v, type):
            text = type(v).__name__ + repr(fields)
        else:
            text = repr(v)
    except Exception:
        text = "<%s>" % type(v).__name__
    return text if len(text) <= limit else text[:limit] + "..."


def check(spec_source, calls):
    tree = ast.parse(spec_source)
    lines = spec_source.splitlines()
    functions = spec_functions(tree, lines)
    by_name = {}
    for c in calls:
        by_name.setdefault(c["function"][2], []).append(c)
    report = {"functions": {}, "clauses": []}
    for name, spec in functions.items():
        info = {"matched": None, "calls": 0}
        report["functions"][name] = info
        matched = name if name in by_name else None
        if matched is None:
            # A method the spec put in another class, or a function it
            # made a method: match on the last part of the name if unique.
            last = name.split(".")[-1]
            options = [n for n in by_name if n.split(".")[-1] == last]
            if len(options) == 1:
                matched = options[0]
        if matched is None:
            info["problem"] = "no recorded calls"
            continue
        fcalls = by_name[matched]
        info.update(matched=matched, calls=len(fcalls))
        recorded = list(fcalls[0]["before"])
        names = match_params(spec["params"], recorded)
        if names is None:
            info["problem"] = "parameters %s don't match recorded %s" % (
                spec["params"],
                recorded,
            )
            continue
        base, problem = module_globals(fcalls[0]["function"][0])
        if problem:
            info["problem"] = problem
        base = dict(vars(builtins), **base)
        base.update(shims())
        info["helper_problems"] = helpers(tree, base)
        for clause in spec["clauses"]:
            result = check_clause(clause, fcalls, names, base)
            result["function"] = name
            report["clauses"].append(result)
    return report


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    spec_path, calls_path, out_path = argv
    with open(spec_path) as f:
        spec_source = f.read()
    with open(calls_path, "rb") as f:
        calls = pickle.load(f)["calls"]
    report = check(spec_source, calls)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
