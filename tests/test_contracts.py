from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from student_agent.contracts import (
    MCP_DOMAINS,
    PRIMARY_ISSUES,
    TRACE_EVENT_TYPES,
    WORKFLOW_REQUIRED_EVENTS,
    ContractError,
    Contracts,
    L3AOutputDict,
    McpEvidenceResponseDict,
    SubmissionManifestDict,
    TraceEventDict,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT / "contracts" / "schemas"


@pytest.fixture
def contracts() -> Contracts:
    return Contracts(SCHEMAS_DIR)


def test_all_schemas_are_valid_draft_2020_12() -> None:
    expected_schemas = [
        "l3a-output-v2.schema.json",
        "l3b-output-v2.schema.json",
        "mcp-evidence-response-v1.schema.json",
        "submission-manifest-v2.schema.json",
        "trace-event-v1.schema.json",
    ]
    for filename in expected_schemas:
        schema_path = SCHEMAS_DIR / filename
        assert schema_path.exists(), f"Missing schema file: {filename}"
        schema_content = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema_content)


# --- 1. L3A Case Output Contract Tests ---


def sample_valid_l3a_output() -> L3AOutputDict:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": "L3A_CASE_001",
        "assessment": {
            "primary_issue": "canceled_order_paid",
            "case_status": "action_required",
            "confidence": 0.95,
        },
        "affected_entities": {
            "order_ids": ["ord_abc123456789012345678901"],
            "item_ids": ["item_01"],
            "seller_ids": ["seller_xyz"],
            "payment_references": ["pay_ref_001"],
            "shipment_ids": ["ship_001"],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1}],
            "responsible_parties": [{"party_type": "platform", "party_id": "platform"}],
        },
        "evidence_refs": ["ev_1234567890abcdef1234567890"],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 150.50,
            "refund_lines": [
                {
                    "reason_code": "FULL_REFUND_CANCELED_ORDER",
                    "amount_brl": 150.50,
                    "entity_id": "ord_abc123456789012345678901",
                }
            ],
        },
        "resolution_actions": ["PROCESS_REFUND", "NOTIFY_CUSTOMER"],
    }


