"""Credential profiles: named sets of environment variables (tokens, keys) the bridge keeps in
its config dir, where the agent can neither read nor write them (permissions.deny).

`<config dir>/credentials/<name>.json` is `{"env": {"NAME": "value", ...}}`, mode 600. They are
handed to a command only through tools/cred_run.py, which also masks the values in the command's
output; the bridge masks them in what it posts to Slack (see `mask`).
"""
import base64
import json
import os
import re
import stat
import urllib.parse

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Names that change how a program is loaded or found: a profile is not the place for them.
FORBIDDEN_ENV = {"PATH", "HOME", "SHELL", "USER", "IFS", "ENV", "BASH_ENV", "PWD", "OLDPWD"}
FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHON", "NODE_", "PERL", "RUBY", "BASH_FUNC_")
MIN_SECRET_LEN = 6            # shorter values would mask ordinary text
MASK = "***"


class ProfileError(Exception):
    """A profile that cannot be used. The message never contains a value."""


def credentials_dir(base: "str | None" = None) -> str:
    base = base or os.environ.get("AGY_BRIDGE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "agy-slack-bridge")
    return os.path.join(base, "credentials")


def profile_path(name: str, base: "str | None" = None) -> str:
    if not NAME_RE.match(name or ""):
        raise ProfileError("プロファイル名は英数字・_・- だけです")
    return os.path.join(credentials_dir(base), f"{name}.json")


def env_name_problem(name: str) -> "str | None":
    if not ENV_NAME_RE.match(name or ""):
        return "環境変数名は大文字・数字・_ だけです（例: OP_SERVICE_ACCOUNT_TOKEN）"
    if name in FORBIDDEN_ENV or name.startswith(FORBIDDEN_ENV_PREFIXES):
        return f"{name} はプログラムの読み込みや探索を変える変数なので、プロファイルには入れられません"
    return None


def read_profile(path: str) -> dict:
    """The profile's environment. Refuses a file others can read or write (a secret store
    that is not private is not one) and any name or value that is not usable as is."""
    try:
        st = os.stat(path)
    except OSError as e:
        raise ProfileError(f"プロファイルを読めません: {path}（{e.strerror}）") from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProfileError(f"{path} が group/other から読み書きできます（chmod 600 にしてください）")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise ProfileError(f"{path} を JSON として読めません") from None
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict) or not env:
        raise ProfileError(f"{path} に env（環境変数の名前と値）がありません")
    for k, v in env.items():
        problem = env_name_problem(k)
        if problem:
            raise ProfileError(problem)
        if not isinstance(v, str) or not v:
            raise ProfileError(f"{path}: {k} の値が文字列ではありません")
    return dict(env)


def write_profile(path: str, env: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"env": env}, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def secret_forms(value: str) -> list:
    """The value as it may appear in output: as is, base64, URL-encoded, JSON-escaped."""
    raw = value.encode("utf-8")
    forms = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value),
             json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1],
             base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(),
             base64.b64encode(raw).decode().rstrip("="), base64.urlsafe_b64encode(raw).decode().rstrip("=")}
    return [f for f in forms if len(f) >= MIN_SECRET_LEN]


def secrets_of(env: dict) -> list:
    out: set = set()
    for v in env.values():
        if len(v) >= MIN_SECRET_LEN:
            out.update(secret_forms(v))
    return sorted(out, key=len, reverse=True)           # longest first: a form that contains another


def mask(text: str, secrets: list) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, MASK)
    return text


_CACHE: dict = {}


def all_secrets(base: "str | None" = None) -> list:
    """Every readable profile's secrets (for the bridge's outgoing text), cached by mtime."""
    d = credentials_dir(base)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    found: list = []
    for n in names:
        path = os.path.join(d, n)
        try:
            key = (path, os.stat(path).st_mtime_ns)
            if key not in _CACHE:
                _CACHE[key] = secrets_of(read_profile(path))
            found.extend(_CACHE[key])
        except (OSError, ProfileError):
            continue
    return sorted(set(found), key=len, reverse=True)
