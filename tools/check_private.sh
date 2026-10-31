#!/bin/bash
# 公開してはいけない文字列が、フォルダの中（作業コピーと git の履歴）に無いかを調べる。公開（push）の前の関所。
#
# 使い方: tools/check_private.sh [--builtin-only] [--history] <フォルダ>
# 終了コード:
#   0   見つからなかった（かつ、調べるべきものを全部調べた）
#   1   見つかった
#   2   調べきれなかった。個人の文字列のファイルが無い・空・読めない・ふつうのファイルでない、
#       フォルダに .git があるのに git が読めない、または grep・git などが失敗した。
#       「見つからなかった」ではないので、公開してはいけない
#   64  使い方の誤り
#
# 調べるもの（大文字小文字は区別しない。かなの濁点は NFC と NFD の両方の綴りで探す）:
#   - 作者の環境の印（下の BUILTIN）
#   - ~/.appstoreconnect/private-strings.txt の各行（1行1つ。空行は除く）
#     このファイルが使えないときは 2 で終わる。--builtin-only を付けたときだけ、このファイルを見ずに
#     BUILTIN だけを調べる（成功の文に「personal list not checked」と出る）
# それぞれの文字列は、ファイルの中身だけでなく、ファイル名・フォルダ名（作業コピーの相対パス）とも照合する。
# --history を付けると、git の履歴の全部の版に出てくるファイル名（改名・削除の前の名前も。git log --all --name-only）とも
# 照合する。公開の前（履歴ごと出す前）に付ける。付けなければ履歴のファイル名は調べず、成功の文にそう書く。
# .git が無いフォルダだけは、ふつうのフォルダとして作業コピーだけを調べる（成功の文に「no git history」と出る）。
# 見つかったときは「どのファイルの何行目か」だけを出す。一致した中身と private-strings.txt の中身は、どこにも出さない。
# ファイル名が当たったときは、パスそのものに文字列が入っているので、パスは出さず、当たった数だけを出す。
# 環境変数 BRIDGEBOARD_PRIVATE_STRINGS で、個人の文字列のファイルの場所を変えられる（試験用）。
#
# 限界: 1つの文字列の中で NFC と NFD が混ざった綴り（濁点の一部だけ分解）は探せない。
#       作業コピーのシンボリックリンクの先の文字列は調べない（コミット済みのものは、履歴の git grep が調べる）。
# BUILTIN の文字列はこのファイル自身に当たらないよう、2つに割って書いてある。
set -eEuo pipefail
trap 'echo "check_private: ERROR: unexpected failure at line $LINENO; the check did not complete" >&2; exit 2' ERR
# 呼び出し元（git の hook など）が git の向き先を変えていても、調べるフォルダの .git だけを見る
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE

usage() {
  echo "usage: $0 [--builtin-only] [--history] <directory>" >&2
  exit 64
}

builtin_only=0
history_names=0
DIR=""
for arg in "$@"; do
  case "$arg" in
    --builtin-only) builtin_only=1 ;;
    --history) history_names=1 ;;
    -*) usage ;;
    *) [ -z "$DIR" ] || usage; DIR="$arg" ;;
  esac
done
{ [ -n "$DIR" ] && [ -d "$DIR" ]; } || usage
DIR="$(cd "$DIR" && pwd -P)"                       # シンボリックリンクを実体にする（grep -r はリンクを辿らない）
export GIT_CEILING_DIRECTORIES="$(dirname "$DIR")"  # .git が壊れたときに、親のフォルダのリポジトリを読んで通さない
PRIVATE_FILE="${BRIDGEBOARD_PRIVATE_STRINGS:-${HOME:-}/.appstoreconnect/private-strings.txt}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export LC_ALL=C

# 元のファイル → 探す文字列のファイル（各行の原文・NFC・NFD。前後の空白と空行は除く。空のパターンは全部に当たるため）
EXPAND='
import sys, unicodedata
src, dst = sys.argv[1], sys.argv[2]
with open(src, "rb") as f:
    raw = f.read()
if b"\0" in raw:                                    # UTF-16 など。バイトで探しても当たらない文字列になる
    sys.exit(3)
text = raw.decode("utf-8", errors="surrogateescape")
seen = []
for line in text.split("\n"):
    s = line.replace("\ufeff", "").strip()           # 先頭の BOM（U+FEFF）が付いた行も当たるように
    if not s:
        continue
    for form in (s, unicodedata.normalize("NFC", s), unicodedata.normalize("NFD", s)):
        if form not in seen:
            seen.append(form)
with open(dst, "wb") as f:
    for s in seen:
        f.write(s.encode("utf-8", errors="surrogateescape") + b"\n")
