"""Conservative existing-road topology repair; never creates connector edges."""
import logging
import math
from collections import defaultdict

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point, box
from shapely.ops import substring
from shapely.strtree import STRtree

LOGGER = logging.getLogger(__name__)
EPS = 1e-7
SNAP_AMBIGUITY_WINDOW_M = 0.01
ISSUE_COLUMNS = ["IssueID", "IssueType", "SourceRoadID", "TargetRoadID",
                 "SourceRoadClass", "TargetRoadClass", "Distance_m",
                 "SameComponentBefore", "SameComponentAfter", "RepairApplied",
                 "Reason", "SourceX", "SourceY", "TargetX", "TargetY", "geometry"]


def component_metadata(graph):
    node_component = {}
    records = []
    for component_id, nodes in enumerate(nx.connected_components(graph), 1):
        for node in nodes:
            node_component[node] = component_id
        records.append({"ComponentID": component_id, "NodeCount": len(nodes),
                        "EdgeCount": 0, "HasPublicRoad": 0})
    for u, v, data in graph.edges(data=True):
        record = records[node_component[u] - 1]
        record["EdgeCount"] += 1
        if data.get("ROAD_CLASS") == "PUBLIC":
            record["HasPublicRoad"] = 1
    return node_component, pd.DataFrame(records, columns=[
        "ComponentID", "NodeCount", "EdgeCount", "HasPublicRoad"])


