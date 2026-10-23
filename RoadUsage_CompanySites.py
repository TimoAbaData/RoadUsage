"""Report-only origin preference for nearby sites; source routes stay intact.

Input points must already use the workflow's projected metre CRS. Sites use a
10 m maximum pairwise diameter by default, without transitive chaining.
"""
from __future__ import annotations

import json
import math
from shapely.geometry import Point, box
from shapely.strtree import STRtree

PRIORITY = {"WELL": 0, "ERCB_FACILITY": 1, "EQUIPMENT": 2}
EPS_M = 1e-7


def _origin_type(record):
    kind = str(record.get("OriginType") or "UNKNOWN").strip().upper()
    return "ERCB_FACILITY" if kind == "FACILITY" else kind


def _company(record):
    value = record.get("OriginCompany")
    if value is None:
        return None
    value = str(value).strip()
    return None if not value or value.casefold() == "unknown" else value.upper()


def assign_site_preferences(origins_by_route, points_by_route, tolerance_m=10):
    """Return copied origin metadata keyed by the original RouteIDs.

    Greedy deterministic seeds follow WELL > ERCB_FACILITY > EQUIPMENT, then
    OriginUID and RouteID. An unassigned candidate must be within tolerance
    of the seed AND every already accepted member. This prevents transitive
    chaining and keeps each site's maximum pairwise separation within the
    configured threshold. STRtree limits comparisons to nearby candidates.

    The preferred seed's company is not inferred from adjacent origins. If
    known companies disagree among the highest-priority members, only the
    preferred row's OriginCompany becomes UNKNOWN. Its source company and
    every lower-priority record remain available for audit.
    """
    if isinstance(tolerance_m, bool):
        raise ValueError("Site tolerance must be a finite nonnegative distance")
    tolerance = float(tolerance_m)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Site tolerance must be a finite nonnegative distance")

    def preference_key(route_id):
        record = origins_by_route[route_id]
        return (PRIORITY.get(_origin_type(record), 3),
                str(record.get("OriginUID") or ""), str(route_id))

    route_ids = sorted(origins_by_route, key=preference_key)
    if not route_ids:
        return {}
    points = []
    for route_id in route_ids:
        point = points_by_route.get(route_id)
        if not isinstance(point, Point) or point.is_empty or not all(
                math.isfinite(value) for value in (point.x, point.y)):
            raise ValueError(f"Missing or invalid projected origin point: {route_id}")
        points.append(point)
    tree = STRtree(points)
    remaining = set(range(len(route_ids)))
    result = {route_id: dict(origins_by_route[route_id]) for route_id in route_ids}
    for route_id, record in result.items():
        record.setdefault("SourceOriginCompany", record.get("OriginCompany"))
        record.setdefault("SourceOriginCompanyStatus", record.get("OriginCompanyStatus"))
    site_number = 0
    for seed_index, seed_id in enumerate(route_ids):
        if seed_index not in remaining:
            continue
        site_number += 1
        seed_point = points[seed_index]
        nearby = tree.query(box(seed_point.x - tolerance - EPS_M, seed_point.y - tolerance - EPS_M,
                                seed_point.x + tolerance + EPS_M, seed_point.y + tolerance + EPS_M))
        candidates = [int(index) for index in nearby if int(index) in remaining
                      and int(index) != seed_index
                      and seed_point.distance(points[int(index)]) <= tolerance + EPS_M]
        candidates.sort(key=lambda index: (preference_key(route_ids[index])[0],
                                           seed_point.distance(points[index]),
                                           *preference_key(route_ids[index])[1:]))
        members = [seed_index]
        # Repeated exact coordinates add no new diameter constraint.
        distinct_points = {(seed_point.x, seed_point.y): seed_point}
        for index in candidates:
            point = points[index]
            if all(point.distance(member) <= tolerance + EPS_M for member in distinct_points.values()):
                members.append(index)
                distinct_points.setdefault((point.x, point.y), point)
        remaining.difference_update(members)
        priority = preference_key(seed_id)[0]
        top_companies = {_company(origins_by_route[route_ids[index]]) for index in members
                         if preference_key(route_ids[index])[0] == priority}
        top_companies.discard(None)
        conflict = len(top_companies) > 1
        preferred_company = "UNKNOWN" if conflict else (_company(origins_by_route[seed_id]) or "UNKNOWN")
        lower_companies = {_company(origins_by_route[route_ids[index]]) for index in members
                           if preference_key(route_ids[index])[0] > priority}
        lower_companies.discard(None)
        lower_disagreement = bool(top_companies and lower_companies - top_companies)
        for index in members:
            route_id = route_ids[index]
            record = result[route_id]
            record.update(
                SiteID=f"S{site_number:07d}",
                PreferredRouteID=seed_id,
                PreferredOriginType=_origin_type(origins_by_route[seed_id]),
                PreferredCompanyName=preferred_company,
                IsPreferredOrigin=int(index == seed_index),
                SiteMemberCount=len(members),
                SiteDistance_m=float(seed_point.distance(points[index])),
                SiteCompanyConflict=int(conflict),
                SiteCompanyCandidates=json.dumps(sorted(top_companies), ensure_ascii=False, separators=(",", ":")),
                SiteLowerPriorityCompanyDisagreement=int(lower_disagreement),
            )
        if conflict:
            result[seed_id]["OriginCompany"] = "UNKNOWN"
            result[seed_id]["OriginCompanyStatus"] = "SITE_COMPANY_CONFLICT"
    return result
