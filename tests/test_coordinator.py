from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from student_agent.agents import PAYMENT_AGENT, SHIPMENT_AGENT, verify_output
from student_agent.contracts import WORKFLOW_REQUIRED_EVENTS, Contracts
from student_agent.coordinator import (
    FAMILY_DELIVERY,
    FAMILY_PAYMENT,
    FAMILY_UNKNOWN,
    analyze_intent,
)
from student_agent.state import CaseState
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = Contracts(ROOT / "contracts" / "schemas")
ORDER_ID = "e2a03ccf5ea816036608b2d8c3ab8e60"

TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_order_payments": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}


def make_case(topic: str) -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_001",
        "customer_request": {
            "claimed_order_id": ORDER_ID,
            "claims": [
                {"claim_id": "claim-001-a", "topic": topic},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


class FakeGateway:
    def __init__(self, data: dict[str, Any], failing: set[str] | None = None) -> None:
        self.data = data
        self.failing = failing or set()
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name in self.failing or tool_name not in self.data:
            raise RuntimeError(f"MCP tool {tool_name} failed")
        payload = self.data[tool_name]
        evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{secrets.token_urlsafe(18)}",
            "result_hash": "sha256:" + hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
            "domain": TOOL_DOMAINS[tool_name],
            "data": payload,
        }
        CONTRACTS.validate_evidence(evidence)
        return evidence


def _rule(status: str, action: str, refund: float, party: str) -> dict[str, Any]:
    return {
        "case_status": status,
        "recommended_action": action,
        "refund_brl": refund,
        "responsible_parties": [{"party_type": party, "party_id": None}],
    }


def _captured(amount: str, event_at: str = "2018-01-02T10:00:00-03:00") -> dict[str, str]:
    return {"event_at": event_at, "event_type": "captured", "amount_brl": amount}


def fake_data(order_status: str = "canceled") -> dict[str, Any]:
    return {
        "get_order": {
            "order_id": ORDER_ID,
            "order_status": order_status,
            "order_purchase_timestamp": "2018-01-02T09:00:00-03:00",
            "order_approved_at": "2018-01-02T10:00:00-03:00",
            "order_delivered_customer_date": None,
            "order_estimated_delivery_date": "2018-01-12T09:00:00-03:00",
        },
        "get_order_items": [
            {"order_item_id": "item-1", "seller_id": "seller-1", "price": "79.00",
             "freight_value": "10.00"}
        ],
        "get_payment_timeline": {
            "payments": [{"payment_value": "79.00"}],
            # The second capture lies months outside the order lifecycle: a distractor.
            "events": [_captured("79.00"), _captured("18.00", "2018-06-01T10:00:00-03:00")],
        },
        "get_shipment_summary": {"order_id": ORDER_ID, "events": []},
        "get_sellers": [{"seller_id": "seller-1"}],
        "get_policy": {
            "policy_version": "EC_POLICY_V1",
            "rules": {
                "canceled_order_paid": _rule("action_required", "issue_refund", 79.0, "platform"),
                "payment_mismatch": _rule(
                    "action_required", "reconcile_payment", 35.0, "payment_provider"
                ),
                "valid_split_payment": _rule("no_action", "document_no_action", 0.0, "customer"),
            },
        },
    }


def run_case(case: dict[str, Any], gateway: FakeGateway, tmp_path: Path) -> tuple[dict, list[dict]]:
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, CONTRACTS)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    return output, events


def test_analyze_intent_routes_by_claimed_topic() -> None:
    payment = analyze_intent(make_case("duplicate_charge"))
    assert payment.intent_family == FAMILY_PAYMENT
    assert payment.specialists[0] == PAYMENT_AGENT
    assert payment.refund_requested and payment.order_id == ORDER_ID

    delivery = analyze_intent(make_case("late_delivery_seller"))
    assert delivery.intent_family == FAMILY_DELIVERY
    assert delivery.specialists[0] == SHIPMENT_AGENT
    assert delivery.claim_ids == ["claim-001-a", "claim-001-b"]

    assert analyze_intent(make_case("unsupported_claim")).intent_family == FAMILY_UNKNOWN


