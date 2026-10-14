# Road Usage Project — Codex Context

## Current delivery workflow (2026-10-06)

The user now requests a simple, schedulable numbered workflow. This supersedes
older bridge-search-only scope below: Step 1 prepares roads/attributes; Step 2
creates routes and exceptions; optional Step 3 reports disposition distances
and ownership where source fields permit. RoadUsage_Config.json is the shared
configuration and RoadUsage_Run.py runs the numbered scripts. The reviewed
working CRS is EPSG:3400, superseding the historical Web Mercator setting.
Apply the configurable ML-team disposition filter from RoadUsage_Config.json
to every disposition read in Steps 1 and 3; preserve prior runs.
All 6,609 reviewed bridge candidates were rejected; do not insert them. Keep
source data unchanged, preserve prior runs, and do not delete files without
approval. The Thursday delivery prioritizes routes and exceptions over reports.

## Project Purpose

This project builds a routable road network for Alberta oil and gas wells/facilities and determines the shortest practical route from each origin to a public road.

The project must also eventually report:
- total route distance from origin to public road
- which dispositions were used along the route
- distance travelled on each disposition
- which other companies' dispositions were used by the subject company
- which external routes used the subject company's dispositions

IMPORTANT CURRENT SCOPE:
For now, start from Step 2 of the missing-road workflow:
**Search for a nearby network bridge.**

Do not implement the later disposition reporting/ownership workflow yet unless explicitly requested.

---

# Development Environment

Primary environment:

- Windows
- Python 3.11
- ArcGIS Pro Python environment / clone
- ArcPy
- GeoPandas
- Shapely
- NetworkX
- Pyogrio where practical

Example Python environment:

C:\Users\timothys\AppData\Local\anaconda3\envs\geo311\arcgispro-py3-clone\arcgispro-py3-clone

Project folder:

D:\Work files\Ad-Hoc\Road Usage\Road Usage Python\RoadUsage

ArcGIS Pro project:

D:\Work files\Ad-Hoc\Road Usage\ArcGIS\RoadUsage.aprx

ArcGIS map name:

Road Usage

Main processing script historically:

RoadUsage_Step1.py

Do not hard-code paths where avoidable. Prefer configuration variables near the top of the script.

---

# Core Datasets

## Roads

Polyline feature class containing the existing road network.

Important field:

ROADJURIS

Used to distinguish public versus private roads.

---

## Disposition

Polyline dataset representing disposition boundaries.

Important field:

DISP_NUM

Potential additional fields:
- disposition name
- operator
- licensee
- disposition type
- status
- ownership/company identifiers

Disposition geometry may contain:
- closed loops
- small gaps
- large gaps
- open ends
- fragmented boundaries

Never silently close large gaps.

---

## Road_Network

Processed / machine-generated road geometry.

Geometry type:

Polyline

This may contain useful road geometry but not ownership/disposition attributes.

Previous projected feature count was approximately:

126,403

---

## Wells / Facilities

These are route origins.

Each origin should preserve:
- Origin Type: Well or Facility
- Origin ID
- Origin Name
- Company/operator where available

The ultimate target is to route each origin to a public road.

---

# Coordinate System

Routing and distance calculations must use a projected CRS.

Current target CRS:

WGS_1984_Web_Mercator_Auxiliary_Sphere

Distances are calculated in meters internally.

Convert to kilometers only for reporting:

total_route_km = total_route_length_m / 1000.0

---

# Road Classification

Normalize road classification into:

- Public
- Private
- Unknown

Keep the original ROADJURIS value.

Centralize classification logic in one function, for example:

```python
def classify_road(roadjuris):
    ...
```

---

# Routing Goal

For every Well or Facility:

1. Snap the origin to the usable road network.
2. Route through the road graph.
3. Reach the public-road network.
4. Preserve the complete route geometry.
5. Calculate total route distance.
6. Later, intersect the completed route with disposition data for reporting.

---

# NetworkX Routing Design

The road network should be represented as a graph:

- nodes = road intersections/endpoints
- edges = road segments
- edge weight = segment length in meters

The main routing direction is:

Well / Facility -> Public Road

Use multi-source Dijkstra where practical.

All public-road nodes can act as destinations:

```python
dist_to_dest, path_to_dest = nx.multi_source_dijkstra(
    G,
    sources=destination_nodes,
    weight="weight",
)
```

Prefer computing this once for the graph rather than once per origin.

---

# Known Problem: Missing Road Segments

Some wells/facilities visibly have established access roads, but the connecting segment is absent from:
- Roads
- Road_Network
- disposition-derived geometry

This causes disconnected graph components or gaps.

