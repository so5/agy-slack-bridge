"""Gmail の CLI(ブリッジ同梱)。トークンはプロファイル(<設定ディレクトリ>/google/NAME.json)の中。
送信と削除は、この CLI にはない(下書きを作るところまで。送るのは人)。

使い方(`--profile` は必ずサブコマンドより前):
  <prefix>/.venv/bin/python -I <prefix>/tools/google_mail.py --profile NAME <command> [options]

読み取り系(許可リストに入る):
  search      [--query Q] [--max-results N]     検索(既定の条件: 未読の受信トレイ)。ID・日付・差出人・件名・冒頭を出す
  read        --msg-id ID [--html]              1 通を読む(ヘッダ、添付の一覧、本文。HTML だけなら変換する)
  attachment  --msg-id ID --index N [--text]    添付を scratch/mail/<ID>/ に保存して、パスを出す。
                                                --text で PDF・テキストの本文も出す
書き込み系(プロファイルの write_mode が confirm なら毎回承認、allow なら許可リストに入る):
  draft-reply --msg-id ID --body TEXT [--from-addr ADDR]     返信の下書きを作る(送らない)
  draft-new   --to ADDR --subject S --body TEXT [--from-addr ADDR]   新規の下書きを作る(送らない)

プロファイルに mail_filter があると、search には条件が AND で足され、read / attachment / draft-reply は、
その条件に合うメールにしか使えない(絞り込みであって、権限の境界ではない)。
実行は <設定ディレクトリ>/google/actions.log に記録される(本文などの中身は記録しない)。
"""
import argparse
import base64
import html
import os
import re
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # -I では、スクリプトのディレクトリが入らない
import google_common as gc  # noqa: E402

DEFAULT_QUERY = "label:UNREAD label:INBOX"
MAX_ATTACH_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 30000


def service_for(profile):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=gc.credentials(profile, "mail"), cache_discovery=False)


def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def walk_parts(payload):
    stack = [payload]
    while stack:
        part = stack.pop(0)
        yield part
        stack = part.get("parts", []) + stack


def html_to_text(src):
    src = re.sub(r"(?is)<(script|style).*?</\1>", "", src)
    src = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", src)
    src = re.sub(r"(?s)<[^>]+>", "", src)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(src)).strip()


def body_text(payload, prefer_html=False):
    plain, htm = [], []
    for part in walk_parts(payload):
        if part.get("filename"):
            continue
        data = part.get("body", {}).get("data")
        if not data:
            continue
        text = _decode(data).decode("utf-8", errors="replace")
        if part.get("mimeType") == "text/plain":
            plain.append(text)
        elif part.get("mimeType") == "text/html":
            htm.append(text)
    if prefer_html and htm:
        return "\n".join(htm)
    if plain:
        return "\n".join(plain)
    return html_to_text("\n".join(htm)) if htm else ""


def attachments_of(payload):
    return [p for p in walk_parts(payload)
            if p.get("filename") and (p.get("body", {}).get("attachmentId") or p.get("body", {}).get("data"))]


def with_filter(profile, query):
    """検索条件に、プロファイルの mail_filter を AND で足す。"""
    flt = profile["mail_filter"]
    if not flt:
        return query
    return f"({flt}) ({query})" if query else f"({flt})"


def ensure_matches_filter(service, profile, msg_id):
    """mail_filter がある場合、そのメールが条件に合うかを確かめる(合わなければ、無いものとして扱う)。
    Message-ID で、条件つきの検索をして、同じ ID が返るかを見る。Message-ID が無ければ、通さない。"""
    if not profile["mail_filter"]:
        return
    try:
        meta = service.users().messages().get(userId="me", id=msg_id, format="metadata",
                                              metadataHeaders=["Message-ID"]).execute()
    except Exception:
        sys.exit("error: メールが見つかりません（ID が違う、または、このプロファイルの条件に合いません）")
    mid = get_header(meta.get("payload", {}).get("headers", []), "Message-ID").strip().strip("<>")
    found = []
    if mid:
        found = service.users().messages().list(
            userId="me", q=with_filter(profile, f"rfc822msgid:{mid}"), maxResults=5).execute().get("messages", [])
    if msg_id not in [m["id"] for m in found]:
        sys.exit("error: メールが見つかりません（ID が違う、または、このプロファイルの条件に合いません）")


