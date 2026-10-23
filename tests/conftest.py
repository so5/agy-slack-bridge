import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "hooks"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_agy_settings(tmp_path_factory, monkeypatch):
    """No test may read or write the real agy settings.json / hooks.json."""
    import agy_settings
    home = tmp_path_factory.mktemp("agy-home")
    monkeypatch.setattr(agy_settings, "GLOBAL_SETTINGS_PATH", home / "settings.json")
    monkeypatch.setattr(agy_settings, "GLOBAL_HOOKS_PATH", home / "hooks.json")
