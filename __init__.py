"""ST Geometry Provider plugin for QGIS"""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    """invoke plugin"""
    # Register the data provider as early as possible so that projects which
    # already contain stgeometry layers can load them.
    from .src.provider import register_provider
    register_provider()
    from .src.plugin import StGeometryPlugin
    return StGeometryPlugin(iface)
