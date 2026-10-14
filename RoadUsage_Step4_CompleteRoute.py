"""Step 4: one continuous network-route feature with disposition list columns.

Run after Steps 2 and 3 with the same --config and --run-dir. The output is a
separate GeoPackage whose layer appears in ArcGIS Pro as main.CompleteRoute.
"""

from RoadUsage_Workflow import prepare_environment
prepare_environment()

import argparse
import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

import fiona
from shapely.geometry import LineString, Point, mapping, shape

from RoadUsage_Workflow import DEFAULT_CONFIG, config_hash, load_config, write_json


LOG = logging.getLogger(__name__)
OUTPUT_NAME = "RoadUsage_CompleteRoute.gpkg"
LAYER_NAME = "CompleteRoute"
LIST_FIELDS = ("DispositionID", "DispositionOwnerName", "OwnerConflict",
               "DistanceTravelled_m", "DistanceTravelled_km", "PercentOfRoute")
SCHEMA = {"geometry": "LineString", "properties": {
    "RouteID": "str", "OriginUID": "str", "OriginID": "str", "OriginType": "str",
    "OriginName": "str", "OriginCompany": "str", "RouteStatus": "str",
    "DestinationRoadID": "int", "DestinationRoadName": "str",
    "TotalRoute_m": "float", "TotalRoute_km": "float", "SnapDistance_m": "float",
    "DispositionID": "str", "DispositionOwnerName": "str", "OwnerConflict": "str",
    "DistanceTravelled_m": "str", "DistanceTravelled_km": "str",
    "PercentOfRoute": "str", "DispositionCount": "int",
    "DispositionAttributed_km": "float", "Unattributed_km": "float",
    "Overlap_km": "float", "OverlapQC": "str",
}}


def read_rows(path):
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def indexed_rows(path):
    rows = {}
    for row in read_rows(path):
        route_id = row["RouteID"]
        if route_id in rows:
            raise ValueError(f"Duplicate RouteID in {path.name}: {route_id}")
        rows[route_id] = row
    return rows


def as_number(value):
    return None if value is None or str(value).strip() == "" else float(value)


def as_integer(value):
    number = as_number(value)
    if number is None or not number.is_integer():
        raise ValueError(f"Expected an integer identifier, got {value!r}")
    return int(number)


def as_text(value):
    return None if value is None or str(value).strip() == "" else str(value)


def complete_line(geometry):
    """Join the already ordered network parts without adding a snap connector."""
    route = shape(geometry)
    if route.geom_type == "LineString":
        parts = [route]
    elif route.geom_type == "MultiLineString":
        parts = list(route.geoms)
    else:
        raise ValueError(f"Unexpected route geometry: {route.geom_type}")
    coords = []
    for part in parts:
        current = list(part.coords)
        if len(current) < 2:
            raise ValueError("Route contains an empty line part")
        if coords and Point(coords[-1]).distance(Point(current[0])) > 1e-6:
            raise ValueError("Route parts are not connected in travel order")
        coords.extend(current if not coords else current[1:])
    if len(coords) < 2:
        raise ValueError("Route has no line geometry")
    return LineString(coords)


def list_properties(details):
    """Keep each disposition's values at the same position in all six lists."""
    details = sorted(details, key=lambda row: row["DispositionID"])
    if len({row["DispositionID"] for row in details}) != len(details):
        raise ValueError("A route has duplicate disposition IDs in its detail report")
    values = {
        "DispositionID": [row["DispositionID"] for row in details],
        "DispositionOwnerName": [as_text(row["DispositionOwnerName"]) for row in details],
        "OwnerConflict": [int(row["OwnerConflict"]) for row in details],
        "DistanceTravelled_m": [float(row["DistanceTravelled_m"]) for row in details],
        "DistanceTravelled_km": [float(row["DistanceTravelled_km"]) for row in details],
        "PercentOfRoute": [float(row["PercentOfRoute"]) for row in details],
    }
    return {name: json.dumps(values[name], ensure_ascii=False, separators=(",", ":"))
            for name in LIST_FIELDS}


