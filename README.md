# Road Usage: repeatable production workflow

Use **RoadUsage_Config.json** for dataset paths, origin ID/name/company fields,
CRS and distance thresholds. Relative paths resolve against the config file,
not the scheduler's working directory. Current working CRS is **EPSG:3400**.

| Script | Purpose | Main outputs |
|---|---|---|
| `RoadUsage_Step1.py` | Prepare ML road geometry and overlay disposition/road attributes | `Prepared_Roads.gdb` |
| `RoadUsage_Step2_Routing.py` | Route origins to public roads | `RoadUsage_Routes.gpkg`, `Route_Summary.csv`, `Route_Segments.csv` |
| `RoadUsage_Step3_Reporting.py` | Optional disposition distances and ownership reports | `Route_Disposition_Detail.csv`, `Route_Disposition_QC.csv` |
| `RoadUsage_Step4_CompleteRoute.py` | One continuous route feature with disposition lists | `RoadUsage_CompleteRoute.gpkg` (`main.CompleteRoute`) |
| `RoadUsage_Run.py` | Run the steps in order, stopping on failure | timestamped run folder, logs and `Run_Status.json` |

The `_Fast`, topology and bridge files are supporting/diagnostic tools. You do
not need to run them separately. The former duplicate Step 2 and original
Step 1 are preserved in `Workflow_Backup_20261006/`.

## Current delivery

`Delivery_20261006/RoadUsage_Routes.gpkg` contains **30,269 route lines**,
**6,993 zero-distance public-road arrivals**, and **37,105 exceptions**. All
74,367 origins match the reviewed network's reachability. Network route lengths
exclude the separately identified, unverified origin snap offsets.

The numbered workflow passed an ArcGIS end-to-end run with new synthetic inputs
(`Runs/Smoke_20261006_112432/Delivery/`), including origin name/company handoff,
all four routing outcomes, and company/disposition reports. **52 unit tests
passed**, including the subsequent disposition overlap precision fix. ArcGIS
processing requires its licensed environment; the Codex sandbox could not
initialize that license, but the same test succeeded outside the sandbox.

Optional reports are in `Delivery_20261006/`; use them only when
`Step3_Complete.json` reports `COMPLETE`. Archived reports in `Report_Backup_*`
are superseded. Company-use reports are not generated for this snapshot because
origin companies were not retained and no subject company is configured.

## Run a new dataset

From the ArcGIS Pro clone environment, edit the configuration and run:

```powershell
python -B -u RoadUsage_Run.py --config RoadUsage_Config.json
# Optional reports and CompleteRoute after routing succeeds:
python -B -u RoadUsage_Run.py --config RoadUsage_Config.json --with-report
```

The `disposition_filter` in `RoadUsage_Config.json` uses the ML team's
disposition-type and purpose-code selection. Step 1 applies it before copying,
projecting, polygon conversion, and road overlay. Step 3 applies the same filter
to its polygon source, including an explicit `--dispositions` source. Both
`DISP_TYPE` and `PURPCD` must be present; source and selected counts are logged.
Existing runs retain their original disposition attribution. Make a new run to
produce outputs with this filter.

A fresh folder is created under `output_root` for every run. Sources and prior
runs are preserved. Step 1 creates a new processed geodatabase and does not
modify the ArcGIS Pro project or open a GUI. Missing/unreadable required inputs
fail the run. The runner streams progress to the terminal and the step log;
`Run_Status.json` records completion, failure, or interruption. Step 1 road
matching reports its processed-segment rate every 10,000 rows. It also skips
buffer intersections for candidates whose measured separation exceeds the
configured 3 m overlap buffer. No bridge candidates are automatically
accepted or inserted.

To run individual steps, use the same configuration and run folder:

```powershell
python -B -u RoadUsage_Step1.py --config RoadUsage_Config.json --run-dir Runs/example
python -B -u RoadUsage_Step2_Routing.py --config RoadUsage_Config.json --run-dir Runs/example
python -B -u RoadUsage_Step3_Reporting.py --config RoadUsage_Config.json --run-dir Runs/example
python -B -u RoadUsage_Step4_CompleteRoute.py --config RoadUsage_Config.json --run-dir Runs/example
```

