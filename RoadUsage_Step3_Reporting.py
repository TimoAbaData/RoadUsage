"""Optional Step 3: actual route/disposition overlap and ownership summaries."""
from RoadUsage_Workflow import prepare_environment
prepare_environment()
# ArcGIS must initialize its DLLs before GeoPandas/GDAL in fresh-data CLI runs.
import sys
if __name__ == "__main__" and not any(arg in ("-h", "--help") for arg in sys.argv):
    import arcpy
# Reassert the clone's PROJ settings after ArcPy initializes, before GeoPandas imports.
prepare_environment()
import argparse
import csv
from itertools import groupby
import json
import logging
import shutil
from datetime import datetime as timestamp
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.strtree import STRtree
import RoadUsage_Step2_Routing_Fast as engine
from RoadUsage_DispositionContinuity import merge_spans, smooth_short_intervals
from RoadUsage_Workflow import DEFAULT_CONFIG, load_config, config_hash, disposition_filter_layer, write_json

LOG = logging.getLogger(__name__)


def merge_intervals(intervals):
    """Union one-dimensional travelled distances, avoiding geometric slivers."""
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1e-7:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end-start for start,end in merged)


def line_parts(geometry):
    if geometry.geom_type == "LineString":
        if geometry.is_ring or not geometry.is_simple:
            from shapely.geometry import LineString
            coords = list(geometry.coords)
            return [LineString([a,b]) for a,b in zip(coords[:-1],coords[1:]) if a[:2]!=b[:2]]
        return [geometry]
    return [line for part in getattr(geometry,"geoms",[]) for line in line_parts(part)]


def attribute_route(geometry, dispositions, tree, id_field, owner_field=None, cache=None, minimum_overlap_m=0.0, protected_intervals=None):
    """Measure polygon coverage as intervals on each travelled route part.

    A per-run cache reuses intersections on roads shared by many routes. Keep
    the cache local to one disposition dataset. No polygons or roads are edited.
    """
    from shapely.geometry import Point
    if cache is None: cache = {}
    parts = line_parts(geometry)
    keys = [part.wkb for part in parts]
    lengths = [part.length for part in parts]
    offsets = []
    travelled = 0.0
    for length in lengths:
        offsets.append(travelled)
        travelled += length
    intervals_by_id, owners = {}, {}
    for part_raw, polygon_raw in tree.query(parts, predicate="intersects").T:
        part_idx, polygon_idx = int(part_raw), int(polygon_raw)
        meta_key = ("meta", polygon_idx)
        if meta_key not in cache:
            row = dispositions.iloc[polygon_idx]
            owner = row.get(owner_field) if owner_field else None
            owner = str(owner) if owner is not None and pd.notna(owner) and str(owner).strip() else None
            cache[meta_key] = (str(row[id_field]), owner, row.geometry)
        disp_id, owner, polygon = cache[meta_key]
        clip_key = (keys[part_idx], polygon_idx)
        if clip_key not in cache:
            line = parts[part_idx]
            intersection = line.intersection(polygon)
            intervals = []
            for clipped in line_parts(intersection):
                if clipped.length <= 1e-7: continue
                a = float(line.project(Point(clipped.coords[0])))
                b = float(line.project(Point(clipped.coords[-1])))
                low, high = sorted((a, b))
                low, high = max(0.0, low), min(lengths[part_idx], high)
                if high - low > 1e-7: intervals.append((low, high))
            cache[clip_key] = intervals
        intervals = cache[clip_key]
        if not intervals: continue
        intervals_by_id.setdefault(disp_id, []).extend(
            (offsets[part_idx] + low, offsets[part_idx] + high)
            for low, high in intervals)
        if owner: owners.setdefault(disp_id, set()).add(owner)
    protected_intervals = protected_intervals or {}
    for disp_id, intervals in protected_intervals.items():
        intervals_by_id.setdefault(disp_id, []).extend(intervals)
    effective = smooth_short_intervals(intervals_by_id, minimum_overlap_m,
                                       protected_by_label=protected_intervals)
    details = []
    for disp_id, intervals in effective.items():
        length = sum(end - start for start, end in intervals)
        names = sorted(owners.get(disp_id, set()))
        details.append({"DispositionID":disp_id,"DispositionOwnerName":" | ".join(names) or None,
                        "OwnerConflict":int(len(names)>1),"DistanceTravelled_m":length,
                        "DistanceTravelled_km":length/1000})
    attributed = sum(end - start for start, end in merge_spans(
        [span for spans in effective.values() for span in spans]))
    overlap = max(0.0, sum(d["DistanceTravelled_m"] for d in details) - attributed)
    return details, attributed, overlap



