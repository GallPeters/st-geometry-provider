# -*- coding: utf-8 -*-
"""
Client-side decoder for the Esri ST_GEOMETRY "POINTS" BLOB (Oracle), reverse engineered
from synthetic samples:

  bytes 0..7   header (payload length, flags) - not needed for decoding
  bytes 8..    a stream of variable-length signed integers:
                 first byte : 0x80 = more bytes follow, 0x40 = negative, 0x3F = 6 value bits
                 next bytes : 0x80 = more bytes follow, 0x7F = 7 value bits (little-endian groups)
  values       first vertex absolute, every following vertex = delta to the previous one
               (the delta chain continues across rings / parts)
  coordinates  x = value / XYUNITS + FALSEX ,  y = value / XYUNITS + FALSEY  (integer grid)
  Z            after X/Y (layout and scaling verified at runtime, see verify())
  polygons     no ring markers: a ring ends where it returns to its own start point

Because parts of the format are inferred, every layer verifies this decoder against
sde.st_asbinary on real rows before using it, per entity type; anything not verified,
or any row that does not decode cleanly, is converted by Esri on the server instead.
"""

import struct
from array import array
from itertools import accumulate

from qgis.core import QgsGeometry
from qgis.PyQt.QtCore import QByteArray

ENT_POINT, ENT_LINE, ENT_POLY = 1, 4, 8
CANDIDATE_ENTITIES = (ENT_POINT, ENT_LINE, ENT_POLY)
_PACK_HDR = struct.Struct("<BII").pack
_PACK_PT2 = struct.Struct("<BIdd").pack
_PACK_PT3 = struct.Struct("<BIddd").pack
_PACK_U32 = struct.Struct("<I").pack


def varints(b, start=8):
    out = []
    ap = out.append
    it = iter(memoryview(b)[start:])
    nx = it.__next__
    try:
        for x in it:
            v = x & 0x3F
            neg = x & 0x40
            sh = 6
            while x & 0x80:
                x = nx()
                v |= (x & 0x7F) << sh
                sh += 7
            ap(-v if neg else v)
    except StopIteration:      # truncated value
        return None
    return out


def _split_rings(X, Y, n):
    """Ring boundaries by closure on the exact integer grid (no per-vertex tuples)."""
    rings = []
    s = 0
    xi = X.index
    while s < n:
        x0, y0 = X[s], Y[s]
        i = s + 3
        while True:
            try:
                i = xi(x0, i)
            except ValueError:
                return None
            if Y[i] == y0:
                break
            i += 1
        rings.append((s, i + 1))
        s = i + 1
    return rings


def _inside(px, py, xs, ys, a, b):
    """Ray casting point-in-ring (ring = xs[a:b], closed)."""
    c = False
    j = b - 1
    for i in range(a, b):
        xi, yi, xj, yj = xs[i], ys[i], xs[j], ys[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-300) + xi:
            c = not c
        j = i
    return c


