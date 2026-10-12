import json

import pytest

import bridge


def _write_project(dir_, project_id, **extra):
    data = {"projectResources": {"resources": []}, **extra}
    (dir_ / f"{project_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _allow(dir_, project_id):
    data = json.loads((dir_ / f"{project_id}.json").read_text(encoding="utf-8"))
    return data["permissionGrants"]["permissionGrants"]["allow"]


@pytest.fixture
def perms(tmp_path):
    _write_project(tmp_path, "a")
    _write_project(tmp_path, "b")
    return bridge.ProjectPermissionsFile(tmp_path)


def test_add_writes_double_nested_allow(perms, tmp_path):
    assert perms.add_entry("a", "command(git diff)") is True
    assert _allow(tmp_path, "a") == ["command(git diff)"]


def test_add_is_scoped_to_one_project(perms, tmp_path):
    perms.add_entry("a", "command(ls)")
    assert perms.list_entries("a") == ["command(ls)"]
    assert perms.list_entries("b") == []
    assert "permissionGrants" not in json.loads((tmp_path / "b.json").read_text())


def test_add_duplicate_returns_false(perms):
    assert perms.add_entry("a", "command(ls)") is True
    assert perms.add_entry("a", "command(ls)") is False
    assert perms.list_entries("a") == ["command(ls)"]


def test_remove(perms):
    perms.add_entry("a", "command(ls)")
    assert perms.remove_entry("a", "command(ls)") is True
    assert perms.remove_entry("a", "command(ls)") is False
    assert perms.list_entries("a") == []


def test_other_keys_preserved(tmp_path):
    _write_project(tmp_path, "a", settings={"x": 1},
                   permissionGrants={"other": True, "permissionGrants": {"deny": ["d"], "allow": ["e1"]}})
    p = bridge.ProjectPermissionsFile(tmp_path)
    p.add_entry("a", "e2")
    data = json.loads((tmp_path / "a.json").read_text())
    assert data["settings"] == {"x": 1}
    assert data["permissionGrants"]["other"] is True
    assert data["permissionGrants"]["permissionGrants"]["deny"] == ["d"]
    assert data["permissionGrants"]["permissionGrants"]["allow"] == ["e1", "e2"]


def test_missing_project_file_is_refused_not_created(tmp_path):
    p = bridge.ProjectPermissionsFile(tmp_path)
    assert p.add_entry("nope", "command(ls)") is False
    assert p.remove_entry("nope", "command(ls)") is False
    assert p.list_entries("nope") == []
    assert not (tmp_path / "nope.json").exists()
