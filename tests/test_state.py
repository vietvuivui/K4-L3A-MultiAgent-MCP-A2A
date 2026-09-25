from __future__ import annotations

from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.state import AgentInput, AgentOutput, CaseState
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT / "contracts" / "schemas"


def test_agent_input_positive() -> None:
    inp = AgentInput(
        task="investigate_order",
        case_id="L3A_CASE_001",
        context={"order_id": "ord_123"},
    )
    data = inp.to_dict()
    assert data["task"] == "investigate_order"
    assert data["case_id"] == "L3A_CASE_001"
    assert data["context"]["order_id"] == "ord_123"


def test_agent_input_negative_invalid_case_id() -> None:
    inp = AgentInput(task="investigate", case_id="bad_id")
    with pytest.raises(ValueError, match="invalid case_id"):
        inp.validate()


def test_agent_input_negative_empty_task() -> None:
    inp = AgentInput(task="  ", case_id="L3A_CASE_001")
    with pytest.raises(ValueError, match="task must be a non-empty string"):
        inp.validate()


def test_agent_output_positive() -> None:
    out = AgentOutput(
        findings={"status": "delivered", "is_late": False},
        confidence=0.92,
        evidence_refs=["ev_1234567890abcdef1234567890"],
    )
    data = out.to_dict()
    assert data["confidence"] == 0.92
    assert data["evidence_ref"] == "ev_1234567890abcdef1234567890"
    assert len(data["evidence_refs"]) == 1


def test_agent_output_single_evidence_ref() -> None:
    out = AgentOutput(
        findings={"verified": True},
        confidence=0.85,
        evidence_ref="ev_1234567890abcdef1234567890",
    )
    data = out.to_dict()
    assert "ev_1234567890abcdef1234567890" in data["evidence_refs"]


def test_agent_output_negative_confidence() -> None:
    out = AgentOutput(findings={}, confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        out.validate()


def test_agent_output_negative_invalid_evidence_ref() -> None:
    out = AgentOutput(findings={}, confidence=0.8, evidence_refs=["bad_ref"])
    with pytest.raises(ValueError, match="invalid evidence_ref"):
        out.validate()


def test_case_state_lifecycle() -> None:
    state = CaseState(case_id="L3A_CASE_001")
    ref = state.add_evidence(
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_1234567890abcdef1234567890",
            "result_hash": (
                "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "domain": "order",
            "data": {"order_id": "ord_123"},
        }
    )
    assert ref == "ev_1234567890abcdef1234567890"
    assert state.get_evidence(ref) is not None
    assert state.all_evidence_refs() == [ref]

    state.update_decision({"primary_issue": "canceled_order_paid"})
    state.update_context("order_id", "ord_123")

    data = state.to_dict()
    assert data["case_id"] == "L3A_CASE_001"
    assert data["decision"]["primary_issue"] == "canceled_order_paid"
    assert data["context"]["order_id"] == "ord_123"


def test_trace_writer_helpers(tmp_path: Path) -> None:
    contracts = Contracts(SCHEMAS_DIR)
    trace_path = tmp_path / "trace.jsonl"
    writer = TraceWriter(trace_path, contracts)

    # Test emit_handoff
    writer.emit_handoff(
        case_id="L3A_CASE_001",
        actor="coordinator",
        target="order-item-agent",
        decision_code="TASK_DISPATCHED",
    )

    # Test emit_tool_call
    writer.emit_tool_call(
        case_id="L3A_CASE_001",
        actor="order-item-agent",
        tool_name="get_order",
        evidence_refs=["ev_1234567890abcdef1234567890"],
    )

    # Test emit_decision
    writer.emit_decision(
        case_id="L3A_CASE_001",
        actor="policy-agent",
        decision_code="POLICY_REFUND_APPROVED",
        evidence_refs=["ev_1234567890abcdef1234567890"],
    )

    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
