# -*- coding: utf-8 -*-
"""
Read-only QGIS vector data provider for Esri SDE.ST_GEOMETRY columns in Oracle.

Layer source (QgsDataSourceUri syntax), e.g.
  stconn='MyOracleConn' table="OWNER"."PARCELS" (SHAPE) key='OBJECTID' type=MultiPolygonZ stcrs='EPSG:4326'
Optional params:
  stconn      name of a saved QGIS Oracle connection (else host/port/dbname/user/password/authcfg are used)
  key         integer unique column used as feature id (auto: SDE registration rowid column / OBJECTID)
  type        layer geometry type (auto-detected from data)
  stcrs       CRS override (auto: from SDE spatial reference, fallback EPSG:4326)
  stforce2d   '1' drop Z/M
  stevw       '1' read through the versioned view <TABLE>_EVW if it exists
  stgeomtype  point|line|polygon (override detection)
  sql         subset string (Oracle SQL WHERE clause)
"""

import datetime
import struct
import math
import decimal
import time
import traceback

from qgis.core import (QgsPointXY, QgsVertexId, QgsVectorDataProvider, QgsDataProvider, QgsAbstractFeatureSource, QgsAbstractFeatureIterator,
                       QgsFeatureIterator, QgsFeatureRequest, QgsFields, QgsField, QgsGeometry,
                       QgsRectangle, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException,
                       QgsDataSourceUri, QgsWkbTypes, QgsProviderMetadata, QgsProviderRegistry, QgsError)
from qgis.PyQt.QtCore import QDateTime, QDate, QTime, QByteArray

from . import compat as C
from .backend import get_backend, parallel_degree, ParallelRows, timing_enabled, index_max_rows, scan_threshold
from time import perf_counter
from itertools import islice

BATCH_ROWS = 1000
_CELL_P = struct.Struct("<BIIBIII10d").pack      # MultiPolygon( Polygon( 5-point square ) )
_CELL_PZ = struct.Struct("<BIIBIII15d").pack     # MultiPolygonZ
from .expr_compiler import compile_filter
from . import st_decoder
from .backend import decoder_pref, overview_enabled, overview_pixels, curves_mode, curve_tolerance
from . import st_curves
from qgis.core import QgsAbstractGeometry

DECODERS = {}   # layer label -> verified EsriDecoder (used by the benchmark)

PROVIDER_KEY = "stgeometry"
PROVIDER_DESC = "ST Geometry Provider (Esri ST_Geometry in Oracle)"
DEFAULT_CRS = "EPSG:4326"

LAST_ERRORS = {}   # "OWNER.TABLE" -> last error text (shown by the dialog)
_metadata = None


def qi(name):
    return '"%s"' % str(name).replace('"', '""')


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _kind(dtype, prec, scale):
    dt = (dtype or "").upper()
    if dt == "NUMBER":
        if scale is not None and scale == 0:
            if prec is not None and prec <= 9:
                return "int"
            return "int64"
        return "double"
    if dt in ("FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"):
        return "double"
    if dt in ("INTEGER", "SMALLINT"):
        return "int64"
    if dt in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "VARCHAR", "CLOB", "NCLOB", "ROWID", "UROWID"):
        return "string"
    if dt == "DATE" or dt.startswith("TIMESTAMP"):
        return "datetime"
    return None


