import json
import subprocess

import pytest

import setup_agy_project as setup


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake machine: own agy dirs, a source dir, a bridge checkout, one target repo."""
    gemini = tmp_path / "gemini"
    monkeypatch.setattr(setup, "GLOBAL_SETTINGS_PATH", gemini / "settings.json")
    monkeypatch.setattr(setup, "AGY_PROJECTS_DIR", gemini / "projects")
    (gemini / "projects").mkdir(parents=True)

    bridge = tmp_path / "bridge"
    src = bridge / "tools"
    (src / "hooks").mkdir(parents=True)
    (bridge / "bridge.py").write_text("")
    for name in ("agent-safety-rules.md", "hooks.json", "gdrive_cli.py", "hooks/script_integrity.py"):
        (src / name).write_text(f"# {name}\n")

    target = tmp_path / "proj"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (target / ".gitignore").write_text("*.pyc\n")
    return type("Env", (), dict(tmp=tmp_path, gemini=gemini, bridge=bridge, src=src, target=target))


def run(env, *extra):
    setup.main([str(env.target), "--source-dir", str(env.src),
                "--bridge-config-dir", str(env.tmp / "cfg"), *extra])


def settings(env):
    return json.loads(env.gemini.joinpath("settings.json").read_text())


def test_links_agents_md_gitignore_and_settings(env):
    run(env)
    agents = env.target / ".agents"
    assert (agents / "agent-safety-rules.md").resolve() == (env.src / "agent-safety-rules.md").resolve()
    assert (agents / "hooks").is_symlink()
    assert (env.target / "AGENTS.md").read_text() == "@[エージェント安全規則](.agents/agent-safety-rules.md)\n"
    assert "scratch/" in (env.target / ".gitignore").read_text()
    s = settings(env)
    assert s["trustedWorkspaces"] == [str(env.target.resolve())]
    deny = s["permissions"]["deny"]
    assert f"read_file({env.bridge.resolve()})" in deny
    assert f"read_file({(env.tmp / 'cfg').resolve()})" in deny
    assert "allow" not in s["permissions"]  # never writes machine-wide allow


def test_local_tools_md_linked_and_included_only_if_present(env):
    (env.src / "local-tools.md").write_text("tools")
    run(env)
    assert (env.target / ".agents/local-tools.md").is_symlink()
    assert "(.agents/local-tools.md)" in (env.target / "AGENTS.md").read_text()


def test_idempotent(env):
    run(env)
    before = (settings(env), (env.target / "AGENTS.md").read_text(), (env.target / ".gitignore").read_text())
    run(env)
    assert before == (settings(env), (env.target / "AGENTS.md").read_text(), (env.target / ".gitignore").read_text())


def test_refuses_real_file_in_place_of_link(env):
    (env.target / ".agents").mkdir()
    (env.target / ".agents/hooks.json").write_text("mine")
    with pytest.raises(SystemExit):
        run(env)
    assert (env.target / ".agents/hooks.json").read_text() == "mine"


def test_refuses_target_that_is_not_a_git_repo(env, tmp_path):
    other = tmp_path / "nogit"
    other.mkdir()
    with pytest.raises(SystemExit):
        setup.main([str(other), "--source-dir", str(env.src)])


def test_refuses_bridge_checkout_as_target(env):
    with pytest.raises(SystemExit):
        setup.main([str(env.bridge), "--source-dir", str(env.src)])


def test_bridge_dir_required_when_not_inferable(env, tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    with pytest.raises(SystemExit):
        setup.main([str(env.target), "--source-dir", str(lone)])


def test_gdrive_requires_secret_with_safe_name(env):
    with pytest.raises(SystemExit):
        run(env, "--with-gdrive")
    with pytest.raises(SystemExit):
        run(env, "--with-gdrive", "--gdrive-secret", "a b; rm")


def _register_project(env, pid="proj-1"):
    path = env.gemini / "projects" / f"{pid}.json"
    path.write_text(json.dumps({"projectResources": {"resources": [
        {"folderUri": f"file://{env.target.resolve()}"}]}}))
    return path


def test_gdrive_grants_go_only_to_that_projects_file_and_include_secret(env):
    mine = _register_project(env, "mine")
    other = env.gemini / "projects" / "other.json"
    other.write_text(json.dumps({"projectResources": {"resources": []}}))
    run(env, "--with-gdrive", "--gdrive-secret", "my-secret")

    allow = json.loads(mine.read_text())["permissionGrants"]["permissionGrants"]["allow"]
    assert allow == [f"command(.venv/bin/python -I scripts/gdrive_cli.py --secret my-secret {s})"
                     for s in ("list", "search", "meta", "read", "download")]
    assert "permissionGrants" not in json.loads(other.read_text())
    assert "allow" not in settings(env)["permissions"]

    # write-capable subcommands are never pre-approved
    assert not any(w in e for e in allow for w in ("mkdir", "upload", "update"))

    # script is linked and sealed with its content hash
    script = env.target / "scripts/gdrive_cli.py"
    assert script.is_symlink()
    pins = json.loads((env.target / ".agents/.permanent-commands.json").read_text())
    import hashlib
    assert pins[str(script)] == hashlib.sha256(script.read_bytes()).hexdigest()
    assert f"write_file({env.target.resolve()}/scripts/gdrive_cli.py)" in settings(env)["permissions"]["deny"]


def test_gdrive_unregistered_project_skips_grants_but_still_seals(env):
    run(env, "--with-gdrive", "--gdrive-secret", "s")
    assert list((env.gemini / "projects").iterdir()) == []
    assert (env.target / ".agents/.permanent-commands.json").exists()
