#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_elevation.py  —  地図データに標高を足す

gunma_sv_mobile.json の各道路に「標高」と「起伏（まわり1kmの標高差）」を追加します。
標高は国土地理院の標高タイルから取得します。APIキーは不要、料金もかかりません。

使い方（gunma_sv_mobile.json と同じフォルダに置いて）:
    python add_elevation.py

終わると gunma_sv_mobile.json が書き換わります（元のファイルは
gunma_sv_mobile.backup.json として残します）。あとは GitHub に上げ直すだけです。

追加ライブラリは不要ですが、Pillow が入っていればそちらを使います。
"""

import json
import math
import os
import shutil
import struct
import sys
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "gunma_sv_mobile.json")
TILE = "https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png"
TILE_ALT = "https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png"
Z = 12                      # 1ピクセル約38m。山間部の判定には十分
RELIEF_PX = 13              # まわり±13px（約1km）の標高差を「起伏」とする
UA = "gunma-sv-elevation/1.0"

try:
    from PIL import Image
    import io
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


# ----------------------------------------------------------------------
# PNG を読む（Pillow が無くても動くように）
# ----------------------------------------------------------------------
def decode_png(data):
    """8bit の PNG を (width, height, pixels) で返す。pixels は RGB のバイト列。"""
    if HAVE_PIL:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        return im.width, im.height, im.tobytes()

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNGではありません")
    pos = 8
    w = h = depth = ctype = None
    idat = bytearray()
    plte = None
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace != 0:
                raise ValueError("対応していないPNG形式です（pip install pillow で解決します）")
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break

    nch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if nch is None:
        raise ValueError("対応していないPNG形式です（pip install pillow で解決します）")
    raw = zlib.decompress(bytes(idat))
    stride = w * nch
    out = bytearray(stride * h)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                b = prev[i]
                c = prev[i - nch] if i >= nch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line

    if ctype == 2:
        return w, h, bytes(out)
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        if ctype == 6:
            rgb[i * 3:i * 3 + 3] = out[i * 4:i * 4 + 3]
        elif ctype == 0:
            v = out[i]
            rgb[i * 3] = rgb[i * 3 + 1] = rgb[i * 3 + 2] = v
        elif ctype == 4:
            v = out[i * 2]
            rgb[i * 3] = rgb[i * 3 + 1] = rgb[i * 3 + 2] = v
        elif ctype == 3:
            idx = out[i]
            rgb[i * 3:i * 3 + 3] = plte[idx * 3:idx * 3 + 3]
    return w, h, bytes(rgb)


def px_to_elev(r, g, b):
    """地理院標高タイルの画素 → 標高(m)。無効値は None"""
    if r == 128 and g == 0 and b == 0:
        return None
    x = (r << 16) + (g << 8) + b
    if x == (1 << 23):
        return None
    if x > (1 << 23):
        x -= (1 << 24)
    return x * 0.01


# ----------------------------------------------------------------------
# タイル
# ----------------------------------------------------------------------
def lon2px(lon, z):
    return (lon + 180.0) / 360.0 * (256 << z)


def lat2px(lat, z):
    s = math.sin(math.radians(lat))
    s = max(-0.9999, min(0.9999, s))
    return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * (256 << z)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def main():
    if not os.path.exists(SRC):
        sys.exit("gunma_sv_mobile.json が見つかりません。このスクリプトと同じフォルダに置いてください。")

    print("データを読み込んでいます…")
    with open(SRC, encoding="utf-8") as f:
        D = json.load(f)
    roads = D["roads"]
    print("道路 %s 本" % "{:,}".format(len(roads)))

    # 各道路の代表点（真ん中あたり）
    pts = []
    for r in roads:
        g = r[5]
        x, y = g[0], g[1]
        half = (len(g) - 2) // 4 * 2
        for i in range(2, 2 + half, 2):
            x += g[i]
            y += g[i + 1]
        pts.append((x / 1e5, y / 1e5))

    # 必要なタイルを洗い出す
    need = set()
    for lng, lat in pts:
        need.add((int(lon2px(lng, Z)) >> 8, int(lat2px(lat, Z)) >> 8))
    # 起伏を見るため、まわりのタイルも足しておく
    for tx, ty in list(need):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                need.add((tx + dx, ty + dy))
    need = sorted(need)
    print("標高タイルを %d 枚取得します（国土地理院）" % len(need))

    tiles = {}
    done = [0]

    def grab(t):
        tx, ty = t
        for tpl in (TILE, TILE_ALT):
            try:
                data = fetch(tpl.format(z=Z, x=tx, y=ty))
            except Exception:
                data = None
            if data:
                try:
                    w, h, rgb = decode_png(data)
                    tiles[t] = (w, h, rgb)
                except ValueError as e:
                    sys.exit(str(e))
                break
        done[0] += 1
        if done[0] % 20 == 0:
            sys.stdout.write("\r  %d / %d 枚" % (done[0], len(need)))
            sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(grab, need))
    sys.stdout.write("\r  %d / %d 枚 取得完了\n" % (len(need), len(need)))

    def elev_at(gx, gy):
        """全体ピクセル座標 → 標高(m)。取れなければ None"""
        t = (int(gx) >> 8, int(gy) >> 8)
        tile = tiles.get(t)
        if not tile:
            return None
        w, h, rgb = tile
        px = int(gx) & 255
        py = int(gy) & 255
        if px >= w or py >= h:
            return None
        i = (py * w + px) * 3
        return px_to_elev(rgb[i], rgb[i + 1], rgb[i + 2])

    print("標高を割り当てています…")
    miss = 0
    for n, (lng, lat) in enumerate(pts):
        gx, gy = lon2px(lng, Z), lat2px(lat, Z)
        e = elev_at(gx, gy)
        if e is None:
            # 近くの画素で代用
            for d in (1, 2, 3):
                for ddx, ddy in ((d, 0), (-d, 0), (0, d), (0, -d)):
                    e = elev_at(gx + ddx, gy + ddy)
                    if e is not None:
                        break
                if e is not None:
                    break
        if e is None:
            miss += 1
            roads[n][6:8] = [0, 0]
            continue
        lo = hi = e
        step = max(1, RELIEF_PX // 4)
        for ddx in range(-RELIEF_PX, RELIEF_PX + 1, step):
            for ddy in range(-RELIEF_PX, RELIEF_PX + 1, step):
                v = elev_at(gx + ddx, gy + ddy)
                if v is None:
                    continue
                if v < lo:
                    lo = v
                if v > hi:
                    hi = v
        roads[n][6:8] = [int(round(e)), int(round(hi - lo))]
        if (n + 1) % 20000 == 0:
            print("  %s 本" % "{:,}".format(n + 1))

    D["hasElev"] = 1
    shutil.copyfile(SRC, os.path.join(HERE, "gunma_sv_mobile.backup.json"))
    with open(SRC, "w", encoding="utf-8") as f:
        json.dump(D, f, ensure_ascii=False, separators=(",", ":"))

    ok = [r[6] for r in roads if len(r) > 6]
    ok = [v for v in ok]
    ok_nonzero = [v for v in ok if v]
    print("\n完了しました。")
    print("  標高が取れなかった道: %s 本" % "{:,}".format(miss))
    if ok_nonzero:
        ok_nonzero.sort()
        print("  標高の分布: 最小 %dm / 中央 %dm / 最大 %dm"
              % (ok_nonzero[0], ok_nonzero[len(ok_nonzero) // 2], ok_nonzero[-1]))
        for th in (200, 300, 500, 800, 1000):
            c = sum(1 for v in ok if v >= th)
            print("    %4dm以上: %s 本" % (th, "{:,}".format(c)))
    print("  ファイルサイズ: %.1f MB" % (os.path.getsize(SRC) / 1048576))
    print("\ngunma_sv_mobile.json を GitHub に上げ直してください。")
    print("（元のファイルは gunma_sv_mobile.backup.json に残してあります）")


if __name__ == "__main__":
    main()
