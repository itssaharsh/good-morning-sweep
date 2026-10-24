"""Copies and near-copies, found by code: exact bytes (sha1) and pictures that look the same (dhash distance)."""
import numpy as np

# Which copy is kept: a camera original over a WhatsApp copy over a screenshot over what the parent sent on.
FOLDER_RANK = {"camera": 0, "wa_images": 1, "screenshots": 2, "wa_video": 3, "wa_sent": 4, "wa_video_sent": 5}
NEAR_BITS = 5          # dhash bits that may differ for two pictures to count as the same picture
BLUR_FLAG = 25.0       # sharpness under this: looks blurry
DARK_FLAG = 22.0       # brightness under this (0-255): looks black

_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def keeper_key(row, sharp=None):
    """Sort key: the best copy first."""
    return (FOLDER_RANK.get(row["folder"], 9), -((row["width"] or 0) * (row["height"] or 0)),
            -(sharp or 0.0), row["mtime"] or 0, row["id"])


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def exact_groups(rows):
    """{sha1: [rows]} for every sha1 held by two or more files."""
    by = {}
    for r in rows:
        if r["sha1"]:
            by.setdefault(r["sha1"], []).append(r)
    return {k: v for k, v in by.items() if len(v) > 1}


def informative(dh):
    """A flat picture (black, white, one colour) has almost no bits set in its difference hash, so every flat picture
    looks like every other one. Those are left out of look-alike groups."""
    n = bin(int(dh, 16)).count("1")
    return 8 <= n <= 56


def near_groups(rows, bits=NEAR_BITS, chunk=512):
    """Lists of rows whose pictures look the same (dhash within `bits`), one row per sha1, groups of 2+."""
    uniq, seen = [], set()
    for r in rows:
        if r["dhash"] and informative(r["dhash"]) and r["sha1"] not in seen:
            seen.add(r["sha1"])
            uniq.append(r)
    n = len(uniq)
    if n < 2:
        return []
    h = np.array([int(r["dhash"], 16) for r in uniq], dtype=np.uint64)
    hb = h.view(np.uint8).reshape(n, 8)
    uf = _UF(n)
    for s in range(0, n, chunk):
        x = np.bitwise_xor(hb[s:s + chunk, None, :], hb[None, :, :])
        d = _POP[x].sum(axis=2, dtype=np.uint16)
        ii, jj = np.nonzero(d <= bits)
        for i, j in zip(ii + s, jj):
            if i < j:
                uf.union(int(i), int(j))
    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(uniq[i])
    return [g for g in groups.values() if len(g) > 1]
