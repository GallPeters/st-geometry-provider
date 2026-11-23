<br>

<div align="center">

<img width="80" alt="icon" src="icon.png" />

# ST Geometry Provider

[![📜 License](https://img.shields.io/badge/License-GPL--2.0-98b023?style=for-the-badge)](LICENSE)
[![QGIS](https://img.shields.io/badge/QGIS-3.34%20–%204.x-589632?style=for-the-badge)](https://qgis.org)

**Read Esri ST_Geometry tables in Oracle directly as fast, read-only QGIS layers.**

_No ArcGIS, no database changes, no conversion to SDO_GEOMETRY._

</div>

<br>

## What it does
ST Geometry Provider adds a QGIS data provider for Oracle tables (ArcSDE / enterprise geodatabases) that store geometry in Esri's `SDE.ST_GEOMETRY` type. The tables open as ordinary QGIS layers. Rendering, the attribute table, identify, selection, filters, expressions, labels and styles all work.

- **Fast:**
  - geometries are decoded locally by a numpy batch decoder, verified against Esri's own `st_asbinary`;
  - rows are fetched in bulk with python-oracledb, with optional parallel sessions;
  - the ST spatial index or a plain scan is chosen per view.
- **Fast overview (optional):** features smaller than a pixel are merged in Oracle, so zoomed-out maps of millions of features stay responsive.
- **True curves (optional):** circular arcs, with segmentation by maximum angle or distance.
- **Standalone:** connect with an existing QGIS Oracle connection or with a `connections.json` file. Passwords can be left out of it.

Full technical documentation: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) · Changes: [docs/CHANGELOG.md](docs/CHANGELOG.md)

## Install
1. Download the latest zip from [releases/](releases/) (e.g. `STGeometryProvider-1.0.zip`).
2. QGIS → *Plugins → Manage and Install Plugins → Install from ZIP*.
3. Toolbar **Add ST_Geometry Layer(s)…** → choose a connection → *List tables* → *Add selected layer(s)*.

Requirements:
- QGIS 3.34 – 4.x;
- python-oracledb (recommended) *or* QGIS's Oracle (Qt OCI) support;
- numpy (bundled with QGIS) for batch decoding.

### Connections
Use a QGIS Oracle connection (*Browser → Oracle*), or copy `connections.example.json` to `connections.json` and fill it in. The file is looked for in this order:
1. the path in `ST_GEOMETRY_CONFIG`;
2. the plugin folder;
3. `<QGIS profile>/st_geometry_connections.json`.

To ship a ready-to-use zip inside your organisation: `python tools/build_release.py path/to/connections.json`. **Never commit a real `connections.json`.**

## Repository layout
| Path | Content |
|---|---|
| `__init__.py`, `metadata.txt`, `icon.*`, `LICENSE` | Plugin entry point and metadata |
| `src/` | Plugin sources: provider, decoders, Oracle access, dialog |
| `docs/` | Technical documentation and changelog |
| `tests/` | Headless tests inside QGIS against a simulated Oracle ([tests/README.md](tests/README.md)) |
| `tools/` | `build_release.py` (release zip), `st_blob_samples.py` (format samples), `gpkg_display_fields.py` (stand-alone GeoPackage tool) |
| `releases/` | Installable plugin zips |

## Notes
- **Read-only:** the plugin never writes to the database.
- **Reverse-engineered format:** the Esri `POINTS` format was worked out from synthetic samples, and true-curve parsing follows GDAL's implementation of Esri's extended shape buffer (MIT). Both decoders verify themselves against Esri's functions before they're used.
- Esri, ArcGIS, ArcSDE and ST_Geometry are trademarks of Esri. This project is not affiliated with or endorsed by Esri.

## License
[GNU General Public License v2.0](LICENSE) (or later).
