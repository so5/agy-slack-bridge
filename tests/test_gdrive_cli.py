import json
import os
import sys

import pytest

import gdrive_cli as g

PY = sys.executable


def make_profile(**over):
    data = {"kind": "service_account", "credentials": {"command": [PY, "-c", "print(1)"]}}
    data.update(over)
    return g.parse_profile(data)


# --- parse_profile ---------------------------------------------------------

def test_defaults_are_todays_behaviour():
    p = make_profile()
    assert p["commands"] == set(g.READ_COMMAND_NAMES + g.WRITE_COMMAND_NAMES)
    assert p["allowed_folders"] == set()
    assert p["scope_read"] == g.SCOPE_READ_DEFAULT and p["scope_write"] == g.SCOPE_WRITE_DEFAULT
    assert p["deny_names"] == g.DENY_UPLOAD_NAMES_DEFAULT
    assert p["deny_dirs"] == set(g.DENY_UPLOAD_DIRS_DEFAULT)


@pytest.mark.parametrize("bad", [
    {"kind": None},
    {"kind": "password"},
    {"credentials": {}},
    {"credentials": {"command": "aws secretsmanager"}},  # must be an argv list, not a shell string
    {"credentials": {"command": []}},
    {"commands": ["list", "delete"]},
    {"allowed_folders": ["short"]},
    {"scopes": {"read": 1}},
    {"scopes": {"admin": "x"}},
    {"upload_deny": {"nmaes": []}},
    {"comands": ["list"]},  # typo must not silently drop a restriction
])
def test_invalid_profiles_rejected(bad):
    data = {"kind": "oauth", "credentials": {"command": ["x"]}}
    data.update(bad)
    if data.get("kind") is None:
        data.pop("kind")
    with pytest.raises(g.ProfileError):
        g.parse_profile(data)


def test_overrides_replace_defaults():
    p = make_profile(commands=["list", "read"], scopes={"read": "R", "write": "W"},
                     upload_deny={"names": ["*.x"], "dirs": ["d"]},
                     allowed_folders=["A" * 12])
    assert p["commands"] == {"list", "read"}
    assert (p["scope_read"], p["scope_write"]) == ("R", "W")
    assert p["deny_names"] == ["*.x"] and p["deny_dirs"] == {"d"}
    assert p["allowed_folders"] == {"A" * 12}


# --- load_profile ----------------------------------------------------------

@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_CONFIG_DIR", str(tmp_path))
    d = tmp_path / "gdrive"
    d.mkdir()
    d.chmod(0o700)
    return d


def write_profile(pdir, name="p", mode=0o600, **over):
    data = {"kind": "oauth", "credentials": {"command": ["x"]}, **over}
    f = pdir / f"{name}.json"
    f.write_text(json.dumps(data))
    f.chmod(mode)
    return f


def test_load_profile_ok(pdir):
    write_profile(pdir)
    assert g.load_profile("p")["kind"] == "oauth"


@pytest.mark.parametrize("name", ["../p", "a/b", "", "p.json", "p q"])
def test_profile_name_cannot_traverse(pdir, name):
    with pytest.raises(g.ProfileError):
        g.load_profile(name)


def test_missing_profile(pdir):
    with pytest.raises(g.ProfileError):
        g.load_profile("nope")


def test_group_writable_profile_refused(pdir):
    write_profile(pdir, mode=0o660)
    with pytest.raises(g.ProfileError, match="書き込み可能"):
        g.load_profile("p")


def test_group_writable_profile_dir_refused(pdir):
    write_profile(pdir)
    pdir.chmod(0o770)
    with pytest.raises(g.ProfileError):
        g.load_profile("p")


def test_world_readable_profile_only_warns(pdir, capsys):
    write_profile(pdir, mode=0o644)
    g.load_profile("p")
    assert "group/other から読めます" in capsys.readouterr().err


def test_invalid_json_message_has_no_content(pdir):
    f = pdir / "p.json"
    f.write_text("{SECRETISH")
    f.chmod(0o600)
    with pytest.raises(g.ProfileError) as e:
        g.load_profile("p")
    assert "SECRETISH" not in str(e.value)


