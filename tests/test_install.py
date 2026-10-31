"""install.sh・uninstall.sh・bblist/installtool.py・準備のプロンプト・説明書の試験。
python3 -m unittest discover -s tests -t . -p 'test_install.py' -v

本物の ~/Library・本物の launchctl・ポート 8772/8773 には触れない。HOME は tmp。launchctl は PATH の先頭に置いた偽物
（受けた引数を書き残す。`bootstrap` では plist の ProgramArguments のとおり、本物の listserver.py を tmp の HOME で起こし、
`bootout` で止める。つまり入れる→起動→/v1/health の通しを、本物のサーバーのコードで確かめる）。
xcode-select・tailscale も偽物（状態は state フォルダのファイルで切り替える）。plutil は本物（tmp の plist を調べるだけ）。
偽物が本当に先に見つかることを、走らせる前に確かめる（見つからなければ、動いているサーバーを止めてしまう）。
本物の /opt/homebrew などの探し場所は BRIDGEBOARD_FS_ROOT で空のフォルダに向ける。ポートは空きを選んで config.json に書く。
標準出力を端末にする回は pty で作る。
"""
from __future__ import annotations

import http.server
import json
import os
import pty
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bblist import VERSION, installtool
from bblist.sessions import ps_start

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
UNINSTALL = REPO / "uninstall.sh"
LABEL = "jp.onemorelab.bridgeboard.server"
HEALTH_WAIT = "40"                                   # 本物のサーバーを起こす回の上限（秒）。通れば早く終わる
SHORT_WAIT = "3"                                     # 答えが来ない回は短くする


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# bootout はすぐ返る（プロセスが終わるのを待たない）。本物がどちらでも、install.sh が自分で終わりを待つことを確かめるため
LAUNCHCTL = r'''#!/bin/sh
echo "$@" >> "$FAKE_STATE/launchctl.calls"
case "$1" in
  print) [ -e "$FAKE_STATE/loaded" ]; exit $? ;;
  bootstrap)
    if [ -e "$FAKE_STATE/bootstrap-fails" ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    touch "$FAKE_STATE/loaded"
    if [ -e "$FAKE_STATE/run-server" ]; then
      py="$(/usr/bin/plutil -extract ProgramArguments.0 raw -o - "$3")"
      script="$(/usr/bin/plutil -extract ProgramArguments.1 raw -o - "$3")"
      "$py" "$script" > "$FAKE_STATE/server.out" 2>&1 < /dev/null &
      echo $! > "$FAKE_STATE/pid"
    fi
    ;;
  bootout)
    if [ -f "$FAKE_STATE/pid" ]; then kill "$(cat "$FAKE_STATE/pid")" 2> /dev/null; rm -f "$FAKE_STATE/pid"; fi
    rm -f "$FAKE_STATE/loaded"
    ;;
esac
exit 0
'''
XCODE_SELECT = '#!/bin/sh\n[ -e "$FAKE_STATE/no-clt" ] && exit 2\necho /Library/Developer/CommandLineTools\n'
TAILSCALE = r'''#!/bin/sh
state="$(cat "$FAKE_STATE/ts-state" 2> /dev/null || echo Running)"
dns="$(cat "$FAKE_STATE/ts-dns" 2> /dev/null || echo on)"        # on | off（MagicDNS が無効）| noname（名前が無い）
if [ -e "$FAKE_STATE/ts-slow" ] && [ "$*" = "ip -4" ]; then sleep 2; exit 1; fi   # 遅く、しかも答えない
case "$*" in
  "ip -4") echo 127.0.0.1 ;;
  "status --json")
    case "$dns" in
      off) printf '{"BackendState":"%s","CurrentTailnet":{"MagicDNSEnabled":false},"Self":{"DNSName":"fake-mac.example.ts.net."}}\n' "$state" ;;
      noname) printf '{"BackendState":"%s","CurrentTailnet":{"MagicDNSEnabled":true},"Self":{}}\n' "$state" ;;
      *) printf '{"BackendState":"%s","CurrentTailnet":{"MagicDNSEnabled":true},"Self":{"DNSName":"fake-mac.example.ts.net."}}\n' "$state" ;;
    esac ;;
  *) exit 1 ;;
esac
'''
OLD_PYTHON = "#!/bin/sh\nexit 1\n"                    # `python3 -c "…3.9 以上か…"` に「いいえ」と答える python3


