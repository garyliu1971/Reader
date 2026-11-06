"""Generate web/icons/*.png (pure stdlib: zlib + struct, no image libraries). Run: python web/tools/gen_icons.py
The same open-book glyph as icons/icon.svg: white book on a blue rounded square (maskable variant is full-bleed, glyph in the safe zone)."""
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icons")
BG = (47, 111, 221)
FG = (255, 255, 255)
SPINE = (47, 111, 221)


def in_poly(x, y, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def rounded(x, y, r):  # unit square [0,1]^2 with corner radius r
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


# glyph in unit coordinates (open book: two pages + spine gap)
LEFT = [(0.50, 0.30), (0.26, 0.24), (0.20, 0.27), (0.20, 0.70), (0.26, 0.67), (0.50, 0.73)]
RIGHT = [(0.50, 0.30), (0.74, 0.24), (0.80, 0.27), (0.80, 0.70), (0.74, 0.67), (0.50, 0.73)]


def sample(x, y, maskable, scale):
    if not maskable and not rounded(x, y, 0.22):
        return (0, 0, 0, 0)
    # glyph scaled about the centre (smaller for the maskable safe zone)
    gx = (x - 0.5) / scale + 0.5
    gy = (y - 0.5) / scale + 0.5
    if (in_poly(gx, gy, LEFT) or in_poly(gx, gy, RIGHT)) and abs(gx - 0.5) > 0.008:
        return FG + (255,)
    return BG + (255,)


def render(size, maskable=False, ss=3):
    scale = 0.8 if maskable else 1.0
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    r, g, b, a = sample((px + (sx + .5) / ss) / size, (py + (sy + .5) / ss) / size, maskable, scale)
                    acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a
            n = ss * ss
            if acc[3]:
                row += bytes((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], acc[3] // n))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, size, mask in (("icon-192.png", 192, False), ("icon-512.png", 512, False), ("maskable-512.png", 512, True)):
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(render(size, mask, ss=2 if size > 256 else 3))
        print("wrote", name)
