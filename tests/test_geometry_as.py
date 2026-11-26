"""'Load polygons as' lines / points, in real QGIS against the simulated Oracle."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_integration.py"), encoding="utf-8").read()
exec(src.split("# ------------------------------------------------------------------ tests")[0])
from qgis.core import QgsWkbTypes, QgsGeometry, QgsFeatureRequest, QgsRectangle

# make every 7th small feature an L-shape (its centroid lies outside the polygon)
L_SHAPES = []
for r in ROWS:
    if r["oid"] % 7 or r["oid"] in ROWS_KIND or r["oid"] % 10 == 0 or r["oid"] % 50 == 0:
        continue
    x, y, s = r["minx"], r["miny"], 0.0004
    w = s / 8                                   # thin arms -> centroid falls in the empty corner
    ring = [(x, y), (x + s, y), (x + s, y + w), (x + w, y + w), (x + w, y + s), (x, y + s), (x, y)]
    g = QgsGeometry.fromWkt("POLYGON Z ((%s))" % ",".join("%r %r 5" % p for p in ring))
    bb = g.boundingBox()
    r.update(points=blob(ring, [5.0] * len(ring)), numpts=len(ring), area=abs(g.area()),
             wkb=bytes(QgsGeometry.fromWkt(g.asWkt(17)).asWkb()),
             minx=bb.xMinimum(), miny=bb.yMinimum(), maxx=bb.xMaximum(), maxy=bb.yMaximum())
    L_SHAPES.append(r["oid"])

res = []


def check(n, c, i=""):
    res.append(c)
    print("%-62s %s %s" % (n, "PASS" if c else "FAIL", i))


P.register_provider()
B.set_parallel_degree(1)
B.set_overview(False)
B.set_curves_mode("off")
B.set_decoder_pref("auto")
base = "stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'"
poly = QgsVectorLayer(base, "poly", "stgeometry")
polys = {f.id(): f.geometry() for f in poly.getFeatures()}
lo = L_SHAPES[0]
c = polys[lo].centroid()
check("test data: L-shape centroid is outside its polygon", not polys[lo].intersects(c), "%d L-shapes" % len(L_SHAPES))

# ---------------- lines
lines = QgsVectorLayer(base + " stgeomas='boundary'", "lines", "stgeometry")
check("lines: layer valid, type MultiLineStringZ", lines.isValid() and QgsWkbTypes.displayString(lines.wkbType()) == "MultiLineStringZ",
      QgsWkbTypes.displayString(lines.wkbType()))
lf = {f.id(): f.geometry() for f in lines.getFeatures()}
check("lines: one feature per polygon", len(lf) == len(polys), "%d" % len(lf))
hole = 50
check("lines: polygon with hole -> 2 lines (outer + hole)", lf[hole].constGet().numGeometries() == 2,
      str(lf[hole].constGet().numGeometries()))
mp = next(o for o, k in ROWS_KIND.items() if k == "multipart+hole")
check("lines: multipart+hole -> 3 lines", lf[mp].constGet().numGeometries() == 3, str(lf[mp].constGet().numGeometries()))
check("lines: length == polygon perimeter", all(abs(lf[o].length() - polys[o].constGet().perimeter()) < 1e-12 for o in polys))
check("lines: attributes kept", next(lines.getFeatures(QgsFeatureRequest(hole)))["NAME"] == "bldg 50")
src_l = lines.dataProvider().dataSourceUri()
check("lines: mode stored in layer source", "stgeomas='boundary'" in src_l)
l2 = QgsVectorLayer(src_l, "l2", "stgeometry")
check("lines: reopen from stored source keeps lines", QgsWkbTypes.displayString(l2.wkbType()) == "MultiLineStringZ")

# ---------------- points
pts = QgsVectorLayer(base + " stgeomas='point'", "pts", "stgeometry")
check("points: layer valid, type PointZ", pts.isValid() and QgsWkbTypes.displayString(pts.wkbType()) == "PointZ",
      QgsWkbTypes.displayString(pts.wkbType()))
pf = {f.id(): f.geometry() for f in pts.getFeatures()}
inside = sum(1 for o in polys if polys[o].intersects(pf[o]))
check("points: every point lies inside its polygon", inside == len(polys), "%d/%d" % (inside, len(polys)))
check("points: L-shape point moved inside (not the raw centroid)",
      polys[lo].intersects(pf[lo]) and not pf[lo].equals(polys[lo].centroid()))
sq = 1
check("points: convex polygon uses the true centroid", pf[sq].asPoint().distance(polys[sq].centroid().asPoint()) < 1e-12)
check("points: single-part points with Z", QgsWkbTypes.displayString(pf[sq].wkbType()) == "PointZ"
      and abs(pf[sq].constGet().z() - 5.0) < 1e-9)

# identify on the converted geometry
g = polys[lo]
bb = g.boundingBox()
r = QgsRectangle(bb.xMinimum() - 1e-9, bb.yMinimum() - 1e-9, bb.xMinimum() + 2e-5, bb.yMinimum() + 2e-5)
hit = [f.id() for f in lines.getFeatures(QgsFeatureRequest().setFilterRect(r).setFlags(Qgis.FeatureRequestFlag.ExactIntersect))]
check("identify on lines layer", lo in hit, str(hit[:5]))

# force 2D + rendering (with and without fast overview)
p2d = QgsVectorLayer(base + " stgeomas='point' stforce2d='1'", "p2d", "stgeometry")
check("points + Force 2D -> Point", QgsWkbTypes.displayString(p2d.wkbType()) == "Point")
for ov in (False, True):
    B.set_overview(ov)
    for lyr in (lines, pts):
        ms = QgsMapSettings(); ms.setLayers([lyr]); ms.setDestinationCrs(lyr.crs()); ms.setExtent(lyr.extent())
        ms.setOutputSize(QSize(800, 600)); ms.setFlag(Qgis.MapSettingsFlag.UseRenderingOptimization, True)
        job = QgsMapRendererSequentialJob(ms); job.start(); job.waitForFinished()
        img = job.renderedImage()
        drawn = sum(1 for xx in range(0, img.width(), 5) for yy in range(0, img.height(), 5) if img.pixel(xx, yy) & 0xFFFFFF != 0xFFFFFF)
        check("render %s (overview %s)" % (lyr.name(), "on" if ov else "off"), drawn > 20, "%d px" % drawn)
B.set_overview(False)
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
