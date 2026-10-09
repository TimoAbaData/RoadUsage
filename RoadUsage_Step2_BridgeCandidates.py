"""Step 2 only: find gap bridges for review, without changing the road graph."""

import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree


# Inputs match the existing Step 1 and fast routing configuration.
INPUT_GDB = r"F:\Road Usage\Progress\RoadUsageProcess.gdb"
PRIVATE_ROADS_LAYER = "Road_Network_Private_Unknown"
PUBLIC_ROADS_LAYER = "Road_Network_Public"
TARGET_WKID = 3400
MAX_GAP_BRIDGE_M = 100.0

# Separate QA files; authoritative inputs and existing routing outputs are untouched.
OUTPUT_FOLDER = os.path.dirname(os.path.abspath(__file__))
OUTPUT_GPKG = os.path.join(OUTPUT_FOLDER, "RoadUsage_Bridge_QA.gpkg")
OUTPUT_COMPONENT_CSV = os.path.join(OUTPUT_FOLDER, "Bridge_Component_QC.csv")
OUTPUT_SUMMARY_JSON = os.path.join(OUTPUT_FOLDER, "Bridge_QA_Run_Summary.json")
CANDIDATE_LAYER = "Candidate_Gap_Bridges"

LOGGER = logging.getLogger(__name__)
CANDIDATE_SCHEMA = {
    "geometry": "LineString",
    "properties": {
        "CandidateID": "int",
        "SourceComponentID": "int",
        "TargetComponentID": "int",
        "SourceNodeID": "str",
        "TargetNodeID": "str",
        "SourceSegmentID": "int",
        "TargetSegmentID": "int",
        "GapDistance_m": "float",
        "TargetHasPublicRoad": "int",
        "CandidateRank": "int",
        "Accepted": "int",
        "RejectionReason": "str",
    },
}


def boundary_endpoints(graph):
    """Ignore duplicate owners of one path, but do not mistake loops for ends.

    A closed road split into two different paths between the same two nodes has
    one unique neighbour at each node, yet neither node is an open road end.
    """
    endpoints = set()
    for node in graph:
        if len(graph[node]) != 1 or node in graph[node]:
            continue
        parallel = next(iter(graph[node].values()))
        geometries = [data["geometry"] for data in parallel.values()]
        if all(geometries[0].equals(line) for line in geometries[1:]):
            endpoints.add(node)
    return endpoints


def compute_connected_components(graph, endpoints=None):
    """Full-network IDs, distinct from the routing script's public-only IDs.

    Any node incident to a PUBLIC edge is a public-road node. In this undirected
    graph, every node in its full component can reach that public road. The
    routing script's 500 m public-destination filter is deliberately not applied.
    IDs are local to this graph build; no node/edge attributes are modified.
    """
    if endpoints is None:
        endpoints = boundary_endpoints(graph)
    node_component = {}
    records = []
    for component_id, nodes in enumerate(nx.connected_components(graph), start=1):
        for node in nodes:
            node_component[node] = component_id
        records.append({
            "ComponentID": component_id,
            "HasPublicRoad": 0,
            "NodeCount": len(nodes),
            "EdgeCount": 0,
            "EndpointCount": len(nodes & endpoints),
        })
    for u, v, data in graph.edges(data=True):
        record = records[node_component[u] - 1]
        record["EdgeCount"] += 1
        if data.get("ROAD_CLASS") == "PUBLIC":
            record["HasPublicRoad"] = 1
    components = pd.DataFrame(records, columns=[
        "ComponentID", "HasPublicRoad", "NodeCount", "EdgeCount", "EndpointCount",
    ])
    return node_component, components


