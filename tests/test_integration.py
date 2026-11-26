"""
Integration test of the st_geometry_provider plugin inside real QGIS 4.2 (headless),
with a fake Oracle that answers the plugin's SQL from a synthetic dataset.
"""
import math
import os
import random
import re
import sys
import time
import traceback

PLUGIN_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_PARENT)

from qgis.core import (QgsApplication, QgsVectorLayer, QgsFeatureRequest, QgsRectangle, QgsGeometry,
                       QgsMapSettings, QgsMapRendererSequentialJob, QgsProject, QgsWkbTypes, Qgis,
                       QgsCoordinateReferenceSystem)
from qgis.PyQt.QtCore import QSize

app = QgsApplication([], False)
app.initQgis()
LOG = []
QgsApplication.messageLog().messageReceived.connect(lambda *a: LOG.append("%s: %s" % (a[1], a[0])))

import src.backend as B
import src.provider as P
from src import st_decoder as D
import src.compat as CC
_orig_log = CC.log
CC.log = lambda msg, level="info": (LOG.append(str(msg)), _orig_log(msg, level))

# ------------------------------------------------------------------ synthetic data
UX = 1.0e12          # arbitrary synthetic coordinate grid (units per degree)
FX = FY = -1000.0   # arbitrary synthetic false origin


def enc(v):
    neg = v < 0
    v = abs(v)
    out = [(v & 0x3F) | (0x40 if neg else 0)]
    v >>= 6
    while v:
        out[-1] |= 0x80
        out.append(v & 0x7F)
        v >>= 7
    return bytes(out)


def blob(pts, zs=None):
    ints = [(round((x - FX) * UX), round((y - FY) * UX)) for x, y in pts]
    b = b"".join(enc(c) for c in ints[0])
    px, py = ints[0]
    for x, y in ints[1:]:
        b += enc(x - px) + enc(y - py)
        px, py = x, y
    if zs is not None:  # block layout, delta coded, scale 1000, offset 0 (our assumption for the test)
        zi = [round(z * 1000) for z in zs]
        b += enc(zi[0]) + b"".join(enc(zi[i] - zi[i - 1]) for i in range(1, len(zi)))
    return b"\0" * 8 + b


random.seed(7)
ROWS_KIND = {}
ROWS = []          # dicts: oid, name, height, created, minx..maxy, entity, numpts, area, points, wkb
N = 3000
for oid in range(1, N + 1):
    x = 10.00 + random.random() * 0.20
    y = 45.00 + random.random() * 0.20
    big = oid % 10 == 0
    s = 0.004 if (big or oid % 70 == 0 or oid % 90 == 0) else 0.00008   # big parcels vs small buildings
    ring = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
    if oid % 70 == 0:                         # multipart: 2 separate parts, the 2nd with a hole
        r2 = [(x + 2 * s, y), (x + 3 * s, y), (x + 3 * s, y + s), (x + 2 * s, y + s), (x + 2 * s, y)]
        q = s / 4
        h2 = [(x + 2 * s + q, y + q), (x + 2 * s + q, y + 2 * q), (x + 2 * s + 2 * q, y + 2 * q), (x + 2 * s + 2 * q, y + q), (x + 2 * s + q, y + q)]
        pts = ring + r2 + h2
        g = QgsGeometry.fromWkt("MULTIPOLYGON Z (((%s)),((%s),(%s)))" % (
            ",".join("%r %r 5" % p for p in ring), ",".join("%r %r 5" % p for p in r2), ",".join("%r %r 5" % p for p in h2)))
        ROWS_KIND[oid] = "multipart+hole"
    elif oid % 90 == 0:                       # one polygon, 2 holes + island inside the first hole
        a = s / 10
        h1 = [(x + a, y + a), (x + a, y + 5 * a), (x + 5 * a, y + 5 * a), (x + 5 * a, y + a), (x + a, y + a)]
        h2 = [(x + 6 * a, y + 6 * a), (x + 6 * a, y + 8 * a), (x + 8 * a, y + 8 * a), (x + 8 * a, y + 6 * a), (x + 6 * a, y + 6 * a)]
        isl = [(x + 2 * a, y + 2 * a), (x + 4 * a, y + 2 * a), (x + 4 * a, y + 4 * a), (x + 2 * a, y + 4 * a), (x + 2 * a, y + 2 * a)]
        pts = ring + h1 + h2 + isl
        g = QgsGeometry.fromWkt("MULTIPOLYGON Z (((%s),(%s),(%s)),((%s)))" % tuple(
            ",".join("%r %r 5" % p for p in rg) for rg in (ring, h1, h2, isl)))
        ROWS_KIND[oid] = "2 holes+island"
    elif oid % 50 == 0:                       # polygon with hole
        h = s / 4
        hole = [(x + h, y + h), (x + h, y + 2 * h), (x + 2 * h, y + 2 * h), (x + 2 * h, y + h), (x + h, y + h)]
        pts = ring + hole
        g = QgsGeometry.fromWkt("POLYGON Z ((%s),(%s))" % (
            ",".join("%r %r 5" % p for p in ring), ",".join("%r %r 5" % p for p in hole)))
    else:
        pts = ring
        g = QgsGeometry.fromWkt("POLYGON Z ((%s))" % ",".join("%r %r 5" % p for p in ring))
    # coordinates as Esri would round them to the grid
    gr = QgsGeometry.fromWkt(g.asWkt(17))
    ROWS.append(dict(oid=oid, name="bldg %d" % oid, height=float(oid % 30), created=None,
                     minx=x, miny=y, maxx=x + s, maxy=y + s, entity=8, numpts=len(pts),
                     area=abs(g.area()), points=blob(pts, [5.0] * len(pts)), wkb=bytes(gr.asWkb())))