class EsriDecoder(object):
    def __init__(self, fx, fy, ux, fz=None, uz=None, zmode="bd", keep_z=False, entities=CANDIDATE_ENTITIES):
        self.fx, self.fy, self.ux = float(fx), float(fy), float(ux)
        self.fz = float(fz) if fz is not None else None
        self.uz = float(uz) if uz is not None else None
        self.inv = 1.0 / self.ux
        self.invz = (1.0 / self.uz) if self.uz else None
        self.zmode = zmode            # 'i'/'b' = interleaved / block after XY ; 'd'/'a' = delta / absolute
        self.keep_z = keep_z and self.uz is not None
        self.entities = tuple(entities)

    # ------------------------------------------------------------------ core
    def _coords(self, npts, points):
        vals = varints(points)
        if vals is None or npts <= 0:
            return None
        cnt = len(vals)
        if cnt == 2 * npts:
            xr, yr, zr = vals[0::2], vals[1::2], None
        elif cnt == 3 * npts:
            if self.zmode[0] == "i":
                xr, yr, zr = vals[0::3], vals[1::3], vals[2::3]
            else:
                two = 2 * npts
                xr, yr, zr = vals[0:two:2], vals[1:two:2], vals[two:]
        else:
            return None   # M values or unknown layout -> server
        X = list(accumulate(xr))
        Y = list(accumulate(yr))
        Z = None
        if zr is not None and self.keep_z:
            Z = list(accumulate(zr)) if self.zmode[1] == "d" else zr
        return X, Y, Z

    def _ring_bytes(self, xs, ys, zs, a, b):
        m = b - a
        if zs is None:
            arr = array("d", bytes(16 * m))
            arr[0::2] = array("d", xs[a:b])
            arr[1::2] = array("d", ys[a:b])
        else:
            arr = array("d", bytes(24 * m))
            arr[0::3] = array("d", xs[a:b])
            arr[1::3] = array("d", ys[a:b])
            arr[2::3] = array("d", zs[a:b])
        return _PACK_U32(m) + arr.tobytes()

    # ------------------------------------------------------------------ cache in the layer source
    def to_config(self):
        return "v1;%s;%s;%d;%r;%r;%r;%r;%r" % (self.zmode, ",".join(str(e) for e in self.entities),
                                               1 if self.keep_z else 0, self.fx, self.fy, self.ux, self.fz, self.uz)

    @staticmethod
    def from_config(text):
        p = text.split(";")
        if len(p) != 9 or p[0] != "v1":
            return None
        num = lambda v: None if v == "None" else float(v)
        ents = tuple(int(e) for e in p[2].split(",") if e)
        if not ents:
            return None
        return EsriDecoder(num(p[4]), num(p[5]), num(p[6]), num(p[7]), num(p[8]), p[1], p[3] == "1", ents)

    def wkb(self, ent, npts, points):
        """ISO WKB bytes, or None when this row must be converted by the server."""
        if points is None:
            return None, False
        c = self._coords(int(npts), points)
        if c is None:
            return None, False
        X, Y, Z = c
        fx, fy, inv = self.fx, self.fy, self.inv
        xs = [fx + v * inv for v in X]
        ys = [fy + v * inv for v in Y]
        zs = None
        if Z is not None:
            fz, invz = self.fz, self.invz
            zs = [fz + v * invz for v in Z]
        hz = zs is not None
        n = len(xs)
        # output is always the Multi type (= the layer type) -> no per-feature conversion later
        if ent == ENT_POINT:
            if n != 1:
                return None, False
            if hz:
                return _PACK_HDR(1, 1004, 1) + _PACK_PT3(1, 1001, xs[0], ys[0], zs[0]), False
            return _PACK_HDR(1, 4, 1) + _PACK_PT2(1, 1, xs[0], ys[0]), False
        if ent == ENT_LINE:
            if n < 2:
                return None, False
            return (_PACK_HDR(1, 1005 if hz else 5, 1) + _PACK_HDR(1, 1002 if hz else 2, n)
                    + self._ring_bytes(xs, ys, zs, 0, n)[4:]), False
        if ent == ENT_POLY:
            rings = _split_rings(X, Y, n)   # exact integer grid -> exact closure test
            if rings is None:
                return None, False          # unclosed ring -> not understood
            ptype = 1003 if hz else 3
            mtype = 1006 if hz else 6
            if len(rings) == 1:
                return (_PACK_HDR(1, mtype, 1) + _PACK_HDR(1, ptype, 1)
                        + self._ring_bytes(xs, ys, zs, 0, n)), False
            # several rings: holes go to the exterior that contains them
            polys = []
            for r in rings:
                px, py = xs[r[0]], ys[r[0]]
                target = None
                for poly in reversed(polys):
                    ea, eb = poly[0]
                    if _inside(px, py, xs, ys, ea, eb) and not any(_inside(px, py, xs, ys, ha, hb) for ha, hb in poly[1:]):
                        target = poly
                        break
                if target is None:
                    polys.append([r])
                else:
                    target.append(r)
            parts = []
            for poly in polys:
                parts.append(_PACK_HDR(1, ptype, len(poly)) + b"".join(self._ring_bytes(xs, ys, zs, a, b) for a, b in poly))
            return _PACK_HDR(1, mtype, len(parts)) + b"".join(parts), True
        return None, False

    def geometry(self, ent, npts, area, points):
        """QgsGeometry, or None when this row must be converted by the server."""
        if ent is None:
            return None
        w, multi_ring = self.wkb(int(ent), npts, points)
        if w is None:
            return None
        g = QgsGeometry()
        g.fromWkb(w)                   # bytes directly: ~0.8 us faster than wrapping in QByteArray
        if multi_ring and area is not None:
            # independent check of ring grouping against Esri's stored AREA
            a, ref = g.area(), abs(float(area))
            if abs(a - ref) > 1e-6 * max(ref, 1e-30):
                return None
        return g


# ---------------------------------------------------------------------- verification
def _sig(g):
    """[(part)[(ring)[(x,y,z)...]...]...] of a geometry, multi-normalized."""
    g = QgsGeometry(g)
    g.convertToMultiType()
    ag = g.constGet()
    parts = []
    for i in range(ag.numGeometries()):
        p = ag.geometryN(i)
        if hasattr(p, "exteriorRing"):
            rr = [p.exteriorRing()] + [p.interiorRing(k) for k in range(p.numInteriorRings())]
        elif hasattr(p, "xAt"):
            rr = [p]
        else:
            parts.append([[(p.x(), p.y(), p.z())]])
            continue
        parts.append([[(r.xAt(k), r.yAt(k), r.zAt(k)) for k in range(r.numPoints())] for r in rr])
    return parts


def _same_ring(a, b, txy, tz):
    if len(a) != len(b):
        return "bad"
    best = "bad"
    for cand in (b, b[::-1]):       # ring orientation may differ - irrelevant for QGIS
        zbad = False
        ok = True
        for (x1, y1, z1), (x2, y2, z2) in zip(a, cand):
            if abs(x1 - x2) > txy or abs(y1 - y2) > txy:
                ok = False
                break
            if z1 == z1 and z2 == z2 and abs(z1 - z2) > tz:   # both not NaN
                zbad = True
        if ok:
            if not zbad:
                return "ok"
            best = "xy"
    return best


def compare(gd, gs, txy, tz):
    if gd is None or gs is None or gd.isNull() or gs.isNull():
        return "bad"
    a, b = _sig(gd), _sig(gs)
    if len(a) != len(b):
        return "bad"
    res = "ok"
    for pa, pb in zip(a, b):
        if len(pa) != len(pb):
            return "bad"
        for ra, rb in zip(pa, pb):
            r = _same_ring(ra, rb, txy, tz)
            if r == "bad":
                return "bad"
            if r == "xy":
                res = "xy"
    return res


def verify(rows, sr, need_z):
    """
    rows: [(entity, numpts, points_bytes, server_wkb_bytes), ...] from the same layer.
    sr:   dict with fx, fy, ux, fz, uz.
    Returns (decoder or None, info text).
    """
    if sr.get("ux") in (None, 0):
        return None, "no XYUNITS in spatial reference"
    server = []
    for ent, npts, pts, wkb in rows:
        if ent is None or pts is None or wkb is None:
            continue
        g = QgsGeometry()
        g.fromWkb(QByteArray(bytes(wkb)))
        server.append((int(ent), int(npts), bytes(pts), g))
    if not server:
        return None, "no sample rows"
    txy = 2.0 / sr["ux"] + 1e-9
    tz = (2.0 / sr["uz"] + 1e-6) if sr.get("uz") else 1e-3
    best = None
    for zmode in ("bd", "id", "ba", "ia"):
        dec = EsriDecoder(sr["fx"], sr["fy"], sr["ux"], sr.get("fz"), sr.get("uz"), zmode, keep_z=True)
        full, xyonly, bad = {}, {}, set()
        for ent, npts, pts, gs in server:
            if ent not in CANDIDATE_ENTITIES:
                continue
            w, _m = dec.wkb(ent, npts, pts)
            gd = None
            if w is not None:
                gd = QgsGeometry()
                gd.fromWkb(QByteArray(w))
            r = compare(gd, gs, txy, tz)
            if r == "bad":
                bad.add(ent)
            elif r == "ok":
                full[ent] = full.get(ent, 0) + 1
            else:
                xyonly[ent] = xyonly.get(ent, 0) + 1
        ents_full = [e for e in full if e not in bad and e not in xyonly]
        ents_xy = [e for e in set(full) | set(xyonly) if e not in bad]
        score = (sum(full[e] for e in ents_full), sum(full.get(e, 0) + xyonly.get(e, 0) for e in ents_xy))
        if best is None or score > best[0]:
            best = (score, zmode, ents_full, ents_xy, len(server))
    (score_full, score_xy), zmode, ents_full, ents_xy, n = best
    if need_z:
        if not ents_full:
            return None, "Z layout not verified (xy ok for %d/%d rows) - tick Force 2D to use the local decoder" % (score_xy, n)
        dec = EsriDecoder(sr["fx"], sr["fy"], sr["ux"], sr.get("fz"), sr.get("uz"), zmode, True, ents_full)
        return dec, "ON entities=%s zmode=%s verified %d/%d rows" % (sorted(ents_full), zmode, score_full, n)
    if not ents_xy:
        return None, "not verified (0/%d rows matched)" % n
    dec = EsriDecoder(sr["fx"], sr["fy"], sr["ux"], sr.get("fz"), sr.get("uz"), zmode, False, ents_xy)
    return dec, "ON (2D) entities=%s zmode=%s verified %d/%d rows" % (sorted(ents_xy), zmode, score_xy, n)


# ---------------------------------------------------------------------- batch decoding (numpy)
try:
    import numpy as _np
except Exception:          # numpy missing -> per-row decoding only
    _np = None


