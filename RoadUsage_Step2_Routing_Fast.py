import os
import math
import warnings
from collections import defaultdict

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import unary_union, substring
from shapely.strtree import STRtree
from shapely import wkb
from RoadUsage_Topology import prepare_road_topology, component_metadata

# =============================================================================
# ROAD USAGE - STEP 2 / STEP 3 ROUTING (FAST VERSION)
# =============================================================================
# Key changes from the previous version:
#   1. File GDB inputs are read with ArcPy SearchCursor, NOT pyogrio/Fiona.
#   2. Shortest distance/path-to-destination is calculated ONCE for the graph.
#   3. Each origin only:
#        - finds its nearest edge,
#        - compares the two endpoints of that edge,
#        - follows the already-computed shortest-path tree.
#   4. The graph is NOT copied for every origin.
#   5. Dijkstra is NOT rerun for every origin.
#
# No ArcGIS Network Analyst extension is used.
# ArcPy is used only for ordinary File GDB reading and geometry access.
# =============================================================================

# =============================================================================
# CONFIGURATION
# =============================================================================
OUT_GDB = r"F:\Road Usage\Progress\RoadUsageProcess.gdb"
TARGET_WKID = 3400

PRIVATE_ROADS_LAYER = "Road_Network_Private_Unknown"
PUBLIC_ROADS_LAYER = "Road_Network_Public"

ORIGIN_LAYERS = [
    {
        "layer": "ErcbFacility_SampleClip",
        "id_field": "FacilityId",
        "origin_type": "ERCB_FACILITY",
    },
    {
        "layer": "abadata2_dbo_equipment_SampleClip",
        "id_field": "FacilityID",
        "origin_type": "EQUIPMENT",
    },
    {
        "layer": "oil_and_gas_wells_pt_SampleClip",
        "id_field": "WellId",
        "origin_type": "WELL",
    },
]

# Endpoint-to-road near-miss connection tolerance.
ROAD_CONNECT_TOLERANCE_M = 5.0

# Existing geometry repair only; synthetic connectors are not used by main().
TOPOLOGY_SNAP_TOLERANCE_M = 0.25
GRADE_LEVEL_FIELD = None  # Set only to a verified road-level/elevation field.
GRADE_STRUCTURE_FIELDS = ("BRIDGE", "TUNNEL", "STRUCTURE")

# Origin snapping QC thresholds.
ORIGIN_WARN_DISTANCE_M = 25.0
ORIGIN_MAX_SNAP_DISTANCE_M = 100.0

# Optional public-destination length filter. Zero accepts every public road.
# The former 500 m default could reject an already connected origin.
MIN_PUBLIC_COMPONENT_LENGTH_M = 0.0

# Coordinate rounding for NetworkX node keys.
NODE_ROUND_DECIMALS = 3

# Progress message interval during routing.
PROGRESS_EVERY_ORIGINS = 1000

# Set to an integer during testing if you only want to route the first N origins.
# Example: MAX_ORIGINS_FOR_TEST = 1000
MAX_ORIGINS_FOR_TEST = None

OUTPUT_FOLDER = os.path.dirname(OUT_GDB)
OUTPUT_GPKG = os.path.join(OUTPUT_FOLDER, "RoadUsage_Routing_Output.gpkg")
OUTPUT_CSV = os.path.join(OUTPUT_FOLDER, "RoadUsage_Route_Summary.csv")
OUTPUT_PUBLIC_COMPONENT_CSV = os.path.join(OUTPUT_FOLDER, "Public_Component_QC.csv")

OVERWRITE_OUTPUT = True

# =============================================================================
# GENERAL HELPERS
# =============================================================================
def log(message):
    print(message, flush=True)


def node_key(x, y):
    return (
        round(float(x), NODE_ROUND_DECIMALS),
        round(float(y), NODE_ROUND_DECIMALS),
    )


def edge_nodes(row):
    """Use junction IDs on repaired edges to keep separated crossings separate."""
    geom = row["geometry"]
    u = node_key(geom.coords[0][0], geom.coords[0][1])
    v = node_key(geom.coords[-1][0], geom.coords[-1][1])
    if "NODE_U_ID" in row and pd.notna(row["NODE_U_ID"]):
        u += (int(row["NODE_U_ID"]),)
        v += (int(row["NODE_V_ID"]),)
    return u, v


def safe_value(value):
    """Convert values that commonly cause GeoPackage/DataFrame issues."""
    if value is None:
        return None
    # ArcPy can return bytearray/memoryview for some field types.
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value).hex()
    return value


