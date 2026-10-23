import os
import stat

import json

import pytest

import agy_settings
import install


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "prefix", tmp_path / "cfg"


def run(dirs, *extra):
    prefix, cfg = dirs
    install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--no-venv", "--no-setup", *extra])


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_installs_code_readonly_and_config(dirs):
    prefix, cfg = dirs
    run(dirs)
    for rel in ("bridge.py", "requirements.txt", "tools/gdrive_cli.py",
                "tools/setup_agy_project.py", "tools/hooks/script_integrity.py", "tools/hooks.json"):
        assert (prefix / rel).is_file(), rel
        assert mode(prefix / rel) == 0o444
    assert not list(prefix.rglob("__pycache__"))
    assert mode(cfg) == 0o700
    assert mode(cfg / "env") == 0o600
    assert "channels:" in (cfg / "config.yaml").read_text()


def test_unit_points_at_prefix_and_config(dirs):
    prefix, cfg = dirs
    run(dirs)
    unit = (prefix / "systemd/agy-slack-bridge.service").read_text()
    assert f"ExecStart={prefix}/.venv/bin/python {prefix}/bridge.py" in unit
    assert f"AGY_BRIDGE_CONFIG={cfg}/config.yaml" in unit


def test_rerun_never_overwrites_config_but_upgrades_code(dirs):
    prefix, cfg = dirs
    run(dirs)
    (cfg / "config.yaml").write_text("channels: {C1: {project: x}}\n")
    (cfg / "env").write_text("SLACK_BOT_TOKEN=real\n")
    (prefix / "bridge.py").chmod(0o644)
    (prefix / "bridge.py").write_text("tampered")
    (prefix / "tools" / "stale.py").write_text("old")
    run(dirs)
    assert (cfg / "config.yaml").read_text() == "channels: {C1: {project: x}}\n"
    assert (cfg / "env").read_text() == "SLACK_BOT_TOKEN=real\n"
    assert (prefix / "bridge.py").read_text() == (install.REPO_ROOT / "bridge.py").read_text()
    assert not (prefix / "tools" / "stale.py").exists()


def test_refuses_to_install_onto_checkout(tmp_path):
    with pytest.raises(SystemExit):
        install.main(["--prefix", str(install.REPO_ROOT), "--no-venv", "--no-config"])


def test_config_only_leaves_prefix_alone(dirs):
    prefix, cfg = dirs
    run(dirs, "--no-config")
    assert not cfg.exists()
    before = (prefix / "bridge.py").stat().st_mtime_ns
    install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--config-only"])
    assert (cfg / "config.yaml").exists()
    assert (prefix / "bridge.py").stat().st_mtime_ns == before


def test_config_only_without_install_fails(dirs):
    prefix, cfg = dirs
    with pytest.raises(SystemExit):
        install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--config-only"])


def test_settings_get_machine_wide_denies_and_no_allow(dirs):
    prefix, cfg = dirs
    run(dirs)
    data = json.loads(agy_settings.GLOBAL_SETTINGS_PATH.read_text())
    deny = data["permissions"]["deny"]
    for rule in (f"read_file({prefix.resolve()})", f"write_file({prefix.resolve()})",
                 f"read_file({cfg.resolve()})", f"write_file({cfg.resolve()})"):
        assert rule in deny
    assert "allow" not in data["permissions"]
    run(dirs)  # idempotent
    assert json.loads(agy_settings.GLOBAL_SETTINGS_PATH.read_text()) == data


def test_no_settings_flag_leaves_settings_alone(dirs):
    run(dirs, "--no-settings")
    assert not agy_settings.GLOBAL_SETTINGS_PATH.exists()


def test_setup_offer_yes_runs_setup_no_prints_command(dirs, capsys, monkeypatch):
    prefix, cfg = dirs
    ran = []
    for answer, expect_run in (("", True), ("y", True), ("n", False)):
        ran.clear()
        install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--no-venv"],
                     input_fn=lambda _p, a=answer: a, runner=lambda cmd: ran.append(cmd))
        assert bool(ran) == expect_run
    out = capsys.readouterr().out
    assert "後で設定する場合は" in out and f"{prefix.resolve()}/tools/setup_agy_project.py" in out
    assert ran == []
    install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--no-venv"],
                 input_fn=lambda _p: "y", runner=lambda cmd: ran.append(cmd))
    assert ran[0][1:] == [f"{prefix.resolve()}/tools/setup_agy_project.py", "--prefix", str(prefix.resolve()),
                          "--bridge-config-dir", str(cfg.resolve())]
