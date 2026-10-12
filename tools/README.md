# .agents/ について

ここにあるファイル(安全規則、このマシンのツール一覧、Drive CLI の案内、
commit-before-run フック)は、経理・秘書の両プロジェクトで共通です。実体は
このリポジトリ(`accountant`)にあり、他のプロジェクトはシンボリックリンクで
共有しています。編集は、必ずこの `accountant` 側の実体で行ってください。

## 新しい agy プロジェクトに同じ設定を入れる

手作業で1つずつ揃えるのではなく、`scripts/setup_agy_project.py` を使います。

```
.venv/bin/python scripts/setup_agy_project.py <対象プロジェクトのパス> [--with-gdrive]
```

- 対象ディレクトリに、ここの共有ファイルへのシンボリックリンクを張り、
  `AGENTS.md` に読み込み行を追記し、`scratch/*.py` が commit-before-run
  フックの対象になるよう `.gitignore` を調整します。
- 何度実行しても安全です(揃っているものはスキップします)。
- Google Drive の読み書きが要るプロジェクトなら `--with-gdrive` を付けます。
- agy 側のプロジェクト登録・許可リストの設定と、Slack ブリッジの
  `config.yaml` へのチャンネル追加は、プロジェクト固有の ID が要るため
  対象外です(実行後に案内が出ます。`agy-slack-bridge` の README も参照)。

新しい共有ファイルをここに追加したときは、`SHARED_LINKS`(または
`GDRIVE_LINKS`)にも追記してください。