def test_l3a_output_positive_minimal(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    contracts.validate_output(output, "valid minimal output")


def test_l3a_output_positive_with_claim_assessments(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    output["claim_assessments"] = [
        {
            "claim_id": "claim_01",
            "verdict": "supported",
            "confidence": 0.98,
            "evidence_refs": ["ev_1234567890abcdef1234567890"],
        }
    ]
    contracts.validate_output(output, "valid output with claim_assessments")


def test_l3a_output_all_primary_issues(contracts: Contracts) -> None:
    for issue in PRIMARY_ISSUES:
        output = sample_valid_l3a_output()
        output["assessment"]["primary_issue"] = issue
        contracts.validate_output(output, f"issue: {issue}")


def test_l3a_output_negative_missing_required_field(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    del output["assessment"]  # type: ignore[misc]
    with pytest.raises(ContractError, match="assessment"):
        contracts.validate_output(output, "missing assessment")


def test_l3a_output_negative_invalid_schema_version(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    output["schema_version"] = "invalid-schema-v1"  # type: ignore[typeddict-item]
    with pytest.raises(ContractError, match="schema_version"):
        contracts.validate_output(output, "invalid schema version")


def test_l3a_output_negative_invalid_case_id(contracts: Contracts) -> None:
    for invalid_case_id in ["c1", "case_01", "CASE#01", "a" * 65]:
        output = sample_valid_l3a_output()
        output["case_id"] = invalid_case_id
        with pytest.raises(ContractError, match="case_id"):
            contracts.validate_output(output, "invalid case_id")


def test_l3a_output_negative_invalid_primary_issue(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    output["assessment"]["primary_issue"] = "unknown_issue"  # type: ignore[typeddict-item]
    with pytest.raises(ContractError, match="primary_issue"):
        contracts.validate_output(output, "invalid primary_issue")


def test_l3a_output_negative_confidence_bounds(contracts: Contracts) -> None:
    for bad_conf in [-0.01, 1.01]:
        output = sample_valid_l3a_output()
        output["assessment"]["confidence"] = bad_conf
        with pytest.raises(ContractError, match="confidence"):
            contracts.validate_output(output, "invalid confidence")


def test_l3a_output_negative_evidence_refs_format(contracts: Contracts) -> None:
    bad_refs = [
        "not_an_evidence_ref",
        "ev_too_short",
        "ev_" + "a" * 100,  # exceeds 96 chars suffix
    ]
    for bad_ref in bad_refs:
        output = sample_valid_l3a_output()
        output["evidence_refs"] = [bad_ref]
        with pytest.raises(ContractError, match="evidence_refs"):
            contracts.validate_output(output, "invalid evidence_ref")


def test_l3a_output_negative_duplicate_evidence_refs(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    valid_ref = "ev_1234567890abcdef1234567890"
    output["evidence_refs"] = [valid_ref, valid_ref]
    with pytest.raises(ContractError, match="evidence_refs"):
        contracts.validate_output(output, "duplicate evidence_refs")


def test_l3a_output_negative_invalid_rank(contracts: Contracts) -> None:
    for bad_rank in [0, 6]:
        output = sample_valid_l3a_output()
        output["root_cause_analysis"]["ranked_causes"] = [
            {"cause_code": "VALID_CAUSE_CODE", "rank": bad_rank}
        ]
        with pytest.raises(ContractError, match="ranked_causes"):
            contracts.validate_output(output, "invalid rank")


def test_l3a_output_negative_currency(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    output["financial_resolution"]["currency"] = "USD"  # type: ignore[typeddict-item]
    with pytest.raises(ContractError, match="currency"):
        contracts.validate_output(output, "invalid currency")


def test_l3a_output_negative_negative_amount(contracts: Contracts) -> None:
    output = sample_valid_l3a_output()
    output["financial_resolution"]["recommended_refund_brl"] = -10.0
    with pytest.raises(ContractError, match="recommended_refund_brl"):
        contracts.validate_output(output, "negative refund")


def test_l3a_output_negative_additional_properties(contracts: Contracts) -> None:
    output: dict[str, Any] = dict(sample_valid_l3a_output())
    output["unexpected_field"] = "surprise"
    with pytest.raises(ContractError, match="unexpected_field"):
        contracts.validate_output(output, "extra root field")


# --- 2. Trace Event Contract Tests ---


def sample_valid_trace_event(event_type: str = "case_received") -> TraceEventDict:
    return {
        "schema_version": "day09-trace-event-v1",
        "event_id": "evt_1234567890abcdef12345678",
        "case_id": "L3A_CASE_001",
        "event_type": event_type,  # type: ignore[typeddict-item]
        "occurred_at": "2026-09-25T10:00:00Z",
        "actor": "coordinator",
    }


def test_trace_event_positive_all_event_types(contracts: Contracts) -> None:
    for event_type in TRACE_EVENT_TYPES:
        event = sample_valid_trace_event(event_type)
        contracts.validate_trace(event, f"event: {event_type}")


def test_trace_event_positive_all_required_workflow_events(contracts: Contracts) -> None:
    for required_type in WORKFLOW_REQUIRED_EVENTS:
        event = sample_valid_trace_event(required_type)
        contracts.validate_trace(event, f"required event: {required_type}")


def test_trace_event_positive_with_optional_fields(contracts: Contracts) -> None:
    event = sample_valid_trace_event("tool_result_consumed")
    event.update(
        {
            "target": "order_agent",
            "decision_code": "ORDER_CONFIRMED",
            "tool_name": "get_order",
            "evidence_refs": ["ev_1234567890abcdef1234567890"],
            "attributes": {"order_status": "delivered", "retry_count": 0, "is_late": False},
        }
    )
    contracts.validate_trace(event, "trace event with all optional fields")


def test_trace_event_negative_invalid_event_id(contracts: Contracts) -> None:
    event = sample_valid_trace_event()
    event["event_id"] = "not_evt_id"
    with pytest.raises(ContractError, match="event_id"):
        contracts.validate_trace(event, "invalid event_id")


def test_trace_event_negative_invalid_occurred_at(contracts: Contracts) -> None:
    event = sample_valid_trace_event()
    event["occurred_at"] = "not-a-datetime"
    with pytest.raises(ContractError, match="occurred_at"):
        contracts.validate_trace(event, "invalid occurred_at")


def test_trace_event_negative_invalid_event_type(contracts: Contracts) -> None:
    event = sample_valid_trace_event()
    event["event_type"] = "unsupported_event_type"  # type: ignore[typeddict-item]
    with pytest.raises(ContractError, match="event_type"):
        contracts.validate_trace(event, "invalid event_type")


def test_trace_event_negative_additional_properties(contracts: Contracts) -> None:
    event: dict[str, Any] = dict(sample_valid_trace_event())
    event["prompt"] = "chain of thought leaking"
    with pytest.raises(ContractError, match="prompt"):
        contracts.validate_trace(event, "extra field in trace")


# --- 3. Submission Manifest Contract Tests ---


def sample_valid_manifest(variant: str = "l3a") -> SubmissionManifestDict:
    return {
        "schema_version": "day09-submission-manifest-v2",
        "competition_id": "day09-multiagent-mcp-a2a",
        "variant_id": variant,  # type: ignore[typeddict-item]
        "case_set_version": "cases-v1.0",
        "output_schema_version": f"day09-{variant}-output-v2",  # type: ignore[typeddict-item]
        "trace_schema_version": "day09-trace-event-v1",
        "generated_at": "2026-09-25T10:00:00Z",
        "client": {"name": "test-client", "version": "1.0.0"},
    }


def test_manifest_positive_l3a_and_l3b(contracts: Contracts) -> None:
    contracts.validate_manifest(sample_valid_manifest("l3a"))
    contracts.validate_manifest(sample_valid_manifest("l3b"))


def test_manifest_negative_variant_schema_mismatch(contracts: Contracts) -> None:
    bad_manifest = sample_valid_manifest("l3a")
    bad_manifest["output_schema_version"] = "day09-l3b-output-v2"
    with pytest.raises(ContractError):
        contracts.validate_manifest(bad_manifest)


def test_manifest_negative_invalid_competition_id(contracts: Contracts) -> None:
    bad_manifest = sample_valid_manifest("l3a")
    bad_manifest["competition_id"] = "wrong-competition"  # type: ignore[typeddict-item]
    with pytest.raises(ContractError, match="competition_id"):
        contracts.validate_manifest(bad_manifest)


# --- 4. MCP Evidence Response Contract Tests ---


def sample_valid_mcp_response(domain: str = "order") -> McpEvidenceResponseDict:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_1234567890abcdef1234567890",
        "result_hash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "domain": domain,  # type: ignore[typeddict-item]
        "data": {"order_id": "ord_123", "status": "canceled"},
    }


def test_mcp_evidence_positive_all_domains(contracts: Contracts) -> None:
    for domain in MCP_DOMAINS:
        response = sample_valid_mcp_response(domain)
        contracts.validate_evidence(response, f"evidence domain: {domain}")


def test_mcp_evidence_positive_with_warnings(contracts: Contracts) -> None:
    response = sample_valid_mcp_response()
    response["warnings"] = ["Entity has pending update", "Partial match"]
    contracts.validate_evidence(response, "evidence with warnings")


def test_mcp_evidence_negative_invalid_hash(contracts: Contracts) -> None:
    response = sample_valid_mcp_response()
    response["result_hash"] = "md5:abcdef"
    with pytest.raises(ContractError, match="result_hash"):
        contracts.validate_evidence(response, "invalid hash")


def test_mcp_evidence_negative_invalid_domain(contracts: Contracts) -> None:
    response = sample_valid_mcp_response()
    response["domain"] = "unknown_domain"  # type: ignore[typeddict-item]
    with pytest.raises(ContractError, match="domain"):
        contracts.validate_evidence(response, "invalid domain")


def test_mcp_evidence_negative_invalid_ref(contracts: Contracts) -> None:
    response = sample_valid_mcp_response()
    response["evidence_ref"] = "invalid_ref_format"
    with pytest.raises(ContractError, match="evidence_ref"):
        contracts.validate_evidence(response, "invalid ref")