def route_segment_protections(path):
    """Stream ordered Step 2 segments as effective Step 1 disposition spans."""
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if "DispositionID" not in (reader.fieldnames or []):
            raise ValueError("Route_Segments.csv lacks disposition attributes; rerun Step 2")
        for route_id, rows in groupby(reader, key=lambda row: row["RouteID"]):
            offset = 0.0
            protected = {}
            for expected, row in enumerate(rows, 1):
                if int(row["Sequence"]) != expected:
                    raise ValueError(f"Route segment sequence mismatch: {route_id}")
                length = float(row["Distance_m"])
                if length < 0:
                    raise ValueError(f"Negative route segment distance: {route_id}")
                disp_id = (row["DispositionID"] or "").strip()
                if disp_id and length > 0:
                    protected.setdefault(disp_id, []).append((offset, offset + length))
                offset += length
            yield route_id, protected, offset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--dispositions",type=Path,help="Explicit polygon feature class; defaults to Step 1 Disposition_Polygons")
    parser.add_argument("--replace-report",action="store_true",help="Back up and regenerate existing report files")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    folder = args.run_dir.resolve()
    manifest = json.loads((folder / "Step2_Complete.json").read_text())
    if manifest["Status"]!="COMPLETE" or manifest["ConfigSHA256"]!=config_hash(config):
        raise ValueError("Step 2 must be complete using the same configuration")
    if (folder / "Route_Disposition_Detail.csv").exists():
        if not args.replace_report:
            raise FileExistsError("Report exists; use --replace-report to back it up before regenerating")
        backup = folder / ("Report_Backup_" + timestamp.now().strftime("%Y%m%d_%H%M%S"))
        backup.mkdir(exist_ok=False)
        for name in ("Route_Disposition_Detail.csv", "Route_Disposition_QC.csv", "Step3_Complete.json",
                     "Dispositions_Used_By_Me.csv", "Who_Used_My_Disposition.csv"):
            if (folder / name).exists(): shutil.copy2(folder / name, backup / name)
    write_json(folder / "Step3_Complete.json", {"Status":"RUNNING"})
    source = args.dispositions or folder / "Prepared_Roads.gdb" / "Disposition_Polygons"
    engine.TARGET_WKID = config["target_wkid"]
    settings = config.get("reporting",{})
    id_field = settings.get("disposition_id_field","DISP_NUM")
    owner_field = settings.get("owner_field")
    import arcpy
    selected_source, source_count, selected_count = disposition_filter_layer(
        arcpy, source, config["disposition_filter"], "step3_filtered_dispositions")
    LOG.info("Disposition filter selected %s / %s features: %s",
             selected_count, source_count, config["disposition_filter"])
    available = {field.name.lower():field.name for field in arcpy.ListFields(selected_source)}
    if id_field.lower() not in available:
        raise ValueError(f"Disposition ID field not found: {id_field}")
    id_field = available[id_field.lower()]
    owner_field = available.get(owner_field.lower()) if owner_field else None
    requested = [id_field] + ([owner_field] if owner_field else [])
    polygons = engine.validate_crs(engine.read_gdb_layer_arcpy(str(selected_source), requested), "Dispositions")
    arcpy.management.Delete(selected_source)
    if len(polygons) != selected_count:
        raise ValueError("Unreadable disposition geometry; refusing incomplete attribution")
    if id_field not in polygons or polygons[id_field].isna().any():
        raise ValueError("Disposition identifier field is missing or contains nulls")
    if not polygons.geometry.geom_type.isin(["Polygon","MultiPolygon"]).all() or not polygons.geometry.is_valid.all():
        raise ValueError("Reporting needs valid disposition polygons, not open boundary lines")
    if owner_field and owner_field not in polygons:
        LOG.warning("Owner field %s unavailable; ownership will remain blank",owner_field)
        owner_field=None
    routes = gpd.read_file(folder / "RoadUsage_Routes.gpkg",layer="Generated_Routes")
    results = pd.read_csv(folder / "Route_Summary.csv",dtype={"OriginID":str,"OriginUID":str})
    lookup = results.set_index("RouteID")
    tree = STRtree(list(polygons.geometry))
    detail, totals = [], []
    intersection_cache = {}
    segment_groups = route_segment_protections(folder / "Route_Segments.csv")
    for number,route in enumerate(routes.itertuples(index=False),1):
        route_id, protected, segment_length = next(segment_groups)
        if route_id != route.RouteID or abs(segment_length - route.TotalRoute_m) > 1e-5:
            raise ValueError(f"Route segments do not match geometry: {route.RouteID}")
        records,attributed,overlap = attribute_route(
            route.geometry,polygons,tree,id_field,owner_field,intersection_cache,
            config.get("min_disposition_overlap_m",0.0),protected)
        if attributed > route.TotalRoute_m + 1e-6 or any(r["DistanceTravelled_m"] > route.TotalRoute_m + 1e-6 for r in records):
            raise RuntimeError(f"Disposition attribution exceeds route distance: {route.RouteID}")
        origin = lookup.loc[route.RouteID]
        for record in records:
            record.update(RouteID=route.RouteID,OriginID=origin.OriginID,OriginType=origin.OriginType,
                OriginName=origin.OriginName,OriginCompany=origin.OriginCompany,TotalRoute_km=route.TotalRoute_km,
                PercentOfRoute=100*record["DistanceTravelled_m"]/route.TotalRoute_m if route.TotalRoute_m else 0)
            detail.append(record)
        totals.append({"RouteID":route.RouteID,"TotalRoute_km":route.TotalRoute_km,
            "DispositionAttributed_km":attributed/1000,"Unattributed_km":max(0,route.TotalRoute_m-attributed)/1000,
            "Overlap_km":overlap/1000,"OverlapQC":"REVIEW_OVERLAPPING_DISPOSITIONS" if overlap>1e-6 else "OK"})
        if number%1000==0: LOG.info("Attributed %s / %s routes",number,len(routes))
    if next(segment_groups, None) is not None:
        raise ValueError("Route segments contain a route missing from Generated_Routes")
    for row in results[results.RouteStatus.eq("ALREADY_PUBLIC")].itertuples(index=False):
        totals.append({"RouteID":row.RouteID,"TotalRoute_km":0,"DispositionAttributed_km":0,
                       "Unattributed_km":0,"Overlap_km":0,"OverlapQC":"OK"})
    columns=["RouteID","OriginID","OriginType","OriginName","OriginCompany","DispositionID",
             "DispositionOwnerName","OwnerConflict","DistanceTravelled_m","DistanceTravelled_km","TotalRoute_km","PercentOfRoute"]
    details=pd.DataFrame(detail,columns=columns)
    details.to_csv(folder / "Route_Disposition_Detail.csv",index=False)
    totals_frame=pd.DataFrame(totals,columns=["RouteID","TotalRoute_km","DispositionAttributed_km","Unattributed_km","Overlap_km","OverlapQC"])
    expected_routes = set(results.loc[results.RouteStatus.isin(["ROUTED","ALREADY_PUBLIC"]), "RouteID"])
    if set(totals_frame.RouteID) != expected_routes or not totals_frame.RouteID.is_unique:
        raise RuntimeError("Report route accounting mismatch")
    if not (totals_frame.TotalRoute_km - totals_frame.DispositionAttributed_km - totals_frame.Unattributed_km).abs().le(1e-8).all():
        raise RuntimeError("Disposition distance reconciliation failed")
    totals_frame.to_csv(folder / "Route_Disposition_QC.csv",index=False)
    subject=settings.get("subject_company")
    business_status="NOT_REQUESTED_SUBJECT_COMPANY_UNSET"
    if subject and owner_field and results.OriginCompany.notna().any():
        subject=subject.strip().casefold()
        mine=details.OriginCompany.fillna("").str.strip().str.casefold().eq(subject)
        owned=details.DispositionOwnerName.fillna("").str.strip().str.casefold().eq(subject)
        known=details.OriginCompany.fillna("").str.strip().ne("")
        details[mine].to_csv(folder / "Dispositions_Used_By_Me.csv",index=False)
        details[owned & known & ~mine & details.OwnerConflict.eq(0)].to_csv(folder / "Who_Used_My_Disposition.csv",index=False)
        business_status="COMPLETE_FOR_KNOWN_COMPANIES_EXACT_NAME_MATCH"
    elif subject:
        business_status="UNAVAILABLE_COMPANY_FIELDS"
    write_json(folder / "Step3_Complete.json",{"Status":"COMPLETE","DispositionSource":str(source),
        "DispositionFilter":config["disposition_filter"],
        "MinDispositionOverlap_m":config.get("min_disposition_overlap_m",0.0),
        "SourceDispositionCount":source_count,"SelectedDispositionCount":selected_count,
        "DetailRows":len(details),"RoutesWithOverlap":int(totals_frame.OverlapQC.ne("OK").sum()),
        "OwnershipReports":business_status,"DistanceDefinition":"Network route only; unverified origin snap links excluded.",
        "OverlapRule":"DispositionAttributed_km is unique union coverage. Per-disposition distances may overlap: use Overlap_km to identify double attribution; do not sum them as unique route distance."})
    LOG.info("Reporting complete: %s disposition-use rows",len(details))


if __name__ == "__main__":
    main()
