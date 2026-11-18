"""Google カレンダーの CLI(ブリッジ同梱)。トークンはプロファイル(<設定ディレクトリ>/google/NAME.json)の中。

使い方(`--profile` は必ずサブコマンドより前):
  <prefix>/.venv/bin/python -I <prefix>/tools/google_calendar.py --profile NAME <command> [options]

読み取り系(許可リストに入る):
  list    [--start ISO] [--end ISO] [--calendar-id ID]        期間の予定(既定: 今日から 7 日)
  search  --query TEXT [--start ISO] [--end ISO] [--calendar-id ID]
          予定のタイトル・説明・場所・参加者を、文字列で検索する(既定: 30 日前から 365 日後)
書き込み系(プロファイルの write_mode が confirm なら毎回承認、allow なら許可リストに入る):
  create  --summary S --start ISO --end ISO [--description D] [--location L] [--reminders "3,1"]
          [--meet] [--transcribe] [--attendees a@x,b@y] [--send-updates none|all|externalOnly]
  update  --event-id ID [上と同じオプション]   指定した項目だけ変える。--attendees は参加者を全部置き換える
  delete  --event-id ID [--send-updates ...]

ゲストへの通知メール(--send-updates)の既定は none(送らない)。送るときは all などを明示する。
時刻は ISO 8601。タイムゾーンが無ければ、プロファイルの timezone(既定 Asia/Tokyo)とみなす。
実行は <設定ディレクトリ>/google/actions.log に記録される。
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # -I では、スクリプトのディレクトリが入らない
import google_common as gc  # noqa: E402

MEET_API_BASE = "https://meet.googleapis.com/v2"      # v2beta ではなく v2(v2beta は 404、実機検証済み)
SEND_UPDATES = ("none", "all", "externalOnly")


def service_for(profile):
    from googleapiclient.discovery import build
    creds = gc.credentials(profile, "calendar")
    return build("calendar", "v3", credentials=creds, cache_discovery=False), creds


def zone(profile):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(profile["timezone"])
    except Exception:
        sys.exit(f"error: タイムゾーン {profile['timezone']!r} が不正です（プロファイルの timezone）")


def parse_time(profile, text):
    """ISO 8601 の文字列を、タイムゾーン付きの文字列にする(無ければプロファイルの timezone)。"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(profile))
    return dt.isoformat()


def day_start(profile, offset_days=0):
    now = datetime.now(zone(profile))
    d = (now + timedelta(days=offset_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.isoformat()


def parse_reminders(text):
    try:
        minutes = [int(m.strip()) for m in text.split(",")]
    except ValueError:
        sys.exit("error: --reminders は、分をカンマで区切った整数にしてください（例: 3,1）")
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in minutes]}


def emails_of(text):
    return [e.strip() for e in text.split(",") if e.strip()]


# --- Meet(文字起こし) ----------------------------------------------------------

def extract_meeting_code(event):
    conf_id = event.get("conferenceData", {}).get("conferenceId")
    if conf_id:
        return conf_id
    link = event.get("hangoutLink", "")
    return link.rstrip("/").split("/")[-1] if link else None


def set_meet_artifacts(session, space_name):
    """自動文字起こしと Gemini スマートノートを有効にする。スマートノートで失敗(ライセンス無し等)したら、
    文字起こしだけで再試行する。updateMask は省略(指定したフィールドだけ更新される、実機検証済み)。"""
    both = {"config": {"artifactConfig": {"transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
                                          "smartNotesConfig": {"autoSmartNotesGeneration": "ON"}}}}
    resp = session.patch(f"{MEET_API_BASE}/{space_name}", json=both)
    if resp.status_code == 200:
        return resp.json()
    print(f"Warning: 文字起こし+スマートノートの有効化に失敗 ({resp.status_code}) / 文字起こしだけで再試行します",
          file=sys.stderr)
    only = {"config": {"artifactConfig": {"transcriptionConfig": {"autoTranscriptionGeneration": "ON"}}}}
    resp2 = session.patch(f"{MEET_API_BASE}/{space_name}", json=only)
    if resp2.status_code != 200:
        print(f"Error: 文字起こしの有効化に失敗 ({resp2.status_code})", file=sys.stderr)
        return None
    return resp2.json()


