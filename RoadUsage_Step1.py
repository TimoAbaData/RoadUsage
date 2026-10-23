# ============================================================================
# STEP 1 - Recover attributes onto ML-generated Road_Network
#
# ArcGIS Pro 3.5 / ArcPy
# ArcGIS Basic compatible
#
# INPUTS
#   1. Road_Network  - ML-generated lines, geometry only
#   2. Roads         - source roads, contains ROADJURIS
#   3. Disposition   - closed-loop polylines, unique ID = DISP_NUM
#
# OUTPUT
#   Road_Network_Attributed
#
# Workflow
#   1. Convert each closed Disposition polyline to polygon geometry
#   2. Intersect Road_Network with disposition polygons
#   3. Preserve road portions outside dispositions
#   4. Split into singlepart road segments
#   5. Match each segment back to original Roads using proximity + overlap
#   6. Recover ROADJURIS
#   7. Classify PUBLIC / PRIVATE
#   8. Add QC fields
# ============================================================================

arcpy = None  # Loaded only when processing starts; --help needs no license.
import os
import math
import time
from collections import defaultdict


# ============================================================================
# USER SETTINGS
# ============================================================================

# --- CHANGE THESE PATHS ---

ROAD_NETWORK_SOURCE  = r"F:\Road Usage\Progress\RoadUsageProcess.gdb\SampleClipped_ML_RoadData"
ROADS_SOURCE  = r"F:\Road Usage\Progress\RoadUsageProcess.gdb\SampleClipped_RoadSeg"
DISPOSITION_SOURCE  = r"F:\Road Usage\Progress\RoadUsageProcess.gdb\SampleClipped_DIDS"
DISPOSITION_WHERE_CLAUSE = None
MIN_DISPOSITION_OVERLAP_M = 0.0
OUT_GDB = r"F:\Road Usage\Progress\RoadUsageProcess.gdb"
TARGET_WKID = 3400

ROAD_NETWORK = os.path.join(OUT_GDB, "_input_Road_Network_Projected")
ROADS = os.path.join(OUT_GDB, "_input_Roads_Projected")
DISPOSITION = os.path.join(OUT_GDB, "_input_Disposition_Projected")

# ---------------------------------------------------------------------------
# Known fields
# ---------------------------------------------------------------------------

DISP_ID_FIELD = "DISP_NUM"

ROAD_JURIS_FIELD = "ROADJURIS"


# ---------------------------------------------------------------------------
# Output names
# ---------------------------------------------------------------------------

OUT_NAME = "Road_Network_Attributed"
PRIVATE_UNKNOWN_NAME = "Road_Network_Private_Unknown"
PUBLIC_NAME = "Road_Network_Public"
DISP_POLY_NAME = "Disposition_Polygons"
DISP_QC_NAME = "Disposition_Polygon_QC"


def prepare_projected_inputs():
    from RoadUsage_Workflow import disposition_filter_layer

    msg("")
    msg("--------------------------------------------------")
    msg("Preparing projected working datasets...")
    msg("--------------------------------------------------")

    target_sr = arcpy.SpatialReference(TARGET_WKID)
    inputs = [
        (
            ROAD_NETWORK_SOURCE,
            ROAD_NETWORK,
            "Road_Network"
        ),
        (
            ROADS_SOURCE,
            ROADS,
            "Roads"
        ),
        (
            DISPOSITION_SOURCE,
            DISPOSITION,
            "Disposition"
        )
    ]

    for source_fc, output_fc, label in inputs:

        if not arcpy.Exists(source_fc):
            raise RuntimeError(
                f"{label} does not exist:\n{source_fc}"
            )

        source_sr = arcpy.Describe( source_fc).spatialReference

        msg("")
        msg(f"{label}")
        msg(f"  Source CRS : {source_sr.name}")
        msg(f"  Target CRS : {target_sr.name}")

        input_fc = source_fc
        filtered_layer = None
        if label == "Disposition":
            filtered_layer, source_count, selected_count = disposition_filter_layer(arcpy, source_fc, DISPOSITION_WHERE_CLAUSE, "step1_filtered_dispositions")
            input_fc = filtered_layer
            msg(f"  Disposition filter: {DISPOSITION_WHERE_CLAUSE}")
            msg(f"  Filtered features : {selected_count:,} / {source_count:,}")

        delete_if_exists(output_fc)

        # ---------------------------------------------------------------
        # If already in target CRS, simply copy.
        # Otherwise project into Alberta 10-TM.
        # ---------------------------------------------------------------

        same_crs = False

        if (
            source_sr.factoryCode
            and
            target_sr.factoryCode
        ):
            same_crs = (
                source_sr.factoryCode
                == target_sr.factoryCode
            )

        if same_crs:

            msg("  Already in target CRS - copying.")

            arcpy.management.CopyFeatures(
                input_fc,
                output_fc
            )

        else:

            if source_sr.name == "Unknown":

                raise RuntimeError(
                    f"{label} has an UNKNOWN coordinate system.\n"
                    "Define its correct coordinate system before running "
                    "this script. Do not use Project on unknown coordinates."
                )

            msg("  Projecting...")

            arcpy.management.Project(
                in_dataset=input_fc,
                out_dataset=output_fc,
                out_coor_system=target_sr
            )

        count = int(
            arcpy.management.GetCount(
                output_fc
            )[0]
        )

        msg(
            f"  Features   : {count:,}"
        )
        if filtered_layer is not None:
            arcpy.management.Delete(filtered_layer)

    msg("")
    msg("Projected inputs prepared successfully.")

# ---------------------------------------------------------------------------
# Matching tolerances
#
# These values assume the data is in a projected coordinate system using
# METRES, such as NAD83 / Alberta 10-TM.
# ---------------------------------------------------------------------------

# A disposition whose start/end vertices are <= this distance apart
# will be closed automatically.
#
# Since you said the data is reliably closed, this is only protection
# against tiny coordinate differences.
MAX_DISP_CLOSE_GAP = 0.50       # metres


# Search original Roads within this distance of each ML segment.
ROAD_SEARCH_RADIUS = "10 Meters"


# Buffer around each candidate source road used to determine how much
# of the ML road follows that candidate.
ROAD_OVERLAP_BUFFER = 3.0       # metres


# >= 80% of segment follows original road = strong match
ROAD_HIGH_CONF = 0.80

# 50%-80% = keep match but flag for review
ROAD_LOW_CONF = 0.50


# If two Roads candidates have nearly equal match percentages,
# flag the result as ambiguous.
ROAD_AMBIGUITY_DELTA = 0.05


