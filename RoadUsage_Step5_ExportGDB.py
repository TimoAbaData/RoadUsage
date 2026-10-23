"""Step 5: copy completed GeoPackage feature classes into a separate file GDB.

Run alone to export an existing run, or automatically through RoadUsage_Run.py.
No road preparation, routing, reporting, or source GeoPackage edits are performed.
"""
from collections import Counter
import argparse
import hashlib
import json
import logging
from pathlib import Path

from RoadUsage_Workflow import DEFAULT_CONFIG, prepare_environment, write_json

LOG = logging.getLogger(__name__)
OUTPUT_NAME = "RoadUsage_Final.gdb"
SOURCES = (("RoadUsage_Routes.gpkg", "Step2_Complete.json", {"Generated_Routes", "Origin_Results"}),
           ("RoadUsage_CompleteRoute.gpkg", "Step4_Complete.json", {"CompleteRoute"}))


def source_packages(folder):
    """Require completed sources, including CompleteRoute whenever it is present."""
    packages = []
    for filename, marker_name, required_layers in SOURCES:
        path, marker = folder / filename, folder / marker_name
        if filename != SOURCES[0][0] and not path.exists() and not marker.exists():
            continue
        if not path.is_file() or not marker.is_file():
            raise FileNotFoundError(f"Completed export requires both {path.name} and {marker.name}")
        if json.loads(marker.read_text(encoding="utf-8"))["Status"] != "COMPLETE":
            raise ValueError(f"Source step is not complete: {marker}")
        packages.append((path, required_layers))
    return packages


def attribute_signature(arcpy, dataset, fields):
    """Compare every attribute, including long JSON lists, independent of row order."""
    names = [field.name for field in fields]
    text_lengths = {field.name: 0 for field in fields if field.type == "String"}
    signature = Counter()
    with arcpy.da.SearchCursor(str(dataset), names) as rows:
        for row in rows:
            payload = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
            signature[hashlib.sha256(payload.encode("utf-8")).digest()] += 1
            for name, value in zip(names, row):
                if name in text_lengths and value is not None:
                    # ArcGIS field widths also accommodate supplementary Unicode characters.
                    text_lengths[name] = max(text_lengths[name], len(value.encode("utf-16-le")) // 2)
    return signature, text_lengths


def copy_feature_class(arcpy, source, target):
    fields = [field for field in arcpy.ListFields(str(source)) if field.type not in ("OID", "Geometry")]
    signature, text_lengths = attribute_signature(arcpy, source, fields)
    mappings = arcpy.FieldMappings()
    for field in fields:
        mapping = arcpy.FieldMap()
        mapping.addInputField(str(source), field.name)
        output_field = mapping.outputField
        output_field.name = field.name
        if field.type == "String":
            # GeoPackage TEXT reports an unlimited width. Use its actual maximum,
            # not an arbitrary 255-character limit that could truncate JSON lists.
            output_field.length = max(255, text_lengths[field.name])
        mapping.outputField = output_field
        mappings.addFieldMap(mapping)
    arcpy.conversion.ExportFeatures(str(source), str(target), field_mapping=mappings)
    source_description, target_description = arcpy.Describe(str(source)), arcpy.Describe(str(target))
    if (source_description.shapeType != target_description.shapeType or
            source_description.spatialReference.factoryCode != target_description.spatialReference.factoryCode):
        raise RuntimeError(f"Geometry type or coordinate system changed: {target}")
    target_fields = {field.name: field for field in arcpy.ListFields(str(target))}
    for field in fields:
        if field.name not in target_fields or target_fields[field.name].type != field.type:
            raise RuntimeError(f"Field name/type changed: {target.name}.{field.name}")
    copied, _ = attribute_signature(arcpy, target, fields)
    count = sum(signature.values())
    if copied != signature or int(arcpy.management.GetCount(str(target))[0]) != count:
        raise RuntimeError(f"GDB attributes or feature count differ from GeoPackage: {target}")
    return {"Source": str(source), "FeatureClass": str(target), "FeatureCount": count,
            "AttributesVerified": True, "MaximumTextLengths": text_lengths,
            "WKID": target_description.spatialReference.factoryCode}


def export_geodatabase(folder, output_gdb=None):
    folder = Path(folder).resolve()
    output = Path(output_gdb).resolve() if output_gdb else folder / OUTPUT_NAME
    marker = folder / "Step5_Complete.json"
    if output.suffix.lower() != ".gdb":
        raise ValueError("Output must be a file geodatabase path ending in .gdb")
    if output.exists() or marker.exists():
        raise FileExistsError(f"GDB export already exists: {output} or {marker}; existing outputs are preserved")
    packages = source_packages(folder)
    prepare_environment()
    import arcpy

    # A standalone process keeps ArcGIS DLL initialization separate from Fiona/GDAL.
    with arcpy.EnvManager(overwriteOutput=False, extent=None, outputCoordinateSystem=None):
        plan, names = [], set()
        for package, required in packages:
            with arcpy.EnvManager(workspace=str(package)):
                layers = arcpy.ListFeatureClasses() or []
            found = set()
            for layer in layers:
                name = layer.rsplit(".", 1)[-1]  # main.CompleteRoute -> CompleteRoute
                if name.casefold() in names:
                    raise ValueError(f"Duplicate destination feature class name: {name}")
                names.add(name.casefold())
                found.add(name)
                plan.append((package / layer, output / name))
            if required - found:
                raise ValueError(f"Missing expected layers in {package.name}: {sorted(required - found)}")
        output.parent.mkdir(parents=True, exist_ok=True)
        arcpy.management.CreateFileGDB(str(output.parent), output.name)
        results = []
        for source, target in plan:
            LOG.info("Exporting %s -> %s", source.name, target)
            result = copy_feature_class(arcpy, source, target)
            results.append(result)
            LOG.info("Verified %s: %s features; all attributes preserved", target.name, result["FeatureCount"])
    write_json(marker, {"Status": "COMPLETE", "FileGDB": str(output), "FeatureClasses": results,
                        "SourceGeoPackages": [str(path) for path, _ in packages],
                        "Note": "Additional copies only. GeoPackages and prior processing results are unchanged."
                                " GDB OBJECTIDs and Shape_Length are system-managed."})
    LOG.info("GDB export complete: %s", output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Accepted for workflow compatibility; export uses the completed GeoPackages")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-gdb", type=Path, help="Optional new GDB path; defaults to RoadUsage_Final.gdb in the run folder")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    export_geodatabase(args.run_dir, args.output_gdb)


if __name__ == "__main__":
    main()
