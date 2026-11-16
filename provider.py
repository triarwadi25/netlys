# -*- coding: utf-8 -*-
from qgis.core import QgsProcessingProvider
from .core import make_icon
from .alg_service_area import ServiceAreaAlgorithm
from .alg_paths import PathAlgorithm, NearestFacilityAlgorithm
from .alg_connectivity import ConnectivityAlgorithm


class NetlysProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(ServiceAreaAlgorithm())
        self.addAlgorithm(PathAlgorithm())
        self.addAlgorithm(NearestFacilityAlgorithm())
        self.addAlgorithm(ConnectivityAlgorithm())

    def id(self):
        return 'netlys'

    def name(self):
        return 'NETLYS'

    def longName(self):
        return 'NETLYS - Road Network Analysis'

    def icon(self):
        return make_icon('logo.png')
