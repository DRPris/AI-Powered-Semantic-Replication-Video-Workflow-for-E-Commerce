"""
Gemini API 服务封装 (REST API 版本)
提供视频分析、商品分析、脚本生成、提示词转换等功能
"""

import asyncio
import base64
import json
import logging
import os
import random
import tempfile
from typing import Any, Optional

import httpx
import requests as sync_requests

from prompts.video_analysis import VIDEO_ANALYSIS_PROMPT
from prompts.product_analysis import PRODUCT_ANALYSIS_PROMPT
from prompts.product_video_analysis import (
    PRODUCT_VIDEO_ANALYSIS_PROMPT,
    format_product_video_analysis_prompt,
)
from prompts.rhythm_analysis import RHYTHM_ANALYSIS_PROMPT
from prompts.script_replication import format_script_replication_prompt
from prompts.prompt_conversion import format_prompt_conversion
from prompts.product_listing_extraction import format_product_listing_extraction_prompt
from prompts.action_compatibility import format_action_compatibility_prompt
from prompts.script_validation import format_script_validation_prompt
from prompts.script_fix import format_script_fix_prompt
from prompts.shot_prompt_audit import format_shot_prompt_audit_prompt
from config import settings
from services.token_utils import (
    get_cache_key, get_cache, set_cache, 
    log_token_usage, compress_video_analysis, compress_product_analysis
)
from services.token_tracker import token_tracker
from services.image_utils import remove_background

logger = logging.getLogger(__name__)


def _trim_video_analysis_for_ambient(video_analysis: dict[str, Any]) -> dict[str, Any]:
    """
    为镜头内环境音三步决策法精选原视频分析字段，避免 Token 浪费。
    仅保留：shots[].{shot_number, audio.sound_effects, audio.music,
    environment.background / surface_floor}.
    """
    try:
        shots = video_analysis.get("shots") or []
        trimmed_shots = []
        for s in shots:
            if not isinstance(s, dict):
                continue
            audio = s.get("audio") or {}
            env = s.get("environment") or {}
            trimmed_shots.append({
                "shot_number": s.get("shot_number"),
                "audio": {
                    "sound_effects": audio.get("sound_effects", ""),
                    "music": audio.get("music", ""),
                },
                "environment": {
                    "background": env.get("background", ""),
                },
            })
        return {"shots": trimmed_shots}
    except Exception as e:
        logger.warning(f"_trim_video_analysis_for_ambient fallback (pass empty): {e}")
        return {"shots": []}


