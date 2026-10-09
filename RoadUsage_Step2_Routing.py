"""Step 2: export network routes, origin results and explicit exceptions.

Run after Step 1 with the same --config and --run-dir. No synthetic bridges.
"""
from RoadUsage_Workflow import prepare_environment
prepare_environment()
# ArcGIS must initialize its DLLs before GeoPandas/GDAL in fresh-data CLI runs.
import sys
if __name__ == "__main__" and not any(arg in ("-h", "--help") for arg in sys.argv) and not any(arg.startswith("--reviewed-cache") for arg in sys.argv):
    import arcpy
import csv
import hashlib
import json
import logging
import math
from pathlib import Path
import time

import fiona
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, LineString, MultiLineString, mapping
import RoadUsage_Step2_Routing_Fast as engine
from RoadUsage_Workflow import arguments, load_config, config_hash, write_json

LOG = logging.getLogger(__name__)
EPS = 1e-6


def configure(config):
    r = config["routing"]
    engine.TARGET_WKID = config["target_wkid"]
    engine.TOPOLOGY_SNAP_TOLERANCE_M = r["topology_tolerance_m"]
    engine.ORIGIN_MAX_SNAP_DISTANCE_M = r["max_origin_snap_m"]
    engine.ORIGIN_WARN_DISTANCE_M = r["warn_origin_snap_m"]
    engine.GRADE_LEVEL_FIELD = r.get("grade_level_field")
    engine.GRADE_STRUCTURE_FIELDS = tuple(r.get("grade_structure_fields", []))
    engine.MIN_PUBLIC_COMPONENT_LENGTH_M = 0.0


def load_fresh_origins(config):
    import arcpy
    parts = []
    for spec in config["origins"]:
        fields = [spec["id_field"]] + [spec[k] for k in ("name_field", "company_field") if spec.get(k)]
        frame = engine.read_gdb_layer_arcpy(spec["path"], list(dict.fromkeys(fields)))
        if len(frame) != int(arcpy.management.GetCount(spec["path"])[0]):
            raise ValueError(f"Unreadable origin geometries in {spec['path']}; input correction required")
        frame = engine.validate_crs(frame, spec["origin_type"])
        if not frame.geometry.geom_type.eq("Point").all() or frame.geometry.is_empty.any():
            raise ValueError("Origins must have nonempty point geometry")
        def values(key):
            requested = spec.get(key)
            if not requested:
                return pd.Series(None, index=frame.index, dtype=object)
            field = next(c for c in frame.columns if c.lower() == requested.lower())
            return frame[field]
        out = gpd.GeoDataFrame({"ORIGIN_ID":values("id_field"), "ORIGIN_NAME":values("name_field"),
            "ORIGIN_COMPANY":values("company_field"), "ORIGIN_TYPE":spec["origin_type"],
            "SOURCE_LAYER":spec["path"], "geometry":frame.geometry}, crs=frame.crs)
        if out.ORIGIN_ID.isna().any():
            raise ValueError("Origin IDs must not be null")
        out["ORIGIN_ID"] = out.ORIGIN_ID.astype(str)
        out["ORIGIN_UID"] = out.ORIGIN_TYPE + "|" + out.ORIGIN_ID
        parts.append(out)
    if not parts:
        raise ValueError("Configure at least one origin layer")
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)


