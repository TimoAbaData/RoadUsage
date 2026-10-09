"""Export regression review context from completed QA; never repair the graph."""
import json
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.strtree import STRtree


OUTPUT_FOLDER = Path(__file__).resolve().parent
COMPARISON_CSV = OUTPUT_FOLDER / "RoadUsage_Topology_QA_Origin_Comparison.csv"
TOPOLOGY_QA = OUTPUT_FOLDER / "RoadUsage_Topology_QA.gpkg"
SOURCE_LOOKUP = OUTPUT_FOLDER / "Topology_Road_Source_Lookup.csv"
REVIEW_GPKG = OUTPUT_FOLDER / "Topology_Regression_Review.gpkg"
SUMMARY_JSON = OUTPUT_FOLDER / "Topology_Regression_Review_Summary.json"
REVIEW_RADIUS_M = 2000.0  # Context selection only, never a connection tolerance.


def main():
    comparison = pd.read_csv(COMPARISON_CSV, dtype={"OriginID": str, "OriginUID": str})
    held = comparison[(comparison.ReachableDestinationBefore == 1) &
                      (comparison.ReachableDestinationAfter == 0)].copy()
    # Read CRS from the completed QA instead of assuming a different projection.
    issues = gpd.read_file(TOPOLOGY_QA, layer="Topology_Gaps")
    if issues.crs is None or not issues.crs.is_projected or any(
        abs(axis.unit_conversion_factor - 1.0) > 1e-9 for axis in issues.crs.axis_info[:2]
    ):
        raise ValueError("Regression review requires projected metre-based QA.")
    origins = gpd.GeoDataFrame(
        held, geometry=gpd.points_from_xy(held.OriginXAfter, held.OriginYAfter), crs=issues.crs)
    suspects = issues[(issues.RepairApplied == 0) & issues.IssueType.isin([
        "INTERSECTION_NOT_NODED", "TOUCH_NOT_NODED", "COORDINATE_MISMATCH",
        "ENDPOINT_NEAR_ROAD", "SELF_INTERSECTION_REVIEW",
    ])].copy()
    tree = STRtree(list(suspects.geometry)) if len(suspects) else None
    indices = set()
    for point in origins.geometry:
        if tree is not None:
            indices.update(int(i) for i in tree.query(
                point.buffer(REVIEW_RADIUS_M), predicate="intersects"))
    nearby = suspects.iloc[sorted(indices)].copy()
    origin_tree = STRtree(list(origins.geometry)) if len(origins) else None
    associations = []
    for point in nearby.geometry:
        origin = origins.iloc[int(origin_tree.nearest(point))]
        associations.append((origin.OriginID, int(origin.ComponentIDAfter),
                             float(point.distance(origin.geometry))))
    nearby["NearestHeldOriginID"] = [a[0] for a in associations]
    nearby["NearestHeldComponentID"] = [a[1] for a in associations]
    nearby["OriginDistance_m"] = [a[2] for a in associations]
    nearby["Assessment"] = "NEARBY_ISSUE_CAUSAL_LINK_UNCONFIRMED"
    origins["ReviewStatus"] = "EXISTING_ROAD_CONNECTIVITY_REVIEW"
    lookup = pd.read_csv(SOURCE_LOOKUP, usecols=[
        "SourceRoadID", "SourceLayer", "SourceOID"]).set_index("SourceRoadID")
    for prefix in ("Source", "Target"):
        nearby[prefix + "Layer"] = nearby[prefix + "RoadID"].map(lookup.SourceLayer)
        nearby[prefix + "OID"] = nearby[prefix + "RoadID"].map(lookup.SourceOID).astype("Int64")

    origins.to_file(REVIEW_GPKG, layer="Held_Origins", driver="GPKG", engine="fiona", index=False)
    schema = {"geometry": "Point", "properties": {
        "IssueID": "int", "IssueType": "str", "SourceRoadID": "int", "TargetRoadID": "int",
        "SourceRoadClass": "str", "TargetRoadClass": "str", "Distance_m": "float",
        "SameComponentBefore": "int", "SameComponentAfter": "int", "RepairApplied": "int",
        "Reason": "str", "NearestHeldOriginID": "str", "NearestHeldComponentID": "int",
        "OriginDistance_m": "float", "Assessment": "str", "SourceLayer": "str", "TargetLayer": "str",
        "SourceOID": "int", "TargetOID": "int",
    }}
    for column, kind in schema["properties"].items():
        if kind == "int":
            nearby[column] = pd.array(nearby[column], dtype="Int64")
    nearby[list(schema["properties"]) + ["geometry"]].to_file(
        REVIEW_GPKG, layer="Nearby_Unrepaired_Issues", driver="GPKG", engine="fiona",
        schema=schema, index=False)
    groups = held.groupby("ComponentIDAfter").agg(
        Origins=("OriginID", "size"), NodeCount=("ComponentNodeCountAfter", "first"),
        EdgeCount=("ComponentEdgeCountAfter", "first"))
    summary = {
        "GeneratedUTC": datetime.now(timezone.utc).isoformat(),
        "SourceComparison": str(COMPARISON_CSV), "SourceTopologyQA": str(TOPOLOGY_QA),
        "CRS": origins.crs.to_string(), "ReviewRadius_m": REVIEW_RADIUS_M,
        "ReviewRadiusPurpose": "Select context only; does not create a connection",
        "HeldOrigins": len(held),
        "Components": {str(int(k)): {name: int(v) for name, v in values.items()}
                       for k, values in groups.to_dict("index").items()},
        "SameSourceRoadBeforeAfter": int((held.SourceRoadIDBefore == held.SourceRoadIDAfter).sum()),
        "AmbiguousOriginSnapsAfter": int(held.SnapAmbiguousAfter.sum()),
        "MaxSnapDistanceChange_m": float((held.SnapDistance_mAfter - held.SnapDistance_mBefore).abs().max())
                                   if len(held) else None,
        "NearbyUnrepairedIssues": len(nearby), "IssueReasons": nearby.Reason.value_counts().to_dict(),
        "Status": "PENDING_ROAD_AND_GRADE_REVIEW",
        "Caution": "Nearby issues are context; proximity does not prove the cause or an at-grade junction.",
        "AutomaticallyAddedConnectors": 0,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    # Verify the actual GIS artifact and preservation of leading-zero origin IDs.
    read_origins = gpd.read_file(REVIEW_GPKG, layer="Held_Origins")
    read_issues = gpd.read_file(REVIEW_GPKG, layer="Nearby_Unrepaired_Issues")
    if (len(read_origins) != len(held) or set(read_origins.OriginID) != set(held.OriginID)
            or len(read_issues) != len(nearby) or not (read_issues.RepairApplied == 0).all()
            or read_origins.crs != origins.crs or read_issues.crs != origins.crs):
        raise RuntimeError("Regression review export failed round-trip verification.")
    print(json.dumps(summary, indent=2))
    print("QA round-trip verified:", REVIEW_GPKG)


if __name__ == "__main__":
    main()