def reverse_line(line):
    return LineString(list(line.coords)[::-1])


# =============================================================================
# FILE GDB READING - ARCPY, NOT PYOGRIO/FIONA
# =============================================================================
def gdb_featureclass_path(layer_name):
    return os.path.join(OUT_GDB, layer_name)


def list_attribute_fields(feature_class):
    """
    Return ordinary attribute fields safe to request through SearchCursor.

    OID is not required because this routing script creates its own SOURCE_OID.
    Geometry/blob/raster fields are excluded.
    """
    import arcpy
    excluded_types = {"OID", "Geometry", "Blob", "Raster"}
    return [
        fld.name
        for fld in arcpy.ListFields(feature_class)
        if fld.type not in excluded_types
    ]


def read_gdb_layer_arcpy(layer_name, attribute_fields=None):
    """
    Read a File GDB feature class using arcpy.da.SearchCursor and convert the
    geometry to Shapely through SHAPE@WKB.

    This avoids the pyogrio/Fiona File GDB problem and Fiona date parsing.
    """
    import arcpy
    # ArcPy feature layers preserve their SQL selection; named GDB classes
    # still resolve against the configured output geodatabase.
    feature_class = layer_name if arcpy.Exists(layer_name) else gdb_featureclass_path(layer_name)

    if not arcpy.Exists(feature_class):
        raise FileNotFoundError(
            f"Feature class not found:\n  {feature_class}"
        )

    desc = arcpy.Describe(feature_class)
    spatial_ref = desc.spatialReference
    source_wkid = int(spatial_ref.factoryCode or 0)

    if source_wkid <= 0:
        raise ValueError(
            f"Could not determine EPSG/WKID for '{layer_name}'. "
            f"Spatial reference: {spatial_ref.name}"
        )

    if attribute_fields is None:
        attribute_fields = list_attribute_fields(feature_class)
    else:
        available = {f.name.lower(): f.name for f in arcpy.ListFields(feature_class)}
        resolved = []
        for requested in attribute_fields:
            actual = available.get(requested.lower())
            if actual is None:
                raise KeyError(
                    f"Field '{requested}' not found in '{layer_name}'. "
                    f"Available fields: {sorted(available.values())}"
                )
            resolved.append(actual)
        attribute_fields = resolved

    cursor_fields = list(attribute_fields) + ["OID@", "SHAPE@WKB"]

    records = []
    geometries = []

    with arcpy.da.SearchCursor(feature_class, cursor_fields) as cursor:
        for row in cursor:
            geom_wkb = row[-1]
            if geom_wkb is None:
                continue

            try:
                geom = wkb.loads(bytes(geom_wkb))
            except Exception:
                continue

            attrs = {
                field_name: safe_value(row[i])
                for i, field_name in enumerate(attribute_fields)
            }
            attrs["SOURCE_OID"] = int(row[-2])
            attrs["SOURCE_LAYER"] = layer_name
            records.append(attrs)
            geometries.append(geom)

    gdf = gpd.GeoDataFrame(
        records,
        geometry=geometries,
        crs=f"EPSG:{source_wkid}",
    )

    return gdf


def force_lines(gdf):
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf = gdf.explode(index_parts=False, ignore_index=True)
    gdf = gdf[gdf.geometry.geom_type == "LineString"].copy()
    return gdf.reset_index(drop=True)


def validate_crs(gdf, name):
    if gdf.crs is None:
        raise ValueError(f"{name} has no CRS.")

    epsg = gdf.crs.to_epsg()
    if epsg != TARGET_WKID:
        log(f"[CRS] Reprojecting {name} from {gdf.crs} to EPSG:{TARGET_WKID}")
        gdf = gdf.to_crs(epsg=TARGET_WKID)

    return gdf


def read_roads():
    log("[1/10] Reading private/unknown and public roads with ArcPy...")

    # Keep all current road attributes so Step 1 disposition attribution remains
    # available on the routed edge output.
    private = read_gdb_layer_arcpy(PRIVATE_ROADS_LAYER)
    public = read_gdb_layer_arcpy(PUBLIC_ROADS_LAYER)

    private = validate_crs(force_lines(private), "PRIVATE_ROADS")
    public = validate_crs(force_lines(public), "PUBLIC_ROADS")

    if "ROAD_CLASS" not in private:
        private["ROAD_CLASS"] = "PRIVATE_UNKNOWN"
    if "ROAD_CLASS" not in public:
        public["ROAD_CLASS"] = "PUBLIC"

    log(f"      Private/Unknown roads: {len(private):,}")
    log(f"      Public roads         : {len(public):,}")

    return private, public


