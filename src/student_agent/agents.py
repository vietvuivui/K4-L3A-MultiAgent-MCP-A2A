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
            "payment_summary": data,
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
    """Draft a deterministic decision from authoritative specialist findings."""
    policy = await call_tool(
        ctx, POLICY_AGENT, "get_policy", policy_version=task.context["policy_version"]
    )
    if policy is not None:
        ctx.state.update_context(
            "policy", {"data": policy["data"], "evidence_ref": policy["evidence_ref"]}
        )

    draft = build_policy_output(ctx.state, task.context)
    ctx.state.update_decision(draft)
    ctx.trace.emit_decision(
        case_id=ctx.case_id,
        actor=POLICY_AGENT,
        decision_code=draft["assessment"]["primary_issue"],
        evidence_refs=draft["evidence_refs"][:20] or None,
        attributes={
            "case_status": draft["assessment"]["case_status"],
            "refund_brl": draft["financial_resolution"]["recommended_refund_brl"],
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


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows_total(rows: Any, *keys: str) -> float:
    if not isinstance(rows, list):
        return 0.0
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in keys:
            amount = _as_float(row.get(key))
            if amount is not None:
                total += amount
                break
    return round(total, 2)


def _refs_for_domain(state: CaseState, *actors: str) -> list[str]:
    meta = state.context.get("evidence_meta", {})
    return [ref for ref, info in meta.items() if info.get("actor") in actors]


def _claim_assessments(
    context: dict[str, Any],
    verdict: str,
    confidence: float,
    refs: list[str],
    issue: str,
) -> list[dict[str, Any]]:
    assessments = []
    for claim_id, topic in zip(
        context.get("claim_ids", []), context.get("claim_topics", []), strict=True
    ):
        claim_verdict = verdict
        claim_refs = list(refs)
        claim_confidence = confidence
        if topic == issue:
            claim_verdict = "supported"
        elif topic == "requested_full_refund":
            claim_verdict = "supported" if issue in {
                "canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed"
            } else "unsupported"
            claim_confidence = (
                min(confidence, 0.6) if claim_verdict == "unsupported" else confidence
            )
        else:
            claim_verdict = "insufficient_evidence"
            claim_confidence = 0.3
            claim_refs = []
        assessments.append({
            "claim_id": claim_id,
            "verdict": claim_verdict,
            "confidence": claim_confidence,
            "evidence_refs": claim_refs,
        })
    return assessments[:5]


def _decision_output(
    state: CaseState,
    context: dict[str, Any],
    *,
    issue: str,
    status: str,
    confidence: float,
    refs: list[str],
    cause: str,
    parties: list[dict[str, str | None]],
    refund: float = 0.0,
    refund_reason: str | None = None,
    action: str | None = None,
    verdict: str = "supported",
) -> dict[str, Any]:
    unique_refs = list(dict.fromkeys(refs))[:30]
    lines = []
    if refund > 0:
        lines = [{
            "reason_code": refund_reason or issue.upper(),
            "amount_brl": refund,
            "entity_id": context.get("order_id"),
        }]
    actions = [action] if action else (
        ["REJECT_CLAIM"] if verdict == "unsupported" else ["ESCALATE_MANUAL_REVIEW"]
    )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": state.case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": affected_entities_from_state(state),
        "claim_assessments": _claim_assessments(
            context, verdict, confidence, unique_refs, issue
        ),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause, "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": unique_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": lines,
        },
        "resolution_actions": actions,
    }


