"""Reproduce R0008684 and compare its northwest alternative on frozen inputs.

Diagnostic only: reads preserved GeoPackage subsets; never edits production data.
Run using the ArcGIS clone Python. ArcPy is not needed.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from RoadUsage_Workflow import prepare_environment
prepare_environment()

import json
import math
import geopandas as gpd
import networkx as nx
from shapely.geometry import Point, MultiLineString
import RoadUsage_Step2_Routing as delivery
from RoadUsage_Step2_Routing import engine

TOLERANCE_M = 1e-6
FORK_XY = (483824.7840999998, 5854136.3774999995)
NW_FAR_XY = (483449.2087000003, 5854457.837300001)
NW_SOURCE_ROADS = {89505, 89506, 121032, 121033}


def nearest_node(graph, xy):
    point = Point(xy)
    node = min(graph, key=lambda candidate: point.distance(Point(candidate[:2])))
    # Graph coordinates are rounded to millimetres; actual edge geometry is exact.
    if point.distance(Point(node[:2])) > 0.001:
        raise ValueError(f"Frozen comparison node is missing: {xy}")
    return node


def pieces_for_nodes(graph, nodes):
    pieces = []
    for a, b in zip(nodes[:-1], nodes[1:]):
        _, data = engine.select_edge_between(graph, a, b)
        line = data["geometry"]
        if engine.edge_nodes(data)[0] != a:
            line = engine.reverse_line(line)
        pieces.append((int(data["EDGE_ID"]), int(data["SOURCE_ROAD_ID"]), line))
    return pieces


def validate_pieces(pieces, start, destination):
    prior = start
    gaps = []
    for _, _, line in pieces:
        gaps.append(prior.distance(Point(line.coords[0])))
        prior = Point(line.coords[-1])
    edge_ids = [edge_id for edge_id, _, _ in pieces]
    repeated_edges = sorted({edge_id for edge_id in edge_ids if edge_ids.count(edge_id) > 1})
    # Repeated coordinates at consecutive piece boundaries are expected.
    vertices = [tuple(pieces[0][2].coords[0])]
    for _, _, line in pieces:
        vertices.extend(tuple(xy) for xy in list(line.coords)[1:])
    keys = [(round(x, 6), round(y, 6)) for x, y, *_ in vertices]
    repeated_vertices = len(keys) - len(set(keys))
    result = {
        "piece_count": len(pieces),
        "maximum_gap_m": max(gaps, default=0.0),
        "destination_touch_distance_m": prior.distance(destination["geometry"]),
        "repeated_edge_ids": repeated_edges,
        "repeated_vertices_at_micrometre_precision": repeated_vertices,
    }
    if result["maximum_gap_m"] > TOLERANCE_M:
        raise ValueError(f"Discontinuous diagnostic route: {result}")
    if result["destination_touch_distance_m"] > TOLERANCE_M:
        raise ValueError(f"Diagnostic route misses public destination: {result}")
    if repeated_edges or repeated_vertices:
        raise ValueError(f"Diagnostic route backtracks or repeats geometry: {result}")
    return result


def public_destination(graph, node):
    choices = [data for neighbor in graph[node].values() for data in neighbor.values()
               if data.get("ROAD_CLASS") == "PUBLIC"]
    return min(choices, key=lambda data: int(data["EDGE_ID"]))


def main():
    output = HERE / "Compare_Routes.gpkg"
    metadata = HERE / "comparison.json"
    if output.exists() or metadata.exists():
        raise FileExistsError("Diagnostic outputs already exist; preserve them and choose a new output path.")
    edges = gpd.read_file(HERE / "Wide_Context.gpkg", layer="Baseline_Edges")
    origins = gpd.read_file(HERE / "Context.gpkg", layer="Origin")
    saved = gpd.read_file(HERE / "Context.gpkg", layer="Existing_Route")
    origin = origins.iloc[0]
    if str(origin.RouteID) != "R0008684":
        raise ValueError("Unexpected frozen origin")
    graph = engine.build_graph(edges)
    engine.MIN_PUBLIC_COMPONENT_LENGTH_M = 0.0
    destinations, _ = engine.public_destination_nodes(graph)
    distance, parent, super_node = engine.precompute_destination_tree(graph, destinations)
    geometries, tree = engine.build_edge_spatial_index(edges)
    index, _, snap, gap, measure = engine.nearest_edge(origin.geometry, geometries, tree)
    snap_edge = edges.iloc[index]
    current, current_length, current_destination, current_end = delivery.route_one(
        origin.geometry, snap_edge, snap, measure, graph, destinations, distance, parent, super_node)
    saved_length = float(saved.iloc[0].TotalRoute_m)
    if abs(saved_length - current_length) > TOLERANCE_M:
        raise ValueError("Frozen graph no longer reproduces saved route length")
    if abs(float(origin.SnapDistance_m) - gap) > TOLERANCE_M:
        raise ValueError("Frozen graph no longer reproduces saved origin snap")

    fork = nearest_node(graph, FORK_XY)
    far = nearest_node(graph, NW_FAR_XY)
    prefix = []
    for piece in current:
        prefix.append(piece)
        if Point(piece[2].coords[-1]).distance(Point(FORK_XY)) <= TOLERANCE_M:
            break
    else:
        raise ValueError("Fork was not found on current route")

    branch = nx.MultiGraph()
    for u, v, key, data in graph.edges(keys=True, data=True):
        if int(data["SOURCE_ROAD_ID"]) in NW_SOURCE_ROADS:
            branch.add_edge(u, v, key=key, **data)
    branch_nodes = nx.shortest_path(branch, fork, far, weight="weight")
    northwest_pieces = pieces_for_nodes(branch, branch_nodes)
    onward_nodes = engine.reconstruct_route_nodes(far, parent, super_node)
    if not onward_nodes or onward_nodes[-1] not in destinations:
        raise ValueError("Northwest alternative cannot reach a public road")
    if set(onward_nodes[1:]) & set(branch_nodes[:-1]):
        raise ValueError("Northwest alternative reverses back along its branch")
    onward_pieces = pieces_for_nodes(graph, onward_nodes)
    alternative = prefix + northwest_pieces + onward_pieces
    alternative_length = sum(line.length for _, _, line in alternative)
    alternative_destination = public_destination(graph, onward_nodes[-1])
    alternative_end = Point(alternative[-1][2].coords[-1])
    current_checks = validate_pieces(current, snap, current_destination)
    alternative_checks = validate_pieces(alternative, snap, alternative_destination)
    prefix_length = sum(line.length for _, _, line in prefix)
    branch_length = sum(line.length for _, _, line in northwest_pieces)
    onward_length = sum(line.length for _, _, line in onward_pieces)

    for layer, pieces, length, destination in [
        ("Current_Route", current, current_length, current_destination),
        ("Northwest_Alternative", alternative, alternative_length, alternative_destination),
    ]:
        frame = gpd.GeoDataFrame([{
            "RouteID": str(origin.RouteID), "Alternative": layer,
            "OriginUID": str(origin.OriginUID), "TotalRoute_m": length,
            "TotalRoute_km": length / 1000.0, "SnapDistance_m": gap,
            "DestinationRoadID": int(destination["SOURCE_ROAD_ID"]),
            "geometry": MultiLineString([line for _, _, line in pieces]),
        }], crs=edges.crs)
        frame.to_file(output, layer=layer, driver="GPKG", engine="fiona", index=False)
    arrivals = gpd.GeoDataFrame([
        {"Alternative": "Current_Route", "DestinationRoadID": int(current_destination["SOURCE_ROAD_ID"]),
         "geometry": current_end},
        {"Alternative": "Northwest_Alternative", "DestinationRoadID": int(alternative_destination["SOURCE_ROAD_ID"]),
         "geometry": alternative_end},
    ], crs=edges.crs)
    arrivals.to_file(output, layer="Public_Destinations", driver="GPKG", engine="fiona", index=False)
    gpd.GeoDataFrame([{"Location": "Branch choice", "geometry": Point(FORK_XY)}], crs=edges.crs).to_file(
        output, layer="Fork", driver="GPKG", engine="fiona", index=False)

    result = {
        "RouteID": str(origin.RouteID), "OriginUID": str(origin.OriginUID),
        "scope": "Frozen local subset that exactly reproduces the saved current route; no production files modified.",
        "CRS": str(edges.crs), "snap_distance_m": gap,
        "snap_source_road_id": int(snap_edge.SOURCE_ROAD_ID),
        "current_length_m": current_length, "saved_length_m": saved_length,
        "northwest_length_m": alternative_length,
        "northwest_extra_m": alternative_length - current_length,
        "common_origin_to_fork_m": prefix_length,
        "current_fork_to_public_m": current_length - prefix_length,
        "northwest_branch_including_continuation_m": branch_length,
        "northwest_far_node_to_public_m": onward_length,
        "northwest_fork_to_public_m": branch_length + onward_length,
        "current_destination_source_road_id": int(current_destination["SOURCE_ROAD_ID"]),
        "northwest_destination_source_road_id": int(alternative_destination["SOURCE_ROAD_ID"]),
        "current_destination_source_oid": int(current_destination["SOURCE_OID"]),
        "current_destination_source_layer": str(current_destination["SOURCE_LAYER"]),
        "northwest_destination_source_oid": int(alternative_destination["SOURCE_OID"]),
        "northwest_destination_source_layer": str(alternative_destination["SOURCE_LAYER"]),
        "current_checks": current_checks, "northwest_checks": alternative_checks,
        "current_edge_ids": [edge_id for edge_id, _, _ in current],
        "northwest_edge_ids": [edge_id for edge_id, _, _ in alternative],
        "inputs": ["Context.gpkg", "Wide_Context.gpkg"],
        "output": output.name,
    }
    metadata.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    # A focused static preview; the GeoPackage retains full unsimplified geometry.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    prepared = gpd.read_file(HERE / "Wide_Context.gpkg", layer="Prepared_Roads")
    fig, ax = plt.subplots(figsize=(12, 9), facecolor="white")
    prepared.plot(ax=ax, color="#c8cdd2", linewidth=0.65, zorder=1)
    prepared.loc[prepared.ROAD_CLASS == "PUBLIC"].plot(
        ax=ax, color="#b48a25", linewidth=1.6, zorder=2)
    alternative_geom = MultiLineString([line for _, _, line in alternative])
    current_geom = MultiLineString([line for _, _, line in current])
    gpd.GeoSeries([alternative_geom], crs=edges.crs).plot(
        ax=ax, color="#8e44ad", linewidth=2.6, zorder=3)
    gpd.GeoSeries([current_geom], crs=edges.crs).plot(
        ax=ax, color="#1672c4", linewidth=2.6, zorder=4)
    markers = [
        (origin.geometry, "Origin", "o", "#222222", (10, -23)),
        (Point(FORK_XY), "Fork", "D", "#222222", (13, 14)),
        (current_end, "Current public-road arrival", "s", "#1672c4", (12, -25)),
        (alternative_end, "NW public-road arrival", "s", "#8e44ad", (12, 15)),
    ]
    for point, label, marker, color, offset in markers:
        ax.scatter([point.x], [point.y], s=46, marker=marker, color=color,
                   edgecolors="white", linewidths=0.8, zorder=5)
        ax.annotate(label, (point.x, point.y), xytext=offset, textcoords="offset points",
                    fontsize=10, color=color, zorder=6,
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.88, "pad": 2})
    bounds = gpd.GeoSeries([current_geom, alternative_geom], crs=edges.crs).total_bounds
    ax.set_xlim(bounds[0] - 150, bounds[2] + 150)
    ax.set_ylim(bounds[1] - 150, bounds[3] + 150)
    ax.set_aspect("equal")
    ax.ticklabel_format(style="plain", useOffset=False)
    ax.tick_params(labelsize=9)
    ax.set_xlabel("Easting (m), EPSG:3400")
    ax.set_ylabel("Northing (m), EPSG:3400")
    ax.grid(color="#e7eaed", linewidth=0.5, zorder=0)
    handles = [
        Line2D([0], [0], color="#1672c4", linewidth=2.6,
               label=f"Current complete route: {current_length:,.1f} m"),
        Line2D([0], [0], color="#8e44ad", linewidth=2.6,
               label=f"NW complete alternative: {alternative_length:,.1f} m"),
        Line2D([0], [0], color="#b48a25", linewidth=1.6, label="Classified public road"),
        Line2D([0], [0], color="#c8cdd2", linewidth=1.0, label="Other prepared roads"),
    ]
    ax.legend(handles=handles, loc="lower right", framealpha=0.96, fontsize=10)
    ax.set_title(f"R0008684: current route is {alternative_length - current_length:,.1f} m shorter",
                 loc="left", fontsize=15, fontweight="bold", pad=16)
    fig.text(0.5, 0.02, "Network travel only; the separate 7.285 m origin snap is excluded from both totals.",
             ha="center", fontsize=10, color="#555555")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(HERE / "Comparison.png", dpi=180, facecolor="white")
    plt.close(fig)
    print(json.dumps({key: value for key, value in result.items() if not key.endswith("edge_ids")}, indent=2))


if __name__ == "__main__":
    main()
