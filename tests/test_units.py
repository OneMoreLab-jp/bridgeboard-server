"""bblist/units.py と手本の試験。python3 -m unittest discover -s tests -t . -p 'test_units.py' -v

手本（fixtures/*.jsonl）は本物の会話ログの本文を「文{n}」に置き換えたもの。本文で行を探せないので、
指示は uuid・行の種類・つながり（parentUuid）で探す。決まりを1つだけ確かめたいときは、手本の行を写して1か所だけ変える。
指示の数は、最初に作った使い捨ての読み取り器（公開していない）の答えと同じことを確かめて固定した（PROBE_COUNTS）。
"""
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from bblist.units import EARLY_RESULTS_MAX, KNOWN_ORIGIN_KINDS, UnitBuilder, extract_images
from tests.make_fixture import (CWD, FAKE_UUID_PREFIX, KEEP_KEYS, PNG_1x1, PNG_1x1_B64, FakeIds, LeakError,
                                Scrubber, _REAL_ID)

FIX = Path(__file__).resolve().parent / "fixtures"
# 手本ごとの指示の数。最初の読み取り器（同じ uuid をまとめた後の数）と一致することを 2026-10-31 に実測して固定した
PROBE_COUNTS = {"tmux_rc.jsonl": 13, "tmux_rc_cleared.jsonl": 1, "vscode.jsonl": 4}
TS = "2026-10-27T01:00:00.000Z"


def rows(name: str) -> list[tuple[int, dict]]:
    """手本を (行の先頭のバイト位置, 行) の並びにする。"""
    out, off = [], 0
    for line in (FIX / name).read_bytes().splitlines(keepends=True):
        out.append((off, json.loads(line)))
        off += len(line)
    return out


def build(name: str) -> UnitBuilder:
    """手本を1行ずつ feed した UnitBuilder。"""
    b = UnitBuilder()
    for off, row in rows(name):
        b.feed(row, off)
    return b


def kinds(b: UnitBuilder, kind: str) -> list[dict]:
    return [it for it in b.items_since(0) if it["kind"] == kind]


def count(b: UnitBuilder, kind: str) -> int:
    return len(kinds(b, kind))


def tool_uses(row: dict) -> list[dict]:
    if row.get("type") != "assistant":
        return []
    return [p for p in row["message"]["content"] if p.get("type") == "tool_use"]


def result_index(rs: list[tuple[int, dict]], tid: str) -> int:
    """tool_use の id に答えた行（tool_result を持つ user の行）の番号。"""
    for k, (_, r) in enumerate(rs):
        c = r.get("message", {}).get("content") if r.get("type") == "user" else None
        if isinstance(c, list) and any(p.get("tool_use_id") == tid for p in c):
            return k
    raise AssertionError(f"{tid} の答えが手本に無い")


def assistant(uuid: str, *blocks, **extra) -> dict:
    return {"type": "assistant", "uuid": uuid, "timestamp": extra.pop("ts", TS),
            "message": {"role": "assistant", "content": list(blocks)}, **extra}


def tool_use(tid: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(uuid: str, tid: str, content="ok", **extra) -> dict:
    return {"type": "user", "uuid": uuid, "timestamp": extra.pop("ts", TS),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]},
            **extra}


def human(uuid: str, text: str, origin: bool = True) -> dict:
    o = {"type": "user", "uuid": uuid, "timestamp": TS, "message": {"role": "user", "content": text}}
    if origin:
        o["origin"] = {"kind": "human"}
    return o