Do not change configuration between steps. Choose a new folder to rerun outputs;
existing geodatabases and route exports are not overwritten. Pickle caches are
not used by normal future runs.

## Windows Task Scheduler

Use the ArcGIS Pro clone's **python.exe** as the Program/script. Arguments:

```text
-B -u "D:\Work files\Ad-Hoc\Road Usage\Road Usage Python\RoadUsage\RoadUsage_Run.py" --config "D:\Work files\Ad-Hoc\Road Usage\Road Usage Python\RoadUsage\RoadUsage_Config.json"
```

Add `--with-report` only when needed. Run under an account with ArcGIS Pro and
access to the input locations. For mapped drives, configure paths available to
that scheduled account (UNC paths if appropriate). In task settings select
**Do not start a new instance** if the previous run is still running. No task
has been installed automatically; choose your desired schedule in Windows.

## Read the route delivery

- `Generated_Routes`: continuous, oriented network geometry from the snapped
  point to a public road. One feature per positive-length route.
- `main.CompleteRoute` in `RoadUsage_CompleteRoute.gpkg`: one `LineString` feature
  per positive-length network route with the same `RouteID` and distance as
  `Generated_Routes`. Run Step 4 after the disposition report, or use
  `--with-report` to run both automatically. `DispositionID`,
  `DispositionOwnerName`, `OwnerConflict`, `DistanceTravelled_m`,
  `DistanceTravelled_km`, and `PercentOfRoute` are JSON lists in the attribute
  table. Entries at the same position refer to the same disposition; lists
  are ordered by disposition ID. Empty lists mean no disposition overlap.
  `DispositionAttributed_km` is unique covered distance and `Overlap_km`
  flags duplicated attribution where dispositions overlap.
- `Origin_Results`: every origin, including zero-distance public-road arrivals
  and exceptions; origin identifiers and available names/companies are retained.
- `Public_Road_Arrivals`: arrival points and destination source-road identifiers.
- `Unroutable_Origins`: origins over the snap limit or disconnected from public roads.
- `Origin_Snap_Links`: **unverified** straight snap offsets for reachable origins,
  kept separate from network route geometry. These are not approved access roads.

`TotalRoute_m` / `TotalRoute_km` measure **network travel only**. `SnapDistance_m`
is separate; it is not added to route length. `ALREADY_PUBLIC` means the snapped
point has reached a public road (zero network length); such origins have an
arrival point and result row, not a fabricated zero-length line. Snap distances
above the warning threshold retain `SnapQC = REVIEW`.

`Route_Segments.csv` retains ordered edge IDs and actual travelled partial/full
segment lengths. Destination IDs identify the processed source road; use
`DestinationSourceLayer` and `DestinationSourceOID` to locate it. Road names
remain blank when unavailable. Origins lacking configured name/company fields
also retain blanks rather than inferred values.

The delivery run can explicitly reuse the reviewed snapshot:

```powershell
python -B -u RoadUsage_Step2_Routing.py --config RoadUsage_Config.json --run-dir Delivery_20261006 --reviewed-cache Bridge_QA_20261005_124955/Bridge_QA_Run_Summary.json
```

This special mode checks reviewed input metadata, topology/engine code hashes,
graph counts and every origin's prior reachability. The reviewed snapshot has
origin IDs/types but no origin names/companies. It is for this delivery only,
not a way to process new input data. All 6,609 bridge candidates were rejected;
none are included in the routes.

## Optional reporting

Step 3 uses valid `Disposition_Polygons` from Step 1. An existing polygon feature
class can be selected explicitly with `--dispositions`. It measures polygon coverage as distance intervals on the travelled road
segments and merges overlapping intervals, avoiding double attribution from
floating-point line slivers or polygon unions. It aggregates repeated use of
the same disposition and excludes
unverified snap links. `DispositionAttributed_km` measures unique union coverage;
`Overlap_km` flags distance attributed to multiple dispositions. Do not sum
per-disposition distances as unique route distance when that flag is present.

