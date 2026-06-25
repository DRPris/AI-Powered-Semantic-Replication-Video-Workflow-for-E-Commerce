"""
Token 消耗追踪服务

按 project_id + stage + call_type 维度记录每次 AI 调用的 Token 消耗，
JSON 文件本地持久化，并支持汇总查询与 Airtable 同步。
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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
            "timestamp": datetime.now().isoformat(),
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
        if not records:
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

        summary = {
            "project_id": project_id,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "call_count": len(records),
            "cache_hit_count": 0,
            "by_stage": {},
            "by_model": {},
        }

        for r in records:
            inp = r.get("input_tokens", 0)
            out = r.get("output_tokens", 0)
            cost = r.get("estimated_cost_usd", 0.0)
            stage = r.get("stage", "unknown")
            model = r.get("model", "unknown")

            summary["total_input_tokens"] += inp
            summary["total_output_tokens"] += out
            summary["total_tokens"] += inp + out
            summary["total_cost_usd"] += cost
            if r.get("cached"):
                summary["cache_hit_count"] += 1

            # by_stage
            if stage not in summary["by_stage"]:
                summary["by_stage"][stage] = {
                    "input_tokens": 0, "output_tokens": 0,
                    "total_tokens": 0, "cost_usd": 0.0, "call_count": 0,
                }
            s = summary["by_stage"][stage]
            s["input_tokens"] += inp
            s["output_tokens"] += out
            s["total_tokens"] += inp + out
            s["cost_usd"] += cost
            s["call_count"] += 1

            # by_model
            if model not in summary["by_model"]:
                summary["by_model"][model] = {
                    "input_tokens": 0, "output_tokens": 0,
                    "total_tokens": 0, "cost_usd": 0.0, "call_count": 0,
                }
            m = summary["by_model"][model]
            m["input_tokens"] += inp
            m["output_tokens"] += out
            m["total_tokens"] += inp + out
            m["cost_usd"] += cost
            m["call_count"] += 1

        summary["total_cost_usd"] = round(summary["total_cost_usd"], 6)
        return summary

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


# 全局单例
token_tracker = TokenTracker()