# --- credential command ----------------------------------------------------

def test_fetch_credential_info_runs_argv_and_parses_json():
    p = make_profile(credentials={"command": [PY, "-c", "print('{\"a\": 1}')"]})
    assert g.fetch_credential_info(p) == {"a": 1}


def test_failure_never_leaks_stdout_or_stderr():
    code = "import sys; print('TOPSECRET-OUT'); print('TOPSECRET-ERR', file=sys.stderr); sys.exit(3)"
    p = make_profile(credentials={"command": [PY, "-c", code]})
    with pytest.raises(g.ProfileError) as e:
        g.fetch_credential_info(p)
    msg = str(e.value)
    assert "TOPSECRET" not in msg and "3" in msg
    assert "TOPSECRET" not in repr(e.value.__cause__)


def test_non_json_output_never_leaks():
    p = make_profile(credentials={"command": [PY, "-c", "print('TOPSECRET not json')"]})
    with pytest.raises(g.ProfileError) as e:
        g.fetch_credential_info(p)
    assert "TOPSECRET" not in str(e.value)


def test_non_object_json_rejected():
    p = make_profile(credentials={"command": [PY, "-c", "print('[1]')"]})
    with pytest.raises(g.ProfileError):
        g.fetch_credential_info(p)


def test_missing_command():
    p = make_profile(credentials={"command": ["/nonexistent/binary"]})
    with pytest.raises(g.ProfileError, match="見つかりません"):
        g.fetch_credential_info(p)


def test_timeout(monkeypatch):
    monkeypatch.setattr(g, "CREDENTIAL_COMMAND_TIMEOUT_SEC", 0.2)
    p = make_profile(credentials={"command": [PY, "-c", "import time; time.sleep(5)"]})
    with pytest.raises(g.ProfileError, match="終わりません"):
        g.fetch_credential_info(p)


def test_credential_command_is_not_run_through_a_shell():
    # a shell metacharacter in argv is just an argument
    p = make_profile(credentials={"command": [PY, "-c", "import sys; print(json.dumps({'arg': sys.argv[1]}))"
                                                        .replace("print(json", "import json; print(json"),
                                              "; echo pwned"]})
    assert g.fetch_credential_info(p) == {"arg": "; echo pwned"}


def test_world_readable_credential_file_warns(tmp_path, capsys):
    f = tmp_path / "k.json"
    f.write_text("{}")
    f.chmod(0o644)
    p = make_profile(credentials={"command": [PY, "-c", "print('{}')"], "paths": [str(f)]})
    g.fetch_credential_info(p)
    assert "group/other" in capsys.readouterr().err


def test_credential_shape_validation():
    g.validate_credential_shape("service_account", {"type": "service_account", "private_key": "k", "client_email": "e"})
    g.validate_credential_shape("oauth", {"client_id": "a", "client_secret": "b", "refresh_token": "c"})
    with pytest.raises(g.ProfileError) as e:
        g.validate_credential_shape("service_account", {"client_id": "a", "client_secret": "b", "refresh_token": "SECRETVAL"})
    assert "SECRETVAL" not in str(e.value) and "private_key" in str(e.value)
    with pytest.raises(g.ProfileError, match="refresh_token"):
        g.validate_credential_shape("oauth", {"type": "service_account", "private_key": "k", "client_email": "e"})


# --- allowed_folders -------------------------------------------------------

class FakeDrive:
    """files().get(fileId=..., fields='parents').execute() over a parent map."""
    def __init__(self, parents):
        self.parents, self.calls = parents, []

    def files(self):
        return self

    def get(self, fileId, **kw):
        self.calls.append(fileId)
        return type("Req", (), {"execute": lambda s: {"parents": self.parents.get(fileId, [])}})()


A, B, C, X = "A" * 12, "B" * 12, "C" * 12, "X" * 12


def test_no_allowed_folders_means_unrestricted():
    svc = FakeDrive({})
    g.ensure_in_allowed(svc, make_profile(), X)
    assert svc.calls == []


