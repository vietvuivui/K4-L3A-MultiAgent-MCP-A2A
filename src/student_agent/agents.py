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
from datetime import datetime, timedelta
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
            # Gateway returned isError: either not found for this scoped entity or a
            # transient backend failure. Retry the identical call before giving up.
            if attempt >= MAX_RETRIES:
                return None
            await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
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


def _refs_for_tools(state: CaseState, *tools: str) -> list[str]:
    meta = state.context.get("evidence_meta", {})
    return [ref for tool in tools for ref, info in meta.items() if info.get("tool") == tool]


# Evidence groups cited for each issue ("fetch broad, cite narrow").
ISSUE_EVIDENCE_TOOLS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_payment_timeline", "get_policy"),
    "unavailable_order_paid": (
        "get_order", "get_payment_timeline", "get_order_items", "get_policy",
    ),
    "late_delivery_seller": (
        "get_order", "get_shipment_summary", "get_order_items", "get_policy",
    ),
    "late_delivery_logistics": ("get_order", "get_shipment_summary", "get_policy"),
    "valid_split_payment": ("get_order", "get_payment_timeline", "get_policy"),
    "payment_mismatch": ("get_order", "get_payment_timeline", "get_policy"),
    "duplicate_charge": ("get_order", "get_payment_timeline", "get_policy"),
    "refund_pending": ("get_order", "get_refund_timeline", "get_payment_timeline", "get_policy"),
    "refund_failed": ("get_order", "get_refund_timeline", "get_payment_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order", "get_shipment_summary", "get_payment_timeline", "get_policy",
    ),
}

# How far after delivery / the estimated date an event still belongs to this order.
# Rows far outside the order lifecycle are distractors injected into the evidence.
EVENT_WINDOW_DAYS = 10
FULL_REFUND_ISSUES = frozenset(
    {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}
)
PARTIAL_REFUND_ISSUES = frozenset(
    {"late_delivery_seller", "late_delivery_logistics", "payment_mismatch", "duplicate_charge",
     "refund_pending"}
)
DECISION_CONFIDENCE = 0.95


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _order_window(order: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = _parse_time(order.get("order_purchase_timestamp"))
    ends = [
        moment
        for moment in (
            _parse_time(order.get("order_delivered_customer_date")),
            _parse_time(order.get("order_estimated_delivery_date")),
            _parse_time(order.get("order_approved_at")),
        )
        if moment
    ]
    if start is None or not ends:
        return None
    return start - timedelta(days=1), max(ends) + timedelta(days=EVENT_WINDOW_DAYS)


def _in_window(rows: Any, window: tuple[datetime, datetime] | None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        moment = _parse_time(row.get("event_at"))
        if window is None or (moment is not None and window[0] <= moment <= window[1]):
            result.append(row)
    return result


def classify_issue(state: CaseState) -> tuple[str, dict[str, Any]]:
    """Decide the primary issue from authoritative evidence scoped to the order lifecycle."""
    order = state.context.get("order", {})
    order_data = order.get("order", {}) if isinstance(order.get("order"), dict) else {}
    window = _order_window(order_data)
    payment = state.context.get("payment", {})
    shipment = state.context.get("shipment", {})

    payment_events = _in_window(payment.get("payment_events"), window)
    refund_data = payment.get("refund_events")
    refund_rows = refund_data.get("events") if isinstance(refund_data, dict) else refund_data
    refund_events = _in_window(refund_rows, window)
    shipment_data = shipment.get("shipment") if isinstance(shipment.get("shipment"), dict) else {}
    shipment_events = _in_window(shipment_data.get("events"), window)

    captures = [
        amount
        for row in payment_events
        if row.get("event_type") == "captured"
        and (amount := _as_float(row.get("amount_brl"))) is not None
    ]
    facts: dict[str, Any] = {
        "captured_brl": round(sum(captures), 2),
        "capture_count": len(captures),
        "ignored_events": (
            len(payment.get("payment_events") or []) - len(payment_events)
            + len(refund_rows or []) - len(refund_events)
            + len(shipment_data.get("events") or []) - len(shipment_events)
        ),
    }

    status = order.get("order_status")
    if status == "canceled" and captures:
        return "canceled_order_paid", facts
    if status == "unavailable" and captures:
        return "unavailable_order_paid", facts

    refund_statuses = {str(row.get("status", "")).lower() for row in refund_events}
    if "failed" in refund_statuses:
        return "refund_failed", facts
    if refund_statuses & {"pending", "processing", "requested"}:
        return "refund_pending", facts
    if any(row.get("event_type") == "reconciliation_mismatch" for row in payment_events):
        return "payment_mismatch", facts

    if len(captures) >= 2 and len(set(captures)) < len(captures):
        items = order.get("items") or []
        first = items[0] if items and isinstance(items[0], dict) else {}
        order_value = (_as_float(first.get("price")) or 0.0) + (
            _as_float(first.get("freight_value")) or 0.0
        )
        facts["order_value_brl"] = round(order_value, 2)
        if order_value > 0 and abs(sum(captures) - order_value) < 0.01:
            return "valid_split_payment", facts
        return "duplicate_charge", facts

    delivered = _parse_time(order_data.get("order_delivered_customer_date"))
    estimated = _parse_time(order_data.get("order_estimated_delivery_date"))
    if delivered and estimated and delivered > estimated:
        late_actors = {
            row.get("actor") for row in shipment_events if row.get("event_type") == "delivered_late"
        }
        if "seller" in late_actors:
            return "late_delivery_seller", facts
        if "logistics_provider" in late_actors:
            return "late_delivery_logistics", facts
        facts["late_without_actor"] = True
        return "insufficient_evidence", facts

    return "unsupported_claim", facts


def _claim_assessments(
    context: dict[str, Any],
    issue: str,
    confidence: float,
    refs: list[str],
) -> list[dict[str, Any]]:
    assessments = []
    for claim_id, topic in zip(
        context.get("claim_ids", []), context.get("claim_topics", []), strict=True
    ):
        if topic == "requested_full_refund":
            if issue in FULL_REFUND_ISSUES:
                verdict = "supported"
            elif issue in PARTIAL_REFUND_ISSUES:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif topic == issue and issue != "unsupported_claim":
            verdict = "supported"
        else:
            verdict = "unsupported"
        assessments.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": confidence,
            "evidence_refs": list(refs),
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
) -> dict[str, Any]:
    unique_refs = list(dict.fromkeys(refs))[:30]
    lines = []
    if refund > 0:
        lines = [{
            "reason_code": refund_reason or issue.upper(),
            "amount_brl": refund,
            "entity_id": context.get("order_id"),
        }]
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": state.case_id,
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": affected_entities_from_state(state),
        "claim_assessments": _claim_assessments(context, issue, confidence, unique_refs),
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
        "resolution_actions": [action] if action else ["escalate_manual_review"],
    }


