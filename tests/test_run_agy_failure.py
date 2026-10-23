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
