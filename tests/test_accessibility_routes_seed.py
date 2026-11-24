"""Accessibility-aware joint dispatch regression tests.

The original seed documented the defective greedy behaviour (R1 consumes the
only lift-equipped BUS-A, R2 is nominally assigned to BUS-B it cannot board).
These tests pin the repaired behaviour: qualification-aware feasible edges,
the two authorised scheduling routes, deadhead-bound failure with fallback,
minimum conflict sets and explicit revocation of stale dispatch permits.
"""
import copy
from pathlib import Path

import pytest

from schoolride.dispatch_capture import (
    PermitLedger,
    load_scenario,
    plan_evidence,
    reschedule_with_evidence,
)
from schoolride.route_dispatch import (
    assign,
    minimum_conflict_set,
    recommend,
    search_plans,
)
from schoolride.vehicle_match import (
    compatible,
    entitlement_gate,
    feasible_edges,
    rejection_reason,
    to_minutes,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "accessibility-routes.json"


@pytest.fixture()
def scenario():
    return load_scenario(FIXTURE)


def test_wheelchair_route_rejects_inaccessible_bus(scenario):
    """The fixed bug: spare ordinary seats cannot offset missing lift/tie-down."""
    r1, r2 = scenario["routes"]
    bus_a, bus_b = scenario["vehicles"]
    d1 = scenario["drivers"][0]

    assert r2["students"][0]["wheelchair"] is True
    assert bus_b["wheelchair_lift"] is False
    assert compatible(r2, bus_b, d1) is False
    assert "升降装置" in rejection_reason(r2, bus_b, d1)
    assert compatible(r2, bus_a, d1) is True
    # Ordinary route still fits either vehicle on capacity alone.
    assert compatible(r1, bus_b, d1) is True
    assert compatible(r1, bus_a, d1) is True


def test_joint_dispatch_repairs_greedy_assignment(scenario):
    """R2 must never be nominally dispatched to a vehicle it cannot board."""
    outcome = recommend(
        scenario["routes"], scenario["vehicles"], scenario["drivers"], scenario, 1
    )
    selected = outcome["selected"]
    assert selected is not None
    r2_vehicle = selected["assignments"]["R2"]["vehicle_id"]
    assert r2_vehicle == "BUS-A"
    assert assign(scenario["routes"], scenario["vehicles"], scenario,
                  traffic_version=1) == {
        "R1": "BUS-A",  # plan B chains BUS-A: R1 first, then R2
        "R2": "BUS-A",
    }
    rejected = {(r["route_id"], r["vehicle_id"]) for r in outcome["rejected"]}
    assert ("R2", "BUS-B") in rejected


def test_two_authorised_scheduling_routes_are_both_kept(scenario):
    """Plan A (two buses, original windows) and Plan B (chained + backup)."""
    evidence = plan_evidence(scenario, traffic_version=1)
    kinds = {row["plan"] for row in evidence["plans"]}
    assert kinds == {"A", "B"}

    plan_a = next(row for row in evidence["plans"] if row["plan"] == "A")
    plan_b = next(row for row in evidence["plans"] if row["plan"] == "B")

    # Route A: BUS-A serves wheelchair R2, BUS-B serves R1, original windows.
    assert plan_a["vehicle_occupancy"] == ["BUS-A", "BUS-B"]
    assert plan_a["assignments"]["R2"]["vehicle_id"] == "BUS-A"
    assert plan_a["assignments"]["R1"]["vehicle_id"] == "BUS-B"
    assert plan_a["pickups"] == {"R1": "07:30", "R2": "07:35"}
    assert plan_a["backup_vehicles"] == []

    # Route B: BUS-A runs R1 then R2; R2 boards 20 minutes late; BUS-B backup.
    assert plan_b["vehicle_occupancy"] == ["BUS-A"]
    assert plan_b["backup_vehicles"] == ["BUS-B"]
    assert plan_b["pickups"] == {"R1": "07:30", "R2": "07:55"}
    chained = plan_b["assignments"]["R2"]
    assert chained["deadhead_minutes"] == 10
    assert chained["traffic_version"] == 1
    assert evidence["selected"] == "B"


def test_vehicle_and_driver_intervals_never_overlap(scenario):
    for version in (1, 2):
        outcome = search_plans(
            scenario["routes"], scenario["vehicles"],
            scenario["drivers"], scenario, version,
        )
        for plan in outcome["plans"]:
            vehicle_intervals = {}
            driver_intervals = {}
            for item in plan["assignments"].values():
                window = [to_minutes(item["service"][0]), to_minutes(item["service"][1])]
                vehicle_intervals.setdefault(item["vehicle_id"], []).append(window)
                if item["driver_id"]:
                    driver_intervals.setdefault(item["driver_id"], []).append(window)
            for intervals in (vehicle_intervals, driver_intervals):
                for busy in intervals.values():
                    busy.sort()
                    for earlier, later in zip(busy, busy[1:]):
                        assert earlier[1] <= later[0]


def test_deadhead_bound_breach_falls_back_to_route_a(scenario):
    """v2 traffic (25 min deadhead) misses the 07:55 parent window."""
    v1 = plan_evidence(scenario, traffic_version=1)
    assert v1["selected"] == "B"

    v2 = plan_evidence(scenario, traffic_version=2)
    assert [row["plan"] for row in v2["plans"]] == ["A"]
    assert v2["selected"] == "A"
    assert "回退路线A" in v2["policy"]["reason"]
    plan_a = v2["plans"][0]
    assert plan_a["pickups"] == {"R1": "07:30", "R2": "07:35"}

    outcome = recommend(
        scenario["routes"], scenario["vehicles"], scenario["drivers"], scenario, 2
    )
    assert outcome["selected"]["kind"] == "A"
    # No chained edge can satisfy the window under the late evidence.
    chained = [e for e in outcome["edges"] if e["shape"] == "chained"]
    assert all(edge["within_window"] is False for edge in chained)


def test_reschedule_without_consent_cannot_disappear_conflict(scenario):
    """Without parental consent Plan B stays on record but is never selected."""
    no_consent = copy.deepcopy(scenario)
    no_consent["parent_approvals"][0]["reschedule"]["consented"] = False

    outcome = recommend(
        no_consent["routes"], no_consent["vehicles"],
        no_consent["drivers"], no_consent, 1,
    )
    assert outcome["selected"]["kind"] == "A"
    assert all(check["consent_ok"] is False for check in outcome["policy"]["consent_checks"])
    # The chained plan still exists as evidence; it simply is not authorised.
    assert {p["kind"] for p in outcome["plans"]} == {"A", "B"}

    missing = copy.deepcopy(scenario)
    missing["parent_approvals"] = []
    outcome_missing = recommend(
        missing["routes"], missing["vehicles"], missing["drivers"], missing, 1
    )
    assert outcome_missing["selected"]["kind"] == "A"


def test_r1_fixed_commitment_is_never_rescheduled(scenario):
    for version in (1, 2):
        evidence = plan_evidence(scenario, version)
        for row in evidence["plans"]:
            assert row["assignments"]["R1"]["pickup"] == "07:30"


def test_stale_permit_is_revoked_not_silently_reused(scenario):
    ledger = PermitLedger(scenario)
    first = recommend(
        scenario["routes"], scenario["vehicles"], scenario["drivers"], scenario, 1
    )
    assert first["selected"]["kind"] == "B"
    ledger.issue_for_plan(first["selected"], traffic_version=1)
    old_r2 = ledger.active_permit("R2")
    assert old_r2["pickup"] == "07:55"
    assert old_r2["based_on_traffic_version"] == 1

    result = reschedule_with_evidence(scenario, traffic_version=2)
    # A fresh ledger inside the helper; emulate the same ledger receiving v2:
    second = recommend(
        scenario["routes"], scenario["vehicles"], scenario["drivers"], scenario, 2
    )
    ledger.issue_for_plan(second["selected"], traffic_version=2)

    assert ledger.active_permit("R2")["pickup"] == "07:35"
    revoked = [r for r in ledger.snapshot()["revocations"] if r["route_id"] == "R2"][0]
    assert revoked["permit_id"] == old_r2["id"]
    assert "禁止静默沿用" in revoked["reason"]
    assert revoked["superseded_by"] == ledger.active_permit("R2")["id"]
    assert all(p["status"] != "active" or p["based_on_traffic_version"] == 2
               for p in ledger.snapshot()["permits"])

    # The end-to-end helper likewise reports the fallback and fresh notices.
    assert result["selected"]["kind"] == "A"
    assert {n["route_id"]: n["actual_pickup"] for n in result["notices"]} == {
        "R1": "07:30", "R2": "07:35",
    }


def test_modified_notice_cites_parental_approval(scenario):
    result = reschedule_with_evidence(scenario, traffic_version=1)
    r2_notice = next(n for n in result["notices"] if n["route_id"] == "R2")
    assert r2_notice["changed"] is True
    assert r2_notice["delay_minutes"] == 20
    assert r2_notice["consent_evidence"] == "PARENT-W1-LATE"
    assert r2_notice["latest_approved_pickup"] == "07:55"
    r1_notice = next(n for n in result["notices"] if n["route_id"] == "R1")
    assert r1_notice["changed"] is False
    assert r1_notice["actual_pickup"] == "07:30"


def test_imported_fixture_permit_is_revoked_on_late_evidence(scenario):
    """The permit shipped in the fixture cannot silently survive v2 traffic."""
    old_record = scenario["dispatch_permits"][0]
    assert old_record["status"] == "active"

    ledger = PermitLedger(scenario)
    ledger.import_permit(old_record)
    assert ledger.active_permit("R2")["id"] == old_record["id"]

    second = recommend(
        scenario["routes"], scenario["vehicles"], scenario["drivers"], scenario, 2
    )
    ledger.issue_for_plan(second["selected"], traffic_version=2)

    assert ledger.active_permit("R2")["id"] != old_record["id"]
    revocation = next(
        r for r in ledger.snapshot()["revocations"] if r["permit_id"] == old_record["id"]
    )
    assert "禁止静默沿用" in revocation["reason"]
    assert revocation["superseded_by"] == ledger.active_permit("R2")["id"]


def test_minimum_conflict_set_when_lift_is_out_of_service(scenario):
    broken = copy.deepcopy(scenario)
    broken["vehicles"][0]["lift_status"] = "fault"

    outcome = recommend(
        broken["routes"], broken["vehicles"], broken["drivers"], broken, 1
    )
    assert outcome["selected"] is None
    conflict = outcome["conflict_set"]
    assert [item["route_id"] for item in conflict] == ["R2"]
    assert any("升降装置" in reason for reason in conflict[0]["reasons"])
    assert assign(broken["routes"], broken["vehicles"], broken) == {
        "R1": None, "R2": None,
    }
    # R1 remains independently feasible and is therefore not a conflict.
    assert minimum_conflict_set(
        broken["routes"][:1], broken["vehicles"], broken["drivers"], broken, 1
    ) == []


def test_entitlement_rules_are_mandatory_input(scenario):
    healthy = entitlement_gate(scenario["routes"][1], scenario["service_date"])
    assert healthy["passed"] is True

    revoked = copy.deepcopy(scenario["routes"][1])
    revoked["students"][0]["revoked"] = True
    gate = entitlement_gate(revoked, scenario["service_date"])
    assert gate["passed"] is False
    assert gate["admitted"] == []

    report = feasible_edges(
        [revoked], scenario["vehicles"], scenario["drivers"], scenario, 1
    )
    assert report["edges"] == []
    assert report["rejected"][0]["reason"] == "名册/车次权益门禁未通过"


def test_single_driver_still_chains_but_cannot_cover_parallel_plan(scenario):
    one_driver = copy.deepcopy(scenario)
    one_driver["drivers"] = scenario["drivers"][:1]
    outcome = search_plans(
        one_driver["routes"], one_driver["vehicles"],
        one_driver["drivers"], one_driver, 1,
    )
    assert {plan["kind"] for plan in outcome["plans"]} == {"B"}
    plan = outcome["plans"][0]
    drivers = {item["driver_id"] for item in plan["assignments"].values()}
    assert drivers == {"D1"}


def test_unqualified_driver_cannot_serve_wheelchair_route(scenario):
    uncertified = copy.deepcopy(scenario)
    for driver in uncertified["drivers"]:
        driver["wheelchair_certified"] = False
    r2 = scenario["routes"][1]
    for driver in uncertified["drivers"]:
        assert compatible(r2, scenario["vehicles"][0], driver) is False
        assert rejection_reason(r2, scenario["vehicles"][0], driver) == "司机无轮椅接送资质"

    outcome = recommend(
        uncertified["routes"], uncertified["vehicles"],
        uncertified["drivers"], uncertified, 1,
    )
    assert outcome["selected"] is None
    assert [item["route_id"] for item in outcome["conflict_set"]] == ["R2"]
