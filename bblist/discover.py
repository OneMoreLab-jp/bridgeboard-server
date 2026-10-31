"""この Mac の環境を探す：Tailscale の実体・IP・MagicDNS の名前、Claude Code を使った作業フォルダ。

Python 3.9 の標準ライブラリだけで動かす。サーバー（listserver.py）と bbctl が同じ関数を使う（取り方を1か所にする）。

Tailscale の実体の探し方（find_tailscale）：設定の値 → PATH → /opt/homebrew/bin → /usr/local/bin →
/Applications/Tailscale.app → ~/Applications/Tailscale.app。launchd の PATH は /usr/bin:/bin:/usr/sbin:/sbin だけなので、
PATH に無いのがふつう。アプリの中の実体は、TAILSCALE_BE_CLI=1 を付けて呼ぶと CLI として動く（付けないと窓のアプリとして
立ち上がろうとし、失敗の文を標準出力に出して 0 で終わる）。Homebrew の CLI に付けても害は無いので、いつも付ける。
Tailscale は読むだけ（ip -4・status --json）。ネットには出ない。

作業フォルダ（TranscriptFolders）：~/.claude/projects/<フォルダ>/*.jsonl の先頭の行に出てくる cwd。会話ログは読むだけで、
全部は読まない（先頭 HEAD_BYTES バイトだけ。cwd は先頭の数行にある）。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")     # Tailscale の IPv4 の範囲。待ち受けてよいのは、これと loopback だけ
TAILSCALE_TIMEOUT = 5.0                                   # 秒。tailscale が答えなくても、呼ぶ側は 10 秒以内に先へ進める
_FIXED = ("opt/homebrew/bin/tailscale", "usr/local/bin/tailscale",
          "Applications/Tailscale.app/Contents/MacOS/Tailscale")
_HOME_APP = "Applications/Tailscale.app/Contents/MacOS/Tailscale"
_DNS_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def _executable(path) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find_tailscale(config_value: str | None, which=shutil.which, home: Path | None = None,
                   root: Path | str = "/") -> str | None:
    """tailscale の実体のパス。設定の値 → PATH → Homebrew（arm64・Intel）→ /Applications → ~/Applications の順で、
    最初の実行できるファイル。無ければ None。設定の値が無い（消えた・動かせない）なら、次の場所へ進む。
    root は固定の場所の起点（試験が偽の実体を置くために差し替える。既定は /）。"""
    home = Path(home) if home is not None else Path.home()
    candidates = [config_value, which("tailscale"), *(str(Path(root) / rel) for rel in _FIXED), str(home / _HOME_APP)]
    return next((c for c in candidates if _executable(c)), None)


def run_tailscale(cmd: str, *args: str) -> str | None:
    """`<cmd> <args>` の標準出力（cmd は find_tailscale が見つけた実体）。つながっていない・答えない・0 で終わらないなら None。

    アプリの中の実体は、SHLVL・TERM などの環境変数が無いと窓のアプリとして立ち上がろうとし、失敗の文を標準出力に出して
    0 で終わる（launchd の環境。【実測 2026-10-27】）。TAILSCALE_BE_CLI=1 で CLI として呼ぶ
    【Claude調査：tailscale.com/kb/1080/cli の macOS】。"""
    try:
        r = subprocess.run([cmd, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=TAILSCALE_TIMEOUT, env={**os.environ, "TAILSCALE_BE_CLI": "1"})
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def backend_state(cmd: str) -> str | None:
    """`tailscale status --json` の BackendState（"Running" なら動いている。"Stopped"・"NeedsLogin" など）。取れなければ None。
    install.sh --check が使う。cmd は find_tailscale が見つけた実体。"""
    try:
        state = json.loads(run_tailscale(cmd, "status", "--json") or "")["BackendState"]
    except (ValueError, KeyError, TypeError):
        return None
    return state if isinstance(state, str) and 0 < len(state) <= 64 else None


def ip_of(cmd: str) -> str | None:
    """`<cmd> ip -4` の最初の語（cmd は find_tailscale が見つけた実体）。IPv4 でなければ None。"""
    out = (run_tailscale(cmd, "ip", "-4") or "").split()
    if not out:
        return None
    try:
        return str(ipaddress.IPv4Address(out[0]))
    except ValueError:
        return None


def tailscale_ip(config_value: str | None = None) -> str | None:
    """`tailscale ip -4` の最初の語。取れない（入っていない・つながっていない・答えない）なら None。
    最初の語が IPv4 でなければ取れなかったことにする（窓のアプリの失敗の文を待ち受けの名前にしない）。
    config_value は設定の tailscale（find_tailscale を見よ）。"""
    cmd = find_tailscale(config_value)
    return None if cmd is None else ip_of(cmd)


def _dns_name(doc) -> str | None:
    """`status --json` の Self.DNSName（小文字・末尾の点なし）。無い・名前の形でないなら None。"""
    me = doc.get("Self") if isinstance(doc, dict) else None
    name = me.get("DNSName") if isinstance(me, dict) else None
    if not isinstance(name, str):
        return None
    name = name.strip().lower().rstrip(".")
    return name if len(name) <= 253 and _DNS_NAME.fullmatch(name) else None


def magicdns_of(cmd: str) -> tuple[bool | None, str | None]:
    """(MagicDNS が有効か, この Mac の MagicDNS の名前)。`<cmd> status --json` の CurrentTailnet.MagicDNSEnabled と Self.DNSName。
    cmd は find_tailscale が見つけた実体。有効かは True / False、欄が無い・真偽でない・答えが読めないなら None。
    名前は取れない・名前の形でないなら None。MagicDNS が無効でも名前だけ見えることがあるので、2つは別々に答える。"""
    try:
        doc = json.loads(run_tailscale(cmd, "status", "--json") or "")
    except (ValueError, RecursionError):
        return None, None
    tailnet = doc.get("CurrentTailnet") if isinstance(doc, dict) else None
    enabled = tailnet.get("MagicDNSEnabled") if isinstance(tailnet, dict) else None
    return (enabled if isinstance(enabled, bool) else None), _dns_name(doc)


def magicdns_status(config_value: str | None = None) -> tuple[bool | None, str | None]:
    """magicdns_of を、設定の tailscale（find_tailscale を見よ）から探した実体に当てる。tailscale が見つからなければ (None, None)。
    bbctl pair が使う（アプリは host が空の QR を断るので、有効で名前が取れなければ、符号を作る前に案内して終わる）。"""
    cmd = find_tailscale(config_value)
    return (None, None) if cmd is None else magicdns_of(cmd)


def magicdns_name(config_value: str | None = None) -> str | None:
    """この Mac の MagicDNS の名前（`tailscale status --json` の Self.DNSName。小文字・末尾の点なし）。
    取れない・名前の形でないなら None（サーバーは Host の見出しで名前を受けないだけ。bbctl pair は magicdns_status で
    有効かも見て、だめなら MagicDNS を有効にするよう案内して終わる）。"""
    return magicdns_status(config_value)[1]


# ---------------------------------------------------------------- 会話ログの作業フォルダ

HEAD_BYTES = 64 * 1024                                    # 会話ログの先頭からこれだけ読む（cwd は先頭の数行にある【実測】）
HEAD_LINES = 50                                           # 先頭のうち、見る行の数
MAX_FOLDERS = 300                                         # 集める作業フォルダの数の上限（新しい順）
_PATH_MAX = 4096


def _sig(path) -> tuple | None:
    """変化を見る印 (変更時刻 ns, 大きさ)。変更時刻（ctime）は touch・utime で戻せない。無い・読めないなら None。"""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_ctime_ns, st.st_size)


def _first_cwd(path: str) -> str | None:
    """会話ログの先頭の行にある、一番上の cwd。通常のファイルでなければ（FIFO・リンク）開かない・読まない。
    行が JSON として読めるものだけを使う（途中で切れた行・壊れた行は読めない）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        head = os.read(fd, HEAD_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    for line in head.split(b"\n")[:HEAD_LINES]:
        if b'"cwd"' not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        cwd = row.get("cwd") if isinstance(row, dict) else None
        if _usable_cwd(cwd):
            return cwd
    return None


def _usable_cwd(cwd) -> bool:
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > _PATH_MAX or "\0" in cwd:
        return False
    try:
        cwd.encode("utf-8")                               # 孤立サロゲートは後で鍵（UTF-8 にして SHA-256）にできない
    except UnicodeEncodeError:
        return False
    return True


class TranscriptFolders:
    """~/.claude/projects の会話ログから、Claude Code を使った作業フォルダ（cwd）を集める。読むだけ。

    1つの置き場のフォルダに複数の cwd が入りうる（日本語の名前は - に潰れて同じ置き場になる）ので、置き場ごとでなく
    会話ログ1本ごとに先頭の cwd を読む。読んだ答えは覚える：cwd が見つかったファイルは開き直さない（同じ inode の間）。
    cwd がまだ無いファイル（できたばかり）は、大きさが変わるまで開き直さない。

    refresh() は、前回の走査のあとで変わったときだけ走査し直す（走査したら True）。変化は、置き場とその中の各フォルダの
    変更時刻と大きさ（ctime。touch で戻せない）、cwd がまだ無いファイルの大きさで見る。毎秒呼んでも軽い（印を引くだけ）。
    folders は、新しい会話ログの順の、重複のない cwd（最大 max_folders）。
    """

    def __init__(self, projects_dir: Path, max_folders: int = MAX_FOLDERS):
        self._dir = Path(projects_dir)
        self._max = max_folders
        self._cache: dict[str, tuple] = {}                # 会話ログのパス → ((dev, ino), 大きさ, cwd | None)
        self._watch: list[str] = []
        self._key: tuple | None = None
        self.folders: list[str] = []

    def refresh(self) -> bool:
        if self._key is not None and self._key == tuple(_sig(p) for p in self._watch):
            return False
        self._scan()
        return True

    def _scan(self) -> None:
        watch: list[str] = [str(self._dir)]
        key: list = [_sig(self._dir)]                      # 印は、読む前に取る（読む間に変われば次の呼び出しで違いが出る）
        cache: dict[str, tuple] = {}
        found: dict[str, int] = {}                         # cwd → その会話ログの更新時刻 ns の最大
        try:
            with os.scandir(self._dir) as it:
                places = [e for e in it if _is_dir(e)]
        except OSError:
            places = []
        for place in places:
            watch.append(place.path)
            key.append(_sig(place.path))
            try:
                with os.scandir(place.path) as it:
                    files = [e for e in it if e.name.endswith(".jsonl") and _is_file(e)]
            except OSError:
                continue
            for e in files:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                ident = (st.st_dev, st.st_ino)
                hit = self._cache.get(e.path)
                if hit is not None and hit[0] == ident and (hit[2] is not None or hit[1] == st.st_size):
                    cwd = hit[2]
                else:
                    cwd = _first_cwd(e.path)
                cache[e.path] = (ident, st.st_size, cwd)
                if cwd is None:
                    watch.append(e.path)                   # できたばかり・まだ cwd が無い：大きさが変われば読み直す
                    key.append((st.st_ctime_ns, st.st_size))    # 読む前に引いた stat の印（読む間に伸びれば次で違いが出る）
                else:
                    found[cwd] = max(found.get(cwd, 0), st.st_mtime_ns)
        self._cache, self._watch, self._key = cache, watch, tuple(key)
        self.folders = sorted(found, key=lambda c: (-found[c], c))[:self._max]


def _is_dir(entry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _is_file(entry) -> bool:
    """通常のファイル（リンクをたどらない）。リンク・FIFO・フォルダの名前が .jsonl でも開かない。"""
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False
