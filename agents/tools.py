"""
Agent 工具集
为 ProductBriefAgent 提供可调用的原子工具
- deep_inspect_image: 对产品图局部细节再分析
- extract_product_listing_retry: 重试商品链接提取
- web_search_brand: 品牌/竞品/人群信息搜索（占位实现）
- ask_user_clarification: 收集待确认问题（不真实发送）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from prompts.product_analysis import PRODUCT_ANALYSIS_PROMPT

logger = logging.getLogger(__name__)


class AgentTools:
    """Agent 可用工具集合，共享外部服务依赖"""

    def __init__(
        self,
        gemini_service,
        product_image_url: str,
        product_listing_url: Optional[str] = None,
        enable_web_search: bool = False,
    ) -> None:
        self.gemini = gemini_service
        self.product_image_url = product_image_url
        self.product_listing_url = product_listing_url
        self.enable_web_search = enable_web_search

    async def deep_inspect_image(self, focus: str = "") -> dict[str, Any]:
        """对产品图再做一次带聚焦指令的视觉分析

        不做真实 crop（避免新增依赖），而是通过 prompt 指引 Gemini 聚焦特定细节。
        """
        focus_hint = focus.strip() or "any ambiguous materials, logos, or textures"
        logger.info(f"[AgentTool] deep_inspect_image focus={focus_hint}")

        try:
            image_part = await self.gemini._download_as_base64(self.product_image_url, "image/jpeg")
            prompt_text = (
                "Re-inspect this product image and focus ONLY on: "
                f"{focus_hint}.\n\n"
                "Return a concise JSON with keys: {\"focus\": str, \"observations\": [str], "
                "\"corrections\": {\"field_path\": \"corrected_value\"}, \"confidence\": 0-1}. "
                "Do NOT repeat the full product analysis. No markdown fences."
            )
            contents = [{"role": "user", "parts": [image_part, {"text": prompt_text}]}]
            response = await self.gemini._generate_content(contents, context="agent.deep_inspect_image")
            text = self.gemini._extract_text_from_response(response)
            return self.gemini._parse_structured_output(text)
        except Exception as e:
            logger.warning(f"[AgentTool] deep_inspect_image failed: {e}")
            return {"error": str(e), "focus": focus_hint}

    async def extract_product_listing_retry(self) -> dict[str, Any]:
        """重试商品链接提取（跳过缓存）"""
        if not self.product_listing_url:
            return {"error": "no_listing_url"}
        logger.info(f"[AgentTool] extract_product_listing_retry url={self.product_listing_url}")
        try:
            result = await self.gemini.extract_product_listing(
                self.product_listing_url, use_cache=False
            )
            return result or {"error": "empty_result"}
        except Exception as e:
            logger.warning(f"[AgentTool] extract_product_listing_retry failed: {e}")
            return {"error": str(e)}

    async def web_search_brand(self, query: str = "") -> dict[str, Any]:
        """品牌/竞品/目标人群搜索 - 接入 Tavily（主）/ Serper（备）

        - 优先使用 Tavily（返回已提炼的 answer + 摘要，对 LLM 更友好）
        - Tavily 未配置或失败时自动降级到 Serper
        - 两者均未配置时返回明确错误，Agent 会基于已有证据继续推理
        """
        if not self.enable_web_search:
            return {"error": "web_search_disabled", "query": query}
        if not query or not str(query).strip():
            return {"error": "empty_query"}

        from config import settings
        import httpx

        # --- Tavily 优先 ---
        tavily_key = getattr(settings, "TAVILY_API_KEY", "") or ""
        if tavily_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": tavily_key,
                            "query": query,
                            "search_depth": "basic",
                            "max_results": 5,
                            "include_answer": True,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                results = [
                    {
                        "title": it.get("title"),
                        "url": it.get("url"),
                        "content": (it.get("content") or "")[:500],
                    }
                    for it in (data.get("results") or [])
                ]
                logger.info(f"[AgentTool] tavily search ok, results={len(results)} query={query!r}")
                return {
                    "provider": "tavily",
                    "query": query,
                    "answer": data.get("answer"),
                    "results": results,
                }
            except Exception as e:
                logger.warning(f"[AgentTool] tavily search failed, will try serper: {e}")

        # --- Serper 降级 ---
        serper_key = getattr(settings, "SERPER_API_KEY", "") or ""
        if serper_key:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        "https://google.serper.dev/search",
                        headers={
                            "X-API-KEY": serper_key,
                            "Content-Type": "application/json",
                        },
                        json={"q": query, "num": 5},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                results = [
                    {
                        "title": it.get("title"),
                        "url": it.get("link"),
                        "content": (it.get("snippet") or "")[:500],
                    }
                    for it in (data.get("organic") or [])
                ]
                logger.info(f"[AgentTool] serper search ok, results={len(results)} query={query!r}")
                return {"provider": "serper", "query": query, "results": results}
            except Exception as e:
                logger.warning(f"[AgentTool] serper search failed: {e}")
                return {"error": f"serper_failed: {e}", "query": query}

        return {
            "error": "no_provider_configured",
            "query": query,
            "hint": "Set TAVILY_API_KEY or SERPER_API_KEY in .env",
        }

    async def ask_user_clarification(self, items: list[dict[str, Any]] = None) -> dict[str, Any]:
        """收集 Agent 生成的待用户确认问题

        工具返回值仅用于记录，不真实阻塞等待。Phase A 末尾会把 items
        一并写入 Airtable 待确认字段。
        """
        items = items or []
        # 清洗与字段约束
        cleaned: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cleaned.append({
                "field": str(it.get("field", "")).strip(),
                "question": str(it.get("question", "")).strip(),
                "suggestions": [str(s) for s in (it.get("suggestions") or [])][:6],
                "default_value": it.get("default_value"),
            })
        logger.info(f"[AgentTool] ask_user_clarification collected {len(cleaned)} items")
        return {"recorded": True, "items": cleaned}

    async def dispatch(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """根据工具名分发调用。未知工具返回 error。"""
        args = args or {}
        try:
            if tool_name == "deep_inspect_image":
                return await self.deep_inspect_image(focus=args.get("focus", ""))
            if tool_name == "extract_product_listing_retry":
                return await self.extract_product_listing_retry()
            if tool_name == "web_search_brand":
                return await self.web_search_brand(query=args.get("query", ""))
            if tool_name == "ask_user_clarification":
                return await self.ask_user_clarification(items=args.get("items", []))
            return {"error": f"unknown_tool: {tool_name}"}
        except Exception as e:
            logger.exception(f"[AgentTool] dispatch failed for {tool_name}")
            return {"error": str(e), "tool": tool_name}


def safe_truncate_observation(obs: Any, max_chars: int = 4000) -> Any:
    """把观察结果压缩到合理长度，避免下一轮 prompt 过大"""
    try:
        text = json.dumps(obs, ensure_ascii=False)
    except Exception:
        text = str(obs)
    if len(text) <= max_chars:
        return obs
    return {"truncated": True, "preview": text[:max_chars] + "...(truncated)"}
