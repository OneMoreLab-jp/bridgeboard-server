# 手本（本文を置き換えた本物の会話ログ）

作った日: 2026-10-27（Claude Code 2.1.310 の会話ログ）。作り方の道具: [`../make_fixture.py`](../make_fixture.py)

本物の会話ログを短く切り、**形はそのまま、本文だけを置き換えた**もの。
行の種類・項目・`uuid` と `parentUuid` のつながり・行の順・改行の位置は元のまま。
文字列の値は `KEEP_KEYS`（`type`・`uuid`・`timestamp`・`name` など）を除いて `文{n}` にした（同じ文字列は同じ `n`）。
空の文字列は空のまま、`<task-notification>` は `<task-notification>文{n}</task-notification>`、
`<command-…>`・`<local-command-…>`・`<bash-…>`・`<agent-message` で始まる文字列は先頭の札の名前だけを残して `<札>文{n}</札>`
（一覧サーバーがこの札でコマンドの行・知らせを見分けるため。2026-10-27 に足し、`tmux_rc_cleared.jsonl` の4行が変わった）、
貼った画像は 1×1 の PNG、`cwd` は `/fixture/project`。UUID（`uuid`・`parentUuid`・`sessionId` など）と `msg_`・`toolu_` の ID は、
形と長さを保った作り物（`make_fixture.py` の `FakeIds`）にした（同じ ID は同じ作り物なので、つながりは残る）。決まりの全体は `make_fixture.py` の先頭に書いた。

切り出した元は、作者の手元の試験用セッション（下の A・B・C。`~/.claude/projects/` の下の会話ログ）だけ。
ほかのプロジェクトの会話は使っていない。

## 各手本

| 手本 | 元（セッションの会話ログ） | 行の範囲 | 入っていること |
|---|---|---|---|
| `tmux_rc.jsonl` | セッション A の会話ログ（tmux の Remote Control・`--permission-mode manual`） | 1-13, 48-129, 161-217, 271-306, 416-476 | 権限の確認を経た Bash の呼び出し 4回（答えの行に `permissionDecision`）、`AskUserQuestion` 3問（呼び出しの行と答えの行）、作業中に claude.ai から送った指示（enqueue に本文 → `remove` → `queued_command`、`prompt` は文字列）、`<task-notification>` の enqueue と `origin.kind: task-notification` の行、作業中に2本たまった待ち行列（enqueue 2 → dequeue 2）、巻き戻し（R1〜R4。R4 の `parentUuid` が R2 と同じ行を指す）、最後の `/clear` の enqueue |
| `tmux_rc_cleared.jsonl` | セッション A と同じフォルダの、上の `/clear` の後にできた会話ログ | 1-54（全部） | 新しい会話ログの先頭：`isMeta` の行、`origin` の無い `<command-name>` の行、`system` の `local_command`、その後の最初の指示（C1）と返事 |
| `vscode.jsonl` | セッション B の会話ログ（VS Code のセッション） | 1-8, 982-1015, 1095-1103, 1147-1183, 2144-2152 | Mac から送った作業中の指示（enqueue が空 → `queued_command` → `remove`）2本。うち1本は `prompt` に画像、並列のツール呼び出し（親が同じ行の組：Edit 2本、Edit 5本＋Bash、Write＋Bash）、`Write`・`Edit`、貼った画像つきの指示（user の `image`）と `isMeta` の `[Image: …]` の行 |
| `compact_error.jsonl` | セッション C の会話ログ＋手で作った2行 | 978-994、末尾の2行は手で作った | `system` の `api_error`（本物の行）。**末尾の2行（`system` の `compact_boundary` と `isCompactSummary` の行）は、元の2つのフォルダに圧縮の行が無かったため、他の会話ログ（2.1.220）の同じ種類の行から項目の形だけを写して作った**（値は写していない。`uuid` は新しく作り、`version` はこの手本にそろえた） |

| `image_only.jsonl` | セッション C の会話ログ | 2638-2646（2026-10-27 に足した） | **画像だけを貼った指示**（`origin.kind: human`、`content` は `image` 1つだけでテキストのブロックが無い）と、その後の `isMeta` の `[Image: …]` の行。前の返事・空の enqueue/dequeue も入る。最初の読み取り器はこの指示を数えない（テキストが無いため）ので、照合の試験には入れない |

## 作り直し方

```bash
A=<セッション A の会話ログ.jsonl>     # ~/.claude/projects/<フォルダ>/<セッションID>.jsonl
A2=<セッション A の /clear の後の会話ログ.jsonl>
B=<セッション B の会話ログ.jsonl>
C=<セッション C の会話ログ.jsonl>
F=tests/fixtures
python3 tests/make_fixture.py --src "$A" --lines 1-13,48-129,161-217,271-306,416-476 --out $F/tmux_rc.jsonl
python3 tests/make_fixture.py --src "$A2" --lines 1-54 --out $F/tmux_rc_cleared.jsonl
python3 tests/make_fixture.py --src "$B" --lines 1-8,982-1015,1095-1103,1147-1183,2144-2152 --out $F/vscode.jsonl
python3 tests/make_fixture.py --src "$C" --lines 978-994 --out $F/compact_error.jsonl
python3 tests/make_fixture.py --src "$C" --lines 2638-2646 --out $F/image_only.jsonl
```

`compact_error.jsonl` を作り直すときは、末尾の手で作った2行を残しておいて足し直す。
`test_units.py` の `test_fixtures_hold_no_original_text` が、どの手本にも置き換えていない本文・`/Users/` が無いことを毎回確かめる。