# =============================================================================
# ROAD TOPOLOGY / NEAR-MISS CONNECTIONS
# =============================================================================
def build_nearmiss_connectors(roads):
    log("[2/10] Detecting endpoint near-miss road connections...")

    geoms = list(roads.geometry)
    tree = STRtree(geoms)

    connectors = []
    seen = set()

    for i, line in enumerate(geoms):
        endpoints = [Point(line.coords[0]), Point(line.coords[-1])]

        for endpoint in endpoints:
            candidate_idxs = tree.query(endpoint.buffer(ROAD_CONNECT_TOLERANCE_M))

            best_j = None
            best_dist = math.inf
            best_pt = None

            for j_raw in candidate_idxs:
                j = int(j_raw)
                if j == i:
                    continue

                target = geoms[j]
                distance = float(endpoint.distance(target))

                # Already intersects/touches; exact noding will handle this.
                if distance <= 1e-8:
                    continue
                if distance > ROAD_CONNECT_TOLERANCE_M:
                    continue

                projected = target.interpolate(target.project(endpoint))

                if distance < best_dist:
                    best_dist = distance
                    best_j = j
                    best_pt = projected

            if best_j is None or best_pt is None:
                continue

            a = node_key(endpoint.x, endpoint.y)
            b = node_key(best_pt.x, best_pt.y)
            pair = tuple(sorted([a, b]))

            if pair in seen:
                continue
            seen.add(pair)

            connector = LineString([endpoint, best_pt])
            if connector.length <= 1e-8:
                continue

            connectors.append(
                {
                    "ROAD_CLASS": "CONNECTOR",
                    "SOURCE_OID": -1,
                    "CONNECT_DIST_M": float(connector.length),
                    "geometry": connector,
                }
            )

    if connectors:
        result = gpd.GeoDataFrame(connectors, geometry="geometry", crs=roads.crs)
    else:
        result = gpd.GeoDataFrame(
            columns=["ROAD_CLASS", "SOURCE_OID", "CONNECT_DIST_M", "geometry"],
            geometry="geometry",
            crs=roads.crs,
        )

    log(f"      Near-miss connectors created: {len(result):,}")
    return result


def legacy_noded_segments(roads, connectors):
    """
    Node exact line intersections with unary_union, then recover the Step 1 road
    attributes for each resulting segment from the source road at its midpoint.
    """
    log("[3/10] Noding road network at intersections...")

    line_list = list(roads.geometry)
    if not connectors.empty:
        line_list.extend(list(connectors.geometry))

    merged = unary_union(line_list)

    if merged.geom_type == "LineString":
        segment_geometries = [merged]
    elif merged.geom_type == "MultiLineString":
        segment_geometries = list(merged.geoms)
    elif merged.geom_type == "GeometryCollection":
        segment_geometries = [
            geom for geom in merged.geoms if geom.geom_type == "LineString"
        ]
    else:
        raise RuntimeError(f"Unexpected noded geometry type: {merged.geom_type}")

    original_geoms = list(roads.geometry)
    original_tree = STRtree(original_geoms)

    records = []

    for edge_id, segment in enumerate(segment_geometries):
        if segment.is_empty or segment.length <= 1e-8:
            continue

        midpoint = segment.interpolate(0.5, normalized=True)
        candidate_idxs = original_tree.query(
            midpoint.buffer(max(0.05, ROAD_CONNECT_TOLERANCE_M))
        )

        best_idx = None
        best_dist = math.inf

        for idx_raw in candidate_idxs:
            idx = int(idx_raw)
            distance = float(midpoint.distance(original_geoms[idx]))
            if distance < best_dist:
                best_dist = distance
                best_idx = idx

        if best_idx is not None and best_dist <= 0.05:
            src = roads.iloc[best_idx]
            record = src.drop(labels="geometry").to_dict()
            record["geometry"] = segment
            record["EDGE_ID"] = int(edge_id)
            record["LENGTH_M"] = float(segment.length)
            record["IS_CONNECTOR"] = 0
        else:
            record = {
                "ROAD_CLASS": "CONNECTOR",
                "SOURCE_OID": -1,
                "EDGE_ID": int(edge_id),
                "LENGTH_M": float(segment.length),
                "IS_CONNECTOR": 1,
                "geometry": segment,
            }

        records.append(record)

    edges = gpd.GeoDataFrame(records, geometry="geometry", crs=roads.crs)
    log(f"      Noded graph edges: {len(edges):,}")
    return edges


