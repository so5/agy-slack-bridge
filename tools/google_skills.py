"""カレンダー・メールの CLI を、プロジェクトのエージェントに使わせるための、許可エントリとスキルの文面
(setup_agy_project.py が使う)。許可エントリと、スキルに書くコマンドは、同じ文字列から作る
(書き方が違うと許可に当たらず、毎回承認になる)。"""
import re
from pathlib import Path

import google_common as gc

TOOLS = {"calendar": "google_calendar.py", "mail": "google_mail.py"}
SKILL_NAMES = {"calendar": "google-calendar", "mail": "google-mail"}
SKILL_WHEN = {"google-calendar": "Google カレンダーの予定を探す・確認する・作る・変更する・削除するとき",
              "google-mail": "Gmail のメールを探す・読む・添付を取り出す・下書きを作るとき"}
REPORT_MARK = "<!-- google: 承認なしの書き込みは、全件を報告する -->"


def base_command(prefix: Path, tool: str, profile: str) -> str:
    # --profile はサブコマンドより前に固定する(末尾の自由引数にすると、エージェントが別のプロファイルを選べてしまう)。
    return f"{prefix}/.venv/bin/python -I {prefix}/tools/{TOOLS[tool]} --profile {profile}"


def allowed_subcommands(commands: "list[str] | set[str]", write_mode: str) -> "dict[str, list[str]]":
    """許可リストに入れるサブコマンド(ツールごと)。読み取り系は常に。書き込み系は write_mode が allow のときだけ。"""
    out = {"calendar": [], "mail": []}
    for tool, read, write in (("calendar", gc.CALENDAR_READ, gc.CALENDAR_WRITE), ("mail", gc.MAIL_READ, gc.MAIL_WRITE)):
        subs = list(read) + (list(write) if write_mode == "allow" else [])
        out[tool] = [s for s in subs if f"{tool}.{s}" in commands]
    return out


def allow_entries(prefix: Path, profile: str, commands, write_mode: str) -> list[str]:
    return [f"command({base_command(prefix, tool, profile)} {sub})"
            for tool, subs in allowed_subcommands(commands, write_mode).items() for sub in subs]


def all_possible_entries(prefix: Path, profile: str) -> list[str]:
    """このプロファイルのエントリになり得るものすべて(外すときに使う)。"""
    return allow_entries(prefix, profile, gc.ALL_COMMANDS, "allow")


def attached_profiles(allow: "list[str]", prefix: Path) -> list[str]:
    """許可リストにあるエントリから、このプロジェクトで使っている Google のプロファイル名(出てきた順)。"""
    found = []
    for e in allow:
        for tool in TOOLS:
            marker = f"{prefix}/tools/{TOOLS[tool]} --profile "
            if marker in e:
                name = e.split(marker, 1)[1].split()[0]
                if name not in found:
                    found.append(name)
    return found


# --- スキルの文面 -----------------------------------------------------------------------

_CALENDAR_CMDS = {
    "list": "予定の一覧（既定: 今日から 7 日）: `{b} list [--start <ISO8601>] [--end <ISO8601>] [--calendar-id <ID>]`",
    "search": "予定の検索（タイトル・説明・場所・参加者。既定: 30 日前から 365 日後）: "
              "`{b} search --query <文字列> [--start <ISO8601>] [--end <ISO8601>] [--calendar-id <ID>]`",
    "create": "予定を作る: `{b} create --summary <タイトル> --start <ISO8601> --end <ISO8601> [--description <文>] "
              "[--location <場所>] [--reminders <分,分>] [--meet] [--transcribe] [--attendees <メール,メール>] "
              "[--send-updates none|all|externalOnly]`",
    "update": "予定を変える（指定した項目だけ。`--attendees` は参加者を全部置き換える）: "
              "`{b} update --event-id <ID> [--summary ..] [--start ..] [--end ..] [--description ..] [--location ..] "
              "[--reminders ..] [--meet] [--transcribe] [--attendees ..] [--send-updates ..]`",
    "delete": "予定を消す: `{b} delete --event-id <ID> [--send-updates none|all|externalOnly]`",
}
_MAIL_CMDS = {
    "search": "検索（`--query` を省くと未読の受信トレイ）: `{b} search [--query <Gmail の検索条件>] [--max-results <件数>]`",
    "read": "1 通を読む（ヘッダ・添付の一覧・本文）: `{b} read --msg-id <ID> [--html]`",
    "attachment": "添付を取り出す（プロジェクトの `scratch/mail/<ID>/` に保存し、パスを出す。"
                  "`--text` で PDF・テキストの本文も出す）: `{b} attachment --msg-id <ID> --index <番号> [--text]`",
    "draft-reply": "返信の下書きを作る（送らない）: `{b} draft-reply --msg-id <ID> --body <本文> [--from-addr <差出人>]`",
    "draft-new": "新規の下書きを作る（送らない）: `{b} draft-new --to <宛先> --subject <件名> --body <本文> [--from-addr <差出人>]`",
}


