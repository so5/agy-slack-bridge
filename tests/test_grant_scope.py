import json
from pathlib import Path

import pytest

import bridge
import grant_scope as gs

FID = "1VImn9cBkc-6R_RPJ7VMT9FTdEceLT7qu"
ROOT = Path(__file__).resolve().parent.parent
BASE = "/inst/.venv/bin/python -I /inst/tools/gdrive_cli.py --profile STUDY"


@pytest.fixture
def rules():
    return gs.load_rules(ROOT / "tools" / "grant_scopes.json", "/inst")


def test_shipped_rules_cover_the_three_write_commands(rules):
    labels = {r["flags"][0]: r["label"] for r in rules}
    assert labels == {"--folder": "このフォルダへのアップロード",
                      "--parent": "このフォルダへのディレクトリ作成",
                      "--file": "このファイルの上書き"}


@pytest.mark.parametrize("cmd,entry,label", [
    (f"{BASE} upload --folder {FID} /p/a.pdf --name x", f"{BASE} upload --folder {FID}", "このフォルダへのアップロード"),
    (f"{BASE} mkdir --parent {FID} sub", f"{BASE} mkdir --parent {FID}", "このフォルダへのディレクトリ作成"),
    (f"{BASE} update --file {FID} /p/a.pdf", f"{BASE} update --file {FID}", "このファイルの上書き"),
    (f"{BASE} upload --folder {FID}", f"{BASE} upload --folder {FID}", "このフォルダへのアップロード"),
])
def test_entry_ends_at_the_target(rules, cmd, entry, label):
    assert gs.match(cmd, rules) == {"entry_command": entry, "label": label}
    assert gs.is_scope_entry(entry, rules)


@pytest.mark.parametrize("cmd", [
    f"{BASE} upload /p/a.pdf {FID}",                       # old positional order
    f"{BASE} upload /p/a.pdf --folder {FID}",              # flag not right after the subcommand
    f"{BASE} upload --folder",                             # no value
    f"{BASE} upload --folder --name x",                    # value is another flag
    f"{BASE} list {FID}", f"{BASE} mkdir {FID} sub",
    f"/other/python -I /other/gdrive_cli.py --profile S upload --folder {FID} a",
    f"{BASE} upload --folder {FID} a > /tmp/x", f"{BASE} upload --folder {FID} a; rm -rf x",
    f"{BASE} upload --folder 'unterminated", "",
])
def test_everything_else_keeps_the_full_command(rules, cmd):
    assert gs.match(cmd, rules) is None


def test_broken_or_missing_rule_file_means_no_rules(tmp_path):
    assert gs.load_rules(tmp_path / "missing.json", "/inst") == []
    (tmp_path / "bad.json").write_text("{not json")
    assert gs.load_rules(tmp_path / "bad.json", "/inst") == []
    (tmp_path / "odd.json").write_text(json.dumps([{"prefix": ["x"]}]))
    assert gs.load_rules(tmp_path / "odd.json", "/inst") == []


def _err(cmd):
    return {"tool_name": "run_command", "parameters": {"CommandLine": cmd}, "message": "user denied permission"}


def test_bridge_grants_the_scoped_entry_and_asks_with_the_label(rules, monkeypatch):
    monkeypatch.setattr(bridge, "_GRANT_SCOPE_RULES", rules)
    cmd = f"{BASE} upload --folder {FID} /p/a.pdf"
    g = bridge._grantable_entry(_err(cmd))
    assert g["entry"] == f"command({BASE} upload --folder {FID})" and g["describe"] == cmd
    line = bridge._permission_line(_err(cmd))
    assert line.startswith("「このフォルダへのアップロード」を許可しますか?") and "a.pdf" in line
    blocks = bridge._build_reply_blocks("x", [_err(cmd)], "rid")
    button = next(b for b in blocks if b.get("block_id", "").startswith("perm_0"))["elements"][0]
    assert json.loads(button["value"])["label"] == "このフォルダへのアップロード"
    plain = bridge._grantable_entry(_err("git status"))
    assert plain == {"entry": "command(git status)", "describe": "git status"}
    assert bridge._permission_line(_err("git status")) == bridge._describe_tool_error(_err("git status"))


def test_scoped_grant_is_kept_for_the_conversation(rules, monkeypatch):
    monkeypatch.setattr(bridge, "_GRANT_SCOPE_RULES", rules)
    monkeypatch.setattr(bridge, "_project_root", lambda pid: None)
    bridge._KEPT_SCRIPT_GRANTS.clear()
    bridge._keep_for_conversation("p", "c1", f"command({BASE} upload --folder {FID})")
    bridge._keep_for_conversation("p", "c1", f"command({BASE} upload /p/a.pdf {FID})")   # full-text: not scoped
    assert bridge._KEPT_SCRIPT_GRANTS["c1"] == {f"command({BASE} upload --folder {FID})"}
    bridge._KEPT_SCRIPT_GRANTS.clear()
