#!/usr/bin/env python3
"""
scratch/ の下のものを削除する固定コマンド（エージェント用。gdrive_cli.py と同じく、インストール先に置く）
===========================================================================================
`rm` は、agy の許可（前方一致）で `rm <許可したパス> <別のパス>` のように、あとに別のパスを足されても
通ってしまうので、スクリプトの許可と一緒に出す削除の許可には使えない。代わりにこれを使う。
検査は、このスクリプト自身が行う。

実行方法（<prefix> = インストール先、<root> = プロジェクトのディレクトリ。許可リストのエントリと一致させること。
-I と --root は必須で、--root は必ず先頭）:
  <prefix>/.venv/bin/python -I <prefix>/tools/scratch_clean.py --root <root> <パス>...

- 消せるのは、<root>/scratch/ の下にあるものだけ（ふつうのファイル、ディレクトリ、ソケットなど何でも。
  ディレクトリは中身ごと）。scratch/ そのものは消さない。
- パスは、<root> からの相対パスか、絶対パス。末尾の名前を除いて解決する（`..` やシンボリックリンクを
  たどったあとで scratch/ の下であること）。
- 末尾の名前がシンボリックリンクなら、リンク自体を消す（指す先は、どこにあっても消さない）。
  ディレクトリの中のリンクも、たどらずに、リンク自体だけを消す。
- 引数は全部を先に検査して、1 つでも条件に合わなければ、何も消さずに終わる（終了コード 1）。
- `-` で始まる引数は、オプションとして扱わず、拒否する。
"""
import os
import shutil
import sys


def resolve(root: str, arg: str) -> str:
    """削除対象の、解決したパス。条件に合わなければ ValueError（理由つき）。"""
    root_real = os.path.realpath(root)
    scratch = os.path.realpath(os.path.join(root_real, "scratch"))
    if not os.path.isdir(scratch):
        raise ValueError(f"{root_real}/scratch がありません")
    if arg.startswith("-"):
        raise ValueError(f"{arg}: `-` で始まる引数は受け付けません")
    path = os.path.join(root_real, arg).rstrip(os.sep)
    head, tail = os.path.split(path)
    if tail in ("", ".", ".."):
        raise ValueError(f"{arg}: 削除するものの名前で終わるパスを指定してください")
    target = os.path.join(os.path.realpath(head), tail)
    if not target.startswith(scratch + os.sep):
        raise ValueError(f"{arg}: {scratch} の下にありません（scratch/ 自体も消せません）")
    if not os.path.lexists(target):
        raise ValueError(f"{arg}: 存在しません")
    return target


def remove(target: str) -> None:
    """リンクは、リンク自体を消す。ディレクトリは中身ごと（中のリンクもたどらない）。"""
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    else:
        os.unlink(target)


def clean(root: str, args: "list[str]") -> "tuple[list[str], list[str]]":
    """(消したもの, 拒否の理由)。拒否の理由が 1 つでもあれば、何も消さない。"""
    targets, errors = [], []
    for arg in args:
        try:
            targets.append(resolve(root, arg))
        except ValueError as e:
            errors.append(str(e))
    if errors or not targets:
        return [], errors or ["削除するパスを指定してください"]
    done = []
    for target in dict.fromkeys(targets):
        if not os.path.lexists(target):          # 先に消した親ディレクトリの中にあった
            continue
        remove(target)
        done.append(target)
    return done, []


def main(argv: "list[str] | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 3 or argv[0] != "--root":
        print("使い方: scratch_clean.py --root <プロジェクトのディレクトリ> <パス>...", file=sys.stderr)
        return 2
    if not os.path.isdir(argv[1]):
        print(f"--root がディレクトリではありません: {argv[1]}", file=sys.stderr)
        return 2
    done, errors = clean(argv[1], argv[2:])
    for e in errors:
        print(f"拒否: {e}", file=sys.stderr)
    if errors:
        print("何も削除していません", file=sys.stderr)
        return 1
    for d in done:
        print(f"削除: {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
