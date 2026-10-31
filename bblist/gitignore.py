"""git が無視しているファイルを、既定では返さない。

秘密のファイルは、名前（paths.is_denied）で拒めないものが多い（config/local.yml・out/ の書き出し・.log）。
そういうものは、たいてい .gitignore に書いてある。「git が無視しているもの」は見せない、とすると、名前の一覧に頼らず守れる。

- git のリポジトリの起点（.git がある）だけに効く。git でない起点・フォルダには何もしない
- 判定は `git check-ignore --stdin -z` に名前をまとめて渡す（フォルダ1つの一覧で1回）。答えは (起点, フォルダ) ごとに ttl 秒覚える
- 追跡済みのファイルは無視として扱わない：git の `check-ignore` は、索引にあるもの（追跡済み）を無視と答えない
  （`add -f` したファイル・無視の規則より前から追跡しているファイルは、履歴に入っていて隠す意味がない。`--no-index` を
  付けると、それらも無視と答えてしまう）。そのかわり索引を読む check-ignore は、名前の数 × 索引の数に比例して遅い
  （試した：2,000 名 × 索引 22,000 件で 430 ms。`--no-index` なら 12 ms で、索引の大きさによらない）。
  そこで2段にする：まず `--no-index` で「規則に当たる名前」を速く絞り、1つでもあれば、その名前だけを索引つきで聞き直して
  追跡済みを外す（無視されるものは普通は少ない）。2段目が失敗・時間切れでも、1段目の答えを使う
  （追跡済みなのに規則に当たるファイルが隠れるだけで、無視されたファイルは隠れたまま。フォルダごと見せることはしない）
- git が動かない（入っていない・時間切れ・予期しない失敗）ときは隠さず、available を False にする（診断に出す）

git の呼び方は gitcmd.run（`-c core.fsmonitor=false`・GIT_ で始まる環境変数を渡さない・引数の並び・時間切れ）。
標準入力に名前、出力は標準出力と標準エラーを受ける。時間切れは 5 秒（1回ごと）。
フォルダごとに `git -C <そのフォルダ> check-ignore <名前>` と呼ぶ。起点から `a/b/c` と渡すと、途中にシンボリックリンクや
サブモジュールがあるとき git が「リンクの先」「サブモジュールの中」と言って全体が止まる（試して確かめた）。
そのフォルダの中で呼べば、サブモジュールはその中のリポジトリの規則で判定でき、リンクは呼ぶ側が実体の相対パスに直して渡す。
フォルダの中で呼ぶと git は規則を NFC で比べる（macOS の core.precomposeunicode。git status も同じ）ので、名前は NFC の綴りでも聞く。

名前は `./<名前>` として渡し、出力の `./` を外す。名前のまま渡すと、`:!x` `:^x` `:(glob)y` のような名前を git が pathspec
の magic と読み、128 で全体が止まる（フォルダに1つ置かれるだけで一覧ぜんぶが隠れなくなる）。`:foo.log` は黙って `foo.log` として
調べられる。`GIT_LITERAL_PATHSPECS` は check-ignore には効かない（試した）。

索引つきの check-ignore（2段目）は名前を glob として索引に当てる（`GIT_LITERAL_PATHSPECS` も `:(literal)` も check-ignore は受けない）。
追跡済みの `tracked.log` があると、`[t]racked.log` `t?acked.log` `*.log` という名前の無視されたファイルが追跡済みと読まれて
見えてしまう。`* ? [ \\` を含む名前は2段目に回さず、1段目の答えのまま隠す（追跡済みの glob 名のファイルが隠れ過ぎるだけ）。

別のリポジトリ（git clone したもの）が、無視されたフォルダの中にあると、git はその中のリポジトリで判定し、外の規則
（`vendor/`）を見ない。そこで、パスの祖先のフォルダのうち `.git` があるもの自身も、外のリポジトリで聞く
（そのフォルダが無視されていれば、その下は全部隠す）。

check-ignore には GIT_CEILING_DIRECTORIES（起点の親。綴りが NFC・NFD のどちらでも効くよう両方）を付ける。
起点に `.git` があっても作業コピーでない（空・壊れている）と、git は上のフォルダへ登って別のリポジトリを使う。
ホームを dotfiles の git にして `*` を書いている人では、ぜんぶ無視になってしまう。

Python 3.9 の標準ライブラリだけで動かす。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

from . import gitcmd
from .paths import nfc

log = logging.getLogger(__name__)

TIMEOUT = 5                                               # 秒（git を1回呼ぶごと）。答えなければ、隠さずに進める
_GLOB = frozenset("*?[\\")                                   # git が pathspec の glob と読む文字（\ も：エスケープ）


def _ceilings(root) -> tuple:
    """起点の親（実体のパス）を、NFC・NFD の綴りで。GIT_CEILING_DIRECTORIES の区切りの `:` が入るパスは付けない
    （GitIgnore.ignored の .git の確認だけに頼る）。root が無い・親が無いなら空。"""
    if root is None:
        return ()
    parent = os.path.dirname(os.path.realpath(root))
    if not parent or parent == os.sep or ":" in parent:
        return ()
    return tuple(dict.fromkeys((parent, nfc(parent), unicodedata.normalize("NFD", parent))))


def run_check_ignore(directory, names, root=None) -> set[str] | None:
    """directory（フォルダ）の中の names（そのフォルダ直下の名前）のうち、git が無視しているものの集まり。

    git でないフォルダ・入れないフォルダ・無いフォルダなら空（隠すものがない）。git が動かなかった・予期しない答えなら None
    （隠さずに、呼ぶ側が available を False にする）。返すのは、渡した綴りのままの名前。root は起点（天井を決める）。
    追跡済みのファイルは含まない（モジュールの説明を参照。`--no-index` で絞り、残りを索引つきで聞き直す）。
    """
    if not names:
        return set()
    if not os.access(directory, os.R_OK | os.X_OK):       # 無い・入れない：隠すものはない。読めない扱いは呼ぶ側の 403・404
        return set()
    ceilings = _ceilings(root)
    matching = _check_ignore(directory, names, ["--no-index"], ceilings)   # 索引を読まない段：規則に当たるもの（追跡済みを含む）
    if not matching:                                      # None（失敗）か空（git でない・当たるものがない）
        return matching
    # 索引を見て、追跡済みを外す。この段が失敗しても、1段目の答え（隠す側）を使う。
    # glob の文字を含む名前は2段目に回さない：索引つきの check-ignore は名前を glob として索引に当てるので、
    # 追跡済みの tracked.log があると `[t]racked.log` `t?acked.log` `*.log` という名前の無視されたファイルを追跡済みと読む。
    # 1段目（--no-index）は名前を文字どおりに当てるので、その答えのまま（隠す側）にする
    literal = [n for n in names if n in matching and _GLOB.isdisjoint(n)]
    if not literal:
        return matching
    second = _check_ignore(directory, literal, [], ceilings)
    if second is None:
        return matching
    return second | {n for n in matching if not _GLOB.isdisjoint(n)}


def _check_ignore(directory, names, flags: list[str], ceilings) -> set[str] | None:
    """`git check-ignore --stdin -z` を1回呼ぶ。git でないフォルダは空の集まり、動かなかったら None。"""
    try:
        r = gitcmd.run(["check-ignore", "--stdin", "-z", *flags], directory, timeout=TIMEOUT, ceilings=ceilings,
                       input=b"".join(b"./" + os.fsencode(n) + b"\0" for n in names))   # ./ で pathspec の magic を避ける
    except (OSError, subprocess.SubprocessError, ValueError):   # git が無い・時間切れ・引数に NUL
        return None
    if r.returncode in (0, 1):                            # 0 は無視されたものがある、1 は無い
        return {os.fsdecode(p[2:] if p.startswith(b"./") else p) for p in r.stdout.split(b"\0") if p}
    if r.returncode == 128 and b"not a git repository" in r.stderr:
        return set()                                      # git でないフォルダ。失敗ではない
    return None


class GitIgnore:
    """git が無視しているものを調べて覚える。show_ignored なら何も隠さず、git も呼ばない。

    git は `git(フォルダ, 名前の並び, 起点) -> 無視されている名前の集まり | None`（None は失敗）。試験では偽物を渡す。
    複数の糸（要求ごと）から呼ばれるので、覚えた答えは lock で守る。git を呼んでいる間は lock を持たない。
    """

    MAX_FOLDERS = 256                                     # 覚えるフォルダの数の上限（古いものから捨てる）

    def __init__(self, show_ignored: bool, git=run_check_ignore, ttl: float = 30.0, clock=time.monotonic):
        self._show = bool(show_ignored)
        self._git = git
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()                     # 下の2つを守る
        self._cache: dict[tuple[str, str], list] = {}     # (起点, 起点からのフォルダ) -> [聞いた時刻, {名前: 無視されているか}]
        self._available = True

    @property
    def show_ignored(self) -> bool:
        """無視されたものも返す設定か（診断が報告する）。"""
        return self._show

    @property
    def available(self) -> bool:
        """直近の問い合わせで git が動いたか（まだ聞いていなければ True）。False の間、無視されたものも隠さない。"""
        return self._available

    def ignored(self, root: Path, rels: list[str]) -> set[str]:
        """root からの相対パス rels（'/' 区切り・途中にシンボリックリンクがない実体のパス）のうち、無視されているもの。

        git のリポジトリの起点（root に .git がある）だけに効く。ほかは空。渡した綴りのまま返す。
        パスの祖先のフォルダのうち別のリポジトリ（.git がある）のものは、そのフォルダ自身が外のリポジトリで無視されていないかも
        聞く：無視されたフォルダの下の git clone は、中のリポジトリの規則だけでは無視と答えない。
        """
        if self._show:
            return set()
        root = Path(root)
        if not os.path.lexists(root / ".git"):            # 上のフォルダが git でも、git でない起点には効かせない
            return set()
        is_repo: dict[str, bool] = {}                     # 祖先のフォルダ -> 別のリポジトリか（一覧の中で1回だけ見る）
        asks: dict[str, list[str]] = {}                   # rel -> 聞くパス（自分と、別のリポジトリになっている祖先）
        for rel in dict.fromkeys(rels):
            parts = rel.split("/")
            if any(p in ("", ".", "..") for p in parts):  # 起点の外へ出る・曖昧な形は git に渡さない
                continue
            ask = [rel]
            for k in range(1, len(parts)):
                folder = "/".join(parts[:k])
                if folder not in is_repo:
                    is_repo[folder] = os.path.lexists(root / folder / ".git")
                if is_repo[folder]:
                    ask.append(folder)
            asks[rel] = ask
        groups: dict[str, list[str]] = {}
        for path in dict.fromkeys(p for ask in asks.values() for p in ask):
            parent, _, name = path.rpartition("/")
            groups.setdefault(parent, []).append(name)
        hidden: set[str] = set()
        for parent, names in groups.items():
            hidden.update(f"{parent}/{n}" if parent else n for n in self._folder(root, parent, names))
        return {rel for rel, ask in asks.items() if hidden.intersection(ask)}

    def _folder(self, root: Path, parent: str, names: list[str]) -> set[str]:
        """フォルダ parent の中の names のうち無視されているもの。聞いていない名前だけ git に聞いて覚える。"""
        key = (str(root), parent)
        now = self._clock()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and now - entry[0] >= self._ttl:
                del self._cache[key]
                entry = None
            known = entry[1] if entry is not None else {}
            missing = [n for n in names if n not in known]
            if not missing:
                return {n for n in names if known[n]}
        verdict = self._ask(root, root / parent if parent else root, missing)
        with self._lock:
            if verdict is None:
                self._set_available(False)
                entry = self._cache.get(key)               # 失敗は覚えない。覚えている分があれば、それは使う
                known = entry[1] if entry is not None else {}
                return {n for n in names if known.get(n)}
            self._set_available(True)
            entry = self._cache.get(key)
            if entry is None:
                self._drop_old(now)
                entry = self._cache[key] = [now, {}]
            entry[1].update(verdict)
            return {n for n in names if entry[1].get(n)}

    def _ask(self, root: Path, directory: Path, names: list[str]) -> dict[str, bool] | None:
        """git に聞く。各名前を、渡された綴りと NFC の綴りの両方で聞く（git は規則を NFC で比べるが、
        名前の綴りは比べる前にそろえない。ディスクの NFD の名前のままでは、NFC で書いた規則に当たらない）。"""
        asked: dict[str, list[str]] = {}
        for n in names:
            for form in dict.fromkeys((nfc(n), n)):
                asked.setdefault(form, []).append(n)
        found = self._git(directory, list(asked), root)
        if found is None:
            return None
        verdict = {n: False for n in names}
        for form in found:
            for n in asked.get(form, ()):
                verdict[n] = True
        return verdict

    def _set_available(self, value: bool) -> None:
        """lock を持って呼ぶ。動かなくなったときだけ記録に1行（パスも名前も書かない）。"""
        if self._available and not value:
            log.warning("git check-ignore is not working; files that git ignores are being shown")
        self._available = value

    def _drop_old(self, now: float) -> None:
        """lock を持って呼ぶ。新しい1件を入れる前に、期限の切れたものと、上限を超える古いものを捨てる。"""
        for k in [k for k, e in self._cache.items() if now - e[0] >= self._ttl]:
            del self._cache[k]
        while len(self._cache) >= self.MAX_FOLDERS:
            del self._cache[next(iter(self._cache))]
