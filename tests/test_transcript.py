"""bblist/transcript.py の試験。python3 -m unittest discover -s tests -t . -p 'test_transcript.py' -v

tmp に projects/<フォルダ>/<sid>.jsonl を作り、手本（fixtures/*.jsonl）の行を書き足しながら読む。
「最初から読んだもの」は、何も覚えていない新しい TranscriptStore で読んだ結果と比べる（同じ store だと覚えた単位を返すだけになる）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bblist.transcript import TranscriptStore, _wanted, decode_cursor, encode_cursor
from bblist.units import DRAWN_TYPES, KNOWN_TYPES, UnitBuilder
from tests.make_fixture import PNG_1x1

FIX = Path(__file__).resolve().parent / "fixtures"
FIXTURES = ["tmux_rc.jsonl", "tmux_rc_cleared.jsonl", "vscode.jsonl", "compact_error.jsonl", "image_only.jsonl"]
SID = "600fbcff-0000-4000-8000-000000000001"
TS = "2026-10-27T01:00:00.000Z"
KEYS = {"sessionId", "cursor", "reset", "items", "updates", "lastRecordAt", "pendingCall", "queue"}
STATE_KEYS = ("lastRecordAt", "pendingCall", "queue")
# 読み飛ばしの目印（どれも含まない行は JSON に直さない）
MARKS = (b'"type":"user"', b'"type":"assistant"', b'"type":"attachment"', b'"type":"system"',
         b'"type":"queue-operation"')


def fixture_lines(name: str) -> list[bytes]:
    return (FIX / name).read_bytes().splitlines(keepends=True)


def line(row: dict) -> bytes:
    """Claude Code と同じ詰めた形（区切りに空白なし）の1行。"""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def human(uuid: str, text: str) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": TS, "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}}


def ask(tid: str) -> dict:
    return {"type": "assistant", "uuid": f"a-{tid}", "timestamp": TS, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": {"questions": [{"question": "色は"}]}}]}}


def answer(tid: str, value: dict) -> dict:
    return {"type": "user", "uuid": f"r-{tid}", "timestamp": TS, "toolUseResult": value,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def b64(obj) -> str:
    """cursor の形（URL で使える base64・= を外す）で任意の値を包む。壊れた cursor を作るため。"""
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def ids(r: dict) -> list:
    return [it["id"] for it in r["items"]]


class StoreCase(unittest.TestCase):
    """tmp に projects/<フォルダ>/ を作る土台。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.projects = Path(self._tmp.name) / "projects"
        self.folder = self.projects / "-fixture-project"
        self.folder.mkdir(parents=True)
        self.store = TranscriptStore(self.projects)

    def log(self, sid: str = SID) -> Path:
        return self.folder / f"{sid}.jsonl"

    def write(self, data: bytes, sid: str = SID) -> None:
        with open(self.log(sid), "wb") as f:
            f.write(data)

    def append(self, data: bytes, sid: str = SID) -> None:
        with open(self.log(sid), "ab") as f:
            f.write(data)

    def full(self, sid: str = SID) -> dict:
        """何も覚えていない store で最初から読んだ結果。"""
        return TranscriptStore(self.projects).fetch(sid, None)


