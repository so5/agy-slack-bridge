"""`agy models` の一覧と、その共有キャッシュ。bridge.py(/agy-model)と setup_agy_project.py(モデルの選択)が
同じファイル(<設定ディレクトリ>/models-cache.json)を使う。どちらかが取得すれば、もう一方は agy を呼ばない。
鮮度は読む側が決める(ttl)。取得に失敗したときは、古いキャッシュを返す。"""
import json
import os
import subprocess
import time
from pathlib import Path

CACHE_NAME = "models-cache.json"


def parse(stdout: str) -> "list[tuple[str, str]]":
    """`id<TAB>表示名` の行を (id, 表示名) にする。"""
    models = []
    for line in stdout.splitlines():
        model_id, _, label = line.strip().partition("\t")
        if model_id:
            models.append((model_id, label.strip() or model_id))
    return models


def run_agy_models(agy_bin: str) -> "list[tuple[str, str]]":
    try:
        proc = subprocess.run([agy_bin, "models"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse(proc.stdout)


def load_cache(config_dir: Path) -> "tuple[float, list[tuple[str, str]]]":
    try:
        data = json.loads((Path(config_dir) / CACHE_NAME).read_text(encoding="utf-8"))
        return float(data["ts"]), [(m[0], m[1]) for m in data["models"]]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return 0.0, []


def save_cache(config_dir: Path, models: "list[tuple[str, str]]") -> None:
    path = Path(config_dir) / CACHE_NAME
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"ts": time.time(), "models": models}, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


def get_models(config_dir: Path, agy_bin: str, ttl: float) -> "list[tuple[str, str]]":
    """ttl 秒以内のキャッシュがあればそれを返す。無ければ agy に問い合わせて保存する。
    取得に失敗したら、古いキャッシュ(無ければ空)を返す。"""
    ts, cached = load_cache(config_dir)
    if cached and time.time() - ts < ttl:
        return cached
    fresh = run_agy_models(agy_bin)
    if fresh:
        save_cache(config_dir, fresh)
        return fresh
    return cached
