"""Coordinator / supervisor: intent analysis, task routing and A2A handoffs.

The customer's claimed topic is only a routing hypothesis. It decides which
specialists to consult first; it never decides ``primary_issue`` (policy agent's job).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agents import (
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    SPECIALISTS,
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    VERIFIER,
    AgentContext,
    Specialist,
    build_safe_output,
    verify_output,
)
from .mcp_gateway import EvidenceGateway
from .state import AgentInput, AgentOutput, CaseState
from .trace import TraceWriter

COORDINATOR = "coordinator"

FAMILY_PAYMENT = "payment"
FAMILY_REFUND = "refund"
FAMILY_DELIVERY = "delivery"
FAMILY_UNKNOWN = "unknown"

TOPIC_FAMILIES: dict[str, str] = {
    "canceled_order_paid": FAMILY_PAYMENT,
    "unavailable_order_paid": FAMILY_PAYMENT,
    "valid_split_payment": FAMILY_PAYMENT,
    "payment_mismatch": FAMILY_PAYMENT,
    "duplicate_charge": FAMILY_PAYMENT,
    "refund_pending": FAMILY_REFUND,
    "refund_failed": FAMILY_REFUND,
    "late_delivery_seller": FAMILY_DELIVERY,
    "late_delivery_logistics": FAMILY_DELIVERY,
}

# Specialists consulted first for each intent family (after the order agent).
FAMILY_SPECIALISTS: dict[str, tuple[str, ...]] = {
    FAMILY_PAYMENT: (PAYMENT_AGENT,),
    FAMILY_REFUND: (PAYMENT_AGENT,),
    FAMILY_DELIVERY: (SHIPMENT_AGENT,),
    FAMILY_UNKNOWN: (PAYMENT_AGENT, SHIPMENT_AGENT),
}

# L3A does not score efficiency, and the claimed topic may be wrong, so the
# remaining domain specialists are consulted too ("fetch broad, cite narrow").
# The policy agent still cites only evidence that supports its conclusion.
BROAD_SCAN = True

REFUND_TOPICS = frozenset({"refund_pending", "refund_failed", "requested_full_refund"})
PAYMENT_RISK_STATUSES = frozenset({"canceled", "unavailable"})

TASK_CODES: dict[str, str] = {
    ORDER_AGENT: "INVESTIGATE_ORDER",
    PAYMENT_AGENT: "AUDIT_PAYMENTS",
    SHIPMENT_AGENT: "AUDIT_SHIPMENT",
    POLICY_AGENT: "DECIDE_POLICY",
    VERIFIER: "VERIFY_DRAFT",
}


@dataclass
class IntentPlan:
    """Routing plan derived from the case input. Hypothesis only, not ground truth."""

    case_id: str
    order_id: str | None
    policy_version: str | None
    claimed_topic: str | None
    intent_family: str
    refund_requested: bool
    claim_ids: list[str]
    claim_topics: list[str]
    specialists: list[str] = field(default_factory=list)
    route_reasons: dict[str, str] = field(default_factory=dict)

    def add(self, agent: str, reason: str) -> None:
        if agent not in self.specialists:
            self.specialists.append(agent)
            self.route_reasons[agent] = reason

    def to_attributes(self) -> dict[str, str | int | float | bool | None]:
        return {
            "intent_family": self.intent_family,
            "claimed_topic": self.claimed_topic,
            "refund_requested": self.refund_requested,
        }


def analyze_intent(case: dict[str, Any]) -> IntentPlan:
    request = case.get("customer_request") or {}
    claims = [claim for claim in request.get("claims") or [] if isinstance(claim, dict)]
    topics = [str(claim.get("topic")) for claim in claims if claim.get("topic")]

    # The first non-refund topic is the customer's main complaint.
    claimed_topic = next((topic for topic in topics if topic != "requested_full_refund"), None)
    family = TOPIC_FAMILIES.get(claimed_topic or "", FAMILY_UNKNOWN)
    order_id = request.get("claimed_order_id")

    plan = IntentPlan(
        case_id=case["case_id"],
        order_id=order_id if isinstance(order_id, str) and order_id else None,
        policy_version=case.get("policy_version"),
        claimed_topic=claimed_topic,
        intent_family=family,
        refund_requested="requested_full_refund" in topics,
        claim_ids=[str(claim["claim_id"]) for claim in claims if claim.get("claim_id")],
        claim_topics=[str(claim["topic"]) for claim in claims if claim.get("claim_id")],
    )
    for agent in FAMILY_SPECIALISTS[family]:
        plan.add(agent, "intent")
    if BROAD_SCAN:
        for agent in (PAYMENT_AGENT, SHIPMENT_AGENT):
            plan.add(agent, "broad_scan")
    return plan


def refine_plan(plan: IntentPlan, order_findings: dict[str, Any]) -> None:
    """Adjust routing with authoritative order facts (e.g. canceled order → audit payments)."""
    status = order_findings.get("order_status")
    if status in PAYMENT_RISK_STATUSES:
        if PAYMENT_AGENT in plan.specialists:
            plan.specialists.remove(PAYMENT_AGENT)
        plan.specialists.insert(0, PAYMENT_AGENT)
        plan.route_reasons[PAYMENT_AGENT] = f"order_status_{status}"
    if order_findings.get("order", {}).get("order_delivered_customer_date"):
        plan.add(SHIPMENT_AGENT, "delivered_order")


class Coordinator:
    def __init__(
        self,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        specialists: dict[str, Specialist] | None = None,
    ) -> None:
        self.gateway = gateway
        self.trace = trace
        self.specialists = specialists or SPECIALISTS

    async def run(self, case: dict[str, Any]) -> dict[str, Any]:
        """Solve one case. Always returns a schema-shaped output; never raises on agent failure."""
        state = CaseState(case_id=case["case_id"])
        ctx = AgentContext(case=case, state=state, gateway=self.gateway, trace=self.trace)
        plan = analyze_intent(case)
        state.update_context("intent", {**plan.to_attributes(), "order_id": plan.order_id})
        base_context = {
            "order_id": plan.order_id,
            "policy_version": plan.policy_version,
            "claimed_topic": plan.claimed_topic,
            "intent_family": plan.intent_family,
            "claim_ids": plan.claim_ids,
            "claim_topics": [
                str(claim.get("topic"))
                for claim in (case.get("customer_request") or {}).get("claims", [])
                if isinstance(claim, dict) and claim.get("claim_id")
            ],
        }

        # 1. Order agent always runs first: it confirms the claimed order exists in scope.
        order_ok = False
        if plan.order_id:
            order_out = await self._delegate(ctx, plan, ORDER_AGENT, base_context)
            order_ok = order_out.findings.get("status") == STATUS_OK
            if order_ok:
                refine_plan(plan, order_out.findings)

        # 2. Domain specialists chosen by intent + authoritative order facts.
        if order_ok:
            for agent in plan.specialists:
                task_context = dict(base_context)
                if agent == PAYMENT_AGENT:
                    task_context["include_refunds"] = (
                        plan.intent_family in (FAMILY_REFUND, FAMILY_UNKNOWN)
                        or plan.claimed_topic in REFUND_TOPICS
                    )
                await self._delegate(ctx, plan, agent, task_context)

        # 3. Policy agent drafts the decision, then hands off to the verifier.
        policy_out = await self._delegate(
            ctx,
            plan,
            POLICY_AGENT,
            {**base_context, "order_verified": order_ok},
            return_to=VERIFIER,
        )
        draft = policy_out.findings.get("draft")
        if not isinstance(draft, dict):
            draft = build_safe_output(state, base_context)

        # 4. Verifier checks invariants and returns the approved output to the coordinator.
        verifier_out = await self._delegate(
            ctx, plan, VERIFIER, {**base_context, "draft": draft}, assign=False
        )
        output = verifier_out.findings.get("output")
        if not isinstance(output, dict):
            try:
                output, _ = verify_output(draft, state)
            except (KeyError, TypeError, ValueError):
                output = build_safe_output(state, base_context)
        return output

    async def _delegate(
        self,
        ctx: AgentContext,
        plan: IntentPlan,
        agent: str,
        context: dict[str, Any],
        *,
        return_to: str = COORDINATOR,
        assign: bool = True,
    ) -> AgentOutput:
        """Assign a task, run the agent in isolation, and emit the handoff back."""
        task_code = TASK_CODES[agent]
        # assign=False when the previous agent's handoff already addressed this agent.
        if assign:
            self.trace.emit(
                case_id=ctx.case_id,
                event_type="task_assigned",
                actor=COORDINATOR,
                target=agent,
                decision_code=task_code,
                attributes={**plan.to_attributes(), "route_reason": plan.route_reasons.get(agent)},
            )

        task = AgentInput(task=task_code.lower(), case_id=ctx.case_id, context=context)
        try:
            task.validate()
            result = await self.specialists[agent](task, ctx)
            result.validate()
        except Exception as exc:  # isolate failures: one agent must not crash the run
            result = AgentOutput(
                findings={"status": STATUS_ERROR, "error_type": type(exc).__name__}, confidence=0.0
            )

        status = str(result.findings.get("status", STATUS_OK))
        self.trace.emit_handoff(
            case_id=ctx.case_id,
            actor=agent,
            target=return_to,
            decision_code=_handoff_code(agent, status),
            attributes={"status": status, "evidence_count": len(result.evidence_refs)},
        )
        return result


def _handoff_code(agent: str, status: str) -> str:
    if agent == VERIFIER:
        return "APPROVED" if status == STATUS_OK else "VERIFICATION_ERROR"
    if agent == POLICY_AGENT:
        return "DRAFT_READY" if status == STATUS_OK else "DRAFT_FAILED"
    domain = agent.split("-")[0].upper()
    if status == STATUS_NOT_FOUND:
        return f"{domain}_NOT_FOUND"
    if status == STATUS_ERROR:
        return f"{domain}_ERROR"
    return f"{domain}_VERIFIED"