# --- 読み取り ---------------------------------------------------------------------

def cmd_search(profile, args):
    service = service_for(profile)
    query = with_filter(profile, args.query or DEFAULT_QUERY)
    msgs = service.users().messages().list(userId="me", q=query, maxResults=args.max_results).execute().get("messages", [])
    print(f"query: {query!r} -> {len(msgs)} 件")
    for m in msgs:
        d = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                           metadataHeaders=["Subject", "From", "To", "Date"]).execute()
        h = d.get("payload", {}).get("headers", [])
        print(f"- ID: {m['id']}")
        print(f"  Thread ID: {m['threadId']}")
        print(f"  Date: {get_header(h, 'Date')}")
        print(f"  From: {get_header(h, 'From')}")
        print(f"  Subject: {get_header(h, 'Subject')}")
        print(f"  Snippet: {html.unescape(d.get('snippet', ''))[:120]}")
        print("-" * 40)
    return {"query": args.query or DEFAULT_QUERY, "count": len(msgs)}


def fetch_full(service, profile, msg_id):
    ensure_matches_filter(service, profile, msg_id)
    try:
        return service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    except Exception as e:
        sys.exit(f"error: メールを取得できません: {e.__class__.__name__}")


def cmd_read(profile, args):
    service = service_for(profile)
    msg = fetch_full(service, profile, args.msg_id)
    payload = msg.get("payload", {})
    h = payload.get("headers", [])
    print("=== EMAIL ===")
    for k in ("From", "To", "Cc", "Date", "Subject", "Message-ID"):
        v = get_header(h, k)
        if v:
            print(f"{k}: {v}")
    print(f"ID: {msg['id']}  Thread: {msg['threadId']}")
    atts = attachments_of(payload)
    if atts:
        print("--- Attachments ---")
        for i, p in enumerate(atts):
            print(f"[{i}] {p['filename']}  ({p.get('mimeType')}, {p.get('body', {}).get('size', 0)} bytes)")
    print("--- Body ---")
    print(body_text(payload, prefer_html=args.html))
    print("=============")
    return {"msg_id": args.msg_id}


def extract_text(path, mime):
    low = path.lower()
    if low.endswith(".pdf") or mime == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "(PDF の本文を取り出すための pypdf が入っていません。install.py --with-gdrive で入れてください)"
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    if mime.startswith("text/") or low.endswith((".txt", ".csv", ".md", ".json", ".ics", ".eml")):
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    return None


def cmd_attachment(profile, args):
    service = service_for(profile)
    msg = fetch_full(service, profile, args.msg_id)
    atts = attachments_of(msg.get("payload", {}))
    if not 0 <= args.index < len(atts):
        sys.exit(f"error: index {args.index} は範囲外（添付 {len(atts)} 件）。read で一覧を見てください")
    part = atts[args.index]
    body = part["body"]
    if body.get("size", 0) > MAX_ATTACH_BYTES:
        sys.exit(f"error: 添付が大きすぎます（{body['size']} bytes > {MAX_ATTACH_BYTES}）")
    if body.get("attachmentId"):
        data = service.users().messages().attachments().get(
            userId="me", messageId=args.msg_id, id=body["attachmentId"]).execute()["data"]
    else:
        data = body["data"]
    raw = _decode(data)
    out_dir = os.path.join(gc.project_dir(), "scratch", "mail", gc.safe_name(args.msg_id))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{args.index}_{gc.safe_name(part['filename'])}")
    with open(path, "wb") as f:
        f.write(raw)
    print(f"saved: {path} ({len(raw)} bytes, {part.get('mimeType')})")
    if args.text:
        try:
            text = extract_text(path, part.get("mimeType", ""))
        except Exception as e:      # 壊れた PDF など
            print(f"(テキスト抽出に失敗: {e.__class__.__name__})")
        else:
            if text is None:
                print("(この形式はテキスト抽出なし。保存したファイルを直接開く)")
            else:
                print("--- Text ---")
                print(text[:MAX_TEXT_CHARS] + ("\n…(省略)" if len(text) > MAX_TEXT_CHARS else ""))
    return {"msg_id": args.msg_id, "index": args.index, "file": part["filename"], "bytes": len(raw)}


