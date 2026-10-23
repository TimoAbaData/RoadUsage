"""Reconstruct saved disposition attribution and summarize company coverage.

Intervals are travelled measures along a route, not unique physical road length.
The existing Step 3 clipping, 20 m rule, and Step 1 protections are retained.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from RoadUsage_Workflow import prepare_environment
prepare_environment()
from shapely.geometry import Point
from RoadUsage_DispositionContinuity import merge_spans, smooth_short_intervals
from RoadUsage_Step3_Reporting import line_parts


def reconstruct_effective_intervals(geometry, dispositions, tree, id_field,
                                    cache=None, minimum_overlap_m=0,
                                    protected_intervals=None):
    """Return disposition IDs mapped to effective travelled spans in metres.

    Mirrors Step 3's interval calculation, including repeated route travel.
    Reuse a cache only within one disposition dataset. Its metadata namespace
    is separate from Step 3's so either function may safely use the same cache.
    """
    if cache is None:
        cache = {}
    parts = line_parts(geometry)
    keys = [part.wkb for part in parts]
    lengths = [part.length for part in parts]
    offsets = []
    travelled = 0.0
    for length in lengths:
        offsets.append(travelled)
        travelled += length
    intervals_by_id = {}
    for part_raw, polygon_raw in tree.query(parts, predicate="intersects").T:
        part_idx, polygon_idx = int(part_raw), int(polygon_raw)
        meta_key = ("company_interval_meta", polygon_idx)
        if meta_key not in cache:
            row = dispositions.iloc[polygon_idx]
            cache[meta_key] = (str(row[id_field]), row.geometry)
        disp_id, polygon = cache[meta_key]
        clip_key = (keys[part_idx], polygon_idx)
        if clip_key not in cache:
            line = parts[part_idx]
            intersection = line.intersection(polygon)
            intervals = []
            for clipped in line_parts(intersection):
                if clipped.length <= 1e-7:
                    continue
                a = float(line.project(Point(clipped.coords[0])))
                b = float(line.project(Point(clipped.coords[-1])))
                low, high = sorted((a, b))
                low, high = max(0.0, low), min(lengths[part_idx], high)
                if high - low > 1e-7:
                    intervals.append((low, high))
            cache[clip_key] = intervals
        intervals = cache[clip_key]
        if not intervals:
            continue
        intervals_by_id.setdefault(disp_id, []).extend(
            (offsets[part_idx] + low, offsets[part_idx] + high)
            for low, high in intervals)
    protected_intervals = protected_intervals or {}
    for disp_id, intervals in protected_intervals.items():
        intervals_by_id.setdefault(disp_id, []).extend(intervals)
    return smooth_short_intervals(intervals_by_id, minimum_overlap_m,
                                  protected_by_label=protected_intervals)


def company_coverage(effective, holder_by_disposition, *,
                     conflict_dispositions=(), route_length_m=None):
    """Union effective route intervals by canonical holder key.

    Mapping values are resolved company-key strings or None. Missing/blank
    values are unknown. IDs in conflict_dispositions override their mapping
    and are never assigned to a particular holder. Company alias resolution
    belongs to the caller; this function does not infer company identity.

    by_holder_m counts each holder's union, even where other holders overlap.
    exclusive_by_holder_m counts only one known holder with no simultaneous
    unknown or conflicting disposition. Thus the additive partition is:
      exclusive_known_m + cross_holder_only_m + uncertain_holder_m
      + unattributed_m == route_length_m (when supplied).
    Other overlap measures intentionally overlap and must not be summed.
    """
    route_length = None
    if route_length_m is not None:
        route_length = float(route_length_m)
        if not math.isfinite(route_length) or route_length < 0:
            raise ValueError("Route length must be finite and nonnegative")
    conflict_ids = set(conflict_dispositions)
    events = defaultdict(Counter)
    for disp_id, spans in effective.items():
        holder = holder_by_disposition.get(disp_id)
        if holder is not None and not isinstance(holder, str):
            raise TypeError("Holder keys must be strings or None")
        holder = holder.strip() if holder else None
        token = (("conflict", "") if disp_id in conflict_ids else
                 ("holder", holder) if holder else ("unknown", ""))
        checked = []
        for start, end in spans:
            start, end = float(start), float(end)
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError(f"Invalid disposition interval: {disp_id}: {(start, end)}")
            tolerance = max(1e-6, (route_length or 0) * 1e-9)
            if start < -tolerance or (route_length is not None and end > route_length + tolerance):
                raise ValueError(f"Disposition interval outside route: {disp_id}: {(start, end)}")
            start = max(0.0, start)
            if route_length is not None:
                end = min(route_length, end)
            checked.append((start, end))
        for start, end in merge_spans(checked):
            events[start][token] += 1
            events[end][token] -= 1

    by_holder = defaultdict(float)
    exclusive_by_holder = defaultdict(float)
    totals = dict.fromkeys(("attributed_m", "exclusive_known_m", "cross_holder_overlap_m",
                           "cross_holder_only_m", "unknown_holder_m", "conflicting_holder_m",
                           "uncertain_holder_m", "ambiguous_m", "known_holder_union_m"), 0.0)
    active = Counter()
    prior = None
    for position in sorted(events):
        if prior is not None and position > prior:
            length = position - prior
            holders = {key for (kind, key), count in active.items()
                       if kind == "holder" and count > 0}
            unknown = active[("unknown", "")] > 0
            conflict = active[("conflict", "")] > 0
            uncertain = unknown or conflict
            if holders or uncertain:
                totals["attributed_m"] += length
            for holder in holders:
                by_holder[holder] += length
            if holders:
                totals["known_holder_union_m"] += length
            if unknown:
                totals["unknown_holder_m"] += length
            if conflict:
                totals["conflicting_holder_m"] += length
            if uncertain:
                totals["uncertain_holder_m"] += length
            if len(holders) > 1:
                totals["cross_holder_overlap_m"] += length
                if not uncertain:
                    totals["cross_holder_only_m"] += length
            if uncertain or len(holders) > 1:
                totals["ambiguous_m"] += length
            if len(holders) == 1 and not uncertain:
                holder = next(iter(holders))
                exclusive_by_holder[holder] += length
                totals["exclusive_known_m"] += length
        active.update(events[position])
        prior = position
    if any(active.values()):
        raise RuntimeError("Disposition interval sweep did not balance")
    totals["by_holder_m"] = dict(sorted(by_holder.items()))
    totals["exclusive_by_holder_m"] = dict(sorted(exclusive_by_holder.items()))
    totals["unattributed_m"] = (None if route_length is None else
                                max(0.0, route_length - totals["attributed_m"]))
    return totals