Set `reporting.subject_company`, `reporting.owner_field`, and each origin's
`company_field` to enable company reports. Names are matched exactly after
case/whitespace normalization; unknown companies and conflicting disposition
owners are not assigned to external companies by guesswork. Without these
fields the distance report still works and company reports are marked unavailable.

Use `--replace-report` on Step 3 to preserve existing reports in a timestamped
backup and regenerate them. Report completion requires every reachable route
to reconcile its attributed and unattributed distances to its network total.

A step is deliverable only when its `StepN_Complete.json` says `COMPLETE`.

---

## Historical implementation and QA notes

# RoadUsage

**Current checkpoint (2026-10-05):** The user accepted spatial review of the
nine corrected junctions. The decision is recorded in
`Topology_Spatial_Review_Decision.json`; the previous topology-review deferral
is lifted. Fresh bridge QA completed in `Bridge_QA_20261005_124955/`
from the reviewed cache: 6,609 candidates, including 351 priority candidates for
components containing 1,043 disconnected origins. All 74,367 origins matched
the validated network. Its `Run.log` records the run. All 6,609 unaccepted candidates were subsequently rejected by the user because
no probable road connection was found. Rejection reasons are recorded in the
QA layers and priority CSV; a pre-decision backup is retained. No synthetic
connectors were inserted.

## Existing-road connectivity comes before gap bridges

`RoadUsage_Step2_Routing_Fast.py` now prepares existing-road topology before
building the graph. Its previous exact `unary_union` did split 2D intersections,
but nearest-midpoint attribute recovery could lose public classification on
overlaps, graph construction omitted closed roads, and the 500 m public-only
destination filter could reject a component that already contained public roads.
The old main also automatically inserted 5 m near-miss connectors. That call is
removed from the topology-first routing path.

The replacement `noded_segments()` delegates to
`RoadUsage_Topology.prepare_road_topology()`. It moves eligible existing endpoints
by at most `TOPOLOGY_SNAP_TOLERANCE_M`, splits source lines at approved junctions,
retains their attributes directly, and splits closed roads into usable edges.
Junction IDs accompany coordinates so separated crossings cannot become shared
nodes through rounding. `edge_nodes()` is used consistently by graph construction,
origin routing, and partial-route geometry. Public destination nodes come from
the same graph; the default minimum public-only component length is now zero.
An explicit nonzero `MIN_PUBLIC_COMPONENT_LENGTH_M` still enables that filter.
Approved endpoint snaps explicitly register a shared junction: projected points
on sloping lines can fail an exact `intersects()` test by floating-point epsilon.

The inspected dataset has 35,297 exact private-endpoint/public-road touches and
about 1,195 positive gaps below 0.25 m. Most positive gaps within the former 5 m
threshold are much larger. The initial topology tolerance is therefore **0.25 m**,
separate from the later bridge search's 100 m threshold. Ambiguous nearby roads,
already established junctions that would move, and cascading anchor moves are
held for review rather than snapped indiscriminately.

The processed routing layers have no Z, bridge, tunnel, or grade-level fields.
The original `_input_Roads_Projected` reference does contain `STRUCTTYPE` and
`STRUCTID`, but Step 1 does not carry them into these routing layers. Their
presence is not proof of elevation at every crossing; unknown values must not
be interpreted as a confirmed ground-level connection. Exact
endpoint junctions and overlapping source roads supply junction evidence.
Interior-to-interior crossings with unknown grade remain separate and are
flagged for review. Configure `GRADE_LEVEL_FIELD` only when verified road-level
data is available; matching known levels allow genuine crossing junctions.
Different levels, structure flags, or conflicting Z values block connection.
This conservative policy can hold routes that the old unconditional 2D union
allowed. The before/after report explicitly counts such cases.

