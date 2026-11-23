# -*- coding: utf-8 -*-
"""
Make a "display-only" copy of the GeoPackage(s) used by the current QGIS project.

The copy is identical to the original (all tables, saved styles in layer_styles, metadata,
spatial indexes, rasters, ...) except that every vector table used by a project layer keeps
ONLY the attribute fields needed to draw it:

  * renderer             renderer.usedAttributes(ctx) - all renderer types incl. nested rules,
                         data-defined symbol properties, geometry generators; plus "order by"
  * labeling             every QgsPalLayerSettings (simple, rule-based incl. nested rules,
                         other sub-providers) .referencedFields(ctx) + rule filter expressions
  * diagrams             diagram renderer + diagram data-defined properties
  * other display bits   layer filter (subset string), temporal fields (if active),
                         optionally display expression / map tip (see options)
  * virtual fields       expanded to the physical fields their expressions use
  * every style          the layer's current style, all other styles in its style manager,
                         AND every style stored for that table in the GeoPackage layer_styles table

Anything that may reference "all attributes" (e.g. attribute(@feature, ...)) keeps all fields.
The original GeoPackage is never modified.

Run: QGIS > Plugins > Python Console > Show Editor > open this file > Run
"""

# ----------------------------- options ---------------------------------------
OUTPUT_SUFFIX = "_display"             # <name>_display.gpkg next to the original
OUTPUT_DIR = ""                        # "" = same folder as the original gpkg
DRY_RUN = False                        # True = only print what would be removed
INCLUDE_DISABLED_LABELS = False        # keep fields of labeling that is configured but switched off
INCLUDE_DISPLAY_EXPRESSION = False     # keep fields of display expression and map tip
INCLUDE_GPKG_STORED_STYLES = True      # also analyse every style stored in layer_styles
KEEP_ALWAYS = []                       # field names to keep in every table, e.g. ["OBJECTID"]
# -----------------------------------------------------------------------------

import os
import shutil
import sqlite3
import tempfile
import traceback

from osgeo import ogr
from qgis.core import (Qgis, QgsProject, QgsVectorLayer, QgsProviderRegistry, QgsRenderContext,
                       QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest,
                       QgsRuleBasedLabeling)

try:
    from qgis.utils import iface
except Exception:
    iface = None

ALL = getattr(QgsFeatureRequest, "ALL_ATTRIBUTES", "#!allattributes!#")
ORIGIN_EXPRESSION = getattr(getattr(Qgis, "FieldOrigin", None), "Expression", None)
if ORIGIN_EXPRESSION is None:
    from qgis.core import QgsFields
    ORIGIN_EXPRESSION = QgsFields.OriginExpression


# ----------------------------- helpers ---------------------------------------
def expr_cols(text):
    if not text:
        return set()
    e = QgsExpression(text)
    if e.hasParserError():
        return set()
    return set(e.referencedColumns())


def make_context(layer):
    ctx = QgsRenderContext()
    try:
        if iface is not None:
            ctx = QgsRenderContext.fromMapSettings(iface.mapCanvas().mapSettings())
    except Exception:
        pass
    ec = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    ctx.setExpressionContext(ec)
    return ctx


def pal_fields(s, ctx):
    out = set()
    if s is None:
        return out
    try:
        out |= set(s.referencedFields(ctx))
    except Exception:
        # older API: field / expression + data-defined properties
        if s.isExpression:
            out |= expr_cols(s.fieldName)
        elif s.fieldName:
            out.add(s.fieldName)
        try:
            out |= set(s.dataDefinedProperties().referencedFields(ctx.expressionContext()))
        except Exception:
            pass
    return out


def rule_filters(rule):
    out = set()
    if rule is None:
        return out
    try:
        out |= expr_cols(rule.filterExpression())
    except Exception:
        pass
    for ch in rule.children():
        out |= rule_filters(ch)
    return out


def labeling_fields(layer, ctx):
    lab = layer.labeling()
    if lab is None:
        return set()
    if not layer.labelsEnabled() and not INCLUDE_DISABLED_LABELS:
        return set()
    out = set()
    try:
        for pid in lab.subProviders():                 # simple + every (nested) rule with settings
            out |= pal_fields(lab.settings(pid), ctx)
    except Exception:
        out |= pal_fields(lab.settings(), ctx)
    if isinstance(lab, QgsRuleBasedLabeling) or lab.type() == "rule-based":
        out |= rule_filters(lab.rootRule())           # rule filters (nested)
    return out


