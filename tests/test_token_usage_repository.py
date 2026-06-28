import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from persistence.database import Base
from persistence import models  # noqa: F401
from persistence.token_usage_repository import TokenUsageRepository
from services.cost_guard import CostBudgetExceeded, assert_cost_budget_available


async def _repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, TokenUsageRepository(factory), factory


def test_token_usage_repository_summarizes_by_project_stage_and_model():
    async def scenario():
        engine, repository, _ = await _repository()
        try:
            await repository.record(
                {
                    "project_id": "project-1",
                    "stage": "stage1",
                    "call_type": "video_analysis",
                    "model": "gemini-2.5-flash",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "total_tokens": 1500,
                    "cached": False,
                    "estimated_cost_usd": 0.00045,
                }
            )
            await repository.record(
                {
                    "project_id": "project-1",
                    "stage": "stage3",
                    "call_type": "prompt_audit",
                    "model": "qwen-plus",
                    "input_tokens": 2000,
                    "output_tokens": 1000,
                    "total_tokens": 3000,
                    "cached": False,
                    "estimated_cost_usd": 0.00165,
                }
            )

            summary = await repository.project_summary("project-1")
            assert summary["call_count"] == 2
            assert summary["total_tokens"] == 4500
            assert summary["total_cost_usd"] == 0.0021
            assert summary["by_stage"]["stage1"]["call_count"] == 1
            assert summary["by_model"]["qwen-plus"]["total_tokens"] == 3000

            global_summary = await repository.global_summary()
            assert global_summary["global_total"]["total_calls"] == 2
            assert global_summary["global_total"]["project_count"] == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cost_guard_blocks_when_project_budget_is_exceeded(monkeypatch):
    async def scenario():
        engine, repository, factory = await _repository()
        try:
            await repository.record(
                {
                    "project_id": "project-budget",
                    "stage": "stage1",
                    "call_type": "video_analysis",
                    "model": "gemini-2.5-flash",
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "cached": False,
                    "estimated_cost_usd": 1.25,
                }
            )

            import services.cost_guard as cost_guard

            monkeypatch.setattr(cost_guard, "get_session_factory", lambda: factory)
            settings = SimpleNamespace(
                ENABLE_COST_GUARD=True,
                PROJECT_BUDGET_USD=1.0,
                DAILY_BUDGET_USD=0.0,
            )
            with pytest.raises(CostBudgetExceeded):
                await assert_cost_budget_available(settings, "project-budget")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
