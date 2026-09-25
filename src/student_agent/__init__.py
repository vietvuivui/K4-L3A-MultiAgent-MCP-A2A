"""Day09 student starter kit."""

from .contracts import VARIANT_ID
from .state import AgentInput, AgentOutput, CaseState

OUTPUT_SCHEMA_VERSION = "day09-l3a-output-v2"

__all__ = [
    "OUTPUT_SCHEMA_VERSION",
    "VARIANT_ID",
    "AgentInput",
    "AgentOutput",
    "CaseState",
]