class InstructionTest(unittest.TestCase):
    def test_instruction_count_matches_probe(self):
        for f, want in PROBE_COUNTS.items():
            with self.subTest(f=f):
                self.assertEqual(count(build(f), "instruction"), want)
        # /clear の後の会話ログ：claude.ai と同じく最初の指示（C1）だけ。/clear の行は出さない
        self.assertEqual(count(build("tmux_rc_cleared.jsonl"), "instruction"), 1)

    def test_command_rows_without_origin_are_not_instructions(self):
        # /clear の後の会話ログは、origin の無い <command-name> の行で始まる【実測】
        for text in ["<command-name>/clear</command-name>", "<local-command-stdout></local-command-stdout>",
                     "<bash-input>ls</bash-input>"]:
            with self.subTest(text=text):
                b = UnitBuilder()
                b.feed(human("u1", text, origin=False), 0)
                self.assertEqual(count(b, "instruction"), 0)
        # それ以外の origin の無い行は、計画の決まり（古い会話ログ向け）のまま
        b = UnitBuilder()
        b.feed(human("u2", "古い形の指示", origin=False), 0)
        b.feed(human("u3", "<command-name>/model</command-name>"), 10)   # origin のある行は外さない
        self.assertEqual([it["id"] for it in kinds(b, "instruction")], ["u2", "u3"])

    def test_rewind_branch_instructions_all_kept_in_file_order(self):
        humans = [r for _, r in rows("tmux_rc.jsonl")
                  if r.get("type") == "user" and (r.get("origin") or {}).get("kind") == "human"]
        # 巻き戻し：R4（最後の指示）の parentUuid は R2 と同じ行を指す。R2・R3 はファイルに残る【実測】
        r4 = humans[-1]
        r2 = next(r for r in humans[:-1] if r["parentUuid"] == r4["parentUuid"])
        i = humans.index(r2)
        r1, r3 = humans[i - 1], humans[i + 1]
        self.assertEqual(humans.index(r3), len(humans) - 2)
        ids = [it["id"] for it in kinds(build("tmux_rc.jsonl"), "instruction")]
        self.assertEqual(ids[-4:], [r1["uuid"], r2["uuid"], r3["uuid"], r4["uuid"]])

    def test_queued_instruction_uses_source_uuid_and_dedups(self):
        rs = rows("tmux_rc.jsonl")
        att = next(r for _, r in rs if r.get("type") == "attachment"
                   and r["attachment"].get("type") == "queued_command")
        src = att["attachment"]["source_uuid"]
        self.assertNotEqual(src, att["uuid"])
        b = build("tmux_rc.jsonl")
        queued = [it for it in kinds(b, "instruction") if it["queued"]]
        self.assertEqual([(it["id"], it["text"], it["ts"]) for it in queued],
                         [(src, att["attachment"]["prompt"], att["timestamp"])])
        self.assertTrue(all(not it["queued"] for it in kinds(b, "instruction") if it["id"] != src))
        # 同じ id がもう一度出ても1つのまま（作業中に送った指示も、同じ uuid の人の指示も）
        n, end = len(b.items_since(0)), (FIX / "tmux_rc.jsonl").stat().st_size
        b.feed(att, end)
        b.feed(human(src, "もう一度"), end + 10)
        self.assertEqual(len(b.items_since(0)), n)
        self.assertEqual(b.items_since(end), [])
        # source_uuid が無ければ行の uuid
        b2 = UnitBuilder()
        no_src = copy.deepcopy(att)
        del no_src["attachment"]["source_uuid"]
        b2.feed(no_src, 0)
        self.assertEqual([it["id"] for it in kinds(b2, "instruction")], [att["uuid"]])

    def test_task_notification_and_meta_rows_are_not_instructions(self):
        b = build("tmux_rc.jsonl")
        ids = {it["id"] for it in kinds(b, "instruction")}
        for _, r in rows("tmux_rc.jsonl"):
            if r.get("type") == "user" and (r.get("origin") or {}).get("kind") == "task-notification":
                self.assertNotIn(r["uuid"], ids)
        b = build("vscode.jsonl")
        ids = {it["id"] for it in kinds(b, "instruction")}
        metas = [r["uuid"] for _, r in rows("vscode.jsonl") if r.get("type") == "user" and r.get("isMeta")]
        self.assertTrue(metas)
        self.assertFalse(ids & set(metas))

    def test_origin_fallback_only_until_first_origin_row(self):
        # origin の項目が無い行は、それまでに origin を持つ行が1つも無いときだけ人の指示（古い会話ログ向け）
        b = UnitBuilder()
        b.feed(human("u1", "古い形", origin=False), 0)
        b.feed(human("u2", "新しい形"), 10)
        b.feed(human("u3", "origin の無い行", origin=False), 20)
        b.feed({**human("u4", "知らせ"), "origin": {"kind": "task-notification"}}, 30)
        self.assertEqual([it["id"] for it in kinds(b, "instruction")], ["u1", "u2"])


