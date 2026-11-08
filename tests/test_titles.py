"""bblist/titles.py の試験。python3 -m unittest discover -s tests -t . -p 'test_titles.py' -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bblist.sessions import Catalog
from bblist.titles import TAIL_BYTES, LogTitles

SID = "11111111-2222-3333-4444-555555555555"
PROC = "Sun Oct 25 14:01:12 2026"


def line(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n"


class TitlesCase(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.projects = Path(t.name) / "projects"
        (self.projects / "folder").mkdir(parents=True)
        self.log = self.projects / "folder" / f"{SID}.jsonl"
        self.clock = 1000.0
        self.titles = LogTitles(self.projects, clock=lambda: self.clock)

    def test_uses_the_last_ai_title(self):
        self.log.write_text(line("ai-title", aiTitle="前") + line("user") + line("ai-title", aiTitle="後"), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "後")

    def test_custom_title_comes_before_ai_title(self):
        self.log.write_text(line("custom-title", customTitle="付けた名前") + line("ai-title", aiTitle="自動"), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "付けた名前")

    def test_no_log_or_no_title_is_none(self):
        self.assertIsNone(self.titles.title(SID))
        self.log.write_text(line("user"), encoding="utf-8")
        self.clock += 60
        self.assertIsNone(self.titles.title(SID))

    def test_only_the_tail_is_read(self):
        with self.log.open("w", encoding="utf-8") as f:
            f.write(line("ai-title", aiTitle="遠すぎる"))
            f.write(line("user", pad="x" * (TAIL_BYTES + 10)))
            f.write(line("ai-title", aiTitle="末尾"))
        with mock.patch("os.read", wraps=os.read) as read:
            self.assertEqual(self.titles.title(SID), "末尾")
        self.assertLessEqual(sum(c.args[1] for c in read.call_args_list), TAIL_BYTES)

    def test_title_keeps_when_it_scrolls_out_of_the_tail(self):
        self.log.write_text(line("ai-title", aiTitle="覚えた題"), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "覚えた題")
        with self.log.open("a", encoding="utf-8") as f:
            f.write(line("user", pad="x" * (TAIL_BYTES + 10)))
        self.assertEqual(self.titles.title(SID), "覚えた題")

    def test_unchanged_log_is_not_opened_again(self):
        self.log.write_text(line("ai-title", aiTitle="題"), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "題")
        with mock.patch("os.open", wraps=os.open) as opened:
            self.assertEqual(self.titles.title(SID), "題")
        opened.assert_not_called()

    def test_changed_title_is_picked_up(self):
        self.log.write_text(line("ai-title", aiTitle="前"), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "前")
        with self.log.open("a", encoding="utf-8") as f:
            f.write(line("ai-title", aiTitle="後の題"))
        self.assertEqual(self.titles.title(SID), "後の題")

    def test_missing_log_is_not_searched_every_time(self):
        self.assertIsNone(self.titles.title(SID))
        with mock.patch.object(Path, "glob", wraps=None, side_effect=AssertionError("探し直した")):
            self.assertIsNone(self.titles.title(SID))
        self.log.write_text(line("ai-title", aiTitle="あとから"), encoding="utf-8")
        self.clock += 60
        self.assertEqual(self.titles.title(SID), "あとから")

    def test_newer_of_two_copies_is_used(self):
        (self.projects / "old").mkdir()
        old = self.projects / "old" / f"{SID}.jsonl"
        old.write_text(line("ai-title", aiTitle="古い控え"), encoding="utf-8")
        os.utime(old, (1, 1))
        self.log.write_text(line("ai-title", aiTitle="新しい"), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "新しい")

    def test_bad_ids_and_bad_values_are_ignored(self):
        for bad in (None, 5, "", "../x", "*", "a" * 81):
            self.assertIsNone(self.titles.title(bad))
        self.log.write_text("".join((
            line("ai-title", aiTitle="正しい題"),
            "{壊れた行 ai-title\n",
            line("ai-title", aiTitle=5),
            line("ai-title", aiTitle="   "),
            '{"type":"ai-title","aiTitle":"\\ud83d"}\n',
            '{"type":"user","text":"\\"ai-title\\""}\n',
        )), encoding="utf-8")
        self.assertEqual(self.titles.title(SID), "正しい題")

    def test_title_is_squashed_and_cut(self):
        self.log.write_text(line("ai-title", aiTitle="  一\n二\t三 " + "長" * 500), encoding="utf-8")
        got = self.titles.title(SID)
        self.assertTrue(got.startswith("一 二 三 長"))
        self.assertEqual(len(got), 200)

    def test_links_and_folders_are_not_followed(self):
        real = self.projects / "real.txt"
        real.write_text(line("ai-title", aiTitle="リンクの先"), encoding="utf-8")
        self.log.symlink_to(real)
        self.assertIsNone(self.titles.title(SID))
        self.log.unlink()
        self.log.mkdir()
        self.clock += 60
        self.assertIsNone(self.titles.title(SID))

    def test_keep_forgets_other_sessions(self):
        self.log.write_text(line("ai-title", aiTitle="題"), encoding="utf-8")
        self.titles.title(SID)
        self.titles.keep(set())
        self.log.unlink()
        self.assertIsNone(self.titles.title(SID))


class CatalogNameCase(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(os.path.realpath(t.name))
        self.claude = self.tmp / ".claude"
        (self.claude / "sessions").mkdir(parents=True)
        (self.claude / "projects" / "folder").mkdir(parents=True)
        (self.tmp / "work").mkdir()

    def name_of(self, source, name="marketing-8f", title="題の文", session_id=SID):
        d = {"pid": 101, "sessionId": session_id, "cwd": str(self.tmp / "work"), "startedAt": 1, "procStart": PROC,
             "version": "2.1.306", "kind": "interactive", "entrypoint": "claude-vscode", "name": name,
             "updatedAt": 2, "status": "idle", "statusUpdatedAt": 2, "bridgeSessionId": "session_101"}
        if source is not None:
            d["nameSource"] = source
        (self.claude / "sessions" / "101.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        log = self.claude / "projects" / "folder" / f"{SID}.jsonl"
        log.write_text(line("ai-title", aiTitle=title) if title else "", encoding="utf-8")
        cat = Catalog(self.claude, [], self.tmp, [], start_of=lambda p: PROC, secret=bytes(32),
                      git=lambda c: None, alive=lambda p: True)
        return cat.overview()["sessions"][0]["name"]

    def test_derived_name_is_replaced_by_the_log_title(self):
        self.assertEqual(self.name_of("derived"), "題の文")

    def test_derived_name_stays_without_a_title(self):
        self.assertEqual(self.name_of("derived", title=None), "marketing-8f")

    def test_user_and_auto_names_are_kept_and_the_log_is_not_read(self):
        for source in ("user", "auto", None):
            with mock.patch("bblist.titles.LogTitles.title", side_effect=AssertionError("読まない")):
                self.assertEqual(self.name_of(source, name="付けた名前"), "付けた名前")

    def test_session_id_of_another_shape_keeps_the_row(self):
        self.assertEqual(self.name_of("derived", session_id=["形が違う"]), "marketing-8f")

    def test_name_is_nfc(self):
        self.assertEqual(self.name_of("derived", title="カ\u3099題"), "\u30ac題")


if __name__ == "__main__":
    unittest.main()
