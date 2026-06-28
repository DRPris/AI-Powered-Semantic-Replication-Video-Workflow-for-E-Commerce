"""Cost budget checks for expensive workflow entry points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from persistence.database import get_session_factory
from persistence.token_usage_repository import TokenUsageRepository


@dataclass(frozen=True)
class CostBudgetExceeded(Exception):
    scope: str
    current_cost_usd: float
    budget_usd: float

    def __str__(self) -> str:
        return (
            f"{self.scope} budget exceeded: "
            f"${self.current_cost_usd:.6f} >= ${self.budget_usd:.6f}"
        )


async def assert_cost_budget_available(settings: Any, project_id: str) -> None:
    if not bool(getattr(settings, "ENABLE_COST_GUARD", True)):
        return
    repository = TokenUsageRepository(get_session_factory())

    project_budget = float(getattr(settings, "PROJECT_BUDGET_USD", 0.0) or 0.0)
    if project_budget > 0:
        project_summary = await repository.project_summary(project_id)
        project_cost = float(project_summary.get("total_cost_usd", 0.0) or 0.0)
        if project_cost >= project_budget:
            raise CostBudgetExceeded("project", project_cost, project_budget)

    daily_budget = float(getattr(settings, "DAILY_BUDGET_USD", 0.0) or 0.0)
    if daily_budget > 0:
        daily_cost = await repository.daily_cost_usd()
        if daily_cost >= daily_budget:
            raise CostBudgetExceeded("daily", daily_cost, daily_budget)
