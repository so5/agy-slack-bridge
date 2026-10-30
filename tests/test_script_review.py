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
    notes, auto = bridge._review_scripts(err, "p", "r", True, PERMS)
    assert notes == [] and auto == err          # normal result: nothing added, granted without a button
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res("harmless", truncated=True))
    notes, auto = bridge._review_scripts(err, "p", "r", True, PERMS)
    assert "先頭のみ" in notes[0] and auto == []
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res("intended", operations=["ファイル書き込み"]))
    notes, auto = bridge._review_scripts(err, "p", "r", True, PERMS)
    intended = notes[0]
    assert auto == []
    assert "次の操作を行なうスクリプトを実行しようとしています" in intended and "\n  • ファイル書き込み" in intended
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res(
        "needs_review", reasons=["依頼にない送信"], summary="データを送る", script_text="x```y"))
    note = bridge._review_scripts(err, "p", "r", True, PERMS)[0][0]
    assert "依頼にない送信" in note and "データを送る" in note and "x" in note and "最終" not in note
    assert note.count("```") == 2            # the script cannot close our code block
    off = bridge._review_scripts(err, "p", "r", False, PERMS)[0][0]
    assert "依頼にない送信" in off and "```" not in off


def test_long_script_listing_is_cut(project, monkeypatch):
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res(
        "needs_review", reasons=["r"], script_text="a" * 5000))
    note = bridge._review_scripts([_err("python3 scratch/a.py")], "p", "r", True, PERMS)[0][0]
    assert "先頭のみ表示" in note and len(note) < 3500


def test_failure_note_and_non_scripts(project, monkeypatch):
    def boom(*a, **k):
        raise bridge.script_review.ReviewError("timeout")
    monkeypatch.setattr(bridge.script_review, "review", boom)
    notes, auto = bridge._review_scripts([_err("python3 scratch/a.py"), _err("git status")], "p", "r", True, PERMS)
    assert len(notes) == 1 and "自動チェックできませんでした" in notes[0] and auto == []
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: pytest.fail("reviewed"))
    assert bridge._review_scripts([_err("git status")], "p", "r", True, PERMS) == ([], [])


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


# --- automatic temporary grant (stage 1 clean) ------------------------------------------

class FakeClient:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw["text"])

    def assistant_threads_setStatus(self, **kw):
        pass


class FakeStore:
    def __init__(self):
        self.temp, self.logged, self.retries = [], [], []

    def put(self, *a): pass
    def put_retry(self, *a): self.retries.append(a)
    def clear_temp_grants(self, cid, pid, keep=()):
        self.cleared_keep = set(keep)
        return []
    def add_temp_grant(self, cid, entry): self.temp.append((cid, entry))
    def log_permission(self, ch, cid, event, entries, user=None): self.logged.append((event, entries))


class FakePermissions:
    def __init__(self):
        self.entries = []

    def add_entry(self, pid, entry):
        self.entries.append(entry)
        return True

    def list_entries(self, pid):
        return list(self.entries)

    def remove_entry(self, pid, entry):
        self.entries.remove(entry)


def _agy_result(errors, response="x"):
    return {"status": "SUCCESS", "conversation_id": "c1", "response": response, "_tool_errors": errors,
            "_tool_steps": 1, "num_turns": 2}


def _drive(monkeypatch, project, outcome, results, **kw):
    monkeypatch.setattr(bridge, "_run_with_status", lambda *a, **k: results.pop(0) if len(results) > 1 else results[0])
    monkeypatch.setattr(bridge, "_script_summary_enabled", lambda c: True)
    monkeypatch.setattr(bridge, "_file_rules", lambda *a: {"deny": [], "allow": []})
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res(outcome, **kw))
    bridge._denial_log.clear()
    client, store, perms = FakeClient(), FakeStore(), FakePermissions()
    bridge._run_and_reply(client, store, perms, "C1", "t1", "p", "c1", "依頼", {})
    return client, store, perms


def test_stage1_clean_script_is_granted_and_rerun_without_a_button(project, monkeypatch):
    err = {"tool_name": "run_command", "parameters": {"CommandLine": "python3 scratch/a.py"},
           "message": "user denied permission to run"}
    results = [_agy_result([err]), _agy_result([], response="done")]
    client, store, perms = _drive(monkeypatch, project, "harmless", results)
    assert perms.entries == ["command(python3 scratch/a.py)"]
    assert store.temp == [("c1", "command(python3 scratch/a.py)")]
    assert ("granted_auto", ["command(python3 scratch/a.py)"]) in store.logged
    assert any("自動で許可" in p for p in client.posts) and any("done" in p for p in client.posts)
    assert store.retries == []                       # no button was prepared


def test_script_that_needs_review_still_gets_a_button(project, monkeypatch):
    err = {"tool_name": "run_command", "parameters": {"CommandLine": "python3 scratch/a.py"},
           "message": "user denied permission to run"}
    client, store, perms = _drive(monkeypatch, project, "needs_review", [_agy_result([err])], reasons=["余計な送信"])
    assert perms.entries == [] and store.temp == [] and len(store.retries) == 1
    assert any("余計な送信" in p for p in client.posts)


def test_auto_grants_count_toward_the_denial_throttle(project, monkeypatch):
    def err(i):
        return {"tool_name": "run_command", "parameters": {"CommandLine": f"python3 scratch/a.py {i}"},
                "message": "user denied permission to run"}
    results = [_agy_result([err(i)]) for i in range(10)]          # a new denied command every time
    client, store, perms = _drive(monkeypatch, project, "harmless", results)
    assert len(store.temp) == bridge.DENIAL_BUTTON_LIMIT           # then the existing limit stops it
    assert store.retries == []                                     # throttled: no button either
    assert any("一時停止" in p for p in client.posts)