EXT = (min(r["minx"] for r in ROWS), min(r["miny"] for r in ROWS), max(r["maxx"] for r in ROWS), max(r["maxy"] for r in ROWS))
SQL_LOG = []


# ------------------------------------------------------------------ fake database
class FakeRows(object):
    def __init__(self, names, rows):
        self.names, self._it = names, iter(rows)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def fetch_batch(self):
        out = []
        for r in self._it:
            out.append(r)
            if len(out) >= 500:
                break
        return out

    def close(self):
        pass


def _flt(sql, name):
    m = re.search(name + r"\s*/\s*([0-9.eE+-]+)\)", sql)
    return float(m.group(1)) if m else None


class FakeConn(B._ConnBase):
    def query(self, sql, params=None, arraysize=1000):
        SQL_LOG.append(sql)
        p = params or {}
        s = " ".join(sql.split())
        u = s.upper()
        if "FROM ALL_TAB_COLUMNS" in u and "DATA_TYPE = 'ST_GEOMETRY'" not in u and "DATA_TYPE='ST_GEOMETRY'" not in u:
            return FakeRows(["COLUMN_NAME", "DATA_TYPE", "DATA_PRECISION", "DATA_SCALE"], [
                ("OBJECTID", "NUMBER", 38, 0), ("NAME", "NVARCHAR2", None, None),
                ("HEIGHT", "NUMBER", 10, 2), ("CREATED", "DATE", None, None), ("SHAPE", "ST_GEOMETRY", None, None)])
        if "SDE.TABLE_REGISTRY" in u:
            return FakeRows(["ROWID_COLUMN"], [("OBJECTID",)])
        if "FROM DUAL" in u and "MIN(" in u:
            return FakeRows(["A", "B"], [(1, N)])
        if "SDE.ST_GEOMETRY_COLUMNS" in u:
            return FakeRows(["SRID"], [(123456,)])
        if "ST_SPATIAL_REFERENCES" in u:
            return FakeRows(["SRID", "FALSEX", "FALSEY", "XYUNITS", "FALSEZ", "ZUNITS", "DEFINITION", "ORGANIZATION", "ORG_COORDSYS_ID"],
                            [(123456, FX, FY, UX, 0.0, 1000.0, 'GEOGCS["GCS_WGS_1984"]', "EPSG", 4326)])
        if "FROM ALL_TABLES" in u:
            return FakeRows(["NUM_ROWS"], [(N,)])
        if "SDE.LAYERS" in u:
            return FakeRows(["MINX", "MINY", "MAXX", "MAXY"], [(-180.0, -90.0, 180.0, 90.0)])   # bogus SDE extent
        if "SELECT 1 FROM DUAL" in u or "SELECT USER FROM DUAL" in u:
            return FakeRows(["X"], [(1,)])
        if u.startswith("SELECT COUNT(*)"):
            return FakeRows(["C"], [(len(self._filter(s, p)),)])
        if "MIN(T.\"SHAPE\".MINX)" in u:
            return FakeRows(["A", "B", "C", "D"], [EXT])
        # ---------------- data queries
        rows = self._filter(s, p)
        if "ROWNUM <= 50" in u or "ROWNUM <= 300" in u:
            rows = rows[:300]
            if ".ENTITY" in u and ".POINTS" in u and "ST_ASBINARY" in u:       # decoder verification
                return FakeRows(["E", "N", "P", "W"], [(r["entity"], r["numpts"], r["points"], r["wkb"]) for r in rows])
            return FakeRows(["W"], [(r["wkb"],) for r in rows[:50]])
        sel = s[len("SELECT "):s.upper().index(" FROM ")]
        if sel.strip() == "1":
            return FakeRows(["X"], [(1,)] if rows else [])
        cols = [c.strip() for c in re.split(r",(?![^()]*\))", sel)]
        if "GROUP BY" in u:                                                  # overview merged pixels
            tol = _flt(s, r'"SHAPE"\.minx')
            groups = {}
            for r in rows:
                cx, cy = math.floor(r["minx"] / tol), math.floor(r["miny"] / tol)
                key = (cx, cy, r["name"], r["height"])
                groups.setdefault(key, []).append(r)
            out = []
            for (cx, cy, _n, _h), rs in groups.items():
                rr = rs[0]
                row = [min(x["oid"] for x in rs)]
                for c in cols[1:-3]:            # key, attrs..., cx, cy, marker
                    row.append(self._val(c, rr))
                out += [tuple(row + [cx, cy, -1])]
            return FakeRows([], out)
        if "FETCH FIRST" in u:
            rows = rows[:int(re.search(r"FETCH FIRST (\d+)", u).group(1))]
        return FakeRows([], [tuple(self._val(c, r) for c in cols) for r in rows])

    def _case(self, c, r):
        body = c[len("CASE "):-len(" END")]
        other = None
        if " ELSE " in body:
            body, other = body.rsplit(" ELSE ", 1)
        for clause in body.split("WHEN ")[1:]:
            cond, val = clause.rsplit(" THEN ", 1)
            if self._cond(cond.strip(), r):
                return self._v(val.strip(), r)
        return self._v(other.strip(), r) if other else None

    def _cond(self, cond, r):
        if "maxx - " in cond:
            tol = float(re.search(r"< ([0-9.eE+-]+)", cond).group(1))
            return r["maxx"] - r["minx"] < tol and r["maxy"] - r["miny"] < tol
        ents = [int(e) for e in re.search(r"IN \(([0-9,]+)\)", cond).group(1).split(",")]
        return r["entity"] in ents

    def _v(self, val, r):
        if val == "NULL":
            return None
        m = re.match(r'FLOOR\(t\."SHAPE"\.(minx|miny) / ([0-9.eE+-]+)\)', val)
        if m:
            return math.floor(r[m.group(1)] / float(m.group(2)))
        return self._val(val, r)

    def _val(self, c, r):
        cu = c.upper()
        if cu.startswith("CASE WHEN"):
            return self._case(c, r)
        if "ST_ASBINARY" in cu:
            return r["wkb"]
        for k in ("entity", "numpts", "area", "points"):
            if cu.endswith(".%s" % k.upper()):
                return r[k]
        name = re.search(r'"([A-Z]+)"$', c).group(1)
        return {"OBJECTID": r["oid"], "NAME": r["name"], "HEIGHT": r["height"], "CREATED": r["created"]}[name]

    def _filter(self, s, p):
        rows = ROWS
        s = s[s.upper().index(" FROM "):] if " FROM " in s.upper() else s      # WHERE part only
        m = re.search(r"st_envintersects\(t\.\"SHAPE\", :(\w+), :(\w+), :(\w+), :(\w+)\)", s)
        if m:
            x1, y1, x2, y2 = (p[g] for g in m.groups())
            rows = [r for r in rows if r["minx"] <= x2 and r["maxx"] >= x1 and r["miny"] <= y2 and r["maxy"] >= y1]
        elif ".minx <= :x2" in s:
            x1, y1, x2, y2 = p["x1"], p["y1"], p["x2"], p["y2"]
            rows = [r for r in rows if r["minx"] <= x2 and r["maxx"] >= x1 and r["miny"] <= y2 and r["maxy"] >= y1]
        m = re.search(r'GREATEST\(t\."SHAPE"\.(minx|miny), :(\w+)\) >= :t1 AND .*? (<=|<) :t2', s)
        if m:
            ax, b, op = m.group(1), p[m.group(2)], m.group(3)
            t1, t2 = p["t1"], p["t2"]
            rows = [r for r in rows if t1 <= max(r[ax], b) and (max(r[ax], b) <= t2 if op == "<=" else max(r[ax], b) < t2)]
        tol = None
        m = re.search(r'maxx - t\."SHAPE"\.minx (>=|<) ([0-9.eE+-]+)', s)
        if m:
            tol = float(m.group(2))
            if m.group(1) == ">=":
                rows = [r for r in rows if r["maxx"] - r["minx"] >= tol or r["maxy"] - r["miny"] >= tol]
            else:
                rows = [r for r in rows if r["maxx"] - r["minx"] < tol and r["maxy"] - r["miny"] < tol]
        m = re.search(r'"OBJECTID" = (\d+)', s) or re.search(r'"OBJECTID" = :k', s)
        if m and "= :k" in m.group(0):
            rows = [r for r in rows if r["oid"] == p["k"]]
        elif m:
            rows = [r for r in rows if r["oid"] == int(m.group(1))]
        m = re.search(r'"OBJECTID" IN \(([0-9,]+)\)', s)
        if m:
            ids = set(int(i) for i in m.group(1).split(","))
            rows = [r for r in rows if r["oid"] in ids]
        for op, v in re.findall(r'"OBJECTID" (<|>=) (\d+)', s):
            v = int(v)
            rows = [r for r in rows if (r["oid"] < v if op == "<" else r["oid"] >= v)]
        m = re.search(r'MOD\(t\."OBJECTID", (\d+)\) = (\d+)', s)
        if m:
            rows = [r for r in rows if r["oid"] % int(m.group(1)) == int(m.group(2))]
        m = re.search(r'"HEIGHT" > :(q\d+)', s)
        if m:
            rows = [r for r in rows if r["height"] > p[m.group(1)]]
        return rows

    def close(self):
        pass