def renderer_fields(layer, ctx):
    r = layer.renderer()
    if r is None:
        return set()
    out = set(r.usedAttributes(ctx))                  # covers nested rules, DD symbols, generators
    try:
        if r.orderByEnabled():
            for clause in r.orderBy():
                out |= set(clause.expression().referencedColumns())
    except Exception:
        pass
    return out


def diagram_fields(layer, ctx):
    out = set()
    try:
        dr = layer.diagramRenderer()
        if dr is not None and layer.diagramsEnabled():
            out |= set(dr.referencedFields(ctx.expressionContext()))
            ds = layer.diagramLayerSettings()
            out |= set(ds.dataDefinedProperties().referencedFields(ctx.expressionContext()))
    except Exception:
        pass
    return out


def other_fields(layer):
    out = set()
    out |= expr_cols(layer.subsetString())
    try:
        tp = layer.temporalProperties()
        if tp.isActive():
            for f in (tp.startField(), tp.endField(), tp.durationField()):
                if f:
                    out.add(f)
            for ex in (tp.startExpression(), tp.endExpression()):
                out |= expr_cols(ex)
    except Exception:
        pass
    if INCLUDE_DISPLAY_EXPRESSION:
        out |= expr_cols(layer.displayExpression())
        tip = layer.mapTipTemplate() or ""
        import re
        for m in re.findall(r"\[%(.*?)%\]", tip, flags=re.S):
            out |= expr_cols(m)
    return out


def expand_virtual(layer, names):
    """Replace virtual (expression) fields by the fields their expressions reference (recursively)."""
    fields = layer.fields()
    result, todo, seen = set(), list(names), set()
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        if n == ALL:
            result.add(ALL)
            continue
        idx = fields.lookupField(n)
        if idx >= 0 and fields.fieldOrigin(idx) == ORIGIN_EXPRESSION:
            todo.extend(expr_cols(layer.expressionField(idx)))
        else:
            result.add(n)
    return result


def needed_for_current_style(layer):
    ctx = make_context(layer)
    names = set()
    names |= renderer_fields(layer, ctx)
    names |= labeling_fields(layer, ctx)
    names |= diagram_fields(layer, ctx)
    names |= other_fields(layer)
    return names


def needed_all_project_styles(layer):
    """Current style + every other style in the layer's style manager."""
    names = set(needed_for_current_style(layer))
    sm = layer.styleManager()
    current = sm.currentStyle()
    try:
        for st in sm.styles():
            if st == current:
                continue
            if sm.setCurrentStyle(st):
                names |= needed_for_current_style(layer)
    finally:
        sm.setCurrentStyle(current)
    return expand_virtual(layer, names)


def stored_styles(gpkg, table):
    try:
        con = sqlite3.connect(ro_uri(gpkg), uri=True)
        try:
            rows = con.execute("SELECT styleName, styleQML FROM layer_styles WHERE f_table_name = ?",
                               (table,)).fetchall()
        finally:
            con.close()
        return [(n, q) for n, q in rows if q]
    except sqlite3.Error:
        return []   # no layer_styles table


def needed_stored_styles(gpkg, table, uri):
    names = set()
    styles = stored_styles(gpkg, table)
    if not styles:
        return names
    tmpdir = tempfile.mkdtemp(prefix="gpkgstyles_")
    try:
        for i, (sname, qml) in enumerate(styles):
            tmp = QgsVectorLayer(uri, "tmp_style", "ogr")
            if not tmp.isValid():
                break
            p = os.path.join(tmpdir, "s%d.qml" % i)
            with open(p, "w", encoding="utf-8") as f:
                f.write(qml)
            res = tmp.loadNamedStyle(p)
            ok = res[1] if isinstance(res, tuple) else True
            if not ok:
                print("   ! could not load stored style '%s' of %s: %s" % (sname, table, res[0]))
                continue
            names |= expand_virtual(tmp, needed_for_current_style(tmp))
            del tmp
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return names


