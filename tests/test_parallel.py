"""Parallel sessions per layer: every degree, every plan, every view shape -> exactly the right features."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_integration.py"), encoding="utf-8").read()
exec(src.split("# ------------------------------------------------------------------ tests")[0])
from qgis.core import QgsFeatureRequest, QgsRectangle

res = []


def check(n, c, i=""):
    res.append(c)
    if not c or "--verbose" in sys.argv:
        print("%-70s %s %s" % (n, "PASS" if c else "FAIL", i))


def expected(r, pred=lambda row: True):
    return sorted(row["oid"] for row in ROWS if row["minx"] <= r.xMaximum() and row["maxx"] >= r.xMinimum()
                  and row["miny"] <= r.yMaximum() and row["maxy"] >= r.yMinimum() and pred(row))


P.register_provider()
B.set_overview(False)
B.set_curves_mode("off")
B.set_decoder_pref("auto")
B.set_index_max_rows(0)                     # auto threshold
lyr = QgsVectorLayer("stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'", "b", "stgeometry")
E = lyr.extent()
cx, cy = ROWS[0]["minx"], ROWS[0]["miny"]
VIEWS = {
    "zoomed-in square (index, vertical strips)": QgsRectangle(cx - 0.01, cy - 0.01, cx + 0.01, cy + 0.01),
    "tall view (index, horizontal strips)": QgsRectangle(cx - 0.004, cy - 0.03, cx + 0.004, cy + 0.03),
    "wide view (index, vertical strips)": QgsRectangle(cx - 0.03, cy - 0.004, cx + 0.03, cy + 0.004),
    "view over the data edge (index)": QgsRectangle(E.xMinimum() - 0.01, E.yMinimum() - 0.01, E.xMinimum() + 0.01, E.yMinimum() + 0.01),
    "full extent (no spatial filter, key ranges)": QgsRectangle(E.xMinimum() - 1, E.yMinimum() - 1, E.xMaximum() + 1, E.yMaximum() + 1),
}
plans = {}
for par in (1, 2, 3, 4, 8, 16):
    B.set_parallel_degree(par)
    for name, r in VIEWS.items():
        LOG.clear()
        ids = [f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(r))]
        exp = expected(r)
        check("parallel %-2d %s" % (par, name), sorted(ids) == exp and len(ids) == len(set(ids)),
              "%d returned / %d expected" % (len(ids), len(exp)))
    # forced plain scan on half of the data (key-range partitions)
    B.set_index_max_rows(1000)
    half = QgsRectangle(E.xMinimum(), E.yMinimum(), E.center().x(), E.yMaximum())
    ids = [f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(half))]
    check("parallel %-2d half extent (plain scan, key ranges)" % par, sorted(ids) == expected(half) and len(ids) == len(set(ids)),
          "%d / %d" % (len(ids), len(expected(half))))
    B.set_index_max_rows(0)
    # expression filter (bind variables) + strips
    r = VIEWS["zoomed-in square (index, vertical strips)"]
    ids = [f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(r).setFilterExpression('"HEIGHT" > 10'))]
    check("parallel %-2d zoomed-in + expression filter" % par, sorted(ids) == expected(r, lambda row: row["height"] > 10))
    # layer filter (subset string) + strips
    lyr.setSubsetString('"HEIGHT" > 20')
    ids = [f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(r))]
    lyr.setSubsetString("")
    check("parallel %-2d zoomed-in + layer filter" % par, sorted(ids) == expected(r, lambda row: row["height"] > 20))
    # the reported bug: a real zoomed-in render with parallel sessions must draw features
    for ov in (False, True):
        B.set_overview(ov)
        B.set_timing(True)
        LOG.clear()
        ms = QgsMapSettings(); ms.setLayers([lyr]); ms.setDestinationCrs(lyr.crs()); ms.setExtent(r)
        ms.setOutputSize(QSize(800, 600)); ms.setFlag(Qgis.MapSettingsFlag.UseRenderingOptimization, True)
        job = QgsMapRendererSequentialJob(ms); job.start(); job.waitForFinished()
        QgsApplication.processEvents()
        img = job.renderedImage()
        drawn = sum(1 for xx in range(0, img.width(), 4) for yy in range(0, img.height(), 4) if img.pixel(xx, yy) & 0xFFFFFF != 0xFFFFFF)
        errors = [l for l in LOG if "Error fetching" in l or "DPY-" in l]
        check("parallel %-2d zoomed-in RENDER draws features (overview %s)" % (par, "on" if ov else "off"),
              drawn > 50 and not errors, "%d px %s" % (drawn, errors[:1]))
        B.set_timing(False)
    B.set_overview(False)

# automatic scan threshold
check("auto threshold: small table -> at least 50,000", B.scan_threshold(0, 3000) == 50000)
check("auto threshold: 3,000,000-row table -> 300,000 (10 %)", B.scan_threshold(0, 3000000) == 300000)
check("fixed threshold respected", B.scan_threshold(120000, 3000000) == 120000)
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
