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


def test_inner_command_on_buggy_code():
    cmd = inner_command(TASK, "buggy", 2)
    assert "git apply /out/test.patch &&" in cmd
    # Buggy code: the gold patch is never applied.
    assert "gold" not in cmd
    changed = "/testbed/requests/models.py,/testbed/requests/utils.py"
    assert f"tracer.py discover /testbed {changed} /out/keys.json" in cmd
    assert "/out/calls.pkl" in cmd
    assert cmd.index("discover") < cmd.index("record")
    assert cmd.index("record") < cmd.index(
        "show /out/calls.pkl > /out/calls.txt"
    )
    assert "flatten.py /out/calls.pkl 2 /out/flat.pkl" in cmd
    files = "requests/models.py requests/utils.py"
    assert f"cp --parents {files} /out/src/" in cmd
    # The self-check runs first, so a broken tracer stops the run early.
    assert cmd.index("selfcheck.py") < cmd.index("discover")


def test_inner_command_on_fixed_code():
    cmd = inner_command(TASK, "fixed", 2)
    assert "git apply /out/test.patch /out/gold.patch &&" in cmd


def test_docker_command(tmp_path):
    mounts = {"/src/tracing": "/trace:ro", tmp_path: "/out"}
    cmd = docker_command(TASK["image"], mounts, "echo hi", "spec_x")
    assert cmd[:5] == ["docker", "run", "--rm", "--name", "spec_x"]
    assert "/src/tracing:/trace:ro" in cmd
    assert f"{tmp_path}:/out" in cmd
    assert cmd[-4:] == [TASK["image"], "bash", "-lc", "echo hi"]