def ro_uri(path):
    """Read-only SQLite URI (handles spaces / drive letters)."""
    from pathlib import Path
    return Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def gpkg_source(layer):
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return None, None
    d = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    path = d.get("path") or ""
    if not path.lower().endswith(".gpkg"):
        return None, None
    table = d.get("layerName")
    if not table:
        ds = ogr.Open(path)
        if ds is not None and ds.GetLayerCount() == 1:
            table = ds.GetLayer(0).GetName()
        ds = None
    return os.path.normcase(os.path.abspath(path)), table


def output_path(src):
    base = os.path.splitext(os.path.basename(src))[0] + OUTPUT_SUFFIX + ".gpkg"
    return os.path.join(OUTPUT_DIR or os.path.dirname(src), base)


def mb(p):
    return os.path.getsize(p) / 1048576.0 if os.path.exists(p) else 0.0


# ----------------------------- main ------------------------------------------
def run():
    plan = {}   # gpkg -> table -> {"keep": set, "layers": [names], "all": bool}
    for layer in QgsProject.instance().mapLayers().values():
        gpkg, table = gpkg_source(layer)
        if not gpkg or not table:
            continue
        entry = plan.setdefault(gpkg, {}).setdefault(table, {"keep": set(), "layers": [], "uri": layer.source()})
        entry["layers"].append(layer.name())
        try:
            entry["keep"] |= needed_all_project_styles(layer)
        except Exception as e:
            print("! %s: style analysis failed (%s) -> keeping all fields" % (layer.name(), e))
            entry["keep"].add(ALL)

    if not plan:
        print("No GeoPackage vector layers in this project.")
        return

    if INCLUDE_GPKG_STORED_STYLES:
        for gpkg, tables in plan.items():
            for table, entry in tables.items():
                try:
                    entry["keep"] |= needed_stored_styles(gpkg, table, entry["uri"])
                except Exception as e:
                    print("! %s: stored style analysis failed (%s) -> keeping all fields" % (table, e))
                    entry["keep"].add(ALL)

    for gpkg, tables in plan.items():
        dst = output_path(gpkg)
        print("\n=== %s\n    -> %s" % (gpkg, dst))
        if os.path.normcase(os.path.abspath(dst)) == gpkg:
            print("! output equals input, skipped")
            continue

        if not DRY_RUN:
            for ext in ("", "-wal", "-shm", "-journal"):
                if os.path.exists(dst + ext):
                    os.remove(dst + ext)
            # consistent snapshot of the source (includes WAL content), source untouched
            src_con = sqlite3.connect(ro_uri(gpkg), uri=True)
            dst_con = sqlite3.connect(dst)
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
                src_con.close()

        ds = ogr.Open(dst if not DRY_RUN else gpkg, 1 if not DRY_RUN else 0)
        if ds is None:
            print("! cannot open %s with OGR" % (dst if not DRY_RUN else gpkg))
            continue
        keep_always = set(k.upper() for k in KEEP_ALWAYS)
        for table, entry in sorted(tables.items()):
            lyr = ds.GetLayerByName(table)
            if lyr is None:
                print("! table %s not found" % table)
                continue
            defn = lyr.GetLayerDefn()
            physical = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
            keep = entry["keep"]
            if ALL in keep:
                print("  %-40s keep ALL %d fields (a style references all attributes)" % (table, len(physical)))
                continue
            keep_u = set(k.upper() for k in keep) | keep_always
            drop = [f for f in physical if f.upper() not in keep_u]
            kept = [f for f in physical if f.upper() in keep_u]
            missing = sorted(k for k in keep if k.upper() not in set(p.upper() for p in physical))
            print("  %-40s keep %d/%d: %s" % (table, len(kept), len(physical), ", ".join(kept) or "-"))
            if missing:
                print("  %-40s (referenced but not in table - joins/unknown: %s)" % ("", ", ".join(missing)))
            if DRY_RUN or not drop:
                continue
            for name in drop:
                idx = lyr.GetLayerDefn().GetFieldIndex(name)
                if idx >= 0 and lyr.DeleteField(idx) != 0:
                    print("  ! could not delete %s.%s" % (table, name))
        ds = None

        if not DRY_RUN:
            con = sqlite3.connect(dst)
            try:
                con.execute("VACUUM")
            finally:
                con.close()
            print("  size: %.1f MB -> %.1f MB" % (mb(gpkg), mb(dst)))

    print("\nDone%s." % (" (dry run, nothing written)" if DRY_RUN else ""))


try:
    run()
except Exception:
    traceback.print_exc()
