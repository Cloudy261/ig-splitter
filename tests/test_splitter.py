"""Checks that tiles are lossless: pixel-identical to the source region, metadata kept.

Run with: python3 -m unittest discover tests
"""

import io
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import splitter  # noqa: E402


def decode_jpeg_exact(path: Path) -> Image.Image:
    """Decode without fancy upsampling, so each pixel depends only on its own JPEG block."""
    pnm = subprocess.run(["djpeg", "-nosmooth", "-pnm", str(path)], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(pnm))


def png_rows(data: bytes):
    """Tiny reference PNG decoder (non-interlaced) that keeps 16-bit samples: (bytes per pixel, rows)."""
    pos, ihdr, idat = 8, b"", b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        ihdr = body if kind == b"IHDR" else ihdr
        idat += body if kind == b"IDAT" else b""
        pos += 12 + length
    w, h, depth, color = struct.unpack(">IIBB", ihdr[:10])
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bpp, stride = max(1, channels * depth // 8), (w * channels * depth + 7) // 8
    raw, rows, prev = zlib.decompress(idat), [], bytearray(stride)
    for r in range(h):
        kind, line = raw[r * (stride + 1)], bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return bpp, rows


def valid_rect(info: dict, n: int):
    """A crop that obeys the block grid and does not start at 0/0, to exercise the offsets."""
    gx, gy = info["grid"]["x"], info["grid"]["y"]
    tw = gx["step"] * ((info["width"] - gx["step"]) // n // gx["step"])
    w = n * tw
    x = gx["step"] if gx["anchor"] == "start" else (info["width"] - w) % gx["step"]
    h = info["height"] - gy["step"] - 3
    y = gy["step"] if gy["anchor"] == "start" else (info["height"] - h) % gy["step"]
    return x, y, w, h


def noise(size, mode="RGB") -> Image.Image:
    """Detailed test picture, so any re-encoding would show up as changed pixels."""
    return Image.frombytes(mode, size, bytes((i * 7919 + i // 13) % 251 for i in range(size[0] * size[1] * len(mode))))


class SplitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_jpeg_tiles_are_bit_exact_for_every_orientation_and_subsampling(self):
        for subsampling in (0, 1, 2):  # 4:4:4, 4:2:2, 4:2:0
            for orientation in range(1, 9):
                with self.subTest(subsampling=subsampling, orientation=orientation):
                    src = self.dir / f"o{orientation}_s{subsampling}.jpg"
                    exif = Image.Exif()
                    exif[0x0112] = orientation
                    icc = b"fake ICC profile " * 20
                    noise((203, 141)).save(src, "JPEG", quality=92, subsampling=subsampling,
                                           exif=exif.tobytes(), icc_profile=icc)
                    info = splitter.image_info(src)
                    display = decode_jpeg_exact(src)
                    if orientation in splitter._TRANSPOSE:
                        display = display.transpose(splitter._TRANSPOSE[orientation])
                    self.assertEqual(display.size, (info["width"], info["height"]))

                    x, y, w, h = valid_rect(info, 3)
                    outs = splitter.split(src, (x, y, w, h), 3, self.dir / "out")
                    tw = w // 3
                    for i, out in enumerate(outs):
                        with Image.open(out) as tile_meta:
                            self.assertEqual(tile_meta.getexif().get(0x0112), orientation)
                            self.assertEqual(tile_meta.info.get("icc_profile"), icc)
                        tile = decode_jpeg_exact(out)
                        if orientation in splitter._TRANSPOSE:
                            tile = tile.transpose(splitter._TRANSPOSE[orientation])
                        expected = display.crop((x + i * tw, y, x + (i + 1) * tw, y + h))
                        self.assertEqual(tile.tobytes(), expected.tobytes(), f"tile {i + 1} differs")

    def test_jpeg_rejects_cuts_off_the_block_grid(self):
        src = self.dir / "a.jpg"
        noise((480, 160)).save(src, "JPEG", subsampling=2)
        with self.assertRaisesRegex(ValueError, "block grid"):
            splitter.split(src, (8, 0, 96, 160), 2, self.dir / "out")  # 4:2:0 -> 16 px blocks
        with self.assertRaisesRegex(ValueError, "evenly"):
            splitter.split(src, (0, 0, 479, 160), 2, self.dir / "out")

    def test_png_rgb_keeps_pixels_and_metadata(self):
        src = self.dir / "a.png"
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Author", "Test")
        meta.add(b"gAMA", (45455).to_bytes(4, "big"))
        noise((301, 100), "RGBA").save(src, "PNG", pnginfo=meta, icc_profile=b"fake ICC " * 30, dpi=(300, 300))
        outs = splitter.split(src, (1, 2, 300, 97), 3, self.dir / "out")
        with Image.open(src) as im:
            for i, out in enumerate(outs):
                with Image.open(out) as tile:
                    self.assertEqual(tile.mode, "RGBA")
                    self.assertEqual(tile.tobytes(), im.crop((1 + i * 100, 2, 101 + i * 100, 99)).tobytes())
                    self.assertEqual(tile.info["icc_profile"], im.info["icc_profile"])
                    self.assertEqual(tile.info["Author"], "Test")
                    self.assertEqual(tile.info["gamma"], im.info["gamma"])
                    self.assertEqual(tile.info["dpi"], im.info["dpi"])

    def test_png_palette_keeps_indices_palette_and_transparency(self):
        src = self.dir / "p.png"
        img = noise((120, 50), "L").point(lambda v: v % 16).convert("P")
        img.putpalette([v for i in range(16) for v in (i * 16, 255 - i * 16, i * 8)])
        img.save(src, "PNG", transparency=bytes([0] + [255] * 15))
        outs = splitter.split(src, (0, 0, 120, 50), 4, self.dir / "out")
        with Image.open(src) as im:
            for i, out in enumerate(outs):
                with Image.open(out) as tile:
                    self.assertEqual(tile.mode, "P")
                    self.assertEqual(tile.tobytes(), im.crop((i * 30, 0, (i + 1) * 30, 50)).tobytes())
                    self.assertEqual(tile.getpalette()[:48], im.getpalette()[:48])
                    self.assertEqual(tile.info["transparency"], im.info["transparency"])

    def test_png_16_bit_keeps_every_sample(self):
        for color, extra in ((2, ["-alpha", "off"]), (6, []), (0, ["-colorspace", "gray", "-alpha", "off"]),
                             (4, ["-colorspace", "gray"])):
            with self.subTest(color=color):
                src = self.dir / f"deep{color}.png"
                seed = self.dir / "seed.png"
                noise((90, 40), "RGBA").save(seed)
                subprocess.run(["magick", str(seed), *extra, "-depth", "16", "-evaluate", "add", "1.3%",
                                "-define", f"png:color-type={color}", str(src)], check=True)
                data = src.read_bytes()
                self.assertEqual((data[24], data[25]), (16, color))
                outs = splitter.split(src, (3, 1, 81, 37), 3, self.dir / "out")
                bpp, rows = png_rows(data)
                for i, out in enumerate(outs):
                    tile = out.read_bytes()
                    self.assertEqual((tile[24], tile[25]), (16, color))
                    _, tile_rows = png_rows(tile)
                    x = 3 + i * 27
                    expected = [row[x * bpp:(x + 27) * bpp] for row in rows[1:38]]
                    self.assertEqual(tile_rows, expected, f"tile {i + 1} differs")

    def test_jpeg_border_keeps_photo_blocks_bit_exact(self):
        for orientation in (1, 3, 6, 8):
            with self.subTest(orientation=orientation):
                src = self.dir / f"b{orientation}.jpg"
                exif = Image.Exif()
                exif[0x0112] = orientation
                icc = b"fake ICC profile " * 20
                noise((203, 141)).save(src, "JPEG", quality=90, subsampling=2, exif=exif.tobytes(), icc_profile=icc)
                info = splitter.image_info(src)
                display = decode_jpeg_exact(src).transpose(splitter._TRANSPOSE[orientation]) \
                    if orientation in splitter._TRANSPOSE else decode_jpeg_exact(src)
                W, H = info["width"], info["height"]
                gx, gy = info["grid"]["x"], info["grid"]["y"]
                # frame sticking out on all four sides, tile starts on the block grid
                n, sx, sy = 3, gx["step"], gy["step"]
                tw = sx * -(-(W + 3 * sx) // n // sx)
                w, h = n * tw, H + 2 * sy + 5
                x = -sx if gx["anchor"] == "start" else W % sx - sx
                y = -sy if gy["anchor"] == "start" else (H - h) % sy - sy
                outs = splitter.split(src, (x, y, w, h), n, self.dir / "out", border="#ff0000")
                # the partial last block row/column next to the border is left out
                left = W % sx if gx["anchor"] == "end" else 0
                right = W - (W % sx if gx["anchor"] == "start" else 0)
                top = H % sy if gy["anchor"] == "end" else 0
                bottom = H - (H % sy if gy["anchor"] == "start" else 0)
                for i, out in enumerate(outs):
                    with Image.open(out) as meta:
                        self.assertEqual(meta.getexif().get(0x0112), orientation)
                        self.assertEqual(meta.info.get("icc_profile"), icc)
                    tile = decode_jpeg_exact(out)
                    if orientation in splitter._TRANSPOSE:
                        tile = tile.transpose(splitter._TRANSPOSE[orientation])
                    tx = x + i * tw
                    px0, px1, py0, py1 = max(tx, left), min(tx + tw, right), max(y, top), min(y + h, bottom)
                    expected = Image.new("RGB", (tw, h), (255, 0, 0))
                    if px1 > px0:
                        part = display.crop((px0, py0, px1, py1))
                        expected.paste(part, (px0 - tx, py0 - y))
                        got = tile.crop((px0 - tx, py0 - y, px1 - tx, py1 - y))
                        self.assertEqual(got.tobytes(), part.tobytes(), f"photo in tile {i + 1} differs")
                    worst = max(hi for _, hi in ImageChops.difference(tile, expected).getextrema())
                    self.assertLessEqual(worst, 6, f"border of tile {i + 1} isn't red")

    def test_png_border_is_exact(self):
        src = self.dir / "border.png"
        img = noise((120, 50), "L").point(lambda v: v % 16).convert("P")
        img.putpalette([v for i in range(16) for v in (i * 16, 255 - i * 16, i * 8)])
        img.save(src, "PNG", transparency=bytes([0] + [255] * 15), icc_profile=b"fake ICC " * 30)
        outs = splitter.split(src, (-10, -5, 150, 60), 3, self.dir / "out", border="#00ff00")
        with Image.open(src) as im:
            canvas = Image.new("RGBA", (150, 60), (0, 255, 0, 255))
            canvas.paste(im.convert("RGBA"), (10, 5))
            for i, out in enumerate(outs):
                with Image.open(out) as tile:
                    self.assertEqual(tile.mode, "RGBA")
                    self.assertEqual(tile.tobytes(), canvas.crop((i * 50, 0, (i + 1) * 50, 60)).tobytes())
                    self.assertEqual(tile.info["icc_profile"], im.info["icc_profile"])

    def test_png_16_bit_border_keeps_every_sample(self):
        src, seed = self.dir / "deep.png", self.dir / "seed.png"
        noise((90, 40), "RGB").save(seed)
        subprocess.run(["magick", str(seed), "-depth", "16", "-evaluate", "add", "1.3%", "PNG48:" + str(src)], check=True)
        outs = splitter.split(src, (0, -4, 90, 48), 2, self.dir / "out", border="#ffffff")
        bpp, rows = png_rows(src.read_bytes())
        for i, out in enumerate(outs):
            tile = out.read_bytes()
            self.assertEqual((tile[24], tile[25]), (16, 2))
            _, tile_rows = png_rows(tile)
            self.assertEqual(tile_rows[:4] + tile_rows[44:], [b"\xff" * 45 * bpp] * 8)
            self.assertEqual(tile_rows[4:44], [row[i * 45 * bpp:(i + 1) * 45 * bpp] for row in rows])

    def test_instagram_size_scales_down_and_applies_rotation(self):
        src = self.dir / "rot.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6
        noise((1000, 2400)).save(src, "JPEG", quality=92, exif=exif.tobytes(), icc_profile=b"fake ICC " * 30)
        outs = splitter.split(src, (100, 0, 1500, 1000), 2, self.dir / "out", border=None, size=(600, 800))
        with Image.open(src) as im:
            display = ImageOps.exif_transpose(im)
        for i, out in enumerate(outs):
            with Image.open(out) as tile:
                self.assertEqual(tile.size, (600, 800))
                self.assertEqual(tile.getexif().get(0x0112, 1), 1)
                self.assertEqual(tile.info["icc_profile"], b"fake ICC " * 30)
                expected = display.crop((100 + i * 750, 0, 850 + i * 750, 1000)).resize((600, 800))
                mean = sum(ImageStat.Stat(ImageChops.difference(tile.convert("RGB"), expected)).mean) / 3
                self.assertLess(mean, 12)  # same picture, just resampled
        # tiles narrower than the target width keep their size; borders fill outside the photo
        outs = splitter.split(src, (-50, 0, 1500, 1000), 2, self.dir / "small", border="#000000", size=(1080, 1440))
        with Image.open(outs[0]) as tile:
            self.assertEqual(tile.size, (750, 1000))
            self.assertEqual(tile.getpixel((10, 500)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
