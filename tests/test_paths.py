"""bblist/paths.py の試験。python3 -m unittest discover -s tests -t . -p 'test_paths.py' -v

NFD の名前は、ディスクに本当に NFD で作って試す（APFS は書いた形のまま残す）。
「テスト」は NFC と NFD が同じ文字列なので使えない。濁点の付く名前を使う。
"""
from __future__ import annotations

import os
import tempfile
import unicodedata
import unittest
from pathlib import Path

from bblist.paths import (
    TREE_SKIP, deny_stamp, files_visible, folder_id as _folder_id, is_denied, nfc, preview_kind, rel_below, resolve_in_root,
)

SECRET = bytes(range(32))


def folder_id(path, secret: bytes = SECRET) -> str:
    return _folder_id(path, secret)


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FolderIdTest(unittest.TestCase):
    def test_nfc_and_nfd_paths_give_same_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = "デバイス"
            self.assertNotEqual(nfd(name), nfc(name))          # 試験の前提：形が本当に違う
            nfd_path = Path(tmp) / nfd(name)
            nfd_path.mkdir()
            self.assertIn(nfd(name), os.listdir(tmp))           # ディスク上が NFD で残っている
            nfc_path = Path(tmp) / nfc(name)
            self.assertTrue(os.path.samefile(nfd_path, nfc_path))
            self.assertEqual(folder_id(nfd_path), folder_id(nfc_path))
            self.assertEqual(folder_id(str(nfd_path)), folder_id(nfc_path))
            self.assertEqual(len(folder_id(nfd_path)), 16)
            int(folder_id(nfd_path), 16)                        # 16進

    def test_id_is_sha256_of_secret_root_prefix_and_nfc_real_path(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.realpath(tmp)
            want = hashlib.sha256(SECRET + b"root:" + nfc(real).encode("utf-8")).hexdigest()[:16]
            self.assertEqual(folder_id(tmp), want)

    def test_root_id_salted(self):
        # 同じパスでも secret が違えば rootId が違う（パスが分かっても rootId を計算できない）
        with tempfile.TemporaryDirectory() as tmp:
            a, b = folder_id(tmp, bytes(32)), folder_id(tmp, b"\x01" * 32)
            self.assertNotEqual(a, b)
            self.assertEqual(a, folder_id(tmp, bytes(32)))             # 同じ secret なら安定
            import hashlib
            self.assertNotEqual(a, hashlib.sha256(nfc(os.path.realpath(tmp)).encode("utf-8")).hexdigest()[:16])   # 塩なしの旧い形とも違う

    def test_different_folders_give_different_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            a.mkdir()
            b.mkdir()
            self.assertNotEqual(folder_id(a), folder_id(b))

    def test_symlink_gives_target_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            self.assertEqual(folder_id(link), folder_id(real))


class FilesVisibleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.own = [self.home / "Library/Application Support/BridgeBoard",
                    self.home / "Library/Logs/BridgeBoard"]

    def test_rules(self):
        home = self.home
        cases = {
            home: False, home.parent: False, Path("/"): False,
            home / ".claude": False, home / ".claude/projects": False,
            home / "Library": False, home / "Library/Application Support/x": False,
            home / "Library/Mobile Documents": False,
            home / "Library/Mobile Documents/com~apple~CloudDocs": False,
            home / "Library/Mobile Documents/com~apple~CloudDocs/本": True,
            home / "Projects": True,
            home / "Projects/30_devices/MyApp": True,
            home / "Documents/x": True,
        }
        for root, want in cases.items():
            self.assertEqual(files_visible(root, home, self.own), want, root)

    def test_compares_by_component_not_prefix(self):
        self.assertIs(files_visible(self.home / ".claudette", self.home, self.own), False)  # . で始まる起点は false でよい
        self.assertTrue(files_visible(self.home / "Library2/x", self.home, self.own))

    def test_dot_component_below_home_is_denied(self):
        # 「. で始まる名前は出さない」を起点にも当てる（途中のどの階層でも）。
        # trusted_base を渡さない起点（worktree でないもの）は今までどおり。worktree の緩め方は下の試験
        self.assertIs(files_visible(self.home / "Projects/x/.worktrees/y", self.home, self.own), False)
        self.assertTrue(files_visible(self.home / "Projects/x/worktrees/y", self.home, self.own))

    def test_worktree_inside_visible_parent_skips_dot_rule_below_the_parent(self):
        # 見せてよい親のプロジェクトの中の worktree は、親より下の . で始まる名前では見せなくしない
        repo = self.home / "Projects/x/Repo"
        for rel in (".worktrees/y", ".claude/worktrees/y", "a/.b/y"):
            self.assertTrue(files_visible(repo / rel, self.home, self.own, trusted_base=repo), rel)
            self.assertIs(files_visible(repo / rel, self.home, self.own), False, rel)   # 親を渡さなければ今までどおり
        icloud = self.home / "Library/Mobile Documents/com~apple~CloudDocs/Repo"     # 見せてよい ~/Library の中の親
        self.assertTrue(files_visible(icloud / ".worktrees/y", self.home, self.own, trusted_base=icloud))
        outside = Path(self.tmp.name) / "ext/Repo"                                   # ホームの外の親は、親ごと見せない
        self.assertIs(files_visible(outside / ".worktrees/y", self.home, self.own, trusted_base=outside), False)

    def test_worktree_outside_its_parent_keeps_the_dot_rule(self):
        # 親の外にある worktree は緩めない（~/.config/x、親の隣の . フォルダ、~/.claude の中）
        repo = self.home / "Projects/x/Repo"
        for root in (self.home / ".config/y", self.home / "Projects/x/.worktrees/Repo-y",
                     self.home / ".claude/worktrees/y", Path(self.tmp.name) / "ext/.wt/y"):
            self.assertIs(files_visible(root, self.home, self.own, trusted_base=repo), False, root)

    def test_worktree_of_an_invisible_parent_stays_hidden(self):
        # 親が見せないフォルダなら緩めない：ホームそのもの（dotfiles のリポジトリ）・~/Library の中・. の下・自分の置き場
        tmp = Path(self.tmp.name)
        own_repo = self.home / "Documents/own-repo"
        cases = [
            (self.home, self.home / ".wt/y"),
            (self.home / "Library/Application Support/Repo", self.home / "Library/Application Support/Repo/.worktrees/y"),
            (tmp / "ext/.hidden/Repo", tmp / "ext/.hidden/Repo/.worktrees/y"),
            (own_repo, own_repo / ".worktrees/y"),
        ]
        own = self.own + [own_repo / "data"]                                          # 親が自分の置き場を含む
        for base, root in cases:
            self.assertIs(files_visible(root, self.home, own, trusted_base=base), False, root)

    def test_worktree_parent_compared_after_nfc(self):
        # 親と worktree の綴りが NFC と NFD で違っても、同じフォルダの中として扱う
        name = "デバイス"
        self.assertNotEqual(nfd(name), nfc(name))
        wt = self.home / nfd(name) / "Repo" / ".worktrees" / "y"
        wt.mkdir(parents=True)
        self.assertTrue(files_visible(wt, self.home, self.own, trusted_base=self.home / nfc(name) / "Repo"))

    def test_dot_component_outside_home_is_denied(self):
        outside = Path(self.tmp.name) / "ext/.hidden/proj"      # ホームの外
        self.assertIs(files_visible(outside, self.home, self.own), False)

    def test_own_dirs_are_denied_where_they_live(self):
        # ~/Library の外にあっても、自分の置き場を含む・その中のフォルダは見せない
        own = [self.home / "Documents/own-data"]
        self.assertIs(files_visible(self.home / "Documents", self.home, own), False)
        self.assertIs(files_visible(self.home / "Documents/own-data", self.home, own), False)
        self.assertIs(files_visible(self.home / "Documents/own-data/sub", self.home, own), False)
        self.assertTrue(files_visible(self.home / "Documents/own-data2", self.home, own))
        self.assertTrue(files_visible(self.home / "Documents/other", self.home, own))

    def test_symlink_to_claude_dir_is_denied(self):
        (self.home / ".claude/projects").mkdir(parents=True)
        (self.home / "Documents").mkdir()
        os.symlink(self.home / ".claude", self.home / "Documents/innocent")
        self.assertIs(files_visible(self.home / "Documents/innocent", self.home, self.own), False)
        self.assertIs(files_visible(self.home / "Documents/innocent/projects", self.home, self.own), False)

    def test_symlinked_home_is_resolved(self):
        # home を symlink 経由で渡しても、実体で比べる
        (self.home / ".claude").mkdir()
        alias = Path(self.tmp.name) / "alias"
        os.symlink(self.home, alias)
        self.assertIs(files_visible(self.home / ".claude", alias, self.own), False)
        self.assertIs(files_visible(alias / "Library/x", self.home, self.own), False)
        self.assertTrue(files_visible(alias / "Documents/x", self.home, self.own))

    def test_nfd_spelling_is_compared_after_nfc(self):
        # ディスク上に NFD の名前で作り、NFC・NFD どちらの綴りで渡しても同じ答え
        name = "デバイス"
        self.assertNotEqual(nfd(name), nfc(name))
        nfd_root = self.home / nfd(name) / "proj"
        nfd_root.mkdir(parents=True)
        self.assertTrue(files_visible(nfd_root, self.home, self.own))
        self.assertTrue(files_visible(self.home / nfc(name) / "proj", self.home, self.own))
        # 自分の置き場の綴りが違っても同じ（NFD で作った own を NFC で聞く）
        own = [self.home / nfd(name) / "own"]
        (self.home / nfd(name) / "own").mkdir()
        self.assertIs(files_visible(self.home / nfc(name), self.home, own), False)


    # ---- 見せてよい起点が広がらないようにする（パスの実体 (st_dev, st_ino) で比べる）

    def test_claude_dir_symlink_hidden(self):
        # ~/.claude が ~/dotfiles/claude へのリンクだと、リンクの名前だけでは守れない。実体とその上も見せない
        dotfiles = self.home / "dotfiles"
        (dotfiles / "claude/projects").mkdir(parents=True)
        (dotfiles / "zsh").mkdir()
        os.symlink(dotfiles / "claude", self.home / ".claude")
        for root in (dotfiles, dotfiles / "claude", dotfiles / "claude/projects",
                     self.home / ".claude", self.home / ".claude/projects"):
            self.assertIs(files_visible(root, self.home, self.own), False, root)
        self.assertTrue(files_visible(dotfiles / "zsh", self.home, self.own))        # 隣は見せてよい
        self.assertTrue(files_visible(self.home / "dotfiles2/x", self.home, self.own))   # 区切りごとに比べる（前方一致ではない）

    def test_claude_config_dir_hidden(self):
        # CLAUDE_CONFIG_DIR で ~/conf/claude を使うとき（. の無い場所）。listserver は実体を own_dirs に入れる
        conf = self.home / "conf/claude"
        (conf / "projects").mkdir(parents=True)
        (self.home / "conf/other").mkdir()
        own = self.own + [conf]
        for root in (conf, conf / "projects", conf.parent):
            self.assertIs(files_visible(root, self.home, own), False, root)
        self.assertTrue(files_visible(self.home / "conf/other", self.home, own))
        self.assertTrue(files_visible(self.home / "conf/other", self.home, self.own))   # 入れなければ見える（対照）
        link = self.home / "conf-link"                                                  # リンク越しに渡しても同じ
        os.symlink(conf, link)
        self.assertIs(files_visible(conf / "projects", self.home, self.own + [link]), False)
        gone = self.home / "nope/claude"                                                # まだ無い場所でも、綴りで守る
        self.assertIs(files_visible(gone / "projects", self.home, self.own + [gone]), False)

    def test_library_symlink_hides_its_real_folder_and_the_folder_above(self):
        # ~/Library が ~/stuff/Library へのリンクなら、実体と、それを含む ~/stuff も見せない。
        # 置き場（~/Library/Application Support/BridgeBoard）が実体の下にあると own_dirs でも止まるので、own_dirs を空にして ~/Library の決まりだけを見る
        stuff = self.home / "stuff"
        (stuff / "Library/Keychains").mkdir(parents=True)
        (stuff / "Library/Mobile Documents/com~apple~CloudDocs/本").mkdir(parents=True)
        (stuff / "other").mkdir()
        os.symlink(stuff / "Library", self.home / "Library")
        for own in ([], self.own):
            for root in (stuff, stuff / "Library", stuff / "Library/Keychains", self.home / "Library",
                         self.home / "Library/Keychains"):
                self.assertIs(files_visible(root, self.home, own), False, root)
            self.assertTrue(files_visible(stuff / "other", self.home, own))
            self.assertTrue(files_visible(stuff / "Library/Mobile Documents/com~apple~CloudDocs/本", self.home, own))   # iCloud は例外のまま

    def test_deny_stamp_changes_when_a_forbidden_folder_is_moved_and_linked_back(self):
        # mv では inode が変わらない。stat の印は同じでも、リンクそのものの印（lstat）が変わる
        (self.home / ".claude").mkdir()
        (self.home / "dotfiles").mkdir()
        own = self.own + [self.home / "data"]
        (self.home / "data").mkdir()
        before = deny_stamp(self.home, own)
        self.assertEqual(deny_stamp(self.home, own), before)                     # 何も変えなければ同じ
        os.rename(self.home / ".claude", self.home / "dotfiles/claude")
        os.symlink(self.home / "dotfiles/claude", self.home / ".claude")
        after = deny_stamp(self.home, own)
        self.assertNotEqual(after, before)
        os.rename(self.home / "data", self.home / "dotfiles/data")               # 置き場でも同じ
        os.symlink(self.home / "dotfiles/data", self.home / "data")
        self.assertNotEqual(deny_stamp(self.home, own), after)

    def _aliases(self, real_home: Path) -> list:
        """real_home の別名の綴り：全部大文字・/System/Volumes/Data 経由（firmlink）。このボリュームで通じるものだけ。"""
        out = []
        upper = Path(str(real_home).upper())
        if upper != real_home and os.path.exists(upper) and os.path.samefile(upper, real_home):
            out.append(("case", upper))
        data = Path("/System/Volumes/Data")
        firm = data / str(real_home).lstrip("/")
        if os.path.isdir(data) and os.path.exists(firm) and os.path.samefile(firm, real_home):
            out.append(("firmlink", firm))
        return out

    def test_case_and_firmlink_aliases_hidden(self):
        # APFS は大文字小文字を区別せずに引く。/USERS/<名前>・/System/Volumes/Data/Users/<名前> も同じ場所（実体で比べる）
        real_home = Path(os.path.realpath(self.home))
        aliases = self._aliases(real_home)
        if not any(kind == "case" for kind, _ in aliases):
            self.skipTest("大文字小文字を区別しないボリュームではない")
        own = self.own + [real_home / "Documents/own-data"]
        for rel in (".claude/projects", "Library/x", "Library/Application Support/BridgeBoard", "Documents/own-data/sub"):
            (real_home / rel).mkdir(parents=True, exist_ok=True)
        (real_home / "Projects/p").mkdir(parents=True)
        for rel in ("LIBRARY/x", "library/application support/bridgeboard", "DOCUMENTS/OWN-DATA/sub"):   # ホームの中だけ綴りが違う
            self.assertIs(files_visible(real_home / rel, self.home, own), False, rel)
        self.assertTrue(files_visible(real_home / "PROJECTS/p", self.home, own))                  # 対照
        for kind, alias in aliases:
            for rel in ("", ".claude", ".claude/projects", "Library", "Library/x",
                        "Library/Application Support/BridgeBoard", "Documents/own-data", "Documents/own-data/sub"):
                self.assertIs(files_visible(alias / rel, self.home, own), False, (kind, rel))
            self.assertIs(files_visible(alias.parent, self.home, own), False, kind)       # ホームを含むフォルダ
            self.assertTrue(files_visible(alias / "Projects/p", self.home, own), kind)    # 対照：別名でも普通の起点は見える
            # ホームの方を別名で渡しても同じ
            self.assertIs(files_visible(real_home / "Library/x", alias, own), False, kind)
            self.assertTrue(files_visible(real_home / "Projects/p", alias, own), kind)

    def test_outside_home_hidden_by_default(self):
        for root in (Path("/tmp/x"), Path("/Volumes/x"), Path("/Volumes"), Path("/opt/proj"),
                     Path(self.tmp.name) / "ext/proj",          # ホームの隣
                     self.home.parent / (self.home.name + "2") / "x"):   # 名前の頭が同じ別のフォルダ
            self.assertIs(files_visible(root, self.home, self.own), False, root)
        self.assertTrue(files_visible(self.home / "Projects/x", self.home, self.own))      # 対照


class IsDeniedTest(unittest.TestCase):
    def test_denied_names(self):
        for n in [".env", ".git", ".claude", ".DS_Store", "credentials.json", "credentials.dat", "id_rsa", "id_rsa.pub",
                  "id_ed25519", "id_ed25519.pub", "secrets.txt", "secrets", "secret.json", "k.pem", "a.p12", "a.pfx",
                  "a.keystore", "a.jks", "a.ppk", "x.sqlite", "x.db", "a.bak-20260101", "x.env.local",
                  "prod.env"]:
            self.assertTrue(is_denied(n), n)

    def test_case_insensitive(self):
        for n in ["CREDENTIALS.JSON", "CREDENTIALS.DAT", "ID_RSA", "Secrets.md", "SECRET.JSON", "K.PEM", "X.DB",
                  "A.BAK-1", "X.ENV", ".DS_STORE"]:
            self.assertTrue(is_denied(n), n)

    def test_casefold_not_lower(self):
        # APFS は大文字小文字を区別せずに引く。ſ（U+017F）は lower() では ſ のままだが、
        # casefold() では s になる。この綴りで拒む名前をすり抜けさせない
        for n in ["ſecrets.txt", "ſecret.json", "credentialſ.json", "credentialſ.dat", "id_rſa", "x.ſqlite"]:
            self.assertTrue(is_denied(n), n)

    def test_ordinary_names_pass(self):
        for n in ["README.md", "a.md", "docs", "index.html", "photo.png", "dbfile.txt", "environment.md",
                  "node_modules", "デバイス・ツール", "credentials.md", "my_id_rsa", "secret", "secretary.md"]:
            self.assertFalse(is_denied(n), n)

    def test_more_secret_names_denied(self):
        for n in ["AuthKey_ABC123.p8", "SubscriptionKey_ABC123.p8", "x.p8", "id_ecdsa", "id_ecdsa.pub", "id_dsa",
                  "id_dsa.pub", "credentials", "master.key", "a.kdbx", "login.keychain-db", "x.tfstate", "x.tfvars",
                  "prod.auto.tfvars", "terraform.tfvars.json", "prod.auto.tfvars.json", "terraform.tfstate.backup",
                  "embedded.mobileprovision", "embedded.provisionprofile", "kubeconfig", "service-account.json",
                  "service-account-prod.json", "keystore.properties"]:
            self.assertTrue(is_denied(n), n)

    def test_more_secret_names_are_case_insensitive(self):
        for n in ["AUTHKEY_ABC123.P8", "SUBSCRIPTIONKEY_ABC.P8", "ID_ECDSA", "Credentials", "MASTER.KEY",
                  "Login.Keychain-DB", "KUBECONFIG", "Service-Account-Prod.JSON", "Keystore.Properties",
                  "TERRAFORM.TFVARS.JSON", "A.PROVISIONPROFILE", "ſervice-account.json", "maſter.key"]:
            self.assertTrue(is_denied(n), n)

    def test_keynote_key_not_denied(self):
        # .key は Keynote の拡張子。丸ごとは拒まず、master.key のような完全一致だけ拒む
        for n in ["参考フォーマット.key", "slides.key", "SLIDES.KEY", "master.keynote", "my-master.key", "master.key.md"]:
            self.assertFalse(is_denied(n), n)

    def test_similar_looking_names_pass(self):
        for n in ["AuthKey.md", "AuthKey_notes.txt", "p8.md", "credentials.md", "credentials-guide.md", "my_id_dsa",
                  "kubeconfig.md", "service-accounts.md", "service-account.md", "keystore.md", "tfstate.md", "tfvars"]:
            self.assertFalse(is_denied(n), n)

    def test_tree_skip_is_not_denied(self):
        # TREE_SKIP は「ツリーに出さない」だけ。中身は返すので、拒む名前とは別物
        self.assertEqual(TREE_SKIP, frozenset({"node_modules", "__pycache__", "build", "DerivedData"}))
        for n in TREE_SKIP:
            self.assertFalse(is_denied(n), n)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "root"
        self.outside = base / "outside"
        write(self.root / "docs/a.md")
        write(self.outside / "x")
        os.symlink(self.outside, self.root / "link_to_outside")
        os.symlink(self.root / "docs", self.root / "link_to_docs")
        self.root_real = Path(os.path.realpath(self.root))

    def test_inside(self):
        self.assertEqual(resolve_in_root(self.root, "docs/a.md"), self.root_real / "docs/a.md")

    def test_root_itself(self):
        self.assertEqual(resolve_in_root(self.root, ""), self.root_real)
        self.assertEqual(resolve_in_root(self.root, "/"), self.root_real)

    def test_dotdot(self):
        self.assertIsNone(resolve_in_root(self.root, "../x"))
        self.assertIsNone(resolve_in_root(self.root, "docs/../../outside/x"))

    def test_absolute_rel_stays_inside(self):
        # 先頭の / で root から外れない（root の下の etc/passwd として扱われ、外は指さない）
        got = resolve_in_root(self.root, "/etc/passwd")
        self.assertEqual(got, self.root_real / "etc/passwd")

    def test_nul_byte(self):
        self.assertIsNone(resolve_in_root(self.root, "docs/a.md\0.png"))

    def test_symlink_escape(self):
        self.assertIsNone(resolve_in_root(self.root, "link_to_outside/x"))
        self.assertIsNone(resolve_in_root(self.root, "link_to_outside"))

    def test_symlink_inside_ok(self):
        self.assertEqual(resolve_in_root(self.root, "link_to_docs/a.md"), self.root_real / "docs/a.md")

    def test_symlink_to_denied_name_inside_root(self):
        # 名前は無害でも、行き先が拒む名前なら返さない
        write(self.root / ".env", "SECRET=1")
        write(self.root / ".git/config")
        os.symlink(self.root / ".env", self.root / "innocent.txt")
        os.symlink(self.root / ".git", self.root / "innocent_dir")
        self.assertIsNone(resolve_in_root(self.root, "innocent.txt"))
        self.assertIsNone(resolve_in_root(self.root, "innocent_dir/config"))

    def test_denied_names(self):
        rels = [".env", ".git/config", "a/.hidden/b.png", "credentials.json", "k.pem", "id_rsa", "id_ed25519.pub",
                "secrets.txt", "x.sqlite", "x.db", "a.bak-20260101", "x.env.local", "y.p12", "z.keystore",
                "credentials.dat", "secret.json"]
        for rel in rels:
            write(self.root / rel)                              # 実在させる。拒む理由が名前だけになるように
        write(self.root / "a/ok.png")
        self.assertIsNotNone(resolve_in_root(self.root, "a/ok.png"))   # 対照：同じ階層の普通の名前は通る
        for rel in rels:
            self.assertIsNone(resolve_in_root(self.root, rel), rel)

    def test_more_secret_names_are_refused_but_keynote_is_not(self):
        secret = ["AuthKey_ABC123.p8", "SubscriptionKey_ABC123.p8", "id_ecdsa", "credentials", "master.key", "a.kdbx",
                  "x.tfstate", "terraform.tfvars.json", "a.provisionprofile", "kubeconfig",
                  "service-account-prod.json", "keystore.properties"]
        for rel in secret:
            write(self.root / "cfg" / rel)
        write(self.root / "参考フォーマット.key")
        for rel in secret:
            self.assertIsNone(resolve_in_root(self.root, f"cfg/{rel}"), rel)
        self.assertIsNotNone(resolve_in_root(self.root, "参考フォーマット.key"))

    def test_denied_name_is_case_insensitive(self):
        write(self.root / "Credentials.JSON")
        self.assertIsNone(resolve_in_root(self.root, "Credentials.JSON"))

    def test_denied_name_spelled_with_casefold_alias(self):
        # APFS は ſ（U+017F）で書いても s のファイルを引く。実在の拒む名前を、別綴りで取りに来ても返さない
        pairs = {"secrets.txt": "ſecrets.txt", "secret.json": "ſecret.json", "credentials.json": "credentialſ.json",
                 "credentials.dat": "credentialſ.dat", "id_rsa": "id_rſa", "x.sqlite": "x.ſqlite"}
        for real_name, alias in pairs.items():
            write(self.root / real_name, "SECRET")
            if not os.path.exists(self.root / alias):
                self.skipTest("このファイルシステムは大文字小文字を区別して引くので、この試験は意味がない")
            self.assertIsNone(resolve_in_root(self.root, alias), alias)
            self.assertIsNone(resolve_in_root(self.root, real_name), real_name)

    def test_root_under_dot_directory_is_not_a_denial(self):
        # 拒むのは root の下の名前だけ。root そのものの綴りは files_visible の担当
        root = Path(self.tmp.name) / ".hidden_parent" / "proj"
        write(root / "a.md")
        self.assertIsNotNone(resolve_in_root(root, "a.md"))

    def test_nfc_request_for_nfd_file(self):
        folder, fname = "資料", "デバッグ.md"
        self.assertNotEqual(nfd(fname), nfc(fname))
        write(self.root / nfd(folder) / nfd(fname))
        self.assertIn(nfd(fname), os.listdir(self.root / nfd(folder)))   # ディスク上は NFD
        got = resolve_in_root(self.root, nfc(f"{folder}/{fname}"))
        self.assertIsNotNone(got)
        self.assertTrue(os.path.samefile(got, self.root / nfd(folder) / nfd(fname)))

    def test_nfd_request_for_nfc_file(self):   # 逆向き
        fname = "デバッグ.md"
        write(self.root / "docs" / nfc(fname))
        self.assertIn(nfc(fname), os.listdir(self.root / "docs"))        # ディスク上は NFC
        got = resolve_in_root(self.root, "docs/" + nfd(fname))
        self.assertIsNotNone(got)
        self.assertTrue(os.path.samefile(got, self.root / "docs" / nfc(fname)))

    def test_root_given_in_other_normalization(self):
        # ディスク上の root の名前は NFD、渡す root は NFC
        name = "デバイス"
        nfd_root = Path(self.tmp.name) / nfd(name)
        write(nfd_root / "docs/a.md")
        got = resolve_in_root(Path(self.tmp.name) / nfc(name), "docs/a.md")
        self.assertIsNotNone(got)
        self.assertTrue(os.path.samefile(got, nfd_root / "docs/a.md"))

    def test_symlink_target_spelled_in_other_normalization(self):
        # root は NFC の綴りで渡し、中の symlink の行き先が NFD の綴り。NFC にそろえて比べないと外に見える
        name = "デバイス"
        nfd_root = Path(self.tmp.name) / nfd(name)
        write(nfd_root / "docs/a.md")
        os.symlink(str(nfd_root / "docs"), nfd_root / "link_to_docs")   # 行き先は NFD の絶対パス
        got = resolve_in_root(Path(self.tmp.name) / nfc(name), "link_to_docs/a.md")
        self.assertIsNotNone(got)
        self.assertTrue(os.path.samefile(got, nfd_root / "docs/a.md"))


class PreviewKindTest(unittest.TestCase):
    def test_kinds(self):
        for n, k in {"a.md": "md", "a.HTML": "html", "a.htm": "html", "a.png": "image", "a.jpeg": "image",
                     "a.jpg": "image", "a.svg": "image", "a.webp": "image", "a.gif": "image", "a.json": None,
                     "a": None, "a.md.txt": None}.items():
            self.assertEqual(preview_kind(n), k, n)


class NfcTest(unittest.TestCase):
    def test_nfc(self):
        self.assertEqual(nfc(nfd("デバッグ")), "デバッグ")
        self.assertNotEqual(nfd("デバッグ"), "デバッグ")
        self.assertEqual(nfc("abc"), "abc")


class RelBelowTest(unittest.TestCase):
    """realpath にした root の下の、実体のパスの相対パス（git の判定に渡す形）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.root = self.base / "root"
        write(self.root / "docs/a.md")

    def test_relative_path_in_the_spelling_on_disk(self):
        self.assertEqual(rel_below(self.root, resolve_in_root(self.root, "docs/a.md")), "docs/a.md")
        self.assertEqual(rel_below(self.root, resolve_in_root(self.root, "docs")), "docs")
        self.assertEqual(rel_below(self.root, resolve_in_root(self.root, "")), "")           # root そのもの

    def test_symlink_inside_root_gives_the_real_path(self):
        os.symlink(self.root / "docs", self.root / "link")
        self.assertEqual(rel_below(self.root, resolve_in_root(self.root, "link/a.md")), "docs/a.md")

    def test_root_reached_through_a_symlink(self):
        os.symlink(self.root, self.base / "alias")
        self.assertEqual(rel_below(self.base / "alias", resolve_in_root(self.base / "alias", "docs/a.md")), "docs/a.md")

    def test_root_and_target_spelled_in_different_normalization_forms(self):
        name = "プロジェクト"
        self.assertNotEqual(nfd(name), nfc(name))
        write(self.base / nfd(name) / "a.md")
        # root は NFC、target は NFD の綴り：区切りごとに NFC にそろえて比べ、返すのは target の綴り
        self.assertEqual(rel_below(self.base / nfc(name), self.base / nfd(name) / "a.md"), "a.md")
        self.assertEqual(rel_below(self.base / nfd(name), self.base / nfc(name) / nfd("ぱ.md")), nfd("ぱ.md"))
        got = rel_below(self.root, resolve_in_root(self.root, nfd("データ") + "/" + nfd("ぱ.md")))
        self.assertEqual(got, nfd("データ") + "/" + nfd("ぱ.md"))        # target の綴りのまま

    def test_outside_root_is_none(self):
        write(self.base / "other/x.md")
        self.assertIsNone(rel_below(self.root, self.base / "other" / "x.md"))
        self.assertIsNone(rel_below(self.root, self.base))


if __name__ == "__main__":
    unittest.main()