def build_policy_output(state: CaseState, context: dict[str, Any]) -> dict[str, Any]:
    """Apply deterministic rules and stay conservative when facts are incomplete."""
    order = state.context.get("order", {})
    if order.get("status") != STATUS_OK:
        return build_safe_output(state, context)

    order_status = order.get("order_status")
    order_refs = _refs_for_domain(state, ORDER_AGENT)
    payment = state.context.get("payment", {})
    payment_refs = _refs_for_domain(state, PAYMENT_AGENT)
    shipment = state.context.get("shipment", {})
    shipment_refs = _refs_for_domain(state, SHIPMENT_AGENT)
    payment_rows = payment.get("payments", [])
    payment_summary = payment.get("payment_summary", {})
    captured = _as_float(payment_summary.get("captured_total_brl")) or _rows_total(
        payment_rows, "payment_value", "captured_total_brl", "amount_brl", "value"
    )
    refund_events = payment.get("refund_events")
    refund_statuses = {
        str(row.get("status", "")).lower()
        for row in refund_events or []
        if isinstance(row, dict)
    }

    if order_status == "canceled" and captured > 0:
        return _decision_output(
            state, context, issue="canceled_order_paid", status="action_required", confidence=0.95,
            refs=order_refs + payment_refs, cause="ORDER_CANCELED_AFTER_PAYMENT",
            parties=[{"party_type": "platform", "party_id": None}], refund=captured,
            refund_reason="CANCELED_ORDER_PAYMENT", action="PROCESS_REFUND",
        )
    if order_status == "unavailable" and captured > 0:
        return _decision_output(
            state,
            context,
            issue="unavailable_order_paid",
            status="action_required",
            confidence=0.95,
            refs=order_refs + payment_refs, cause="ORDER_UNAVAILABLE_AFTER_PAYMENT",
            parties=[{"party_type": "platform", "party_id": None}], refund=captured,
            refund_reason="UNAVAILABLE_ORDER_PAYMENT", action="PROCESS_REFUND",
        )
    if "failed" in refund_statuses:
        return _decision_output(
            state, context, issue="refund_failed", status="action_required", confidence=0.9,
            refs=payment_refs, cause="REFUND_FAILED",
            parties=[{"party_type": "platform", "party_id": None}], action="RETRY_REFUND",
        )
    if {"pending", "processing"} & refund_statuses:
        return _decision_output(
            state, context, issue="refund_pending", status="action_required", confidence=0.88,
            refs=payment_refs, cause="REFUND_PENDING",
            parties=[{"party_type": "platform", "party_id": None}], action="MONITOR_REFUND",
        )

    payment_ids = [
        str(row.get("payment_id") or row.get("transaction_id"))
        for row in payment_rows
        if isinstance(row, dict) and (row.get("payment_id") or row.get("transaction_id"))
    ]
    payment_events = payment.get("payment_events", [])
    explicit_duplicate = any(
        isinstance(row, dict)
        and (row.get("duplicate_charge") is True or row.get("is_duplicate") is True)
        for row in [*payment_rows, *payment_events]
    )
    if (payment_ids and len(payment_ids) != len(set(payment_ids))) or explicit_duplicate:
        return _decision_output(
            state, context, issue="duplicate_charge", status="action_required", confidence=0.9,
            refs=payment_refs, cause="DUPLICATE_PAYMENT_REFERENCE",
            parties=[{"party_type": "payment_provider", "party_id": None}],
            action="INVESTIGATE_DUPLICATE_CHARGE",
        )

    order_data = order.get("order", {})
    order_total = next(
        (
            _as_float(order_data.get(key))
            for key in ("order_total_brl", "total_price", "order_total", "total_amount")
            if _as_float(order_data.get(key)) is not None
        ),
        None,
    )
    if order_total is not None and captured > 0 and abs(order_total - captured) >= 0.01:
        return _decision_output(
            state, context, issue="payment_mismatch", status="action_required", confidence=0.9,
            refs=order_refs + payment_refs, cause="ORDER_PAYMENT_TOTAL_MISMATCH",
            parties=[{"party_type": "payment_provider", "party_id": None}],
            action="INVESTIGATE_PAYMENT_MISMATCH",
        )

    installment_values = [
        payment_summary.get(key)
        for key in ("payment_installments", "installments", "installment_count")
    ] + [
        row.get(key)
        for row in payment_rows
        if isinstance(row, dict)
        for key in ("payment_installments", "installments", "installment_count")
    ]
    if any((_as_float(value) or 0) > 1 for value in installment_values):
        return _decision_output(
            state, context, issue="valid_split_payment", status="no_action", confidence=0.9,
            refs=payment_refs, cause="VALID_INSTALLMENT_PAYMENT",
            parties=[{"party_type": "payment_provider", "party_id": None}],
            action="CONFIRM_SPLIT_PAYMENT",
        )

    shipment_data = shipment.get("shipment", {})
    if isinstance(shipment_data, dict):
        late_seller = bool(
            shipment_data.get("late_delivery_seller") or shipment_data.get("late_seller")
        )
        late_logistics = bool(
            shipment_data.get("late_delivery_logistics")
            or shipment_data.get("late_logistics")
        )
        if late_seller or late_logistics:
            issue = "late_delivery_seller" if late_seller else "late_delivery_logistics"
            party_type = "seller" if late_seller else "logistics_provider"
            return _decision_output(
                state, context, issue=issue, status="action_required", confidence=0.88,
                refs=order_refs + shipment_refs, cause=issue.upper(),
                parties=[{"party_type": party_type, "party_id": None}], action="OFFER_COMPENSATION",
            )

    if not order_refs + payment_refs + shipment_refs:
        return build_safe_output(state, context)
    return _decision_output(
        state, context, issue="unsupported_claim", status="no_action", confidence=0.72,
        refs=order_refs, cause="CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
        parties=[{"party_type": "platform", "party_id": None}], verdict="unsupported",
    )


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
        try:
            confidence = min(max(float(claim.get("confidence", 0.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            failed.append("CLAIM_CONFIDENCE_INVALID")
            confidence = 0.3
        verdict = claim.get("verdict", "insufficient_evidence")
        if verdict in {"supported", "partially_supported"} and not claim_refs:
            failed.append("SUPPORTED_CLAIM_WITHOUT_EVIDENCE")
            verdict = "insufficient_evidence"
            confidence = 0.3
        claims.append({**claim, "evidence_refs": claim_refs, "confidence": confidence})
        claims[-1]["verdict"] = verdict
    if "claim_assessments" in output:
        output["claim_assessments"] = claims

    assessment = dict(output["assessment"])
    try:
        confidence = min(max(float(assessment.get("confidence", 0.0)), 0.0), 1.0)
    except (TypeError, ValueError):
        failed.append("CONFIDENCE_INVALID")
        confidence = 0.3
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
    valid_lines = []
    for line in lines:
        try:
            amount = float(line["amount_brl"])
        except (KeyError, TypeError, ValueError):
            failed.append("REFUND_LINE_INVALID")
            continue
        valid_lines.append({**line, "amount_brl": amount})
    lines = valid_lines
    try:
        recommended = float(financial.get("recommended_refund_brl", 0.0))
    except (TypeError, ValueError):
        failed.append("REFUND_TOTAL_INVALID")
        recommended = 0.0
    total = round(sum(line["amount_brl"] for line in lines), 2)
    if abs(total - recommended) >= 0.01:
        failed.append("REFUND_TOTAL_MISMATCH")
    financial.update({"currency": "BRL", "refund_lines": lines, "recommended_refund_brl": total})
    output["financial_resolution"] = financial

    actions = list(dict.fromkeys(output.get("resolution_actions", [])))
    if actions != output.get("resolution_actions", []):
        failed.append("DUPLICATE_ACTIONS")
    if total > 0 and "PROCESS_REFUND" not in actions:
        failed.append("REFUND_WITHOUT_PROCESS_ACTION")
        actions.insert(0, "PROCESS_REFUND")
    if assessment["case_status"] == "no_action" and total > 0:
        failed.append("NO_ACTION_WITH_REFUND")
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
        actions = [action for action in actions if action != "PROCESS_REFUND"]

    issue = assessment.get("primary_issue")
    parties = list(output.get("root_cause_analysis", {}).get("responsible_parties", []))
    required_party = {
        "late_delivery_seller": "seller",
        "late_delivery_logistics": "logistics_provider",
    }.get(issue)
    if required_party and not any(party.get("party_type") == required_party for party in parties):
        failed.append("RESPONSIBILITY_MISMATCH")
        parties.append({"party_type": required_party, "party_id": None})
        output["root_cause_analysis"]["responsible_parties"] = parties[:5]

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
