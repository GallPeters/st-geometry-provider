# -*- coding: utf-8 -*-
"""
Option B: Value Map widgets (descriptions instead of codes in the attribute table and forms).

Applied to EVERY ST Geometry layer that enters a project with domain descriptions enabled - added
from the plugin dialog, the Browser, a script or a loaded project - for the subtype field and for raw
fields whose domain is the same for every subtype. Widgets the user configured are left alone.
"""

from qgis.core import QgsDataSourceUri, QgsEditorWidgetSetup, QgsProject

from . import compat as C

PROVIDER_KEY = "stgeometry"
_connected = False


def _label(layer):
    u = QgsDataSourceUri(layer.source())
    return ("%s.%s" % (u.schema().strip('"'), u.table().strip('"'))).upper()


def apply_value_maps(layer):
    """Set Value Map widgets from the layer's domain information. Returns the number of fields set."""
    try:
        if layer is None or layer.providerType() != PROVIDER_KEY or not layer.isValid():
            return 0
        from .provider import DOMAIN_INFO
        info = DOMAIN_INFO.get(_label(layer)) or []
        fields = layer.fields()
        n = 0
        for d in info:
            if not d.valuemap or d.src >= fields.count():
                continue
            idx = d.src                                    # raw column index == field index
            current = layer.editorWidgetSetup(idx).type()
            if current not in ("", "TextEdit", "ValueMap"):
                continue                                   # the user chose another widget: keep it
            layer.setEditorWidgetSetup(idx, QgsEditorWidgetSetup(
                "ValueMap", {"map": [{str(desc): code} for desc, code in d.valuemap]}))
            n += 1
        if n:
            C.log("%s: value maps (descriptions) on %d field(s)" % (layer.name(), n))
        return n
    except Exception as e:
        C.log("value maps not applied to %s: %s" % (getattr(layer, "name", lambda: "?")(), e), "warning")
        return 0


def _on_layers_added(layers):
    for lyr in layers:
        apply_value_maps(lyr)


def connect_project():
    global _connected
    if not _connected:
        QgsProject.instance().layersAdded.connect(_on_layers_added)
        _connected = True


def disconnect_project():
    global _connected
    if _connected:
        try:
            QgsProject.instance().layersAdded.disconnect(_on_layers_added)
        except Exception:
            pass
        _connected = False