def build_policy_output(state: CaseState, context: dict[str, Any]) -> dict[str, Any]:
    """Classify from evidence, then take status/action/refund/parties from the policy rule."""
    order = state.context.get("order", {})
    if order.get("status") != STATUS_OK:
        return build_safe_output(state, context)

    issue, facts = classify_issue(state)
    state.update_context("decision_facts", {"issue": issue, **facts})
    if issue == "insufficient_evidence":
        return build_safe_output(state, context)

    policy = state.context.get("policy", {})
    rules = policy.get("data", {}).get("rules", {}) if isinstance(policy.get("data"), dict) else {}
    rule = rules.get(issue) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        return build_safe_output(state, context)

    seller_ids = order.get("seller_ids") or []
    parties = []
    for party in rule.get("responsible_parties") or [{"party_type": "unknown"}]:
        party_type = party.get("party_type", "unknown")
        # The policy example IDs belong to other orders; bind the seller of this order.
        party_id = seller_ids[0] if party_type == "seller" and seller_ids else None
        parties.append({"party_type": party_type, "party_id": party_id})

    refs = _refs_for_tools(state, *ISSUE_EVIDENCE_TOOLS.get(issue, ("get_order", "get_policy")))
    refund = round(_as_float(rule.get("refund_brl")) or 0.0, 2)
    return _decision_output(
        state,
        context,
        issue=issue,
        status=str(rule.get("case_status", "needs_investigation")),
        confidence=DECISION_CONFIDENCE,
        refs=refs,
        cause=issue.upper(),
        parties=parties[:5],
        refund=refund,
        refund_reason=issue.upper(),
        action=rule.get("recommended_action"),
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
    if total > 0 and not actions:
        failed.append("REFUND_WITHOUT_PROCESS_ACTION")
        actions.insert(0, "issue_refund")
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
