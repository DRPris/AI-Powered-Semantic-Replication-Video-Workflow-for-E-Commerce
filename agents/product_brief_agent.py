"""
ProductBriefAgent: 商品分析 Agent 主类
- Phase A (preflight): 视觉+链接初步分析 -> Agent Loop 补全 -> 产出 ProductBrief 草稿 + 待确认问题
- Phase B (finalize): 读取用户答复 -> Agent Loop 继续补全 -> 产出最终 ProductBrief

设计要点：
- 不引入外部 agent 框架，使用 Gemini + 自建 ReAct-lite 循环
- Agent 每轮输出严格 JSON，包含 next_action (tool_call | finish) 和最终 brief
- 任何异常都降级为返回基于现有证据拼装的 brief，不中断工作流
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from config import settings
from models.schemas import ProductBrief, ClarificationItem
from prompts.product_brief_agent import (
    PRODUCT_BRIEF_AGENT_SYSTEM_PROMPT,
    format_agent_round_prompt,
)
from agents.tools import AgentTools, safe_truncate_observation

logger = logging.getLogger(__name__)


class ProductBriefAgent:
    """商品分析 Agent 主类

    典型用法：
        agent = ProductBriefAgent(gemini, product_image_url, product_listing_url=url)
        brief = await agent.run_preflight()                # Phase A
        # ... 用户答复后 ...
        brief = await agent.run_finalize(brief, user_answers)  # Phase B
    """

    def __init__(
        self,
        gemini_service,
        product_image_url: str,
        product_listing_url: Optional[str] = None,
        max_loops: Optional[int] = None,
        timeout_sec: Optional[int] = None,
        enable_web_search: Optional[bool] = None,
    ) -> None:
        self.gemini = gemini_service
        self.product_image_url = product_image_url
        self.product_listing_url = product_listing_url
        self.max_loops = max_loops if max_loops is not None else settings.PRODUCT_AGENT_MAX_LOOPS
        self.timeout_sec = timeout_sec if timeout_sec is not None else settings.PRODUCT_AGENT_TIMEOUT_SEC
        self.enable_web_search = (
            enable_web_search
            if enable_web_search is not None
            else settings.PRODUCT_AGENT_ENABLE_WEB_SEARCH
        )
        self.tools = AgentTools(
            gemini_service=gemini_service,
            product_image_url=product_image_url,
            product_listing_url=product_listing_url,
            enable_web_search=self.enable_web_search,
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run_preflight(
        self,
        preliminary_analysis: Optional[dict] = None,
        product_listing_info: Optional[dict] = None,
    ) -> ProductBrief:
        """Phase A: 预检 - 产出 Brief 草稿 + clarification_items

        如果外部已并行执行过 analyze_product / extract_product_listing，可把结果传入
        避免重复调用；否则内部自动补齐。
        """
        # 1) 补齐初步视觉分析（如未传入）
        if preliminary_analysis is None:
            try:
                preliminary_analysis = await self.gemini.analyze_product(self.product_image_url)
            except Exception as e:
                logger.warning(f"[ProductBriefAgent] preliminary analyze_product failed: {e}")
                preliminary_analysis = {}

        # 2) 补齐链接信息（如未传入且 url 存在）
        if product_listing_info is None and self.product_listing_url:
            try:
                product_listing_info = await self.gemini.extract_product_listing(
                    self.product_listing_url
                )
            except Exception as e:
                logger.warning(f"[ProductBriefAgent] preliminary extract_product_listing failed: {e}")
                product_listing_info = None

        # 3) 跑 Agent Loop
        try:
            brief = await asyncio.wait_for(
                self._run_loop(
                    preliminary_analysis=preliminary_analysis or {},
                    product_listing_info=product_listing_info,
                    user_answers=None,
                ),
                timeout=self.timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning("[ProductBriefAgent] preflight timeout, falling back to rule-based brief")
            brief = self._fallback_brief(preliminary_analysis or {}, product_listing_info)
        except Exception as e:
            logger.warning(f"[ProductBriefAgent] preflight error, falling back: {e}")
            brief = self._fallback_brief(preliminary_analysis or {}, product_listing_info)

        # 4) 判定 phase：有 clarification 且启用确认 -> awaiting_user；否则 finalized
        if brief.clarification_items and settings.PRODUCT_AGENT_REQUIRE_USER_CONFIRMATION:
            brief.phase = "awaiting_user"
        else:
            brief.phase = "finalized"
        return brief

    async def run_finalize(
        self,
        draft_brief: ProductBrief,
        user_answers: Optional[dict[str, Any]] = None,
    ) -> ProductBrief:
        """Phase B: 基于用户答复继续补全，产出最终 Brief"""
        preliminary = {
            "layer_0_component_decomposition": {"components": draft_brief.core_components},
            "layer_1_physical_attributes": draft_brief.physical_attrs,
            "layer_2_operation_mechanics": draft_brief.operation_mechanics,
            "layer_3_use_effect": draft_brief.use_effect,
            "_draft_brief": draft_brief.model_dump(),
        }
        try:
            brief = await asyncio.wait_for(
                self._run_loop(
                    preliminary_analysis=preliminary,
                    product_listing_info=None,
                    user_answers=user_answers or {},
                ),
                timeout=self.timeout_sec,
            )
        except Exception as e:
            logger.warning(f"[ProductBriefAgent] finalize error, keeping draft with user answers merged: {e}")
            brief = self._merge_user_answers(draft_brief, user_answers or {})

        brief.phase = "finalized"
        # 保留原有 sources 中的 user 标记
        if user_answers and "user" not in brief.sources:
            brief.sources.append("user")
        return brief

    # ------------------------------------------------------------------
    # Core Loop
    # ------------------------------------------------------------------

    async def _run_loop(
        self,
        preliminary_analysis: dict,
        product_listing_info: Optional[dict],
        user_answers: Optional[dict],
    ) -> ProductBrief:
        """Agent ReAct-lite 循环

        - 每轮向 Gemini 发送 system + 累积 state
        - 解析输出的 next_action，执行工具或结束
        - 最多 max_loops 轮
        """
        tool_observations: list[dict[str, Any]] = []
        last_called: set[str] = set()
        consecutive_no_progress = 0

        for round_idx in range(1, self.max_loops + 1):
            user_prompt = format_agent_round_prompt(
                round_index=round_idx,
                max_rounds=self.max_loops,
                preliminary_analysis=preliminary_analysis,
                product_listing_info=product_listing_info,
                tool_observations=tool_observations,
                user_answers=user_answers,
                enable_web_search=self.enable_web_search,
                listing_url_available=bool(getattr(self.tools, "product_listing_url", None)),
            )

            # Gemini 调用：使用 system_instruction 注入 system prompt
            contents = [
                {"role": "user", "parts": [{"text": PRODUCT_BRIEF_AGENT_SYSTEM_PROMPT}]},
                {"role": "model", "parts": [{"text": "Understood. I will output strict JSON only."}]},
                {"role": "user", "parts": [{"text": user_prompt}]},
            ]

            try:
                response = await self.gemini._generate_content(
                    contents,
                    context=f"product_brief_agent.round_{round_idx}",
                )
                text = self.gemini._extract_text_from_response(response)
                decision = self.gemini._parse_structured_output(text)
            except Exception as e:
                logger.warning(f"[ProductBriefAgent] round {round_idx} LLM call failed: {e}")
                break

            next_action = (decision or {}).get("next_action") or {}
            action_type = next_action.get("type")
            logger.info(
                f"[ProductBriefAgent] round {round_idx} thought={decision.get('thought', '')[:120]} "
                f"action={action_type}"
            )

            if action_type == "finish":
                brief_data = decision.get("product_brief") or {}
                return self._build_brief_from_dict(brief_data, preliminary_analysis, product_listing_info)

            if action_type != "tool_call":
                logger.warning(f"[ProductBriefAgent] invalid action type: {action_type}, finishing")
                break

            tool_name = next_action.get("tool", "")
            args = next_action.get("args", {}) or {}
            dedup_key = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            if dedup_key in last_called:
                logger.info(f"[ProductBriefAgent] tool already called with same args, forcing finish")
                break
            last_called.add(dedup_key)

            observation = await self.tools.dispatch(tool_name, args)
            observation_compact = safe_truncate_observation(observation)
            tool_observations.append({
                "round": round_idx,
                "tool": tool_name,
                "args": args,
                "observation": observation_compact,
            })

            # 无进展检测：observation 带 error 视为无进展
            if isinstance(observation, dict) and observation.get("error"):
                consecutive_no_progress += 1
            else:
                consecutive_no_progress = 0
            if consecutive_no_progress >= 2:
                logger.info("[ProductBriefAgent] 2 consecutive no-progress rounds, finishing")
                break

        # 循环结束但未 finish，降级用累积的观察构造 brief
        logger.info("[ProductBriefAgent] loop exhausted, building fallback brief from observations")
        return self._fallback_brief(preliminary_analysis, product_listing_info, tool_observations)

    # ------------------------------------------------------------------
    # Brief construction helpers
    # ------------------------------------------------------------------

    def _build_brief_from_dict(
        self,
        brief_data: dict,
        preliminary_analysis: dict,
        product_listing_info: Optional[dict],
    ) -> ProductBrief:
        """将 Agent 输出的 brief dict 转换为 ProductBrief，并合并 clarification_items"""
        clarifications = self._collect_clarifications(brief_data)
        # 基础字段从 preliminary 兜底
        layer0 = (preliminary_analysis or {}).get("layer_0_component_decomposition", {}) or {}
        layer1 = (preliminary_analysis or {}).get("layer_1_physical_attributes", {}) or {}
        layer2 = (preliminary_analysis or {}).get("layer_2_operation_mechanics", {}) or {}
        layer3 = (preliminary_analysis or {}).get("layer_3_use_effect", {}) or {}

        try:
            return ProductBrief(
                product_name=str(brief_data.get("product_name") or (product_listing_info or {}).get("product_name") or ""),
                brand=brief_data.get("brand"),
                category=str(brief_data.get("category") or (product_listing_info or {}).get("category") or ""),
                core_components=brief_data.get("core_components") or layer0.get("components") or [],
                physical_attrs=brief_data.get("physical_attrs") or layer1 or {},
                operation_mechanics=brief_data.get("operation_mechanics") or layer2 or {},
                use_effect=brief_data.get("use_effect") or layer3 or {},
                usage_instructions=list(brief_data.get("usage_instructions") or
                                         (product_listing_info or {}).get("usage_instructions") or [])[:12],
                key_selling_points=list(brief_data.get("key_selling_points") or
                                         (product_listing_info or {}).get("key_selling_points") or [])[:8],
                target_audience=str(brief_data.get("target_audience") or ""),
                tone=str(brief_data.get("tone") or ""),
                competitor_differentiators=list(brief_data.get("competitor_differentiators") or [])[:8],
                constraints=list(brief_data.get("constraints") or [])[:10],
                product_video_url=(product_listing_info or {}).get("product_video_url"),
                product_video_analysis=(product_listing_info or {}).get("product_video_analysis"),
                confidence_score=float(brief_data.get("confidence_score") or 0.0),
                info_gaps=list(brief_data.get("info_gaps") or [])[:10],
                sources=list(brief_data.get("sources") or ["image"])[:10],
                clarification_items=clarifications,
                phase="draft",
            )
        except Exception as e:
            logger.warning(f"[ProductBriefAgent] build_brief_from_dict validation failed: {e}")
            return self._fallback_brief(preliminary_analysis, product_listing_info)

    def _collect_clarifications(self, brief_data: dict) -> list[ClarificationItem]:
        """从 Agent 的 tool_observations 中收集 ask_user_clarification 的记录"""
        items: list[ClarificationItem] = []
        # Agent 可能把 clarification 直接放 brief，也可能通过 tool 记录
        raw_items = brief_data.get("clarification_items") or []
        for it in raw_items:
            try:
                items.append(ClarificationItem(**it))
            except Exception:
                continue
        return items

    def _fallback_brief(
        self,
        preliminary_analysis: dict,
        product_listing_info: Optional[dict],
        tool_observations: Optional[list] = None,
    ) -> ProductBrief:
        """基于已有证据兜底构造 Brief，不依赖 Agent 输出"""
        layer0 = (preliminary_analysis or {}).get("layer_0_component_decomposition", {}) or {}
        layer1 = (preliminary_analysis or {}).get("layer_1_physical_attributes", {}) or {}
        layer2 = (preliminary_analysis or {}).get("layer_2_operation_mechanics", {}) or {}
        layer3 = (preliminary_analysis or {}).get("layer_3_use_effect", {}) or {}
        listing = product_listing_info or {}

        sources = ["image"] if preliminary_analysis else []
        if listing:
            sources.append("listing")
        if listing.get("product_video_analysis"):
            sources.append("product_video")

        info_gaps = []
        # 优先从视频差分分析的 new_info_not_in_listing 吸取，其次是 listing
        video_analysis = listing.get("product_video_analysis") or {}
        video_new_info = list(video_analysis.get("new_info_not_in_listing") or [])
        selling_points = list(listing.get("key_selling_points") or [])
        merged_selling_points: list = []
        for sp in video_new_info + selling_points:
            if sp and sp not in merged_selling_points:
                merged_selling_points.append(sp)
        if len(merged_selling_points) < 3:
            info_gaps.append("key_selling_points")

        # 从 observations 中吸收 clarification
        clarifications: list[ClarificationItem] = []
        if tool_observations:
            for obs in tool_observations:
                if obs.get("tool") == "ask_user_clarification":
                    items = (obs.get("observation") or {}).get("items") or []
                    for it in items:
                        try:
                            clarifications.append(ClarificationItem(**it))
                        except Exception:
                            continue

        return ProductBrief(
            product_name=str(listing.get("product_name") or ""),
            brand=listing.get("brand"),
            category=str(listing.get("category") or ""),
            core_components=layer0.get("components") or [],
            physical_attrs=layer1 or {},
            operation_mechanics=layer2 or {},
            use_effect=layer3 or {},
            usage_instructions=list(listing.get("usage_instructions") or [])[:12],
            key_selling_points=merged_selling_points[:8],
            target_audience=(video_analysis.get("target_audience_visual_cues") or [""])[0] if video_analysis else "",
            tone=((video_analysis.get("tone_hints") or {}).get("style") or "") if video_analysis else "",
            competitor_differentiators=(
                list(video_analysis.get("pain_points_shown") or [])[:3]
                + list(video_analysis.get("new_info_not_in_listing") or [])[:2]
            ) if video_analysis else [],
            constraints=[],
            product_video_url=listing.get("product_video_url"),
            product_video_analysis=listing.get("product_video_analysis"),
            confidence_score=(0.55 if video_analysis else (0.4 if listing else 0.3)),
            info_gaps=info_gaps + (["target_audience", "tone"] if not video_analysis else []),
            sources=sources,
            clarification_items=clarifications,
            phase="draft",
        )

    def _merge_user_answers(self, draft: ProductBrief, user_answers: dict) -> ProductBrief:
        """当 finalize Loop 异常时，直接把用户答复合并进 draft"""
        updated = draft.model_copy(deep=True)
        for field, value in (user_answers or {}).items():
            if not hasattr(updated, field):
                continue
            try:
                setattr(updated, field, value)
            except Exception:
                continue
        if user_answers and "user" not in updated.sources:
            updated.sources.append("user")
        return updated