def find_nearby_bridge_candidates(graph, crs, max_gap_m=MAX_GAP_BRIDGE_M):
    """Search true road endpoints against reachable graph-edge geometry.

    Rank all candidates within the threshold by gap distance per source component.
    Duplicate geometry owners do not hide a dead end; distinct loop paths do.
    Use original geometry coordinates, rather than rounded graph keys, for gaps.
    Components without end nodes remain in the component QA table for review.
    """
    if not math.isfinite(max_gap_m) or max_gap_m <= 0:
        raise ValueError("max_gap_m must be a finite, positive distance in metres.")
    empty = gpd.GeoDataFrame(
        columns=list(CANDIDATE_SCHEMA["properties"]) + ["geometry"],
        geometry="geometry", crs=crs,
    )
    if empty.crs is None or not empty.crs.is_projected or not all(
        math.isclose(axis.unit_conversion_factor, 1.0)
        for axis in empty.crs.axis_info[:2]
    ):
        raise ValueError("Bridge search requires a projected CRS with metre units.")
    if graph.is_directed() or not graph.is_multigraph():
        raise TypeError("Expected the existing undirected NetworkX MultiGraph.")

    endpoints = boundary_endpoints(graph)
    node_component, components = compute_connected_components(graph, endpoints)
    reachable = set(components.loc[components.HasPublicRoad == 1, "ComponentID"])
    targets = []
    sources = defaultdict(list)
    for u, v, data in graph.edges(data=True):
        geometry = data["geometry"]
        edge_id = int(data["EDGE_ID"])
        component_id = node_component[u]
        if component_id in reachable:
            targets.append((u, v, component_id, edge_id, geometry))
            continue
        for node in (u, v):
            if node not in endpoints:
                continue
            # Geometry orientation need not match NetworkX edge iteration order.
            xy = min((geometry.coords[0][:2], geometry.coords[-1][:2]),
                     key=lambda coord: math.dist(coord, node[:2]))
            sources[component_id].append((node, edge_id, Point(xy)))

    LOGGER.info("Graph: %s nodes, %s edges; %s components, %s with public roads, "
                "%s disconnected", graph.number_of_nodes(), graph.number_of_edges(),
                len(components), len(reachable), len(components) - len(reachable))
    tree = STRtree([target[4] for target in targets]) if targets else None
    records = []
    rejected_distance = 0
    for source_id, locations in sources.items():
        # Deduplicate identical connectors, e.g. at adjacent target segments.
        candidates = {}
        for source_node, source_edge_id, point in locations:
            if tree is None:
                break
            nearby = tree.query(box(point.x - max_gap_m, point.y - max_gap_m,
                                    point.x + max_gap_m, point.y + max_gap_m))
            for index in nearby:
                u, v, target_id, target_edge_id, line = targets[int(index)]
                projected = line.interpolate(line.project(point))
                target_point = Point(projected.x, projected.y)
                gap = float(point.distance(target_point))
                if gap > max_gap_m:
                    rejected_distance += 1
                    continue
                if gap <= 1e-8:
                    continue  # Exact touches are handled by the existing noding.
                target_node = None
                if any(target_point.distance(Point(xy[:2])) <= 1e-8
                       for xy in (line.coords[0], line.coords[-1])):
                    target_node = str(min((u, v), key=lambda n: math.dist(n[:2], target_point.coords[0])))
                record = {
                    "SourceComponentID": int(source_id),
                    "TargetComponentID": int(target_id),
                    "SourceNodeID": str(source_node),
                    "TargetNodeID": target_node,
                    "SourceSegmentID": source_edge_id,
                    "TargetSegmentID": target_edge_id,
                    "GapDistance_m": gap,
                    "TargetHasPublicRoad": 1,
                    "Accepted": 0,
                    "RejectionReason": "",
                    "geometry": LineString([point, target_point]),
                }
                key = (source_node, point.x, point.y, target_id, target_point.x, target_point.y)
                previous = candidates.get(key)
                if previous is None or (source_edge_id, target_edge_id) < (
                    previous["SourceSegmentID"], previous["TargetSegmentID"]
                ):
                    candidates[key] = record
        ranked = sorted(candidates.values(), key=lambda r: (
            r["GapDistance_m"], r["TargetComponentID"], r["SourceNodeID"],
            r["SourceSegmentID"], r["TargetSegmentID"],
        ))
        for rank, record in enumerate(ranked, start=1):
            record["CandidateID"] = len(records) + 1
            record["CandidateRank"] = rank
            records.append(record)

    candidates = (gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
                  if records else empty)
    counts = candidates.groupby("SourceComponentID").size()
    components["CandidateCount"] = components.ComponentID.map(counts).fillna(0).astype(int)
    components["SearchStatus"] = "NO_CANDIDATE_WITHIN_LIMIT"
    components.loc[components.EndpointCount == 0, "SearchStatus"] = "NO_ENDPOINTS"
    components.loc[components.CandidateCount > 0, "SearchStatus"] = "CANDIDATES_FOR_REVIEW"
    components.loc[components.HasPublicRoad == 1, "SearchStatus"] = "HAS_PUBLIC_ROAD"
    LOGGER.info("Bridge candidates: %s; bounding-box matches beyond threshold: %s",
                len(candidates), rejected_distance)
    if records:
        LOGGER.info("Candidate gap range: %.3f to %.3f m",
                    candidates.GapDistance_m.min(), candidates.GapDistance_m.max())
    return candidates, components


