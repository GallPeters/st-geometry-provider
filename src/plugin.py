# -*- coding: utf-8 -*-
"""Plugin GUI glue: menu / toolbar actions."""

import os

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from .provider import register_provider
from . import compat as C

MENU = "ST Geometry Provider"


class StGeometryPlugin(object):
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dlg = None

    def initGui(self):  # noqa: N802
        register_provider()
        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QgsApplication.getThemeIcon("/mActionAddOracleLayer.svg")
        self.action = QAction(icon, "Add ST_Geometry Layer(s)...", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        try:
            self.iface.addPluginToDatabaseMenu(MENU, self.action)
        except Exception:
            self.iface.addPluginToMenu(MENU, self.action)
        try:
            self.iface.addLayerMenu().addAction(self.action)
        except Exception:
            pass

    def unload(self):
        if self.action is None:
            return
        try:
            self.iface.removePluginDatabaseMenu(MENU, self.action)
        except Exception:
            pass
        try:
            self.iface.removePluginMenu(MENU, self.action)
        except Exception:
            pass
        try:
            self.iface.addLayerMenu().removeAction(self.action)
        except Exception:
            pass
        self.iface.removeToolBarIcon(self.action)
        self.action = None
        # Note: QGIS has no API to unregister a data provider; it stays registered until restart.

    def run(self):
        try:
            from .dialog import AddLayerDialog
            if self.dlg is None:
                self.dlg = AddLayerDialog(self.iface, self.iface.mainWindow())
            self.dlg.show()
            self.dlg.raise_()
            self.dlg.activateWindow()
        except Exception as e:
            import traceback
            C.log("Cannot open dialog: %s\n%s" % (e, traceback.format_exc()), "critical")
            raise
