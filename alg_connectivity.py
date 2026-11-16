# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingOutputNumber,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsWkbTypes,
)

from .core import GROUP_METRICS, make_icon, slug
from .metrics import connectivity_from_edges, interpret_beta, interpret_gamma


def _polylines(g):
    if g.isMultipart():
        return g.asMultiPolyline()
    return [g.asPolyline()]


class ConnectivityAlgorithm(QgsProcessingAlgorithm):
    NETWORK = 'NETWORK'
    NODING = 'NODING'
    IGNORE2 = 'IGNORE2'
    COUNT_ENDS = 'COUNT_ENDS'
    TOL = 'TOL'
    OUT_SUMMARY = 'OUT_SUMMARY'
    OUT_NODES = 'OUT_NODES'
    EDGES = 'EDGES'
    NODES = 'NODES'
    BETA = 'BETA'
    GAMMA = 'GAMMA'

    def name(self):
        return 'networkconnectivity'

    def displayName(self):
        return 'Network Connectivity (Beta & Gamma)'

    def group(self):
        return GROUP_METRICS

    def groupId(self):
        return slug(GROUP_METRICS)

    def icon(self):
        return make_icon('connectivity.png')

    def tags(self):
        return ['connectivity', 'beta', 'gamma', 'graph', 'network', 'junction', 'node', 'edge']

    def createInstance(self):
        return ConnectivityAlgorithm()

    def shortHelpString(self):
        return (
            "<p>Counts the <b>edges (e)</b> and <b>nodes/junctions (v)</b> of a road network and computes "
            "the connectivity indices:</p>"
            "<p><b>Beta</b> = e / v<br><b>Gamma</b> = e / (3 x (v - 2)) &nbsp;(planar network)</p>"
            "<p>The summary table also contains <b>definition fields</b>:</p><ul>"
            "<li><b>Beta</b>: &gt; 1 complex; &lt; 1 not complex.</li>"
            "<li><b>Gamma</b> (0 &lt; Gamma &lt; 1): 0 - 0.3 low; 0.3 - 0.7 medium; 0.7 - 1 high.</li></ul>"
            "<p>Nodes = junctions (degree &gt;= 3) <b>and</b> line ends without a junction / dead ends (degree 1); "
            "line ends count as junctions when <i>Count line ends</i> is enabled (default). "
            "<i>Ignore degree-2 nodes</i> removes pseudo-nodes (a point that only joins two edges); "
            "the two edges meeting there are counted as one edge.</p>"
            "<p><i>Noding</i> splits lines at all crossing points; turn it off if the network is already split at "
            "every junction and has overpasses/underpasses that must not count as junctions.</p>"
            "<p>Note: completely isolated ring roads are not counted.</p>"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.NETWORK, 'Road network layer (line)', [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterBoolean(
            self.NODING, 'Split lines at crossing points (noding)', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.IGNORE2, 'Ignore degree-2 nodes (pseudo-nodes)', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.COUNT_ENDS, 'Count line ends without a junction (dead ends) as nodes/junctions',
            defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOL, 'Node snap tolerance (map units)',
            QgsProcessingParameterNumber.Double, defaultValue=0.01, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_SUMMARY, 'Connectivity summary', QgsProcessing.TypeVector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_NODES, 'Node points (optional)', QgsProcessing.TypeVectorPoint,
            optional=True, createByDefault=False))
        self.addOutput(QgsProcessingOutputNumber(self.EDGES, 'Number of edges (e)'))
        self.addOutput(QgsProcessingOutputNumber(self.NODES, 'Number of nodes (v)'))
        self.addOutput(QgsProcessingOutputNumber(self.BETA, 'Beta index'))
        self.addOutput(QgsProcessingOutputNumber(self.GAMMA, 'Gamma index'))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.NETWORK, context)
        if source is None:
            raise QgsProcessingException('Invalid network layer.')
        noding = self.parameterAsBool(parameters, self.NODING, context)
        ignore2 = self.parameterAsBool(parameters, self.IGNORE2, context)
        count_ends = self.parameterAsBool(parameters, self.COUNT_ENDS, context)
        tol = self.parameterAsDouble(parameters, self.TOL, context)

        geoms = []
        for f in source.getFeatures():
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            geoms.append(g)
        if not geoms:
            raise QgsProcessingException('The network layer is empty.')

        if noding:
            feedback.pushInfo('Noding: splitting lines at crossing points...')
            merged = QgsGeometry.unaryUnion(geoms)
            if merged is None or merged.isNull() or merged.isEmpty():
                raise QgsProcessingException('Noding failed.')
            polylines = _polylines(merged)
        else:
            polylines = []
            for g in geoms:
                polylines.extend(_polylines(g))

        def key(p):
            if tol > 0:
                return (int(round(p.x() / tol)), int(round(p.y() / tol)))
            return (p.x(), p.y())

        coords = {}
        edges = []
        for pl in polylines:
            if len(pl) < 2:
                continue
            if len(pl) == 2 and pl[0] == pl[1]:
                continue
            a, b = key(pl[0]), key(pl[-1])
            coords.setdefault(a, pl[0])
            coords.setdefault(b, pl[-1])
            edges.append((a, b))
        if not edges:
            raise QgsProcessingException('No valid edges found.')

        st = connectivity_from_edges(edges, ignore2, count_ends)
        e, v = st['e'], st['v']
        beta, gamma = st['beta'], st['gamma']
        beta_cls, beta_def = interpret_beta(beta)
        gamma_cls, gamma_def = interpret_gamma(gamma)
        feedback.pushInfo('Edges (e) = %d, Nodes (v) = %d' % (e, v))
        feedback.pushInfo('Junctions (degree>=3) = %d, Dead ends = %d, Degree-2 nodes = %d' %
                          (st['n_junction'], st['n_end'], st['n_deg2']))
        feedback.pushInfo(beta_def)
        feedback.pushInfo(gamma_def)

        fields = QgsFields()
        fields.append(QgsField('edges', QVariant.Int))
        fields.append(QgsField('nodes', QVariant.Int))
        fields.append(QgsField('junctions', QVariant.Int))
        fields.append(QgsField('dead_ends', QVariant.Int))
        fields.append(QgsField('deg2_nodes', QVariant.Int))
        fields.append(QgsField('ignore_deg2', QVariant.Int))
        fields.append(QgsField('count_ends', QVariant.Int))
        fields.append(QgsField('beta', QVariant.Double))
        fields.append(QgsField('beta_class', QVariant.String, len=20))
        fields.append(QgsField('beta_def', QVariant.String, len=254))
        fields.append(QgsField('gamma', QVariant.Double))
        fields.append(QgsField('gamma_class', QVariant.String, len=20))
        fields.append(QgsField('gamma_def', QVariant.String, len=254))
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUT_SUMMARY, context, fields, QgsWkbTypes.NoGeometry, source.sourceCrs())
        if sink is None:
            raise QgsProcessingException('Failed to create the summary table.')
        feat = QgsFeature(fields)
        feat.setAttributes([e, v, st['n_junction'], st['n_end'], st['n_deg2'],
                            1 if ignore2 else 0, 1 if count_ends else 0,
                            beta, beta_cls, beta_def, gamma, gamma_cls, gamma_def])
        sink.addFeature(feat, QgsFeatureSink.FastInsert)

        result = {self.OUT_SUMMARY: dest_id, self.EDGES: e, self.NODES: v}
        if beta is not None:
            result[self.BETA] = beta
        if gamma is not None:
            result[self.GAMMA] = gamma

        if parameters.get(self.OUT_NODES) not in (None, ''):
            nfields = QgsFields()
            nfields.append(QgsField('node_id', QVariant.Int))
            nfields.append(QgsField('degree', QVariant.Int))
            nfields.append(QgsField('type', QVariant.String, len=20))
            (nsink, ndest) = self.parameterAsSink(
                parameters, self.OUT_NODES, context, nfields, QgsWkbTypes.Point, source.sourceCrs())
            if nsink is not None:
                nid = 0
                for k, d in st['deg'].items():
                    if ignore2 and d == 2:
                        continue
                    if (not count_ends) and d == 1:
                        continue
                    nid += 1
                    tipe = 'dead_end' if d == 1 else ('junction' if d >= 3 else 'degree-2')
                    nf = QgsFeature(nfields)
                    nf.setGeometry(QgsGeometry.fromPointXY(coords[k]))
                    nf.setAttributes([nid, d, tipe])
                    nsink.addFeature(nf, QgsFeatureSink.FastInsert)
                result[self.OUT_NODES] = ndest
        return result
