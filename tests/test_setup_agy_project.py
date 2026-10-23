import json
import os
import stat
import subprocess
import uuid

import pytest
import yaml

import agy_settings
import install
import script_integrity
import setup_agy_project as setup


class Scripted(setup.Prompter):
    """Answers prompts by substring of their text; anything unmatched is Enter."""

    def __init__(self, answers=None, secrets=None):
        self.answers = answers or {}
        self.secrets = secrets or {}
        self.asked = []
        super().__init__(input_fn=self._in, secret_fn=self._sec, out=lambda *_: None)

    def _in(self, prompt):
        self.asked.append(prompt)
        for needle, ans in self.answers.items():
            if needle in prompt:
                if isinstance(ans, list):  # successive answers for a re-prompted question
                    return ans.pop(0) if len(ans) > 1 else ans[0]
                return ans
        return ""

    def _sec(self, prompt):
        for needle, ans in self.secrets.items():
            if needle in prompt:
                return ans
        return ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake machine: own agy dirs, an installed prefix + config dir, one target repo,
    and a fake `agy --new-project` that registers the cwd the way the real one does."""
    gemini = tmp_path / "gemini"
    monkeypatch.setattr(agy_settings, "GLOBAL_SETTINGS_PATH", gemini / "settings.json")
    monkeypatch.setattr(setup, "AGY_PROJECTS_DIR", gemini / "projects")
    (gemini / "projects").mkdir(parents=True)

    prefix, cfg = tmp_path / "prefix", tmp_path / "cfg"
    install.main(["--prefix", str(prefix), "--config-dir", str(cfg), "--no-venv", "--no-setup"])

    target = tmp_path / "proj"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (target / ".gitignore").write_text("*.pyc\n")

    registered = []

    def fake_register(target_dir):
        pid = str(uuid.uuid4())
        registered.append(pid)
        (gemini / "projects" / f"{pid}.json").write_text(json.dumps({
            "id": pid, "name": target_dir.name,
            "projectResources": {"resources": [{"folderUri": f"file://{target_dir}"}]}}))

    monkeypatch.setattr(setup, "register_agy_project", fake_register)
    return type("Env", (), dict(tmp=tmp_path, gemini=gemini, prefix=prefix, cfg=cfg,
                                target=target, registered=registered))


def run(env, ask=None, target=None, *extra):
    setup.main([str(target or env.target), "--prefix", str(env.prefix),
                "--bridge-config-dir", str(env.cfg), *extra], ask=ask or Scripted())


def settings(env):
    return json.loads(env.gemini.joinpath("settings.json").read_text())


def project_json(env):
    (path,) = (env.gemini / "projects").glob("*.json")
    return path


def channels(env):
    return yaml.safe_load((env.cfg / "config.yaml").read_text())["channels"]


def test_fresh_setup_registers_links_and_denies(env):
    run(env)
    assert len(env.registered) == 1
    agents = env.target / ".agents"
    safety = agents / "agent-safety-rules.md"
    assert safety.is_symlink() and safety.resolve() == (env.prefix / "tools/agent-safety-rules.md").resolve()
    assert (env.target / "AGENTS.md").read_text() == "@[エージェント安全規則](.agents/agent-safety-rules.md)\n"
    assert "scratch/" in (env.target / ".gitignore").read_text()

    assert not (agents / "hooks.json").exists()  # the hook is global now (installed by install.py)

    s = settings(env)
    assert s["trustedWorkspaces"] == [str(env.target.resolve())]
    deny = s["permissions"]["deny"]
    # machine-wide denies (prefix, config dir) come from install.py; setup adds only its own
    assert f"read_file({env.prefix.resolve()})" in deny and f"write_file({env.cfg.resolve()})" in deny
    assert not any("hooks.json" in d or ".permanent-commands" in d for d in deny)
    assert "allow" not in s["permissions"]  # never writes machine-wide allow
    # the hook's state (seals, ledger) is registered under the config dir, not in the project
    state = script_integrity.state_dir(str(env.cfg.resolve()), str(env.target.resolve()))
    assert os.path.isdir(state)
    assert not (agents / ".permanent-commands.json").exists()


def test_idempotent(env):
    answers = {"チャンネル ID": "C0TEST123", "Slack での表示名": "Bot", "アイコン（絵文字": ":robot_face:"}
    run(env, Scripted(answers))
    snap = lambda: (settings(env), (env.target / "AGENTS.md").read_text(),
                    (env.target / ".gitignore").read_text(), (env.cfg / "config.yaml").read_text())
    before = snap()
    run(env, Scripted({"Slack チャンネルとの対応づけ": "y"}))  # Enter on every value keeps it
    assert snap() == before
    assert len(env.registered) == 1


def test_declined_registration_still_sets_up_files_but_nothing_project_specific(env):
    ask = Scripted({"agy にまだ登録されていません": "n", "Drive CLI": "y"})
    run(env, ask)
    assert env.registered == []
    assert not any("チャンネル" in p for p in ask.asked)
    assert channels(env) == {}


def test_refuses_real_file_in_place_of_the_safety_rules_link(env):
    (env.target / ".agents").mkdir()
    (env.target / ".agents/agent-safety-rules.md").write_text("mine")
    with pytest.raises(SystemExit):
        run(env)
    assert (env.target / ".agents/agent-safety-rules.md").read_text() == "mine"


def test_old_project_level_hook_is_removed_so_it_does_not_fire_twice(env):
    agents = env.target / ".agents"
    agents.mkdir()
    (agents / "hooks.json").write_text(json.dumps({"script-integrity": {"PreToolUse": []}}))
    run(env)
    assert not (agents / "hooks.json").exists()

    (agents / "hooks.json").write_text(json.dumps({
        "script-integrity": {"PreToolUse": []}, "mine": {"Stop": []}}))
    run(env)
    assert json.loads((agents / "hooks.json").read_text()) == {"mine": {"Stop": []}}

    (agents / "hooks.json").write_text(json.dumps({"mine": {"Stop": []}}))
    run(env)  # someone else's hooks.json is left alone
    assert json.loads((agents / "hooks.json").read_text()) == {"mine": {"Stop": []}}


def test_refuses_targets_inside_install_or_config_dir(env):
    for bad in (env.prefix, env.prefix / "tools", env.cfg):
        with pytest.raises(SystemExit):
            run(env, target=bad)


def test_requires_installed_prefix(env, tmp_path):
    with pytest.raises(SystemExit):
        setup.main([str(env.target), "--prefix", str(tmp_path / "nothing")], ask=Scripted())


def test_git_is_mandatory_and_initialized_without_asking(env, tmp_path, capsys):
    other = tmp_path / "nogit"
    other.mkdir()
    ask = Scripted()
    run(env, ask, target=other)
    assert (other / ".git").exists()
    assert not any("git" in p for p in ask.asked)
    assert "git init" in capsys.readouterr().out


def test_missing_target_is_created_when_confirmed(env, tmp_path):
    new = tmp_path / "brand-new"
    run(env, Scripted({"作成しますか": "y"}), target=new)
    assert (new / ".git").is_dir() and (new / ".agents/agent-safety-rules.md").is_symlink()


def test_refuses_without_a_terminal(env):
    with pytest.raises(SystemExit):
        setup.main([str(env.target), "--prefix", str(env.prefix), "--bridge-config-dir", str(env.cfg)])


# --- Drive ------------------------------------------------------------------------

def test_gdrive_grants_go_only_to_that_projects_file_pinned_and_absolute(env):
    other = env.gemini / "projects" / "other.json"
    other.write_text(json.dumps({"projectResources": {"resources": []}}))
    run(env, Scripted({"Drive CLI": "y", "プロファイル名": "my-profile"}))
    (mine,) = [p for p in (env.gemini / "projects").glob("*.json") if p != other]

    py, script = env.prefix.resolve() / ".venv/bin/python", env.prefix.resolve() / "tools/gdrive_cli.py"
    allow = json.loads(mine.read_text())["permissionGrants"]["permissionGrants"]["allow"]
    assert allow == [f"command({py} -I {script} --profile my-profile {s})"
                     for s in ("list", "search", "meta", "read", "download")]
    assert "permissionGrants" not in json.loads(other.read_text())
    assert "allow" not in settings(env)["permissions"]
    assert not any(w in e for e in allow for w in ("mkdir", "upload", "update"))
    # nothing sealed or copied into the project: the script lives only in the prefix
    assert not (env.target / "scripts").exists()
    assert not (env.target / ".agents/.permanent-commands.json").exists()


def test_gdrive_profile_defaults_to_project_name_and_is_kept_on_rerun(env):
    run(env, Scripted({"Drive CLI": "y"}))
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) == "proj"
    ask = Scripted()
    run(env, ask)  # Enter everywhere: "use Drive CLI?" defaults to yes now, profile kept
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) == "proj"
    assert any("Drive CLI" in p and "[Y/n]" in p for p in ask.asked)


def test_gdrive_profile_name_is_validated(env):
    ask = Scripted({"Drive CLI": "y", "プロファイル名": ["a b; rm", "../x", "ok-name"]})
    run(env, ask)
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) == "ok-name"


def test_profile_is_pinned_before_the_subcommand_in_every_entry(tmp_path):
    for e in setup.gdrive_allow_entries(tmp_path, "p"):
        tokens = e[len("command("):-1].split()
        assert tokens[1:5] == ["-I", f"{tmp_path}/tools/gdrive_cli.py", "--profile", "p"]
        assert tokens[5] in setup.GDRIVE_READONLY_SUBCOMMANDS and len(tokens) == 6


def test_warns_when_profile_missing(env, capsys):
    run(env, Scripted({"Drive CLI": "y"}))
    assert "プロファイルがまだありません" in capsys.readouterr().out


# --- Slack channel mapping ------------------------------------------------------------

def test_channel_mapping_written_with_project_id_and_all_fields(env):
    run(env, Scripted({
        "チャンネル ID": "C0TEST123", "Slack での表示名": "Helper", "アイコン（絵文字": ":abacus:",
        "モデル（`agy": "gemini-x", "effort": "high", "監視する設定ファイル": ".agents/local-tools.md, a/b.md",
        "権限ボタンの統計": "y"}))
    pid = project_json(env).stem
    assert channels(env) == {"C0TEST123": {
        "project": pid, "name": "proj", "display_name": "Helper", "icon_emoji": ":abacus:",
        "model": "gemini-x", "effort": "high",
        "watched_config_files": [".agents/local-tools.md", "a/b.md"],
        "telemetry": {"permission_stats": True}}}
    assert (env.cfg / "config.yaml.bak").exists()
    assert "# Example entries" in (env.cfg / "config.yaml").read_text()  # comments survive


def test_channel_prompts_reject_bad_values_then_accept(env):
    ask = Scripted({"チャンネル ID": ["nope", "C0TEST123"], "effort": ["extreme", "low"],
                    "アイコン（絵文字": ["abacus", ":abacus:"], "監視する設定ファイル": ["/etc/passwd", "../x", "ok.md"]})
    run(env, ask)
    e = channels(env)["C0TEST123"]
    assert (e["effort"], e["icon_emoji"], e["watched_config_files"]) == ("low", ":abacus:", ["ok.md"])


def test_rerun_shows_current_values_and_dash_clears(env):
    run(env, Scripted({"チャンネル ID": "C0TEST123", "Slack での表示名": "Helper", "モデル（`agy": "m1"}))
    ask = Scripted({"Slack チャンネルとの対応づけ": "y", "モデル（`agy": "-", "Slack での表示名": "Renamed"})
    run(env, ask)
    e = channels(env)["C0TEST123"]
    assert e["display_name"] == "Renamed" and "model" not in e
    assert any("チャンネル ID [C0TEST123]" in p for p in ask.asked)


def test_unknown_keys_of_an_existing_entry_are_preserved(env):
    run(env, Scripted({"チャンネル ID": "C0TEST123"}))
    cfg = env.cfg / "config.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["channels"]["C0TEST123"]["future_key"] = {"a": 1}
    cfg.write_text(yaml.safe_dump(data))
    run(env, Scripted({"Slack チャンネルとの対応づけ": "y", "Slack での表示名": "X"}))
    assert channels(env)["C0TEST123"]["future_key"] == {"a": 1}


def test_channel_of_another_project_is_not_taken_over_by_default(env, tmp_path):
    run(env, Scripted({"チャンネル ID": "C0TEST123", "Slack での表示名": "first"}))
    second = tmp_path / "proj2"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    run(env, Scripted({"チャンネル ID": "C0TEST123", "Slack での表示名": "second"}), target=second)
    assert channels(env)["C0TEST123"]["display_name"] == "first"
    run(env, Scripted({"チャンネル ID": "C0TEST123", "置き換えますか": "y", "Slack での表示名": "second"}),
        target=second)
    assert channels(env)["C0TEST123"]["display_name"] == "second"


# --- tokens -------------------------------------------------------------------------

def test_tokens_filled_only_when_unset_and_file_stays_private(env):
    run(env, Scripted(secrets={"SLACK_BOT_TOKEN": "xoxb-real", "SLACK_APP_TOKEN": "xapp-real"}))
    text = (env.cfg / "env").read_text()
    assert "SLACK_BOT_TOKEN=xoxb-real" in text and "SLACK_APP_TOKEN=xapp-real" in text
    assert stat.S_IMODE(os.stat(env.cfg / "env").st_mode) == 0o600
    run(env, Scripted(secrets={"SLACK_BOT_TOKEN": "xoxb-other"}))
    assert "SLACK_BOT_TOKEN=xoxb-real" in (env.cfg / "env").read_text()


# --- picking an existing agy project -----------------------------------------------

def _existing_project(env, name, folder, pid=None):
    pid = pid or str(uuid.uuid4())
    data = {"id": pid, "name": name}
    if folder is not None:
        data["projectResources"] = {"resources": [{"folderUri": f"file://{folder}"}]}
    (env.gemini / "projects" / f"{pid}.json").write_text(json.dumps(data))
    return pid


def run_menu(env, ask):
    setup.main(["--prefix", str(env.prefix), "--bridge-config-dir", str(env.cfg)], ask=ask)


def test_existing_project_can_be_picked_from_the_list(env, tmp_path):
    folder = tmp_path / "already-there"
    folder.mkdir()
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    pid = _existing_project(env, "already", folder.resolve())
    ask = Scripted({"番号": "1", "チャンネル ID": "C0TEST123"})
    run_menu(env, ask)
    assert env.registered == []  # not re-registered with agy
    assert channels(env)["C0TEST123"]["project"] == pid
    assert (folder / ".agents/agent-safety-rules.md").is_symlink()
    assert str(folder.resolve()) in settings(env)["trustedWorkspaces"]


def test_project_without_a_directory_only_gets_drive_and_channel_setup(env):
    pid = _existing_project(env, "chat-only", None)
    run_menu(env, Scripted({"番号": "1", "チャンネル ID": "C0TEST123"}))
    assert channels(env)["C0TEST123"]["project"] == pid
    assert "trustedWorkspaces" not in settings(env) if env.gemini.joinpath("settings.json").exists() else True
    assert not (env.tmp / "chat-only").exists()


def test_menu_rejects_unknown_numbers_and_accepts_a_path_or_zero(env, tmp_path):
    _existing_project(env, "one", None)
    new = tmp_path / "fresh"
    ask = Scripted({"番号": ["7", "0"], "新しいプロジェクトのディレクトリ": str(new), "作成しますか": "y"})
    run_menu(env, ask)
    assert (new / ".git").is_dir()
    assert len(env.registered) == 1
    ask = Scripted({"番号": str(tmp_path / "other")})
    (tmp_path / "other").mkdir()
    run_menu(env, ask)
    assert (tmp_path / "other/.git").is_dir()
