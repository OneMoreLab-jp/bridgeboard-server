"""ファイルツリー（1段）・Claude が書いたパスを (rootId, relPath) に引く・Content-Type（ツリーとファイル口の材料）。

Python 3.9 の標準ライブラリだけで動かす。

名前はディスクにあるまま返す（NFC/NFD を書き換えない）。比べるのは realpath を NFC にそろえた区切りごと。
読めないフォルダの PermissionError は握りつぶさずに上げる（403 にするのは呼び出し側）。
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from .paths import TREE_SKIP, is_denied, nfc, preview_kind, rel_below, resolve_in_root

# システムの mime.types を読まない（機械ごとに答えが変わらないように）。3.9 の表に無い md・webp を足す
_MIME = mimetypes.MimeTypes()
_MIME.add_type("text/markdown", ".md")
_MIME.add_type("image/webp", ".webp")
# text/* のほかに、文字でできているもの（html が相対パスで読む js・json、svg）
_TEXTUAL = frozenset({"application/json", "application/javascript", "image/svg+xml"})


def content_type(name: str) -> str:
    """拡張子から決める Content-Type。文字のものは `; charset=utf-8`、知らないものは application/octet-stream。"""
    kind, encoding = _MIME.guess_type(name)
    if kind is None or encoding:                            # a.md.gz は md ではない（圧縮の入れ物）
        return "application/octet-stream"
    return kind + "; charset=utf-8" if kind.startswith("text/") or kind in _TEXTUAL else kind


def list_dir(root: Path, rel: str, limit: int = 2000, ignored=None) -> dict | None:
    """root の下の rel の中身を1段だけ返す（GET /v1/roots/<rootId>/tree の中身から rootId を除いたもの）。

    無い・フォルダでない・拒む名前・root の外なら None。読めなければ PermissionError をそのまま上げる。
    フォルダが先、その中は名前順（NFC にそろえた大文字小文字なし）。limit を超えたら先頭 limit 件と truncated: true。

    ignored は git で無視されたものを隠す関数 `ignored(root, 相対パスの並び) -> 隠すものの集まり`（GitIgnore.ignored）。
    渡された相対パスは root の実体からのもので、途中にシンボリックリンクがない（リンクは行き先の実体で聞く）。
    フォルダそのものが無視されていれば None（中も見せない）。隠した後で limit に切る。
    """
    target = resolve_in_root(root, rel)
    if target is None or not target.is_dir():                # is_dir は EACCES を握りつぶさない
        return None
    here = rel_below(root, target) if ignored is not None else ""
    if here and ignored(root, [here]):                       # 無視されたフォルダの中は、直接開かれても見せない
        return None
    parts = [p for p in rel.split("/") if p]
    found = []                                               # (名前, フォルダか, DirEntry, リンクの行き先の実体のパス)
    with os.scandir(target) as it:
        for e in it:
            if e.name in TREE_SKIP or is_denied(e.name):     # . で始まる名前も is_denied が落とす
                continue
            try:
                # target は root の中と確かめ済みなので、外へ出られるのはシンボリックリンクだけ
                real = None
                if e.is_symlink():
                    real = resolve_in_root(root, "/".join(parts + [e.name]))
                    if real is None:
                        continue
                found.append((e.name, e.is_dir(), e, real))
            except OSError:                                  # 1つだけ読めないものは、一覧ごと落とさず飛ばす
                continue
    if ignored is not None and found:
        found = _drop_ignored(root, here, found, ignored)
    found.sort(key=lambda t: (not t[1], nfc(t[0]).casefold(), t[0]))
    entries = []
    for name, is_dir, e, _ in found[:limit]:                 # stat は載せる分だけ
        if is_dir:
            entries.append({"name": name, "kind": "dir"})
            continue
        try:
            st = e.stat()
        except OSError:                                      # 行き先の無いシンボリックリンクなど
            continue
        entries.append({"name": name, "kind": "file", "size": st.st_size,
                        "mtime": st.st_mtime_ns // 1_000_000, "preview": preview_kind(name)})
    out = {"path": "/".join(parts), "entries": entries}
    if len(found) > limit:
        out["truncated"] = True
    return out


def _drop_ignored(root: Path, here: str, found: list, ignored) -> list:
    """git で無視されたものを found から外す。フォルダ1つの一覧で、聞くのは1回（ignored がまとめて答える）。
    シンボリックリンクは、リンクそのものと行き先の実体のどちらかが無視されていれば外す（出しても開けない）。"""
    prefix = here + "/" if here else ""
    asks = {}                                                # 名前 -> 聞くパス
    for name, _, _, real in found:
        ask = [prefix + name]
        if real is not None:
            target = rel_below(root, real)
            if target and target not in ask:
                ask.append(target)
        asks[name] = ask
    hidden = ignored(root, list(dict.fromkeys(p for ask in asks.values() for p in ask)))
    if not hidden:
        return found
    return [t for t in found if hidden.isdisjoint(asks[t[0]])]


def _real_parts(path) -> tuple:
    """realpath を NFC にそろえ、区切りごとに分ける。"""
    return Path(nfc(os.path.realpath(path))).parts


def locate(path: str, cwd: str, own_root: tuple[str, Path] | None, roots: dict[str, Path]) -> dict | None:
    """Claude が書いたパス（絶対、または cwd からの相対）を、見せてよいフォルダの (rootId, relPath) に引く。

    まず own_root（そのセッションの起点）、外なら roots の中で最も深いもの。どこにも入らない・拒む名前なら None。
    起点の中でも拒まれたら（途中に secrets… など）、次の候補（より深い起点）で引き直す。
    relPath は書かれた綴りのまま（NFC に直さない。resolve_in_root に戻せば同じファイルに着く）。
    ファイルが無くても起点の中なら返す（exists: false）。
    """
    if "\0" in path or "\0" in cwd:
        return None
    if not os.path.isabs(path):
        if not os.path.isabs(cwd):                           # cwd が無いのに、サーバー自身の cwd から引かない
            return None
        path = os.path.join(cwd, path)
    real = os.path.realpath(path)
    real_n, real_raw = Path(nfc(real)).parts, Path(real).parts   # 区切りの数は NFC にしても変わらない
    candidates = [own_root] if own_root else []
    candidates += sorted(roots.items(), key=lambda kv: len(_real_parts(kv[1])), reverse=True)
    for root_id, root in candidates:
        r = _real_parts(root)
        if real_n[:len(r)] != r:                             # 文字列の前方一致ではなく、区切りごとに比べる
            continue
        rel = "/".join(real_raw[len(r):])
        target = resolve_in_root(root, rel)
        if target is None:
            continue
        return {"rootId": root_id, "relPath": rel, "preview": preview_kind(os.path.basename(rel)),
                "exists": _exists(target)}
    return None


def _exists(target: Path) -> bool | None:
    """あるか。無ければ False、読めないフォルダの中（chmod・macOS の守り）で分からなければ None。
    os.path.exists は PermissionError も False にして「消えた」と見分けがつかないので使わない。"""
    try:
        os.stat(target)
    except PermissionError:
        return None
    except OSError:                                          # FileNotFoundError・NotADirectoryError など
        return False
    return True
