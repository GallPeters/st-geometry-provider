# -*- coding: utf-8 -*-
"""'Add ST_Geometry layer' dialog + diagnostics."""

import time
import traceback

from qgis.core import (QgsDataSourceUri, QgsVectorLayer, QgsProject, QgsFeatureRequest, QgsRectangle, QgsFeature)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QPushButton, QLineEdit,
                                 QTreeWidget, QTreeWidgetItem, QCheckBox, QLabel, QMessageBox, QAbstractItemView,
                                 QApplication, QPlainTextEdit, QSpinBox, QDoubleSpinBox)

from . import compat as C
from . import backend as B
from .provider import PROVIDER_KEY, LAST_ERRORS

LIST_SQL = """
SELECT c.owner, c.table_name, c.column_name, t.num_rows
FROM all_tab_columns c
JOIN all_tables t ON t.owner = c.owner AND t.table_name = c.table_name
WHERE c.data_type = 'ST_GEOMETRY'
  AND c.owner NOT IN ('SDE', 'SYS', 'SYSTEM', 'MDSYS')
  AND NOT REGEXP_LIKE(c.table_name, '^[AD][0-9]+$')
ORDER BY c.owner, c.table_name
"""

_BACKENDS = [("auto", "Auto (python-oracledb, fallback Qt OCI)"),
             ("oracledb", "python-oracledb only"),
             ("qsql", "Qt OCI driver only (slower)")]


class _Busy(object):
    def __enter__(self):
        QApplication.setOverrideCursor(C.qenum(Qt, "CursorShape", "WaitCursor"))

    def __exit__(self, *a):
        QApplication.restoreOverrideCursor()


def _exec(dlg):
    f = getattr(dlg, "exec", None) or getattr(dlg, "exec_")
    return f()


def show_text(parent, title, text):
    d = QDialog(parent)
    d.setWindowTitle(title)
    d.resize(760, 420)
    lay = QVBoxLayout(d)
    ed = QPlainTextEdit(d)
    ed.setReadOnly(True)
    ed.setPlainText(text)
    lay.addWidget(ed)
    b = QPushButton("Close", d)
    b.clicked.connect(d.accept)
    lay.addWidget(b)
    _exec(d)


class AddLayerDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("ST Geometry Provider - add Esri ST_Geometry layer(s)")
        self.resize(820, 560)
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("QGIS Oracle connection:"))
        self.conn_combo = QComboBox()
        row.addWidget(self.conn_combo, 1)
        self.btn_list = QPushButton("List tables")
        row.addWidget(self.btn_list)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Search:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("filter by owner / table name")
        row2.addWidget(self.search, 1)
        lay.addLayout(row2)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Owner", "Table", "Geometry column", "Rows (stats)"])
        self.tree.setSelectionMode(C.qenum(QAbstractItemView, "SelectionMode", "ExtendedSelection"))
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        lay.addWidget(self.tree, 1)

        grid = QGridLayout()
        self.chk_2d = QCheckBox("Force 2D (drop Z/M)")
        self.chk_evw = QCheckBox("Read through versioned view (<TABLE>_EVW) when it exists")
        self.gtype = QComboBox()
        for v, t in (("auto", "Auto-detect"), ("polygon", "Polygon"), ("line", "Line"), ("point", "Point")):
            self.gtype.addItem(t, v)
        self.backend_combo = QComboBox()
        for v, t in _BACKENDS:
            self.backend_combo.addItem(t, v)
        cur = B.backend_pref()
        self.backend_combo.setCurrentIndex([v for v, _ in _BACKENDS].index(cur))
        grid.addWidget(self.chk_2d, 0, 0)
        grid.addWidget(self.chk_evw, 0, 1, 1, 3)
        grid.addWidget(QLabel("Geometry type:"), 1, 0)
        grid.addWidget(self.gtype, 1, 1)
        grid.addWidget(QLabel("Driver:"), 1, 2)
        grid.addWidget(self.backend_combo, 1, 3)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 16)
        self.parallel.setValue(B.parallel_degree())
        self.parallel.setToolTip("Oracle sessions used concurrently for one big map read (python-oracledb only). 1 = off.")
        grid.addWidget(QLabel("Parallel sessions per layer:"), 2, 0)
        grid.addWidget(self.parallel, 2, 1)
        self.chk_timing = QCheckBox("Timing log (one line per map read in Log Messages > ST_Geometry)")
        self.chk_timing.setChecked(B.timing_enabled())
        grid.addWidget(self.chk_timing, 2, 2, 1, 2)
        self.chk_ext = QCheckBox("Compute exact data extent when adding (one-time scan; enables the fast plan for zoomed-out views)")
        self.chk_ext.setChecked(True)
        grid.addWidget(self.chk_ext, 3, 0, 1, 4)
        self.idx_max = QSpinBox()
        self.idx_max.setRange(1000, 10000000)
        self.idx_max.setSingleStep(10000)
        self.idx_max.setValue(B.index_max_rows())
        self.idx_max.setToolTip("Estimated rows in view above which a plain table scan is used instead of the "
                                "ST spatial index. Lower = scan sooner. Applies to all layers from the next redraw.")
        grid.addWidget(QLabel("Use plain scan above (est. rows in view):"), 4, 0)
        grid.addWidget(self.idx_max, 4, 1)
        self.dec_combo = QComboBox()
        self.dec_combo.addItem("Local decoder where verified (fast, less DB load)", "auto")
        self.dec_combo.addItem("Esri st_asbinary on the server", "server")
        self.dec_combo.setCurrentIndex(0 if B.decoder_pref() == "auto" else 1)
        self.dec_combo.setToolTip("Decoder choice is checked when a layer is added/opened; re-add layers after changing it.")
        grid.addWidget(QLabel("Geometry decoding:"), 4, 2)
        grid.addWidget(self.dec_combo, 4, 3)
        self.chk_ov = QCheckBox("Fast overview: draw features smaller than")
        self.chk_ov.setChecked(B.overview_enabled())
        self.chk_ov.setToolTip("Map drawing only. Features whose envelope is smaller than the given number of "
                               "screen pixels are drawn as pixel squares, merged in Oracle (one per pixel cell and "
                               "style value) - no geometry is fetched for them. Identify, selection, attribute table, "
                               "export and processing always get exact geometries. Applies from the next redraw.")
        self.ov_px = QDoubleSpinBox()
        self.ov_px.setRange(0.5, 10.0)
        self.ov_px.setSingleStep(0.5)
        self.ov_px.setDecimals(1)
        self.ov_px.setSuffix(" px")
        self.ov_px.setValue(B.overview_pixels())
        self.ov_px.setEnabled(self.chk_ov.isChecked())
        grid.addWidget(self.chk_ov, 5, 0)
        grid.addWidget(self.ov_px, 5, 1)
        grid.addWidget(QLabel("as pixel cells (map drawing only; identify / selection / export stay exact)"), 5, 2, 1, 2)
        self.curve_combo = QComboBox()
        self.curve_combo.addItem("Off - ArcGIS densified shape", "off")
        self.curve_combo.addItem("True curves - QGIS segments them (Options > Rendering > Curve segmentation)", "curves")
        self.curve_combo.addItem("True curves segmented by the plugin with:", "segmentize")
        self.curve_combo.setCurrentIndex(["off", "curves", "segmentize"].index(B.curves_mode()))
        self.curve_combo.setToolTip("Arcs are read from SE_ANNO_CAD_DATA / GDB_GEOMATTR_DATA and verified against the "
                                    "ST shape when a layer is added. Re-add layers after changing this.")
        ctype, cval = B.curve_tolerance()
        self.curve_type = QComboBox()
        self.curve_type.addItem("max angle (degrees)", "angle")
        self.curve_type.addItem("max distance from arc (m)", "distance")
        self.curve_type.setCurrentIndex(0 if ctype == "angle" else 1)
        self.curve_val = QDoubleSpinBox()
        self.curve_val.setDecimals(3)
        self.curve_val.setRange(0.001, 90.0)
        self.curve_val.setValue(cval)
        grid.addWidget(QLabel("Arcs (true curves):"), 6, 0)
        grid.addWidget(self.curve_combo, 6, 1)
        grid.addWidget(self.curve_type, 6, 2)
        grid.addWidget(self.curve_val, 6, 3)

        def _curve_ui(*_):
            seg = self.curve_combo.currentData() == "segmentize"
            self.curve_type.setEnabled(seg)
            self.curve_val.setEnabled(seg)
        _curve_ui()
        self.curve_combo.currentIndexChanged.connect(lambda *_: (B.set_curves_mode(self.curve_combo.currentData()), _curve_ui()))
        self.curve_type.currentIndexChanged.connect(
            lambda *_: B.set_curve_tolerance(self.curve_type.currentData(), self.curve_val.value()))
        self.curve_val.valueChanged.connect(
            lambda v: B.set_curve_tolerance(self.curve_type.currentData(), v))
        lay.addLayout(grid)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        self.btn_diag = QPushButton("Diagnostics (first selected)")
        self.btn_bench = QPushButton("Benchmark current map view (first selected)")
        self.btn_add = QPushButton("Add selected layer(s)")
        self.btn_close = QPushButton("Close")
        btns.addWidget(self.btn_diag)
        btns.addWidget(self.btn_bench)
        btns.addStretch(1)
        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_close)
        lay.addLayout(btns)

        self.btn_list.clicked.connect(self.list_tables)
        self.btn_add.clicked.connect(self.add_layers)
        self.btn_diag.clicked.connect(self.diagnostics)
        self.btn_bench.clicked.connect(self.benchmark)
        self.btn_close.clicked.connect(self.reject)
        self.search.textChanged.connect(self.apply_filter)
        self.tree.itemDoubleClicked.connect(lambda *a: self.add_layers())
        self.backend_combo.currentIndexChanged.connect(self._backend_changed)
        self.parallel.valueChanged.connect(lambda v: B.set_parallel_degree(v))
        self.chk_timing.toggled.connect(lambda on: B.set_timing(on))
        self.idx_max.valueChanged.connect(lambda v: B.set_index_max_rows(v))
        self.dec_combo.currentIndexChanged.connect(lambda *_: B.set_decoder_pref(self.dec_combo.currentData()))
        self.chk_ov.toggled.connect(lambda on: B.set_overview(on))
        self.chk_ov.toggled.connect(self.ov_px.setEnabled)
        self.ov_px.valueChanged.connect(lambda v: B.set_overview_pixels(v))

        for n in B.config_connection_names():
            self.conn_combo.addItem("%s   [connections.json]" % n, ("cfg", n))
        for n in B.oracle_connection_names():
            self.conn_combo.addItem(n, ("qgis", n))
        if self.conn_combo.count() == 0:
            self.status.setText("No connections: fill connections.json in the plugin folder, "
                                "or create one in Browser > Oracle > New Connection.")

    # ------------------------------------------------------------------
    def _backend_changed(self, *_):
        B.set_backend_pref(self.backend_combo.currentData())

    def _conn(self):
        d = self.conn_combo.currentData()
        if isinstance(d, (tuple, list)) and len(d) == 2:
            return d[0], d[1]
        return "qgis", self.conn_combo.currentText()

    def _conn_uri(self):
        kind, name = self._conn()
        u = QgsDataSourceUri()
        u.setParam("stcfg" if kind == "cfg" else "stconn", name)
        return u

    def list_tables(self):
        if not self.conn_combo.currentText():
            return
        self.tree.clear()
        try:
            with _Busy():
                t0 = time.time()
                be = B.get_backend(self._conn_uri())
                conn = be.acquire()
                try:
                    rows = conn.fetchall(LIST_SQL)
                finally:
                    conn.close()
                kind, name = self._conn()
                if kind == "cfg":
                    schemas = B.load_config().get(name, {}).get("schemas") or []
                    if schemas:
                        allowed = set(str(s).upper() for s in schemas)
                        rows = [r for r in rows if str(r[0]).upper() in allowed]
            self.tree.setSortingEnabled(False)
            for owner, table, col, n in rows:
                it = QTreeWidgetItem([str(owner), str(table), str(col), "" if n is None else str(n)])
                self.tree.addTopLevelItem(it)
            self.tree.setSortingEnabled(True)
            for i in range(4):
                self.tree.resizeColumnToContents(i)
            self.status.setText("%d ST_GEOMETRY tables found (%.1fs, driver: %s)." % (len(rows), time.time() - t0, be.kind))
            self.apply_filter()
        except Exception as e:
            C.log("List tables failed: %s\n%s" % (e, traceback.format_exc()), "critical")
            QMessageBox.warning(self, "ST Geometry Provider", "Cannot list tables:\n%s" % e)

    def apply_filter(self, *_):
        txt = self.search.text().strip().upper()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            hay = (it.text(0) + "." + it.text(1)).upper()
            it.setHidden(bool(txt) and txt not in hay)

    def _selected(self):
        return [it for it in self.tree.selectedItems() if not it.isHidden()]

    def _layer_uri(self, it):
        u = self._conn_uri()
        u.setDataSource(it.text(0), it.text(1), it.text(2), "", "")
        if self.chk_2d.isChecked():
            u.setParam("stforce2d", "1")
        if self.chk_evw.isChecked():
            u.setParam("stevw", "1")
        gt = self.gtype.currentData()
        if gt and gt != "auto":
            u.setParam("stgeomtype", gt)
        if self.chk_ext.isChecked():
            u.setParam("stcalcext", "1")
        return u.uri(False)

    def _make_layer(self, it):
        name = it.text(1)
        layer = QgsVectorLayer(self._layer_uri(it), name, PROVIDER_KEY)
        if layer.isValid():
            # re-create the layer from the provider's completed source (key, type, CRS, exact extent)
            # so the project saves it and reopening needs no detection at all
            try:
                full = layer.dataProvider().dataSourceUri()
                if full and full != layer.source():
                    l2 = QgsVectorLayer(full, name, PROVIDER_KEY)
                    if l2.isValid():
                        layer = l2
            except Exception as e:
                C.log("Could not store detected layer settings: %s" % e, "warning")
        return layer

    def add_layers(self):
        items = self._selected()
        if not items:
            self.status.setText("Select one or more tables first.")
            return
        ok, failed = 0, []
        with _Busy():
            for it in items:
                try:
                    layer = self._make_layer(it)
                    if layer.isValid():
                        QgsProject.instance().addMapLayer(layer)
                        ok += 1
                    else:
                        key = "%s.%s" % (it.text(0), it.text(1))
                        failed.append("%s: %s" % (key, LAST_ERRORS.get(key, "invalid layer (see Log Messages > ST_Geometry)")))
                except Exception as e:
                    failed.append("%s.%s: %s" % (it.text(0), it.text(1), e))
                QApplication.processEvents()
        self.status.setText("%d layer(s) added. %s" % (ok, ("%d failed." % len(failed)) if failed else ""))
        if failed:
            show_text(self, "ST Geometry Provider - failed layers", "\n".join(failed))

    # ------------------------------------------------------------------
    def benchmark(self):
        """Split the cost of reading the current map view into: Oracle index, Oracle+network,
        Esri st_asbinary, Python WKB decode, provider feature building, and QGIS rendering."""
        items = self._selected()
        if not items:
            self.status.setText("Select a table first (the one you are looking at on the map).")
            return
        it = items[0]
        out = []
        CAP = 300000

        def p(*a):
            out.append(" | ".join(str(x) for x in a))

        from time import perf_counter as pc
        from qgis.core import (QgsCoordinateTransform, QgsGeometry, QgsMapSettings, QgsMapRendererSequentialJob)
        from qgis.PyQt.QtCore import QByteArray
        old_par = B.parallel_degree()
        old_timing = B.timing_enabled()
        conn = None
        view_rows = 0
        try:
            with _Busy():
                layer = self._make_layer(it)
                if not layer.isValid():
                    p("layer invalid", LAST_ERRORS.get("%s.%s" % (it.text(0), it.text(1)), "see log"))
                    raise RuntimeError("layer invalid")
                u = QgsDataSourceUri(layer.dataProvider().dataSourceUri())
                owner, table, geom, key = u.schema(), u.table(), u.geometryColumn(), u.keyColumn()
                if not key:
                    raise RuntimeError("key column not detected")
                rel = '"%s"."%s"' % (owner, table)
                g, k = 't."%s"' % geom, 't."%s"' % key
                canvas = self.iface.mapCanvas()
                ms0 = canvas.mapSettings()
                ct = QgsCoordinateTransform(ms0.destinationCrs(), layer.crs(), QgsProject.instance())
                view = ct.transformBoundingBox(canvas.extent())
                lext = layer.extent()
                p("B0 view/layer area %.4f" % ((view.width() * view.height()) / max(1e-30, lext.width() * lext.height())),
                  "layer extent ok", (not lext.isNull()) and lext.width() > 0 and lext.height() > 0,
                  "view contains layer extent", view.contains(lext))
                be = B.get_backend(self._conn_uri())
                p("driver", be.kind)
                conn = be.acquire()
                prm = {"x1": view.xMinimum(), "y1": view.yMinimum(), "x2": view.xMaximum(), "y2": view.yMaximum()}
                W = " WHERE sde.st_envintersects(%s, :x1, :y1, :x2, :y2) = 1" % g

                WS = (" WHERE %s.minx <= :x2 AND %s.maxx >= :x1 AND %s.miny <= :y2 AND %s.maxy >= :y1"
                      % (g, g, g, g))
                for label, w in (("B1 index count (cap 200k)", W), ("B1b plain-scan count (cap 200k)", WS)):
                    try:
                        t = pc()
                        n = conn.fetchone("SELECT COUNT(*) FROM (SELECT 1 FROM %s t%s AND ROWNUM <= 200000)"
                                          % (rel, w), prm)[0]
                        p(label, n, "%.2fs" % (pc() - t))
                        view_rows = n
                    except Exception as e:
                        p(label, "ERR", str(e)[:120])
                if "ROWNUM" not in W:
                    W = W + " AND ROWNUM <= %d" % CAP

                def read(label, sql, keep=False):
                    t = pc()
                    rows = conn.query(sql, prm)
                    cnt = nbytes = 0
                    kept = []
                    try:
                        for r in rows:
                            cnt += 1
                            b = r[-1]
                            if b is not None and not isinstance(b, (int, float)):
                                nbytes += len(b)
                            if keep:
                                kept.append(b)
                            if cnt >= CAP:
                                break
                    finally:
                        rows.close()
                    p(label, "rows", cnt, "MB %.1f" % (nbytes / 1048576.0), "%.2fs" % (pc() - t))
                    return kept

                def safe(label, sql, keep=False):
                    try:
                        return read(label, sql, keep)
                    except Exception as e:
                        p(label, "ERR", str(e)[:120])
                        return []

                safe("B2 keys only", "SELECT %s FROM %s t%s" % (k, rel, W))
                safe("B3 raw Esri blob", "SELECT %s, %s.points FROM %s t%s" % (k, g, rel, W))
                try:
                    from .provider import DECODERS
                    dec = DECODERS.get("%s.%s" % (owner, table))
                    if dec is None:
                        p("B3b local decoder", "not active for this layer (see Log Messages)")
                    else:
                        t = pc()
                        raw = conn.fetchall("SELECT %s.entity, %s.numpts, %s.area, %s.points FROM %s t%s"
                                            % (g, g, g, g, rel, W), prm)
                        t_fetch = pc() - t
                        t = pc()
                        okn = fb = 0
                        for ent, npts, area, pts in raw:
                            gg = dec.geometry(ent, npts, area, pts) if (ent is not None and int(ent) in dec.entities) else None
                            if gg is None:
                                fb += 1
                            else:
                                okn += 1
                        p("B3b local decoder", "rows", len(raw), "fetch %.2fs" % t_fetch, "decode %.2fs" % (pc() - t),
                          "decoded", okn, "server-needed", fb)
                        del raw
                except Exception as e:
                    p("B3b local decoder", "ERR", str(e)[:120])
                wkbs = safe("B4 st_asbinary", "SELECT %s, sde.st_asbinary(%s) FROM %s t%s" % (k, g, rel, W), keep=True)
                conn.close()
                conn = None

                t = pc()
                ok = 0
                for b in wkbs:
                    if b:
                        gg = QgsGeometry()
                        gg.fromWkb(QByteArray(b if isinstance(b, bytes) else bytes(b)))
                        ok += 1
                p("B5 python WKB decode", ok, "%.2fs" % (pc() - t))
                del wkbs

                B.set_timing(False)
                for par in (1, 4):
                    B.set_parallel_degree(par)
                    t = pc()
                    cnt = 0
                    f = QgsFeature()
                    itr = layer.getFeatures(QgsFeatureRequest().setFilterRect(view))
                    while itr.nextFeature(f):
                        cnt += 1
                        if cnt >= CAP:
                            break
                    itr.close()
                    p("B6 provider features parallel=%d" % par, cnt, "%.2fs" % (pc() - t))

                for par in (1, 4):
                    if view_rows >= 200000:
                        p("B7 full render skipped (>200k features in view; zoom in and rerun)")
                        break
                    B.set_parallel_degree(par)
                    ms = QgsMapSettings()
                    ms.setLayers([layer])
                    ms.setDestinationCrs(ms0.destinationCrs())
                    ms.setExtent(canvas.extent())
                    ms.setOutputSize(canvas.size())
                    job = QgsMapRendererSequentialJob(ms)
                    t = pc()
                    job.start()
                    job.waitForFinished()
                    p("B7 full render parallel=%d" % par, "%.2fs" % (pc() - t))
        except Exception as e:
            p("ERROR", e)
            C.log(traceback.format_exc(), "critical")
        finally:
            if conn is not None:
                conn.close()
            B.set_parallel_degree(old_par)
            B.set_timing(old_timing)
        text = "\n".join(out)
        C.log("Benchmark %s.%s\n%s" % (it.text(0), it.text(1), text))
        show_text(self, "ST Geometry Provider - benchmark", text)

    def diagnostics(self):
        items = self._selected()
        if not items:
            self.status.setText("Select a table first.")
            return
        it = items[0]
        out = []

        def p(*a):
            out.append(" | ".join(str(x) for x in a))

        try:
            with _Busy():
                t0 = time.time()
                be = B.get_backend(self._conn_uri())
                p("driver", be.kind, "%.2fs" % (time.time() - t0))
                try:
                    import oracledb
                    p("oracledb", oracledb.__version__, "thin" if oracledb.is_thin_mode() else "thick")
                except Exception:
                    p("oracledb", "not available")
                t0 = time.time()
                layer = self._make_layer(it)
                p("layer valid", layer.isValid(), "%.2fs" % (time.time() - t0))
                if not layer.isValid():
                    p("error", LAST_ERRORS.get("%s.%s" % (it.text(0), it.text(1)), "see log"))
                else:
                    pr = layer.dataProvider()
                    from qgis.core import QgsWkbTypes
                    p("type", QgsWkbTypes.displayString(layer.wkbType()), "crs", layer.crs().authid() or "custom",
                      "fields", layer.fields().count())
                    ext = layer.extent()
                    p("extent valid", not ext.isNull())
                    t0 = time.time()
                    p("count", pr.featureCount(), "%.2fs" % (time.time() - t0))

                    def run(label, req, maxn=None):
                        t0 = time.time()
                        n = ng = 0
                        f = QgsFeature()
                        itr = layer.getFeatures(req)
                        while itr.nextFeature(f):
                            n += 1
                            if f.hasGeometry():
                                ng += 1
                            if maxn and n >= maxn:
                                break
                        itr.close()
                        p(label, "rows", n, "with geom", ng, "%.2fs" % (time.time() - t0))

                    nogeom = C.FLAG_NO_GEOMETRY
                    r1 = QgsFeatureRequest()
                    if nogeom is not None:
                        r1.setFlags(nogeom)
                    r1.setLimit(2000)
                    run("attrs 2000", r1)
                    r2 = QgsFeatureRequest()
                    r2.setLimit(2000)
                    run("geom 2000", r2)
                    r2b = QgsFeatureRequest()
                    run("geom ALL (parallel=%d)" % B.parallel_degree(), r2b, 200000)
                    if not ext.isNull():
                        cx, cy = ext.center().x(), ext.center().y()
                        hw, hh = ext.width() / 20.0, ext.height() / 20.0
                        r3 = QgsFeatureRequest()
                        r3.setFilterRect(QgsRectangle(cx - hw, cy - hh, cx + hw, cy + hh))
                        run("bbox 10%", r3, 100000)
                    if layer.fields().count():
                        fname = layer.fields().at(0).name()
                        r4 = QgsFeatureRequest()
                        r4.setFilterExpression('"%s" IS NOT NULL' % fname)
                        r4.setLimit(500)
                        run("expr 500", r4)
        except Exception as e:
            p("ERROR", e)
            C.log(traceback.format_exc(), "critical")
        text = "\n".join(out)
        C.log("Diagnostics %s.%s\n%s" % (it.text(0), it.text(1), text))
        show_text(self, "ST Geometry Provider - diagnostics", text)
