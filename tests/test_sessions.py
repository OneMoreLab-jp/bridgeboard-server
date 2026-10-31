"""bblist/sessions.py の試験。python3 -m unittest discover -s tests -t . -p 'test_sessions.py' -v

セッションの生死と procStart は辞書で差し替える（本物の ps は ps_start の試験でだけ呼ぶ）。
git は、worktree の試験とrun_git の試験だけ本物を tmp に作る。ほかは差し替える。
NFD の名前は、ディスクに本当に NFD で作って試す（APFS は書いた形のまま残す）。
"""
from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unicodedata
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from bblist.paths import folder_id as _folder_id, nfc
from bblist.sessions import Catalog, _alive, ps_start, run_git

PROC = "Sun Oct 25 14:01:12 2026"
SECRET = bytes(range(32))                 # rootId の塩。試験はここから rootId を導く（値を書き写さない）


def folder_id(path) -> str:
    return _folder_id(path, SECRET)


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def gitfile(wt: Path, common: Path) -> None:
    """偽の git の答えで worktree を作るとき、本物の worktree と同じ作りにする。
    toplevel に .git のファイル（gitdir: <common>/worktrees/<名前>）、その gitdir ファイルは toplevel の .git へ戻る。"""
    admin = common / "worktrees" / wt.name
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "gitdir").write_text(f"{wt}/.git\n", encoding="utf-8")
    (wt / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"),
                 ("core.hooksPath", str(path / "no-hooks"))):
        git(path, "config", k, v)
    git(path, "commit", "-q", "--allow-empty", "--no-verify", "-m", "init")
    return path


