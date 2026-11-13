import sys
import logging
import subprocess

import pytest

import bridge


class FakeChild:
    def __init__(self, out, err, rc):
        self._r = (out, err)
        self.returncode = rc
        self.pid = 1

    def communicate(self, timeout=None):
        return self._r


def _run(monkeypatch, out, err, rc):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeChild(out, err, rc))
    return bridge.run_agy("hi", "proj", None)


def test_nonzero_exit_logs_stdout_tail(monkeypatch, caplog):
    out = "\n".join(f'{{"event":"step_update","n":{i}}}' for i in range(50))
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as ei:
        _run(monkeypatch, out, "", 2)
    assert str(ei.value) == "agy exited 2: "
    assert '"n":49' in caplog.text
    assert '"n":29' not in caplog.text  # only the last 20 lines


def test_stdout_not_in_exception(monkeypatch):
    with pytest.raises(RuntimeError) as ei:
        _run(monkeypatch, "RESOURCE_EXHAUSTED in stdout", "boom", 1)
    assert "RESOURCE_EXHAUSTED" not in str(ei.value)


def test_stdout_tail_empty_and_truncated():
    assert bridge._stdout_tail("") == "(empty)"
    assert len(bridge._stdout_tail("x" * 10000)) == 6000


def test_startup_fails_fast_with_a_clear_message_when_agy_is_not_runnable(monkeypatch):
    monkeypatch.setattr(bridge, "AGY_BIN", "definitely-not-a-real-agy-binary")
    with pytest.raises(SystemExit) as ei:
        bridge.check_agy_available()
    assert "AGY_BIN" in str(ei.value) and "absolute path" in str(ei.value)
    monkeypatch.setattr(bridge, "AGY_BIN", sys.executable)
    bridge.check_agy_available()  # an existing executable passes


def test_agy_runs_in_the_project_directory(monkeypatch, tmp_path):
    seen = {}

    def popen(*a, **k):
        seen.update(k)
        return FakeChild('{"event":"result","result":{}}', "", 0)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(bridge, "_project_root", lambda pid: tmp_path)
    bridge.run_agy("hi", "proj", None)
    assert seen["cwd"] == str(tmp_path)               # not the bridge's own (install) directory
    monkeypatch.setattr(bridge, "_project_root", lambda pid: None)
    bridge.run_agy("hi", "proj", None)
    assert seen["cwd"] is None                        # unknown project: unchanged behaviour


_ERR = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 17h2m45s."


def _quota_stream(command="python scratch/x.py"):
    import json as _j
    step = {"event": "step_update", "step_update": {"step_index": 318, "state": "DONE", "step_type": "tool",
            "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": command}}}}
    res = {"event": "result", "result": {"conversation_id": "c1", "status": "ERROR", "response": "",
           "error": _ERR, "denied_actions": [{"action": "command", "display_name": "Bash"}]}}
    return "\n".join(_j.dumps(e) for e in (step, res))


def test_nonzero_exit_with_a_result_keeps_the_denial_of_the_same_run(monkeypatch):
    result = _run(monkeypatch, _quota_stream(), f"error: {_ERR}", 3)
    assert result["status"] == "ERROR" and result["_exit_code"] == 3
    assert [e["parameters"]["CommandLine"] for e in result["_tool_errors"]] == ["python scratch/x.py"]


def test_nonzero_exit_without_denied_actions_adds_no_denial(monkeypatch):
    out = _quota_stream().replace('"denied_actions": [{"action": "command", "display_name": "Bash"}]', '"x": 1')
    assert _run(monkeypatch, out, f"error: {_ERR}", 3)["_tool_errors"] == []


def test_quota_text_names_the_model_and_flags_a_repeated_error():
    bridge._LAST_QUOTA_ERROR.clear()
    first = bridge._agy_failure_text(_ERR, "gemini-3.8-flash-high", "c1")
    assert "gemini-3.8-flash-high" in first and "17h2m45s" in first and "前回と同じ" not in first
    assert "agy のデフォルト" in bridge._agy_failure_text(_ERR.replace("17h2m45s", "1h"), None, "c2")
    again = bridge._agy_failure_text(_ERR, "gemini-3.8-flash-high", "c1")
    assert "前回と同じ" in again
    assert "前回と同じ" not in bridge._agy_failure_text(_ERR.replace("17h2m45s", "17h0m1s"), None, "c1")
