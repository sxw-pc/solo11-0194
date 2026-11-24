"""Capture dispatch plans as versioned, auditable permits.

Offline delay updates must never silently keep a dispatch permit alive: once
new traffic evidence changes feasibility, permits built on the old evidence
are explicitly revoked and a new plan (with its own evidence and notices) is
issued.  Both authorised scheduling routes are captured as comparable plan
evidence rather than one being presented as a compliance shortcut.
"""
import json
from pathlib import Path

from .route_dispatch import recommend
from .vehicle_match import latest_traffic, to_minutes


def load_scenario(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class PermitLedger:
    """In-order permit journal with explicit supersession on evidence change."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.permits = []
        self.revocations = []
        self._seq = 0

    def _next_id(self, route_id):
        self._seq += 1
        return f"PERMIT-{route_id}-{self._seq:03d}"

    def active_permit(self, route_id):
        return next(
            (p for p in reversed(self.permits)
             if p["route_id"] == route_id and p["status"] == "active"),
            None,
        )

    def import_permit(self, record):
        """Adopt a pre-existing dispatch permit from offline evidence."""
        permit = {
            "id": record["id"],
            "route_id": record["route_id"],
            "vehicle_id": record["vehicle_id"],
            "driver_id": record.get("driver_id"),
            "pickup": record["pickup"],
            "service_window": record.get("service_window"),
            "shape": record.get("shape", "window"),
            "deadhead_segment": record.get("deadhead_segment"),
            "deadhead_bound_minutes": record.get("deadhead_bound_minutes"),
            "based_on_traffic_version": record.get("based_on_traffic_version"),
            "status": record.get("status", "active"),
            "imported": True,
        }
        self.permits.append(permit)
        return permit

    def _revoke_stale(self, route_id, reason):
        stale = self.active_permit(route_id)
        if stale is None:
            return None
        stale["status"] = "revoked"
        record = {
            "permit_id": stale["id"],
            "route_id": route_id,
            "reason": reason,
            "superseded_by": None,
        }
        self.revocations.append(record)
        return record

    def issue_for_plan(self, plan, traffic_version, notice_evidence=None):
        """Issue one permit per route; revoke any permit whose evidence changed."""
        issued = []
        for route_id, item in plan["assignments"].items():
            segment_id = item.get("deadhead_segment")
            bound = None
            if segment_id:
                segment = next(
                    s for s in self.scenario["deadhead_segments"]
                    if s["id"] == segment_id
                )
                bound = latest_traffic(segment, item.get("traffic_version"))["minutes"]
            current = self.active_permit(route_id)
            stale_reason = None
            if current is not None and (
                current["based_on_traffic_version"] != traffic_version
                or current["vehicle_id"] != item["vehicle_id"]
                or current["driver_id"] != item["driver_id"]
                or current["pickup"] != item["pickup"]
            ):
                stale_reason = (
                    f"派车证基于交通证据v{current['based_on_traffic_version']},"
                    f"当前证据v{traffic_version}或资源占用已变化,禁止静默沿用"
                )
            revocation = self._revoke_stale(route_id, stale_reason) if stale_reason else None
            permit = {
                "id": self._next_id(route_id),
                "route_id": route_id,
                "vehicle_id": item["vehicle_id"],
                "driver_id": item["driver_id"],
                "pickup": item["pickup"],
                "service_window": item["service"],
                "shape": item["shape"],
                "deadhead_segment": segment_id,
                "deadhead_bound_minutes": bound,
                "based_on_traffic_version": traffic_version,
                "status": "active",
                "notice": build_notice(
                    self.scenario, route_id, item, plan, notice_evidence),
            }
            if revocation is not None:
                revocation["superseded_by"] = permit["id"]
                permit["replaces"] = revocation["permit_id"]
            self.permits.append(permit)
            issued.append(permit)
        return issued

    def snapshot(self):
        return {
            "permits": [dict(p) for p in self.permits],
            "revocations": [dict(r) for r in self.revocations],
            "active": {
                route_id: permit["id"]
                for route_id in {p["route_id"] for p in self.permits}
                if (permit := self.active_permit(route_id))
            },
        }


def plan_evidence(scenario, traffic_version=None):
    """Comparable evidence for both authorised routes under one evidence version."""
    routes = scenario["routes"]
    vehicles = scenario["vehicles"]
    drivers = scenario.get("drivers", [])
    outcome = recommend(routes, vehicles, drivers, scenario, traffic_version)
    rows = []
    for plan in outcome["plans"]:
        rows.append({
            "plan": plan["kind"],
            "traffic_version": traffic_version,
            "vehicle_occupancy": plan["vehicle_occupancy"],
            "driver_occupancy": plan["driver_occupancy"],
            "backup_vehicles": plan["backup_vehicles"],
            "pickups": {rid: item["pickup"] for rid, item in plan["assignments"].items()},
            "assignments": plan["assignments"],
            "chained_routes": plan["chained_routes"],
            "feasible": True,
        })
    return {
        "plans": rows,
        "selected": outcome["selected"]["kind"] if outcome["selected"] else None,
        "policy": outcome["policy"],
        "conflict_set": outcome["conflict_set"],
        "rejected_edges": outcome["rejected"],
    }


def build_notice(scenario, route_id, item, plan, consent_evidence=None):
    """Modified pickup notice; reschedule notices cite the parental approval."""
    route = next(r for r in scenario["routes"] if r["id"] == route_id)
    scheduled = route["start"]
    notice = {
        "route_id": route_id,
        "scheduled_pickup": scheduled,
        "actual_pickup": item["pickup"],
        "vehicle_id": item["vehicle_id"],
        "driver_id": item["driver_id"],
        "changed": item["pickup"] != scheduled,
    }
    if notice["changed"]:
        approval = next(
            (a for a in scenario.get("parent_approvals", [])
             if a.get("route_id") == route_id),
            None,
        )
        reschedule = (approval or {}).get("reschedule") or {}
        delay = to_minutes(item["pickup"]) - to_minutes(scheduled)
        notice.update({
            "kind": "reschedule",
            "delay_minutes": delay,
            "consent_evidence": consent_evidence or reschedule.get("evidence_id"),
            "latest_approved_pickup": reschedule.get("latest_pickup"),
            "guardian": (approval or {}).get("guardian"),
            "message": (
                f"{route_id} 因车辆接续改于 {item['pickup']} 上车"
                f"(晚{delay}分钟),已取得家长改时同意"
            ),
        })
    else:
        notice.update({
            "kind": "on_time",
            "message": f"{route_id} 按承诺时刻 {scheduled} 由{item['vehicle_id']}接送",
        })
    if plan["backup_vehicles"]:
        notice["backup_vehicle"] = plan["backup_vehicles"][0]
    return notice


def reschedule_with_evidence(scenario, traffic_version):
    """Re-plan from a (possibly late) traffic evidence version end to end.

    Returns the selected plan, fresh permits, explicit revocations of permits
    based on superseded evidence, and the notices families receive.  Raising
    the traffic version never leaves an old permit active by default.
    """
    ledger = PermitLedger(scenario)
    outcome = recommend(
        scenario["routes"], scenario["vehicles"],
        scenario.get("drivers", []), scenario, traffic_version)
    if outcome["selected"] is None:
        return {
            "traffic_version": traffic_version,
            "selected": None,
            "ledger": ledger.snapshot(),
            "conflict_set": outcome["conflict_set"],
            "notices": [],
        }
    ledger.issue_for_plan(outcome["selected"], traffic_version)
    notices = [
        p["notice"] for p in ledger.permits if p["status"] == "active"
    ]
    return {
        "traffic_version": traffic_version,
        "selected": outcome["selected"],
        "policy": outcome["policy"],
        "ledger": ledger.snapshot(),
        "notices": notices,
        "comparable_plans": plan_evidence(scenario, traffic_version)["plans"],
    }