def to_qgis_value(v):
    if v is None:
        return None
    t = type(v)
    if t is int or t is float or t is str:
        return v
    if isinstance(v, datetime.datetime):
        return QDateTime(QDate(v.year, v.month, v.day), QTime(v.hour, v.minute, v.second, v.microsecond // 1000))
    if isinstance(v, datetime.date):
        return QDate(v.year, v.month, v.day)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return QByteArray(bytes(v))
    if hasattr(v, "read"):  # LOB object (should not happen with output type handler)
        try:
            return v.read()
        except Exception:
            return None
    return v


def _chain(conn, parts):
    """Run several statements one after another on one connection, yielding all rows."""
    for sql, prm in parts:
        rows = conn.query(sql, prm)
        try:
            for r in rows:
                yield r
        finally:
            rows.close()


def wkb_to_geometry(b):
    if b.__class__ is bytes:          # hot path (python-oracledb)
        if not b:
            return None
        g = QgsGeometry()
        g.fromWkb(b)
        return g
    if b is None:
        return None
    if hasattr(b, "read"):
        b = b.read()
    if isinstance(b, QByteArray):
        ba = b
    else:
        if not b:
            return None
        ba = QByteArray(b if isinstance(b, bytes) else bytes(b))
    g = QgsGeometry()
    g.fromWkb(ba)
    return g


def normalize_geometry(g, target, force2d):
    """Make the geometry match the layer type (multi, Z/M)."""
    if g is None or g.isNull():
        return QgsGeometry()
    if g.wkbType() == target:          # fast path: nothing to do
        return g
    if QgsWkbTypes.isCurvedType(target):
        if not QgsWkbTypes.isCurvedType(g.wkbType()):
            g = QgsGeometry(g.constGet().toCurveType())
    elif QgsWkbTypes.isCurvedType(g.wkbType()):
        g = QgsGeometry(g.constGet().segmentize())
    t = g.wkbType()
    tz, tm = QgsWkbTypes.hasZ(target), QgsWkbTypes.hasM(target)
    if QgsWkbTypes.hasZ(t) and (force2d or not tz):
        g.get().dropZValue()
    if QgsWkbTypes.hasM(t) and (force2d or not tm):
        g.get().dropMValue()
    t = g.wkbType()
    if tz and not force2d and not QgsWkbTypes.hasZ(t):
        g.get().addZValue(0)
    if tm and not force2d and not QgsWkbTypes.hasM(t):
        g.get().addMValue(0)
    if QgsWkbTypes.isMultiType(target):
        if not QgsWkbTypes.isMultiType(g.wkbType()):
            g.convertToMultiType()
    elif QgsWkbTypes.isMultiType(g.wkbType()) and g.constGet().numGeometries() == 1:
        g = QgsGeometry(g.constGet().geometryN(0).clone())
    return g


def convert_polygon(g, mode):
    """Polygon -> 'boundary' (all rings as lines) or 'point' (centroid, moved inside if needed)."""
    if g is None or g.isNull():
        return g
    if mode == "boundary":
        b = g.constGet().boundary()
        return QgsGeometry(b) if b is not None else QgsGeometry()
    if mode == "point":
        c = g.centroid()
        if c.isNull() or not g.intersects(c):       # outside (U/L shapes, multipart) -> point on surface
            c = g.pointOnSurface()
        if not c.isNull() and QgsWkbTypes.hasZ(g.wkbType()):
            try:
                z = g.constGet().vertexAt(QgsVertexId(0, 0, 0)).z()
                c.get().addZValue(z if z == z else 0.0)
            except Exception:
                pass
        return c
    return g


GEOM_AS = ("boundary", "point")


class _Col(object):
    __slots__ = ("name", "sql", "kind", "lob")

    def __init__(self, name, kind, lob=False):
        self.name = name
        self.sql = "t." + qi(name)
        self.kind = kind
        self.lob = lob          # CLOB/NCLOB: cannot be used in GROUP BY


# =====================================================================
class StFeatureSource(QgsAbstractFeatureSource):
    def __init__(self, p):
        super().__init__()
        self.backend = p._backend
        self.relation = p._relation
        self.geom_sql = "t." + qi(p._geom)
        self.key_sql = "t." + qi(p._key)
        self.cols = list(p._cols)
        self.colmap = {c.name.upper(): c for c in self.cols}
        self.fields = QgsFields(p._fields)
        self.wkb = p._wkb
        self.force2d = p._force2d
        self.crs = QgsCoordinateReferenceSystem(p._crs)
        self.extent = QgsRectangle(p._extent)
        self.subset = p._subset
        self.key_range = p._key_range
        self.parallel = parallel_degree() if getattr(self.backend, "threaded", False) else 1
        self.timing = timing_enabled()
        self.label = p._label
        self.ext_exact = p._ext_exact
        self.num_rows = p._num_rows
        self.index_max_rows = index_max_rows()
        self.dec = p._dec if decoder_pref() != "server" else None
        self.overview = overview_enabled()
        self.overview_px = overview_pixels()
        self.cad_sql = ("t." + qi(p._cad)) if (p._cad and curves_mode() != "off") else None
        self.curve_mode = curves_mode()
        self.curve_hint = p._curve_hint
        self.keep_z = QgsWkbTypes.hasZ(p._wkb) and not p._force2d
        self.geom_as = p._geom_as
        ttype, tval = curve_tolerance()
        TT = getattr(QgsAbstractGeometry, "SegmentationToleranceType", QgsAbstractGeometry)
        if ttype == "angle":
            self.seg = (math.radians(tval), TT.MaximumAngle)
        else:
            d = tval / 111320.0 if p._crs.isGeographic() else tval     # metres -> degrees for lat/lon data
            self.seg = (d, TT.MaximumDifference)
        self.gcat = QgsWkbTypes.geometryType(p._wkb)

    def getFeatures(self, request=QgsFeatureRequest()):
        return QgsFeatureIterator(StFeatureIterator(self, request))


class StFeatureIterator(QgsAbstractFeatureIterator):
    def __init__(self, source, request):
        super().__init__(request)
        self._s = source
        self._req = QgsFeatureRequest(request)
        self._conn = None
        self._rows = None
        self._closed = False
        self._empty = False
        self._engine = None
        self._transform = QgsCoordinateTransform()
        try:
            self._transform = self._req.calculateTransform(source.crs)
        except AttributeError:
            dcrs = self._req.destinationCrs()
            if dcrs.isValid() and dcrs != source.crs:
                self._transform = QgsCoordinateTransform(source.crs, dcrs, self._req.transformContext())
        self._has_transform = self._transform.isValid()
        self._parts = []
        self._spatial_used = False
        self._plan_info = ""
        self._srv_conn = None
        self._n_fallback = 0
        self._batch = []
        self._next_batch = None
        self._pre_typed = False
        self._blen = 0
        self._bi = 0
        self._pre = None
        self._batch_dec = False          # set in _build once we know geometry is read
        self._n_cells = 0
        self._ov = False
        self._cell_len = -1
        self._view_cond = None
        self._seen = None
        self._timing = source.timing
        self._t0 = None
        self._t_db = 0.0
        self._t_build = 0.0
        self._n = 0
        self._rect = QgsRectangle()
        try:
            self._rect = self.filterRectToSourceCrs(self._transform)
        except QgsCsException:
            self._empty = True
        try:
            self._build()
        except Exception as e:
            C.log("Cannot build query: %s\n%s" % (e, traceback.format_exc()), "critical")
            self._empty = True

    # ----------------------------------------------------------- query
    def _build(self):
        s, req = self._s, self._req
        ftype = req.filterType()
        flags = req.flags()
        nf = s.fields.count()
        if C.has_flag(flags, C.FLAG_SUBSET) and ftype != C.FILTER_EXPR:
            idx = sorted(i for i in set(req.subsetOfAttributes()) if 0 <= i < nf)
        else:
            idx = list(range(nf))
        self._idx = idx
        self._nf = nf
        self._want_geom = not C.has_flag(flags, C.FLAG_NO_GEOMETRY)
        rect = self._rect
        has_rect = rect is not None and not rect.isNull()
        self._exact = has_rect and C.has_flag(flags, C.FLAG_EXACT)
        # DistanceWithin spatial filter: exact distance test on the client
        self._dist_geom, self._dist = None, None
        try:
            if C.SPATIAL_DISTANCE is not None and req.spatialFilterType() == C.SPATIAL_DISTANCE:
                rg = QgsGeometry(req.referenceGeometry())
                if not rg.isNull():
                    if self._transform.isValid() and not self._transform.isShortCircuited():
                        rg.transform(self._transform, C.TRANSFORM_REVERSE)
                    self._dist_geom, self._dist = rg, float(req.distanceWithin())
        except Exception as e:
            C.log("DistanceWithin filter ignored: %s" % e, "warning")
        fe = req.filterExpression() if ftype == C.FILTER_EXPR else None
        self._need_geom = (self._want_geom or self._exact or self._dist_geom is not None
                           or bool(fe is not None and fe.needsGeometry()))
        if self._exact:
            rg = QgsGeometry.fromRect(rect)
            self._engine = QgsGeometry.createGeometryEngine(rg.constGet())
            self._engine.prepareGeometry()
            self._rect_geom = rg

        # ---- fast overview (map rendering only): QGIS passes the pixel size as simplification
        # tolerance. Features whose stored envelope is smaller than the cell size get NO geometry
        # columns (no blob, no Esri conversion) - only their cell; they are merged per cell and
        # style value on the client. One query, so the spatial filter runs once.
        self._ov = False
        self._tol = 0.0
        try:
            sm = req.simplifyMethod()
            if (s.overview and self._want_geom and not self._exact and self._dist_geom is None
                    and C.SIMPLIFY_NONE is not None and sm.methodType() != C.SIMPLIFY_NONE
                    and sm.tolerance() > 0 and ftype in (C.FILTER_NONE, C.FILTER_EXPR)
                    and not (fe is not None and fe.needsGeometry())):
                # QGIS tolerance = simplification threshold (px) x map units per pixel
                px = float(sm.tolerance()) / (float(sm.threshold()) if sm.threshold() > 0 else 1.0)
                self._ov = True
                self._tol = px * s.overview_px
                self._plan_info += " overview=%.3gpx cell=%.3g" % (s.overview_px, self._tol)
        except Exception as e:
            C.log("overview mode not available: %s" % e, "info")

        g = s.geom_sql
        if self._ov:
            tol = "%r" % self._tol
            small = "%s.maxx - %s.minx < %s AND %s.maxy - %s.miny < %s" % (g, g, tol, g, g, tol)

            def W(e):                                          # no blob / conversion for sub-cell rows
                return "CASE WHEN %s THEN NULL ELSE %s END" % (small, e)
        else:
            def W(e):
                return e
        sel = [s.key_sql] + [s.cols[i].sql for i in idx]
        if self._need_geom:
            if s.dec is not None:
                sel += ["%s.entity" % g, "%s.numpts" % g, "%s.area" % g, W("%s.points" % g),
                        ("CASE WHEN %s THEN NULL WHEN %s.entity IN (%s) THEN NULL ELSE sde.st_asbinary(%s) END"
                         % (small, g, ",".join(str(e) for e in s.dec.entities), g)) if self._ov else
                        ("CASE WHEN %s.entity IN (%s) THEN NULL ELSE sde.st_asbinary(%s) END"
                         % (g, ",".join(str(e) for e in s.dec.entities), g))]
            else:
                sel.append(W("sde.st_asbinary(%s)" % g))
        self._has_cad = bool(self._need_geom and s.cad_sql)
        if self._has_cad:
            sel.append(W(s.cad_sql))
        if self._ov:                                           # last two columns: cell of sub-cell rows
            sel += ["CASE WHEN %s THEN FLOOR(%s.minx / %s) END" % (small, g, tol),
                    "CASE WHEN %s THEN FLOOR(%s.miny / %s) END" % (small, g, tol)]
            self._seen = set()
        where, params = [], {}
        if s.subset:
            where.append("(%s)" % s.subset)
        # skip the spatial filter only when the view really contains a plausible layer extent
        ext = s.extent
        ext_ok = (not ext.isNull()) and ext.width() > 0 and ext.height() > 0
        has_spatial = has_rect and not (ext_ok and rect.contains(ext))
        self._spatial_used = "none"
        if has_spatial:
            # Index or plain scan? The ST grid index costs ~70 us per returned row (measured),
            # a full scan with envelope predicates has a fixed cost. With an exact data extent
            # and row statistics we estimate the rows in view and pick the cheaper plan.
            use_index = True
            self._plan_info = "exact_extent=%s rows=%s" % ("yes" if s.ext_exact else "NO", s.num_rows)
            if s.ext_exact and ext_ok:
                inter = rect.intersect(ext)
                frac = 0.0 if inter.isNull() else (inter.width() * inter.height()) / (ext.width() * ext.height())
                if s.num_rows:
                    est = frac * s.num_rows
                    thr = scan_threshold(s.index_max_rows, s.num_rows)
                    use_index = est <= thr
                    self._plan_info += " view=%.3f est=%d scan_above=%d" % (frac, int(est), thr)
                else:  # no row count: decide by area share of the data extent
                    use_index = frac <= 0.25
                    self._plan_info += " view=%.3f" % frac
            if use_index:
                self._view_cond = "sde.st_envintersects(%s, :x1, :y1, :x2, :y2) = 1" % s.geom_sql
                where.append(self._view_cond)
                self._spatial_used = "index"
            else:
                g = s.geom_sql
                where.append("%s.minx <= :x2 AND %s.maxx >= :x1 AND %s.miny <= :y2 AND %s.maxy >= :y1" % (g, g, g, g))
                self._spatial_used = "scan"
            params.update(x1=float(rect.xMinimum()), y1=float(rect.yMinimum()),
                          x2=float(rect.xMaximum()), y2=float(rect.yMaximum()))
        exact_sql = True
        if ftype == C.FILTER_FID:
            where.append("%s = %d" % (s.key_sql, int(req.filterFid())))
        elif ftype == C.FILTER_FIDS:
            fids = sorted(int(x) for x in req.filterFids())
            if not fids:
                self._empty = True
            elif len(fids) <= 50000:
                chunks = [fids[i:i + 1000] for i in range(0, len(fids), 1000)]
                where.append("(" + " OR ".join("%s IN (%s)" % (s.key_sql, ",".join(str(f) for f in ch))
                                               for ch in chunks) + ")")
            else:
                exact_sql = False
        elif ftype == C.FILTER_EXPR:
            fe = req.filterExpression()
            text = fe.expression() if fe is not None else ""
            sqlx, binds, complete = compile_filter(text, s.colmap)
            if sqlx:
                where.append("(%s)" % sqlx)
                params.update(binds)
            if not complete:
                exact_sql = False
        elif ftype != C.FILTER_NONE:
            exact_sql = False

        base_sql = "SELECT %s FROM %s t" % (", ".join(sel), s.relation)

        def make_sql(extra=None):
            w = where + ([extra] if extra else [])
            return base_sql + (" WHERE " + " AND ".join(w) if w else "")

        lim = req.limit()
        try:
            has_order = bool(req.orderBy().list())
        except Exception:
            has_order = True
        push_limit = (lim is not None and lim >= 0 and exact_sql and not self._exact and not has_order
                      and self._dist_geom is None)
        if self._ov:
            push_limit = False
        sql = make_sql()
        if push_limit:
            sql += " FETCH FIRST %d ROWS ONLY" % int(lim)
        self._sql, self._params = sql, params

        # ---- parallel sessions: big reads are split into N parts fetched concurrently.
        #  index plan : the VIEW is cut into N strips; each session searches the ST index only in
        #               its strip, and each feature belongs to exactly one strip (by its envelope
        #               corner clipped to the view) -> index work is divided, never repeated
        #  scan / none: OBJECTID ranges -> each session reads only its own part of the table
        self._parts = [(sql, params)]
        n = s.parallel
        if (n > 1 and self._want_geom and not self._exact and self._dist_geom is None and not push_limit
                and ftype in (C.FILTER_NONE, C.FILTER_EXPR)):
            if self._spatial_used == "index":
                x1, y1, x2, y2 = params["x1"], params["y1"], params["x2"], params["y2"]
                vertical = (x2 - x1) >= (y2 - y1)
                lo, hi = (x1, x2) if vertical else (y1, y2)
                ax, a1, a2 = ("minx", ":x1", ":tx") if vertical else ("miny", ":y1", ":ty")
                tile_idx = ("sde.st_envintersects(%s, :t1, :y1, :t2, :y2) = 1" if vertical else
                            "sde.st_envintersects(%s, :x1, :t1, :x2, :t2) = 1") % g
                parts = []
                for k in range(n):
                    t1 = lo + (hi - lo) * k / n
                    t2 = hi if k == n - 1 else lo + (hi - lo) * (k + 1) / n
                    corner = "GREATEST(%s.%s, %s)" % (g, ax, a1)
                    own = "%s >= :t1 AND %s %s :t2" % (corner, corner, "<=" if k == n - 1 else "<")
                    w = [tile_idx if c == self._view_cond else c for c in where] + [own]
                    parts.append((base_sql + " WHERE " + " AND ".join(w), dict(params, t1=t1, t2=t2)))
                self._parts = parts
            elif s.key_range is not None:
                lo, hi = s.key_range
                if hi - lo >= n * 1000:
                    step = (hi - lo + 1) / float(n)
                    b = [lo + int(step * k) for k in range(1, n)]
                    conds = ["%s < %d" % (s.key_sql, b[0])]
                    conds += ["%s >= %d AND %s < %d" % (s.key_sql, b[k - 1], s.key_sql, b[k]) for k in range(1, n - 1)]
                    conds.append("%s >= %d" % (s.key_sql, b[-1]))
                    self._parts = [(make_sql("(%s)" % c), params) for c in conds]

        # ---- precomputed attribute placement (hot loop helpers)
        self._all_attrs = idx == list(range(nf))
        self._conv_pos = [(1 + j, i) for j, i in enumerate(idx) if s.cols[i].kind == "datetime"]
        self._pairs = [(1 + j, i) for j, i in enumerate(idx)]
        self._na1 = 1 + len(idx)                     # end of the attribute columns in a row
        self._geom_ok_type = s.wkb
        # batch decoding only when the geometry columns (entity, numpts, area, points, wkb) are selected
        self._batch_dec = bool(self._need_geom and s.dec is not None and getattr(s.dec, "batch_capable", False))
        # batch output is the plain Multi(Z) layer type unless the layer converts or uses curves
        self._pre_typed = bool(self._batch_dec and not s.geom_as and not QgsWkbTypes.isCurvedType(s.wkb)
                               and not QgsWkbTypes.hasM(s.wkb) and s.dec.keep_z == QgsWkbTypes.hasZ(s.wkb))

    # ----------------------------------------------------------- iteration
    def _open(self):
        if self._timing:
            C.log("[timing] %s start spatial=%s geom=%s decoder=%s parts=%d %s" % (
                self._s.label, self._spatial_used, "yes" if self._need_geom else "no",
                "local" if self._s.dec is not None else "server", len(self._parts) or 1,
                self._plan_info))
        self._next_batch = None
        if self._s.parallel > 1:
            # background fetch threads (1..N partitions): network/DB work overlaps
            # with feature building in this thread
            self._rows = ParallelRows(self._s.backend, self._parts)
        else:
            self._conn = self._s.backend.acquire()
            if len(self._parts) > 1:
                self._rows = _chain(self._conn, self._parts)
            else:
                self._rows = self._conn.query(self._sql, self._params)
        self._next_batch = getattr(self._rows, "next_batch", None)

    def _log_timing(self):
        t0, self._t0 = self._t0, None
        if t0 is None:
            return
        total = perf_counter() - t0
        C.log("[timing] %s rows=%d cells=%d fallback=%d spatial=%s geom=%s parts=%d wait_db=%.2fs build=%.2fs qgis=%.2fs total=%.2fs" % (
            self._s.label, self._n, self._n_cells, self._n_fallback, self._spatial_used, "yes" if self._need_geom else "no",
            len(self._parts), self._t_db, self._t_build, max(0.0, total - self._t_db - self._t_build), total))

    def _predecode(self, batch):
        """Batch-decode the POINTS blobs of rows that need the local decoder (numpy)."""
        cad = self._has_cad
        ov = self._ov
        idx, ents, npts, blobs = [], [], [], []
        for j, row in enumerate(batch):
            if ov:
                if row[-2] is not None:
                    continue                       # sub-cell row: no geometry
                row = row[:-2]
            if cad:
                if row[-1] is not None:
                    continue                       # true curve row
                r = row[:-1]
            else:
                r = row
            if r[-1] is None and r[-2] is not None:
                idx.append(j)
                ents.append(r[-5])
                npts.append(r[-4])
                blobs.append(r[-2])
        if not idx:
            return None
        res = self._s.dec.decode_batch(ents, npts, blobs)
        pre = [None] * len(batch)
        for j, g in zip(idx, res):
            pre[j] = g
        return pre

    def _cell_geom(self, cx, cy):
        """One-pixel stand-in geometry for merged sub-pixel features (rendering only)."""
        t = self._tol
        x0, y0 = float(cx) * t, float(cy) * t
        cat = self._s.gcat
        wt = self._s.wkb
        if not QgsWkbTypes.isCurvedType(wt):          # fast path: WKB directly in the layer type
            hz = QgsWkbTypes.hasZ(wt)
            if cat == C.GEOM_POLYGON:
                x1, y1 = x0 + t, y0 + t
                if hz:
                    w = _CELL_PZ(1, 1006, 1, 1, 1003, 1, 5, x0, y0, 0.0, x1, y0, 0.0, x1, y1, 0.0, x0, y1, 0.0, x0, y0, 0.0)
                else:
                    w = _CELL_P(1, 6, 1, 1, 3, 1, 5, x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
                g = QgsGeometry()
                g.fromWkb(w)
                if not QgsWkbTypes.hasM(wt):
                    return g
        if cat == C.GEOM_POLYGON:
            g = QgsGeometry.fromRect(QgsRectangle(x0, y0, x0 + t, y0 + t))
        elif cat == C.GEOM_LINE:
            g = QgsGeometry.fromPolylineXY([QgsPointXY(x0, y0), QgsPointXY(x0 + t, y0 + t)])
        else:
            g = QgsGeometry.fromPointXY(QgsPointXY(x0 + t / 2.0, y0 + t / 2.0))
        return normalize_geometry(g, self._s.wkb, self._s.force2d)

    def _server_geom(self, fid):
        """Esri conversion for a single row the local decoder did not accept."""
        try:
            if self._srv_conn is None:
                self._srv_conn = self._s.backend.acquire()
            r = self._srv_conn.fetchone("SELECT sde.st_asbinary(%s) FROM %s t WHERE %s = :k"
                                        % (self._s.geom_sql, self._s.relation, self._s.key_sql), {"k": int(fid)})
            self._n_fallback += 1
            return wkb_to_geometry(r[0]) if r else None
        except Exception as e:
            C.log("server fallback failed for %s: %s" % (fid, e), "warning")
            return None

    def _release(self):
        if self._timing:
            self._log_timing()
        sc, self._srv_conn = self._srv_conn, None
        if sc is not None:
            try:
                sc.close()
            except Exception:
                pass
        r, self._rows = self._rows, None
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        c, self._conn = self._conn, None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def fetchFeature(self, f):  # noqa: N802
        if self._closed or self._empty:
            return False
        try:
            timing = self._timing
            if self._rows is None:
                if timing:
                    self._t0 = perf_counter()
                self._open()
                if timing:
                    self._t_db += perf_counter() - self._t0
            s = self._s
            nf = self._nf
            while True:
                # rows are consumed in batches so the local decoder can decode a whole batch at once
                if self._bi >= self._blen:
                    if timing:
                        ta = perf_counter()
                    nb = self._next_batch
                    self._batch = nb() if nb is not None else list(islice(self._rows, BATCH_ROWS))
                    if timing:
                        tb = perf_counter()
                        self._t_db += tb - ta
                    self._blen = len(self._batch)
                    self._bi = 0
                    if not self._blen:
                        self._release()
                        self._closed = True
                        return False
                    self._pre = self._predecode(self._batch) if self._batch_dec else None
                    if timing:
                        self._t_build += perf_counter() - tb
                row = self._batch[self._bi]
                pre = self._pre[self._bi] if self._pre is not None else None
                self._bi += 1
                if timing:
                    tb = perf_counter()
                fid = row[0]
                if fid is None:
                    continue
                geom = None
                typed = False                                  # geometry already in the layer type?
                cell = False
                if self._ov:
                    cx = row[-2]
                    if cx is not None:
                        key = (cx, row[-1]) + row[1:self._na1]
                        if key in self._seen:
                            continue                           # same cell + same style values: drawn already
                        self._seen.add(key)
                        geom = self._cell_geom(cx, row[-1])
                        self._n_cells += 1
                        cell = True
                    else:
                        row = row[:-2]
                if not cell and self._need_geom:
                    if self._has_cad:
                        cad = row[-1]
                        row = row[:-1]
                        if cad is not None:
                            geom, _off = st_curves.to_geometry(bytes(cad), s.keep_z, s.curve_hint)
                            if geom is not None and s.curve_mode == "segmentize":
                                geom = QgsGeometry(geom.constGet().segmentize(s.seg[0], s.seg[1]))
                    w = row[-1]
                    if geom is not None:
                        pass                                   # true curve already built
                    elif pre is not None:
                        geom = pre                             # decoded with the batch
                        typed = self._pre_typed
                    elif s.dec is None or w is not None:
                        geom = wkb_to_geometry(w)
                    elif row[-2] is not None:
                        geom = s.dec.geometry(row[-5], row[-4], row[-3], row[-2])
                        if geom is None:          # not understood -> Esri converts this one row
                            geom = self._server_geom(fid)
                    if s.geom_as and geom is not None:
                        geom = convert_polygon(geom, s.geom_as)
                    if self._exact:
                        if geom is None or geom.isNull() or not self._engine.intersects(geom.constGet()):
                            continue
                    if self._dist_geom is not None:
                        if geom is None or geom.isNull() or geom.distance(self._dist_geom) > self._dist:
                            continue
                f.setFields(s.fields, False)
                f.setId(fid if fid.__class__ is int else int(fid))
                if self._all_attrs:
                    attrs = list(row[1:1 + nf])
                else:
                    attrs = [None] * nf
                    for j, i in self._pairs:
                        attrs[i] = row[j]
                for j, i in self._conv_pos:
                    attrs[i] = to_qgis_value(row[j])
                f.setAttributes(attrs)
                if typed and self._want_geom:
                    f.setGeometry(geom)
                    if self._has_transform:
                        self.geometryToDestinationCrs(f, self._transform)
                elif self._want_geom and geom is not None and not geom.isNull():
                    if geom.wkbType() != self._geom_ok_type:
                        geom = normalize_geometry(geom, s.wkb, s.force2d)
                    f.setGeometry(geom)
                    if self._has_transform:
                        self.geometryToDestinationCrs(f, self._transform)
                else:
                    f.clearGeometry()
                f.setValid(True)
                if timing:
                    self._n += 1
                    self._t_build += perf_counter() - tb
                return True
        except Exception as e:
            C.log("Error fetching features: %s\nSQL: %s\n%s" % (e, self._sql, traceback.format_exc()), "critical")
            self._release()
            self._closed = True
            return False

    def rewind(self):
        self._release()
        self._closed = False
        self._batch, self._blen, self._bi, self._pre = [], 0, 0, None
        return True

    def close(self):
        self._release()
        self._closed = True
        return True

    def __iter__(self):
        return self

    def __next__(self):
        from qgis.core import QgsFeature
        f = QgsFeature()
        if not self.nextFeature(f):
            raise StopIteration
        return f

    def __del__(self):
        try:
            self._release()
        except Exception:
            pass


# =====================================================================
class StGeometryProvider(QgsVectorDataProvider):

    def __init__(self, uri="", options=None, flags=None):
        if options is None:
            options = QgsDataProvider.ProviderOptions()
        try:
            if flags is None:
                super().__init__(uri, options)
            else:
                super().__init__(uri, options, flags)
        except TypeError:
            super().__init__(uri)
        self._valid = False
        self._error = ""
        self._backend = None
        self._fields = QgsFields()
        self._cols = []
        self._wkb = C.WKB_UNKNOWN
        self._crs = QgsCoordinateReferenceSystem()
        self._extent = QgsRectangle()
        self._count = None
        self._subset = ""
        self._key = ""
        self._key_index = -1
        self._geom = ""
        self._relation = ""
        self._base = ""
        self._force2d = False
        self._label = ""
        self._key_range = None
        self._ext_exact = False
        self._num_rows = None
        self._dec = None
        self._dec_info = "not checked"
        self._cad = None
        self._curve_ok = False
        self._curve_hint = None
        self._curve_cache = None
        self._geom_as = ""
        try:
            t0 = time.time()
            self._load(uri)
            self._valid = True
            C.log("Layer %s ready in %.2fs (%s, key=%s, %s, %d fields, backend=%s)" % (
                self._label, time.time() - t0, QgsWkbTypes.displayString(self._wkb), self._key,
                self._crs.authid() or "custom CRS", self._fields.count(), self._backend.kind))
        except Exception as e:
            self._error = str(e)
            LAST_ERRORS[self._label or uri] = self._error
            C.log("Cannot open layer %s: %s\n%s" % (self._label or uri, e, traceback.format_exc()), "critical")
            try:
                self.setError(QgsError(self._error, PROVIDER_KEY))
            except Exception:
                pass

    # ----------------------------------------------------------- loading
    def _load(self, uri):
        u = QgsDataSourceUri(uri)
        owner = (u.schema() or "").strip().strip('"')
        table = (u.table() or "").strip().strip('"')
        if not table:
            raise RuntimeError("No table in layer source.")
        self._force2d = u.param("stforce2d") == "1" if u.hasParam("stforce2d") else False
        self._backend = get_backend(u)
        conn = self._backend.acquire()
        try:
            if not owner:
                owner = conn.fetchone("SELECT USER FROM dual")[0]
            cols_sql = ("SELECT column_name, data_type, data_precision, data_scale FROM all_tab_columns "
                        "WHERE owner = :o AND table_name = :t ORDER BY column_id")
            cols = conn.fetchall(cols_sql, {"o": owner, "t": table})
            if not cols and (owner != owner.upper() or table != table.upper()):
                owner, table = owner.upper(), table.upper()
                cols = conn.fetchall(cols_sql, {"o": owner, "t": table})
            self._label = "%s.%s" % (owner, table)
            if not cols:
                raise RuntimeError("Table %s not found or no SELECT privilege." % self._label)
            base = "%s.%s" % (qi(owner), qi(table))
            self._base = base

            geom = (u.geometryColumn() or "").strip().strip('"')
            st_cols = [c[0] for c in cols if (c[1] or "").upper() == "ST_GEOMETRY"]
            if not geom:
                if not st_cols:
                    raise RuntimeError("Table %s has no ST_GEOMETRY column." % self._label)
                geom = st_cols[0]
            self._geom = geom

            # ---- fields
            fields = QgsFields()
            skipped = []
            for name, dtype, prec, scale in cols:
                if name == geom or (dtype or "").upper() == "ST_GEOMETRY":
                    continue
                kind = _kind(dtype, _int_or_none(prec), _int_or_none(scale))
                if kind is None:
                    skipped.append("%s(%s)" % (name, dtype))
                    continue
                fld = QgsField(name, C.make_field_type(kind), str(dtype or ""))
                fields.append(fld)
                self._cols.append(_Col(name, kind, (dtype or "").upper() in ("CLOB", "NCLOB")))
            if skipped:
                C.log("%s: unsupported columns skipped: %s" % (self._label, ", ".join(skipped)), "info")
            self._fields = fields
            names = [c.name for c in self._cols]

            # ---- key column
            key = (u.keyColumn() or "").strip().strip('"')
            if key and key not in names:
                raise RuntimeError("Key column %s not found in %s." % (key, self._label))
            if not key:
                try:
                    r = conn.fetchone("SELECT rowid_column FROM sde.table_registry WHERE owner = :o AND table_name = :t",
                                      {"o": owner, "t": table})
                    if r and r[0] in names:
                        key = r[0]
                except Exception as e:
                    C.log("sde.table_registry lookup failed: %s" % e, "info")
            if not key and "OBJECTID" in names:
                key = "OBJECTID"
            if not key:
                rows = conn.fetchall(
                    "SELECT ic.column_name FROM all_indexes i JOIN all_ind_columns ic "
                    "ON ic.index_owner = i.owner AND ic.index_name = i.index_name "
                    "WHERE i.table_owner = :o AND i.table_name = :t AND i.uniqueness = 'UNIQUE' "
                    "AND (SELECT COUNT(*) FROM all_ind_columns c2 WHERE c2.index_owner = i.owner "
                    "AND c2.index_name = i.index_name) = 1", {"o": owner, "t": table})
                for (cn,) in rows:
                    if cn in names and self._cols[names.index(cn)].kind in ("int", "int64"):
                        key = cn
                        break
            if not key:
                raise RuntimeError("No integer unique key column found in %s; add key='COLUMN' to the layer source." % self._label)
            self._key = key
            self._key_index = names.index(key)
            if u.hasParam("stkeys"):
                try:
                    lo, hi = u.param("stkeys").split(",")
                    self._key_range = (int(lo), int(hi))   # partitions are open-ended: a stale range is safe
                except Exception:
                    self._key_range = None
            if self._key_range is None and getattr(self._backend, "threaded", False):
                try:  # two scalar subqueries -> two INDEX FULL SCAN (MIN/MAX) lookups, not a full scan
                    r = conn.fetchone("SELECT (SELECT MIN(%s) FROM %s), (SELECT MAX(%s) FROM %s) FROM dual"
                                      % (qi(key), base, qi(key), base))
                    if r and r[0] is not None and r[1] is not None:
                        self._key_range = (int(r[0]), int(r[1]))
                except Exception as e:
                    C.log("%s: key range lookup failed (no parallel full scans): %s" % (self._label, e), "info")

            # ---- relation (optionally versioned view)
            self._relation = base
            if u.hasParam("stevw") and u.param("stevw") == "1":
                r = conn.fetchone("SELECT COUNT(*) FROM all_views WHERE owner = :o AND view_name = :v",
                                  {"o": owner, "v": table + "_EVW"})
                if r and int(r[0]) > 0:
                    self._relation = "%s.%s" % (qi(owner), qi(table + "_EVW"))
                else:
                    C.log("%s: no versioned view %s_EVW, using base table" % (self._label, table), "warning")

            # ---- CRS
            crs = QgsCoordinateReferenceSystem()
            if u.hasParam("stcrs") and u.param("stcrs"):
                crs = QgsCoordinateReferenceSystem(u.param("stcrs"))
            if not crs.isValid():
                crs = self._detect_crs(conn, owner, table, geom)
            if not crs.isValid():
                crs = QgsCoordinateReferenceSystem(DEFAULT_CRS)
            self._crs = crs

            # ---- geometry type
            wkb = u.wkbType()
            if wkb is None or wkb == C.WKB_UNKNOWN:
                wkb = self._detect_type(conn, base, geom, u.param("stgeomtype") if u.hasParam("stgeomtype") else "")
            if self._force2d:
                wkb = QgsWkbTypes.dropM(QgsWkbTypes.dropZ(wkb))
            wkb = QgsWkbTypes.multiType(wkb)
            self._wkb = wkb

            # ---- local decoder for the Esri POINTS blob (verified against sde.st_asbinary)
            self._dec_cache = None
            if decoder_pref() != "server":
                cached = u.param("stdec") if u.hasParam("stdec") else ""
                if cached == "off":
                    self._dec_info = "off (verification failed when the layer was added; re-add to retry)"
                    self._dec_cache = "off"
                elif cached:
                    self._dec = st_decoder.EsriDecoder.from_config(cached)
                    self._dec_info = ("ON (verified when added) entities=%s" % list(self._dec.entities)
                                      if self._dec is not None else "off (bad cached config)")
                    self._dec_cache = cached if self._dec is not None else None
                if not cached or (self._dec is None and self._dec_cache is None):
                    try:
                        verified = self._setup_decoder(conn, owner, table, geom, base)
                        if self._dec is not None:
                            self._dec_cache = self._dec.to_config()
                        elif verified:
                            self._dec_cache = "off"
                    except Exception as e:
                        self._dec, self._dec_info = None, "error: %s" % e
                C.log("%s: local decoder %s" % (self._label, self._dec_info))

            # ---- true curves (arcs) kept by ArcGIS in SE_ANNO_CAD_DATA / GDB_GEOMATTR_DATA
            cadcols = [c[0] for c in cols if c[0].upper() in st_curves.CURVE_COLUMNS and (c[1] or "").upper() == "BLOB"]
            if cadcols and curves_mode() != "off":
                self._setup_curves(conn, base, geom, cadcols[0], u)
            if not (self._curve_ok and curves_mode() == "curves"):
                self._wkb = QgsWkbTypes.linearType(self._wkb)

            # ---- load polygons as lines (boundaries) or points (centroid inside)
            self._geom_as = u.param("stgeomas") if u.hasParam("stgeomas") else ""
            if self._geom_as not in GEOM_AS:
                self._geom_as = ""
            if self._geom_as:
                cat = QgsWkbTypes.geometryType(self._wkb)
                hz, hm = QgsWkbTypes.hasZ(self._wkb), QgsWkbTypes.hasM(self._wkb)
                curved = QgsWkbTypes.isCurvedType(self._wkb)
                if cat == C.GEOM_POLYGON:
                    if self._geom_as == "boundary":
                        t = QgsWkbTypes.parseType("MultiCurve" if curved else "MultiLineString")
                    else:
                        t = QgsWkbTypes.parseType("Point")
                    if hz:
                        t = QgsWkbTypes.addZ(t)
                    if hm:
                        t = QgsWkbTypes.addM(t)
                    self._wkb = t
                elif not ((self._geom_as == "boundary" and cat == C.GEOM_LINE)
                          or (self._geom_as == "point" and cat == C.GEOM_POINT)):
                    C.log("%s: 'load as %s' applies to polygon tables only - ignored" % (self._label, self._geom_as),
                          "warning")
                    self._geom_as = ""
                if self._dec is not None:
                    DECODERS[self._label] = self._dec

            # ---- extent: exact data extent stored in the source (stext) > computed now (stcalcext=1) > SDE.LAYERS
            self._ext_exact = False
            stext = u.param("stext") if u.hasParam("stext") else ""
            if stext:
                try:
                    v = [float(x) for x in stext.split(",")]
                    self._extent = QgsRectangle(v[0], v[1], v[2], v[3])
                    self._ext_exact = True
                except Exception:
                    stext = ""
            if not stext:
                if u.hasParam("stcalcext") and u.param("stcalcext") == "1":
                    t0 = time.time()
                    self._extent = self._envelope_extent(conn, geom, base)
                    self._ext_exact = not self._extent.isNull()
                    C.log("%s: exact data extent computed in %.1fs" % (self._label, time.time() - t0))
                else:
                    self._extent = self._detect_extent(conn, owner, table, geom, base)
            try:
                r = conn.fetchone("SELECT num_rows FROM all_tables WHERE owner = :o AND table_name = :t",
                                  {"o": owner, "t": table})
                self._num_rows = int(r[0]) if r and r[0] is not None else None
            except Exception:
                self._num_rows = None
            if self._num_rows is None and u.hasParam("strows"):
                try:
                    self._num_rows = int(u.param("strows"))
                except Exception:
                    pass
            if self._num_rows is None and u.hasParam("stcalcext") and u.param("stcalcext") == "1":
                # no optimizer statistics -> count once (unique key index) and store it in the source
                try:
                    r = conn.fetchone("SELECT COUNT(*) FROM %s" % base)
                    self._num_rows = int(r[0])
                except Exception as e:
                    C.log("%s: row count failed: %s" % (self._label, e), "info")

            # ---- subset
            self._subset = (u.sql() or "").strip()
        finally:
            conn.close()

        # persist detected values in the layer source -> fast reload next time
        u.setDataSource(owner, table, geom, self._subset, self._key)
        u.setWkbType(self._wkb)
        if self._crs.authid():
            u.removeParam("stcrs")
            u.setParam("stcrs", self._crs.authid())
        u.removeParam("stcalcext")
        if self._num_rows is not None:
            u.removeParam("strows")
            u.setParam("strows", str(self._num_rows))
        if self._key_range is not None:
            u.removeParam("stkeys")
            u.setParam("stkeys", "%d,%d" % self._key_range)
        if self._curve_cache:
            u.removeParam("stcurve")
            u.setParam("stcurve", self._curve_cache)
        u.setWkbType(self._wkb)
        if getattr(self, "_dec_cache", None):
            u.removeParam("stdec")
            u.setParam("stdec", self._dec_cache)
        if self._ext_exact:
            e = self._extent
            u.removeParam("stext")
            u.setParam("stext", "%r,%r,%r,%r" % (e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()))
        self.setDataSourceUri(u.uri(False))

    def _setup_curves(self, conn, base, geom, cadcol, u):
        cached = u.param("stcurve") if u.hasParam("stcurve") else ""
        keep_z = QgsWkbTypes.hasZ(self._wkb) and not self._force2d
        if cached.startswith("ok"):
            self._curve_ok = True
            try:
                self._curve_hint = int(cached.split(";")[1])
            except Exception:
                self._curve_hint = None
            info = "ON (verified when added)"
        elif cached in ("none", "fail"):
            info = "off (%s when the layer was added; re-add to retry)" % ("no curves" if cached == "none" else "not recognised")
        else:
            g = "t." + qi(geom)
            c = "t." + qi(cadcol)
            try:
                rows = conn.fetchall("SELECT %s.minx, %s.miny, %s.maxx, %s.maxy, sde.st_asbinary(%s), %s FROM %s t "
                                     "WHERE %s IS NOT NULL AND ROWNUM <= 50" % (g, g, g, g, g, c, base, c))
                ok, hint, info = st_curves.verify(rows, keep_z)
            except Exception as e:
                ok, hint, info = False, None, "error: %s" % e
            self._curve_ok = bool(ok)
            self._curve_hint = hint
            self._curve_cache = ("ok;%s" % (hint if hint is not None else -1)) if ok else ("none" if ok is None else "fail")
        if self._curve_ok:
            self._cad = cadcol
            if curves_mode() == "curves":
                self._wkb = QgsWkbTypes.curveType(self._wkb)
        C.log("%s: true curves (%s) %s" % (self._label, cadcol, info))

    def _srid(self, conn, owner, table, geom):
        try:
            r = conn.fetchone("SELECT srid FROM sde.st_geometry_columns WHERE owner = :o AND table_name = :t "
                              "AND column_name = :c", {"o": owner, "t": table, "c": geom})
            if r and r[0] is not None:
                return int(r[0])
        except Exception:
            pass
        r = conn.fetchone("SELECT t.%s.srid FROM %s.%s t WHERE t.%s IS NOT NULL AND ROWNUM = 1"
                          % (qi(geom), qi(owner), qi(table), qi(geom)))
        return int(r[0]) if r and r[0] is not None else None

    def _setup_decoder(self, conn, owner, table, geom, base):
        srid = self._srid(conn, owner, table, geom)
        if srid is None:
            self._dec_info = "off (no srid)"
            return
        d = conn.fetch_dicts("SELECT * FROM sde.st_spatial_references WHERE srid = :s", {"s": srid}, 1)
        if not d:
            self._dec_info = "off (spatial reference %s not found)" % srid
            return
        d = d[0]

        def num(*names):
            for n in names:
                v = d.get(n)
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        pass
            return None

        sr = {"fx": num("FALSEX", "X_OFFSET"), "fy": num("FALSEY", "Y_OFFSET"), "ux": num("XYUNITS"),
              "fz": num("FALSEZ", "Z_OFFSET"), "uz": num("ZUNITS", "Z_SCALE")}
        if sr["fx"] is None or sr["fy"] is None or not sr["ux"]:
            self._dec_info = "off (spatial reference columns not found: %s)" % ",".join(sorted(d.keys()))
            return
        g = "t." + qi(geom)
        rows = conn.fetchall("SELECT %s.entity, %s.numpts, %s.points, sde.st_asbinary(%s) FROM %s t "
                             "WHERE %s IS NOT NULL AND ROWNUM <= 300" % (g, g, g, g, base, g))
        need_z = QgsWkbTypes.hasZ(self._wkb) and not self._force2d
        if not rows:
            self._dec_info = "off (no rows to verify)"
            return False
        self._dec, self._dec_info = st_decoder.verify(rows, sr, need_z)
        return True

    def _detect_crs(self, conn, owner, table, geom):
        srid = None
        try:
            r = conn.fetchone("SELECT srid FROM sde.st_geometry_columns WHERE owner = :o AND table_name = :t "
                              "AND column_name = :c", {"o": owner, "t": table, "c": geom})
            srid = r[0] if r else None
        except Exception as e:
            C.log("sde.st_geometry_columns lookup failed: %s" % e, "info")
        if srid is None:
            try:
                r = conn.fetchone("SELECT t.%s.srid FROM %s.%s t WHERE t.%s IS NOT NULL AND ROWNUM = 1"
                                  % (qi(geom), qi(owner), qi(table), qi(geom)))
                srid = r[0] if r else None
            except Exception as e:
                C.log("srid lookup failed: %s" % e, "info")
        if srid is None:
            return QgsCoordinateReferenceSystem()
        try:
            d = conn.fetch_dicts("SELECT * FROM sde.st_spatial_references WHERE srid = :s", {"s": int(srid)}, 1)
        except Exception as e:
            C.log("sde.st_spatial_references lookup failed: %s" % e, "info")
            return QgsCoordinateReferenceSystem()
        if not d:
            return QgsCoordinateReferenceSystem()
        d = d[0]
        org = str(d.get("ORGANIZATION") or "").upper()
        orgid = _int_or_none(d.get("ORG_COORDSYS_ID"))
        if org == "EPSG" and orgid:
            crs = QgsCoordinateReferenceSystem("EPSG:%d" % orgid)
            if crs.isValid():
                return crs
        definition = str(d.get("DEFINITION") or "")
        if definition:
            up = definition.upper()
            if up.startswith("GEOGCS") and "WGS_1984" in up and "PROJCS" not in up:
                return QgsCoordinateReferenceSystem("EPSG:4326")
            crs = QgsCoordinateReferenceSystem.fromWkt(definition)
            if crs.isValid():
                return crs
        return QgsCoordinateReferenceSystem()

    def _detect_type(self, conn, base, geom, override):
        cats, hasz, hasm = set(), False, False
        try:
            rows = conn.fetchall("SELECT sde.st_asbinary(t.%s) FROM %s t WHERE t.%s IS NOT NULL AND ROWNUM <= 50"
                                 % (qi(geom), base, qi(geom)))
        except Exception as e:
            C.log("%s: geometry type detection failed: %s" % (self._label, e), "warning")
            rows = []
        for (b,) in rows:
            g = wkb_to_geometry(b)
            if g is None or g.isNull():
                continue
            t = g.wkbType()
            cats.add(QgsWkbTypes.geometryType(t))
            hasz = hasz or QgsWkbTypes.hasZ(t)
            hasm = hasm or QgsWkbTypes.hasM(t)
        override = (override or "").lower()
        if override in ("point", "line", "polygon"):
            name = {"point": "MultiPoint", "line": "MultiLineString", "polygon": "MultiPolygon"}[override]
        elif C.GEOM_POLYGON in cats or not cats:
            name = "MultiPolygon"
        elif C.GEOM_LINE in cats:
            name = "MultiLineString"
        else:
            name = "MultiPoint"
        wkb = QgsWkbTypes.parseType(name)
        if hasz:
            wkb = QgsWkbTypes.addZ(wkb)
        if hasm:
            wkb = QgsWkbTypes.addM(wkb)
        return wkb

    def _envelope_extent(self, conn, geom, base):
        try:
            g = "t." + qi(geom)
            r = conn.fetchone("SELECT MIN(%s.minx), MIN(%s.miny), MAX(%s.maxx), MAX(%s.maxy) FROM %s t"
                              % (g, g, g, g, base))
            if r and all(v is not None for v in r):
                return QgsRectangle(float(r[0]), float(r[1]), float(r[2]), float(r[3]))
        except Exception as e:
            C.log("%s: extent computation failed: %s" % (self._label, e), "warning")
        return QgsRectangle()

    def _detect_extent(self, conn, owner, table, geom, base):
        try:
            r = conn.fetchone("SELECT minx, miny, maxx, maxy FROM sde.layers WHERE owner = :o AND table_name = :t "
                              "AND spatial_column = :c", {"o": owner, "t": table, "c": geom})
            if r and all(v is not None for v in r):
                return QgsRectangle(float(r[0]), float(r[1]), float(r[2]), float(r[3]))
        except Exception as e:
            C.log("sde.layers lookup failed: %s" % e, "info")
        try:
            g = "t." + qi(geom)
            r = conn.fetchone("SELECT MIN(%s.minx), MIN(%s.miny), MAX(%s.maxx), MAX(%s.maxy) FROM %s t"
                              % (g, g, g, g, base))
            if r and all(v is not None for v in r):
                return QgsRectangle(float(r[0]), float(r[1]), float(r[2]), float(r[3]))
        except Exception as e:
            C.log("extent computation failed: %s" % e, "warning")
        return QgsRectangle()

    # ----------------------------------------------------------- provider API
    def name(self):
        return PROVIDER_KEY

    def description(self):
        return PROVIDER_DESC

    def storageType(self):  # noqa: N802
        return "Oracle ST_Geometry"

    def isValid(self):  # noqa: N802
        return self._valid

    def crs(self):
        return QgsCoordinateReferenceSystem(self._crs)

    def extent(self):
        return QgsRectangle(self._extent)

    def updateExtents(self):  # noqa: N802
        pass

    def wkbType(self):  # noqa: N802
        return self._wkb

    def fields(self):
        return QgsFields(self._fields)

    def pkAttributeIndexes(self):  # noqa: N802
        return [self._key_index] if self._key_index >= 0 else []

    def capabilities(self):
        if C.CAP_SELECT_AT_ID is None:
            return super().capabilities()
        return C.CAP_SELECT_AT_ID

    def featureSource(self):  # noqa: N802
        return StFeatureSource(self)

    def getFeatures(self, request=QgsFeatureRequest()):  # noqa: N802
        if not self._valid:
            return QgsFeatureIterator()
        return QgsFeatureIterator(StFeatureIterator(StFeatureSource(self), request))

    def _where(self):
        return " WHERE (%s)" % self._subset if self._subset else ""

    def featureCount(self):  # noqa: N802
        if not self._valid:
            return -1
        if self._count is None:
            conn = None
            try:
                conn = self._backend.acquire()
                r = conn.fetchone("SELECT COUNT(*) FROM %s t%s" % (self._relation, self._where()))
                self._count = int(r[0])
            except Exception as e:
                C.log("%s: feature count failed: %s" % (self._label, e), "warning")
                return -1
            finally:
                if conn is not None:
                    conn.close()
        return self._count

    def reloadProviderData(self):  # noqa: N802
        self._count = None

    def subsetString(self):  # noqa: N802
        return self._subset

    def supportsSubsetString(self):  # noqa: N802
        return True

    def setSubsetString(self, subset, updateFeatureCount=True):  # noqa: N802,N803
        subset = (subset or "").strip()
        if subset == self._subset:
            return True
        if subset:
            conn = None
            try:
                conn = self._backend.acquire()
                conn.fetchone("SELECT 1 FROM %s t WHERE (%s) AND ROWNUM = 1" % (self._relation, subset))
            except Exception as e:
                C.log("%s: invalid filter '%s': %s" % (self._label, subset, e), "warning")
                return False
            finally:
                if conn is not None:
                    conn.close()
        self._subset = subset
        self._count = None
        u = QgsDataSourceUri(self.dataSourceUri())
        u.setSql(subset)
        self.setDataSourceUri(u.uri(False))
        return True

    def _column_stat(self, fieldIndex, sql_fn):  # noqa: N803
        if not self._valid or fieldIndex < 0 or fieldIndex >= len(self._cols):
            return None
        conn = None
        try:
            conn = self._backend.acquire()
            r = conn.fetchone("SELECT %s(%s) FROM %s t%s" % (sql_fn, self._cols[fieldIndex].sql, self._relation, self._where()))
            return to_qgis_value(r[0]) if r else None
        except Exception as e:
            C.log("%s: %s failed: %s" % (self._label, sql_fn, e), "warning")
            return None
        finally:
            if conn is not None:
                conn.close()

    def minimumValue(self, fieldIndex):  # noqa: N802,N803
        return self._column_stat(fieldIndex, "MIN")

    def maximumValue(self, fieldIndex):  # noqa: N802,N803
        return self._column_stat(fieldIndex, "MAX")

    def uniqueValues(self, fieldIndex, limit=-1):  # noqa: N802,N803
        out = set()
        if not self._valid or fieldIndex < 0 or fieldIndex >= len(self._cols):
            return out
        col = self._cols[fieldIndex].sql
        sql = "SELECT DISTINCT %s FROM %s t%s" % (col, self._relation, self._where())
        if limit is not None and limit >= 0:
            sql += " FETCH FIRST %d ROWS ONLY" % int(limit)
        conn = None
        try:
            conn = self._backend.acquire()
            for (v,) in conn.fetchall(sql):
                try:
                    out.add(to_qgis_value(v))
                except TypeError:
                    out.add(str(v))
        except Exception as e:
            C.log("%s: unique values failed: %s" % (self._label, e), "warning")
        finally:
            if conn is not None:
                conn.close()
        return out

    def handlePostCloneOperations(self, source):  # noqa: N802
        pass


# =====================================================================
def _create_provider(uri, *args):
    options = args[0] if len(args) > 0 else None
    flags = args[1] if len(args) > 1 else None
    return StGeometryProvider(uri, options, flags)


def register_provider():
    global _metadata
    reg = QgsProviderRegistry.instance()
    if reg.providerMetadata(PROVIDER_KEY) is not None:
        return True
    _metadata = QgsProviderMetadata(PROVIDER_KEY, PROVIDER_DESC, _create_provider)
    ok = reg.registerProvider(_metadata)
    C.log("Provider '%s' registered: %s" % (PROVIDER_KEY, ok))
    return ok
