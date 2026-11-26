# ST Geometry Provider for QGIS: what it does and how it works

Version 1.5 · read-only · QGIS 3.34 to 4.x · Oracle with Esri SDE.ST_GEOMETRY

---

## 1. In one paragraph

The plugin adds a new QGIS **data provider** called `stgeometry`. It reads Oracle tables whose shape column has the Esri type `SDE.ST_GEOMETRY` and shows them as normal QGIS vector layers. You can draw them, open the attribute table, use Identify, select, filter, label and style them.

It does **not** convert anything to Oracle `SDO_GEOMETRY`, and it never writes to the database. For each feature it asks Oracle for the geometry in the standard OGC **Well-Known Binary (WKB)** format, by calling Esri's own function `sde.st_asbinary`. QGIS reads WKB natively.

---

## 2. Does it convert ST_GEOMETRY to SDO_GEOMETRY?

**No.** Only two formats are involved: the stored Esri format and WKB.

```
 Oracle                                                     QGIS (your PC)
 ──────────────────────────────────────────────────────     ───────────────────────────────
 table row
   SHAPE  (SDE.ST_GEOMETRY object)
     │
     │  sde.st_asbinary(SHAPE)   ← Esri's own C library (st_shapelib),
     │                             called by Oracle through "extproc"
     ▼
   OGC WKB bytes  ──── network, 1000 rows per round trip ──►  QgsGeometry.fromWkb()
                                                              │
                                                              ▼
                                                   QGIS geometry (GEOS compatible)
                                                   drawn / identified / selected
```

Here is how this differs from the query layer you used before:

| | Old query layer | This plugin |
|---|---|---|
| Server-side conversion | `sde.st_astext` makes WKT text, then `SDO_UTIL.FROM_WKTGEOMETRY` (Java inside Oracle) makes SDO_GEOMETRY | `sde.st_asbinary` makes WKB (binary, no text, no Java, no SDO) |
| Transfer | SDO object, one LOB fetched per row | WKB bytes fetched inline, 1000 rows per round trip |
| Client | QGIS Oracle provider decodes SDO | QGIS decodes WKB directly |
| Z | Forced to 2D | Dropped by default ("Force 2D"); can be kept |
| Map extent filter | Envelope of the converted SDO | ST spatial index (`sde.st_envintersects`) |

---

## 3. How ST_GEOMETRY is stored (background)

`SDE.ST_GEOMETRY` is an Oracle object type with these main attributes:

| Attribute | Meaning |
|---|---|
| `ENTITY` | Shape type (point, line, polygon, multipart, has Z / M, …) |
| `NUMPTS` | Number of vertices |
| `MINX, MINY, MAXX, MAXY` (+ `MINZ/MAXZ`, `MINM/MAXM`) | Envelope (bounding box) |
| `AREA`, `LEN` | Pre-computed area and length |
| `SRID` | Id of the Esri spatial reference (`SDE.ST_SPATIAL_REFERENCES`) |
| `POINTS` | BLOB holding the vertices in Esri's compressed binary format: integer coordinates on the spatial reference's grid (false origin + XY units), delta-encoded |

The `POINTS` format is internal to Esri and not officially documented. That's why the plugin lets Esri's own `sde.st_asbinary` decode it, which guarantees exactly the same geometry ArcGIS sees.

A spatial index on an ST_GEOMETRY column is a **grid index**: a side table `S<layer_id>_IDX$` that maps grid cells to rows. `sde.st_envintersects` uses it.

---

## 4. What happens step by step

### 4.1 Adding a layer (done once per layer)

1. **Connection.** The plugin reuses a saved QGIS Oracle connection (Browser → Oracle), including its user and password or authentication config.
2. **Columns.** It reads `ALL_TAB_COLUMNS`. The ST_GEOMETRY column becomes the geometry, and the other columns become fields:

   | Oracle type | QGIS field |
   |---|---|
   | NUMBER(p≤9, 0) | Integer |
   | NUMBER(p>9, 0) / NUMBER(*,0) | Integer64 |
   | NUMBER(p,s>0), NUMBER, FLOAT, BINARY_* | Double |
   | VARCHAR2, NVARCHAR2, CHAR, NCHAR, CLOB, NCLOB | String |
   | DATE, TIMESTAMP* | DateTime |
   | BLOB, RAW, LONG, other object types (e.g. `GDB_GEOMATTR_DATA`) | skipped (logged) |

3. **Feature id (key).** In order of preference:
   - the SDE registration row id column from `SDE.TABLE_REGISTRY` (normally `OBJECTID`)
   - otherwise a column named `OBJECTID`
   - otherwise a single-column unique integer index
4. **CRS.** It reads the SRID from `SDE.ST_GEOMETRY_COLUMNS`, then the matching row of `SDE.ST_SPATIAL_REFERENCES`, and tries these in order:
   - the EPSG code
   - the WKT definition (Esri `GCS_WGS_1984` maps to EPSG:4326)
   - fallback EPSG:4326
