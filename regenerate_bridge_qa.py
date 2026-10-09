"""Regenerate reviewed-network bridge QA from the existing cache; never insert bridges."""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

import RoadUsage_Step2_BridgeCandidates as bridges
import RoadUsage_Step2_Routing_Fast as routing

ROOT = Path(__file__).resolve().parent
INPUT_CACHE = ROOT / "Topology_Input_Cache.pkl"
VALIDATION_FOLDER = ROOT / "Topology_Validation_20261005_113118"
REVIEW_DECISION = ROOT / "Topology_Spatial_Review_Decision.json"
LOGGER = logging.getLogger(__name__)


def annotate_review(candidates, components, origins, edges):
    """Prioritize only origins within snapping range that lack public access."""
    affected = origins[origins.Status == "DISCONNECTED"]
    counts = affected.groupby("ComponentID").size()
    components = components.copy()
    components["AffectedOrigins"] = components.ComponentID.map(counts).fillna(0).astype(int)
    review = candidates.copy()
    review["AffectedOrigins"] = review.SourceComponentID.map(counts).fillna(0).astype(int)
    review["ReviewType"] = "GAP_BRIDGE_REVIEW"
    review.loc[review.GapDistance_m <= routing.TOPOLOGY_SNAP_TOLERANCE_M,
               "ReviewType"] = "TOPOLOGY_OR_GRADE_REVIEW"
    lookup = edges.set_index("EDGE_ID")
    for prefix in ("Source", "Target"):
        for field, suffix in (("SOURCE_ROAD_ID", "RoadID"), ("SOURCE_LAYER", "Layer"),
                              ("SOURCE_OID", "OID")):
            review[prefix + suffix] = review[prefix + "SegmentID"].map(lookup[field])
    return review, components


def audit_origins(origins, edges, graph, components):
    node_component, _ = bridges.compute_connected_components(graph, endpoints=set())
    public = set(components.loc[components.HasPublicRoad == 1, "ComponentID"])
    geometries, tree = routing.build_edge_spatial_index(edges)
    records = []
    for number, origin in enumerate(origins.itertuples(index=False), 1):
        index, line, snap, gap, measure = routing.nearest_edge(origin.geometry, geometries, tree)
        edge = edges.iloc[index]
        component = node_component[routing.edge_nodes(edge)[0]]
        status = ("SNAP_TOO_FAR" if gap > routing.ORIGIN_MAX_SNAP_DISTANCE_M else
                  "REACHABLE" if component in public else "DISCONNECTED")
        records.append({
            "OriginRowID": number, "OriginUID": str(origin.ORIGIN_UID),
            "OriginID": str(origin.ORIGIN_ID), "OriginType": origin.ORIGIN_TYPE,
            "ComponentID": component, "SourceSegmentID": int(edge.EDGE_ID),
            "SourceRoadID": int(edge.SOURCE_ROAD_ID), "SnapDistance_m": gap,
            "Status": status, "X": origin.geometry.x, "Y": origin.geometry.y,
        })
        if number % 10000 == 0:
            LOGGER.info("Origin audit: %s / %s", number, len(origins))
    return pd.DataFrame(records)