def load_inputs(config, folder, reviewed):
    if reviewed:
        snapshot = json.loads(reviewed.read_text(encoding="utf-8"))
        inputs = snapshot["Input"]
        for name in ("RoadUsage_Topology.py", "RoadUsage_Step2_Routing_Fast.py"):
            actual = hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            if actual != inputs["CodeSHA256"][name]:
                raise ValueError(f"Reviewed cache code mismatch: {name}")
        cache_path = Path(inputs["Cache"])
        stat = cache_path.stat()
        if stat.st_size != inputs["CacheBytes"] or stat.st_mtime_ns != inputs["CacheModifiedNs"]:
            raise ValueError("Reviewed input cache changed")
        if snapshot["TopologySnapTolerance_m"] != engine.TOPOLOGY_SNAP_TOLERANCE_M:
            raise ValueError("Reviewed topology tolerance differs from configuration")
        if snapshot["CRS"] != f"EPSG:{config['target_wkid']}" or engine.ORIGIN_MAX_SNAP_DISTANCE_M != 100:
            raise ValueError("Reviewed snapshot requires its original CRS and 100 m snap limit")
        LOG.info("Loading explicitly selected reviewed network: %s", reviewed)
        cache = pd.read_pickle(cache_path)
        origins = cache["origins"]
        edges = pd.read_pickle(inputs["PreparedEdges"])
        source_info = {"Mode":"REVIEWED_SNAPSHOT", "Snapshot":str(reviewed.resolve()),
                       "OriginMetadata":"Snapshot retains IDs and types; names and companies unavailable"}
        return edges, origins, source_info, snapshot
    marker = folder / "Step1_Complete.json"
    if not marker.exists():
        raise FileNotFoundError("Run Step 1 first with this run folder, or explicitly select --reviewed-cache")
    first = json.loads(marker.read_text())
    if first["ConfigSHA256"] != config_hash(config):
        raise ValueError("Configuration differs from the completed Step 1 run")
    engine.OUT_GDB = first["PreparedGDB"]
    private, public = engine.read_roads()
    roads = gpd.GeoDataFrame(pd.concat([private, public], ignore_index=True), crs=private.crs)
    roads["SOURCE_ROAD_ID"] = roads.index.astype(int)
    # Only retain fields needed to route and identify source segments.
    wanted = ["SOURCE_ROAD_ID", "SOURCE_LAYER", "SOURCE_OID", "ROAD_CLASS", "geometry",
              engine.GRADE_LEVEL_FIELD, *engine.GRADE_STRUCTURE_FIELDS]
    roads = roads[list(dict.fromkeys(c for c in wanted if c in roads))]
    edges = engine.noded_segments(roads)
    origins = load_fresh_origins(config)
    return edges, origins, {"Mode":"FRESH_STEP1", "PreparedGDB":engine.OUT_GDB}, None


def route_one(point, edge, snap, measure, graph, destinations, distance, parent, super_node):
    """Return oriented road pieces and destination edge, or None if disconnected."""
    u, v = engine.edge_nodes(edge)
    if edge.get("ROAD_CLASS") == "PUBLIC" and u in destinations and v in destinations:
        return [], 0.0, edge, snap
    choices = [(measure + distance.get(u, math.inf), u),
               (edge.geometry.length - measure + distance.get(v, math.inf), v)]
    length, endpoint = min(choices, key=lambda item: item[0])
    if math.isinf(length):
        return None
    nodes = engine.reconstruct_route_nodes(endpoint, parent, super_node)
    if not nodes or nodes[-1] not in destinations:
        raise RuntimeError("Shortest path did not end at a public destination")
    pieces = []
    partial = engine.make_partial_snap_segment(edge, snap, measure, endpoint)
    if partial is not None:
        pieces.append((int(edge.EDGE_ID), int(edge.SOURCE_ROAD_ID), partial))
    for a, b in zip(nodes[:-1], nodes[1:]):
        _, data = engine.select_edge_between(graph, a, b)
        line = data["geometry"]
        if engine.edge_nodes(data)[0] != a:
            line = engine.reverse_line(line)
        pieces.append((int(data["EDGE_ID"]), int(data["SOURCE_ROAD_ID"]), line))
    public = [data for neighbor in graph[nodes[-1]].values() for data in neighbor.values()
              if data.get("ROAD_CLASS") == "PUBLIC"]
    destination = min(public, key=lambda data: int(data["EDGE_ID"]))
    end = Point(pieces[-1][2].coords[-1]) if pieces else snap
    actual = sum(line.length for _, _, line in pieces)
    if abs(actual - length) > max(EPS, length * 1e-9):
        raise RuntimeError("Route geometry length does not match Dijkstra distance")
    prior = snap
    for _, _, line in pieces:
        if prior.distance(Point(line.coords[0])) > EPS:
            raise RuntimeError("Route contains a geometry gap")
        prior = Point(line.coords[-1])
    if end.distance(destination["geometry"]) > EPS:
        raise RuntimeError("Route endpoint does not touch its public destination road")
    return pieces, actual, destination, end


def string(value):
    return None if value is None or pd.isna(value) else str(value)