class FakeBackend(object):
    kind = "fake"
    threaded = True

    def acquire(self):
        return FakeConn()


FAKE = FakeBackend()
B.get_backend = lambda u: FAKE
P.get_backend = lambda u: FAKE

# ------------------------------------------------------------------ tests
results = []


def pump():
    for _ in range(5):
        QgsApplication.processEvents()


REQS = []
_orig_build = P.StFeatureIterator._build
def _spy_build(self):
    sm = self._req.simplifyMethod()
    REQS.append((str(sm.methodType()), sm.tolerance(), self._req.filterRect().toString(3)))
    return _orig_build(self)
P.StFeatureIterator._build = _spy_build


def check(name, cond, info=""):
    pump()
    results.append((name, bool(cond), info))
    print("%-62s %s %s" % (name, "PASS" if cond else "FAIL", info))


try:
    check("provider registered", P.register_provider())
    B.set_parallel_degree(1)
    B.set_overview(True)
    B.set_overview_pixels(1.0)
    B.set_decoder_pref("auto")
    uri = "stcfg='X' table=\"GIS\".\"BLDG\" (SHAPE) stforce2d='0' stcalcext='1'"
    t0 = time.time()
    lyr = QgsVectorLayer(uri, "bldg", "stgeometry")
    check("layer valid", lyr.isValid(), "%.2fs" % (time.time() - t0))
    pr = lyr.dataProvider()
    check("wkb type MultiPolygonZ", QgsWkbTypes.displayString(lyr.wkbType()) == "MultiPolygonZ", QgsWkbTypes.displayString(lyr.wkbType()))
    check("crs EPSG:4326", lyr.crs().authid() == "EPSG:4326", lyr.crs().authid())
    check("fields", [f.name() for f in lyr.fields()] == ["OBJECTID", "NAME", "HEIGHT", "CREATED"], str([f.name() for f in lyr.fields()]))
    ext = lyr.extent()
    check("exact extent (not SDE world extent)", ext.width() < 1, ext.toString(4))
    src = pr.dataSourceUri()
    check("source stores stdec/stext/strows/stkeys", all(k in src for k in ("stdec=", "stext=", "strows=", "stkeys=")), src[:200])
    dec_line = [l for l in LOG if "local decoder" in l]
    check("decoder verified", dec_line and "ON" in dec_line[-1], dec_line[-1] if dec_line else "")
    check("featureCount", pr.featureCount() == N, str(pr.featureCount()))

    # reopen from the stored source: no verification query
    SQL_LOG.clear()
    l2 = QgsVectorLayer(src, "bldg2", "stgeometry")
    check("reopen valid", l2.isValid())
    check("reopen runs no verification", not any("ROWNUM <= 300" in q for q in SQL_LOG), "%d queries" % len(SQL_LOG))

    # full read, compare decoded geometry with server WKB
    B.set_timing(True)
    feats = {f.id(): f for f in lyr.getFeatures()}
    check("all features read", len(feats) == N, str(len(feats)))
    bad = 0
    for r in ROWS:
        g = feats[r["oid"]].geometry()
        ref = QgsGeometry()
        ref.fromWkb(r["wkb"])
        if not g.isGeosEqual(ref) and abs(g.area() - ref.area()) > 1e-6 * ref.area():
            bad += 1
    check("decoded geometries == Esri WKB", bad == 0, "mismatch %d" % bad)
    for kind in ("multipart+hole", "2 holes+island"):
        oids = [o for o, k in ROWS_KIND.items() if k == kind]
        ok = 0
        for o in oids:
            g = feats[o].geometry(); ref = QgsGeometry(); ref.fromWkb(ROWS[o - 1]["wkb"])
            parts = [(g.constGet().geometryN(i).numInteriorRings()) for i in range(g.constGet().numGeometries())]
            rparts = [(ref.constGet().geometryN(i).numInteriorRings()) for i in range(ref.constGet().numGeometries())]
            if parts == rparts and abs(g.area() - ref.area()) < 1e-9 * ref.area():
                ok += 1
        check("decoder: %s (%d rows) parts/holes/area exact" % (kind, len(oids)), ok == len(oids), "%d/%d %s" % (ok, len(oids), parts))
    f0 = feats[50]
    check("hole preserved", f0.geometry().constGet().geometryN(0).numInteriorRings() == 1)
    check("attributes", f0["NAME"] == "bldg 50" and f0["HEIGHT"] == 20.0, str(f0.attributes()))

    # attribute table style request
    r = QgsFeatureRequest().setFlags(Qgis.FeatureRequestFlag.NoGeometry).setSubsetOfAttributes([1])
    fs = list(lyr.getFeatures(r))
    check("no-geometry subset request", len(fs) == N and not fs[0].hasGeometry() and fs[0]["NAME"] is not None and fs[0]["HEIGHT"] is None)

    # expression filter pushed down
    SQL_LOG.clear()
    fs = list(lyr.getFeatures(QgsFeatureRequest().setFilterExpression('"HEIGHT" > 25')))
    check("expression filter", len(fs) == sum(1 for r in ROWS if r["height"] > 25), "%d rows" % len(fs))
    check("expression compiled to SQL", any('"HEIGHT" > :q0' in q for q in SQL_LOG))

    # identify-like exact rect
    rr = QgsRectangle(ROWS[9]["minx"], ROWS[9]["miny"], ROWS[9]["minx"] + 1e-6, ROWS[9]["miny"] + 1e-6)
    fs = list(lyr.getFeatures(QgsFeatureRequest().setFilterRect(rr).setFlags(Qgis.FeatureRequestFlag.ExactIntersect)))
    check("identify exact", any(f.id() == 10 for f in fs), str([f.id() for f in fs]))

    # fids
    fs = list(lyr.getFeatures(QgsFeatureRequest().setFilterFids([5, 77, 2999])))
    check("fid filter", sorted(f.id() for f in fs) == [5, 77, 2999])

    # subset string
    check("setSubsetString", lyr.setSubsetString('"HEIGHT" > 10'))
    lyr.setSubsetString("")

    # ---------------- rendering through QGIS' real renderer
    def render(layer, extent, size=(1200, 800)):
        ms = QgsMapSettings()
        ms.setLayers([layer])
        ms.setDestinationCrs(layer.crs())
        ms.setExtent(extent)
        ms.setOutputSize(QSize(*size))
        ms.setFlag(Qgis.MapSettingsFlag.UseRenderingOptimization, True)
        job = QgsMapRendererSequentialJob(ms)
        LOG.clear()
        SQL_LOG.clear()
        t = time.time()
        job.start()
        job.waitForFinished()
        pump()
        return job.renderedImage(), time.time() - t

    REQS.clear()
    img, dt = render(lyr, lyr.extent())
    print("    render request simplify:", REQS)
    starts = [l for l in LOG if "[timing]" in l and " start " in l]
    ends = [l for l in LOG if "[timing]" in l and " rows=" in l]
    check("render full extent", not img.isNull(), "%.2fs" % dt)
    check("overview activated in real render", any("overview=1px" in l for l in starts), starts[0] if starts else "no timing line")
    ends_ov = [l for l in LOG if "[timing]" in l and " rows=" in l]
    check("overview: one statement, sub-pixel rows merged on the client",
          len(SQL_LOG) == 1 and "FLOOR(" in SQL_LOG[0] and any(" cells=" in l and " cells=0 " not in l for l in ends_ov),
          ends_ov[-1] if ends_ov else "")
    if ends:
        print("   ", ends[-1])
    # non-black pixels sanity
    nonwhite = sum(1 for xx in range(0, img.width(), 7) for yy in range(0, img.height(), 7) if img.pixel(xx, yy) & 0xFFFFFF != 0xFFFFFF)
    check("something drawn", nonwhite > 50, "%d sampled pixels" % nonwhite)

    B.set_overview_pixels(3.0)
    render(lyr, lyr.extent())
    st = [l for l in LOG if " start " in l]
    check("overview tolerance setting used (3 px)", any("overview=3px" in l for l in st), st[0] if st else "")
    B.set_overview_pixels(1.0)
    # exactness outside rendering: a rect request without render optimization must return real geometry
    fs = list(lyr.getFeatures(QgsFeatureRequest().setFilterRect(lyr.extent())))
    check("non-render rect request is exact (no pixel cells)", len(fs) == N and abs(fs[0].geometry().area() - ROWS[fs[0].id()-1]["area"]) < 1e-15, str(len(fs)))
    B.set_overview(False)
    img2, dt2 = render(lyr, lyr.extent())
    check("render with overview off", not any("FLOOR(" in q for q in SQL_LOG), "%.2fs" % dt2)
    B.set_overview(True)

    # zoomed in: index plan
    zx, zy = ROWS[0]["minx"], ROWS[0]["miny"]
    img3, dt3 = render(lyr, QgsRectangle(zx - 0.002, zy - 0.002, zx + 0.002, zy + 0.002))
    st = [l for l in LOG if " start " in l]
    check("zoomed-in render uses index", any("spatial=index" in l for l in st), st[0] if st else "")

    # parallel sessions with partitions
    B.set_parallel_degree(2)
    img4, dt4 = render(lyr, lyr.extent())
    check("parallel render ok", not img4.isNull() and len(SQL_LOG) >= 2, "%d statements %.2fs" % (len(SQL_LOG), dt4))
    # index plan + parallel: the view is cut into strips; same features as one session, no duplicates
    zr = QgsRectangle(ROWS[0]["minx"] - 0.02, ROWS[0]["miny"] - 0.02, ROWS[0]["minx"] + 0.02, ROWS[0]["miny"] + 0.02)
    SQL_LOG.clear()
    ids_par = [f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(zr))]
    strips = [q for q in SQL_LOG if "GREATEST(" in q]
    B.set_parallel_degree(1)
    ids_one = [f.id() for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(zr))]
    check("parallel strips: index work split (%d strip statements)" % len(strips), len(strips) == 2)
    check("parallel strips: identical features, no duplicates", sorted(ids_par) == sorted(ids_one) and len(set(ids_par)) == len(ids_par),
          "%d vs %d" % (len(ids_par), len(ids_one)))
    B.set_parallel_degree(2)
    fs = list(lyr.getFeatures())
    check("parallel full read count", len(fs) == N, str(len(fs)))
    B.set_parallel_degree(1)

    # Force 2D layer
    l2d = QgsVectorLayer(uri.replace("stforce2d='0'", "stforce2d='1'"), "b2d", "stgeometry")
    check("force2d layer", l2d.isValid() and QgsWkbTypes.displayString(l2d.wkbType()) == "MultiPolygon",
          QgsWkbTypes.displayString(l2d.wkbType()))
    g = next(l2d.getFeatures()).geometry()
    check("force2d geometry 2D", not QgsWkbTypes.hasZ(g.wkbType()), QgsWkbTypes.displayString(g.wkbType()))

    # plugin dialog construction (GUI-less check of imports/enums)
    import src.dialog as DL
    check("dialog module imports", True)
except Exception:
    traceback.print_exc()
    check("NO EXCEPTION", False)

print("\n%d/%d passed" % (sum(1 for r in results if r[1]), len(results)))
crit = [l for l in LOG if "rror" in l or "Traceback" in l]
if crit:
    print("log errors:\n  " + "\n  ".join(crit[:10]))
app.exitQgis()
