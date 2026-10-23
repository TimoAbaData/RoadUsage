"""Standalone all-company road-use reporting from a completed RoadUsage run.

python -B -u RoadUsage_Step6_CompanyReports.py --run-dir Runs/20261021_101423

Reads the frozen Step 2/3 outputs and current origin-company metadata. Writes a
new CompanyReports/timestamp folder only. Steps 1-5 and their outputs are never
changed. "Use" means modeled network-route use; holders are disposition holders.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as _report_clock
import csv
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import unicodedata

from RoadUsage_Workflow import prepare_environment, write_json

LOG = logging.getLogger(__name__)
DEFAULT_COMPANY_CONFIG = Path(__file__).with_name("RoadUsage_CompanyReports_Config.json")
UNKNOWN = "UNKNOWN"
UNKNOWN_NAMES = {"", "UNKNOWN", "N/A", "NULL", "NONE", "NOT AVAILABLE", "NAN", "<NA>"}


def clean(value):
    return "" if value is None else str(value).strip()


def company_name(value, aliases=None):
    name = " ".join(unicodedata.normalize("NFKC", clean(value)).upper().split())
    if name in UNKNOWN_NAMES:
        return ""
    visited = set()
    while aliases and name in aliases:
        if name in visited:
            raise ValueError(f"Company alias cycle: {name}")
        visited.add(name)
        name = aliases[name]
    return name


def company_code(value):
    return clean(value).strip("{}").upper()


def load_company_config(path):
    path = Path(path).resolve()
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    aliases = {}
    for alias, target in settings.get("aliases", {}).items():
        key, value = company_name(alias), company_name(target)
        if not key or not value:
            raise ValueError("Company aliases must contain nonblank names")
        if key != value:
            aliases[key] = value
    for key in aliases:
        company_name(key, aliases)
    for spec in list(settings.get("origins", {}).values()) + settings.get("lookups", []):
        if spec.get("path"):
            p = Path(spec["path"])
            spec["path"] = str((p if p.is_absolute() else path.parent / p).resolve())
    return settings, aliases


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def unique_rows(rows, key, description):
    result = {}
    for row in rows:
        value = row[key]
        if value in result:
            raise ValueError(f"Duplicate {key} in {description}: {value}")
        result[value] = row
    return result


def read_fields(arcpy, path, requested):
    """Read only requested fields; CSV lookups keep leading-zero IDs as text."""
    requested = list(dict.fromkeys(field for field in requested if field))
    if Path(path).suffix.lower() == ".csv":
        with Path(path).open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            available = {name.casefold(): name for name in reader.fieldnames or []}
            missing = [field for field in requested if field.casefold() not in available]
            if missing:
                raise ValueError(f"Missing fields in {path}: {missing}")
            return [{field: row[available[field.casefold()]] for field in requested} for row in reader]
    available = {field.name.casefold(): field.name for field in arcpy.ListFields(str(path))}
    missing = [field for field in requested if field.casefold() not in available]
    if missing:
        raise ValueError(f"Missing company mapping fields in {path}: {missing}")
    with arcpy.da.SearchCursor(str(path), [available[f.casefold()] for f in requested]) as rows:
        return [dict(zip(requested, row)) for row in rows]


def resolve_origin_matches(matches, spec, lookups, aliases=None):
    """Resolve exact source IDs. Unknown or contradictory matches are not external users."""
    if not matches:
        return {"OriginCompany": UNKNOWN, "OriginCompanyStatus": "SOURCE_ID_NOT_FOUND",
                "OriginCompanyCode": "", "CompanyCandidates": "", "CompanyEvidence": "", "SourceMatchCount": 0}
    names, codes, evidence = set(), set(), set()
    unresolved_codes = set()
    for row in matches:
        direct = company_name(row.get(spec.get("company_name_field")), aliases)
        code = company_code(row.get(spec.get("company_code_field")))
        if code:
            codes.add(code)
        if direct:
            names.add(direct)
            evidence.add("SOURCE_NAME:" + spec["company_name_field"])
        elif code:
            candidates = lookups.get((spec.get("namespace", ""), code), set())
            names.update(candidates)
            evidence.add("COMPANY_LOOKUP:" + spec.get("namespace", ""))
            if not candidates:
                unresolved_codes.add(code)
    if len(names) > 1:
        status, resolved = "CONFLICT", ""
    elif names and not unresolved_codes:
        status, resolved = "MATCHED", next(iter(names))
    elif names:
        status, resolved = "PARTIAL_CONFLICT", ""
    else:
        status, resolved = ("COMPANY_CODE_NOT_FOUND" if codes else "COMPANY_BLANK"), ""
    return {"OriginCompany": resolved or UNKNOWN, "OriginCompanyStatus": status,
            "OriginCompanyCode": " | ".join(sorted(codes)),
            "CompanyCandidates": " | ".join(sorted(names)),
            "CompanyEvidence": " | ".join(sorted(evidence)), "SourceMatchCount": len(matches)}


def enrich_origins(arcpy, summaries, run_config, settings, aliases):
    sources, lookups, evidence = {}, defaultdict(set), defaultdict(set)
    for origin in run_config["origins"]:
        kind = origin["origin_type"]
        mapping_kind = "ERCB_FACILITY" if kind == "FACILITY" else kind
        if kind not in settings["origins"] and mapping_kind not in settings["origins"]:
            raise ValueError(f"Add a company-field mapping for origin type {kind} to the separate company config")
        spec = dict(settings["origins"].get(kind, settings["origins"].get(mapping_kind)))
        spec["saved_company_field"] = origin.get("company_field")
        spec["path"] = spec.get("path") or origin["path"]
        spec["id_field"] = spec.get("id_field") or origin["id_field"]
        rows = read_fields(arcpy, spec["path"], [spec["id_field"], spec.get("company_name_field"),
                                               spec.get("company_code_field"), spec.get("name_field")])
        by_id = defaultdict(list)
        for row in rows:
            by_id[clean(row[spec["id_field"]])].append(row)
            code = company_code(row.get(spec.get("company_code_field")))
            name = company_name(row.get(spec.get("company_name_field")), aliases)
            if code and name and spec.get("namespace"):
                key = (spec["namespace"], code)
                lookups[key].add(name)
                evidence[key].add(kind + ":" + spec["company_name_field"])
        sources[kind] = (spec, by_id)
        LOG.info("Read company metadata for %s: %s source rows", kind, len(rows))
    for spec in settings.get("lookups", []):
        fields = [spec["id_field"], spec["name_field"], spec.get("namespace_field")]
        for row in read_fields(arcpy, spec["path"], fields):
            namespace = clean(row[spec["namespace_field"]]) if spec.get("namespace_field") else spec["namespace"]
            code, name = company_code(row[spec["id_field"]]), company_name(row[spec["name_field"]], aliases)
            if code and name:
                lookups[(namespace, code)].add(name)
                evidence[(namespace, code)].add(str(spec["path"]))
    enriched, unresolved = {}, Counter()
    for route_id, original in summaries.items():
        row = dict(original)
        spec, by_id = sources[row["OriginType"]]
        matches = by_id.get(clean(row["OriginID"]), [])
        identity = resolve_origin_matches(matches, spec, lookups, aliases)
        frozen_company = company_name(original.get("OriginCompany"), aliases)
        frozen_is_code = bool(spec.get("saved_company_field") and spec.get("company_code_field")
                              and spec["saved_company_field"].casefold() == spec["company_code_field"].casefold())
        if frozen_company and not frozen_is_code:
            identity.update(OriginCompany=frozen_company, OriginCompanyStatus="FROZEN_ROUTE_METADATA",
                            CompanyEvidence="Route_Summary.csv", CompanyCandidates=frozen_company)
        row.update(identity)
        row["CompanyNamespace"] = spec.get("namespace", "")
        row["CompanyRole"] = spec.get("role", "SOURCE_COMPANY")
        if not clean(row.get("OriginName")) and spec.get("name_field"):
            labels = {clean(m.get(spec["name_field"])) for m in matches} - {""}
            if len(labels) == 1:
                row["OriginName"] = next(iter(labels))
        if not company_name(row["OriginCompany"]):
            unresolved[(row["OriginType"], row["CompanyNamespace"], row["OriginCompanyCode"],
                        row["OriginCompanyStatus"])] += 1
        enriched[route_id] = row
    lookup_rows = [{"Namespace": ns, "CompanyCode": code, "Company": next(iter(names)) if len(names) == 1 else UNKNOWN,
                    "CompanyStatus": "MATCHED" if len(names) == 1 else "CONFLICT",
                    "CompanyCandidates": " | ".join(sorted(names)),
                    "CompanyEvidence": " | ".join(sorted(evidence[(ns, code)]))}
                   for (ns, code), names in sorted(lookups.items())]
    missing = [{"OriginType": kind, "Namespace": ns, "CompanyCode": code, "Company": UNKNOWN,
                "CompanyStatus": status, "OriginRecordCount": count}
               for (kind, ns, code, status), count in sorted(unresolved.items())]
    return enriched, lookup_rows, missing, [spec for spec, _ in sources.values()]


def resolve_holders(polygons, id_field, owner_field, details, aliases):
    candidates = defaultdict(set)
    blank_parts = Counter()
    for disp, raw_owner in zip(polygons[id_field], polygons[owner_field]):
        disp = str(disp)
        # pandas nulls must not become a company literally called NAN.
        name = company_name(raw_owner, aliases)
        if name:
            candidates[disp].add(name)
        else:
            blank_parts[disp] += 1
        candidates.setdefault(disp, set())
    for row in details:
        disp = row["DispositionID"]
        original = row.get("DispositionOwnerName")
        if clean(original):
            # Step 3 joins distinct holder names with this exact delimiter.
            for name in clean(original).split(" | "):
                normalized = company_name(name, aliases)
                if normalized:
                    candidates[disp].add(normalized)
        candidates.setdefault(disp, set())
    rows, holders, conflicts = [], {}, set()
    for disp, names in sorted(candidates.items()):
        status = "MATCHED" if len(names) == 1 else "CONFLICT" if names else "UNKNOWN"
        holder = next(iter(names)) if len(names) == 1 else ""
        holders[disp] = holder or None
        if len(names) > 1:
            conflicts.add(disp)
        rows.append({"DispositionID": disp, "DispositionCompany": holder or UNKNOWN, "HolderStatus": status,
                     "CompanyCandidates": " | ".join(sorted(names)), "BlankSourcePartCount": blank_parts[disp]})
    return holders, conflicts, rows


def relationship(origin_company, holder, origin_status="", holder_conflict=False):
    origin_company, holder = company_name(origin_company), company_name(holder)
    if holder_conflict or origin_status in {"CONFLICT", "PARTIAL_CONFLICT", "SITE_COMPANY_CONFLICT"}:
        return "CONFLICT"
    if not origin_company and not holder:
        return "UNKNOWN_BOTH"
    if not origin_company:
        return "UNKNOWN_ORIGIN_COMPANY"
    if not holder:
        return "UNKNOWN_HOLDER"
    return "SELF_USE" if origin_company == holder else "EXTERNAL_USE"


def assert_close(actual, expected, label):
    if not math.isfinite(float(actual)) or not math.isfinite(float(expected)) or abs(float(actual) - float(expected)) > max(1e-5, abs(float(expected)) * 1e-9):
        raise ValueError(f"Saved-report reconciliation failed for {label}: {actual} != {expected}")


def pair_summaries(route_companies):
    """Aggregate route/holder unions, never sum disposition-detail distances."""
    groups = {}
    for row in route_companies:
        origin, holder = company_name(row["OriginCompany"]), company_name(row["DispositionCompany"])
        if not origin or not holder:
            continue
        key = (origin, holder)
        group = groups.setdefault(key, {"OriginCompany": origin, "DispositionCompany": holder,
            "Relationship": row["Relationship"], "routes": set(), "origins": set(), "dispositions": set(),
            "AttributedRoute_km": 0.0, "ExclusiveRoute_km": 0.0, "AmbiguousRoute_km": 0.0})
        group["routes"].add(row["RouteID"])
        group["origins"].add(row["OriginUID"])
        group["dispositions"].update(json.loads(row["DispositionIDs"]))
        for field in ("AttributedRoute_km", "ExclusiveRoute_km", "AmbiguousRoute_km"):
            group[field] += row[field]
    result = []
    for key in sorted(groups):
        group = groups[key]
        group["RouteCount"] = len(group.pop("routes"))
        group["OriginRecordCount"] = len(group.pop("origins"))
        group["DispositionCount"] = len(group.pop("dispositions"))
        result.append(group)
    return result


def perspective(rows, owner=False):
    return [{"SubjectCompany": row["DispositionCompany"] if owner else row["OriginCompany"],
             "OtherCompany": row["OriginCompany"] if owner else row["DispositionCompany"], **row}
            for row in rows if row["Relationship"] == "EXTERNAL_USE"]


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def numeric_type(field):
    if field.endswith("Count") or field in {"OwnerConflict", "BlankSourcePartCount", "SavedDistancesMatch"}:
        return "LONG"
    if field in {"IsPreferredOrigin", "SiteCompanyConflict", "SiteLowerPriorityCompanyDisagreement"}:
        return "LONG"
    if field.endswith(("_m", "_km")) or field == "PercentOfRoute":
        return "DOUBLE"
    return "TEXT"


def export_tables(arcpy, folder, tables):
    gdb = folder / "RoadUsage_CompanyReports.gdb"
    arcpy.management.CreateFileGDB(str(folder), gdb.name)
    for name, (rows, fields) in tables.items():
        target = str(gdb / name)
        arcpy.management.CreateTable(str(gdb), name)
        specs = []
        for field in fields:
            kind = numeric_type(field)
            width = max([len(clean(row.get(field)).encode("utf-16-le")) // 2 for row in rows] + [255])
            specs.append([field, kind, field, width if kind == "TEXT" else None])
        arcpy.management.AddFields(target, specs)
        with arcpy.da.InsertCursor(target, fields) as cursor:
            for row in rows:
                values = []
                for field in fields:
                    value = row.get(field)
                    if value is None or value == "":
                        values.append(None)
                    elif numeric_type(field) == "LONG":
                        values.append(int(value))
                    elif numeric_type(field) == "DOUBLE":
                        values.append(float(value))
                    else:
                        values.append(str(value))
                cursor.insertRow(values)
        if int(arcpy.management.GetCount(target)[0]) != len(rows):
            raise RuntimeError(f"GDB table count mismatch: {name}")
        LOG.info("Exported GDB table %s: %s rows", name, len(rows))
    return gdb

SITE_FIELDS = ["SiteID", "PreferredRouteID", "PreferredOriginType", "PreferredCompanyName",
               "IsPreferredOrigin", "SiteMemberCount", "SiteDistance_m",
               "SiteCompanyConflict", "SiteLowerPriorityCompanyDisagreement"]
IDENTITY_FIELDS = ["OriginCompany", "OriginCompanyStatus", "OriginCompanyCode",
                   "CompanyNamespace", "CompanyRole", "CompanyEvidence", "CompanyCandidates"]
DETAIL_FIELDS = ["RouteID", "OriginUID", "OriginID", "OriginType", "OriginName"] + IDENTITY_FIELDS + [
    "DispositionID", "DispositionOwnerName", "DispositionCompany", "HolderStatus", "Relationship",
    "DistanceTravelled_m", "DistanceTravelled_km", "TotalRoute_km", "PercentOfRoute",
    "OwnerConflict", "OverlapQC"] + SITE_FIELDS
ROUTE_COMPANY_FIELDS = ["RouteID", "OriginUID", "OriginID", "OriginType", "OriginCompany",
    "DispositionCompany", "Relationship", "DispositionIDs", "DispositionCount",
    "AttributedRoute_km", "ExclusiveRoute_km", "AmbiguousRoute_km", "TotalRoute_km"] + SITE_FIELDS
PAIR_FIELDS = ["OriginCompany", "DispositionCompany", "Relationship", "RouteCount",
               "OriginRecordCount", "DispositionCount", "AttributedRoute_km",
               "ExclusiveRoute_km", "AmbiguousRoute_km"]


# Shared route attribute contract, in the order requested for both route layers.
# OBJECTID, Shape and Shape_Length are geodatabase-managed system fields.
COMPLETE_ROUTE_FIELDS = (
    "RouteID", "OriginUID", "OriginID", "OriginType", "OriginName", "OriginCompany",
    "RouteStatus", "DestinationRoadID", "DestinationRoadName", "TotalRoute_m",
    "TotalRoute_km", "SnapDistance_m", "DispositionID", "DispositionOwnerName",
    "OwnerConflict", "DistanceTravelled_m", "DistanceTravelled_km", "PercentOfRoute",
    "DispositionCount", "DispositionAttributed_km", "Unattributed_km", "Overlap_km",
    "OverlapQC",
)


def copy_company_routes(folder, output, enriched, arcpy, gdb=None):
    """Write both route layers with the Step 4 schema and resolved company names."""
    import fiona
    source = folder / "RoadUsage_CompleteRoute.gpkg"
    if not source.exists():
        return None
    marker = folder / "Step4_Complete.json"
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8"))["Status"] != "COMPLETE":
        raise ValueError("A completed Step 4 is required to copy CompleteRoute")
    target = output / "RoadUsage_CompanyRoutes.gpkg"
    with fiona.open(source, layer="CompleteRoute") as original:
        source_properties = original.schema["properties"]
        missing = set(COMPLETE_ROUTE_FIELDS) - set(source_properties)
        if missing:
            raise ValueError(f"CompleteRoute is missing required schema fields: {sorted(missing)}")
        schema = {"geometry": original.schema["geometry"],
                  "properties": {field: source_properties[field] for field in COMPLETE_ROUTE_FIELDS}}
        crs_wkt = original.crs_wkt
    counts = {}
    for layer, only_preferred in (("CompleteRoute", False), ("Preferred_CompleteRoute", True)):
        count = 0
        batch = []
        seen = set()
        with fiona.open(source, layer="CompleteRoute") as original, fiona.open(
                target, "w", driver="GPKG", layer=layer, schema=schema, crs_wkt=crs_wkt) as sink:
            for feature in original:
                rid = feature["properties"]["RouteID"]
                row = enriched[rid]
                if rid in seen or row["RouteStatus"] != "ROUTED":
                    raise ValueError(f"Unexpected or duplicate CompleteRoute: {rid}")
                seen.add(rid)
                from shapely.geometry import shape
                assert_close(shape(feature["geometry"]).length, row["TotalRoute_m"], rid + " CompleteRoute length")
                if only_preferred and not row["IsPreferredOrigin"]:
                    continue
                properties = {field: feature["properties"][field] for field in COMPLETE_ROUTE_FIELDS}
                properties["OriginCompany"] = row["OriginCompany"]
                # Source origin label fills an existing blank; route geometry is copied directly.
                if not properties.get("OriginName"):
                    properties["OriginName"] = row.get("OriginName") or None
                batch.append({"geometry": feature["geometry"], "properties": properties})
                if len(batch) >= 1000:
                    sink.writerecords(batch)
                    batch.clear()
                count += 1
            if batch:
                sink.writerecords(batch)
        expected = {rid for rid, row in enriched.items() if row["RouteStatus"] == "ROUTED"}
        if seen != expected:
            raise ValueError("CompleteRoute IDs differ from the completed routing results")
        with fiona.open(target, layer=layer) as check:
            if tuple(check.schema["properties"]) != COMPLETE_ROUTE_FIELDS or len(check) != count:
                raise RuntimeError(f"Route schema or count changed: {layer}")
        counts[layer] = count
        LOG.info("Wrote %s company-enriched route features to %s", count, layer)
    if gdb:
        from RoadUsage_Step5_ExportGDB import copy_feature_class
        with arcpy.EnvManager(overwriteOutput=False, extent=None, outputCoordinateSystem=None):
            for layer in counts:
                copy_feature_class(arcpy, target / ("main." + layer), gdb / layer)
    return {"GeoPackage": str(target), "FeatureCounts": counts, "AttributeFields": list(COMPLETE_ROUTE_FIELDS)}


def company_coverage_rows(enriched, route_companies, holders):
    names = {company_name(row["OriginCompany"]) for row in enriched.values()}
    names.update(company_name(row.get("SourceOriginCompany", row["OriginCompany"])) for row in enriched.values())
    names.update(holder for holder in holders.values() if holder)
    names.discard("")
    names.add(UNKNOWN)
    groups = {name: {"Company": name, "OriginalOriginRecordCount": 0, "PreferredSiteCount": 0,
                    "PreferredRoutedSiteCount": 0, "PreferredAlreadyPublicSiteCount": 0,
                    "PreferredUnroutedSiteCount": 0, "UsesOtherCompanies": set(),
                    "UsedByOtherCompanies": set(), "IncomingUnknownOriginSites": set()}
              for name in names}
    for row in enriched.values():
        name = company_name(row["OriginCompany"]) or UNKNOWN
        group = groups[name]
        original_name = company_name(row.get("SourceOriginCompany", row["OriginCompany"])) or UNKNOWN
        groups[original_name]["OriginalOriginRecordCount"] += 1
        if row["IsPreferredOrigin"]:
            group["PreferredSiteCount"] += 1
            status = row["RouteStatus"]
            field = ("PreferredRoutedSiteCount" if status == "ROUTED" else
                     "PreferredAlreadyPublicSiteCount" if status == "ALREADY_PUBLIC" else
                     "PreferredUnroutedSiteCount")
            group[field] += 1
    for row in route_companies:
        if not row["IsPreferredOrigin"]:
            continue
        origin = company_name(row["OriginCompany"])
        holder = row["DispositionCompany"]
        if not origin:
            groups[holder]["IncomingUnknownOriginSites"].add(row["SiteID"])
        elif origin != holder:
            groups[origin]["UsesOtherCompanies"].add(holder)
            groups[holder]["UsedByOtherCompanies"].add(origin)
    result = []
    for name in sorted(groups):
        row = groups[name]
        row["UsesOtherCompanyCount"] = len(row.pop("UsesOtherCompanies"))
        row["UsedByOtherCompanyCount"] = len(row.pop("UsedByOtherCompanies"))
        row["IncomingUnknownOriginSiteCount"] = len(row.pop("IncomingUnknownOriginSites"))
        result.append(row)
    return result


def run_reports(folder, company_config, output=None, no_gdb=False):
    folder = Path(folder).resolve()
    settings, aliases = load_company_config(company_config)
    tolerance = float(settings.get("site_tolerance_m", 10.0))
    if not 0 <= tolerance < float("inf"):
        raise ValueError("Site tolerance must be a nonnegative finite distance in metres")
    expected_priority = ["WELL", "ERCB_FACILITY", "EQUIPMENT"]
    if settings.get("origin_priority", expected_priority) != expected_priority:
        raise ValueError("This script uses the reviewed WELL, ERCB_FACILITY, EQUIPMENT preference")
    step2 = json.loads((folder / "Step2_Complete.json").read_text(encoding="utf-8"))
    step3 = json.loads((folder / "Step3_Complete.json").read_text(encoding="utf-8"))
    if step2["Status"] != "COMPLETE" or step3["Status"] != "COMPLETE":
        raise ValueError("A completed Step 2 and Step 3 run is required")
    config = step2["Config"]
    if step3.get("DispositionFilter") != config["disposition_filter"]:
        raise ValueError("Saved Step 3 disposition filter differs from the frozen run configuration")
    if float(step3.get("MinDispositionOverlap_m", 0)) != float(config.get("min_disposition_overlap_m", 0)):
        raise ValueError("Saved Step 3 continuity threshold differs from the frozen run configuration")
    output = Path(output).resolve() if output else folder / "CompanyReports" / _report_clock.datetime.now().strftime("%Y%m%d_%H%M%S")
    if output.exists():
        raise FileExistsError(f"Choose a new report folder; existing results are preserved: {output}")

    # Fingerprint preserved outputs and the original workflow files before any GIS reads.
    original_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".csv", ".json", ".gpkg"}]
    project = Path(__file__).resolve().parent
    original_files += [p for p in project.glob("RoadUsage*.py") if p.name not in {
        Path(__file__).name, "RoadUsage_CompanyIntervals.py", "RoadUsage_CompanySites.py"}]
    original_files += [project / "RoadUsage_Config.json", project / "AGENTS.md", project / "README.md"]
    before = {str(p): file_hash(p) for p in original_files}

    prepare_environment()
    import arcpy
    prepare_environment()
    import fiona
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    from RoadUsage_Step3_Reporting import route_segment_protections
    from RoadUsage_CompanyIntervals import reconstruct_effective_intervals, company_coverage
    from RoadUsage_CompanySites import assign_site_preferences
    import RoadUsage_Step2_Routing_Fast as engine

    summaries = unique_rows(read_csv(folder / "Route_Summary.csv"), "RouteID", "Route_Summary.csv")
    saved_qc = unique_rows(read_csv(folder / "Route_Disposition_QC.csv"), "RouteID", "Route_Disposition_QC.csv")
    saved_details = read_csv(folder / "Route_Disposition_Detail.csv")
    detail_groups = defaultdict(dict)
    for row in saved_details:
        rid, disp = row["RouteID"], row["DispositionID"]
        if rid not in summaries or disp in detail_groups[rid]:
            raise ValueError(f"Invalid/duplicate saved route-disposition row: {rid}, {disp}")
        detail_groups[rid][disp] = row
    enriched, lookup_rows, missing, source_specs = enrich_origins(arcpy, summaries, config, settings, aliases)
    with fiona.open(folder / "RoadUsage_Routes.gpkg", layer="Origin_Results") as origins:
        if origins.crs.to_epsg() != int(config["target_wkid"]):
            raise ValueError("Origin geometry CRS differs from the frozen run configuration")
        points = {f["properties"]["RouteID"]: shape(f["geometry"]) for f in origins}
    if set(points) != set(summaries):
        raise ValueError("Origin geometries do not match saved summary route IDs")
    crs = arcpy.SpatialReference(int(config["target_wkid"]))
    if crs.type != "Projected" or abs(crs.metersPerUnit - 1) > 1e-12:
        raise ValueError("Same-site distances require the saved projected metre coordinate system")
    enriched = assign_site_preferences(enriched, points, tolerance_m=tolerance)
    LOG.info("Preferred origins: %s of %s, using %s m and WELL > ERCB_FACILITY > EQUIPMENT",
             sum(row["IsPreferredOrigin"] for row in enriched.values()), len(enriched), tolerance)

    source = str(step3["DispositionSource"])
    fields = {field.name.casefold(): field.name for field in arcpy.ListFields(source)}
    id_field = fields[config["reporting"].get("disposition_id_field", "DISP_NUM").casefold()]
    requested_owner = config["reporting"].get("owner_field") or "COMPANY"
    owner_field = fields.get(requested_owner.casefold())
    from RoadUsage_Workflow import disposition_filter_layer
    selection, source_count, selected_count = disposition_filter_layer(
        arcpy, source, config["disposition_filter"], "company_report_dispositions")
    engine.TARGET_WKID = config["target_wkid"]
    requested = [id_field] + ([owner_field] if owner_field else [])
    polygons = engine.validate_crs(engine.read_gdb_layer_arcpy(str(selection), requested), "Saved dispositions")
    if len(polygons) != selected_count:
        raise ValueError("Some filtered disposition geometries could not be read")
    if not owner_field:
        owner_field = "_MissingOwner"
        polygons[owner_field] = None
    if polygons[id_field].isna().any() or not polygons.geometry.is_valid.all():
        raise ValueError("Disposition IDs and geometries must be valid")
    holders, holder_conflicts, holder_rows = resolve_holders(polygons, id_field, owner_field, saved_details, aliases)
    holder_status = {row["DispositionID"]: row["HolderStatus"] for row in holder_rows}
    tree = STRtree(list(polygons.geometry))
    segments = route_segment_protections(folder / "Route_Segments.csv")
    cache, detailed, route_companies, qc_rows, visited = {}, [], [], [], set()
    with fiona.open(folder / "RoadUsage_Routes.gpkg", layer="Generated_Routes") as routes:
        for number, feature in enumerate(routes, 1):
            rid = feature["properties"]["RouteID"]
            if rid in visited or rid not in enriched:
                raise ValueError(f"Unexpected/duplicate route geometry: {rid}")
            visited.add(rid)
            origin = enriched[rid]
            geom = shape(feature["geometry"])
            total_m = float(origin["TotalRoute_m"])
            segment_rid, protected, segment_length = next(segments)
            if segment_rid != rid:
                raise ValueError(f"Route segment order mismatch: {rid}")
            assert_close(segment_length, total_m, rid + " segment length")
            assert_close(geom.length, total_m, rid + " geometry length")
            effective = reconstruct_effective_intervals(geom, polygons, tree, id_field, cache,
                         config.get("min_disposition_overlap_m", 0.0), protected)
            expected = detail_groups.get(rid, {})
            if set(effective) != set(expected):
                raise ValueError(f"Disposition identities differ from saved report: {rid}")
            for disp, spans in effective.items():
                assert_close(sum(b-a for a, b in spans), expected[disp]["DistanceTravelled_m"], rid + "/" + disp)
            coverage = company_coverage(effective, holders, conflict_dispositions=holder_conflicts,
                                        route_length_m=total_m)
            qc = saved_qc[rid]
            assert_close(coverage["attributed_m"]/1000, qc["DispositionAttributed_km"], rid + " attributed")
            assert_close(coverage["unattributed_m"]/1000, qc["Unattributed_km"], rid + " unattributed")
            overlap = max(0.0, sum(float(d["DistanceTravelled_m"]) for d in expected.values()) - coverage["attributed_m"])
            assert_close(overlap/1000, qc["Overlap_km"], rid + " overlap")
            assert_close(coverage["exclusive_known_m"] + coverage["cross_holder_only_m"] +
                         coverage["uncertain_holder_m"] + coverage["unattributed_m"], total_m, rid + " company partition")
            site = {field: origin.get(field) for field in SITE_FIELDS}
            for disp, saved in expected.items():
                holder = holders.get(disp)
                row = dict(saved)
                row.update({field: origin.get(field) for field in ["OriginUID", "OriginID", "OriginType", "OriginName"] + IDENTITY_FIELDS})
                row.update(site)
                row.update(DispositionCompany=holder or UNKNOWN, HolderStatus=holder_status.get(disp, "UNKNOWN"),
                           Relationship=relationship(origin["OriginCompany"], holder,
                             origin["OriginCompanyStatus"], disp in holder_conflicts), OverlapQC=qc["OverlapQC"])
                detailed.append(row)
            for holder, length in coverage["by_holder_m"].items():
                exclusive = coverage["exclusive_by_holder_m"].get(holder, 0.0)
                disp_ids = sorted(d for d in effective if holders.get(d) == holder and d not in holder_conflicts)
                route_companies.append({"RouteID": rid, "OriginUID": origin["OriginUID"],
                    "OriginID": origin["OriginID"], "OriginType": origin["OriginType"],
                    "OriginCompany": origin["OriginCompany"], "DispositionCompany": holder,
                    "Relationship": relationship(origin["OriginCompany"], holder, origin["OriginCompanyStatus"]),
                    "DispositionIDs": json.dumps(disp_ids, separators=(",", ":")), "DispositionCount": len(disp_ids),
                    "AttributedRoute_km": length/1000, "ExclusiveRoute_km": exclusive/1000,
                    "AmbiguousRoute_km": max(0.0, length-exclusive)/1000, "TotalRoute_km": total_m/1000, **site})
            qc_rows.append({"RouteID": rid, "OriginUID": origin["OriginUID"], "OriginCompany": origin["OriginCompany"],
                "TotalRoute_km": total_m/1000, "Attributed_km": coverage["attributed_m"]/1000,
                "ExclusiveKnown_km": coverage["exclusive_known_m"]/1000,
                "CrossHolderOnly_km": coverage["cross_holder_only_m"]/1000,
                "CrossHolderOverlap_km": coverage["cross_holder_overlap_m"]/1000,
                "UncertainHolder_km": coverage["uncertain_holder_m"]/1000,
                "Unattributed_km": coverage["unattributed_m"]/1000, "SavedDistancesMatch": 1, **site})
            if number % 5000 == 0:
                LOG.info("Reconciled %s saved routes; company intervals match", number)
    if next(segments, None) is not None:
        raise ValueError("Extra saved route segments without route geometry")
    routed_ids = {rid for rid, row in summaries.items() if row["RouteStatus"] == "ROUTED"}
    if visited != routed_ids:
        raise ValueError("Generated route accounting differs from the completed run")
    preferred_detail = [row for row in detailed if row["IsPreferredOrigin"]]
    preferred_companies = [row for row in route_companies if row["IsPreferredOrigin"]]
    pairs = pair_summaries(preferred_companies)
    coverage_rows = company_coverage_rows(enriched, route_companies, holders)
    origin_fields = list(next(iter(summaries.values()))) + [field for field in
        IDENTITY_FIELDS + SITE_FIELDS + ["SourceOriginCompany", "SourceOriginCompanyStatus", "SourceMatchCount", "SiteCompanyCandidates"]
        if field not in next(iter(summaries.values()))]
    qc_fields = ["RouteID", "OriginUID", "OriginCompany", "TotalRoute_km", "Attributed_km", "ExclusiveKnown_km",
                 "CrossHolderOnly_km", "CrossHolderOverlap_km", "UncertainHolder_km", "Unattributed_km",
                 "SavedDistancesMatch"] + SITE_FIELDS
    tables = {
        "Whose_Roads_I_Use": (perspective(preferred_detail), ["SubjectCompany", "OtherCompany"] + DETAIL_FIELDS),
        "Who_Uses_My_Road": (perspective(preferred_detail, owner=True), ["SubjectCompany", "OtherCompany"] + DETAIL_FIELDS),
        "Whose_Roads_I_Use_Summary": (perspective(pairs), ["SubjectCompany", "OtherCompany"] + PAIR_FIELDS),
        "Who_Uses_My_Road_Summary": (perspective(pairs, owner=True), ["SubjectCompany", "OtherCompany"] + PAIR_FIELDS),
        "Self_Use": ([r for r in preferred_detail if r["Relationship"] == "SELF_USE"], DETAIL_FIELDS),
        "Unresolved_Use": ([r for r in preferred_detail if r["Relationship"] not in {"SELF_USE", "EXTERNAL_USE"}], DETAIL_FIELDS),
        "Route_Disposition_Companies": (detailed, DETAIL_FIELDS),
        "Route_Company_Distances": (route_companies, ROUTE_COMPANY_FIELDS),
        "Origin_Company_Matches": (list(enriched.values()), origin_fields),
        "Disposition_Company_Matches": (holder_rows, ["DispositionID", "DispositionCompany", "HolderStatus", "CompanyCandidates", "BlankSourcePartCount"]),
        "Route_Company_QC": (qc_rows, qc_fields),
        "Company_Coverage": (coverage_rows, list(coverage_rows[0])),
        "Resolved_Company_Codes": (lookup_rows, ["Namespace", "CompanyCode", "Company", "CompanyStatus", "CompanyCandidates", "CompanyEvidence"]),
        "Missing_Company_Lookups": (missing, ["OriginType", "Namespace", "CompanyCode", "Company", "CompanyStatus", "OriginRecordCount"]),
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "CompanyReports_Status.json", {"Status": "WRITING_OUTPUTS", "SourceRun": str(folder)})
    for name, (rows, fields) in tables.items():
        write_csv(output / (name + ".csv"), rows, fields)
    gdb = None
    if not no_gdb:
        with arcpy.EnvManager(overwriteOutput=False, extent=None, outputCoordinateSystem=None):
            gdb = export_tables(arcpy, output, tables)
    route_output = copy_company_routes(folder, output, enriched, arcpy, gdb)
    after = {path: file_hash(path) for path in before}
    if before != after:
        raise RuntimeError("A pre-existing workflow file or run output changed during reporting")
    unknown_origins = sum(not company_name(row["OriginCompany"]) for row in enriched.values())
    unknown_preferred = sum(row["IsPreferredOrigin"] and not company_name(row["OriginCompany"]) for row in enriched.values())
    marker = {"Status": "COMPLETE_WITH_UNKNOWNS" if unknown_preferred or tables["Unresolved_Use"][0] else "COMPLETE",
        "SourceRun": str(folder), "OutputFolder": str(output), "MetadataReadUTC": _report_clock.datetime.now(_report_clock.timezone.utc).isoformat(),
        "OriginRecords": len(enriched), "PreferredSites": sum(r["IsPreferredOrigin"] for r in enriched.values()),
        "UnknownOriginRecords": unknown_origins, "UnknownPreferredSites": unknown_preferred,
        "RoutedOriginsReconciled": len(visited), "PreferredRoutedSites": sum(enriched[rid]["IsPreferredOrigin"] for rid in visited),
        "ExternalCompanyPairs": sum(r["Relationship"] == "EXTERNAL_USE" for r in pairs),
        "ExternalDispositionUseRows": len(tables["Whose_Roads_I_Use"][0]), "UnresolvedUseRows": len(tables["Unresolved_Use"][0]),
        "DispositionSource": source, "DispositionFilter": config["disposition_filter"],
        "SourceDispositionCount": source_count, "SelectedDispositionCount": selected_count,
        "MinimumDispositionOverlap_m": config.get("min_disposition_overlap_m", 0.0),
        "SiteTolerance_m": tolerance, "OriginPriority": expected_priority,
        "CompanyConfig": settings, "OriginMetadataSources": source_specs,
        "ExistingFilesUnchanged": True, "InputSHA256": before,
        "GDB": str(gdb) if gdb else None, "CompanyRoutes": route_output,
        "Rows": {name: len(rows) for name, (rows, _) in tables.items()},
        "Interpretation": "Modeled route use and disposition-holder attribution, not observed traffic or legal road ownership. "
                          "Route kilometres are summed across preferred sites, not unique physical road length. "
                          "AttributedRoute_km unions same-company overlaps within each route; "
                          "ExclusiveRoute_km excludes all other-holder and unresolved-holder overlap."}
    write_json(output / "CompanyReports_Status.json", marker)
    notes = (
        "ALL-COMPANY ROAD USE REPORTS\n\n"
        "Filter SubjectCompany in either Whose_Roads_I_Use or Who_Uses_My_Road.\n"
        "Those two views contain external use by known companies at preferred origins.\n"
        "Self-use and unknown/conflicting identities have separate tables.\n"
        "The summary tables merge same-holder overlaps per route; do not sum the detail\n"
        "DistanceTravelled_km column as unique distance. AttributedRoute_km may overlap\n"
        "between different companies. ExclusiveRoute_km excludes ambiguous ownership.\n"
        "Multiple routes may use the same physical road; totals are modeled route-km.\n\n"
        "Site preference: WELL, ERCB_FACILITY, EQUIPMENT, within the configured tolerance.\n"
        "All original records are retained with IsPreferredOrigin flags. A site must\n"
        "fit within the configured maximum pairwise distance; chains are not merged.\n"
        "Different companies among the best-priority records yield UNKNOWN, not a guess.\n"
        "Preference follows origin type even if that origin is unroutable; its saved\n"
        "exception stays visible in Company_Coverage and Origin_Company_Matches.\n"
        "CompleteRoute retains all existing route geometries and IDs with company fields.\n"
        "Preferred_CompleteRoute is its preferred-origin subset. Join tables by RouteID.\n"
        "Both route layers use the original 23-field attribute schema. Company/site\n"
        "matching metadata remains in Origin_Company_Matches and the report tables.\n\n"
        "UNKNOWN is not counted as a confirmed external company. Company_Coverage\n"
        "shows unknown-origin use against known holders. Add verified company lookup\n"
        "tables/aliases to the separate company config, then rerun into a new folder.\n"
        "The original configuration, scripts, GeoPackages, reports and GDBs are unchanged.\n"
    )
    (output / "README.txt").write_text(notes, encoding="utf-8")
    LOG.info("Company reports complete: %s; %s unresolved preferred companies", output, unknown_preferred)
    print(json.dumps({k: marker[k] for k in ("Status", "OutputFolder", "PreferredSites", "UnknownPreferredSites",
                     "PreferredRoutedSites", "ExternalCompanyPairs", "ExternalDispositionUseRows",
                     "ExistingFilesUnchanged")}, indent=2), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="Completed run to read; never overwritten")
    parser.add_argument("--company-config", type=Path, default=DEFAULT_COMPANY_CONFIG,
                        help="Separate company-field mappings, optional lookups and aliases")
    parser.add_argument("--output-dir", type=Path, help="New folder; defaults to run/CompanyReports/timestamp")
    parser.add_argument("--no-gdb", action="store_true", help="Write CSV and GeoPackage copies without GDB copies")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_reports(args.run_dir, args.company_config, args.output_dir, args.no_gdb)


if __name__ == "__main__":
    main()
