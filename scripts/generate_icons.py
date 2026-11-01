"""Generate opaque, maskable app icons using only Python's standard library."""

import struct
import zlib
from pathlib import Path

DEST = Path(__file__).resolve().parents[1] / "static" / "icons"
BACKGROUND = (50, 111, 91)
HEART = (247, 251, 245)
CROSS = (50, 111, 91)


def color_at(x, y, size):
    hx = (x - size * 0.5) / (size * 0.245)
    hy = (size * 0.51 - y) / (size * 0.245)
    inside = (hx * hx + hy * hy - 1) ** 3 - hx * hx * hy**3 <= 0
    if not inside:
        return BACKGROUND
    dx = abs(x / size - 0.5)
    dy = abs(y / size - 0.49)
    if (dx < 0.028 and dy < 0.095) or (dx < 0.095 and dy < 0.028):
        return CROSS
    return HEART


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def write_icon(size, filename):
    rows = bytearray()
    offsets = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    for y in range(size):
        rows.append(0)
        for x in range(size):
            pixels = [color_at(x + dx, y + dy, size) for dx, dy in offsets]
            rows.extend((sum(pixel[i] for pixel in pixels) // 4 for i in range(3)))
            rows.append(255)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
    (DEST / filename).write_bytes(png)


if __name__ == "__main__":
    DEST.mkdir(parents=True, exist_ok=True)
    for dimension, name in [
        (180, "apple-touch-icon.png"),
        (192, "icon-192.png"),
        (512, "icon-512.png"),
    ]:
        write_icon(dimension, name)
    print("Generated 180px, 192px and 512px opaque PNG icons.")
