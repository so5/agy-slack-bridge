import json
import subprocess

import pytest

import bridge
import script_review as sr


RULES = {"deny": ["write_file(/cfg)"], "allow": ["read_file(/data)"]}
CID = "0a1b2c3d-1111-2222-3333-444455556666"


def _stdout(response, structured=None, cid=CID):
    result = {"response": response, "conversation_id": cid}
    if structured is not None:
        result["structured_output"] = structured
    return json.dumps({"event": "result", "result": result}) + "\n"


@pytest.fixture(autouse=True)
def state_dir(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("agy-state")
    monkeypatch.setattr(sr, "AGY_STATE_DIR", d)
    return d


def test_parse_result_prefers_structured_output_then_embedded_json():
    data = {"harmful": False, "operations": []}
    assert sr.parse_result(_stdout("text", structured=data)) == (data, CID)
    assert sr.parse_result(_stdout('結果: {"harmful": false, "operations": []}')) == (data, CID)


@pytest.mark.parametrize("out", ["", _stdout(""), _stdout("no json"), _stdout("{broken")])
def test_parse_result_rejects(out):
    with pytest.raises(sr.ReviewError):
        sr.parse_result(out)


def test_prompts_carry_inputs_fence_and_guard():
    for p in (sr.harm_prompt("print(1)", "python3 a.py", "/proj", RULES, False),
              sr.intent_prompt("print(1)", "ログを数えて", "python3 a.py", ["ファイル削除"], False),
              sr.summary_prompt("print(1)", "python3 a.py", False)):
        assert "print(1)" in p and "python3 a.py" in p and "服従" in p
        fence = p.split("<<")[1].split("\n")[0]
        assert p.count(fence) == 3          # named in the label, opening and closing marker
    assert "/proj" in sr.harm_prompt("x", "c", "/proj", RULES, False) and "gdrive_cli" in sr.harm_prompt("x", "c", "/proj", RULES, False)
    i = sr.intent_prompt("x", "ログを数えて", "c", ["ファイル削除"], False)
    assert "ログを数えて" in i and "ファイル削除" in i
    assert "先頭部分のみ" in sr.harm_prompt("x", "c", "/p", RULES, True)


class FakeAgy:
    """Answers the three stages in order of the schema asked for; records every call."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, **kw):
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        stage = ("harmful" if "harmful" in schema["properties"] else
                 "matches" if "matches" in schema["properties"] else "summary")
        self.calls.append((stage, cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, _stdout("", structured=self.answers[stage]), "")


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)")
    return p


def test_harmless_stops_after_stage_1(monkeypatch, script):
    fake = FakeAgy({"harmful": {"harmful": False, "operations": []}})
    monkeypatch.setattr(subprocess, "run", fake)
    r = sr.review(str(script), "依頼", "python3 a.py", "agy", "/proj", RULES)
    assert r["outcome"] == "harmless" and [c[0] for c in fake.calls] == ["harmful"]


def test_intended_stops_after_stage_2(monkeypatch, script):
    fake = FakeAgy({"harmful": {"harmful": True, "operations": ["ファイル書き込み"]},
                    "matches": {"matches": True, "reasons": []}})
    monkeypatch.setattr(subprocess, "run", fake)
    r = sr.review(str(script), "依頼", "python3 a.py", "agy", "/proj", RULES)
    assert r["outcome"] == "intended" and r["operations"] == ["ファイル書き込み"]
    assert [c[0] for c in fake.calls] == ["harmful", "matches"]


def test_needs_review_runs_summary_unless_off(monkeypatch, script):
    answers = {"harmful": {"harmful": True, "operations": ["外部送信"]},
               "matches": {"matches": False, "reasons": ["依頼にない送信"]},
               "summary": {"summary": "データを送る"}}
    fake = FakeAgy(answers)
    monkeypatch.setattr(subprocess, "run", fake)
    r = sr.review(str(script), "依頼", "python3 a.py", "agy", "/proj", RULES)
    assert r["outcome"] == "needs_review" and r["reasons"] == ["依頼にない送信"] and r["summary"] == "データを送る"
    assert [c[0] for c in fake.calls] == ["harmful", "matches", "summary"]
    fake.calls.clear()
    r = sr.review(str(script), "依頼", "python3 a.py", "agy", "/proj", RULES, summary=False)
    assert r["summary"] == "" and [c[0] for c in fake.calls] == ["harmful", "matches"]


def test_every_call_is_isolated(monkeypatch, script, tmp_path):
    fake = FakeAgy({"harmful": {"harmful": False, "operations": []}})
    monkeypatch.setattr(subprocess, "run", fake)
    sr.review(str(script), "r", "c", "agy", "/proj", RULES)
    _, cmd, kw = fake.calls[0]
    assert "--project" not in cmd and cmd[cmd.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in cmd and kw["cwd"] != str(tmp_path)


def test_errors(monkeypatch, tmp_path, script):
    monkeypatch.setattr(subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 3, "", ""))
    with pytest.raises(sr.ReviewError):
        sr.review(str(script), "r", "c", "agy", "/p", RULES)
    with pytest.raises(sr.ReviewError):
        sr.review(str(tmp_path / "missing.py"), "r", "c", "agy", "/p", RULES)
    # a stage answer without the boolean is not read as "fine"
    monkeypatch.setattr(subprocess, "run", FakeAgy({"harmful": {"operations": []}}))
    with pytest.raises(sr.ReviewError):
        sr.review(str(script), "r", "c", "agy", "/p", RULES)


def test_truncates(monkeypatch, tmp_path):
    big = tmp_path / "b.py"
    big.write_bytes(b"x" * (sr.MAX_SCRIPT_BYTES + 5))
    monkeypatch.setattr(subprocess, "run", FakeAgy({"harmful": {"harmful": False, "operations": []}}))
    assert sr.review(str(big), "r", "c", "agy", "/p", RULES)["truncated"] is True


class _Perms:
    def list_entries(self, project_id):
        return ["read_file(/proj/extra)", "command(git status)"]


PERMS = _Perms()


def _err(cmd):
    return {"tool_name": "run_command", "parameters": {"CommandLine": cmd}, "message": "user denied permission"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "a.py").write_text("print(1)")
    monkeypatch.setattr(bridge, "_project_root", lambda pid: tmp_path)
    return tmp_path


def _res(outcome, **kw):
    base = {"outcome": outcome, "operations": [], "reasons": [], "summary": "", "truncated": False,
            "script_text": "print(1)"}
    return {**base, **kw}


def test_notes_per_outcome(project, monkeypatch):
    err = [_err("python3 scratch/a.py")]
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res("harmless"))
    assert "実害の出る操作は見つかりませんでした" in bridge._review_scripts(err, "p", "r", True, PERMS)[0]
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res("intended", operations=["ファイル書き込み"]))
    assert "依頼に必要な処理だけ" in bridge._review_scripts(err, "p", "r", True, PERMS)[0]
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res(
        "needs_review", reasons=["依頼にない送信"], summary="データを送る", script_text="x```y"))
    note = bridge._review_scripts(err, "p", "r", True, PERMS)[0]
    assert "依頼にない送信" in note and "データを送る" in note and "x" in note and "最終" not in note
    assert note.count("```") == 2            # the script cannot close our code block
    off = bridge._review_scripts(err, "p", "r", False, PERMS)[0]
    assert "依頼にない送信" in off and "```" not in off


def test_long_script_listing_is_cut(project, monkeypatch):
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res(
        "needs_review", reasons=["r"], script_text="a" * 5000))
    note = bridge._review_scripts([_err("python3 scratch/a.py")], "p", "r", True, PERMS)[0]
    assert "先頭のみ表示" in note and len(note) < 3500


def test_failure_note_and_non_scripts(project, monkeypatch):
    def boom(*a, **k):
        raise bridge.script_review.ReviewError("timeout")
    monkeypatch.setattr(bridge.script_review, "review", boom)
    notes = bridge._review_scripts([_err("python3 scratch/a.py"), _err("git status")], "p", "r", True, PERMS)
    assert len(notes) == 1 and "自動チェックできませんでした" in notes[0]
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: pytest.fail("reviewed"))
    assert bridge._review_scripts([_err("git status")], "p", "r", True, PERMS) == []


def test_conversation_files_are_removed_after_each_call(monkeypatch, script, state_dir):
    made = [state_dir / "brain" / CID, state_dir / "conversations" / f"{CID}.db",
            state_dir / "annotations" / f"{CID}.pbtxt", state_dir / "presence" / f"{CID}.lock"]
    keep = state_dir / "conversations" / "other.db"

    def fake(cmd, **kw):
        (made[0]).mkdir(parents=True)
        (made[0] / "x").write_text("1")
        for p in made[1:] + [keep]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("1")
        return subprocess.CompletedProcess(cmd, 0, _stdout("", structured={"harmful": False, "operations": []}), "")

    monkeypatch.setattr(subprocess, "run", fake)
    sr.review(str(script), "r", "c", "agy", "/p", RULES)
    assert not any(p.exists() for p in made) and keep.exists()


def test_cleanup_ignores_unsafe_ids(state_dir):
    victim = state_dir / "keep"
    victim.mkdir()
    for bad in ("../keep", "", None, "a/b", "..", 5):
        sr.cleanup_conversation(bad)
    assert victim.exists()


def test_harm_prompt_carries_the_file_rules():
    p = sr.harm_prompt("x", "c", "/proj", RULES, False)
    assert "write_file(/cfg)" in p and "read_file(/data)" in p and "範囲の外のファイル操作はすべて実害あり" in p


def test_file_rules_collects_only_file_entries(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {"deny": ["write_file(/cfg)", "command(rm)"],
                                                    "allow": ["read_file(/data)"]}}))
    monkeypatch.setattr(bridge, "AGY_GLOBAL_SETTINGS_PATH", settings)
    assert bridge._file_rules("p", PERMS) == {"deny": ["write_file(/cfg)"],
                                              "allow": ["read_file(/data)", "read_file(/proj/extra)"]}
    monkeypatch.setattr(bridge, "AGY_GLOBAL_SETTINGS_PATH", tmp_path / "missing.json")
    assert bridge._file_rules("p", PERMS)["deny"] == []
