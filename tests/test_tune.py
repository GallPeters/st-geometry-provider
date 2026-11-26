"""'Tune on current map view' + 'Fastest settings' buttons, in real QGIS (off-screen GUI)."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_integration.py"), encoding="utf-8").read()
src = src.replace("app = QgsApplication([], False)", "app = QgsApplication([], True)")
exec(src.split("# ------------------------------------------------------------------ tests")[0])
from qgis.core import QgsMapSettings
from qgis.PyQt.QtWidgets import QTreeWidgetItem
import src.dialog as DL

res = []


def check(n, c, i=""):
    res.append(c)
    print("%-58s %s %s" % (n, "PASS" if c else "FAIL", i))


P.register_provider()
ref = QgsVectorLayer("stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'", "ref", "stgeometry")
ms = QgsMapSettings()
ms.setDestinationCrs(ref.crs())
ms.setExtent(ref.extent())
ms.setOutputSize(QSize(900, 600))
ms.setFlag(Qgis.MapSettingsFlag.UseRenderingOptimization, True)


class Canvas(object):
    def mapSettings(self):
        return ms


class Iface(object):
    def mapCanvas(self):
        return Canvas()


shown = []
DL.show_text = lambda parent, title, text: shown.append(text)
dlg = DL.AddLayerDialog(Iface())
it = QTreeWidgetItem(["GIS", "BLDG", "SHAPE", str(N)])
dlg.tree.addTopLevelItem(it)
it.setSelected(True)

B.set_parallel_degree(1)
B.set_overview(False)
dlg.tune()
text = shown[-1] if shown else ""
print("\n".join("    " + l for l in text.splitlines()))
lines = [l for l in text.splitlines() if l.startswith("parallel")]
check("tune: 6 combinations measured", len(lines) == 6, "%d" % len(lines))
check("tune: fastest combination applied", "APPLIED" in text
      and ("parallel %d" % B.parallel_degree()) in text.split("APPLIED:")[1]
      and dlg.parallel.value() == B.parallel_degree() and dlg.chk_ov.isChecked() == B.overview_enabled())

B.set_parallel_degree(1)
B.set_overview(False)
dlg.apply_fastest()
check("fastest settings: defaults applied", B.parallel_degree() == B.FAST["parallel"] and B.overview_enabled()
      and dlg.chk_2d.isChecked(), "parallel %d" % B.parallel_degree())
check("fastest settings: threshold Auto shown in the dialog",
      B.index_max_rows() == 0 and dlg.idx_max.value() == 0 and dlg.idx_max.text().startswith("Auto"), dlg.idx_max.text())
check("fastest settings: every option at its fastest value",
      B.backend_pref() == "auto" and B.decoder_pref() == "auto" and B.curves_mode() == "off" and B.overview_pixels() == 1.0
      and not B.timing_enabled() and dlg.dec_combo.currentData() == "auto" and dlg.curve_combo.currentData() == "off"
      and dlg.chk_ext.isChecked() and not dlg.chk_evw.isChecked() and dlg.ov_px.isEnabled())

# --- any machine: a fresh profile gets the same fastest values as defaults
from qgis.core import QgsSettings
for k in ("parallel_v12", "overview", "overview_px", "curves", "decoder", "backend", "index_max_rows", "timing"):
    QgsSettings().remove("stgeometry/" + k)
B._parallel = None
B._timing = None
check("fresh profile: defaults == fastest settings",
      B.parallel_degree() == B.FAST["parallel"] and B.overview_enabled() and B.index_max_rows() == 0
      and B.backend_pref() == "auto" and B.decoder_pref() == "auto" and B.curves_mode() == "off" and not B.timing_enabled())

# --- any machine: corrupt / odd saved values never break the plugin
for k, v in (("parallel_v12", "abc"), ("overview", "maybe"), ("overview_px", "x"), ("curves", 7),
             ("decoder", None), ("backend", "oracle!"), ("index_max_rows", "1e5"), ("timing", "yes")):
    QgsSettings().setValue("stgeometry/" + k, v)
B._parallel = None
B._timing = None
try:
    vals = (B.parallel_degree(), B.overview_enabled(), B.overview_pixels(), B.curves_mode(), B.decoder_pref(),
            B.backend_pref(), B.index_max_rows(), B.timing_enabled(), B.curve_tolerance())
    ok = vals[0] == B.FAST["parallel"] and vals[3] == "off" and vals[4] == "auto" and vals[5] == "auto" and vals[6] == 100000
except Exception as e:
    ok, vals = False, e
check("corrupt saved settings: safe fallbacks, no exception", ok, str(vals))
dlg2 = DL.AddLayerDialog(Iface())
dlg2.apply_fastest()
check("corrupt saved settings: 'Fastest settings' repairs them",
      B.parallel_degree() == B.FAST["parallel"] and B.overview_enabled() and B.index_max_rows() == 0)


# --- any machine: without python-oracledb (Qt OCI driver) parallel sessions are skipped safely
class NoThreads(FakeBackend):
    threaded = False


lyr = QgsVectorLayer("stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'", "q", "stgeometry")
P.get_backend = lambda u: NoThreads()
lq = QgsVectorLayer("stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1'", "q2", "stgeometry")
srcq = P.StFeatureSource(lq.dataProvider())
nq = sum(1 for _ in lq.getFeatures())
check("no python-oracledb: parallel falls back to 1, reads all", srcq.parallel == 1 and nq == N, "%d features" % nq)
P.get_backend = lambda u: FAKE

# --- any machine: without numpy the per-row decoder gives the same geometries
from src import st_decoder as SD
geoms_np = {f.id(): f.geometry().asWkt(12) for f in lyr.getFeatures()}
SD.EsriDecoder.batch_capable = False
geoms_py = {f.id(): f.geometry().asWkt(12) for f in lyr.getFeatures()}
SD.EsriDecoder.batch_capable = True
check("no numpy: per-row decoding gives identical geometries", geoms_np == geoms_py and len(geoms_py) == N)

print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
