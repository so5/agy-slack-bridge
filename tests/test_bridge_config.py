import pytest
import yaml

import bridge_config as bc

SAMPLE = """\
# header comment
channels:
  C1AAA:
    project: "p1"
    display_name: "one"
    icon_emoji: ":a:"
  # comment before C2
  C2BBB:
    project: "p2"
    model: "m"

# Optional usage statistics
#telemetry:
#  permission_stats: false
"""


def test_replace_existing_entry_keeps_everything_else():
    out = bc.upsert_channel(SAMPLE, "C1AAA", {"project": "p1", "display_name": "ONE", "effort": "high"})
    assert "# header comment" in out and "# comment before C2" in out and "#telemetry:" in out
    ch = bc.read_channels(out)
    assert ch["C1AAA"] == {"project": "p1", "display_name": "ONE", "effort": "high"}
    assert ch["C2BBB"] == {"project": "p2", "model": "m"}


def test_append_new_entry_before_trailing_top_level_comments():
    out = bc.upsert_channel(SAMPLE, "C3CCC", {"project": "p3", "name": "日本語"})
    assert bc.read_channels(out)["C3CCC"] == {"project": "p3", "name": "日本語"}
    assert out.index("C3CCC") < out.index("# Optional usage statistics")
    assert set(bc.read_channels(out)) == {"C1AAA", "C2BBB", "C3CCC"}


def test_empty_flow_channels_becomes_block():
    out = bc.upsert_channel("# c\nchannels: {}\n# tail\n", "C1", {"project": "p"})
    assert "channels:\n  C1:" in out and "# tail" in out
    assert bc.read_channels(out) == {"C1": {"project": "p"}}


def test_missing_channels_key_is_added():
    out = bc.upsert_channel("telemetry:\n  permission_stats: true\n", "C1", {"project": "p"})
    assert yaml.safe_load(out)["telemetry"] == {"permission_stats": True}
    assert bc.read_channels(out) == {"C1": {"project": "p"}}


def test_unknown_keys_and_nested_values_survive():
    entry = {"project": "p", "telemetry": {"permission_stats": True}, "future_key": [1, 2]}
    out = bc.upsert_channel("channels: {}\n", "C1", entry)
    assert bc.read_channels(out)["C1"] == entry


def test_flow_style_channels_with_content_refused():
    with pytest.raises(bc.ConfigEditError):
        bc.upsert_channel("channels: {C1: {project: p}}\n", "C2", {"project": "q"})


def test_env_value_replace_uncomment_and_append():
    text = "SLACK_BOT_TOKEN=xoxb-...\n# AGY_BIN=agy\n"
    text = bc.set_env_value(text, "SLACK_BOT_TOKEN", "xoxb-real")
    text = bc.set_env_value(text, "AGY_BIN", "/x/agy")
    text = bc.set_env_value(text, "NEW", "1")
    assert text == "SLACK_BOT_TOKEN=xoxb-real\n# AGY_BIN=agy\nAGY_BIN=/x/agy\nNEW=1\n"
    assert bc.get_env_value(text, "SLACK_BOT_TOKEN") == "xoxb-real"
    with pytest.raises(bc.ConfigEditError):
        bc.set_env_value(text, "X", "a\nb")
