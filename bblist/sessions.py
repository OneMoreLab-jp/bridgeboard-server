"""セッションの一覧とプロジェクト（GET /v1/sessions の中身）。

Python 3.9 の標準ライブラリだけで動かす。

`~/.claude/sessions/<PID>.json` だけを読む（glob は *.json だけ。隣の <PID>.<64桁>.key は開かない）。
フォルダ名を文字列のまま比べない。比べるのは folder_id（realpath を NFC にそろえた鍵）か、NFC にそろえた後。
"""
from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

from . import gitcmd
from .discover import TranscriptFolders
from .paths import deny_stamp, files_visible, folder_id, nfc


def ps_start(pid: int) -> str | None:
    """プロセスの起動時刻（UTC）。`TZ=UTC ps -o lstart= -p <pid>` の空白を詰めたもの。無ければ None。

    LC_ALL=C は、月・曜日の綴りを sessions/*.json の procStart（英語）にそろえるため。
    """
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                           env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}, timeout=5)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    out = _squash(r.stdout)
    return out if r.returncode == 0 and out else None


def run_git(cwd: str) -> tuple[str, str] | None:
    """`git -C <cwd> rev-parse --path-format=absolute --show-toplevel --git-common-dir` の2行。git でなければ None。
    git の呼び方（設定の命令を走らせない・GIT_ の環境変数を渡さない）は gitignore と同じ bblist/gitcmd.py。"""
    try:
        r = gitcmd.run(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"], cwd, timeout=10)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    lines = os.fsdecode(r.stdout).splitlines()
    return (lines[0], lines[1]) if r.returncode == 0 and len(lines) == 2 else None


def _squash(s) -> str:
    """空白（改行を含む）の連なりを1つの空白にする。ps は1桁の日の前に空白を2つ出すため。"""
    return " ".join(s.split()) if isinstance(s, str) else ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:       # 別のユーザーのプロセス。生きてはいる（procStart の照合で外れる）
        return True
    except (OSError, OverflowError, ValueError):   # 大きすぎる PID は C の int に入らず OverflowError
        return False
    return True


_GONE = object()                  # _read の答え：読む前に消えた（終わったセッションが自分の json を消した）。壊れたものではない
MAX_VERSIONS = 16                 # 診断に載せる Claude Code の版の数と長さの上限（持ち主が自由に書けるファイルの値）
VERSION_LEN = 40


def _usable(o) -> bool:
    """sessions/*.json の中身が、生死を確かめられる形か（pid と cwd の型と値の範囲だけ。bridgeSessionId・procStart は別）。"""
    if not isinstance(o, dict):
        return False
    pid, cwd = o.get("pid"), o.get("cwd")
    if (not isinstance(pid, int) or isinstance(pid, bool)
            or not 0 < pid <= 2 ** 31 - 1                      # kill(0, 0) は自分のグループに届く。pid_t は 32 ビット
            or not isinstance(cwd, str) or not cwd or "\0" in cwd):
        return False
    try:
        cwd.encode("utf-8")                                    # 孤立サロゲートは folder_id（UTF-8 にして SHA-256）が落ちる
    except UnicodeEncodeError:
        return False
    return True


def _has_bridge(o: dict) -> bool:
    """Remote Control の bridgeSessionId（空でない文字列）があるか。無ければ claude.ai から開けないので、一覧に載せない。"""
    bridge = o.get("bridgeSessionId")
    return isinstance(bridge, str) and bool(bridge)


def _version_key(v: str) -> list:
    """版の並べ方：数字の連なりは数として（2.9.0 < 2.10.0）、ほかは文字として。"""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", v)]


def _num(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


class Catalog:
    """起動中のセッションとプロジェクトの一覧。糸をまたいで呼ばれる。

    読み直し（refresh）は同時に1本だけ（_refresh_lock）。値は _lock で守り、組み立ては _lock の外でやって、
    できたものを一度に差し替える。読む側が読み直しの間じゅう待たされない。
    読む側（overview・session・root_path・visible_roots）は、中の値そのものでなく写しを返す。
    """

    def __init__(self, claude_dir: Path, launcher_roots, home: Path, own_dirs: list[Path],
                 start_of=ps_start, git=run_git, alive=None, *, secret: bytes, launcher_auto: bool = False):
        """launcher_roots は [(起点のフォルダ, 深さ 1|2), …]（設定の launcherRoots。bblist/config.py）。深さ 2 は
        <起点>/<分類>/<フォルダ>、深さ 1 は <起点>/<フォルダ> がランチャーの行。空で launcher_auto が真なら、会話ログ
        （claude_dir/projects）の作業フォルダ（cwd）のうち見せてよいものを出す。空で launcher_auto が偽なら、ランチャーは空
        （セッションのあるフォルダだけがプロジェクトになる）。"""
        self._secret = secret                                      # rootId の塩（secrets_store）。必ず渡す（既定は無い）
        self._sessions_dir = Path(claude_dir) / "sessions"
        self._launcher_roots = [(Path(r), int(d)) for r, d in launcher_roots]
        self._transcripts = (TranscriptFolders(Path(claude_dir) / "projects")
                             if launcher_auto and not self._launcher_roots else None)
        self._home = Path(home)
        # 見せない場所：置き場・記録に加えて、Claude の設定フォルダそのもの（CLAUDE_CONFIG_DIR で ~/.claude の外にも置ける）
        self._own_dirs = list(own_dirs) + [Path(claude_dir)]
        self._deny = None                                          # 見せない場所の実体の印（deny_stamp）。変われば覚えを捨てる
        self._start_of = start_of
        self._git = git
        self._alive = alive or _alive
        self._places: dict[str, tuple] = {}                        # 作業フォルダ → 置き場所（git の答え。git でない答えも覚える）
        self._idents: dict[tuple, tuple] = {}                      # (フォルダのパス, worktree の親か None) → ((dev, ino)…, (鍵, 見せてよいか, カテゴリ))
        self._files: dict[Path, tuple[tuple[int, int], object]] = {}   # sessions/*.json → ((大きさ, 更新時刻 ns), 読んだ中身)
        self._verdicts: dict[int, tuple[str, bool]] = {}           # PID → (確かめた procStart, ps と一致したか)
        self._launcher_key = None                                  # ランチャーを数えたときの変更時刻（数え直すかの鍵）
        self._launcher_dirs: list[str] = []                        # そのとき時刻を取ったフォルダ（起点・分類・各フォルダ）
        self._launcher_rows: list[tuple] = []                      # そのときのフォルダの並び（_launcher_listed を見よ）
        self._auto_key = None                                      # 自動のランチャー：作業フォルダの変更時刻（同じ）
        self._auto_paths: list[str] = []
        self._auto_rows: list[tuple] = []
        self._refresh_lock = threading.Lock()
        self._lock = threading.Lock()
        self._generated_at = 0
        self._diag = {"sessions": dict.fromkeys(("files", "malformed", "dead", "procMismatch", "noBridge"), 0),
                      "claudeCodeVersions": []}                    # 直近の見回りの数（diagnostics）
        self._hidden: frozenset = frozenset()                      # 見せない（filesVisible: false）起点の rootId（root_hidden）
        self._projects: list[dict] = []
        self._sessions: list[dict] = []
        self._by_bridge: dict[str, dict] = {}
        self._roots: dict[str, Path] = {}
        self.refresh()

    # ---------------------------------------------------------------- 読む側

    def overview(self) -> dict:
        """GET /v1/sessions の JSON（セッションとプロジェクトの一覧）。"""
        with self._lock:
            return copy.deepcopy({"generatedAt": self._generated_at,
                                  "projects": self._projects, "sessions": self._sessions})

    def session(self, bridge_id: str) -> dict | None:
        """一覧の1行。起動中でなければ None。"""
        with self._lock:
            row = self._by_bridge.get(bridge_id)
            return copy.deepcopy(row) if row else None

    def root_path(self, root_id: str) -> Path | None:
        """見せてよい（filesVisible）起点のフォルダだけ。"""
        with self._lock:
            return self._roots.get(root_id)

    def visible_roots(self) -> dict[str, Path]:
        with self._lock:
            return dict(self._roots)

    def root_hidden(self, root_id: str) -> bool:
        """一覧に出ているが、ファイルを見せない起点か（root_path が None の理由が「見せない」か「知らない」かを分ける）。"""
        with self._lock:
            return root_id in self._hidden

    def diagnostics(self) -> dict:
        """直近の見回りで数えた sessions/*.json の形（GET /v1/diagnostics の sessions と claudeCodeVersions）。

        files = malformed + dead + noBridge + procMismatch + 一覧に出る数（同じ bridge の二重起動を整理する前）。
        どれか1つだけに数える。順は malformed（読めない・pid か cwd の形が違う）、dead（kill -0 が通らない）、
        noBridge（生きているが bridgeSessionId が無い。ps は聞かない）、procMismatch（生きているが procStart が無い・ps と合わない・
        ps が答えない・ps の問い合わせが例外を出した）。pid・cwd の形が分かった後の例外は malformed でなく procMismatch。
        ファイルを数え直さない：数えるのは見回りのときで、ここは覚えた数の写しを返すだけ。"""
        with self._lock:
            return copy.deepcopy(self._diag)

    # ---------------------------------------------------------------- 読み直す側

    def refresh(self) -> bool:
        """sessions/ とランチャーを読み直す。generatedAt を除いた中身が変わったら True。"""
        with self._refresh_lock:
            live, diag = self._live_sessions()
            projects, sessions, roots, hidden = self._build(live)
            with self._lock:
                changed = projects != self._projects or sessions != self._sessions
                self._projects, self._sessions, self._roots = projects, sessions, roots
                self._hidden, self._diag = hidden, diag
                self._by_bridge = {s["bridgeSessionId"]: s for s in sessions}
                self._generated_at = int(time.time() * 1000)
            return changed

    def _live_sessions(self) -> tuple[list[dict], dict]:
        """(生きていて procStart が合い、bridgeSessionId のある sessions/*.json（読み込んだままの辞書）, 診断の数)。

        毎秒の見回りを軽くするため、変わっていないものはやり直さない。
        - json は、大きさと更新時刻が前回と同じなら読み直さない（_read）
        - ps は、(PID, procStart) の組ごとに1回だけ聞く。以後の見回りは kill -0 だけ
        診断の数（diagnostics を見よ）は、どのファイルも1つの区分に数える。数えるだけで、新しくファイルを開いたり ps を呼んだりしない。
        """
        out = []
        counts = dict.fromkeys(("files", "malformed", "dead", "procMismatch", "noBridge"), 0)
        versions = set()
        try:
            files = sorted(self._sessions_dir.glob("*.json"))
        except OSError:
            files = []
        listed = set(files)
        self._files = {f: v for f, v in self._files.items() if f in listed}      # 消えたファイルを忘れる
        pids = set()
        for f in files:
            kind = "malformed"                  # 形が分かる前（_usable を通る前）に例外が出たものは、壊れたものとして数える
            try:
                o = self._read(f)
                if o is _GONE:
                    continue
                if isinstance(o, dict) and isinstance(o.get("version"), str) and o["version"] and len(versions) < 4 * MAX_VERSIONS:
                    versions.add(o["version"][:VERSION_LEN])
                if _usable(o):
                    kind = "procMismatch"       # 形は合っている。この先の例外（ps の問い合わせなど）は「確かめられなかった」
                    pids.add(o["pid"])
                    if not self._alive(o["pid"]):
                        self._verdicts.pop(o["pid"], None)
                        kind = "dead"
                    elif not _has_bridge(o):
                        kind = "noBridge"
                    elif self._matches_ps(o["pid"], _squash(o.get("procStart"))):
                        kind = None
                        out.append(o)
                    else:
                        kind = "procMismatch"
            except Exception:                   # 1本の不具合で見回り全体を止めない。中身は記録に出さない
                self._files.pop(f, None)
            counts["files"] += 1
            if kind is not None:
                counts[kind] += 1
        self._verdicts = {p: v for p, v in self._verdicts.items() if p in pids}
        found = sorted(versions, key=_version_key)[-MAX_VERSIONS:]
        return out, {"sessions": counts, "claudeCodeVersions": found}

    def _read(self, f: Path):
        """sessions/<PID>.json 1本を読む。読めない・書きかけは None（覚えず、次の見回りで読み直す）。読む前に消えていたら _GONE。

        大きさと更新時刻が前回と同じなら、開き直さず前回の中身を返す。
        先に stat して後で読む。読む間に書き換わっても、覚える key は古い方なので、次の見回りで違いが出て読み直す。
        """
        try:
            st = f.stat()
        except OSError:
            self._files.pop(f, None)
            return _GONE
        key = (st.st_size, st.st_mtime_ns)
        hit = self._files.get(f)
        if hit is not None and hit[0] == key:
            return hit[1]
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
            # あとで JSON にして返せない中身（孤立サロゲート・NaN）は、壊れたものとして扱う
            json.dumps(o, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (OSError, ValueError):             # UnicodeEncodeError は ValueError の仲間
            self._files.pop(f, None)
            return None
        self._files[f] = (key, o)
        return o

    def _matches_ps(self, pid: int, proc: str) -> bool:
        """（生きている PID について）記録の procStart が ps の起動時刻と合うか（PID の使い回しを外す）。

        合う・合わないの答えは (PID, procStart) ごとに覚える。PID が終われば（呼び出し側が）忘れる。
        ps が何も答えなかったとき（終わる直前など）は覚えず、次の見回りで聞き直す。
        """
        if not proc:
            self._verdicts.pop(pid, None)
            return False
        known = self._verdicts.get(pid)
        if known is None or known[0] != proc:
            started = _squash(self._start_of(pid))
            if not started:
                self._verdicts.pop(pid, None)
                return False
            known = self._verdicts[pid] = (proc, started == proc)
        return known[1]

    def _build(self, live: list[dict]):
        """(プロジェクトの並び, セッションの並び, 見せてよい起点, 見せない起点の rootId の集まり) を組み立てる。"""
        # 二重起動：同じ sessionId を持つ生きたプロセスが2つ以上（同じ bridge の古い方も数える）
        counts: dict = {}
        for o in live:
            if isinstance(o.get("sessionId"), str):
                counts[o["sessionId"]] = counts.get(o["sessionId"], 0) + 1
        # 同じ bridgeSessionId は updatedAt の新しい方（同じなら PID の大きい方）だけを出す
        newest: dict[str, dict] = {}
        for o in live:
            cur = newest.get(o["bridgeSessionId"])
            if cur is None or (_num(o.get("updatedAt")), o["pid"]) > (_num(cur.get("updatedAt")), cur["pid"]):
                newest[o["bridgeSessionId"]] = o

        deny = deny_stamp(self._home, self._own_dirs)
        if deny != self._deny:                  # ~/.claude の張り替えなどで見せない場所が動いた：覚えた「見せてよいか」を引き直す
            self._deny, self._idents, self._launcher_key, self._auto_key = deny, {}, None, None
        projects: dict[str, dict] = {}
        roots: dict[str, Path] = {}
        known: dict[tuple, tuple] = {}          # この見回りで引いたフォルダ。鍵は (パス, worktree の親か None)

        def identify(path: str, info: tuple | None = None, root: Path | None = None, base: str | None = None) -> tuple:
            # info・root はランチャーが前に引いた答え（あれば引き直さない）。base は worktree の起点のときの親
            key = (path, base)
            if key not in known:
                known[key] = info if info is not None else self._identify(path, base)
                if known[key][1]:
                    roots.setdefault(known[key][0], root if root is not None else Path(path))
            return known[key]

        def add_project(path: str, category, is_git: bool) -> str:
            pid, visible, _ = identify(path)
            if pid in projects:
                projects[pid]["isGit"] = projects[pid]["isGit"] or is_git
                return pid
            base = os.path.basename(path.rstrip("/")) or path
            projects[pid] = {"id": pid, "rootId": pid, "name": nfc(base), "category": category,
                             "path": path, "isGit": is_git, "filesVisible": visible, "sessionCount": 0}
            return pid

        # ランチャー：設定の起点の下のフォルダをすべて（起点が無ければ会話ログの作業フォルダ）。セッションが無くても出す
        for category, path, is_git, info, root in self._launcher():
            identify(path, info, root)
            add_project(path, category, is_git)

        rows = []
        for o in newest.values():
            try:
                project_path, root_path, worktree, is_git = self._place(o["cwd"])
                project_id, _, category = identify(project_path)
                root_id, root_visible, _ = identify(root_path, base=project_path if worktree is not None else None)
                name = o.get("name")
                row = {
                    "bridgeSessionId": o["bridgeSessionId"], "url": f"https://claude.ai/code/{o['bridgeSessionId']}",
                    "pid": o["pid"], "sessionId": o.get("sessionId"), "projectId": project_id, "rootId": root_id,
                    "name": nfc(name) if isinstance(name, str) else name, "cwd": o["cwd"], "worktree": worktree,
                    "status": o.get("status"), "waitingFor": o.get("waitingFor"),
                    "statusUpdatedAt": o.get("statusUpdatedAt"), "updatedAt": o.get("updatedAt"),
                    "entrypoint": o.get("entrypoint"),
                    "duplicate": isinstance(o.get("sessionId"), str) and counts[o["sessionId"]] > 1,
                    "filesVisible": root_visible,       # rootId のファイルを見せてよいか（プロジェクトの行と別に持つ）
                }
            except Exception:                   # この行だけ飛ばす。プロジェクトの数にも入れない（先に全部を組み、あとで足す）
                continue
            add_project(project_path, category, is_git)
            projects[project_id]["sessionCount"] += 1
            rows.append(row)
        # 使わなくなったものを忘れる（作業フォルダの数だけ増え続けない）
        self._idents = {k: v for k, v in self._idents.items() if k in known}
        cwds = {o["cwd"] for o in newest.values()}
        self._places = {c: v for c, v in self._places.items() if c in cwds}
        rows.sort(key=lambda s: (-_num(s["updatedAt"]), s["pid"]))
        ordered = sorted(projects.values(), key=lambda p: (p["name"], p["id"]))
        hidden = frozenset(v[0] for v in known.values() if not v[1]) - roots.keys()   # 同じ rootId が見せてよい側にもあれば見せる
        return ordered, rows, roots, hidden

    def _identify(self, path: str, base: str | None = None) -> tuple:
        """フォルダの (鍵, 見せてよいか, 起点の下のカテゴリ名)。

        base は worktree の起点のときの親のプロジェクト（files_visible の trusted_base）。
        どれも realpath を引く重い仕事なので、フォルダ（base があれば base も）の (dev, ino) が前回と同じ間は前回の答えを使う。
        シンボリックリンクの付け替え・作り直しは (dev, ino) が変わるので引き直す。
        カテゴリは、深さ 2 の起点の下で2段以上深いときだけ、直下のフォルダ名。外・起点の直下・深さ 1 の起点の下は None。
        """
        key = (path, base)
        try:
            st = os.stat(path)
            stamp = (st.st_dev, st.st_ino)
            if base is not None:
                sb = os.stat(base)
                stamp += (sb.st_dev, sb.st_ino)
        except (OSError, ValueError):             # 無くなったフォルダは覚えない
            stamp = None
        hit = self._idents.get(key)
        if stamp is not None and hit is not None and hit[0] == stamp:
            return hit[1]
        real = Path(nfc(os.path.realpath(path))).parts
        category = None
        for root, depth in self._launcher_roots:
            if depth == 2:
                lab = Path(nfc(os.path.realpath(root))).parts
                if len(real) >= len(lab) + 2 and real[:len(lab)] == lab:
                    category = real[len(lab)]
                    break
        visible = files_visible(Path(path), self._home, self._own_dirs,
                                trusted_base=Path(base) if base is not None else None)
        info = (folder_id(path, self._secret), visible, category)
        if stamp is None:
            self._idents.pop(key, None)
        else:
            self._idents[key] = (stamp, info)
        return info

    def _launcher(self) -> list[tuple]:
        """ランチャーのフォルダの並び：(カテゴリ名 NFC | None, フォルダのパス, git か, _identify の答え, 起点の Path)。"""
        if self._launcher_roots:
            return self._launcher_listed()
        if self._transcripts is not None:
            return self._launcher_auto()
        return []                                  # 起点が無い：ランチャーは空

    def _launcher_listed(self) -> list[tuple]:
        """設定の起点の下のフォルダ。どちらの段も . で始まる名前とフォルダでないものは除く。

        毎秒の見回りを軽くするため（何も変わっていない Mac で CPU を使い続けない）、起点・各カテゴリ・各フォルダの変更時刻（ctime）が前回数えたときと
        同じ間は、数え直さず前回の並びを使う。フォルダの足し引きはカテゴリの、カテゴリの足し引きは起点の、
        .git の出し入れ（git init）はそのフォルダの時刻を変える。時刻はそれぞれのフォルダを読む前に取るので、
        読む間に変われば、次の見回りで時刻が違って数え直す。読めないフォルダがあった回は覚えない（毎回数え直す）。
        更新時刻（mtime）でなく変更時刻にするのは、touch -r・utime で戻せる mtime では、フォルダの足し引きや
        chmod・rename に気づけないことがあるため。
        """
        if self._launcher_key is not None and self._launcher_key == self._launcher_stamp():
            return self._launcher_rows
        complete = True
        dirs, times, rows = [], [], []

        def note(path: str) -> None:
            dirs.append(path)
            times.append(_ctime(path))

        def children(path: str):
            nonlocal complete
            try:
                with os.scandir(path) as it:
                    return [e for e in it if not e.name.startswith(".") and _is_dir(e)]
            except OSError:
                complete = False
                return []

        for root, depth in self._launcher_roots:
            note(str(root))
            if depth == 1:
                for f in children(root):
                    note(f.path)
                    rows.append((None, f.path, os.path.lexists(os.path.join(f.path, ".git")),
                                 self._identify(f.path), Path(f.path)))
                continue
            for c in children(root):
                note(c.path)
                for f in children(c.path):
                    note(f.path)
                    rows.append((nfc(c.name), f.path, os.path.lexists(os.path.join(f.path, ".git")),
                                 self._identify(f.path), Path(f.path)))
        self._launcher_dirs, self._launcher_rows = dirs, rows
        self._launcher_key = tuple(times) if complete else None
        return rows

    def _launcher_stamp(self) -> tuple:
        """前回数えたフォルダの、いまの変更時刻（_launcher_key と同じ形）。"""
        return tuple(_ctime(d) for d in self._launcher_dirs)

    def _launcher_auto(self) -> list[tuple]:
        """起点が設定されていないとき：会話ログの作業フォルダ（cwd）のうち、あって、見せてよいもの（ホームの中・. で始まらない・
        ~/Library などでない。_identify の答え）。分類は無い（None）。

        会話ログは持ち主が書き換えられるが、出るのは見せてよい規則を通ったフォルダだけ。走査は discover.TranscriptFolders
        （変わったときだけ。毎秒は印を引くだけ）。行を組み直すのは、走査し直したときか、作業フォルダの変更時刻が動いたとき
        （フォルダが消えた・作られた）。
        """
        scanned = self._transcripts.refresh()
        if not scanned and self._auto_key is not None and self._auto_key == tuple(_ctime(p) for p in self._auto_paths):
            return self._auto_rows
        paths = list(self._transcripts.folders)
        times = tuple(_ctime(p) for p in paths)                    # フォルダを読む前に取る
        rows = []
        for path in paths:
            if not os.path.isdir(path):
                continue
            info = self._identify(path)
            if info[1]:
                rows.append((None, path, os.path.lexists(os.path.join(path, ".git")), info, Path(path)))
        self._auto_paths, self._auto_rows, self._auto_key = paths, rows, times
        return rows

    def _place(self, cwd: str):
        """作業フォルダ → (プロジェクトのフォルダ, 起点のフォルダ, worktree の名前, git か)。

        プロジェクトは git のルート。worktree は親（--git-common-dir の親）に入れ、起点は worktree そのもの。
        git でなければ、また git のルートに .git（ファイルかフォルダ）が無ければ、作業フォルダそのもの。
        答えは作業フォルダごとに1回だけ求める（git を見回りのたびには叩かない）。
        """
        if cwd not in self._places:
            self._places[cwd] = self._find_place(cwd)
        return self._places[cwd]

    def _find_place(self, cwd: str):
        found = self._git(cwd)
        if found is None:
            return cwd, cwd, None, False
        top, common = found
        common = common.rstrip("/")
        # git の答え（toplevel）は、.git/config の core.worktree や .git ファイルの gitdir: で、持ち主が好きな場所へ向けられる。
        # toplevel の .git が、git が実際に使っているもの（common-dir）につながると確かめられなければ、信じて起点を広げない
        # （隣のプロジェクトまで見えてしまう）。作業フォルダに縮める（まれな正当な使い方も同じ）。git が答えた場所なので isGit は真
        kind = _git_entry_kind(top, common)
        if kind is None:
            return cwd, cwd, None, True
        # worktree は、.git のファイルが <common>/worktrees/<名前> を指し、その gitdir が .git へ戻る本物だけ。サブモジュールや
        # --separate-git-dir（kind は repo）、core.worktree で作業コピーを .git などに向けただけのもの（worktree として扱うと .git/config まで
        # 見せてしまう）は worktree と見なさない。common-dir の名前が .git でないのは bare の worktree。その親は使わない
        if kind == "worktree" and os.path.basename(common) == ".git":
            parent = os.path.dirname(common)
            if folder_id(parent, self._secret) != folder_id(top, self._secret):
                return parent, top, nfc(os.path.basename(top.rstrip("/"))), True
        return top, top, None, True


def _same(a, b) -> bool:
    """同じフォルダ・ファイルか（実体 (st_dev, st_ino) で比べる）。どちらかが無い・引けなければ False。"""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _read_small(path) -> bytes | None:
    """通常のファイルの先頭 4096 バイト。通常のファイルでない（FIFO・デバイスなど）・読めないなら None。

    持ち主が自由に作れるフォルダの中のファイルを読むので、大きくは読まず、開くだけで止まるものは開かない。
    FIFO を O_NONBLOCK なしで開くと、書き手が来るまで止まる（一覧の糸が止まり、起動時なら待ち受けに進めない）。
    O_NONBLOCK で開いて fstat で種類を確かめてから読む（シンボリックリンクの先が FIFO でも同じ）。
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)


def _first_line(path) -> str | None:
    """通常のファイルの最初の行（先頭 4096 バイトまで）。読めない・空なら None。"""
    data = _read_small(path)
    lines = data.splitlines() if data else []
    return os.fsdecode(lines[0]) if lines else None


def _git_entry_kind(top: str, common: str) -> str | None:
    """git の toplevel の .git が、git が実際に使っている common-dir につながっているか。

    - "repo"：.git がフォルダ（リンクでもよい）で common-dir と同じ実体、または .git ファイル（`gitdir: <パス>`）の行き先が
      common-dir（サブモジュール・--separate-git-dir）。プロジェクトも起点も toplevel そのもので、広がらない
    - "worktree"：.git ファイルの行き先が <common-dir>/worktrees/<名前> で、その gitdir ファイルが戻る先が toplevel の .git
      （`git worktree add` が作る本物。パスは相対でも絶対でもよい）。プロジェクトが親のリポジトリに上がるのは、これだけ
    - None：そのほか（.git が無い・行き先が違う・戻ってこない）

    worktree の「戻る先」は、実体の比べ（samefile）でなく、書かれたパスで見る（名前が .git で、その親が toplevel）。
    実体で比べると、本物の worktree の .git ファイルへのシンボリックリンクやハードリンクを自分の .git にした
    フォルダが、その worktree と区別できず、親のリポジトリを見せてしまう。リンクの .git ファイルは worktree にしない
    （`git worktree add` はいつも通常のファイルを書く）。
    """
    entry = os.path.join(top, ".git")
    if os.path.isdir(entry):
        return "repo" if _same(entry, common) else None
    if not os.path.isfile(entry):
        return None
    line = _first_line(entry)
    if line is None or not line.startswith("gitdir:"):
        return None
    target = os.path.join(top, line[len("gitdir:"):].strip())      # 相対なら .git ファイルのあるフォルダから
    if _same(target, common):
        return "repo"
    if os.path.islink(entry):
        return None
    if _same(os.path.dirname(target.rstrip("/")), os.path.join(common, "worktrees")):
        back = _first_line(os.path.join(target, "gitdir"))
        if back is not None:
            pointed = os.path.join(target, back).rstrip("/")
            if os.path.basename(pointed) == ".git" and _same(os.path.dirname(pointed), top):
                return "worktree"
    return None


def _is_dir(entry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _ctime(path) -> int | None:
    """フォルダの変更時刻（ns。シンボリックリンクは先を見る）。無い・読めないなら None。
    ctime はフォルダの足し引き・rename・chmod・utime で動き、touch では戻せない（更新時刻 mtime は戻せる）。"""
    try:
        return os.stat(path).st_ctime_ns
    except (OSError, ValueError):
        return None
