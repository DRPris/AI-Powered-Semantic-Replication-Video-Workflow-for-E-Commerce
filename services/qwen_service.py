"""
Qwen (通义千问) 服务封装
通过阿里云百炼平台（DashScope）的 OpenAI 兼容模式调用 Qwen 系列模型。
当前用途：分镜提示词物理一致性自审核。
"""

import json
import logging
import re
from typing import Any, Optional

import httpx

from config import settings
from prompts.shot_prompt_audit import format_shot_prompt_audit_prompt
from prompts.ambient_extraction import format_ambient_extraction_prompt
from services.token_tracker import token_tracker

logger = logging.getLogger(__name__)


class QwenService:
    """通义千问 AI 服务（百炼 OpenAI 兼容模式）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.QWEN_API_KEY
        if not self.api_key:
            raise ValueError(
                "Qwen API key is required (set QWEN_API_KEY in .env, "
                "obtain from https://bailian.console.aliyun.com/)"
            )

        self.model_name = model_name or settings.QWEN_MODEL or "qwen-plus"
        self.base_url = (base_url or settings.QWEN_BASE_URL or
                         "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")

        # Token 追踪上下文
        self._tracking_project_id: Optional[str] = None
        self._tracking_stage: Optional[str] = None

        logger.info(
            f"QwenService initialized: model={self.model_name}, base_url={self.base_url}"
        )

    def set_context(self, project_id: str, stage: str) -> None:
        """设置当前追踪上下文（project_id + stage），供 Token 统计使用"""
        self._tracking_project_id = project_id
        self._tracking_stage = stage

    async def _chat_completion(
        self,
        prompt: str,
        context: str = "",
        temperature: float = 0.2,
        response_json: bool = True,
        timeout: float = 120.0,
    ) -> str:
        """
        调用百炼 OpenAI 兼容模式的 chat/completions 接口，返回模型输出文本。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        if response_json:
            # 百炼 OpenAI 兼容模式支持 response_format=json_object
            payload["response_format"] = {"type": "json_object"}

        logger.info(
            f"Qwen request: model={self.model_name}, context={context or 'n/a'}, "
            f"prompt_len={len(prompt)} chars, response_json={response_json}"
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.error(
                    f"Qwen API HTTP {resp.status_code}: {resp.text[:500]}"
                )
                resp.raise_for_status()
            data = resp.json()

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Qwen response missing choices/content: {e}; raw={data}")

        usage = data.get("usage") or {}
        if usage:
            logger.info(
                f"Qwen usage [{context or 'n/a'}]: "
                f"prompt_tokens={usage.get('prompt_tokens')}, "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"total_tokens={usage.get('total_tokens')}"
            )
            # 写入 TokenTracker
            if self._tracking_project_id:
                token_tracker.record(
                    project_id=self._tracking_project_id,
                    stage=self._tracking_stage or "unknown",
                    call_type=context or "qwen_chat",
                    model=self.model_name,
                    input_tokens=usage.get('prompt_tokens', 0),
                    output_tokens=usage.get('completion_tokens', 0),
                    cached=False,
                )

        return text

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """尽力解析 JSON：支持裸 JSON、```json 代码块、以及残留 code fence 的情况。"""
        if not text:
            return {}
        # 去除可能的 ```json ... ``` 包裹
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
        if fence:
            text = fence.group(1)
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 退一步：截取首个 { 到最后一个 } 之间的内容
            first = text.find("{")
            last = text.rfind("}")
            if first >= 0 and last > first:
                try:
                    return json.loads(text[first : last + 1])
                except Exception:
                    pass
            raise

    async def _chat_multimodal(
        self,
        prompt: str,
        image_urls: list[str],
        model: Optional[str] = None,
        context: str = "",
        temperature: float = 0.2,
        response_json: bool = True,
        timeout: float = 120.0,
    ) -> str:
        """
        调用百炼 OpenAI 兼容模式的 vision 多模态接口。
        messages[].content 支持 text + image_url 数组结构。
        支持传入公网 URL（http/https），也支持 data:image/...;base64,xxx。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 默认使用 VL 模型（除非主动指定）
        vl_model = model or getattr(settings, "QWEN_VL_MODEL", None) or "qwen-vl-plus"

        # 构造多模态 content：先图后文本（百炼 VL 推荐顺序）
        content: list[dict[str, Any]] = []
        for img in image_urls or []:
            if not img:
                continue
            content.append({"type": "image_url", "image_url": {"url": img}})
        content.append({"type": "text", "text": prompt})

        payload: dict[str, Any] = {
            "model": vl_model,
            "messages": [
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
        }
        if response_json:
            payload["response_format"] = {"type": "json_object"}

        logger.info(
            f"Qwen multimodal request: model={vl_model}, context={context or 'n/a'}, "
            f"images={len([c for c in content if c.get('type') == 'image_url'])}, "
            f"prompt_len={len(prompt)} chars, response_json={response_json}"
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.error(
                    f"Qwen VL API HTTP {resp.status_code}: {resp.text[:500]}"
                )
                resp.raise_for_status()
            data = resp.json()

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Qwen VL response missing choices/content: {e}; raw={data}")

        usage = data.get("usage") or {}
        if usage:
            logger.info(
                f"Qwen VL usage [{context or 'n/a'}]: "
                f"prompt_tokens={usage.get('prompt_tokens')}, "
                f"completion_tokens={usage.get('completion_tokens')}, "
                f"total_tokens={usage.get('total_tokens')}"
            )
            if self._tracking_project_id:
                token_tracker.record(
                    project_id=self._tracking_project_id,
                    stage=self._tracking_stage or "unknown",
                    call_type=context or "qwen_vl_chat",
                    model=vl_model,
                    input_tokens=usage.get('prompt_tokens', 0),
                    output_tokens=usage.get('completion_tokens', 0),
                    cached=False,
                )

        # VL 输出可能是字符串或 list（OpenAI 兼容模式下通常是字符串）
        if isinstance(text, list):
            # 合并所有 text 分块
            joined = "".join(
                (item.get("text", "") if isinstance(item, dict) else str(item))
                for item in text
            )
            return joined
        return text

    async def audit_text(
        self,
        prompt: str,
        context: str = "audit_text",
        temperature: float = 0.1,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """
        文本审查通用入口，用于 1.1（视频分析）/ 1.2（商品分析）。
        约定 prompt 输出 JSON：
            {
              "passed": bool,
              "confidence": 0.0-1.0,
              "critical_issues": [str],
              "warnings": [str],
              "reason_summary": str
            }
        解析失败 / 调用异常时返回开箱 passed=False，confidence=0.0，错误写入 critical_issues，
        即失败即转人审（不接纳 “审查异常全通过” 的兔底语义）。
        """
        try:
            text = await self._chat_completion(
                prompt=prompt,
                context=context,
                temperature=temperature,
                response_json=True,
                timeout=timeout,
            )
            data = self._parse_json(text)
            if not isinstance(data, dict):
                raise ValueError(f"audit_text returned non-dict: {data!r}")
            return self._normalize_audit_result(data)
        except Exception as e:
            logger.warning(f"Qwen audit_text failed [{context}]: {e}")
            return {
                "passed": False,
                "confidence": 0.0,
                "critical_issues": [f"审查调用异常: {type(e).__name__}: {e}"],
                "warnings": [],
                "reason_summary": "Qwen 审查调用失败，自动转人审",
            }

    async def audit_images(
        self,
        prompt: str,
        image_urls: list[str],
        context: str = "audit_images",
        temperature: float = 0.1,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        """
        图片审查通用入口，用于 3.5（关键帧）/ 4.4（生成视频抽帧）。
        输出结构与 audit_text 完全一致。
        """
        try:
            text = await self._chat_multimodal(
                prompt=prompt,
                image_urls=image_urls or [],
                context=context,
                temperature=temperature,
                response_json=True,
                timeout=timeout,
            )
            data = self._parse_json(text)
            if not isinstance(data, dict):
                raise ValueError(f"audit_images returned non-dict: {data!r}")
            return self._normalize_audit_result(data)
        except Exception as e:
            logger.warning(f"Qwen audit_images failed [{context}]: {e}")
            return {
                "passed": False,
                "confidence": 0.0,
                "critical_issues": [f"审查调用异常: {type(e).__name__}: {e}"],
                "warnings": [],
                "reason_summary": "Qwen VL 审查调用失败，自动转人审",
            }

    @staticmethod
    def _normalize_audit_result(data: dict[str, Any]) -> dict[str, Any]:
        """对齐与 AuditResult 字段，对缺失字段做安全兵底。"""
        def _as_list(v: Any) -> list[str]:
            if v is None:
                return []
            if isinstance(v, str):
                return [v] if v.strip() else []
            if isinstance(v, list):
                return [str(x) for x in v if x]
            return [str(v)]

        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        return {
            "passed": bool(data.get("passed", False)),
            "confidence": confidence,
            "critical_issues": _as_list(data.get("critical_issues")),
            "warnings": _as_list(data.get("warnings")),
            "reason_summary": str(data.get("reason_summary") or "").strip(),
        }

    async def audit_shot_prompts(
        self,
        shots: list[dict[str, Any]],
        product_analysis: str = "",
        product_listing_info: str = "",
        video_analysis: str = "",
        script: str = "",
    ) -> dict[str, Any]:
        """
        使用 Qwen 对 Stage 3 生成的逐镜头提示词做物理一致性审核。
        接口契约与 GeminiService.audit_shot_prompts 保持一致，便于调用方无缝切换。

        Returns:
            {"shots": [{shot_number, passed, critical_issues, warnings, reason_summary}, ...]}
            解析/调用失败时兜底返回 {"shots": []}，由调用方按 "全部通过" 处理。
        """
        logger.info(f"Qwen: starting shot prompt audit for {len(shots)} shots")

        if not shots:
            return {"shots": []}

        try:
            shots_str = json.dumps(shots, ensure_ascii=False, indent=2)
            prompt = format_shot_prompt_audit_prompt(
                shots=shots_str,
                product_analysis=product_analysis or "",
                product_listing_info=product_listing_info or "",
                video_analysis=video_analysis or "",
                script=script or "",
            )

            text = await self._chat_completion(
                prompt=prompt,
                context="shot_prompt_audit",
                temperature=0.1,
                response_json=True,
            )

            result = self._parse_json(text)
            audited = result.get("shots", []) if isinstance(result, dict) else []
            if not isinstance(audited, list):
                logger.warning(
                    "Qwen shot prompt audit returned non-list 'shots' field, treating as empty"
                )
                audited = []

            rejected = [s for s in audited if not s.get("passed", True)]
            warned = [s for s in audited if s.get("passed", True) and s.get("warnings")]
            logger.info(
                f"Qwen shot prompt audit completed: total={len(audited)}, "
                f"rejected={len(rejected)}, warnings_only={len(warned)}"
            )
            if rejected:
                rejected_numbers = [s.get("shot_number") for s in rejected]
                logger.warning(
                    f"Qwen shot prompt audit rejected shots: {rejected_numbers}"
                )

            return {"shots": audited}

        except Exception as e:
            logger.warning(f"Qwen shot prompt audit failed (non-blocking): {e}")
            return {"shots": []}

    async def extract_ambient_prompt(
        self,
        visual_prompt: str,
        extra_context: str = "",
    ) -> str:
        """
        从单个镜头的视觉 prompt 中抽取环境音描述（英文单句）。

        Returns:
            例如 "coffee shop chatter, espresso machine hiss"
            解析失败或 prompt 为空时，返回堆底 "subtle room tone, soft ambience"。
        """
        fallback = "subtle room tone, soft ambience"
        if not visual_prompt or not visual_prompt.strip():
            return fallback

        try:
            prompt = format_ambient_extraction_prompt(
                visual_prompt=visual_prompt,
                extra_context=extra_context,
            )
            text = await self._chat_completion(
                prompt=prompt,
                context="ambient_extraction",
                temperature=0.2,
                response_json=True,
                timeout=60.0,
            )
            data = self._parse_json(text)
            ambient = (data.get("ambient_prompt") or "").strip() if isinstance(data, dict) else ""
            if not ambient:
                logger.warning(
                    "Qwen ambient extraction returned empty 'ambient_prompt', using fallback"
                )
                return fallback
            # 防御：过滤不应出现的词
            lower = ambient.lower()
            forbidden = ("voiceover", "narration", "dialogue", "speech", "music", " bgm", "score")
            if any(f in lower for f in forbidden):
                logger.warning(
                    f"Qwen ambient prompt contains forbidden token, using fallback: {ambient}"
                )
                return fallback
            return ambient
        except Exception as e:
            logger.warning(f"Qwen ambient extraction failed (non-blocking): {e}")
            return fallback