def noded_segments(roads, connectors=None, return_qa=False):
    """Repair small existing-road topology before constructing graph nodes."""
    if connectors is not None and not connectors.empty:
        raise ValueError("Synthetic connectors must be evaluated after topology repair.")
    edges, qa = prepare_road_topology(
        roads, TOPOLOGY_SNAP_TOLERANCE_M, GRADE_LEVEL_FIELD, GRADE_STRUCTURE_FIELDS,
    )
    return (edges, qa) if return_qa else edges


# =============================================================================
# NETWORKX GRAPH
# =============================================================================
def add_edge_to_graph(graph, row):
    geom = row.geometry
    u, v = edge_nodes(row)

    if u == v:
        return

    attrs = row.drop(labels="geometry").to_dict()
    attrs["geometry"] = geom
    attrs["weight"] = float(geom.length)
    attrs["LENGTH_M"] = float(geom.length)

    graph.add_node(u, x=u[0], y=u[1])
    graph.add_node(v, x=v[0], y=v[1])
    graph.add_edge(u, v, **attrs)


def build_graph(edges):
    log("[4/10] Building NetworkX graph...")

    graph = nx.MultiGraph()

    for _, row in edges.iterrows():
        add_edge_to_graph(graph, row)

    log(f"      Graph nodes: {graph.number_of_nodes():,}")
    log(f"      Graph edges: {graph.number_of_edges():,}")
    log(f"      Connected components: {nx.number_connected_components(graph):,}")

    return graph


# =============================================================================
# PUBLIC DESTINATION IDENTIFICATION
# =============================================================================
def public_destination_nodes(graph):
    """
    Identify public components large enough to be a final destination.

    This implementation sums public component length in one pass instead of
    rescanning the full graph for every public component.
    """
    log("[5/10] Identifying qualifying public-road destination components...")

    public_graph = nx.Graph()

    for u, v, key, data in graph.edges(keys=True, data=True):
        if data.get("ROAD_CLASS") != "PUBLIC":
            continue

        weight = float(data.get("LENGTH_M", data.get("weight", 0.0)))

        if public_graph.has_edge(u, v):
            public_graph[u][v]["weight"] = min(
                public_graph[u][v]["weight"], weight
            )
        else:
            public_graph.add_edge(u, v, weight=weight)

    node_to_component = {}
    components = []

    for comp_id, nodes in enumerate(nx.connected_components(public_graph), start=1):
        node_set = set(nodes)
        components.append((comp_id, node_set))
        for node in node_set:
            node_to_component[node] = comp_id

    component_length = defaultdict(float)

    # One pass through original graph edges.
    for u, v, key, data in graph.edges(keys=True, data=True):
        if data.get("ROAD_CLASS") != "PUBLIC":
            continue

        comp_id = node_to_component.get(u)
        if comp_id is None or node_to_component.get(v) != comp_id:
            continue

        component_length[comp_id] += float(
            data.get("LENGTH_M", data.get("weight", 0.0))
        )

    qualifying_nodes = set()
    component_records = []

    for comp_id, nodes in components:
        total_length = component_length.get(comp_id, 0.0)
        qualifies = total_length >= MIN_PUBLIC_COMPONENT_LENGTH_M

        if qualifies:
            qualifying_nodes.update(nodes)

        component_records.append(
            {
                "PUBLIC_COMP_ID": comp_id,
                "TOTAL_PUBLIC_M": total_length,
                "QUALIFIES": int(qualifies),
                "NODE_COUNT": len(nodes),
            }
        )

    if not qualifying_nodes:
        log("WARNING: No qualifying public destinations; origins will be diagnosed as unroutable.")

    log(f"      Qualifying destination nodes: {len(qualifying_nodes):,}")

    return qualifying_nodes, pd.DataFrame(component_records)


