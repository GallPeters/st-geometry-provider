<div align="center">

<img width="80" alt="icon" src="icon.png" />

# ST Geometry Provider

**Open Esri ST_Geometry tables from Oracle directly in QGIS. Fast and read-only.**

[![License](https://img.shields.io/badge/License-GPL--2.0-98b023)](LICENSE)
[![QGIS](https://img.shields.io/badge/QGIS-3.34%20–%204.x-589632)](https://qgis.org)

</div>

## Features
- ST_Geometry tables open as normal QGIS layers: styling, labels, attribute table, identify, filters.
- No ArcGIS, no database changes, no conversion to SDO_GEOMETRY.
- Fast by default: local geometry decoding, bulk fetching, parallel sessions, fast overview.
- Load polygons as **lines** (boundaries) or **points** (centroid, always inside).
- Optional fast overview for millions of features, and true curves (arcs).

## Install
1. Download [`releases/st_geometry_provider-1.2.zip`](releases/st_geometry_provider-1.2.zip).
2. QGIS → *Plugins → Manage and Install Plugins → Install from ZIP*.
3. Click **Add ST_Geometry Layer(s)…**, pick a connection, list tables, add.

**Connections:** use a QGIS Oracle connection, or copy `connections.example.json` to `connections.json` and fill it in.

## Docs
- [How it works](docs/HOW_IT_WORKS.md)
- [Changelog](docs/CHANGELOG.md)
- [Tests](tests/README.md)

---
<sub>Esri, ArcGIS and ST_Geometry are trademarks of Esri. This project is not affiliated with Esri.</sub>
