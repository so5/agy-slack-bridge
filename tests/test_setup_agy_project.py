import json
import os
import stat
import subprocess
import uuid

import pytest
import yaml

import agy_models
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
    monkeypatch.setattr(setup, "fetch_agy_models", lambda *_: [("m1", "Model 1"), ("gemini-x", "Gemini X")])
    return type("Env", (), dict(tmp=tmp_path, gemini=gemini, prefix=prefix, cfg=cfg,
                                target=target, registered=registered))


def pname(env, target=None):
    """The default profile name of a project: its agy project id."""
    t = (target or env.target).resolve()
    if setup.find_agy_project_json(t) is None:
        setup.register_agy_project(t)          # the id exists only once agy knows the project
    return setup.find_agy_project_json(t).stem


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
    safety = agents / "rules/agent-safety-rules.md"
    assert safety.is_symlink() and safety.resolve() == (env.prefix / "tools/agent-safety-rules.md").resolve()
    assert safety.read_text().startswith("---\ntrigger: always_on\n---\n")  # agy needs the frontmatter
    assert not (env.target / "AGENTS.md").exists()  # AGENTS.md stays the agent's to edit
    assert "scratch/" in (env.target / ".gitignore").read_text()

    assert not (agents / "hooks.json").exists()  # the hook is global now (installed by install.py)

    s = settings(env)
    assert s["trustedWorkspaces"] == [str(env.target.resolve())]
    deny = s["permissions"]["deny"]
    # machine-wide denies (prefix, config dir) come from install.py; setup adds only its own
    assert f"read_file({env.prefix.resolve()})" in deny and f"write_file({env.cfg.resolve()})" in deny
    # only the safety-rules link is write-protected; other rules and AGENTS.md stay editable
    assert [d for d in deny if str(env.target.resolve()) in d] == [
        f"write_file({env.target.resolve()}/.agents/rules/agent-safety-rules.md)"]
    assert "allow" not in s["permissions"]  # never writes machine-wide allow
    # the hook's state (seals, ledger) is registered under the config dir, not in the project
    state = script_integrity.state_dir(str(env.cfg.resolve()), str(env.target.resolve()))
    assert os.path.isdir(state)
    assert not (agents / ".permanent-commands.json").exists()


def test_idempotent(env):
    answers = {"任意の項目": "y", "チャンネル ID": "C0TEST123", "表示名": "Bot", "アイコン絵文字": ":robot_face:"}
    run(env, Scripted(answers))
    snap = lambda: (settings(env), (env.target / ".gitignore").read_text(), (env.cfg / "config.yaml").read_text())
    before = snap()
    run(env, Scripted({"Slack チャンネルを対応づけ": "y"}))  # Enter on every value keeps it
    assert snap() == before
    assert len(env.registered) == 1


def test_failed_registration_still_sets_up_files_but_nothing_project_specific(env, monkeypatch):
    monkeypatch.setattr(setup, "register_agy_project", lambda d: None)
    ask = Scripted({"Drive CLI": "y"})
    run(env, ask)
    assert env.registered == []
    assert not any("チャンネル" in p for p in ask.asked)
    assert channels(env) == {}


def test_refuses_real_file_in_place_of_the_safety_rules_link(env):
    (env.target / ".agents/rules").mkdir(parents=True)
    (env.target / ".agents/rules/agent-safety-rules.md").write_text("mine")
    with pytest.raises(SystemExit):
        run(env)
    assert (env.target / ".agents/rules/agent-safety-rules.md").read_text() == "mine"


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
    run(env, Scripted({"ディレクトリ": "y"}), target=new)
    assert (new / ".git").is_dir() and (new / ".agents/rules/agent-safety-rules.md").is_symlink()


def test_refuses_without_a_terminal(env):
    with pytest.raises(SystemExit):
        setup.main([str(env.target), "--prefix", str(env.prefix), "--bridge-config-dir", str(env.cfg)])


# --- Drive ------------------------------------------------------------------------

def test_gdrive_grants_go_only_to_that_projects_file_pinned_and_absolute(env):
    other = env.gemini / "projects" / "other.json"
    other.write_text(json.dumps({"projectResources": {"resources": []}}))
    run(env, Scripted({"Drive CLI": "y"}))
    (mine,) = [p for p in (env.gemini / "projects").glob("*.json") if p != other]

    py, script = env.prefix.resolve() / ".venv/bin/python", env.prefix.resolve() / "tools/gdrive_cli.py"
    allow = json.loads(mine.read_text())["permissionGrants"]["permissionGrants"]["allow"]
    assert allow == [f"command({py} -I {script} --profile {pname(env)} {s})"
                     for s in ("list", "search", "meta", "read", "download")]
    assert "permissionGrants" not in json.loads(other.read_text())
    assert "allow" not in settings(env)["permissions"]
    assert not any(w in e for e in allow for w in ("mkdir", "upload", "update"))
    # nothing sealed or copied into the project: the script lives only in the prefix
    assert not (env.target / "scripts").exists()
    assert not (env.target / ".agents/.permanent-commands.json").exists()


