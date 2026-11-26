# Changelog

## 1.1

- **Load polygons as lines or points.** In the add-layer dialog, *Load polygons as*:
  - **Lines:** the polygon boundaries; every ring (holes included) becomes a line.
  - **Points:** one point per feature, the centroid. If the centroid falls outside the polygon (U/L shapes, multipart), QGIS's *point on surface* is used instead, so the point is always inside.

  The same table can be added several times in different modes. Identify, selection, filters and fast overview work on the converted geometry, and the mode is stored in the layer source.

## 1.0 — first public release

A read-only QGIS data provider (`stgeometry`) for Oracle tables that store geometry in Esri `SDE.ST_GEOMETRY`.

**Reading**
- Tables become ordinary QGIS layers: rendering, attribute table, identify, selection, layer filter (Oracle SQL), expressions, labels and styles.
- Bulk fetching with python-oracledb (thin, or thick automatically), with LOBs returned inline. Automatic fallback to QGIS's Qt OCI driver.
- Optional parallel sessions per map read (1–16).

**Geometry**
- Local decoder for the Esri `POINTS` storage format: a numpy batch decoder with a per-row fallback. It is verified against `sde.st_asbinary` when a layer is added and used only for shape types that verified; any other row is converted by Esri on the server.
- Polygons with holes, multipart polygons, islands in holes, lines and points. Optional Force 2D.
- Optional true curves (circular arcs) from `SE_ANNO_CAD_DATA` / `GDB_GEOMATTR_DATA`: either kept as curves (QGIS segmentation settings) or segmented by the plugin with a maximum angle or maximum distance.

**Performance**
- The ST grid index (`sde.st_envintersects`) or a plain envelope scan is chosen per view, from the exact data extent and the row count.
- Optional fast overview: features smaller than *N* pixels are merged in Oracle per pixel and style value (map drawing only).
- Expression push-down to Oracle SQL; only the needed columns are fetched; detected layer settings are stored in the layer source.

**Usability**
- "Add layer" dialog with table search, diagnostics and a benchmark of the current map view.
- Standalone connections through `connections.json` (passwords optional; never stored in projects).
- Timing log per map read.