### Reproducible diagnosis and ArcGIS Pro QA

Use the **same activated ArcGIS Pro clone** for the inspection cache and its
comparison (the cache contains environment-specific NumPy/Pandas objects):

```powershell
python .\inspect_topology_inputs.py
python .\RoadUsage_Topology_QA.py
```

The read-only inspection writes `Topology_Input_Inspection.json` and a processed
input cache. The comparison reuses the existing graph and Dijkstra functions.
Its baseline is the original exact-intersection noding **without synthetic
connectors**, with the previous 500 m destination filter. The repaired comparison
uses the new topology and public-access rule. `ReachablePublicBefore` separately
shows access to any public road before changing the destination filter.

Outputs in the project folder:

- `RoadUsage_Topology_QA.gpkg`, layer `Topology_Gaps`: point locations with the
  requested issue, road, distance, before/after component, and repair fields.
  `SourceX/Y` and `TargetX/Y` identify endpoint displacement. Points also represent
  zero-distance intersections safely. No connector road edges are created.
  `Recovered_Origins` and `Origins_Now_Held` show reachability changes as points.
- `Topology_Road_Source_Lookup.csv`: join `SourceRoadID` to QA road IDs;
  `SourceLayer` and `SourceOID` identify the original processed feature. Existing
  `ROAD_SRC_OID` reference metadata is retained when available.
- `RoadUsage_Topology_QA_Failed_Origins.csv` and `Topology_Diagnostics.log`: every
  failing origin's snapped node/segment/class, component ID and node/edge counts,
  public access, nearest public geometry/node distances, geometric private/public
  touching, and failure reason. Geometric touching alone does not certify grade.
  Distances are measured from the original origin point; the snapped node is the
  nearer endpoint along its snapped segment when no route endpoint can be chosen.
- `RoadUsage_Topology_QA_Origin_Comparison.csv`, component CSVs, and summary JSON:
  full-network before/after counts and origin reachability, including improvements
  and formerly reachable cases now held by conservative topology.
- `RoadUsage_Topology_Example.gpkg`: raw roads, before/after edges, repaired routes,
  origin snaps, and topology issues around **114.9018549 W, 52.2732104 N**.
  The local selection uses a 2 km radius; full-network results establish actual
  reachability beyond that boundary. Nearest origin IDs are in
  `Topology_Example_Nearest_Origins.csv`.

The repair function preserves all original attributes. The full-network
diagnostic graph stores routing fields and source identifiers to limit memory;
normal routing retains all attributes, and the example's repaired attribute
snapshot is also saved alongside its GeoPackage.

The final 2026-10-01 full-data comparison covered 74,367 configured origins.
Components changed from 1,500 to 1,433, with public-road components changing
from 61 to 32. There were 172 newly reachable origins: 11 previously rejected
by the public destination length filter and 161 that previously lacked graph
public access. There were also 28 previously reachable origins held for review in that
historical snapshot. The 2026-10-05 precision-gap follow-up below resolves their
graph reachability; spatial QA remains pending. Fewer components alone is not
a valid success criterion.
The baseline excludes the former automatic 5 m connectors.

At the supplied coordinate, nearest well `0385071414000` and facility
`ABBT0047002` are reachable before and after repair in the full graph. Nearby
facility `ABGS0133895` also remains reachable after explicit snap junctions.
The focused comparison preserves all 20 previously reachable origins. The coordinate
has therefore not established the exact reported failed origin or its cause.
Use the comparison CSV and focused road layers to identify that case.

To refresh only the focused export, use
`python .\RoadUsage_Topology_QA.py --example-only` in the same clone. Healthy
exact junctions are omitted from `Topology_Gaps`; full failed-origin records
remain in the log while console output shows progress and summaries.

The previous `RoadUsage_Bridge_QA.gpkg` predates this repair. Review topology QA
first; generate new synthetic bridge candidates separately only for genuinely
disconnected existing geometry after topology review. No bridge candidate is
automatically accepted or added by these scripts.

