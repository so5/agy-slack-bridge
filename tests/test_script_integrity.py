import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "hooks"))
import script_integrity as si  # noqa: E402


def test_find_script_targets_only_executed_scripts(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.sh").write_text("")
    root = str(tmp_path)
    assert si.find_script_targets("python3 a.py x", root) == [str(tmp_path / "a.py")]
    assert si.find_script_targets("python3 -I a.py && bash b.sh", root) == [
        str(tmp_path / "a.py"), str(tmp_path / "b.sh")]
    assert si.find_script_targets("./b.sh", root) == [str(tmp_path / "b.sh")]
    # a .py that is merely an argument to a non-interpreter is not executed
    assert si.find_script_targets("cat a.py", root) == []
    assert si.find_script_targets("python3 missing.py", root) == []


def _hook(root, cmdline):
    payload = {"toolCall": {"args": {"CommandLine": cmdline}}, "workspacePaths": [str(root)]}
    out = subprocess.run([sys.executable, si.__file__], input=json.dumps(payload),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    (tmp_path / "tool.py").write_text("print(1)\n")
    subprocess.run([*git, "add", "tool.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    return tmp_path


def _seal(root, script, digest):
    (root / ".agents").mkdir(exist_ok=True)
    (root / ".agents/.permanent-commands.json").write_text(json.dumps({str(root / script): digest}))


def test_non_script_command_falls_through_to_ask(repo):
    assert _hook(repo, "ls -l")["decision"] == "ask"


def test_permanent_script_sealed_committed_clean_is_allowed_through(repo):
    _seal(repo, "tool.py", si.file_hash(str(repo / "tool.py")))
    assert _hook(repo, "python3 tool.py")["decision"] == "ask"


def test_permanent_script_changed_since_seal_is_denied_even_if_committed(repo):
    _seal(repo, "tool.py", si.file_hash(str(repo / "tool.py")))
    (repo / "tool.py").write_text("print(2)\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-qam", "edit"], check=True)
    res = _hook(repo, "python3 tool.py")
    assert res["decision"] == "deny" and "seal" in res["reason"]


def test_permanent_script_uncommitted_edit_is_denied(repo):
    _seal(repo, "tool.py", si.file_hash(str(repo / "tool.py")))
    (repo / "tool.py").write_text("print(1)\n# x\n")
    _seal(repo, "tool.py", si.file_hash(str(repo / "tool.py")))  # re-sealed but not committed
    assert _hook(repo, "python3 tool.py")["decision"] == "deny"


def test_temp_script_changed_between_runs_is_denied_once_with_diff(repo):
    (repo / "scratch.py").write_text("a\n")
    assert _hook(repo, "python3 scratch.py")["decision"] == "ask"  # first sighting
    (repo / "scratch.py").write_text("b\n")
    res = _hook(repo, "python3 scratch.py")
    assert res["decision"] == "deny" and "-a" in res["reason"] and "+b" in res["reason"]
    assert _hook(repo, "python3 scratch.py")["decision"] == "ask"  # second ask passes
