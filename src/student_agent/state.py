from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from .contracts import (
    PATTERN_CASE_ID,
    PATTERN_EVIDENCE_REF,
)

# --- A2A Message Contract TypedDicts ---


class AgentInputDict(TypedDict):
    task: str
    case_id: str
    context: dict[str, Any]


class AgentOutputDict(TypedDict):
    evidence_refs: list[str]
    evidence_ref: str | None
    findings: dict[str, Any]
    confidence: float


# --- State Schema TypedDict ---


class StateDict(TypedDict):
    case_id: str
    evidence_pool: dict[str, dict[str, Any]]
    decision: dict[str, Any]
    context: dict[str, Any]


# --- A2A Message Contract Classes ---


@dataclass
class AgentInput:
    """Input contract for each specialist agent."""

    task: str
    case_id: str
    context: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.task or not self.task.strip():
            raise ValueError("AgentInput: task must be a non-empty string")
        if not PATTERN_CASE_ID.fullmatch(self.case_id):
            raise ValueError(f"AgentInput: invalid case_id pattern: {self.case_id}")

    def to_dict(self) -> AgentInputDict:
        self.validate()
        return {
            "task": self.task,
            "case_id": self.case_id,
            "context": self.context,
        }


@dataclass
class AgentOutput:
    """Output contract returned by each specialist agent."""

    findings: dict[str, Any]
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_ref and self.evidence_ref not in self.evidence_refs:
            self.evidence_refs.append(self.evidence_ref)
        elif self.evidence_refs and not self.evidence_ref:
            self.evidence_ref = self.evidence_refs[0]

    def validate(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"AgentOutput: confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        for ref in self.evidence_refs:
            if not PATTERN_EVIDENCE_REF.fullmatch(ref):
                raise ValueError(f"AgentOutput: invalid evidence_ref pattern: {ref}")

    def to_dict(self) -> AgentOutputDict:
        self.validate()
        return {
            "evidence_refs": list(self.evidence_refs),
            "evidence_ref": self.evidence_ref,
            "findings": self.findings,
            "confidence": self.confidence,
        }


# --- State Schema Class ---


@dataclass
class CaseState:
    """Central state schema holding case_id, evidence_pool, decision, and context."""

    case_id: str
    evidence_pool: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not PATTERN_CASE_ID.fullmatch(self.case_id):
            raise ValueError(f"CaseState: invalid case_id pattern: {self.case_id}")

    def add_evidence(self, evidence: dict[str, Any]) -> str:
        """Store an authoritative MCP evidence response in the evidence_pool."""
        ref = evidence.get("evidence_ref")
        if not isinstance(ref, str) or not PATTERN_EVIDENCE_REF.fullmatch(ref):
            raise ValueError(f"CaseState: invalid or missing evidence_ref: {ref}")
        self.evidence_pool[ref] = evidence
        return ref

    def get_evidence(self, ref: str) -> dict[str, Any] | None:
        """Retrieve evidence by its evidence_ref."""
        return self.evidence_pool.get(ref)

    def all_evidence_refs(self) -> list[str]:
        """Return unique sorted list of all evidence_refs in the pool."""
        return sorted(self.evidence_pool.keys())

    def update_decision(self, updates: dict[str, Any]) -> None:
        """Update draft or final decision fields."""
        self.decision.update(updates)

    def update_context(self, key: str, value: Any) -> None:
        """Update shared blackboard context for agents."""
        self.context[key] = value

    def to_dict(self) -> StateDict:
        return {
            "case_id": self.case_id,
            "evidence_pool": dict(self.evidence_pool),
            "decision": dict(self.decision),
            "context": dict(self.context),
        }