Symptoms include:
- route stopping before reaching public road
- no route found
- unrealistic detour
- well visibly near a road but topologically disconnected

Do not solve this by blindly connecting every origin to the nearest road with a straight line.

Synthetic connections must be constrained and auditable.

---

# Missing-Road Workflow

## Step 1 — Detect disconnected cases

This logic may already exist or may be handled by the current routing process.

Determine whether the origin's snapped graph component contains a public-road node.

If yes:
- route normally

If no:
- treat the origin/component as disconnected

CURRENT IMPLEMENTATION PRIORITY DOES NOT START HERE.

---

# STEP 2 — CURRENT PRIORITY: Search for a Nearby Network Bridge

THIS IS THE CURRENT DEVELOPMENT STARTING POINT.

For a disconnected road component:

1. Identify boundary/end nodes belonging to the disconnected component.
2. Search for nearby nodes or road segments belonging to a different component.
3. Prefer candidate components that already contain a public-road node.
4. Compute the true geometric gap distance.
5. Rank bridge candidates by:
   - shortest distance
   - component reachability to public roads
   - endpoint alignment
   - geometric plausibility
6. Reject candidates beyond a configurable maximum bridge distance.

Example configuration:

```python
MAX_GAP_BRIDGE_M = 100.0
```

Keep this configurable.

A reasonable testing range may be approximately 50–150 m, but do not hard-code that range into the algorithm.

---

## Candidate Bridge Search Requirements

The search must be spatially efficient.

Prefer:
- GeoPandas spatial index
- Shapely STRtree
- scipy KDTree / cKDTree for node searches
- precomputed component IDs

Avoid nested comparison of every disconnected endpoint against every road/network node.

For each graph node or relevant endpoint, retain a component identifier.

For each connected component, also retain whether that component can reach a public road.

Example conceptual metadata:

```text
component_id
contains_public_road
node_count
edge_count
```

---

## Preferred Candidate Order

Bridge search should prioritize:

1. disconnected endpoint -> reachable road segment
2. disconnected endpoint -> reachable endpoint/node
3. disconnected segment -> reachable segment

Prefer connecting into a component that contains public-road access.

Do not bridge from one disconnected component into another disconnected component unless there is a clear reason and the resulting merged component is subsequently evaluated.

---

## Gap Distance

Calculate true geometry distance in the projected CRS.

Do not use latitude/longitude degrees.

Record:

- SourceComponentID
- TargetComponentID
- SourceNodeID / SourceSegmentID
- TargetNodeID / TargetSegmentID
- GapDistance_m

---

## Endpoint Alignment

Distance alone is not enough.

Where possible, compare the direction of the road near the source endpoint against the direction toward the candidate bridge.

A candidate continuing approximately in the same direction should score better than one requiring an implausible sharp turn.

Potential factors:

- gap distance
- source bearing
- target bearing
- angular difference
- whether candidate component reaches public road

Do not overcomplicate the first implementation. Distance + reachability should be implemented first; alignment can be added as a second-stage refinement.

---

## Step 2 Output — Candidate Bridges

Before automatically modifying the graph, produce a QA output containing candidate bridges.

Suggested output:

Candidate_Gap_Bridges

Fields:

- CandidateID
- OriginID if applicable
- SourceComponentID
- TargetComponentID
- SourceNodeID
- TargetNodeID
- GapDistance_m
- TargetHasPublicRoad
- AlignmentScore if implemented
- CandidateRank
- Accepted
- RejectionReason
- Geometry

Geometry should be a line connecting the source gap location to the selected candidate location.

This allows visual inspection in ArcGIS Pro.

---

## Current Step 2 Goal

The first implementation should answer:

> For each disconnected component, what is the nearest plausible bridge into a road component that can reach a public road?

Do not automatically merge every candidate immediately.

First generate:
- ranked candidates
- gap distances
- target component reachability
- candidate connector geometry

Then inspect the result in ArcGIS Pro.

---

# Step 3 — Future: Create Accepted Synthetic Connectors

Do not focus on this until Step 2 candidate generation is working well.

Accepted synthetic connectors should eventually preserve:

- Synthetic = 1
- ConnectionType = "GapBridge"
- GapDistance_m
- Confidence
- SourceComponentID
- TargetComponentID
- source/target segment IDs

Synthetic connections must remain distinguishable from authoritative road geometry.

---

# Step 4 — Future: Flag Uncertain Cases

If no acceptable bridge exists:

write the case to:

Unroutable_Origins

Suggested fields:

- OriginID
- OriginType
- OriginName
- ComponentID
- nearest reachable network distance
- reason
- suggested review status