# =============================================================================
# ORIGIN READING
# =============================================================================
def load_origins():
    log("[6/10] Reading origin point layers with ArcPy...")

    parts = []

    for config in ORIGIN_LAYERS:
        layer_name = config["layer"]
        id_field = config["id_field"]
        origin_type = config["origin_type"]

        # Only read the ID field for origins. This is much lighter than loading
        # every database attribute through Fiona/GeoPandas.
        gdf = read_gdb_layer_arcpy(layer_name, attribute_fields=[id_field])
        gdf = validate_crs(gdf, origin_type)

        # The ArcPy reader resolves case-insensitive field names to actual names.
        actual_id_field = next(
            field for field in gdf.columns if field.lower() == id_field.lower()
        )

        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
        gdf = gdf[gdf.geometry.geom_type == "Point"].copy()

        out = gdf[[actual_id_field, "geometry"]].copy()
        out = out.rename(columns={actual_id_field: "ORIGIN_ID"})
        out["ORIGIN_TYPE"] = origin_type
        out["SOURCE_LAYER"] = layer_name

        parts.append(out)
        log(f"      {layer_name}: {len(out):,} origin(s)")

    origins = gpd.GeoDataFrame(
        pd.concat(parts, ignore_index=True),
        geometry="geometry",
        crs=f"EPSG:{TARGET_WKID}",
    )

    origins["ORIGIN_UID"] = (
        origins["ORIGIN_TYPE"].astype(str)
        + "|"
        + origins["ORIGIN_ID"].astype(str)
    )

    if MAX_ORIGINS_FOR_TEST is not None:
        origins = origins.head(int(MAX_ORIGINS_FOR_TEST)).copy()
        log(f"      TEST MODE: limiting origins to {len(origins):,}")

    log(f"      Total origins: {len(origins):,}")
    return origins


# =============================================================================
# ONE-TIME DIJKSTRA PRECOMPUTATION
# =============================================================================
def precompute_destination_tree(graph, destination_nodes):
    """
    Run Dijkstra ONCE from a temporary super-destination node connected to every
    qualifying public destination node with zero cost.

    Returns:
        dist[node]   = shortest network distance from node to any destination
        parent[node] = the next node to follow when travelling FROM node TOWARD
                       its nearest destination

    Why this is fast:
        Previous code copied the ~850k-edge graph and reran Dijkstra for each of
        ~74k origins. This version performs one graph-wide Dijkstra total.
    """
    log("[7/10] Precomputing shortest paths to public destinations ONCE...")

    super_node = ("__PUBLIC_DESTINATION_SUPER_NODE__",)

    if super_node in graph:
        raise RuntimeError("Internal super-node name already exists in graph.")

    graph.add_node(super_node)

    for destination in destination_nodes:
        graph.add_edge(
            super_node,
            destination,
            weight=0.0,
            LENGTH_M=0.0,
            ROAD_CLASS="SUPER_DESTINATION",
            IS_SUPER=1,
        )

    try:
        predecessor, distance = nx.dijkstra_predecessor_and_distance(
            graph,
            super_node,
            weight="weight",
        )
    finally:
        # Always restore the original routing graph after Dijkstra.
        graph.remove_node(super_node)

    parent = {}

    for node, predecessors in predecessor.items():
        if node == super_node:
            continue

        if not predecessors:
            parent[node] = None
        else:
            # A node can have multiple equally short predecessors. Any one of
            # them is a valid shortest-path tree parent.
            parent[node] = predecessors[0]

    # Destination nodes have super_node as their predecessor. Keep that marker
    # in parent so path reconstruction knows where to stop.
    log(f"      Nodes with destination distance: {len(distance) - 1:,}")

    # Remove the super-node distance entry; other nodes remain valid.
    distance.pop(super_node, None)

    return distance, parent, super_node


# =============================================================================
# ORIGIN SNAPPING / ROUTE RECONSTRUCTION
# =============================================================================
def build_edge_spatial_index(edges):
    geometries = list(edges.geometry)
    return geometries, STRtree(geometries)


def nearest_edge(point, edge_geometries, edge_tree):
    edge_index = int(edge_tree.nearest(point))
    geom = edge_geometries[edge_index]
    measure = float(geom.project(point))
    snap_point = geom.interpolate(measure)
    snap_distance = float(point.distance(snap_point))

    return edge_index, geom, snap_point, snap_distance, measure


def select_edge_between(graph, u, v):
    """Return minimum-weight parallel edge data between two graph nodes."""
    choices = graph.get_edge_data(u, v)

    if not choices:
        raise RuntimeError(f"Missing graph edge between {u} and {v}")

    key, data = min(
        choices.items(),
        key=lambda item: float(item[1].get("weight", math.inf)),
    )

    return key, data


