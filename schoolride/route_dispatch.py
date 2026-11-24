"""Joint, time-aware dispatch search across all routes.

The first-available-vehicle greedy loop cannot reason about equipment
qualification, driver/vehicle non-overlap, deadhead traffic intervals,
parental reschedule consent or backup coverage.  This module enumerates the
feasible edges produced by :mod:`schoolride.vehicle_match` and searches their
joint combinations, keeping every executable plan plus a minimum conflict
set when no plan can cover all routes.
"""
from .vehicle_match import feasible_edges, to_minutes


def _overlaps(a, b):
    """Half-open interval overlap: a vehicle/driver cannot be double-booked."""
    return a[0] < b[1] and b[0] < a[1]


def _plan_signature(assignments):
    # Same vehicle/time pattern is one scheduling route; driver alternatives
    # are retained on the materialised plan rather than duplicated as plans.
    return tuple(sorted(
        (route_id, edge["vehicle_id"], edge["shape"], edge["pickup"])
        for route_id, edge in assignments.items()
    ))


def search_plans(routes, vehicles, drivers=None, scenario=None, traffic_version=None):
    """Enumerate every executable dispatch plan.

    Routes are considered in input order; chained edges require the
    predecessor route to be served by the same vehicle/driver in the same
    partial plan.  Vehicle and driver service intervals must never overlap.
    """
    scenario = scenario or {}
    report = feasible_edges(routes, vehicles, drivers, scenario, traffic_version)
    edges_by_route = {}
    for edge in report["edges"]:
        edges_by_route.setdefault(edge["route_id"], []).append(edge)

    route_ids = [r["id"] for r in routes]
    windows = {
        r["id"]: (
            to_minutes(r.get("window", {}).get("earliest", r["start"])),
            to_minutes(r.get("window", {}).get("latest", r["start"])),
        )
        for r in routes
    }
    plans = []
    seen = {}

    def backtrack(index, assignments, vehicle_busy, driver_busy, chain_meta):
        if index == len(route_ids):
            signature = _plan_signature(assignments)
            existing = seen.get(signature)
            if existing is not None:
                for route_id, edge in assignments.items():
                    if edge["driver_id"] is not None:
                        candidates = existing["_driver_candidates"][route_id]
                        if edge["driver_id"] not in candidates:
                            candidates.append(edge["driver_id"])
                return
            plan = _materialize(assignments, chain_meta, scenario, report)
            plan["_driver_candidates"] = {
                route_id: [edge["driver_id"]] if edge["driver_id"] else []
                for route_id, edge in assignments.items()
            }
            seen[signature] = plan
            plans.append(plan)
            return
        route_id = route_ids[index]
        earliest, latest = windows[route_id]
        for edge in edges_by_route.get(route_id, []):
            if not (earliest <= edge["pickup"] <= latest):
                continue
            if edge["shape"] == "chained":
                prior = assignments.get(edge["predecessor"])
                if prior is None or prior["vehicle_id"] != edge["vehicle_id"]:
                    continue
                if edge["driver_id"] is not None and prior["driver_id"] != edge["driver_id"]:
                    continue
                if not edge["within_window"]:
                    continue
            if any(_overlaps(edge["service"], busy) for busy in vehicle_busy.get(edge["vehicle_id"], [])):
                continue
            if edge["driver_id"] is not None and any(
                _overlaps(edge["service"], busy) for busy in driver_busy.get(edge["driver_id"], [])
            ):
                continue
            assignments[route_id] = edge
            vehicle_busy.setdefault(edge["vehicle_id"], []).append(edge["service"])
            if edge["driver_id"] is not None:
                driver_busy.setdefault(edge["driver_id"], []).append(edge["service"])
            if edge["shape"] == "chained":
                chain_meta[(edge["vehicle_id"], route_id)] = edge
            backtrack(index + 1, assignments, vehicle_busy, driver_busy, chain_meta)
            assignments.pop(route_id)
            vehicle_busy[edge["vehicle_id"]].pop()
            if edge["driver_id"] is not None:
                driver_busy[edge["driver_id"]].pop()
            chain_meta.pop((edge["vehicle_id"], route_id), None)

    backtrack(0, {}, {}, {}, {})
    for plan in plans:
        for route_id, candidates in plan.pop("_driver_candidates").items():
            plan["assignments"][route_id]["driver_candidates"] = sorted(set(candidates))
            plan["assignments"][route_id]["driver_id"] = candidates[0] if candidates else None
    return {
        "plans": plans,
        "rejected": report["rejected"],
        "edges": report["edges"],
        "segments": report["segments"],
        "traffic_version": traffic_version,
    }


