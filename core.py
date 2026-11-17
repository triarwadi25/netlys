# -*- coding: utf-8 -*-
import os
import re

from qgis.PyQt.QtGui import QIcon

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterEnum,
    QgsPointXY,
    QgsCoordinateTransform,
)
from qgis.analysis import (
    QgsVectorLayerDirector,
    QgsNetworkStrategy,
    QgsNetworkDistanceStrategy,
    QgsGraphBuilder,
)

DIST_IDX = 0   # cost property index: distance (meters)
TIME_IDX = 1   # cost property index: travel time (minutes)

GROUP = 'NETLYS'
GROUP_ID = 'netlys'
GROUP_AREA = 'Service Area'
GROUP_ROUTING = 'Routing and Accessibility'
GROUP_METRICS = 'Network Metrics'
ICON_DIR = os.path.join(os.path.dirname(__file__), 'icons')


def make_icon(filename):
    return QIcon(os.path.join(ICON_DIR, filename))


def slug(text):
    return ''.join(c for c in text.lower() if c.isalnum())


class TimeStrategy(QgsNetworkStrategy):
    """Cost = travel time in MINUTES = distance (m) / speed (km/h)."""

    def __init__(self, speed_idx, default_speed):
        super().__init__()
        self.speed_idx = speed_idx
        self.default_speed = float(default_speed)

    def cost(self, distance, feature):
        speed = self.default_speed
        if self.speed_idx >= 0:
            v = feature[self.speed_idx]
            if isinstance(v, (int, float)) and v > 0:
                speed = float(v)
        return distance / (speed * 1000.0 / 60.0)

    def requiredAttributes(self):
        return [self.speed_idx] if self.speed_idx >= 0 else []


def parse_classes(text):
    vals = []
    for tok in re.split(r'[,;\s]+', (text or '').strip()):
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            raise QgsProcessingException('Invalid class value: "%s"' % tok)
        if v <= 0:
            raise QgsProcessingException('Class values must be greater than 0.')
        vals.append(v)
    vals = sorted(set(vals))
    if not vals:
        raise QgsProcessingException('Enter at least one class value.')
    return vals


def read_points(source, id_field, dest_crs, context):
    """Read points from a feature source -> (list of QgsPointXY in dest CRS, list of id strings)."""
    transform = QgsCoordinateTransform(source.sourceCrs(), dest_crs, context.transformContext())
    points, ids = [], []
    for f in source.getFeatures():
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        pt = g.asMultiPoint()[0] if g.isMultipart() else g.asPoint()
        points.append(transform.transform(QgsPointXY(pt)))
        ids.append(str(f[id_field]) if id_field else str(f.id()))
    if not points:
        raise QgsProcessingException('No valid points in "%s".' % source.sourceName())
    return points, ids


def trace_path(graph, tree, start, end):
    """Trace the shortest path from the Dijkstra tree.
    Returns (list of QgsPointXY, distance_m, time_min) or None if unreachable."""
    pts = [graph.vertex(end).point()]
    dist = 0.0
    time = 0.0
    v = end
    guard = 0
    while v != start:
        eid = tree[v]
        if eid == -1:
            return None
        e = graph.edge(eid)
        dist += float(e.cost(DIST_IDX))
        time += float(e.cost(TIME_IDX))
        v = e.fromVertex()
        pts.append(graph.vertex(v).point())
        guard += 1
        if guard > 10000000:
            return None
    pts.reverse()
    return pts, dist, time


class NetworkContext:
    """Keeps objects alive (avoids garbage collection) during the analysis."""
    pass