def reconstruct_route_nodes(start_node, parent, super_node):
    """Follow the precomputed shortest-path tree from start to a destination."""
    route_nodes = [start_node]
    current = start_node
    seen = {current}

    while True:
        next_node = parent.get(current)

        if next_node is None:
            return None

        if next_node == super_node:
            # current is already a qualifying destination node.
            return route_nodes

        if next_node in seen:
            raise RuntimeError("Loop encountered in shortest-path parent tree.")

        route_nodes.append(next_node)
        seen.add(next_node)
        current = next_node


def make_partial_snap_segment(edge_row, snap_point, snap_measure, endpoint):
    """
    Build only the part of the snapped edge needed to reach the chosen graph
    endpoint. Geometry is oriented from snap point toward the endpoint.
    """
    geom = edge_row.geometry
    total_length = float(geom.length)

    u, v = edge_nodes(edge_row)

    tolerance = 1e-8

    if endpoint == u:
        if snap_measure <= tolerance:
            return None
        piece = substring(geom, 0.0, snap_measure)
        if piece.is_empty or piece.length <= tolerance:
            return None
        return reverse_line(piece)

    if endpoint == v:
        if snap_measure >= total_length - tolerance:
            return None
        piece = substring(geom, snap_measure, total_length)
        if piece.is_empty or piece.length <= tolerance:
            return None
        return piece

    raise ValueError("Chosen endpoint does not match snapped edge endpoints.")