# Very tiny segments generally indicate boundary/sliver issues.
MIN_SEGMENT_LENGTH = 2.0        # metres


# ROADJURIS expected values.
#
# Add additional values here if ROADJURIS uses codes.
PUBLIC_VALUES = {
    "PUBLIC",
    "PUB"
}

PRIVATE_VALUES = {
    "PRIVATE",
    "PRIV",
    "PVT"
}


# ============================================================================
# WORKING DATASETS
# ============================================================================

OUT_FC = os.path.join(OUT_GDB, OUT_NAME)
PRIVATE_UNKNOWN_FC = os.path.join( OUT_GDB,PRIVATE_UNKNOWN_NAME)
PUBLIC_FC = os.path.join(OUT_GDB, PUBLIC_NAME)
DISP_POLY = os.path.join(OUT_GDB, DISP_POLY_NAME)
DISP_QC = os.path.join(OUT_GDB, DISP_QC_NAME)

ROAD_WORK = os.path.join(OUT_GDB, "_wrk_RoadNetwork")
MATCHED_FC = os.path.join(OUT_GDB, "_wrk_DispMatched")
UNMATCHED_FC = os.path.join(OUT_GDB, "_wrk_DispUnmatched")
MERGED_FC = os.path.join(OUT_GDB, "_wrk_RoadMerged")
NEAR_TABLE = os.path.join(OUT_GDB, "_wrk_RoadNear")


ML_ID_FIELD = "ML_SEG_ID"





# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def msg(text):
    """Print to console and ArcGIS geoprocessing messages."""
    print(text)

    try:
        arcpy.AddMessage(text)
    except Exception:
        pass


def delete_if_exists(dataset):
    if arcpy.Exists(dataset):
        arcpy.management.Delete(dataset)


def field_exists(fc, field_name):
    return any(
        f.name.upper() == field_name.upper()
        for f in arcpy.ListFields(fc)
    )


def add_field_if_missing(fc, name, field_type, length=None):
    if field_exists(fc, name):
        return

    if field_type.upper() == "TEXT":
        arcpy.management.AddField(
            fc,
            name,
            field_type,
            field_length=length
        )
    else:
        arcpy.management.AddField(
            fc,
            name,
            field_type
        )


def normalize_jurisdiction(value):
    """
    Convert ROADJURIS to standardized PUBLIC / PRIVATE / UNKNOWN.
    """

    if value is None:
        return "UNKNOWN"

    value = str(value).strip().upper()

    if value in PUBLIC_VALUES:
        return "PUBLIC"

    if value in PRIVATE_VALUES:
        return "PRIVATE"

    return "UNKNOWN"


def spatial_references_match(sr1, sr2):
    """
    Basic CRS comparison.
    """

    if sr1.factoryCode and sr2.factoryCode:
        return sr1.factoryCode == sr2.factoryCode

    return sr1.name == sr2.name


def create_output_gdb():
    """
    Create output File GDB if it does not already exist.
    """

    if arcpy.Exists(OUT_GDB):
        return

    parent = os.path.dirname(OUT_GDB)
    name = os.path.basename(OUT_GDB)

    msg(f"Creating output geodatabase: {OUT_GDB}")

    arcpy.management.CreateFileGDB(
        parent,
        name
    )

def export_road_class_layers():

    msg("")
    msg("--------------------------------------------------")
    msg("Creating road classification outputs...")
    msg("--------------------------------------------------")

    # Make script re-runnable.
    delete_if_exists(PRIVATE_UNKNOWN_FC)
    delete_if_exists(PUBLIC_FC)

    road_class_field = arcpy.AddFieldDelimiters(
        OUT_FC,
        "ROAD_CLASS"
    )

    # -----------------------------------------------------------------------
    # PRIVATE + UNKNOWN
    # -----------------------------------------------------------------------

    private_unknown_where = (
        f"{road_class_field} IN ('PRIVATE', 'UNKNOWN')"
    )

    arcpy.conversion.ExportFeatures(
        in_features=OUT_FC,
        out_features=PRIVATE_UNKNOWN_FC,
        where_clause=private_unknown_where
    )

    private_unknown_count = int(
        arcpy.management.GetCount(
            PRIVATE_UNKNOWN_FC
        )[0]
    )

    msg(
        f"PRIVATE + UNKNOWN roads : "
        f"{private_unknown_count:,}"
    )

    # -----------------------------------------------------------------------
    # PUBLIC ONLY
    # -----------------------------------------------------------------------

    public_where = (
        f"{road_class_field} = 'PUBLIC'"
    )

    arcpy.conversion.ExportFeatures(
        in_features=OUT_FC,
        out_features=PUBLIC_FC,
        where_clause=public_where
    )

    public_count = int(
        arcpy.management.GetCount(
            PUBLIC_FC
        )[0]
    )

    msg(
        f"PUBLIC roads            : "
        f"{public_count:,}"
    )

    msg("")
    msg("Road classification outputs created.")
# ============================================================================
# VALIDATION
# ============================================================================

def validate_inputs():

    msg("Validating inputs...")

    for dataset in [ROAD_NETWORK, ROADS, DISPOSITION]:

        if not arcpy.Exists(dataset):
            raise RuntimeError(
                f"Input does not exist:\n{dataset}"
            )

    if not field_exists(DISPOSITION, DISP_ID_FIELD):
        raise RuntimeError(
            f"{DISPOSITION} does not contain required field "
            f"'{DISP_ID_FIELD}'."
        )

    if not field_exists(ROADS, ROAD_JURIS_FIELD):
        raise RuntimeError(
            f"{ROADS} does not contain required field "
            f"'{ROAD_JURIS_FIELD}'."
        )

    rn_desc = arcpy.Describe(ROAD_NETWORK)
    rd_desc = arcpy.Describe(ROADS)
    dp_desc = arcpy.Describe(DISPOSITION)

    if rn_desc.shapeType != "Polyline":
        raise RuntimeError("Road_Network must be Polyline.")

    if rd_desc.shapeType != "Polyline":
        raise RuntimeError("Roads must be Polyline.")

    if dp_desc.shapeType != "Polyline":
        raise RuntimeError("Disposition must be Polyline.")

    rn_sr = rn_desc.spatialReference
    rd_sr = rd_desc.spatialReference
    dp_sr = dp_desc.spatialReference

    if rn_sr.type != "Projected":
        raise RuntimeError(
            "Road_Network must use a projected coordinate system. "
            "Do not run this workflow in latitude/longitude."
        )

    if not spatial_references_match(rn_sr, rd_sr):
        raise RuntimeError(
            "Road_Network and Roads have different coordinate systems.\n"
            f"Road_Network: {rn_sr.name}\n"
            f"Roads: {rd_sr.name}"
        )

    if not spatial_references_match(rn_sr, dp_sr):
        raise RuntimeError(
            "Road_Network and Disposition have different coordinate systems.\n"
            f"Road_Network: {rn_sr.name}\n"
            f"Disposition: {dp_sr.name}"
        )

    msg(f"Coordinate system: {rn_sr.name}")
    msg(f"Linear units: {rn_sr.linearUnitName}")

    if "meter" not in rn_sr.linearUnitName.lower():
        raise RuntimeError(
            "This script currently assumes metre-based projected coordinates. "
            "Adjust the distance tolerances before running in another unit."
        )


