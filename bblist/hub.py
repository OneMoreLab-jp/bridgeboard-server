"""見回りと知らせ（GET /v1/events の中身）。

Python 3.9 の標準ライブラリだけで動かす。

見回りは1本の糸（Hub.start）。sessions/*.json（Catalog.refresh）・見張られているセッションの会話ログ
（TranscriptStore.stat_key）・使用率のファイルを見て、変わったものを購読ごとの待ち行列へ SSE の1かたまりとして置く。
知らせには差分を入れない。アプリは知らせを受けたら、持っている cursor で会話の道から取り直す。
会話の本文は読まない。記録には失敗の型の名前だけを書く。

糸をまたぐのは購読の一覧（_subs）と購読の closed だけで、_lock で守る。ほかに、眠っている見回りの糸を起こす合図
（_wake、threading.Event）を subscribe・close・stop が立てる。購読ごとの見張りの状態（_row・_mark・_pending）は
見回りの糸だけが触る。要求を受ける糸が触るのは subscribe・Subscription.get・closed・close だけ。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

from .usage import read_usage

log = logging.getLogger(__name__)

QUEUE_MAX = 100                    # 購読ごとの待ち行列の長さ
MIN_WAIT = 0.01                    # 糸が続けて起きるときの最短の間（秒）。間隔 0 の試験で糸が回り続けないように
FALLBACK_WAIT = 0.1                # 次に起きる時刻を求められなかったときの待ち（秒）


def _chunk(name: str, data: dict) -> str:
    """SSE の1かたまり。json.dumps は改行を出さず、ASCII だけにするので、data の行は1本に収まる。"""
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _fields(row: dict | None):
    """session の知らせの中身になる値。一覧に無ければ None。"""
    if row is None:
        return None
    return tuple(row.get(k) for k in ("sessionId", "status", "waitingFor", "statusUpdatedAt"))


class Subscription:
    """SSE 1本ぶんの待ち行列。Hub.subscribe が作る。"""

    def __init__(self, hub: Hub, watch: str | None):
        self.watch = watch
        self._hub = hub
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._row = None               # 前に見た watch のセッションの値（session の知らせの元）
        self._mark = None              # 前に見た (sessionId, 会話ログの stat_key)（transcript の知らせの元）
        self._pending: dict[str, list] = {}   # 知らせの名前 → [出す時刻, 中身]（まとめている間）
        self._closed = False           # 書くのは Hub の _lock の中だけ（一覧から外すのと一緒に）

    def get(self, timeout: float) -> str | None:
        """次の知らせ（SSE の1かたまり）。timeout 秒たっても無ければ None。閉じた後も待ち時間いっぱい待つ。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def closed(self) -> bool:
        """閉じたか（close を呼んだ、または枠が埋まって新しい購読に追い出された）。閉じたら知らせはもう来ない。"""
        return self._closed

    def close(self) -> None:
        """枠を返す。何度呼んでもよい。追い出された後に呼んでも、ほかの購読の枠には触らない。"""
        self._hub._release(self)

    def _put(self, chunk: str) -> None:
        """待ち行列に置く。満杯（遅い相手・眠った相手）なら古い方を捨てる。

        知らせには差分が無い。捨てても、アプリは次の知らせか取り直しで cursor から追いつく。
        置くのは見回りの糸だけなので、1つ空ければ必ず入る。
        """
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                pass