# --- 書き込み(下書きだけ) ---------------------------------------------------------

def create_draft(service, message, thread_id=None):
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    body = {"message": {"raw": raw}}
    if thread_id:
        body["message"]["threadId"] = thread_id
    return service.users().drafts().create(userId="me", body=body).execute()


def cmd_draft_reply(profile, args):
    service = service_for(profile)
    orig = fetch_full(service, profile, args.msg_id)
    h = orig.get("payload", {}).get("headers", [])
    to = get_header(h, "Reply-To") or get_header(h, "From")
    subject = get_header(h, "Subject")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message = EmailMessage()
    message.set_content(args.body)
    message["To"], message["Subject"] = to, subject
    mid = get_header(h, "Message-ID")
    if mid:
        message["In-Reply-To"] = mid
        message["References"] = mid
    if args.from_addr:
        message["From"] = args.from_addr
    draft = create_draft(service, message, orig["threadId"])
    print("=== DRAFT REPLY CREATED ===")
    print(f"Draft ID: {draft.get('id')}\nThread ID: {orig['threadId']}")
    print(f"From: {args.from_addr or 'default'}\nTo: {to}\nSubject: {subject}")
    print("Draft saved successfully (NOT sent). Final check and sending must be done by the user.")
    print("===========================")
    return {"draft_id": draft.get("id"), "thread_id": orig["threadId"], "to": to, "subject": subject}


def cmd_draft_new(profile, args):
    service = service_for(profile)
    message = EmailMessage()
    message.set_content(args.body)
    if args.from_addr:
        message["From"] = args.from_addr
    message["To"], message["Subject"] = args.to, args.subject
    draft = create_draft(service, message)
    print("=== NEW DRAFT CREATED ===")
    print(f"Draft ID: {draft.get('id')}\nFrom: {args.from_addr or 'default'}\nTo: {args.to}\nSubject: {args.subject}")
    print("Draft saved successfully (NOT sent). Final check and sending must be done by the user.")
    print("=========================")
    return {"draft_id": draft.get("id"), "to": args.to, "subject": args.subject}


COMMANDS = {"search": cmd_search, "read": cmd_read, "attachment": cmd_attachment,
            "draft-reply": cmd_draft_reply, "draft-new": cmd_draft_new}


def build_parser():
    p = argparse.ArgumentParser(description="Gmail CLI（ブリッジ同梱。送信・削除はない）")
    p.add_argument("--profile", required=True, help="プロファイル名（<設定ディレクトリ>/google/NAME.json）")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search")
    s.add_argument("--query", default="", help=f"検索条件（既定: {DEFAULT_QUERY}）")
    s.add_argument("--max-results", type=int, default=10)
    s = sub.add_parser("read")
    s.add_argument("--msg-id", required=True)
    s.add_argument("--html", action="store_true", help="HTML 本文をそのまま出す")
    s = sub.add_parser("attachment")
    s.add_argument("--msg-id", required=True)
    s.add_argument("--index", type=int, required=True)
    s.add_argument("--text", action="store_true", help="PDF・テキストの本文も出す")
    s = sub.add_parser("draft-reply")
    s.add_argument("--msg-id", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--from-addr", help="差出人のアドレス（エイリアス）")
    s = sub.add_parser("draft-new")
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--from-addr", help="差出人のアドレス（エイリアス）")
    return p


TOOL = "mail"


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
