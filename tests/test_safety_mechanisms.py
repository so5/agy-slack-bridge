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


def msgs(project="pid"):
    return [p.message for p in bridge._safety_mechanism_problems(project)]


def test_complete_stack_has_no_problems(stack):
    assert msgs() == []


def test_missing_rules_file(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").unlink()
    assert any("安全規則がありません" in m for m in msgs())


def test_dangling_rules_symlink(stack):
    f = stack.root / ".agents/rules/agent-safety-rules.md"
    f.unlink()
    f.symlink_to(stack.root / "nowhere.md")
    assert any("安全規則がありません" in m for m in msgs())


def test_rules_without_always_on_frontmatter_are_ignored_by_agy_so_they_count_as_missing(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").write_text("rules only\n")
    assert any("always_on" in m for m in msgs())


def test_loosened_rules_content_is_the_users_call(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").write_text("---\ntrigger: always_on\n---\n(anything)\n")
    assert msgs() == []


def test_missing_hook_state_dir(stack):
    (stack.cfg / "integrity" / stack.key).rmdir()
    assert any("状態ディレクトリ" in m for m in msgs())


def test_hook_not_registered(stack):
    stack.hooks.write_text("{}")
    assert any("script-integrity" in m for m in msgs())
    stack.hooks.unlink()
    assert any("script-integrity" in m for m in msgs())


def test_missing_deny_rules(stack):
    stack.settings.write_text(json.dumps({"permissions": {"deny": [f"write_file({stack.cfg})"]}}))
    ms = msgs()
    assert any("インストール先" in m for m in ms) and not any("設定ディレクトリ" in m for m in ms)


def test_project_without_workspace_still_needs_the_machine_wide_mechanisms(stack):
    assert msgs("no-such-project") == []
    stack.hooks.write_text("{}")
    assert msgs("no-such-project")


# --- automatic repair ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_notices():
    bridge._take_safety_repair_notices()
    yield
    bridge._take_safety_repair_notices()


def test_missing_rules_are_relinked_to_the_installed_file_and_reported(stack):
    link = stack.root / ".agents/rules/agent-safety-rules.md"
    link.unlink()
    assert bridge._ensure_safety_mechanisms("pid") == []
    assert link.is_symlink() and link.resolve() == (stack.prefix / "tools/agent-safety-rules.md").resolve()
    assert any("リンク" in n for n in bridge._take_safety_repair_notices())


def test_dangling_link_is_replaced(stack):
    f = stack.root / ".agents/rules/agent-safety-rules.md"
    f.unlink()
    f.symlink_to(stack.root / "nowhere.md")
    assert bridge._ensure_safety_mechanisms("pid") == []
    assert f.resolve() == (stack.prefix / "tools/agent-safety-rules.md").resolve()


def test_users_rules_get_only_the_trigger_line_fixed_and_the_rest_is_kept(stack):
    f = stack.root / ".agents/rules/agent-safety-rules.md"
    f.write_text("my own rules, no frontmatter\n")
    assert bridge._ensure_safety_mechanisms("pid") == []
    assert f.read_text() == "---\ntrigger: always_on\n---\nmy own rules, no frontmatter\n" and not f.is_symlink()
    assert any("always_on" in n for n in bridge._take_safety_repair_notices())


@pytest.mark.parametrize("before,after", [
    ("---\ntrigger: manual\n---\nbody\n", "---\ntrigger: always_on\n---\nbody\n"),
    ("---\ndescription: x\n---\nbody\n", "---\ntrigger: always_on\ndescription: x\n---\nbody\n"),
    ("---\ntrigger: always_on\n---\nbody\n", "---\ntrigger: always_on\n---\nbody\n"),
    ("---\n---\nbody\n", "---\ntrigger: always_on\n---\nbody\n"),
    ("--- not frontmatter\nbody\n", "---\ntrigger: always_on\n---\n--- not frontmatter\nbody\n"),
])
def test_with_always_on(before, after):
    import safety_stack
    assert safety_stack.with_always_on(before) == after


def test_a_symlink_to_somewhere_else_is_not_edited(stack):
    f = stack.root / ".agents/rules/agent-safety-rules.md"
    other = stack.root / "elsewhere.md"
    other.write_text("no frontmatter\n")
    f.unlink()
    f.symlink_to(other)
    assert [p.code for p in bridge._ensure_safety_mechanisms("pid")] == ["rules_frontmatter"]
    assert other.read_text() == "no frontmatter\n"


def test_state_dir_hook_and_deny_rules_are_restored_keeping_everything_else(stack):
    (stack.cfg / "integrity" / stack.key).rmdir()
    stack.hooks.write_text(json.dumps({"other-hook": {"x": 1}}))
    stack.settings.write_text(json.dumps({"permissions": {"deny": ["read_file(/secret)"]}, "trustedWorkspaces": ["/w"]}))
    assert bridge._ensure_safety_mechanisms("pid") == []
    assert (stack.cfg / "integrity" / stack.key).is_dir()
    hooks = json.loads(stack.hooks.read_text())
    assert "script-integrity" in hooks and hooks["other-hook"] == {"x": 1}
    st = json.loads(stack.settings.read_text())
    assert "read_file(/secret)" in st["permissions"]["deny"] and st["trustedWorkspaces"] == ["/w"]
    assert f"write_file({stack.prefix})" in st["permissions"]["deny"] and f"write_file({stack.cfg})" in st["permissions"]["deny"]
    assert "allow" not in st["permissions"]                       # repair never writes a grant


def test_unfixable_problems_block_agy_and_never_start_it(stack, monkeypatch):
    import subprocess
    monkeypatch.setattr(bridge, "_require_safety_mechanisms", _real_require)
    other = stack.root / "elsewhere.md"
    other.write_text("no frontmatter\n")
    link = stack.root / ".agents/rules/agent-safety-rules.md"
    link.unlink()
    link.symlink_to(other)                                        # not ours to edit -> cannot be repaired
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(bridge.SafetyMechanismMissing) as ei:
        bridge.run_agy("hi", "pid", None)
    assert spawned == []
    assert bridge.SAFETY_MISSING_PREFIX in str(ei.value) and "自動では直せません" in str(ei.value)
    assert bridge._agy_failure_text(str(ei.value)).startswith(":no_entry:")


def test_repaired_stack_lets_agy_run(stack, monkeypatch):
    import subprocess
    monkeypatch.setattr(bridge, "_require_safety_mechanisms", _real_require)
    (stack.root / ".agents/rules/agent-safety-rules.md").unlink()
    spawned = []

    def popen(*a, **k):
        spawned.append(a)
        raise RuntimeError("stop here")                           # we only care that it got this far
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="stop here"):
        bridge.run_agy("hi", "pid", None)
    assert len(spawned) == 1


def test_repair_notice_is_posted_to_the_channel(stack):
    (stack.root / ".agents/rules/agent-safety-rules.md").unlink()
    bridge._ensure_safety_mechanisms("pid")
    posted = []

    class C:
        def chat_postMessage(self, **kw):
            posted.append(kw)
    bridge._post_safety_repair_notices(C(), "C1", "1.0", {})
    assert len(posted) == 1 and "自動で修復" in posted[0]["text"] and posted[0]["thread_ts"] == "1.0"
    bridge._post_safety_repair_notices(C(), "C1", "1.0", {})       # reported once
    assert len(posted) == 1


_real_require = bridge._require_safety_mechanisms


# --- files install.py created come back from the backup in the config dir -----------------

@pytest.fixture
def fake_install(tmp_path):
    """A tmp copy of the install dir's safety files, backed up the way install.py does."""
    import shutil
    import safety_stack
    src = Path(bridge.__file__).resolve().parent
    prefix, cfg = tmp_path / "prefix", tmp_path / "cfg"
    for rel in safety_stack.SAFETY_FILES:
        (prefix / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, prefix / rel)
    cfg.mkdir(mode=0o700)
    assert sorted(safety_stack.backup_safety_files(prefix, cfg)) == sorted(safety_stack.SAFETY_FILES)
    return prefix, cfg


def _check(prefix, cfg, tmp_path):
    import safety_stack
    hooks, settings = tmp_path / "h.json", tmp_path / "s.json"
    hooks.write_text(json.dumps({"script-integrity": {"PreToolUse": []}}))
    settings.write_text(json.dumps({"permissions": {"deny": [f"write_file({prefix})", f"write_file({cfg})"]}}))
    return safety_stack, (prefix, cfg, None, None, hooks, settings)


def test_missing_hook_script_is_restored_from_the_backup(fake_install, tmp_path):
    prefix, cfg = fake_install
    sc, args = _check(prefix, cfg, tmp_path)
    original = (prefix / "tools/hooks/script_integrity.py").read_bytes()
    (prefix / "tools/hooks/script_integrity.py").unlink()
    problems = sc.check(*args)
    assert [(p.code, p.fixable) for p in problems] == [("hook_script", True)]
    done = sc.repair(*args, problems)
    assert (prefix / "tools/hooks/script_integrity.py").read_bytes() == original
    assert oct((prefix / "tools/hooks/script_integrity.py").stat().st_mode & 0o777) == "0o444"
    assert any("控えから" in d for d in done) and sc.check(*args) == []


def test_without_a_backup_a_missing_hook_script_cannot_be_repaired(fake_install, tmp_path):
    import shutil
    prefix, cfg = fake_install
    shutil.rmtree(cfg / "safety-backup")
    sc, args = _check(prefix, cfg, tmp_path)
    (prefix / "tools/hooks/script_integrity.py").unlink()
    assert [(p.code, p.fixable) for p in sc.check(*args)] == [("hook_script", False)]


def test_install_makes_the_backup_private(fake_install):
    prefix, cfg = fake_install
    f = cfg / "safety-backup/tools/hooks/script_integrity.py"
    assert f.is_file() and oct(f.stat().st_mode & 0o777) == "0o600"
    assert oct(f.parent.stat().st_mode & 0o777) == "0o700"