class UnknownOriginKindTest(unittest.TestCase):
    """人の指示かを決める origin.kind が、知っている値の一覧に無い値だったら数える（診断 unknownOriginKinds）。
    kind の名前が変わると人の指示が黙って落ちる。数えるだけで、指示にするかどうかの決まりは変えない。"""

    def feed(self, *rs: dict) -> UnitBuilder:
        b = UnitBuilder()
        for k, r in enumerate(rs):
            b.feed(r, k * 10)
        return b

    def test_known_kinds_are_not_counted(self):
        self.assertEqual(sorted(KNOWN_ORIGIN_KINDS), ["coordinator", "human", "peer", "task-notification"])
        for kind in KNOWN_ORIGIN_KINDS:
            b = self.feed({**human("u1", "x"), "origin": {"kind": kind}})
            self.assertEqual(b.unknown_origin_kinds, 0, kind)

    def test_an_unknown_kind_is_counted_and_is_still_not_an_instruction(self):
        b = self.feed(human("u1", "人の指示"), {**human("u2", "新しい形の指示"), "origin": {"kind": "person"}},
                      {**human("u3", "もう1つ"), "origin": {"kind": "person"}})
        self.assertEqual(b.unknown_origin_kinds, 2)
        self.assertEqual([it["id"] for it in kinds(b, "instruction")], ["u1"])      # 決まりは今までどおり（人だけ）

    def test_an_origin_without_a_usable_kind_is_counted(self):
        for origin in ({}, {"kind": None}, {"kind": 5}, {"kind": ["human"]}, {"type": "human"}, None, [], "human"):
            b = self.feed({**human("u1", "x"), "origin": origin})
            self.assertEqual(b.unknown_origin_kinds, 1, repr(origin))

    def test_rows_without_an_origin_are_not_counted(self):
        # origin を持つ行が前にあっても、持たない行（ツール結果など）は kind を見る行ではない
        b = self.feed(human("u1", "x"), tool_result("r1", "t1"), human("u2", "古い形", origin=False))
        self.assertEqual(b.unknown_origin_kinds, 0)

    def test_rows_that_never_reach_the_kind_check_are_not_counted(self):
        odd = {"origin": {"kind": "person"}}
        b = self.feed({**human("u1", "x"), **odd, "isMeta": True}, {**human("u2", "x"), **odd, "isSidechain": True},
                      {**human("u3", "x"), **odd, "isCompactSummary": True},
                      {**assistant("a1"), **odd}, {"type": "attachment", "uuid": "t1", **odd})
        self.assertEqual(b.unknown_origin_kinds, 0)

    def test_fixtures_hold_only_known_kinds(self):
        for name in ("tmux_rc.jsonl", "tmux_rc_cleared.jsonl", "vscode.jsonl"):
            self.assertEqual(build(name).unknown_origin_kinds, 0, name)


class TextTest(unittest.TestCase):
    def test_text_items_skip_sidechain_and_keep_markdown(self):
        want = [(r["uuid"], p["text"]) for _, r in rows("vscode.jsonl") if r.get("type") == "assistant"
                for p in r["message"]["content"] if p.get("type") == "text"]
        self.assertTrue(want)
        b = build("vscode.jsonl")
        self.assertEqual([(it["id"], it["markdown"]) for it in kinds(b, "text")], want)
        # サブエージェントの行（isSidechain）の文章は出さない
        real = next(r for _, r in rows("vscode.jsonl") if r.get("type") == "assistant"
                    and any(p.get("type") == "text" for p in r["message"]["content"]))
        b = UnitBuilder()
        b.feed({**copy.deepcopy(real), "isSidechain": True}, 0)
        self.assertEqual(kinds(b, "text"), [])
        # Markdown は HTML に直さずそのまま。2つ目からの text ブロックは <uuid>:<i>
        md = "# 見出し\n\n**太字** <b>生の HTML</b> & `code`\n\n| a | b |\n|---|---|"
        b.feed(assistant("a1", {"type": "text", "text": md}, {"type": "thinking", "thinking": ""},
                         {"type": "text", "text": "二つ目"}), 10)
        self.assertEqual(kinds(b, "text"), [{"kind": "text", "id": "a1", "ts": TS, "markdown": md},
                                            {"kind": "text", "id": "a1:1", "ts": TS, "markdown": "二つ目"}])


class QuestionTest(unittest.TestCase):
    def test_question_before_and_after_answer(self):
        rs = rows("tmux_rc.jsonl")
        i = next(k for k, (_, r) in enumerate(rs) if any(p["name"] == "AskUserQuestion" for p in tool_uses(r)))
        call = tool_uses(rs[i][1])[0]
        b = UnitBuilder()
        for off, r in rs[:i + 1]:
            b.feed(r, off)
        q = kinds(b, "question")[-1]
        self.assertEqual(q, {"kind": "question", "id": call["id"], "ts": rs[i][1]["timestamp"],
                             "questions": call["input"]["questions"], "answer": None})
        pos = rs[i + 1][0]            # ここまでを渡した cursor の位置
        self.assertEqual(b.updates_since(pos), [])
        for off, r in rs[i + 1:]:
            b.feed(r, off)
        ans = rs[result_index(rs, call["id"])][1]
        ups = b.updates_since(pos)
        self.assertEqual(ups, [{"kind": "question", "id": call["id"], "answer": ans["toolUseResult"]}])
        self.assertIsNotNone(ups[0]["answer"])
        # 質問の行が pos より後なら、答えは updates ではなく items の中に入る
        self.assertEqual([u for u in b.updates_since(rs[i][0]) if u["id"] == call["id"]], [])
        q = next(it for it in b.items_since(rs[i][0]) if it["id"] == call["id"])
        self.assertEqual(q["answer"], ans["toolUseResult"])

    def test_answer_falls_back_to_tool_result_text(self):
        b = UnitBuilder()
        b.feed(assistant("a1", tool_use("toolu_q", "AskUserQuestion", questions=[{"question": "色は"}])), 0)
        b.feed(tool_result("u1", "toolu_q", [{"type": "text", "text": "答えなかった"}], toolUseResult="Error"), 10)
        self.assertEqual(kinds(b, "question")[0]["answer"], "答えなかった")


