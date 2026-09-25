from __future__ import annotations

from typing import Any

from .coordinator import Coordinator
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the L3A coordinator → specialists → policy → verifier workflow for one case.

    The CLI emits ``case_received`` before and ``case_finalized`` after this call.
    """
    return await Coordinator(gateway, trace).run(case)