### Focused review of routing regressions

`python -B .\review_topology_regressions.py` uses the completed comparison CSV,
topology QA, and road source lookup without rebuilding or changing the network.
It writes `Topology_Regression_Review.gpkg` with `Held_Origins` and
`Nearby_Unrepaired_Issues`, plus `Topology_Regression_Review_Summary.json`.
Origin IDs, including leading zeros, are retained. Source/target layer names and
OIDs let reviewers locate processed road features directly in ArcGIS Pro.

The current review contains 28 held origins in five components. Component 37
contains 22 of them. No after-repair origin snap is flagged as ambiguous; the
maximum before/after snap-distance change is 0.0153 m, while 12 origins selected
a different source-road ID. The cause of those changes still needs road review.
The export selects 362 unrepaired issues within a 2 km context radius, including
three crossings with unverified grade. Other reasons include collapsed roads,
ambiguous nearby targets, locked anchors, and self-crossings. Proximity does not
establish that an issue caused a failed route. This radius is only for context;
it is neither the topology snapping tolerance nor a synthetic bridge tolerance.

Review `Held_Origins` for component 37 first, then the other four components.
Check source roads and relevant junctions against authoritative road/structure
information before approving a connection. The review output remains
`PENDING_ROAD_AND_GRADE_REVIEW`; it creates no connector edges.

## Step 2: nearby bridge candidates for ArcGIS Pro QA

Run `RoadUsage_Step2_BridgeCandidates.py` in the ArcGIS Pro Python environment
after Step 1 has produced `Road_Network_Private_Unknown` and
`Road_Network_Public`. Configure `INPUT_GDB`, the two layer names,
`TARGET_WKID`, `MAX_GAP_BRIDGE_M`, and the QA output paths near the top:

```powershell
python .\RoadUsage_Step2_BridgeCandidates.py
```

The script reuses the reader, topology preparation, and graph builder in
`RoadUsage_Step2_Routing_Fast.py`. It bypasses automatic near-miss connectors
and does not run routing or disposition reporting. Topology repair runs first.

### Existing representations

- `RoadUsage_Step1.py` recovers attributes and writes road-class feature classes;
  it does not build a graph. `RoadUsage_Step2_Routing.py` also currently contains
  this attribute-preparation workflow despite its filename.
- The fast routing script uses an undirected NetworkX `MultiGraph`. Node IDs are
  `(x, y)` endpoint coordinates rounded to three decimal places. Edges store
  `EDGE_ID`, original Shapely line geometry, `ROAD_CLASS`, `LENGTH_M`, and metre
  length as `weight`. Repaired edges also carry junction IDs in their node keys.
  Closed roads are split before graph construction.
- Public-road nodes are endpoints of `PUBLIC` edges. The router builds a separate
  public-only graph and accepts destination nodes only in public-only components
  totalling at least `MIN_PUBLIC_COMPONENT_LENGTH_M` (formerly 500 m, now 0 m).
  Its `PUBLIC_COMP_ID` values are not full-network component IDs.
- The full graph previously had only a connected-component count. The router
  labels an origin `UNROUTABLE_DISCONNECTED` when neither endpoint of its snapped
  edge has a destination distance in the one-time Dijkstra result.

### Candidate search and review

The QA script computes full-network component IDs once. A component has public
access when it contains any `PUBLIC` edge; all nodes in that component can reach
that road through the undirected graph. This deliberately answers the Step 2
public-access question; the router now defaults to the same public-access rule.

For each disconnected component, the search uses end nodes with one unique
neighbour, including ends with duplicate owners of the same geometric path.
Distinct parallel paths forming a two-node closed loop are excluded: one unique
neighbour does not by itself prove an open endpoint. A Shapely STRtree indexes only
segments in components with public access. Each candidate connects the actual,
unrounded source endpoint to the closest point on a target segment, which can be
a private road in a component with public access. Only positive geometric gaps
at or below `MAX_GAP_BRIDGE_M` (default 100 m) are retained. Distances use the
projected CRS in metres. The current EPSG:3400 configuration is preserved.