class CompactErrorTest(unittest.TestCase):
    def test_compact_and_error_items(self):
        rs = [r for _, r in rows("compact_error.jsonl")]
        comp = next(r for r in rs if r.get("isCompactSummary"))
        err = next(r for r in rs if r.get("type") == "system" and r.get("subtype") == "api_error")
        b = build("compact_error.jsonl")
        self.assertEqual(kinds(b, "compact"), [{"kind": "compact", "id": comp["uuid"], "ts": comp["timestamp"]}])
        self.assertEqual(kinds(b, "error"), [{"kind": "error", "id": err["uuid"], "ts": err["timestamp"],
                                              "subtype": "api_error"}])
        self.assertNotIn(comp["uuid"], {it["id"] for it in kinds(b, "instruction")})
        # 圧縮の要約は origin の無い古い会話ログでも指示にしない
        b = UnitBuilder()
        b.feed(comp, 0)
        self.assertEqual(count(b, "instruction"), 0)
        self.assertEqual(count(b, "compact"), 1)


class StateTest(unittest.TestCase):
    def test_state_queue_counts_and_texts(self):
        # tmux：claude.ai から送った分は texts に本文、task-notification は数えない
        rs = rows("tmux_rc.jsonl")
        b, seen, pair = UnitBuilder(), [], None
        for k, (off, r) in enumerate(rs):
            before = b.state()["queue"]
            b.feed(r, off)
            q = b.state()["queue"]
            seen.append(q)
            if r.get("type") == "queue-operation" and "<task-notification>" in (r.get("content") or ""):
                self.assertEqual(q, before)
            if (k and r.get("operation") == "enqueue" and rs[k - 1][1].get("operation") == "enqueue"
                    and pair is None):
                pair = (rs[k - 1][1]["content"], r["content"], q)
        c1, c2, q = pair
        self.assertEqual(q, {"count": 2, "texts": [c1, c2]})
        self.assertTrue(c1.startswith("文") and c2.startswith("文"))
        self.assertEqual(b.state()["queue"], {"count": 0, "texts": []})
        # vscode：Mac から送った分は enqueue が空なので texts は None。remove で外れる
        b, seen = UnitBuilder(), []
        for off, r in rows("vscode.jsonl"):
            b.feed(r, off)
            seen.append(b.state()["queue"])
        self.assertIn({"count": 1, "texts": [None]}, seen)
        self.assertEqual(b.state()["queue"], {"count": 0, "texts": []})

    def test_dequeue_on_empty_queue_does_nothing(self):
        b = UnitBuilder()
        b.feed({"type": "queue-operation", "operation": "dequeue", "timestamp": TS}, 0)
        b.feed({"type": "queue-operation", "operation": "enqueue", "timestamp": TS, "content": "A"}, 10)
        b.feed({"type": "queue-operation", "operation": "enqueue", "timestamp": TS,
                "content": "<task-notification>x</task-notification>"}, 20)
        b.feed({"type": "queue-operation", "operation": "remove", "timestamp": TS,
                "content": "<task-notification>x</task-notification>"}, 30)
        self.assertEqual(b.state()["queue"], {"count": 1, "texts": ["A"]})

    def test_task_entries_stay_in_queue_but_are_not_counted(self):
        def op(b, k, operation, content=None):
            r = {"type": "queue-operation", "operation": operation, "timestamp": TS}
            if content is not None:
                r["content"] = content
            b.feed(r, k)

        task = "<task-notification>終わった</task-notification>"
        # 知らせの enqueue の後に指示がたまり、知らせの dequeue（本文なし）が来ても、指示は外れない
        b = UnitBuilder()
        op(b, 0, "enqueue", task)
        op(b, 10, "enqueue", "A")
        self.assertEqual(b.state()["queue"], {"count": 1, "texts": ["A"]})
        op(b, 20, "dequeue")
        self.assertEqual(b.state()["queue"], {"count": 1, "texts": ["A"]})
        # remove は本文の同じものを外す（無ければ先頭）
        b = UnitBuilder()
        op(b, 0, "enqueue", "A")
        op(b, 10, "enqueue", task)
        op(b, 20, "remove", task)
        self.assertEqual(b.state()["queue"], {"count": 1, "texts": ["A"]})
        op(b, 30, "remove", "本文の違う控え")
        self.assertEqual(b.state()["queue"], {"count": 0, "texts": []})
        # サブエージェントの知らせ（<agent-message）も数えない
        b = UnitBuilder()
        op(b, 0, "enqueue", '<agent-message from="a1">終わった</agent-message>')
        self.assertEqual(b.state()["queue"], {"count": 0, "texts": []})
        op(b, 10, "enqueue")
        op(b, 20, "dequeue")
        self.assertEqual(b.state()["queue"], {"count": 1, "texts": [None]})

    def test_pending_call_brief_is_60_chars(self):
        # 手本：Bash の呼び出しの後は pendingCall、答えが来たら None
        rs = rows("tmux_rc.jsonl")
        i = next(k for k, (_, r) in enumerate(rs) if any(p["name"] == "Bash" for p in tool_uses(r)))
        call = tool_uses(rs[i][1])[0]
        b = UnitBuilder()
        for off, r in rs[:i + 1]:
            b.feed(r, off)
        self.assertEqual(b.state()["pendingCall"], {"name": "Bash", "brief": call["input"]["command"],
                                                    "since": rs[i][1]["timestamp"]})
        j = result_index(rs, call["id"])
        for off, r in rs[i + 1:j + 1]:
            b.feed(r, off)
        self.assertIsNone(b.state()["pendingCall"])
        # Bash は command、Edit は file_path を 60 字まで。並列なら最後に始まったもの
        b = UnitBuilder()
        long_cmd = "git log --oneline " + "あ" * 80
        b.feed(assistant("a1", tool_use("t1", "Bash", command=long_cmd, description="履歴")), 0)
        b.feed(assistant("a2", tool_use("t2", "Edit", file_path="/p/" + "x" * 80, old_string="a", new_string="b"),
                         ts="2026-10-27T01:00:05.000Z"), 10)
        self.assertEqual(b.state()["pendingCall"], {"name": "Edit", "brief": ("/p/" + "x" * 80)[:60],
                                                    "since": "2026-10-27T01:00:05.000Z"})
        b.feed(tool_result("u2", "t2"), 20)
        p = b.state()["pendingCall"]
        self.assertEqual((p["name"], p["brief"], len(p["brief"])), ("Bash", long_cmd[:60], 60))
        b.feed(tool_result("u1", "t1"), 30)
        b.feed(assistant("a3", tool_use("t3", "TodoWrite", todos=[])), 40)
        self.assertEqual(b.state()["pendingCall"]["brief"], "")

    def test_result_before_its_call_is_not_pending(self):
        # 答えの行が呼び出しの行より先に書かれることがある（裏で動かした Agent。2026-10-29 実測）。
        # 先に来た答えを覚えておき、後から来た呼び出しを答えの来ていない呼び出しにしない
        b = UnitBuilder()
        b.feed(tool_result("u1", "t1", ts="2026-10-27T15:05:08.359Z"), 0)
        b.feed(assistant("a1", tool_use("t1", "Agent", description="査読", prompt="読む"),
                         ts="2026-10-27T15:05:07.506Z"), 10)
        self.assertIsNone(b.state()["pendingCall"])
        # 後の呼び出しは、いつもどおり答えが来るまで pendingCall
        b.feed(assistant("a2", tool_use("t2", "Bash", command="ls")), 20)
        self.assertEqual(b.state()["pendingCall"]["brief"], "ls")
        b.feed(tool_result("u2", "t2"), 30)
        self.assertIsNone(b.state()["pendingCall"])

    def test_answer_before_its_question_is_kept(self):
        b = UnitBuilder()
        b.feed(tool_result("u1", "toolu_q", toolUseResult={"answers": {"色は": "青"}}), 0)
        b.feed(assistant("a1", tool_use("toolu_q", "AskUserQuestion", questions=[{"question": "色は"}])), 10)
        self.assertEqual(kinds(b, "question")[0]["answer"], {"answers": {"色は": "青"}})
        self.assertIsNone(b.state()["pendingCall"])
        # どこで区切って渡しても、答えは質問の単位に入って届き、updates には出ない（答えの行が質問の行より前のため）
        for pos in (0, 5, 10):
            q = next(it for it in b.items_since(pos) if it["id"] == "toolu_q")
            self.assertEqual(q["answer"], {"answers": {"色は": "青"}})
        for pos in (0, 5, 10, 11):
            self.assertEqual(b.updates_since(pos), [])

    def test_early_results_are_bounded(self):
        # 呼び出しの来ない答えを覚え続けない。上限を超えたら古い答えから忘れる
        n = EARLY_RESULTS_MAX
        b = UnitBuilder()
        for k in range(n):
            b.feed(tool_result(f"u{k}", f"t{k}"), k)
        b.feed(assistant("a0", tool_use("t0", "Bash", command="一番古い")), n)
        self.assertIsNone(b.state()["pendingCall"])                            # 上限ちょうどなら忘れない
        b = UnitBuilder()
        for k in range(n + 1):
            b.feed(tool_result(f"u{k}", f"t{k}"), k)
        b.feed(assistant("a0", tool_use("t0", "Bash", command="一番古い")), n + 1)
        self.assertEqual(b.state()["pendingCall"]["brief"], "一番古い")       # 忘れたので答え待ちになる
        b.feed(assistant("an", tool_use(f"t{n}", "Bash", command="一番新しい")), n + 2)
        self.assertEqual(b.state()["pendingCall"]["brief"], "一番古い")       # 覚えているので答え待ちにしない

    def test_last_record_at_is_last_row_time(self):
        rs = rows("tmux_rc.jsonl")
        last = [r["timestamp"] for _, r in rs if isinstance(r.get("timestamp"), str)][-1]
        self.assertEqual(build("tmux_rc.jsonl").state()["lastRecordAt"], last)
        self.assertIsNone(UnitBuilder().state()["lastRecordAt"])


