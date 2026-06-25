"""
可灵（Kling）官方 API 服务封装

平台：国内版 klingai.com
认证方式：AccessKey + SecretKey 生成 HS256 JWT → Authorization: Bearer {jwt}

核心接口：
- POST /v1/videos/image2video  创建图生视频任务（支持 image_tail 尾帧锚定）
- GET  /v1/videos/image2video/{task_id}  查询任务状态

与 KIE AI 中转不同点：
1. URL 不同（api.klingai.com vs api.kie.ai）
2. 认证不同（JWT vs Bearer static）
3. 参数名不同：image / image_tail（可灵官方） vs image_urls / last_frame_url（KIE 中转）
4. 响应结构不同
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from config import settings


logger = logging.getLogger(__name__)


class KlingOfficialService:
    """可灵官方 API 封装（国内版 klingai.com）"""

    def __init__(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.access_key = access_key or settings.KLING_ACCESS_KEY
        self.secret_key = secret_key or settings.KLING_SECRET_KEY
        if not self.access_key or not self.secret_key:
            raise ValueError(
                "Kling 官方 API 需要 KLING_ACCESS_KEY 和 KLING_SECRET_KEY，请在 .env 中配置"
            )
        self.base_url = (base_url or settings.KLING_BASE_URL or "https://api-beijing.klingai.com").rstrip("/")
        self.model = model or settings.KLING_MODEL or "kling-v1-6"

    # ---------------------------------------------------------------
    # JWT 签名
    # ---------------------------------------------------------------
    def _gen_jwt(self, ttl: int = 1800) -> str:
        """生成 HS256 JWT。

        可灵官方要求 payload 至少包含：
          iss = access_key
          exp = now + ttl
          nbf = now - 5  （避免服务器时钟漂移）
        """
        try:
            import jwt  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "缺少 PyJWT 依赖，请执行: pip install PyJWT==2.8.0"
            ) from e

        now = int(time.time())
        headers = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "iss": self.access_key,
            "exp": now + ttl,
            "nbf": now - 5,
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS256", headers=headers)
        # PyJWT 1.x 返回 bytes，2.x 返回 str
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._gen_jwt()}",
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------
    # 创建图生视频任务
    # ---------------------------------------------------------------
    async def submit_image_to_video(
        self,
        prompt: str,
        image_url: Optional[str] = None,
        image_tail_url: Optional[str] = None,
        negative_prompt: str = "",
        duration: int = 5,
        aspect_ratio: str = "9:16",
        cfg_scale: float = 0.5,
        mode: Optional[str] = None,
        external_task_id: Optional[str] = None,
        timeout: float = 30.0,
    ) -> str:
        """提交 image2video 任务，返回 task_id。

        Args:
            prompt: 文字描述（最多 2500 字符）
            image_url: 首帧图片 URL（image2video 必填，除非用 text2video）
            image_tail_url: 尾帧图片 URL，启用首尾双锚定
            negative_prompt: 负面提示词
            duration: 视频时长，可灵仅支持 5 或 10 秒
            aspect_ratio: 宽高比，仅支持 "16:9" / "9:16" / "1:1"
            cfg_scale: 灵活度，0.0-1.0（越低越贴合 prompt）
            mode: "std"（标准） / "pro"（高清）
            external_task_id: 业务方自定义 ID，便于对账
        """
        if not image_url:
            raise ValueError("Kling image2video 至少需要 image_url（首帧）")

        # mode 从 settings.KLING_MODE 读取默认值（允许调用方显式覆盖）
        effective_mode = (mode or settings.KLING_MODE or "std").lower()
        if effective_mode not in ("std", "pro"):
            effective_mode = "std"
        # 已知限制：部分旧模型 + image_tail 仅支持 pro 模式（如 kling-v1-6）
        # 新一代模型（kling-v2*、kling-v3）std 即支持首尾双锚定，无需升级
        _require_pro_with_tail = {"kling-v1-6"}
        if image_tail_url and self.model in _require_pro_with_tail and effective_mode == "std":
            logger.warning(
                f"[KlingOfficial] {self.model} + image_tail 必须 pro 模式，自动从 std 升级为 pro"
            )
            effective_mode = "pro"

        # 可灵 duration 只支持 5 或 10
        kl_duration = "10" if int(duration) > 7 else "5"

        # aspect_ratio 归一化（可灵只支持这三种）
        ar = aspect_ratio if aspect_ratio in ("16:9", "9:16", "1:1") else "9:16"

        body: dict = {
            "model_name": self.model,
            "image": image_url,
            "prompt": (prompt or "")[:2500],
            "cfg_scale": max(0.0, min(1.0, float(cfg_scale))),
            "mode": effective_mode,
            "duration": kl_duration,
            "aspect_ratio": ar,
        }
        if image_tail_url:
            body["image_tail"] = image_tail_url
        if negative_prompt:
            body["negative_prompt"] = negative_prompt[:2500]
        if external_task_id:
            body["external_task_id"] = external_task_id

        url = f"{self.base_url}/v1/videos/image2video"
        logger.info(
            f"[KlingOfficial] createTask url={url} model={self.model} "
            f"duration={kl_duration} ar={ar} mode={effective_mode} image_tail={'Y' if image_tail_url else 'N'}"
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}

            logger.info(f"[KlingOfficial] createTask resp status={resp.status_code} body={data}")

            if resp.status_code != 200:
                raise RuntimeError(f"Kling createTask HTTP {resp.status_code}: {data}")
            if data.get("code") not in (0, "0"):
                raise RuntimeError(
                    f"Kling createTask 失败: code={data.get('code')} msg={data.get('message')}"
                )
            task_id = (data.get("data") or {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"Kling createTask 响应缺少 task_id: {data}")
            return task_id

    # ---------------------------------------------------------------
    # 查询任务状态
    # ---------------------------------------------------------------
    async def query_task(self, task_id: str, timeout: float = 30.0) -> dict:
        """查询 image2video 任务状态，归一化为统一结构。

        Returns:
            {
              "status": "PENDING|IN_PROGRESS|SUCCESS|FAILED",
              "video_url": str | None,
              "error": str | None,
              "raw_status": str,
            }
        """
        url = f"{self.base_url}/v1/videos/image2video/{task_id}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=self._headers())
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}

            if resp.status_code != 200:
                return {
                    "status": "FAILED",
                    "video_url": None,
                    "error": f"HTTP {resp.status_code}: {data}",
                    "raw_status": "http_error",
                }
            if data.get("code") not in (0, "0"):
                return {
                    "status": "FAILED",
                    "video_url": None,
                    "error": f"code={data.get('code')} msg={data.get('message')}",
                    "raw_status": "code_error",
                }

            d = data.get("data") or {}
            raw = d.get("task_status", "unknown")
            state_map = {
                "submitted": "PENDING",
                "queued": "PENDING",
                "processing": "IN_PROGRESS",
                "succeed": "SUCCESS",
                "success": "SUCCESS",
                "failed": "FAILED",
                "fail": "FAILED",
            }
            status = state_map.get(raw, "UNKNOWN")
            video_url = None
            error = None
            if status == "SUCCESS":
                videos = ((d.get("task_result") or {}).get("videos")) or []
                if videos:
                    video_url = videos[0].get("url")
            elif status == "FAILED":
                error = d.get("task_status_msg") or "Kling 任务失败"

            return {
                "status": status,
                "video_url": video_url,
                "error": error,
                "raw_status": raw,
            }

    # ---------------------------------------------------------------
    # 轮询等待完成
    # ---------------------------------------------------------------
    async def wait_for_completion(
        self,
        task_id: str,
        poll_interval: int = 15,
        max_wait: int = 900,
    ) -> dict:
        """轮询直到任务终态。

        Returns:
            {"video_url": str, "last_frame_image_url": None}  兼容 VideoGenService 返回签名
        """
        elapsed = 0
        current_interval = poll_interval
        unknown_count = 0
        while elapsed < max_wait:
            await asyncio.sleep(current_interval)
            elapsed += current_interval

            result = await self.query_task(task_id)
            status = result["status"]
            logger.info(
                f"[KlingOfficial] 任务 {task_id} 状态: {status} (raw={result.get('raw_status')}) 已等待 {elapsed}s"
            )

            if status == "SUCCESS":
                if not result["video_url"]:
                    raise RuntimeError(f"Kling 任务完成但无 video_url: {task_id}")
                return {"video_url": result["video_url"], "last_frame_image_url": None}
            if status == "FAILED":
                raise RuntimeError(f"Kling 任务失败: {result.get('error', '未知错误')}")
            if status == "UNKNOWN":
                unknown_count += 1
                if unknown_count >= 3:
                    raise RuntimeError(
                        f"Kling 任务连续 {unknown_count} 次返回未知状态: {result.get('raw_status')}"
                    )
            else:
                unknown_count = 0

            current_interval = min(int(current_interval * 1.2), 60)

        raise TimeoutError(f"Kling 任务超时: {task_id}，已等待 {elapsed}s")

    # ---------------------------------------------------------------
    # 组合接口：兼容 VideoGenService.generate_video 的调用签名
    # ---------------------------------------------------------------
    async def generate_video(
        self,
        model: Optional[str] = None,
        prompt: str = "",
        image_urls=None,
        first_frame_url: Optional[str] = None,
        last_frame_url: Optional[str] = None,
        duration: int = 5,
        aspect_ratio: str = "9:16",
        negative_prompt: str = "",
        mode: Optional[str] = None,
    ) -> str:
        """与 VideoGenService.generate_video 签名一致，方便在 Stage4 无缝替换。"""
        image_url = first_frame_url
        if not image_url and image_urls:
            image_url = image_urls[0] if isinstance(image_urls, (list, tuple)) else image_urls

        # model 参数兼容：stage4 传入的可能是 VideoModel 枚举（如 "kling-3.0/video"），
        # 这是 KIE AI 中转平台的模型名格式，与可灵官方 API 的 model_name 不兼容。
        # 官方 API 合法模型名：kling-v1 / kling-v1-5 / kling-v1-6 / kling-v2-master / kling-v2-1 / kling-v3 等。
        # 策略：只接受形如 "kling-v*" 或 "kling-v3" 的官方模型名（无 "/"），其他值忽略并使用 self.model（来自 KLING_MODEL）。
        effective_model = None
        if model:
            m_str = model.value if hasattr(model, "value") else str(model)
            if m_str.startswith("kling-v") and "/" not in m_str:
                effective_model = m_str
            else:
                logger.info(
                    f"[KlingOfficial] 忽略不兼容的 model={m_str}，使用官方模型名 {self.model}"
                )

        if effective_model:
            prev_model = self.model
            self.model = effective_model
            try:
                task_id = await self.submit_image_to_video(
                    prompt=prompt,
                    image_url=image_url,
                    image_tail_url=last_frame_url,
                    negative_prompt=negative_prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    mode=mode,
                )
            finally:
                self.model = prev_model
            return task_id

        return await self.submit_image_to_video(
            prompt=prompt,
            image_url=image_url,
            image_tail_url=last_frame_url,
            negative_prompt=negative_prompt,
            duration=duration,
            aspect_ratio=aspect_ratio,
            mode=mode,
        )

    async def check_status(self, task_id: str) -> dict:
        """与 VideoGenService.check_status 签名兼容。"""
        return await self.query_task(task_id)
