"""The live referee on a small fake repo, with calls made directly."""

import sys
import textwrap

import pytest

from check.live import install, run_calls

REPO = {
    "livepkg/__init__.py": "",
    "livepkg/models.py": """
        class Headers(dict):
            pass

        class Prepared:
            def __init__(self, method):
                self.method = method
                self.headers = Headers()

            def prepare_content_length(self, body):
                self.headers['Content-Length'] = '0'  # the bug

            def fail(self):
                raise ValueError("no")

        def size(x):
            return len(x)
    """,
    "livepkg/other.py": """
        from livepkg.models import size

        def use(x):
            return size(x)
    """,
}

SPEC = """
from nagini_contracts.contracts import *

class Headers(dict):
    '''A stub, which must not replace the real one.'''

def abstract(x) -> bool:
    ...

class Prepared:
    def prepare_content_length(self, body):
        Requires(isinstance(self.headers, Headers))
        Ensures(Implies(body is None, 'Content-Length' not in self.headers))
        Ensures(Implies(self.method == 'POST',
                        'Content-Length' in self.headers))
        Ensures(len(self.headers) == len(Old(self.headers)) + 1)
        Ensures(abstract(body))

    def fail(self):
        Ensures(Result() is None)

def size(x):
    Ensures(Result() == len(x))
    Ensures(Result() > 100)
"""


@pytest.fixture
def repo(tmp_path):
    for name, text in REPO.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for m in [m for m in sys.modules if m.startswith("livepkg")]:
        del sys.modules[m]


def counts(report, text):
    c = next(c for c in report["clauses"] if c["text"] == text)
    return (c["held"], c["violated"], c["vacuous"], c["unresolved"])


def test_referee_counts_each_outcome(repo):
    report = install(SPEC, repo, ["livepkg.models", "livepkg.other"])
    models = sys.modules["livepkg.models"]
    models.Prepared("GET").prepare_content_length(None)
    f = report["functions"]["Prepared.prepare_content_length"]
    assert f["matched"] == "livepkg.models:Prepared.prepare_content_length"
    assert f["calls"] == 1
    #                                                held violated vac unres
    assert counts(report, "Requires(isinstance(self.headers, Headers))") == (
        1,
        0,
        0,
        0,
    )
    bug = (
        "Ensures(Implies(body is None, 'Content-Length' not in self.headers))"
    )
    assert counts(report, bug) == (0, 1, 0, 0)
    post = [c["text"] for c in report["clauses"] if "'POST'" in c["text"]][0]
    assert counts(report, post) == (0, 0, 1, 0)
    old = "Ensures(len(self.headers) == len(Old(self.headers)) + 1)"
    assert counts(report, old) == (1, 0, 0, 0)  # the copy kept the old size
    assert counts(report, "Ensures(abstract(body))") == (0, 0, 0, 1)


def test_referee_never_changes_the_call(repo):
    install(SPEC, repo, ["livepkg.models", "livepkg.other"])
    models = sys.modules["livepkg.models"]
    p = models.Prepared("GET")
    p.prepare_content_length(None)
    assert p.headers == {"Content-Length": "0"}
    assert models.size("abc") == 3
    with pytest.raises(ValueError):
        p.fail()


def test_posts_are_skipped_when_the_call_raises(repo):
    report = install(SPEC, repo, ["livepkg.models", "livepkg.other"])
    with pytest.raises(ValueError):
        sys.modules["livepkg.models"].Prepared("GET").fail()
    c = next(c for c in report["clauses"] if c["function"] == "Prepared.fail")
    assert c["skipped_raised"] == 1


def test_functions_imported_by_name_elsewhere_are_wrapped(repo):
    """other.py did "from livepkg.models import size" before the referee
    went in, so its copy has to be replaced too."""
    report = install(SPEC, repo, ["livepkg.models", "livepkg.other"])
    sys.modules["livepkg.other"].use("abcd")
    assert counts(report, "Ensures(Result() == len(x))") == (1, 0, 0, 0)
    assert counts(report, "Ensures(Result() > 100)") == (0, 1, 0, 0)


def test_unknown_functions_are_reported(repo):
    spec = "def nowhere(x):\n    Ensures(x)\n"
    report = install(spec, repo, ["livepkg.models"])
    assert report["functions"]["nowhere"]["problem"] == "not found in the repo"


def test_generated_calls_run_under_the_referee(repo, tmp_path):
    report = install(SPEC, repo, ["livepkg.models", "livepkg.other"])
    calls = tmp_path / "calls.py"
    calls.write_text(
        "from livepkg.models import Prepared\n"
        "Prepared('GET').prepare_content_length(None)\n"
        "Prepared('GET').prepare_content_length('abc')\n"
        "undefined_name()\n"
    )
    run_calls(calls, report)
    assert report["generated_calls"] == {"run": 3, "raised": 1}
    bug = (
        "Ensures(Implies(body is None, 'Content-Length' not in self.headers))"
    )
    c = next(c for c in report["clauses"] if c["text"] == bug)
    # Violated on the first call (no body), vacuous on the second.
    assert (c["held"], c["violated"], c["vacuous"]) == (0, 1, 1)
    assert c["example"]["test"] == "generated::2"


def test_violations_keep_the_values_that_caused_them(repo):
    report = install(SPEC, repo, ["livepkg.models", "livepkg.other"])
    sys.modules["livepkg.models"].Prepared("GET").prepare_content_length(None)
    bug = (
        "Ensures(Implies(body is None, 'Content-Length' not in self.headers))"
    )
    c = next(c for c in report["clauses"] if c["text"] == bug)
    (e,) = c["examples"].values()
    # Before the call: GET, no headers. After: the header was added.
    assert "'method': 'GET'" in e["before"]["self"]
    assert "'headers': {}" in e["before"]["self"]
    assert "'Content-Length': '0'" in e["after"]["self"]
    assert e["before"]["body"] == "None"
    assert e["result"] == "None"
