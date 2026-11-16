# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    Qgis,
)
from qgis.analysis import QgsGraphAnalyzer

from .core import NetlysNetworkAlgorithm, parse_classes, read_points, DIST_IDX, TIME_IDX, GROUP_AREA
from .metrics import reachable_segments, tree_segments


def _cap_join_styles():
    """End-cap and join style constants (compatible with old and new QGIS 3.x)."""
    try:
        return {'square': Qgis.EndCapStyle.Square, 'flat': Qgis.EndCapStyle.Flat}, Qgis.JoinStyle.Miter
    except AttributeError:
        return ({'square': getattr(QgsGeometry, 'CapSquare', 3), 'flat': getattr(QgsGeometry, 'CapFlat', 2)},
                getattr(QgsGeometry, 'JoinStyleMiter', 2))


def make_buffer(lines, dist, style):
    """Buffer reachable line pieces. style: 'square' (angular, ArcGIS-like), 'flat', or 'round'."""
    geom = QgsGeometry.collectGeometry(lines)
    try:
        merged = geom.mergeLines()          # join connected pieces so corners get clean mitre joins
        if merged is not None and not merged.isNull() and not merged.isEmpty():
            geom = merged

    if style == 'round':
        return geom.buffer(dist, 8)
    caps, join = _cap_join_styles()
    return geom.buffer(dist, 8, caps[style], join, 5.0)


def _fill_holes(geom):
    if geom.isMultipart():
        parts = [QgsGeometry.fromPolygonXY([p[0]]) for p in geom.asMultiPolygon()]
        return QgsGeometry.unaryUnion(parts)
    poly = geom.asPolygon()
    return QgsGeometry.fromPolygonXY([poly[0]])