def test_over_60kb_script_gets_a_button_not_an_auto_grant(project, monkeypatch):
    err = {"tool_name": "run_command", "parameters": {"CommandLine": "python3 scratch/a.py"},
           "message": "user denied permission to run"}
    client, store, perms = _drive(monkeypatch, project, "harmless", [_agy_result([err])], truncated=True)
    assert perms.entries == [] and len(store.retries) == 1
    assert any("先頭のみ" in p for p in client.posts)


def test_same_command_denied_again_after_auto_grant_stops_the_loop(project, monkeypatch):
    err = {"tool_name": "run_command", "parameters": {"CommandLine": "python3 scratch/a.py"},
           "message": "user denied permission to run"}
    client, store, perms = _drive(monkeypatch, project, "harmless", [_agy_result([err])])   # fails identically
    assert len(store.temp) == 1 and store.retries == []            # already allowed: reported as stuck, no loop


def test_script_notes_say_the_grant_lasts_for_the_conversation(project, monkeypatch):
    err = [_err("python3 scratch/a.py")]
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res("needs_review", reasons=["r"]))
    assert bridge.SCRIPT_GRANT_NOTE in bridge._review_scripts(err, "p", "r", True, PERMS)[0][0]
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: _res("intended", operations=["x"]))
    assert bridge.SCRIPT_GRANT_NOTE in bridge._review_scripts(err, "p", "r", True, PERMS)[0][0]


CHANGED = ("a.py の中身が、前回この同じコマンドを実行した時から変わっています。\n変更内容:\n--- a.py\n+print(2)\n"
           "変更が意図したものか確認し、問題なければ同じ指示をもう一度送ってください。")


def _changed_err(cmd="python3 scratch/a.py"):
    return {"tool_name": "run_command", "parameters": {"CommandLine": cmd}, "message": CHANGED}


def test_changed_script_is_reviewed_and_granted_like_a_first_run(project, monkeypatch):
    results = [_agy_result([_changed_err()]), _agy_result([], response="done")]
    client, store, perms = _drive(monkeypatch, project, "harmless", results)
    assert store.temp == [("c1", "command(python3 scratch/a.py)")]
    assert any("done" in p for p in client.posts) and store.retries == []
    assert not any("print(2)" in p for p in client.posts)        # the diff is not shown


def test_changed_script_with_harm_gets_a_button_and_no_diff(project, monkeypatch):
    client, store, perms = _drive(monkeypatch, project, "needs_review", [_agy_result([_changed_err()])],
                                  reasons=["余計な送信"])
    assert len(store.retries) == 1 and perms.entries == []
    text = "\n".join(client.posts)
    assert "内容が前回から変わっています" in text and "print(2)" not in text


def test_changed_script_already_allowed_is_not_reported_as_stuck(project, monkeypatch):
    results = [_agy_result([_changed_err()]), _agy_result([], response="done")]
    monkeypatch.setattr(FakePermissions, "list_entries", lambda self, pid: ["command(python3 scratch/a.py)"])
    client, store, perms = _drive(monkeypatch, project, "harmless", results)
    assert any("done" in p for p in client.posts)
    assert not any("すでに許可リスト" in p for p in client.posts)


def test_auto_granted_script_survives_the_clean_turn(project, monkeypatch):
    bridge._KEPT_SCRIPT_GRANTS.clear()
    err = {"tool_name": "run_command", "parameters": {"CommandLine": "python3 scratch/a.py"},
           "message": "user denied permission to run"}
    results = [_agy_result([err]), _agy_result([], response="done")]
    client, store, perms = _drive(monkeypatch, project, "harmless", results)
    assert "command(python3 scratch/a.py)" in store.cleared_keep      # not released at the clean turn


def test_clear_temp_grants_keeps_the_kept_entries(tmp_path):
    store = bridge.ThreadStore(tmp_path / "s.db")
    store.put("C", "t", "c1", "a")
    store.add_temp_grant("c1", "command(x)")
    store.add_temp_grant("c1", "command(ls)")
    assert store.clear_temp_grants("c1", "a", keep={"command(x)"}) == ["command(ls)"]
    assert store.clear_temp_grants("c1", "a") == ["command(x)"]


def test_entry_the_person_refused_is_refused_again_in_that_conversation(project, monkeypatch):
    err = {"tool_name": "run_command", "parameters": {"CommandLine": "python3 scratch/a.py"},
           "message": "user denied permission to run"}
    bridge._USER_DENIED.clear()
    bridge._remember_user_denial("c1", ["command(python3 scratch/a.py)"])
    monkeypatch.setattr(bridge.script_review, "review", lambda *a, **k: pytest.fail("reviewed"))
    client, store, perms = _drive(monkeypatch, project, "harmless", [_agy_result([err])])
    assert perms.entries == [] and store.retries == [] and store.temp == []
    assert any("拒否したため、自動で拒否" in p for p in client.posts)
    assert ("refused_auto", ["command(python3 scratch/a.py)"]) in store.logged
    bridge._USER_DENIED.clear()
    bridge._remember_user_denial("other", ["command(python3 scratch/a.py)"])      # another conversation: asked as usual
    client, store, perms = _drive(monkeypatch, project, "needs_review", [_agy_result([err])], reasons=["r"])
    assert len(store.retries) == 1
    bridge._USER_DENIED.clear()
