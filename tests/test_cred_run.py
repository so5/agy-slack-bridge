import os
import subprocess
import sys
from pathlib import Path

import pytest

import bridge
import cred_profiles as cp
import cred_run

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "hooks"))
import script_integrity as si  # noqa: E402

TOKEN = "tok_ABCdef123456"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    cp.write_profile(cp.profile_path("op"), {"OP_SERVICE_ACCOUNT_TOKEN": TOKEN})
    return tmp_path / "cfg"


def test_profile_file_is_private_and_validated(cfg):
    path = cp.profile_path("op")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert cp.read_profile(path) == {"OP_SERVICE_ACCOUNT_TOKEN": TOKEN}
    os.chmod(path, 0o644)
    with pytest.raises(cp.ProfileError):
        cp.read_profile(path)
    with pytest.raises(cp.ProfileError):
        cp.profile_path("../x")


@pytest.mark.parametrize("name", ["PATH", "LD_PRELOAD", "PYTHONPATH", "lower"])
def test_env_names_that_change_loading_are_refused(name):
    assert cp.env_name_problem(name)


def test_mask_covers_encoded_forms_and_short_values_are_ignored():
    import base64
    s = cp.secrets_of({"A": TOKEN, "B": "abc"})
    text = f"x {TOKEN} y {base64.b64encode(TOKEN.encode()).decode()} z abc"
    assert cp.mask(text, s) == "x *** y *** z abc"


def test_command_gets_the_profile_env_and_output_is_masked(cfg, capsys):
    script = cfg.parent / "show.py"
    script.write_text("import os,sys\nprint(os.environ['OP_SERVICE_ACCOUNT_TOKEN'])\nprint('e:' + os.environ['OP_SERVICE_ACCOUNT_TOKEN'], file=sys.stderr)\nsys.exit(3)\n")
    code = cred_run.main(["--profile", "op", "--", sys.executable, str(script)])
    out = capsys.readouterr()
    assert code == 3 and TOKEN not in out.out + out.err and out.out.strip() == "***" and "e:***" in out.err


@pytest.mark.parametrize("cmd", [["env"], ["printenv", "X"], ["python3", "-c", "print(1)"], ["bash", "-c", "echo"],
                                 ["op", "run", "--", "env"], ["op", "run", "--", "node", "-e", "1"],
                                 ["cat", "/proc/self/environ"]])
def test_environment_printing_commands_are_refused(cfg, cmd, capsys):
    assert cred_run.main(["--profile", "op", "--", *cmd]) == 2
    assert TOKEN not in capsys.readouterr().err


def test_a_script_run_is_not_refused():
    assert cred_run.refusal(["op", "run", "--env-file=.env", "--", "python", "scratch/x.py", "-c", "cfg"]) is None


def test_missing_profile_and_command_do_not_leak(cfg, capsys):
    assert cred_run.main(["--profile", "nope", "--", "true"]) == 2
    assert cred_run.main(["--profile", "op", "--", "definitely-not-a-command"]) == 127
    assert cred_run.main(["--profile", "op"]) == 2


CMD = "/p/.venv/bin/python -I /p/tools/cred_run.py --profile op -- op run --env-file=.env -- python scratch/x.py"


def test_hook_and_bridge_look_past_the_wrapper_to_the_script(tmp_path):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "x.py").write_text("print(1)\n")
    assert si.find_script_targets(CMD, str(tmp_path)) == [str(tmp_path / "scratch" / "x.py")]
    assert bridge._resolve_permanent_script_path(f"command({CMD})", tmp_path) == str(tmp_path / "scratch" / "x.py")
    direct = "/p/.venv/bin/python -I /p/tools/cred_run.py --profile op -- python scratch/x.py"
    assert si.find_script_targets(direct, str(tmp_path)) == [str(tmp_path / "scratch" / "x.py")]


def test_unwrap_agrees_between_hook_and_bridge():
    for cmd in (CMD, "python3 a.py", "x/cred_run.py --profile op -- ./b.sh", "cred_run.py --profile op -- op read x"):
        toks = cmd.split()
        assert si.unwrap_credential_command(toks) == bridge._unwrap_credential_command(toks)


def test_outgoing_text_masks_every_profile_value(cfg, monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG_PATH", cfg / "config.yaml")
    assert cp.mask(f"leaked {TOKEN}", cp.all_secrets(str(cfg))) == "leaked ***"