def test_gdrive_profile_defaults_to_project_name_and_is_kept_on_rerun(env):
    run(env, Scripted({"Drive CLI": "y"}))
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) == pname(env)
    ask = Scripted()
    run(env, ask)  # Enter everywhere: "use Drive CLI?" defaults to yes now, profile kept
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) == pname(env)
    assert any("Drive CLI" in p and "[Y/n]" in p for p in ask.asked)


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
        "任意の項目": "y", "チャンネル ID": "C0TEST123", "表示名": "Helper", "アイコン絵文字": ":abacus:",
        "モデル": "2", "effort": "high", "監視するファイル": ".agents/local-tools.md, a/b.md",
        "権限ボタンの統計": "y"}))
    pid = project_json(env).stem
    assert channels(env) == {"C0TEST123": {
        "project": pid, "display_name": "Helper", "icon_emoji": ":abacus:",
        "model": "gemini-x", "effort": "high",
        "watched_config_files": [".agents/local-tools.md", "a/b.md"],
        "telemetry": {"permission_stats": True}}}
    assert (env.cfg / "config.yaml.bak").exists()
    assert "# Example entries" in (env.cfg / "config.yaml").read_text()  # comments survive


def test_channel_prompts_reject_bad_values_then_accept(env):
    ask = Scripted({"任意の項目": "y", "チャンネル ID": ["nope", "C0TEST123"], "effort": ["extreme", "low"],
                    "アイコン絵文字": ["bad name", "abacus"], "監視するファイル": ["/etc/passwd", "../x", "ok.md"]})
    run(env, ask)
    e = channels(env)["C0TEST123"]
    assert (e["effort"], e["icon_emoji"], e["watched_config_files"]) == ("low", ":abacus:", ["ok.md"])


def test_rerun_shows_current_values_and_dash_clears(env):
    run(env, Scripted({"任意の項目": "y", "チャンネル ID": "C0TEST123", "表示名": "Helper", "モデル": "1"}))
    ask = Scripted({"任意の項目": "y", "Slack チャンネルを対応づけ": "y", "モデル": "-", "表示名": "Renamed"})
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
    run(env, Scripted({"任意の項目": "y", "Slack チャンネルを対応づけ": "y", "表示名": "X"}))
    assert channels(env)["C0TEST123"]["future_key"] == {"a": 1}


