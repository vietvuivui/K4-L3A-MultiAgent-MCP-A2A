from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

VARIANT_ID = "l3a"

# --- Public Contract Enums & Types ---

PrimaryIssue = Literal[
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
]

PRIMARY_ISSUES: tuple[PrimaryIssue, ...] = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
)

CaseStatus = Literal["action_required", "no_action", "needs_investigation"]
CASE_STATUSES: tuple[CaseStatus, ...] = ("action_required", "no_action", "needs_investigation")

ClaimVerdict = Literal["supported", "unsupported", "partially_supported", "insufficient_evidence"]
CLAIM_VERDICTS: tuple[ClaimVerdict, ...] = (
    "supported",
    "unsupported",
    "partially_supported",
    "insufficient_evidence",
)

PartyType = Literal[
    "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"
]
PARTY_TYPES: tuple[PartyType, ...] = (
    "seller",
    "platform",
    "logistics_provider",
    "payment_provider",
    "customer",
    "unknown",
)

TraceEventType = Literal[
    "case_received",
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
    "case_finalized",
]
TRACE_EVENT_TYPES: tuple[TraceEventType, ...] = (
    "case_received",
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
    "case_finalized",
)

WORKFLOW_REQUIRED_EVENTS: tuple[TraceEventType, ...] = (
    "case_received",
    "task_assigned",
    "handoff",
    "verification_completed",
    "case_finalized",
)

McpDomain = Literal[
    "order", "item", "payment", "shipment", "seller", "customer", "product", "refund", "policy"
]
MCP_DOMAINS: tuple[McpDomain, ...] = (
    "order",
    "item",
    "payment",
    "shipment",
    "seller",
    "customer",
    "product",
    "refund",
    "policy",
)

# --- Regex Patterns matching Public Schemas ---
PATTERN_CASE_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")
PATTERN_EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
PATTERN_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_-]{12,96}$")
PATTERN_RESULT_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
PATTERN_CAUSE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
PATTERN_CASE_SET_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


# --- TypedDict Definitions for Contracts ---


class AssessmentDict(TypedDict):
    primary_issue: PrimaryIssue
    case_status: CaseStatus
    confidence: float


class AffectedEntitiesDict(TypedDict):
    order_ids: list[str]
    item_ids: list[str]
    seller_ids: list[str]
    payment_references: list[str]
    shipment_ids: list[str]


class RankedCauseDict(TypedDict):
    cause_code: str
    rank: int


class ResponsiblePartyDict(TypedDict):
    party_type: PartyType
    party_id: str | None


class RootCauseAnalysisDict(TypedDict):
    ranked_causes: list[RankedCauseDict]
    responsible_parties: list[ResponsiblePartyDict]


class ClaimAssessmentDict(TypedDict):
    claim_id: str
    verdict: ClaimVerdict
    confidence: float
    evidence_refs: list[str]


class DataConflictDict(TypedDict):
    field: str
    sources: list[str]
    selected_source: str | None
    resolution_code: str


class RefundLineDict(TypedDict):
    reason_code: str
    amount_brl: float
    entity_id: str | None


class FinancialResolutionDict(TypedDict):
    currency: Literal["BRL"]
    recommended_refund_brl: float
    refund_lines: list[RefundLineDict]


class L3AOutputDict(TypedDict):
    schema_version: Literal["day09-l3a-output-v2"]
    case_id: str
    assessment: AssessmentDict
    affected_entities: AffectedEntitiesDict
    root_cause_analysis: RootCauseAnalysisDict
    evidence_refs: list[str]
    data_conflicts: list[DataConflictDict]
    financial_resolution: FinancialResolutionDict
    resolution_actions: list[str]
    claim_assessments: NotRequired[list[ClaimAssessmentDict]]


class TraceEventDict(TypedDict, total=False):
    schema_version: Literal["day09-trace-event-v1"]
    event_id: str
    case_id: str
    event_type: TraceEventType
    occurred_at: str
    actor: str
    target: str | None
    decision_code: str | None
    tool_name: str | None
    evidence_refs: list[str]
    attributes: dict[str, str | int | float | bool | None]


class ClientInfoDict(TypedDict):
    name: str
    version: str


class SubmissionManifestDict(TypedDict, total=False):
    schema_version: Literal["day09-submission-manifest-v2"]
    competition_id: Literal["day09-multiagent-mcp-a2a"]
    variant_id: Literal["l3a", "l3b"]
    case_set_version: str
    output_schema_version: Literal["day09-l3a-output-v2", "day09-l3b-output-v2"]
    trace_schema_version: Literal["day09-trace-event-v1"]
    generated_at: str
    client: ClientInfoDict


class McpEvidenceResponseDict(TypedDict, total=False):
    schema_version: Literal["day09-mcp-evidence-v1"]
    evidence_ref: str
    result_hash: str
    domain: McpDomain
    data: Any
    warnings: list[str]


# --- Contract Error and Validator ---


class ContractError(ValueError):
    pass


class Contracts:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        for path in sorted(self.root.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name] = schema
            resource = Resource.from_contents(schema)
            registry = registry.with_resource(schema["$id"], resource)
        self._schemas = schemas
        self._registry = registry

    def validate(self, schema_name: str, value: Any, label: str) -> None:
        schema = self._schemas.get(schema_name)
        if schema is None:
            raise ContractError(f"contract not found: {schema_name}")
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ContractError(f"{label}:{location}: {error.message}")

    def validate_output(self, value: Any, label: str) -> None:
        self.validate(f"{VARIANT_ID}-output-v2.schema.json", value, label)

    def validate_trace(self, value: Any, label: str) -> None:
        self.validate("trace-event-v1.schema.json", value, label)

    def validate_manifest(self, value: Any, label: str = "manifest.json") -> None:
        self.validate("submission-manifest-v2.schema.json", value, label)

    def validate_evidence(self, value: Any, label: str = "MCP response") -> None:
        self.validate("mcp-evidence-response-v1.schema.json", value, label)