class InstallCase(unittest.TestCase):
    run_server = True                                 # launchctl の偽物が、bootstrap で本物のサーバーを起こすか

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.tmp = tmp
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.fsroot = tmp / "fsroot"
        for d in (self.home, self.state, self.fsroot):
            d.mkdir()
        self.bin = tmp / "bin"
        self.tsbin = tmp / "tsbin"
        self.oldpy = tmp / "oldpy"
        for d, name, body in ((self.bin, "launchctl", LAUNCHCTL), (self.bin, "xcode-select", XCODE_SELECT),
                              (self.tsbin, "tailscale", TAILSCALE), (self.oldpy, "python3", OLD_PYTHON)):
            d.mkdir(exist_ok=True)
            (d / name).write_text(body, encoding="utf-8")
            (d / name).chmod(0o755)
        self.system_path = "/usr/bin:/bin:/usr/sbin:/sbin"
        self.use_home(self.home)
        self.set_path(f"{self.bin}:{self.tsbin}:{self.system_path}")
        found = shutil.which("launchctl", path=self.env["PATH"])
        if found != str(self.bin / "launchctl"):         # 本物の launchctl を呼ぶくらいなら走らせない
            self.fail(f"偽の launchctl が先に見つからない: {found}")
        self.calls = self.state / "launchctl.calls"
        self.api_port, self.file_port = free_port(), free_port()
        if self.run_server:
            (self.state / "run-server").touch()
        self.addCleanup(self.stop_server)

    def use_home(self, home: Path) -> None:
        self.home = home
        self.support = home / "Library" / "Application Support" / "BridgeBoard"
        self.logs = home / "Library" / "Logs" / "BridgeBoard"
        self.plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if hasattr(self, "env"):
            self.set_path(self.env["PATH"])

    def set_path(self, path: str) -> None:
        self.env = {"HOME": str(self.home), "PATH": path, "LANG": "en_US.UTF-8", "FAKE_STATE": str(self.state),
                    "BRIDGEBOARD_FS_ROOT": str(self.fsroot), "BRIDGEBOARD_HEALTH_WAIT": HEALTH_WAIT}

    def stop_server(self) -> None:
        pid_file = self.state / "pid"
        if not pid_file.exists():
            return
        pid = int(pid_file.read_text())
        for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            end = time.monotonic() + wait
            while time.monotonic() < end:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)

    # ---- 道具

    def write_config(self, mode: int = 0o600, **items) -> Path:
        """置き場に config.json を書く（ポートは空きを選んで入れる。8772/8773 は使わない）。"""
        self.support.mkdir(parents=True, exist_ok=True)
        os.chmod(self.support, 0o700)
        data = {"apiPort": self.api_port, "filePort": self.file_port, **items}
        assert data["apiPort"] not in (8772, 8773) and data["filePort"] not in (8772, 8773)
        path = self.support / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def read_config(self) -> dict:
        return json.loads((self.support / "config.json").read_text(encoding="utf-8"))

    def make_live_session(self) -> None:
        sessions = self.home / ".claude" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        project = self.home / "project"
        project.mkdir(exist_ok=True)
        pid = os.getpid()
        (sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": "00000000-0000-4000-8000-000000000001", "cwd": str(project),
            "bridgeSessionId": "session_test", "procStart": ps_start(pid), "version": "2.1.310"}), encoding="utf-8")

    def run_script(self, script: Path, *args: str, env=None, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(script), *args], env=env or self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)

    def install(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.run_script(INSTALL, *args, **kw)

    def run_tty(self, script: Path, *args: str) -> tuple:
        """標準出力を端末（pty）にして走らせる（ユーザーが自分の端末で走らせたとき）。(終了コード, 標準出力, 標準エラー)。"""
        master, slave = pty.openpty()
        try:
            p = subprocess.Popen(["/bin/bash", str(script), *args], env=self.env, stdin=subprocess.DEVNULL,
                                 stdout=slave, stderr=subprocess.PIPE)
        finally:
            os.close(slave)
        out = b""
        try:
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:                           # 子が端末を閉じた（EIO）
                    break
                if not chunk:
                    break
                out += chunk
        finally:
            os.close(master)
        err = p.stderr.read()
        p.stderr.close()
        return p.wait(timeout=120), out.decode("utf-8").replace("\r\n", "\n"), err.decode("utf-8")

    def launchctl_calls(self) -> list:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def assert_installed(self, r: subprocess.CompletedProcess) -> None:
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)


# ---------------------------------------------------------------- --check

