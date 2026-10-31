"""組む QR を PNG と端末の文字に描く。

Python 3.9 の標準ライブラリ（zlib・struct）と、同梱の vendor/qrcodegen.py（Project Nayuki、MIT）だけで動かす。

行列は qrcodegen が作る（誤り訂正は MEDIUM）。PNG は 1 ビット白黒（フィルタなし）で、1 モジュールを SCALE 画素、
まわりに BORDER モジュールの白い余白（QR の静かな領域＝4 モジュール以上）を付ける。
中に符号が入るので、PNG の置き場は権限 700・ファイルは 600（write_png）。この関数群は符号を記録・例外の文に出さない。
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

from vendor import qrcodegen

SCALE = 8                                                 # 1 モジュールの画素数
BORDER = 4                                                # 余白のモジュール数
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TERMINAL_STYLE = "\x1b[30;47m"                            # 黒の字・白の地（端末の配色に左右されない）
TERMINAL_RESET = "\x1b[0m"
_HALF = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}


def matrix(text: str) -> list:
    """text を QR にした、暗いモジュールが True の行列（行の並び）。余白は含まない。"""
    code = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    size = code.get_size()
    return [[code.get_module(x, y) for x in range(size)] for y in range(size)]


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_bytes(modules: list, scale: int = SCALE, border: int = BORDER) -> bytes:
    """行列を 1 ビット白黒の PNG にする（0 が黒）。"""
    side = (len(modules) + 2 * border) * scale
    stride = (side + 7) // 8

    def line(my: int) -> bytes:
        """モジュールの行 my（余白の外は白）を、横に scale 倍して詰めた 1 行（フィルタ 0 つき）。"""
        bits = 0
        for x in range(side):
            mx = x // scale - border
            dark = 0 <= my < len(modules) and 0 <= mx < len(modules) and modules[my][mx]
            bits = (bits << 1) | (0 if dark else 1)
        bits <<= stride * 8 - side                        # 行の終わりの余りのビットは白で埋める
        return b"\x00" + bits.to_bytes(stride, "big")

    raw = b"".join(line(my) * scale for my in range(-border, len(modules) + border))   # 同じモジュールの行は scale 回
    ihdr = struct.pack(">IIBBBBB", side, side, 1, 0, 0, 0, 0)
    return _SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def write_png(path: Path, text: str) -> None:
    """text の QR を path に PNG で書く（権限 600）。あるファイル・リンクには書かない（FileExistsError・OSError）。"""
    data = png_bytes(matrix(text))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise


def terminal_art(modules: list, border: int = BORDER) -> str:
    """行列を端末に描く文字（上下2モジュールを半分の高さの文字1つに）。各行を黒字・白地の色で包む。
    余白（静かな領域）も白で描く。"""
    size = len(modules)

    def dark(x: int, y: int) -> bool:
        mx, my = x - border, y - border
        return 0 <= mx < size and 0 <= my < size and modules[my][mx]

    total = size + 2 * border
    lines = []
    for y in range(0, total, 2):
        row = "".join(_HALF[(dark(x, y), dark(x, y + 1))] for x in range(total))
        lines.append(f"{TERMINAL_STYLE}{row}{TERMINAL_RESET}")
    return "\n".join(lines)