class ServiceAreaAlgorithm(NetlysNetworkAlgorithm):
    STARTS = 'STARTS'
    STARTID = 'STARTID'
    MODE = 'MODE'
    CLASSES = 'CLASSES'
    METHOD = 'METHOD'
    BUFFER = 'BUFFER'
    CORNERS = 'CORNERS'
    FILL = 'FILL'
    LINES = 'LINES'
    OUTPUT = 'OUTPUT'

    METHODS = ['overlap', 'dissolve', 'split']
    CORNER_STYLES = ['square', 'flat', 'round']

    GROUP_NAME = GROUP_AREA
    ICON_FILE = 'area.png'
    TAGS = ['service area', 'network area', 'isochrone', 'catchment', 'accessibility', 'buffer', 'network']

    def name(self):
        return 'networkareamulticlass'

    def displayName(self):
        return 'Network Area (Distance/Time, Multi-Class)'

    def createInstance(self):
        return ServiceAreaAlgorithm()

    def shortHelpString(self):
        return (
            "<p>Creates <b>service area polygons</b> from a road network, with several classes in one layer.</p>"
            "<p><b>Analysis type:</b> Distance (classes in meters) or Time (classes in minutes). "
            "Time uses the speed field (km/h) or the default speed.</p>"
            "<p><b>Class polygon method:</b></p><ul>"
            "<li><b>Overlap</b> - every start point gets cumulative polygons per class "
            "(0-500, 0-1000, ...), overlapping each other.</li>"
            "<li><b>Dissolve</b> - polygons of all start points are merged per class "
            "(cumulative, one polygon per class).</li>"
            "<li><b>Split</b> - all start points are dissolved, then split into non-overlapping bands "
            "(0-500, 500-1000, ...). Each location gets the smallest class reachable from any start point.</li>"
            "</ul>"
            "<p>Edges that cross a cutoff are cut exactly at the cutoff (interpolation). "
            "The polygon is a buffer (trim) around the reachable edges; <i>Polygon corner style</i> controls the shape: "
            "square ends + mitre corners give angular, ArcGIS-like polygons, flat ends stop exactly at the cutoff, "
            "round gives smooth edges. The defaults "
            "(Split + 100 m buffer + fill holes) resemble the ArcGIS Pro Service Area "
            "(detailed polygons, merged by break value, no overlap). "
            "The optional line output holds the reachable edges split at class cutoffs, "
            "with cumulative start/end cost.</p>"
            "<p><i>The network layer must use a projected CRS in meters. "
            "Use a dot as the decimal separator in classes.</i></p>"
        )

    def initAlgorithm(self, config=None):
        self.add_network_parameter()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.STARTS, 'Start points / facilities (point)', [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.STARTID, 'Start ID field (optional, default: feature ID)',
            parentLayerParameterName=self.STARTS, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, 'Analysis type', options=['Distance (meters)', 'Time (minutes)'], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.CLASSES, 'Classes / cutoffs (comma separated)', defaultValue='500,1000,2000,3000'))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, 'Class polygon method',
            options=['Overlap', 'Dissolve', 'Split'], defaultValue=2))
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER, 'Buffer distance around roads (meters)',
            QgsProcessingParameterNumber.Double, defaultValue=100.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterEnum(
            self.CORNERS, 'Polygon corner style',
            options=['Square ends + mitre corners (angular, ArcGIS-like)',
                     'Flat ends + mitre corners (exact at cutoff)',
                     'Round (smooth)'], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FILL, 'Fill holes in polygons', defaultValue=True))
        self.add_speed_parameters()
        self.add_advanced_parameters()
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'Network Area', QgsProcessing.TypeVectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.LINES, 'Reachable road lines per class (optional)', QgsProcessing.TypeVectorLine,
            optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        starts = self.parameterAsSource(parameters, self.STARTS, context)
        if starts is None:
            raise QgsProcessingException('Invalid start points.')
        mode = self.parameterAsEnum(parameters, self.MODE, context)
        method = self.METHODS[self.parameterAsEnum(parameters, self.METHOD, context)]
        classes = parse_classes(self.parameterAsString(parameters, self.CLASSES, context))
        buf = self.parameterAsDouble(parameters, self.BUFFER, context)
        fill = self.parameterAsBool(parameters, self.FILL, context)
        corner_style = self.CORNER_STYLES[self.parameterAsEnum(parameters, self.CORNERS, context)]
        id_name = self.parameterAsString(parameters, self.STARTID, context)

        net_layer = self.parameterAsVectorLayer(parameters, self.NETWORK, context)
        if net_layer is None:
            raise QgsProcessingException('Invalid network layer.')
        points, ids = read_points(starts, id_name, net_layer.crs(), context)

        ctx = self.prepare_graph(parameters, context, feedback, [points])
        graph, network = ctx.graph, ctx.network
        tied = ctx.tied[0]
        crit = DIST_IDX if mode == 0 else TIME_IDX
        unit = 'm' if mode == 0 else 'min'
        mode_name = 'distance' if mode == 0 else 'time'

        vpts = [(graph.vertex(v).point().x(), graph.vertex(v).point().y())
                for v in range(graph.vertexCount())]
        edges = []
        for e in range(graph.edgeCount()):
            ed = graph.edge(e)
            edges.append((ed.fromVertex(), ed.toVertex(), float(ed.cost(crit))))

        fields = QgsFields()
        fields.append(QgsField('start_id', QVariant.String, len=50))
        fields.append(QgsField('class', QVariant.Int))
        fields.append(QgsField('mode', QVariant.String, len=10))
        fields.append(QgsField('method', QVariant.String, len=10))
        fields.append(QgsField('from_val', QVariant.Double))
        fields.append(QgsField('to_val', QVariant.Double))
        fields.append(QgsField('unit', QVariant.String, len=10))
        fields.append(QgsField('label', QVariant.String, len=50))
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.MultiPolygon, network.crs())
        if sink is None:
            raise QgsProcessingException('Failed to create the output layer.')

        lfields = QgsFields()
        lfields.append(QgsField('start_id', QVariant.String, len=50))
        lfields.append(QgsField('class', QVariant.Int))
        lfields.append(QgsField('cutoff', QVariant.Double))
        lfields.append(QgsField('from_cost', QVariant.Double))
        lfields.append(QgsField('to_cost', QVariant.Double))
        lfields.append(QgsField('unit', QVariant.String, len=10))
        lsink, ldest = None, None
        if parameters.get(self.LINES) not in (None, ''):
            lsink, ldest = self.parameterAsSink(
                parameters, self.LINES, context, lfields, QgsWkbTypes.LineString, network.crs())

        def write_rows(rows):
            # rows: (sid, k, L, lo, geom); largest class written first so small classes draw on top
            for (sid, k, L, lo, g) in sorted(rows, key=lambda r: -r[1]):
                if g is None or g.isEmpty():
                    continue
                if not g.isMultipart():
                    g.convertToMultiType()
                feat = QgsFeature(fields)
                feat.setGeometry(g)
                feat.setAttributes([sid, k, mode_name, method, lo, L, unit,
                                    '%g-%g %s' % (lo, L, unit)])
                sink.addFeature(feat, QgsFeatureSink.FastInsert)

        per_class = {L: [] for L in classes}   # for dissolve / split
        total = len(points)
        for n, (pt, sid) in enumerate(zip(tied, ids)):
            if feedback.isCanceled():
                break
            start = graph.findVertex(pt)
            if start == -1:
                feedback.reportError('Start point "%s" could not be tied to the network, skipped.' % sid)
                continue
            tree, costs = QgsGraphAnalyzer.dijkstra(graph, start, crit)
            segs = reachable_segments(edges, vpts, costs, tree, start, classes)

            if lsink is not None:
                for (k, L, a, b, q1, q2) in tree_segments(edges, vpts, costs, tree, classes):
                    lf = QgsFeature(lfields)
                    lf.setGeometry(QgsGeometry.fromPolylineXY(
                        [QgsPointXY(q1[0], q1[1]), QgsPointXY(q2[0], q2[1])]))
                    lf.setAttributes([sid, k, L, a, b, unit])
                    lsink.addFeature(lf, QgsFeatureSink.FastInsert)

            polys = {}
            for L in classes:
                if not segs[L]:
                    continue
                lines = [QgsGeometry.fromPolylineXY([QgsPointXY(a[0], a[1]), QgsPointXY(b[0], b[1])])
                         for (a, b) in segs[L]]
                g = make_buffer(lines, max(buf, 0.01), corner_style)
                if g is None or g.isEmpty():
                    continue
                if fill:
                    g = _fill_holes(g)
                polys[L] = g

            if method == 'overlap':
                write_rows([(sid, k, L, 0.0, polys[L])
                            for k, L in enumerate(classes, start=1) if L in polys])
            else:
                for L, g in polys.items():
                    per_class[L].append(g)
            feedback.setProgress(int(100.0 * (n + 1) / total))

        if method in ('dissolve', 'split'):
            feedback.pushInfo('Dissolving polygons of all start points per class...')
            cum = {}
            for L in classes:
                if per_class[L]:
                    cum[L] = QgsGeometry.unaryUnion(per_class[L])
            rows = []
            prev_geom, prev_val = None, 0.0
            for k, L in enumerate(classes, start=1):
                if L not in cum:
                    continue
                g = cum[L]
                if method == 'dissolve':
                    rows.append(('ALL', k, L, 0.0, g))
                else:
                    band = g.difference(prev_geom) if prev_geom is not None else g
                    rows.append(('ALL', k, L, prev_val, band))
                prev_geom, prev_val = g, L
            write_rows(rows)

        result = {self.OUTPUT: dest_id}
        if ldest:
            result[self.LINES] = ldest
        return result