'
expand_patterns() {
  python3 -I -c "$EXPAND" "$1" "$2"
}

fail_check() {   # 調べきれなかった印。最後に 2 で終わる
  echo "check_private: ERROR: $1" >&2
  : > "$TMP/error"
}

# 内蔵の印（1行1つ）
{
  echo "100.80"".";  echo "macbook""m1max";  echo "taile""870a1"
  echo "/Users/""tomoaki";  echo "21_One""MoreLab"
} > "$TMP/builtin.raw"
if ! expand_patterns "$TMP/builtin.raw" "$TMP/builtin.pat"; then
  echo "check_private: ERROR: could not prepare the built-in patterns (is python3 available?)" >&2
  exit 2
fi
kinds=(builtin)

# 個人の文字列。使えなければ、黙って飛ばさず 2 で終わる（--builtin-only のときだけ見ない）
if [ "$builtin_only" = 0 ]; then
  reason=""
  if [ ! -e "$PRIVATE_FILE" ]; then
    reason="does not exist"
  elif [ ! -f "$PRIVATE_FILE" ]; then
    reason="is not a regular file"
  elif [ ! -r "$PRIVATE_FILE" ]; then
    reason="is not readable"
  else
    rc=0
    expand_patterns "$PRIVATE_FILE" "$TMP/private.pat" || rc=$?
    if [ "$rc" != 0 ]; then
      reason="could not be read"
    elif [ ! -s "$TMP/private.pat" ]; then
      reason="has no strings (empty, or blank lines only)"
    fi
  fi
  if [ -n "$reason" ]; then
    echo "check_private: the personal strings list was NOT checked: $PRIVATE_FILE $reason" >&2
    echo "check_private: refusing to report ok. Fix the list, or pass --builtin-only to check only the built-in patterns." >&2
    exit 2
  fi
  kinds+=(private)
fi

report() {   # $1 = 種類、$2 = grep の出力（場所:行:中身）。場所と行だけを出す（中身は出さない）
  local out
  out="$(awk -F: '{ for (i = 2; i <= NF; i++) if ($i ~ /^[0-9]+$/) { o = $1; for (j = 2; j <= i; j++) o = o ":" $j; print o; next } print $1 }' "$2" \
    | sed "s/\$/  <- $1/")"
  if [ -n "$out" ]; then
    printf '%s\n' "$out"
    : > "$TMP/found"
  fi
}

# grep・git grep の終わり方：0 一致・1 一致なし・それ以外は失敗（一致が出ていても、調べきれていない）
handle_status() {   # $1 = 種類、$2 = 終了コード、$3 = 何を調べたか
  case "$2" in
    0) report "$1 pattern" "$TMP/hits" ;;
    1) ;;
    *) [ ! -s "$TMP/hits" ] || report "$1 pattern" "$TMP/hits"
       fail_check "$3 failed (exit $2); the result is incomplete" ;;
  esac
}

# 1. 作業コピー（.git と __pycache__ は除く。履歴は下で別に調べる）。場所は DIR からの相対で出す
TMPNAME="$(basename "$TMP")"
for kind in "${kinds[@]}"; do
  st=0
  ( cd "$DIR" || exit 2
    grep -rnaiF -f "$TMP/$kind.pat" --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir="$TMPNAME" -- . ) \
    > "$TMP/hits" || st=$?
  handle_status "$kind" "$st" "scanning the working tree"
done

# 1b. 作業コピーのファイル名・フォルダ名（相対パス）。.git と __pycache__ は除く（中身の検査と同じ）
count_names() {   # $1 = 種類、$2 = 名前の一覧のファイル、$3 = どこの名前か（文に出す）。パスそのものに文字列が入るので、数だけ出す
  local n st=0
  n="$(grep -ciaF -f "$TMP/$1.pat" "$2")" || st=$?   # 一致が 0 本のときは終了コード 1（数は 0）
  case "$st" in
    0) echo "file names: $n path(s) in $3 match a $1 pattern (the paths are not printed, because they hold the pattern; to find them: find . | grep -i <your string>)"
       : > "$TMP/found" ;;
    1) ;;
    *) fail_check "scanning the file names in $3 failed (exit $st); the result is incomplete" ;;
  esac
}
st=0
( cd "$DIR" || exit 2
  find . \( -name .git -o -name __pycache__ -o -name "$TMPNAME" \) -prune -o -print ) > "$TMP/names" || st=$?
if [ "$st" != 0 ]; then
  fail_check "listing the file names in the working tree failed (exit $st); the result is incomplete"
else
  for kind in "${kinds[@]}"; do
    count_names "$kind" "$TMP/names" "the working tree"
  done
fi

