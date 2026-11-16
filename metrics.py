# -*- coding: utf-8 -*-
"""Pure Python logic (no QGIS dependency) so it can be unit-tested."""


def connectivity_from_edges(edges, ignore_deg2=True, count_ends=True):
    """edges: list of (node_a, node_b). Returns connectivity statistics.

    Beta  = e / v
    Gamma = e / (3 * (v - 2))   (planar network)
    If ignore_deg2 is True, degree-2 nodes (pseudo-nodes) are not counted as nodes and
    the two edges meeting there are counted as one edge.
    If count_ends is False, dead-end nodes (degree 1) are excluded from v.
    """
    deg = {}
    for a, b in edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    e_raw = len(edges)
    v_raw = len(deg)
    n1 = sum(1 for d in deg.values() if d == 1)
    n2 = sum(1 for d in deg.values() if d == 2)
    n3 = sum(1 for d in deg.values() if d >= 3)
    if ignore_deg2:
        e = max(e_raw - n2, 0)
        v = max(v_raw - n2, 0)
    else:
        e, v = e_raw, v_raw
    if not count_ends:
        v = max(v - n1, 0)
    beta = (e / float(v)) if v > 0 else None
    gamma = (e / (3.0 * (v - 2))) if v > 2 else None
    return {'e': e, 'v': v, 'n_end': n1, 'n_deg2': n2, 'n_junction': n3,
            'beta': beta, 'gamma': gamma, 'deg': deg}


def interpret_beta(beta):
    """Returns (class, definition) for the Beta index."""
    if beta is None:
        return 'N/A', 'Beta cannot be computed (no nodes).'
    if abs(beta - 1.0) < 1e-9:
        return 'Borderline', ('Beta = %.3f = 1: the road network contains a single circuit '
                              '(borderline between simple and complex).' % beta)
    if beta > 1:
        return 'Complex', 'Beta = %.3f > 1: the road network is complex.' % beta
    return 'Not complex', 'Beta = %.3f < 1: the road network is not complex.' % beta


def interpret_gamma(gamma):
    """Returns (class, definition) for the Gamma index (valid range 0 < gamma < 1)."""
    if gamma is None:
        return 'N/A', 'Gamma cannot be computed (needs more than 2 nodes).'
    if gamma > 1.0 + 1e-9:
        return 'Out of range', ('Gamma = %.3f > 1: outside the theoretical range (0-1); '
                                'check network topology (noding, snap tolerance, overpasses).' % gamma)
    if gamma >= 0.7:
        return 'High', 'Gamma = %.3f, range 0.7 <= Gamma <= 1: high connectivity.' % gamma
    if gamma >= 0.3:
        return 'Medium', 'Gamma = %.3f, range 0.3 <= Gamma < 0.7: medium connectivity.' % gamma
    return 'Low', 'Gamma = %.3f, range 0 <= Gamma < 0.3: low connectivity.' % gamma


def reachable_segments(edges, vpts, costs, tree, start, classes):
    """Reachable edges (or parts of edges) for each cost class.

    edges : list of (from, to, cost) directed edges
    vpts  : list of (x, y) vertex coordinates
    costs, tree : Dijkstra result (tree[v] == -1 => unreachable)
    classes : ascending list of cost cutoffs
    Returns {cutoff: [((x1,y1),(x2,y2)), ...]}
    """
    max_limit = classes[-1]
    out = dict((L, []) for L in classes)
    for (f, t, ec) in edges:
        if f != start and tree[f] == -1:
            continue
        cf = costs[f]
        if cf < 0 or cf > max_limit:
            continue
        p1, p2 = vpts[f], vpts[t]
        for L in classes:
            if cf > L:
                continue
            if ec <= 0 or cf + ec <= L:
                end = p2
            else:
                fr = (L - cf) / ec
                end = (p1[0] + (p2[0] - p1[0]) * fr, p1[1] + (p2[1] - p1[1]) * fr)
            if p1[0] != end[0] or p1[1] != end[1]:
                out[L].append((p1, end))
    return out


def tree_segments(edges, vpts, costs, tree, classes):
    """Shortest-path-tree segments split at class cutoffs (like ArcGIS service area lines).
    Returns list of (k, cutoff, cost_from, cost_to, (x1,y1), (x2,y2)); k = class order from 1."""
    max_limit = classes[-1]
    out = []
    for v, eid in enumerate(tree):
        if eid == -1:
            continue
        f, t, ec = edges[eid]
        cf, ct = costs[f], costs[t]
        if cf < 0 or cf >= max_limit or ct <= cf:
            continue
        p1, p2 = vpts[f], vpts[t]
        span = ct - cf
        lo = 0.0
        for k, L in enumerate(classes, start=1):
            a, b = max(cf, lo), min(ct, L)
            if b > a:
                f1, f2 = (a - cf) / span, (b - cf) / span
                q1 = (p1[0] + (p2[0] - p1[0]) * f1, p1[1] + (p2[1] - p1[1]) * f1)
                q2 = (p1[0] + (p2[0] - p1[0]) * f2, p1[1] + (p2[1] - p1[1]) * f2)
                out.append((k, L, a, b, q1, q2))
            lo = L
    return out
