"""Esri coded-value domains + subtypes -> <FIELD>_DESC fields (A) and Value Maps (B), in real QGIS."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_integration.py"), encoding="utf-8").read()
src = src.replace("app = QgsApplication([], False)", "app = QgsApplication([], True)")
exec(src.split("# ------------------------------------------------------------------ tests")[0])
from qgis.core import QgsFeatureRequest, QgsRectangle
from qgis.PyQt.QtWidgets import QTreeWidgetItem

NS = ('xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
      'xmlns:typens="http://www.esri.com/schemas/ArcGIS/10.8"')


def coded_domain(name, ftype, xs_type, codes):
    cv = "".join('<CodedValue xsi:type="typens:CodedValue"><Name>%s</Name><Code xsi:type="xs:%s">%s</Code></CodedValue>'
                 % (d, xs_type, c) for c, d in codes)
    return ('<?xml version="1.0" encoding="UTF-8"?><typens:GPCodedValueDomain2 %s xsi:type="typens:GPCodedValueDomain2">'
            '<DomainName>%s</DomainName><FieldType>%s</FieldType><MergePolicy>esriMPTDefaultValue</MergePolicy>'
            '<SplitPolicy>esriSPTDuplicate</SplitPolicy><Description></Description><Owner>GIS</Owner>'
            '<CodedValues xsi:type="typens:ArrayOfCodedValue">%s</CodedValues></typens:GPCodedValueDomain2>'
            % (NS, name, ftype, cv))


# the hard case: the SAME codes mean different things per subtype
DOMAINS = {
    "LU_Res": coded_domain("LU_Res", "esriFieldTypeSmallInteger", "short", [(i, "Res-%d" % i) for i in range(4)]),
    "LU_Com": coded_domain("LU_Com", "esriFieldTypeSmallInteger", "short", [(i, "Com-%d" % i) for i in range(4)]),
    "LU_Default": coded_domain("LU_Default", "esriFieldTypeSmallInteger", "short", [(i, "Gen-%d" % i) for i in range(4)]),
    "ZoneDom": coded_domain("ZoneDom", "esriFieldTypeString", "string", [(z, "Zone %s" % z) for z in "ABC"]),
}


def fi(name, dom=None):
    return ('<GPFieldInfoEx xsi:type="typens:GPFieldInfoEx"><Name>%s</Name>%s<IsNullable>true</IsNullable></GPFieldInfoEx>'
            % (name, "<DomainName>%s</DomainName>" % dom if dom else ""))


def st(code, name, infos):
    sfi = "".join('<SubtypeFieldInfo xsi:type="typens:SubtypeFieldInfo"><FieldName>%s</FieldName>'
                  '<DomainName>%s</DomainName></SubtypeFieldInfo>' % (f, d) for f, d in infos)
    return ('<Subtype xsi:type="typens:Subtype"><SubtypeName>%s</SubtypeName><SubtypeCode>%d</SubtypeCode>'
            '<FieldInfos xsi:type="typens:ArrayOfSubtypeFieldInfo">%s</FieldInfos></Subtype>' % (name, code, sfi))


FC_XML = ('<?xml version="1.0" encoding="UTF-8"?><typens:DEFeatureClassInfo %s xsi:type="typens:DEFeatureClassInfo">'
          '<CatalogPath>\\GIS.BLDG</CatalogPath><Name>GIS.BLDG</Name><HasOID>true</HasOID><OIDFieldName>OBJECTID</OIDFieldName>'
          '<GPFieldInfoExs xsi:type="typens:ArrayOfGPFieldInfoEx">%s</GPFieldInfoExs>'
          '<SubtypeFieldName>SUBTYPE</SubtypeFieldName><DefaultSubtypeCode>1</DefaultSubtypeCode>'
          '<Subtypes xsi:type="typens:ArrayOfSubtype">%s</Subtypes><ShapeType>esriGeometryPolygon</ShapeType>'
          '</typens:DEFeatureClassInfo>'
          % (NS, fi("OBJECTID") + fi("NAME") + fi("SUBTYPE") + fi("LANDUSE", "LU_Default") + fi("ZONE", "ZoneDom"),
             st(1, "Residential", [("LANDUSE", "LU_Res"), ("ZONE", "ZoneDom")])
             + st(2, "Commercial", [("LANDUSE", "LU_Com"), ("ZONE", "ZoneDom")])
             + st(3, "Industrial", [("ZONE", "ZoneDom")])))          # subtype 3: no LANDUSE domain -> default

for r in ROWS:
    r["subtype"] = r["oid"] % 3 + 1
    r["landuse"] = None if r["oid"] % 13 == 0 else r["oid"] % 4
    r["zone"] = "ABC"[r["oid"] % 3]

_orig_query = FakeConn.query


def query(self, sql, params=None, arraysize=1000):
    u = " ".join(sql.split()).upper()
    if "FROM ALL_TAB_COLUMNS" in u and "ST_GEOMETRY'" not in u:
        r = _orig_query(self, sql, params, arraysize)
        return FakeRows([], list(r) + [("SUBTYPE", "NUMBER", 5, 0), ("LANDUSE", "NUMBER", 5, 0), ("ZONE", "NVARCHAR2", None, None)])
    if "SDE.GDB_ITEMS" in u and "PHYSICALNAME" in u:
        SQL_LOG.append(sql)
        return FakeRows(["D"], [(FC_XML,)] if (params or {}).get("pn") == "GIS.BLDG" else [])
    if "SDE.GDB_ITEMS" in u and "I.NAME IN" in u:
        SQL_LOG.append(sql)
        return FakeRows(["N", "D"], [(n, DOMAINS[n]) for n in (params or {}).values() if n in DOMAINS])
    return _orig_query(self, sql, params, arraysize)


FakeConn.query = query
_orig_val = FakeConn._val


def _val(self, c, r):
    for col, key in (('"SUBTYPE"', "subtype"), ('"LANDUSE"', "landuse"), ('"ZONE"', "zone")):
        if c.endswith(col):
            return r[key]
    return _orig_val(self, c, r)


FakeConn._val = _val
res = []


def check(n, c, i=""):
    res.append(c)
    print("%-64s %s %s" % (n, "PASS" if c else "FAIL", i))


P.register_provider()
B.set_parallel_degree(1)
B.set_overview(False)
uri = "stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stcalcext='1' stdomains='1'"
lyr = QgsVectorLayer(uri, "dom", "stgeometry")
names = [f.name() for f in lyr.fields()]
check("description fields added, raw fields kept", lyr.isValid() and names[-3:] == ["SUBTYPE_DESC", "LANDUSE_DESC", "ZONE_DESC"]
      and "LANDUSE" in names, str(names))
SUB = {1: "Residential", 2: "Commercial", 3: "Industrial"}
PFX = {1: "Res", 2: "Com", 3: "Gen"}


def exp_lu(r):
    return None if r["landuse"] is None else "%s-%d" % (PFX[r["subtype"]], r["landuse"])


feats = {f.id(): f for f in lyr.getFeatures()}
bad = [o for o, f in feats.items()
       if f["LANDUSE_DESC"] != exp_lu(ROWS[o - 1]) or f["SUBTYPE_DESC"] != SUB[ROWS[o - 1]["subtype"]]
       or f["ZONE_DESC"] != "Zone %s" % ROWS[o - 1]["zone"]]
check("same codes, different domain per subtype -> correct description", not bad, "%d wrong e.g. %s" % (len(bad), bad[:3]))
f1, f2, f3 = feats[3], feats[1], feats[2]            # subtypes 1, 2, 3 with LANDUSE 3, 1, 2
check("examples: code per subtype", (f1["LANDUSE_DESC"], f2["LANDUSE_DESC"], f3["LANDUSE_DESC"]) == ("Res-3", "Com-1", "Gen-2"),
      str((f1["LANDUSE_DESC"], f2["LANDUSE_DESC"], f3["LANDUSE_DESC"])))
check("NULL code -> NULL description", feats[13]["LANDUSE_DESC"] is None)
check("raw values unchanged", f1["LANDUSE"] == 3 and f1["SUBTYPE"] == 1 and f1["ZONE"] == "A")

# only a description field requested: its raw + subtype columns are fetched automatically
i_lu = lyr.fields().indexOf("LANDUSE_DESC")
fs = list(lyr.getFeatures(QgsFeatureRequest().setSubsetOfAttributes([i_lu]).setFlags(Qgis.FeatureRequestFlag.NoGeometry)))
check("subset request of a description field", all(f["LANDUSE_DESC"] == exp_lu(ROWS[f.id() - 1]) for f in fs) and len(fs) == N)
fs = list(lyr.getFeatures(QgsFeatureRequest().setFilterExpression("\"LANDUSE_DESC\" = 'Com-2'")))
check("filter on a description field", sorted(f.id() for f in fs) == sorted(r["oid"] for r in ROWS if exp_lu(r) == "Com-2"),
      "%d features" % len(fs))
check("unique values of a description field", lyr.uniqueValues(i_lu) >= {"Res-0", "Com-3", "Gen-1"})
B.set_parallel_degree(4)
r = QgsRectangle(ROWS[0]["minx"] - 0.02, ROWS[0]["miny"] - 0.02, ROWS[0]["minx"] + 0.02, ROWS[0]["miny"] + 0.02)
fs = list(lyr.getFeatures(QgsFeatureRequest().setFilterRect(r)))
check("descriptions with parallel strips", fs and all(f["LANDUSE_DESC"] == exp_lu(ROWS[f.id() - 1]) for f in fs), "%d" % len(fs))
B.set_parallel_degree(1)

# B: value maps through the dialog
import src.dialog as DL
DL.show_text = lambda *a: None
dlg = DL.AddLayerDialog(None)
item = QTreeWidgetItem(["GIS", "BLDG", "SHAPE", str(N)])
dlg.tree.addTopLevelItem(item)
lay = dlg._make_layer(item)
ws = {f.name(): lay.editorWidgetSetup(lay.fields().indexOf(f.name())) for f in lay.fields()}
check("value map on the subtype field", ws["SUBTYPE"].type() == "ValueMap"
      and {"Commercial": 2} in ws["SUBTYPE"].config()["map"], str(ws["SUBTYPE"].config())[:80])
check("value map on a field with one domain for all subtypes", ws["ZONE"].type() == "ValueMap"
      and {"Zone B": "B"} in ws["ZONE"].config()["map"])
check("no value map where the domain differs per subtype (only _DESC is exact)", ws["LANDUSE"].type() != "ValueMap")
check("layer without the option has no description fields",
      not any(n.endswith("_DESC") for n in [f.name() for f in QgsVectorLayer(uri.replace(" stdomains='1'", ""), "x", "stgeometry").fields()]))
print("\n%d/%d passed" % (sum(res), len(res)))
app.exitQgis()
