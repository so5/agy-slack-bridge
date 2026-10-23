import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_agy_settings(tmp_path_factory, monkeypatch):
    """No test may read or write the real ~/.gemini/antigravity-cli/settings.json."""
    import agy_settings
    monkeypatch.setattr(agy_settings, "GLOBAL_SETTINGS_PATH",
                        tmp_path_factory.mktemp("agy-home") / "settings.json")
