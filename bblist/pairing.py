"""5分で切れる、一度きりの組む符号（pairing.json）と、組む口の回数の上限。

Python 3.9 の標準ライブラリだけで動かす。

bbctl pair（別のプロセス）が符号を作り、動いている一覧サーバーが POST /v1/pair で引き換える。作る・引き換える・消すは、
置き場のフォルダの排他（fsutil.exclusive。devices.json と同じ）の中で、読み直してから行う（引き換えが、待つ間に別の bbctl が
書いた新しい符号を消さない）。違う符号は、排他を握らずに断る。
符号そのものは bbctl のメモリと QR の PNG にしか無く、置き場の pairing.json には sha256 のハッシュだけを書く：
`{"hash": sha256(符号), "deadline": 単調な時計の値 + 300, "boot": カーネルの起動時刻}`（権限 600・一時ファイル経由で置き換える）。
符号は secrets.token_urlsafe(16)＝128 ビット。同時に有効なのは1つ（作り直すと前のは使えない）。引き換えると pairing.json を消す。

期限の時計：別のプロセスの間で比べるので、システム全体で同じ値を返す時計でなければならない。time.monotonic() は、このMacの
/usr/bin/python3（3.9.6）ではプロセスの起動からの秒を返し【実測 2026-10-31】、プロセスをまたぐと比べられない。
そこで macOS の CLOCK_MONOTONIC（起動からの秒。眠っている間も進む＝壁の時計を替えても、Mac を眠らせても、5分は5分）を使う。
再起動すると時計が戻るので、起動時刻（sysctl kern.boottime）が違う符号は使えない。壁の時計が直されると起動時刻も同じだけ
ずれうるので、BOOT_TOLERANCE 秒までのずれは同じ起動とみなす（再起動の間は、その何十倍も開く）。
期限が今から 300 秒より先になっているもの（別の時計の値）も信じない。
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from pathlib import Path

from .fsutil import exclusive, write_atomic

NAME = "pairing.json"
CODE_TTL = 300                                            # 符号の有効な秒数
BOOT_TOLERANCE = 2                                        # 起動時刻のずれを同じ起動とみなす秒数
STALE_QR_AGE = CODE_TTL + 60                              # これより古い pair/qr-*.png は、サーバーの起動で消す（秒）
MAX_CODE_LENGTH = 256                                     # これより長い符号は試すまでもなく断る（ハッシュの計算を軽く保つ）
MAX_FILE_BYTES = 4096
SYSCTL = "/usr/sbin/sysctl"
_HASH = re.compile(r"[0-9a-f]{64}")


def monotonic() -> float:
    """システム全体で同じ、戻らない時計（秒）。別のプロセスの値と比べられる。眠っている間も進む。
    使えなければ OSError（time.monotonic() に替えない。その値はこのMacの /usr/bin/python3 ではプロセスごとで、
    別のプロセスが書いた期限と比べると、いつも切れている／いつまでも切れない、のどちらかになる）。"""
    clock_id = getattr(time, "CLOCK_MONOTONIC", None)
    if clock_id is None:
        raise OSError("no system-wide monotonic clock on this platform")
    return time.clock_gettime(clock_id)


def parse_boottime(text: str) -> int:
    """`sysctl -n kern.boottime` の答え（`{ sec = 1792343805, usec = 432273 } Mon Oct 19 ...`）の秒。読めなければ OSError。"""
    m = re.search(r"\bsec\s*=\s*(\d+)", text)
    if m is None:
        raise OSError("cannot read kern.boottime")
    return int(m.group(1))


def boot_time() -> int:
    """カーネルの起動時刻（Unix 秒）。取れなければ OSError。"""
    try:
        r = subprocess.run([SYSCTL, "-n", "kern.boottime"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"cannot run sysctl ({type(e).__name__})") from None
    if r.returncode != 0:
        raise OSError("sysctl kern.boottime failed")
    return parse_boottime(r.stdout)


class Pairing:
    """pairing.json の読み書き。clock は単調な時計（秒）、boot は起動時刻を返すもの（試験が差し替える）。"""

    def __init__(self, support_dir: Path, clock=None, boot=None):
        self.dir = Path(support_dir)
        self.path = self.dir / NAME
        self._clock = clock or monotonic
        self._boot = boot or boot_time

    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    def _load(self):
        """("missing", None)・("bad", None)・("ok", 中身)。リンク・普通でないファイル・形が違うものは "bad"。"""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "bad", None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "bad", None
            raw = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_FILE_BYTES else None
        except (UnicodeDecodeError, ValueError, RecursionError):      # 深すぎる入れ子も壊れたファイル
            return "bad", None
        if (not isinstance(data, dict) or not isinstance(data.get("hash"), str) or not _HASH.fullmatch(data["hash"])
                or isinstance(data.get("deadline"), bool) or not isinstance(data.get("deadline"), (int, float))
                or not math.isfinite(data["deadline"])
                or isinstance(data.get("boot"), bool) or not isinstance(data.get("boot"), int)):
            return "bad", None
        return "ok", data

    def _in_time(self, data: dict) -> bool:
        """期限内か（切れた・今から先すぎる＝別の時計の値、のどちらでもない）。時計が使えなければ OSError。"""
        return 0 < data["deadline"] - self._clock() <= CODE_TTL + 1

    def _live(self, data: dict) -> bool:
        """期限内で、同じ起動のうちのものか。起動時刻は sysctl を呼ぶので、期限が済んでから聞く。
        時計・起動時刻が分からなければ OSError（呼ぶ側は通さない）。"""
        if not self._in_time(data):
            return False
        return abs(self._boot() - data["boot"]) <= BOOT_TOLERANCE

    def create(self) -> str:
        """新しい符号を作って pairing.json に書く（前の符号は使えなくなる）。符号を返す（メモリから先へは渡す側の責任）。
        時計・起動時刻が分からなければ OSError。"""
        code = secrets.token_urlsafe(16)
        data = {"hash": self._hash(code), "deadline": self._clock() + CODE_TTL, "boot": self._boot()}
        with exclusive(self.dir):
            write_atomic(self.path, json.dumps(data).encode("utf-8"))
        return code

    def _mine(self, code: str, data) -> bool:
        return data is not None and hmac.compare_digest(self._hash(code), data["hash"])

    def redeem(self, code) -> bool:
        """符号が合って期限内なら pairing.json を消して True（一度きり）。違う・切れた・使用済みは False（理由は分けない）。
        違う符号では何も消さず、排他も握らない（当てずっぽうで本物の符号を使い切らせない・bbctl を待たせない）。
        合っていそうなときだけ排他を取り、読み直してから消す（待つ間に別の bbctl が書いた新しい符号を消さない）。"""
        if not isinstance(code, str) or not code or len(code) > MAX_CODE_LENGTH:
            return False
        state, data = self._load()
        if state != "ok" or not self._mine(code, data):
            return False
        with exclusive(self.dir):
            state, data = self._load()
            if state != "ok" or not self._mine(code, data):
                return False
            try:
                live = self._live(data)
            except OSError:
                return False                              # 時計・起動時刻が分からない：通さないが、符号は消さない（分かれば使える）
            try:
                os.unlink(self.path)                      # 合った符号は、切れていてもここで終わらせる（生き返らせない）
            except FileNotFoundError:
                return False
            return live

    def check(self, code: str) -> str:
        """bbctl が様子を見る（毎秒呼ぶので sysctl は呼ばない。再起動すれば bbctl も終わる）。"pending"（待っている）・
        "used"（pairing.json が無い＝引き換えられた）・"replaced"（別の符号に置き換わった）・"expired"（切れた・壊れた）。"""
        state, data = self._load()
        if state == "missing":
            return "used"
        if state == "bad":
            return "expired"
        if not self._mine(code, data):
            return "replaced"
        try:
            return "pending" if self._in_time(data) else "expired"
        except OSError:
            return "expired"                              # 時計が使えない：待ち続けない

    def discard(self, code: str) -> None:
        """この符号の pairing.json を消す（別の符号に置き換わっていれば、そちらは消さない）。無くても落ちない。"""
        state, data = self._load()
        if state != "ok" or not self._mine(code, data):
            return
        with exclusive(self.dir):
            state, data = self._load()                    # 待つ間に置き換わったかもしれない
            if state == "ok" and self._mine(code, data):
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass


def remove_stale_qr(support_dir: Path, max_age: float = STALE_QR_AGE, now: float | None = None) -> int:
    """置き場の pair/qr-*.png のうち、max_age 秒より古いものを消す（bbctl が SIGKILL などで消せずに残したもの）。
    組んでいる最中の新しい PNG は残す。リンクは辿らず、リンクそのものを消す。消した数を返す。無くても落ちない。"""
    folder = Path(support_dir) / "pair"
    now = time.time() if now is None else now
    removed = 0
    try:
        if folder.is_symlink() or not folder.is_dir():
            return 0
        names = [e.name for e in os.scandir(folder) if e.name.startswith("qr-") and e.name.endswith(".png")]
    except OSError:
        return 0
    for name in names:
        path = folder / name
        try:
            if now - os.lstat(path).st_mtime > max_age:
                os.unlink(path)
                removed += 1
        except OSError:
            pass
    return removed


class PairLimiter:
    """組む口の回数の上限：送り元ごとに window 秒に per_ip 回、全体で total 回まで。
    断った回はどちらにも数えない（断られ続けても待ちが延びない・全体の枠を使わない）。糸をまたいで使える。
    送り元の表は、窓の外に出た送り元を時々掃除して、増え続けないようにする。"""

    SWEEP_ABOVE = 256                                     # 送り元がこれより多いとき、掃除する

    def __init__(self, per_ip: int = 5, total: int = 30, window: float = 60.0, clock=time.monotonic):
        self.per_ip, self.total, self.window, self._clock = per_ip, total, window, clock
        self._all: collections.deque = collections.deque()
        self._by_ip: dict[str, collections.deque] = {}
        self._swept = float("-inf")
        self._mu = threading.Lock()

    def _expire(self, times: collections.deque, now: float) -> None:
        while times and now - times[0] >= self.window:
            times.popleft()

    def allow(self, ip: str) -> bool:
        now = self._clock()
        with self._mu:
            self._expire(self._all, now)
            mine = self._by_ip.get(ip)
            if mine is not None:
                self._expire(mine, now)
            if len(self._all) >= self.total or (mine is not None and len(mine) >= self.per_ip):
                return False
            if mine is None:
                mine = self._by_ip[ip] = collections.deque()
            mine.append(now)
            self._all.append(now)
            if len(self._by_ip) > self.SWEEP_ABOVE and now - self._swept >= self.window / 4:
                self._swept = now
                for key in list(self._by_ip):
                    self._expire(self._by_ip[key], now)
                    if not self._by_ip[key]:
                        del self._by_ip[key]
            return True