Candidates are deduplicated by connector endpoints and target component, then
ranked by increasing gap distance **within each source component**. All candidates
inside the threshold are retained, with deterministic tie ordering. No alignment
or terrain assessment is performed; the straight connector is for visual review.
Components without end nodes, including cycles, have no candidates in this first
endpoint-based search. They remain visible in the component QA CSV. Candidate
and component IDs apply to the current graph build, not across changed datasets.

Open **`RoadUsage_Bridge_QA.gpkg` → `Candidate_Gap_Bridges`** in ArcGIS Pro
alongside the Step 1 road layers. Filter `CandidateRank = 1` to see the nearest
candidate per source component, then inspect other ranks as alternatives.
The layer preserves:

- `CandidateID`, `SourceComponentID`, `TargetComponentID`, `CandidateRank`
- `GapDistance_m`, `TargetHasPublicRoad` (1 for every retained target)
- `SourceNodeID`, `TargetNodeID` (null when the target lies inside a segment)
- `SourceSegmentID`, `TargetSegmentID` (the noded graph's `EDGE_ID` values)
- `Accepted` (0 for every candidate), blank `RejectionReason` for manual review,
  and connector LineString geometry

`Bridge_Component_QC.csv` contains full-network component IDs, public access,
node/edge/end-node counts, and candidate counts. Disconnected rows with
`CandidateCount = 0` identify components with no reachable target inside the
threshold or no end nodes to search. These outputs are separate from source
features and the existing routing GeoPackage. A rerun replaces the candidate
layer even when the new result is empty. Close that layer in ArcGIS Pro before
rerunning to avoid file locks. Editing `Accepted` is only a QA annotation;
there is no code that adds candidates to the graph.

`SearchStatus` distinguishes `HAS_PUBLIC_ROAD`, `NO_ENDPOINTS`,
`NO_CANDIDATE_WITHIN_LIMIT`, and `CANDIDATES_FOR_REVIEW` in the component CSV.
A regenerated run also writes `Bridge_QA_Run_Summary.json` with the UTC timestamp,
input layers, CRS, topology and bridge tolerances, network counts, candidate gap
range, and `PENDING_MANUAL_REVIEW` status. This records provenance; it does not
certify topology approval or physical access across any gap.

Full-network QA regeneration is deliberately deferred until review of the 28
previously reachable origins now held by topology processing is complete.
The obsolete component CSV was removed during approved cleanup on 2026-10-05.
The earlier bridge GeoPackage remains because another application holds a lock;
the user chose to leave it for now. It must not be treated as repaired-network QA.
After that review, run the command above in the ArcGIS Pro clone to regenerate
both outputs and the summary from the repaired network. Inspect
`CandidateRank = 1` first, then alternatives; distance ranking alone does not
verify alignment, terrain, grade, or permission to cross a gap.

### Verification

The search and GeoPackage writer can be tested without ArcPy:

```powershell
python -m pytest tests -q
```

### Precision-gap regression follow-up (2026-10-05)

The focused trace reproduced all 28 held origins in components 37, 103, 293,
387 and 744. Their baseline routes cross endpoint separations of approximately
0.0001 m beside millimetre-length existing road fragments. The former graph's
three-decimal node rounding joined these endpoints. The topology repair instead
held them as `AMBIGUOUS_NEAR_ROADS`: its fixed 0.01 m ambiguity window treated
a near-coincident target and another target several millimetres away as tied.

The ambiguity window is now capped by the nearest gap as well as
`SNAP_AMBIGUITY_WINDOW_M` (0.01 m). Genuinely similar targets remain held;
grade checks, the 0.25 m movement limit, source attributes and the prohibition
on synthetic connectors are unchanged. A regression test reproduces the old
failure and checks that the short existing road remains present.

`trace_topology_regressions.py --components 37 103 293 387 744` writes a new
timestamped `Regression_Trace_*` directory with per-component GeoPackages.
Inspect `Baseline_Changed_Transitions`, `Baseline_Route_Segments` and
`Local_Network_After`. Local IDs differ from full-network component IDs,
and selection boundaries can influence topology; these exports are focused
evidence, not independent full-network certification. Legacy source attribution
on overlaps also needs verification.

`validate_topology_precision.py` checks every cached origin against the current
full graph and saved comparison, without rebuilding the legacy baseline or
overwriting historical QA. It writes a new `Topology_Validation_*` directory
with a summary, origin comparison, component counts and changed-origin GIS
layers. It uses one destination-tree computation and checks origin ID/CRS
preservation on export. Run both scripts in the ArcGIS clone used for the cache.
Bridge regeneration remains deferred pending review of the updated evidence.

The full-network validation completed on 2026-10-05:
**74,367 origins checked; all 28 regressions recovered; zero new reachability
regressions**. Reachable origins increased from 37,234 to 37,262. All 38 unit
tests passed. The 28 focused restored routes also passed segment-length and
endpoint-continuity checks, with zero adjacent endpoint gaps.

Current evidence:

- `Topology_Validation_20261005_113118/Summary.json`: full-network result.
- `Topology_Validation_20261005_113118/Validation_Origins.gpkg`: recovered origins.
- `Regression_Trace_20261005_113119/Precision_Gap_Review.gpkg`, layer
  `Corrected_Endpoint_Junctions`: nine corrected junctions, gap distances and
  source layer/OID references.
- `Regression_Trace_20261005_113119/Component_*/Focused_Regression_Trace.gpkg`:
  local networks and `Current_Route_Segments` for the restored origins.
- `Regression_Trace_20261005_112211/`: preserved before-fix evidence.

These junctions' input features remain classified as disposition-inferred or
unknown, with input QC flags of REVIEW and no original-road reference OID.
The status is **CONNECTIVITY_VALIDATED_PENDING_SPATIAL_QA**; graph reachability
does not establish physical access or grade. Review these locations in ArcGIS
Pro before regenerating bridge candidates. No synthetic connectors were added.

## Bridge QA after accepted junction review (2026-10-05)

`regenerate_bridge_qa.py` rebuilds the reviewed cached inputs and requires the
recorded spatial-review decision. It checks every origin's identity, component,
and reachability against `Topology_Validation_20261005_113118` before exporting.
Run it in the same ArcGIS Pro clone used to create `Topology_Input_Cache.pkl`.
It creates a timestamped output folder and preserves previous QA outputs.

In the new `RoadUsage_Bridge_QA.gpkg`:

- Start with `Priority_Bridges`, the nearest candidate for each disconnected
  component with affected origins. Sort `AffectedOrigins` descending, then
  `GapDistance_m` ascending to focus the review.
- Use `Candidate_Review` for alternative candidates and source/target road
  references. `TOPOLOGY_OR_GRADE_REVIEW` marks gaps at or below 0.25 m that need
  existing-road topology or grade investigation before synthetic bridging.
- Record decisions in **`Candidate_Gap_Bridges`**, matching `CandidateID`.
  `Candidate_Review` and `Priority_Bridges` are static reference copies; their
  decision fields do not synchronize with the primary layer.
- Inspect `Disconnected_Origins` alongside candidates. `Distant_Origins`
  contains origins exceeding the 100 m snap limit and needs separate access
  or positioning review. Distance bands are also exported to CSV.

All candidates start with `Accepted = 0`. Component origin counts represent
potential benefit, not recovered routes. The search currently uses eligible
endpoints to reachable segments; components without eligible endpoints or a
candidate within 100 m remain unresolved. No connectors are inserted.

`Prepared_Edges.pkl` preserves the rebuilt geometry for future reuse in the
same clone. The runner currently rebuilds on each invocation; it does not
implicitly reload this file. `Bridge_QA_Run_Summary.json` records input and code
provenance. The complete test suite passed with **41 tests** for this change.