class CatalogCase(unittest.TestCase):
    """tmp に claude_dir/sessions・lab_dir/<カテゴリ>/<フォルダ>・home を作る土台。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.claude = self.home / ".claude"
        self.sessions = self.claude / "sessions"
        self.lab = self.home / "Projects"
        self.own = [self.home / "Library" / "Application Support" / "BridgeBoard",
                    self.home / "Library" / "Logs" / "BridgeBoard"]
        self.sessions.mkdir(parents=True)
        self.lab.mkdir()
        self.ps = {}            # pid -> ps が答える起動時刻
        self.ps_calls = []      # start_of を呼ばれた PID（ps を起動した回数）
        self.live = set()       # 生きている PID
        self.git_calls = []
        self.explode = set()    # start_of が例外を出す PID（1行の処理の不具合を作る）

    # ---- 道具

    def start_of(self, pid):
        self.ps_calls.append(pid)
        if pid in self.explode:
            raise RuntimeError("この行の処理が壊れた")
        return self.ps.get(pid)

    @contextlib.contextmanager
    def spy_opens(self):
        """ファイルを開いた道を集める。pathlib の read_text は builtins.open ではなく io.open を呼ぶので両方を見張る。"""
        opened = []

        def spy(real):
            def wrapper(file, *a, **kw):
                opened.append(str(file))
                return real(file, *a, **kw)
            return wrapper

        with mock.patch("builtins.open", spy(builtins.open)), mock.patch("io.open", spy(io.open)):
            yield opened

    def no_git(self, cwd):
        self.git_calls.append(cwd)
        return None

    def make_catalog(self, git=None, alive="fake"):
        return Catalog(self.claude, [(self.lab, 2)], self.home, self.own, start_of=self.start_of, secret=SECRET,
                       git=git or self.no_git, alive=self.live.__contains__ if alive == "fake" else alive)

    def write(self, name: str, text: str) -> Path:
        p = self.sessions / name
        p.write_text(text, encoding="utf-8")
        return p

    def data(self, pid: int, **over) -> dict:
        d = {"pid": pid, "sessionId": f"sid-{pid}", "cwd": str(self.tmp / "work" / f"p{pid}"),
             "startedAt": 1792936873127, "procStart": PROC, "version": "2.1.306", "kind": "interactive",
             "entrypoint": "claude-vscode", "name": f"s{pid}", "nameSource": "derived",
             "updatedAt": 1792941510789, "status": "idle", "statusUpdatedAt": 1792941510789,
             "bridgeSessionId": f"session_{pid}"}
        d.update(over)
        return d

    def add(self, pid: int, live=True, ps="same", drop=(), **over) -> dict:
        """sessions/<pid>.json を置き、生死と ps の答えを登録する。"""
        d = self.data(pid, **over)
        for k in drop:
            d.pop(k)
        self.write(f"{pid}.json", json.dumps(d, ensure_ascii=False))
        if live:
            self.live.add(pid)
        if ps == "same":
            self.ps[pid] = d.get("procStart")
        elif ps is not None:
            self.ps[pid] = ps
        return d

    @staticmethod
    def pids(cat) -> list:
        return [s["pid"] for s in cat.overview()["sessions"]]

    @staticmethod
    def project(cat, name):
        found = [p for p in cat.overview()["projects"] if p["name"] == name]
        assert len(found) == 1, (name, found)
        return found[0]

    def folder(self, *parts) -> Path:
        p = self.lab.joinpath(*parts)
        p.mkdir(parents=True)
        return p


class LiveSessionsTest(CatalogCase):
    def test_only_live_with_matching_procstart_and_bridge(self):
        self.add(101)                                           # 生きて一致（出る）
        self.add(102, live=False)                               # 死んだ PID
        self.add(103, ps="Mon Oct 26 10:00:00 2026")            # procStart 不一致（PID の使い回し）
        self.add(104, drop=("bridgeSessionId",))                # bridgeSessionId なし
        self.add(105, bridgeSessionId="")                       # 空の bridgeSessionId も同じ
        self.assertEqual(self.pids(self.make_catalog()), [101])

    def test_process_unknown_to_ps_is_not_live(self):
        self.add(101, ps=None)
        self.add(102, drop=("procStart",))                      # 記録に procStart が無いものも出さない
        self.assertEqual(self.pids(self.make_catalog()), [])

    def test_procstart_compared_after_collapsing_whitespace(self):
        # 1桁の日は ps が "Oct  5" と空白を2つ出す。ps_start は詰めるので、記録の側も詰めて比べる
        self.add(101, procStart="Mon Oct  5 09:00:00 2026", ps="Mon Oct 5 09:00:00 2026")
        self.assertEqual(self.pids(self.make_catalog()), [101])

    def test_never_opens_key_files(self):
        self.add(101)
        key = self.write(f"101.{'a' * 64}.key", "SECRET")
        with self.spy_opens() as opened:
            cat = self.make_catalog()
            cat.refresh()
        self.assertTrue(any(p.endswith("101.json") for p in opened), opened)   # 見張りが効いている
        self.assertFalse(any(p.endswith(".key") for p in opened), opened)
        self.assertTrue(key.exists())
        self.assertEqual(self.pids(cat), [101])

    def test_half_written_json_is_skipped_then_read(self):
        cat = self.make_catalog()
        self.write("102.json", '{"pid": 102, "sess')          # 書きかけ
        self.live.add(102)
        self.ps[102] = PROC
        cat.refresh()
        self.assertNotIn(102, self.pids(cat))
        self.add(102)                                         # 書き終わった
        self.assertTrue(cat.refresh())
        self.assertIn(102, self.pids(cat))

    def test_malformed_files_are_skipped_without_raising(self):
        for pid in (0, -1):                                   # os.kill(0, 0) は自分のプロセスグループに届く。生きていると見ない
            self.ps[pid] = PROC
        self.write("a.json", "[]")                            # 辞書でない
        self.write("b.json", json.dumps({"pid": "7", "cwd": "/x", "bridgeSessionId": "b", "procStart": PROC}))
        self.write("c.json", json.dumps(self.data(os.getpid(), cwd=None)))                  # cwd なし
        self.write("d.json", json.dumps(self.data(os.getpid(), bridgeSessionId=["x"])))     # 形が違う
        self.write("e.json", json.dumps(self.data(0)))
        self.write("f.json", json.dumps(self.data(-1)))
        self.write("g.json", json.dumps(self.data(True)))     # bool は PID でない
        self.write("h.json", "not json")
        (self.sessions / "i.json").write_bytes(b"\xff\xfe\x00")  # UTF-8 として読めない
        (self.sessions / "j.json").mkdir()                    # フォルダ
        self.write("k.json", json.dumps(self.data(os.getpid(), cwd="/x\0y")))   # NUL を含む cwd は realpath が ValueError を出す
        # 次の4つは、守りが無いと Catalog() 自身が例外で落ちる（__init__ が refresh を呼ぶため、サーバーが起動できない）
        self.ps[2 ** 40] = PROC
        self.write("l.json", json.dumps(self.data(2 ** 40)))                   # PID が大きすぎて os.kill が OverflowError
        self.write("m.json", json.dumps(self.data(os.getpid(), cwd="/tmp/\ud800")))   # 孤立サロゲート。UTF-8 にできない
        self.write("n.json", json.dumps(self.data(os.getpid(), name="\ud800")))       # cwd 以外の文字列でも同じ
        self.write("o.json", '{"pid": %d, "cwd": "/x", "bridgeSessionId": "b", "procStart": "%s", "updatedAt": NaN}'
                   % (os.getpid(), PROC))                                       # NaN は strict な JSON にできない
        self.write("p.json", "[" * 200000)                                      # 深すぎる入れ子は RecursionError
        self.ps[os.getpid()] = PROC
        cat = self.make_catalog(alive=None)                   # 既定の生死判定（os.kill）
        self.assertEqual(self.pids(cat), [])

    def test_pid_beyond_32_bits_is_never_listed_even_if_alive_says_yes(self):
        self.add(2 ** 40)
        self.add(2 ** 31)                                     # pid_t の上限（2**31 - 1）の1つ上
        self.add(101)
        cat = self.make_catalog(alive=lambda pid: True)
        self.assertEqual(self.pids(cat), [101])

    def test_default_alive_says_no_for_a_pid_kill_cannot_take(self):
        self.assertFalse(_alive(2 ** 40))                     # os.kill が OverflowError を出す
        self.assertFalse(_alive(dead_pid()))
        self.assertTrue(_alive(os.getpid()))

    def test_one_bad_row_does_not_hide_the_good_rows(self):
        self.add(101, updatedAt=400)
        self.add(102, updatedAt=300)                          # 確かめる段で例外
        self.add(103, updatedAt=200, cwd=str(self.tmp / "work" / "boom"))   # 組み立てる段で例外（git）
        self.add(104, updatedAt=100)
        self.explode.add(102)

        def git_that_fails_for_boom(cwd):
            if cwd.endswith("boom"):
                raise RuntimeError("この行の処理が壊れた")
            return None

        cat = self.make_catalog(git=git_that_fails_for_boom)
        ov = cat.overview()
        self.assertEqual([s["pid"] for s in ov["sessions"]], [101, 104])
        self.assertEqual(sum(p["sessionCount"] for p in ov["projects"]), 2)   # 壊れた行は数にも入らない
        self.assertEqual({p["id"] for p in ov["projects"]}, {s["projectId"] for s in ov["sessions"]})
        self.explode.clear()                                  # 直れば、次の見回りで出る
        self.assertTrue(cat.refresh())
        self.assertIn(102, self.pids(cat))

    def test_default_alive_uses_kill_zero(self):
        me, gone = os.getpid(), dead_pid()
        self.add(me, live=False)
        self.add(gone, live=False)
        cat = self.make_catalog(alive=None)
        self.assertEqual(self.pids(cat), [me])

    def test_missing_sessions_dir_gives_empty_list(self):
        for f in self.sessions.iterdir():
            f.unlink()
        self.sessions.rmdir()
        self.assertEqual(self.pids(self.make_catalog()), [])


class DuplicateTest(CatalogCase):
    def test_duplicate_flag_on_same_session_id(self):
        self.add(101, sessionId="same", bridgeSessionId="session_a")
        self.add(102, sessionId="same", bridgeSessionId="session_b")
        self.add(103, sessionId="other", bridgeSessionId="session_c")
        rows = {s["pid"]: s for s in self.make_catalog().overview()["sessions"]}
        self.assertTrue(rows[101]["duplicate"])
        self.assertTrue(rows[102]["duplicate"])
        self.assertFalse(rows[103]["duplicate"])

    def test_dead_process_does_not_make_duplicate(self):
        self.add(101, sessionId="same", bridgeSessionId="session_a")
        self.add(102, sessionId="same", bridgeSessionId="session_b", live=False)
        self.assertFalse(self.make_catalog().overview()["sessions"][0]["duplicate"])

    def test_same_bridge_two_processes_keeps_newer(self):
        self.add(101, bridgeSessionId="session_x", updatedAt=100)
        self.add(102, bridgeSessionId="session_x", updatedAt=200)
        self.add(103, bridgeSessionId="session_y", updatedAt=400)   # 新しい方が先に読まれる並びも
        self.add(104, bridgeSessionId="session_y", updatedAt=300)
        cat = self.make_catalog()
        self.assertEqual(self.pids(cat), [103, 102])
        self.assertEqual((cat.session("session_x")["pid"], cat.session("session_y")["pid"]), (102, 103))

    def test_same_bridge_and_same_session_id_still_flags_the_survivor(self):
        # 同じ会話を2つの生きたプロセスが持っている。一覧は新しい1行だが、二重起動の目印は付ける
        self.add(101, bridgeSessionId="session_x", sessionId="same", updatedAt=100)
        self.add(102, bridgeSessionId="session_x", sessionId="same", updatedAt=200)
        rows = self.make_catalog().overview()["sessions"]
        self.assertEqual([(s["pid"], s["duplicate"]) for s in rows], [(102, True)])


class ProjectTest(CatalogCase):
    def test_worktree_goes_to_parent_project_but_root_is_worktree(self):
        repo = make_repo(self.folder("30_devices", "Repo"))
        wt = self.home / "worktrees" / "feature-x"              # ホームの中（外は既定で見せない）
        git(repo, "worktree", "add", "-q", "-b", "feature-x", str(wt))
        sub = repo / "docs"
        sub.mkdir()
        self.add(101, cwd=str(wt), bridgeSessionId="session_wt")
        self.add(102, cwd=str(sub), bridgeSessionId="session_sub")
        cat = self.make_catalog(git=run_git)

        s = cat.session("session_wt")
        self.assertEqual(s["projectId"], folder_id(repo))
        self.assertEqual(s["rootId"], folder_id(wt))
        self.assertEqual(s["worktree"], "feature-x")
        self.assertEqual(s["cwd"], str(wt))
        self.assertIs(s["filesVisible"], True)
        s = cat.session("session_sub")                        # git のルートの中のフォルダ → ルートが起点
        self.assertEqual((s["projectId"], s["rootId"], s["worktree"]), (folder_id(repo),) * 2 + (None,))

        p = self.project(cat, "Repo")
        self.assertEqual((p["id"], p["rootId"], p["sessionCount"], p["isGit"]), (folder_id(repo),) * 2 + (2, True))
        self.assertEqual(len(cat.overview()["projects"]), 1)  # worktree は別のプロジェクトにならない
        self.assertEqual(os.path.realpath(cat.root_path(folder_id(wt))), os.path.realpath(wt))
        self.assertIn(folder_id(repo), cat.visible_roots())

    def test_common_dir_not_named_dot_git_uses_toplevel(self):
        # サブモジュールや bare の worktree は common-dir が <名前>.git や .git/modules/<名前>。その親をプロジェクトにしない
        top = self.folder("10_work", "Sub")
        self.add(101, cwd=str(top), bridgeSessionId="session_sm")
        cat = self.make_catalog(git=lambda cwd: (str(top), str(self.tmp / "super" / ".git" / "modules" / "Sub")))
        s = cat.session("session_sm")
        self.assertEqual((s["projectId"], s["rootId"], s["worktree"]), (folder_id(top),) * 2 + (None,))

    def test_non_git_cwd_is_its_own_project(self):
        work = self.tmp / "elsewhere" / "plain"
        work.mkdir(parents=True)
        self.add(101, cwd=str(work))
        cat = self.make_catalog()
        s = cat.overview()["sessions"][0]
        self.assertEqual((s["projectId"], s["rootId"], s["worktree"]), (folder_id(work),) * 2 + (None,))
        p = self.project(cat, "plain")
        self.assertEqual((p["path"], p["isGit"], p["sessionCount"]), (str(work), False, 1))

    def test_launcher_lists_every_lab_folder_even_without_sessions(self):
        a = self.folder("10_work", "A")
        b = self.folder("30_devices", "B")
        self.folder(".hidden", "C")
        self.folder("10_work", ".hid")
        (self.lab / "10_work" / "file.txt").write_text("x")   # フォルダでないものは出さない
        (self.lab / "loose.txt").write_text("x")
        cat = self.make_catalog()
        projects = cat.overview()["projects"]
        self.assertEqual([(p["name"], p["category"], p["sessionCount"]) for p in projects],
                         [("A", "10_work", 0), ("B", "30_devices", 0)])
        self.assertEqual(projects[0]["id"], folder_id(a))
        self.assertEqual(projects[1]["rootId"], folder_id(b))
        self.assertEqual(projects[1]["path"], str(b))
        self.assertEqual(cat.overview()["sessions"], [])

    def test_root_ids_depend_on_the_secret(self):         # パスが分かっても rootId を計算できない
        a = self.folder("10_work", "A")
        self.add(101, cwd=str(a))
        salted = Catalog(self.claude, [(self.lab, 2)], self.home, self.own, start_of=self.start_of, git=self.no_git,
                         alive=self.live.__contains__, secret=b"\x01" * 32)
        ids = lambda cat: (cat.overview()["sessions"][0]["rootId"], cat.overview()["projects"][0]["id"],
                           sorted(cat.visible_roots()))
        mine, other = ids(self.make_catalog()), ids(salted)
        self.assertEqual(mine, (folder_id(a), folder_id(a), [folder_id(a)]))
        self.assertNotEqual(mine[0], other[0])
        self.assertNotEqual(mine[2], other[2])
        self.assertIn(other[0], salted.visible_roots())
        self.assertNotIn(folder_id(a), salted.visible_roots())

    def test_without_launcher_roots_the_launcher_is_empty(self):
        # 起点を渡さなければ（既定。自動の旗も立てない）ランチャーは空。ディスクにフォルダがあっても数えない
        self.folder("10_work", "A")
        a = self.folder("10_work", "B")
        no_lab = Catalog(self.claude, [], self.home, self.own, start_of=self.start_of, git=self.no_git,
                         alive=self.live.__contains__, secret=SECRET)
        self.assertEqual(no_lab.overview()["projects"], [])
        self.add(101, cwd=str(a))                                # セッションのあるフォルダだけがプロジェクトになる
        no_lab = Catalog(self.claude, [], self.home, self.own, start_of=self.start_of, git=self.no_git,
                         alive=self.live.__contains__, secret=SECRET)
        projects = no_lab.overview()["projects"]
        self.assertEqual([(p["name"], p["category"], p["sessionCount"]) for p in projects], [("B", None, 1)])
        no_lab.refresh()                                         # 読み直しても落ちない・増えない
        self.assertEqual(len(no_lab.overview()["projects"]), 1)

    def test_launcher_marks_git_folders(self):
        make_repo(self.folder("10_work", "G"))
        self.folder("10_work", "N")
        cat = self.make_catalog()
        self.assertEqual((self.project(cat, "G")["isGit"], self.project(cat, "N")["isGit"]), (True, False))

    def test_session_in_lab_folder_joins_the_launcher_entry(self):
        a = self.folder("10_work", "A")
        self.add(101, cwd=str(a))
        self.add(102, cwd=str(a))
        self.add(103, cwd=str(a / "sub"), bridgeSessionId="session_sub")     # git でない中のフォルダは別のプロジェクト
        cat = self.make_catalog()
        self.assertEqual(self.project(cat, "A")["sessionCount"], 2)
        self.assertEqual(len([p for p in cat.overview()["projects"] if p["name"] == "A"]), 1)

    def test_project_outside_lab_has_null_category(self):
        work = self.tmp / "elsewhere" / "plain"
        self.add(101, cwd=str(work))
        self.assertIsNone(self.project(self.make_catalog(), "plain")["category"])

    def test_project_below_lab_folder_takes_its_category(self):
        a = self.folder("10_work", "A")
        self.add(101, cwd=str(a / "sub"))
        self.add(102, cwd=str(self.lab), bridgeSessionId="session_lab")      # ランチャーの直下は、カテゴリが無い
        cat = self.make_catalog()
        self.assertEqual(self.project(cat, "sub")["category"], "10_work")
        self.assertIsNone(self.project(cat, "Projects")["category"])

    def test_name_is_nfc_and_path_is_as_given(self):
        name = "デバイス"
        self.assertNotEqual(nfd(name), nfc(name))              # 試験の前提：形が本当に違う
        dev = self.folder("10_work", nfd(name))
        self.assertIn(nfd(name), os.listdir(self.lab / "10_work"))   # ディスク上が NFD で残っている
        work = self.tmp / "elsewhere" / nfd("ガ")
        work.mkdir(parents=True)
        self.add(101, cwd=str(work), name=nfd("ガ名"))
        cat = self.make_catalog()
        p = self.project(cat, name)                            # name は NFC で引ける
        self.assertEqual(p["path"], str(dev))                  # path は OS から受けたまま（NFD）
        self.assertIn(nfd(name), p["path"])
        self.assertEqual(self.project(cat, nfc("ガ"))["path"], str(work))
        s = cat.overview()["sessions"][0]
        self.assertEqual(s["name"], nfc("ガ名"))
        self.assertEqual(s["cwd"], str(work))                  # cwd もそのまま

    def test_same_folder_from_nfc_and_nfd_paths_is_one_project(self):
        self.folder("10_work", nfd("デバイス"))
        self.add(101, cwd=os.path.join(self.lab, "10_work", nfc("デバイス")))   # 別の綴りで来た同じフォルダ
        cat = self.make_catalog()
        self.assertEqual(len(cat.overview()["projects"]), 1)
        self.assertEqual(cat.overview()["projects"][0]["sessionCount"], 1)

    def test_projects_sorted_by_nfc_name_and_sessions_newest_first(self):
        self.folder("10_work", nfd("ガ"))                       # NFD の文字列順なら「カア」より前になる名前
        self.folder("10_work", "カア")
        self.add(101, updatedAt=100)
        self.add(102, updatedAt=300)
        self.add(103, updatedAt=200)
        cat = self.make_catalog()
        launcher = [p["name"] for p in cat.overview()["projects"] if p["category"]]
        self.assertEqual(launcher, ["カア", nfc("ガ")])
        self.assertEqual(self.pids(cat), [102, 103, 101])


class FilesVisibleTest(CatalogCase):
    def test_files_visible_false_for_home_and_dot_claude(self):
        self.add(101, cwd=str(self.home), bridgeSessionId="session_home")
        self.add(102, cwd=str(self.claude), bridgeSessionId="session_claude")
        a = self.folder("10_work", "A")
        self.add(103, cwd=str(a), bridgeSessionId="session_a")
        cat = self.make_catalog()
        self.assertEqual(sorted(self.pids(cat)), [101, 102, 103])
        for bridge in ("session_home", "session_claude"):
            s = cat.session(bridge)
            self.assertIsNotNone(s)                            # 一覧には出る
            self.assertIsNone(cat.root_path(s["rootId"]))
            self.assertNotIn(s["rootId"], cat.visible_roots())
            self.assertFalse(self.project(cat, Path(s["cwd"]).name)["filesVisible"])
        self.assertTrue(self.project(cat, "A")["filesVisible"])
        a_id = cat.session("session_a")["rootId"]
        self.assertEqual(cat.root_path(a_id), a)
        self.assertEqual(cat.visible_roots()[a_id], a)

    def test_own_dirs_are_not_visible(self):
        own = self.folder("10_work", "Own")                    # ~/Library の外に置き、own_dirs だけが理由になるようにする
        self.folder("10_work", "Other")
        self.own = [own]
        self.add(101, cwd=str(own))
        cat = self.make_catalog()
        self.assertFalse(self.project(cat, "Own")["filesVisible"])
        self.assertTrue(self.project(cat, "Other")["filesVisible"])
        self.assertIsNone(cat.root_path(cat.overview()["sessions"][0]["rootId"]))

    def test_worktree_root_visibility_is_judged_on_the_worktree(self):
        # 親のプロジェクトは見せてよいが、worktree が ~/.claude の中にあれば、そのセッションの起点は見せない
        repo = self.folder("10_work", "Repo")
        wt = self.claude / "worktrees" / "w1"
        wt.mkdir(parents=True)
        gitfile(wt, repo / ".git")
        cat = self.make_catalog(git=lambda cwd: (str(wt), str(repo / ".git")))
        self.add(101, cwd=str(wt), bridgeSessionId="session_w")
        cat.refresh()
        s = cat.session("session_w")
        self.assertEqual((s["projectId"], s["rootId"]), (folder_id(repo), folder_id(wt)))
        self.assertTrue(self.project(cat, "Repo")["filesVisible"])
        self.assertIsNone(cat.root_path(s["rootId"]))
        self.assertIsNotNone(cat.root_path(s["projectId"]))
        self.assertIs(s["filesVisible"], False)                # セッションの行の filesVisible は rootId のもの

    def test_session_row_files_visible_follows_its_root(self):
        self.add(101, cwd=str(self.home), bridgeSessionId="session_home")
        a = self.folder("10_work", "A")
        self.add(102, cwd=str(a), bridgeSessionId="session_a")
        cat = self.make_catalog()
        self.assertIs(cat.session("session_home")["filesVisible"], False)
        self.assertIs(cat.session("session_a")["filesVisible"], True)
        self.assertEqual({s["bridgeSessionId"]: s["filesVisible"] for s in cat.overview()["sessions"]},
                         {"session_home": False, "session_a": True})

    def test_worktree_in_a_dot_folder_of_a_visible_project_shows_files(self):
        # .worktrees/<名前> と .claude/worktrees/<名前>（プロジェクトの中）。本物の git で作る
        repo = make_repo(self.folder("10_work", "Repo"))
        wts = {}
        for i, container in enumerate((".worktrees", ".claude/worktrees"), 1):
            wt = repo / container / f"w{i}"
            git(repo, "worktree", "add", "-q", "-b", f"w{i}", str(wt))
            self.add(100 + i, cwd=str(wt), bridgeSessionId=f"session_w{i}")
            wts[f"session_w{i}"] = wt
        cat = self.make_catalog(git=run_git)
        for bridge, wt in wts.items():
            s = cat.session(bridge)
            self.assertEqual((s["projectId"], s["rootId"], s["worktree"]), (folder_id(repo), folder_id(wt), wt.name))
            self.assertIs(s["filesVisible"], True, bridge)
            self.assertEqual(os.path.realpath(cat.root_path(s["rootId"])), os.path.realpath(wt), bridge)
        self.assertEqual(len(cat.overview()["projects"]), 1)  # worktree は別のプロジェクトにならない

    def test_worktree_outside_its_parent_or_of_a_hidden_parent_stays_hidden(self):   # 見せてよい親の外
        repo = self.folder("10_work", "Repo")
        dot_config = self.home / ".config" / "w"                # 親の外の . フォルダ
        home_wt = self.home / ".wt" / "w"                       # 親がホームそのもの（dotfiles のリポジトリ）
        library = self.home / "Library" / "Application Support" / "R"
        library_wt = library / ".worktrees" / "w"               # 親が ~/Library の中
        for p, common in ((dot_config, repo / ".git"), (home_wt, self.home / ".git"), (library_wt, library / ".git")):
            p.mkdir(parents=True)
            gitfile(p, common)
        answers = {str(dot_config): (str(dot_config), str(repo / ".git")),
                   str(home_wt): (str(home_wt), str(self.home / ".git")),
                   str(library_wt): (str(library_wt), str(library / ".git"))}
        cat = self.make_catalog(git=answers.get)
        for i, wt in enumerate((dot_config, home_wt, library_wt), 1):
            self.add(100 + i, cwd=str(wt), bridgeSessionId=f"session_{i}")
        cat.refresh()
        for i, wt in enumerate((dot_config, home_wt, library_wt), 1):
            s = cat.session(f"session_{i}")
            self.assertEqual((s["rootId"], s["worktree"]), (folder_id(wt), "w"), wt)
            self.assertIs(s["filesVisible"], False, wt)
            self.assertIsNone(cat.root_path(s["rootId"]), wt)

    def test_core_worktree_into_a_dot_folder_is_not_a_worktree(self):
        # core.worktree で作業コピーを .git やほかの . フォルダに向けると、toplevel と common-dir の親が食い違い、
        # worktree に見えていた。worktree として扱うと .git/config まで見えうる。本物の worktree（.git がファイル）だけを worktree とする
        for i, target in enumerate((".git", ".secret"), 1):
            repo = make_repo(self.folder("10_work", f"Repo{i}"))
            inside = repo / target
            inside.mkdir(exist_ok=True)
            git(repo, "config", "core.worktree", str(inside))
            self.add(100 + i, cwd=str(inside), bridgeSessionId=f"session_{i}")
        cat = self.make_catalog(git=run_git)
        for i, target in enumerate((".git", ".secret"), 1):
            s = cat.session(f"session_{i}")
            self.assertIsNone(s["worktree"], target)
            self.assertIs(s["filesVisible"], False, target)
            self.assertIsNone(cat.root_path(s["rootId"]), target)

    def test_core_worktree_does_not_widen_root(self):
        # evil/.git/config の core.worktree = ../.. で、git の toplevel が親の src になる。隣の victim まで見せない。
        # toplevel に .git が無ければ、起点は作業フォルダに縮める（まれな正当な使い方も同じ）
        src = self.home / "src"
        evil = make_repo(src / "evil")
        victim = src / "victim"
        victim.mkdir()
        (victim / "AuthKey_X.p8").write_text("secret")
        git(evil, "config", "core.worktree", "../..")
        sub = evil / "sub"
        sub.mkdir()
        self.assertEqual(run_git(str(evil))[0], os.path.realpath(src))   # 試験の前提：細工が本当に効いている
        self.add(101, cwd=str(evil), bridgeSessionId="session_evil")
        self.add(102, cwd=str(sub), bridgeSessionId="session_sub")
        cat = self.make_catalog(git=run_git)
        for bridge, cwd in (("session_evil", evil), ("session_sub", sub)):
            s = cat.session(bridge)
            self.assertEqual((s["projectId"], s["rootId"], s["worktree"]), (folder_id(cwd),) * 2 + (None,), bridge)
            self.assertIs(s["filesVisible"], True, bridge)
            self.assertEqual(os.path.realpath(cat.root_path(s["rootId"])), os.path.realpath(cwd), bridge)
        self.assertNotIn(folder_id(src), cat.visible_roots())
        self.assertNotIn(folder_id(victim), cat.visible_roots())
        self.assertEqual(self.project(cat, "evil")["path"], str(evil))

    def test_claude_dir_symlink_hides_its_real_folder(self):
        # ~/.claude が ~/dotfiles/claude へのリンクなら、実体のフォルダとその上（dotfiles）の起点は見せない
        dotfiles = self.home / "dotfiles"
        (dotfiles / "claude").mkdir(parents=True)
        (dotfiles / "zsh").mkdir()
        (self.home / "Projects/p").mkdir()
        for name, path in (("claude", dotfiles / "claude"), ("dotfiles", dotfiles), ("zsh", dotfiles / "zsh"),
                           ("p", self.home / "Projects/p")):
            self.add(101 + len(name), cwd=str(path), bridgeSessionId=f"session_{name}")
        moved = self.tmp / "sessions-moved"
        self.sessions.rename(moved)                          # 実体を dotfiles に移し、~/.claude を張り直す
        self.claude.rmdir()
        os.symlink(dotfiles / "claude", self.claude)
        moved.rename(dotfiles / "claude" / "sessions")
        cat = self.make_catalog()
        self.assertEqual({s["bridgeSessionId"]: s["filesVisible"] for s in cat.overview()["sessions"]},
                         {"session_claude": False, "session_dotfiles": False, "session_zsh": True, "session_p": True})

    def test_claude_dir_moved_and_linked_back_drops_the_cached_answers(self):
        # 一番ありがちな張り替え：mv ~/.claude ~/dotfiles/claude && ln -s ~/dotfiles/claude ~/.claude。
        # mv では inode が変わらない（stat の印は同じ）ので、リンクそのものの印（lstat）も見て、覚えた答えを捨てる
        dotfiles = self.home / "dotfiles"
        dotfiles.mkdir()
        launcher_link = self.folder("10_work", "L")
        launcher_link.rmdir()
        os.symlink(dotfiles, launcher_link)                    # ランチャーの中のリンク
        self.add(101, cwd=str(dotfiles), bridgeSessionId="session_d")
        # listserver と同じく、起動のときの ~/.claude の実体を own_dirs に入れておく
        cat = Catalog(self.claude, [(self.lab, 2)], self.home, self.own + [Path(os.path.realpath(self.claude))],
                      start_of=self.start_of, git=self.no_git, alive=self.live.__contains__, secret=SECRET)
        self.assertIs(cat.session("session_d")["filesVisible"], True)
        self.assertIs(self.project(cat, "L")["filesVisible"], True)
        os.rename(self.claude, dotfiles / "claude")
        os.symlink(dotfiles / "claude", self.claude)
        cat.refresh()
        cat.refresh()
        self.assertIs(cat.session("session_d")["filesVisible"], False)
        self.assertIs(self.project(cat, "L")["filesVisible"], False)
        self.assertNotIn(folder_id(dotfiles), cat.visible_roots())

    def test_claude_config_dir_is_hidden_by_the_catalog_itself(self):
        # CLAUDE_CONFIG_DIR=~/conf/claude（. の無い場所）。Catalog は渡された claude_dir を、own_dirs に無くても見せない
        conf = self.home / "conf" / "claude"
        (conf / "sessions").mkdir(parents=True)
        (self.home / "conf" / "other").mkdir()
        self.sessions = conf / "sessions"
        for name, path in (("claude", conf), ("projects", conf / "projects"), ("conf", conf.parent),
                           ("other", self.home / "conf" / "other")):
            self.add(101 + len(name), cwd=str(path), bridgeSessionId=f"session_{name}")
        cat = Catalog(conf, [(self.lab, 2)], self.home, self.own, start_of=self.start_of, git=self.no_git,
                      alive=self.live.__contains__, secret=SECRET)
        self.assertEqual({s["bridgeSessionId"]: s["filesVisible"] for s in cat.overview()["sessions"]},
                         {"session_claude": False, "session_projects": False, "session_conf": False,
                          "session_other": True})

    def test_outside_home_roots_are_hidden_but_listed(self):
        outside = self.tmp / "ext" / "proj"
        outside.mkdir(parents=True)
        self.add(101, cwd=str(outside), bridgeSessionId="session_out")
        cat = self.make_catalog()
        s = cat.session("session_out")
        self.assertIsNotNone(s)                                # 一覧には出る
        self.assertIs(s["filesVisible"], False)
        self.assertIsNone(cat.root_path(s["rootId"]))
        self.assertIs(self.project(cat, "proj")["filesVisible"], False)

    def test_forbidden_folder_changing_identity_drops_the_cached_answers(self):
        # 起点ごとの「見せてよいか」は覚えている。~/.claude の張り直しで見せない場所が変わったら、覚えを捨てて引き直す
        dotfiles = self.home / "dotfiles"
        (dotfiles / "claude").mkdir(parents=True)
        launcher_link = self.folder("10_work", "L")
        launcher_link.rmdir()
        os.symlink(dotfiles / "claude", launcher_link)         # ランチャーの中のリンク
        self.add(101, cwd=str(dotfiles / "claude"), bridgeSessionId="session_c")
        cat = self.make_catalog()
        self.assertIs(cat.session("session_c")["filesVisible"], True)     # まだ ~/.claude とは別のフォルダ
        self.assertIs(self.project(cat, "L")["filesVisible"], True)
        moved = self.tmp / "sessions-moved"
        self.sessions.rename(moved)
        self.claude.rmdir()
        os.symlink(dotfiles / "claude", self.claude)           # ~/.claude を dotfiles/claude へのリンクにする
        moved.rename(dotfiles / "claude" / "sessions")
        cat.refresh()
        self.assertIs(cat.session("session_c")["filesVisible"], False)
        self.assertIs(self.project(cat, "L")["filesVisible"], False)
        self.assertNotIn(folder_id(dotfiles / "claude"), cat.visible_roots())

    def test_linked_worktree_inside_dot_git_stays_hidden(self):
        repo = make_repo(self.folder("10_work", "Repo"))
        wt = repo / ".git" / "wt"
        git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
        self.add(101, cwd=str(wt), bridgeSessionId="session_wt")
        cat = self.make_catalog(git=run_git)
        s = cat.session("session_wt")
        self.assertEqual(s["worktree"], "wt")
        self.assertIs(s["filesVisible"], False)
        self.assertIsNone(cat.root_path(s["rootId"]))


class GitIdentityTest(CatalogCase):
    """git の答え（toplevel）を、.git の実体で確かめる。core.worktree や gitdir: の細工で起点・プロジェクトが広がらない。

    git の toplevel の .git が、git が実際に使っているもの（--git-common-dir）を指すと確かめられたときだけ信じる。
    本物の worktree は、.git のファイルの行き先が <common>/worktrees/<名前> で、その gitdir ファイルが .git へ戻る。
    確かめられなければ、起点は作業フォルダに縮める。
    """

    def setUp(self):
        super().setUp()
        self.src = self.home / "src"
        self.src.mkdir()

    def catalog_for(self, *cwds):
        for i, cwd in enumerate(cwds, 1):
            self.add(100 + i, cwd=str(cwd), bridgeSessionId=f"session_{i}")
        return self.make_catalog(git=run_git)

    def assertPlace(self, cat, n, project, root, worktree=None, visible=True):
        s = cat.session(f"session_{n}")
        self.assertEqual((s["projectId"], s["rootId"], s["worktree"]),
                         (folder_id(project), folder_id(root), worktree), n)
        self.assertIs(s["filesVisible"], visible, n)

    def test_core_worktree_into_a_real_repo_shrinks_to_cwd(self):   # (A)
        victim = make_repo(self.src / "victim")
        evil = make_repo(self.src / "evil")
        git(evil, "config", "core.worktree", "../../victim")
        self.assertEqual(run_git(str(evil))[0], os.path.realpath(victim))     # 前提：細工が効いている
        cat = self.catalog_for(evil)
        self.assertPlace(cat, 1, evil, evil)
        self.assertNotIn(folder_id(victim), cat.visible_roots())
        self.assertEqual([p["name"] for p in cat.overview()["projects"] if p["sessionCount"]], ["evil"])

    def test_core_worktree_into_an_ancestor_repo_shrinks_to_cwd(self):   # (C)
        mono = make_repo(self.home / "mono")
        evil = make_repo(mono / "pkg" / "evil2")
        git(evil, "config", "core.worktree", "../../..")
        self.assertEqual(run_git(str(evil))[0], os.path.realpath(mono))
        cat = self.catalog_for(evil)
        self.assertPlace(cat, 1, evil, evil)
        self.assertNotIn(folder_id(mono), cat.visible_roots())

    def test_gitfile_to_a_gitdir_whose_core_worktree_points_away_shrinks(self):   # (evil4)
        victim = make_repo(self.src / "victim")
        evil = self.src / "evil4"
        evil.mkdir()
        git(evil, "init", "-q", "--separate-git-dir", str(self.src / "gitstash"))
        git(evil, "config", "core.worktree", "../victim")                        # gitstash の config。gitstash/../victim
        self.assertEqual(run_git(str(evil))[0], os.path.realpath(victim))
        cat = self.catalog_for(evil)
        self.assertPlace(cat, 1, evil, evil)
        self.assertNotIn(folder_id(victim), cat.visible_roots())

    def test_gitfile_pointing_at_a_neighbours_git_dir_is_not_a_worktree(self):   # (B)
        victim = make_repo(self.src / "victim")
        evil = self.src / "evil3"
        evil.mkdir()
        (evil / ".git").write_text("gitdir: ../victim/.git\n", encoding="utf-8")
        self.assertEqual(run_git(str(evil)), (os.path.realpath(evil), os.path.join(os.path.realpath(victim), ".git")))
        cat = self.catalog_for(evil)
        self.assertPlace(cat, 1, evil, evil)                                      # victim のプロジェクトにも worktree にもならない
        self.assertNotIn(folder_id(victim), cat.visible_roots())
        self.assertEqual([p["name"] for p in cat.overview()["projects"] if p["sessionCount"]], ["evil3"])

    def test_gitfile_borrowing_a_real_worktrees_gitdir_shrinks(self):
        repo = make_repo(self.src / "repo")
        wt = self.src / "repo-wt"
        git(repo, "worktree", "add", "-q", "-b", "wt1", str(wt))
        evil = self.src / "evil6"
        evil.mkdir()
        (evil / ".git").write_text(f"gitdir: {repo}/.git/worktrees/repo-wt\n", encoding="utf-8")   # gitdir は wt へ戻る
        cat = self.catalog_for(evil)
        self.assertPlace(cat, 1, evil, evil)

    def test_real_repo_subfolder_submodule_and_separate_git_dir_resolve_as_before(self):
        repo = make_repo(self.src / "repo")
        (repo / "docs").mkdir()
        origin = make_repo(self.src / "subsrc")
        subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(origin), "mods/s"], check=True, capture_output=True)
        sep = self.src / "sep"
        sep.mkdir()
        git(sep, "init", "-q", "--separate-git-dir", str(self.home / "sepgit"))
        (sep / "sub").mkdir()
        mod = repo / "mods" / "s"
        self.assertTrue((mod / ".git").is_file() and (sep / ".git").is_file())    # 前提：どちらも .git はファイル
        cat = self.catalog_for(repo, repo / "docs", mod, sep, sep / "sub")
        self.assertPlace(cat, 1, repo, repo)
        self.assertPlace(cat, 2, repo, repo)
        self.assertPlace(cat, 3, mod, mod)                                        # サブモジュールは worktree ではない
        self.assertPlace(cat, 4, sep, sep)                                        # --separate-git-dir も
        self.assertPlace(cat, 5, sep, sep)

    def test_real_linked_worktrees_resolve_as_before(self):
        repo = make_repo(self.src / "repo")
        inside = repo / ".worktrees" / "w1"
        outside = self.home / "wt-outside"
        git(repo, "worktree", "add", "-q", "-b", "w1", str(inside))
        git(repo, "worktree", "add", "-q", "-b", "w2", str(outside))
        cat = self.catalog_for(inside, outside)
        self.assertPlace(cat, 1, repo, inside, "w1")
        self.assertPlace(cat, 2, repo, outside, "wt-outside")

    def test_linked_worktree_written_with_relative_paths_resolves(self):
        repo = make_repo(self.src / "repo")
        wt = self.src / "repo-rel"
        try:
            subprocess.run(["git", "-C", str(repo), "-c", "worktree.useRelativePaths=true", "worktree", "add", "-q",
                            "-b", "rel", str(wt)], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            self.skipTest("この git は相対パスの worktree を書けない")
        if (wt / ".git").read_text().split(":", 1)[1].strip().startswith("/"):
            self.skipTest("この git は設定を無視して絶対パスで書いた")
        cat = self.catalog_for(wt)
        self.assertPlace(cat, 1, repo, wt, "repo-rel")

    def test_symlinked_or_hardlinked_git_file_of_a_real_worktree_is_not_a_worktree(self):   # (S1・S2・S3)
        # 本物の worktree の .git ファイルへのリンクを自分の .git にすれば、中身（gitdir: 行き先）は本物と同じ。
        # 行き先の gitdir ファイルが戻る先は本物の worktree の .git なので、「戻ってくるか」は toplevel の実体ではなく
        # 戻る先のパス（名前が .git で、その親が toplevel）で見る。親のリポジトリ R を、セッションが無いまま見せない
        repo = make_repo(self.src / "victimrepo")
        (repo / "secret.txt").write_text("s")
        wt = self.src / "victim-wt"
        git(repo, "worktree", "add", "-q", "-b", "w", str(wt))
        sym = self.src / "evil_symlink"
        sym.mkdir()
        os.symlink(wt / ".git", sym / ".git")
        hard = self.src / "evil_hardlink"
        hard.mkdir()
        try:
            os.link(wt / ".git", hard / ".git")
        except OSError:
            self.skipTest("このファイルシステムはハードリンクを作れない")
        sub = sym / "sub"
        sub.mkdir()
        for p in (sym, hard):                                                       # 前提：細工が効いている（git は R の worktree と答える）
            top, common = run_git(str(p))
            self.assertEqual((top, common), (os.path.realpath(p), os.path.join(os.path.realpath(repo), ".git")), p)
        cat = self.catalog_for(sym, hard, sub)
        self.assertPlace(cat, 1, sym, sym)
        self.assertPlace(cat, 2, hard, hard)
        self.assertPlace(cat, 3, sub, sub)
        self.assertNotIn(folder_id(repo), cat.visible_roots())
        self.assertEqual(sorted(p["name"] for p in cat.overview()["projects"]), ["evil_hardlink", "evil_symlink", "sub"])

    def test_symlinked_git_dir_is_still_a_repo(self):
        # .git のフォルダを別の場所へ置き、リンクを張った作り（正当）。worktree には広げないが、リポジトリとしては解ける
        repo = make_repo(self.src / "linked")
        store = self.home / "gits" / "linked.git"
        store.parent.mkdir()
        os.rename(repo / ".git", store)
        os.symlink(store, repo / ".git")
        (repo / "docs").mkdir()
        cat = self.catalog_for(repo, repo / "docs")
        self.assertPlace(cat, 1, repo, repo)
        self.assertPlace(cat, 2, repo, repo)

    def _fifo_catalog(self, symlinked: bool):
        """worktrees/<名前>/gitdir が FIFO の偽の worktree。開くと書き手が来るまで止まるので、別の糸で作って時間を区切る。"""
        evil = self.src / "evil"
        evil.mkdir()
        store = self.src / "store.git"
        subprocess.run(["git", "init", "-q", "--bare", str(store)], check=True, capture_output=True)
        admin = store / "worktrees" / "x"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text("ref: refs/heads/main\n")
        (admin / "commondir").write_text("../..\n")
        fifo = self.tmp / "fifo" if symlinked else admin / "gitdir"
        os.mkfifo(fifo)
        if symlinked:
            os.symlink(fifo, admin / "gitdir")                 # FIFO へのリンクでも同じ
        (evil / ".git").write_text(f"gitdir: {admin}\n")
        answer = run_git(str(evil))
        self.assertEqual(answer, (os.path.realpath(evil), os.path.realpath(store)))   # 前提：git は答える（FIFO の gitdir を読まない）
        self.add(101, cwd=str(evil), bridgeSessionId="session_1")
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("cat", self.make_catalog(git=run_git)), daemon=True)
        th.start()
        th.join(10)
        if th.is_alive():                                      # 止まった：書き手を送って糸を解き、試験を落とす（試験全体を止めない）
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            th.join(5)
            self.fail("FIFO を開いて止まった")
        return evil, out["cat"]

    def test_a_fifo_as_the_worktree_gitdir_file_does_not_hang_the_catalog(self):
        for symlinked in (False, True):
            with self.subTest(symlinked=symlinked):
                self.setUp()                                    # 毎回まっさらな tmp で
                evil, cat = self._fifo_catalog(symlinked)
                self.assertPlace(cat, 1, evil, evil)            # 戻ってこない（読めない）ので worktree ではない

    def test_fake_git_answers_are_checked_against_the_git_entry(self):
        # 偽の git の答え（toplevel, common-dir）と .git の作りの組ごとに、信じるか（top）・worktree か・縮めるか（shrink）
        repo = self.folder("10_work", "Repo")
        (repo / ".git").mkdir()
        other = self.folder("10_work", "Other")
        (other / ".git").mkdir()
        store = self.home / "store" / "proj.git"                   # --separate-git-dir・サブモジュールの行き先
        store.mkdir(parents=True)
        bare = self.home / "bare.git"
        bare.mkdir()

        def text(content):
            return lambda t: (t / ".git").write_text(content, encoding="utf-8")

        def wt_bad_back(t):                                        # worktrees/<名前> はあるが、その gitdir が別の .git へ戻る
            gitfile(t, repo / ".git")
            (repo / ".git" / "worktrees" / t.name / "gitdir").write_text(f"{other}/.git\n", encoding="utf-8")

        cases = {   # 名前: (git の common-dir, toplevel に .git を作る, 期待)
            "dir_same": (lambda t: t / ".git", lambda t: (t / ".git").mkdir(), "top"),
            "dir_other": (lambda t: other / ".git", lambda t: (t / ".git").mkdir(), "shrink"),   # git が使っているものと違う
            "file_same": (lambda t: store, text(f"gitdir: {store}\n"), "top"),                   # --separate-git-dir・サブモジュール
            "file_other": (lambda t: repo / ".git", text(f"gitdir: {other}/.git\n"), "shrink"),  # 隣の .git を指す
            "file_missing": (lambda t: store, text("gitdir: /nonexistent/x\n"), "shrink"),
            "file_garbage": (lambda t: store, text("hello\n"), "shrink"),
            "none": (lambda t: store, lambda t: None, "shrink"),
            "fifo_entry": (lambda t: store, lambda t: os.mkfifo(t / ".git"), "shrink"),           # 開くと止まるものは開かない
            "wt_ok": (lambda t: repo / ".git", lambda t: gitfile(t, repo / ".git"), "worktree"),
            "wt_bad_back": (lambda t: repo / ".git", wt_bad_back, "shrink"),
            "wt_bare": (lambda t: bare, lambda t: gitfile(t, bare), "top"),                       # bare の worktree は親を使わない
        }
        answers, tops = {}, {}
        for i, (name, (common, build, _)) in enumerate(cases.items(), 1):
            top = tops[name] = self.home / "tops" / name
            (top / "a").mkdir(parents=True)
            build(top)
            answers[str(top / "a")] = (str(top), str(common(top)))
            self.add(100 + i, cwd=str(top / "a"), bridgeSessionId=f"session_{name}")
        cat = self.make_catalog(git=answers.get)
        for name, (_, _, expect) in cases.items():
            s = cat.session(f"session_{name}")
            top, cwd = tops[name], tops[name] / "a"
            want = {"top": (folder_id(top), folder_id(top), None),
                    "shrink": (folder_id(cwd), folder_id(cwd), None),
                    "worktree": (folder_id(repo), folder_id(top), name)}[expect]
            self.assertEqual((s["projectId"], s["rootId"], s["worktree"]), want, name)


class PassThroughTest(CatalogCase):
    def test_status_and_waitingfor_passed_through(self):
        self.add(101, status="shell", bridgeSessionId="session_1")
        self.add(102, status="weird", bridgeSessionId="session_2")
        self.add(103, status="waiting", waitingFor="dialog open", bridgeSessionId="session_3")
        cat = self.make_catalog()
        self.assertEqual((cat.session("session_1")["status"], cat.session("session_1")["waitingFor"]), ("shell", None))
        self.assertEqual(cat.session("session_2")["status"], "weird")
        s = cat.session("session_3")
        self.assertEqual((s["status"], s["waitingFor"]), ("waiting", "dialog open"))
        self.assertEqual(s["statusUpdatedAt"], 1792941510789)

    def test_response_shape_matches_the_spec(self):
        a = self.folder("10_work", "A")
        self.add(101, cwd=str(a), bridgeSessionId="session_01abc")
        ov = self.make_catalog().overview()
        self.assertEqual(set(ov), {"generatedAt", "projects", "sessions"})
        self.assertIsInstance(ov["generatedAt"], int)
        self.assertEqual(set(ov["projects"][0]), {"id", "rootId", "name", "category", "path", "isGit",
                                                  "filesVisible", "sessionCount"})
        s = ov["sessions"][0]
        self.assertEqual(set(s), {"bridgeSessionId", "url", "pid", "sessionId", "projectId", "rootId", "name",
                                  "cwd", "worktree", "status", "waitingFor", "statusUpdatedAt", "updatedAt",
                                  "entrypoint", "duplicate", "filesVisible"})
        self.assertEqual(s["url"], "https://claude.ai/code/session_01abc")
        self.assertEqual((s["sessionId"], s["entrypoint"], s["updatedAt"]), ("sid-101", "claude-vscode", 1792941510789))

    def test_unknown_bridge_gives_none(self):
        self.add(101)
        cat = self.make_catalog()
        self.assertIsNone(cat.session("session_nope"))
        self.assertIsNone(cat.root_path("0123456789abcdef"))


class GitCacheTest(CatalogCase):
    def test_git_called_once_per_cwd(self):
        self.add(101, cwd=str(self.tmp / "x"), bridgeSessionId="session_1")
        self.add(102, cwd=str(self.tmp / "x"), bridgeSessionId="session_2")
        self.add(103, cwd=str(self.tmp / "y"), bridgeSessionId="session_3")
        cat = self.make_catalog()
        cat.refresh()
        cat.refresh()
        self.assertEqual(sorted(self.git_calls), [str(self.tmp / "x"), str(self.tmp / "y")])   # git でない答え（None）も覚える

    def test_git_answer_is_forgotten_when_no_session_uses_the_cwd(self):
        # 使われなくなった作業フォルダは忘れる（増え続けない。あとで git init されても、新しいセッションで気づく）
        self.add(101, cwd=str(self.tmp / "x"))
        cat = self.make_catalog()
        (self.sessions / "101.json").unlink()
        cat.refresh()
        self.add(102, cwd=str(self.tmp / "x"))
        cat.refresh()
        self.assertEqual(self.git_calls, [str(self.tmp / "x")] * 2)

    def test_git_answer_is_reused_for_a_repo_too(self):
        calls = []
        top = self.folder("10_work", "R")

        def fake(cwd):
            calls.append(cwd)
            return (str(top), str(top / ".git"))

        self.add(101, cwd=str(top))
        cat = self.make_catalog(git=fake)
        cat.refresh()
        cat.refresh()
        self.assertEqual(calls, [str(top)])


class RefreshTest(CatalogCase):
    def test_refresh_reports_change_only_when_content_changes(self):
        cat = self.make_catalog()
        with mock.patch("bblist.sessions.time.time", return_value=1000.0):
            self.assertFalse(cat.refresh())                    # 何も変わらない
            first = cat.overview()["generatedAt"]
        with mock.patch("bblist.sessions.time.time", return_value=2000.0):
            self.assertFalse(cat.refresh())                    # generatedAt だけが変わっても False
            second = cat.overview()["generatedAt"]
        self.assertEqual((first, second), (1000000, 2000000))

        self.add(101)
        self.assertTrue(cat.refresh())                         # セッションが増えた
        self.assertFalse(cat.refresh())
        self.add(101, status="busy", statusUpdatedAt=1792941519999)
        self.assertTrue(cat.refresh())                         # 状態が変わった
        self.assertFalse(cat.refresh())
        self.folder("10_work", "New")
        self.assertTrue(cat.refresh())                         # ランチャーのフォルダが増えた
        (self.sessions / "101.json").unlink()
        self.assertTrue(cat.refresh())                         # プロセスが終わった
        self.assertEqual(self.pids(cat), [])

    def test_dead_process_drops_out_on_next_refresh(self):
        self.add(101)
        cat = self.make_catalog()
        self.assertEqual(self.pids(cat), [101])
        self.live.discard(101)
        self.assertTrue(cat.refresh())
        self.assertEqual(self.pids(cat), [])


class SteadyStateTest(CatalogCase):
    """変わっていないものは、ps も起動せず、json も開き直さない（変わったものだけ読み直す）。"""

    def jsons(self, opened):
        return sorted(os.path.basename(p) for p in opened if p.endswith(".json"))

    def test_unchanged_files_cost_no_ps_and_no_open(self):
        self.add(101)
        self.add(102)
        cat = self.make_catalog()
        self.assertEqual(sorted(self.ps_calls), [101, 102])        # 最初の1回は確かめる
        before = cat.overview()
        self.ps_calls.clear()
        with self.spy_opens() as opened:
            self.assertFalse(cat.refresh())
            self.assertFalse(cat.refresh())
        self.assertEqual(self.ps_calls, [])
        self.assertEqual(self.jsons(opened), [])
        after = cat.overview()
        before.pop("generatedAt"), after.pop("generatedAt")
        self.assertEqual(after, before)

    def test_changed_file_alone_is_reread(self):
        self.add(101)
        self.add(102)
        cat = self.make_catalog()
        self.add(102, name="名前が長くなった" * 3, status="busy")  # 大きさが変わる
        with self.spy_opens() as opened:
            self.assertTrue(cat.refresh())
        self.assertEqual(self.jsons(opened), ["102.json"])
        rows = {s["pid"]: s for s in cat.overview()["sessions"]}
        self.assertEqual((rows[102]["status"], rows[101]["status"]), ("busy", "idle"))
        self.assertEqual(self.ps_calls.count(102), 1)               # procStart は同じ。ps は聞き直さない

    def test_same_size_rewrite_is_reread_when_mtime_changes(self):
        self.add(101, status="idle")
        cat = self.make_catalog()
        path = self.sessions / "101.json"
        st = path.stat()
        self.add(101, status="busy")                                # "idle" と "busy" は同じ長さ
        self.assertEqual(path.stat().st_size, st.st_size)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))   # 更新時刻だけが違う
        self.assertTrue(cat.refresh())
        self.assertEqual(cat.session("session_101")["status"], "busy")

    def test_changed_procstart_is_checked_again_for_that_pid_only(self):
        self.add(101)
        self.add(102)
        cat = self.make_catalog()
        self.ps_calls.clear()
        self.add(102, procStart="Mon Oct 26 10:00:00 2026")          # 記録の procStart が変わり、ps も同じ答え
        cat.refresh()
        self.assertEqual(self.ps_calls, [102])
        self.assertEqual(sorted(self.pids(cat)), [101, 102])
        self.ps_calls.clear()
        self.add(102, procStart="Tue Oct 27 10:00:00 2026", ps="Mon Oct 26 10:00:00 2026")   # 記録だけが変わり、ps と食い違う
        self.assertTrue(cat.refresh())
        self.assertEqual(self.ps_calls, [102])
        self.assertEqual(self.pids(cat), [101])

    def test_dead_pid_forgets_verdict_so_a_reused_pid_is_checked_again(self):
        self.add(101)
        cat = self.make_catalog()
        self.live.discard(101)
        self.assertTrue(cat.refresh())                               # 終わった
        self.assertEqual(self.pids(cat), [])
        self.live.add(101)                                           # 同じ PID を別のプロセスが使い始めた
        self.ps[101] = "Mon Oct 26 10:00:00 2026"
        self.ps_calls.clear()
        cat.refresh()
        self.assertEqual(self.ps_calls, [101])                       # 覚えていた答えは使わず、ps で確かめ直す
        self.assertEqual(self.pids(cat), [])

    def test_mismatch_is_remembered_too(self):
        self.add(101, ps="Mon Oct 26 10:00:00 2026")                 # PID の使い回し。ps と食い違い続ける
        cat = self.make_catalog()
        self.ps_calls.clear()
        cat.refresh()
        cat.refresh()
        self.assertEqual(self.ps_calls, [])
        self.assertEqual(self.pids(cat), [])

    def test_ps_not_answering_is_asked_again_next_time(self):
        self.add(101, ps=None)                                       # ps が答えない（終わる直前など）
        cat = self.make_catalog()
        self.ps_calls.clear()
        cat.refresh()
        self.assertEqual(self.ps_calls, [101])                       # 「無い」は覚えない
        self.ps[101] = PROC
        self.assertTrue(cat.refresh())
        self.assertEqual(self.pids(cat), [101])

    def test_removed_file_drops_out_and_a_new_file_with_the_same_name_is_read(self):
        self.add(101, name="前")
        cat = self.make_catalog()
        (self.sessions / "101.json").unlink()
        self.assertTrue(cat.refresh())
        self.assertEqual(self.pids(cat), [])
        self.add(101, name="後")
        self.assertTrue(cat.refresh())
        self.assertEqual(cat.overview()["sessions"][0]["name"], "後")


    def test_removed_file_is_forgotten_even_if_a_new_one_has_the_same_size_and_mtime(self):
        self.add(101, name="前")
        cat = self.make_catalog()
        path = self.sessions / "101.json"
        st = path.stat()
        path.unlink()
        self.assertTrue(cat.refresh())
        self.add(101, name="後")                                     # 同じ長さ。更新時刻も前のものに合わせる
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(path.stat().st_size, st.st_size)
        self.assertTrue(cat.refresh())
        self.assertEqual(cat.overview()["sessions"][0]["name"], "後")  # 前の中身を覚えたままにしない

    def test_verdict_is_forgotten_when_the_file_goes_away(self):
        self.add(101)
        cat = self.make_catalog()
        (self.sessions / "101.json").unlink()                        # プロセスは生きたまま、ファイルだけ無くなった
        cat.refresh()
        self.ps[101] = "Mon Oct 26 10:00:00 2026"                    # 同じ PID を別のプロセスが使っている
        self.add(101, ps="Mon Oct 26 10:00:00 2026")                 # ファイルは同じ procStart で書き直された
        self.ps_calls.clear()
        cat.refresh()
        self.assertEqual(self.ps_calls, [101])
        self.assertEqual(self.pids(cat), [])


class FolderIdentityCacheTest(CatalogCase):
    """フォルダの鍵・見せてよいか・カテゴリは realpath を引く重い仕事。同じフォルダなら引き直さない。"""

    def test_steady_refresh_resolves_no_paths(self):
        a = self.folder("10_work", "A")
        self.folder("30_devices", "B")
        plain = self.tmp / "elsewhere" / "plain"
        plain.mkdir(parents=True)
        self.add(101, cwd=str(a))
        self.add(102, cwd=str(plain))
        cat = self.make_catalog()
        with mock.patch("os.path.realpath", side_effect=AssertionError("realpath を引き直した")):
            self.assertFalse(cat.refresh())
            self.assertFalse(cat.refresh())
        self.assertEqual(len(cat.overview()["projects"]), 3)

    def test_symlink_retargeted_to_another_folder_gets_a_new_id(self):
        x, y = self.tmp / "x", self.tmp / "y"
        x.mkdir()
        y.mkdir()
        (self.lab / "10_work").mkdir()
        link = self.lab / "10_work" / "L"
        link.symlink_to(x)
        cat = self.make_catalog()
        self.assertEqual(self.project(cat, "L")["id"], folder_id(x))
        link.unlink()
        link.symlink_to(y)
        self.assertTrue(cat.refresh())
        self.assertEqual(self.project(cat, "L")["id"], folder_id(y))

    def test_folder_that_becomes_a_git_repo_is_marked_on_the_next_refresh(self):
        g = self.folder("10_work", "G")
        cat = self.make_catalog()
        self.assertFalse(self.project(cat, "G")["isGit"])
        git(g, "init", "-q")
        self.assertTrue(cat.refresh())
        self.assertTrue(self.project(cat, "G")["isGit"])

    def test_unchanged_launcher_tree_is_not_scanned_again(self):
        # lab_dir・カテゴリ・各フォルダの更新時刻が同じなら、フォルダを数え直さず、鍵も引き直さない（毎秒の見回りを軽くするため）
        a = self.folder("10_work", "A")
        self.folder("10_work", "B")
        self.folder("30_devices", "C")
        self.folder("40_空のカテゴリ")
        self.add(101, cwd=str(a))
        cat = self.make_catalog()
        before = cat.overview()
        with mock.patch("os.scandir", wraps=os.scandir) as scans, \
                mock.patch.object(cat, "_identify", wraps=cat._identify) as idents, \
                mock.patch("os.path.lexists", wraps=os.path.lexists) as gits:
            self.assertFalse(cat.refresh())
            self.assertFalse(cat.refresh())
        lab = os.path.realpath(self.lab)
        touched = [os.path.realpath(str(c.args[0] if c.args else ".")) for m in (scans, idents, gits)
                   for c in m.call_args_list]
        under_lab = [p for p in touched if p == lab or p.startswith(lab + os.sep)]
        self.assertEqual(under_lab, [])                         # ランチャーの下は数え直さず、鍵も .git も見直さない
        after = cat.overview()
        self.assertEqual((after["projects"], after["sessions"]), (before["projects"], before["sessions"]))
        self.assertEqual(cat.visible_roots(), self.make_catalog().visible_roots())

    def test_folder_added_under_a_category_appears_on_the_next_refresh(self):
        self.folder("10_work", "A")
        self.folder("40_空のカテゴリ")
        cat = self.make_catalog()
        self.assertEqual([p["name"] for p in cat.overview()["projects"]], ["A"])
        self.folder("10_work", "B")                            # あるカテゴリに足す
        self.assertTrue(cat.refresh())
        self.folder("40_空のカテゴリ", "D")                     # フォルダの無かったカテゴリに足す
        self.assertTrue(cat.refresh())
        self.folder("50_新しいカテゴリ", "E")                   # カテゴリごと足す
        self.assertTrue(cat.refresh())
        self.assertEqual([p["name"] for p in cat.overview()["projects"]], ["A", "B", "D", "E"])
        self.assertEqual(cat.overview()["projects"], self.make_catalog().overview()["projects"])

    def test_folder_removed_from_a_category_disappears_on_the_next_refresh(self):
        self.folder("10_work", "A")
        b = self.folder("10_work", "B")
        c = self.folder("30_devices", "C")
        cat = self.make_catalog()
        b_id = folder_id(b)
        self.assertIn(b_id, cat.visible_roots())
        b.rmdir()
        self.assertTrue(cat.refresh())
        self.assertEqual([p["name"] for p in cat.overview()["projects"]], ["A", "C"])
        self.assertNotIn(b_id, cat.visible_roots())             # ファイルの区画も閉じる
        c.rmdir()
        (self.lab / "30_devices").rmdir()                       # カテゴリごと消す
        self.assertTrue(cat.refresh())
        self.assertEqual([p["name"] for p in cat.overview()["projects"]], ["A"])

    def test_session_whose_cwd_vanished_still_lists(self):
        gone = self.tmp / "gone"
        gone.mkdir()
        self.add(101, cwd=str(gone))
        cat = self.make_catalog()
        gone.rmdir()
        self.assertFalse(cat.refresh())                              # 存在しなくなっても鍵は前のまま（一覧から消さない）
        self.assertEqual(self.pids(cat), [101])


class LauncherRootsTest(CatalogCase):
    """設定の launcherRoots（{"path", "depth"}）がランチャーの起点。depth 2 は <起点>/<分類>/<フォルダ>、depth 1 は <起点>/<フォルダ>。"""

    def catalog_of(self, roots, auto=False) -> Catalog:
        return Catalog(self.claude, roots, self.home, self.own, start_of=self.start_of, git=self.no_git,
                       alive=self.live.__contains__, secret=SECRET, launcher_auto=auto)

    def launcher(self, cat) -> list:
        """ランチャーの行：(名前, 分類) を名前順。"""
        return [(p["name"], p["category"]) for p in cat.overview()["projects"]]

    def test_launcher_from_config_roots(self):
        # <起点>/<分類>/<フォルダ> の形（分類は番号つき）を depth 2 で再現する。分類・フォルダの名前には NFD も混ざる
        lab = self.home / "Workspace"
        folders = [("10_work", "Alpha"), ("10_work", nfd("ガイド")), (nfd("40_ゲーム"), "Beta"),
                   ("40_ゲーム", "Gamma"), ("90_misc", "docs-only")]
        made = {}
        for cat, name in folders:
            (lab / cat / name).mkdir(parents=True)
            made[(nfc(cat), nfc(name))] = lab / cat / name
        (lab / ".hidden" / "x").mkdir(parents=True)                # . で始まる段は出さない
        (lab / "10_work" / ".hid").mkdir()
        (lab / "10_work" / "file.txt").write_text("x")             # フォルダでないものは出さない
        (lab / "loose").mkdir()                                    # 直下のフォルダは depth 2 では分類として数える（中が空なので行は無い）
        cat = self.catalog_of([(lab, 2)])
        got = {(p["category"], p["name"]): p for p in cat.overview()["projects"]}
        self.assertEqual(set(got), set(made))                      # 分類と名前は NFC。同じ組
        for key, path in made.items():
            self.assertEqual(got[key]["id"], folder_id(path))      # 実体（NFC にそろえた realpath の鍵）が同じ
            self.assertTrue(os.path.samefile(got[key]["path"], path))   # 綴りでなく実体で比べる（NFC/NFD は混ざる）
            self.assertIs(got[key]["filesVisible"], True)
            self.assertEqual(got[key]["sessionCount"], 0)
        self.assertEqual(set(cat.visible_roots()), {folder_id(p) for p in made.values()})

    def test_depth_1_lists_the_folders_directly_below_without_a_category(self):
        flat = self.home / "Projects2"
        for name in ("alpha", "beta", ".dot"):
            (flat / name).mkdir(parents=True)
        (flat / "file.txt").write_text("x")
        cat = self.catalog_of([(flat, 1)])
        self.assertEqual(self.launcher(cat), [("alpha", None), ("beta", None)])
        self.add(101, cwd=str(flat / "alpha" / "sub"))             # 中のフォルダも分類を持たない
        cat.refresh()
        self.assertEqual({p["name"]: p["category"] for p in cat.overview()["projects"]},
                         {"alpha": None, "beta": None, "sub": None})

    def test_several_roots_are_all_listed_and_a_folder_in_two_roots_once(self):
        lab, flat = self.home / "Lab", self.home / "Flat"
        (lab / "10_x" / "A").mkdir(parents=True)
        (flat / "B").mkdir(parents=True)
        (flat / "Lab").mkdir()
        cat = self.catalog_of([(lab, 2), (flat, 1), (lab / "10_x", 1)])   # 3つ目は A を二重に出しうる
        got = sorted((p["name"], p["category"]) for p in cat.overview()["projects"])
        self.assertEqual(got, [("A", "10_x"), ("B", None), ("Lab", None)])

    def test_a_missing_or_unreadable_root_is_skipped_without_failing(self):
        lab = self.home / "Lab"
        (lab / "10_x" / "A").mkdir(parents=True)
        cat = self.catalog_of([(self.home / "nowhere", 2), (lab, 2), (self.home / "also-nowhere", 1)])
        self.assertEqual(self.launcher(cat), [("A", "10_x")])
        cat.refresh()
        self.assertEqual(self.launcher(cat), [("A", "10_x")])

    def test_category_of_a_session_folder_comes_from_a_depth_2_root_only(self):
        lab, flat = self.home / "Lab", self.home / "Flat"
        (lab / "10_x" / "A").mkdir(parents=True)
        (flat / "B").mkdir(parents=True)
        self.add(101, cwd=str(lab / "10_x" / "A" / "deep"), bridgeSessionId="session_deep")
        self.add(102, cwd=str(flat / "B" / "deep"), bridgeSessionId="session_flat")
        cat = self.catalog_of([(lab, 2), (flat, 1)])
        self.assertEqual({p["path"]: p["category"] for p in cat.overview()["projects"] if p["name"] == "deep"},
                         {str(lab / "10_x" / "A" / "deep"): "10_x", str(flat / "B" / "deep"): None})

    def test_roots_given_as_strings_or_with_symlinks_work(self):
        real = self.home / "real"
        (real / "10_x" / "A").mkdir(parents=True)
        link = self.home / "link"
        link.symlink_to(real)
        self.assertEqual(self.launcher(self.catalog_of([(link, 2)])), [("A", "10_x")])

    # ---- 作り直しの印は変更時刻（ctime）。touch で戻せる更新時刻（mtime）ではない

    def test_launcher_rebuild_notices_a_change_even_when_mtime_was_set_back(self):
        a = self.folder("10_work", "A")
        cat = self.make_catalog()
        self.assertEqual(self.launcher(cat), [("A", "10_work")])
        stamps = {d: os.stat(d) for d in (self.lab, self.lab / "10_work")}
        self.folder("10_work", "B")                                # カテゴリに足す
        self.folder("20_new", "C")                                 # カテゴリごと足す
        for d, st in stamps.items():                               # touch -r のように、更新時刻を前の値に戻す
            os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertTrue(cat.refresh())
        self.assertEqual(self.launcher(cat), [("A", "10_work"), ("B", "10_work"), ("C", "20_new")])
        before = os.stat(a)
        git(a, "init", "-q")                                       # .git の出し入れ（そのフォルダの変更）も
        os.utime(a, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(cat.refresh())
        self.assertTrue(self.project(cat, "A")["isGit"])

    def test_launcher_key_is_made_of_change_times_not_modification_times(self):
        # chmod は更新時刻（mtime）を変えない。ランチャーの数え直しの鍵が ctime なら、chmod で鍵が動く
        a = self.folder("10_work", "A")
        cat = self.make_catalog()
        key = cat._launcher_stamp()
        mtime = os.stat(a).st_mtime_ns
        os.chmod(a, 0o700)
        self.assertEqual(os.stat(a).st_mtime_ns, mtime)            # 更新時刻は同じ
        self.assertNotEqual(cat._launcher_stamp(), key)            # 鍵は動く（作り直しになる）
        cat.refresh()                                              # 作り直す
        self.assertEqual(cat._launcher_stamp(), cat._launcher_key)


class AutoLauncherTest(CatalogCase):
    """launcherRoots が空のとき、会話ログの作業フォルダ（cwd）をランチャーにする（ホームの中・見せてよいものだけ）。"""

    def setUp(self):
        super().setUp()
        self.projects = self.claude / "projects"
        self.projects.mkdir()

    def transcript(self, place: str, name: str, cwd, age: float = 0.0, extra=()) -> Path:
        d = self.projects / place
        d.mkdir(exist_ok=True)
        p = d / f"{name}.jsonl"
        rows = [{"type": "summary"}, {"type": "user", "cwd": cwd, "message": {"content": "hi"}}, *extra]
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def auto(self, roots=()) -> Catalog:
        return Catalog(self.claude, list(roots), self.home, self.own, start_of=self.start_of, git=self.no_git,
                       alive=self.live.__contains__, secret=SECRET, launcher_auto=True)

    def names(self, cat) -> list:
        return [(p["name"], p["category"], p["filesVisible"]) for p in cat.overview()["projects"]]

    def test_launcher_auto_from_transcripts(self):
        a, b = self.home / "work" / "alpha", self.home / "work" / nfd("ベータ")
        for p in (a, b):
            p.mkdir(parents=True)
        outside = self.tmp / "elsewhere" / "outside"               # ホームの外
        outside.mkdir(parents=True)
        dotted = self.home / ".config" / "tool"                    # . で始まる場所
        dotted.mkdir(parents=True)
        library = self.home / "Library" / "Application Support" / "X"
        library.mkdir(parents=True)
        self.transcript("-a", "s1", str(a))
        self.transcript("-b", "s2", str(b))
        self.transcript("-c", "s3", str(outside))
        self.transcript("-d", "s4", str(dotted))
        self.transcript("-e", "s5", str(library))
        self.transcript("-f", "s6", str(self.claude / "projects"))  # Claude の設定フォルダの中
        self.transcript("-g", "s7", str(self.home))                  # ホームそのもの
        self.transcript("-h", "s8", str(self.home / "work" / "gone"))   # もう無いフォルダ
        cat = self.auto()
        self.assertEqual(self.names(cat), [("alpha", None, True), ("ベータ", None, True)])   # 名前は NFC、分類は無い
        self.assertEqual(set(cat.visible_roots()), {folder_id(a), folder_id(b)})
        self.assertEqual(cat.overview()["sessions"], [])

    def test_auto_launcher_marks_git_folders_and_joins_live_sessions(self):
        g = self.home / "work" / "repo"
        g.mkdir(parents=True)
        (g / ".git").mkdir()
        self.transcript("-a", "s1", str(g))
        self.add(101, cwd=str(g))
        cat = self.auto()
        self.assertEqual(len(cat.overview()["projects"]), 1)       # セッションのあるフォルダと同じ行になる
        self.assertEqual(cat.overview()["projects"][0]["sessionCount"], 1)
        self.assertIs(cat.overview()["projects"][0]["isGit"], True)

    def test_configured_roots_switch_the_auto_launcher_off(self):
        (self.lab / "10_x" / "A").mkdir(parents=True)
        other = self.home / "work" / "alpha"
        other.mkdir(parents=True)
        self.transcript("-a", "s1", str(other))
        cat = self.auto(roots=[(self.lab, 2)])
        self.assertEqual(self.names(cat), [("A", "10_x", True)])

    def test_without_the_auto_flag_nothing_is_read_from_transcripts(self):
        other = self.home / "work" / "alpha"
        other.mkdir(parents=True)
        self.transcript("-a", "s1", str(other))
        with self.spy_opens() as opened:
            cat = Catalog(self.claude, [], self.home, self.own, start_of=self.start_of, git=self.no_git,
                          alive=self.live.__contains__, secret=SECRET)
        self.assertEqual(cat.overview()["projects"], [])
        self.assertFalse(any(p.endswith(".jsonl") for p in opened), opened)

    def test_a_new_transcript_adds_a_row_and_a_deleted_folder_removes_it(self):
        a = self.home / "work" / "alpha"
        a.mkdir(parents=True)
        self.transcript("-a", "s1", str(a))
        cat = self.auto()
        self.assertEqual([n[0] for n in self.names(cat)], ["alpha"])
        b = self.home / "work" / "beta"
        b.mkdir()
        self.transcript("-b", "s2", str(b))
        self.assertTrue(cat.refresh())
        self.assertEqual([n[0] for n in self.names(cat)], ["alpha", "beta"])
        a.rmdir()
        self.assertTrue(cat.refresh())
        self.assertEqual([n[0] for n in self.names(cat)], ["beta"])
        self.assertNotIn(folder_id(a), cat.visible_roots())

    def test_steady_refresh_reads_no_transcripts_and_scans_no_folders(self):
        a = self.home / "work" / "alpha"
        a.mkdir(parents=True)
        self.transcript("-a", "s1", str(a))
        self.add(101, cwd=str(a))
        cat = self.auto()
        before = cat.overview()
        with mock.patch("os.scandir", wraps=os.scandir) as scans, \
                mock.patch.object(cat, "_identify", wraps=cat._identify) as idents, \
                self.spy_opens() as opened:
            self.assertFalse(cat.refresh())
            self.assertFalse(cat.refresh())
        self.assertEqual([c.args[0] for c in scans.call_args_list if str(c.args[0]).startswith(str(self.projects))], [])
        self.assertEqual(idents.call_args_list, [])
        self.assertFalse(any(p.endswith(".jsonl") for p in opened), opened)
        self.assertEqual(cat.overview()["projects"], before["projects"])

    def test_a_secret_name_or_an_unsafe_folder_in_a_transcript_is_not_offered(self):
        # 会話ログは持ち主が書き換えられる。cwd に何が書いてあっても、見せてよい規則を通ったものだけが出る
        keys = self.home / ".ssh"
        keys.mkdir()
        self.transcript("-a", "s1", str(keys))
        self.transcript("-b", "s2", str(self.home / "Library" / "Mobile Documents"))
        self.transcript("-c", "s3", "/")
        self.transcript("-d", "s4", "/etc")
        self.transcript("-e", "s5", str(self.own[0]))
        cat = self.auto()
        self.assertEqual(cat.overview()["projects"], [])
        self.assertEqual(cat.visible_roots(), {})


class ThreadSafetyTest(CatalogCase):
    def test_overview_and_session_return_copies(self):
        self.folder("10_work", "A")
        self.add(101, bridgeSessionId="session_1")
        cat = self.make_catalog()
        ov = cat.overview()
        ov["sessions"].clear()
        ov["projects"][0]["name"] = "壊した"
        ov["generatedAt"] = 0
        s = cat.session("session_1")
        s["status"] = "壊した"
        roots = cat.visible_roots()
        roots.clear()
        self.assertEqual(self.pids(cat), [101])
        self.assertNotEqual(cat.overview()["projects"][0]["name"], "壊した")
        self.assertEqual(cat.session("session_1")["status"], "idle")
        self.assertNotEqual(cat.overview()["generatedAt"], 0)
        self.assertTrue(cat.visible_roots())

    def test_readers_never_see_half_swapped_state(self):
        # 読む糸が、refresh の途中（プロジェクトだけ新しい、など）を見ない
        cat = self.make_catalog()
        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                ov = cat.overview()
                ids = {p["id"] for p in ov["projects"]}
                total = sum(p["sessionCount"] for p in ov["projects"])
                if any(s["projectId"] not in ids for s in ov["sessions"]) or total != len(ov["sessions"]):
                    errors.append(ov)
                    return
                stop.wait(0.001)                # 読み続けるだけで鍵を占めて refresh を飢えさせない

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for i in range(60):
                pid = 200 + i % 3
                if i % 2:
                    (self.sessions / f"{pid}.json").unlink(missing_ok=True)
                    self.live.discard(pid)
                else:
                    self.add(pid, cwd=str(self.tmp / "work" / f"p{i}"))
                cat.refresh()
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])


    def test_concurrent_refresh_calls_git_once_per_cwd(self):
        # refresh を同時に呼ばれても、読み直しは1本ずつ。git の答えの覚えと「変わったか」の答えが食い違わない
        calls = []

        def slow_git(cwd):
            calls.append(cwd)
            time.sleep(0.05)
            return None

        cat = self.make_catalog(git=slow_git)
        self.add(101)
        results = []
        threads = [threading.Thread(target=lambda: results.append(cat.refresh())) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(results), [False] * 5 + [True])      # 変わったと知らせるのは1回だけ


class PsStartTest(unittest.TestCase):
    def test_own_process_start_is_utc_and_whitespace_collapsed(self):
        s = ps_start(os.getpid())
        self.assertRegex(s, r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d\d:\d\d:\d\d \d{4}$")
        started = datetime.strptime(s, "%a %b %d %H:%M:%S %Y")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertLess(abs((now - started).total_seconds()), 3600)   # UTC で読んでこの試験の起動の頃になる

    def test_gone_process_is_none(self):
        self.assertIsNone(ps_start(dead_pid()))


class RunGitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_repo_and_subdir(self):
        repo = make_repo(self.tmp / "repo")
        (repo / "sub").mkdir()
        real = os.path.realpath(repo)
        self.assertEqual(run_git(str(repo)), (real, os.path.join(real, ".git")))
        self.assertEqual(run_git(str(repo / "sub")), (real, os.path.join(real, ".git")))

    def test_worktree_has_own_toplevel_and_shared_common_dir(self):
        repo = make_repo(self.tmp / "repo")
        wt = self.tmp / "wt"
        git(repo, "worktree", "add", "-q", "-b", "w", str(wt))
        top, common = run_git(str(wt))
        self.assertEqual(top, os.path.realpath(wt))
        self.assertEqual(common, os.path.join(os.path.realpath(repo), ".git"))

    def test_not_a_repo_or_missing_dir_is_none(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(run_git(str(plain)))
        self.assertIsNone(run_git(str(self.tmp / "missing")))
        self.assertIsNone(run_git("bad\0path"))

    def test_japanese_nfd_path(self):
        repo = make_repo(self.tmp / nfd("デバイス"))
        top, _ = run_git(str(repo))
        self.assertEqual(folder_id(top), folder_id(repo))


class DiagnosticsTest(CatalogCase):
    """Catalog.diagnostics()：sessions/*.json の形が合わないものを黙って落とさず、数える。

    数えるのは見回りのとき（_live_sessions）。要求のたびにファイルを数え直さない。
    files = malformed + dead + noBridge + procMismatch + 出るもの（二重起動の整理の前）。
    """

    def counts(self, cat) -> dict:
        return cat.diagnostics()["sessions"]

    def test_diagnostics_counts(self):
        self.add(101)                                           # 出る
        self.add(102, live=False)                               # 死んだ PID                                → dead
        self.add(103, ps="Mon Oct 26 10:00:00 2026")            # ps と違う（PID の使い回し）               → procMismatch
        self.add(104, drop=("bridgeSessionId",))                # 生きているが bridgeSessionId なし         → noBridge
        self.add(105, bridgeSessionId="")                       # 空も同じ                                  → noBridge
        self.add(106, procStart="1792936873127", ps=PROC)              # procStart の形が違う（ps の日付の形でない） → procMismatch
        self.add(107, drop=("procStart",))                      # procStart の項目が無い                    → procMismatch
        self.add(108, ps=None)                                  # ps が答えない（確かめられない）            → procMismatch
        self.add(109, live=False, drop=("bridgeSessionId",))    # 死んでいて bridge も無い                  → dead（死んだ方が先）
        self.write("a.json", "[]")                              # 辞書でない                                → malformed
        self.write("b.json", "not json")                        #                                           → malformed
        self.write("c.json", json.dumps(self.data(os.getpid(), cwd=None)))     # cwd が型違い            → malformed
        self.write("d.json", json.dumps({**self.data(1), "pid": "7"}))         # pid が型違い            → malformed
        cat = self.make_catalog()
        self.assertEqual(self.pids(cat), [101])
        self.assertEqual(self.counts(cat), {"files": 13, "malformed": 4, "dead": 2, "procMismatch": 4, "noBridge": 2})
        self.assertEqual(sum(v for k, v in self.counts(cat).items() if k != "files") + 1, 13)   # 出る1本を足すと files

    def test_an_exception_while_asking_ps_counts_as_proc_mismatch_not_malformed(self):
        # 形の合ったファイル（pid・cwd が使える）の確かめで ps の問い合わせが例外を出しても、ファイルは壊れていない。
        # 確かめられなかった＝procMismatch（malformed に混ぜると「ファイルの形が崩れた」と読めてしまう）
        self.add(101)
        self.add(102)
        self.explode.add(102)
        self.write("x.json", "not json")                        # 本当に壊れたものは malformed のまま
        cat = self.make_catalog()
        self.assertEqual(self.pids(cat), [101])
        self.assertEqual(self.counts(cat), {"files": 3, "malformed": 1, "dead": 0, "procMismatch": 1, "noBridge": 0})
        self.explode.clear()                                    # 直れば、次の見回りで出る
        cat.refresh()
        self.assertEqual(self.counts(cat), {"files": 3, "malformed": 1, "dead": 0, "procMismatch": 0, "noBridge": 0})
        self.assertEqual(self.pids(cat), [101, 102])

    def test_an_exception_before_the_shape_is_known_stays_malformed(self):
        # pid・cwd が使える形だと分かる前（_usable を通る前）の例外は、ファイルの形の問題として malformed のまま
        self.add(101)
        cat = self.make_catalog()
        with mock.patch.object(cat, "_read", side_effect=RuntimeError("boom")):
            cat.refresh()
        self.assertEqual(self.counts(cat), {"files": 1, "malformed": 1, "dead": 0, "procMismatch": 0, "noBridge": 0})

    def test_ps_not_being_asked_for_a_session_without_bridge(self):
        # noBridge は kill -0 だけで決める（Remote Control の無いセッションのために ps を起こさない）
        self.add(104, drop=("bridgeSessionId",))
        cat = self.make_catalog()
        self.assertEqual(self.counts(cat)["noBridge"], 1)
        self.assertEqual(self.ps_calls, [])

    def test_half_written_file_is_counted_and_then_not(self):
        cat = self.make_catalog()
        self.write("102.json", '{"pid": 102, "sess')            # 書きかけ
        self.live.add(102)
        self.ps[102] = PROC
        cat.refresh()
        self.assertEqual(self.counts(cat), {"files": 1, "malformed": 1, "dead": 0, "procMismatch": 0, "noBridge": 0})
        self.add(102)                                           # 書き終わった
        cat.refresh()
        self.assertEqual(self.counts(cat), {"files": 1, "malformed": 0, "dead": 0, "procMismatch": 0, "noBridge": 0})

    def test_a_file_that_vanishes_between_the_listing_and_the_read_is_not_counted(self):
        # 終わったセッションが自分の json を消した。壊れたファイルとして数えない
        self.add(101)
        cat = self.make_catalog()
        gone = self.sessions / "101.json"
        real_stat = Path.stat

        def stat(path, *a, **kw):
            if path == gone:
                raise FileNotFoundError(str(path))
            return real_stat(path, *a, **kw)

        cat._files.clear()
        with mock.patch.object(Path, "stat", stat):
            cat.refresh()
        self.assertEqual(self.counts(cat)["malformed"], 0)

    def test_counts_follow_each_refresh(self):
        self.add(101)
        cat = self.make_catalog()
        self.assertEqual(self.counts(cat)["dead"], 0)
        self.live.discard(101)                                  # 終わった
        cat.refresh()
        self.assertEqual(self.counts(cat), {"files": 1, "malformed": 0, "dead": 1, "procMismatch": 0, "noBridge": 0})
        (self.sessions / "101.json").unlink()                   # ファイルも消えた
        cat.refresh()
        self.assertEqual(self.counts(cat)["files"], 0)

    def test_unchanged_files_are_counted_without_opening_or_asking_ps(self):
        self.add(101)
        self.add(102, ps="Mon Oct 26 10:00:00 2026")
        self.add(103, drop=("bridgeSessionId",))
        self.write("x.json", "not json")
        cat = self.make_catalog()
        before = (list(self.ps_calls), self.counts(cat))
        with self.spy_opens() as opened:
            cat.refresh()
            cat.refresh()
            self.assertEqual(self.counts(cat), before[1])
        # 読めたファイルは開かない。読めなかったもの（x.json）だけは、書きかけかもしれないので毎回読み直す（もとからの決まり）
        self.assertEqual({os.path.basename(p) for p in opened if p.endswith(".json")}, {"x.json"})
        self.assertEqual(self.ps_calls, before[0])                              # ps も聞き直さない

    def test_claude_code_versions_are_distinct_sorted_and_cover_every_readable_file(self):
        self.add(101, version="2.1.306")
        self.add(102, version="2.1.306")
        self.add(103, version="2.1.310", live=False)            # 死んだセッションの版も（形が変わる前の版が分かる）
        self.add(104, version="2.2.0", drop=("bridgeSessionId",))
        self.add(105, version=2)                                # 文字列でないものは入れない
        self.add(106, drop=("version",))
        self.write("x.json", "not json")
        cat = self.make_catalog()
        self.assertEqual(cat.diagnostics()["claudeCodeVersions"], ["2.1.306", "2.1.310", "2.2.0"])

    def test_claude_code_versions_are_bounded(self):
        # 持ち主が自由に書けるファイルの値。長さも種類の数も上限を置く（診断の応答を膨らませない）
        for i in range(30):
            self.add(200 + i, version=f"9.{i}.0", live=False)
        self.add(300, version="v" * 500, live=False)
        versions = self.make_catalog().diagnostics()["claudeCodeVersions"]
        self.assertLessEqual(len(versions), 16)
        self.assertTrue(all(len(v) <= 40 for v in versions), versions)

    def test_diagnostics_returns_a_copy(self):
        self.add(101, version="2.1.306")
        cat = self.make_catalog()
        d = cat.diagnostics()
        d["sessions"]["files"] = 99
        d["claudeCodeVersions"].append("x")
        self.assertEqual(self.counts(cat)["files"], 1)
        self.assertEqual(cat.diagnostics()["claudeCodeVersions"], ["2.1.306"])

    def test_missing_sessions_dir_counts_nothing(self):
        self.sessions.rmdir()
        cat = self.make_catalog()
        self.assertEqual(cat.diagnostics(), {"sessions": {"files": 0, "malformed": 0, "dead": 0, "procMismatch": 0,
                                                          "noBridge": 0}, "claudeCodeVersions": []})

    def test_root_hidden_tells_a_hidden_root_from_an_unknown_one(self):
        # 403 hidden（見せない起点）と 404（知らない起点）を分けるための問い。rootId は secret 入りなので、知っている者にだけ意味がある
        proj = self.folder("30_cat", "proj")
        self.add(101, cwd=str(proj))
        self.add(102, cwd=str(self.home))
        cat = self.make_catalog()
        ids = {s["pid"]: s["rootId"] for s in cat.overview()["sessions"]}
        self.assertFalse(cat.root_hidden(ids[101]))             # 見せてよい
        self.assertTrue(cat.root_hidden(ids[102]))              # ホームそのもの：見せない
        self.assertFalse(cat.root_hidden("0123456789abcdef"))   # 知らない
        self.assertFalse(cat.root_hidden(""))


if __name__ == "__main__":
    unittest.main()