---

# Final Route Reporting Requirement

The eventual completed workflow must support route-level and disposition-level reporting.

For every route preserve:

- RouteID
- OriginType
- OriginID
- OriginName
- OriginCompany
- DestinationRoadID
- DestinationRoadName
- TotalRoute_m
- TotalRoute_km
- RouteStatus

Example:

| OriginType | OriginID | OriginName | PublicRoadConnection | TotalRoute_km |
|---|---|---|---|---:|
| Well | W12345 | Example Well | Public Road Segment 9821 | 12.47 |

---

# Disposition Distance Reporting — Future

After routing works reliably, intersect the final route with disposition data.

For every disposition actually used by the route, retain:

- RouteID
- OriginType
- OriginID
- OriginName
- OriginCompany
- DispositionID
- DispositionName
- DispositionOwnerID
- DispositionOwnerName
- DispositionType
- DistanceTravelled_m
- DistanceTravelled_km
- TotalRoute_km
- PercentOfRoute

The distance must be based on actual route/disposition overlap, not the full disposition length.

If a route enters the same disposition multiple times, aggregate the travelled distance for the summary report while optionally retaining segment-level detail.

---

# Final Business Goal — Future

The complete system should eventually answer:

1. Where did the route start?
2. Which public road did it reach?
3. What was the total route distance in km?
4. Which dispositions were travelled on?
5. How many km were travelled on each disposition?
6. Who owns each disposition?
7. Which dispositions did my wells/facilities use?
8. Which other companies' wells/facilities used my dispositions?
9. How much of my disposition did each external route use?

Suggested future outputs:

- Generated_Routes
- Route_Disposition_Detail
- Dispositions_Used_By_Me
- Who_Used_My_Disposition

---

# Important Reporting Rule — Future

Do not double-count disposition distance.

Overlapping dispositions can cause:

sum(disposition distance) > total route distance

Detect and QC this explicitly.

Also calculate:

- TotalRoute_km
- DispositionAttributed_km
- Unattributed_km
- SyntheticConnector_km
- PublicRoad_km where useful

---

# Performance Requirements

Prefer:

- spatial indexes
- STRtree / KDTree
- vectorized GeoPandas/Shapely operations
- one-time graph construction
- one multi-source Dijkstra computation
- precomputed connected components
- precomputed component reachability to public roads

Avoid:

- repeated reading of the same GDB layer
- rebuilding spatial indexes repeatedly
- Dijkstra once per origin
- nested loops over all roads
- loading entire network into Folium for routine QA

---

# Folium

Folium may be used for focused QA only.

For large datasets:
- show selected routes/components only
- simplify preview geometry
- export QA layers to GDB/GeoPackage
- inspect full outputs in ArcGIS Pro

ArcGIS Pro should be the primary visual QA environment.

---

# Coding Style

Prefer modular functions such as:

```text
load_data()
prepare_crs()
classify_roads()
build_graph()
identify_public_destination_nodes()
compute_connected_components()
mark_components_with_public_access()
snap_origins()
detect_disconnected_components()

# CURRENT PRIORITY
find_nearby_bridge_candidates()
rank_bridge_candidates()
write_candidate_bridge_qc()

# FUTURE
accept_gap_bridges()
rebuild_or_patch_graph()
generate_routes()
attribute_dispositions()
write_route_reports()
```

Use logging rather than excessive print statements.

Important logs should include:

- graph node count
- graph edge count
- connected component count
- components containing public roads
- disconnected components
- bridge candidates found
- minimum/maximum candidate gap
- rejected candidates beyond threshold
- processing time

---

# Data Safety

Do not modify original source feature classes.

Write processed outputs separately.

Never overwrite authoritative:
- Roads
- Road_Network
- Disposition
- Wells
- Facilities

unless explicitly requested.

---

# Instructions for Codex

1. Read this AGENTS.md first.
2. Inspect the existing project before making structural changes.
3. Preserve working routing behavior.
4. Start from **Step 2: Search for a Nearby Network Bridge**.
5. Do not jump ahead to automatic bridging, disposition reporting, or ownership reports until Step 2 candidate generation is validated.
6. First inspect the existing graph construction and disconnected-component logic.
7. Reuse existing graph/node/component structures where possible.
8. Implement the smallest reliable change.
9. Produce QA-friendly candidate bridge geometry that can be inspected in ArcGIS Pro.
10. Keep thresholds configurable.
11. Maintain Python 3.11 / ArcGIS Pro compatibility.
12. Explain major algorithm changes before large rewrites.
13. Avoid adding unnecessary dependencies.
14. Preserve original datasets.
