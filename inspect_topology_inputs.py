"""Read-only inventory and small-gap measurements before choosing a tolerance."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from shapely.geometry import Point
from shapely.strtree import STRtree


def main():
    import arcpy
    import RoadUsage_Step2_Routing_Fast as routing

    if "--reference-layers" in sys.argv:
        report = {"reference_layers": {}, "project_layers": []}
        target = arcpy.SpatialReference(routing.TARGET_WKID)
        example = arcpy.PointGeometry(arcpy.Point(-114.9018549, 52.2732104), arcpy.SpatialReference(4326)).projectAs(target)
        region = example.buffer(2000)
        for layer in ["SampleClipped_RoadSeg", "_input_Roads_Projected", "SampleClipped_ML_RoadData"]:
            path = routing.gdb_featureclass_path(layer)
            if not arcpy.Exists(path):
                continue
            desc = arcpy.Describe(path)
            fields = arcpy.ListFields(path)
            selected = [field.name for field in fields if field.type not in {"OID", "Geometry", "Blob", "Raster"}
                        and any(word in field.name.upper() for word in ["ELEV", "LEVEL", "GRADE", "STRU", "BRIDGE", "TUNNEL", "ROADJURIS", "ROADCLASS", "NODE"]) ]
            local_records = []
            with arcpy.da.SearchCursor(path, ["OID@", *selected, "SHAPE@"],
                                       spatial_filter=region.projectAs(desc.spatialReference), spatial_relationship="INTERSECTS") as cursor:
                for row in cursor:
                    geometry = row[-1].projectAs(target)
                    local_records.append({"OID": row[0], **{name: str(row[i + 1]) for i, name in enumerate(selected)},
                                          "ExampleDistance_m": example.distanceTo(geometry), "Length_m": geometry.length})
            report["reference_layers"][layer] = {"fields": [{"name": field.name, "type": field.type, "domain": field.domain} for field in fields],
                                                 "has_z": desc.hasZ, "near_example": local_records}
        project_path = r"D:\Work files\Ad-Hoc\Road Usage\ArcGIS\RoadUsage.aprx"
        if Path(project_path).exists():
            project = arcpy.mp.ArcGISProject(project_path)
            for map_ in project.listMaps():
                for layer in map_.listLayers():
                    if layer.supports("DATASOURCE"):
                        report["project_layers"].append({"map": map_.name, "layer": layer.name, "data_source": layer.dataSource,
                                                         "broken": layer.isBroken})
        output = Path(__file__).with_name("Topology_Reference_Inspection.json")
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        for name, info in report["reference_layers"].items():
            print(name, "Z:", info["has_z"], "fields:", [field["name"] for field in info["fields"]], flush=True)
            print("Nearest reference roads:", sorted(info["near_example"], key=lambda row: row["ExampleDistance_m"])[:5], flush=True)
        print("ArcGIS map layers:", json.dumps(report["project_layers"]), flush=True)
        return

    if "--refresh-source-identifiers" in sys.argv:
        path = Path(__file__).with_name("Topology_Input_Cache.pkl")
        cache = pd.read_pickle(path)
        for key, layer in [("private", routing.PRIVATE_ROADS_LAYER), ("public", routing.PUBLIC_ROADS_LAYER)]:
            with arcpy.da.SearchCursor(routing.gdb_featureclass_path(layer), ["SEG_ID", "OID@", "ROAD_CLASS"]) as cursor:
                lookup = {segment: (oid, classification) for segment, oid, classification in cursor}
            frame = cache[key]
            frame["SOURCE_OID"] = frame.SEG_ID.map(lambda value: lookup[value][0])
            frame["ROAD_CLASS"] = frame.SEG_ID.map(lambda value: lookup[value][1])
            frame["SOURCE_LAYER"] = layer
        pd.to_pickle(cache, path)
        print("Cached source OIDs and original road classifications refreshed.", flush=True)
        return

    report = {"layers": {}, "grade_fields": []}
    for name in [routing.PRIVATE_ROADS_LAYER, routing.PUBLIC_ROADS_LAYER] + [
        entry["layer"] for entry in routing.ORIGIN_LAYERS
    ]:
        path = routing.gdb_featureclass_path(name)
        desc = arcpy.Describe(path)
        fields = [field.name for field in arcpy.ListFields(path)]
        report["layers"][name] = {
            "count": int(arcpy.management.GetCount(path)[0]),
            "crs": desc.spatialReference.name,
            "has_z": desc.hasZ,
            "fields": fields,
        }
    print(json.dumps(report, indent=2), flush=True)
    private, public = routing.read_roads()
    origins = routing.load_origins()
    pd.to_pickle({"private": private, "public": public, "origins": origins},
                 Path(__file__).with_name("Topology_Input_Cache.pkl"))
    # Measure private endpoints against actual public geometry, not rounded nodes.
    public_lines = list(public.geometry)
    tree = STRtree(public_lines)
    gaps = []
    touches = 0
    for number, line in enumerate(private.geometry):
        for xy in (line.coords[0], line.coords[-1]):
            point = Point(xy[:2])
            target = public_lines[int(tree.nearest(point))]
            distance = point.distance(target)
            if distance <= 1e-8:
                touches += 1
            elif distance <= 5:
                gaps.append(distance)
        if number and number % 100000 == 0:
            print(f"Measured {number:,} private roads", flush=True)
    bins = [0, 0.001, 0.01, 0.1, 0.25, 0.5, 1, 2, 5]
    report["private_endpoint_public_gaps"] = {
        "exact_touches": touches,
        "bin_limits_m": bins,
        "bin_counts": np.histogram(gaps, bins=bins)[0].tolist(),
        "gap_quantiles_m": np.quantile(gaps, [0, .25, .5, .75, .95, 1]).tolist() if gaps else [],
    }
    path = Path(__file__).with_name("Topology_Input_Inspection.json")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