def export_routes(graph, edges, origins, folder):
    destinations, _ = engine.public_destination_nodes(graph)
    distance, parent, super_node = engine.precompute_destination_tree(graph, destinations)
    geoms, tree = engine.build_edge_spatial_index(edges)
    gpkg = folder / "RoadUsage_Routes.gpkg"
    schema = {"geometry":"MultiLineString", "properties":{"RouteID":"str", "OriginUID":"str",
              "OriginID":"str", "OriginType":"str", "TotalRoute_m":"float", "TotalRoute_km":"float",
              "SnapDistance_m":"float", "DestinationRoadID":"int"}}
    records, point_geoms, end_records, end_geoms, links = [], [], [], [], []
    segment_count = 0
    with fiona.open(gpkg, "w", driver="GPKG", layer="Generated_Routes", schema=schema,
                    crs_wkt=edges.crs.to_wkt()) as output, (folder / "Route_Segments.csv").open("w",newline="",encoding="utf-8") as segfile:
        segments = csv.writer(segfile)
        segments.writerow(["RouteID","Sequence","EdgeID","SourceRoadID","Distance_m"])
        for rownum, origin in enumerate(origins.itertuples(index=False), 1):
            point = origin.geometry
            idx, _, snap, gap, measure = engine.nearest_edge(point, geoms, tree)
            edge = edges.iloc[idx]
            routeid = f"R{rownum:07d}"
            record = {"RouteID":routeid, "OriginUID":str(origin.ORIGIN_UID), "OriginID":str(origin.ORIGIN_ID),
                "OriginType":str(origin.ORIGIN_TYPE), "OriginName":string(getattr(origin,"ORIGIN_NAME",None)),
                "OriginCompany":string(getattr(origin,"ORIGIN_COMPANY",None)),
                "SnapDistance_m":gap, "SnapQC":"REVIEW" if gap > engine.ORIGIN_WARN_DISTANCE_M else "OK",
                "SnapSourceRoadID":int(edge.SOURCE_ROAD_ID), "RouteStatus":"UNROUTABLE_DISCONNECTED",
                "TotalRoute_m":None, "TotalRoute_km":None, "DestinationRoadID":None,
                "DestinationSourceLayer":None, "DestinationSourceOID":None,
                "DestinationRoadName":None, "Reason":"NO_PUBLIC_ROAD_IN_COMPONENT"}
            routed = None
            if gap > engine.ORIGIN_MAX_SNAP_DISTANCE_M:
                record.update(RouteStatus="UNROUTED_SNAP_TOO_FAR", SnapQC="FAIL_TOO_FAR", Reason="ORIGIN_EXCEEDS_SNAP_LIMIT")
            else:
                routed = route_one(point, edge, snap, measure, graph, destinations, distance, parent, super_node)
            if routed is not None:
                pieces, length, dest, end = routed
                record.update(RouteStatus="ROUTED" if pieces else "ALREADY_PUBLIC", TotalRoute_m=length,
                    TotalRoute_km=length/1000, DestinationRoadID=int(dest["SOURCE_ROAD_ID"]),
                    DestinationSourceLayer=string(dest.get("SOURCE_LAYER")),
                    DestinationSourceOID=int(dest["SOURCE_OID"]), Reason=None)
                if pieces:
                    output.write({"geometry":mapping(MultiLineString([p[2] for p in pieces])),
                                  "properties":{k:record[k] for k in schema["properties"]}})
                    for seq, (edgeid, roadid, line) in enumerate(pieces,1):
                        segments.writerow([routeid,seq,edgeid,roadid,line.length])
                        segment_count += 1
                end_records.append({"RouteID":routeid,"DestinationRoadID":record["DestinationRoadID"]})
                end_geoms.append(end)
                if gap > EPS:
                    links.append({"RouteID":routeid,"SnapDistance_m":gap,"VerifiedAccess":0,
                                  "geometry":LineString([point,snap])})
            records.append(record); point_geoms.append(point)
            if rownum % 5000 == 0:
                LOG.info("Processed %s / %s origins", rownum, len(origins))
    summary = pd.DataFrame(records)
    if len(summary) != len(origins) or not summary.RouteID.is_unique:
        raise RuntimeError("Origin accounting failed")
    summary.to_csv(folder / "Route_Summary.csv",index=False)
    points = gpd.GeoDataFrame(summary,geometry=point_geoms,crs=edges.crs)
    frames = {"Origin_Results":points, "Unroutable_Origins":points[points.Reason.notna()]}
    if end_records:
        frames["Public_Road_Arrivals"] = gpd.GeoDataFrame(end_records,geometry=end_geoms,crs=edges.crs)
    if links:
        frames["Origin_Snap_Links"] = gpd.GeoDataFrame(links,geometry="geometry",crs=edges.crs)
    for layer, frame in frames.items():
        if not frame.empty:
            frame.to_file(gpkg,layer=layer,driver="GPKG",engine="fiona",index=False)
    with fiona.open(gpkg,layer="Generated_Routes") as check:
        if len(check) != int(summary.RouteStatus.eq("ROUTED").sum()):
            raise RuntimeError("Exported route count mismatch")
        for feature in check:
            from shapely.geometry import shape
            if abs(shape(feature["geometry"]).length-feature["properties"]["TotalRoute_m"]) > EPS:
                raise RuntimeError("Exported geometry length mismatch")
    for layer, frame in frames.items():
        if not frame.empty:
            with fiona.open(gpkg,layer=layer) as check:
                if len(check) != len(frame): raise RuntimeError(f"Export count mismatch: {layer}")
    return summary, segment_count