class CheckTest(InstallCase):
    run_server = False

    def setUp(self):
        super().setUp()
        self.listener = None
        self.make_live_session()
        self.write_config()

    def check(self) -> tuple:
        r = self.install("--check")
        lines = r.stdout.splitlines()
        return r, {line.split()[1]: line for line in lines}, lines

    def reset_fakes(self) -> None:
        """subTest の間に、偽の環境を元に戻す。"""
        for name in ("no-clt", "ts-state", "ts-dns"):
            (self.state / name).unlink(missing_ok=True)
        self.set_path(f"{self.bin}:{self.tsbin}:{self.system_path}")
        shutil.rmtree(self.home / ".claude", ignore_errors=True)
        self.make_live_session()
        if self.listener is not None:
            self.listener.close()
            self.listener = None

    def listen_on_api_port(self) -> None:
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", self.api_port))
        self.listener.listen()

    def test_check_all_ok(self):
        r, by_item, lines = self.check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([l.split()[:2] for l in lines],
                         [["ok", "clt"], ["ok", "python>=3.9"], ["ok", "tailscale"], ["ok", "magicdns"], ["ok", "ports"],
                          ["ok", "claude-sessions"], ["ok", "remote-control"]])
        self.assertIn("1 live session", by_item["remote-control"])
        self.assertEqual(r.stderr, "")

    def test_check_changes_nothing(self):
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        self.check()
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")), before)
        self.assertEqual([c for c in self.launchctl_calls() if c.split()[0] in ("bootstrap", "bootout")], [])

    def test_check_reports_each_item(self):
        def clt():
            (self.state / "no-clt").touch()

        def python():
            self.set_path(f"{self.oldpy}:{self.bin}:{self.tsbin}:{self.system_path}")

        def tailscale_missing():
            self.set_path(f"{self.bin}:{self.system_path}")

        def tailscale_stopped():
            (self.state / "ts-state").write_text("Stopped")

        def magicdns_off():
            (self.state / "ts-dns").write_text("off")

        def magicdns_without_a_name():
            (self.state / "ts-dns").write_text("noname")

        def sessions():
            shutil.rmtree(self.home / ".claude" / "sessions")

        # (直す前の準備, fail になる項目, 理由の文に入る語)
        cases = [(clt, "clt", "xcode-select --install"), (python, "python>=3.9", "no Python"),
                 (tailscale_missing, "tailscale", "not found"), (tailscale_stopped, "tailscale", "Stopped"),
                 (magicdns_off, "magicdns", "turned off"), (magicdns_without_a_name, "magicdns", "no DNS name"),
                 (self.listen_on_api_port, "ports", str(self.api_port)), (sessions, "claude-sessions", "not found")]
        for setup, item, word in cases:
            with self.subTest(item=item, word=word):
                self.reset_fakes()
                setup()
                r, by_item, lines = self.check()
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertTrue(by_item[item].startswith(f"fail {item} "), by_item[item])
                self.assertIn(word, by_item[item])
                self.assertEqual(len(lines), 7, lines)             # 7項目とも1行ずつ（落ちた項目の後も続ける）
                if item not in ("clt", "python>=3.9"):             # この2つは、あとの項目を調べられなくする。ほかは巻き込まない
                    self.assertEqual([l.split()[1] for l in lines if l.startswith("fail ")], [item], lines)
                if item == "tailscale":                            # Tailscale が使えないときの MagicDNS は、失敗を重ねず「調べていない」
                    self.assertTrue(by_item["magicdns"].startswith("warn magicdns not checked"), by_item["magicdns"])

    def test_without_python_the_other_items_are_reported_as_not_checked(self):
        (self.state / "no-clt").touch()
        r, by_item, lines = self.check()
        self.assertEqual(r.returncode, 3)
        for item in ("tailscale", "magicdns", "ports", "claude-sessions", "remote-control"):
            self.assertTrue(by_item[item].startswith(f"fail {item} not checked"), by_item[item])

    def test_no_remote_control_session_is_a_warning_not_a_failure(self):
        shutil.rmtree(self.home / ".claude" / "sessions")
        (self.home / ".claude" / "sessions").mkdir()
        r, by_item, lines = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(by_item["remote-control"].startswith("warn remote-control 0 live sessions"), by_item["remote-control"])
        self.assertTrue(by_item["claude-sessions"].startswith("ok "))

    def test_a_busy_port_is_fine_when_our_own_launchagent_is_the_one_loaded(self):
        self.listen_on_api_port()
        (self.state / "loaded").touch()                  # launchctl print が 0 で終わる＝入れてある
        r, by_item, lines = self.check()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("will be restarted", by_item["ports"])

    def test_check_does_not_run_the_system_python_stub_without_the_command_line_tools(self):
        # CLT が無い Mac の /usr/bin/python3 は、実行すると導入の窓を開く。--check は実行しない
        (self.state / "no-clt").touch()
        marker = self.tmp / "ran"
        fake_usr = self.fsroot / "usr" / "bin"
        fake_usr.mkdir(parents=True)
        (fake_usr / "python3").write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n", encoding="utf-8")
        (fake_usr / "python3").chmod(0o755)
        self.check()                                        # PATH の python3 は本物の /usr/bin/python3（これも実行しない）
        self.assertFalse(marker.exists(), "CLT が無いのに /usr/bin/python3 を実行した")

    def test_python_search_order(self):
        """config.json の python → PATH → /opt/homebrew/bin → /usr/local/bin（→ CLT があれば /usr/bin）。"""
        def fake_python(path: Path) -> Path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")        # 3.9 以上か？ に「はい」
            path.chmod(0o755)
            return path

        def python_line() -> str:
            return next(l for l in self.install("--check").stdout.splitlines() if l.startswith(("ok python", "fail python")))

        (self.state / "no-clt").touch()                    # /usr/bin は候補から外れる
        self.set_path(f"{self.bin}:{self.system_path}")    # PATH に python3 の偽物は無い（/usr/bin/python3 は外れる）
        brew = fake_python(self.fsroot / "opt" / "homebrew" / "bin" / "python3")
        local = fake_python(self.fsroot / "usr" / "local" / "bin" / "python3")
        self.assertEqual(python_line(), f"ok python>=3.9 {brew}")
        brew.unlink()
        self.assertEqual(python_line(), f"ok python>=3.9 {local}")
        on_path = fake_python(self.tmp / "pathdir" / "python3")
        self.set_path(f"{on_path.parent}:{self.bin}:{self.system_path}")
        self.assertEqual(python_line(), f"ok python>=3.9 {on_path}")
        configured = fake_python(self.tmp / "mine" / "python3")
        self.write_config(python=str(configured))
        self.assertEqual(python_line(), f"ok python>=3.9 {configured}")
        os.chmod(self.support / "config.json", 0o666)      # 他人が書ける config.json の python は信じない
        self.assertEqual(python_line(), f"ok python>=3.9 {on_path}")
        local.unlink()
        on_path.unlink()
        self.assertTrue(python_line().startswith("fail python>=3.9"))

    # ---- launchd の下で動く Python だけを選ぶ

    def make_venv(self) -> Path:
        """本物の仮想環境（sys.prefix が元の Python と違う）。その python3 を返す。"""
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
        return venv / "bin" / "python3"

    def fake_python(self, path: Path, body: str = "exit 0\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def python_line(self) -> str:
        return next(l for l in self.install("--check").stdout.splitlines() if l.startswith(("ok python", "fail python")))

    def test_a_python_in_a_venv_is_skipped(self):
        # 仮想環境を有効にしたシェルでは、PATH の python3 がその仮想環境のもの。launchd の下では、その環境は消えている
        venv_python = self.make_venv()
        (self.state / "no-clt").touch()
        brew = self.fake_python(self.fsroot / "opt" / "homebrew" / "bin" / "python3")
        self.set_path(f"{venv_python.parent}:{self.bin}:{self.system_path}")
        self.assertEqual(shutil.which("python3", path=self.env["PATH"]), str(venv_python))     # 試験の前提：先に見つかるのは仮想環境
        self.assertEqual(self.python_line(), f"ok python>=3.9 {brew}")
        brew.unlink()
        self.assertTrue(self.python_line().startswith("fail python>=3.9"), self.python_line())    # 仮想環境しか無ければ見つからない

    def test_a_python_that_only_works_with_the_shell_path_is_skipped(self):
        # pyenv の shim のように、PATH に別の道具が無いと動かないもの。launchd の PATH は /usr/bin:/bin:/usr/sbin:/sbin だけ
        tools = self.tmp / "tools"
        self.fake_python(tools / "pyreal")                                            # 本体。シェルの PATH にだけある
        shim = self.fake_python(self.tmp / "shims" / "python3", "exec pyreal \"$@\"\n")
        (self.state / "no-clt").touch()
        brew = self.fake_python(self.fsroot / "opt" / "homebrew" / "bin" / "python3")
        self.set_path(f"{shim.parent}:{tools}:{self.bin}:{self.system_path}")
        self.assertEqual(subprocess.run([str(shim)], env=self.env).returncode, 0)             # 試験の前提：シェルの PATH では動く
        self.assertNotEqual(subprocess.run([str(shim)], env={"PATH": self.system_path}, stderr=subprocess.DEVNULL).returncode, 0)   # launchd の PATH では動かない
        self.assertEqual(self.python_line(), f"ok python>=3.9 {brew}")

    def test_the_candidates_are_run_with_the_launchd_environment(self):
        # 環境変数を持ち込まない：HOME・PYTHONPATH などの無い、PATH だけの状態で、3.9 以上かを確かめる
        seen = self.tmp / "seen-env"
        probe = self.fake_python(self.fsroot / "opt" / "homebrew" / "bin" / "python3",
                                 f'if [ "$1" = -c ]; then echo "home=${{HOME-unset}} sentinel=${{BB_SENTINEL-unset}} path=$PATH" > "{seen}"; fi\n'
                                 'exit 0\n')                                      # 確かめ（-c）のときだけ書く。この偽物は後で installtool の代わりにも走る
        (self.state / "no-clt").touch()
        self.set_path(f"{self.bin}:{self.system_path}")
        self.env["BB_SENTINEL"] = "1"
        self.assertEqual(self.python_line(), f"ok python>=3.9 {probe}")
        self.assertEqual(seen.read_text(encoding="utf-8").strip(), "home=unset sentinel=unset path=/usr/bin:/bin:/usr/sbin:/sbin")

    def test_a_configured_python_that_fails_the_check_is_not_used(self):
        venv_python = self.make_venv()
        (self.state / "no-clt").touch()
        brew = self.fake_python(self.fsroot / "opt" / "homebrew" / "bin" / "python3")
        self.set_path(f"{self.bin}:{self.system_path}")
        self.write_config(python=str(venv_python))                                        # 前の入れ方が選んでしまった仮想環境
        self.assertEqual(self.python_line(), f"ok python>=3.9 {brew}")


# ---------------------------------------------------------------- install

class InstallTest(InstallCase):
    def setUp(self):
        super().setUp()
        self.write_config()

    def test_install_writes_versions_current_and_config(self):
        r = self.install()
        self.assert_installed(r)
        version_dir = self.support / "versions" / VERSION
        self.assertTrue((version_dir / "listserver.py").is_file())
        self.assertTrue((version_dir / "bblist" / "web.py").is_file())
        self.assertTrue((version_dir / "vendor" / "qrcodegen.py").is_file())
        for left_out in ("tests", ".git", "install.sh", "README.md", "setup"):
            self.assertFalse((version_dir / left_out).exists(), left_out)
        current = self.support / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current), f"versions/{VERSION}")            # 相対リンク（置き場ごと動かせる）
        # config.json：Python と Tailscale を書く。既定の項目が揃う
        config = self.read_config()
        self.assertEqual(config["tailscale"], str(self.tsbin / "tailscale"))
        self.assertTrue(config["python"].endswith("python3"))
        self.assertEqual((config["apiPort"], config["filePort"]), (self.api_port, self.file_port))
        self.assertEqual((config["showGitIgnored"], config["previewExternal"], config["launcherRoots"]), (False, True, []))
        self.assertEqual(config["claudeDir"], "~/.claude")
        self.assertEqual(oct(os.stat(self.support / "config.json").st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.support).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(self.logs).st_mode & 0o777), "0o700")
        # LaunchAgent：current/listserver.py を、見つけた Python で動かす
        lint = subprocess.run(["/usr/bin/plutil", "-lint", str(self.plist)], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertEqual((self.plist_arg(0), self.plist_arg(1)), (config["python"], f"{self.support}/current/listserver.py"))
        # launchctl：先に外してから入れる
        calls = self.launchctl_calls()
        self.assertEqual([c.split()[0] for c in calls if c.split()[0] != "print"], ["bootout", "bootstrap"])
        self.assertIn(f"bootstrap gui/{os.getuid()} {self.plist}", calls)
        # 入れた後の確かめ：本物のサーバーが答えている
        self.assertIn("Listening on 127.0.0.1", (self.logs / "listserver.log").read_text(encoding="utf-8"))
        self.assertIn(f"http://127.0.0.1:{self.api_port}/v1/health", r.stdout)
        # 最後の行は次にやること（パスは引用符で包む。置き場の名前に空白があるため）
        self.assertEqual(r.stdout.splitlines()[-1], f'next: run "{current}/bbctl" pair')
        self.assertEqual(r.stderr, "")

    def plist_arg(self, index: int) -> str:
        return subprocess.run(["/usr/bin/plutil", "-extract", f"ProgramArguments.{index}", "raw", "-o", "-", str(self.plist)],
                              capture_output=True, text=True).stdout.rstrip("\n")

    def test_the_bbctl_launcher_uses_the_chosen_python(self):
        self.assert_installed(self.install())
        launcher = self.support / "current" / "bbctl"
        self.assertTrue(os.access(launcher, os.X_OK))
        r = subprocess.run([str(launcher), "devices", "--support-dir", str(self.tmp / "other")], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "No paired devices."), r.stderr)
        text = launcher.read_text(encoding="utf-8")
        self.assertIn(self.read_config()["python"], text)
        self.assertNotIn("@PYTHON@", text)

    def test_install_again_replaces_the_server_and_keeps_old_versions(self):
        self.assert_installed(self.install())
        stray = self.support / "versions" / VERSION / "stray.txt"
        stray.write_text("x")
        old = self.support / "versions" / "0.0.1"
        old.mkdir()
        self.assert_installed(self.install())                # 動いているサーバーを外してから入れ替える
        self.assertFalse(stray.exists())                   # 同じ版は丸ごと入れ替える
        self.assertTrue(old.is_dir())                      # 古い版は消さない（戻すため）
        self.assertEqual(len([c for c in self.launchctl_calls() if c.startswith("bootstrap")]), 2)
        self.assertEqual(sorted(p.name for p in self.support.iterdir() if p.name.startswith(".")), [])   # 一時ファイルが残らない
        self.assertEqual(sorted(p.name for p in self.plist.parent.iterdir()), [self.plist.name])

    def test_install_keeps_existing_config(self):
        self.write_config(showGitIgnored=True, previewExternal=False, claudeDir="~/elsewhere",
                          launcherRoots=[{"path": "~/Work", "depth": 2}], somethingNew={"a": [1, 2]},
                          python="/no/such/python3", tailscale="/no/such/tailscale")
        self.assert_installed(self.install())
        config = self.read_config()
        self.assertEqual((config["apiPort"], config["filePort"]), (self.api_port, self.file_port))
        self.assertEqual((config["showGitIgnored"], config["previewExternal"], config["claudeDir"]),
                         (True, False, "~/elsewhere"))
        self.assertEqual(config["launcherRoots"], [{"path": "~/Work", "depth": 2}])
        self.assertEqual(config["somethingNew"], {"a": [1, 2]})                    # 知らない項目も残す
        self.assertEqual(config["tailscale"], str(self.tsbin / "tailscale"))      # 動かせなくなった値は、見つけたもので直す
        self.assertNotEqual(config["python"], "/no/such/python3")

    def test_a_configured_python_that_fails_the_check_is_replaced_on_install(self):
        # 入れ直しで同じ失敗を繰り返さない：使えない python は使わず、選び直した値で config.json を直す
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
        self.write_config(python=str(venv / "bin" / "python3"))
        self.assert_installed(self.install())
        chosen = self.read_config()["python"]
        self.assertNotEqual(chosen, str(venv / "bin" / "python3"))
        self.assertEqual(self.plist_arg(0), chosen)

    def test_flags_write_config(self):
        self.assert_installed(self.install("--show-gitignored", "--launcher-root", "/Volumes/x/Projects:2",
                                           "--launcher-root", "~/Notes:1"))
        config = self.read_config()
        self.assertTrue(config["showGitIgnored"])
        self.assertEqual(config["launcherRoots"], [{"path": "/Volumes/x/Projects", "depth": 2}, {"path": "~/Notes", "depth": 1}])

    def test_without_flags_the_launcher_roots_and_show_flag_stay(self):
        self.write_config(showGitIgnored=True, launcherRoots=[{"path": "~/Work", "depth": 1}])
        self.assert_installed(self.install())
        config = self.read_config()
        self.assertEqual((config["showGitIgnored"], config["launcherRoots"]), (True, [{"path": "~/Work", "depth": 1}]))

    def test_wrong_arguments_change_nothing(self):
        for args in (["--new-token"], ["--lab-dir", "/x"], ["--launcher-root"], ["--launcher-root", "relative:2"],
                     ["--launcher-root", "/x:3"], ["--launcher-root", "/x"], ["--check", "--show-gitignored"], ["extra"]):
            with self.subTest(args=args):
                r = self.install(*args)
                self.assertEqual(r.returncode, 64, r.stderr)
                self.assertFalse(self.plist.exists())
                self.assertFalse(self.calls.exists())
                self.assertFalse((self.support / "versions").exists())
        self.assertNotIn("new-token", self.install("--new-token").stderr)         # 使い方の文にも古い旗は残さない

    def test_install_fails_when_health_never_answers(self):
        (self.state / "run-server").unlink()                 # launchctl bootstrap は何も起こさない
        self.logs.mkdir(parents=True)
        (self.logs / "listserver.log").write_text("".join(f"old line {n}\n" for n in range(1, 16)), encoding="utf-8")
        t0 = time.monotonic()
        r = self.install(env={**self.env, "BRIDGEBOARD_HEALTH_WAIT": SHORT_WAIT})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertLess(time.monotonic() - t0, 30)
        self.assertIn("did not answer", r.stderr)
        for n in range(6, 16):                               # 記録の末尾の10行
            self.assertIn(f"old line {n}\n", r.stderr)
        self.assertNotIn("old line 5\n", r.stderr)
        self.assertNotIn("next:", r.stdout)                  # 組む案内は出さない

    def test_an_old_server_answering_on_the_port_does_not_count(self):
        """ポートを別のサーバーが握っていて、新しいサーバーが待ち受けられないのに、health だけ答える。入れたことにしない。"""
        class Old(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true, "apiVersion": 2}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        old = http.server.HTTPServer(("127.0.0.1", self.api_port), Old)
        threading.Thread(target=old.serve_forever, daemon=True).start()
        self.addCleanup(old.server_close)
        self.addCleanup(old.shutdown)
        r = self.install(env={**self.env, "BRIDGEBOARD_HEALTH_WAIT": "8"})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("Cannot listen on 127.0.0.1", r.stderr)    # 本当の理由が記録の末尾に出る

    def test_an_untrusted_or_broken_config_stops_the_install_before_anything_changes(self):
        real = self.tmp / "real.json"
        real.write_text('{"apiPort": 1}')
        os.chmod(real, 0o600)

        def make_link(p: Path) -> None:
            p.unlink()
            p.symlink_to(real)

        for label, prepare in (("writable by others", lambda p: os.chmod(p, 0o666)),
                               ("broken json", lambda p: p.write_text("{broken")),
                               ("not an object", lambda p: p.write_text("[]")),
                               ("a link", make_link)):
            with self.subTest(label):
                shutil.rmtree(self.support, ignore_errors=True)
                path = self.write_config()
                prepare(path)
                before = None if path.is_symlink() else path.read_bytes()
                r = self.install()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("config.json cannot be used", r.stderr)
                self.assertIn("were not changed", r.stderr)
                self.assertFalse(self.plist.exists())
                self.assertFalse((self.support / "current").exists())
                self.assertFalse((self.support / "versions" / VERSION).exists())
                self.assertEqual(sorted(p.name for p in self.support.iterdir() if p.name.startswith(".")), [])   # 一時ファイルを消した
                self.assertEqual(os.listdir(self.home / "Library" / "LaunchAgents"), [])
                if before is not None:
                    self.assertEqual(path.read_bytes(), before)
        self.assertEqual(real.read_text(), '{"apiPort": 1}')                       # リンクの先は書き換えない
        self.assertEqual([c for c in self.launchctl_calls() if c.startswith(("bootout", "bootstrap"))], [])

    def test_the_health_wait_is_in_seconds_not_in_tries(self):
        """待つのは秒の締め切り（1回の確かめに時間が掛かっても、15秒を大きく超えない）。"""
        (self.state / "run-server").unlink()
        (self.state / "ts-slow").touch()                  # 確かめるたびに 2 秒掛かる（回数で数えると 4 回 × 3 秒以上になる）
        t0 = time.monotonic()
        r = self.install(env={**self.env, "BRIDGEBOARD_HEALTH_WAIT": "4"})
        elapsed = time.monotonic() - t0
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertLess(elapsed, 11, f"締め切りの秒を、確かめの回数で数えている（{elapsed:.1f} 秒）")   # 秒の締め切りなら約7秒、回数なら約15秒

    def test_a_rotated_log_does_not_hide_the_new_servers_listening_line(self):
        """記録が 1 MB で回って、数えていた「Listening on」の行が新しいファイルに無くても、入れたことになる。"""
        self.logs.mkdir(parents=True)
        old = "".join(f"2026-01-01 00:00:0{n} INFO listserver Listening on 100.64.0.1: api port 8772, file port 8773 (0.0.1)\n"
                      for n in range(5))
        (self.logs / "listserver.log").write_text(old + "x" * 1_100_000 + "\n", encoding="utf-8")   # 次の1行で回る大きさ
        r = self.install(env={**self.env, "BRIDGEBOARD_HEALTH_WAIT": "20"})
        self.assert_installed(r)
        self.assertTrue((self.logs / "listserver.log.1").exists())                  # 本当に回った（試験の前提）
        self.assertLess((self.logs / "listserver.log").stat().st_size, 100_000)
        self.assertIn("Listening on 127.0.0.1", (self.logs / "listserver.log").read_text(encoding="utf-8"))

    def test_a_failing_bootstrap_exits_1(self):
        (self.state / "bootstrap-fails").touch()
        r = self.install()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Input/output error", r.stderr)                                # 最後の1回のエラーを見せる
        self.assertEqual(len([c for c in self.launchctl_calls() if c.startswith("bootstrap")]), 6)   # 5回＋最後の1回
        self.assertNotIn("next:", r.stdout)

    def test_an_old_server_that_does_not_stop_is_not_replaced(self):
        """古いサーバーが終わらないまま、その入っているフォルダを消さない。"""
        self.assert_installed(self.install())                                         # 入れてある
        marker = self.support / "versions" / VERSION / "marker.txt"
        marker.write_text("x")
        self.stop_server()                                                            # 本物のサーバーは止め、
        stuck = subprocess.Popen(["/usr/bin/python3", "-c", "import time; time.sleep(120)",
                                  f"{self.support}/current/listserver.py"])           # 同じ名前の道を引数に持つ、止まらない者を置く
        self.addCleanup(stuck.wait)
        self.addCleanup(stuck.kill)
        calls_before = len([c for c in self.launchctl_calls() if c.startswith("bootstrap")])
        r = self.install(env={**self.env, "BRIDGEBOARD_STOP_WAIT": "2"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not stop", r.stderr)
        self.assertTrue(marker.exists(), "止まっていないサーバーのフォルダを消した")
        self.assertEqual(len([c for c in self.launchctl_calls() if c.startswith("bootstrap")]), calls_before)   # 入れ直しはしていない
        self.assertEqual(sorted(p.name for p in self.support.iterdir() if p.name.startswith(".")), [])           # 一時ファイルは消えた

    def test_no_python_means_exit_3_and_nothing_is_installed(self):
        (self.state / "no-clt").touch()
        self.set_path(f"{self.oldpy}:{self.bin}:{self.system_path}")
        r = self.install()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("--check", r.stderr)
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.support / "versions").exists())

    def test_a_current_that_is_a_real_folder_is_not_touched(self):
        (self.support / "current").mkdir()
        (self.support / "current" / "mine.txt").write_text("x")
        r = self.install()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertTrue((self.support / "current" / "mine.txt").exists())
        self.assertFalse(self.plist.exists())

    def test_a_dangerous_folder_name_cannot_break_the_plist(self):
        """& や < を含む HOME でも plist は正しい XML になる。"""
        weird = self.tmp / "ho&me <x>"
        weird.mkdir()
        self.use_home(weird)
        self.write_config()
        self.assert_installed(self.install())
        lint = subprocess.run(["/usr/bin/plutil", "-lint", str(self.plist)], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)
        self.assertEqual(self.plist_arg(1), f"{self.support}/current/listserver.py")

    def test_no_token_file_and_no_secret_in_the_output(self):
        # 共通のトークンのファイルは作らない。置き場にある鍵の材料（secret・端末の鍵のハッシュ）は、出力のどこにも出ない
        secret = bytes(range(1, 33))
        (self.support / "secret").write_bytes(secret)
        os.chmod(self.support / "secret", 0o600)
        fake_hash = "d3adb33f" * 8
        (self.support / "devices.json").write_text(json.dumps({"devices": [{"id": "0123456789abcdef", "name": "iPad",
                                                                           "keyHash": fake_hash}]}), encoding="utf-8")
        os.chmod(self.support / "devices.json", 0o600)
        r = self.install()
        self.assert_installed(r)
        code, out, err = self.run_tty(INSTALL)               # ユーザーが自分の端末で走らせた回
        self.assertEqual(code, 0, err + out)
        for text in (r.stdout, r.stderr, out, err):
            for needle in (secret.hex(), fake_hash, "d3adb33f"):
                self.assertNotIn(needle, text)
            self.assertNotIn("token", text.lower())
            self.assertNotIn("code=", text)                  # 組む符号は install では作らない
        self.assertFalse((self.support / "token").exists())
        self.assertRegex(out.splitlines()[-1], r'^next: run ".*/current/bbctl" pair$')
        self.assertEqual((self.support / "secret").read_bytes(), secret)       # 既にある secret はそのまま使う

    def test_no_secret_in_the_failure_output(self):
        (self.state / "run-server").unlink()
        (self.support / "secret").write_bytes(bytes(range(1, 33)))
        r = self.install(env={**self.env, "BRIDGEBOARD_HEALTH_WAIT": SHORT_WAIT})
        self.assertEqual(r.returncode, 4)
        self.assertNotIn(bytes(range(1, 33)).hex(), r.stdout + r.stderr)


