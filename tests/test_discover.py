"""bblist/discover.py の試験。python3 -m unittest tests.test_discover -v

Tailscale の実体の探し方（設定 → PATH → Homebrew → /Applications → ~/Applications）・IP と MagicDNS の名前の取り方・
会話ログの作業フォルダ（cwd）の集め方。本物の Tailscale と本物の ~/.claude には触れない（偽の実体と tmp を使う）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bblist import discover
from bblist.discover import (TranscriptFolders, backend_state, find_tailscale, magicdns_name, magicdns_of, magicdns_status,
                             run_tailscale, tailscale_ip)

APP = "Applications/Tailscale.app/Contents/MacOS/Tailscale"
FIXED = ("opt/homebrew/bin/tailscale", "usr/local/bin/tailscale", f"{APP}")


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class FindTailscaleTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.root, self.home = self.tmp / "root", self.tmp / "home"
        self.root.mkdir()
        self.home.mkdir()

    def exe(self, path: Path, mode: int = 0o755) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, mode)
        return path

    def find(self, config_value=None, which=lambda name: None):
        return find_tailscale(config_value, which, self.home, root=self.root)

    def test_find_tailscale_order(self):
        # 6つの場所に偽の実体を置き、上から1つずつ外して、次に当たるものが決まった順であることを見る
        configured = self.exe(self.tmp / "mine" / "tailscale")
        on_path = self.exe(self.tmp / "pathbin" / "tailscale")
        brew = self.exe(self.root / FIXED[0])
        local = self.exe(self.root / FIXED[1])
        app = self.exe(self.root / FIXED[2])
        home_app = self.exe(self.home / APP)
        asked = []

        def which(name):
            asked.append(name)
            return str(on_path) if on_path.exists() else None

        for found in (configured, on_path, brew, local, app, home_app):
            with self.subTest(expect=str(found)):
                self.assertEqual(self.find(str(configured), which), str(found))
            found.unlink()
        self.assertIsNone(self.find(str(configured), which))
        self.assertEqual(set(asked), {"tailscale"})                 # PATH に聞くのは tailscale という名前だけ

    def test_each_place_alone_is_found(self):
        for rel in FIXED:
            with self.subTest(rel=rel):
                p = self.exe(self.root / rel)
                self.assertEqual(self.find(), str(p))
                p.unlink()
        p = self.exe(self.home / APP)
        self.assertEqual(self.find(), str(p))

    def test_the_homebrew_place_wins_over_the_app(self):
        brew, app = self.exe(self.root / FIXED[0]), self.exe(self.root / FIXED[2])
        self.assertEqual(self.find(), str(brew))
        self.assertNotEqual(self.find(), str(app))

    def test_default_places_are_the_real_ones(self):
        # 本物の場所の綴りを見張る（root を渡さなければ / から）
        seen = []
        with mock.patch.object(discover.os.path, "isfile", side_effect=lambda p: seen.append(p) or False):
            self.assertIsNone(find_tailscale(None, lambda name: None, Path("/Users/someone")))
        self.assertEqual(seen, ["/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale",
                                "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                                "/Users/someone/Applications/Tailscale.app/Contents/MacOS/Tailscale"])

    def test_a_configured_path_that_is_gone_falls_through(self):
        brew = self.exe(self.root / FIXED[0])
        self.assertEqual(self.find(str(self.tmp / "gone" / "tailscale")), str(brew))

    def test_not_executable_or_not_a_file_is_skipped(self):
        self.exe(self.root / FIXED[0], mode=0o644)
        (self.root / FIXED[1]).mkdir(parents=True)                  # フォルダ
        local = self.exe(self.root / FIXED[2])
        self.assertEqual(self.find(), str(local))

    def test_the_which_answer_must_be_a_real_executable_too(self):
        self.assertIsNone(self.find(which=lambda name: str(self.tmp / "nowhere")))


class FakeTailscale(TmpCase):
    def fake(self, body: str) -> str:
        app = self.tmp / "tailscale"
        app.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        app.chmod(0o755)
        return str(app)

    def fake_app(self, cli_ip) -> str:
        """アプリの中の Tailscale のまね。SHLVL・TERM などが無いと窓のアプリとして立ち上がろうとし、
        失敗の文を標準出力に出して 0 で終わる【実測 2026-10-27：launchd の環境で ip -4 を呼んだとき】。
        TAILSCALE_BE_CLI=1 なら CLI として cli_ip を答える（cli_ip が None なら、いつも窓のアプリの側に行く）。"""
        cli = f"if [ \"${{TAILSCALE_BE_CLI:-}}\" = 1 ]; then echo {cli_ip}; exit 0; fi\n" if cli_ip else ""
        return self.fake(cli + "printf 'The Tailscale GUI failed to start: (Tailscale.CLIError error 3.)\\n\\n'\n")


class TailscaleIpTest(FakeTailscale):
    def test_tailscale_ip_asks_the_app_as_cli_under_launchd_env(self):
        # launchd の plist は PATH と PYTHONUTF8 だけを渡す。それでもアプリを CLI として呼べる
        app = self.fake_app("100.101.102.103")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUTF8": "1"}, clear=True):
            self.assertEqual(tailscale_ip(app), "100.101.102.103")

    def test_tailscale_ip_ignores_an_answer_that_is_not_an_ipv4(self):
        # 0 で終わっても、最初の語が IPv4 でなければ取れなかったことにする（"The" を待ち受けの名前にしない）
        for cli_ip in (None, "not-an-ip", "fd7a:115c:a1e0::1"):
            with self.subTest(cli_ip=cli_ip):
                self.assertIsNone(tailscale_ip(self.fake_app(cli_ip)))

    def test_it_asks_for_ip_dash_4(self):
        app = self.fake('[ "$1 $2" = "ip -4" ] && echo 100.64.0.7\n')
        self.assertEqual(tailscale_ip(app), "100.64.0.7")

    def test_missing_failing_or_hanging_tailscale_is_none(self):
        self.assertIsNone(tailscale_ip(self.fake("exit 1\n")))
        hang = self.fake("exec sleep 30\n")
        t0 = time.monotonic()
        with mock.patch.object(discover, "TAILSCALE_TIMEOUT", 0.5):
            self.assertIsNone(tailscale_ip(hang))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertLess(discover.TAILSCALE_TIMEOUT, 10)             # 本番の待ちも 10秒より短い

    def test_no_tailscale_anywhere_is_none(self):
        with mock.patch.object(discover, "find_tailscale", return_value=None):
            self.assertIsNone(tailscale_ip())
            self.assertIsNone(magicdns_name())

    def test_the_configured_path_is_the_one_run(self):
        with mock.patch.object(discover, "find_tailscale", wraps=discover.find_tailscale) as found:
            tailscale_ip(self.fake("echo 100.64.0.7\n"))
        self.assertEqual(found.call_args.args[0], str(self.tmp / "tailscale"))

    def test_the_tailscale_net_is_the_100_64_range(self):
        import ipaddress
        self.assertEqual(discover.TAILSCALE_NET, ipaddress.ip_network("100.64.0.0/10"))


class MagicDnsNameTest(FakeTailscale):
    def name(self, app):
        return magicdns_name(app)

    def test_reads_self_dnsname_without_the_trailing_dot(self):
        app = self.fake('[ "$1 $2" = "status --json" ] && [ "${TAILSCALE_BE_CLI:-}" = 1 ] && '
                        'echo \'{"Self": {"DNSName": "Mac.tailnet-1234.ts.net.", "HostName": "mac"}}\'\n')
        self.assertEqual(self.name(app), "mac.tailnet-1234.ts.net")

    def test_anything_odd_means_none(self):
        for body in ("exit 1\n", "echo not-json\n", "echo '[]'\n", "echo '{}'\n", "echo '{\"Self\": {}}'\n",
                     "echo '{\"Self\": {\"DNSName\": 5}}'\n", "echo '{\"Self\": {\"DNSName\": \"\"}}'\n",
                     "echo '{\"Self\": {\"DNSName\": \"bad name.ts.net.\"}}'\n",
                     "echo '{\"Self\": {\"DNSName\": \"a&b=c.ts.net.\"}}'\n"):
            self.assertIsNone(self.name(self.fake(body)), body)

    def test_a_missing_or_hanging_tailscale_is_none(self):
        hang = self.fake("exec sleep 30\n")
        t0 = time.monotonic()
        with mock.patch.object(discover, "TAILSCALE_TIMEOUT", 0.5):
            self.assertIsNone(self.name(hang))
        self.assertLess(time.monotonic() - t0, 5)


class MagicDnsStatusTest(FakeTailscale):
    """bbctl pair と install.sh --check が使う：MagicDNS が有効か（CurrentTailnet.MagicDNSEnabled）と、この Mac の名前。"""

    def status(self, doc: str):
        return magicdns_of(self.fake(f"echo '{doc}'\n"))

    def test_reads_the_flag_and_the_name_as_cli(self):
        app = self.fake('[ "$1 $2" = "status --json" ] && [ "${TAILSCALE_BE_CLI:-}" = 1 ] && '
                        'echo \'{"CurrentTailnet": {"MagicDNSEnabled": true}, "Self": {"DNSName": "Mac.tn-1.ts.net."}}\'\n')
        self.assertEqual(magicdns_of(app), (True, "mac.tn-1.ts.net"))
        self.assertEqual(magicdns_status(app), (True, "mac.tn-1.ts.net"))        # 設定の tailscale を探してから同じ答え

    def test_a_disabled_flag_is_false_and_keeps_the_name(self):
        self.assertEqual(self.status('{"CurrentTailnet": {"MagicDNSEnabled": false}, "Self": {"DNSName": "mac.ts.net."}}'),
                         (False, "mac.ts.net"))

    def test_a_missing_or_odd_flag_is_unknown_not_false(self):
        for doc in ('{"Self": {"DNSName": "mac.ts.net."}}', '{"CurrentTailnet": {}, "Self": {"DNSName": "mac.ts.net."}}',
                    '{"CurrentTailnet": {"MagicDNSEnabled": "true"}, "Self": {"DNSName": "mac.ts.net."}}',
                    '{"CurrentTailnet": [], "Self": {"DNSName": "mac.ts.net."}}'):
            with self.subTest(doc=doc):
                self.assertEqual(self.status(doc), (None, "mac.ts.net"))

    def test_a_missing_or_odd_name_is_none(self):
        for name in ('', '5', '"bad name.ts.net."', '"a&b=c.ts.net."'):
            with self.subTest(name=name):
                self.assertEqual(self.status('{"CurrentTailnet": {"MagicDNSEnabled": true}, "Self": {"DNSName": %s}}' % (name or '""')),
                                 (True, None))
        self.assertEqual(self.status('{"CurrentTailnet": {"MagicDNSEnabled": true}}'), (True, None))

    def test_anything_unreadable_is_unknown(self):
        for body in ("exit 1\n", "echo not-json\n", "echo '[]'\n"):
            self.assertEqual(magicdns_of(self.fake(body)), (None, None), body)
        self.assertEqual(magicdns_of(str(self.tmp / "missing")), (None, None))
        t0 = time.monotonic()
        with mock.patch.object(discover, "TAILSCALE_TIMEOUT", 0.5):
            self.assertEqual(magicdns_of(self.fake("exec sleep 30\n")), (None, None))
        self.assertLess(time.monotonic() - t0, 5)

    def test_no_tailscale_anywhere_is_unknown(self):
        with mock.patch.object(discover, "find_tailscale", return_value=None):
            self.assertEqual(magicdns_status(), (None, None))


class BackendStateTest(FakeTailscale):
    """install.sh --check が使う：tailscale status --json の BackendState（Running なら動いている）。"""

    def test_reads_backend_state_as_cli(self):
        app = self.fake('[ "$1 $2" = "status --json" ] && [ "${TAILSCALE_BE_CLI:-}" = 1 ] && '
                        'echo \'{"BackendState": "Running", "Self": {"DNSName": "mac.ts.net."}}\'\n')
        self.assertEqual(backend_state(app), "Running")
        self.assertEqual(backend_state(self.fake('echo \'{"BackendState": "Stopped"}\'\n')), "Stopped")

    def test_anything_odd_means_none(self):
        for body in ("exit 1\n", "echo not-json\n", "echo '[]'\n", "echo '{}'\n", "echo '{\"BackendState\": 5}'\n",
                     "echo '{\"BackendState\": \"" + "x" * 100 + "\"}'\n"):
            self.assertIsNone(backend_state(self.fake(body)), body)
        self.assertIsNone(backend_state(str(self.tmp / "missing")))

    def test_a_hanging_tailscale_is_none(self):
        t0 = time.monotonic()
        with mock.patch.object(discover, "TAILSCALE_TIMEOUT", 0.5):
            self.assertIsNone(backend_state(self.fake("exec sleep 30\n")))
        self.assertLess(time.monotonic() - t0, 5)

    def test_run_tailscale_runs_exactly_the_given_executable(self):
        self.assertEqual(run_tailscale(self.fake('echo "$@"\n'), "a", "b").strip(), "a b")


# ---------------------------------------------------------------- 会話ログの作業フォルダ

def jsonl(*rows) -> bytes:
    return b"".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for r in rows)


class TranscriptFoldersCase(TmpCase):
    def setUp(self):
        super().setUp()
        self.projects = self.tmp / "projects"
        self.projects.mkdir()

    def put(self, folder: str, name: str, *rows, raw: bytes | None = None, age: float = 0.0) -> Path:
        d = self.projects / folder
        d.mkdir(exist_ok=True)
        p = d / f"{name}.jsonl"
        p.write_bytes(raw if raw is not None else jsonl(*rows))
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def user(self, cwd, **extra):
        return {"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}, **extra}

    def make(self, **kw) -> TranscriptFolders:
        t = TranscriptFolders(self.projects, **kw)
        t.refresh()
        return t


class TranscriptFoldersTest(TranscriptFoldersCase):
    def test_collects_the_cwd_of_every_transcript_newest_first(self):
        self.put("-a", "s1", {"type": "summary"}, self.user("/work/old"), age=300)
        self.put("-a", "s2", self.user("/work/new"), age=10)
        self.put("-b", "s3", self.user("/work/mid"), age=100)
        self.assertEqual(self.make().folders, ["/work/new", "/work/mid", "/work/old"])

    def test_the_same_folder_is_listed_once_at_its_newest_time(self):
        self.put("-a", "s1", self.user("/work/x"), age=500)
        self.put("-a", "s2", self.user("/work/y"), age=300)
        self.put("-b", "s3", self.user("/work/x"), age=10)
        self.assertEqual(self.make().folders, ["/work/x", "/work/y"])

    def test_several_cwds_under_one_project_folder_are_all_found(self):
        # フォルダ名は日本語を - に潰すので、別のフォルダの会話ログが同じ置き場に入る（本物で4組）
        self.put("-same", "s1", self.user("/work/りんご"), age=30)
        self.put("-same", "s2", self.user("/work/テニス"), age=20)
        self.assertEqual(set(self.make().folders), {"/work/りんご", "/work/テニス"})

    def test_only_the_top_level_cwd_of_early_lines_is_used(self):
        row = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "cwd is /elsewhere"}]},
               "toolUseResult": {"cwd": "/nested"}}
        self.put("-a", "s1", {"type": "file-history-snapshot"}, row, self.user("/work/real"))
        self.assertEqual(self.make().folders, ["/work/real"])

    def test_files_without_a_usable_cwd_are_skipped(self):
        self.put("-a", "empty", raw=b"")
        self.put("-a", "nocwd", {"type": "summary"})
        self.put("-a", "relative", self.user("work/relative"))
        self.put("-a", "nul", self.user("/work/a\u0000b"))
        self.put("-a", "number", self.user(5))
        self.put("-a", "garbage", raw=b'not json "cwd"\n{"cwd": "/work/after-garbage"}\n')
        (self.projects / "-a" / "notes.txt").write_bytes(jsonl(self.user("/work/wrong-extension")))
        (self.projects / "-a" / "s.jsonl.bak").write_bytes(jsonl(self.user("/work/bak")))
        (self.projects / "stray-file").write_bytes(jsonl(self.user("/work/not-in-a-folder")))
        self.assertEqual(self.make().folders, ["/work/after-garbage"])

    def test_only_the_head_of_a_file_is_read(self):
        seen = []
        real_read = os.read

        def spy(fd, n):
            seen.append(n)
            return real_read(fd, n)

        big = jsonl({"type": "summary", "pad": "x" * (discover.HEAD_BYTES * 2)}, self.user("/work/too-far"))
        self.put("-a", "far", raw=big)
        self.put("-a", "near", self.user("/work/near"))
        with mock.patch.object(discover.os, "read", side_effect=spy):
            self.assertEqual(self.make().folders, ["/work/near"])
        self.assertTrue(seen and max(seen) <= discover.HEAD_BYTES, seen)

    def test_a_partial_last_line_in_the_head_is_not_used(self):
        pad = {"type": "summary", "pad": "x" * (discover.HEAD_BYTES - 70)}
        raw = jsonl(pad, self.user("/work/cut-off"))              # cwd の行は、読む範囲の終わりで途中で切れる
        self.assertGreater(len(raw), discover.HEAD_BYTES)
        self.assertIn(b'"cwd"', raw[:discover.HEAD_BYTES])        # 切れた行にも cwd の目印はある
        self.put("-a", "cut", raw=raw)
        self.assertEqual(self.make().folders, [])

    def test_fifo_symlink_and_directory_named_like_a_transcript_are_not_followed(self):
        d = self.projects / "-a"
        d.mkdir()
        os.mkfifo(d / "pipe.jsonl")                               # 開くと待つ種類
        real = self.tmp / "real.jsonl"
        real.write_bytes(jsonl(self.user("/work/through-link")))
        (d / "link.jsonl").symlink_to(real)
        (d / "dir.jsonl").mkdir()
        self.put("-a", "ok", self.user("/work/ok"))
        self.assertEqual(self.make().folders, ["/work/ok"])

    def test_a_missing_projects_dir_is_empty(self):
        t = TranscriptFolders(self.tmp / "nowhere")
        t.refresh()
        self.assertEqual(t.folders, [])

    def test_at_most_max_folders_the_newest_ones(self):
        for i in range(10):
            self.put("-a", f"s{i}", self.user(f"/work/f{i}"), age=1000 - i)
        self.assertEqual(self.make(max_folders=3).folders, ["/work/f9", "/work/f8", "/work/f7"])


class TranscriptFoldersCacheTest(TranscriptFoldersCase):
    def opened(self):
        """open された .jsonl の数を数える窓口。"""
        paths = []
        real = os.open

        def spy(path, *a, **kw):
            if str(path).endswith(".jsonl"):
                paths.append(str(path))
            return real(path, *a, **kw)

        return paths, mock.patch.object(discover.os, "open", side_effect=spy)

    def test_nothing_changed_means_no_rescan_and_no_open(self):
        for i in range(5):
            self.put(f"-d{i}", "s", self.user(f"/work/f{i}"))
        t = self.make()
        paths, patch = self.opened()
        with patch:
            self.assertFalse(t.refresh())
            self.assertFalse(t.refresh())
        self.assertEqual(paths, [])
        self.assertEqual(len(t.folders), 5)

    def test_a_growing_transcript_does_not_rescan_but_a_new_one_does_and_only_the_new_one_is_opened(self):
        a = self.put("-a", "s1", self.user("/work/a"))
        t = self.make()
        with open(a, "ab") as f:
            f.write(jsonl({"type": "assistant", "message": {"content": "more"}}))   # 書き足し：作業フォルダの一覧は変わらない
        self.assertFalse(t.refresh())
        paths, patch = self.opened()
        self.put("-a", "s2", self.user("/work/b"))                  # 新しい会話ログ（置き場のフォルダの更新時刻が変わる）
        with patch:
            self.assertTrue(t.refresh())
        self.assertEqual([Path(p).name for p in paths], ["s2.jsonl"])
        self.assertEqual(set(t.folders), {"/work/a", "/work/b"})

    def test_a_new_project_folder_is_found(self):
        self.put("-a", "s1", self.user("/work/a"))
        t = self.make()
        self.put("-b", "s2", self.user("/work/b"))
        self.assertTrue(t.refresh())
        self.assertEqual(set(t.folders), {"/work/a", "/work/b"})

    def test_a_transcript_that_got_its_cwd_later_is_found_without_any_directory_change(self):
        # 会話ログができた直後は空で、あとから cwd の行が書かれる。置き場のフォルダの更新時刻は変わらない
        p = self.put("-a", "s1", raw=b"")
        t = self.make()
        self.assertEqual(t.folders, [])
        with open(p, "ab") as f:
            f.write(jsonl(self.user("/work/late")))
        self.assertTrue(t.refresh())
        self.assertEqual(t.folders, ["/work/late"])
        self.assertFalse(t.refresh())

    def test_a_deleted_transcript_removes_its_folder(self):
        a = self.put("-a", "s1", self.user("/work/a"))
        self.put("-b", "s2", self.user("/work/b"))
        t = self.make()
        a.unlink()
        self.assertTrue(t.refresh())
        self.assertEqual(t.folders, ["/work/b"])

    def test_a_rewritten_transcript_is_read_again(self):
        p = self.put("-a", "s1", self.user("/work/before"))
        t = self.make()
        p.unlink()
        self.put("-a", "s1", self.user("/work/after"))              # 同じ名前で作り直し（別の inode）
        self.assertTrue(t.refresh())
        self.assertEqual(t.folders, ["/work/after"])

    def test_a_modification_time_set_back_does_not_hide_a_new_file(self):
        # 更新時刻は touch で戻せる。変化の検出は変更時刻（ctime）で行う（戻せない）
        d = self.projects / "-a"
        self.put("-a", "s1", self.user("/work/a"))
        old = os.stat(d)
        t = self.make()
        self.put("-a", "s2", self.user("/work/b"))
        os.utime(d, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.utime(self.projects, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertTrue(t.refresh())
        self.assertEqual(set(t.folders), {"/work/a", "/work/b"})


if __name__ == "__main__":
    unittest.main()
