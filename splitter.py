"""Splitting of wide photos into equal tiles for a seamless Instagram carousel.

Two export modes:

* Instagram size (`size` given): tiles are scaled down to Instagram's size
  (e.g. 1080×1440) and re-encoded. Simple, not lossless.
* Lossless (no `size`): JPEG tiles are cut with `jpegtran -crop`, which copies the
  compressed DCT blocks without decoding them, so every tile is bit-exact. That only
  works when each tile's left and top edge sit on the JPEG block grid (8 or 16 px,
  depending on chroma subsampling); `image_info()["grid"]` tells the UI where those
  edges may go. PNG is lossless anyway: tiles are decoded and re-encoded with Pillow
  (or ImageMagick for 16-bit files, which Pillow would truncate to 8 bit), and every
  metadata chunk (ICC profile, gamma, text, EXIF, ...) is copied from the source.

Either mode can add a border (`border` colour) where the frame extends past the photo.
For lossless JPEGs the border is a solid JPEG encoded with the photo's own quantisation
tables, and the photo's blocks are dropped into it unchanged (`jpegtran -drop`).

All rectangles from the UI are in display orientation (EXIF rotation applied). Lossless
cuts map them onto the stored pixels and keep the source's EXIF orientation tag, so the
tiles display exactly like the original.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageColor, ImageOps, JpegImagePlugin

SUFFIXES = {".jpg", ".jpeg", ".png"}

# EXIF orientation -> for display x and display y: (stored axis it runs along, reversed?)
_AXES = {
    1: (("x", False), ("y", False)),
    2: (("x", True), ("y", False)),
    3: (("x", True), ("y", True)),
    4: (("x", False), ("y", True)),
    5: (("y", False), ("x", False)),
    6: (("y", True), ("x", False)),
    7: (("y", True), ("x", True)),
    8: (("y", False), ("x", True)),
}
# Transpose that turns the stored pixels into the displayed picture (as ImageOps.exif_transpose).
_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_PNG_COLOR = {0: "gray", 2: "RGB", 3: "palette", 4: "gray + alpha", 6: "RGBA"}
_PNG_LAYOUT = {b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"}  # written by the encoder
_PNG_STALE = {b"hIST", b"acTL", b"fcTL", b"fdAT"}  # describe the old pixels / animation
_PNG_COLOR_TYPED = {b"sBIT", b"bKGD"}  # only valid for the colour type they were written for


def image_info(path: Path) -> dict:
    """Displayed size, block grid and cutting method of a JPEG or PNG."""
    with Image.open(path) as im:
        fmt = im.format
        if fmt == "JPEG":
            # One (id, h_sampling, v_sampling, qtable) entry per colour component.
            if len(im.layer) == 1:
                block = (8, 8)
            else:
                block = (8 * max(c[1] for c in im.layer), 8 * max(c[2] for c in im.layer))
            method, detail = "jpegtran", f"JPEG · {block[0]}×{block[1]} px blocks"
        elif fmt == "PNG":
            depth, color = _png_header(path)
            block = (1, 1)
            method = "imagemagick" if depth == 16 else "pillow"
            detail = f"PNG · {depth}-bit {_PNG_COLOR.get(color, '')}"
        else:
            raise ValueError(f"{path.name}: only JPEG and PNG are supported")
        orientation = _orientation(im)
        stored = im.size
    (ax, rx), (ay, ry) = _AXES[orientation]
    step = {"x": block[0], "y": block[1]}
    width, height = stored if ax == "x" else stored[::-1]
    return {
        "name": path.name,
        "format": fmt,
        "detail": detail,
        "method": method,
        "width": width,
        "height": height,
        "stored": stored,
        "orientation": orientation,
        "block": block,
        # Where tile edges may go along each display axis for a lossless cut: offsets
        # measured from the start (left/top) or the end (right/bottom) must be multiples of `step`.
        "grid": {
            "x": {"step": step[ax], "anchor": "end" if rx else "start"},
            "y": {"step": step[ay], "anchor": "end" if ry else "start"},
        },
        "mtime": path.stat().st_mtime_ns,
    }


def preview(path: Path, max_side: int) -> bytes:
    """Downscaled JPEG of the image as displayed (EXIF rotation applied), for the browser."""
    orientation = image_info(path)["orientation"]
    with Image.open(path) as im:
        scale = min(1.0, max_side / max(im.size))
        size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        im.draft("RGB", size)  # JPEG: decode at reduced size, much faster
        im = _eight_bit(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail(size)
        if orientation in _TRANSPOSE:
            im = im.transpose(_TRANSPOSE[orientation])
        out = io.BytesIO()
        im.save(out, "JPEG", quality=90)
        return out.getvalue()


def split(path: Path, rect, tiles: int, out_dir: Path, border: str | None = None,
          size=None) -> list[Path]:
    """Cut `rect` (x, y, w, h in display pixels) into `tiles` equal tiles, left to right.

    `border` fills the parts of the frame outside the photo. `size` (width, height) scales
    tiles at least that wide to exactly that size (Instagram size); smaller tiles keep their
    size. Without `size` the cut is lossless.
    """
    info = image_info(path)
    x, y, w, h = (int(v) for v in rect)
    if tiles < 1 or w < tiles or h < 1:
        raise ValueError("Invalid crop frame")
    if w % tiles:
        raise ValueError(f"{w} px can't be split evenly into {tiles} tiles")
    inside = x >= 0 and y >= 0 and x + w <= info["width"] and y + h <= info["height"]
    if not inside and not border:
        raise ValueError("The crop frame lies outside the image")
    if border:
        ImageColor.getrgb(border)  # raises ValueError for an unknown colour
    tw = w // tiles
    rects = [(x + i * tw, y, tw, h) for i in range(tiles)]
    jobs = None if size else _lossless_jobs(info, (x, y, w, h), rects)  # validate before writing
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = [out_dir / f"{path.stem}_{i + 1}of{tiles}{path.suffix}" for i in range(tiles)]
    if size:
        _cut_resized(path, rects, outs, border, (int(size[0]), int(size[1])))
    else:
        cut = {"jpegtran": _cut_jpeg, "pillow": _cut_png, "imagemagick": _cut_png16}[info["method"]]
        cut(path, jobs, outs, border)
    return outs


def _orientation(im: Image.Image) -> int:
    # A PNG's eXIf chunk is only known up front if it precedes the pixel data;
    # getexif() would otherwise decode the whole image just to look.
    if im.format == "PNG" and "exif" not in im.info:
        return 1
    value = im.getexif().get(0x0112, 1)
    return value if value in _AXES else 1


def _to_stored(rect, info: dict) -> tuple[int, int, int, int]:
    """Map a display-orientation rectangle onto the stored pixel grid."""
    x, y, w, h = rect
    (ax, rx), (ay, ry) = _AXES[info["orientation"]]
    along_x = info["width"] - x - w if rx else x
    along_y = info["height"] - y - h if ry else y
    return (along_x, along_y, w, h) if ax == "x" else (along_y, along_x, h, w)


def _lossless_jobs(info: dict, frame, rects) -> list:
    """(tile box, photo part box or None) per tile in stored pixels; raises if a cut can't be lossless."""
    bw, bh = info["block"]
    sw, sh = info["stored"]
    fx, fy, fw, fh = _to_stored(frame, info)
    # A JPEG's last block column/row can be partial. Where the border continues past
    # it, it is left out, so no stray pixels end up in the border.
    ew = sw - sw % bw if fx + fw > sw else sw
    eh = sh - sh % bh if fy + fh > sh else sh
    jobs = []
    for rect in rects:
        tx, ty, tw, th = tile = _to_stored(rect, info)
        x0, y0, x1, y1 = max(tx, 0), max(ty, 0), min(tx + tw, ew), min(ty + th, eh)
        part = (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None
        if part and (x0 % bw or y0 % bh or (x0 - tx) % bw or (y0 - ty) % bh):
            raise ValueError(f"Cut lines must sit on the {bw}×{bh} px JPEG block grid to stay lossless")
        jobs.append((tile, part))
    return jobs


def _cut_resized(src: Path, rects, outs, border: str | None, size: tuple[int, int]) -> None:
    """Instagram size: compose each tile, scale it to `size` and re-encode it."""
    with Image.open(src) as original:
        fmt = original.format
        im = _eight_bit(ImageOps.exif_transpose(original))  # also resets the orientation tag
    if border:
        im = im.convert(_border_mode(im, border))
    keep = {k: im.info[k] for k in ("icc_profile", "exif") if im.info.get(k)}
    for rect, out in zip(rects, outs):
        tile = _compose(im, rect, border)
        if tile.width >= size[0] and tile.size != size:
            tile = tile.resize(size, Image.Resampling.LANCZOS)
        if fmt == "JPEG":
            tile.save(out, "JPEG", quality=95, subsampling=0, **keep)
        else:
            tile.save(out, "PNG", **keep)


def _compose(im: Image.Image, rect, border: str | None) -> Image.Image:
    """Region `rect` of `im`; parts outside the image are filled with `border`."""
    x, y, w, h = rect
    if not border:
        return im.crop((x, y, x + w, y + h))
    tile = Image.new(im.mode, (w, h), ImageColor.getcolor(border, im.mode))
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, im.width), min(y + h, im.height)
    if x1 > x0 and y1 > y0:
        tile.paste(im.crop((x0, y0, x1, y1)), (x0 - x, y0 - y))
    return tile


