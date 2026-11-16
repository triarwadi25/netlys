# -*- coding: utf-8 -*-
def classFactory(iface):
    from .netlys_plugin import NetlysPlugin
    return NetlysPlugin(iface)
