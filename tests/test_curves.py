"""True-curve tests in real QGIS 4.2: synthetic Esri extended shape buffers (arcs) + densified ST shapes."""
import math
import struct
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_integration.py"), encoding="utf-8").read()
exec(src.split("# ------------------------------------------------------------------ tests")[0])
from qgis.core import QgsWkbTypes, QgsGeometry, QgsAbstractGeometry
from src import st_curves as SC


def shape_buffer(gtype, parts, arcs, z=None):
    """Esri extended shape buffer. parts: list of vertex lists; arcs: {global_index: ('c', cx, cy, ccw) | ('ip', x, y)}"""
    pts = [p for part in parts for p in part]
    flags = 0x20 | (0x80 if z is not None else 0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    b = struct.pack("<BBBB", gtype, 0, 0, flags) + struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
    starts, k = [], 0
    for part in parts:
        starts.append(k)
        k += len(part)
    b += struct.pack("<ii", len(parts), len(pts)) + struct.pack("<%di" % len(parts), *starts)
    b += b"".join(struct.pack("<dd", *p) for p in pts)
    if z is not None:
        b += struct.pack("<dd", min(z), max(z)) + struct.pack("<%dd" % len(z), *z)
    b += struct.pack("<i", len(arcs))
    for idx in sorted(arcs):
        a = arcs[idx]
        if a[0] == "c":
            b += struct.pack("<ii", idx, 1) + struct.pack("<ddi", a[1], a[2], 0x08 if a[3] else 0)
        else:
            b += struct.pack("<ii", idx, 1) + struct.pack("<ddi", a[1], a[2], 0x80)
    return b


CURVED = {}
for r in ROWS:
    oid = r["oid"]
    if oid % 40 or oid in ROWS_KIND or oid % 50 == 0:
        continue
    x, y, s = r["minx"], r["miny"], 0.002
    if oid % 80 == 0:
        # polygon: square whose top edge is a half-circle bulging up (center form, CCW from right to left)
        ring = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
        arcs = {2: ("c", x + s / 2, y + s, True)}
        kind = "polygon arc (center)"
    else:
        # polygon with a hole whose bottom edge is an arc given by an interior point
        ring = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
        h = s / 4
        hole = [(x + h, y + h), (x + h, y + 3 * h), (x + 3 * h, y + 3 * h), (x + 3 * h, y + h), (x + h, y + h)]
        arcs = {8: ("ip", x + 2 * h, y + h * 0.5)}   # segment hole[3] -> hole[4] (global index 5+3)
        ring, parts = ring, [ring, hole]
        kind = "hole with arc (interior point)"
    parts = [ring] if oid % 80 == 0 else [ring, hole]
    npts = sum(len(p) for p in parts)
    cad = shape_buffer(51, parts, arcs, z=[5.0] * npts)
    g, _o = SC.to_geometry(cad, True)
    dens = QgsGeometry(g.constGet().segmentize(math.radians(2), QgsAbstractGeometry.SegmentationToleranceType.MaximumAngle))
    dens.convertToMultiType()
    # densified ST copy (what ArcGIS stores in ST_GEOMETRY)
    allpts = []
    for i in range(dens.constGet().numGeometries()):
        poly = dens.constGet().geometryN(i)
        for ring_ in [poly.exteriorRing()] + [poly.interiorRing(k) for k in range(poly.numInteriorRings())]:
            allpts += [(ring_.xAt(k), ring_.yAt(k)) for k in range(ring_.numPoints())]
    bb = dens.boundingBox()
    r.update(cad=cad, wkb=bytes(QgsGeometry.fromWkt(dens.asWkt(17)).asWkb()), points=blob(allpts, [5.0] * len(allpts)),
             numpts=len(allpts), area=abs(dens.area()), minx=bb.xMinimum(), miny=bb.yMinimum(), maxx=bb.xMaximum(), maxy=bb.yMaximum())
    CURVED[oid] = kind
for r in ROWS:
    r.setdefault("cad", None)

# fake DB: curve column + verification query
_orig_query = FakeConn.query


def query(self, sql, params=None, arraysize=1000):
    u = " ".join(sql.split()).upper()
    if "FROM ALL_TAB_COLUMNS" in u and "ST_GEOMETRY'" not in u:
        r = _orig_query(self, sql, params, arraysize)
        return FakeRows([], list(r) + [("SE_ANNO_CAD_DATA", "BLOB", None, None)])
    if '"SE_ANNO_CAD_DATA" IS NOT NULL' in u:
        SQL_LOG.append(sql)
        rows = [r for r in ROWS if r["cad"] is not None][:50]
        return FakeRows([], [(r["minx"], r["miny"], r["maxx"], r["maxy"], r["wkb"], r["cad"]) for r in rows])
    return _orig_query(self, sql, params, arraysize)


FakeConn.query = query
_orig_val = FakeConn._val
FakeConn._val = lambda self, c, r: r["cad"] if c.upper().endswith('"SE_ANNO_CAD_DATA"') else _orig_val(self, c, r)

res = []


def pump():
    for _ in range(5):
        QgsApplication.processEvents()



def check(n, c, i=""):
    res.append(c)
    print("%-60s %s %s" % (n, "PASS" if c else "FAIL", i))


P.register_provider()
B.set_parallel_degree(1)
B.set_overview(False)
B.set_decoder_pref("auto")
print("curved rows:", len(CURVED), sorted(set(CURVED.values())))
uri = "stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'"
arc_oid = next(o for o, k in CURVED.items() if "center" in k)
hole_oid = next(o for o, k in CURVED.items() if "interior" in k)

# --- mode off: densified
B.set_curves_mode("off")
l0 = QgsVectorLayer(uri, "off", "stgeometry")
g0 = l0.getFeature(arc_oid).geometry()
check("off: layer linear type", QgsWkbTypes.displayString(l0.wkbType()) == "MultiPolygonZ", QgsWkbTypes.displayString(l0.wkbType()))
check("off: geometry densified (no curves)", not QgsWkbTypes.isCurvedType(g0.wkbType()), "%d vertices" % len(list(g0.vertices())))

# --- mode curves: true curves
B.set_curves_mode("curves")
LOG.clear()
l1 = QgsVectorLayer(uri, "curves", "stgeometry")
pump()
cl = [l for l in LOG if "true curves" in l]
check("curves: verified", l1.isValid() and cl and "ON" in cl[-1], cl[-1] if cl else "")
check("curves: layer type MultiSurfaceZ", QgsWkbTypes.displayString(l1.wkbType()) == "MultiSurfaceZ", QgsWkbTypes.displayString(l1.wkbType()))
g1 = l1.getFeature(arc_oid).geometry()
check("curves: arc feature is a true curve", QgsWkbTypes.isCurvedType(g1.wkbType()) and "CircularString" in g1.asWkt(3), g1.asWkt(3)[:150])
ref_area = s_ = 0.002
exact = s_ * s_ + math.pi * (s_ / 2) ** 2 / 2     # square + half disc
check("curves: arc area exact (square + half disc)", abs(g1.area() - exact) / exact < 1e-3, "%.4e vs %.4e" % (g1.area(), exact))
g2 = l1.getFeature(hole_oid).geometry()
cp = g2.constGet().geometryN(0)
check("curves: hole with arc kept as hole", cp.numInteriorRings() == 1 and "CircularString" in g2.asWkt(3), g2.asWkt(3)[:160])
gplain = l1.getFeature(1).geometry()
check("curves: plain feature in curve layer", gplain.wkbType() == l1.wkbType(), QgsWkbTypes.displayString(gplain.wkbType()))
src = l1.dataProvider().dataSourceUri()
check("curves: verification cached in source", "stcurve='ok" in src, src[src.find("stcurve"):src.find("stcurve") + 20])

# --- mode segmentize: tolerance controls vertex count
B.set_curves_mode("segmentize")
counts = {}
for t, v in (("angle", 10.0), ("angle", 1.0), ("distance", 1.0), ("distance", 0.01)):
    B.set_curve_tolerance(t, v)
    l2 = QgsVectorLayer(uri, "seg", "stgeometry")
    g = l2.getFeature(arc_oid).geometry()
    counts[(t, v)] = len(list(g.vertices()))
    if (t, v) == ("angle", 10.0):
        check("segmentize: layer linear type", QgsWkbTypes.displayString(l2.wkbType()) == "MultiPolygonZ", QgsWkbTypes.displayString(l2.wkbType()))
        check("segmentize: geometry linear", not QgsWkbTypes.isCurvedType(g.wkbType()))
print("   vertices per setting:", counts)
check("segmentize: 1 deg gives more vertices than 10 deg", counts[("angle", 1.0)] > counts[("angle", 10.0)])
check("segmentize: 1 cm gives more vertices than 1 m", counts[("distance", 0.01)] > counts[("distance", 1.0)])

# rendering a curve layer through QGIS
B.set_curves_mode("curves")
ms = QgsMapSettings(); ms.setLayers([l1]); ms.setDestinationCrs(l1.crs()); ms.setExtent(l1.extent()); ms.setOutputSize(QSize(800, 600))
ms.setFlag(Qgis.MapSettingsFlag.UseRenderingOptimization, True)
job = QgsMapRendererSequentialJob(ms); job.start(); job.waitForFinished()
check("curves: render ok", not job.renderedImage().isNull())
B.set_curves_mode("off")
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