class Hub:
    """sessions・会話ログ・使用率を見回って、購読へ知らせる。"""

    def __init__(self, catalog, store, usage_path: Path, poll_sessions: float = 1.0, poll_log: float = 0.5,
                 poll_usage: float = 5.0, debounce: float = 0.3, max_clients: int = 4, clock=time.monotonic):
        self._catalog = catalog
        self._store = store
        self._usage_path = Path(usage_path)
        self._every = {"sessions": poll_sessions, "log": poll_log, "usage": poll_usage}
        self._debounce = debounce
        self._max = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._next: dict[str, float] = {}          # 見る種類 → 次に見る時刻
        self._usage_key = self._usage_stat()
        self._stop = threading.Event()
        self._wake = threading.Event()             # 購読が増えた・減った・止める。眠っている糸を起こす
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 要求を受ける糸から

    def subscribe(self, watch: str | None) -> Subscription:
        """購読を1つ作る。watch は見張るセッションの bridgeSessionId（無ければ None）。

        枠（max_clients）が埋まっていれば、一番古い購読を閉じて（closed にして）入れる。断らない。眠ったまま消えた
        iPad の接続は、TCP があきらめるまで（数分）ping の書き込みが失敗せず枠を握り続けるので、断ると目覚めた
        iPad が締め出される。追い出された購読の SSE は、closed を見て ping の間隔のうちに抜ける。
        """
        watch = watch or None
        sub = Subscription(self, watch)
        with self._lock:
            while self._subs and len(self._subs) >= self._max:
                oldest = self._subs.pop(0)             # 一覧は購読した順
                oldest._closed = True
                log.info("Notification slots are full: closed the oldest subscription")
            if watch is not None:
                # 購読した時点の値を基準にする。つないでから取りに行くアプリが、つないだ直後の変化を取りこぼさない
                try:
                    row = self._catalog.session(watch)
                    sub._row, sub._mark = _fields(row), self._mark_of(row)
                except Exception as e:             # 基準が取れなければ空のまま（最初の見回りで余分に1回知らせるだけ）
                    log.warning("Could not read the subscription baseline: %s", type(e).__name__)
            self._subs.append(sub)
        self._wake.set()                               # 見張りが増えたら、会話ログの間隔で起きるように眠り直させる
        return sub

    def _release(self, sub: Subscription) -> None:
        with self._lock:
            sub._closed = True                         # 一覧から外すのと同じ鍵の中で（一覧に閉じた購読を残さない）
            try:
                self._subs.remove(sub)
            except ValueError:
                pass
        self._wake.set()

    # ---------------------------------------------------------------- 糸

    def start(self) -> None:
        """見回りの糸を始める。間隔が来た種類だけ見て、次に何かが来る時刻（_wait_for）まで眠る。"""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bblist-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join()

    def _loop(self) -> None:
        """何も開いていないときは sessions の間隔（1秒）ごとにだけ起きる（決まった短い間隔で起き続けて、何も開いていない Mac を起こさない）。
        起こす合図（_wake）は見回りの前に下ろす。見回りの間に購読が変われば、合図が残って眠らずにもう1回見る。"""
        while not self._stop.is_set():
            self._wake.clear()
            self._run(force=False)
            if self._stop.is_set():
                break
            try:
                wait = self._wait_for(self._clock())
            except Exception as e:                     # 求められなくても糸は止めない
                log.warning("Watch round failed: wait %s", type(e).__name__)
                wait = FALLBACK_WAIT
            self._wake.wait(wait)

    def _wait_for(self, now: float) -> float:
        """次に起きるまでの秒数。次の sessions・使用率の見回り、見張りがあれば次の会話ログの見回り、
        まとめている知らせのうち一番早く出すもの、のうち最も早い時刻まで（MIN_WAIT より短くはしない）。"""
        subs = self._snapshot()
        kinds = ("sessions", "usage", "log") if any(s.watch is not None for s in subs) else ("sessions", "usage")
        times = [self._next.get(k, now) for k in kinds]
        times += [due for sub in subs for due, _ in sub._pending.values()]
        return max(MIN_WAIT, min(times) - now)

    # ---------------------------------------------------------------- 見回り

    def step(self) -> None:
        """見回り1回分。間隔にかかわらず3種類とも見て、まとめ終えた知らせを出す。例外は出さない。"""
        self._run(force=True)

    def _run(self, force: bool) -> None:
        now = self._clock()
        for kind, look in (("sessions", self._look_sessions), ("log", self._look_logs),
                           ("usage", self._look_usage)):
            if self._due(kind, now, force):
                self._guard(kind, look, now)
        self._guard("flush", self._flush, now)

    def _snapshot(self) -> list[Subscription]:
        """いまの購読の写し。変わったことは見回りで1度しか分からない（sessions は refresh() の戻り値、usage は鍵）ので、
        配る相手は、変化を見つけたあとに取る。見回りの始めに取ると、途中でつないだ購読が、その回の知らせを永久に
        受け損なう（アプリはつないでから取りに行くが、取りに行った時点では古い値のことがある）。
        見つけたあとにつないだ購読は、取りに行って新しい値を得るので、知らせなくてよい。"""
        with self._lock:
            return list(self._subs)

    def _due(self, kind: str, now: float, force: bool) -> bool:
        nxt = self._next.get(kind)
        if not force and nxt is not None and now < nxt:
            return False
        self._next[kind] = now + self._every[kind]
        return True

    @staticmethod
    def _guard(kind: str, fn, now: float) -> None:
        """1つの失敗で糸を止めない。記録に出すのは型の名前だけ（メッセージには本文や道が入りうる）。"""
        try:
            fn(now)
        except Exception as e:
            log.warning("Watch round failed: %s %s", kind, type(e).__name__)

    def _look_sessions(self, now: float) -> None:
        """一覧が変われば sessions。watch のセッションの値が変われば、その購読に session。"""
        changed = self._catalog.refresh()
        data = {"generatedAt": self._catalog.overview()["generatedAt"]} if changed else None
        subs = self._snapshot()                # refresh のあとに取る（_snapshot を見よ）
        if data is not None:
            for sub in subs:
                self._note(sub, "sessions", data, now)
        rows: dict[str, dict | None] = {}
        for sub in subs:
            if sub.watch is None:
                continue
            if sub.watch not in rows:
                rows[sub.watch] = self._catalog.session(sub.watch)
            fields = _fields(rows[sub.watch])
            if fields == sub._row:
                continue
            sub._row = fields
            if fields is not None:             # 一覧から消えたときは sessions だけで足りる
                sid, status, waiting_for, updated = fields
                self._note(sub, "session", {"bridgeSessionId": sub.watch, "sessionId": sid, "status": status,
                                            "waitingFor": waiting_for, "statusUpdatedAt": updated}, now)

    def _look_logs(self, now: float) -> None:
        """watch されているセッションの会話ログだけ見る。大きさ・更新時刻・inode か sessionId が変われば transcript。"""
        marks: dict[str, tuple | None] = {}
        for sub in self._snapshot():
            if sub.watch is None:
                continue
            if sub.watch not in marks:
                marks[sub.watch] = self._mark_of(self._catalog.session(sub.watch))
            mark = marks[sub.watch]
            if mark == sub._mark:
                continue
            sub._mark = mark
            if mark is not None:               # 終わったセッションの会話ログは見ない・知らせない
                self._note(sub, "transcript", {"bridgeSessionId": sub.watch, "sessionId": mark[0]}, now)

    def _mark_of(self, row: dict | None):
        """(sessionId, 会話ログの stat_key)。セッションが一覧に無ければ None（会話ログも見に行かない）。"""
        if row is None:
            return None
        sid = row.get("sessionId")
        return sid, self._store.stat_key(sid)

    def _look_usage(self, now: float) -> None:
        key = self._usage_stat()
        if key == self._usage_key:
            return
        self._usage_key = key
        data = {"capturedAt": read_usage(self._usage_path).get("capturedAt")}     # 読めなければ None
        for sub in self._snapshot():           # 鍵を見て読んだあとに取る（_snapshot を見よ）
            self._note(sub, "usage", data, now)

    def _usage_stat(self):
        try:
            st = self._usage_path.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    # ---------------------------------------------------------------- まとめて出す

    def _note(self, sub: Subscription, name: str, data: dict, now: float) -> None:
        """変化を覚える。気づいた時刻から debounce 後の見回りで出す。その間の同じ知らせは1つにまとめる（中身は最新）。"""
        slot = sub._pending.get(name)
        if slot is None:
            sub._pending[name] = [now + self._debounce, data]
        else:
            slot[1] = data

    def _flush(self, now: float) -> None:
        for sub in self._snapshot():
            for name, (due, data) in list(sub._pending.items()):
                if due <= now:
                    del sub._pending[name]
                    sub._put(_chunk(name, data))