def test_descendant_allowed():
    svc = FakeDrive({C: [B], B: [A]})
    g.ensure_in_allowed(svc, make_profile(allowed_folders=[A]), C)


def test_folder_itself_allowed_without_api_calls():
    svc = FakeDrive({})
    g.ensure_in_allowed(svc, make_profile(allowed_folders=[A]), A)
    assert svc.calls == []


def test_outside_refused():
    svc = FakeDrive({X: [B], B: []})
    with pytest.raises(SystemExit):
        g.ensure_in_allowed(svc, make_profile(allowed_folders=[A]), X)


def test_parent_cycle_terminates():
    svc = FakeDrive({X: [B], B: [X]})
    with pytest.raises(SystemExit):
        g.ensure_in_allowed(svc, make_profile(allowed_folders=[A]), X)


def test_any_of_multiple_parents_suffices():
    svc = FakeDrive({C: [X, B], B: [A]})
    g.ensure_in_allowed(svc, make_profile(allowed_folders=[A]), C)


def test_search_without_in_refused_when_restricted():
    with pytest.raises(SystemExit):
        g.cmd_search(FakeDrive({}), make_profile(allowed_folders=[A]), ["x"])


# --- upload source ---------------------------------------------------------

@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_upload_default_denies(proj):
    (proj / "ok.txt").write_text("x")
    (proj / ".env").write_text("x")
    (proj / "my_credentials.json").write_text("x")
    (proj / ".venv").mkdir()
    (proj / ".venv/a.txt").write_text("x")
    p = make_profile()
    assert g.resolve_upload_source("ok.txt", p) == str(proj / "ok.txt")
    for bad in (".env", "my_credentials.json", ".venv/a.txt"):
        with pytest.raises(SystemExit):
            g.resolve_upload_source(bad, p)


def test_upload_outside_project_refused(proj, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "f.txt"
    out.write_text("x")
    with pytest.raises(SystemExit):
        g.resolve_upload_source(str(out), make_profile())


def test_upload_symlink_escape_refused(proj, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "f.txt"
    out.write_text("x")
    os.symlink(out, proj / "link.txt")
    with pytest.raises(SystemExit):
        g.resolve_upload_source("link.txt", make_profile())


def test_upload_profile_can_loosen_and_tighten(proj):
    (proj / ".env").write_text("x")
    (proj / "a.log").write_text("x")
    loose = make_profile(upload_deny={"names": [], "dirs": []})
    assert g.resolve_upload_source(".env", loose)
    tight = make_profile(upload_deny={"names": ["*.log"]})
    with pytest.raises(SystemExit):
        g.resolve_upload_source("a.log", tight)


# --- argv ------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    [], ["list"], ["--secret", "x", "list"],
    ["list", "--profile", "p"],  # --profile must come first
    ["--profile", "p"],  # no command
    ["--profile", "p", "delete", "x"],
])
def test_main_rejects_bad_argv(argv):
    with pytest.raises(SystemExit):
        g.main(argv)


QUOTA = (b'{"error": {"code": 403, "message": "Service Accounts do not have storage quota.", '
         b'"errors": [{"reason": "storageQuotaExceeded", "domain": "usageLimits"}]}}')


def test_http_error_message_explains_service_account_storage_quota():
    msg = g.http_error_message(403, QUOTA, True, "sa@proj.iam.gserviceaccount.com")
    assert "storageQuotaExceeded" in msg and "sa@proj.iam.gserviceaccount.com" in msg
    assert "共有ドライブ" in msg and "OAuth" in msg
    assert "共有されていません" not in msg  # not the misleading 'not shared' hint


def test_http_error_message_keeps_the_sharing_hint_for_other_403_404():
    other = b'{"error": {"errors": [{"reason": "notFound"}]}}'
    assert "共有されていません" in g.http_error_message(404, other, False, "sa@x")
    assert "編集権限" in g.http_error_message(403, b"not json", True)
    assert g.http_error_message(500, b"", False) == "error: Drive API 500"
    assert g._error_reason('{"error": {"status": "PERMISSION_DENIED"}}') == "PERMISSION_DENIED"
