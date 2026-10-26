"""The bridge refuses to run agy when the safety mechanisms (rules, hook, deny rules) are absent."""
import hashlib
import json
import os
from pathlib import Path

import pytest

import agy_settings
import bridge


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """A complete, valid safety stack for one project; tests then break one piece."""
    prefix = Path(bridge.__file__).resolve().parent
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    root = tmp_path / "proj"
    (root / ".agents/rules").mkdir(parents=True)
    (root / ".agents/rules/agent-safety-rules.md").write_text("---\ntrigger: always_on\n---\nrules\n")
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "pid.json").write_text(json.dumps(
        {"id": "pid", "projectResources": {"resources": [{"folderUri": f"file://{root}"}]}}))
    key = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]
    (cfg / "integrity" / key).mkdir(parents=True)
    hooks, settings = tmp_path / "hooks.json", tmp_path / "settings.json"
    hooks.write_text(json.dumps({"script-integrity": {"PreToolUse": []}}))
    settings.write_text(json.dumps({"permissions": {"deny": [f"write_file({prefix})", f"write_file({cfg})"]}}))
    monkeypatch.setattr(bridge, "CONFIG_PATH", cfg / "config.yaml")
    monkeypatch.setattr(bridge, "AGY_PROJECTS_DIR", projects)
    monkeypatch.setattr(bridge, "AGY_GLOBAL_HOOKS_PATH", hooks)
    monkeypatch.setattr(bridge, "AGY_GLOBAL_SETTINGS_PATH", settings)
    return type("S", (), dict(root=root, hooks=hooks, settings=settings, cfg=cfg, key=key, prefix=prefix))


def test_complete_stack_has_no_problems(stack):
    assert bridge._safety_mechanism_problems("pid") == []


def test_missing_rules_file(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").unlink()
    assert any("安全規則がありません" in p for p in bridge._safety_mechanism_problems("pid"))


def test_dangling_rules_symlink(stack):
    f = stack.root / ".agents/rules/agent-safety-rules.md"
    f.unlink()
    f.symlink_to(stack.root / "nowhere.md")
    assert any("安全規則がありません" in p for p in bridge._safety_mechanism_problems("pid"))


def test_rules_without_always_on_frontmatter_are_ignored_by_agy_so_they_count_as_missing(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").write_text("rules only\n")
    assert any("always_on" in p for p in bridge._safety_mechanism_problems("pid"))


def test_loosened_rules_content_is_the_users_call(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").write_text("---\ntrigger: always_on\n---\n(anything)\n")
    assert bridge._safety_mechanism_problems("pid") == []


def test_missing_hook_state_dir(stack):
    (stack.cfg / "integrity" / stack.key).rmdir()
    assert any("状態ディレクトリ" in p for p in bridge._safety_mechanism_problems("pid"))


def test_hook_not_registered(stack):
    stack.hooks.write_text("{}")
    assert any("script-integrity" in p for p in bridge._safety_mechanism_problems("pid"))
    stack.hooks.unlink()
    assert any("script-integrity" in p for p in bridge._safety_mechanism_problems("pid"))


def test_missing_deny_rules(stack):
    stack.settings.write_text(json.dumps({"permissions": {"deny": [f"write_file({stack.cfg})"]}}))
    problems = bridge._safety_mechanism_problems("pid")
    assert any("インストール先" in p for p in problems) and not any("設定ディレクトリ" in p for p in problems)


def test_project_without_workspace_still_needs_the_machine_wide_mechanisms(stack):
    assert bridge._safety_mechanism_problems("no-such-project") == []
    stack.hooks.write_text("{}")
    assert bridge._safety_mechanism_problems("no-such-project")


def test_require_raises_before_spawning(stack, monkeypatch):
    import subprocess
    # restore the real check (conftest stubs it for other tests)
    monkeypatch.setattr(bridge, "_require_safety_mechanisms", _real_require)
    (stack.root / ".agents/rules/agent-safety-rules.md").unlink()
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(bridge.SafetyMechanismMissing) as ei:
        bridge.run_agy("hi", "pid", None)
    assert spawned == []
    assert bridge.SAFETY_MISSING_PREFIX in str(ei.value) and "setup_agy_project.py" in str(ei.value)
    assert bridge._agy_failure_text(str(ei.value)).startswith(":no_entry:")


_real_require = bridge._require_safety_mechanisms
