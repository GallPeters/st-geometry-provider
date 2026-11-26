"""Versioned view (<TABLE>_EVW): hybrid read is exact, and wrongly-encoded edited rows never land in the sea."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_integration.py"), encoding="utf-8").read()
exec(src.split("# ------------------------------------------------------------------ tests")[0])
from qgis.core import QgsFeatureRequest, QgsRectangle, QgsGeometry

# ---- versioned data: base table ROWS + edits in the delta tables
# updated features: moved a little, and their POINTS blob is written with a DIFFERENT grid
# (as an edit table written by another client could be) - decoding it with the base table's
# parameters would put the building far away ("in the sea"); Esri's envelope and WKB are correct.
UPD = [r["oid"] for r in ROWS if r["oid"] % 97 == 0 and r["oid"] not in ROWS_KIND][:25]
DEL = [r["oid"] for r in ROWS if r["oid"] % 89 == 0 and r["oid"] % 97 and r["oid"] not in ROWS_KIND][:20]
ins_ids = list(range(N + 1, N + 21))


def make(oid, x, y, s, wrong_grid=False):
    ring = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
    g = QgsGeometry.fromWkt("POLYGON Z ((%s))" % ",".join("%r %r 5" % p for p in ring))
    if wrong_grid:                       # encode with another false origin -> base decoder puts it ~5 deg away
        pts = [(px + 5.0, py - 5.0) for px, py in ring]
    else:
        pts = ring
    return dict(oid=oid, name="edited %d" % oid, height=99.0, created=None, minx=x, miny=y, maxx=x + s, maxy=y + s,
                entity=8, numpts=5, area=abs(g.area()), points=blob(pts, [5.0] * 5),
                wkb=bytes(QgsGeometry.fromWkt(g.asWkt(17)).asWkb()))


edited = {}
for oid in UPD:
    r = ROWS[oid - 1]
    edited[oid] = make(oid, r["minx"] + 0.0001, r["miny"] + 0.0001, 0.00008, wrong_grid=True)
for i, oid in enumerate(ins_ids):
    r = ROWS[i * 7]
    edited[oid] = make(oid, r["minx"] + 0.0003, r["miny"] + 0.0003, 0.00008)
CHANGED_IDS = set(UPD) | set(DEL) | set(ins_ids)
EVW = [edited.get(r["oid"], r) for r in ROWS if r["oid"] not in DEL] + [edited[o] for o in ins_ids]
globals()["EVW_ROWS"] = EVW
CHANGED.update(CHANGED_IDS)

res = []


def check(n, c, i=""):
    res.append(c)
    print("%-66s %s %s" % (n, "PASS" if c else "FAIL", i))


P.register_provider()
B.set_overview(False)
B.set_curves_mode("off")
B.set_decoder_pref("auto")
B.set_index_max_rows(0)
LOG.clear()
lyr = QgsVectorLayer("stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1' stevw='1'", "evw", "stgeometry")
QgsApplication.processEvents()
check("EVW layer valid, hybrid read enabled", lyr.isValid() and any("hybrid" in l for l in LOG))
by_id = {r["oid"]: r for r in EVW}
for par in (1, 4):
    B.set_parallel_degree(par)
    SQL_LOG.clear()
    feats = {f.id(): f for f in lyr.getFeatures()}
    check("parallel %d: exactly the EVW features (edits, inserts, deletes)" % par,
          sorted(feats) == sorted(by_id), "%d vs %d" % (len(feats), len(by_id)))
    check("parallel %d: unedited rows read from the base table, edited through _EVW" % par,
          any('"BLDG" t' in q and "NOT IN (SELECT" in q for q in SQL_LOG)
          and any('"BLDG_EVW" t' in q and '"OBJECTID" IN (SELECT' in q for q in SQL_LOG))
    wrong = []
    for oid, f in feats.items():
        b = f.geometry().boundingBox()
        r = by_id[oid]
        if abs(b.xMinimum() - r["minx"]) > 1e-9 or abs(b.yMaximum() - r["maxy"]) > 1e-9:
            wrong.append(oid)
    check("parallel %d: no building in the sea (every feature where Esri says)" % par, not wrong,
          "%d misplaced, e.g. %s" % (len(wrong), wrong[:3]))
    check("parallel %d: edited attributes from the version" % par,
          all(feats[o]["NAME"] == "edited %d" % o for o in UPD + ins_ids))
    # zoomed-in view around an edited feature
    e = by_id[UPD[0]]
    r = QgsRectangle(e["minx"] - 0.005, e["miny"] - 0.005, e["minx"] + 0.005, e["miny"] + 0.005)
    ids = sorted(f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(r)))
    exp = sorted(x["oid"] for x in EVW if x["minx"] <= r.xMaximum() and x["maxx"] >= r.xMinimum()
                 and x["miny"] <= r.yMaximum() and x["maxy"] >= r.yMinimum())
    check("parallel %d: zoomed-in view == expected EVW features" % par, ids == exp, "%d vs %d" % (len(ids), len(exp)))
check("feature count (hybrid)", lyr.dataProvider().featureCount() == len(EVW), str(lyr.dataProvider().featureCount()))

# envelope safety net on the plain table layer too: a corrupt blob never ends up misplaced
base = QgsVectorLayer("stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'", "base", "stgeometry")
good = ROWS[100]
saved = good["points"]
good["points"] = blob([(good["minx"] + 3, good["miny"]), (good["minx"] + 3.0001, good["miny"]),
                       (good["minx"] + 3.0001, good["miny"] + 0.0001), (good["minx"] + 3, good["miny"])], [5.0] * 4)
good["numpts"] = 4
g = next(base.getFeatures(QgsFeatureRequest(good["oid"]))).geometry()
good["points"], good["numpts"] = saved, 5
check("corrupt blob in base table -> Esri conversion, correct place",
      abs(g.boundingBox().xMinimum() - good["minx"]) < 1e-9, g.boundingBox().toString(5))
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