def _eight_bit(im: Image.Image) -> Image.Image:
    """Convert to a mode Pillow can scale and save as JPEG or PNG (RGB, RGBA, L or LA)."""
    if im.mode.startswith("I"):  # 16-bit grayscale
        return im.point(lambda v: v / 256).convert("L")
    if im.mode in ("RGB", "RGBA", "L", "LA"):
        return im
    return im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")


def _border_mode(im: Image.Image, border: str) -> str:
    """Pixel mode that can hold both the photo and the border colour."""
    if im.mode in ("RGB", "RGBA"):
        return im.mode
    if im.mode in ("L", "LA") and len(set(ImageColor.getrgb(border)[:3])) == 1:
        return im.mode
    return "RGBA" if "A" in im.mode or "transparency" in im.info else "RGB"


def _cut_jpeg(src: Path, jobs, outs, border: str | None) -> None:
    jpegtran = _tool("jpegtran", "brew install jpeg-turbo (macOS) or apt install libjpeg-turbo-progs (Debian/Ubuntu)")
    for (tile, part), out in zip(jobs, outs):
        x, y, w, h = tile
        if part == tile:
            _run([jpegtran, "-copy", "all", "-crop", f"{w}x{h}+{x}+{y}", "-outfile", str(out), str(src)])
        else:
            out.write_bytes(_jpeg_with_border(jpegtran, src, tile, part, border))
        with Image.open(out) as result:
            if result.size != (w, h):
                raise RuntimeError(f"jpegtran made a {result.size} tile instead of {(w, h)}")