def test_channel_of_another_project_is_not_taken_over_by_default(env, tmp_path):
    run(env, Scripted({"任意の項目": "y", "チャンネル ID": "C0TEST123", "表示名": "first"}))
    second = tmp_path / "proj2"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    run(env, Scripted({"任意の項目": "y", "チャンネル ID": "C0TEST123", "表示名": "second"}), target=second)
    assert channels(env)["C0TEST123"]["display_name"] == "first"
    run(env, Scripted({"任意の項目": "y", "チャンネル ID": "C0TEST123", "置き換えますか": "y", "表示名": "second"}),
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
    assert (folder / ".agents/rules/agent-safety-rules.md").is_symlink()
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
    ask = Scripted({"番号": ["7", "0"], "ディレクトリのパス": str(new)})
    run_menu(env, ask)
    assert (new / ".git").is_dir()
    assert len(env.registered) == 1
    ask = Scripted({"番号": str(tmp_path / "other"), "上書きしますか": "y"})
    (tmp_path / "other").mkdir()
    run_menu(env, ask)
    assert (tmp_path / "other/.git").is_dir()


def test_optional_shared_files_become_rules_and_warn_without_frontmatter(env, capsys):
    shared = env.cfg / "shared"
    shared.mkdir()
    (shared / "local-tools.md").write_text("---\ntrigger: always_on\n---\ntools\n")
    (shared / "gdrive-cli.md").write_text("no frontmatter here\n")
    run(env)
    rules = env.target / ".agents/rules"
    assert (rules / "local-tools.md").resolve() == (shared / "local-tools.md").resolve()
    assert (rules / "gdrive-cli.md").is_symlink()
    out = capsys.readouterr().out
    assert f"{shared / 'gdrive-cli.md'} に frontmatter" in out
    assert f"{shared / 'local-tools.md'} に frontmatter" not in out
    assert not (env.target / "AGENTS.md").exists()


def test_legacy_links_and_agents_md_lines_are_migrated_but_foreign_ones_kept(env):
    agents = env.target / ".agents"
    agents.mkdir()
    (agents / "agent-safety-rules.md").symlink_to(env.prefix / "tools/agent-safety-rules.md")
    foreign = env.tmp / "elsewhere.md"
    foreign.write_text("mine")
    (agents / "local-tools.md").symlink_to(foreign)  # not ours: leave it
    (env.target / "AGENTS.md").write_text(
        "@[エージェント安全規則](.agents/agent-safety-rules.md)\n"
        "@[このマシンで使えるツール](.agents/local-tools.md)\n# my own notes\n")
    run(env)
    assert not (agents / "agent-safety-rules.md").exists()
    assert (agents / "local-tools.md").is_symlink()
    assert (env.target / "AGENTS.md").read_text() == (
        "@[このマシンで使えるツール](.agents/local-tools.md)\n# my own notes\n")
    assert (agents / "rules/agent-safety-rules.md").is_symlink()


# --- Drive profile policy (interactive) -------------------------------------------------

import gdrive_policy  # noqa: E402

FOLDER = "1AbCdEfGhIjKlMnOp"
OTHER_FOLDER = "2AbCdEfGhIjKlMnOp"
PROFILE_Q = {"Drive CLI": "y"}  # profile name defaults to the project dir name: "proj"


def profile_path(env, name=None):
    name = name or pname(env)
    return env.cfg / "gdrive" / f"{name}.json"


def allow(env):
    data = json.loads(project_json(env).read_text())
    return data["permissionGrants"]["permissionGrants"]["allow"]


def subs_in(entries):
    return sorted(e[:-1].split()[-1] for e in entries)


def seed_profile(env, **extra):
    data = {"kind": "service_account", "credentials": {"command": ["cat", "/key.json"]},
            "scopes": {"read": "https://www.googleapis.com/auth/drive.readonly",
                       "write": "https://www.googleapis.com/auth/drive"}, **extra}
    gdrive_policy.write_profile(profile_path(env), data)
    return data


def test_missing_profile_is_created_interactively_with_default_policy(env):
    ask = Scripted({**PROFILE_Q, "プロファイルを作成しますか": "y", "認証の種類": "oauth",
                    "資格情報を取り出すコマンド": "op read 'op://vault/item/token json'"})
    run(env, ask)
    data = json.loads(profile_path(env).read_text())
    assert data == {"kind": "oauth", "credentials": {"command": ["op", "read", "op://vault/item/token json"]}}
    assert stat.S_IMODE(os.stat(profile_path(env)).st_mode) == 0o600
    assert subs_in(allow(env)) == ["download", "list", "meta", "read", "search"]


def test_service_account_key_file_is_stored_under_config_dir_and_wired_up(env, tmp_path, capsys):
    key = tmp_path / "downloaded.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "bot@p.iam.gserviceaccount.com",
                               "private_key": "-----BEGIN-----"}))
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    ask = Scripted({**PROFILE_Q, "プロファイルを作成しますか": "y",
                    "サービスアカウントの JSON 鍵のパス": [str(bad), str(tmp_path / "none.json"), str(key)]})
    run(env, ask)
    stored = env.cfg / "gdrive" / f"{pname(env)}.sa.json"
    assert stat.S_IMODE(os.stat(stored).st_mode) == 0o600
    assert json.loads(stored.read_text())["client_email"] == "bot@p.iam.gserviceaccount.com"
    data = json.loads(profile_path(env).read_text())
    assert data["kind"] == "service_account"
    assert data["credentials"]["command"][1:] == [str(stored)] and data["credentials"]["paths"] == [str(stored)]
    out = capsys.readouterr().out
    assert "bot@p.iam.gserviceaccount.com" in out and "共有してください" in out
    assert not any("資格情報を取り出すコマンド" in p for p in ask.asked)  # no command prompt needed
    setup.gdrive_cli.parse_profile(data)


def test_custom_scopes_are_asked_only_on_request(env):
    ask = Scripted({**PROFILE_Q, "プロファイルを作成しますか": "y", "認証の種類": "service_account",
                    "資格情報を取り出すコマンド": "cat /k", "詳細設定": "y",
                    "読み取りに使うスコープ": ["bad", "https://www.googleapis.com/auth/drive.file"],
                    "書き込みに使うスコープ": "https://www.googleapis.com/auth/drive.file"})
    run(env, ask)
    assert json.loads(profile_path(env).read_text())["scopes"] == {
        "read": "https://www.googleapis.com/auth/drive.file", "write": "https://www.googleapis.com/auth/drive.file"}


def test_declining_to_create_or_missing_input_leaves_no_profile_and_all_reads_allowed(env):
    run(env, Scripted({**PROFILE_Q, "プロファイルを作成しますか": "n"}))
    assert not profile_path(env).exists()
    assert subs_in(allow(env)) == ["download", "list", "meta", "read", "search"]
    run(env, Scripted({**PROFILE_Q, "プロファイルを作成しますか": "y"}))  # no kind entered
    assert not profile_path(env).exists()


def test_policy_change_writes_only_deviations_and_keeps_credentials_and_scopes(env):
    before = seed_profile(env)
    ask = Scripted({**PROFILE_Q, "詳細設定": "y", "有効にするコマンド": "list,read",
                    "許可フォルダ": f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing",
                    "アップロード拒否パターン（k": "a", "追加する拒否ファイル名パターン": "*.sqlite, *.db",
                    "追加する拒否ディレクトリ名": "private"})
    run(env, ask)
    data = json.loads(profile_path(env).read_text())
    assert data["kind"] == before["kind"] and data["credentials"] == before["credentials"]
    assert data["scopes"] == before["scopes"]
    assert data["commands"] == ["list", "read"] and data["allowed_folders"] == [FOLDER]
    assert data["upload_deny"]["names"][-2:] == ["*.sqlite", "*.db"]
    assert data["upload_deny"]["dirs"][-1] == "private"
    assert subs_in(allow(env)) == ["list", "read"]  # only the enabled read commands