class GeminiService:
    """Gemini AI 服务类 - 使用 REST API"""

    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None) -> None:
        """
        初始化 Gemini 服务

        Args:
            api_key: Gemini API 密钥，如果不提供则使用配置中的密钥
            model_name: 模型名称，默认为 gemini-3-flash-preview
        """
        self.api_key = api_key or settings.GEMINI_API_KEY
        if not self.api_key:
            raise ValueError("Gemini API key is required")

        # 配置模型
        self.model_name = model_name or "gemini-3-flash-preview"
        self.image_gen_model = "gemini-2.5-flash-image"

        # Base URL
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

        # 初始化代理配置 (使用 requests + socks5h 确保 DNS 也走代理，避免 Gemini 地域限制)
        self.proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or "socks5h://127.0.0.1:13659"
        # 确保使用 socks5h (远程 DNS 解析) 而非 socks5
        if self.proxy_url.startswith("socks5://"):
            self.proxy_url = self.proxy_url.replace("socks5://", "socks5h://", 1)
        logger.info(f"GeminiService using proxy: {self.proxy_url}")
        
        self._proxies = {"https": self.proxy_url, "http": self.proxy_url} if self.proxy_url else None

        # Token 追踪上下文
        self._tracking_project_id: Optional[str] = None
        self._tracking_stage: Optional[str] = None

        logger.info(f"GeminiService initialized with model: {self.model_name}")

    def set_context(self, project_id: str, stage: str) -> None:
        """设置当前追踪上下文（project_id + stage），供 Token 统计使用"""
        self._tracking_project_id = project_id
        self._tracking_stage = stage

    async def close(self) -> None:
        """无需清理 (使用无状态 requests 调用)"""
        pass

    async def _download_as_base64(self, url: str, mime_type: str) -> dict[str, Any]:
        """
        下载文件并转为 inline_data 格式（base64）
        使用 inline_data 方式避免 Gemini File API 的地区限制

        Args:
            url: 文件 URL
            mime_type: MIME 类型

        Returns:
            inline_data part dict: {"inline_data": {"mime_type": ..., "data": ...}}
        """
        logger.info(f"Downloading file from: {url}")

        # 下载文件（不走代理，OSS 在阿里郎 VPN 例外列表中）
        # follow_redirects=True：兼容 Lazada/阿里系 CDN 的 302 跳转直链（cloud.video.lazada.com → lazvideo.lazcdn.com）
        async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(120.0), follow_redirects=True) as download_client:
            response = await download_client.get(url)
            response.raise_for_status()

        file_size = len(response.content)
        file_b64 = base64.b64encode(response.content).decode("utf-8")
        logger.info(f"File downloaded: {file_size} bytes, base64 encoded for inline_data")

        return {"inline_data": {"mime_type": mime_type, "data": file_b64}}

    async def _generate_content(
        self,
        contents: list[dict[str, Any]],
        model: Optional[str] = None,
        generation_config: Optional[dict[str, Any]] = None,
        context: str = "api_call",
        max_retries: int = 6,
    ) -> dict[str, Any]:
        """
        调用 Gemini generateContent API（含自动重试）

        Args:
            contents: 请求内容
            model: 模型名称，默认使用 self.model_name
            generation_config: 生成配置，如 responseModalities 等
            context: 调用上下文描述，用于 Token 统计日志
            max_retries: 最大重试次数，默认6次（VPN不稳定需要更多重试）

        Returns:
            API 响应的 JSON
        """
        model_name = model or self.model_name
        url = f"{self.base_url}/models/{model_name}:generateContent?key={self.api_key}"

        payload = {"contents": contents}
        if generation_config:
            payload["generationConfig"] = generation_config

        # Token 统计 - 请求前估算
        log_token_usage(f"{context} (model={model_name})", contents)

        last_error = None
        for attempt in range(max_retries):
            try:
                # 使用 requests + socks5h 确保 DNS 通过代理解析，避免 Gemini 地域限制
                response = await asyncio.to_thread(
                    sync_requests.post,
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    proxies=self._proxies,
                    timeout=300,
                )
                if response.status_code >= 400:
                    logger.error(f"Gemini API error response: {response.text}")
                    response.raise_for_status()

                result = response.json()
                
                # Token 统计 - 响应后记录实际消耗
                usage = result.get("usageMetadata", {})
                if usage:
                    input_tokens = usage.get('promptTokenCount', 0)
                    output_tokens = usage.get('candidatesTokenCount', 0)
                    logger.info(
                        f"[Token] {context}: actual input={input_tokens}, "
                        f"output={output_tokens}, total={usage.get('totalTokenCount')}"
                    )
                    # 写入 TokenTracker
                    if self._tracking_project_id:
                        token_tracker.record(
                            project_id=self._tracking_project_id,
                            stage=self._tracking_stage or "unknown",
                            call_type=context,
                            model=model_name,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached=False,
                        )

                return result
            except (sync_requests.exceptions.ConnectionError, sync_requests.exceptions.Timeout, 
                    sync_requests.exceptions.HTTPError) as e:
                last_error = e
                # 指数退避 + 随机抖动: 5-8s, 10-16s, 15-24s, 20-32s, 25-40s...
                base_wait = 5 * (attempt + 1)
                jitter = random.uniform(0, base_wait * 0.6)
                wait_time = base_wait + jitter
                logger.warning(
                    f"[{context}] Gemini API request failed (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {wait_time:.1f}s..."
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)

        raise last_error

    def _extract_text_from_response(self, response: dict[str, Any]) -> str:
        """
        从 API 响应中提取文本内容

        Args:
            response: API 响应 JSON

        Returns:
            提取的文本
        """
        candidates = response.get("candidates", [])
        if not candidates:
            raise ValueError("No candidates in response")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        text_parts = []
        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])

        return "\n".join(text_parts)

    def _extract_images_from_response(self, response: dict[str, Any]) -> list[str]:
        """
        从 API 响应中提取图片
        支持两种格式：inline_data (蛇形) 和 inlineData (驼峰)

        Args:
            response: API 响应 JSON

        Returns:
            base64 编码的图片列表
        """
        images = []
        candidates = response.get("candidates", [])
        if not candidates:
            return images

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        for part in parts:
            # 支持两种格式：inline_data (蛇形) 和 inlineData (驼峰)
            inline_data = part.get("inline_data") or part.get("inlineData")
            if inline_data:
                # 同样支持两种格式的 mime_type
                mime_type = inline_data.get("mime_type") or inline_data.get("mimeType") or "image/png"
                data = inline_data.get("data", "")
                if data:
                    images.append(f"data:{mime_type};base64,{data}")

        return images

    def _parse_structured_output(self, text: str) -> dict[str, Any]:
        """
        解析 Gemini 的文本输出为结构化 JSON

        Args:
            text: Gemini 返回的文本内容

        Returns:
            解析后的 JSON 对象

        Raises:
            ValueError: 解析失败时抛出
        """
        try:
            # 尝试直接解析
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 代码块
        try:
            # 查找 ```json 和 ``` 之间的内容
            if "```json" in text:
                start = text.find("```json") + 7
                end = text.find("```", start)
                json_str = text[start:end].strip()
                return json.loads(json_str)
            # 查找 ``` 和 ``` 之间的内容
            elif "```" in text:
                start = text.find("```") + 3
                end = text.find("```", start)
                json_str = text[start:end].strip()
                return json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试查找 JSON 对象的开始和结束
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

        raise ValueError(f"Failed to parse structured output: {text[:200]}...")

    async def analyze_video(self, video_url: str, use_cache: bool = True) -> dict[str, Any]:
        """
        分析原始视频，返回逐镜头分析结果
        支持缓存：相同视频 URL 不重复分析

        Args:
            video_url: 视频 URL
            use_cache: 是否使用缓存，默认 True

        Returns:
            结构化分析结果，包含 shots 列表

        Raises:
            ValueError: 解析失败时抛出
            httpx.HTTPError: API 调用失败时抛出
        """
        logger.info(f"Starting video analysis for: {video_url}")

        # 检查缓存
        if use_cache:
            cache_key = get_cache_key(video_url, "video_analysis")
            cached = get_cache(cache_key)
            if cached:
                logger.info(f"Video analysis loaded from cache, found {len(cached.get('shots', []))} shots")
                if self._tracking_project_id:
                    token_tracker.record(
                        project_id=self._tracking_project_id,
                        stage=self._tracking_stage or "unknown",
                        call_type="video_analysis", model=self.model_name,
                        input_tokens=0, output_tokens=0, cached=True,
                    )
                return cached

        try:
            # 下载视频并转为 base64 inline_data
            video_part = await self._download_as_base64(video_url, "video/mp4")

            # 构建请求内容
            contents = [
                {
                    "role": "user",
                    "parts": [
                        video_part,
                        {"text": VIDEO_ANALYSIS_PROMPT},
                    ],
                }
            ]

            # 调用 Gemini API
            response = await self._generate_content(contents, context="video_analysis")

            logger.info("Video analysis completed, parsing response...")

            # 解析输出
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            # 写入缓存
            if use_cache:
                set_cache(cache_key, result)

            logger.info(f"Video analysis parsed successfully, found {len(result.get('shots', []))} shots")
            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during video analysis: {e}")
            raise

    async def analyze_rhythm(self, video_url: str, use_cache: bool = True) -> dict[str, Any]:
        """
        对视频进行画面+声音节奏联合分析
        提取节奏结构，供复刻剧辑阶段对照参考

        Args:
            video_url: 视频 URL
            use_cache: 是否使用缓存，默认 True

        Returns:
            结构化节奏分析结果，包含 overview/audio/shots/rhythm_timeline/replication_rhythm_guide

        Raises:
            ValueError: 解析失败时抛出
            httpx.HTTPError: API 调用失败时抛出
        """
        logger.info(f"Starting rhythm analysis for: {video_url}")

        # 检查缓存
        if use_cache:
            cache_key = get_cache_key(video_url, "rhythm_analysis")
            cached = get_cache(cache_key)
            if cached:
                logger.info("Rhythm analysis loaded from cache")
                if self._tracking_project_id:
                    token_tracker.record(
                        project_id=self._tracking_project_id,
                        stage=self._tracking_stage or "unknown",
                        call_type="rhythm_analysis", model=self.model_name,
                        input_tokens=0, output_tokens=0, cached=True,
                    )
                return cached

        try:
            # 下载视频并转为 base64 inline_data
            video_part = await self._download_as_base64(video_url, "video/mp4")

            # 构建请求内容
            contents = [
                {
                    "role": "user",
                    "parts": [
                        video_part,
                        {"text": RHYTHM_ANALYSIS_PROMPT},
                    ],
                }
            ]

            # 调用 Gemini API
            response = await self._generate_content(contents, context="rhythm_analysis")

            logger.info("Rhythm analysis completed, parsing response...")

            # 解析输出
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            # 写入缓存
            if use_cache:
                set_cache(cache_key, result)

            shots_count = len(result.get("shots", []))
            timeline_count = len(result.get("rhythm_timeline", []))
            logger.info(
                f"Rhythm analysis parsed successfully: {shots_count} shots, "
                f"{timeline_count} rhythm timeline events"
            )
            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API HTTP error during rhythm analysis: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during rhythm analysis: {e}")
            raise

    async def analyze_rhythm_from_bytes(
        self,
        video_bytes: bytes,
        mime_type: str = "video/mp4",
    ) -> dict[str, Any]:
        """
        直接传入视频字节进行节奏分析（不经过公网 URL）
        适用于本地文件上传场景

        Args:
            video_bytes: 视频文件字节
            mime_type: MIME 类型，默认 video/mp4

        Returns:
            结构化节奏分析结果
        """
        logger.info(f"Starting rhythm analysis from uploaded bytes ({len(video_bytes)} bytes, {mime_type})")

        try:
            # 直接将字节转为 base64 inline_data
            file_b64 = base64.b64encode(video_bytes).decode("utf-8")
            video_part = {"inline_data": {"mime_type": mime_type, "data": file_b64}}

            contents = [
                {
                    "role": "user",
                    "parts": [
                        video_part,
                        {"text": RHYTHM_ANALYSIS_PROMPT},
                    ],
                }
            ]

            response = await self._generate_content(contents, context="rhythm_analysis_upload")

            logger.info("Rhythm analysis (from bytes) completed, parsing response...")

            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            shots_count = len(result.get("shots", []))
            timeline_count = len(result.get("rhythm_timeline", []))
            logger.info(
                f"Rhythm analysis (from bytes) parsed: {shots_count} shots, "
                f"{timeline_count} rhythm timeline events"
            )
            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API HTTP error during rhythm analysis (bytes): {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during rhythm analysis (bytes): {e}")
            raise

    async def extract_product_listing(self, listing_url: str, use_cache: bool = True) -> Optional[dict[str, Any]]:
        """
        从商品详情页链接提取结构化产品信息
        使用 httpx 抓取页面内容，再通过 Gemini 提取结构化数据

        Args:
            listing_url: 商品详情页 URL
            use_cache: 是否使用缓存

        Returns:
            结构化产品信息 dict，失败时返回 None
        """
        logger.info(f"Starting product listing extraction for: {listing_url}")

        # 检查缓存
        if use_cache:
            cache_key = get_cache_key(listing_url, "product_listing")
            cached = get_cache(cache_key)
            if cached:
                logger.info("Product listing extraction loaded from cache")
                if self._tracking_project_id:
                    token_tracker.record(
                        project_id=self._tracking_project_id,
                        stage=self._tracking_stage or "unknown",
                        call_type="product_listing_extraction", model=self.model_name,
                        input_tokens=0, output_tokens=0, cached=True,
                    )
                return cached

        try:
            # 抓取网页内容（不走代理）
            async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(30.0)) as download_client:
                response = await download_client.get(
                    listing_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    },
                    follow_redirects=True
                )
                response.raise_for_status()

            html_content = response.text
            logger.info(f"Page fetched: {len(html_content)} chars")

            # 简单文本提取：去除 HTML 标签中的噪声
            page_text = self._extract_text_from_html(html_content)
            logger.info(f"Text extracted from HTML: {len(page_text)} chars")

            if len(page_text.strip()) < 50:
                logger.warning("Extracted text too short, listing extraction skipped")
                return None

            # 构建 prompt
            prompt = format_product_listing_extraction_prompt(page_text)

            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            # 调用 Gemini API
            response = await self._generate_content(contents, context="product_listing_extraction")

            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            # 附加：从 HTML 提取商品视频 URL 候选（非 LLM 通路，纯规则抽取）
            try:
                video_urls = self._extract_product_video_urls(html_content, base_url=listing_url)
                if video_urls:
                    result["product_video_urls"] = video_urls
                    logger.info(f"Product listing: detected {len(video_urls)} candidate video URL(s)")
            except Exception as _e:
                logger.warning(f"Product video URL extraction failed (non-blocking): {_e}")

            # 写入缓存
            if use_cache:
                set_cache(cache_key, result)

            logger.info(f"Product listing extraction completed: {result.get('product_name', 'unknown')}")
            return result

        except Exception as e:
            logger.warning(f"Product listing extraction failed (non-blocking): {e}")
            return None

    @staticmethod
    def _extract_text_from_html(html: str) -> str:
        """
        从 HTML 中提取纯文本内容，去除导航、脚本、样式等噪声

        优先提取 <script type="application/ld+json"> 中的结构化商品数据（Schema.org Product），
        这类数据信噪比优越，对 Gemini 结构化提取准确率大幅提升。至于 HTML 中
        普通的可见文本（标题、详情、规格等）仍按原有正则策略提取，两者拼接后返回。

        Args:
            html: 原始 HTML 字符串

        Returns:
            提取的纯文本（ld+json 在前，普通文本在后）
        """
        import re
        import json as _json

        # ---- 优先抽取 Schema.org ld+json 块 ----
        ld_json_texts: list = []
        for match in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            raw = match.group(1).strip()
            if not raw:
                continue
            try:
                data = _json.loads(raw)
            except Exception:
                # JSON 解析失败时保留原文，Gemini 仍可能从中取证
                ld_json_texts.append(raw[:4000])
                continue
            # 只保留与商品相关的 Schema（Product / ItemPage / Offer 等）
            def _looks_like_product(obj) -> bool:
                if not isinstance(obj, dict):
                    return False
                t = obj.get("@type") or obj.get("type") or ""
                if isinstance(t, list):
                    return any("product" in str(x).lower() or "item" in str(x).lower() for x in t)
                return "product" in str(t).lower() or "item" in str(t).lower() or "offer" in str(t).lower()

            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if _looks_like_product(item):
                    ld_json_texts.append(_json.dumps(item, ensure_ascii=False)[:6000])
                # 嵌套在 @graph 里的情况
                graph = item.get("@graph") if isinstance(item, dict) else None
                if isinstance(graph, list):
                    for g in graph:
                        if _looks_like_product(g):
                            ld_json_texts.append(_json.dumps(g, ensure_ascii=False)[:6000])

        # ---- 常规正则文本提取 ----
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # 移除 HTML 注释
        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        # 移除 nav, header, footer 标签及内容
        text = re.sub(r'<nav[^>]*>.*?</nav>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<footer[^>]*>.*?</footer>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # 替换 HTML 标签为换行
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</(p|div|h[1-6]|li|tr)>', '\n', text, flags=re.IGNORECASE)
        # 移除所有剩余 HTML 标签
        text = re.sub(r'<[^>]+>', ' ', text)
        # 解码 HTML 实体
        import html as html_module
        text = html_module.unescape(text)
        # 清理多余空白
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        text = text.strip()

        # ---- 拼接：ld+json 在前，普通文本在后 ----
        if ld_json_texts:
            header = "### Structured Product Data (Schema.org ld+json):\n" + "\n\n".join(ld_json_texts)
            return header + "\n\n### Page Visible Text:\n" + text
        return text

    @staticmethod
    def _extract_product_video_urls(html: str, base_url: str = "") -> list[str]:
        """从商品详情页 HTML 中提取可用的商品视频 URL 候选

        支持的来源（按优先级组合打分 + 去重）：
        1. JSON-LD VideoObject 的 contentUrl / embedUrl
        2. <video src> / <source src>
        3. OpenGraph / Twitter 视频 meta（og:video / og:video:url / og:video:secure_url / twitter:player:stream）
        4. 针对 Lazada 等商城脚本内嵌的 JSON：正则匹配 "videoUrl":"..."、
           以及 .mp4 结尾的阿里系 CDN（*.alicdn.com / *.lazcdn.com / cloud.video.taobao.com）

        策略：
        - 仅保留直链 mp4（.mp4 / .mp4?query）；m3u8 / youtube / tiktok 等不可直接下载的源跳过
        - 在去重的基础上按出现顺序返回前 N 个（默认 max=3，避免浪费 token）
        """
        import re
        from urllib.parse import urljoin

        if not html:
            return []

        candidates: list[str] = []

        def _push(url: str) -> None:
            if not url:
                return
            u = url.strip().strip('"').strip("'")
            # 只保留 http/https 或协议相对
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/") and base_url:
                u = urljoin(base_url, u)
            if not (u.startswith("http://") or u.startswith("https://")):
                return
            # 仅放行直链 mp4 / mov / webm
            lower = u.split("?", 1)[0].lower()
            if not (lower.endswith(".mp4") or lower.endswith(".mov") or lower.endswith(".webm")):
                # 关键补异：Lazada 的 tbcdn 有时 .mp4 在 query 前
                if ".mp4" not in lower:
                    return
            if u not in candidates:
                candidates.append(u)

        # ---- 1. JSON-LD VideoObject ----
        import json as _json
        for m in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            raw = m.group(1).strip()
            if not raw:
                continue
            try:
                data = _json.loads(raw)
            except Exception:
                continue

            def _walk(obj):
                if isinstance(obj, dict):
                    t = obj.get("@type") or obj.get("type") or ""
                    t_str = "".join(t) if isinstance(t, list) else str(t)
                    if "video" in t_str.lower():
                        for k in ("contentUrl", "url", "embedUrl"):
                            v = obj.get(k)
                            if isinstance(v, str):
                                _push(v)
                    # 嵌套字段（video / mainEntity.video）
                    for v in obj.values():
                        _walk(v)
                elif isinstance(obj, list):
                    for it in obj:
                        _walk(it)

            _walk(data)

        # ---- 2. <video> / <source> tags ----
        for m in re.finditer(r'<video[^>]*\ssrc=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
            _push(m.group(1))
        for m in re.finditer(r'<source[^>]*\ssrc=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
            _push(m.group(1))

        # ---- 3. OG / Twitter meta ----
        meta_patterns = [
            r'<meta[^>]*property=["\']og:video(?::secure_url|:url)?["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:video(?::secure_url|:url)?["\']',
            r'<meta[^>]*name=["\']twitter:player:stream["\'][^>]*content=["\']([^"\']+)["\']',
        ]
        for pat in meta_patterns:
            for m in re.finditer(pat, html, flags=re.IGNORECASE):
                _push(m.group(1))

        # ---- 4. Lazada / 阿里系商城：脚本内嵌 JSON 字段 ----
        # 4a. 类似 "videoUrl":"https://..." / "mainVideoUrl":"..." / "videoSrc":"..."
        for m in re.finditer(
            r'"(?:video[_]?[Uu]rl|main[Vv]ideo[Uu]rl|video[Ss]rc|videoUrl)"\s*:\s*"([^"]+)"',
            html,
        ):
            url = m.group(1).replace("\\/", "/").replace("\\u002F", "/")
            _push(url)

        # 4b. 直接匹配阿里/Lazada CDN 的 mp4 绝对 URL
        for m in re.finditer(
            r'https?:(?:\\?/\\?/)(?:[\w.-]*\.)?(?:alicdn|lazcdn|video\.taobao|cloud\.video\.taobao)\.com/[^"\' <>]+?\.mp4[^"\' <>]*',
            html,
            flags=re.IGNORECASE,
        ):
            url = m.group(0).replace("\\/", "/")
            _push(url)

        # 限制数量，避免污染 listing_info
        return candidates[:3]

    async def analyze_product_video(
        self,
        video_url: str,
        known_facts: Optional[dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> Optional[dict[str, Any]]:
        """对商品详情页嵌入的宣传短视频进行差分式语义理解

        与 analyze_video 的区别：
        - 使用 PRODUCT_VIDEO_ANALYSIS_PROMPT（差分模式），输出聚焦在 listing/主图
          没有的信息：多角度细节、使用中动态状态、材质在运动中的表现等
        - 专为 Product Brief Agent 消费，失败非阻塞（返回 None）
        - 限制下载体积（超过 ~18MB 直接放弃，Gemini inline_data 上限 ~20MB）

        Args:
            video_url: 商品视频 URL（建议从 extract_product_listing 返回的
                product_video_urls[0] 取得）
            known_facts: 从 listing / 商品主图已经得知的事实摘要；
                会注入到 prompt 中明确告知 LLM"不要重复这些"。
                建议通过 prompts.product_video_analysis.build_known_facts_from_sources 构造。
            use_cache: 是否使用缓存

        Returns:
            结构化商品视频差分分析结果，或 None（失败时）
        """
        if not video_url or not str(video_url).strip():
            return None

        logger.info(f"Starting product video analysis (differential) for: {video_url}")

        # 缓存键需加入 known_facts 指纹，避免不同 listing 复用旧缓存
        cache_key = None
        if use_cache:
            try:
                import hashlib as _hashlib
                import json as _json
                facts_fingerprint = ""
                if known_facts:
                    facts_fingerprint = _hashlib.md5(
                        _json.dumps(known_facts, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:10]
                cache_key = get_cache_key(
                    f"{video_url}|kf={facts_fingerprint}",
                    "product_video_analysis",
                )
            except Exception:
                cache_key = get_cache_key(video_url, "product_video_analysis")
            cached = get_cache(cache_key)
            if cached:
                logger.info("Product video analysis loaded from cache")
                if self._tracking_project_id:
                    token_tracker.record(
                        project_id=self._tracking_project_id,
                        stage=self._tracking_stage or "unknown",
                        call_type="product_video_analysis", model=self.model_name,
                        input_tokens=0, output_tokens=0, cached=True,
                    )
                return cached

        try:
            # 预检查体积（HEAD），避免下载满后被 Gemini 拒绝
            max_bytes = int(getattr(settings, "PRODUCT_VIDEO_MAX_BYTES", 18 * 1024 * 1024) or (18 * 1024 * 1024))
            try:
                async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(15.0)) as _c:
                    head = await _c.head(video_url, follow_redirects=True)
                    cl = int(head.headers.get("content-length", "0") or 0)
                    if cl and cl > max_bytes:
                        logger.warning(
                            f"Product video too large for inline_data ({cl} bytes > {max_bytes}), skipped"
                        )
                        return None
            except Exception as _he:
                logger.debug(f"Product video HEAD check skipped: {_he}")

            video_part = await self._download_as_base64(video_url, "video/mp4")
            prompt_text = format_product_video_analysis_prompt(known_facts)
            contents = [
                {
                    "role": "user",
                    "parts": [
                        video_part,
                        {"text": prompt_text},
                    ],
                }
            ]
            response = await self._generate_content(contents, context="product_video_analysis")
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            if use_cache and cache_key:
                set_cache(cache_key, result)

            logger.info(
                f"Product video analysis parsed: new_info={len(result.get('new_info_not_in_listing', []))}, "
                f"angles={sum(1 for v in (result.get('multi_angle_details') or {}).values() if v)}, "
                f"dynamic_states={len(result.get('in_use_dynamic_states', []))}"
            )
            return result

        except Exception as e:
            logger.warning(f"Product video analysis failed (non-blocking): {e}")
            return None

    async def check_action_compatibility(
        self,
        video_analysis: dict[str, Any],
        product_analysis: dict[str, Any],
        product_listing_info: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """
        检测原视频动作与新产品的兼容性
        逐镜头判断动作是否可行，并为不兼容的镜头提供替代方案

        Args:
            video_analysis: 原视频分析结果
            product_analysis: 商品属性分析结果
            product_listing_info: 商品链接提取信息（可选）

        Returns:
            逐镜头兼容性检测结果，失败时返回 None
        """
        logger.info("Starting action compatibility check")

        try:
            video_analysis_str = json.dumps(video_analysis, ensure_ascii=False, indent=2)
            product_analysis_str = json.dumps(product_analysis, ensure_ascii=False, indent=2)
            listing_info_str = ""
            if product_listing_info:
                listing_info_str = json.dumps(product_listing_info, ensure_ascii=False, indent=2)

            prompt = format_action_compatibility_prompt(
                video_analysis=video_analysis_str,
                product_analysis=product_analysis_str,
                product_listing_info=listing_info_str
            )

            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            response = await self._generate_content(contents, context="action_compatibility_check")

            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            # 记录检测结果概要
            overall = result.get("overall_compatibility", "unknown")
            compatible = result.get("compatible_shot_count", 0)
            incompatible = result.get("incompatible_shot_count", 0)
            needs_adj = result.get("needs_adjustment_shot_count", 0)
            logger.info(
                f"Action compatibility check completed: overall={overall}, "
                f"compatible={compatible}, needs_adjustment={needs_adj}, incompatible={incompatible}"
            )
            return result

        except Exception as e:
            logger.warning(f"Action compatibility check failed (non-blocking): {e}")
            return None

    async def analyze_product(self, image_url: str, use_cache: bool = True) -> dict[str, Any]:
        """
        分析产品图，返回商品属性
        支持缓存：相同图片 URL 不重复分析

        Args:
            image_url: 产品图片 URL
            use_cache: 是否使用缓存，默认 True

        Returns:
            结构化商品属性分析结果

        Raises:
            ValueError: 解析失败时抛出
            httpx.HTTPError: API 调用失败时抛出
        """
        logger.info(f"Starting product analysis for: {image_url}")

        # 检查缓存
        if use_cache:
            cache_key = get_cache_key(image_url, "product_analysis")
            cached = get_cache(cache_key)
            if cached:
                logger.info("Product analysis loaded from cache")
                if self._tracking_project_id:
                    token_tracker.record(
                        project_id=self._tracking_project_id,
                        stage=self._tracking_stage or "unknown",
                        call_type="product_analysis", model=self.model_name,
                        input_tokens=0, output_tokens=0, cached=True,
                    )
                return cached

        try:
            # 下载图片并转为 base64 inline_data
            image_part = await self._download_as_base64(image_url, "image/jpeg")

            # 构建请求内容
            contents = [
                {
                    "role": "user",
                    "parts": [
                        image_part,
                        {"text": PRODUCT_ANALYSIS_PROMPT},
                    ],
                }
            ]

            # 调用 Gemini API
            response = await self._generate_content(contents, context="product_analysis")

            logger.info("Product analysis completed, parsing response...")

            # 解析输出
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            # 写入缓存
            if use_cache:
                set_cache(cache_key, result)

            logger.info("Product analysis parsed successfully")
            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during product analysis: {e}")
            raise

    async def generate_three_views(
        self, 
        image_url: str, 
        product_description: str = "",
        enable_background_removal: bool = True,
        aspect_ratio: str = "9:16",
    ) -> list[str]:
        """
        从产品图生成白底三视图

        Args:
            image_url: 产品图片 URL
            product_description: 产品描述（可选），用于指导三视图生成更准确地还原产品特征
            enable_background_removal: 是否启用抠图预处理（默认True），抠图后生成效果更好

        Returns:
            三张图片的 base64 字符串列表（正视图、侧视图、俯视图）
        """
        logger.info(f"Starting three-view generation for: {image_url}")
        if product_description:
            logger.info(f"Product description provided: {product_description[:100]}...")

        max_retries = 3

        try:
            # 下载图片
            logger.info(f"Downloading image from: {image_url}")
            async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(120.0)) as download_client:
                response = await download_client.get(image_url)
                response.raise_for_status()
            
            image_bytes = response.content
            logger.info(f"Image downloaded: {len(image_bytes)} bytes")
            
            # 抠图预处理：移除复杂背景，让 AI 更专注于产品本身
            if enable_background_removal:
                logger.info("Starting background removal preprocessing...")
                processed_bytes = await remove_background(
                    image_bytes=image_bytes,
                    removebg_api_key=settings.REMOVEBG_API_KEY,
                )
                if processed_bytes != image_bytes:
                    logger.info(f"Background removed successfully, processed size: {len(processed_bytes)} bytes")
                    image_bytes = processed_bytes
                else:
                    logger.info("Background removal skipped or failed, using original image")
            
            # 转为 base64 inline_data
            file_b64 = base64.b64encode(image_bytes).decode("utf-8")
            image_part = {"inline_data": {"mime_type": "image/png", "data": file_b64}}
            
        except Exception as e:
            logger.error(f"Failed to download/process image for three-view generation: {e}")
            raise

        # 构建产品描述上下文
        product_context = ""
        if product_description:
            product_context = f"""
Product Description (MUST be faithfully reproduced):
{product_description}

CRITICAL: The generated views MUST exactly match the product in the reference image above.
Maintain the exact same colors, shapes, proportions, materials, textures, and branding.
Do NOT reimagine or redesign the product - reproduce it faithfully.
"""

        # 三视图生成 prompt
        three_view_prompt = f"""IMPORTANT: Generate ALL images in 9:16 portrait aspect ratio (720x1280 pixels). The product should be centered vertically with white background filling the frame.

Based on the reference product image provided, generate three photorealistic views of this EXACT product on a pure white background:
{product_context}
1. Front view - showing the product from the front
2. Side view - showing the product from the side  
3. Top view - showing the product from above

Requirements:
- Output image size: 720x1280 pixels (9:16 portrait orientation)
- Pure white background (#FFFFFF)
- MUST faithfully reproduce the exact product shown in the reference image
- Professional product photography style
- Consistent lighting across all views
- Same scale/proportion across all views
- No shadows or reflections
- Product centered in frame with adequate white space

Output all three images in 9:16 portrait format."""

        # 构建请求内容
        contents = [
            {
                "role": "user",
                "parts": [
                    image_part,
                    {"text": three_view_prompt},
                ],
            }
        ]

        for attempt in range(max_retries):
            try:
                # 调用 Gemini API (使用图片生成模型)
                response = await self._generate_content(
                    contents,
                    model=self.image_gen_model,
                    generation_config={
                        "responseModalities": ["TEXT", "IMAGE"]
                    }
                )

                # 记录响应结构，便于调试
                candidates = response.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    # 支持两种格式：inline_data (蛇形) 和 inlineData (驼峰)
                    part_types = [("image" if ("inline_data" in p or "inlineData" in p) else "text") for p in parts]
                    logger.info(f"Three-view response parts: {len(parts)} parts, types: {part_types}")
                else:
                    logger.warning("Three-view response has no candidates")

                # 提取生成的图片
                images = self._extract_images_from_response(response)

                if images:
                    logger.info(f"Three-view generation succeeded on attempt {attempt + 1}, got {len(images)} images")
                    # 标准化所有图片为 9:16 尺寸（兜底处理）
                    from services.image_utils import standardize_image_to_9_16
                    standardized_views = []
                    for view_b64 in images:
                        if view_b64.startswith("data:"):
                            header, b64_data = view_b64.split(",", 1)
                        else:
                            header = "data:image/png;base64"
                            b64_data = view_b64
                        img_bytes = base64.b64decode(b64_data)
                        standardized_bytes = standardize_image_to_9_16(img_bytes, 720, 1280)
                        new_b64 = base64.b64encode(standardized_bytes).decode()
                        standardized_views.append(f"data:image/png;base64,{new_b64}")
                    logger.info(f"All {len(standardized_views)} views standardized to 720x1280 (9:16)")
                    return standardized_views

                logger.warning(f"Three-view generation returned 0 images, retrying ({attempt + 1}/{max_retries})...")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)

            except Exception as e:
                logger.warning(f"Three-view generation attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                else:
                    raise

        logger.warning(f"Three-view generation failed after {max_retries} attempts, returning empty list")
        return []

    # 镜头数校验最大重试次数
    SHOT_COUNT_MAX_RETRIES = 2

    async def generate_script(
        self,
        video_analysis: dict[str, Any],
        product_analysis: dict[str, Any],
        rhythm_analysis: Optional[dict[str, Any]] = None,
        action_compatibility: Optional[dict[str, Any]] = None,
        product_listing_info: Optional[dict[str, Any]] = None,
        replicate_hook: bool = True
    ) -> dict[str, Any]:
        """
        生成复刻脚本

        带镜头数硬校验：如果节奏分析 / 视频分析中能确定原片镜头数，
        则校验生成结果的 shot 数量必须 1:1 一致，不一致时自动重试。

        Args:
            video_analysis: 视频分析结果
            product_analysis: 商品属性分析结果
            rhythm_analysis: 视频节奏分析结果（可选，有则注入节奏约束）
            action_compatibility: 动作兼容性检测结果（可选）
            product_listing_info: 商品详情页提取信息（可选）
            replicate_hook: 是否复刻 hook 镜头

        Returns:
            结构化复刻脚本，包含 shots 列表

        Raises:
            ValueError: 解析失败时抛出
            httpx.HTTPError: API 调用失败时抛出
        """
        logger.info(f"Starting script generation (rhythm_data={'yes' if rhythm_analysis else 'no'}, action_compat={'yes' if action_compatibility else 'no'}, listing_info={'yes' if product_listing_info else 'no'}, replicate_hook={replicate_hook})")

        try:
            # 将分析结果转换为 JSON 字符串
            video_analysis_str = json.dumps(video_analysis, ensure_ascii=False, indent=2)
            product_analysis_str = json.dumps(product_analysis, ensure_ascii=False, indent=2)

            # 节奏数据：只提取输入脚本生成需要的关键字段，避免全量注入影响 Token
            rhythm_analysis_str = ""
            if rhythm_analysis:
                rhythm_compact = {
                    "overview": rhythm_analysis.get("overview", {}),
                    "shots": [
                        {
                            "shot_number": s.get("shot_number"),
                            "duration_sec": s.get("duration_sec"),
                            "pace": s.get("pace"),
                            "beat_aligned": s.get("beat_aligned"),
                            "sync_description": s.get("sync_description"),
                        }
                        for s in rhythm_analysis.get("shots", [])
                    ],
                    "rhythm_timeline": rhythm_analysis.get("rhythm_timeline", []),
                    "replication_rhythm_guide": rhythm_analysis.get("replication_rhythm_guide", {}),
                }
                rhythm_analysis_str = json.dumps(rhythm_compact, ensure_ascii=False, indent=2)

            # ---- 确定期望镜头数 ----
            expected_shot_count = self._resolve_expected_shot_count(
                video_analysis, rhythm_analysis
            )

            # 动作兼容性检测结果
            action_compatibility_str = ""
            if action_compatibility:
                action_compatibility_str = json.dumps(action_compatibility, ensure_ascii=False, indent=2)

            # 商品详情页信息
            product_listing_str = ""
            if product_listing_info:
                product_listing_str = json.dumps(product_listing_info, ensure_ascii=False, indent=2)

            # 格式化 prompt
            prompt = format_script_replication_prompt(
                video_analysis_str,
                product_analysis_str,
                rhythm_analysis=rhythm_analysis_str,
                action_compatibility=action_compatibility_str,
                product_listing_info=product_listing_str,
                replicate_hook=replicate_hook
            )

            # 构建请求内容
            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            # 调用 Gemini API
            response = await self._generate_content(contents)

            logger.info("Script generation completed, parsing response...")

            # 解析输出
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)
            generated_count = len(result.get("shots", []))

            # §14 品牌尾镜过滤：effective_expected = expected - filtered_brand_outros_count
            filtered_outros = int((result.get("summary") or {}).get("filtered_brand_outros_count") or 0)
            effective_expected = (expected_shot_count - filtered_outros) if expected_shot_count else None

            logger.info(
                f"Script parsed: {generated_count} shots "
                f"(expected={expected_shot_count}, filtered_outros={filtered_outros}, effective_expected={effective_expected})"
            )

            # ---- 镜头数硬校验 + 自动重试（尊重品牌尾镜过滤）----
            if effective_expected and generated_count != effective_expected:
                result = await self._retry_script_for_shot_count(
                    contents, result, generated_count, effective_expected
                )

            return result

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during script generation: {e}")
            raise

    # ------------------------------------------------------------------
    # 镜头数校验辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_expected_shot_count(
        video_analysis: dict[str, Any],
        rhythm_analysis: Optional[dict[str, Any]],
    ) -> Optional[int]:
        """
        从节奏分析或视频分析中推断原片镜头数。
        优先级：rhythm_analysis.shots 长度 > rhythm_analysis.overview.total_shots > video_analysis.shots 长度
        返回 None 表示无法确定，跳过校验。
        """
        if rhythm_analysis:
            rhythm_shots = rhythm_analysis.get("shots", [])
            if rhythm_shots:
                return len(rhythm_shots)
            overview_total = rhythm_analysis.get("overview", {}).get("total_shots")
            if overview_total and isinstance(overview_total, int) and overview_total > 0:
                return overview_total

        # 退而求其次：从视频分析提取
        va_shots = video_analysis.get("shots", [])
        if va_shots:
            return len(va_shots)

        return None

    async def _retry_script_for_shot_count(
        self,
        original_contents: list[dict],
        last_result: dict[str, Any],
        generated_count: int,
        expected_count: int,
    ) -> dict[str, Any]:
        """
        镜头数不一致时，用纠错 prompt 追加对话上下文重试生成。
        最多重试 SHOT_COUNT_MAX_RETRIES 次；仍然不一致则打日志警告并返回最后一次结果。
        """
        contents = list(original_contents)  # 浅拷贝，不修改原始
        result = last_result
        current_count = generated_count

        for attempt in range(1, self.SHOT_COUNT_MAX_RETRIES + 1):
            logger.warning(
                f"Shot count mismatch: generated {current_count}, expected {expected_count}. "
                f"Retry {attempt}/{self.SHOT_COUNT_MAX_RETRIES}"
            )

            # 把上一次模型输出 + 纠错指令追加到对话
            correction_text = (
                f"CRITICAL ERROR: You generated {current_count} shots but the expected count is "
                f"exactly {expected_count} shots (after applying §14 BRAND OUTRO FILTERING). "
                f"You MUST output exactly {expected_count} shots — no more, no less. "
                f"Rules reminder: "
                f"(1) Keep 1-to-1 correspondence with the original video's shots in the same order, "
                f"EXCEPT for shots that qualify as brand outros per §14 (pure platform/brand logo + CTA button + slogan, no product action). "
                f"(2) Brand outro shots MUST be filtered out and NOT appear in the shots array; "
                f"they also must NOT be counted toward the shot total. "
                f"(3) Update summary.filtered_brand_outros_count accordingly and list the filtered original shot numbers in summary.notes. "
                f"Please regenerate the complete JSON with exactly {expected_count} shots after filtering."
            )
            contents.append({
                "role": "model",
                "parts": [{"text": json.dumps(result, ensure_ascii=False)}],
            })
            contents.append({
                "role": "user",
                "parts": [{"text": correction_text}],
            })

            response = await self._generate_content(contents)
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)
            current_count = len(result.get("shots", []))

            logger.info(f"Retry {attempt} result: {current_count} shots (expected {expected_count})")

            if current_count == expected_count:
                logger.info("Shot count now matches after retry")
                return result

        logger.warning(
            f"Shot count still mismatched after {self.SHOT_COUNT_MAX_RETRIES} retries: "
            f"{current_count} vs expected {expected_count}. Proceeding with best effort."
        )
        return result

    async def convert_to_prompts(
        self,
        replicated_script: dict[str, Any],
        original_video_analysis: Optional[dict[str, Any]] = None,
        product_scene_context: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        """
        将复刻脚本转换为逐镜头生成提示词

        Args:
            replicated_script: 复刻后的脚本
            original_video_analysis: 原视频分析 dict，供镜头内环境音三步决策法参考。
                为避免 Token 浪费，此处会缩减为每个 shot 的
                {shot_number, audio, environment.setting} 精选字段。
            product_scene_context: 新商品场景上下文 dict，用于决定新场景类型。

        Returns:
            列表，每个元素包含 first_frame, motion, camera, duration, constraints, audio

        Raises:
            ValueError: 解析失败时抛出
            httpx.HTTPError: API 调用失败时抛出
        """
        logger.info("Starting prompt conversion")

        try:
            # 将脚本转换为 JSON 字符串
            script_str = json.dumps(replicated_script, ensure_ascii=False, indent=2)

            # 精简原视频分析为 ambient 决策所需字段，避免 Token 浪费
            video_analysis_str = ""
            if original_video_analysis:
                trimmed = _trim_video_analysis_for_ambient(original_video_analysis)
                video_analysis_str = json.dumps(trimmed, ensure_ascii=False, indent=2)
            product_context_str = ""
            if product_scene_context:
                product_context_str = json.dumps(product_scene_context, ensure_ascii=False, indent=2)

            # 格式化 prompt
            prompt = format_prompt_conversion(
                replicated_script=script_str,
                original_video_analysis=video_analysis_str,
                product_scene_context=product_context_str,
            )

            # 构建请求内容
            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            # 调用 Gemini API
            response = await self._generate_content(contents)

            logger.info("Prompt conversion completed, parsing response...")

            # 解析输出
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)
            shots = result.get("shots", [])

            logger.info(f"Prompt conversion parsed successfully, converted {len(shots)} shots")
            return shots

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during prompt conversion: {e}")
            raise

    async def validate_script(
        self,
        script_result: dict[str, Any],
        product_listing_info: Optional[dict[str, Any]] = None,
        video_analysis: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        自动验证生成的复刻脚本，检查镜头重复、功能虚构、卖点覆盖等问题

        Args:
            script_result: 生成的脚本 dict
            product_listing_info: 商品详情页提取信息（可选）
            video_analysis: 原视频分析结果（可选）

        Returns:
            验证结果 dict，包含 passed, confidence, issues
        """
        logger.info("Starting script validation")

        try:
            script_str = json.dumps(script_result, ensure_ascii=False, indent=2)
            listing_str = json.dumps(product_listing_info, ensure_ascii=False, indent=2) if product_listing_info else ""
            video_str = json.dumps(video_analysis, ensure_ascii=False, indent=2) if video_analysis else ""

            prompt = format_script_validation_prompt(
                script_result=script_str,
                product_listing_info=listing_str,
                video_analysis=video_str
            )

            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            response = await self._generate_content(contents, context="script_validation")
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            passed = result.get("passed", False)
            confidence = result.get("confidence", 0.0)
            issues = result.get("issues", [])
            critical_count = sum(1 for i in issues if i.get("severity") == "critical")
            warning_count = sum(1 for i in issues if i.get("severity") == "warning")

            logger.info(
                f"Script validation completed: passed={passed}, confidence={confidence:.2f}, "
                f"critical={critical_count}, warnings={warning_count}"
            )
            return result

        except Exception as e:
            logger.warning(f"Script validation failed (non-blocking): {e}")
            # 验证失败时返回保守结果：未通过，需要人审
            return {"passed": False, "confidence": 0.0, "issues": [{"type": "validation_error", "severity": "critical", "shot_numbers": [], "description": str(e), "fix_instruction": ""}]}

    async def audit_shot_prompts(
        self,
        shots: list[dict[str, Any]],
        product_analysis: str = "",
        product_listing_info: str = "",
        video_analysis: str = "",
        script: str = "",
    ) -> dict[str, Any]:
        """
        对 Stage 3 生成的逐镜头提示词做物理一致性审核。

        Args:
            shots: 待审核的镜头列表，每项至少包含 shot_number / first_frame / motion /
                constraints / shot_type / scene_type 等字段。
            product_analysis: 商品属性分析结果（JSON 字符串或文本）。
            product_listing_info: 商品详情页提取信息（JSON 字符串或文本）。
            video_analysis: 原视频分析结果（JSON 字符串或文本）。
            script: 完整复刻脚本（JSON 字符串或文本），用于因果链审查。

        Returns:
            审核结果 dict，结构为 {"shots": [{shot_number, passed, critical_issues,
            warnings, reason_summary}, ...]}。解析失败时兜底返回 {"shots": []}，
            调用方需将空结果视为 "全部通过" 以保持原有行为。
        """
        logger.info(f"Starting shot prompt audit for {len(shots)} shots")

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

            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            response = await self._generate_content(contents, context="shot_prompt_audit")
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            audited = result.get("shots", []) if isinstance(result, dict) else []
            if not isinstance(audited, list):
                logger.warning("Shot prompt audit returned non-list 'shots' field, treating as empty")
                audited = []

            rejected = [s for s in audited if not s.get("passed", True)]
            warned = [s for s in audited if s.get("passed", True) and s.get("warnings")]
            logger.info(
                f"Shot prompt audit completed: total={len(audited)}, "
                f"rejected={len(rejected)}, warnings_only={len(warned)}"
            )
            if rejected:
                rejected_numbers = [s.get("shot_number") for s in rejected]
                logger.warning(f"Shot prompt audit rejected shots: {rejected_numbers}")

            return {"shots": audited}

        except Exception as e:
            # 审核失败不阻断主流程：返回空结果，由调用方按 "全部通过" 兜底
            logger.warning(f"Shot prompt audit failed (non-blocking): {e}")
            return {"shots": []}

    async def fix_script(
        self,
        script_result: dict[str, Any],
        validation_issues: list[dict[str, Any]],
        video_analysis: Optional[dict[str, Any]] = None,
        product_analysis: Optional[dict[str, Any]] = None,
        product_listing_info: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        根据验证报告中的问题自动修正脚本

        Args:
            script_result: 当前脚本 dict
            validation_issues: 验证报告中的 issues 列表
            video_analysis: 原视频分析结果（可选）
            product_analysis: 商品属性分析结果（可选）
            product_listing_info: 商品详情页提取信息（可选）

        Returns:
            修正后的脚本 dict

        Raises:
            ValueError: 解析失败时抛出
        """
        logger.info(f"Starting script fix for {len(validation_issues)} issues")

        try:
            script_str = json.dumps(script_result, ensure_ascii=False, indent=2)
            issues_str = json.dumps(validation_issues, ensure_ascii=False, indent=2)
            listing_str = json.dumps(product_listing_info, ensure_ascii=False, indent=2) if product_listing_info else ""
            video_str = json.dumps(video_analysis, ensure_ascii=False, indent=2) if video_analysis else ""
            product_str = json.dumps(product_analysis, ensure_ascii=False, indent=2) if product_analysis else ""

            prompt = format_script_fix_prompt(
                script_result=script_str,
                validation_issues=issues_str,
                product_listing_info=listing_str,
                video_analysis=video_str,
                product_analysis=product_str
            )

            contents = [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]

            response = await self._generate_content(contents, context="script_fix")
            text = self._extract_text_from_response(response)
            result = self._parse_structured_output(text)

            fixed_count = len(result.get("shots", []))
            logger.info(f"Script fix completed: {fixed_count} shots in fixed script")
            return result

        except Exception as e:
            logger.error(f"Script fix failed: {e}")
            raise