def _jpeg_with_border(jpegtran: str, src: Path, tile, part, border: str) -> bytes:
    """A solid `border` JPEG encoded with the photo's own quantisation tables, with the
    photo's blocks dropped in unchanged, so the photo part stays bit-exact."""
    tx, ty, tw, th = tile
    with Image.open(src) as im:
        sampling = JpegImagePlugin.get_sampling(im)
        if im.mode not in ("RGB", "L") or im.info.get("adobe_transform") == 0 or (im.mode == "RGB" and sampling < 0):
            raise ValueError("Lossless borders need a standard YCbCr or grayscale JPEG; use Instagram size instead")
        extra = {"subsampling": sampling} if im.mode == "RGB" else {}
        background = io.BytesIO()
        Image.new(im.mode, (tw, th), ImageColor.getcolor(border, im.mode)).save(
            background, "JPEG", qtables=im.quantization, **extra)
    data = background.getvalue()
    if part:
        px, py, pw, ph = part
        with tempfile.TemporaryDirectory() as tmp:
            base, piece = Path(tmp, "base.jpg"), Path(tmp, "piece.jpg")
            base.write_bytes(data)
            _run([jpegtran, "-crop", f"{pw}x{ph}+{px}+{py}", "-outfile", str(piece), str(src)])
            data = _run([jpegtran, "-drop", f"+{px - tx}+{py - ty}", str(piece), str(base)])
    return _with_jpeg_markers(data, src.read_bytes())


def _jpeg_segments(data: bytes):
    """Marker segments before the scan as (marker, bytes), plus the scan data itself."""
    pos, segments = 2, []
    while True:
        while data[pos + 1] == 0xFF:  # fill bytes
            pos += 1
        marker = data[pos + 1]
        if marker == 0xDA:  # start of scan: the rest is entropy-coded data
            return segments, data[pos:]
        end = pos + 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
        segments.append((marker, data[pos:end]))
        pos = end


def _with_jpeg_markers(tile: bytes, source: bytes) -> bytes:
    """`tile` with its APPn/COM metadata (EXIF, ICC, XMP, ...) replaced by the source's."""
    def is_meta(marker):
        return 0xE0 <= marker <= 0xEF or marker == 0xFE

    meta = [raw for marker, raw in _jpeg_segments(source)[0] if is_meta(marker)]
    segments, scan = _jpeg_segments(tile)
    return b"\xff\xd8" + b"".join(meta + [raw for marker, raw in segments if not is_meta(marker)]) + scan


