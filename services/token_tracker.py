"""
Token 消耗追踪服务

按 project_id + stage + call_type 维度记录每次 AI 调用的 Token 消耗，
PostgreSQL 持久化为生产默认，同时保留 JSON 文件兼容旧本地记录。
"""

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from persistence.database import get_session_factory
from persistence.token_usage_repository import TokenUsageRepository, summarize_records

logger = logging.getLogger(__name__)

# 持久化目录
STATS_DIR = Path(__file__).parent.parent / "tmp" / "token_stats"
STATS_DIR.mkdir(parents=True, exist_ok=True)

# Gemini 定价 (USD per 1M tokens) — gemini-2.5-flash 系列
# 参考: https://ai.google.dev/pricing
PRICING = {
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-flash-image": {"input": 0.15, "output": 0.60},
    "gemini-3-flash-preview": {"input": 0.15, "output": 0.60},
    # Qwen (通义千问) — 百炼平台 qwen-plus 定价 (约 0.004 CNY/千token ≈ 0.55 USD/1M)
    "qwen-plus": {"input": 0.55, "output": 0.55},
    "qwen-max": {"input": 1.20, "output": 1.20},
}

# 默认定价 (未知模型)
DEFAULT_PRICING = {"input": 0.50, "output": 1.00}


class TokenTracker:
    """Token 消耗追踪器（单例模式，线程安全）"""

    _instance: Optional["TokenTracker"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "TokenTracker":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._write_lock = threading.Lock()
        logger.info(f"TokenTracker initialized, stats_dir={STATS_DIR}")

    def record(
        self,
        project_id: str,
        stage: str,
        call_type: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached: bool = False,
    ) -> None:
        """
        记录一次 AI 调用的 Token 消耗

        Args:
            project_id: 项目 ID (如 recXXX)
            stage: 阶段标识 (stage1/stage2/stage3/stage4/stage5)
            call_type: 调用类型 (video_analysis/product_analysis/script_generation 等)
            model: 模型名称
            input_tokens: 输入 Token 数
            output_tokens: 输出 Token 数
            cached: 是否命中缓存（命中则不计费）
        """
        entry = {
            "project_id": project_id,
            "stage": stage,
            "call_type": call_type,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cached": cached,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 计算估算费用 (USD)
        pricing = PRICING.get(model, DEFAULT_PRICING)
        if cached:
            entry["estimated_cost_usd"] = 0.0
        else:
            entry["estimated_cost_usd"] = round(
                (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000,
                6,
            )

        # 持久化到 JSON 文件
        self._append_to_file(project_id, entry)
        self._write_to_database(entry)

        logger.info(
            f"[TokenTracker] {project_id}/{stage}/{call_type}: "
            f"model={model}, in={input_tokens}, out={output_tokens}, "
            f"cached={cached}, cost=${entry['estimated_cost_usd']:.6f}"
        )

    def _get_file_path(self, project_id: str) -> Path:
        """获取项目统计文件路径"""
        return STATS_DIR / f"{project_id}.json"

    def _append_to_file(self, project_id: str, entry: dict) -> None:
        """追加记录到项目 JSON 文件"""
        file_path = self._get_file_path(project_id)
        with self._write_lock:
            records = []
            if file_path.exists():
                try:
                    records = json.loads(file_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, IOError):
                    records = []
            records.append(entry)
            file_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    async def _record_database_async(self, entry: dict[str, Any]) -> None:
        try:
            repository = TokenUsageRepository(get_session_factory())
            await repository.record(entry)
        except Exception as exc:
            logger.warning("Token usage database write failed; JSON fallback kept: %s", exc)

    def _write_to_database(self, entry: dict[str, Any]) -> None:
        if os.getenv("COST_TRACKING_BACKEND", "database").lower() != "database":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._record_database_async(entry))
            return
        loop.create_task(self._record_database_async(entry))

    def get_project_records(self, project_id: str) -> list[dict]:
        """获取项目的所有 Token 消耗记录"""
        file_path = self._get_file_path(project_id)
        if not file_path.exists():
            return []
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return []

    def get_project_summary(self, project_id: str) -> dict[str, Any]:
        """
        按阶段汇总某项目的 Token 消耗

        Returns:
            {
              "project_id": "recXXX",
              "total_input_tokens": ...,
              "total_output_tokens": ...,
              "total_tokens": ...,
              "total_cost_usd": ...,
              "call_count": ...,
              "cache_hit_count": ...,
              "by_stage": {
                "stage1": {"input_tokens": ..., "output_tokens": ..., "total_tokens": ..., "cost_usd": ..., "call_count": ...},
                ...
              },
              "by_model": {
                "gemini-2.5-flash": {"input_tokens": ..., "output_tokens": ..., "cost_usd": ..., "call_count": ...},
                ...
              }
            }
        """
        records = self.get_project_records(project_id)
        return summarize_records(project_id, records)

    async def get_project_records_async(
        self, project_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        repository = TokenUsageRepository(get_session_factory())
        records = await repository.project_records(project_id, limit=limit)
        return records or self.get_project_records(project_id)

    async def get_project_summary_async(self, project_id: str) -> dict[str, Any]:
        repository = TokenUsageRepository(get_session_factory())
        summary = await repository.project_summary(project_id)
        if summary.get("call_count", 0):
            return summary
        return self.get_project_summary(project_id)

    def get_all_summary(self, limit: int = 50) -> dict[str, Any]:
        """
        全局 Token 消耗汇总

        Args:
            limit: 最多返回的项目数

        Returns:
            {"projects": [...], "global_total": {...}}
        """
        project_files = sorted(STATS_DIR.glob("*.json"), key=os.path.getmtime, reverse=True)

        projects = []
        global_total = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "total_calls": 0,
            "project_count": len(project_files),
        }

        for f in project_files[:limit]:
            pid = f.stem
            summary = self.get_project_summary(pid)
            projects.append(summary)
            global_total["total_input_tokens"] += summary["total_input_tokens"]
            global_total["total_output_tokens"] += summary["total_output_tokens"]
            global_total["total_tokens"] += summary["total_tokens"]
            global_total["total_cost_usd"] += summary["total_cost_usd"]
            global_total["total_calls"] += summary["call_count"]

        global_total["total_cost_usd"] = round(global_total["total_cost_usd"], 6)

        return {
            "projects": projects,
            "global_total": global_total,
        }

    async def get_all_summary_async(self, limit: int = 50) -> dict[str, Any]:
        repository = TokenUsageRepository(get_session_factory())
        result = await repository.global_summary(limit=limit)
        if result.get("global_total", {}).get("total_calls", 0):
            return result
        return self.get_all_summary(limit=limit)


# 全局单例
token_tracker = TokenTracker()