def test_disabling_a_command_removes_the_allow_entry_setup_added_earlier(env):
    seed_profile(env)
    run(env, Scripted(PROFILE_Q))
    assert subs_in(allow(env)) == ["download", "list", "meta", "read", "search"]
    run(env, Scripted({**PROFILE_Q, "詳細設定": "y", "有効にするコマンド": "list,meta"}))
    assert subs_in(allow(env)) == ["list", "meta"]
    run(env, Scripted({**PROFILE_Q, "詳細設定": "y", "有効にするコマンド": "all", "緩める変更を適用しますか": "y"}))
    assert subs_in(allow(env)) == ["download", "list", "meta", "read", "search"]


def test_loosening_needs_a_separate_yes_and_is_dropped_on_no(env):
    seed_profile(env, allowed_folders=[FOLDER], commands=["list"])
    original = profile_path(env).read_text()
    base = {**PROFILE_Q, "詳細設定": "y", "有効にするコマンド": "list,upload", "許可フォルダ": "-"}
    ask = Scripted({**base, "緩める変更を適用しますか": "n"})
    run(env, ask)
    assert profile_path(env).read_text() == original
    assert any("緩める変更を適用しますか" in p and "[y/N]" in p for p in ask.asked)  # default is no
    run(env, Scripted({**base, "緩める変更を適用しますか": "y"}))
    data = json.loads(profile_path(env).read_text())
    assert data["commands"] == ["list", "upload"] and "allowed_folders" not in data


def test_tightening_does_not_ask_for_the_loosening_confirmation(env):
    seed_profile(env)
    ask = Scripted({**PROFILE_Q, "詳細設定": "y", "許可フォルダ": FOLDER})
    run(env, ask)
    assert json.loads(profile_path(env).read_text())["allowed_folders"] == [FOLDER]
    assert not any("緩める変更" in p for p in ask.asked)


def test_policy_prompts_reject_bad_input_then_accept(env):
    seed_profile(env)
    ask = Scripted({**PROFILE_Q, "詳細設定": "y", "有効にするコマンド": ["delete", "list"],
                    "許可フォルダ": ["https://example.com/x", FOLDER]})
    run(env, ask)
    data = json.loads(profile_path(env).read_text())
    assert data["commands"] == ["list"] and data["allowed_folders"] == [FOLDER]


def test_rerun_with_enter_everywhere_changes_nothing(env):
    seed_profile(env, allowed_folders=[FOLDER], commands=["list", "read"])
    run(env, Scripted(PROFILE_Q))
    first = (profile_path(env).read_text(), allow(env))
    run(env, Scripted())
    assert (profile_path(env).read_text(), allow(env)) == first


def test_broken_or_invalid_profile_is_never_overwritten(env, capsys):
    profile_path(env).parent.mkdir(parents=True)
    profile_path(env).write_text("{not json")
    run(env, Scripted(PROFILE_Q))
    assert profile_path(env).read_text() == "{not json"
    profile_path(env).parent.mkdir(parents=True, exist_ok=True)
    profile_path(env).write_text(json.dumps({"kind": "oauth", "oops": 1}))
    run(env, Scripted(PROFILE_Q))
    assert json.loads(profile_path(env).read_text()) == {"kind": "oauth", "oops": 1}
    assert "スキップ" in capsys.readouterr().out


def test_credential_command_that_does_not_exist_needs_confirmation(env):
    base = {**PROFILE_Q, "プロファイルを作成しますか": "y", "認証の種類": "oauth", "資格情報を取り出すコマンド": "drive"}
    run(env, Scripted(base))  # "drive" is not a command: default answer is no -> nothing written
    assert not profile_path(env).exists()
    run(env, Scripted({**base, "というコマンドが見つかりません": "y"}))
    assert json.loads(profile_path(env).read_text())["credentials"]["command"] == ["drive"]
    # a real command (cat) raises no question
    profile_path(env).unlink()
    ask = Scripted({**base, "資格情報を取り出すコマンド": "cat /k"})
    run(env, ask)
    assert not any("見つかりません" in p for p in ask.asked)
    assert profile_path(env).exists()


def test_authentication_kind_defaults_to_service_account_and_prompt_is_short(env, capsys):
    ask = Scripted({**PROFILE_Q, "プロファイルを作成しますか": "y", "資格情報を取り出すコマンド": "cat /k"})  # Enter on the kind
    run(env, ask)
    assert json.loads(profile_path(env).read_text())["kind"] == "service_account"
    out = capsys.readouterr().out
    assert "7 日で失効" not in out and "同意画面" not in out  # explanations live in the docs
    kind_prompt = next(p for p in ask.asked if "認証の種類" in p)
    assert len(kind_prompt) < 80


# --- menus -----------------------------------------------------------------------------

def menu_session(env, answers):
    ask = Scripted(answers)
    return setup.Session(ask, env.prefix.resolve(), env.cfg.resolve()), ask


def test_main_menu_lists_projects_and_quits_on_enter(env, capsys):
    session, ask = menu_session(env, {})
    setup.main_menu(session)                      # Enter = the default, q
    out = capsys.readouterr().out
    assert "新しいプロジェクトを作る" in out and "終了" in out and "Slack app の設定" in out
    assert "再起動" in out


def test_new_project_then_project_menu_runs_everything_and_returns(env, capsys):
    session, ask = menu_session(env, {"選択": ["n", "1", "b", "q"],
                                      "新しいプロジェクトのディレクトリ": str(env.target),
                                      "上書きしますか": "y", "Drive CLI": "n", "Slack チャンネルを対応づけ": "y",
                                      "チャンネル ID": "C0TEST123"})
    setup.main_menu(session)
    assert channels(env)["C0TEST123"]["project"] == session.project_id
    assert (env.target / ".agents/rules/agent-safety-rules.md").is_symlink()


def test_selecting_an_existing_project_by_number(env, capsys):
    run(env)                                      # registers env.target
    session, ask = menu_session(env, {"選択": ["1", "8", "b", "q"]})
    setup.main_menu(session)
    out = capsys.readouterr().out
    assert "1) proj" in out and "プロジェクト: proj" in out and "Slack チャンネル: 未設定" in out


def test_project_menu_shows_state_per_item(env, capsys):
    run(env)
    session, ask = menu_session(env, {"選択": ["1", "b", "q"]})
    setup.main_menu(session)
    out = capsys.readouterr().out
    assert "配置・更新する  [設定済み]" in out
    assert "設定する  [未設定]" in out          # Slack channel and Drive CLI are not set


def test_menu_rejects_unknown_choices_and_survives_errors(env, capsys):
    session, ask = menu_session(env, {"選択": ["zzz", "n", "q"],
                                      "新しいプロジェクトのディレクトリ": str(env.prefix),
                                      "上書きしますか": "y"})  # inside the install prefix
    setup.main_menu(session)
    out = capsys.readouterr().out
    assert sum("選択" in p for p in ask.asked) == 3  # "zzz" was rejected and asked again
    assert "インストール先" in out and "再起動" in out


def test_menu_tokens_item_is_global(env):
    session, ask = menu_session(env, {"選択": ["t", "q"]})
    session.ask.secrets = {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"}
    setup.main_menu(session)
    env_text = (env.cfg / "env").read_text()
    assert "xoxb-1" in env_text and "xapp-1" in env_text


def test_drive_and_channel_need_a_registered_project(env, capsys):
    session, _ = menu_session(env, {})
    session.drive()
    session.channel()
    assert capsys.readouterr().out.count("登録されたプロジェクトが必要") == 2


def test_path_completion_expands_tilde_and_marks_directories(tmp_path, monkeypatch):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alps.txt").write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert setup.complete_path("~/al") == [str(tmp_path / "alpha") + "/", str(tmp_path / "alps.txt")]


def test_quit_from_project_menu_ends_the_whole_setup(env, capsys):
    run(env)
    # "q" is the last scripted answer; if it only returned to the list, the list would ask again and fail
    session, ask = menu_session(env, {"選択": ["1", "q"]})
    setup.main_menu(session)
    assert "終了" in capsys.readouterr().out


def test_icon_emoji_colons_are_optional(env):
    run(env, Scripted({"任意の項目": "y", "チャンネル ID": "C0TEST123", "アイコン絵文字": "試験管"}))
    assert channels(env)["C0TEST123"]["icon_emoji"] == ":試験管:"


def test_icon_emoji_accepts_any_script_and_rejects_only_spaces_and_inner_colons(env):
    assert setup._validate_emoji("สวัสดี") is None          # combining marks
    assert setup._validate_emoji(":試験管:") is None
    assert setup._validate_emoji("a b") is not None
    assert setup._validate_emoji("a:b") is not None


def test_existing_directory_needs_overwrite_confirmation_and_new_one_does_not(env, tmp_path):
    ask = Scripted({"選択": ["n", "q"], "新しいプロジェクトのディレクトリ": str(env.target)})  # 上書き: Enter = no
    session = setup.Session(ask, env.prefix, env.cfg)
    setup.main_menu(session)
    assert any("上書きしますか" in p for p in ask.asked)
    assert env.registered == []
    ask = Scripted({"選択": ["n", "q"], "新しいプロジェクトのディレクトリ": str(tmp_path / "brand-new")})
    setup.main_menu(setup.Session(ask, env.prefix, env.cfg))
    assert not any("上書きしますか" in p or "作成しますか" in p or "登録しますか" in p for p in ask.asked)
    assert (tmp_path / "brand-new/.git").is_dir() and len(env.registered) == 1


def test_menu_items_3_and_4_do_not_ask_the_use_it_gate(env):
    run(env)   # registers env.target
    session, ask = menu_session(env, {"選択": ["1", "3", "4", "b", "q"], "認証の種類": "oauth",
                                      "資格情報を取り出すコマンド": "cat /k", "チャンネル ID": "C0TEST123"})
    setup.main_menu(session)
    assert not any("使いますか" in p or "対応づけますか" in p for p in ask.asked)
    assert channels(env)["C0TEST123"]["project"] == session.project_id


def test_default_profile_name_is_the_project_id(env):
    run(env, Scripted({"Drive CLI": "y"}))
    pid = project_json(env).stem
    assert setup.default_profile_name(pid) == pid
    assert setup.default_profile_name("a b;c") == "a-b-c" and setup._PROFILE_NAME_RE.match(pid)
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) == pid


def test_second_project_can_share_the_first_projects_profile_or_create_its_own(env, tmp_path):
    run(env, Scripted({"Drive CLI": "y"}))                       # first project: its own profile
    first = pname(env)
    profile_path(env).parent.mkdir(parents=True, exist_ok=True)
    profile_path(env).write_text(json.dumps({"kind": "oauth", "credentials": {"command": ["cat", "/k"]}}))
    second = tmp_path / "second"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    ask = Scripted({"Drive CLI": "y", "選択": "1"})              # "proj と同じ設定・権限を使う"
    run(env, ask, target=second)
    mine = setup.find_agy_project_json(second.resolve())
    assert setup.current_gdrive_profile(mine, env.prefix.resolve()) == first
    assert not profile_path(env, pname(env, second)).exists()

    third = tmp_path / "third"
    third.mkdir()
    subprocess.run(["git", "init", "-q", str(third)], check=True)
    run(env, Scripted({"Drive CLI": "y", "認証の種類": "oauth", "資格情報を取り出すコマンド": "cat /k"}), target=third)
    t = setup.find_agy_project_json(third.resolve())
    assert setup.current_gdrive_profile(t, env.prefix.resolve()) == pname(env, third) != first


# --- Drive CLI update menu (project already has a profile) -----------------------------

def _drive_project(env, target=None, cmd="cat /k"):
    """A project with an oauth profile created through the script; returns the profile name."""
    t = target or env.target
    run(env, Scripted({**PROFILE_Q, "認証の種類": "oauth", "資格情報を取り出すコマンド": cmd}), target=t)
    return setup.current_gdrive_profile(setup.find_agy_project_json(t.resolve()), env.prefix.resolve())


def _drive_menu(env, answers):
    """main menu: project 1 -> project menu 3 (Drive) -> the scripted drive-menu answers -> back -> quit"""
    session, ask = menu_session(env, answers)
    setup.main_menu(session)
    return ask