def _cut_png(src: Path, jobs, outs, border: str | None) -> None:
    data = src.read_bytes()
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        keep = {"transparency": im.info["transparency"]} if "transparency" in im.info else {}
        if any(part != tile for tile, part in jobs):
            mode = _border_mode(im, border)
            if mode != im.mode:
                im, keep = im.convert(mode), {}
        for (tile, part), out in zip(jobs, outs):
            tx, ty, tw, th = tile
            if part == tile:
                img = im.crop((tx, ty, tx + tw, ty + th))
            else:
                img = Image.new(im.mode, (tw, th), ImageColor.getcolor(border, im.mode))
                if part:
                    px, py, pw, ph = part
                    img.paste(im.crop((px, py, px + pw, py + ph)), (px - tx, py - ty))
            png = io.BytesIO()
            img.save(png, "PNG", **keep)
            out.write_bytes(_with_metadata(png.getvalue(), data))


def _cut_png16(src: Path, jobs, outs, border: str | None) -> None:
    """Pillow reads 16-bit PNGs as 8 bit, so ImageMagick cuts those."""
    magick = _tool("magick", "brew install imagemagick (macOS) or apt install imagemagick (Debian/Ubuntu)",
                   fallback="convert")
    data = src.read_bytes()
    depth, color = data[24], data[25]
    for (tile, part), out in zip(jobs, outs):
        tx, ty, tw, th = tile
        out_color = color
        if part != tile and color in (0, 4) and len(set(ImageColor.getrgb(border)[:3])) > 1:
            out_color = color + 2  # gray (+ alpha) -> RGB(A), so a coloured border stays coloured
        if part is None:
            cmd, stdin = [magick, "-size", f"{tw}x{th}", f"xc:{border}"], None
        else:
            px, py, pw, ph = part
            cmd, stdin = [magick, "png:-", "-crop", f"{pw}x{ph}+{px}+{py}", "+repage"], data
            if part != tile:
                cmd += ["-background", border, "-extent", f"{tw}x{th}-{px - tx}-{py - ty}"]
        cmd += ["-define", f"png:bit-depth={depth}", "-define", f"png:color-type={out_color}", "png:-"]
        png = _run(cmd, stdin)
        if (png[24], png[25]) != (depth, out_color):
            raise RuntimeError("ImageMagick changed the PNG's bit depth or colour type")
        out.write_bytes(_with_metadata(png, data))


def _png_header(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(26)
    return head[24], head[25]  # IHDR bit depth, colour type


def _png_chunks(png: bytes):
    pos = 8
    while pos < len(png):
        end = pos + 12 + int.from_bytes(png[pos:pos + 4], "big")
        yield png[pos + 4:pos + 8], png[pos:end]
        pos = end


def _with_metadata(tile: bytes, source: bytes) -> bytes:
    """Pixel chunks from `tile`, all metadata chunks from `source`, each in its original slot."""
    skip = _PNG_LAYOUT | _PNG_STALE
    if tile[25] != source[25]:  # colour type changed (border added)
        skip |= _PNG_COLOR_TYPED
    head, before_idat, tail = [], [], []
    group = head
    for kind, raw in _png_chunks(source):
        if kind == b"PLTE" and group is head:
            group = before_idat
        elif kind == b"IDAT":
            group = tail
        elif kind not in skip:
            group.append(raw)
    out, idat_seen = [source[:8]], False
    for kind, raw in _png_chunks(tile):
        if kind not in _PNG_LAYOUT:
            continue  # the encoder's own metadata is replaced by the source's
        if kind == b"IDAT" and not idat_seen:
            out += before_idat
            idat_seen = True
        if kind == b"IEND":
            out += tail
        out.append(raw)
        if kind == b"IHDR":
            out += head
    return b"".join(out)


def _tool(name: str, install: str, fallback: str | None = None) -> str:
    # Also look in Homebrew's folders, in case the app was started without a login shell.
    search = os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    path = shutil.which(name, path=search) or (fallback and shutil.which(fallback, path=search))
    if not path:
        raise RuntimeError(f"`{name}` not found. Install it with: {install}")
    return path


def _run(cmd: list[str], stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, input=stdin, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout
