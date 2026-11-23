# -*- coding: utf-8 -*-
"""
True curves (circular arcs) stored by ArcGIS outside ST_GEOMETRY.

ST_GEOMETRY only holds a densified copy of curved features. ArcGIS keeps the real curve
definition in an extra BLOB column (Oracle: SE_ANNO_CAD_DATA, elsewhere GDB_GEOMATTR_DATA),
in Esri's "extended shape buffer" format. The parser below follows GDAL's implementation
(ogr/ogrpgeogeometry.cpp, OGRCreateFromShapeBin, MIT licence):

  int32  shape type: byte0 = 50 (general polyline) / 51 (general polygon),
         byte3 flags: 0x80 Z, 0x40 M, 0x20 curves
  4 x double bbox | int32 nParts | int32 nPoints | int32[nParts] part starts
  nPoints x (x, y) | [Z: zmin, zmax, z[nPoints]] | [M: mmin, mmax, m[nPoints]]
  [curves: int32 nCurves, then per curve: int32 startIndex, int32 type, payload]
     type 1 circular arc : double v1, double v2, int32 bits (20 bytes)
            bits 0x80 IP   -> (v1, v2) is a point ON the arc
            otherwise      -> (v1, v2) is the CENTER; bit 0x08 = counter-clockwise
            bits 0x01 empty / 0x20 line / 0x40 point -> straight segment
     type 4 cubic Bezier : 4 doubles (two control points)          -> sampled
     type 5 elliptic arc : 5 doubles + int32 (44 bytes)            -> not handled (ArcGIS densified copy is used)

Output: QgsMultiSurface / QgsMultiCurve with QgsCircularString segments. The provider then either
keeps them as true curves (QGIS segments them for display with its own tolerance setting)
or segmentizes them with the tolerance chosen in the plugin.
"""

import math
import struct

from qgis.core import (QgsPoint, QgsLineString, QgsCircularString, QgsCompoundCurve, QgsCurvePolygon,
                       QgsMultiSurface, QgsMultiCurve, QgsGeometry)

GEN_LINE, GEN_POLY = 50, 51
CURVE_COLUMNS = ("SE_ANNO_CAD_DATA", "GDB_GEOMATTR_DATA")
_II = struct.Struct("<ii").unpack_from
_DDI = struct.Struct("<ddi").unpack_from
_D4 = struct.Struct("<4d").unpack_from


def find_header(buf, env=None, hint=None):
    """Offset of the shape header inside the BLOB (0 for a bare shape buffer)."""
    if buf is None:
        return None
    n = len(buf)
    cands = ([hint] if hint is not None else []) + list(range(0, min(max(n - 44, 0), 512)))
    for off in cands:
        if off is None or off < 0 or off + 44 > n:
            continue
        if buf[off] not in (GEN_LINE, GEN_POLY) or buf[off + 1] or buf[off + 2] or not (buf[off + 3] & 0x20):
            continue
        nparts, npts = _II(buf, off + 36)
        if not (0 < nparts <= npts) or off + 44 + 4 * nparts + 16 * npts > n:
            continue
        if env is not None:
            bx = _D4(buf, off + 4)
            tol = (abs(env[2] - env[0]) + abs(env[3] - env[1])) + 1e-9   # plausibility only: arcs may bulge past the densified envelope
            if any(abs(a - b) > tol for a, b in zip(bx, env)):
                continue
        return off
    return None


def _bezier(p0, c1, c2, p3, hasz, steps=32):
    out = []
    for k in range(1, steps + 1):
        t = k / float(steps)
        u = 1.0 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        x = a * p0.x() + b * c1[0] + c * c2[0] + d * p3.x()
        y = a * p0.y() + b * c1[1] + c * c2[1] + d * p3.y()
        out.append(QgsPoint(x, y, p0.z() + (p3.z() - p0.z()) * t) if hasz else QgsPoint(x, y))
    out[-1] = QgsPoint(p3)
    return out


def _arc_points(p1, p3, seg, hasz):
    """Points of a circular string for one arc segment, or None for a straight segment."""
    _k, v1, v2, bits = seg
    zmid = (p1.z() + p3.z()) / 2.0 if hasz else None

    def mk(x, y):
        return QgsPoint(x, y, zmid) if hasz else QgsPoint(x, y)

    if (bits & 0x80) and not (bits & 0x20):                       # interior point given
        p2 = mk(v1, v2)
        if p1.x() == p3.x() and p1.y() == p3.y():                 # full circle (GDAL workaround)
            if p1.x() == v1 and p1.y() == v2:
                return None
            cx, cy = (p1.x() + v1) / 2.0, (p1.y() + v2) / 2.0
            return [p1, mk(cx - (v2 - cy), cy + (p1.x() - cx)), p2,
                    mk(cx + (v2 - cy), cy - (p1.x() - cx)), p3]
        return [p1, p2, p3]
    if not (bits & 0x01) and not (bits & 0x20) and not (bits & 0x40):  # center point given
        cx, cy = v1, v2
        a0 = math.atan2(p1.y() - cy, p1.x() - cx)
        a1 = math.atan2(p3.y() - cy, p3.x() - cx)
        r = math.hypot(p3.x() - cx, p3.y() - cy)
        if bits & 0x08:
            if a0 >= a1:
                a1 += 2 * math.pi
        elif a0 <= a1:
            a1 -= 2 * math.pi
        m = (a0 + a1) / 2.0
        return [p1, mk(cx + r * math.cos(m), cy + r * math.sin(m)), p3]
    return None


def _part_curve(P, a, b, curves, hasz):
    """CompoundCurve for vertices P[a:b] with arc / Bezier modifiers."""
    cc = QgsCompoundCurve()
    run = [P[a]]
    has_curve = False

    def flush():
        if len(run) >= 2:
            cc.addCurve(QgsLineString(run))

    for i in range(a, b - 1):
        seg = curves.get(i)
        if seg is not None and seg[0] == "arc":
            pts = _arc_points(P[i], P[i + 1], seg, hasz)
            if pts is not None:
                flush()
                cs = QgsCircularString()
                cs.setPoints(pts)
                cc.addCurve(cs)
                run = [P[i + 1]]
                has_curve = True
                continue
        if seg is not None and seg[0] == "bez":
            c = seg[1]
            run.extend(_bezier(P[i], (c[0], c[1]), (c[2], c[3]), P[i + 1], hasz))
            continue
        run.append(P[i + 1])
    flush()
    return cc, has_curve


def parse(buf, off, keep_z):
    """QgsMultiSurface / QgsMultiCurve from an extended shape buffer, or None if not handled."""
    n = len(buf)
    t = buf[off]
    flags = buf[off + 3]
    hasz_buf, hasm, hasc = bool(flags & 0x80), bool(flags & 0x40), bool(flags & 0x20)
    nparts, npts = _II(buf, off + 36)
    starts = list(struct.unpack_from("<%di" % nparts, buf, off + 44))
    o = off + 44 + 4 * nparts
    xy = struct.unpack_from("<%dd" % (2 * npts), buf, o)
    o += 16 * npts
    zs = None
    if hasz_buf:
        if o + 16 + 8 * npts > n:
            return None
        zs = struct.unpack_from("<%dd" % npts, buf, o + 16)
        o += 16 + 8 * npts
    if hasm:
        o += 16 + 8 * npts
    curves = {}
    if hasc and o + 4 <= n:
        nc = struct.unpack_from("<i", buf, o)[0]
        o += 4
        if nc < 0 or nc > (n - o) // 28:
            nc = 0
        prev = -1
        for _ in range(nc):
            if o + 8 > n:
                return None
            idx, typ = _II(buf, o)
            o += 8
            if idx < 0 or idx >= npts or idx <= prev:
                return None
            prev = idx
            if typ == 1:
                v1, v2, bits = _DDI(buf, o)
                o += 20
                curves[idx] = ("arc", v1, v2, bits)
            elif typ == 4:
                curves[idx] = ("bez", _D4(buf, o))
                o += 32
            else:
                return None            # elliptic arc / unknown -> use the densified ST shape
    hasz = keep_z and zs is not None
    if hasz:
        P = [QgsPoint(xy[2 * i], xy[2 * i + 1], zs[i]) for i in range(npts)]
    else:
        P = [QgsPoint(xy[2 * i], xy[2 * i + 1]) for i in range(npts)]
    bounds = starts + [npts]
    parts = [_part_curve(P, bounds[k], bounds[k + 1], curves, hasz)[0] for k in range(nparts)
             if bounds[k + 1] - bounds[k] >= 2]
    if t == GEN_LINE:
        mc = QgsMultiCurve()
        for c in parts:
            mc.addGeometry(c)
        return mc
    # polygon: rings inside an earlier exterior (and outside its holes) are holes, else new parts
    polys = []
    for ring in parts:
        probe = QgsCurvePolygon()
        probe.setExteriorRing(ring.clone())
        rg = QgsGeometry(probe)
        pt = rg.pointOnSurface()
        target = None
        for poly in reversed(polys):
            if poly[1].contains(pt) and not any(h.contains(pt) for h in poly[2]):
                target = poly
                break
        if target is None:
            cp = QgsCurvePolygon()
            cp.setExteriorRing(ring)
            polys.append([cp, rg, []])
        else:
            target[0].addInteriorRing(ring)
            target[2].append(rg)
    ms = QgsMultiSurface()
    for cp, _g, _h in polys:
        ms.addGeometry(cp)
    return ms


def to_geometry(buf, keep_z, hint=None, env=None):
    """(QgsGeometry or None, header offset)."""
    if buf is None:
        return None, hint
    off = find_header(buf, env, hint)
    if off is None:
        return None, hint
    try:
        g = parse(buf, off, keep_z)
    except Exception:
        return None, off
    return (QgsGeometry(g) if g is not None else None), off


def verify(rows, keep_z):
    """rows: [(minx, miny, maxx, maxy, server_wkb, curve_blob)] -> (ok, header_offset, info)."""
    if not rows:
        return None, None, "no true curves in this table"
    ok = bad = skipped = 0
    hint = None
    heads = set()
    for minx, miny, maxx, maxy, wkb, blob in rows:
        blob = bytes(blob) if blob is not None else None
        if blob is None or wkb is None:
            continue
        heads.add(blob[:4].hex())
        g, off = to_geometry(blob, keep_z, hint, (minx, miny, maxx, maxy))
        if g is None:
            skipped += 1
            continue
        hint = off
        ref = QgsGeometry()
        ref.fromWkb(bytes(wkb))
        diag = math.hypot(maxx - minx, maxy - miny) or 1e-9
        lin = QgsGeometry(g.constGet().segmentize())
        try:
            hd = lin.hausdorffDistance(ref)
        except Exception:
            hd = float("inf")
        if 0 <= hd <= 0.02 * diag:
            ok += 1
        else:
            bad += 1
    if ok and not bad:
        return True, hint, "ON (verified %d/%d curved rows, %d not handled -> densified)" % (ok, len(rows), skipped)
    return False, hint, "off (curve format not recognised: %d ok, %d mismatched, %d unreadable; blob starts %s)" % (
        ok, bad, skipped, ",".join(sorted(heads))[:60])