# ============================================================================
# CREATE DISPOSITION POLYGONS
# ============================================================================

def create_disposition_polygons():

    msg("")
    msg("--------------------------------------------------")
    msg("Creating polygons from closed Disposition lines...")
    msg("--------------------------------------------------")

    delete_if_exists(DISP_POLY)
    delete_if_exists(DISP_QC)

    desc = arcpy.Describe(DISPOSITION)
    sr = desc.spatialReference

    out_path = os.path.dirname(DISP_POLY)
    out_name = os.path.basename(DISP_POLY)

    # ------------------------------------------------------------------
    # Create polygon output using Disposition schema
    # ------------------------------------------------------------------
    arcpy.management.CreateFeatureclass(
        out_path=out_path,
        out_name=out_name,
        geometry_type="POLYGON",
        template=DISPOSITION,
        has_m="DISABLED",
        has_z="DISABLED",
        spatial_reference=sr
    )

    add_field_if_missing(
        DISP_POLY,
        "SRC_DISP_OID",
        "LONG"
    )

    add_field_if_missing(
        DISP_POLY,
        "DISP_PART",
        "LONG"
    )

    add_field_if_missing(
        DISP_POLY,
        "DISP_GAP",
        "DOUBLE"
    )

    add_field_if_missing(
        DISP_POLY,
        "DISP_GEOM_QC",
        "TEXT",
        40
    )

    # ------------------------------------------------------------------
    # QC table
    # ------------------------------------------------------------------
    arcpy.management.CreateTable(
        OUT_GDB,
        os.path.basename(DISP_QC)
    )

    add_field_if_missing(
        DISP_QC,
        "DISP_NUM",
        "TEXT",
        255
    )

    add_field_if_missing(
        DISP_QC,
        "SRC_DISP_OID",
        "LONG"
    )

    add_field_if_missing(
        DISP_QC,
        "PART_NO",
        "LONG"
    )

    add_field_if_missing(
        DISP_QC,
        "GAP_DIST",
        "DOUBLE"
    )

    add_field_if_missing(
        DISP_QC,
        "QC_FLAG",
        "TEXT",
        100
    )

    # ------------------------------------------------------------------
    # Determine fields to transfer from Disposition
    # ------------------------------------------------------------------
    attribute_fields = []

    for f in arcpy.ListFields(DISPOSITION):

        if f.type in (
            "OID",
            "Geometry",
            "Blob",
            "Raster",
            "GlobalID"
        ):
            continue

        if f.required:
            continue

        if not f.editable:
            continue

        attribute_fields.append(f.name)

    if DISP_ID_FIELD not in attribute_fields:

        raise RuntimeError(
            f"Could not access {DISP_ID_FIELD} "
            f"in Disposition."
        )

    disp_num_index = attribute_fields.index(
        DISP_ID_FIELD
    )

    search_fields = [
        "OID@",
        "SHAPE@"
    ] + attribute_fields

    insert_fields = [
        "SHAPE@",
        "SRC_DISP_OID",
        "DISP_PART",
        "DISP_GAP",
        "DISP_GEOM_QC"
    ] + attribute_fields

    qc_insert_fields = [
        "DISP_NUM",
        "SRC_DISP_OID",
        "PART_NO",
        "GAP_DIST",
        "QC_FLAG"
    ]

    valid_count = 0
    failed_count = 0
    small_gap_count = 0

    # ================================================================
    # IMPORTANT:
    #
    # Do NOT open a second InsertCursor here.
    #
    # QC rows are held temporarily in Python and written only after
    # the polygon InsertCursor has completely closed.
    # ================================================================
    qc_rows = []

    # ------------------------------------------------------------------
    # First pass:
    # read Disposition and write valid polygons
    # ------------------------------------------------------------------
    with arcpy.da.SearchCursor(
        DISPOSITION,
        search_fields
    ) as search_cur:

        with arcpy.da.InsertCursor(
            DISP_POLY,
            insert_fields
        ) as insert_cur:

            for row in search_cur:

                source_oid = row[0]
                geometry = row[1]

                attrs = list(row[2:])

                disp_num = attrs[
                    disp_num_index
                ]

                # ------------------------------------------------------
                # Null geometry
                # ------------------------------------------------------
                if geometry is None:

                    qc_rows.append(
                        [
                            str(disp_num),
                            source_oid,
                            None,
                            None,
                            "NULL_GEOMETRY"
                        ]
                    )

                    failed_count += 1
                    continue

                # ------------------------------------------------------
                # Process multipart geometries individually
                # ------------------------------------------------------
                for part_no, part in enumerate(
                    geometry,
                    start=1
                ):

                    points = [
                        p
                        for p in part
                        if p is not None
                    ]

                    if len(points) < 3:

                        qc_rows.append(
                            [
                                str(disp_num),
                                source_oid,
                                part_no,
                                None,
                                "TOO_FEW_POINTS"
                            ]
                        )

                        failed_count += 1
                        continue

                    first = points[0]
                    last = points[-1]

                    # --------------------------------------------------
                    # Endpoint gap in projected units
                    # --------------------------------------------------
                    gap = math.hypot(
                        first.X - last.X,
                        first.Y - last.Y
                    )

                    # --------------------------------------------------
                    # Do not force-close a larger gap
                    # --------------------------------------------------
                    if gap > MAX_DISP_CLOSE_GAP:

                        qc_rows.append(
                            [
                                str(disp_num),
                                source_oid,
                                part_no,
                                gap,
                                "OPEN_LOOP"
                            ]
                        )

                        failed_count += 1
                        continue

                    # --------------------------------------------------
                    # Construct clean 2D ring
                    # --------------------------------------------------
                    point_array = arcpy.Array()

                    for p in points:

                        point_array.add(
                            arcpy.Point(
                                p.X,
                                p.Y
                            )
                        )

                    polygon_qc = "OK"

                    # Explicitly close tiny gaps
                    if gap > 0:

                        point_array.add(
                            arcpy.Point(
                                first.X,
                                first.Y
                            )
                        )

                        polygon_qc = (
                            "SMALL_GAP_CLOSED"
                        )

                        small_gap_count += 1

                    # --------------------------------------------------
                    # Construct polygon
                    # --------------------------------------------------
                    try:

                        polygon = arcpy.Polygon(
                            point_array,
                            sr
                        )

                        if (
                            polygon is None
                            or polygon.area <= 0
                        ):

                            qc_rows.append(
                                [
                                    str(disp_num),
                                    source_oid,
                                    part_no,
                                    gap,
                                    "INVALID_POLYGON"
                                ]
                            )

                            failed_count += 1
                            continue

                        insert_cur.insertRow(
                            [
                                polygon,
                                source_oid,
                                part_no,
                                gap,
                                polygon_qc
                            ]
                            + attrs
                        )

                        valid_count += 1

                    except Exception as ex:

                        qc_rows.append(
                            [
                                str(disp_num),
                                source_oid,
                                part_no,
                                gap,
                                "POLYGON_ERROR"
                            ]
                        )

                        failed_count += 1

                        msg(
                            f"Polygon error: "
                            f"DISP_NUM={disp_num}, "
                            f"OID={source_oid}, "
                            f"Part={part_no}, "
                            f"Error={ex}"
                        )

    # ================================================================
    # At this point:
    #
    # SearchCursor CLOSED
    # DISP_POLY InsertCursor CLOSED
    #
    # Now it is safe to open the QC InsertCursor.
    # ================================================================

    arcpy.management.ClearWorkspaceCache()

    if qc_rows:

        with arcpy.da.InsertCursor(
            DISP_QC,
            qc_insert_fields
        ) as qc_cur:

            for qc_row in qc_rows:

                qc_cur.insertRow(
                    qc_row
                )

    msg("")
    msg("Disposition polygon conversion results:")
    msg(
        f"  Valid polygons       : "
        f"{valid_count:,}"
    )
    msg(
        f"  Small gaps closed    : "
        f"{small_gap_count:,}"
    )
    msg(
        f"  Failed/open parts    : "
        f"{failed_count:,}"
    )

    if failed_count:

        msg("")
        msg(
            "Some Disposition features require QC."
        )

        msg(
            f"Review table:\n{DISP_QC}")


