# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterPoint,
    QgsProcessingParameterFeatureSink,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
)
from qgis.analysis import QgsGraphAnalyzer

from .core import NetlysNetworkAlgorithm, read_points, trace_path, DIST_IDX, TIME_IDX, GROUP_ROUTING


def _crit_candidates(graph, tree, costs, start, vertices):
    """List of (criterion_cost, destination_index) for reachable destinations."""
    cand = []
    for j, v in enumerate(vertices):
        if v == -1:
            continue
        if v != start and tree[v] == -1:
            continue
        cand.append((costs[v], j))
    return cand


class PathAlgorithm(NetlysNetworkAlgorithm):
    ORIGINS = 'ORIGINS'
    ORIGIN_ID = 'ORIGIN_ID'
    ORIGIN_POINT = 'ORIGIN_POINT'
    DESTS = 'DESTS'
    DEST_ID = 'DEST_ID'
    DEST_POINT = 'DEST_POINT'
    PAIRING = 'PAIRING'
    CRITERION = 'CRITERION'
    SELECT = 'SELECT'
    OUTPUT = 'OUTPUT'

    GROUP_NAME = GROUP_ROUTING
    ICON_FILE = 'path.png'
    TAGS = ['shortest path', 'longest path', 'route', 'routing', 'network', 'origin destination']

    def name(self):
        return 'shortestlongestpath'

    def displayName(self):
        return 'Shortest & Longest Path'

    def createInstance(self):
        return PathAlgorithm()

    def shortHelpString(self):
        return (
            "<p>Computes routes on a road network from <b>origin points</b> to <b>destination points</b>. "
            "The output is a route line layer with distance (m) and travel time (minutes).</p>"
            "<p>Origins and destinations can come from a <b>layer</b> (use <i>Selected features only</i> if needed) "
            "and/or be <b>clicked on the map canvas</b>. If both are given, both are used.</p>"
            "<p><b>Origin-destination pairing:</b></p><ul>"
            "<li><b>All combinations</b> - every origin to every destination.</li>"
            "<li><b>Paired</b> - an origin is only connected to destinations with the same ID field value "
            "(without an ID field, feature IDs are matched).</li></ul>"
            "<p><b>Result to keep:</b></p><ul>"
            "<li><b>All pairs</b> - the shortest route for each pair.</li>"
            "<li><b>Shortest</b> - for each origin, only the destination with the lowest cost.</li>"
            "<li><b>Longest</b> - for each origin, the destination with the highest cost among the "
            "shortest routes available.</li></ul>"
            "<p>The compared cost can be Distance or Time. Points are tied to the nearest point on the road.</p>"
        )

    def initAlgorithm(self, config=None):
        self.add_network_parameter()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ORIGINS, 'Origin points - from layer (point)', [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.ORIGIN_ID, 'Origin ID field (optional)',
            parentLayerParameterName=self.ORIGINS, optional=True))
        self.addParameter(QgsProcessingParameterPoint(
            self.ORIGIN_POINT, 'Origin point - from map canvas (optional)', optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DESTS, 'Destination points - from layer (point)', [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.DEST_ID, 'Destination ID field (optional)',
            parentLayerParameterName=self.DESTS, optional=True))
        self.addParameter(QgsProcessingParameterPoint(
            self.DEST_POINT, 'Destination point - from map canvas (optional)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.PAIRING, 'Origin-destination pairing',
            options=['All combinations', 'Paired (same ID)'], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.CRITERION, 'Cost to optimize', options=['Distance', 'Time'], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.SELECT, 'Result to keep',
            options=['All origin-destination pairs', 'Shortest (nearest destination per origin)',
                     'Longest (farthest destination per origin)'], defaultValue=0))
        self.add_speed_parameters()
        self.add_advanced_parameters()
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'Routes', QgsProcessing.TypeVectorLine))

    def _get_points(self, parameters, context, crs, layer_p, id_p, point_p, label):
        pts, ids = [], []
        if parameters.get(layer_p) not in (None, ''):
            src = self.parameterAsSource(parameters, layer_p, context)
            if src is not None:
                p, i = read_points(src, self.parameterAsString(parameters, id_p, context), crs, context)
                pts += p
                ids += i
        if parameters.get(point_p) not in (None, ''):
            pt = self.parameterAsPoint(parameters, point_p, context, crs)
            pts.append(QgsPointXY(pt))
            ids.append('manual_point')
        if not pts:
            raise QgsProcessingException('%s: choose a layer or set a point on the map.' % label)
        return pts, ids

    def processAlgorithm(self, parameters, context, feedback):
        net_layer = self.parameterAsVectorLayer(parameters, self.NETWORK, context)
        if net_layer is None:
            raise QgsProcessingException('Invalid network layer.')
        crit = self.parameterAsEnum(parameters, self.CRITERION, context)   # 0 distance, 1 time
        select = self.parameterAsEnum(parameters, self.SELECT, context)
        pairing = self.parameterAsEnum(parameters, self.PAIRING, context)
        crit_idx = DIST_IDX if crit == 0 else TIME_IDX

        o_pts, o_ids = self._get_points(parameters, context, net_layer.crs(),
                                        self.ORIGINS, self.ORIGIN_ID, self.ORIGIN_POINT, 'Origin points')
        d_pts, d_ids = self._get_points(parameters, context, net_layer.crs(),
                                        self.DESTS, self.DEST_ID, self.DEST_POINT, 'Destination points')

        ctx = self.prepare_graph(parameters, context, feedback, [o_pts, d_pts])
        graph = ctx.graph
        o_tied, d_tied = ctx.tied
        d_vertices = [graph.findVertex(p) for p in d_tied]

        fields = QgsFields()
        fields.append(QgsField('origin_id', QVariant.String, len=50))
        fields.append(QgsField('dest_id', QVariant.String, len=50))
        fields.append(QgsField('rank', QVariant.Int))
        fields.append(QgsField('type', QVariant.String, len=15))
        fields.append(QgsField('criterion', QVariant.String, len=10))
        fields.append(QgsField('dist_m', QVariant.Double))
        fields.append(QgsField('time_min', QVariant.Double))
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.LineString, ctx.network.crs())
        if sink is None:
            raise QgsProcessingException('Failed to create the output layer.')

        tipe = ['pair', 'shortest', 'longest'][select]
        kriteria = 'distance' if crit == 0 else 'time'
        total = len(o_tied)
        n_out = 0
        for i, pt in enumerate(o_tied):
            if feedback.isCanceled():
                break
            start = graph.findVertex(pt)
            if start == -1:
                feedback.reportError('Origin "%s" could not be tied to the network, skipped.' % o_ids[i])
                continue
            tree, costs = QgsGraphAnalyzer.dijkstra(graph, start, crit_idx)
            cand = _crit_candidates(graph, tree, costs, start, d_vertices)
            if pairing == 1:
                cand = [c for c in cand if d_ids[c[1]] == o_ids[i]]
            if not cand:
                feedback.pushWarning('Origin "%s": no reachable/paired destination.' % o_ids[i])
                continue
            if select == 0:
                chosen = sorted(cand)
            elif select == 1:
                chosen = [min(cand)]
            else:
                chosen = [max(cand)]

            for rank, (c, j) in enumerate(chosen, start=1):
                res = trace_path(graph, tree, start, d_vertices[j])
                if res is None:
                    continue
                pts, dist, time = res
                feat = QgsFeature(fields)
                if len(pts) >= 2:
                    feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
                feat.setAttributes([o_ids[i], d_ids[j], rank, tipe, kriteria, dist, time])
                sink.addFeature(feat, QgsFeatureSink.FastInsert)
                n_out += 1
            feedback.setProgress(int(100.0 * (i + 1) / total))

        feedback.pushInfo('Routes created: %d' % n_out)
        return {self.OUTPUT: dest_id}


