from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from student_agent.agents import PAYMENT_AGENT, SHIPMENT_AGENT
from student_agent.contracts import WORKFLOW_REQUIRED_EVENTS, Contracts
from student_agent.coordinator import (
    FAMILY_DELIVERY,
    FAMILY_PAYMENT,
    FAMILY_UNKNOWN,
    analyze_intent,
)
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


def fake_data(order_status: str = "canceled") -> dict[str, Any]:
    return {
        "get_order": {"order_id": ORDER_ID, "order_status": order_status},
        "get_order_items": [{"order_item_id": "item-1", "seller_id": "seller-1", "price": "79.00"}],
        "get_payment_timeline": {"payments": [{"payment_value": "79.00"}], "events": []},
        "get_shipment_summary": {"order_id": ORDER_ID, "events": []},
        "get_sellers": [{"seller_id": "seller-1"}],
        "get_policy": {"policy_version": "EC_POLICY_V1", "rules": {}},
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