def _decode_batch(self, ents, npts, blobs):
    """
    Decode many POINTS blobs at once with numpy (varint parsing, signs, delta sums, scaling and
    ring-closure checks are array operations). Returns a list aligned with the input: QgsGeometry,
    or None where the row must use the per-row path (multi-ring polygons, unexpected layouts...).
    Results are identical to geometry() for every row it accepts.
    """
    np = _np
    out = [None] * len(blobs)
    if np is None:
        return out
    sel, views, nlist, elist = [], [], [], []
    ents_ok = self.entities
    for i in range(len(blobs)):
        b, e, n = blobs[i], ents[i], npts[i]
        if b is None or e is None or n is None:
            continue
        e, n = int(e), int(n)
        if e not in ents_ok or n <= 0 or len(b) <= 8 or b[-1] >= 0x80:   # last value truncated -> per-row path
            continue
        sel.append(i)
        views.append(memoryview(b)[8:])
        nlist.append(n)
        elist.append(e)
    if not sel:
        return out
    buf = np.frombuffer(b"".join(views), dtype=np.uint8)
    blen = np.fromiter((len(v) for v in views), dtype=np.int64, count=len(views))
    bend = np.cumsum(blen)
    bstart = bend - blen
    term = buf < 0x80
    if not term[bend - 1].all():                  # a truncated value would corrupt the grouping
        return out
    gstart = np.flatnonzero(np.concatenate(([True], term[:-1])))
    gid = np.cumsum(term) - term
    k = np.arange(buf.size, dtype=np.int64) - gstart[gid]
    low = np.where(k == 0, buf & 0x3F, buf & 0x7F).astype(np.int64)
    vals = np.add.reduceat(low << np.maximum(7 * k - 1, 0), gstart)
    vals = np.where((buf[gstart] & 0x40) != 0, -vals, vals)
    cnt = np.add.reduceat(term.astype(np.int64), bstart)        # values per blob
    voff = np.cumsum(cnt) - cnt
    nn = np.asarray(nlist, dtype=np.int64)
    d = np.where(cnt == 2 * nn, 2, np.where(cnt == 3 * nn, 3, 0))
    ok = d > 0
    if not ok.any():
        return out
    # keep valid blobs only
    vidx = np.flatnonzero(ok)
    nn, d, voff = nn[vidx], d[vidx], voff[vidx]
    total = int(nn.sum())
    ptstart = np.cumsum(nn) - nn
    rep = np.repeat
    i_in = np.arange(total, dtype=np.int64) - rep(ptstart, nn)
    dd, vo = rep(d, nn), rep(voff, nn)
    inter = self.zmode[0] == "i"
    stride = np.where((dd == 3) & inter, 3, 2) if inter else 2
    xi = vo + stride * i_in
    X, Y = vals[xi], vals[xi + 1]

    def acc(V):                                    # per-blob running sum without int64 overflow
        V0 = V[ptstart]
        Vd = V.copy()
        Vd[ptstart] = 0
        cs = np.cumsum(Vd)
        return cs - rep(cs[ptstart], nn) + rep(V0, nn)

    Xa, Ya = acc(X), acc(Y)
    hz = self.keep_z
    Z = None
    if hz:
        zi = (xi + 2) if inter else (vo + 2 * rep(nn, nn) + i_in)
        Zr = np.where(dd == 3, vals[np.where(dd == 3, zi, 0)], 0)
        Za = acc(Zr) if self.zmode[1] == "d" else Zr
        Z = self.fz + Za * self.invz if self.invz else np.zeros(total)
        Z = np.where(dd == 3, Z, 0.0)
    xs = self.fx + Xa * self.inv
    ys = self.fy + Ya * self.inv
    eq = (Xa == rep(Xa[ptstart], nn)) & (Ya == rep(Ya[ptstart], nn))
    neq = np.add.reduceat(eq.astype(np.int64), ptstart)
    last_eq = eq[ptstart + nn - 1]
    dim = 3 if hz else 2
    C = np.empty(total * dim)
    C[0::dim] = xs
    C[1::dim] = ys
    if hz:
        C[2::dim] = Z
    cb = C.tobytes()
    step = 8 * dim
    mtype, ptype = (1006, 1003) if hz else (6, 3)
    hdr_poly = _PACK_HDR(1, mtype, 1) + _PACK_HDR(1, ptype, 1)
    hdr_mpt = _PACK_HDR(1, 1004 if hz else 4, 1) + struct.pack("<BI", 1, 1001 if hz else 1)
    ltype = (1005, 1002) if hz else (5, 2)
    starts_l, nn_l, neq_l, last_l, d_l = ptstart.tolist(), nn.tolist(), neq.tolist(), last_eq.tolist(), d.tolist()
    for j, v in enumerate(vidx.tolist()):
        if hz and d_l[j] != 3:
            continue                                  # 2D row in a Z layer: per-row path (identical output)
        e = elist[v]
        n = nn_l[j]
        a = starts_l[j] * step
        if e == ENT_POLY:
            if n < 4 or neq_l[j] != 2 or not last_l[j]:
                continue                              # several rings / unclosed -> per-row path
            w = hdr_poly + _PACK_U32(n) + cb[a:a + n * step]
        elif e == ENT_POINT:
            if n != 1:
                continue
            w = hdr_mpt + cb[a:a + step]
        elif e == ENT_LINE:
            if n < 2:
                continue
            w = _PACK_HDR(1, ltype[0], 1) + _PACK_HDR(1, ltype[1], n) + cb[a:a + n * step]
        else:
            continue
        g = QgsGeometry()
        g.fromWkb(w)
        out[sel[v]] = g
    return out


EsriDecoder.decode_batch = _decode_batch
EsriDecoder.batch_capable = _np is not None
