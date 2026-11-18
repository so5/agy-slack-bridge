import base64
import json
import os
import sys
import types

import pytest

import google_calendar as cal
import google_common as gc
import google_mail as mail
import google_policy as gp

TOKEN = {"client_id": "cid", "client_secret": "sec", "refresh_token": "rt",
         "scopes": ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.modify"]}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def put(cfg, name, data):
    gp.write_profile(cfg / "google" / f"{name}.json", data)


def prof(**kw):
    p = gc.parse_profile({"token": TOKEN, **kw})
    p["name"] = "p"
    return p


# --- profile -------------------------------------------------------------------

def test_profile_defaults_and_validation():
    p = prof()
    assert p["commands"] == set(gc.ALL_COMMANDS) and p["write_mode"] == "confirm" and p["mail_filter"] == ""
    for bad in ({"bogus": 1}, {"commands": ["mail.send"]}, {"write_mode": "yes"}, {"mail_filter": "x" * 2000},
                {"token_from": "other"}):
        with pytest.raises(gc.ProfileError):
            gc.parse_profile({"token": TOKEN, **bad})
    with pytest.raises(gc.ProfileError):
        gc.parse_profile({"token": {"client_id": "a"}})
    with pytest.raises(gc.ProfileError):
        gc.parse_profile({})


def test_token_from_shares_the_token_and_keeps_its_own_filter(cfg):
    put(cfg, "personal", {"token": TOKEN})
    put(cfg, "kyushu", {"token_from": "personal", "mail_filter": "(to:a@x)"})
    p = gc.load_profile("kyushu")
    assert p["token"] == TOKEN and p["mail_filter"] == "(to:a@x)"
    put(cfg, "chain", {"token_from": "kyushu"})
    with pytest.raises(gc.ProfileError):
        gc.load_profile("chain")                 # no chains
    with pytest.raises(gc.ProfileError):
        gc.load_profile("../etc")


def test_profile_writable_by_others_is_refused_and_file_is_0600(cfg):
    put(cfg, "p", {"token": TOKEN})
    path = cfg / "google" / "p.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    os.chmod(path, 0o666)
    with pytest.raises(gc.ProfileError):
        gc.load_profile("p")


def test_scopes_requested_are_only_the_tools_own(cfg):
    p = prof()
    assert gc.scopes_for(p, "calendar") == ["https://www.googleapis.com/auth/calendar"]
    assert gc.scopes_for(p, "mail") == ["https://www.googleapis.com/auth/gmail.modify"]
    p2 = gc.parse_profile({"token": {k: v for k, v in TOKEN.items() if k != "scopes"}})
    assert gc.scopes_for(p2, "mail") == gc.DEFAULT_SCOPES["mail"]


def test_disabled_command_exits_and_action_log_is_0600_and_holds_no_body(cfg):
    p = prof(commands=["mail.search"])
    with pytest.raises(SystemExit):
        gc.require_command(p, "mail", "draft-new")
    gc.require_command(p, "mail", "search")
    gc.log_action(p, "mail", "draft-new", True, {"to": "a@x", "subject": "hi"})
    line = (cfg / "google" / "actions.log").read_text().splitlines()[0]
    e = json.loads(line)
    assert e["profile"] == "p" and e["command"] == "draft-new" and e["ok"] and e["detail"]["to"] == "a@x"
    assert oct((cfg / "google" / "actions.log").stat().st_mode & 0o777) == "0o600"


# --- policy --------------------------------------------------------------------

def test_policy_writes_only_deviations_and_round_trips(cfg):
    base = {"token": TOKEN}
    assert gp.apply_policy(base, gp.default_policy()) == base
    pol = gp.default_policy()
    pol.update(commands=["calendar.list", "mail.read"], write_mode="allow", mail_filter="(to:a@x)")
    data = gp.apply_policy(base, pol)
    assert data["commands"] == ["calendar.list", "mail.read"] and data["write_mode"] == "allow"
    assert gp.policy_from_data(data) == {**pol, "timezone": "Asia/Tokyo"}
    assert any("恒久許可" in l for l in gp.loosening(gp.default_policy(), pol))
    assert gp.loosening(pol, gp.default_policy()) != []           # filter removed = wider
    assert gp.diff(gp.default_policy(), pol)


def test_read_token_file_takes_only_the_needed_keys(tmp_path):
    f = tmp_path / "token.json"
    f.write_text(json.dumps({**TOKEN, "token": "ACCESS", "expiry": "x", "token_uri": "https://evil.example"}))
    t = gp.read_token_file(f)
    assert t == TOKEN and "token" not in t
    f.write_text(json.dumps({"client_id": "a"}))
    with pytest.raises(ValueError):
        gp.read_token_file(f)


# --- mail ----------------------------------------------------------------------

class Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGmail:
    """messages: {id: {"mid": "Message-ID", "headers": {...}, "payload": {...}}}"""

    def __init__(self, messages):
        self.messages_ = messages
        self.queries = []
        self.drafts_made = []

    def users(self):
        return self

    def messages(self):
        return self

    def drafts(self):
        return self

    def attachments(self):
        return self

    def list(self, userId, q, maxResults=10):
        self.queries.append(q)

        def run():
            hits = []
            for i, m in self.messages_.items():
                if "rfc822msgid:" in q and f"rfc822msgid:{m['mid']}" not in q:
                    continue
                if m.get("in_filter", True) or "to:" not in q:
                    hits.append({"id": i, "threadId": "t" + i})
            return {"messages": hits}
        return Call(run)

    def get(self, userId=None, id=None, format=None, metadataHeaders=None, messageId=None):
        m = self.messages_[id] if id in self.messages_ else None

        def run():
            if m is None:
                raise KeyError(id)
            hdr = [{"name": "Message-ID", "value": f"<{m['mid']}>"}, {"name": "Subject", "value": "S"},
                   {"name": "From", "value": "f@x"}, {"name": "Date", "value": "d"}]
            return {"id": id, "threadId": "t" + id, "snippet": "snip",
                    "payload": m.get("payload") or {"headers": hdr, "mimeType": "text/plain",
                                                    "body": {"data": base64.urlsafe_b64encode(b"hello").decode()}}}
        return Call(run)

    def create(self, userId, body):
        self.drafts_made.append(body)
        return Call(lambda: {"id": "d1", "message": {"threadId": body["message"].get("threadId")}})


@pytest.fixture
def gmail(monkeypatch):
    svc = FakeGmail({"m1": {"mid": "a@mid", "in_filter": True}, "m2": {"mid": "b@mid", "in_filter": False}})
    monkeypatch.setattr(mail, "service_for", lambda profile: svc)
    return svc


def ns(**kw):
    return types.SimpleNamespace(**kw)


def test_search_adds_the_profile_filter_and_defaults_to_unread_inbox(gmail, capsys):
    out = mail.cmd_search(prof(mail_filter="(to:a@x)"), ns(query="", max_results=5))
    assert gmail.queries == ["((to:a@x)) (label:UNREAD label:INBOX)"] and out["count"] >= 1
    mail.cmd_search(prof(), ns(query="from:z", max_results=5))
    assert gmail.queries[-1] == "from:z"
    mail.cmd_search(prof(mail_filter="(to:a@x)"), ns(query="invoice", max_results=5))
    assert gmail.queries[-1] == "((to:a@x)) (invoice)"


def test_read_refuses_a_message_outside_the_filter_but_allows_one_inside(gmail, capsys):
    p = prof(mail_filter="(to:a@x)")
    mail.cmd_read(p, ns(msg_id="m1", html=False))
    assert "hello" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        mail.cmd_read(p, ns(msg_id="m2", html=False))
    assert "条件に合いません" in str(e.value)
    with pytest.raises(SystemExit):
        mail.cmd_read(p, ns(msg_id="nope", html=False))
    mail.cmd_read(prof(), ns(msg_id="m2", html=False))      # no filter: anything the account has


def test_message_without_a_message_id_is_not_passed_by_the_filter(gmail):
    gmail.messages_["m3"] = {"mid": "", "in_filter": True}
    with pytest.raises(SystemExit):
        mail.ensure_matches_filter(gmail, prof(mail_filter="(to:a@x)"), "m3")


def test_attachment_goes_to_scratch_mail_in_the_project_with_a_safe_name(gmail, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(gc, "INSTALL_ROOT", str(tmp_path / "install"))
    data = base64.urlsafe_b64encode(b"%PDF-notreally").decode()
    gmail.messages_["m1"]["payload"] = {"headers": [{"name": "Message-ID", "value": "<a@mid>"}], "parts": [
        {"filename": "../../evil name.txt", "mimeType": "text/plain", "body": {"data": data, "size": 14}}]}
    out = mail.cmd_attachment(prof(), ns(msg_id="m1", index=0, text=True))
    saved = list((proj / "scratch" / "mail" / "m1").iterdir())
    assert len(saved) == 1 and saved[0].name == "0_evil_name.txt" and saved[0].read_bytes() == b"%PDF-notreally"
    assert out["bytes"] == 14
    with pytest.raises(SystemExit):
        mail.cmd_attachment(prof(), ns(msg_id="m1", index=3, text=False))


def test_attachment_refuses_to_run_inside_the_install_dir(gmail, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gc, "INSTALL_ROOT", str(tmp_path))
    gmail.messages_["m1"]["payload"] = {"headers": [{"name": "Message-ID", "value": "<a@mid>"}], "parts": [
        {"filename": "a.txt", "mimeType": "text/plain", "body": {"data": "eA==", "size": 1}}]}
    with pytest.raises(SystemExit):
        mail.cmd_attachment(prof(), ns(msg_id="m1", index=0, text=False))


def test_drafts_are_only_drafts_and_never_sent(gmail, capsys):
    mail.cmd_draft_new(prof(), ns(to="a@x", subject="hi", body="b", from_addr=None))
    gmail.messages_["m1"]["payload"] = {"headers": [
        {"name": "Message-ID", "value": "<a@mid>"}, {"name": "From", "value": "f@x"}, {"name": "Subject", "value": "Q"}]}
    out = mail.cmd_draft_reply(prof(), ns(msg_id="m1", body="thanks", from_addr=None))
    assert len(gmail.drafts_made) == 2 and out["subject"] == "Re: Q" and out["to"] == "f@x"
    assert gmail.drafts_made[1]["message"]["threadId"] == "tm1"
    assert not hasattr(gmail, "send") and "mail_client" not in sys.modules


def test_mail_has_no_send_or_delete_commands():
    assert set(mail.COMMANDS) == {"search", "read", "attachment", "draft-reply", "draft-new"}


def test_html_only_body_is_converted():
    assert mail.html_to_text("<p>a<br>b</p><script>x()</script>") == "a\nb"


# --- calendar --------------------------------------------------------------------

class FakeCalendar:
    def __init__(self):
        self.calls = []
        self.event = {"id": "e1", "summary": "old", "start": {"dateTime": "2026-01-01T10:00:00+09:00"},
                      "end": {"dateTime": "2026-01-01T11:00:00+09:00"},
                      "attendees": [{"email": "g@x"}]}

    def events(self):
        return self

    def _rec(self, name, kw, result):
        self.calls.append((name, kw))
        return Call(lambda: result)

    def list(self, **kw):
        return self._rec("list", kw, {"items": [self.event]})

    def get(self, **kw):
        return self._rec("get", kw, dict(self.event))

    def insert(self, **kw):
        return self._rec("insert", kw, {**self.event, "summary": kw["body"]["summary"]})

    def update(self, **kw):
        return self._rec("update", kw, {**kw["body"]})

    def delete(self, **kw):
        return self._rec("delete", kw, None)


@pytest.fixture
def fcal(monkeypatch):
    svc = FakeCalendar()
    monkeypatch.setattr(cal, "service_for", lambda profile: (svc, None))
    return svc


def test_calendar_search_passes_q_and_a_default_range(fcal, capsys):
    out = cal.cmd_search(prof(), ns(query="打合せ", start=None, end=None, calendar_id="primary"))
    kw = fcal.calls[0][1]
    assert kw["q"] == "打合せ" and kw["singleEvents"] and out["count"] == 1
    assert kw["timeMin"] < kw["timeMax"] and kw["timeMin"].endswith("+09:00")


def test_times_without_a_zone_get_the_profile_timezone():
    assert cal.parse_time(prof(), "2026-03-01T09:00:00") == "2026-03-01T09:00:00+09:00"
    assert cal.parse_time(prof(timezone="UTC"), "2026-03-01T09:00:00") == "2026-03-01T09:00:00+00:00"
    assert cal.parse_time(prof(), "2026-03-01T09:00:00+01:00").endswith("+01:00")


def _create_args(**kw):
    base = dict(summary="S", start="2026-02-01T10:00", end="2026-02-01T11:00", description=None, location=None,
                reminders=None, meet=False, transcribe=False, attendees=None, send_updates="none",
                calendar_id="primary")
    return ns(**{**base, **kw})


def test_create_does_not_notify_guests_unless_asked(fcal, capsys):
    cal.cmd_create(prof(), _create_args(attendees="g@x"))
    assert fcal.calls[0][1]["sendUpdates"] == "none"
    out = cal.cmd_create(prof(), _create_args(attendees="g@x", send_updates="all"))
    assert fcal.calls[1][1]["sendUpdates"] == "all" and out["send_updates"] == "all"
    assert cal.build_parser().parse_args(["--profile", "p", "create", "--summary", "s", "--start", "a", "--end", "b"]
                                         ).send_updates == "none"


def test_update_only_replaces_attendees_when_given(fcal, capsys):
    base = dict(event_id="e1", summary="new", start=None, end=None, description=None, location=None, reminders=None,
                meet=False, transcribe=False, attendees=None, send_updates="none", calendar_id="primary")
    cal.cmd_update(prof(), ns(**base))
    assert fcal.calls[-1][1]["body"]["attendees"] == [{"email": "g@x"}] and fcal.calls[-1][1]["body"]["summary"] == "new"
    cal.cmd_update(prof(), ns(**{**base, "attendees": "a@x,b@x"}))
    assert [a["email"] for a in fcal.calls[-1][1]["body"]["attendees"]] == ["a@x", "b@x"]


def test_main_logs_every_run_and_checks_the_profile(cfg, fcal, monkeypatch, capsys):
    fake_errors = types.ModuleType("googleapiclient.errors")
    fake_errors.HttpError = type("HttpError", (Exception,), {})
    fake_auth = types.ModuleType("google.auth.exceptions")
    fake_auth.RefreshError = type("RefreshError", (Exception,), {})
    for name, mod in (("googleapiclient", types.ModuleType("googleapiclient")), ("googleapiclient.errors", fake_errors),
                      ("google", types.ModuleType("google")), ("google.auth", types.ModuleType("google.auth")),
                      ("google.auth.exceptions", fake_auth)):
        monkeypatch.setitem(sys.modules, name, mod)
    put(cfg, "p", {"token": TOKEN, "commands": ["calendar.list", "calendar.create"]})
    cal.main(["--profile", "p", "list"])
    cal.main(["--profile", "p", "create", "--summary", "S", "--start", "2026-02-01T10:00", "--end", "2026-02-01T11:00"])
    with pytest.raises(SystemExit):
        cal.main(["--profile", "p", "search", "--query", "x"])        # not enabled
    with pytest.raises(SystemExit):
        cal.main(["--profile", "missing", "list"])
    log = [json.loads(l) for l in (cfg / "google" / "actions.log").read_text().splitlines()]
    assert [(e["command"], e["ok"]) for e in log] == [("list", True), ("create", True)]
    assert log[1]["detail"]["summary"] == "S" and log[1]["detail"]["send_updates"] == "none"