class WrittenTest(unittest.TestCase):
    def test_written_paths_newest_first(self):
        last = {}
        for _, r in rows("vscode.jsonl"):
            for p in tool_uses(r):
                if p["name"] in ("Write", "Edit", "NotebookEdit"):
                    path = p["input"]["notebook_path" if p["name"] == "NotebookEdit" else "file_path"]
                    last.pop(path, None)
                    last[path] = r["timestamp"]
        want = [{"path": k, "lastTs": v} for k, v in reversed(list(last.items()))]
        self.assertGreaterEqual(len(want), 2)
        self.assertEqual(build("vscode.jsonl").written(), want)
        # 同じ path は最後の1つ。Read は数えない。NotebookEdit の入力は notebook_path（file_path は無い）
        b = UnitBuilder()
        calls = [("Write", {"file_path": "a", "content": "x"}),
                 ("Edit", {"file_path": "b", "old_string": "x", "new_string": "y"}),
                 ("Read", {"file_path": "c"}),
                 ("Edit", {"file_path": "a", "old_string": "x", "new_string": "y"}),
                 ("NotebookEdit", {"notebook_path": "d", "new_source": "print(1)", "cell_type": "code",
                                   "edit_mode": "replace"})]
        for k, (name, inp) in enumerate(calls):
            b.feed(assistant(f"a{k}", tool_use(f"t{k}", name, **inp), ts=f"2026-10-27T01:00:0{k}.000Z"), k * 10)
        self.assertEqual(b.written(), [{"path": "d", "lastTs": "2026-10-27T01:00:04.000Z"},
                                       {"path": "a", "lastTs": "2026-10-27T01:00:03.000Z"},
                                       {"path": "b", "lastTs": "2026-10-27T01:00:01.000Z"}])


