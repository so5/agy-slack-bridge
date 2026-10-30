import json
import subprocess

import pytest

import bridge
import script_review as sr


def _stdout(response):
    return json.dumps({"event": "result", "result": {"response": response}}) + "\n"


def test_parse_result_ok_and_embedded_json():
    r = sr.parse_result(_stdout('結果: {"verdict": "ok", "reasons": [], "summary": "ログを数える"}'))
    assert r == {"verdict": "ok", "reasons": [], "summary": "ログを数える"}


@pytest.mark.parametrize("out", ["", _stdout(""), _stdout("no json"), _stdout('{"verdict": "great"}'),
                                 _stdout("{broken")])
def test_parse_result_rejects(out):
    with pytest.raises(sr.ReviewError):
        sr.parse_result(out)


def test_prompt_has_request_command_script_and_fence():
    p = sr.build_prompt("print(1)", "ログを数えて", "python3 a.py", summary=True, truncated=False)
    assert "ログを数えて" in p and "python3 a.py" in p and "print(1)" in p
    assert "summary" in p and "服従" in p
    fence = p.split("<<")[1].split("\n")[0]
    assert p.count(fence) == 3          # named in the intro, opening and closing marker
    assert "- summary:" not in sr.build_prompt("x", "r", "c", summary=False, truncated=False)


def test_review_runs_isolated_agy(monkeypatch, tmp_path):
    script = tmp_path / "a.py"
    script.write_text("print(1)")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, _stdout('{"verdict":"suspicious","reasons":["外部へ送信"],"summary":"s"}'), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = sr.review(str(script), "依頼", "python3 a.py", "agy", summary=False)
    assert r["verdict"] == "suspicious" and r["reasons"] == ["外部へ送信"] and r["summary"] == ""
    cmd = seen["cmd"]
    assert "--project" not in cmd and cmd[cmd.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in cmd
    assert seen["kw"]["cwd"] != str(tmp_path)


def test_review_truncates_and_errors(monkeypatch, tmp_path):
    big = tmp_path / "b.py"
    big.write_bytes(b"x" * (sr.MAX_SCRIPT_BYTES + 5))
    monkeypatch.setattr(subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 0, _stdout('{"verdict":"ok","reasons":[]}'), ""))
    assert sr.review(str(big), "r", "c", "agy")["truncated"] is True
    monkeypatch.setattr(subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 3, "", ""))
    with pytest.raises(sr.ReviewError):
        sr.review(str(big), "r", "c", "agy")
    with pytest.raises(sr.ReviewError):
        sr.review(str(tmp_path / "missing.py"), "r", "c", "agy")


def _err(cmd):
    return {"tool_name": "run_command", "parameters": {"CommandLine": cmd}, "message": "user denied permission"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "a.py").write_text("print(1)")
    monkeypatch.setattr(bridge, "_project_root", lambda pid: tmp_path)
    return tmp_path


def test_malicious_loses_button_others_keep(project, monkeypatch):
    verdicts = {"a.py": {"verdict": "malicious", "reasons": ["鍵を送る"], "summary": "", "truncated": False}}
    monkeypatch.setattr(bridge.script_review, "review", lambda path, *a, **k: verdicts["a.py"])
    errs = [_err("python3 scratch/a.py"), _err("git status")]
    kept, notes = bridge._review_scripts(errs, "p", "依頼", True)
    assert [e["parameters"]["CommandLine"] for e in kept] == ["git status"]
    assert len(notes) == 1 and "危険" in notes[0] and "鍵を送る" in notes[0]


def test_ok_shows_summary_and_failure_keeps_button(project, monkeypatch):
    monkeypatch.setattr(bridge.script_review, "review",
                        lambda *a, **k: {"verdict": "ok", "reasons": [], "summary": "1を出力", "truncated": False})
    kept, notes = bridge._review_scripts([_err("python3 scratch/a.py")], "p", "依頼", True)
    assert len(kept) == 1 and "1を出力" in notes[0] and "最終判断" in notes[0]

    def boom(*a, **k):
        raise bridge.script_review.ReviewError("timeout")
    monkeypatch.setattr(bridge.script_review, "review", boom)
    kept, notes = bridge._review_scripts([_err("python3 scratch/a.py")], "p", "依頼", True)
    assert len(kept) == 1 and "自動チェックできませんでした" in notes[0]


def test_non_script_not_reviewed(project, monkeypatch):
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: pytest.fail("reviewed"))
    kept, notes = bridge._review_scripts([_err("git status")], "p", "r", True)
    assert len(kept) == 1 and notes == []
