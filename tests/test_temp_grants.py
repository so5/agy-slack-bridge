import pytest

import bridge


@pytest.fixture
def store(tmp_path):
    return bridge.ThreadStore(tmp_path / "state.sqlite3")


def _thread(store, conv, project, ts=None):
    store.put("C1", ts or conv, conv, project)


def test_clear_releases_entry_when_no_one_else_holds_it(store):
    _thread(store, "c1", "a")
    store.add_temp_grant("c1", "command(ls)")
    assert store.clear_temp_grants("c1", "a") == ["command(ls)"]
    assert store.clear_temp_grants("c1", "a") == []


def test_clear_keeps_entry_held_by_other_conversation_in_same_project(store):
    _thread(store, "c1", "a")
    _thread(store, "c2", "a")
    store.add_temp_grant("c1", "command(ls)")
    store.add_temp_grant("c2", "command(ls)")
    assert store.clear_temp_grants("c1", "a") == []
    assert store.clear_temp_grants("c2", "a") == ["command(ls)"]


def test_clear_ignores_same_entry_held_in_other_project(store):
    _thread(store, "c1", "a")
    _thread(store, "c2", "b")
    store.add_temp_grant("c1", "command(ls)")
    store.add_temp_grant("c2", "command(ls)")
    assert store.clear_temp_grants("c1", "a") == ["command(ls)"]
    assert store.clear_temp_grants("c2", "b") == ["command(ls)"]


def _age(store, conv):
    store._conn.execute("UPDATE temp_grants SET granted_at=datetime('now','-2 hours') "
                        "WHERE conversation_id=?", (conv,))
    store._conn.commit()


def test_sweep_returns_project_and_entry_for_stale_only(store):
    _thread(store, "old", "a")
    _thread(store, "new", "b")
    store.add_temp_grant("old", "command(ls)")
    store.add_temp_grant("new", "command(ls)")
    _age(store, "old")
    assert store.sweep_stale_temp_grants(3600) == [("a", "command(ls)")]
    # the fresh one in another project is untouched
    assert store.clear_temp_grants("new", "b") == ["command(ls)"]


def test_sweep_keeps_entry_still_held_by_fresh_conversation_in_same_project(store):
    _thread(store, "old", "a")
    _thread(store, "new", "a")
    store.add_temp_grant("old", "command(ls)")
    store.add_temp_grant("new", "command(ls)")
    _age(store, "old")
    assert store.sweep_stale_temp_grants(3600) == []


def test_sweep_skips_grant_without_thread_row(store):
    store.add_temp_grant("orphan", "command(ls)")
    _age(store, "orphan")
    assert store.sweep_stale_temp_grants(3600) == []


def test_sweep_dedupes_same_project_entry(store):
    _thread(store, "o1", "a")
    _thread(store, "o2", "a")
    for c in ("o1", "o2"):
        store.add_temp_grant(c, "command(ls)")
        _age(store, c)
    assert store.sweep_stale_temp_grants(3600) == [("a", "command(ls)")]