class ImageTest(unittest.TestCase):
    def test_images_counted_on_instruction_and_offset_kept(self):
        b = build("vscode.jsonl")
        with_images = [it for it in kinds(b, "instruction") if it["images"]]
        it = with_images[0]
        self.assertEqual(it["images"][0], {"n": 0, "mediaType": "image/png", "bytes": len(PNG_1x1)})
        # 画像は queued_command の prompt と user の content の両方に入る【実測】
        self.assertEqual([(i["queued"], len(i["images"])) for i in with_images], [(True, 1), (False, 1)])
        # 覚えているのは行の位置だけ。その1行を読み直すと画像が取り出せる
        for i in with_images:
            off = b.image_row(i["id"])
            self.assertIsNotNone(off)
            with open(FIX / "vscode.jsonl", "rb") as f:
                f.seek(off)
                row = json.loads(f.readline())
            self.assertEqual(extract_images(row), [("image/png", PNG_1x1_B64)])
        plain = next(i for i in kinds(b, "instruction") if not i["images"])
        self.assertEqual(plain["images"], [])
        self.assertIsNone(b.image_row(plain["id"]))
        self.assertIsNone(b.image_row("無い id"))

    def test_image_only_instruction_has_empty_text(self):
        # 画像だけを貼った指示（テキストのブロックが無い）【実測：元の会話ログの 2644 行目】。
        # 最初の読み取り器はこの行を数えない（_text_of が None）ので、照合の試験には入れない
        rs = rows("image_only.jsonl")
        off, row = next((o, r) for o, r in rs if r.get("type") == "user" and not r.get("isMeta"))
        self.assertEqual([p["type"] for p in row["message"]["content"]], ["image"])
        b = build("image_only.jsonl")
        self.assertEqual(kinds(b, "instruction"), [{"kind": "instruction", "id": row["uuid"], "ts": row["timestamp"],
                                                    "text": "", "queued": False,
                                                    "images": [{"n": 0, "mediaType": "image/png",
                                                                "bytes": len(PNG_1x1)}]}])
        self.assertEqual(b.image_row(row["uuid"]), off)
        # 作業中に送った指示でも同じ（手本の queued_command から text ブロックを外す）
        att = copy.deepcopy(next(r for _, r in rows("vscode.jsonl") if r.get("type") == "attachment"
                                 and isinstance(r["attachment"].get("prompt"), list)
                                 and any(p.get("type") == "image" for p in r["attachment"]["prompt"])))
        att["attachment"]["prompt"] = [p for p in att["attachment"]["prompt"] if p.get("type") == "image"]
        b = UnitBuilder()
        b.feed(att, 0)
        it = kinds(b, "instruction")[0]
        self.assertEqual((it["id"], it["text"], it["queued"], len(it["images"])),
                         (att["attachment"]["source_uuid"], "", True, 1))
        self.assertEqual(b.image_row(it["id"]), 0)
        # ツール結果の行に画像が並んでいても指示にしない（origin の無い古い形でも）
        b = UnitBuilder()
        b.feed({"type": "user", "uuid": "u1", "timestamp": TS, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x"}, row["message"]["content"][0]]}}, 0)
        self.assertEqual(count(b, "instruction"), 0)


class BrokenRowTest(unittest.TestCase):
    def test_broken_row_counted_not_raised(self):
        broken = [
            "行ではない",
            {"type": "user", "message": "本文だけ"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": "ls"}]}},
            {"type": "attachment", "attachment": ["queued_command"]},
        ]
        b = UnitBuilder()
        for k, r in enumerate(broken):
            b.feed(r, k)
        self.assertEqual(b.bad_rows, len(broken))
        # 壊れた行の後も、普通の行は同じように読める
        for off, r in rows("tmux_rc.jsonl"):
            b.feed(r, 100 + off)
        want = build("tmux_rc.jsonl")
        self.assertEqual([it["id"] for it in b.items_since(0)], [it["id"] for it in want.items_since(0)])
        self.assertEqual(b.state()["queue"], want.state()["queue"])
        self.assertEqual(b.bad_rows, len(broken))


class ItemsSinceTest(unittest.TestCase):
    def test_items_since_splits_by_row_offset(self):
        rs = rows("tmux_rc.jsonl")
        b = build("tmux_rc.jsonl")
        full = b.items_since(0)
        self.assertTrue(full)
        self.assertTrue(all(not k.startswith("_") for it in full for k in it))
        # mid より前の行だけを読んだ単位 ＋ items_since(mid) ＝ 全部（cursor でつないでも抜け・重複が無い）
        mid = rs[len(rs) // 2][0]
        head = UnitBuilder()
        for off, r in rs:
            if off < mid:
                head.feed(r, off)
        tail = b.items_since(mid)
        self.assertTrue(tail and len(tail) < len(full))
        self.assertEqual([it["id"] for it in head.items_since(0) + tail], [it["id"] for it in full])
        self.assertEqual(b.items_since(rs[-1][0] + 1), [])


class FixtureTest(unittest.TestCase):
    """手本に元の本文が残っていないこと（git に入れるので毎回確かめる）。"""

    TOKEN = re.compile(r"(|文\d+|<(task-notification|agent-message|(?:command|local-command|bash)-[a-z-]+)>文\d+</\2>)")

    def _walk(self, v, key, where):
        if isinstance(v, dict):
            for k, x in v.items():
                self.assertTrue(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}|文\d+", k), where)
                self._walk(x, k, where)
        elif isinstance(v, list):
            for x in v:
                self._walk(x, key, where)
        elif isinstance(v, str) and key not in KEEP_KEYS:
            self.assertTrue(self.TOKEN.fullmatch(v) or v in (CWD, PNG_1x1_B64), f"{where} {key}")

    def test_fixtures_hold_no_original_text(self):
        files = sorted(FIX.glob("*.jsonl"))
        self.assertEqual([f.name for f in files],
                         ["compact_error.jsonl", "image_only.jsonl", "tmux_rc.jsonl", "tmux_rc_cleared.jsonl",
                          "vscode.jsonl"])
        for f in files:
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text, f.name)
            self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{200,}", text), f.name)   # 長い base64 が残っていない
            for n, line in enumerate(text.splitlines(), 1):
                self._walk(json.loads(line), None, f"{f.name}:{n}")

    def test_fixtures_hold_no_real_ids(self):
        """手本の ID は全部、作り物（FakeIds）。本物の会話ログの ID を公開の手本に残さない。"""
        for f in sorted(FIX.glob("*.jsonl")):
            text = f.read_text(encoding="utf-8")
            found = _REAL_ID.findall(text)
            self.assertTrue(found, f.name)
            for x in found:
                fake = x.startswith(FAKE_UUID_PREFIX) if "-" in x else x.split("_", 1)[1].startswith("FIXTURE")
                self.assertTrue(fake, f"{f.name}: {x}")

    def test_fake_ids_keep_the_shape_and_the_links(self):
        ids = FakeIds()
        a, b, m, t = ("11111111-2222-4333-8444-555555555555", "66666666-7777-4888-8999-aaaaaaaaaaaa",
                      "msg_01ABCDEFGHIJKLMNOPQRSTUV", "toolu_01abcdefghijklmnopqrstuv")
        out = ids.apply(f'{{"uuid":"{a}","parentUuid":"{b}","id":"{m}","tool_use_id":"{t}","again":"{a}","n":"abc-def"}}')
        for real in (a, b, m, t):
            self.assertNotIn(real, out)
        row = json.loads(out)
        self.assertEqual((row["uuid"], row["again"]), (ids.fake(a), ids.fake(a)))          # 同じ ID は同じ作り物
        self.assertNotEqual(row["uuid"], row["parentUuid"])
        self.assertRegex(row["uuid"], r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}\Z")   # 形は UUID のまま
        self.assertEqual((len(row["id"]), len(row["tool_use_id"])), (len(m), len(t)))      # msg_・toolu_ は同じ長さ
        self.assertTrue(row["id"].startswith("msg_FIXTURE") and row["tool_use_id"].startswith("toolu_FIXTURE"))
        self.assertEqual(row["n"], "abc-def")                                              # ID でないものは触らない
        self.assertEqual(len({ids.fake(x) for x in (a, b, m, t)}), 4)

    def test_scrub_replaces_text_keys_and_images(self):
        s = Scrubber()
        row = {"type": "user", "uuid": "u1", "cwd": "/Users/alice/p",
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "秘密の本文"},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]},
               "toolUseResult": {"answers": {"好きな色はどれですか？": "赤"}},
               "empty": "", "note": "<task-notification>終わった</task-notification>", "again": "秘密の本文"}
        out = s.row(row)
        self.assertEqual((out["type"], out["uuid"], out["cwd"]), ("user", "u1", CWD))
        self.assertEqual(out["message"]["content"][0]["text"], "文1")
        self.assertEqual(out["message"]["content"][1]["source"],
                         {"type": "base64", "media_type": "image/png", "data": PNG_1x1_B64})
        self.assertEqual(out["toolUseResult"]["answers"], {"文2": "文3"})
        self.assertEqual((out["empty"], out["again"]), ("", "文1"))
        self.assertEqual(out["note"], "<task-notification>文4</task-notification>")
        # 一覧サーバーが先頭の札で見分ける行は、札の名前だけを残す（属性と本文は消す）
        more = s.row({"a": "<command-name>/clear</command-name>\n<command-args></command-args>",
                      "b": "<local-command-stdout>秘密</local-command-stdout>", "c": "<bash-input>ls ~/x</bash-input>",
                      "d": '<agent-message from="a07">秘密の報告</agent-message>', "e": "<b>太字</b>"})
        self.assertEqual(more, {"a": "<command-name>文5</command-name>",
                                "b": "<local-command-stdout>文6</local-command-stdout>",
                                "c": "<bash-input>文7</bash-input>", "d": "<agent-message>文8</agent-message>",
                                "e": "文9"})

    def test_scrub_stops_when_original_text_survives(self):
        # 名前の形をした鍵は置き換えないので、問いの文がそのまま鍵に残る → 止まる
        q = "Is_this_fixture_really_safe"
        row = {"type": "user", "toolUseResult": {"questions": [{"question": q}], "answers": {q: "Yes"}}}
        with self.assertRaises(LeakError):
            Scrubber().row(row, 7)


if __name__ == "__main__":
    unittest.main()
