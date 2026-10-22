import os
import stat

import pytest

import install


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "prefix", tmp_path / "cfg"


def run(dirs, *extra):
    prefix, cfg = dirs
    install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--no-venv", *extra])


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