def _intersection_points(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type == "LineString":
        return [Point(geometry.coords[0][:2]), Point(geometry.coords[-1][:2])]
    return [point for part in geometry.geoms for point in _intersection_points(part)]


def _level(record, field):
    value = record.get(field) if field else None
    if value is None or pd.isna(value):
        return None
    value = str(value).strip().upper()
    return None if value in {"", "UNKNOWN", "UNK", "NONE", "NULL", "N/A"} else value


def _grade_compatible(a, b, line_a, line_b, point, endpoint_evidence,
                      grade_level_field, structure_fields, z_separation_m):
    first, second = _level(a, grade_level_field), _level(b, grade_level_field)
    if first is not None or second is not None:
        if first != second:
            return False, "GRADE_LEVEL_MISMATCH_OR_UNKNOWN"
    else:
        for record in (a, b):
            for field in structure_fields:
                value = record.get(field)
                if value is not None and not pd.isna(value) and str(value).strip().upper() not in {
                    "", "0", "0.0", "FALSE", "NO", "NONE", "GROUND", "AT_GRADE"
                }:
                    return False, "STRUCTURE_REQUIRES_GRADE_REVIEW"
        if not endpoint_evidence:
            return False, "INTERIOR_CROSSING_GRADE_UNVERIFIED"
    if line_a.has_z and line_b.has_z:
        za = line_a.interpolate(line_a.project(point)).z
        zb = line_b.interpolate(line_b.project(point)).z
        if math.isfinite(za) and math.isfinite(zb) and abs(za - zb) > z_separation_m:
            return False, "Z_SEPARATION"
    return True, "EXISTING_ROAD_JUNCTION"


def prepare_road_topology(roads, snap_tolerance_m=1.0, grade_level_field=None,
                          structure_fields=("BRIDGE", "TUNNEL", "STRUCTURE", "STRUCTTYPE"),
                          z_separation_m=3.0, record_exact_junctions=False):
    """Snap genuine near-coincident endpoints, then split each source separately.

    Attributes are copied from the owning source, including overlapping PUBLIC
    lines. Unknown interior/interior crossings are held for grade review. Each
    split location has a junction ID: blocked crossings cannot be joined merely
    by coordinate rounding. Endpoint movement is bounded, uses frozen anchors,
    and does not cascade through a chain of nearby endpoints.
    """
    if not math.isfinite(snap_tolerance_m) or snap_tolerance_m < 0:
        raise ValueError("Topology tolerance must be finite and nonnegative.")
    if roads.crs is None or not roads.crs.is_projected or not all(
        math.isclose(axis.unit_conversion_factor, 1.0) for axis in roads.crs.axis_info[:2]
    ):
        raise ValueError("Topology repair requires a projected metre CRS.")
    roads = roads.reset_index(drop=True).copy()
    if "SOURCE_ROAD_ID" not in roads:
        roads["SOURCE_ROAD_ID"] = roads.index
    if not roads.SOURCE_ROAD_ID.is_unique:
        raise ValueError("SOURCE_ROAD_ID must uniquely identify each source part.")
    source_records = roads.drop(columns="geometry").to_dict("records")
    original = list(roads.geometry)
    if any(line.geom_type != "LineString" or line.is_empty for line in original):
        raise ValueError("Supply nonempty, singlepart road LineStrings.")
    # Topology is planar; Z is retained in the source data and used in grade checks.
    lines = [LineString([xy[:2] for xy in line.coords]) for line in original]
    tree = STRtree(lines)
    issues = []
    snapped_junctions = []

    def issue(kind, i, j, point, target, distance, applied, reason):
        issues.append({"IssueID": len(issues) + 1, "IssueType": kind,
                       "SourceRoadID": int(source_records[i]["SOURCE_ROAD_ID"]),
                       "TargetRoadID": int(source_records[j]["SOURCE_ROAD_ID"]) if j is not None else None,
                       "SourceRoadClass": source_records[i].get("ROAD_CLASS"),
                       "TargetRoadClass": source_records[j].get("ROAD_CLASS") if j is not None else None,
                       "Distance_m": float(distance), "SameComponentBefore": None,
                       "SameComponentAfter": None, "RepairApplied": int(applied), "Reason": reason,
                       "SourceX": point.x, "SourceY": point.y, "TargetX": target.x, "TargetY": target.y,
                       "geometry": Point(point.x, point.y)})

    groups = defaultdict(list)
    for i, line in enumerate(lines):
        for side in (0, -1):
            groups[tuple(line.coords[side])].append((i, side))
    frozen_roads, locked_groups = set(), set()
    for xy, members in groups.items():
        if snap_tolerance_m == 0:
            break
        point = Point(xy)
        member_ids = {i for i, side in members}
        options = []
        exact_interior_touch = False
        for raw_j in tree.query(box(point.x - 2 * snap_tolerance_m, point.y - 2 * snap_tolerance_m,
                                    point.x + 2 * snap_tolerance_m, point.y + 2 * snap_tolerance_m)):
            j = int(raw_j)
            if j in member_ids:
                continue
            target = lines[j].interpolate(lines[j].project(point))
            distance = point.distance(target)
            target_is_end = min(target.distance(Point(lines[j].coords[side])) for side in (0, -1)) <= EPS
            if point.intersects(lines[j]) and not target_is_end:
                exact_interior_touch = True
            if distance == 0 or distance > snap_tolerance_m:
                continue
            allowed, reason = _grade_compatible(source_records[members[0][0]], source_records[j],
                                                original[members[0][0]], original[j], target, True,
                                                grade_level_field, structure_fields, z_separation_m)
            if allowed:
                options.append((distance, j, target, target_is_end))
            else:
                issue("ENDPOINT_NEAR_ROAD", members[0][0], j, point, target, distance, False, reason)
        if not options:
            continue
        options.sort(key=lambda item: (item[0], item[1]))
        distance, j, target, target_is_end = options[0]
        reason = None
        if xy in locked_groups or member_ids & frozen_roads:
            reason = "ANCHOR_LOCKED_NO_CASCADING_SNAP"
        elif exact_interior_touch:
            reason = "EXISTING_JUNCTION_REQUIRES_REVIEW"
        elif any(not _grade_compatible(source_records[i], source_records[j], original[i], original[j],
                                       target, True, grade_level_field, structure_fields, z_separation_m)[0]
                 for i, side in members):
            reason = "ENDPOINT_GROUP_GRADE_CONFLICT"
        # A fixed centimetre window treats a 0.1 mm precision gap and a
        # different target several millimetres away as tied. Cap the window
        # by the nearest gap too; genuinely similar alternatives stay held.
        elif any(abs(option[0] - distance) <= min(SNAP_AMBIGUITY_WINDOW_M, max(EPS, distance))
                 and option[2].distance(target) > EPS
                 for option in options[1:]):
            reason = "AMBIGUOUS_NEAR_ROADS"
        if reason:
            issue("ENDPOINT_NEAR_ROAD", members[0][0], j, point, target, distance, False, reason)
            continue
        for i, side in members:
            coords = list(lines[i].coords)
            coords[side] = (target.x, target.y)
            replacement = LineString(coords)
            if replacement.length <= EPS:
                issue("COORDINATE_MISMATCH", i, j, point, target, distance, False, "COLLAPSED_ROAD_REJECTED")
                continue
            lines[i] = replacement
            snapped_junctions.append((i, side, j, target))
            issue("COORDINATE_MISMATCH" if target_is_end else "ENDPOINT_NEAR_ROAD",
                  i, j, point, target, distance, True, "EXISTING_ENDPOINT_SNAPPED")
        locked_groups.add(xy)
        frozen_roads.add(j)
        if target_is_end:
            locked_groups.add(tuple(target.coords[0]))
    LOGGER.info("Endpoint movements: %s", sum(row["RepairApplied"] for row in issues))

    # Explicit source-specific cuts preserve road ownership/classification.
    tree = STRtree(lines)
    cuts = [dict() for line in lines]
    parents, junction_xy = [], []

    def find(token):
        while parents[token] != token:
            parents[token] = parents[parents[token]]
            token = parents[token]
        return token

    def cut(i, point, measure=None):
        measure = float(lines[i].project(point)) if measure is None else measure
        if measure <= EPS:
            measure = 0.0
        elif lines[i].length - measure <= EPS:
            measure = float(lines[i].length)
        key = round(measure, 7)
        if key not in cuts[i]:
            token = len(parents)
            parents.append(token)
            junction_xy.append((point.x, point.y))
            cuts[i][key] = (measure, token)
        return cuts[i][key][1]

    # A projected point can miss a sloping line by floating-point epsilon.
    # Register approved snaps explicitly instead of relying on intersects().
    for i, side, j, point in snapped_junctions:
        measure = 0.0 if side == 0 else float(lines[i].length)
        a, b = find(cut(i, point, measure)), find(cut(j, point))
        if a != b:
            parents[b] = a
            junction_xy[a] = (point.x, point.y)

    for i, line in enumerate(lines):
        first, last = Point(line.coords[0]), Point(line.coords[-1])
        start, end = cut(i, first, 0.0), cut(i, last, float(line.length))
        if first.distance(last) <= EPS:
            parents[end] = start
            cut(i, line.interpolate(line.length / 2), line.length / 2)  # Keep closed roads routable.
            issue("CLOSED_ROAD_RETAINED", i, None, first, first, 0, True, "CLOSED_ROAD_SPLIT_INTO_ROUTABLE_EDGES")
        if not line.is_simple:
            issue("SELF_INTERSECTION_REVIEW", i, None, line.interpolate(.5, normalized=True),
                  line.interpolate(.5, normalized=True), 0, False, "NO_UNVERIFIED_SELF_CROSSING_JUNCTION")
        for raw_j in tree.query(line, predicate="intersects"):
            j = int(raw_j)
            if j <= i:
                continue
            intersection = line.intersection(lines[j])
            overlaps = intersection.geom_type in {"LineString", "MultiLineString"}
            for point in _intersection_points(intersection):
                point = Point(point.x, point.y)
                endpoint = any(point.distance(Point(geom.coords[side])) <= EPS
                               for geom in (line, lines[j]) for side in (0, -1))
                allowed, reason = _grade_compatible(source_records[i], source_records[j],
                                                    original[i], original[j], point, endpoint or overlaps,
                                                    grade_level_field, structure_fields, z_separation_m)
                kind = "TOUCH_NOT_NODED" if endpoint else "INTERSECTION_NOT_NODED"
                if allowed:
                    a, b = find(cut(i, point)), find(cut(j, point))
                    if a != b:
                        parents[b] = a
                        junction_xy[a] = (point.x, point.y)
                if not allowed or record_exact_junctions:
                    issue(kind, i, j, point, point, 0, allowed, reason)
                elif overlaps and source_records[i].get("ROAD_CLASS") != source_records[j].get("ROAD_CLASS"):
                    issue("PUBLIC_ATTRIBUTE_OVERLAP", i, j, point, point, 0, True, "SOURCE_ATTRIBUTES_PRESERVED_ON_OVERLAP")
        if i and i % 100000 == 0:
            LOGGER.info("Intersection checks: %s / %s source parts", i, len(lines))

    records = []
    for i, line in enumerate(lines):
        ordered = sorted(cuts[i].values())
        for (start, start_token), (end, end_token) in zip(ordered, ordered[1:]):
            if end - start <= EPS:
                continue
            piece = substring(line, start, end)
            if piece.geom_type != "LineString":
                continue
            u, v = find(start_token), find(end_token)
            coords = list(piece.coords)
            coords[0], coords[-1] = junction_xy[u], junction_xy[v]
            geometry = LineString(coords)
            if geometry.length <= EPS:
                continue
            record = source_records[i].copy()
            record.update({"geometry": geometry, "EDGE_ID": len(records),
                           "LENGTH_M": geometry.length, "IS_CONNECTOR": 0,
                           "NODE_U_ID": u, "NODE_V_ID": v})
            records.append(record)
    columns = list(roads.columns) + ["EDGE_ID", "LENGTH_M", "IS_CONNECTOR", "NODE_U_ID", "NODE_V_ID"]
    edges = gpd.GeoDataFrame(records, geometry="geometry", crs=roads.crs) if records else gpd.GeoDataFrame(
        columns=list(dict.fromkeys(columns)), geometry="geometry", crs=roads.crs)
    qa = gpd.GeoDataFrame(issues, geometry="geometry", crs=roads.crs) if issues else gpd.GeoDataFrame(
        columns=ISSUE_COLUMNS, geometry="geometry", crs=roads.crs)
    LOGGER.info("Attribute-preserving noded segments: %s", len(edges))
    return edges, qa
