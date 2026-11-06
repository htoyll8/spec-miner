from llm.write_spec import JOB, build_prompt, candidates_text

TASK = {"problem_statement": "requests.get is ALWAYS sending content length"}
CANDIDATES = {
    "functions": {
        "PreparedRequest.prepare_content_length": {
            "rules": [
                {
                    "kind": "post",
                    "rule": "('Content-Length' in after(self.headers))",
                    "support": 5,
                }
            ]
        },
        "default_hooks": {"skipped": "no source"},
    }
}


def trace_dir(tmp_path):
    src = tmp_path / "src" / "requests"
    src.mkdir(parents=True)
    (src / "models.py").write_text("def prepare_content_length(self, body):\n")
    return tmp_path


def test_baseline_arm_has_issue_code_and_job(tmp_path):
    prompt = build_prompt("psf__requests-1142", TASK, trace_dir(tmp_path))
    assert "requests.get is ALWAYS sending content length" in prompt
    assert "`requests/models.py`" in prompt
    assert "def prepare_content_length" in prompt
    assert prompt.endswith(JOB)
    assert "Candidate rules" not in prompt


def test_candidates_arm_adds_every_rule(tmp_path):
    prompt = build_prompt("t", TASK, trace_dir(tmp_path), CANDIDATES)
    assert "### PreparedRequest.prepare_content_length" in prompt
    assert "post: ('Content-Length' in after(self.headers))   [5]" in prompt
    assert prompt.endswith(JOB)


def test_skipped_functions_are_left_out():
    assert "default_hooks" not in candidates_text(CANDIDATES)