class NearestFacilityAlgorithm(NetlysNetworkAlgorithm):
    ORIGINS = 'ORIGINS'
    ORIGIN_ID = 'ORIGIN_ID'
    FACS = 'FACS'
    FAC_ID = 'FAC_ID'
    CRITERION = 'CRITERION'
    NNEAR = 'NNEAR'
    MAXCOST = 'MAXCOST'
    OUTPUT = 'OUTPUT'
    ROUTES = 'ROUTES'

    GROUP_NAME = GROUP_ROUTING
    ICON_FILE = 'od.png'
    TAGS = ['od matrix', 'cost matrix', 'nearest facility', 'accessibility', 'travel time', 'network']

    def name(self):
        return 'odmatrixnearestfacility'

    def displayName(self):
        return 'Distance/Time to Nearest Facilities (OD Matrix)'

    def createInstance(self):
        return NearestFacilityAlgorithm()

    def shortHelpString(self):
        return (
            "<p>Computes the network <b>distance (m)</b> and <b>travel time (minutes)</b> from each origin "
            "to many destination points / facilities.</p><ul>"
            "<li><b>Number of nearest facilities</b> - N nearest per origin (0 = all = full OD matrix).</li>"
            "<li><b>Ranking</b> uses the selected cost (Distance or Time).</li>"
            "<li><b>Maximum cost cutoff</b> (optional) - meters for Distance, minutes for Time; 0 = no limit.</li>"
            "<li>The main output is a table; route lines are optional.</li></ul>"
        )

    def initAlgorithm(self, config=None):
        self.add_network_parameter()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ORIGINS, 'Origin points (point)', [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.ORIGIN_ID, 'Origin ID field (optional)',
            parentLayerParameterName=self.ORIGINS, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.FACS, 'Destination / facility points (point)', [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.FAC_ID, 'Facility ID / name field (optional)',
            parentLayerParameterName=self.FACS, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.CRITERION, 'Cost used to find the nearest', options=['Distance', 'Time'], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.NNEAR, 'Number of nearest facilities per origin (0 = all)',
            QgsProcessingParameterNumber.Integer, defaultValue=3, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAXCOST, 'Maximum cost cutoff (0 = no limit)',
            QgsProcessingParameterNumber.Double, defaultValue=0.0, minValue=0.0))
        self.add_speed_parameters()
        self.add_advanced_parameters()
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'OD table', QgsProcessing.TypeVector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.ROUTES, 'Route lines (optional)', QgsProcessing.TypeVectorLine,
            optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        o_src = self.parameterAsSource(parameters, self.ORIGINS, context)
        f_src = self.parameterAsSource(parameters, self.FACS, context)
        if o_src is None or f_src is None:
            raise QgsProcessingException('Invalid origin or facility points.')
        net_layer = self.parameterAsVectorLayer(parameters, self.NETWORK, context)
        if net_layer is None:
            raise QgsProcessingException('Invalid network layer.')
        crit = self.parameterAsEnum(parameters, self.CRITERION, context)
        crit_idx = DIST_IDX if crit == 0 else TIME_IDX
        n_near = self.parameterAsInt(parameters, self.NNEAR, context)
        max_cost = self.parameterAsDouble(parameters, self.MAXCOST, context)

        o_pts, o_ids = read_points(o_src, self.parameterAsString(parameters, self.ORIGIN_ID, context),
                                   net_layer.crs(), context)
        f_pts, f_ids = read_points(f_src, self.parameterAsString(parameters, self.FAC_ID, context),
                                   net_layer.crs(), context)

        ctx = self.prepare_graph(parameters, context, feedback, [o_pts, f_pts])
        graph = ctx.graph
        o_tied, f_tied = ctx.tied
        f_vertices = [graph.findVertex(p) for p in f_tied]

        tfields = QgsFields()
        tfields.append(QgsField('origin_id', QVariant.String, len=50))
        tfields.append(QgsField('facility_id', QVariant.String, len=50))
        tfields.append(QgsField('rank', QVariant.Int))
        tfields.append(QgsField('dist_m', QVariant.Double))
        tfields.append(QgsField('time_min', QVariant.Double))
        (tsink, tdest) = self.parameterAsSink(
            parameters, self.OUTPUT, context, tfields, QgsWkbTypes.NoGeometry, ctx.network.crs())
        if tsink is None:
            raise QgsProcessingException('Failed to create the output table.')

        rsink, rdest = None, None
        rfields = QgsFields()
        rfields.append(QgsField('origin_id', QVariant.String, len=50))
        rfields.append(QgsField('facility_id', QVariant.String, len=50))
        rfields.append(QgsField('rank', QVariant.Int))
        rfields.append(QgsField('dist_m', QVariant.Double))
        rfields.append(QgsField('time_min', QVariant.Double))
        if parameters.get(self.ROUTES) not in (None, ''):
            rsink, rdest = self.parameterAsSink(
                parameters, self.ROUTES, context, rfields, QgsWkbTypes.LineString, ctx.network.crs())

        total = len(o_tied)
        no_reach = 0
        for i, pt in enumerate(o_tied):
            if feedback.isCanceled():
                break
            start = graph.findVertex(pt)
            if start == -1:
                feedback.reportError('Origin "%s" could not be tied to the network, skipped.' % o_ids[i])
                continue
            tree, costs = QgsGraphAnalyzer.dijkstra(graph, start, crit_idx)
            cand = sorted(_crit_candidates(graph, tree, costs, start, f_vertices))
            if max_cost > 0:
                cand = [c for c in cand if c[0] <= max_cost]
            if n_near > 0:
                cand = cand[:n_near]
            if not cand:
                no_reach += 1
                continue
            for rank, (c, j) in enumerate(cand, start=1):
                res = trace_path(graph, tree, start, f_vertices[j])
                if res is None:
                    continue
                pts, dist, time = res
                attrs = [o_ids[i], f_ids[j], rank, dist, time]
                tf = QgsFeature(tfields)
                tf.setAttributes(attrs)
                tsink.addFeature(tf, QgsFeatureSink.FastInsert)
                if rsink is not None:
                    rf = QgsFeature(rfields)
                    if len(pts) >= 2:
                        rf.setGeometry(QgsGeometry.fromPolylineXY(pts))
                    rf.setAttributes(attrs)
                    rsink.addFeature(rf, QgsFeatureSink.FastInsert)
            feedback.setProgress(int(100.0 * (i + 1) / total))

        if no_reach:
            feedback.pushWarning('%d origin(s) have no reachable facility (or exceed the cost cutoff).' % no_reach)

        result = {self.OUTPUT: tdest}
        if rdest:
            result[self.ROUTES] = rdest
        return result