def main():
    started = time.perf_counter()
    decision = json.loads(REVIEW_DECISION.read_text(encoding="utf-8"))
    if decision["Status"] != "SPATIAL_REVIEW_ACCEPTED_BY_USER":
        raise ValueError("A recorded spatial review is required for this regeneration.")
    if routing.MIN_PUBLIC_COMPONENT_LENGTH_M != 0:
        raise ValueError("This run expects the validated any-public-road destination rule.")
    output = ROOT / ("Bridge_QA_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    output.mkdir(exist_ok=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(output / "Run.log")])
    LOGGER.info("Output: %s", output)
    code_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "RoadUsage_Topology.py", "RoadUsage_Step2_Routing_Fast.py",
        "RoadUsage_Step2_BridgeCandidates.py", "regenerate_bridge_qa.py")}
    cache = pd.read_pickle(INPUT_CACHE)
    origins = cache["origins"]
    roads = gpd.GeoDataFrame(pd.concat([cache["private"], cache["public"]], ignore_index=True),
                             geometry="geometry", crs=cache["private"].crs)
    roads["SOURCE_ROAD_ID"] = roads.index.astype(int)
    fields = ["SOURCE_ROAD_ID", "SOURCE_LAYER", "SOURCE_OID", "ROAD_CLASS", "geometry",
              routing.GRADE_LEVEL_FIELD, *routing.GRADE_STRUCTURE_FIELDS]
    roads = roads[list(dict.fromkeys(field for field in fields if field in roads))].copy()
    del cache
    LOGGER.info("Rebuilding topology from %s cached source parts", len(roads))
    edges = routing.noded_segments(roads)
    # Retain compact prepared geometry for later QA runs; not an authoritative road layer.
    pd.to_pickle(edges, output / "Prepared_Edges.pkl")
    LOGGER.info("Saved prepared edges for reuse; building graph")
    graph = routing.build_graph(edges)
    graph_size = (graph.number_of_nodes(), graph.number_of_edges())
    candidates, components = bridges.find_nearby_bridge_candidates(
        graph, edges.crs, bridges.MAX_GAP_BRIDGE_M)
    audit = audit_origins(origins, edges, graph, components)
    previous = pd.read_csv(VALIDATION_FOLDER / "Origin_Comparison.csv",
                           dtype={"OriginUID": str, "OriginID": str})
    if (previous.OriginUID.tolist() != audit.OriginUID.tolist()
            or previous.OriginID.tolist() != audit.OriginID.tolist()
            or not (previous.ReachableCurrent.to_numpy() == audit.Status.eq("REACHABLE").astype(int).to_numpy()).all()
            or not (previous.ComponentIDCurrent.to_numpy() == audit.ComponentID.to_numpy()).all()):
        raise RuntimeError("Origin identities, reachability or component IDs differ from validated network.")
    baseline = json.loads((VALIDATION_FOLDER / "Summary.json").read_text())
    if (graph_size != (baseline["Nodes"], baseline["Edges"])
            or len(components) != baseline["Components"]):
        raise RuntimeError("Network counts differ from validated network.")
    if not candidates.Accepted.eq(0).all() or not candidates.TargetHasPublicRoad.eq(1).all():
        raise RuntimeError("Candidates must remain unaccepted and target reachable components.")
    if not candidates.GapDistance_m.between(0, bridges.MAX_GAP_BRIDGE_M, inclusive="right").all():
        raise RuntimeError("Candidate distance outside search limits.")
    review, components = annotate_review(candidates, components, audit, edges)
    priority = review[(review.CandidateRank == 1) & (review.AffectedOrigins > 0)].copy()
    priority = priority.sort_values(["AffectedOrigins", "GapDistance_m"], ascending=[False, True])
    gpkg = output / "RoadUsage_Bridge_QA.gpkg"
    bridges.write_candidate_bridge_qc(candidates, str(gpkg))
    # Always retain the primary candidate layer; optional views are omitted when empty.
    frames = {"Candidate_Review": review, "Priority_Bridges": priority}
    for status, layer in (("DISCONNECTED", "Disconnected_Origins"), ("SNAP_TOO_FAR", "Distant_Origins")):
        selected = audit[audit.Status == status].copy()
        frames[layer] = gpd.GeoDataFrame(selected, geometry=gpd.points_from_xy(selected.X, selected.Y), crs=edges.crs)
    for name, frame in frames.items():
        if not frame.empty:
            frame.to_file(gpkg, layer=name, driver="GPKG", engine="fiona", index=False)
    frames[bridges.CANDIDATE_LAYER] = candidates
    for name, frame in frames.items():
        if frame.empty and name != bridges.CANDIDATE_LAYER:
            continue
        actual = gpd.read_file(gpkg, layer=name)
        if len(actual) != len(frame) or actual.crs != frame.crs:
            raise RuntimeError(f"Round-trip failed: {name}")
        if "OriginID" in frame and actual.OriginID.tolist() != frame.OriginID.tolist():
            raise RuntimeError(f"Origin IDs changed during export: {name}")
        if "Accepted" in frame and not actual.Accepted.eq(0).all():
            raise RuntimeError(f"Acceptance status changed during export: {name}")
        if "GapDistance_m" in actual:
            if not (actual.geometry.length - actual.GapDistance_m).abs().le(1e-6).all():
                raise RuntimeError(f"Geometry and gap distance disagree: {name}")
            if not actual.GapDistance_m.between(0, bridges.MAX_GAP_BRIDGE_M, inclusive="right").all():
                raise RuntimeError(f"Exported gap outside limit: {name}")
    if graph_size != (graph.number_of_nodes(), graph.number_of_edges()):
        raise RuntimeError("QA generation changed graph size")
    components.to_csv(output / "Bridge_Component_QC.csv", index=False)
    audit.to_csv(output / "Origin_Component_QA.csv", index=False)
    priority.drop(columns="geometry").to_csv(output / "Priority_Bridges.csv", index=False)
    distant = audit[audit.Status == "SNAP_TOO_FAR"].copy()
    distant["DistanceBand"] = pd.cut(distant.SnapDistance_m,
        bins=[routing.ORIGIN_MAX_SNAP_DISTANCE_M, 250, 500, 1000, 5000, float("inf")],
        labels=["100-250m", "250-500m", "500m-1km", "1-5km", "over5km"])
    bands = distant.groupby(["OriginType", "DistanceBand"], observed=True).size().reset_index(name="Origins")
    bands.to_csv(output / "Distant_Origin_Distance_Bands.csv", index=False)
    summary = bridges.write_bridge_run_summary(
        candidates, components, str(output / "Bridge_QA_Run_Summary.json"),
        max_gap_m=bridges.MAX_GAP_BRIDGE_M, topology_tolerance_m=routing.TOPOLOGY_SNAP_TOLERANCE_M,
        input_description={"Cache": str(INPUT_CACHE), "CacheBytes": INPUT_CACHE.stat().st_size,
                           "CacheModifiedNs": INPUT_CACHE.stat().st_mtime_ns,
                           "SpatialReviewDecision": decision, "CodeSHA256": code_hashes,
                           "PreparedEdges": str(output / "Prepared_Edges.pkl")})
    affected_components = components[components.AffectedOrigins > 0]
    summary.update({
        "ValidatedAgainst": str(VALIDATION_FOLDER / "Summary.json"),
        "OriginStatusCounts": audit.Status.value_counts().to_dict(),
        "DisconnectedComponentsWithOrigins": len(affected_components),
        "AffectedComponentsWithCandidates": int(affected_components.CandidateCount.gt(0).sum()),
        "OriginsInComponentsWithCandidates": int(affected_components.loc[affected_components.CandidateCount > 0, "AffectedOrigins"].sum()),
        "PriorityCandidateCount": len(priority),
        "PriorityCandidatesNeedingTopologyReview": int(priority.ReviewType.eq("TOPOLOGY_OR_GRADE_REVIEW").sum()),
        "GapReviewNote": "Gaps within topology tolerance require topology/grade review before considering synthetic connections.",
        "BenefitNote": "Affected origin counts indicate potential benefit only; no candidate has been accepted or inserted.",
        "Seconds": time.perf_counter() - started,
    })
    (output / "Bridge_QA_Run_Summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    LOGGER.info("COMPLETE %s", json.dumps(summary))


if __name__ == "__main__":
    main()

