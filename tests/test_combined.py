"""Everything together, as used in practice: versioned view (hybrid, misencoded edits) + domain/subtype
descriptions (A) + value maps through the plugin's project hook (B) + parallel + fast overview + points
mode + project save / reload."""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
src = open(os.path.join(HERE, "test_integration.py"), encoding="utf-8").read()
src = src.replace("app = QgsApplication([], False)", "app = QgsApplication([], True)")
exec(src.split("# ------------------------------------------------------------------ tests")[0])


def section(fname):
    t = open(os.path.join(HERE, fname), encoding="utf-8").read()
    t = t[t.index("exec(src.split("):]
    t = t[t.index("\n") + 1:]
    return t[:t.index("res = []")]


exec(section("test_domains.py"))        # domains + subtypes in the simulated SDE.GDB_ITEMS
exec(section("test_evw.py"))            # versioned view: updates (misencoded), inserts, deletes
for o, r in edited.items():             # edited rows carry domain fields too
    r.update(subtype=2, landuse=1, zone="B")

from qgis.core import QgsProject, QgsFeatureRequest, QgsRectangle, QgsMapSettings, QgsMapRendererSequentialJob
SUB = {1: "Residential", 2: "Commercial", 3: "Industrial"}
PFX = {1: "Res", 2: "Com", 3: "Gen"}


def exp_lu(r):
    return None if r["landuse"] is None else "%s-%d" % (PFX[r["subtype"]], r["landuse"])


res = []


def check(n, c, i=""):
    res.append(c)
    print("%-70s %s %s" % (n, "PASS" if c else "FAIL", i))


# ---- start the plugin like QGIS does (real initGui -> project hook for value maps)
import src.plugin as PL


class _Menu(object):
    def addAction(self, a): pass
    def removeAction(self, a): pass


class Iface(object):
    def mainWindow(self): return None
    def addToolBarIcon(self, a): pass
    def removeToolBarIcon(self, a): pass
    def addPluginToDatabaseMenu(self, m, a): pass
    def removePluginDatabaseMenu(self, m, a): pass
    def addLayerMenu(self): return _Menu()


plugin = PL.StGeometryPlugin(Iface())
plugin.initGui()
B.set_parallel_degree(4)
B.set_overview(True)
B.set_index_max_rows(0)
LOG.clear()

uri = "stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1' stevw='1' stdomains='1'"
lyr = QgsVectorLayer(uri, "BLDG", "stgeometry")
QgsProject.instance().addMapLayer(lyr)                       # hook applies value maps here
pts = QgsVectorLayer(uri + " stgeomas='point'", "BLDG (points)", "stgeometry")
QgsProject.instance().addMapLayer(pts)
QgsApplication.processEvents()

names = [f.name() for f in lyr.fields()]
check("valid EVW layer with descriptions", lyr.isValid() and pts.isValid())
check("hybrid EVW read active", any("hybrid" in l for l in LOG))
check("double fields: raw + _DESC for every domain field",
      all(n in names for n in ("SUBTYPE", "SUBTYPE_DESC", "LANDUSE", "LANDUSE_DESC", "ZONE", "ZONE_DESC")), str(names[-6:]))
ws = lambda L, n: L.editorWidgetSetup(L.fields().indexOf(n))
check("value maps via project hook (layer 1): subtype + zone",
      ws(lyr, "SUBTYPE").type() == "ValueMap" and ws(lyr, "ZONE").type() == "ValueMap")
check("value maps via project hook (layer 2, points)",
      ws(pts, "SUBTYPE").type() == "ValueMap" and ws(pts, "ZONE").type() == "ValueMap")
check("no value map on subtype-dependent domain field", ws(lyr, "LANDUSE").type() != "ValueMap")

by_id = {r["oid"]: r for r in EVW}
feats = {f.id(): f for f in lyr.getFeatures()}
check("exactly the EVW features", sorted(feats) == sorted(by_id), "%d" % len(feats))
sea = [o for o, f in feats.items() if abs(f.geometry().boundingBox().xMinimum() - by_id[o]["minx"]) > 1e-9]
check("no building in the sea", not sea, "%d misplaced" % len(sea))
bad = [o for o, f in feats.items() if f["LANDUSE_DESC"] != exp_lu(by_id[o]) or f["SUBTYPE_DESC"] != SUB[by_id[o]["subtype"]]]
check("descriptions correct, including edited rows", not bad, "%d wrong" % len(bad))
check("edited row: description from its new subtype", feats[UPD[0]]["SUBTYPE_DESC"] == "Commercial"
      and feats[UPD[0]]["LANDUSE_DESC"] == "Com-1")
pf = {f.id(): f.geometry() for f in pts.getFeatures()}
check("points layer: every point inside its EVW polygon", all(feats[o].geometry().intersects(pf[o]) for o in feats))

# rendering a zoomed-in and a zoomed-out view (parallel strips / scan, overview cells)
for nm, ext in (("zoomed-in", QgsRectangle(by_id[UPD[0]]["minx"] - 0.01, by_id[UPD[0]]["miny"] - 0.01,
                                           by_id[UPD[0]]["minx"] + 0.01, by_id[UPD[0]]["miny"] + 0.01)),
                ("full extent", lyr.extent())):
    ms = QgsMapSettings(); ms.setLayers([lyr, pts]); ms.setDestinationCrs(lyr.crs()); ms.setExtent(ext)
    ms.setOutputSize(QSize(900, 600)); ms.setFlag(Qgis.MapSettingsFlag.UseRenderingOptimization, True)
    LOG.clear()
    job = QgsMapRendererSequentialJob(ms); job.start(); job.waitForFinished(); QgsApplication.processEvents()
    img = job.renderedImage()
    drawn = sum(1 for x in range(0, img.width(), 5) for y in range(0, img.height(), 5) if img.pixel(x, y) & 0xFFFFFF != 0xFFFFFF)
    errs = [l for l in LOG if "Error fetching" in l or "DPY-" in l or "Traceback" in l]
    check("render %s (EVW + descriptions + parallel + overview)" % nm, drawn > 50 and not errs, "%d px %s" % (drawn, errs[:1]))

# ---- project save -> reload: fields, value maps and data come back
path = os.path.join(tempfile.mkdtemp(), "combined.qgs")
check("project saved", QgsProject.instance().write(path))
QgsProject.instance().clear()
QgsProject.instance().read(path)
QgsApplication.processEvents()
L = {l.name(): l for l in QgsProject.instance().mapLayers().values()}
r1 = L.get("BLDG")
check("project reloaded: layer valid", r1 is not None and r1.isValid())
check("project reloaded: description fields", r1 is not None and r1.fields().indexOf("LANDUSE_DESC") >= 0)
check("project reloaded: value maps kept", r1 is not None and ws(r1, "SUBTYPE").type() == "ValueMap")
n1 = sum(1 for _ in r1.getFeatures()) if r1 else 0
check("project reloaded: data reads", n1 == len(EVW), "%d" % n1)
plugin.unload()
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