def main():
    args = arguments("Step 2: create routes and exceptions", reviewed=True)
    config = load_config(args.config); configure(config)
    folder = args.run_dir.resolve(); folder.mkdir(parents=True,exist_ok=True)
    if (folder / "RoadUsage_Routes.gpkg").exists() or (folder / "Step2_Complete.json").exists():
        raise FileExistsError("Route outputs already exist; choose a new run folder")
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(),logging.FileHandler(folder / "Step2.log")])
    started = time.perf_counter()
    edges, origins, inputs, snapshot = load_inputs(config, folder, args.reviewed_cache)
    if edges.empty or origins.empty: raise ValueError("Roads and origins must be nonempty")
    if edges.crs != origins.crs or not edges.crs.is_projected:
        raise ValueError("Roads and origins must share a projected CRS")
    if any(abs(axis.unit_conversion_factor-1)>1e-9 for axis in edges.crs.axis_info):
        raise ValueError("Routing CRS units must be metres")
    graph = engine.build_graph(edges)
    if snapshot and (graph.number_of_nodes()!=snapshot["Nodes"] or graph.number_of_edges()!=snapshot["Edges"]):
        raise ValueError("Reviewed graph counts changed")
    summary, segment_count = export_routes(graph,edges,origins,folder)
    reachable = summary.RouteStatus.isin(["ROUTED","ALREADY_PUBLIC"])
    if snapshot:
        audit_path = args.reviewed_cache.parent / "Origin_Component_QA.csv"
        audit = pd.read_csv(audit_path,dtype={"OriginUID":str,"OriginID":str})
        if (summary.OriginUID.tolist()!=audit.OriginUID.tolist()
            or summary.OriginID.tolist()!=audit.OriginID.tolist()
            or reachable.tolist()!=audit.Status.eq("REACHABLE").tolist()):
            raise RuntimeError("Route results differ from reviewed reachability; delivery is incomplete")
    write_json(folder / "Step2_Complete.json", {"Status":"COMPLETE","ConfigSHA256":config_hash(config),
        "Input":inputs,"Origins":len(origins),"ReachableOrigins":int(reachable.sum()),
        "StatusCounts":summary.RouteStatus.value_counts().to_dict(),"RouteSegments":segment_count,
        "CRS":str(edges.crs),"SyntheticBridgesAdded":0,"Seconds":time.perf_counter()-started,
        "DistanceDefinition":"TotalRoute_m/km measures travel from the snapped network point to public road. SnapDistance_m is separate and unverified; it is not included in total route distance.",
        "ZeroLengthRoutes":"ALREADY_PUBLIC origins are in Origin_Results and Public_Road_Arrivals; no fictitious zero-length line is written.",
        "Config":config})
    LOG.info("COMPLETE: %s",summary.RouteStatus.value_counts().to_dict())


if __name__ == "__main__":
    main()
