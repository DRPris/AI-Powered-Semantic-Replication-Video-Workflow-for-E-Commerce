"""Repository for token usage and cost summaries."""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import TokenUsageRecord


def _row_to_dict(record: TokenUsageRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "project_id": record.project_id,
        "stage": record.stage,
        "call_type": record.call_type,
        "model": record.model,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "total_tokens": record.total_tokens,
        "cached": record.cached,
        "estimated_cost_usd": record.estimated_cost_usd,
        "timestamp": record.created_at.isoformat(),
        "metadata": record.metadata_json or {},
    }


def _empty_project_summary(project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "call_count": 0,
        "cache_hit_count": 0,
        "by_stage": {},
        "by_model": {},
    }


class TokenUsageRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, entry: dict[str, Any]) -> TokenUsageRecord:
        async with self._session_factory() as session:
            record = TokenUsageRecord(
                project_id=str(entry["project_id"]),
                stage=str(entry["stage"]),
                call_type=str(entry["call_type"]),
                model=str(entry["model"]),
                input_tokens=int(entry.get("input_tokens", 0) or 0),
                output_tokens=int(entry.get("output_tokens", 0) or 0),
                total_tokens=int(entry.get("total_tokens", 0) or 0),
                cached=bool(entry.get("cached", False)),
                estimated_cost_usd=float(entry.get("estimated_cost_usd", 0.0) or 0.0),
                metadata_json=entry.get("metadata", {}) or {},
            )
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, datetime):
                record.created_at = timestamp
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def project_records(self, project_id: str, limit: int = 500) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            records = (
                await session.scalars(
                    select(TokenUsageRecord)
                    .where(TokenUsageRecord.project_id == project_id)
                    .order_by(TokenUsageRecord.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [_row_to_dict(record) for record in records]

    async def project_summary(self, project_id: str) -> dict[str, Any]:
        records = await self.project_records(project_id, limit=10000)
        if not records:
            return _empty_project_summary(project_id)
        return summarize_records(project_id, records)

    async def global_summary(self, limit: int = 50) -> dict[str, Any]:
        async with self._session_factory() as session:
            project_rows = (
                await session.execute(
                    select(
                        TokenUsageRecord.project_id,
                        func.max(TokenUsageRecord.created_at).label("last_seen"),
                    )
                    .group_by(TokenUsageRecord.project_id)
                    .order_by(func.max(TokenUsageRecord.created_at).desc())
                    .limit(limit)
                )
            ).all()
            project_ids = [row[0] for row in project_rows]
            count_result = await session.execute(
                select(func.count(func.distinct(TokenUsageRecord.project_id)))
            )
            project_count = int(count_result.scalar() or 0)

        projects = [await self.project_summary(project_id) for project_id in project_ids]
        global_total = {
            "total_input_tokens": sum(p["total_input_tokens"] for p in projects),
            "total_output_tokens": sum(p["total_output_tokens"] for p in projects),
            "total_tokens": sum(p["total_tokens"] for p in projects),
            "total_cost_usd": round(sum(p["total_cost_usd"] for p in projects), 6),
            "total_calls": sum(p["call_count"] for p in projects),
            "project_count": project_count,
        }
        return {"projects": projects, "global_total": global_total}

    async def daily_cost_usd(self, day: datetime | None = None) -> float:
        now = day or datetime.now(timezone.utc)
        start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
        end = datetime.combine(now.date(), time.max, tzinfo=timezone.utc)
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(TokenUsageRecord.estimated_cost_usd), 0.0))
                .where(TokenUsageRecord.created_at >= start)
                .where(TokenUsageRecord.created_at <= end)
            )
            return round(float(result.scalar() or 0.0), 6)


def summarize_records(project_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _empty_project_summary(project_id)
    summary["call_count"] = len(records)
    for record in records:
        inp = int(record.get("input_tokens", 0) or 0)
        out = int(record.get("output_tokens", 0) or 0)
        cost = float(record.get("estimated_cost_usd", 0.0) or 0.0)
        stage = str(record.get("stage", "unknown"))
        model = str(record.get("model", "unknown"))

        summary["total_input_tokens"] += inp
        summary["total_output_tokens"] += out
        summary["total_tokens"] += inp + out
        summary["total_cost_usd"] += cost
        if record.get("cached"):
            summary["cache_hit_count"] += 1

        stage_summary = summary["by_stage"].setdefault(
            stage,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "call_count": 0,
            },
        )
        stage_summary["input_tokens"] += inp
        stage_summary["output_tokens"] += out
        stage_summary["total_tokens"] += inp + out
        stage_summary["cost_usd"] += cost
        stage_summary["call_count"] += 1

        model_summary = summary["by_model"].setdefault(
            model,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "call_count": 0,
            },
        )
        model_summary["input_tokens"] += inp
        model_summary["output_tokens"] += out
        model_summary["total_tokens"] += inp + out
        model_summary["cost_usd"] += cost
        model_summary["call_count"] += 1

    summary["total_cost_usd"] = round(summary["total_cost_usd"], 6)
    return summary