def test_workflow_emits_required_events_and_valid_output(tmp_path: Path) -> None:
    gateway = FakeGateway(fake_data())
    output, events = run_case(make_case("late_delivery_seller"), gateway, tmp_path)

    CONTRACTS.validate_output(output, "output")
    types = [event["event_type"] for event in events]
    assert set(WORKFLOW_REQUIRED_EVENTS) <= set(types)
    assert types[0] == "case_received" and types[-1] == "case_finalized"
    assert types.index("policy_decided") < types.index("verification_completed")
    assert all(call[1]["case_id"] == "L3A_CASE_001" for call in gateway.calls)

    # Canceled order: routing is refined so payments are audited before shipment.
    assigned = [e["target"] for e in events if e["event_type"] == "task_assigned"]
    assert assigned[:3] == ["order-item-agent", PAYMENT_AGENT, SHIPMENT_AGENT]

    # Every cited ref was consumed in the trace by the agent that fetched it.
    consumed = {
        ref
        for e in events
        if e["event_type"] == "tool_result_consumed"
        for ref in e["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed


def test_policy_refunds_paid_canceled_order(tmp_path: Path) -> None:
    output, _ = run_case(make_case("canceled_order_paid"), FakeGateway(fake_data()), tmp_path)

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 79.0
    assert output["financial_resolution"]["refund_lines"][0]["amount_brl"] == 79.0
    assert output["resolution_actions"] == ["issue_refund"]


def test_policy_detects_payment_mismatch(tmp_path: Path) -> None:
    data = fake_data(order_status="delivered")
    data["get_payment_timeline"]["events"].append(
        {"event_at": "2018-01-02T10:00:00-03:00", "event_type": "reconciliation_mismatch",
         "amount_brl": "35.00", "status": "open"}
    )

    output, _ = run_case(make_case("payment_mismatch"), FakeGateway(data), tmp_path)

    assert output["assessment"]["primary_issue"] == "payment_mismatch"
    assert output["financial_resolution"]["recommended_refund_brl"] == 35.0
    assert output["resolution_actions"] == ["reconcile_payment"]


def test_policy_detects_valid_split_payment(tmp_path: Path) -> None:
    data = fake_data(order_status="delivered")
    data["get_payment_timeline"]["events"] = [_captured("44.50"), _captured("44.50")]

    output, _ = run_case(make_case("valid_split_payment"), FakeGateway(data), tmp_path)

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"


def test_verifier_handles_malformed_numeric_fields() -> None:
    state = CaseState(case_id="L3A_CASE_001")
    draft = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": state.case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": "bad",
        },
        "affected_entities": {
            "order_ids": [], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "claim_assessments": [],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "BAD_DATA", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": "bad", "refund_lines": [],
        },
        "resolution_actions": [],
    }

    output, failed = verify_output(draft, state)

    assert output["assessment"]["confidence"] == 0.3
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert "CONFIDENCE_INVALID" in failed


def test_missing_order_skips_specialists_and_still_returns_output(tmp_path: Path) -> None:
    gateway = FakeGateway(fake_data(), failing={"get_order"})
    output, events = run_case(make_case("duplicate_charge"), gateway, tmp_path)

    CONTRACTS.validate_output(output, "output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"] == []
    called = {name for name, _ in gateway.calls}
    assert "get_payment_timeline" not in called and "get_shipment_summary" not in called
    handoffs = [e["decision_code"] for e in events if e["event_type"] == "handoff"]
    assert "ORDER_NOT_FOUND" in handoffs


def test_crashing_specialist_is_isolated(tmp_path: Path) -> None:
    class BrokenGateway(FakeGateway):
        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
            if tool_name == "get_payment_timeline":
                raise ValueError("malformed envelope")
            return await super().call(tool_name, case_id=case_id, **arguments)

    output, events = run_case(make_case("duplicate_charge"), BrokenGateway(fake_data()), tmp_path)
    CONTRACTS.validate_output(output, "output")
    assert any(e["event_type"] == "verification_completed" for e in events)
