from tracing.run import (
    changed_files,
    docker_command,
    inner_command,
    pytest_args,
)

PATCH = """diff --git a/requests/models.py b/requests/models.py
--- a/requests/models.py
+++ b/requests/models.py
@@ -1 +1 @@
-a
+b
diff --git a/requests/utils.py b/requests/utils.py
"""

TASK = {
    "patch": PATCH,
    "test_patch": "",
    "image": "swebench/sweb.eval.x86_64.psf_1776_requests-1142:latest",
    "F2P": ["test_requests.py::RequestsTestCase::test_no_content_length"],
    "P2P": ["test_requests.py::RequestsTestCase::test_basic_building"],
    "test_patch_files": ["test_requests.py"],
}


def test_changed_files():
    assert changed_files(PATCH) == ["requests/models.py", "requests/utils.py"]


def test_pytest_ids_are_passed_as_is():
    assert pytest_args(TASK) == TASK["F2P"] + TASK["P2P"]


def test_bare_names_become_files_and_k():
    sympy = {
        "F2P": ["test_issue_12092"],
        "P2P": ["test_no_args"],
        "test_patch_files": ["sympy/utilities/tests/test_lambdify.py"],
    }
    assert pytest_args(sympy) == [
        "sympy/utilities/tests/test_lambdify.py",
        "-k",
        "test_issue_12092 or test_no_args",
    ]


def test_inner_command():
    cmd = inner_command(TASK)
    assert "git apply /out/test.patch" in cmd
    # Only the test patch: the gold patch is never applied.
    assert "gold" not in cmd
    changed = "/testbed/requests/models.py,/testbed/requests/utils.py"
    assert f"tracer.py discover /testbed {changed} /out/keys.json" in cmd
    assert "/out/calls.pkl" in cmd
    assert cmd.index("discover") < cmd.index("record")
    assert cmd.index("record") < cmd.index(
        "show /out/calls.pkl > /out/calls.txt"
    )
    # The self-check runs first, so a broken tracer stops the run early.
    assert cmd.index("selfcheck.py") < cmd.index("discover")


def test_docker_command_mounts_tracer_read_only(tmp_path):
    cmd = docker_command(TASK, tmp_path, "trace_x")
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert any(a.endswith(":/trace:ro") for a in cmd)
    assert f"{tmp_path}:/out" in cmd
    assert TASK["image"] in cmd
