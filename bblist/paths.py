"""フォルダの鍵・ファイルを見せてよい範囲・フォルダの外へ出ない安全なパス。

Python 3.9 の標準ライブラリだけで動かす。

フォルダ名を文字列のまま比べない（フォルダ名は NFC/NFD が混在していることがある）。
比べるのは realpath を NFC にそろえた後と、パスの実体 (st_dev, st_ino)（files_visible）。
ディスクに触るパスは、そろえる前の綴りのまま使う。
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path

# ツリーに出さないだけの名前。中身は返す（拒む名前ではない）
TREE_SKIP = frozenset({"node_modules", "__pycache__", "build", "DerivedData"})

# 秘密のファイルの名前の一覧。
# 配信の棚（serving-over-tailscale の mdrender.py）の is_denied より広い。
# casefold にして比べる（APFS は大文字小文字を区別せずに引く。ſ は lower() では変わらないが casefold() では s になる）
# .key は丸ごとは拒まない（Keynote の拡張子）。master.key のような完全一致の名前だけ拒む
_DENY_EXACT = {".ds_store", "credentials.json", "credentials.dat",
               "credentials", "master.key", "kubeconfig", "keystore.properties"}   # credentials は AWS（拡張子なし）、master.key は Rails
_DENY_PREFIX = (".", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "secrets", "secret.")
_DENY_SUFFIX = (".pem", ".p12", ".pfx", ".keystore", ".jks", ".ppk", ".sqlite", ".db",
                ".kdbx", ".keychain-db", ".tfstate", ".tfvars", ".tfvars.json",
                ".mobileprovision", ".provisionprofile",
                ".p8")                                                               # App Store Connect の鍵（AuthKey_・SubscriptionKey_ など）
_DENY_CONTAINS = (".bak-", ".env", ".tfstate.")                                      # terraform.tfstate.backup も
_DENY_PREFIX_SUFFIX = (("service-account", ".json"),)                                # service-account-prod.json

_PREVIEW = {
    ".md": "md",
    ".html": "html", ".htm": "html",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".svg": "image",
}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def folder_id(path, secret: bytes) -> str:
    """フォルダの鍵（rootId）。SHA-256(secret + b"root:" + realpath を NFC にそろえた文字列) の先頭16桁（16進）。

    secret（置き場の secret。secrets_store）を混ぜるので、パスが分かっても rootId は計算できない。
    同じ secret・同じ場所なら、いつも同じ値（再起動をまたいで☆・開いたフォルダを覚えていられる）。"""
    real = nfc(os.path.realpath(path))
    return hashlib.sha256(secret + b"root:" + real.encode("utf-8")).hexdigest()[:16]


def _ident(path, follow: bool = True) -> tuple | None:
    """フォルダの実体の印 (st_dev, st_ino)。既定ではシンボリックリンクの先を見る（follow=False ならリンクそのもの）。
    無い・引けないなら None。"""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


class _Place:
    """パス1つを、realpath の区切りごとにたどったもの。

    names は NFC の綴り、idents は「先頭からその階層まで」のフォルダの (st_dev, st_ino)（まだ無い階層は None）。
    綴りだけで比べると、/USERS/<名前>（大文字小文字）や /System/Volumes/Data/Users/<名前>（firmlink）、
    リンク越しの ~/.claude の実体をすり抜ける。同じ場所は同じ (st_dev, st_ino) になるので、それでも比べる。
    """
    __slots__ = ("names", "idents")

    def __init__(self, path):
        raw = Path(os.path.realpath(path)).parts            # ディスクに触るのは、そろえる前の綴り
        self.names = tuple(nfc(p) for p in raw)
        self.idents = tuple(_ident(Path(*raw[:i])) for i in range(1, len(raw) + 1))


def _inside(a: _Place, b: _Place) -> int | None:
    """a が b の中（同じ場所を含む）なら、a の先頭から数えた b の階層の数（1 以上）。そうでなければ None。

    綴り（NFC の区切りごと）か実体 (st_dev, st_ino) のどちらかが合えば中。b がまだ無いフォルダでも綴りで比べられる。
    """
    n = len(b.names)
    if a.names[:n] == b.names:
        return n
    target = b.idents[-1]
    if target is not None:
        for k, ident in enumerate(a.idents, 1):
            if ident == target:
                return k
    return None


def deny_stamp(home: Path, own_dirs: list[Path]) -> tuple:
    """files_visible が見せない場所の実体の印 (st_dev, st_ino) の並び。変わったら、覚えた答えを引き直す。

    ~/.claude を dotfiles へのリンクに張り替える・置き場を作り直す、などで見せない場所が動くため。
    リンクの先（stat）に加えて、リンクそのもの（lstat）の印も入れる。`mv ~/.claude ~/dotfiles/claude && ln -s …` は、
    先の inode が変わらないので、stat だけでは気づけない（lstat は フォルダからリンクに変わる）。
    realpath は引かず stat・lstat だけ（見回りのたびに呼ぶ）。
    """
    h = Path(home)
    return tuple(i for p in (h, h / ".claude", h / "Library", h / "Library" / "Mobile Documents", *own_dirs)
                 for i in (_ident(p), _ident(p, follow=False)))


def files_visible(root: Path, home: Path, own_dirs: list[Path], trusted_base: Path | None = None) -> bool:
    """この起点のファイルを見せてよいか。false なら一覧には出すがファイルは出さない。

    場所は realpath の綴りと実体 (st_dev, st_ino) の両方で比べる（_Place）。見せない場所は次のとおり。
    ホームの実体の外（既定）・ホームそのもの（とそれを含むフォルダ）・~/.claude の実体の中と、それを含むフォルダ・
    ~/Library（iCloud を除く）・own_dirs（置き場・記録・Claude の設定フォルダ）の中と、それを含むフォルダ・
    ホームより下の . で始まる名前。

    trusted_base は worktree の起点のときに渡す親のプロジェクト（--git-common-dir の親）。親そのものが見せてよく、
    起点が親の中にあるときだけ、. で始まる名前の決まりを親より上の階層にだけ当てる（`.worktrees/x`・
    `.claude/worktrees/x` を見せる）。ホーム・~/.claude・~/Library・自分の置き場の決まりは、起点そのものにいつも当てる。
    """
    r = _Place(root)
    start = _inside(r, _Place(home))                        # ホームの階層の数。ホームの外（や、ホームを含むフォルダ）は None
    if start is None or start == len(r.names):              # ホームの外、またはホームそのもの
        return False
    claude = _Place(Path(home) / ".claude")                 # ~/.claude がリンクなら、その行き先の実体
    for d in [claude] + [_Place(o) for o in own_dirs]:      # 見せない場所の中、またはそれを含むフォルダ
        if _inside(r, d) is not None or _inside(d, r) is not None:
            return False
    library = _Place(Path(home) / "Library")
    if _inside(library, r) is not None:                     # ~/Library がリンクなら、その実体を含むフォルダも
        return False
    if _inside(r, library) is not None:
        # 例外は Mobile Documents/<アプリごとの入れ物>/ より深いもの（iCloud）
        icloud = _inside(r, _Place(Path(home) / "Library" / "Mobile Documents"))
        if icloud is None or len(r.names) <= icloud + 1:
            return False
    # . で始まる名前は出さない（is_denied と同じ決まり）を起点にも当てる（ホームより下の全部の階層）
    end = len(r.names)
    if trusted_base is not None:
        t = _inside(r, _Place(trusted_base))
        # 親より下（worktree の置き場）の . は見ない。ただし .git の中に置いた worktree は狭めない
        if (t is not None and files_visible(trusted_base, home, own_dirs)
                and not any(p.casefold() == ".git" for p in r.names[t:])):
            end = t
    return not any(p.startswith(".") for p in r.names[start:end])


def is_denied(name: str) -> bool:
    """ファイル名・フォルダ名1つが、ツリーにも出さず中身も返さない名前か。"""
    n = name.casefold()
    return (n in _DENY_EXACT
            or n.startswith(_DENY_PREFIX)
            or n.endswith(_DENY_SUFFIX)
            or any(x in n for x in _DENY_CONTAINS)
            or any(n.startswith(a) and n.endswith(b) for a, b in _DENY_PREFIX_SUFFIX))


def has_denied_name(rel: str) -> bool:
    """相対パス rel の区切りのどれかが、拒む名前か（名前だけで決まる。ディスクには触らない＝あるかどうかを見せない）。
    NUL を含むものは、名前の問題でなく不正なので False（resolve_in_root が None にする）。"""
    return "\0" not in rel and any(is_denied(p) for p in rel.split("/") if p)


def resolve_in_root(root: Path, rel: str) -> Path | None:
    """root の下の相対パス rel を実体のパスにする。root の外・拒む名前なら None。

    ファイルがあるかは見ない（開けるかは呼び出し側が決める）。
    返すのは realpath のまま（NFC にそろえない。ディスクに触るのは元の綴りの方が確実）。
    """
    if "\0" in rel:
        return None
    parts = [p for p in rel.split("/") if p]                # 先頭の / や // で root から外れない
    if any(is_denied(p) for p in parts):                    # ".." も "." で始まるのでここで落ちる
        return None
    base = os.path.realpath(root)
    real = os.path.realpath(os.path.join(base, *parts))
    base_n, real_n = nfc(base), nfc(real)
    try:
        if os.path.commonpath([base_n, real_n]) != base_n:  # 外へ出るシンボリックリンク
            return None
    except ValueError:
        return None
    # 名前は無害でも、中のシンボリックリンクの行き先が拒む名前（.env など）なら返さない
    if any(is_denied(p) for p in Path(real_n).parts[len(Path(base_n).parts):]):
        return None
    return Path(real)


def rel_below(root: Path, target: Path) -> str | None:
    """実体のパス target の、root の実体からの相対パス（'/' 区切り。root そのものは ""）。root の外なら None。

    target は resolve_in_root が返した実体のパス（リンクをたどり終えたもの）。git の判定（gitignore.py）に渡す形で、
    途中のシンボリックリンクがないので、git が「リンクの先」と言って止まらない。root との比べは NFC にそろえた区切りごと
    （resolve_in_root と同じ）で、返すのは target の綴りのまま（resolve_in_root は頼まれた綴りを返すので、ディスクの
    綴りとは限らない。APFS は両方で引ける）。
    """
    base = Path(os.path.realpath(root)).parts
    real = Path(target).parts
    if len(real) < len(base) or [nfc(p) for p in real[:len(base)]] != [nfc(p) for p in base]:
        return None
    return "/".join(real[len(base):])


def preview_kind(name: str) -> str | None:
    """プレビューの種類。md・html・image・None（目のアイコンを出さない）。"""
    return _PREVIEW.get(os.path.splitext(name)[1].lower())
