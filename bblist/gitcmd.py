"""git を呼ぶ共通の口（sessions.run_git と gitignore.run_check_ignore が使う）。

このサーバーは、利用者のプロジェクトの中で git を動かす。git clone した他人のリポジトリも含むので、
git に何も任せない呼び方に1か所でそろえる。

- `-c core.fsmonitor=false`：索引を読む git は、リポジトリの `.git/config` の `core.fsmonitor` に書かれた命令を走らせる
  （check-ignore で再現した）。他人のリポジトリの設定で、このサーバーの権限の命令が動かないようにする
- 環境変数：`GIT_` で始まる継承したものを、ひとつ残らず渡さない（`GIT_DIR` `GIT_WORK_TREE` `GIT_INDEX_FILE` `GIT_CONFIG_*`
  `GIT_EXTERNAL_DIFF` `GIT_SSH_COMMAND` など。向き先・設定・外部命令を、環境が変えられないように）。そのうえで
  `GIT_OPTIONAL_LOCKS=0`（索引を書き直そうとしない）と `LC_ALL=C`（標準エラーの文を英語に固定）を付ける
- 引数の並びで呼ぶ（シェルを通さない）。名前を渡さない呼び出しは stdin を閉じる。時間切れは呼び出し側が決める
- `-C <フォルダ>` で調べるフォルダを決める。`ceilings` を渡すと GIT_CEILING_DIRECTORIES にする（そこより上へは登らせない）

失敗は例外のまま上げる（OSError・subprocess.SubprocessError・ValueError）。どう扱うかは呼び出し側が決める。
記録には、パスも名前も出力も書かない。Python 3.9 の標準ライブラリだけで動かす。
"""
from __future__ import annotations

import os
import subprocess


def git_env(ceilings=()) -> dict:
    """git に渡す環境変数。GIT_ で始まる継承したものは渡さない。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["LC_ALL"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if ceilings:
        env["GIT_CEILING_DIRECTORIES"] = ":".join(ceilings)
    return env


def run(args, directory, *, timeout: float, input: bytes | None = None, ceilings=()) -> subprocess.CompletedProcess:
    """`git -c core.fsmonitor=false -C <directory> <args...>` を1回呼ぶ。標準出力と標準エラーは bytes で受ける。"""
    extra = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(directory), *args],
                          capture_output=True, env=git_env(ceilings), timeout=timeout, **extra)
