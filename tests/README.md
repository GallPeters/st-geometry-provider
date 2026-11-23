# Tests

These tests run **headless inside a real QGIS** (no GUI). Oracle is simulated: a fake database answers the plugin's SQL from synthetic data encoded in Esri's ST_GEOMETRY `POINTS` format. No Oracle server or Esri software is needed, and no real data is used.

| File | Covers |
|---|---|
| `test_integration.py` | Loading, the stored layer settings, local decoder vs Esri WKB, holes, multipart polygons, island-in-hole, attributes, attribute-only reads, expression push-down, identify, fid filters, subset string, real map rendering, fast overview mode (activation, pixel setting, exactness outside rendering), index vs scan plan, parallel sessions, Force 2D |
| `test_curves.py` | True curves from extended shape buffers: arc encodings (center and interior point), arcs on holes, the off / true-curve / segmentize modes, segmentation by angle and by distance, rendering |
| `test_batch_decoder.py` | numpy batch decoder vs per-row decoder on random data: 2D/3D, all Z layouts, points, lines, polygons, truncated rows. Must report 0 mismatches. |

## Running (Windows, OSGeo4W)
Use QGIS's Python launcher, e.g. `python-qgis.bat` from the OSGeo4W `bin` folder:

```
python-qgis.bat -W ignore tests\test_integration.py
python-qgis.bat -W ignore tests\test_curves.py
python-qgis.bat -W ignore tests\test_batch_decoder.py
```

Each script prints one PASS/FAIL line per check and a final `n/n passed` (or `TOTAL MISMATCHES: 0`).

The tests change the plugin's settings (`stgeometry/*` in QGIS settings) of the profile they run under, so use a test profile or reset the options in the plugin dialog afterwards.