# ============================================================================
# PREPARE ML ROAD NETWORK
# ============================================================================

def prepare_road_network():

    msg("")
    msg("Preparing Road_Network...")

    delete_if_exists(ROAD_WORK)

    arcpy.management.CopyFeatures(
        ROAD_NETWORK,
        ROAD_WORK
    )

    add_field_if_missing(
        ROAD_WORK,
        ML_ID_FIELD,
        "LONG"
    )

    # Create stable ML feature ID for this processing run.
    with arcpy.da.UpdateCursor(
        ROAD_WORK,
        ["OID@", ML_ID_FIELD]
    ) as cur:

        for row in cur:
            row[1] = row[0]
            cur.updateRow(row)


# ============================================================================
# SHORT DISPOSITION OVERLAPS
# ============================================================================

def smooth_short_disposition_matches(maximum_m):
    """Relabel short continuous ML-road overlaps in the temporary overlay.

    Only a longer disposition touching the same original ML line can inherit a
    short run. Fully covered duplicate linework is removed. An isolated short
    run is removed and becomes unmatched when the outside pieces are rebuilt.
    Road geometry and original source feature classes are never modified.
    """
    if maximum_m <= 0:
        return
    matched = {field.name.lower(): field.name for field in arcpy.ListFields(MATCHED_FC)}
    attr_fields = [matched[field.name.lower()] for field in arcpy.ListFields(DISP_POLY)
                   if not field.required and field.type not in ("OID", "Geometry", "Blob", "Raster")
                   and field.name.lower() in matched and field.name.lower() != ML_ID_FIELD.lower()]
    disp_field = next((name for name in attr_fields if name.lower() == DISP_ID_FIELD.lower()), None)
    if disp_field is None:
        raise ValueError(f"Disposition field {DISP_ID_FIELD} missing from overlay")
    disp_index = attr_fields.index(disp_field)
    changes = {}
    short_runs = 0

    def process_group(rows):
        nonlocal short_runs
        if not rows:
            return
        parent = list(range(len(rows)))

        def root(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        by_disp = defaultdict(list)
        for index, row in enumerate(rows):
            by_disp[str(row["attrs"][disp_index])].append(index)
        for indices in by_disp.values():
            for position, left in enumerate(indices):
                for right in indices[position + 1:]:
                    if rows[left]["geom"].distanceTo(rows[right]["geom"]) <= 0.05:
                        parent[root(right)] = root(left)
        groups = defaultdict(list)
        for index, row in enumerate(rows):
            groups[root(index)].append(row)
        runs = []
        for members in groups.values():
            combined = members[0]["geom"]
            for member in members[1:]:
                combined = combined.union(member["geom"])
            runs.append({"rows": members, "geom": combined, "length": combined.length,
                         "disp": str(members[0]["attrs"][disp_index])})
        longer = [run for run in runs if run["length"] > maximum_m + 1e-7]
        for short in runs:
            if short["length"] > maximum_m + 1e-7:
                continue
            short_runs += 1
            touching = [run for run in longer if run["disp"] != short["disp"]
                        and short["geom"].distanceTo(run["geom"]) <= 0.25]
            winner = min(touching, key=lambda run: (-run["length"], run["disp"])) if touching else None
            for member in short["rows"]:
                if winner is None:
                    changes[member["oid"]] = None
                else:
                    # A smaller disposition entirely inside a larger one
                    # must not create a duplicate route edge.
                    try:
                        redundant = member["geom"].difference(winner["geom"]).length <= 0.05
                    except Exception:
                        redundant = False
                    changes[member["oid"]] = None if redundant else winner["rows"][0]["attrs"]

    fields = ["OID@", ML_ID_FIELD, "SHAPE@"] + attr_fields
    with arcpy.da.SearchCursor(MATCHED_FC, fields,
                               sql_clause=(None, f"ORDER BY {ML_ID_FIELD}")) as cursor:
        current_id, rows = None, []
        for oid, ml_id, geom, *attrs in cursor:
            if current_id is not None and ml_id != current_id:
                process_group(rows)
                rows = []
            current_id = ml_id
            if geom is not None:
                rows.append({"oid": oid, "geom": geom, "attrs": attrs})
        process_group(rows)
    reassigned = removed = 0
    with arcpy.da.UpdateCursor(MATCHED_FC, ["OID@"] + attr_fields) as cursor:
        for row in cursor:
            oid = row[0]
            if oid not in changes:
                continue
            attrs = changes[oid]
            if attrs is None:
                cursor.deleteRow()
                removed += 1
            else:
                row[1:] = attrs
                cursor.updateRow(row)
                reassigned += 1
    msg(f"Short disposition runs (<= {maximum_m:g} m): {short_runs:,}; "
        f"reassigned pieces: {reassigned:,}; removed pieces: {removed:,}")


# ============================================================================
# DISPOSITION OVERLAY
# ============================================================================

def disposition_overlay():

    msg("")
    msg("--------------------------------------------------")
    msg("Overlaying Road_Network with Disposition polygons...")
    msg("--------------------------------------------------")

    for fc in [
        MATCHED_FC,
        UNMATCHED_FC,
        MERGED_FC,
        OUT_FC
    ]:
        delete_if_exists(fc)

    # -----------------------------------------------------------------------
    # Intersect produces road pieces INSIDE disposition polygons.
    #
    # Because this is exactly two inputs, it is compatible with Basic.
    # -----------------------------------------------------------------------

    arcpy.analysis.Intersect(
        [ROAD_WORK, DISP_POLY],
        MATCHED_FC,
        "ALL",
        None,
        "LINE"
    )

    smooth_short_disposition_matches(MIN_DISPOSITION_OVERLAP_M)

    matched_count = int(
        arcpy.management.GetCount(MATCHED_FC)[0]
    )

    msg(
        f"Disposition-matched pieces produced: "
        f"{matched_count:,}"
    )

    # -----------------------------------------------------------------------
    # Store union of matched geometry for each original ML road.
    #
    # We will subtract these pieces from the original geometry to retain
    # portions OUTSIDE all dispositions.
    # -----------------------------------------------------------------------

    matched_union = {}
    matched_records = defaultdict(list)
    disp_ids_by_ml = defaultdict(set)

    if matched_count:

        with arcpy.da.SearchCursor(
            MATCHED_FC,
            [
                ML_ID_FIELD,
                DISP_ID_FIELD,
                "SHAPE@"
            ]
        ) as cur:

            for ml_id, disp_num, geom in cur:

                if geom is None:
                    continue

                matched_records[ml_id].append(
                    (disp_num, geom)
                )

                if disp_num not in (None, ""):
                    disp_ids_by_ml[ml_id].add(
                        str(disp_num)
                    )

                if ml_id not in matched_union:
                    matched_union[ml_id] = geom

                else:
                    matched_union[ml_id] = (
                        matched_union[ml_id].union(geom)
                    )

    # -----------------------------------------------------------------------
    # Detect actual overlapping disposition assignments.
    #
    # This is different from a road sequentially crossing Disp A then Disp B.
    # We only flag where TWO disposition geometries cover the SAME line.
    # -----------------------------------------------------------------------

    overlapping_disp_ml_ids = set()

    for ml_id, items in matched_records.items():

        if len(items) < 2:
            continue

        for i in range(len(items)):

            disp1, geom1 = items[i]

            for j in range(i + 1, len(items)):

                disp2, geom2 = items[j]

                if str(disp1) == str(disp2):
                    continue

                try:

                    overlap = geom1.intersect(
                        geom2,
                        2       # 2 = line dimension
                    )

                    if overlap and overlap.length > 0.05:
                        overlapping_disp_ml_ids.add(ml_id)

                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # Create feature class to hold road pieces not inside dispositions.
    # -----------------------------------------------------------------------

    arcpy.management.CreateFeatureclass(
        out_path=OUT_GDB,
        out_name=os.path.basename(UNMATCHED_FC),
        geometry_type="POLYLINE",
        template=ROAD_WORK,
        spatial_reference=arcpy.Describe(
            ROAD_WORK
        ).spatialReference
    )

    disp_coverage_by_ml = {}

    with arcpy.da.InsertCursor(
        UNMATCHED_FC,
        ["SHAPE@", ML_ID_FIELD]
    ) as insert_cur:

        with arcpy.da.SearchCursor(
            ROAD_WORK,
            [ML_ID_FIELD, "SHAPE@"]
        ) as search_cur:

            for ml_id, original_geom in search_cur:

                if original_geom is None:
                    continue

                original_length = original_geom.length

                matched_geom = matched_union.get(ml_id)

                if matched_geom is None:

                    # No disposition coverage at all.
                    disp_coverage_by_ml[ml_id] = 0.0

                    insert_cur.insertRow([
                        original_geom,
                        ml_id
                    ])

                    continue

                # Percentage of the ORIGINAL ML line covered by any
                # disposition polygon.
                if original_length > 0:

                    coverage = (
                        matched_geom.length
                        / original_length
                    ) * 100.0

                    coverage = min(
                        max(coverage, 0.0),
                        100.0
                    )

                else:
                    coverage = 0.0

                disp_coverage_by_ml[ml_id] = coverage

                # Remove disposition-covered geometry.
                try:

                    outside_geom = original_geom.difference(
                        matched_geom
                    )

                    if (
                        outside_geom
                        and outside_geom.length > 0.001
                    ):

                        insert_cur.insertRow([
                            outside_geom,
                            ml_id
                        ])

                except Exception as ex:

                    msg(
                        f"Difference failed for ML_SEG_ID="
                        f"{ml_id}: {ex}"
                    )

    unmatched_count = int(
        arcpy.management.GetCount(
            UNMATCHED_FC
        )[0]
    )

    msg(
        f"Outside/unmatched road pieces: "
        f"{unmatched_count:,}"
    )

    # -----------------------------------------------------------------------
    # Merge matched and unmatched pieces.
    #
    # Matched records contain all Disposition attributes.
    # Unmatched pieces will receive NULL disposition attributes.
    # -----------------------------------------------------------------------

    arcpy.management.Merge(
        [MATCHED_FC, UNMATCHED_FC],
        MERGED_FC
    )

    # Ensure every geometry part becomes its own record.
    arcpy.management.MultipartToSinglepart(
        MERGED_FC,
        OUT_FC
    )

    return (
        disp_coverage_by_ml,
        disp_ids_by_ml,
        overlapping_disp_ml_ids
    )


# ============================================================================
# ADD OUTPUT FIELDS
# ============================================================================

def add_output_fields():

    fields = [
        ("SEG_ID", "LONG", None),
        ("SEG_LEN_M", "DOUBLE", None),

        ("DISP_COV_PCT", "DOUBLE", None),
        ("ML_DISP_CNT", "SHORT", None),

        ("ROAD_SRC_OID", "LONG", None),
        ("RD_JURIS", "TEXT", 80),

        ("ROAD_MATCH_PCT", "DOUBLE", None),
        ("ROAD_MATCH_DIST", "DOUBLE", None),
        ("ROAD_MATCH_CONF", "TEXT", 10),

        ("ROAD_CLASS", "TEXT", 15),
        ("CLASS_SRC", "TEXT", 40),

        ("MATCH_METHOD", "TEXT", 30),

        ("QC_LEVEL", "TEXT", 10),
        ("QC_FLAG", "TEXT", 500)
    ]

    for name, field_type, length in fields:

        add_field_if_missing(
            OUT_FC,
            name,
            field_type,
            length
        )

def add_outputs_to_arcgis_project():

    msg("")
    msg("--------------------------------------------------")
    msg("Adding outputs to ArcGIS Pro project...")
    msg("--------------------------------------------------")

    if not arcpy.Exists(APRX_PATH):
        raise RuntimeError(
            f"ArcGIS Pro project not found:\n{APRX_PATH}"
        )

    aprx = arcpy.mp.ArcGISProject(APRX_PATH)

    maps = aprx.listMaps(MAP_NAME)

    if not maps:
        raise RuntimeError(
            f"Map '{MAP_NAME}' was not found in:\n"
            f"{APRX_PATH}"
        )

    road_map = maps[0]

    outputs = [
        OUT_FC,
        PRIVATE_UNKNOWN_FC,
        PUBLIC_FC
    ]

    # Existing layer data sources already in the map
    existing_sources = set()

    for lyr in road_map.listLayers():

        if not lyr.isFeatureLayer:
            continue

        try:
            existing_sources.add(
                os.path.normcase(
                    os.path.normpath(
                        lyr.dataSource
                    )
                )
            )
        except Exception:
            pass

    for fc in outputs:

        normalized_fc = os.path.normcase(
            os.path.normpath(fc)
        )

        if normalized_fc in existing_sources:

            msg(
                f"Already in map - skipping: "
                f"{os.path.basename(fc)}"
            )

            continue

        road_map.addDataFromPath(fc)

        msg(
            f"Added to map: "
            f"{os.path.basename(fc)}"
        )

    aprx.save()

    del road_map
    del aprx

    msg("")
    msg("ArcGIS Pro project updated successfully.")
    
    
# ============================================================================
# LOAD ONLY REQUIRED SOURCE ROAD FEATURES
# ============================================================================

def load_candidate_roads(road_oids):

    """
    Load geometry and ROADJURIS only for Roads features referenced by
    Generate Near Table.

    This avoids loading the entire Roads dataset into memory.
    """

    data = {}

    if not road_oids:
        return data

    oid_field = arcpy.Describe(
        ROADS
    ).OIDFieldName

    delimited_oid = arcpy.AddFieldDelimiters(
        ROADS,
        oid_field
    )

    oid_list = list(road_oids)

    # File geodatabases comfortably handle this size IN clause.
    chunk_size = 900

    for start in range(
        0,
        len(oid_list),
        chunk_size
    ):

        chunk = oid_list[
            start:start + chunk_size
        ]

        where = (
            f"{delimited_oid} IN "
            f"({','.join(str(x) for x in chunk)})"
        )

        with arcpy.da.SearchCursor(
            ROADS,
            [
                "OID@",
                "SHAPE@",
                ROAD_JURIS_FIELD
            ],
            where_clause=where
        ) as cur:

            for oid, geom, jurisdiction in cur:

                data[oid] = (
                    geom,
                    jurisdiction
                )

    return data


# ============================================================================
# MATCH OUTPUT SEGMENTS BACK TO ORIGINAL ROADS
# ============================================================================

def match_original_roads(
    disp_coverage_by_ml,
    disp_ids_by_ml,
    overlapping_disp_ml_ids
):

    msg("")
    msg("--------------------------------------------------")
    msg("Matching segments back to original Roads...")
    msg("--------------------------------------------------")

    delete_if_exists(NEAR_TABLE)

    # -----------------------------------------------------------------------
    # Get candidate source Roads.
    #
    # We ask for up to 5 nearby candidates, because simply taking the nearest
    # line can select a crossing road instead of the road the ML line follows.
    # -----------------------------------------------------------------------

    arcpy.analysis.GenerateNearTable(
        in_features=OUT_FC,
        near_features=ROADS,
        out_table=NEAR_TABLE,
        search_radius=ROAD_SEARCH_RADIUS,
        location="NO_LOCATION",
        angle="NO_ANGLE",
        closest="ALL",
        closest_count=5,
        method="PLANAR"
    )

    candidates_by_segment = defaultdict(list)
    required_road_oids = set()

    with arcpy.da.SearchCursor(
        NEAR_TABLE,
        [
            "IN_FID",
            "NEAR_FID",
            "NEAR_DIST"
        ]
    ) as cur:

        for in_fid, near_fid, near_dist in cur:

            candidates_by_segment[in_fid].append(
                (
                    near_fid,
                    near_dist
                )
            )

            required_road_oids.add(near_fid)

    msg(
        f"Candidate source Roads required: "
        f"{len(required_road_oids):,}"
    )

    source_roads = load_candidate_roads(
        required_road_oids
    )

    # Cache buffers because one source road may be evaluated
    # against many ML segments.
    road_buffer_cache = {}

    # A line farther from every source candidate than the overlap-buffer
    # radius cannot intersect any candidate buffer. Keep these zero-overlap
    # candidates for unchanged ranking/tie behavior, but skip costly buffers
    # and geometry intersections.
    candidate_pair_count = sum(len(v) for v in candidates_by_segment.values())
    far_pair_count = sum(1 for values in candidates_by_segment.values()
                         for _, distance in values
                         if distance is not None and distance > ROAD_OVERLAP_BUFFER)
    msg(f"Candidate comparisons: {candidate_pair_count:,}; "
        f"provably outside {ROAD_OVERLAP_BUFFER:g} m buffer: {far_pair_count:,}")
    match_started = time.perf_counter()

    # -----------------------------------------------------------------------
    # Update final records.
    # -----------------------------------------------------------------------

    update_fields = [
        "OID@",
        "SHAPE@",
        ML_ID_FIELD,
        DISP_ID_FIELD,

        "SEG_ID",
        "SEG_LEN_M",

        "DISP_COV_PCT",
        "ML_DISP_CNT",

        "ROAD_SRC_OID",
        "RD_JURIS",

        "ROAD_MATCH_PCT",
        "ROAD_MATCH_DIST",
        "ROAD_MATCH_CONF",

        "ROAD_CLASS",
        "CLASS_SRC",

        "MATCH_METHOD",

        "QC_LEVEL",
        "QC_FLAG"
    ]

    with arcpy.da.UpdateCursor(
        OUT_FC,
        update_fields
    ) as cur:

        for match_number, row in enumerate(cur, start=1):

            segment_oid = row[0]
            segment_geom = row[1]
            ml_id = row[2]
            disp_num = row[3]

            segment_length = (
                segment_geom.length
                if segment_geom
                else 0.0
            )

            row[4] = segment_oid
            row[5] = segment_length

            disp_coverage = disp_coverage_by_ml.get(
                ml_id,
                0.0
            )

            row[6] = disp_coverage

            disp_count = len(
                disp_ids_by_ml.get(
                    ml_id,
                    set()
                )
            )

            row[7] = disp_count

            has_disposition = (
                disp_num is not None
                and str(disp_num).strip() != ""
            )

            # ===============================================================
            # Evaluate source Roads candidates
            # ===============================================================

            evaluations = []

            for road_oid, near_dist in candidates_by_segment.get(
                segment_oid,
                []
            ):

                source_data = source_roads.get(
                    road_oid
                )

                if not source_data:
                    continue

                source_geom, jurisdiction = source_data

                if source_geom is None:
                    continue

                # GenerateNearTable already computed the true planar distance.
                # A source farther away than the buffer cannot overlap it.
                if (near_dist is not None
                        and near_dist > ROAD_OVERLAP_BUFFER):
                    evaluations.append({
                        "road_oid": road_oid,
                        "distance": near_dist,
                        "ratio": 0.0,
                        "jurisdiction": jurisdiction
                    })
                    continue

                if road_oid not in road_buffer_cache:

                    road_buffer_cache[road_oid] = (
                        source_geom.buffer(
                            ROAD_OVERLAP_BUFFER
                        )
                    )

                road_buffer = road_buffer_cache[
                    road_oid
                ]

                try:

                    overlap_line = segment_geom.intersect(
                        road_buffer,
                        2
                    )

                    if (
                        overlap_line
                        and segment_length > 0
                    ):

                        overlap_ratio = (
                            overlap_line.length
                            / segment_length
                        )

                    else:
                        overlap_ratio = 0.0

                except Exception:

                    overlap_ratio = 0.0

                overlap_ratio = min(
                    max(overlap_ratio, 0.0),
                    1.0
                )

                evaluations.append(
                    {
                        "road_oid": road_oid,
                        "distance": near_dist,
                        "ratio": overlap_ratio,
                        "jurisdiction": jurisdiction
                    }
                )

            # Highest overlap wins.
            # Distance is only used as a tie breaker.
            evaluations.sort(
                key=lambda x: (
                    -x["ratio"],
                    x["distance"]
                )
            )

            road_oid = None
            road_juris = None
            road_match_ratio = 0.0
            road_match_dist = None
            road_match_conf = "NONE"

            ambiguous = False

            if evaluations:

                best = evaluations[0]

                if best["ratio"] >= ROAD_LOW_CONF:

                    road_oid = best["road_oid"]
                    road_juris = best["jurisdiction"]
                    road_match_ratio = best["ratio"]
                    road_match_dist = best["distance"]

                    if best["ratio"] >= ROAD_HIGH_CONF:
                        road_match_conf = "HIGH"
                    else:
                        road_match_conf = "LOW"

                    # Check whether second candidate is nearly tied.
                    if len(evaluations) > 1:

                        second = evaluations[1]

                        if (
                            second["ratio"]
                            >= ROAD_LOW_CONF
                            and
                            (
                                best["ratio"]
                                - second["ratio"]
                            )
                            <= ROAD_AMBIGUITY_DELTA
                        ):
                            ambiguous = True

            # ===============================================================
            # Store Roads match
            # ===============================================================

            row[8] = road_oid

            row[9] = (
                str(road_juris)
                if road_juris is not None
                else None
            )

            row[10] = (
                road_match_ratio * 100.0
            )

            row[11] = road_match_dist
            row[12] = road_match_conf

            # ===============================================================
            # Public / Private classification
            # ===============================================================

            standardized_juris = normalize_jurisdiction(
                road_juris
            )

            qc_flags = []

            if standardized_juris in (
                "PUBLIC",
                "PRIVATE"
            ):

                # ROADJURIS is authoritative when we have a usable
                # source Roads match.
                road_class = standardized_juris

                class_source = (
                    "ROADS.ROADJURIS"
                )

            elif has_disposition:

                # ML geometry clearly lies inside a disposition,
                # but no authoritative ROADJURIS could be recovered.
                road_class = "PRIVATE"

                class_source = (
                    "DISPOSITION_INFERRED"
                )

                qc_flags.append(
                    "CLASS_INFERRED_FROM_DISP"
                )

            else:

                road_class = "UNKNOWN"

                class_source = "NONE"

            row[13] = road_class
            row[14] = class_source

            # ===============================================================
            # Match method
            # ===============================================================

            has_road_match = (
                road_match_conf != "NONE"
            )

            if has_disposition and has_road_match:
                match_method = "BOTH"

            elif has_disposition:
                match_method = "DISP_POLYGON"

            elif has_road_match:
                match_method = "ROAD_LINE"

            else:
                match_method = "NONE"

            row[15] = match_method

            # ===============================================================
            # QC FLAGS
            # ===============================================================

            if road_match_conf == "LOW":
                qc_flags.append(
                    "ROAD_LOW_CONF"
                )

            if ambiguous:
                qc_flags.append(
                    "ROAD_AMBIGUOUS"
                )

            if ml_id in overlapping_disp_ml_ids:
                qc_flags.append(
                    "MULTI_DISP_OVERLAP"
                )

            # Strong evidence of a conflict:
            #
            # Roads says PUBLIC but line is also associated with
            # a Disposition polygon.
            if (
                road_class == "PUBLIC"
                and has_disposition
            ):
                qc_flags.append(
                    "PUBLIC_DISP_CONFLICT"
                )

            # Roads says PRIVATE but no disposition could be identified.
            if (
                road_class == "PRIVATE"
                and
                class_source == "ROADS.ROADJURIS"
                and
                not has_disposition
            ):
                qc_flags.append(
                    "PRIVATE_NO_DISP"
                )

            if (
                road_match_conf != "NONE"
                and
                standardized_juris == "UNKNOWN"
            ):
                qc_flags.append(
                    "ROADJURIS_UNRECOGNIZED"
                )

            if segment_length < MIN_SEGMENT_LENGTH:
                qc_flags.append(
                    "SHORT_SEGMENT"
                )

            if (
                road_class == "UNKNOWN"
                and
                not has_disposition
            ):
                qc_flags.append(
                    "UNMATCHED"
                )

            if qc_flags:

                row[16] = "REVIEW"
                row[17] = ";".join(
                    sorted(set(qc_flags))
                )

            else:

                row[16] = "OK"
                row[17] = "OK"

            cur.updateRow(row)

            if match_number % 10000 == 0:
                elapsed = time.perf_counter() - match_started
                rate = match_number / elapsed if elapsed else 0.0
                msg(f"Road matching progress: {match_number:,}; "
                    f"{rate:,.0f} segments/s; {elapsed / 60:.1f} min elapsed")


# ============================================================================
# SUMMARY
# ============================================================================

def print_summary():

    msg("")
    msg("==================================================")
    msg("STEP 1 COMPLETE")
    msg("==================================================")

    total = int(
        arcpy.management.GetCount(
            OUT_FC
        )[0]
    )

    public_count = 0
    private_count = 0
    unknown_count = 0
    review_count = 0

    with arcpy.da.SearchCursor(
        OUT_FC,
        [
            "ROAD_CLASS",
            "QC_LEVEL"
        ]
    ) as cur:

        for road_class, qc_level in cur:

            if road_class == "PUBLIC":
                public_count += 1

            elif road_class == "PRIVATE":
                private_count += 1

            else:
                unknown_count += 1

            if qc_level == "REVIEW":
                review_count += 1

    msg(f"Total output segments : {total:,}")
    msg(f"PUBLIC                : {public_count:,}")
    msg(f"PRIVATE               : {private_count:,}")
    msg(f"UNKNOWN               : {unknown_count:,}")
    msg(f"QC REVIEW             : {review_count:,}")

    msg("")
    msg("2. PRIVATE + UNKNOWN:")
    msg(PRIVATE_UNKNOWN_FC)
    
    msg("")
    msg("3. PUBLIC:")
    msg(PUBLIC_FC)

    msg("")
    msg(f"Main output:")
    msg(OUT_FC)

    msg("")
    msg(f"Disposition polygons:")
    msg(DISP_POLY)

    msg("")
    msg(f"Disposition geometry QC:")
    msg(DISP_QC)


# ============================================================================
# CLEANUP
# ============================================================================

def cleanup():

    msg("")
    msg("Cleaning temporary datasets...")

    for dataset in [
        ROAD_WORK,
        MATCHED_FC,
        UNMATCHED_FC,
        MERGED_FC,
        NEAR_TABLE
    ]:

        delete_if_exists(dataset)


# ============================================================================
# MAIN
# ============================================================================

def main():

    global arcpy
    if arcpy is None:
        import arcpy
    arcpy.env.overwriteOutput = True
    create_output_gdb()

    prepare_projected_inputs()

    validate_inputs()

    # Release any cached geodatabase connections from previous operations.
    arcpy.management.ClearWorkspaceCache()

    create_disposition_polygons()

    prepare_road_network()

    (
        disp_coverage_by_ml,
        disp_ids_by_ml,
        overlapping_disp_ml_ids
    ) = disposition_overlay()

    add_output_fields()

    match_original_roads( disp_coverage_by_ml,
        disp_ids_by_ml,
        overlapping_disp_ml_ids)
    export_road_class_layers()

    print_summary()

    # Keep working datasets for reproducibility; no project/UI changes in scheduled runs.
    if not globals().get("SCHEDULED_RUN", False):
        cleanup()
        add_outputs_to_arcgis_project()
    
    msg("")
    msg("Finished successfully.")


def scheduled_main():
    from pathlib import Path
    from RoadUsage_Workflow import arguments, load_config, config_hash, write_json
    args = arguments("Step 1: extract road geometry and overlay source attributes")
    config = load_config(args.config)
    global arcpy
    import arcpy
    folder = args.run_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    output_gdb = folder / "Prepared_Roads.gdb"
    if output_gdb.exists():
        raise FileExistsError("Prepared_Roads.gdb already exists; choose a new run folder.")
    settings = config["step1"]
    for key in ("road_network", "roads", "dispositions"):
        if not arcpy.Exists(settings[key]):
            raise FileNotFoundError(settings[key])
    globals().update(SCHEDULED_RUN=True, OUT_GDB=str(output_gdb), TARGET_WKID=config["target_wkid"],
        ROAD_NETWORK_SOURCE=settings["road_network"], ROADS_SOURCE=settings["roads"],
        DISPOSITION_SOURCE=settings["dispositions"], DISP_ID_FIELD=settings["disposition_id_field"],
        ROAD_JURIS_FIELD=settings["road_jurisdiction_field"],
        DISPOSITION_WHERE_CLAUSE=config["disposition_filter"],
        MIN_DISPOSITION_OVERLAP_M=config.get("min_disposition_overlap_m",0.0))
    paths = {"ROAD_NETWORK":"_input_Road_Network_Projected", "ROADS":"_input_Roads_Projected",
        "DISPOSITION":"_input_Disposition_Projected", "OUT_FC":OUT_NAME,
        "PRIVATE_UNKNOWN_FC":PRIVATE_UNKNOWN_NAME, "PUBLIC_FC":PUBLIC_NAME,
        "DISP_POLY":DISP_POLY_NAME, "DISP_QC":DISP_QC_NAME, "ROAD_WORK":"_wrk_RoadNetwork",
        "MATCHED_FC":"_wrk_DispMatched", "UNMATCHED_FC":"_wrk_DispUnmatched",
        "MERGED_FC":"_wrk_RoadMerged", "NEAR_TABLE":"_wrk_RoadNear"}
    globals().update({name: str(output_gdb / layer) for name,layer in paths.items()})
    main()
    write_json(folder / "Step1_Complete.json", {"Status":"COMPLETE", "ConfigSHA256":config_hash(config),
        "PreparedGDB":str(output_gdb), "DispositionFilter":config["disposition_filter"],
        "MinDispositionOverlap_m":config.get("min_disposition_overlap_m",0.0),
        "SelectedDispositionCount":int(arcpy.management.GetCount(str(output_gdb / "_input_Disposition_Projected"))[0]),
        "Config":config})


if __name__ == "__main__":
    scheduled_main()
