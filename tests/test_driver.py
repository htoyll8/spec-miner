from pathlib import Path

import run as driver

CONFIG = Path(__file__).parent.parent / "exp" / "configs" / "three_arms.toml"


def test_config_loads():
    config = driver.load_config(CONFIG)
    assert len(config["tasks"]) == 10
    assert config["llm"]["budget"] == 7.0
    assert config["specs"]["arms"] == ["baseline", "candidates"]
    assert not config["tasks_file"].startswith("~")


def test_a_step_is_done_when_all_its_outputs_exist(tmp_path):
    config = driver.load_config(CONFIG)
    task = "psf__requests-1142"
    outs = driver.outputs(tmp_path, task, config)
    assert list(outs) == [name for name, _, _ in driver.STEPS]
    # Both arms' specs, and each arm with and without the inputs.
    assert len(outs["specs"]) == 2
    assert len(outs["mutants"]) == 4
    for p in outs["mine"]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
    assert all(p.exists() for p in outs["mine"])
    assert not any(p.exists() for p in outs["specs"])
