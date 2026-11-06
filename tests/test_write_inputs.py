from llm.write_inputs import JOB, split_calls


def test_split_calls_keeps_imports_and_valid_calls():
    code = """from mylib.dates import parse_date
# a comment
parse_date('2024-01-31')

parse_date(
parse_date(None)
"""
    imports, calls = split_calls(code)
    assert imports == ["from mylib.dates import parse_date"]
    # The unfinished line is dropped, since it isn't valid on its own.
    assert calls == ["parse_date('2024-01-31')", "parse_date(None)"]


def test_prompt_example_isnt_about_any_task():
    assert "parse_date" in JOB
    assert "requests" not in JOB
