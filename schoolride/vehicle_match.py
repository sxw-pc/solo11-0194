"""Feasible edges between routes and vehicles.

An edge is feasible only when the vehicle's *qualification* covers every
student on the route (a working wheelchair lift, tie-down points and a
certified driver for wheelchair students) *and* its capacity and the
route's pickup window are honoured.  Spare ordinary seats can never offset
a missing accessibility qualification.

The roster/route entitlement rules in :mod:`schoolride.rules` are treated as
a mandatory input: an edge that fails the entitlement gate is rejected even
when headcount and seats alone would fit.
"""

SERVICE_END = "2026-11-24T12:00"


def to_minutes(clock):
    """Convert ``HH:MM`` (or an ``HH:MM:SS`` stamp) to minutes since midnight."""
    if isinstance(clock, (int, float)):
        return int(clock)
    parts = str(clock).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"unparseable clock value: {clock!r}")
    return int(parts[0]) * 60 + int(parts[1])


def headcount(route):
    students = route.get("students")
    if isinstance(students, int):
        return students
    return len(students or [])


def wheelchair_students(route):
    students = route.get("students")
    if isinstance(students, int):
        return 1 if route.get("wheelchair") else 0
    if not students:
        return 1 if route.get("wheelchair") else 0
    return sum(1 for s in students if isinstance(s, dict) and s.get("wheelchair"))


def _vehicle_qualified(route, vehicle):
    """Equipment qualification, independent of seat count."""
    if wheelchair_students(route) == 0:
        return True, ""
    if not vehicle.get("wheelchair_lift"):
        return False, "车辆无轮椅升降装置"
    if vehicle.get("lift_status") not in (None, "working"):
        return False, f"升降装置状态不可用:{vehicle.get('lift_status')}"
    if not vehicle.get("tie_down"):
        return False, "车辆无轮椅固定点"
    return True, ""


def _driver_qualified(route, driver, service_end=SERVICE_END):
    if wheelchair_students(route) == 0:
        return True, ""
    if not driver:
        return False, "未指派司机"
    if not driver.get("wheelchair_certified"):
        return False, "司机无轮椅接送资质"
    cert_until = driver.get("cert_until")
    if cert_until and str(cert_until) < service_end:
        return False, "司机资质未覆盖服务时段"
    return True, ""


def compatible(route, vehicle, driver=None, service_end=SERVICE_END):
    """Qualification + capacity check for one route/vehicle pair."""
    qualified, reason = _vehicle_qualified(route, vehicle)
    if not qualified:
        return False
    if headcount(route) > vehicle.get("seats", 0):
        return False
    driver_ok, _ = _driver_qualified(route, driver, service_end)
    if driver is not None and not driver_ok:
        return False
    return True


def rejection_reason(route, vehicle, driver=None, service_end=SERVICE_END):
    """Human-readable reason why a pair is incompatible (``''`` if compatible)."""
    qualified, reason = _vehicle_qualified(route, vehicle)
    if not qualified:
        return reason
    if headcount(route) > vehicle.get("seats", 0):
        return f"座位不足:需{headcount(route)}座,额定{vehicle.get('seats', 0)}座"
    driver_ok, driver_reason = _driver_qualified(route, driver, service_end)
    if driver is not None and not driver_ok:
        return driver_reason
    return ""


def entitlement_gate(route, service_date):
    """Re-run the roster/route entitlement rules from ``schoolride.rules``.

    The dispatch problem may only use resources that the existing entitlement
    ledger accepts.  Returns a decision dict with an ``admitted`` list and the
    rule decisions produced by :func:`schoolride.rules.calculate`.
    """
    from .rules import calculate

    students = route.get("students")
    if isinstance(students, int):
        student_rows = [
            {"id": f"{route['id']}-S{i + 1}", "wheelchair": False, "stop": stop,
             "enrolled_until": "2099-12-31", "guardian": f"P{i + 1}",
             "route_authorization": {"signed": True, "valid_until": "2099-12-31"}}
            for i, stop in enumerate(route.get("stops", ["STOP"])[: max(students, 1)])
        ]
    else:
        student_rows = [
            {
                "id": s["id"],
                "enrolled_until": "2099-12-31",
                "stop": s.get("stop", "STOP"),
                "guardian": s.get("guardian", f"GUARD-{s['id']}"),
                "route_authorization": {
                    "signed": bool(s.get("authorization_signed", True)),
                    "valid_until": "2099-12-31",
                },
                "revoked": bool(s.get("revoked", False)),
            }
            for s in (students or [])
        ]
    run_id = f"DISPATCH-{route['id']}"
    roster_event = {
        "id": f"ER-{route['id']}", "kind": "roster", "run_id": run_id,
        "version": 1, "effective_at": f"{service_date}T00:00",
        "service_date": service_date, "students": student_rows,
    }
    route_event = {
        "id": f"EV-{route['id']}", "kind": "route", "run_id": run_id,
        "version": 1, "effective_at": f"{service_date}T00:00",
        "service_start": f"{service_date}T00:00",
        "service_end": f"{service_date}T23:59",
        "inspection_until": "2099-12-31T00:00",
        "driver_cert_until": "2099-12-31T00:00",
        "seats": headcount(route), "stops": route.get("stops") or ["STOP"],
        "cancelled": False,
    }
    decisions, _ = calculate([roster_event, route_event])
    seat_decision = decisions[f"{run_id}/座位占用是否超过额定人数"]
    stop_decision = decisions[f"{run_id}/线路绕行是否触及学生停靠站"]
    admit_key = "学籍时段是否覆盖乘车日"
    admitted = [
        s["id"] for s in student_rows
        if decisions.get(f"{run_id}/{s['id']}/{admit_key}", {}).get("result") == "有效"
    ]
    return {
        "passed": seat_decision["result"] == "充足"
        and stop_decision["result"] == "完整"
        and len(admitted) == len(student_rows),
        "admitted": admitted,
        "decisions": decisions,
        "evidence": [roster_event["id"], route_event["id"]],
    }