# 2. git の履歴（コミットのメッセージ・名前・メール、タグ・ブランチの名前と、全部の版のファイル）。公開すると履歴ごと出るため。
#    .git があるのに git が読めなければ、ふつうのフォルダ扱いにせず 2 で終わる。.git が全く無いフォルダだけがふつうのフォルダ
plain_folder=0
if [ -e "$DIR/.git" ] || [ -L "$DIR/.git" ]; then
  if ! git -C "$DIR" rev-parse --git-dir > /dev/null 2> "$TMP/giterr"; then
    cat "$TMP/giterr" >&2
    fail_check "$DIR/.git exists but git cannot read the repository; the git history was NOT checked"
  else
    st=0
    git -C "$DIR" rev-list --all > "$TMP/revs" || st=$?
    if [ "$st" != 0 ]; then
      fail_check "git rev-list failed (exit $st); the git history was NOT checked"
    elif [ -s "$TMP/revs" ]; then
      : > "$TMP/meta"
      st=0
      git -C "$DIR" log --all --format='%an <%ae>%n%cn <%ce>%n%B' >> "$TMP/meta" || st=$?
      [ "$st" = 0 ] || fail_check "git log failed (exit $st); the commit messages were not fully checked"
      st=0
      git -C "$DIR" for-each-ref --format='%(refname)%n%(taggername) %(taggeremail)%n%(contents)' >> "$TMP/meta" || st=$?
      [ "$st" = 0 ] || fail_check "git for-each-ref failed (exit $st); the tag and branch names were not fully checked"
      for kind in "${kinds[@]}"; do
        st=0
        grep -qiF -f "$TMP/$kind.pat" "$TMP/meta" || st=$?
        case "$st" in
          0) echo "git history: a commit message, author or ref name matches a $kind pattern"
             : > "$TMP/found" ;;
          1) ;;
          *) fail_check "scanning the commit messages failed (exit $st); the result is incomplete" ;;
        esac
      done
      # --history：全部の版に出てくるファイル名（改名の前の名前・消したファイルも。-z で名前をそのまま受ける＝日本語が \343… にならない）
      if [ "$history_names" = 1 ]; then
        st=0
        git -C "$DIR" log --all --name-only --no-renames -z --format= | tr '\0' '\n' | sort -u > "$TMP/histnames" || st=$?
        if [ "$st" != 0 ]; then
          fail_check "git log --name-only failed (exit $st); the file names in the git history were NOT checked"
        else
          for kind in "${kinds[@]}"; do
            n=0
            st=0
            n="$(grep -ciaF -f "$TMP/$kind.pat" "$TMP/histnames")" || st=$?
            case "$st" in
              0) echo "git history: $n file name(s) match a $kind pattern (the names are not printed, because they hold the pattern; to find them: git log --all --name-only | grep -i <your string>)"
                 : > "$TMP/found" ;;
              1) ;;
              *) fail_check "scanning the file names in the git history failed (exit $st); the result is incomplete" ;;
            esac
          done
        fi
      fi
      # 版は 50 ずつ git grep に渡す（全部を1度に渡すと、履歴が長いとき引数が長すぎて失敗する）
      st=0
      split -l 50 "$TMP/revs" "$TMP/revchunk." || st=$?
      if [ "$st" != 0 ]; then
        fail_check "split failed (exit $st); the file contents in the git history were NOT checked"
      else
        for chunk in "$TMP"/revchunk.*; do
          [ -f "$chunk" ] || { fail_check "no revision list to scan"; break; }
          for kind in "${kinds[@]}"; do
            st=0
            # shellcheck disable=SC2046
            git -C "$DIR" grep -inaF -f "$TMP/$kind.pat" $(cat "$chunk") -- . > "$TMP/hits" || st=$?
            handle_status "$kind" "$st" "scanning the git history"
          done
        done
      fi
    fi
  fi
else
  plain_folder=1
  echo "check_private: note: $DIR has no .git; it is checked as a plain folder, with no git history" >&2
fi

if [ -e "$TMP/found" ]; then
  echo "check_private: FOUND (see above)" >&2
  exit 1
fi
if [ -e "$TMP/error" ]; then
  echo "check_private: the check could not be completed. This is NOT a clean result." >&2
  exit 2
fi
notes=""
[ "$builtin_only" = 0 ] || notes="$notes; personal list not checked, built-in patterns only"
[ "$plain_folder" = 0 ] || notes="$notes; no git history (the folder has no .git)"
[ "$plain_folder" = 1 ] || [ "$history_names" = 1 ] || notes="$notes; file names in the git history were skipped (pass --history to check them)"
echo "check_private: ok (nothing found$notes)"