def _second_project(env, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    return second


def test_drive_menu_changes_the_policy_of_an_existing_profile(env):
    _drive_project(env)
    _drive_menu(env, {"選択": ["1", "7", "1", "b", "b", "q"], "有効にするコマンド": "list,read"})
    assert json.loads(profile_path(env).read_text())["commands"] == ["list", "read"]
    assert subs_in(allow(env)) == ["list", "read"]


def test_drive_menu_reenters_credentials_keeping_the_policy(env):
    _drive_project(env)
    data = json.loads(profile_path(env).read_text())
    data["allowed_folders"] = ["F" * 20]
    profile_path(env).write_text(json.dumps(data))
    _drive_menu(env, {"選択": ["1", "7", "2", "b", "b", "q"], "認証の種類": "oauth",
                      "資格情報を取り出すコマンド": "cat /new"})
    new = json.loads(profile_path(env).read_text())
    assert new["credentials"]["command"][-1] == "/new" and new["allowed_folders"] == ["F" * 20]


def test_drive_menu_reentering_a_service_account_key_replaces_the_stored_key_and_drops_unused_ones(env, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_email": "sa@p.iam.gserviceaccount.com", "private_key": "k", "type": "service_account"}))
    run(env, Scripted({**PROFILE_Q, "JSON 鍵のパス": str(key)}))
    stored = env.cfg / "gdrive" / f"{pname(env)}.sa.json"
    assert stored.exists()
    _drive_menu(env, {"選択": ["1", "7", "2", "b", "b", "q"], "認証の種類": "oauth",
                      "資格情報を取り出すコマンド": "cat /k"})
    assert not stored.exists()                       # the plaintext key is not left behind
    assert json.loads(profile_path(env).read_text())["kind"] == "oauth"


def test_drive_menu_switches_to_another_projects_profile_and_back_to_a_dedicated_one(env, tmp_path):
    first = _drive_project(env)
    second = _second_project(env, tmp_path)
    own = _drive_project(env, second, "cat /k2")
    assert own != first
    # projects are listed by name: proj (1), second (2). In second: Drive menu -> 3 (switch) -> 1 (proj's profile)
    _drive_menu(env, {"選択": ["2", "7", "3", "1", "b", "b", "q"]})
    mine = setup.find_agy_project_json(second.resolve())
    assert setup.current_gdrive_profile(mine, env.prefix.resolve()) == first
    assert not any(own in e for e in json.loads(mine.read_text())["permissionGrants"]["permissionGrants"]["allow"])
    # own profile is now unused: deleting is offered, default no
    assert (env.cfg / "gdrive" / f"{own}.json").exists()
    # back to the dedicated profile (entry n): reused because it still exists
    _drive_menu(env, {"選択": ["2", "7", "3", "n", "b", "b", "q"]})
    assert setup.current_gdrive_profile(mine, env.prefix.resolve()) == own


def test_drive_menu_detach_removes_only_this_projects_grants_and_offers_to_delete_the_unused_profile(env):
    name = _drive_project(env)
    ask = _drive_menu(env, {"選択": ["1", "7", "4", "q"], "削除しますか": "y"})
    assert setup.current_gdrive_profile(project_json(env), env.prefix.resolve()) is None
    assert not profile_path(env).exists()
    assert any("削除しますか" in p for p in ask.asked)


def test_drive_menu_detach_keeps_a_profile_another_project_still_uses(env, tmp_path):
    first = _drive_project(env)
    second = _second_project(env, tmp_path)
    run(env, Scripted({"Drive CLI": "y", "選択": "1"}), target=second)      # shares the first profile
    ask = _drive_menu(env, {"選択": ["2", "7", "4", "q"]})
    assert setup.current_gdrive_profile(setup.find_agy_project_json(second.resolve()), env.prefix.resolve()) is None
    first_json = setup.find_agy_project_json(env.target.resolve())
    assert setup.current_gdrive_profile(first_json, env.prefix.resolve()) == first
    assert profile_path(env).exists()
    assert not any("削除しますか" in p for p in ask.asked)


def test_channel_asks_only_the_channel_id_then_whether_to_continue_with_the_optional_items(env):
    ask = Scripted({"チャンネル ID": "C0TEST123"})            # the optional-items question: Enter = no
    run(env, ask)
    assert channels(env)["C0TEST123"] == {"project": project_json(env).stem}
    asked = [p for p in ask.asked if "表示名" in p or "モデル" in p or "effort" in p]
    assert len(asked) == 1 and "任意の項目" in asked[0]       # only the gate mentions them


def test_required_settings_come_before_the_optional_drive_cli(env):
    ask = Scripted({"チャンネル ID": "C0TEST123", "Drive CLI": "y"})
    run(env, ask)
    order = [p for p in ask.asked if "チャンネル ID" in p or "Slack トークン" in p or "SLACK_" in p or "Drive CLI を使います" in p]
    assert [("チャンネル" in p, "Drive" in p) for p in order][0] == (True, False)
    assert "Drive" in order[-1]


# --- project list status ------------------------------------------------------------------

def _status(env, capsys):
    session, ask = menu_session(env, {})
    setup.main_menu(session)                 # Enter = quit; the list is what we look at
    return capsys.readouterr().out


def test_list_shows_unset_for_a_registered_but_unconfigured_project(env, capsys):
    setup.register_agy_project(env.target.resolve())
    out = _status(env, capsys)
    assert "[未設定]" in out and "未設定（必須）: ルールとフック, Slack チャンネル" in out
    assert "未設定（任意）" not in out


def test_list_shows_in_progress_with_what_is_missing(env, capsys):
    run(env)                                  # rules + hook, but no channel
    out = _status(env, capsys)
    assert "[利用不可]" in out and "未設定（必須）: Slack チャンネル" in out and "ルールとフック" not in out.split("未設定（必須）:")[1].splitlines()[0]


def test_configured_project_lists_every_unset_optional_item(env, capsys):
    run(env, Scripted({"チャンネル ID": "C0TEST123"}))
    out = _status(env, capsys)
    assert "[利用可能]" in out and "Slack: C0TEST123" in out
    optional = [l for l in out.splitlines() if "未設定（任意）" in l][0]
    for item in ("Drive CLI", "表示名", "アイコン", "モデル", "effort", "監視ファイル", "権限ボタンの統計"):
        assert item in optional


def test_optional_items_disappear_from_the_list_once_set(env, capsys):
    run(env, Scripted({"任意の項目": "y", "チャンネル ID": "C0TEST123", "表示名": "Bot", "アイコン絵文字": "robot_face",
                       "モデル": "1", "effort": "high", "監視するファイル": "AGENTS.md", "権限ボタンの統計": "n",
                       "Drive CLI": "y", "認証の種類": "oauth", "資格情報を取り出すコマンド": "cat /k"}))
    out = _status(env, capsys)
    assert "[利用可能]" in out and "未設定（任意）" not in out


def test_broken_safety_mechanism_makes_a_configured_project_incomplete(env, capsys):
    run(env, Scripted({"チャンネル ID": "C0TEST123"}))
    (env.target / ".agents/rules/agent-safety-rules.md").unlink()
    out = _status(env, capsys)
    assert "[利用不可]" in out and "未設定（必須）: ルールとフック" in out


def test_list_warns_when_the_drive_profile_is_missing(env, capsys):
    run(env, Scripted({"チャンネル ID": "C0TEST123", "Drive CLI": "y", "認証の種類": "oauth",
                       "資格情報を取り出すコマンド": "cat /k"}))
    profile_path(env).unlink()
    assert "⚠ Drive のプロファイルがありません" in _status(env, capsys)


def test_main_menu_shows_whether_the_tokens_are_set(env, capsys):
    out = _status(env, capsys)
    assert "未設定（必須）: SLACK_BOT_TOKEN, SLACK_APP_TOKEN" in out
    (env.cfg / "env").write_text("SLACK_BOT_TOKEN=xoxb-real\nSLACK_APP_TOKEN=xapp-real\n")
    assert "[設定済み]" in _status(env, capsys)


def test_model_menu_lists_agy_models_and_falls_back_to_typing(env, monkeypatch, capsys):
    monkeypatch.setattr(setup, "fetch_agy_models", lambda *_: [("a", "A"), ("b", "B")])
    cfg = env.cfg
    assert setup.ask_model(Scripted({"モデル": "2"}), None, cfg) == "b"
    assert setup.ask_model(Scripted(), "b", cfg) == "b"                   # Enter keeps the current model
    assert setup.ask_model(Scripted(), "zzz", cfg) == "zzz"               # unknown current value is kept
    assert setup.ask_model(Scripted({"モデル": "-"}), "b", cfg) is None
    monkeypatch.setattr(setup, "fetch_agy_models", lambda *_: [])
    assert setup.ask_model(Scripted({"モデル ID": "typed"}), None, cfg) == "typed"
    assert "取得できませんでした" in capsys.readouterr().out


def test_models_cache_is_shared_and_only_refetched_when_stale(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(agy_models, "run_agy_models", lambda agy: calls.append(agy) or [("a", "A")])
    assert agy_models.get_models(tmp_path, "agy", 60) == [("a", "A")]       # fetched, saved
    assert setup.fetch_agy_models(tmp_path) == [("a", "A")]                  # the setup script reuses it
    assert len(calls) == 1
    ts, _ = agy_models.load_cache(tmp_path)
    (tmp_path / agy_models.CACHE_NAME).write_text(json.dumps({"ts": ts - 99999, "models": [["old", "Old"]]}))
    monkeypatch.setattr(agy_models, "run_agy_models", lambda agy: [])        # agy fails: stale list is returned
    assert agy_models.get_models(tmp_path, "agy", 60) == [("old", "Old")]


def test_prefetch_warms_the_cache_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_models, "run_agy_models", lambda agy: [("a", "A")])
    setup.prefetch_agy_models(tmp_path)
    setup._models_prefetch.join()
    assert agy_models.load_cache(tmp_path)[1] == [("a", "A")]


def test_q_quits_from_any_menu_level_and_from_yes_no_prompts(env, capsys):
    _drive_project(env)
    session, ask = menu_session(env, {"選択": ["1", "7", "q"]})       # list -> project -> Drive menu -> q
    setup.main_menu(session)
    assert "中断しました" in capsys.readouterr().out
    with pytest.raises(setup.QuitSetup):
        Scripted({"どうしますか": "q"}).yes_no("どうしますか?")
    with pytest.raises(setup.QuitSetup):
        setup.choose(Scripted({"選択": "q"}), "t", [("1", "a")], "1")


def _interrupting_prompter(replies):
    """input() が replies を順に返す。KeyboardInterrupt / EOFError の項目は、その例外を投げる。"""
    seq = iter(replies)

    def fn(prompt):
        r = next(seq)
        if isinstance(r, type) and issubclass(r, BaseException):
            raise r
        return r
    return setup.Prompter(input_fn=fn, secret_fn=fn, out=lambda *_: None)


def test_ctrl_c_asks_whether_to_quit_and_n_returns_to_the_question():
    ask = _interrupting_prompter([KeyboardInterrupt, "n", "value"])
    assert ask.text("x") == "value"
    ask = _interrupting_prompter([EOFError, "", "yes"])               # Enter on the dialog = no
    assert ask.yes_no("go?") is True


def test_ctrl_c_then_y_quits_without_a_traceback(env, capsys):
    ask = _interrupting_prompter([KeyboardInterrupt, "y"])
    setup.main([str(env.target), "--prefix", str(env.prefix), "--bridge-config-dir", str(env.cfg)], ask=ask)
    out = capsys.readouterr()
    assert "中断しました" in out.out and "Traceback" not in out.err
    with pytest.raises(setup.QuitSetup):                              # a second Ctrl-C on the dialog quits too
        _interrupting_prompter([KeyboardInterrupt, KeyboardInterrupt]).text("x")


def test_channel_option_groups_are_separate_menu_items(env):
    run(env, Scripted({"チャンネル ID": "C0TEST123"}))
    session, ask = menu_session(env, {"選択": ["1", "5", "b", "q"], "モデル": "1", "effort": "high"})
    setup.main_menu(session)
    e = channels(env)["C0TEST123"]
    assert (e["model"], e["effort"]) == ("m1", "high") and "display_name" not in e
    assert not any("表示名" in p or "監視する" in p for p in ask.asked)
