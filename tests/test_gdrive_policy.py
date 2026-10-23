import json
import os
import stat

import pytest

import gdrive_cli as g
import gdrive_policy as gp

CRED = {"kind": "service_account", "credentials": {"command": ["cat", "/x"]}, "scopes": {"read": "r"}}
FOLDER = "1AbCdEfGhIjKlMnOp"


def test_defaults_are_the_cli_defaults():
    d = gp.default_policy()
    assert d["commands"] == list(g.READ_COMMAND_NAMES + g.WRITE_COMMAND_NAMES)
    assert d["allowed_folders"] == [] and d["deny_names"] == g.DENY_UPLOAD_NAMES_DEFAULT


def test_default_policy_writes_nothing_and_keeps_credentials_and_scopes():
    assert gp.apply_policy(CRED, gp.default_policy()) == CRED


def test_only_deviations_are_written_and_round_trip():
    policy = gp.default_policy()
    policy["commands"] = ["list", "read", "upload"]
    policy["allowed_folders"] = [FOLDER]
    policy["deny_dirs"] = policy["deny_dirs"] + ["private"]
    data = gp.apply_policy(CRED, policy)
    assert data["commands"] == ["list", "read", "upload"]
    assert data["allowed_folders"] == [FOLDER]
    assert data["upload_deny"] == {"dirs": g.DENY_UPLOAD_DIRS_DEFAULT + ["private"]}
    assert gp.policy_from_data(data) == policy
    assert gp.apply_policy(data, gp.default_policy()) == CRED  # back to defaults


def test_invalid_policy_is_rejected():
    for field, value in (("commands", ["delete"]), ("allowed_folders", ["not an id"])):
        policy = gp.default_policy()
        policy[field] = value
        with pytest.raises(g.ProfileError):
            gp.apply_policy(CRED, policy)


def test_diff_and_loosening():
    old = gp.default_policy()
    old["commands"] = ["list", "read"]
    old["allowed_folders"] = [FOLDER]
    tighter = dict(old, commands=["list"], allowed_folders=[FOLDER, "2AbCdEfGhIjKlMnOp"][:1])
    assert gp.loosening(old, tighter) == []
    assert gp.diff(old, tighter) == ["コマンド: -read"]

    looser = dict(old, commands=["list", "read", "upload"], allowed_folders=[],
                  deny_names=old["deny_names"][1:], deny_dirs=old["deny_dirs"] + ["x"])
    reasons = gp.loosening(old, looser)
    assert any("upload" in r for r in reasons)
    assert any("制限を解除" in r for r in reasons)
    assert any(old["deny_names"][0] in r for r in reasons)
    assert not any("x" == r for r in reasons)  # adding a deny pattern is not loosening

    more_folders = dict(old, allowed_folders=[FOLDER, "2AbCdEfGhIjKlMnOp"])
    assert gp.loosening(old, more_folders) == ["許可フォルダを増やす: 2AbCdEfGhIjKlMnOp"]
    # restricting an unrestricted profile is tightening
    assert gp.loosening(gp.default_policy(), dict(gp.default_policy(), allowed_folders=[FOLDER])) == []


def test_write_profile_is_private_atomic_and_validated(tmp_path):
    path = tmp_path / "cfg" / "gdrive" / "p.json"
    gp.write_profile(path, gp.new_profile("oauth", ["op", "read", "ref"]))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) & 0o077 == 0
    assert json.loads(path.read_text())["credentials"]["command"] == ["op", "read", "ref"]
    assert not list(path.parent.glob("*.tmp"))
    assert g.parse_profile(json.loads(path.read_text()))["kind"] == "oauth"

    with pytest.raises(g.ProfileError):
        gp.write_profile(path, {"kind": "oauth", "credentials": {"command": []}})
    assert json.loads(path.read_text())["credentials"]["command"] == ["op", "read", "ref"]  # untouched


def test_write_profile_removes_group_other_write_from_an_existing_dir(tmp_path):
    d = tmp_path / "gdrive"
    d.mkdir()
    os.chmod(d, 0o777)
    gp.write_profile(d / "p.json", gp.new_profile("oauth", ["x"]))
    assert stat.S_IMODE(os.stat(d).st_mode) & 0o022 == 0


@pytest.mark.parametrize("text,expected", [
    (FOLDER, FOLDER),
    (f"https://drive.google.com/drive/folders/{FOLDER}", FOLDER),
    (f"https://drive.google.com/drive/u/0/folders/{FOLDER}?usp=sharing", FOLDER),
    (f"https://drive.google.com/open?id={FOLDER}", FOLDER),
    (f"https://docs.google.com/document/d/{FOLDER}/edit", FOLDER),
    ("short", None), ("https://example.com/", None), ("not a url or id", None),
])
def test_extract_drive_id(text, expected):
    assert g.extract_drive_id(text) == expected
