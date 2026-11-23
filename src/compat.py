# -*- coding: utf-8 -*-
"""Small compatibility layer: QGIS 3.34+ (PyQt5) and QGIS 4.x (PyQt6)."""

from qgis.core import Qgis, QgsFeatureRequest, QgsVectorDataProvider, QgsWkbTypes, QgsMessageLog

LOG_TAG = "ST_Geometry"


def resolve(*paths):
    """Return the first attribute path that exists, e.g. resolve((Qgis, 'WkbType.Unknown'), (QgsWkbTypes, 'Unknown'))."""
    for root, path in paths:
        obj = root
        ok = True
        for part in path.split("."):
            try:
                obj = getattr(obj, part)
            except AttributeError:
                ok = False
                break
        if ok:
            return obj
    return None


def qenum(cls, group, name):
    """Qt enum value, scoped (PyQt6) or flat (PyQt5)."""
    g = getattr(cls, group, None)
    if g is not None and hasattr(g, name):
        return getattr(g, name)
    return getattr(cls, name)


FILTER_NONE = resolve((Qgis, "FeatureRequestFilterType.NoFilter"), (QgsFeatureRequest, "FilterNone"))
FILTER_FID = resolve((Qgis, "FeatureRequestFilterType.Fid"), (QgsFeatureRequest, "FilterFid"))
FILTER_FIDS = resolve((Qgis, "FeatureRequestFilterType.Fids"), (QgsFeatureRequest, "FilterFids"))
FILTER_EXPR = resolve((Qgis, "FeatureRequestFilterType.Expression"), (QgsFeatureRequest, "FilterExpression"))

FLAG_NO_GEOMETRY = resolve((Qgis, "FeatureRequestFlag.NoGeometry"), (QgsFeatureRequest, "NoGeometry"))
FLAG_SUBSET = resolve((Qgis, "FeatureRequestFlag.SubsetOfAttributes"), (QgsFeatureRequest, "SubsetOfAttributes"))
FLAG_EXACT = resolve((Qgis, "FeatureRequestFlag.ExactIntersect"), (QgsFeatureRequest, "ExactIntersect"))

SPATIAL_DISTANCE = resolve((Qgis, "SpatialFilterType.DistanceWithin"),)
from qgis.core import QgsSimplifyMethod as _QSM
SIMPLIFY_NONE = resolve((_QSM, "MethodType.NoSimplification"), (_QSM, "NoSimplification"))
TRANSFORM_REVERSE = resolve((Qgis, "TransformDirection.Reverse"),)

CAP_SELECT_AT_ID = resolve((Qgis, "VectorProviderCapability.SelectAtId"), (QgsVectorDataProvider, "SelectAtId"))

GEOM_POINT = resolve((Qgis, "GeometryType.Point"), (QgsWkbTypes, "PointGeometry"))
GEOM_LINE = resolve((Qgis, "GeometryType.Line"), (QgsWkbTypes, "LineGeometry"))
GEOM_POLYGON = resolve((Qgis, "GeometryType.Polygon"), (QgsWkbTypes, "PolygonGeometry"))

WKB_UNKNOWN = resolve((Qgis, "WkbType.Unknown"), (QgsWkbTypes, "Unknown"))

_LEVELS = {
    "info": resolve((Qgis, "MessageLevel.Info"), (Qgis, "Info")),
    "warning": resolve((Qgis, "MessageLevel.Warning"), (Qgis, "Warning")),
    "critical": resolve((Qgis, "MessageLevel.Critical"), (Qgis, "Critical")),
}


def log(msg, level="info"):
    try:
        QgsMessageLog.logMessage(str(msg), LOG_TAG, _LEVELS.get(level, _LEVELS["info"]))
    except Exception:
        print("[%s] %s" % (LOG_TAG, msg))


def has_flag(flags, flag):
    if flag is None or flags is None:
        return False
    try:
        return bool(flags & flag)
    except TypeError:
        try:
            return (int(flags) & int(flag)) != 0
        except Exception:
            return False


def make_field_type(kind):
    """QgsField type for our column kinds: QMetaType on QGIS >= 3.38, QVariant before."""
    if Qgis.QGIS_VERSION_INT >= 33800:
        from qgis.PyQt.QtCore import QMetaType
        T = getattr(QMetaType, "Type", QMetaType)
        return {
            "int": T.Int, "int64": T.LongLong, "double": T.Double,
            "string": T.QString, "datetime": T.QDateTime, "date": T.QDate,
        }[kind]
    from qgis.PyQt.QtCore import QVariant
    return {
        "int": QVariant.Int, "int64": QVariant.LongLong, "double": QVariant.Double,
        "string": QVariant.String, "datetime": QVariant.DateTime, "date": QVariant.Date,
    }[kind]