def _materialize(assignments, chain_meta, scenario, edge_report):
    used_vehicles = {edge["vehicle_id"] for edge in assignments.values()}
    used_drivers = {edge["driver_id"] for edge in assignments.values() if edge["driver_id"]}
    chained = [edge for edge in assignments.values() if edge["shape"] == "chained"]
    all_vehicles = [v["id"] for v in scenario.get("vehicles", [])]
    backup = [vid for vid in all_vehicles if vid not in used_vehicles]
    plan = {
        "kind": "B" if chained else "A",
        "assignments": {
            route_id: {
                "vehicle_id": edge["vehicle_id"],
                "driver_id": edge["driver_id"],
                "driver_candidates": [edge["driver_id"]] if edge["driver_id"] else [],
                "pickup": _clock(edge["pickup"]),
                "service": [_clock(edge["service"][0]), _clock(edge["service"][1])],
                "shape": edge["shape"],
                "deadhead_segment": edge.get("deadhead_segment"),
                "traffic_version": edge.get("traffic_version"),
                "deadhead_minutes": edge.get("deadhead_minutes"),
            }
            for route_id, edge in assignments.items()
        },
        "vehicle_occupancy": sorted(used_vehicles),
        "driver_occupancy": sorted(d for d in used_drivers if d),
        "backup_vehicles": backup,
        "chained_routes": sorted(edge["route_id"] for edge in chained),
    }
    return plan


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _reschedule_consent(scenario, route_id, pickup_minutes):
    for approval in scenario.get("parent_approvals", []):
        if approval.get("route_id") != route_id:
            continue
        reschedule = approval.get("reschedule") or {}
        if not reschedule.get("consented"):
            return False, "家长未同意改时"
        latest = to_minutes(reschedule["latest_pickup"])
        if pickup_minutes > latest:
            return False, f"实际到站{_clock(pickup_minutes)}晚于家长窗口{_clock(latest)}"
        return True, reschedule.get("evidence_id", "")
    return False, "缺少家长批准记录"


def recommend(routes, vehicles, drivers=None, scenario=None, traffic_version=None):
    """Choose an executable operating plan under the school's two-route policy.

    Plan A (two vehicles, original windows) is always the legal fallback.
    Plan B (one accessible vehicle chained, the other held as breakdown
    backup) is selected only when the parent's later-window consent exists
    and the deadhead traffic bound still lands inside that window.
    """
    outcome = search_plans(routes, vehicles, drivers, scenario, traffic_version)
    plans = outcome["plans"]
    plan_a = next((p for p in plans if p["kind"] == "A"), None)
    plan_b = next((p for p in plans if p["kind"] == "B"), None)

    policy = []
    selected = None
    if plan_b is not None:
        consent_ok = True
        for route_id in plan_b["chained_routes"]:
            pickup = to_minutes(plan_b["assignments"][route_id]["pickup"])
            ok, evidence = _reschedule_consent(scenario or {}, route_id, pickup)
            policy.append({
                "route_id": route_id, "consent_ok": ok,
                "consent_evidence": evidence,
            })
            consent_ok = consent_ok and ok
        if consent_ok:
            selected = plan_b
            reason = "路线B:家长已同意较晚窗口且空驶上界可兑现,BUS-B保留为故障备车"
        else:
            selected = plan_a
            reason = "空驶/改时条件不满足,回退路线A"
    else:
        reason = "空驶上界超出家长窗口,链式计划不可执行,回退路线A双车按原窗口接送"
        selected = plan_a
    outcome["selected"] = selected
    outcome["policy"] = {"reason": reason, "consent_checks": policy}
    if selected is None:
        outcome["conflict_set"] = minimum_conflict_set(
            routes, vehicles, drivers, scenario, traffic_version)
    else:
        outcome["conflict_set"] = []
    return outcome


def minimum_conflict_set(routes, vehicles, drivers=None, scenario=None, traffic_version=None):
    """Smallest set of routes that cannot be jointly fulfilled.

    A route belongs to the minimum unservable set only when removing it makes
    the remaining routes jointly feasible; routes that can still be served are
    never reported as conflicts.  If no single-route removal restores
    feasibility, the remaining routes form one irreducible conflict group.
    """
    scenario = scenario or {}
    full = search_plans(routes, vehicles, drivers, scenario, traffic_version)
    if full["plans"]:
        return []

    # Routes with a feasible edge on their own are still serviceable and can
    # never belong to the conflict set.  Keep only routes with no standalone
    # edge, then shrink the remaining group to its minimal unservable core.
    standalone = set()
    for route in routes:
        single = search_plans(
            [route], vehicles, drivers, scenario, traffic_version
        )["plans"]
        if single:
            standalone.add(route["id"])
    unservable = [r for r in routes if r["id"] not in standalone]

    blocked = []
    remaining = [r["id"] for r in unservable]
    while remaining:
        lifted = None
        for route_id in list(remaining):
            trial = [r for r in unservable if r["id"] in remaining and r["id"] != route_id]
            if not trial or search_plans(
                trial, vehicles, drivers, scenario, traffic_version
            )["plans"]:
                lifted = route_id
                break
        if lifted is None:
            blocked.extend(remaining)
            break
        blocked.append(lifted)
        remaining.remove(lifted)

    reasons = {}
    for route_id in blocked:
        route_rejections = [r for r in full["rejected"] if r["route_id"] == route_id]
        if route_rejections:
            reasons[route_id] = sorted({r["reason"] for r in route_rejections})
        else:
            reasons[route_id] = ["可行边存在,但与其他线路在车辆/司机/窗口上全局冲突"]
    return [{"route_id": rid, "reasons": reasons[rid]} for rid in blocked]


def assign(routes, vehicles, scenario=None, drivers=None, traffic_version=None):
    """Executable assignment for every route (``None`` when unfulfillable).

    Unlike the old first-available greedy loop this runs the joint search and
    returns the selected policy plan, so an accessible route can never be
    nominally assigned to a vehicle it cannot board.
    """
    outcome = recommend(routes, vehicles, drivers, scenario, traffic_version)
    selected = outcome["selected"]
    if selected is None:
        return {r["id"]: None for r in routes}
    return {rid: item["vehicle_id"] for rid, item in selected["assignments"].items()}
