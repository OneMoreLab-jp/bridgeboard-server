"""bblist/hub.py の試験。python3 -m unittest discover -s tests -t . -p 'test_hub.py' -v

Catalog・TranscriptStore は本物を tmp で使う（生死と procStart は test_sessions.py と同じく辞書で差し替える）。
時計（clock）は差し替え、step() を直接呼んで進める。待ちは入れない（糸の試験だけ、get の待ち時間で受け取る）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import unittest
from unittest import mock

from bblist.hub import Hub
from bblist.transcript import TranscriptStore
from bblist.usage import read_usage
from tests.test_sessions import CatalogCase

SESSION_KEYS = {"bridgeSessionId", "sessionId", "status", "waitingFor", "statusUpdatedAt"}


def parse(chunk: str):
    """SSE の1かたまり → (event の名前, data の JSON)。形が違えばここで落ちる。"""
    m = re.fullmatch(r"event: (\w+)\ndata: ([^\n]+)\n\n", chunk)
    assert m, repr(chunk)
    return m.group(1), json.loads(m.group(2))


def drain(sub) -> list:
    """いま待っている知らせを全部取る（待たない）。"""
    out = []
    while True:
        chunk = sub.get(0)
        if chunk is None:
            return out
        out.append(parse(chunk))


def names(sub) -> list:
    return [n for n, _ in drain(sub)]


class HubCase(CatalogCase):
    """tmp に claude_dir/projects と使用率のファイルを足した土台。"""

    def setUp(self):
        super().setUp()
        self.projects = self.claude / "projects"
        (self.projects / "folder").mkdir(parents=True)
        self.usage = self.own[0] / "usage.json"
        self.usage.parent.mkdir(parents=True)
        self.store = TranscriptStore(self.projects)
        self.now = 0.0

    def make_hub(self, **kw) -> Hub:
        kw.setdefault("debounce", 0.0)
        self.catalog = self.make_catalog()
        hub = Hub(self.catalog, self.store, self.usage, clock=lambda: self.now, **kw)
        self.addCleanup(hub.stop)
        return hub

    def log(self, sid: str):
        return self.projects / "folder" / f"{sid}.jsonl"

    def append(self, sid: str, text: str = "x\n") -> None:
        with open(self.log(sid), "a", encoding="utf-8") as f:
            f.write(text)

    def write_usage(self, captured: int) -> None:
        """別名で書いてから差し替える（糸の試験で、見回りが空にした直後のファイルを読んで None を知らせないように）。"""
        tmp = self.usage.with_name("usage.json.tmp")
        tmp.write_text(json.dumps({"capturedAt": captured, "rate_limits": {}}), encoding="utf-8")
        tmp.replace(self.usage)

    def at(self, hub: Hub, t: float) -> None:
        """時計を t にして見回りを1回。"""
        self.now = t
        hub.step()


class SubscriptionTest(HubCase):
    def test_chunk_format_and_timeout(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe(None)
        self.assertIsNone(sub.get(0))                         # 時間切れは None
        self.add(1, status="busy")
        self.at(hub, 1.0)
        chunk = sub.get(0)
        self.assertRegex(chunk, r'\Aevent: sessions\ndata: \{"generatedAt":\d+\}\n\n\Z')
        self.assertIsNone(sub.get(0))

    def test_max_clients_and_close_frees_slot(self):         # 閉じた後に空く・埋まっていれば一番古いものを切る
        hub = self.make_hub()
        subs = [hub.subscribe(None) for _ in range(4)]
        self.assertEqual([s.closed for s in subs], [False] * 4)
        fifth = hub.subscribe(None)                           # 断らない。一番古い購読を切って入れる（眠ったまま消えた iPad の枠）
        self.assertFalse(fifth.closed)
        self.assertEqual([s.closed for s in subs], [True, False, False, False])
        subs[2].close()
        self.assertTrue(subs[2].closed)
        sixth = hub.subscribe(None)                           # 閉じて空いた枠に入る。誰も切らない
        self.assertEqual([s.closed for s in subs], [True, False, True, False])
        hub.subscribe(None)                                   # また埋まっていた。残りのうち一番古い subs[1] を切る
        self.assertEqual([s.closed for s in subs], [True, True, True, False])
        self.assertFalse(fifth.closed or sixth.closed)

    def test_close_is_idempotent_and_does_not_free_a_second_slot(self):
        hub = self.make_hub(max_clients=2)
        a, b = hub.subscribe(None), hub.subscribe(None)
        a.close()
        a.close()                                             # 2度閉じても、空くのは1つだけ
        c = hub.subscribe(None)                               # 空いた1つに入る（b は切られない）
        self.assertFalse(b.closed)
        hub.subscribe(None)                                   # もう空きは無い。一番古い b が切られる
        self.assertTrue(b.closed)
        self.assertFalse(c.closed)

    def test_evicted_subscription_receives_nothing_more(self):
        self.add(1)
        hub = self.make_hub(max_clients=1)
        old = hub.subscribe(None)
        new = hub.subscribe(None)
        self.assertTrue(old.closed)
        self.add(1, status="busy")
        self.at(hub, 1.0)
        self.assertEqual(drain(old), [])
        self.assertEqual(names(new), ["sessions"])
        old.close()                                           # 切られた後に閉じても、ほかの購読の枠には触らない
        self.assertFalse(new.closed)
        hub.subscribe(None)
        self.assertTrue(new.closed)

    def test_closed_subscription_receives_nothing_more(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe(None)
        sub.close()
        self.add(1, status="busy")
        self.at(hub, 1.0)
        self.assertEqual(drain(sub), [])

    def test_empty_watch_means_no_watch(self):
        hub = self.make_hub()
        self.assertIsNone(hub.subscribe("").watch)
        self.assertEqual(hub.subscribe("session_1").watch, "session_1")

    def test_full_queue_drops_the_oldest(self):
        # 知らせに差分は無い。遅い相手が取りこぼしても、アプリは cursor で取り直して追いつく
        with mock.patch("bblist.hub.QUEUE_MAX", 3):
            hub = self.make_hub()
            sub = hub.subscribe(None)
        for i in range(1, 6):
            self.write_usage(1000 + i)
            self.at(hub, float(i))
        self.assertEqual([d["capturedAt"] for _, d in drain(sub)], [1003, 1004, 1005])

    def test_subscribe_and_close_from_many_threads_never_exceed_the_slots(self):
        hub = self.make_hub(max_clients=4)
        errors = []
        stop = threading.Event()

        def worker():
            try:
                for _ in range(50):
                    sub = hub.subscribe("session_1")          # 埋まっていても断られない（一番古いものが切られる）
                    sub.get(0)
                    sub.close()
            except Exception as e:                            # 試験の糸の中の例外は、控えて主の糸で落とす
                errors.append(repr(e))

        def watcher():
            # 白箱：購読の一覧を鍵の中で見る。枠の数を超えず、切った・閉じた購読が一覧に残らない
            while not stop.is_set():
                with hub._lock:
                    closed = [s.closed for s in hub._subs]
                if len(closed) > 4 or any(closed):
                    errors.append(closed)

        def stepper():
            while not stop.is_set():
                hub.step()

        helpers = [threading.Thread(target=stepper), threading.Thread(target=watcher)]
        for t in helpers:
            t.start()
        workers = [threading.Thread(target=worker) for _ in range(8)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        stop.set()
        for t in helpers:
            t.join()
        self.assertEqual(errors[:3], [])
        fresh = [hub.subscribe(None) for _ in range(4)]
        self.assertFalse(any(s.closed for s in fresh))        # 全部返っていて、4つとも空いている（誰も切られない）


class SessionEventsTest(HubCase):
    def test_sessions_event_on_status_change(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe(None)
        self.at(hub, 1.0)
        self.assertEqual(drain(sub), [])                      # 変わっていなければ知らせない
        self.add(1, status="waiting", waitingFor="approve")
        self.at(hub, 2.0)
        events = drain(sub)
        self.assertEqual([n for n, _ in events], ["sessions"])
        self.assertEqual(list(events[0][1]), ["generatedAt"])
        self.assertIsInstance(events[0][1]["generatedAt"], int)
        self.at(hub, 3.0)
        self.assertEqual(drain(sub), [])

    def test_sessions_event_on_added_and_removed_sessions(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe(None)
        self.add(2)
        self.at(hub, 1.0)
        self.assertEqual(names(sub), ["sessions"])
        (self.sessions / "2.json").unlink()
        self.at(hub, 2.0)
        self.assertEqual(names(sub), ["sessions"])

    def test_session_event_only_for_watched(self):
        self.add(1)
        self.add(2)
        hub = self.make_hub()
        a, a2, b, none = (hub.subscribe("session_1"), hub.subscribe("session_1"),
                          hub.subscribe("session_2"), hub.subscribe(None))
        self.add(2, status="waiting", waitingFor="approve", statusUpdatedAt=1792941999999)
        self.at(hub, 1.0)
        self.assertEqual(names(a), ["sessions"])              # B が変わっても A には session は来ない
        self.assertEqual(names(a2), ["sessions"])
        self.assertEqual(names(none), ["sessions"])
        events = drain(b)
        self.assertCountEqual([n for n, _ in events], ["sessions", "session"])
        data = dict(events)["session"]
        self.assertEqual(set(data), SESSION_KEYS)
        self.assertEqual(data, {"bridgeSessionId": "session_2", "sessionId": "sid-2", "status": "waiting",
                                "waitingFor": "approve", "statusUpdatedAt": 1792941999999})
        self.add(1, status="busy")
        self.at(hub, 2.0)
        for sub in (a, a2):                                   # 同じセッションを見ている購読は、どれにも来る
            self.assertCountEqual(names(sub), ["sessions", "session"])
        self.assertEqual(names(b), ["sessions"])
        self.assertEqual(names(none), ["sessions"])

    def test_session_event_on_session_id_change(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.add(1, sessionId="sid-1b")                       # /clear
        self.at(hub, 1.0)
        events = dict(drain(sub))
        self.assertEqual(events["session"]["sessionId"], "sid-1b")

    def test_session_event_ignores_changes_to_other_fields(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.add(1, name="renamed")                           # 名前は一覧（sessions）には効くが session の中身ではない
        self.at(hub, 1.0)
        self.assertEqual(names(sub), ["sessions"])

    def test_change_between_subscribe_and_first_step_is_not_lost(self):
        # つないでから取りに行くアプリが、つないだ直後の変化を取りこぼさないように、購読した時点の値を基準にする
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.append("sid-1")
        self.add(1, status="busy")
        self.at(hub, 1.0)
        self.assertCountEqual(names(sub), ["sessions", "session", "transcript"])

    def test_debounce_merges_session_changes_and_keeps_the_latest(self):
        self.add(1)
        hub = self.make_hub(debounce=0.3)
        sub = hub.subscribe("session_1")
        self.add(1, status="busy", statusUpdatedAt=1)
        self.at(hub, 1.0)
        self.add(1, status="waiting", waitingFor="approve", statusUpdatedAt=2)
        self.at(hub, 1.2)
        self.assertEqual(drain(sub), [])
        self.at(hub, 1.3)
        events = drain(sub)
        self.assertCountEqual([n for n, _ in events], ["sessions", "session"])
        self.assertEqual(dict(events)["session"]["status"], "waiting")
        self.assertEqual(dict(events)["session"]["statusUpdatedAt"], 2)


class TranscriptEventsTest(HubCase):
    def test_transcript_event_on_append_and_on_session_id_change(self):
        self.add(1)
        self.append("sid-1")
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.at(hub, 1.0)
        self.assertEqual(drain(sub), [])
        self.append("sid-1")
        self.at(hub, 2.0)
        self.assertEqual(drain(sub), [("transcript", {"bridgeSessionId": "session_1", "sessionId": "sid-1"})])
        self.at(hub, 3.0)
        self.assertEqual(drain(sub), [])
        self.add(1, sessionId="sid-1b")                       # /clear：新しい会話ログは同じフォルダに現れる
        self.append("sid-1b")
        self.at(hub, 4.0)
        events = drain(sub)
        self.assertIn(("transcript", {"bridgeSessionId": "session_1", "sessionId": "sid-1b"}), events)
        self.append("sid-1")                                  # 古い会話ログはもう見ない
        self.at(hub, 5.0)
        self.assertEqual(drain(sub), [])

    def test_session_id_change_alone_is_a_transcript_event(self):
        self.add(1)
        self.append("sid-1")
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.add(1, sessionId="sid-1b")                       # 新しい会話ログがまだ無くても、宛先が変わったことは知らせる
        self.at(hub, 1.0)
        self.assertIn(("transcript", {"bridgeSessionId": "session_1", "sessionId": "sid-1b"}), drain(sub))

    def test_transcript_event_when_the_log_first_appears(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.at(hub, 1.0)
        self.assertEqual(drain(sub), [])
        self.append("sid-1")
        self.at(hub, 2.0)
        self.assertEqual(names(sub), ["transcript"])

    def test_transcript_event_only_for_the_watcher(self):
        self.add(1)
        self.add(2)
        self.append("sid-1")
        self.append("sid-2")
        hub = self.make_hub()
        a, b, none = hub.subscribe("session_1"), hub.subscribe("session_2"), hub.subscribe(None)
        self.append("sid-2")
        self.at(hub, 1.0)
        self.assertEqual(names(a), [])
        self.assertEqual(names(none), [])
        self.assertEqual(drain(b), [("transcript", {"bridgeSessionId": "session_2", "sessionId": "sid-2"})])

    def test_logs_are_polled_only_for_watched_sessions(self):
        self.add(1)
        self.add(2)
        hub = self.make_hub()
        hub.subscribe("session_1")
        hub.subscribe(None)
        with mock.patch.object(self.store, "stat_key", wraps=self.store.stat_key) as spy:
            self.at(hub, 1.0)
            self.at(hub, 2.0)
        self.assertEqual({c.args[0] for c in spy.call_args_list}, {"sid-1"})
        self.assertEqual(len(spy.call_args_list), 2)          # 見回り1回につき、見られている1本を1回だけ

    def test_no_log_is_polled_without_watchers(self):
        self.add(1)
        hub = self.make_hub()
        hub.subscribe(None)
        with mock.patch.object(self.store, "stat_key", wraps=self.store.stat_key) as spy:
            self.at(hub, 1.0)
        spy.assert_not_called()

    def test_debounce_merges_changes_within_300ms(self):
        self.add(1)
        self.append("sid-1")
        hub = self.make_hub(debounce=0.3)
        sub = hub.subscribe("session_1")
        self.at(hub, 0.0)
        for t in (0.1, 0.2, 0.3):                             # 0.1秒おきに3回書き足す
            self.append("sid-1")
            self.at(hub, t)
            self.assertEqual(drain(sub), [], f"t={t}")        # 最初の変化から 0.3秒たつまでは出ない
        self.at(hub, 0.4)                                     # 最初に気づいた 0.1 から 0.3秒後の見回りで、1回だけ
        self.assertEqual(names(sub), ["transcript"])
        self.at(hub, 0.5)
        self.assertEqual(drain(sub), [])
        self.append("sid-1")                                  # まとめ終えたあとの変化は、また新しい1回になる
        self.at(hub, 0.6)
        self.assertEqual(drain(sub), [])
        self.at(hub, 0.9)
        self.assertEqual(names(sub), ["transcript"])

    def test_debounce_is_per_subscriber(self):
        self.add(1)
        self.append("sid-1")
        hub = self.make_hub(debounce=0.3)
        early = hub.subscribe("session_1")
        self.append("sid-1")
        self.at(hub, 0.1)                                     # early だけが変化に気づく
        late = hub.subscribe("session_1")                     # 変化のあとに入った購読は、その変化を受けない
        self.at(hub, 0.4)
        self.assertEqual(names(early), ["transcript"])
        self.assertEqual(names(late), [])

    def test_watched_session_ends(self):
        self.add(1)
        self.append("sid-1")
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        (self.sessions / "1.json").unlink()
        self.live.discard(1)
        with mock.patch.object(self.store, "stat_key", wraps=self.store.stat_key) as spy:
            self.at(hub, 1.0)                                 # 例外を出さない
            self.assertEqual(names(sub), ["sessions"])        # 一覧は変わったと知らせる
            self.append("sid-1")
            self.at(hub, 2.0)
            self.at(hub, 3.0)
        self.assertEqual(drain(sub), [])                      # 以後、会話の知らせは来ない
        spy.assert_not_called()                               # 会話ログも見に行かない

    def test_watched_process_gone_but_json_left_behind(self):
        self.add(1)
        self.append("sid-1")
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.live.discard(1)                                  # json は残り、プロセスだけいない
        self.at(hub, 1.0)
        self.assertEqual(names(sub), ["sessions"])
        self.append("sid-1")
        self.at(hub, 2.0)
        self.assertEqual(drain(sub), [])

    def test_unknown_watch_polls_nothing_until_it_appears(self):
        hub = self.make_hub()
        sub = hub.subscribe("session_9")
        with mock.patch.object(self.store, "stat_key", wraps=self.store.stat_key) as spy:
            self.at(hub, 1.0)
            self.at(hub, 2.0)
        self.assertEqual(drain(sub), [])
        spy.assert_not_called()
        self.add(9)
        self.append("sid-9")
        self.at(hub, 3.0)                                     # あとで現れたら、見ていた相手に知らせる
        self.assertCountEqual(names(sub), ["sessions", "session", "transcript"])


class UsageEventsTest(HubCase):
    def test_usage_event(self):
        hub = self.make_hub()
        sub = hub.subscribe(None)
        self.at(hub, 5.0)
        self.assertEqual(drain(sub), [])
        self.write_usage(1792941510)
        self.at(hub, 10.0)
        self.assertEqual(drain(sub), [("usage", {"capturedAt": 1792941510})])
        self.at(hub, 15.0)
        self.assertEqual(drain(sub), [])
        self.write_usage(1792941999)
        self.at(hub, 20.0)
        self.assertEqual(drain(sub), [("usage", {"capturedAt": 1792941999})])

    def test_usage_event_goes_to_every_subscriber_watching_or_not(self):
        self.add(1)
        hub = self.make_hub()
        a, b = hub.subscribe("session_1"), hub.subscribe(None)
        self.write_usage(1792941510)
        self.at(hub, 5.0)
        self.assertEqual(drain(a), [("usage", {"capturedAt": 1792941510})])
        self.assertEqual(drain(b), [("usage", {"capturedAt": 1792941510})])

    def test_usage_file_removed_or_broken_gives_null(self):
        self.write_usage(1792941510)
        hub = self.make_hub()
        sub = hub.subscribe(None)
        self.usage.write_text("{broken", encoding="utf-8")
        self.at(hub, 5.0)
        self.assertEqual(drain(sub), [("usage", {"capturedAt": None})])
        self.usage.unlink()
        self.at(hub, 10.0)
        self.assertEqual(drain(sub), [("usage", {"capturedAt": None})])

    def test_usage_file_existing_at_start_is_not_a_change(self):
        self.write_usage(1792941510)
        hub = self.make_hub()
        sub = hub.subscribe(None)
        self.at(hub, 5.0)
        self.assertEqual(drain(sub), [])


class MidStepJoinTest(HubCase):
    """見回りの途中でつないだ購読にも、その回の sessions・usage を届ける。

    変わったことは見回りで1度しか分からない（refresh() の戻り値・使用率の鍵）。つないだ購読が、見回りの始めに取った
    一覧に入っていないと、アプリが（つないでから）取りに行く前の値を持ったまま、その知らせを永久に受け損なう。
    """

    def test_sessions_event_reaches_a_subscriber_that_joins_during_refresh(self):
        self.add(1)
        hub = self.make_hub()
        box = {}
        real = self.catalog.refresh

        def refresh():
            box["plain"] = hub.subscribe(None)                # アプリがつなぐ（一覧を取るのは、差し替えの前）
            box["watcher"] = hub.subscribe("session_1")
            return real()

        self.add(1, status="busy")
        with mock.patch.object(self.catalog, "refresh", side_effect=refresh):
            self.at(hub, 1.0)
        self.assertEqual(names(box["plain"]), ["sessions"])
        self.assertCountEqual(names(box["watcher"]), ["sessions", "session"])
        self.at(hub, 2.0)
        self.assertEqual(drain(box["plain"]), [])             # 1度きり

    def test_usage_event_reaches_a_subscriber_that_joins_before_the_stat(self):
        self.write_usage(1000)
        hub = self.make_hub()
        box = {}
        real = self.catalog.refresh

        def refresh():
            box["sub"] = hub.subscribe(None)
            box["fetched"] = json.loads(self.usage.read_text(encoding="utf-8"))["capturedAt"]   # 取りに行く
            self.write_usage(2000)                            # そのあとで使用率のファイルが書き換わる
            return real()

        with mock.patch.object(self.catalog, "refresh", side_effect=refresh):
            self.at(hub, 1.0)
        self.assertEqual(box["fetched"], 1000)
        self.assertEqual(drain(box["sub"]), [("usage", {"capturedAt": 2000})])

    def test_usage_event_reaches_a_subscriber_that_joins_between_the_stat_and_the_fan_out(self):
        self.write_usage(1000)
        hub = self.make_hub()
        box = {}
        real = read_usage

        def read(path):
            out = real(path)
            box["sub"] = hub.subscribe(None)                  # 鍵を見たあと・知らせを配る前につなぐ
            return out

        self.write_usage(2000)
        with mock.patch("bblist.hub.read_usage", side_effect=read):
            self.at(hub, 1.0)
        self.assertEqual(drain(box["sub"]), [("usage", {"capturedAt": 2000})])


class LoopTest(HubCase):
    def test_step_survives_failures_and_logs_only_the_type_name(self):
        self.add(1)
        hub = self.make_hub()
        sub = hub.subscribe("session_1")
        self.write_usage(1792941510)
        with mock.patch.object(self.store, "stat_key", side_effect=RuntimeError("secret-content")):
            with self.assertLogs("bblist.hub", level="WARNING") as cm:
                self.at(hub, 1.0)                             # 会話ログの失敗で、見回りは止まらない
        self.assertEqual(drain(sub), [("usage", {"capturedAt": 1792941510})])
        text = "\n".join(cm.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("secret-content", text)

    def test_thread_polls_each_kind_by_its_own_interval(self):
        hub = self.make_hub(poll_sessions=3600.0, poll_log=3600.0, poll_usage=0.0)
        sub = hub.subscribe(None)
        hub.start()
        self.write_usage(1792941510)
        self.assertEqual(parse(sub.get(5.0)), ("usage", {"capturedAt": 1792941510}))
        self.assertIsNone(sub.get(0))

    def test_start_stop(self):
        self.add(1)
        hub = Hub(self.make_catalog(), self.store, self.usage, poll_sessions=0.0, poll_log=0.0, poll_usage=0.0,
                  debounce=0.0)                               # 本物の時計
        self.addCleanup(hub.stop)
        sub = hub.subscribe(None)
        before = threading.active_count()
        hub.start()
        hub.start()                                           # 2度呼んでも糸は1本
        self.assertEqual(threading.active_count(), before + 1)
        self.add(1, status="busy")
        self.assertEqual(parse(sub.get(5.0))[0], "sessions")
        hub.stop()
        self.assertEqual(threading.active_count(), before)
        hub.stop()                                            # 2度止めてもよい
        self.add(1, status="waiting")
        self.assertIsNone(sub.get(0.3))                       # 止めたあとは何も来ない

    def counting_hub(self, **kw):
        """本物の時計の Hub と、見回り（_run）の時刻の並び・見回りを終えた合図。"""
        hub = Hub(self.make_catalog(), self.store, self.usage, **kw)
        self.addCleanup(hub.stop)
        runs, woke, real = [], threading.Event(), hub._run

        def counted(force):
            runs.append(time.monotonic())
            real(force)
            woke.set()

        hub._run = counted
        return hub, runs, woke

    def test_thread_sleeps_until_the_next_poll_without_subscribers(self):
        # 購読が無ければ、起きるのは sessions の間隔ごとだけ。会話ログの間隔では起きない（見張りが無い）。
        # 決まった短い間隔（以前は 0.1 秒）でも起きない。何も開いていない Mac を起こさないため
        hub, runs, _ = self.counting_hub(poll_sessions=0.25, poll_log=0.02, poll_usage=1.0, debounce=0.0)
        hub.start()
        time.sleep(1.0)
        hub.stop()
        self.assertGreaterEqual(len(runs), 3, runs)
        self.assertLessEqual(len(runs), 7, runs)              # 最初の1回 ＋ 1.0/0.25。0.1 秒ごとなら 10、会話ログの間隔なら 50

    def test_subscribe_close_and_stop_wake_the_thread_at_once(self):
        hub, runs, woke = self.counting_hub(poll_sessions=3600.0, poll_log=3600.0, poll_usage=3600.0, debounce=0.0)
        hub.start()
        self.assertTrue(woke.wait(5.0))                       # 最初の見回り
        woke.clear()
        sub = hub.subscribe("session_1")                      # 次に見るのは 3600 秒後だが、購読が変われば起きる
        self.assertTrue(woke.wait(1.0))
        woke.clear()
        sub.close()
        self.assertTrue(woke.wait(1.0))
        t0 = time.monotonic()
        hub.stop()
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_watcher_wakes_the_thread_for_the_log_and_for_the_debounced_event(self):
        # 見張りがいれば会話ログの間隔で起き、まとめ終えた知らせは出す時刻に起きて出す（sessions の 3600 秒を待たない）
        self.add(1)
        self.append("sid-1")
        hub, runs, _ = self.counting_hub(poll_sessions=3600.0, poll_log=0.05, poll_usage=3600.0, debounce=0.2)
        sub = hub.subscribe("session_1")
        hub.start()
        self.append("sid-1")
        t0 = time.monotonic()
        self.assertEqual(parse(sub.get(5.0)), ("transcript", {"bridgeSessionId": "session_1", "sessionId": "sid-1"}))
        self.assertLess(time.monotonic() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