class NetlysNetworkAlgorithm(QgsProcessingAlgorithm):
    NETWORK = 'NETWORK'
    SPEED_FIELD = 'SPEED_FIELD'
    DEFAULT_SPEED = 'DEFAULT_SPEED'
    DIR_FIELD = 'DIR_FIELD'
    DIR_FWD = 'DIR_FWD'
    DIR_BWD = 'DIR_BWD'
    DIR_BOTH = 'DIR_BOTH'
    DIR_DEFAULT = 'DIR_DEFAULT'
    TOLERANCE = 'TOLERANCE'

    GROUP_NAME = GROUP
    ICON_FILE = 'logo.png'
    TAGS = []

    def group(self):
        return self.GROUP_NAME

    def groupId(self):
        return slug(self.GROUP_NAME)

    def icon(self):
        return make_icon(self.ICON_FILE)

    def tags(self):
        return self.TAGS

    def add_network_parameter(self):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.NETWORK, 'Road network layer (line)', [QgsProcessing.TypeVectorLine]))

    def add_speed_parameters(self):
        self.addParameter(QgsProcessingParameterField(
            self.SPEED_FIELD, 'Speed field in km/h (for travel time, optional)',
            parentLayerParameterName=self.NETWORK, type=QgsProcessingParameterField.Numeric,
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.DEFAULT_SPEED, 'Default speed in km/h (used if field is empty/NULL)',
            QgsProcessingParameterNumber.Double, defaultValue=40.0, minValue=0.1))

    def add_advanced_parameters(self):
        adv = [
            QgsProcessingParameterField(
                self.DIR_FIELD, 'Direction field (optional)',
                parentLayerParameterName=self.NETWORK, optional=True),
            QgsProcessingParameterString(
                self.DIR_FWD, 'Value for one-way (digitizing direction)', optional=True),
            QgsProcessingParameterString(
                self.DIR_BWD, 'Value for one-way (against digitizing direction)', optional=True),
            QgsProcessingParameterString(
                self.DIR_BOTH, 'Value for two-way', optional=True),
            QgsProcessingParameterEnum(
                self.DIR_DEFAULT, 'Default direction',
                options=['Two-way', 'Forward (digitizing direction)', 'Backward (against digitizing)'],
                defaultValue=0),
            QgsProcessingParameterNumber(
                self.TOLERANCE, 'Topology tolerance (map units)',
                QgsProcessingParameterNumber.Double, defaultValue=0.0, minValue=0.0),
        ]
        for p in adv:
            p.setFlags(p.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
            self.addParameter(p)

    def prepare_graph(self, parameters, context, feedback, point_lists):
        """Build a graph with two cost properties (0 = distance in m, 1 = time in minutes).
        point_lists: list of lists of QgsPointXY (network CRS). Points are tied to the network."""
        network = self.parameterAsVectorLayer(parameters, self.NETWORK, context)
        if network is None:
            raise QgsProcessingException('Invalid network layer.')
        if network.crs().isGeographic():
            raise QgsProcessingException(
                'The network layer uses a geographic CRS (degrees). Reproject it to a projected CRS '
                'with meter units first (e.g. UTM EPSG:326xx for Indonesia).')

        tol = self.parameterAsDouble(parameters, self.TOLERANCE, context)
        dir_name = self.parameterAsString(parameters, self.DIR_FIELD, context)
        dir_idx = network.fields().lookupField(dir_name) if dir_name else -1
        dir_map = [QgsVectorLayerDirector.DirectionBoth,
                   QgsVectorLayerDirector.DirectionForward,
                   QgsVectorLayerDirector.DirectionBackward]
        default_dir = dir_map[self.parameterAsEnum(parameters, self.DIR_DEFAULT, context)]

        director = QgsVectorLayerDirector(
            network, dir_idx,
            self.parameterAsString(parameters, self.DIR_FWD, context) or '',
            self.parameterAsString(parameters, self.DIR_BWD, context) or '',
            self.parameterAsString(parameters, self.DIR_BOTH, context) or '',
            default_dir)
        director.addStrategy(QgsNetworkDistanceStrategy())          # index 0
        speed_name = self.parameterAsString(parameters, self.SPEED_FIELD, context)
        speed_idx = network.fields().lookupField(speed_name) if speed_name else -1
        default_speed = self.parameterAsDouble(parameters, self.DEFAULT_SPEED, context)
        director.addStrategy(TimeStrategy(speed_idx, default_speed))  # index 1

        flat = [p for pts in point_lists for p in pts]
        feedback.pushInfo('Building network graph...')
        builder = QgsGraphBuilder(network.crs(), True, tol, context.ellipsoid())
        try:
            tied = director.makeGraph(builder, flat, feedback)
        except TypeError:
            tied = director.makeGraph(builder, flat)
        graph = builder.graph()

        split, i = [], 0
        for pts in point_lists:
            split.append(list(tied[i:i + len(pts)]))
            i += len(pts)

        ctx = NetworkContext()
        ctx.network = network
        ctx.director = director
        ctx.builder = builder
        ctx.graph = graph
        ctx.tied = split
        feedback.pushInfo('Graph: %d vertices, %d edges.' % (graph.vertexCount(), graph.edgeCount()))
        return ctx