class FetchTest(StoreCase):
    def test_no_cursor_returns_all_without_reset(self):
        data = (FIX / "tmux_rc.jsonl").read_bytes()
        self.write(data)
        r = self.store.fetch(SID, None)
        self.assertEqual(set(r), KEYS)
        self.assertEqual((r["sessionId"], r["reset"], r["updates"]), (SID, False, []))
        want = UnitBuilder()
        off, rows = 0, []
        for ln in fixture_lines("tmux_rc.jsonl"):
            rows.append(json.loads(ln))
            want.feed(rows[-1], off)
            off += len(ln)
        self.assertEqual(r["items"], want.items_since(0))
        self.assertEqual((r["pendingCall"], r["queue"]), (want.state()["pendingCall"], want.state()["queue"]))
        # lastRecordAt は読み飛ばした行も含めた最後の行の時刻（一番上に timestamp のある最後の行）
        stamped = [o["timestamp"] for o in rows if isinstance(o.get("timestamp"), str)]
        self.assertEqual(r["lastRecordAt"], stamped[-1])
        self.assertEqual(decode_cursor(r["cursor"]), (SID, self.log().stat().st_ino, len(data)))
        # 同じ求めをもう一度しても、同じ行を2回読まない
        self.assertEqual(self.store.fetch(SID, None), r)

    def test_cursor_chain_equals_full_read(self):
        # 手本を半行ずつ書き足し、そのたびに前の cursor で取る。つないだ items（updates を当てた後）＝ 最初から読んだ items
        for name in FIXTURES:
            with self.subTest(name=name):
                sid = "chain-" + name.split(".")[0]
                self.write(b"", sid)
                r = self.store.fetch(sid, None)
                got, cursor, n_updates = [], r["cursor"], 0
                self.assertEqual(r["items"], [])
                for ln in fixture_lines(name):
                    for part in (ln[:len(ln) // 2], ln[len(ln) // 2:]):
                        self.append(part, sid)
                        r = self.store.fetch(sid, cursor)
                        self.assertFalse(r["reset"])
                        by_id = {it["id"]: it for it in got}
                        for u in r["updates"]:              # 前に渡した質問の変化だけが来る
                            by_id[u["id"]]["answer"] = u["answer"]
                            n_updates += 1
                        self.assertFalse({it["id"] for it in r["items"]} & by_id.keys())
                        got += r["items"]
                        cursor = r["cursor"]
                full = self.full(sid)
                self.assertTrue(full["items"])
                self.assertEqual(got, full["items"])
                self.assertEqual({k: r[k] for k in STATE_KEYS}, {k: full[k] for k in STATE_KEYS})
                self.assertEqual(cursor, full["cursor"])
                if name == "tmux_rc.jsonl":                 # AskUserQuestion 3問の答えは後の行で来る
                    self.assertEqual(n_updates, 3)

    def test_partial_last_line_waits_for_newline(self):
        a, b = line(human("u1", "一つ目")), line(human("u2", "二つ目"))
        self.write(a + b[:10])
        r = self.store.fetch(SID, None)
        self.assertEqual(ids(r), ["u1"])
        self.assertEqual(decode_cursor(r["cursor"])[2], len(a))
        again = self.store.fetch(SID, r["cursor"])
        self.assertEqual((again["items"], again["cursor"]), ([], r["cursor"]))
        self.append(b[10:])
        r2 = self.store.fetch(SID, r["cursor"])
        self.assertEqual((ids(r2), r2["reset"]), (["u2"], False))
        self.assertEqual(decode_cursor(r2["cursor"])[2], len(a + b))

    def test_broken_line_is_skipped_and_logged_without_content(self):
        secret = "見せてはいけない本文"
        data = (line(human("u1", "一つ目"))
                + b'{"type":"user","message":"' + secret.encode("utf-8") + b'\n'      # JSON に直せない行
                + line({"type": "user", "uuid": "x", "message": secret})               # 形の崩れた行
                + line(human("u2", "二つ目")))
        self.write(data)
        with self.assertLogs("bblist.transcript", "WARNING") as cm:
            r = self.store.fetch(SID, None)
        self.assertEqual(ids(r), ["u1", "u2"])
        self.assertEqual(decode_cursor(r["cursor"])[2], len(data))
        out = "\n".join(cm.output)
        self.assertNotIn(secret, out)
        self.assertRegex(out, r"\b2\b")             # 読めなかった行の数


class LastRecordAtTest(StoreCase):
    def test_trailing_skipped_lines_update_last_record_at(self):
        # API に問い合わせ中は最後の行が api-request（読み飛ばす行）。その時刻まで進む
        self.write(line(human("u1", "一つ目")))
        r = self.store.fetch(SID, None)
        self.assertEqual(r["lastRecordAt"], TS)
        t2, t3 = "2026-10-27T01:00:05.000Z", "2026-10-27T01:00:09.000Z"
        skipped = [
            line({"type": "api-request", "sessionId": SID, "id": "req1", "timestamp": t2,
                  "params": {"model": "m", "thinking": {"type": "adaptive"}}}),
            # 一番上に timestamp が無く、入れ子にだけある（古い時刻）→ 取らない【実測：file-history-snapshot】
            line({"type": "file-history-snapshot", "messageId": "m1",
                  "snapshot": {"messageId": "m1", "trackedFileBackups": {}, "timestamp": "2026-10-26T00:00:00.000Z"},
                  "isSnapshotUpdate": True}),
            # 文字列の中の timestamp（エスケープされた引用符）→ 取らない
            line({"type": "api-request-blob", "sessionId": SID, "hash": "h",
                  "message": {"role": "user", "content": '{"timestamp":"2026-10-25T00:00:00.000Z"}'}}),
        ]
        self.assertFalse(any(m in ln for ln in skipped for m in MARKS))
        self.append(b"".join(skipped))
        with mock.patch("bblist.transcript.json.loads", wraps=json.loads) as loads:
            r = self.store.fetch(SID, r["cursor"])
        self.assertFalse([c for c in loads.call_args_list if c.args[0] in skipped])   # 読み飛ばしは保ったまま
        self.assertEqual((r["items"], r["lastRecordAt"]), ([], t2))
        # 一番上の timestamp の前に辞書がある行も取る【実測：file-history-delta】。文字列の中の括弧は数えない
        self.append(line({"type": "file-history-delta", "messageId": "m2", "trackingPath": "p{[x",
                          "backup": {"version": 1, "backupTime": "2026-10-26T00:00:00.000Z"}, "timestamp": t3}))
        r = self.store.fetch(SID, r["cursor"])
        self.assertEqual(r["lastRecordAt"], t3)
        self.append(line({"type": "mode", "timestamp": "x" * 100}))     # 時刻にしては長すぎる値は取らない
        r = self.store.fetch(SID, r["cursor"])
        self.assertEqual(r["lastRecordAt"], t3)
        self.assertEqual(self.full()["lastRecordAt"], t3)   # 最初から読んでも同じ


class ResetTest(StoreCase):
    def rows(self, n: int, prefix: str = "u") -> list[bytes]:
        return [line(human(f"{prefix}{k}", f"指示{k}")) for k in range(n)]

    def test_reset_when_truncated(self):
        lines = self.rows(3)
        self.write(b"".join(lines))
        c = self.store.fetch(SID, None)["cursor"]
        ino = self.log().stat().st_ino
        with open(self.log(), "r+b") as f:
            f.truncate(len(lines[0]))
        self.assertEqual(self.log().stat().st_ino, ino)     # 同じファイルが短くなっただけ
        r = self.store.fetch(SID, c)
        self.assertEqual((r["reset"], ids(r)), (True, ["u0"]))
        self.assertEqual(ids(self.store.fetch(SID, None)), ["u0"])   # 覚えた単位も読み直している
        r2 = self.store.fetch(SID, r["cursor"])
        self.assertEqual((r2["reset"], r2["items"]), (False, []))

    def test_reset_when_inode_changes(self):
        self.write(b"".join(self.rows(2)))
        c = self.store.fetch(SID, None)["cursor"]
        old = self.log().stat().st_ino
        new = self.folder / "new.tmp"                  # 中身の違う別のファイルに置き換えて rename（前より大きい）
        new.write_bytes(b"".join(self.rows(3, "v")))
        os.replace(new, self.log())
        self.assertNotEqual(self.log().stat().st_ino, old)
        r = self.store.fetch(SID, c)
        self.assertEqual((r["reset"], ids(r)), (True, ["v0", "v1", "v2"]))
        self.assertEqual(r["cursor"], self.full()["cursor"])
        # 時刻のある行が1つも無いファイルに置き換えたら、前のファイルの時刻を残さない
        new.write_bytes(line({"type": "mode", "mode": "normal"}))
        os.replace(new, self.log())
        r = self.store.fetch(SID, r["cursor"])
        self.assertEqual((r["reset"], r["items"], r["lastRecordAt"]), (True, [], None))

    def test_reset_when_session_id_differs(self):
        # /clear：sessionId が変わり、同じフォルダの新しい会話ログに移る
        self.write((FIX / "tmux_rc.jsonl").read_bytes(), "sid-before")
        self.write((FIX / "tmux_rc_cleared.jsonl").read_bytes(), "sid-after")
        c = self.store.fetch("sid-before", None)["cursor"]
        r = self.store.fetch("sid-after", c)
        self.assertEqual((r["sessionId"], r["reset"]), ("sid-after", True))
        self.assertEqual(r["items"], self.full("sid-after")["items"])

    def test_reset_when_cursor_beyond_size(self):
        data = b"".join(self.rows(2))
        self.write(data)
        ino = self.log().stat().st_ino
        r = self.store.fetch(SID, encode_cursor(SID, ino, len(data) + 1))
        self.assertEqual((r["reset"], ids(r)), (True, ["u0", "u1"]))
        r = self.store.fetch(SID, encode_cursor(SID, ino, len(data)))
        self.assertEqual((r["reset"], r["items"]), (False, []))

    def test_garbage_cursor_is_treated_as_none(self):
        self.write(b"".join(self.rows(2)))
        ino = self.log().stat().st_ino
        bad = ["", "garbage", "!!!", "%%%", "é", "a" * 5000, b64(b"not json"), b64(b"\xff\xfe"),
               b64(b"[" * 3000), b64(b"[" * 150), b64({"s": SID}), b64([SID, 1]), b64([SID, "1", 2]), b64([SID, ino, -1]),
               b64([SID, True, 0]), b64([SID, ino, 1.5]), b64([1, ino, 0]), b64(None)]
        for c in bad:
            with self.subTest(c=c[:20]):
                self.assertIsNone(decode_cursor(c))
                r = self.store.fetch(SID, c)
                self.assertEqual((r["reset"], ids(r)), (False, ["u0", "u1"]))
        self.assertIsNone(decode_cursor(None))

    def test_cursor_round_trip_is_url_safe(self):
        for sid, ino, pos in [(SID, 123456789012, 0), ("a_b-C", 0, 137364822), ("x" * 80, 2 ** 63, 2 ** 40)]:
            c = encode_cursor(sid, ino, pos)
            self.assertRegex(c, r"\A[A-Za-z0-9_-]+\Z")
            self.assertEqual(decode_cursor(c), (sid, ino, pos))


class MissingAndLruTest(StoreCase):
    def test_missing_log_returns_empty_with_cursor(self):
        r = self.store.fetch(SID, None)
        self.assertEqual(r, {"sessionId": SID, "cursor": r["cursor"], "reset": False, "items": [], "updates": [],
                             "lastRecordAt": None, "pendingCall": None, "queue": {"count": 0, "texts": []}})
        self.assertEqual(decode_cursor(r["cursor"]), (SID, 0, 0))
        self.assertFalse(self.store.fetch(SID, r["cursor"])["reset"])
        # 書かれたら最初から全部（「ファイルが無い」と inode が違う）
        self.write(line(human("u1", "一つ目")))
        r2 = self.store.fetch(SID, r["cursor"])
        self.assertEqual((r2["reset"], ids(r2)), (True, ["u1"]))
        # また消えたら空に戻る。覚えた単位も捨てる
        self.log().unlink()
        r3 = self.store.fetch(SID, r2["cursor"])
        self.assertEqual((r3["reset"], r3["items"], decode_cursor(r3["cursor"])), (True, [], (SID, 0, 0)))
        # projects のフォルダが無くても、開けない会話ログでも空（落ちない）
        self.assertEqual(TranscriptStore(self.projects / "無い").fetch(SID, None)["items"], [])
        self.write(line(human("u1", "一つ目")))
        os.chmod(self.log(), 0)
        self.addCleanup(os.chmod, self.log(), 0o600)
        self.assertEqual(self.store.fetch(SID, None)["items"], [])
        self.assertIsNone(self.store.image(SID, "u1", 0))

    def test_vanished_log_frees_its_slot(self):
        store = TranscriptStore(self.projects, max_sessions=2)
        for sid in ("gone", "kept", "new"):
            self.write(line(human(f"{sid}-1", "一つ目")), sid)
        with mock.patch("bblist.transcript.UnitBuilder", wraps=UnitBuilder) as made:
            store.fetch("kept", None)
            store.fetch("gone", None)
            self.log("gone").unlink()
            store.fetch("gone", None)                  # 消えた会話ログの分を捨てる
            store.fetch("new", None)                   # 空いた枠に入り、kept は捨てない
            n = made.call_count
            store.fetch("kept", None)
            self.assertEqual(made.call_count, n)

    def test_lru_drops_oldest_and_rereads(self):
        store = TranscriptStore(self.projects, max_sessions=2)
        a, b, c = "lru-a", "lru-b", "lru-c"
        for sid, name in [(a, "tmux_rc.jsonl"), (b, "vscode.jsonl"), (c, "compact_error.jsonl")]:
            self.write((FIX / name).read_bytes(), sid)
        with mock.patch("bblist.transcript.UnitBuilder", wraps=UnitBuilder) as made:
            first = {sid: store.fetch(sid, None) for sid in (a, b, c)}     # 3本目で a を捨てる
            self.assertEqual(made.call_count, 3)
            store.fetch(b, None)                       # b は覚えている（いちばん新しく使った）
            self.assertEqual(made.call_count, 3)
            again = store.fetch(a, None)               # a は最初から読む。いちばん長く使っていない c を捨てる
            self.assertEqual(made.call_count, 4)
            self.assertEqual(again, first[a])
            store.fetch(b, None)
            self.assertEqual(made.call_count, 4)
            store.fetch(c, None)
            self.assertEqual(made.call_count, 5)
        # 捨てる前の cursor もそのまま使える（位置はファイルの中のもの）
        r = store.fetch(a, first[a]["cursor"])
        self.assertEqual((r["reset"], r["items"]), (False, []))

    def test_answer_after_drop_comes_as_update(self):
        store = TranscriptStore(self.projects, max_sessions=1)
        self.write(line(ask("toolu_q")))
        c = store.fetch(SID, None)["cursor"]
        self.write(line(human("o1", "別")), "other")
        store.fetch("other", None)                     # SID を捨てる
        self.append(line(answer("toolu_q", {"answers": {"色は": "青"}})))
        r = store.fetch(SID, c)
        self.assertEqual((r["reset"], r["items"]), (False, []))
        self.assertEqual(r["updates"], [{"kind": "question", "id": "toolu_q", "answer": {"answers": {"色は": "青"}}}])


class ImageWrittenTest(StoreCase):
    def test_image_bytes_decoded_from_row(self):
        self.write((FIX / "vscode.jsonl").read_bytes())
        items = self.full()["items"]
        with_images = [it for it in items if it["kind"] == "instruction" and it["images"]]
        self.assertEqual(len(with_images), 2)          # queued_command の prompt と user の content
        for it in with_images:                         # fetch の前でも、画像の道だけで読める
            mt, data = self.store.image(SID, it["id"], 0)
            self.assertEqual((mt, data), ("image/png", PNG_1x1))
            self.assertIsNone(self.store.image(SID, it["id"], 9))
            self.assertIsNone(self.store.image(SID, it["id"], -1))
        plain = next(it for it in items if it["kind"] == "instruction" and not it["images"])
        self.assertIsNone(self.store.image(SID, plain["id"], 0))
        self.assertIsNone(self.store.image(SID, "無い id", 0))
        self.assertIsNone(self.store.image("無いセッション", with_images[0]["id"], 0))

    def test_image_from_rewritten_line_is_refused(self):
        # 同じファイルが同じ大きさで書き換えられた（inode も大きさも変わらない）とき、覚えた位置の行が別の指示なら渡さない
        self.write((FIX / "vscode.jsonl").read_bytes())
        it = next(i for i in self.store.fetch(SID, None)["items"] if i["kind"] == "instruction" and i["images"]
                  and not i["queued"])
        data = self.log().read_bytes()
        mine = f'"uuid":"{it["id"]}"'.encode()             # その行の uuid だけ（次の行の parentUuid は変えない）
        other = mine[:-2] + (b"0" if mine[-2:-1] != b"0" else b"1") + b'"'
        self.assertEqual(data.count(mine), 1)
        with open(self.log(), "r+b") as f:
            f.write(data.replace(mine, other))
        self.assertIsNone(self.store.image(SID, it["id"], 0))

    def test_written_and_stat_key(self):
        data = (FIX / "vscode.jsonl").read_bytes()
        self.write(data)
        want = UnitBuilder()
        off = 0
        for ln in fixture_lines("vscode.jsonl"):
            want.feed(json.loads(ln), off)
            off += len(ln)
        self.assertTrue(want.written())
        self.assertEqual(self.store.written(SID), want.written())
        st = self.log().stat()
        self.assertEqual(self.store.stat_key(SID), (st.st_ino, st.st_size, st.st_mtime_ns))
        self.append(line(human("u9", "足す")))
        self.assertEqual(self.store.stat_key(SID)[1], len(data) + len(line(human("u9", "足す"))))
        self.assertEqual(self.store.written("missing"), [])
        self.assertIsNone(self.store.stat_key("missing"))


class LogPathTest(StoreCase):
    def test_bad_session_id_never_globs(self):
        self.assertIsNone(self.store.log_path("*"))
        self.assertIsNone(self.store.log_path("../x"))
        with mock.patch.object(Path, "glob", side_effect=AssertionError("glob を呼んだ")) as g:
            for bad in ["*", "../x", "", "a/b", "a.b", "x" * 81, "?", "[a]", "a\n", "../../etc/passwd"]:
                with self.subTest(bad=bad):
                    self.assertIsNone(self.store.log_path(bad))
                    self.assertEqual(self.store.fetch(bad, None)["items"], [])
                    self.assertIsNone(self.store.stat_key(bad))
                    self.assertIsNone(self.store.image(bad, "x", 0))
                    self.assertEqual(self.store.written(bad), [])
            g.assert_not_called()
        self.write(line(human("u1", "一つ目")), "x" * 80)
        self.assertEqual(self.store.log_path("x" * 80), self.log("x" * 80))

    def test_duplicate_log_prefers_newest(self):
        # 同じ sessionId の会話ログが2つのフォルダにある【実測：~/.claude/projects に2組。片方は数行の古い控え】
        other = self.projects / "-fixture"
        other.mkdir()
        stub = other / f"{SID}.jsonl"
        stub.write_bytes(line(human("old", "古い控え")))
        self.write(line(human("new", "続き")))
        os.utime(stub, ns=(2_000_000_000, 2_000_000_000))
        os.utime(self.log(), ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(self.store.log_path(SID), stub)
        os.utime(self.log(), ns=(3_000_000_000, 3_000_000_000))
        self.assertEqual(self.store.log_path(SID), self.log())
        self.assertEqual(ids(self.store.fetch(SID, None)), ["new"])


class PrefilterTest(StoreCase):
    def test_prefilter_skips_unneeded_lines(self):
        # api-request-blob の巨大な行（"type":"user" を含まない）を挟んでも items は同じ、json.loads は呼ばれない
        blob = line({"type": "api-request-blob", "sessionId": SID, "hash": "h",
                     "message": {"role": "user", "content": "x" * 2_000_000}})
        self.assertFalse(any(m in blob for m in MARKS))
        src = fixture_lines("tmux_rc.jsonl")
        self.write(b"".join(src[:10] + [blob] + src[10:] + [blob]))
        self.write(b"".join(src), "plain")
        with mock.patch("bblist.transcript.json.loads", wraps=json.loads) as loads:
            r = self.store.fetch(SID, None)
        self.assertEqual(r["items"], self.full("plain")["items"])
        self.assertFalse(any(len(call.args[0]) >= len(blob) for call in loads.call_args_list))
        # 目印のある行だけを1回ずつ JSON に直す（手本の api-request などの行も読み飛ばす）
        marked = [ln for ln in src if any(m in ln for m in MARKS)]
        self.assertLess(len(marked), len(src))
        self.assertEqual(loads.call_count, len(marked))


class ThreadTest(StoreCase):
    def test_threads_share_sessions_safely(self):
        # 2つのセッションを3本ずつの糸が cursor でつなぎながら読む。覚える数は1本なので、読むたびに捨てて読み直す
        store = TranscriptStore(self.projects, max_sessions=1)
        names = {"th-a": "tmux_rc.jsonl", "th-b": "vscode.jsonl"}
        for sid in names:
            self.write(b"", sid)
        stop, errors, results = threading.Event(), [], {}

        def reader(key, sid):
            got, cursor = [], None
            try:
                while True:
                    done = stop.is_set()
                    r = store.fetch(sid, cursor)
                    if cursor is not None and r["reset"]:
                        raise AssertionError("reset")
                    by_id = {it["id"]: it for it in got}
                    for u in r["updates"]:
                        by_id[u["id"]]["answer"] = u["answer"]
                    got += r["items"]
                    cursor = r["cursor"]
                    store.written(sid)
                    store.stat_key(sid)
                    if done:
                        break
            except Exception as e:      # 糸の中の失敗を本体へ渡す
                errors.append(repr(e))
            results[key] = (sid, got)

        threads = [threading.Thread(target=reader, args=(f"{sid}-{k}", sid)) for sid in names for k in range(3)]
        for t in threads:
            t.start()
        la, lb = fixture_lines(names["th-a"]), fixture_lines(names["th-b"])
        for k in range(max(len(la), len(lb))):
            if k < len(la):
                self.append(la[k], "th-a")
            if k < len(lb):
                self.append(lb[k], "th-b")
        stop.set()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(threads))
        for sid, got in results.values():
            self.assertEqual(got, self.full(sid)["items"])


def respaced(row: dict, colon: str = ": ", comma: str = ", ") -> bytes:
    """区切りに空白のある1行（Python の json.dumps の既定は ", " と ": "）。Claude Code が書き方を変えた場合の備え。"""
    return json.dumps(row, ensure_ascii=False, separators=(comma, colon)).encode("utf-8") + b"\n"


def fixture_rows(name: str) -> list:
    return [json.loads(ln) for ln in fixture_lines(name)]


def api_request(i: int, ts: str = TS) -> dict:
    """表示に使わない行（目印の無い種類）。一番上に type と timestamp がある。"""
    return {"type": "api-request", "sessionId": SID, "id": f"req{i}", "timestamp": ts,
            "params": {"model": "m", "thinking": {"type": "adaptive"}}}


class SpacedJsonTest(StoreCase):
    """会話ログの1行の目印は、区切りの空白に依らない（Claude Code の書き方が変わっても空にならない）。"""

    def compare(self, spaced_lines: list, compact_lines: list) -> None:
        self.write(b"".join(spaced_lines), "spaced")
        self.write(b"".join(compact_lines), "compact")
        a, b = self.full("spaced"), self.full("compact")
        for key in ("items", "updates", "pendingCall", "queue", "lastRecordAt"):
            self.assertEqual(a[key], b[key], key)
        self.assertGreater(len(b["items"]), 0)

    def test_transcript_tolerates_spaced_json(self):
        for name in FIXTURES:
            rows = fixture_rows(name)
            self.compare([respaced(r) for r in rows], [line(r) for r in rows])      # "type": "user"

    def test_other_json_whitespace_styles_also_work(self):
        rows = fixture_rows("tmux_rc.jsonl")
        for colon, comma in ((" : ", " , "), (":\t", ",\t"), (": ", ","), (":  ", ", "), (":", ", ")):
            self.compare([respaced(r, colon, comma) for r in rows], [line(r) for r in rows])

    def test_spaced_and_compact_lines_in_one_log(self):
        # 会話ログの途中で Claude Code が新しい版に替わった（再開したセッションは同じファイルに書き足す）
        rows = fixture_rows("vscode.jsonl")
        mixed = [respaced(r) if i % 2 else line(r) for i, r in enumerate(rows)]
        self.compare(mixed, [line(r) for r in rows])

    def test_spaced_unmarked_lines_are_still_skipped_without_parsing(self):
        blob = respaced({"type": "api-request-blob", "sessionId": SID, "hash": "h",
                         "message": {"role": "user", "content": "x" * 2_000_000}})
        # 文字列の中の type は \" で書かれるので、目印にならない
        quoted = respaced({"type": "api-request-blob", "sessionId": SID, "hash": "h2",
                           "message": {"role": "user", "content": '{"type": "user", "type":"assistant"}'}})
        self.write(blob + quoted + respaced(human("u1", "一つ目")))
        with mock.patch("bblist.transcript.json.loads", wraps=json.loads) as loads:
            r = self.store.fetch(SID, None)
        self.assertEqual(ids(r), ["u1"])
        self.assertEqual(loads.call_count, 1)                    # 目印のある行だけを1回

    def test_spaced_skipped_lines_still_move_last_record_at(self):
        t2, t3 = "2026-10-27T01:00:05.000Z", "2026-10-27T01:00:09.000Z"
        self.write(line(human("u1", "一つ目")))
        r = self.store.fetch(SID, None)
        self.append(respaced(api_request(1, t2)))                 # "timestamp": "…"
        r = self.store.fetch(SID, r["cursor"])
        self.assertEqual((r["items"], r["lastRecordAt"]), ([], t2))
        # 一番上の timestamp の前に辞書がある行・一番上に無く入れ子にだけある行は、空白があっても同じ決まり
        self.append(respaced({"type": "file-history-snapshot", "messageId": "m1",
                              "snapshot": {"messageId": "m1", "timestamp": "2026-10-26T00:00:00.000Z"}}))
        r = self.store.fetch(SID, r["cursor"])
        self.assertEqual(r["lastRecordAt"], t2)
        self.append(respaced({"type": "file-history-delta", "trackingPath": "p{[x",
                              "backup": {"version": 1, "backupTime": "2026-10-26T00:00:00.000Z"}, "timestamp": t3}))
        r = self.store.fetch(SID, r["cursor"])
        self.assertEqual(r["lastRecordAt"], t3)
        self.assertEqual(self.full()["lastRecordAt"], t3)


class TranscriptDiagnosticsTest(StoreCase):
    """store.diagnostics()：読めなかった行・知らない type の行・知らない origin.kind（覚えている会話ログの合計）。

    知らない type ＝ 知っている種類（描く種類＋描かないと分かっている種類。units.KNOWN_TYPES）に無い type。普段は 0。
    読み飛ばす行も数える（JSON には直さず、行の頭から一番上の type を読む）。
    """

    def diag(self, **over) -> dict:
        return {"unreadableLines": 0, "unknownTypes": 0, "unknownTypeNames": [], "unknownOriginKinds": 0, **over}

    def read_all(self, data: bytes, sid: str = SID) -> dict:
        """data を会話ログにして、何も覚えていない store で最初から読み、その診断を返す（書き直しても前の覚えに引かれない）。"""
        self.write(data, sid)
        store = TranscriptStore(self.projects)
        store.fetch(sid, None)
        return store.diagnostics()

    def test_empty_store_counts_nothing(self):
        self.assertEqual(self.store.diagnostics(), self.diag())

    def test_unreadable_lines_are_counted(self):
        self.write(line(human("u1", "一つ目"))
                   + b'{"type":"user","message":"broken\n'                        # JSON に直せない行
                   + line({"type": "user", "uuid": "x", "message": "崩れた形"})   # 形の崩れた行
                   + line(human("u2", "二つ目")))
        with self.assertLogs("bblist.transcript", "WARNING"):
            self.store.fetch(SID, None)
        self.assertEqual(self.store.diagnostics(), self.diag(unreadableLines=2))

    def test_unknown_types_are_counted_and_named_without_parsing_the_line(self):
        skipped = [line({"type": "future-kind", "id": "r1", "timestamp": TS}),
                   respaced({"type": "future-kind", "id": "r2"}),                            # 区切りに空白があっても
                   respaced({"type": "another.kind_2", "id": "r3"}),
                   # type が途中にある行（{"parentUuid": null, …, "type": "x"}）
                   line({"parentUuid": None, "isSidechain": False, "cwd": "/x", "sessionId": SID, "type": "mid-kind"}),
                   # 値が type という文字列の項目が先にあっても、キーの type を取る
                   line({"a": "type", "type": "after-value-kind"})]
        self.assertFalse(any(m in ln for ln in skipped for m in MARKS))
        self.write(line(human("u1", "一つ目")) + b"".join(skipped))
        with mock.patch("bblist.transcript.json.loads", wraps=json.loads) as loads:
            r = self.store.fetch(SID, None)
        self.assertEqual(ids(r), ["u1"])
        self.assertEqual(loads.call_count, 1)                      # 読み飛ばした行は JSON に直さない（速さの決まり）
        self.assertEqual(self.store.diagnostics(), self.diag(
            unknownTypes=5, unknownTypeNames=["after-value-kind", "another.kind_2", "future-kind", "mid-kind"]))

    def test_known_types_are_never_counted(self):
        # 描く種類も、描かないと分かっている種類（ai-title・api-request など）も、詰めた形でも空白のある形でも 0
        for t in sorted(KNOWN_TYPES):
            for fmt in (line, respaced):
                self.assertEqual(self.read_all(fmt({"type": t, "uuid": "k", "timestamp": TS, "message": {"content": "x"}})),
                                 self.diag(), (t, fmt.__name__))
        self.assertLess(len(DRAWN_TYPES), len(KNOWN_TYPES))
        self.assertGreater(len(KNOWN_TYPES), 20)
        # 手本（本物の会話ログから作った）の種類は、どれも知っている
        for name in FIXTURES:
            self.assertEqual(self.read_all((FIX / name).read_bytes(), "fx"), self.diag(), name)

    def test_drawn_types_are_the_marked_ones(self):
        # 読む目印と知っている種類の「描く」側は同じ一覧から来る（片方だけ足して食い違わない）
        for t in DRAWN_TYPES:
            self.assertTrue(_wanted(line({"type": t})), t)
        self.assertFalse(_wanted(line({"type": "ai-title"})))

    def test_only_the_top_level_type_counts(self):
        nested = [line({"type": "file-history-snapshot", "snapshot": {"type": "weird-nested"}}),     # 入れ子は数えない
                  line({"type": "ai-title", "list": [{"type": "weird-in-list"}], "x": {"y": {"type": "deep"}}}),
                  line({"snapshot": {"type": "weird-nested"}}),                                    # 一番上に type が無い
                  line({"snapshot": {"type": "weird-nested"}, "list": [{"type": "x"}]})]
        self.assertEqual(self.read_all(b"".join(nested)), self.diag())

    def test_a_type_after_a_large_message_is_found_like_a_real_assistant_row(self):
        # 本物の assistant・attachment の行は、一番上の type が message の後ろ（数 KiB 以上あと）にある【実測】。名前が変わって
        # 目印に合わなくなったとき（assistant → assistant-v2）も数える。message の中の入れ子の type は数えない
        content = [{"type": "text", "text": "}}]]{{[[ \" と括弧 " * 200}, {"type": "tool_use", "id": "t", "input": {"type": "x"}}]
        row = {"parentUuid": None, "isSidechain": False, "message": {"role": "assistant", "content": content},
               "type": "assistant-v2", "uuid": "a1", "timestamp": TS}
        data = line(row)
        self.assertGreater(len(data), 4096)
        self.assertGreater(data.index(b'"type":"assistant-v2"'), 4096)
        self.assertFalse(any(m in data for m in MARKS))
        self.assertEqual(self.read_all(data * 3), self.diag(unknownTypes=3, unknownTypeNames=["assistant-v2"]))
        # 知っている名前なら、同じ形でも 0（目印があるので JSON に直して読む）
        self.assertEqual(self.read_all(line({**row, "type": "assistant"}) * 3), self.diag())

    def test_braces_and_quotes_inside_strings_do_not_confuse_the_depth(self):
        tricky = [line({"text": "}}}{{{[[[ \" \\\\ ", "type": "tricky-after-text"}),
                  line({"k": '{"type":"in-string"}', "type": "tricky-after-quoted-json"}),
                  line({"list": ["]", "}"], "type": "tricky-after-list"})]
        self.assertEqual(self.read_all(b"".join(tricky)), self.diag(
            unknownTypes=3, unknownTypeNames=["tricky-after-list", "tricky-after-quoted-json", "tricky-after-text"]))

    def test_lines_without_a_string_top_level_type_are_not_counted(self):
        untyped = [b"plain text, not json\n", line({"no_type_here": 1, "n": [1, 2]}), b"[1, 2, 3]\n",
                   b'[{"type": "in-array"}]\n', b'{"type": 5}\n', b'{"type": null}\n', b'{"type": ["x"]}\n',
                   line({"msg": "the word type appears but not as a key"}), line({"a": "type"}), b"\n", b"{}\n",
                   b'{"type":"unterminated\n']
        self.assertEqual(self.read_all(b"".join(untyped)), self.diag())

    def test_an_unknown_type_on_a_line_that_is_parsed_is_counted_too(self):
        # 表示に使う種類の目印（"type":"user"）が入れ子にだけある行は JSON に直して読む。一番上の type が知らない種類なら数える
        row = {"type": "future-kind", "uuid": "p1", "timestamp": TS, "message": {"content": [{"type": "user"}]}}
        self.assertTrue(any(m in line(row) for m in MARKS))
        self.assertEqual(self.read_all(line(row) + line(human("u1", "x"))),
                         self.diag(unknownTypes=1, unknownTypeNames=["future-kind"]))

    def test_unknown_type_names_are_bounded_and_plain(self):
        many = [line({"type": f"kind-{i:02d}"}) for i in range(20)]
        self.assertEqual(self.read_all(b"".join(many) + b"".join(many[:3])),
                         self.diag(unknownTypes=23, unknownTypeNames=[f"kind-{i:02d}" for i in range(8)]))   # 数は全部・名前は8個まで

    def test_names_kept_per_log_are_bounded_too(self):
        # 覚えるのは1本につき8個まで（会話ログが何種類の名前を持っても、覚える量は増えない）
        self.write(b"".join(line({"type": f"kind-{i:03d}"}) for i in range(500)))
        self.store.fetch(SID, None)
        self.assertEqual(len(self.store._logs[SID].unknown_names), 8)
        self.assertEqual(self.store.diagnostics()["unknownTypes"], 500)

    def test_unknown_type_names_keep_only_plain_short_names(self):
        ok = ["Foo_bar.baz-1", "a", "x" * 64]
        odd = ["has space", "日本語", 'quo"te', "slash/name", "back\\slash", "tab\tname", "x" * 65, "", "new\nline", "a" * 5000]
        self.assertEqual(self.read_all(b"".join(line({"type": t}) for t in ok + odd)),
                         self.diag(unknownTypes=len(ok) + len(odd), unknownTypeNames=sorted(ok)))      # 合わない名前は数だけ

    def test_a_marker_miss_shows_up_as_unknown_types_and_no_items(self):
        # 目印が合わない書き方に変わった（種類の名前が変わった）：items は空だが、黙らず unknownTypes に出る
        rows = [dict(r, type=r["type"] + "-v2") for r in fixture_rows("vscode.jsonl")
                if r.get("type") in ("user", "assistant")]
        self.write(b"".join(line(r) for r in rows))
        r = self.store.fetch(SID, None)
        self.assertEqual(r["items"], [])
        d = self.store.diagnostics()
        self.assertEqual((d["unknownTypes"], d["unknownTypeNames"]), (len(rows), ["assistant-v2", "user-v2"]))
        self.assertGreater(len(rows), 10)

    def test_unknown_origin_kinds_are_counted(self):
        self.assertEqual(self.read_all(line(human("u1", "人")) + line({**human("u2", "新しい形"), "origin": {"kind": "person"}})
                                       + line({**human("u3", "新しい形"), "origin": {"kind": "person"}})
                                       + line({**human("u4", "知らせ"), "origin": {"kind": "task-notification"}})),
                         self.diag(unknownOriginKinds=2))

    def test_counts_are_not_doubled_by_reading_again(self):
        self.write(line({"type": "future-a"}) + b"{broken\n" + line(human("u1", "一つ目")))
        r = self.store.fetch(SID, None)
        self.append(line({"type": "future-b"}) + line({**human("u2", "x"), "origin": {"kind": "person"}}))
        r = self.store.fetch(SID, r["cursor"])                      # 増えた分だけ読む
        self.store.fetch(SID, r["cursor"])                          # 何も増えない
        self.assertEqual(self.store.diagnostics(), self.diag(
            unknownTypes=2, unknownTypeNames=["future-a", "future-b"], unknownOriginKinds=1))
        self.write(line({"type": "future-c"}) + line(human("u2", "二つ目")))   # 短くなった：最初から読み直す
        self.store.fetch(SID, r["cursor"])
        self.assertEqual(self.store.diagnostics(), self.diag(unknownTypes=1, unknownTypeNames=["future-c"]))

    def test_counts_cover_the_remembered_logs_and_forget_dropped_ones(self):
        store = TranscriptStore(self.projects, max_sessions=2)
        for sid, n in (("d-a", 1), ("d-b", 2), ("d-c", 4)):
            self.write(b"".join(line({"type": f"{sid}-kind-{i}"}) for i in range(n)) + line(human(f"u-{sid}", "x")), sid)
        store.fetch("d-a", None)
        store.fetch("d-b", None)
        self.assertEqual(store.diagnostics()["unknownTypes"], 3)    # 1 + 2
        store.fetch("d-c", None)                                    # 一番古い d-a を捨てる
        d = store.diagnostics()
        self.assertEqual(d["unknownTypes"], 6)                      # 2 + 4
        self.assertNotIn("d-a-kind-0", d["unknownTypeNames"])
        self.assertEqual(d["unknownTypeNames"], sorted(d["unknownTypeNames"]))
        self.assertLessEqual(len(d["unknownTypeNames"]), 8)

    def test_the_same_name_in_two_logs_is_listed_once(self):
        for sid in ("d-a", "d-b"):
            self.write(line({"type": "shared-kind"}) + line(human(f"u-{sid}", "x")), sid)
            self.store.fetch(sid, None)
        self.assertEqual(self.store.diagnostics(), self.diag(unknownTypes=2, unknownTypeNames=["shared-kind"]))

    def test_log_that_vanished_is_forgotten(self):
        self.write(line({"type": "future-kind"}) + line({**human("u1", "x"), "origin": {"kind": "person"}}))
        self.store.fetch(SID, None)
        self.assertEqual(self.store.diagnostics()["unknownTypes"], 1)
        self.assertEqual(self.store.diagnostics()["unknownOriginKinds"], 1)
        self.log().unlink()
        self.store.fetch(SID, None)
        self.assertEqual(self.store.diagnostics(), self.diag())


class PerfToolTest(unittest.TestCase):
    """tests/perf_transcript.py が作る大きな会話ログは、繰り返しごとに uuid が全部別になる（単位が1つにまとまると軽く測れてしまう）。"""

    def test_every_repetition_has_its_own_uuids(self):
        from tests import perf_transcript as perf
        src = b"".join(p.read_bytes() for p in sorted(perf.FIX.glob("*.jsonl")))
        distinct = len(set(perf._UUID_RE.findall(src)))
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.jsonl"
            size = perf.make_log(log, 3.5 * len(src) / 1_000_000)
            reps = size // len(src)
            self.assertEqual(reps, 4)
            self.assertEqual(size, reps * len(src))
            self.assertEqual(len(set(perf._UUID_RE.findall(log.read_bytes()))), reps * distinct)


if __name__ == "__main__":
    unittest.main()
