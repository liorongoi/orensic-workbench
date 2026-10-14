"""Built-in agents.

They act only through the Workbench, so every step is validated and written to the
audit log as an agent action, and anything they add to a chain is a proposal an
examiner must accept. They are deterministic programs (rules and measured scores),
so the same input gives the same output; an LLM agent connected over MCP can call
them as tools and explain or extend their results.
"""

from .planner import WORKFLOWS, plan_enhancement
from .reviewer import review_case
from .triage import triage_case

__all__ = ["WORKFLOWS", "plan_enhancement", "review_case", "triage_case"]
