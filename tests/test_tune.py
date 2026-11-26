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
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
