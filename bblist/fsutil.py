"""置き場のファイルを安全に書く道具（排他・一時ファイル経由の置き換え・権限 600/700）。

Python 3.9 の標準ライブラリだけで動かす。secret・devices.json・pairing.json が使う。
書く中身（鍵・符号のハッシュなど）は、例外の文にも出さない（例外は errno と固定の文だけ）。
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
from pathlib import Path


def ensure_dir(directory: Path) -> None:
    """置き場のフォルダを（無ければ 700 で）作る。あるなら権限は変えない。"""
    Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)


@contextlib.contextmanager
def exclusive(directory: Path):
    """置き場のフォルダの排他（flock）。同じプロセスの別の糸も、別のプロセスも待たせる。フォルダが無ければ 700 で作る。"""
    ensure_dir(directory)
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                                          # 閉じると解ける


def write_atomic(path: Path, data: bytes) -> None:
    """data を path に権限 600 で書く。同じフォルダの一時ファイルに全部書いて fsync してから置き換える（書きかけを読ませない）。
    置き場のフォルダが無ければ 700 で作る。path がシンボリックリンクでも、リンクの先は書かない（置き換えるのはリンクそのもの）。"""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.parent / f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
