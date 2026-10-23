"""bridge.py (writes seals) and the hook (reads them) must agree on the state dir."""
import json
import os
import sys
from pathlib import Path

import bridge

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "hooks"))
import script_integrity as si  # noqa: E402


def test_bridge_and_hook_agree_on_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG_PATH", tmp_path / "cfg" / "config.yaml")
    root = tmp_path / "proj"
    root.mkdir()
    assert str(bridge._integrity_state_dir(root)) == si.state_dir(str(tmp_path / "cfg"), str(root))


def test_mark_permanent_command_writes_seal_outside_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG_PATH", tmp_path / "cfg" / "config.yaml")
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "tool.py"
    script.write_text("print(1)\n")
    monkeypatch.setattr(bridge, "_project_root", lambda pid: root)

    entry = "command(python3 scripts/tool.py)"
    bridge._mark_permanent_command("pid", entry, present=True)
    state = Path(si.state_dir(str(tmp_path / "cfg"), str(root)))
    pins = si.load_permanent_pins(str(state))
    assert pins == {str(script): si.file_hash(str(script))}
    assert not (root / ".agents").exists()

    bridge._mark_permanent_command("pid", entry, present=False)
    assert si.load_permanent_pins(str(state)) == {}