def route_origins_fast(
    graph,
    edges,
    origins,
    destination_nodes,
    distance_to_destination,
    parent,
    super_node,
):
    log("[8/10] Snapping origins and reconstructing routes...")

    edge_geometries, edge_tree = build_edge_spatial_index(edges)

    route_records = []
    snap_records = []
    summary_records = []

    total_origins = len(origins)

    for counter, (_, origin) in enumerate(origins.iterrows(), start=1):
        point = origin.geometry

        (
            edge_idx,
            edge_geom,
            snap_point,
            snap_distance,
            snap_measure,
        ) = nearest_edge(point, edge_geometries, edge_tree)

        edge_row = edges.iloc[edge_idx]

        if snap_distance > ORIGIN_MAX_SNAP_DISTANCE_M:
            snap_qc = "FAIL_TOO_FAR"
        elif snap_distance > ORIGIN_WARN_DISTANCE_M:
            snap_qc = "REVIEW"
        else:
            snap_qc = "OK"

        snap_records.append(
            {
                "ORIGIN_UID": origin.ORIGIN_UID,
                "ORIGIN_ID": origin.ORIGIN_ID,
                "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                "SNAP_DIST_M": snap_distance,
                "SNAP_QC": snap_qc,
                "SNAP_EDGE_ID": int(edge_row.EDGE_ID),
                "SNAP_ROAD_CLASS": edge_row.get("ROAD_CLASS"),
                "geometry": snap_point,
            }
        )

        if snap_qc == "FAIL_TOO_FAR":
            summary_records.append(
                {
                    "ORIGIN_UID": origin.ORIGIN_UID,
                    "ORIGIN_ID": origin.ORIGIN_ID,
                    "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                    "STATUS": "UNROUTED_SNAP_TOO_FAR",
                    "SNAP_DIST_M": snap_distance,
                    "ROUTE_LENGTH_M": None,
                    "DEST_NODE": None,
                }
            )
            continue

        geom = edge_row.geometry
        u, v = edge_nodes(edge_row)

        # If the origin snaps directly onto a qualifying public-road edge, the
        # road-network destination has already been reached at the snap point.
        if (
            edge_row.get("ROAD_CLASS") == "PUBLIC"
            and u in destination_nodes
            and v in destination_nodes
        ):
            summary_records.append(
                {
                    "ORIGIN_UID": origin.ORIGIN_UID,
                    "ORIGIN_ID": origin.ORIGIN_ID,
                    "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                    "STATUS": "ROUTED",
                    "SNAP_DIST_M": snap_distance,
                    "ROUTE_LENGTH_M": 0.0,
                    "DEST_NODE": f"SNAP_POINT({snap_point.x:.3f},{snap_point.y:.3f})",
                }
            )
            continue

        along_to_u = float(snap_measure)
        along_to_v = float(geom.length - snap_measure)

        cost_u = (
            along_to_u + distance_to_destination[u]
            if u in distance_to_destination
            else math.inf
        )
        cost_v = (
            along_to_v + distance_to_destination[v]
            if v in distance_to_destination
            else math.inf
        )

        if math.isinf(cost_u) and math.isinf(cost_v):
            summary_records.append(
                {
                    "ORIGIN_UID": origin.ORIGIN_UID,
                    "ORIGIN_ID": origin.ORIGIN_ID,
                    "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                    "STATUS": "UNROUTABLE_DISCONNECTED",
                    "SNAP_DIST_M": snap_distance,
                    "ROUTE_LENGTH_M": None,
                    "DEST_NODE": None,
                }
            )
            continue

        if cost_u <= cost_v:
            chosen_endpoint = u
            route_length = float(cost_u)
        else:
            chosen_endpoint = v
            route_length = float(cost_v)

        route_nodes = reconstruct_route_nodes(
            chosen_endpoint,
            parent,
            super_node,
        )

        if not route_nodes:
            summary_records.append(
                {
                    "ORIGIN_UID": origin.ORIGIN_UID,
                    "ORIGIN_ID": origin.ORIGIN_ID,
                    "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                    "STATUS": "UNROUTABLE_DISCONNECTED",
                    "SNAP_DIST_M": snap_distance,
                    "ROUTE_LENGTH_M": None,
                    "DEST_NODE": None,
                }
            )
            continue

        route_sequence = 1

        # First segment: exact snap point to selected endpoint of the snapped edge.
        partial_geometry = make_partial_snap_segment(
            edge_row,
            snap_point,
            snap_measure,
            chosen_endpoint,
        )

        if partial_geometry is not None:
            partial_record = edge_row.drop(labels="geometry").to_dict()
            partial_record.update(
                {
                    "ORIGIN_UID": origin.ORIGIN_UID,
                    "ORIGIN_ID": origin.ORIGIN_ID,
                    "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                    "ROUTE_SEQ": route_sequence,
                    "FROM_NODE": f"SNAP({snap_point.x:.3f},{snap_point.y:.3f})",
                    "TO_NODE": str(chosen_endpoint),
                    "LENGTH_M": float(partial_geometry.length),
                    "geometry": partial_geometry,
                }
            )
            route_records.append(partial_record)
            route_sequence += 1

        # Remaining route follows the one-time Dijkstra shortest-path tree.
        for a, b in zip(route_nodes[:-1], route_nodes[1:]):
            key, edge_data = select_edge_between(graph, a, b)

            record = dict(edge_data)
            record.update(
                {
                    "ORIGIN_UID": origin.ORIGIN_UID,
                    "ORIGIN_ID": origin.ORIGIN_ID,
                    "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                    "ROUTE_SEQ": route_sequence,
                    "FROM_NODE": str(a),
                    "TO_NODE": str(b),
                    "geometry": edge_data["geometry"],
                }
            )
            route_records.append(record)
            route_sequence += 1

        destination = route_nodes[-1]

        summary_records.append(
            {
                "ORIGIN_UID": origin.ORIGIN_UID,
                "ORIGIN_ID": origin.ORIGIN_ID,
                "ORIGIN_TYPE": origin.ORIGIN_TYPE,
                "STATUS": "ROUTED",
                "SNAP_DIST_M": snap_distance,
                "ROUTE_LENGTH_M": route_length,
                "DEST_NODE": str(destination),
            }
        )

        if counter % PROGRESS_EVERY_ORIGINS == 0 or counter == total_origins:
            log(
                f"      Routed/snap-checked {counter:,} / "
                f"{total_origins:,} origins..."
            )

    if route_records:
        routes = gpd.GeoDataFrame(
            route_records,
            geometry="geometry",
            crs=edges.crs,
        )
    else:
        routes = gpd.GeoDataFrame(
            columns=["geometry"],
            geometry="geometry",
            crs=edges.crs,
        )

    snaps = gpd.GeoDataFrame(
        snap_records,
        geometry="geometry",
        crs=edges.crs,
    )

    summary = pd.DataFrame(summary_records)

    return routes, snaps, summary


# =============================================================================
# QC LAYERS / OUTPUT
# =============================================================================
def make_graph_node_layer(graph, crs):
    records = []

    for node, data in graph.nodes(data=True):
        # Ignore any non-coordinate internal node just in case.
        if not (
            isinstance(node, tuple)
            and len(node) >= 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
        ):
            continue

        records.append(
            {
                "NODE_ID": str(node),
                "DEGREE": int(graph.degree(node)),
                "geometry": Point(node[0], node[1]),
            }
        )

    return gpd.GeoDataFrame(records, geometry="geometry", crs=crs)