def write_bridge_run_summary(candidates, components, output_json, *, max_gap_m,
                             topology_tolerance_m, input_description):
    """Record the network basis so older QA is not mistaken for a new run.

    This is provenance, not topology approval. Candidate IDs and component IDs
    are valid only for the recorded graph build.
    """
    summary = {
        "GeneratedUTC": datetime.now(timezone.utc).isoformat(),
        "Input": input_description,
        "CRS": candidates.crs.to_string(),
        "TopologySnapTolerance_m": topology_tolerance_m,
        "MaxGapBridge_m": max_gap_m,
        "ReviewStatus": "PENDING_MANUAL_REVIEW",
        "AutomaticallyAddedConnectors": 0,
        "Nodes": int(components.NodeCount.sum()),
        "Edges": int(components.EdgeCount.sum()),
        "Components": len(components),
        "ComponentsWithPublicRoad": int(components.HasPublicRoad.sum()),
        "DisconnectedComponents": int((components.HasPublicRoad == 0).sum()),
        "ComponentsWithCandidates": int((components.CandidateCount > 0).sum()),
        "CandidateCount": len(candidates),
        "MinimumGap_m": float(candidates.GapDistance_m.min()) if len(candidates) else None,
        "MaximumGap_m": float(candidates.GapDistance_m.max()) if len(candidates) else None,
    }
    with open(output_json, "w", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=2)
    LOGGER.info("Run provenance: %s", output_json)
    return summary


def write_candidate_bridge_qc(candidates, output_gpkg=OUTPUT_GPKG):
    """Replace only the dedicated candidate layer, including an empty result.

    Explicit schema preserves long field names and creates a valid empty layer
    on reruns, so stale candidates cannot masquerade as the current result.
    """
    candidates.to_file(output_gpkg, layer=CANDIDATE_LAYER, driver="GPKG",
                       engine="fiona", schema=CANDIDATE_SCHEMA, index=False, mode="w")
    LOGGER.info("Candidate QA: %s | %s", output_gpkg, CANDIDATE_LAYER)


def main():
    # Lazy import lets the geometry search and QA writer be tested without ArcPy.
    import RoadUsage_Step2_Routing_Fast as routing

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    started = time.perf_counter()
    routing.OUT_GDB = INPUT_GDB
    routing.TARGET_WKID = TARGET_WKID
    routing.PRIVATE_ROADS_LAYER = PRIVATE_ROADS_LAYER
    routing.PUBLIC_ROADS_LAYER = PUBLIC_ROADS_LAYER
    private, public = routing.read_roads()
    roads = gpd.GeoDataFrame(pd.concat([private, public], ignore_index=True),
                             geometry="geometry", crs=private.crs)
    # Deliberately bypass build_nearmiss_connectors: no synthetic graph edges.
    no_connectors = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=roads.crs)
    edges = routing.noded_segments(roads, no_connectors)
    graph = routing.build_graph(edges)
    candidates, components = find_nearby_bridge_candidates(graph, edges.crs, MAX_GAP_BRIDGE_M)
    write_candidate_bridge_qc(candidates)
    components.to_csv(OUTPUT_COMPONENT_CSV, index=False)
    write_bridge_run_summary(
        candidates, components, OUTPUT_SUMMARY_JSON, max_gap_m=MAX_GAP_BRIDGE_M,
        topology_tolerance_m=routing.TOPOLOGY_SNAP_TOLERANCE_M,
        input_description={"GDB": INPUT_GDB, "PrivateRoadsLayer": PRIVATE_ROADS_LAYER,
                           "PublicRoadsLayer": PUBLIC_ROADS_LAYER,
                           "GraphPreparation": "Existing-road topology repair; no synthetic connectors"},
    )
    LOGGER.info("Component QA: %s", OUTPUT_COMPONENT_CSV)
    LOGGER.info("Completed in %.1f seconds; all candidates pending manual review.",
                time.perf_counter() - started)


if __name__ == "__main__":
    main()
