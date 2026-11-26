# Changelog

## 1.5

- **Domain and subtype descriptions** (add-layer option, on by default), read once from `SDE.GDB_ITEMS` (standard Esri XML):
  - **A:** `<FIELD>_DESC` text fields for every field with a coded-value domain, plus `<SUBTYPE>_DESC` with the subtype names. **Per-subtype domains are handled exactly:** each feature's description comes from its own subtype's domain, so the same code can mean different things per subtype. Raw values are kept.
  - **B:** Value Map widgets (descriptions instead of codes in the attribute table and forms) on the subtype field and on raw fields whose domain is the same for every subtype.
- **Value maps on every ST Geometry layer:** they're applied automatically whenever such a layer enters a project (dialog, Browser, script or a loaded project). Widgets you've changed yourself are kept.
- **Simpler dialog:** it opens showing only the connection, *List tables*, the table list and *Add selected layer(s)*. All options and tools (Fastest settings, Tune, Diagnostics, Benchmark) are in a collapsible **Advanced settings** box, collapsed on open, as in QGIS processing tools.
- New `test_combined.py` (18 checks, everything together: versioned view with misencoded edits, descriptions, value maps through the plugin's project hook, parallel, fast overview, points layer, and a project save/reload round trip).
- New `test_domains.py` (13 checks: the same codes in different domains per subtype, fallback to the field's default domain, text-coded domains, NULLs, subset requests, filters, unique values, parallel strips, value maps).
- **Fix: features in the wrong place (e.g. "in the sea") when reading through the versioned view.** Rows from ArcGIS's edit tables could be stored in a way the local decoder (verified on the base table only) decoded wrongly. Every decoded geometry must now reproduce Esri's stored envelope (`MINX…MAXY`); otherwise that row is converted by Esri on the server. This protects plain tables too. Cost: about 0.3 µs per feature.
- **Much faster versioned view (`_EVW`): hybrid read.** Never-edited features (not in the `A<n>`/`D<n>` delta tables) are read from the base table, with the ST index, parallel strips and the local decoder. Only edited features are read through `_EVW`. The result is identical to reading `_EVW`; feature counts use the same split.
- **Tests:** new `test_evw.py` (edits, inserts and deletes; parallel 1 and 4; wrongly-encoded edited rows; a corrupt base row). Without the envelope check it reproduces the problem: 25 buildings misplaced.

## 1.4

- **Fix: parallel sessions on zoomed-in views.** With *Parallel sessions per layer* above 1, zoomed-in views (spatial index plan) showed no features. Each parallel strip query was sent with a view coordinate it didn't use, and python-oracledb rejects unused bind values (DPY-4008). The database layer now sends only the bind values that appear in each statement.
- **Automatic scan threshold (new default).** *Use plain scan above* = **Auto**: the plain scan is used when the view holds more than 10 % of the table (at least 50,000 rows), the measured break-even between the ST index (~70 µs per row in view) and a full envelope scan (~7 µs per table row). A fixed number can still be entered. *Fastest settings* sets Auto.
- **Tests:**
  - new `test_parallel.py` (63 checks: parallel 1/2/3/4/8/16 × index strips, horizontal strips, data edge, full extent, plain scan, expression and layer filters, real zoomed-in renders);
  - `test_tune.py` extended (fresh profile, corrupt saved settings, no python-oracledb, no numpy);
  - the simulated database now rejects unused bind values like Oracle does.

## 1.3

- **Parallel sessions no longer repeat index work.** With the ST spatial index, the view is cut into strips: each session searches only its own strip, and each feature belongs to exactly one strip. Previously, `MOD(OBJECTID, N)` repeated the full index search in every session.
- **Fast overview is a single query.** Sub-cell features return no geometry columns (no blob, no Esri conversion) and are merged per cell and style value on the client. Previously, two queries ran the spatial filter twice.
- **New *Tune on current map view* button.** It measures real renders of the current view with parallel 1/4/8 × overview off/on, and applies the fastest.
- **Default parallel sessions: 4** (was 8). Use *Tune* to find the best value for your database.
- New tests: parallel strips return identical features with no duplicates; tune and fastest-settings buttons.

## 1.2

- **Fastest settings by default:**
  - parallel sessions 8 (was 1);
  - fast overview on (1 px);
  - Force 2D ticked for new layers;
  - connection pool up to 64 sessions.
- **New dialog button, *Fastest settings*:** applies the fastest configuration to every option, including settings saved earlier in your QGIS profile.
- **Faster reading, profiled in QGIS 4.2.3:**
  - rows are handed over in whole batches, with no per-row Python call;
  - rows are selected for the batch decoder with comprehensions;
  - each feature's binary geometry header is one struct pack instead of several concatenations;
  - batch-decoded geometries skip the per-feature type check.

  The plugin plus QGIS now costs about **9 µs per feature with geometry** (was about 12) and about 5.6 µs for attribute-only reads. Output is unchanged: 0 mismatches against the per-feature decoder.

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
