"""Specialist agents invoked by the coordinator.

Each specialist owns a fixed set of MCP tools (least privilege), stores every
authoritative evidence envelope in ``CaseState.evidence_pool`` and emits
``tool_result_consumed`` for each result it actually uses.

The policy agent below is a SAFE PLACEHOLDER: it always drafts an
``insufficient_evidence`` output. Replace ``policy_agent`` with the real decision
rules (phase 4) before submitting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .state import AgentInput, AgentOutput, CaseState
from .trace import TraceWriter

ORDER_AGENT = "order-item-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

# Least-privilege tool ownership. A specialist may only call tools listed here.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    ORDER_AGENT: frozenset({"get_order", "get_order_items"}),
    PAYMENT_AGENT: frozenset({"get_order_payments", "get_payment_timeline", "get_refund_timeline"}),
    SHIPMENT_AGENT: frozenset({"get_shipment_summary", "get_sellers"}),
    POLICY_AGENT: frozenset({"get_policy"}),
    VERIFIER: frozenset(),
}

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = (1.0, 2.0)

# Finding status codes shared by all specialists.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"


@dataclass
class AgentContext:
    """Per-case runtime handles. A new instance is created for every case."""

    case: dict[str, Any]
    state: CaseState
    gateway: EvidenceGateway
    trace: TraceWriter

    @property
    def case_id(self) -> str:
        return self.state.case_id


Specialist = Callable[[AgentInput, AgentContext], Awaitable[AgentOutput]]


class ToolNotPermitted(PermissionError):
    pass


async def call_tool(
    ctx: AgentContext,
    actor: str,
    tool_name: str,
    *,
    attributes: dict[str, str | int | float | bool | None] | None = None,
    **arguments: str,
) -> dict[str, Any] | None:
    """Call one MCP tool on behalf of ``actor`` and record consumption.

    Returns the evidence envelope, or ``None`` when the gateway reports a tool
    error (treated as authoritative "no data"). Transport failures are retried
    with bounded backoff; the same arguments are resent, so retries are idempotent.
    """
    if tool_name not in TOOL_PERMISSIONS.get(actor, frozenset()):
        raise ToolNotPermitted(f"{actor} is not allowed to call {tool_name}")

    for attempt in range(MAX_RETRIES + 1):
        try:
            evidence = await ctx.gateway.call(tool_name, case_id=ctx.case_id, **arguments)
            break
        except RuntimeError:
            # Gateway returned isError: not found / no rows for this scoped entity.
            return None
        except ValueError:
            # Malformed envelope: never fabricate, never retry into a different answer.
            return None
        except Exception:
            if attempt >= MAX_RETRIES:
                raise
            await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])

    ref = ctx.state.add_evidence(evidence)
    meta = ctx.state.context.setdefault("evidence_meta", {})
    meta[ref] = {"tool": tool_name, "actor": actor, "domain": evidence["domain"]}
    ctx.trace.emit_tool_call(
        case_id=ctx.case_id,
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[ref],
        attributes={"domain": evidence["domain"], **(attributes or {})},
    )
    return evidence


def _refs_for(ctx: AgentContext, actor: str) -> list[str]:
    meta = ctx.state.context.get("evidence_meta", {})
    return [ref for ref, info in meta.items() if info["actor"] == actor]


# --- Specialists -----------------------------------------------------------------


async def order_item_agent(task: AgentInput, ctx: AgentContext) -> AgentOutput:
    order_id = task.context["order_id"]
    order = await call_tool(ctx, ORDER_AGENT, "get_order", order_id=order_id)
    if order is None:
        return AgentOutput(
            findings={"status": STATUS_NOT_FOUND, "order_id": order_id}, confidence=0.9
        )

    items = await call_tool(ctx, ORDER_AGENT, "get_order_items", order_id=order_id)
    item_rows = items["data"] if items and isinstance(items["data"], list) else []
    findings = {
        "status": STATUS_OK,
        "order_id": order_id,
        "order": order["data"],
        "order_status": order["data"].get("order_status"),
        "items": item_rows,
        "item_ids": sorted({row["order_item_id"] for row in item_rows if row.get("order_item_id")}),
        "seller_ids": sorted({row["seller_id"] for row in item_rows if row.get("seller_id")}),
        "evidence": {
            "order": order["evidence_ref"],
            "items": items["evidence_ref"] if items else None,
        },
    }
    ctx.state.update_context("order", findings)
    return AgentOutput(findings=findings, confidence=0.9, evidence_refs=_refs_for(ctx, ORDER_AGENT))


async def payment_agent(task: AgentInput, ctx: AgentContext) -> AgentOutput:
    order_id = task.context["order_id"]
    timeline = await call_tool(ctx, PAYMENT_AGENT, "get_payment_timeline", order_id=order_id)
    refunds = None
    if task.context.get("include_refunds"):
        refunds = await call_tool(ctx, PAYMENT_AGENT, "get_refund_timeline", order_id=order_id)

    if timeline is None:
        findings: dict[str, Any] = {"status": STATUS_NOT_FOUND}
    else:
        data = timeline["data"] if isinstance(timeline["data"], dict) else {}
        findings = {
            "status": STATUS_OK,
            "payments": data.get("payments", []),
            "payment_events": data.get("events", []),
            "refund_events": refunds["data"] if refunds else None,
            "evidence": {
                "payment_timeline": timeline["evidence_ref"],
                "refund_timeline": refunds["evidence_ref"] if refunds else None,
            },
        }
    ctx.state.update_context("payment", findings)
    return AgentOutput(
        findings=findings, confidence=0.8, evidence_refs=_refs_for(ctx, PAYMENT_AGENT)
    )


async def shipment_agent(task: AgentInput, ctx: AgentContext) -> AgentOutput:
    order_id = task.context["order_id"]
    shipment = await call_tool(ctx, SHIPMENT_AGENT, "get_shipment_summary", order_id=order_id)
    sellers = await call_tool(ctx, SHIPMENT_AGENT, "get_sellers", order_id=order_id)

    if shipment is None:
        findings: dict[str, Any] = {"status": STATUS_NOT_FOUND}
    else:
        findings = {
            "status": STATUS_OK,
            "shipment": shipment["data"],
            "sellers": sellers["data"] if sellers else [],
            "evidence": {
                "shipment": shipment["evidence_ref"],
                "sellers": sellers["evidence_ref"] if sellers else None,
            },
        }
    ctx.state.update_context("shipment", findings)
    return AgentOutput(
        findings=findings, confidence=0.8, evidence_refs=_refs_for(ctx, SHIPMENT_AGENT)
    )


async def policy_agent(task: AgentInput, ctx: AgentContext) -> AgentOutput:
    """PLACEHOLDER — replace with real decision rules before submitting.

    Loads the policy so the real implementation can use ``ctx.state.context['policy']``,
    then drafts a conservative ``insufficient_evidence`` output that only cites
    evidence actually collected in this case.
    """
    policy = await call_tool(
        ctx, POLICY_AGENT, "get_policy", policy_version=task.context["policy_version"]
    )
    if policy is not None:
        ctx.state.update_context(
            "policy", {"data": policy["data"], "evidence_ref": policy["evidence_ref"]}
        )

    draft = build_safe_output(ctx.state, task.context)
    ctx.state.update_decision(draft)
    ctx.trace.emit_decision(
        case_id=ctx.case_id,
        actor=POLICY_AGENT,
        decision_code=draft["assessment"]["primary_issue"],
        evidence_refs=draft["evidence_refs"][:20] or None,
        attributes={
            "case_status": draft["assessment"]["case_status"],
            "refund_brl": draft["financial_resolution"]["recommended_refund_brl"],
            "placeholder": True,
        },
    )
    return AgentOutput(
        findings={"status": STATUS_OK, "draft": draft},
        confidence=draft["assessment"]["confidence"],
        evidence_refs=list(draft["evidence_refs"]),
    )


# --- Shared output helpers -------------------------------------------------------


def affected_entities_from_state(state: CaseState) -> dict[str, list[str]]:
    """Only IDs returned by authoritative evidence, never IDs claimed by the customer."""
    order = state.context.get("order", {})
    order_ids = [order["order_id"]] if order.get("status") == STATUS_OK else []
    # TODO(policy): payment_references / shipment_ids format is not defined by the
    # public contract; fill them once the expected identifiers are confirmed.
    return {
        "order_ids": order_ids,
        "item_ids": list(order.get("item_ids", [])),
        "seller_ids": list(order.get("seller_ids", [])),
        "payment_references": [],
        "shipment_ids": [],
    }


def build_safe_output(state: CaseState, context: dict[str, Any]) -> dict[str, Any]:
    """Schema-valid conservative output: needs_investigation, no refund, low confidence."""
    order = state.context.get("order", {})
    order_ref = order.get("evidence", {}).get("order") if order.get("status") == STATUS_OK else None
    refs = [order_ref] if order_ref else []
    claims = [
        {
            "claim_id": claim_id,
            "verdict": "insufficient_evidence",
            "confidence": 0.3,
            "evidence_refs": refs,
        }
        for claim_id in context.get("claim_ids", [])
    ][:5]
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": state.case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.3,
        },
        "affected_entities": affected_entities_from_state(state),
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["escalate_manual_review"],
    }


# --- Verifier --------------------------------------------------------------------


def verify_output(draft: dict[str, Any], state: CaseState) -> tuple[dict[str, Any], list[str]]:
    """Enforce cross-field invariants. Returns the corrected output and failed check codes."""
    output = {**draft, "case_id": state.case_id}
    failed: list[str] = []
    pool = set(state.evidence_pool)

    refs = [ref for ref in dict.fromkeys(output.get("evidence_refs", [])) if ref in pool]
    if refs != output.get("evidence_refs", []):
        failed.append("EVIDENCE_NOT_IN_POOL")
    output["evidence_refs"] = refs[:30]

    claims = []
    for claim in output.get("claim_assessments", []):
        claim_refs = [ref for ref in dict.fromkeys(claim.get("evidence_refs", [])) if ref in pool]
        if claim_refs != claim.get("evidence_refs", []):
            failed.append("CLAIM_EVIDENCE_NOT_IN_POOL")
        confidence = min(max(float(claim.get("confidence", 0.0)), 0.0), 1.0)
        claims.append({**claim, "evidence_refs": claim_refs, "confidence": confidence})
    if "claim_assessments" in output:
        output["claim_assessments"] = claims

    assessment = dict(output["assessment"])
    confidence = min(max(float(assessment.get("confidence", 0.0)), 0.0), 1.0)
    if confidence != assessment.get("confidence"):
        failed.append("CONFIDENCE_OUT_OF_BOUNDS")
    assessment["confidence"] = confidence
    output["assessment"] = assessment

    financial = dict(output["financial_resolution"])
    lines = list(financial.get("refund_lines", []))
    if assessment["case_status"] == "no_action" and (
        lines or financial.get("recommended_refund_brl")
    ):
        failed.append("NO_ACTION_WITH_REFUND")
        lines = []
    total = round(sum(float(line["amount_brl"]) for line in lines), 2)
    if abs(total - float(financial.get("recommended_refund_brl", 0.0))) >= 0.01:
        failed.append("REFUND_TOTAL_MISMATCH")
    financial.update({"currency": "BRL", "refund_lines": lines, "recommended_refund_brl": total})
    output["financial_resolution"] = financial

    actions = list(dict.fromkeys(output.get("resolution_actions", [])))
    if actions != output.get("resolution_actions", []):
        failed.append("DUPLICATE_ACTIONS")
    output["resolution_actions"] = actions[:8]

    return output, failed


async def verifier_agent(task: AgentInput, ctx: AgentContext) -> AgentOutput:
    draft = task.context["draft"]
    output, failed = verify_output(draft, ctx.state)
    ctx.state.update_decision(output)
    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor=VERIFIER,
        decision_code="CORRECTED" if failed else "PASS",
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"failed_checks": len(failed), "first_failed": failed[0] if failed else None},
    )
    return AgentOutput(
        findings={"status": STATUS_OK, "output": output, "failed_checks": failed},
        confidence=output["assessment"]["confidence"],
        evidence_refs=list(output["evidence_refs"]),
    )


SPECIALISTS: dict[str, Specialist] = {
    ORDER_AGENT: order_item_agent,
    PAYMENT_AGENT: payment_agent,
    SHIPMENT_AGENT: shipment_agent,
    POLICY_AGENT: policy_agent,
    VERIFIER: verifier_agent,
}
