"""設定ファイル config.json（置き場の中・権限 600）を読む。

Python 3.9 の標準ライブラリだけで動かす。

  {"apiPort": 8772, "filePort": 8773, "claudeDir": "~/.claude", "showGitIgnored": false, "previewExternal": true,
   "launcherRoots": [{"path": "~/Projects", "depth": 2}], "tailscale": null, "python": null}

- 無いファイル・無い項目・壊れた項目は既定の値（1項目ずつ。ほかの項目は読む）。壊れた項目の名前は Config.problems に出す
  （値は出さない。パスは利用者のもので、記録に残したくない）。サーバーは読むだけで、書かない・作らない・権限も変えない
  （書くのは install.sh）。
- 権限：このファイルは tailscale のパス（サーバーが実行する）と、見せる範囲を決める。他人が書き換えられるもの
  （グループ・その他に書き込み権がある、または自分のものでない）・リンク・普通のファイルでないものは、信じて読まず
  全部の既定に戻す（開けない・書き換えられた設定を黙って採らない）。ただし previewExternal だけは既定の true でなく
  false（厳しい側）にする。showGitIgnored は既定のまま false。
  previewExternal が書いてあるのに真偽でない値（"false" の文字列など）のときも false（書き手は止めるつもりだったかもしれない）。
  読めるだけの緩さ（644 など）は、中身に秘密が無いので、知らせて読む。
- launcherRoots：{"path", "depth"}。depth 2 は <path>/<分類>/<フォルダ>、depth 1 は <path>/<フォルダ>。空なら
  Catalog が会話ログの作業フォルダから作る（discover.TranscriptFolders）。
- パスの先頭の ~ はホームに読み替える。~ほかの利用者名の形・相対パスは使わない。
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path

NAME = "config.json"
MAX_BYTES = 64 * 1024                                     # これより大きい config.json は読まない
MAX_ROOTS = 32                                            # launcherRoots の数の上限
DEFAULT_API_PORT, DEFAULT_FILE_PORT = 8772, 8773
_UNUSABLE = object()                                      # _read の答え：信じて読めない（問題の文を伴う）


@dataclass(frozen=True)
class Config:
    api_port: int = DEFAULT_API_PORT
    file_port: int = DEFAULT_FILE_PORT
    claude_dir: Path = field(default_factory=lambda: Path.home() / ".claude")
    show_git_ignored: bool = False                        # git で無視されたファイルを返すか
    preview_external: bool = True                         # 開いた html の外への読み込み（画像・CDN・フォント）を許すか
    launcher_roots: tuple = ()                            # ((Path, 深さ 1|2), …)。空なら会話ログから作る
    tailscale: str | None = None                          # tailscale の実体のパス（null なら探す）
    python: str | None = None                             # install.sh が書く Python のパス（サーバー自身は使わない）
    problems: tuple = ()                                  # 読めなかった・緩かった項目の名前（記録に出す。値は入れない）


def load_config(support_dir: Path, home: Path | None = None) -> Config:
    """置き場の config.json を読む。無い・壊れた項目は既定。ここでは何も書かない。"""
    home = Path(home) if home is not None else Path.home()
    defaults = Config(claude_dir=home / ".claude")
    raw, problems = _read(Path(support_dir) / NAME)
    # ファイルがあるのに信じて読めないときは、外への読み込みを許す側（既定の true）に倒さず、厳しい側にする
    strict = replace(defaults, preview_external=False)
    if raw is _UNUSABLE:
        return replace(strict, problems=tuple(problems))
    if not isinstance(raw, dict):
        return replace(strict, problems=tuple(problems + [f"{NAME}: not a JSON object"]))
    bad: list[str] = []

    def pick(key, ok, convert=lambda v: v, default=None):
        """raw[key] が ok を通ればその値（convert 後）。無ければ default。形が違えば default にして key を bad に足す。"""
        if key not in raw:
            return default
        value = raw[key]
        try:
            if ok(value):
                return convert(value)
        except (TypeError, ValueError):
            pass
        bad.append(key)
        return default

    api = pick("apiPort", _is_port, default=DEFAULT_API_PORT)
    file = pick("filePort", _is_port, default=DEFAULT_FILE_PORT)
    if api == file:
        bad.append("apiPort/filePort (must differ)")
        api, file = DEFAULT_API_PORT, DEFAULT_FILE_PORT
    config = Config(
        api_port=api, file_port=file,
        claude_dir=pick("claudeDir", lambda v: _expand(v, home) is not None, lambda v: _expand(v, home),
                        defaults.claude_dir),
        show_git_ignored=pick("showGitIgnored", lambda v: isinstance(v, bool), default=False),
        preview_external=pick("previewExternal", lambda v: isinstance(v, bool), default=True) and "previewExternal" not in bad,
        launcher_roots=_roots(raw.get("launcherRoots", []), home, bad),
        tailscale=pick("tailscale", lambda v: v is None or _expand(v, home) is not None,
                       lambda v: None if v is None else str(_expand(v, home))),
        python=pick("python", lambda v: v is None or _expand(v, home) is not None,
                    lambda v: None if v is None else str(_expand(v, home))),
    )
    return replace(config, problems=tuple(problems + [f"{NAME}: ignored {k}" for k in bad]))


def _is_port(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535


def _expand(v, home: Path) -> Path | None:
    """パスの文字列を絶対パスにする（先頭の ~ はホーム）。文字列でない・空・NUL を含む・~ほかの利用者・相対パスなら None。"""
    if not isinstance(v, str) or not v or "\0" in v:
        return None
    if v == "~":
        return home
    if v.startswith("~/"):
        v = str(home / v[2:])
    p = Path(v)
    return p if p.is_absolute() else None


def _roots(value, home: Path, bad: list) -> tuple:
    """launcherRoots。形の違う要素は1つずつ飛ばして、launcherRoots を bad に足す。"""
    if not isinstance(value, list):
        bad.append("launcherRoots")
        return ()
    out, dropped = [], False
    for item in value:
        path = _expand(item.get("path"), home) if isinstance(item, dict) else None
        depth = item.get("depth", 1) if isinstance(item, dict) else None
        if path is None or isinstance(depth, bool) or depth not in (1, 2) or not isinstance(depth, int):
            dropped = True
        elif len(out) < MAX_ROOTS:
            out.append((path, depth))
        else:
            dropped = True
    if dropped:
        bad.append("launcherRoots")
    return tuple(out)


def _read(path: Path):
    """(JSON の中身, 問題の文の並び)。ファイルが無ければ ({}, [])＝全部既定で問題なし。信じて読めない・JSON でないときは
    (_UNUSABLE, 問題の文)。JSON の null などの辞書でないものは、そのまま返す（呼び出し側が「辞書でない」とする）。

    開いたものを fstat して確かめる（確かめてから開く間に入れ替えられない）。リンクは開かず（O_NOFOLLOW）、FIFO で待たない
    （O_NONBLOCK）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except FileNotFoundError:
        return {}, []
    except OSError as e:                                  # リンク（ELOOP）・権限・親が普通のフォルダでない など
        return _UNUSABLE, [f"{NAME}: not read ({type(e).__name__})"]
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _UNUSABLE, [f"{NAME}: not read (not a regular file)"]
        if st.st_uid != os.geteuid():
            return _UNUSABLE, [f"{NAME}: not read (owned by another user)"]
        if st.st_mode & 0o022:
            return _UNUSABLE, [f"{NAME}: not read (writable by group or others; run chmod 600)"]
        if st.st_size > MAX_BYTES:
            return _UNUSABLE, [f"{NAME}: not read (larger than {MAX_BYTES} bytes)"]
        data = os.read(fd, MAX_BYTES + 1)
        note = [f"{NAME}: readable by group or others (run chmod 600)"] if st.st_mode & 0o044 else []
    except OSError as e:
        return _UNUSABLE, [f"{NAME}: not read ({type(e).__name__})"]
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        return _UNUSABLE, [f"{NAME}: not read (larger than {MAX_BYTES} bytes)"]
    try:
        return json.loads(data.decode("utf-8")), note
    except (ValueError, RecursionError):                  # UnicodeDecodeError は ValueError の仲間
        return _UNUSABLE, note + [f"{NAME}: not read (not valid JSON)"]
