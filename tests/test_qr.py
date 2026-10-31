"""bblist/qr.py の試験。python3 -m unittest tests.test_qr -v

QR の PNG は標準ライブラリ（zlib）だけで書く。試験は PNG を自分で読み戻して、qrcodegen の行列と画素を突き合わせる。
"""
from __future__ import annotations

import os
import stat
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from bblist import qr
from vendor import qrcodegen

TEXT = ("bridgeboard://pair?v=1&host=mac.example.ts.net&ip=100.101.102.103&api=8772&file=8773"
        "&code=AbCdEfGhIjKlMnOpQrStUv&sid=0123456789abcdef")


def decode_png(data: bytes):
    """(幅, 高さ, 暗い画素の行列)。1 ビットの白黒 PNG だけを読む（qr.py が書く形）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG の署名が違う"
    pos, chunks = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body), f"{kind!r} の CRC が違う"
        chunks.append((kind, body))
        pos += 12 + length
    assert [k for k, _ in chunks][0] == b"IHDR" and chunks[-1][0] == b"IEND"
    width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, ctype, comp, filt, interlace) == (1, 0, 0, 0, 0), "1 ビット白黒でない"
    raw = zlib.decompress(b"".join(b for k, b in chunks if k == b"IDAT"))
    stride = (width + 7) // 8 + 1
    assert len(raw) == stride * height
    rows = []
    for y in range(height):
        row = raw[y * stride:(y + 1) * stride]
        assert row[0] == 0, "フィルタを使っていない"
        rows.append([not (row[1 + x // 8] >> (7 - x % 8)) & 1 for x in range(width)])   # 0 が黒
    return width, height, rows


class QrTest(unittest.TestCase):
    def test_matrix_is_the_one_qrcodegen_makes(self):
        m = qr.matrix(TEXT)
        code = qrcodegen.QrCode.encode_text(TEXT, qrcodegen.QrCode.Ecc.MEDIUM)
        self.assertEqual(len(m), code.get_size())
        for y in range(code.get_size()):
            self.assertEqual(m[y], [code.get_module(x, y) for x in range(code.get_size())])

    def test_qr_png_roundtrip(self):
        m = qr.matrix(TEXT)
        size = len(m)
        data = qr.png_bytes(m)
        width, height, pixels = decode_png(data)
        scale, border = qr.SCALE, qr.BORDER
        self.assertEqual((width, height), ((size + 2 * border) * scale,) * 2)       # 寸法
        for y in range(height):
            for x in range(width):
                mx, my = x // scale - border, y // scale - border
                dark = 0 <= mx < size and 0 <= my < size and m[my][mx]
                self.assertEqual(pixels[y][x], dark, (x, y))                          # 白黒の配置（余白は白）

    def test_png_is_a_valid_small_file(self):
        data = qr.png_bytes(qr.matrix(TEXT))
        self.assertLess(len(data), 20_000)
        self.assertGreaterEqual(qr.BORDER, 4)                                         # 読み取りに要る余白（静かな領域）

    def test_png_of_other_text_differs(self):
        self.assertNotEqual(qr.png_bytes(qr.matrix(TEXT)), qr.png_bytes(qr.matrix(TEXT + "x")))

    def test_write_png_makes_a_600_file_and_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "qr-1.png"
            qr.write_png(path, TEXT)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(decode_png(path.read_bytes())[0], (len(qr.matrix(TEXT)) + 2 * qr.BORDER) * qr.SCALE)
            with self.assertRaises(FileExistsError):
                qr.write_png(path, TEXT)
            link = Path(d) / "link.png"
            link.symlink_to(Path(d) / "elsewhere")
            with self.assertRaises(OSError):                                          # リンクの先へは書かない
                qr.write_png(link, TEXT)
            self.assertFalse((Path(d) / "elsewhere").exists())

    def test_terminal_art_draws_the_same_modules(self):
        m = qr.matrix(TEXT)
        art = qr.terminal_art(m)
        lines = art.split("\n")
        size = len(m)
        rows = (size + 2 * qr.BORDER + 1) // 2                                        # 2 段を 1 行に
        self.assertEqual(len(lines), rows)
        plain = [l.replace("\x1b[30;47m", "").replace("\x1b[0m", "") for l in lines]
        for line in plain:
            self.assertEqual(len(line), size + 2 * qr.BORDER)                         # 1 モジュール 1 文字
            self.assertTrue(set(line) <= set(" ▀▄█"))
        glyph = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}

        def dark(x, y):
            mx, my = x - qr.BORDER, y - qr.BORDER
            return 0 <= mx < size and 0 <= my < size and m[my][mx]

        for r, line in enumerate(plain):
            for x, ch in enumerate(line):
                self.assertEqual(ch, glyph[(dark(x, 2 * r), dark(x, 2 * r + 1))], (r, x))


if __name__ == "__main__":
    unittest.main()