def latest_traffic(segment, traffic_version=None):
    """Pick the effective traffic evidence version for a deadhead segment.

    A specific version may be pinned; otherwise the highest-versioned evidence
    is used (offline late evidence replaces earlier estimates rather than
    being ignored).
    """
    versions = sorted(
        segment["traffic_versions"],
        key=lambda v: (v.get("version", 1), v.get("effective_at", "")),
    )
    if traffic_version is None:
        return versions[-1]
    pinned = next((v for v in versions if v.get("version") == traffic_version), None)
    if pinned is None:
        raise ValueError(f"traffic version {traffic_version} missing on {segment['id']}")
    return pinned


def feasible_edges(routes, vehicles, drivers=None, scenario=None, traffic_version=None):
    """Build every feasible route/vehicle/driver edge.

    Two edge shapes are returned:

    * ``window`` edges serve a route directly inside its approved pickup
      window (a vehicle may run several routes at non-overlapping times);
    * ``chained`` edges serve a route after another route on the same vehicle,
      when a deadhead segment plus its traffic interval still lands inside the
      downstream route's approved window.

    The mandatory ``rules`` entitlement gate is evaluated per route and any
    rejection is recorded on the returned report instead of being hidden.
    """
    scenario = scenario or {}
    drivers = drivers if drivers is not None else scenario.get("drivers", [])
    segments = {s["id"]: s for s in scenario.get("deadhead_segments", [])}
    edges = []
    rejected = []
    for route in routes:
        gate = entitlement_gate(route, scenario.get("service_date", "2026-11-24"))
        if not gate["passed"]:
            rejected.append({
                "route_id": route["id"], "vehicle_id": None, "driver_id": None,
                "reason": "名册/车次权益门禁未通过", "gate": gate,
            })
            continue
        window = route.get("window", {})
        earliest = to_minutes(window.get("earliest", route["start"]))
        latest = to_minutes(window.get("latest", route["start"]))
        start = to_minutes(route["start"])
        if not drivers:
            rejected.append({
                "route_id": route["id"], "vehicle_id": None, "driver_id": None,
                "reason": "未提供任何司机,无法指派",
            })
            continue
        for vehicle in vehicles:
            for driver in drivers:
                reason = rejection_reason(route, vehicle, driver)
                if reason:
                    rejected.append({
                        "route_id": route["id"], "vehicle_id": vehicle["id"],
                        "driver_id": driver["id"] if driver else None, "reason": reason,
                    })
                    continue
                edges.append({
                    "route_id": route["id"], "vehicle_id": vehicle["id"],
                    "driver_id": driver["id"],
                    "shape": "window", "pickup": start,
                    "window": [earliest, latest],
                    "service": [start, to_minutes(route.get("end", route["start"]))],
                })
        for segment in scenario.get("deadhead_segments", []):
            if segment.get("to_route") != route["id"]:
                continue
            traffic = latest_traffic(segment, traffic_version)
            predecessor = next(
                (r for r in routes if r["id"] == segment.get("from_route")), None)
            if predecessor is None:
                continue
            travel = int(traffic["minutes"])
            ready = to_minutes(predecessor.get("end", predecessor["start"])) + travel
            for vehicle in vehicles:
                for driver in drivers:
                    reason = rejection_reason(route, vehicle, driver)
                    if reason:
                        continue
                    edges.append({
                        "route_id": route["id"], "vehicle_id": vehicle["id"],
                        "driver_id": driver["id"],
                        "shape": "chained", "pickup": ready,
                        "window": [earliest, latest],
                        "service": [ready, ready + (to_minutes(route.get("end", route["start"])) - start)],
                        "predecessor": predecessor["id"],
                        "deadhead_segment": segment["id"],
                        "traffic_version": traffic.get("version"),
                        "deadhead_minutes": travel,
                        "within_window": earliest <= ready <= latest,
                    })
    return {"edges": edges, "rejected": rejected, "segments": segments}