# ---------------------------------------------------------------- uninstall

class UninstallTest(InstallCase):
    def setUp(self):
        super().setUp()
        self.write_config()

    def installed(self) -> None:
        self.assert_installed(self.install())
        (self.support / "devices.json").write_text("{}", encoding="utf-8")        # 組んだ端末（があるつもり）
        self.assertTrue(self.plist.exists())

    def test_uninstall_unloads_and_keeps_the_data(self):
        self.installed()
        r = self.run_script(self.support / "current" / "uninstall.sh")              # 入れた先の写しを動かす
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.plist.exists())
        self.assertIn(f"bootout gui/{os.getuid()}/{LABEL}", self.launchctl_calls())
        self.assertTrue((self.support / "devices.json").exists())                  # 端末は残す（入れ直せばそのまま使える）
        self.assertTrue((self.support / "versions" / VERSION).is_dir())
        self.assertTrue((self.logs / "listserver.log").exists())
        self.assertIn("--purge", r.stdout)
        self.assertFalse((self.state / "pid").exists())                            # サーバーは止まった

    def test_uninstall_purge(self):
        self.installed()
        r = self.run_script(self.support / "current" / "uninstall.sh", "--purge")   # 自分の入っている置き場を消す
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.support.exists())                                    # devices.json も secret も
        self.assertFalse(self.logs.exists())
        self.assertIn("paired iPad", r.stdout)
        self.assertTrue((self.home / "Library" / "LaunchAgents").is_dir())         # ほかのものは触らない

    def test_purge_does_not_delete_under_a_server_that_does_not_stop(self):
        self.installed()
        self.stop_server()
        stuck = subprocess.Popen(["/usr/bin/python3", "-c", "import time; time.sleep(120)",
                                  f"{self.support}/current/listserver.py"])
        self.addCleanup(stuck.wait)
        self.addCleanup(stuck.kill)
        r = self.run_script(UNINSTALL, "--purge", env={**self.env, "BRIDGEBOARD_STOP_WAIT": "2"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not stop", r.stderr)
        self.assertIn("the data folder and the logs were not deleted", r.stderr)     # LaunchAgent の plist はもう消してある
        self.assertNotIn("nothing was deleted", r.stderr)
        self.assertTrue((self.support / "devices.json").exists())                  # 消していない
        self.assertTrue(self.logs.exists())

    def test_the_header_lists_every_exit_code(self):
        head = UNINSTALL.read_text(encoding="utf-8").split("set -euo pipefail")[0]
        self.assertRegex(head, r"Exit codes: 0 done · 1 [^·]+ · 64 wrong arguments")

    def test_uninstall_from_the_clone_works_too(self):
        self.installed()
        r = self.run_script(UNINSTALL, "--purge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.support.exists())

    def test_uninstall_with_nothing_installed_is_fine(self):
        r = self.run_script(UNINSTALL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.support.exists())                                     # setUp の config.json は触らない

    def test_uninstall_wrong_arguments(self):
        self.installed()
        for args in (["--all"], ["--purge", "x"], ["x"]):
            with self.subTest(args=args):
                r = self.run_script(UNINSTALL, *args)
                self.assertEqual(r.returncode, 64)
                self.assertTrue(self.plist.exists())                               # 何も外さない
        self.assertTrue(self.support.exists())


# ---------------------------------------------------------------- bblist/installtool.py

class InstallToolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_render_escapes_xml_and_quotes_for_the_shell(self):
        tpl = self.tmp / "t"
        out = self.tmp / "o"
        tpl.write_text("<a>@PYTHON@</a>\n", encoding="utf-8")
        installtool.render(tpl, out, "xml", {"PYTHON": "/p/a&b <c>/python3"})
        self.assertEqual(out.read_text(encoding="utf-8"), "<a>/p/a&amp;b &lt;c&gt;/python3</a>\n")
        tpl.write_text("printf %s @PYTHON@\n", encoding="utf-8")
        weird = "/my dir/it's $HOME/python3"
        installtool.render(tpl, out, "sh", {"PYTHON": weird})
        shell = subprocess.run(["/bin/sh", str(out)], capture_output=True, text=True)
        self.assertEqual(shell.stdout, weird)                  # 引用符で1語になり、展開もされない

    def test_render_refuses_a_placeholder_nobody_filled(self):
        tpl = self.tmp / "t"
        tpl.write_text("@PYTHON@ @SUPPORT@\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            installtool.render(tpl, self.tmp / "o", "xml", {"PYTHON": "x"})

    def test_launcher_root_must_be_absolute_or_tilde_with_depth_1_or_2(self):
        self.assertEqual(installtool._roots(["/a:2", "~/b:1"]), [{"path": "/a", "depth": 2}, {"path": "~/b", "depth": 1}])
        for bad in ("a:2", "/a", "/a:0", "/a:3", ":1", "~other/a:1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                installtool._roots([bad])


# ---------------------------------------------------------------- 準備のプロンプトと説明書

class DocsTest(unittest.TestCase):
    URL = "https://github.com/OneMoreLab-jp/bridgeboard-server"

    def text(self, rel: str) -> str:
        return (REPO / rel).read_text(encoding="utf-8")

    def test_prompts_pin_the_tag_of_this_version(self):
        # 版を上げたら、プロンプトのタグも上げる（取りに行くのは決まったタグだけ）
        for rel in ("setup/prompt.en.md", "setup/prompt.ja.md"):
            with self.subTest(rel=rel):
                t = self.text(rel)
                self.assertIn(f"--branch v{VERSION}", t)
                self.assertIn(f"{self.URL}.git", t)
                self.assertNotIn("--branch main", t)

    def test_prompts_have_the_steps_in_order_and_the_rules(self):
        en = self.text("setup/prompt.en.md")
        ja = self.text("setup/prompt.ja.md")
        for t in (en, ja):
            # 各コマンドは1つで完結する：変数や作業フォルダの持ち越しに頼らない（Bash ツールは持ち越さないことがある）
            check_m = re.search(r'^\s*cd "<[^"<>]+>" && \./install\.sh --check$', t, re.M)
            install_m = re.search(r'^\s*cd "<[^"<>]+>" && \./install\.sh$', t, re.M)          # オプションなしの1行
            check, install = check_m.start(), install_m.start()
            pair = t.index('/current/bbctl" pair')
            self.assertLess(t.index("git clone"), check)
            self.assertNotIn("$CLONE", t[check:])                # 変数は手順1の1行の中だけ
            self.assertEqual(len(re.findall(r"^\s*(?:\./install\.sh|cd \"\$CLONE\")", t, re.M)), 0)   # 裸の ./install.sh も、変数の cd も無い
            step1 = next(line for line in t.splitlines() if "git clone" in line)
            for part in ("mktemp -d", "git clone", "rev-parse HEAD", 'echo "$CLONE"'):
                self.assertIn(part, step1)                       # 取得・ID・絶対パスの表示が1つのコマンド
            self.assertEqual(step1.count("&&"), 3)
            self.assertLess(check, install)                  # 確かめてから入れる
            self.assertLess(install, pair)                   # 入れてから組む
            for word in ("sudo", "Application Support/BridgeBoard", "launchctl", "rev-parse HEAD", "Open it yourself"):
                self.assertIn(word, t)
        for phrase in ("verbatim", "Do not edit", "Do not read", "Do not open", "QR image", "five minutes", "fail"):
            self.assertIn(phrase, en)
        for phrase in ("そのまま", "書き換え", "読まない", "開かない", "QR", "5分", "fail"):
            self.assertIn(phrase, ja)

    def test_readmes_cover_every_config_key_and_the_diagnostics_fields(self):
        keys = [k for k, _ in installtool.DEFAULTS]
        fields = ("unknownTypes", "unknownTypeNames", "unknownOriginKinds")
        for rel in ("README.md", "README.ja.md"):
            t = self.text(rel)
            for word in (*keys, *fields, "install.sh --check", "bbctl", "uninstall.sh", "--purge", "MIT", "Anthropic",
                         "Documents", "iCloud", "Tailscale", "versions", "/v1/diagnostics"):
                with self.subTest(rel=rel, word=word):
                    self.assertIn(word, t)
            self.assertNotIn("skippedTyped", t)
            self.assertNotIn("work in progress", t.lower())
            self.assertNotIn("作成中", t)

    def test_readmes_describe_the_flags_the_preview_setting_and_the_commands_exactly(self):
        for rel, replaces, closed, only_without in (("README.md", "**replaces**", "resource hints are not verified", "without the option"),
                                                    ("README.ja.md", "置き換えます", "資源の予告は確かめていません", "付けなければ")):
            t = self.text(rel)
            with self.subTest(rel=rel):
                self.assertIn(replaces, t)                       # --launcher-root は既存の launcherRoots を置き換える
                self.assertIn(only_without, t)
                self.assertIn(closed, t)                         # previewExternal: false は外への読み込みを止めるが、資源の予告は未確認
                self.assertNotIn("this path is closed", t)       # 確かめていないことを「閉じる」と言い切らない
                self.assertNotIn("この道は閉じます", t)
                self.assertIn('"$HOME/Library/Application Support/BridgeBoard/current/bbctl" devices', t)
                self.assertIn('"$HOME/Library/Application Support/BridgeBoard/current/bbctl" revoke', t)
                self.assertIsNone(re.search(r"^bbctl (devices|revoke)", t, re.M))      # PATH に無い bbctl を、裸で書かない
                self.assertIn("Command Line Tools" if rel == "README.md" else "コマンドラインツール", t)

    def test_the_docs_ask_for_magicdns_and_describe_the_new_checks(self):
        for rel in ("README.md", "README.ja.md", "setup/prompt.en.md", "setup/prompt.ja.md"):
            with self.subTest(rel=rel):
                self.assertIn("MagicDNS", self.text(rel))                # 準備のプロンプトにも「MagicDNS が有効」
        for rel in ("README.md", "README.ja.md"):
            t = self.text(rel)
            with self.subTest(rel=rel):
                self.assertIn("`magicdns`", t)                           # --check の項目の表
                self.assertIn("`replaced", t)                            # 別の pair が始まったときの出力 `replaced: ...`
                self.assertIn("PATH=/usr/bin:/bin:/usr/sbin:/sbin", t)   # Python の確かめは launchd に近い環境
        for rel in ("setup/prompt.en.md", "setup/prompt.ja.md"):
            with self.subTest(rel=rel):
                self.assertIn("`replaced", self.text(rel))

    def test_the_docs_describe_the_equal_ports_the_usage_file_and_the_remaining_paths_as_implemented(self):
        en, ja = self.text("README.md"), self.text("README.ja.md")
        for t in (en, ja):
            self.assertIn("`HEAD`", t)                       # GET だけ（例外 POST /v1/pair）。HEAD も 405
        # apiPort と filePort が config.json で同じなら、記録して両方を既定に戻して起動する（終了コード 2 になるのは旗で同じにしたとき）
        self.assertIn("uses 8772 and 8773", en)
        self.assertIn("8772 と 8773 に戻", ja)
        self.assertNotIn("the server logs an error and exits with status 2 if", en)
        for t in (en, ja):
            self.assertIn("--api-port", t)
        # usage.json を作るのは、このリポジトリの外のもの
        self.assertIn("outside this repository", en)
        self.assertIn("このリポジトリの外", ja)
        self.assertNotIn("if you put one in the data folder", en)
        # 「.js・.css の中身」ではなく、script が出す値と CSS のカスタムプロパティの値
        for t, gone in ((en, "contents of the `.js` and `.css` files"), (ja, "`.js`・`.css` の中身")):
            self.assertNotIn(gone, t)
        self.assertIn("custom propert", en)
        self.assertIn("カスタムプロパティの値", ja)

    def test_nothing_in_the_docs_points_at_private_places(self):
        for rel in ("README.md", "README.ja.md", "setup/prompt.en.md", "setup/prompt.ja.md"):
            t = self.text(rel)
            for word in ("docs/decisions", "Ruling", "BACKLOG", "/Users/", "I-19"):
                self.assertNotIn(word, t, rel)


if __name__ == "__main__":
    unittest.main()
