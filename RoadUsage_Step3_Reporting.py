"""Optional Step 3: actual route/disposition overlap and ownership summaries."""
from RoadUsage_Workflow import prepare_environment
prepare_environment()
# ArcGIS must initialize its DLLs before GeoPandas/GDAL in fresh-data CLI runs.
import sys
if __name__ == "__main__" and not any(arg in ("-h", "--help") for arg in sys.argv):
    import arcpy
import argparse
import json
import logging
import shutil
from datetime import datetime as timestamp
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.strtree import STRtree
import RoadUsage_Step2_Routing_Fast as engine
from RoadUsage_Workflow import DEFAULT_CONFIG, load_config, config_hash, write_json

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


def attribute_route(geometry, dispositions, tree, id_field, owner_field=None, cache=None):
    """Measure polygon coverage as intervals on each travelled route part.

    A per-run cache reuses intersections on roads shared by many routes. Keep
    the cache local to one disposition dataset. No polygons or roads are edited.
    """
    from shapely.geometry import Point
    if cache is None: cache = {}
    parts = line_parts(geometry)
    keys = [part.wkb for part in parts]
    lengths = [part.length for part in parts]
    groups, owners, covered = {}, {}, {}
    for part_raw, polygon_raw in tree.query(parts, predicate="intersects").T:
        part_idx, polygon_idx = int(part_raw), int(polygon_raw)
        meta_key = ("meta",polygon_idx)
        if meta_key not in cache:
            row = dispositions.iloc[polygon_idx]
            owner = row.get(owner_field) if owner_field else None
            owner = str(owner) if owner is not None and pd.notna(owner) and str(owner).strip() else None
            cache[meta_key] = (str(row[id_field]),owner,row.geometry)
        disp_id, owner, polygon = cache[meta_key]
        clip_key = (keys[part_idx],polygon_idx)
        if clip_key not in cache:
            line = parts[part_idx]
            intersection = line.intersection(polygon)
            intervals = []
            for clipped in line_parts(intersection):
                if clipped.length <= 1e-7: continue
                a = float(line.project(Point(clipped.coords[0])))
                b = float(line.project(Point(clipped.coords[-1])))
                low,high = sorted((a,b))
                low,high = max(0.0,low),min(lengths[part_idx],high)
                if high-low > 1e-7: intervals.append((low,high))
            cache[clip_key] = intervals
        intervals = cache[clip_key]
        if not intervals: continue
        groups.setdefault(disp_id,{}).setdefault(part_idx,[]).extend(intervals)
        covered.setdefault(part_idx,[]).extend(intervals)
        if owner: owners.setdefault(disp_id,set()).add(owner)
    details = []
    for disp_id, intervals_by_part in groups.items():
        length = sum(merge_intervals(intervals) for intervals in intervals_by_part.values())
        names = sorted(owners.get(disp_id,set()))
        details.append({"DispositionID":disp_id,"DispositionOwnerName":" | ".join(names) or None,
                        "OwnerConflict":int(len(names)>1),"DistanceTravelled_m":length,
                        "DistanceTravelled_km":length/1000})
    attributed = sum(merge_intervals(intervals) for intervals in covered.values())
    overlap = max(0.0,sum(d["DistanceTravelled_m"] for d in details)-attributed)
    return details, attributed, overlap


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
    available = {field.name.lower():field.name for field in arcpy.ListFields(str(source))}
    if id_field.lower() not in available:
        raise ValueError(f"Disposition ID field not found: {id_field}")
    id_field = available[id_field.lower()]
    owner_field = available.get(owner_field.lower()) if owner_field else None
    requested = [id_field] + ([owner_field] if owner_field else [])
    polygons = engine.validate_crs(engine.read_gdb_layer_arcpy(str(source), requested), "Dispositions")
    if len(polygons) != int(arcpy.management.GetCount(str(source))[0]):
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
    for number,route in enumerate(routes.itertuples(index=False),1):
        records,attributed,overlap = attribute_route(route.geometry,polygons,tree,id_field,owner_field,intersection_cache)
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
        "DetailRows":len(details),"RoutesWithOverlap":int(totals_frame.OverlapQC.ne("OK").sum()),
        "OwnershipReports":business_status,"DistanceDefinition":"Network route only; unverified origin snap links excluded.",
        "OverlapRule":"DispositionAttributed_km is unique union coverage. Per-disposition distances may overlap: use Overlap_km to identify double attribution; do not sum them as unique route distance."})
    LOG.info("Reporting complete: %s disposition-use rows",len(details))


if __name__ == "__main__":
    main()