def create_complete_routes(folder, config):
    step2 = json.loads((folder / "Step2_Complete.json").read_text(encoding="utf-8"))
    step3 = json.loads((folder / "Step3_Complete.json").read_text(encoding="utf-8"))
    if (step2.get("Status") != "COMPLETE" or step3.get("Status") != "COMPLETE"
            or step2.get("ConfigSHA256") != config_hash(config)):
        raise ValueError("Steps 2 and 3 must be complete with this configuration")

    output_path = folder / OUTPUT_NAME
    marker = folder / "Step4_Complete.json"
    if output_path.exists() or marker.exists():
        raise FileExistsError(f"CompleteRoute output already exists in {folder}")

    summaries = indexed_rows(folder / "Route_Summary.csv")
    qc = indexed_rows(folder / "Route_Disposition_QC.csv")
    details = defaultdict(list)
    for row in read_rows(folder / "Route_Disposition_Detail.csv"):
        details[row["RouteID"]].append(row)
    routed_ids = {key for key, row in summaries.items() if row["RouteStatus"] == "ROUTED"}
    if set(details) - routed_ids:
        raise ValueError("Disposition details refer to a route without line geometry")
    if routed_ids - set(qc):
        raise ValueError("A routed origin is missing its disposition QC row")

    source_path = folder / "RoadUsage_Routes.gpkg"
    exported = set()
    with fiona.open(source_path, layer="Generated_Routes") as source:
        if not source.crs_wkt:
            raise ValueError("Generated_Routes has no coordinate system")
        with fiona.open(output_path, "w", driver="GPKG", layer=LAYER_NAME,
                        schema=SCHEMA, crs_wkt=source.crs_wkt) as output:
            for feature in source:
                route_id = feature["properties"]["RouteID"]
                if route_id in exported or route_id not in routed_ids:
                    raise ValueError(f"Unexpected or duplicate generated route: {route_id}")
                summary, totals = summaries[route_id], qc[route_id]
                line = complete_line(feature["geometry"])
                total_m = float(summary["TotalRoute_m"])
                if abs(line.length - total_m) > max(1e-6, total_m * 1e-9):
                    raise ValueError(f"CompleteRoute length differs from Route_Summary: {route_id}")
                properties = {
                    "RouteID": route_id,
                    "OriginUID": as_text(summary.get("OriginUID")),
                    "OriginID": summary["OriginID"],
                    "OriginType": summary["OriginType"],
                    "OriginName": as_text(summary.get("OriginName")),
                    "OriginCompany": as_text(summary.get("OriginCompany")),
                    "RouteStatus": summary["RouteStatus"],
                    "DestinationRoadID": as_integer(summary["DestinationRoadID"]),
                    "DestinationRoadName": as_text(summary.get("DestinationRoadName")),
                    "TotalRoute_m": total_m,
                    "TotalRoute_km": float(summary["TotalRoute_km"]),
                    "SnapDistance_m": as_number(summary.get("SnapDistance_m")),
                    "DispositionCount": len(details[route_id]),
                    "DispositionAttributed_km": float(totals["DispositionAttributed_km"]),
                    "Unattributed_km": float(totals["Unattributed_km"]),
                    "Overlap_km": float(totals["Overlap_km"]),
                    "OverlapQC": totals["OverlapQC"],
                }
                properties.update(list_properties(details[route_id]))
                output.write({"geometry": mapping(line), "properties": properties})
                exported.add(route_id)
                if len(exported) % 5000 == 0:
                    LOG.info("Wrote %s / %s complete routes", len(exported), len(routed_ids))
    if exported != routed_ids:
        raise ValueError(f"CompleteRoute is missing {len(routed_ids - exported)} routed origins")
    with fiona.open(output_path, layer=LAYER_NAME) as output:
        if len(output) != len(routed_ids):
            raise ValueError("CompleteRoute output feature count mismatch")
    write_json(marker, {"Status": "COMPLETE", "ConfigSHA256": config_hash(config),
                        "GeoPackage": str(output_path), "Layer": LAYER_NAME,
                        "RouteCount": len(exported),
                        "ZeroLengthPublicOrigins": sum(row["RouteStatus"] == "ALREADY_PUBLIC"
                                                       for row in summaries.values()),
                        "Geometry": "Network route from snapped road point to public road; unverified snap link excluded",
                        "DispositionLists": "JSON arrays of values aligned by position across disposition columns"})
    LOG.info("CompleteRoute output: %s (%s features)", output_path, len(exported))
    return output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    create_complete_routes(args.run_dir.resolve(), load_config(args.config))


if __name__ == "__main__":
    main()