5. **Geometry type.** It converts the first 50 non-null shapes and decides Point, Line or Polygon. If any of those shapes has Z or M, the layer gets Z or M too. The layer type is always the **Multi** variant (e.g. `MultiPolygonZ`), so single and multipart shapes can live in one layer.
6. **Extent.** It's read from `SDE.LAYERS` (instant). Only if the table isn't registered there does the plugin compute it from the shape envelopes.
7. **Saved in the layer source.** Key, geometry type and CRS are written into the layer source, so reopening a project skips detection.

### 4.2 Drawing the map

For every redraw (pan or zoom), QGIS asks the provider for the features in the visible extent. The plugin sends one SQL statement, simplified here:

```sql
SELECT t."OBJECTID", <only the columns the style/labels need>, sde.st_asbinary(t."SHAPE")
FROM   "OWNER"."TABLE" t
WHERE  sde.st_envintersects(t."SHAPE", :xmin, :ymin, :xmax, :ymax) = 1   -- ST spatial index
  AND  (<layer filter, if any>)
```

- If the visible extent contains the whole layer, the spatial condition is left out, so a plain scan is used.
- Rows arrive in batches of 1000. Each WKB becomes a `QgsGeometry`, and the feature goes to the QGIS renderer.
- With **parallel sessions** (default 4; 1–16 in the dialog), a big read is split into N parts that run at the same time on N Oracle sessions (§8).

### 4.3 Attribute table, Identify, selection

- **Attribute table.** QGIS asks for features *without geometry*, so the plugin leaves `st_asbinary` out of the SQL entirely. That's the cheapest possible query.
- **Identify / select by rectangle.** The index finds candidates by envelope. Then the plugin tests the **exact** geometry against the rectangle on the client, so only shapes that really intersect are returned.
- **Selection by id** (e.g. "zoom to selected"): `WHERE OBJECTID IN (...)`, in chunks of 1000.

---

## 5. Polygons: rings, holes, multipart

### 5.1 What Esri stores

An Esri polygon is a list of **rings** (closed paths), not a nested structure. A ring is either:

- an **exterior** ring (the outer boundary; Esri's own convention is clockwise), or
- an **interior** ring, which is a **hole** (counter-clockwise in Esri's convention).

### 5.2 What `sde.st_asbinary` returns (OGC model)

OGC WKB is nested. Esri's function groups the rings:

- each exterior ring together with the holes inside it becomes one OGC **Polygon** (exterior ring first, then its interior rings);
- if there is more than one exterior ring, the result is a **MultiPolygon**.

So a "polygon with 2 rings" can be either of two things, and both are handled correctly:

| Case | Rings | WKB from Oracle | In QGIS |
|---|---|---|---|
| Polygon with a hole | 1 exterior + 1 interior | `Polygon` with 2 rings | Hole is empty (not filled), area excludes the hole |
| Two separate parts | 2 exteriors | `MultiPolygon` with 2 polygons | One feature, two filled parts |
| Island inside a hole | exterior + hole + exterior inside the hole | `MultiPolygon`: (exterior + hole), (island) | Drawn correctly |
| Many holes | 1 exterior + N interiors | `Polygon` with N+1 rings | All holes empty |

### 5.3 What the plugin does with them

- **Ring structure is passed through untouched.** No re-ordering, re-orienting, merging, dissolving or "fixing".
- **Single Polygon to MultiPolygon.** A single polygon is wrapped as a MultiPolygon with one part, so the whole layer has one type. This changes no coordinates, rings or holes.
- **Ring orientation doesn't matter in QGIS.** Drawing, area, length, identify and spatial predicates (GEOS) all work whichever way the rings are oriented. The plugin doesn't change orientation.
- **Validity is not changed.** Esri's "simple" rules are close to OGC validity but not identical; some ring self-touching cases are treated differently. If you run QGIS geoprocessing that needs strictly valid input, check first with *Vector → Geometry Tools → Check Validity* and fix with *Fix Geometries* if needed. Plain display and identify don't need this.

### 5.4 Lines and points

- **Lines.** A single path becomes `LineString`, several paths become `MultiLineString`. The layer is always `MultiLineString(Z)`.
- **Points.** `Point` or `MultiPoint`. The layer is always `MultiPoint(Z)`.

---

## 6. Other geometry details

| Topic | Behaviour |
|---|---|
| **Z values** | Kept when the data has Z. "Force 2D" drops Z and M on the client. |
| **M values** | Kept if present, otherwise none. |
| **Precision** | Coordinates come from Esri's integer grid as doubles. The plugin applies **no rounding**; you get what ArcGIS stores. |
| **NULL / empty shapes** | Feature appears in the attribute table without geometry; never drawn. |
| **True curves** | ST_GEOMETRY holds densified vertices. In geodatabases, ArcGIS keeps true-curve parameters separately (the `GDB_GEOMATTR_DATA` BLOB column), which the plugin skips. You see the densified shape that the ST functions also see. If WKB ever contained curves, they'd be segmentized. |
| **CRS** | Auto-detected from the SDE spatial reference (fallback EPSG:4326). If the QGIS project CRS differs, QGIS reprojects on the fly. |
| **Mixed geometry types in one table** | The layer type comes from the first 50 shapes. Shapes of another kind (rare) aren't drawn by the layer's renderer. |

---

## 7. Filters and expressions

- **Layer filter** (*right click → Filter…*): plain **Oracle SQL WHERE clause**, e.g. `"STATUS" = 'A' AND "AREA_M2" > 1000`. It's validated against Oracle first and stored in the layer source (`sql=`). All queries, counts and unique-value lists respect it.
- **QGIS expressions** (select by expression, rule-based styles, attribute table filter). Simple parts are translated to Oracle SQL so filtering happens in the database:
  - `=`, `<>`, `<`, `<=`, `>`, `>=`
  - `LIKE`, `ILIKE`, `NOT LIKE`
  - `IN`, `NOT IN`
  - `IS NULL`, `IS NOT NULL`
  - `AND`, `OR`, `NOT`
  - `+`, `-`, `*`, applied to number and text columns

  Anything else (functions, dates, `$area`, …) is evaluated by QGIS. QGIS always re-checks the **whole** expression on what comes back, so the translation only affects speed, never results.

---

## 8. Performance design (everything that makes it fast)

Early measurements with the Qt OCI driver showed that reading the raw LOB, `st_asbinary` and `st_astext` all cost about the same per row. So the time went into **per-row LOB fetching**, not into the conversion itself. For small views, the ST spatial index was an order of magnitude faster than a plain envelope-column filter. The design follows from that:

**Database side**

1. **LOBs fetched inline.** The python-oracledb driver is told to return BLOB/CLOB values as plain bytes/strings inside each batch, instead of LOB handles that each need an extra round trip. This was the main bottleneck.
2. **Batches of 1000 rows** (array size + prefetch) per network round trip.
3. **ST spatial index** for the map extent (`sde.st_envintersects`); skipped when the whole layer is visible.
4. **Parallel sessions** (default 4). A big map read is split into N parts fetched concurrently, each on its own Oracle session.
   - With the spatial index, the **view is cut into N strips**. Each session searches the index only inside its strip, and each feature belongs to exactly one strip: the one containing its envelope corner, clipped to the view. The index work is **divided** between the sessions, never repeated.
   - Without the index (plain scan or whole layer), rows are split by OBJECTID ranges, so each session reads only its own part of the table.
   - The first and last ranges are open-ended, so rows added later are never missed.
5. **Pipelining.** Background threads fetch the next batches while QGIS builds features from the current one. A bounded queue keeps memory constant. Cancelling a redraw stops the threads.
6. **Connection pool** (up to 64 sessions, kept open), so redraws don't reconnect.
7. **Only the needed columns.** Rendering asks only for the columns the style and labels use, and the attribute table asks for no geometry.
8. **Bind variables** for the extent and filter values, so Oracle reuses the parsed statement on every pan.
9. **Row limits pushed to Oracle** (`FETCH FIRST n ROWS ONLY`), only when that's provably exact.
10. **Metadata is cheap.** The extent comes from `SDE.LAYERS`, the OBJECTID range from the unique index, and detected key, type and CRS are saved in the project, so a reopen runs no detection.

**Client side (per feature)**

11. WKB is handed straight to QGIS's C++ parser (`fromWkb`); there's no Python-side geometry parsing.
12. The geometry is converted to Multi or Z only when its type differs from the layer type.
13. Attributes are copied as one list; only DATE/TIMESTAMP columns need conversion.
14. Fields are attached without re-initialising, and the coordinate transform is skipped when not needed.

**Settings in the dialog**

- **Driver:** Auto (python-oracledb, fallback Qt OCI) / python-oracledb only / Qt OCI only.
- **Parallel sessions per layer:** 1 to 16. 1 means a single session and no background threads.
- **Diagnostics** button: prints the driver, detected type and CRS, and timings, including a full-layer read with the current parallel setting.

**QGIS settings that also help** (Settings → Options → Rendering):

- **Render layers in parallel:** on (default).
- **Use render caching where possible:** on (default). Redraws after selection or labelling changes reuse the cache.
- **Simplify geometry / threshold:** on (default). It's done on the client and greatly speeds up drawing of detailed polygons when zoomed out.
- Use **scale-dependent visibility** on very large layers, so they aren't drawn at small scales.
- Keep the **project CRS = EPSG:4326** if possible to avoid on-the-fly reprojection.

> **Note for the DBA:** while a big layer is being drawn, the plugin may use up to *N* (parallel setting) extra sessions for that layer. Idle sessions stay in a pool, up to 48 per connection, until QGIS closes. Set "Parallel sessions" to 1 to use one session per read.

---

## 9. Drivers and connections

| Driver | When used | Notes |
|---|---|---|
| **python-oracledb thin** | Default | Pure Python, needs no Oracle client. Fastest path. |
| **python-oracledb thick** | Automatic if the server refuses thin mode (e.g. some network-encryption setups) | Uses the Oracle client libraries QGIS ships with (`oci.dll`) |
| **Qt OCI (QOCISPATIAL)** | Automatic fallback, or chosen in the dialog | The driver QGIS uses itself. Slower (LOBs fetched row by row, no parallel). |

Credentials come from the QGIS connection or authentication config. If none are saved, the plugin asks once per QGIS session and keeps them only in memory.

---

## 10. Versioned data

- By default the plugin reads the **base table**. With **traditional versioning**, edits that sit in the version delta tables (`A<n>` / `D<n>`) and haven't been compressed into the base table are **not visible** there.
- Tick **"Read through versioned view (_EVW)"** to read `<TABLE>_EVW`, which shows the **DEFAULT** version. Other named versions aren't supported.

### Fast and exact: the hybrid read
`_EVW` is a complex view (base table, minus deletes, plus adds, across version states). Oracle can't use the ST spatial index through it efficiently, so querying it directly is slow. The plugin therefore splits each read in two:

| Features | Read from | Why it's exact |
|---|---|---|
| **Never edited** (OBJECTID in neither delta table `A<n>` nor `D<n>`) | the **base table**, with the ST index, parallel strips and the local decoder | Without any edit, the base row *is* the DEFAULT row |
| **Edited** (OBJECTID in `A<n>` or `D<n>`) | **`<TABLE>_EVW`** | Esri's view decides their DEFAULT state (updated, inserted or deleted) |

`<n>` is the feature class's registration id in `SDE.TABLE_REGISTRY`. The edited set is re-read on every map read, so edits show up as soon as they're saved. Unless edits are never compressed, the edited share is small, and the read costs about the same as a plain table read.

The log shows `versioned view read as hybrid …` when the layer loads, and the timing log shows `evw=hybrid`. If the delta tables aren't visible to your user, the plugin reads `_EVW` directly (slower, same result).

---

## 11. Limitations

- **Read-only:** no editing.
- **QGIS Server:** not yet. The provider is designed so it can be loaded there later, but that isn't packaged yet.
- **Spatial filtering** uses the envelope index; exact geometry tests are done in QGIS only where QGIS asks for them (identify, selection).
- **Layer extent** comes from `SDE.LAYERS` and may be slightly stale if ArcGIS hasn't updated it. It only affects "Zoom to layer", not what is drawn.
- Tables with **no integer unique key** need a `key='COLUMN'` in the layer source.

---

## 12. Troubleshooting

- **View → Panels → Log Messages → "ST_Geometry"**: connection and driver used, detection results, skipped columns, and the full SQL of any failed query.
- **Dialog → Diagnostics** gives short timing lines without coordinates or names, safe to share.
- If anything misbehaves after the update, set **Parallel sessions = 1** and/or **Driver = Qt OCI only**. Both return to the simplest, single-session code paths.

### Layer source format (advanced)

```
stconn='MyOracleConn' table="OWNER"."PARCELS" (SHAPE) key='OBJECTID' type=MultiPolygonZ stcrs='EPSG:4326'
   optional:  stforce2d='1'   stevw='1'   stgeomtype='polygon|line|point'   sql=<Oracle WHERE clause>
```

Python: `QgsVectorLayer(source, "Parcels", "stgeometry")`

---

## 13. Spatial plan per view and standalone connections

### Index or plain scan, chosen for every map read
On a large table, the ST grid index (`sde.st_envintersects`) costs roughly a fixed amount **per returned row** (tens of µs). A full scan comparing the envelope columns (`SHAPE.MINX … MAXY`) has a fixed cost that depends on table size, not on the view. For views holding millions of features, the scan wins; for small views, the index wins.

So for every map read the plugin estimates the rows in view:

`estimate = (view ∩ data extent area / data extent area) × table rows (Oracle statistics)`

It then picks one of three plans:

| Situation | Plan |
|---|---|
| View contains the whole data extent | no spatial condition at all |
| Estimated rows > threshold (Auto: 10 % of the table, at least 50,000) | plain scan with envelope conditions (`minx <= :x2 AND maxx >= :x1 …`) |
| Otherwise | ST grid index (`sde.st_envintersects`) |

For this to work the plugin needs the **exact data extent**. `SDE.LAYERS` can hold a much larger extent than the data (yours did). When a layer is added, the dialog option *"Compute exact data extent"* runs a one-time scan (`MIN/MAX` of the envelope columns) and stores the result in the layer source (`stext=`). From then on, *Zoom to layer* is exact too.

When a layer is added, its detected key, geometry type, CRS and extent are now written into the layer source. Saved projects reopen with **no detection queries**.

### Standalone connections: `connections.json`
The plugin no longer needs a QGIS Oracle connection. Copy `connections.example.json` to `connections.json` and fill it in:

```json
{
  "connections": [
    { "name": "GIS production", "host": "db-server", "port": 1521, "service_name": "ORCL",
      "username": "gis_reader", "password": "", "schemas": ["GIS_OWNER"] }
  ]
}
```

- **Where the file is looked for** (the first that exists wins):
  1. the path in the environment variable `ST_GEOMETRY_CONFIG`
  2. `connections.json` in the plugin folder
  3. `st_geometry_connections.json` in the QGIS profile folder, which survives plugin re-installs
- Instead of `host`/`port`/`service_name`, you can give a `dsn` (TNS alias or full descriptor).
- `schemas` (optional) limits the table list to those owners.
- **Credentials:** if `username` or `password` is empty, the user is asked once per QGIS session. Layers store only the connection *name* (`stcfg='GIS production'`), never the password, so project files contain no credentials.
- **Security:** a password written in the file is plain text, readable by anyone who can read the file. Prefer an empty password (asked once per session), or a read-only database account limited to the needed schemas.

### Timing log
With "Timing log" ticked, every map read writes two lines to *Log Messages → ST_Geometry*:

- a `start` line with the chosen plan (`spatial=index|scan|none`);
- an end line with `rows`, `wait_db` (Oracle plus network), `build` (Python), `qgis` (drawing) and `total`.

---

## 14. Local decoder for the Esri POINTS blob

Measurements showed that the **per-row conversion on the server** (`sde.st_asbinary`, run in Esri's external process) was a main cost: many parallel sessions made drawing much faster. The plugin can skip that conversion. It reads the stored `POINTS` bytes and decodes them in Python on your PC.

### The format (reverse engineered from synthetic samples, no real data)
| Part | Content |
|---|---|
| Bytes 0–7 | Header (payload length, flags). Not needed for decoding. |
| Bytes 8… | A stream of variable-length signed integers. **First byte:** `0x80` = more bytes follow, `0x40` = negative, low 6 bits = value. **Next bytes:** `0x80` = more, low 7 bits = value. |
| Vertices | The first vertex is absolute. Every following vertex is the **difference to the previous one**, and the chain continues across rings and parts. |
| Coordinates | `x = value / XYUNITS + FALSEX`, `y = value / XYUNITS + FALSEY`. The parameters come from `SDE.ST_SPATIAL_REFERENCES`. |
| Z | Follows the X/Y values. The exact layout is detected per layer. |
| Polygon rings | **No ring markers.** A ring ends where it returns to its own start point (an exact integer comparison). Rings inside an earlier ring become its holes; rings outside start a new part (MultiPolygon). |

### Safety: it never guesses silently

**Envelope check on every row.** Esri stores each shape's bounding box (`MINX … MAXY`) next to its `POINTS`. Every locally decoded geometry, in batch or per row, must reproduce that box (within the coordinate grid). If it doesn't (for example an edit-table row written differently), the row is converted by Esri on the server instead. A wrongly decoded shape can therefore never be drawn in the wrong place. Cost: about 0.3 µs per feature.

1. **Verification at layer load.** For each layer, 300 rows are decoded locally *and* converted by Esri (`sde.st_asbinary`), then compared vertex by vertex: same parts, same rings, same coordinates within the grid resolution. Ring orientation is ignored, because it doesn't matter to QGIS.
   - The decoder is switched on only for the shape types (entity codes) whose sample rows **all** matched.
   - The Z layout is chosen the same way. If Z can't be verified but X/Y can, the decoder is used only for layers with **Force 2D** ticked.
2. **Unverified shape types go to Esri.** The SQL asks Oracle to convert only those rows:
   `CASE WHEN SHAPE.entity IN (<verified>) THEN NULL ELSE sde.st_asbinary(SHAPE) END`
3. **Per-row fallback.** If a single row doesn't decode cleanly (unexpected value count, unclosed ring), it's fetched once from Esri by its OBJECTID. For polygons with several rings, the decoded area is also checked against Esri's stored `AREA`.
4. **Status** is written to *Log Messages → ST_Geometry* when the layer loads (`local decoder ON entities=[8] … verified 300/300 rows` or the reason it's off). Every timing line shows `decoder=local|server` and `fallback=<rows sent to Esri>`.

### Settings
- Dialog **"Geometry decoding"**: *Local decoder where verified* (default) or *Esri st_asbinary on the server*. Re-add layers after changing it.
- The **Benchmark** has a line **B3b**: fetching raw bytes plus local decoding time, to compare with **B4** (Esri conversion).

### Refinements
- **Verify once, not on every open.** When a layer is added, the verified decoder settings and spatial-reference parameters are stored in the layer source (`stdec=`). So is the OBJECTID range used for splitting reads (`stkeys=`). Reopening a project runs no verification and no spatial-reference or key-range queries. Protection stays the same: only verified shape types are decoded locally, and any row that doesn't decode cleanly still goes to Esri. To re-verify (e.g. after changing Force 2D), re-add the layer.
- **The decoder writes the layer's Multi type directly** (`MultiPolygon(Z)`, `MultiPoint(Z)`, `MultiLineString(Z)`), so features need no type conversion afterwards.
- **Faster inner loops:** iterator-based integer parsing; ring splitting by integer closure without building a tuple per vertex; scaling by a precomputed inverse; and X/Y(/Z) interleaving through array slice assignment.

---

## 15. Fast overview inside the single live layer

**Goal:** one live layer that stays fast when millions of features are in view, with no second layer and no cache.

**How:** when QGIS draws a layer, its request carries the **size of one screen pixel** (the simplification tolerance; on by default, 1 pixel). The plugin keeps **one query** (the spatial filter runs once), but the geometry columns depend on each feature's stored envelope:

| Feature | What Oracle returns |
|---|---|
| **Large** (envelope ≥ cell) | full geometry (local decoder / Esri) |
| **Sub-cell** (envelope < cell) | only the id, the style attributes and its cell (`FLOOR(minx/cell), FLOOR(miny/cell)`). **No geometry bytes, no Esri conversion, no decoding.** |

On your PC, sub-cell features with the **same cell and the same style values are merged**, and only the first one is drawn. Each merged cell is drawn as **one cell-sized square** (or a point/short line for point and line layers). A sub-pixel feature can't look different from a pixel anyway, so the map looks the same. The rows returned depend on **screen size**, not table size.

**Exact everywhere else.** Identify, selection, attribute table, export, processing and labels all use requests without the pixel information. They always get real geometries and real feature counts.

**Side effects at small scales, worth knowing:**
- A *selected* feature that is smaller than a pixel may not be highlighted, because its pixel may be represented by a neighbour's id.
- Styles based on geometry (e.g. colour by `$area`, geometry generators) see the pixel square for sub-pixel features.
- Labels on sub-pixel features are placed on the pixel square. Such labels are usually hidden by label collision anyway.

**Control (dialog):** *Fast overview: draw features smaller than [N px] as pixel cells*.
- **On by default** (fastest). Untick it to always draw full geometries. It applies to all layers from the next redraw.
- **N** (0.5–10 px, default 1). Features whose envelope is smaller than N screen pixels are merged into N-pixel cells. Higher N means faster but coarser at small scales.

It needs QGIS's standard drawing optimisation, which is on by default in QGIS Desktop (Layer Properties → Rendering → *Simplify geometry*). That's how the pixel size reaches the provider. This was verified in QGIS 4.2.3: a render request carries `OptimizeForRendering` with a tolerance of exactly one pixel in map units.

The timing log shows `overview=<N>px cell=<cell size>` on the start line and `cells=<merged rows>` on the end line.


---

## 16. Shapes supported (tested in QGIS 4.2.3)

| Shape | Supported | Notes |
|---|---|---|
| Polygon with one or more holes | ✅ | Holes stay empty. Tested: 1 hole, 2 holes. |
| Multipart polygon | ✅ | Each exterior becomes its own part. Tested: 2 parts with a hole in one. |
| Island inside a hole | ✅ | Becomes a separate part, like Esri's own output |
| Lines, multipart lines, points, multipoints | ✅ | Each type is decoded locally only if verification passes; otherwise Esri converts it |
| **Arcs / true curves** | ✅ (optional) | See §17. With curves off, the ArcGIS densified shape is shown. |

**Test method:** a synthetic table (3,000 features: buildings, parcels, holes, multipart, island-in-hole, Z values) encoded in Esri's `POINTS` format was served by a simulated Oracle to the real QGIS 4.2.3 engine. The results were compared with the reference geometry: parts, holes and coordinates matched, with 0 mismatches.


---

## 17. True curves (arcs) with your own segmentation

**Where arcs live.** ST_GEOMETRY stores a *densified* copy of curved features: many short straight segments chosen by ArcGIS. The real curve definition (circular arcs, Bézier curves) is kept in an extra BLOB column: `SE_ANNO_CAD_DATA` in Oracle, `GDB_GEOMATTR_DATA` elsewhere. It's stored only for features that actually have curves.

**Format.** Esri's *extended shape buffer*: shape type, bounding box, parts, points, Z/M, then a list of *segment modifiers*. Each modifier says "segment *i* is a circular arc through point P", "…around center C, clockwise or counter-clockwise", or "…a cubic Bézier". The parser follows GDAL's implementation of this format (`ogrpgeogeometry.cpp`).

| Curve type | Handling |
|---|---|
| Circular arc (interior-point or center form, CW/CCW, full circle) | **True arc** (`CircularString` inside a `CompoundCurve`) |
| Cubic Bézier | Sampled into 32 points (QGIS has no Bézier type) |
| Elliptic arc | Not handled. That feature uses the ArcGIS densified shape. |

**Self-check.** When a layer is added, up to 50 curved features are decoded and compared with their densified ST shape (Hausdorff distance ≤ 2% of the feature size). Curves are only used if all decoded features match. The result is stored in the layer source (`stcurve=`), and the log shows `true curves (SE_ANNO_CAD_DATA) ON (verified n/n …)` or the reason it's off.

**Cost.** The curve column is fetched together with the geometry. It's NULL (nearly free) for every feature without curves.

### Your control (dialog → *Arcs (true curves)*)
| Mode | What you see | Layer type |
|---|---|---|
| **Off** (default) | ArcGIS densified shape (as before) | MultiPolygon(Z) / MultiLineString(Z) |
| **True curves – QGIS segments them** | Real arcs. QGIS breaks them only for display, using **Settings → Options → Rendering → Curve segmentation**: *tolerance type* = maximum angle **or** maximum difference (distance from the arc), plus the *tolerance* value. Identify and measure show true arcs; vertex editing tools see arc vertices. | MultiSurface(Z) / MultiCurve(Z) |
| **True curves segmented by the plugin with:** *max angle (°)* or *max distance from arc (m)* + value | Arcs broken by the plugin with your tolerance. Distance is in metres, converted to degrees for lat/lon data (1° ≈ 111,320 m). | MultiPolygon(Z) / MultiLineString(Z) |

Changing the mode means re-adding the layer, because the layer's geometry type depends on it.

**Tested in QGIS 4.2.3** with synthetic arcs: a polygon with a half-circle edge (center form), and a hole whose edge is an arc (interior-point form):
- The area of the true-curve polygon equals square + half disc exactly.
- The hole stays a hole.
- Segment counts follow the setting: 10° → 22 vertices, 1° → 184, 1 m → 16, 1 cm → 122.
- Curve layers render.

---

## 18. Profiled optimisation (QGIS 4.2.3)

Profiled with cProfile on 20,000 features inside QGIS 4.2.3. Each change was checked against the previous behaviour.

| Change | Effect |
|---|---|
| **Batch decoding with numpy.** Rows are consumed 1,000 at a time. The Esri `POINTS` blobs of a whole batch are decoded in one pass (integer parsing, signs, difference sums, grid scaling, ring-closure checks), and each feature then only gets its bytes sliced and handed to QGIS. | Local decoding **~2.5× faster** (≈9 µs vs 23 µs per feature). Tested byte-identical to the per-feature decoder on 24,000 random features: 2D/3D, all four Z layouts, points, lines, polygons, truncated rows. **0 differences.** |
| Rows the batch can't handle (multi-ring polygons, curve rows, truncated or mixed 2D/3D rows) | Fall through to the unchanged per-feature path |
| Raw WKB handed to QGIS directly (no `QByteArray` wrapper) | −0.8 µs per feature |
| Overview pixel cells written directly as WKB in the layer's type | **2.2× faster** per cell, identical geometry |
| numpy missing | Automatically falls back to per-feature decoding |

**Result:** the plugin plus QGIS costs about **9 µs per feature with geometry** and about 5.6 µs for attribute-only reads. Rows are handed over in whole batches (no per-row Python call); each binary geometry is one struct-packed header plus one slice of the coordinate buffer; batch-decoded geometries already have the layer type and skip the per-feature type check. An attempt to also build all binary geometries in a single numpy buffer turned out slower and was dropped. All test suites pass: 36/36 functional and 14/14 true-curve tests.


---

## 19. Loading polygons as lines or points

Add-layer dialog → **Load polygons as** (polygon tables only):

| Mode | Geometry per feature | Layer type |
|---|---|---|
| Polygons (default) | as stored | MultiPolygon(Z) |
| **Lines** | the polygon's boundary: every ring (outer rings and holes) becomes a line | MultiLineString(Z) (MultiCurve with true curves) |
| **Points** | the **centroid**. If the centroid falls outside the polygon (U/L shapes, multipart features), QGIS's *point on surface* is used, a point guaranteed to lie inside. Z is taken from the polygon. | Point(Z) |

- **Per layer, not global.** The mode is stored in the layer source (`stgeomas='boundary'` or `'point'`), so the same table can be added several times, e.g. as polygons and as labelled points.
- **Where it happens.** The conversion runs on your PC right after the geometry is decoded, so identify, selection and distance tools work on the lines or points. Spatial filtering still uses the polygon's envelope, which always contains its boundary and its interior point. In fast overview mode, merged pixel cells are drawn as points or short lines to match the layer type.
- **Attributes and feature ids** are unchanged.


---

## 20. Fastest settings (the defaults)

> **Where the options are:** the add-layer dialog opens showing only the connection, *List tables*, the table list and *Add selected layer(s)*. Every option in this document, and the Fastest settings, Tune, Diagnostics and Benchmark buttons, are under **Advanced settings** (collapsed by default; click to expand).

All defaults are chosen for the fastest loading and drawing. The dialog's **Fastest settings** button applies them again at any time; settings saved earlier in your QGIS profile otherwise take priority over defaults.

| Option | Fastest / default | Reason |
|---|---|---|
| Driver | Auto (python-oracledb) | Bulk fetch with inline LOBs |
| Geometry decoding | Local decoder where verified | No per-row conversion on the server |
| Parallel sessions per layer | **4** | Large reads split into view strips (index plan) or OBJECTID ranges (scan). Use *Tune on current map view* to find the best value for your database. |
| Use plain scan above | **Auto**: 10 % of the table, at least 50,000 rows | Measured break-even: the ST index costs ~70 µs per row in view, a full envelope scan ~7 µs per table row. A fixed number can't fit both small and large tables. |
| Fast overview | **On**, 1 px | No geometry fetched for sub-pixel features (map drawing only) |
| Arcs (true curves) | Off | Curves cost extra parsing and drawing |
| Force 2D | **Ticked** | Smaller geometries; the decoder needs no Z verification |
| Versioned view (_EVW) | Off | The view is slower than the base table |
| Compute exact data extent | On | Needed for the index/scan choice and the overview estimate |
| Load polygons as | Polygons | No conversion |
| Timing log | Off | |
| Connection pool | up to 64 sessions | Room for several layers drawing in parallel |

> **Note for the DBA:** with these defaults, drawing one large layer can use up to 4 sessions. If that's too many for a shared database, lower *Parallel sessions* (1 = a single session).


---

## 21. Tuning on your own database

The ideal settings depend on your Oracle server: CPU, load, network and index behaviour. A plugin can't know these in advance, so the dialog can **measure** them.

**Tune on current map view:** select the table you're looking at and press the button. The plugin renders the current map view with each combination of

- parallel sessions **1, 4, 8**, and
- fast overview **off / on**

through QGIS's real renderer, after a warm-up render and taking the best of two renders per combination. It then **applies the fastest combination** and shows the timings. Run it on a typical view, or on the view that matters most to you (e.g. a dense, zoomed-out one).

**Why the strategy changed in 1.3.** Earlier versions split parallel index reads with `MOD(OBJECTID, N)`, which repeated the full ST index search in every session. The fast overview also used two queries, which ran the spatial filter twice. Both multiplied work on the database server. In 1.3:
- parallel index reads are split into **view strips**, so the index work is divided between sessions;
- the overview is a **single query**.


---

## 22. Domain and subtype descriptions

Add-layer dialog → **Domain / subtype descriptions** (ticked by default). The plugin reads the geodatabase's standard Esri XML **once when the layer loads** from `SDE.GDB_ITEMS`:
- the **feature class item**: each field's default domain, the subtype field, and per subtype the domain of each field;
- the **coded-value domain items**: code → description.

### A: description fields (raw values kept)
Every field that has a domain gets a text field **`<FIELD>_DESC`**, and the subtype field gets **`<SUBTYPE>_DESC`** with the subtype names. For each feature:

```
(feature's subtype code, field)  →  domain  →  code  →  description
```

So the **same code can mean different things in different subtypes**, and each feature gets the description of *its own* subtype's domain. If a subtype defines no domain for a field, the field's default domain applies, as in ArcGIS. NULL codes give NULL descriptions.

The description fields behave like any other field: attribute table, identify, labels, styles (e.g. categorized by `LANDUSE_DESC`), filters and unique values. They're computed on your PC with a dictionary lookup per value; when only a description field is requested, the plugin fetches the raw and subtype columns it needs automatically.

### B: Value Map widgets (descriptions instead of codes)
Applied automatically to **every** ST Geometry layer with descriptions that enters a project: added from the dialog, the Browser, a script, or a reloaded project. Widgets you've changed yourself are kept.

Raw fields whose domain is **the same for every subtype**, and the subtype field itself, also get a QGIS **Value Map** widget, so the attribute table and forms show descriptions instead of codes. Fields whose domain **differs between subtypes** don't get one: QGIS gives a field only one value list, so a value map there would be wrong for some features. Their `_DESC` field is exact.

The raw values are never changed. If `SDE.GDB_ITEMS` isn't readable for your user, the layer loads without descriptions and the log says why.