def apply_meet_transcription(creds, event):
    """(更新前の space 設定, 更新後の space 設定)。Meet が無い・API エラーのときは (None, None)。"""
    from google.auth.transport.requests import AuthorizedSession
    code = extract_meeting_code(event)
    if not code:
        print("Warning: --transcribe が指定されましたが、予定に Google Meet のリンクがありません。スキップします。",
              file=sys.stderr)
        return None, None
    session = AuthorizedSession(creds)
    resp = session.get(f"{MEET_API_BASE}/spaces/{code}")
    if resp.status_code != 200:
        print(f"Warning: Meet space の取得に失敗 ({resp.status_code})", file=sys.stderr)
        return None, None
    before = resp.json()
    return before, set_meet_artifacts(session, before.get("name"))


def meet_flags(space):
    if not space:
        return None, None
    cfg = space.get("config", {}).get("artifactConfig", {})
    return (cfg.get("transcriptionConfig", {}).get("autoTranscriptionGeneration", "OFF"),
            cfg.get("smartNotesConfig", {}).get("autoSmartNotesGeneration", "OFF"))


# --- 表示 -----------------------------------------------------------------------

def print_events(events):
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        print(f"- [{start} ~ {end}] {event.get('summary', '(No Title)')} (ID: {event.get('id')})")
        if "description" in event:
            print(f"  Description: {event['description']}")
        if "location" in event:
            print(f"  Location: {event['location']}")
        if "hangoutLink" in event:
            print(f"  Meet: {event['hangoutLink']}")
        if "attendees" in event:
            print("  Attendees: " + ", ".join(f"{a.get('email')}({a.get('responseStatus', 'needsAction')})"
                                              for a in event["attendees"]))


def event_summary(event):
    """記録用。タイトルと日時、参加者のメールアドレス(本文・説明は入れない)。"""
    out = {"event_id": event.get("id"), "summary": event.get("summary"),
           "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date")),
           "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date"))}
    if event.get("attendees"):
        out["attendees"] = [a.get("email") for a in event["attendees"]]
    return out


# --- コマンド ---------------------------------------------------------------------

def cmd_list(profile, args):
    service, _ = service_for(profile)
    start = parse_time(profile, args.start) or day_start(profile)
    end = parse_time(profile, args.end) or day_start(profile, 7)
    print(f"Listing events from {start} to {end}...")
    items = service.events().list(calendarId=args.calendar_id, timeMin=start, timeMax=end,
                                  singleEvents=True, orderBy="startTime").execute().get("items", [])
    if not items:
        print("No upcoming events found.")
    print_events(items)
    return {"start": start, "end": end, "count": len(items)}


def cmd_search(profile, args):
    service, _ = service_for(profile)
    start = parse_time(profile, args.start) or day_start(profile, -30)
    end = parse_time(profile, args.end) or day_start(profile, 365)
    print(f"Searching events for {args.query!r} from {start} to {end}...")
    items = service.events().list(calendarId=args.calendar_id, q=args.query, timeMin=start, timeMax=end,
                                  singleEvents=True, orderBy="startTime", maxResults=250).execute().get("items", [])
    if not items:
        print("No events found.")
    print_events(items)
    return {"query": args.query, "start": start, "end": end, "count": len(items)}


def build_body(profile, args, body):
    if args.summary:
        body["summary"] = args.summary
    tz = profile["timezone"]
    if args.start:
        body["start"] = {"dateTime": parse_time(profile, args.start), "timeZone": tz}
    if args.end:
        body["end"] = {"dateTime": parse_time(profile, args.end), "timeZone": tz}
    if args.description is not None:
        body["description"] = args.description
    if args.location is not None:
        body["location"] = args.location
    if args.reminders is not None:
        body["reminders"] = {"useDefault": True} if args.reminders == "" else parse_reminders(args.reminders)
    if args.attendees is not None:
        body["attendees"] = [{"email": e} for e in emails_of(args.attendees)]
    return body


