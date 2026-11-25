"""Agent fixer: tool loop, budgets, step logging (spec 11).

`AgentFixer` is the Tier 2 fixer in `agent` mode. It reads through a confined
`Workspace`, type-checks with a `TypeChecker`, and leaves a session only through a
patch that passes the AST guard (spec 05).
"""

from guardian.agent.loop import BUDGET_EXCEEDED, TOOLS, AgentFixer, Budget, replay
from guardian.agent.typecheck import StaticTypeChecker, TypeChecker, TypecheckUnavailable
from guardian.agent.workspace import Refused, Workspace

__all__ = [
    "BUDGET_EXCEEDED",
    "TOOLS",
    "AgentFixer",
    "Budget",
    "Refused",
    "StaticTypeChecker",
    "TypeChecker",
    "TypecheckUnavailable",
    "Workspace",
    "replay",
]