def destination_layer(destination_nodes, crs):
    return gpd.GeoDataFrame(
        [
            {
                "DEST_NODE": str(node),
                "geometry": Point(node[0], node[1]),
            }
            for node in destination_nodes
        ],
        geometry="geometry",
        crs=crs,
    )


def write_layer(gdf, layer_name):
    if gdf is None or gdf.empty:
        log(f"      Skipping empty layer: {layer_name}")
        return

    # GeoPackage is kept for open-source compatibility and direct ArcGIS Pro use.
    # Input File GDB reading no longer goes through Fiona/pyogrio.
    gdf.to_file(
        OUTPUT_GPKG,
        layer=layer_name,
        driver="GPKG",
    )


# =============================================================================
# MAIN
# =============================================================================
def main():
    import arcpy
    from RoadUsage_Topology_QA import diagnose_origins, write_topology_issues, annotate_issue_components
    log("=" * 72)
    log("ROAD USAGE - STEP 2 / STEP 3 ROUTING - FAST VERSION")
    log("=" * 72)

    if not arcpy.Exists(OUT_GDB):
        raise FileNotFoundError(f"File GDB not found: {OUT_GDB}")

    if OVERWRITE_OUTPUT and os.path.exists(OUTPUT_GPKG):
        try:
            os.remove(OUTPUT_GPKG)
        except PermissionError as exc:
            raise PermissionError(
                f"Cannot overwrite {OUTPUT_GPKG}. Close it in ArcGIS Pro and retry."
            ) from exc

    private, public = read_roads()

    roads = gpd.GeoDataFrame(
        pd.concat([private, public], ignore_index=True),
        geometry="geometry",
        crs=private.crs,
    )

    roads["SOURCE_ROAD_ID"] = roads.index.astype(int)
    edges, topology_issues = noded_segments(roads, return_qa=True)
    graph = build_graph(edges)
    node_component, component_table = component_metadata(graph)
    component_table.to_csv(os.path.join(OUTPUT_FOLDER, "Topology_Components_After.csv"), index=False)
    topology_issues = annotate_issue_components(topology_issues, edges, node_component, "After")

    destination_nodes, public_components = public_destination_nodes(graph)
    origins = load_origins()

    distance_to_destination, parent, super_node = precompute_destination_tree(
        graph,
        destination_nodes,
    )

    routes, snaps, summary = route_origins_fast(
        graph,
        edges,
        origins,
        destination_nodes,
        distance_to_destination,
        parent,
        super_node,
    )

    log("[9/10] Creating QC layers...")
    nodes = make_graph_node_layer(graph, edges.crs)
    destinations = destination_layer(destination_nodes, edges.crs)

    log("[10/10] Writing outputs...")
    write_layer(edges, "Network_Edges")
    write_layer(nodes, "Network_Nodes")
    write_topology_issues(topology_issues, OUTPUT_GPKG)
    write_layer(destinations, "Destination_Nodes")
    write_layer(snaps, "Origin_Snap_Points")
    write_layer(routes, "Route_Segments")

    summary.to_csv(OUTPUT_CSV, index=False)
    public_components.to_csv(OUTPUT_PUBLIC_COMPONENT_CSV, index=False)
    failed_origins = origins[origins.ORIGIN_UID.isin(summary.loc[summary.STATUS != "ROUTED", "ORIGIN_UID"])]
    diagnostics = diagnose_origins(graph, edges, failed_origins, public, destination_nodes, "After")
    diagnostics.to_csv(os.path.join(OUTPUT_FOLDER, "Topology_Failed_Origins.csv"), index=False)

    routed = (
        int((summary["STATUS"] == "ROUTED").sum())
        if not summary.empty
        else 0
    )
    total = len(summary)

    log("")
    log("=== COMPLETE ===")
    log(f"Origins processed : {total:,}")
    log(f"Routed            : {routed:,}")
    log(f"Not routed        : {total - routed:,}")
    log(f"Spatial output    : {OUTPUT_GPKG}")
    log(f"Route summary     : {OUTPUT_CSV}")
    log(f"Public QC         : {OUTPUT_PUBLIC_COMPONENT_CSV}")
    log("")
    log("Inspect these layers first in ArcGIS Pro:")
    log("  1. Topology_Gaps")
    log("  2. Origin_Snap_Points")
    log("  3. Destination_Nodes")
    log("  4. Route_Segments")


if __name__ == "__main__":
    main()