def cmd_create(profile, args):
    service, creds = service_for(profile)
    body = build_body(profile, args, {})
    want_meet = args.meet or args.transcribe
    if want_meet:
        body["conferenceData"] = {"createRequest": {"requestId": f"meet-{uuid.uuid4().hex}",
                                                    "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    event = service.events().insert(calendarId=args.calendar_id, body=body,
                                    conferenceDataVersion=1 if want_meet else 0,
                                    sendUpdates=args.send_updates).execute()
    after = None
    if args.transcribe:
        _, after = apply_meet_transcription(creds, event)
    print("=== EVENT CREATED ===")
    print(f"ID: {event.get('id')}")
    print(f"Title: {event.get('summary')}")
    print(f"Start: {event['start'].get('dateTime')}")
    print(f"End: {event['end'].get('dateTime')}")
    for label, key in (("Description", "description"), ("Location", "location"), ("Google Meet", "hangoutLink")):
        if event.get(key):
            print(f"{label}: {event[key]}")
    if event.get("attendees"):
        print("Attendees: " + ", ".join(a.get("email") for a in event["attendees"]))
        print(f"Notification emails to guests: {args.send_updates}")
    if event.get("reminders") and not event["reminders"].get("useDefault"):
        print("Reminders: " + ", ".join(f"{o.get('minutes')}m before ({o.get('method')})"
                                        for o in event["reminders"].get("overrides", [])))
    if args.transcribe:
        if after:
            tr, sn = meet_flags(after)
            print(f"Meet Auto-Transcription: {tr}\nMeet Smart Notes: {sn}")
        else:
            print("Meet Auto-Transcription/Smart Notes: 設定失敗（上記 Warning/Error 参照）")
    print("=====================")
    return {**event_summary(event), "send_updates": args.send_updates, "meet": want_meet}


def cmd_update(profile, args):
    service, creds = service_for(profile)
    try:
        old = service.events().get(calendarId=args.calendar_id, eventId=args.event_id).execute()
    except Exception as e:
        sys.exit(f"error: 予定 {args.event_id} を取得できません: {e.__class__.__name__}")
    body = build_body(profile, args, old.copy())
    want_meet = args.meet or args.transcribe
    version = 0
    if want_meet:
        if not body.get("conferenceData") and not body.get("hangoutLink"):
            body["conferenceData"] = {"createRequest": {"requestId": f"meet-{uuid.uuid4().hex}",
                                                        "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        version = 1
    new = service.events().update(calendarId=args.calendar_id, eventId=args.event_id, body=body,
                                  conferenceDataVersion=version, sendUpdates=args.send_updates).execute()
    before = after = None
    if args.transcribe:
        before, after = apply_meet_transcription(creds, new)

    def f(e, k):
        return e.get(k, "")
    changes = []
    for label, a, b in (("Title", f(old, "summary"), f(new, "summary")),
                        ("Start", old["start"].get("dateTime", old["start"].get("date")),
                         new["start"].get("dateTime", new["start"].get("date"))),
                        ("End", old["end"].get("dateTime", old["end"].get("date")),
                         new["end"].get("dateTime", new["end"].get("date"))),
                        ("Description", f(old, "description"), f(new, "description")),
                        ("Location", f(old, "location"), f(new, "location")),
                        ("Google Meet", f(old, "hangoutLink"), f(new, "hangoutLink")),
                        ("Attendees", [x.get("email") for x in old.get("attendees", [])],
                         [x.get("email") for x in new.get("attendees", [])])):
        if a != b:
            changes.append(f"{label}: {a!r} -> {b!r}")
    if json.dumps(old.get("reminders", {}), sort_keys=True) != json.dumps(new.get("reminders", {}), sort_keys=True):
        changes.append(f"Reminders: {old.get('reminders', {})} -> {new.get('reminders', {})}")
    if args.transcribe:
        if after:
            b1, b2 = meet_flags(before)
            a1, a2 = meet_flags(after)
            if b1 != a1:
                changes.append(f"Meet Auto-Transcription: {b1!r} -> {a1!r}")
            if b2 != a2:
                changes.append(f"Meet Smart Notes: {b2!r} -> {a2!r}")
        else:
            changes.append("Meet Auto-Transcription/Smart Notes: 設定失敗（上記 Warning/Error 参照）")
    print("=== EVENT UPDATED (DETAILS) ===")
    print(f"Event ID: {args.event_id}")
    if not changes:
        print("No changes detected (event was re-saved with identical fields).")
    for c in changes:
        print(f"- Changed {c}")
    print("===============================")
    return {**event_summary(new), "send_updates": args.send_updates, "changed": [c.split(":")[0] for c in changes]}


def cmd_delete(profile, args):
    service, _ = service_for(profile)
    try:
        event = service.events().get(calendarId=args.calendar_id, eventId=args.event_id).execute()
    except Exception as e:
        sys.exit(f"error: 予定 {args.event_id} を取得できません: {e.__class__.__name__}")
    service.events().delete(calendarId=args.calendar_id, eventId=args.event_id,
                            sendUpdates=args.send_updates).execute()
    summary = event_summary(event)
    print("=== EVENT DELETED ===")
    print(f"ID: {args.event_id}\nTitle: {summary['summary']}\nStart: {summary['start']}\nEnd: {summary['end']}")
    print("=====================")
    return {**summary, "send_updates": args.send_updates}


COMMANDS = {"list": cmd_list, "search": cmd_search, "create": cmd_create, "update": cmd_update,
            "delete": cmd_delete}


def build_parser():
    p = argparse.ArgumentParser(description="Google Calendar CLI（ブリッジ同梱）")
    p.add_argument("--profile", required=True, help="プロファイル名（<設定ディレクトリ>/google/NAME.json）")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--calendar-id", default="primary", help="カレンダー ID（既定: primary）")

    def event_opts(sp, create):
        sp.add_argument("--summary", required=create, help="タイトル" if create else "新しいタイトル")
        sp.add_argument("--start", required=create, help="開始（ISO 8601）")
        sp.add_argument("--end", required=create, help="終了（ISO 8601）")
        sp.add_argument("--description")
        sp.add_argument("--location")
        sp.add_argument("--reminders", help='ポップアップ通知の分前（カンマ区切り。例 "3,1"。update で "" は既定に戻す）')
        sp.add_argument("--meet", action="store_true", help="Google Meet を付ける")
        sp.add_argument("--transcribe", action="store_true", help="Meet の自動文字起こしとスマートノートを有効にする（--meet を含む）")
        sp.add_argument("--attendees", help="ゲストのメールアドレス（カンマ区切り）。update では、参加者を全部置き換える")
        sp.add_argument("--send-updates", choices=SEND_UPDATES, default="none",
                        help="ゲストへの通知メール（既定: none = 送らない）")

    s = sub.add_parser("list"); common(s)
    s.add_argument("--start"); s.add_argument("--end")
    s = sub.add_parser("search"); common(s)
    s.add_argument("--query", required=True); s.add_argument("--start"); s.add_argument("--end")
    s = sub.add_parser("create"); common(s); event_opts(s, True)
    s = sub.add_parser("update"); common(s); s.add_argument("--event-id", required=True); event_opts(s, False)
    s = sub.add_parser("delete"); common(s); s.add_argument("--event-id", required=True)
    s.add_argument("--send-updates", choices=SEND_UPDATES, default="none")
    return p


TOOL = "calendar"


def main(argv=None):
    args = build_parser().parse_args(argv)
    from googleapiclient.errors import HttpError
    from google.auth.exceptions import RefreshError
    try:
        profile = gc.load_profile(args.profile)
    except gc.ProfileError as e:
        sys.exit(f"error: {e}")
    gc.require_command(profile, TOOL, args.command)
    try:
        detail = COMMANDS[args.command](profile, args)
    except SystemExit as e:          # 条件に合わない、取得できない、など。記録に残して、そのまま終わる
        if e.code not in (0, None):
            gc.log_action(profile, TOOL, args.command, False, {"error": str(e.code)[:200]})
        raise
    except RefreshError:
        gc.log_action(profile, TOOL, args.command, False, {"error": "RefreshError"})
        sys.exit("error: アクセストークンを取得できません。トークンが失効している、または同意時のスコープが足りない可能性があります")
    except HttpError as e:
        gc.log_action(profile, TOOL, args.command, False, {"error": f"HttpError {getattr(e.resp, 'status', '?')}"})
        sys.exit(gc.http_error_message(e))
    gc.log_action(profile, TOOL, args.command, True, detail)


if __name__ == "__main__":
    main()
