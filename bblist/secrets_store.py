"""置き場の secret（起点の名前の塩・ファイルの鍵の親・サーバーの ID の親）と、端末ごと・起点ごとのファイルの鍵。

Python 3.9 の標準ライブラリだけで動かす。

secret は 32 バイトの乱数で、置き場（~/Library/Application Support/BridgeBoard/）の `secret` に権限 600 で置く。
- rootId の塩：rootId = sha256(secret + b"root:" + パス)。パスが分かっても rootId を計算できない（paths.folder_id）
- ファイルの鍵：file_key(secret, deviceId, rootId)。端末ごと・起点ごとに別の値。ある起点の鍵で別の起点のファイルは開かないし、
  端末を取り消せば（devices.json から外せば）その端末の鍵は使えなくなる（web.py が端末の存在も確かめる）
- サーバーの ID：server_id(secret)。QR に入れて、アプリがどのサーバーと組んだかを覚える目印（鍵ではない）
secret・鍵は標準出力・記録・URL・例外の文に出さない。無くなる（消す・壊れる）と作り直し、rootId と鍵とサーバーの ID が一度変わる
（アプリは鍵を /v1/filekey で取り直し、rootId で覚えた☆・開いたフォルダは消える。端末の鍵には影響しない）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

from .devices import DEVICE_ID
from .fsutil import exclusive as _exclusive

SECRET_BYTES = 32
_NAME = "secret"


def load_or_create_secret(support_dir: Path) -> bytes:
    """置き場の secret（32 バイト）を読む。無い・短い（書きかけ）・壊れているなら作る。同時に呼ばれても全員が同じ値を返す。

    置き場が無ければ 700 で作る（あるなら権限は変えない）。secret が緩い権限なら 600 に締める。
    secret の名前がシンボリックリンクなど普通のファイルでなければ OSError（読ませる先・書かせる先を変えさせない）。
    作るときは、同じ置き場の一時ファイル（600）に全部書いてから os.link で secret にする：無ければ作る（あれば EEXIST で、
    先にあるものを読む）ので、最初の起動が重なっても勝つのは1つで、書きかけも読ませない。置き換える（os.replace）だけだと
    最後の者が勝ち、先に返した者の値が捨てられる。
    壊れた secret を直すときだけ、置き場を排他（flock）で使い、中で読み直す（先に直した者の値に従う）。
    """
    support_dir = Path(support_dir)
    support_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = support_dir / _NAME
    got = _read(path)
    if got is not None:
        return got
    secret = secrets.token_bytes(SECRET_BYTES)
    tmp = support_dir / f"{_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(secret)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)                                # 無ければ作る（中身が揃ってから現れる）
            return secret
        except FileExistsError:
            pass
        got = _read(path)                                     # 先に作った者がいる：その値に従う
        if got is not None:
            return got
        with _exclusive(support_dir):                         # あるが壊れている（短い・長い）：直す
            got = _read(path)                                 # 待つ間に、ほかの者が直したかもしれない
            if got is not None:
                return got
            os.replace(tmp, path)
            return secret
    finally:
        try:
            os.unlink(tmp)                                    # link した後・replace で消えた後は、無くてよい
        except FileNotFoundError:
            pass


def _read(path: Path) -> bytes | None:
    """secret のファイルの中身。無い・32 バイトでない（短い・長い）なら None。リンク・普通でないファイルは OSError。
    緩い権限は 600 に締める。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)    # リンクは ELOOP。FIFO でも待たない
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{_NAME} is not a regular file")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        data = os.read(fd, SECRET_BYTES + 1)
    finally:
        os.close(fd)
    return data if len(data) == SECRET_BYTES else None


def file_key(secret: bytes, device_id: str, root_id: str) -> str:
    """端末 device_id が起点 root_id のファイルを開く鍵。HMAC-SHA256(secret, b"file:" + deviceId + b":" + rootId) の16進。
    端末ごと・起点ごとに別の値。device_id は devices.py が作る16桁の小文字の16進だけ（":" を含むものなどで
    (端末, 起点) の切れ目をずらせないように、違えば ValueError）。"""
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        raise ValueError("device_id must be 16 lowercase hex digits")
    msg = b"file:" + device_id.encode("ascii") + b":" + root_id.encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def server_id(secret: bytes) -> str:
    """このサーバーの ID（16桁の16進）。HMAC-SHA256(secret, b"server-id") の先頭。QR に入れる目印で、鍵ではない。"""
    return hmac.new(secret, b"server-id", hashlib.sha256).hexdigest()[:16]
