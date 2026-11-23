# -*- coding: utf-8 -*-
"""
ST_GEOMETRY raw format samples - SYNTHETIC coordinates only (nothing from your data).

Builds made-up geometries IN MEMORY with  SELECT sde.st_geometry('<WKT>', srid) FROM dual
(no table is created or written) and prints Esri's internal POINTS bytes for each one.
These let the plugin decode ST_GEOMETRY itself instead of calling sde.st_asbinary per row.

Run: QGIS Python Console > Show Editor > open this file > Run   (ST Geometry Provider must be installed)
Type back every line starting with [S..]  (S1-S6 are the most important).
"""
CONN_NAME = ""   # QGIS Oracle connection name; "" = first one
TABLE = ""       # "OWNER.TABLE" of one of your Z layers (only its SRID number is used); "" = auto

SAMPLES = [
    ("S1", "POINT (10 20)"),
    ("S2", "POINT (10.5 20.25)"),
    ("S3", "LINESTRING (10 20, 11 21, 13 20)"),
    ("S4", "POLYGON ((10 20, 11 20, 11 21, 10 21, 10 20))"),
    ("S5", "POLYGON ((10 20, 14 20, 14 24, 10 24, 10 20), (11 21, 11 22, 12 22, 12 21, 11 21))"),
    ("S6", "POINT Z (10 20 5)"),
    ("S7", "POLYGON Z ((10 20 1, 11 20 2, 11 21 3, 10 20 1))"),
    ("S8", "MULTIPOLYGON (((10 20, 11 20, 11 21, 10 20)), ((12 22, 13 22, 13 23, 12 22)))"),
    ("S9", "MULTILINESTRING ((10 20, 11 21), (12 22, 13 23))"),
    ("S10", "POINT (-10.25 -20.5)"),
]

from qgis.core import QgsDataSourceUri
from STGeometryProvider.src import backend as B      # installed plugin package


def hexgroups(h):
    h = str(h or "")
    return " ".join(h[i:i + 8] for i in range(0, len(h), 8))


u = QgsDataSourceUri()
_cfg = B.config_connection_names()
_qgis = B.oracle_connection_names()
if CONN_NAME and CONN_NAME in _cfg:
    u.setParam("stcfg", CONN_NAME)
elif CONN_NAME:
    u.setParam("stconn", CONN_NAME)
elif _cfg:
    u.setParam("stcfg", _cfg[0])           # connections.json first
elif _qgis:
    u.setParam("stconn", _qgis[0])
else:
    raise RuntimeError("No connection: fill connections.json or create a QGIS Oracle connection.")
conn = B.get_backend(u).acquire()
try:
    if TABLE:
        owner, table = [s.strip().strip('"').upper() for s in TABLE.split(".", 1)]
        r = conn.fetchone("SELECT srid FROM sde.st_geometry_columns WHERE owner = :o AND table_name = :t",
                          {"o": owner, "t": table})
    else:
        r = conn.fetchone("SELECT srid FROM (SELECT srid, COUNT(*) c FROM sde.st_geometry_columns "
                          "WHERE owner <> 'SDE' GROUP BY srid ORDER BY c DESC) WHERE ROWNUM = 1")
    srid = int(r[0])
    print("[S0] srid used", srid)
    for tag, wkt in SAMPLES:
        try:
            x = conn.fetchone(
                "SELECT x.g.entity, x.g.numpts, dbms_lob.getlength(x.g.points), "
                "RAWTOHEX(DBMS_LOB.SUBSTR(x.g.points, 1000, 1)) "
                "FROM (SELECT sde.st_geometry(:w, :s) g FROM dual) x", {"w": wkt, "s": srid})
            print("[%s] entity=%s numpts=%s len=%s HEX %s" % (tag, x[0], x[1], x[2], hexgroups(x[3])))
        except Exception as e:
            print("[%s] ERR %s" % (tag, str(e)[:150]))
finally:
    conn.close()
print("[S99] done")