def skill_text(tool: str, prefix: Path, profiles: "list[dict]", skill_mark: str) -> str:
    """profiles: [{"name", "commands", "write_mode", "mail_filter"}]。そのツールで有効なコマンドを持つものだけ載せる。"""
    table = _CALENDAR_CMDS if tool == "calendar" else _MAIL_CMDS
    read, write = (gc.CALENDAR_READ, gc.CALENDAR_WRITE) if tool == "calendar" else (gc.MAIL_READ, gc.MAIL_WRITE)
    title = "Google カレンダー" if tool == "calendar" else "Gmail"
    name = SKILL_NAMES[tool]
    any_allow = any(p["write_mode"] == "allow" and any(f"{tool}.{s}" in p["commands"] for s in write) for p in profiles)
    out = ["---", f"name: {name}", f"description: {SKILL_WHEN[name]}に使う。固定コマンドの一覧と、守ること。",
           "---", skill_mark] + ([REPORT_MARK] if any_allow else []) + ["",
           f"# {title}", "",
           "次の**固定コマンドだけ**を使う。",
           "- **コマンドは 1 回に 1 つ。** `;` `&&` `||` `|` `echo` などでつながない（複合コマンドは許可の形に合わず、",
           "  拒否されて承認が要る）。複数の情報が欲しいときは、コマンドを別々に実行する。",
           "- **承認が要るコマンド（書き込み系）と、要らないコマンド（読み取り系）を、同じターンで続けて発行しない。**",
           "  書き込み系は、1 つずつ実行して、承認を待ってから次へ進む。読み取り系は、その後で実行する。",
           "- 下の文字列を、そのまま実行する（`<...>` や `[...]` の部分だけ置き換える）。`-I` は必須。`--help` は不要（下に全部ある）。",
           "- プロジェクトのディレクトリで実行する（`cd` は不要）。"]
    if tool == "mail":
        out.append("- 送信と削除はできない（下書きまで。送るのはユーザー）。")
    else:
        out.append("- ゲストへの通知メール（`--send-updates`）は、既定では送らない（`none`）。送るときだけ明示する。"
                   "時刻に `+09:00` などが無ければ、プロファイルのタイムゾーン（既定 Asia/Tokyo）とみなす。")
    out.append("")
    for p in profiles:
        cmds = [s for s in list(read) + list(write) if f"{tool}.{s}" in p["commands"]]
        if not cmds:
            continue
        b = base_command(prefix, tool, p["name"])
        head = f"## アカウント: {p['name']}" if len(profiles) > 1 else f"## アカウント: {p['name']}（プロファイル）"
        out += [head, ""]
        if tool == "mail" and p.get("mail_filter"):
            out += ["このアカウントのメールは、あらかじめ決められた検索条件に合うものだけが対象"
                    "（自動で足される。条件に合わないメールは、見つからない）。", ""]
        reads = [s for s in cmds if s in read]
        writes = [s for s in cmds if s in write]
        if reads:
            out += ["読み取り（承認済み。そのまま実行してよい）:"] + [f"- {table[s].format(b=b)}" for s in reads] + [""]
        if writes:
            if p["write_mode"] == "allow":
                out += ["書き込み（承認なしで実行できる。**実行したら、そのターンの最後に、何をしたか（全件）をユーザーに報告する**。"
                        "記録は残るが、報告は省略しない）:"]
            else:
                out += ["書き込み（実行のたびにユーザーの承認が要る）:"]
            out += [f"- {table[s].format(b=b)}" for s in writes] + [""]
    return "\n".join(out)
