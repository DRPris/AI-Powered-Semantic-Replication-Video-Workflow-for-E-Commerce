"""
通义万相 Wan 2.7 图生视频服务封装（DashScope 百炼平台）

平台：阿里云百炼 DashScope
认证方式：Bearer {DASHSCOPE_API_KEY}，可复用 QWEN_API_KEY（同源）
模型：wan2.7-i2v-2026-04-25（支持首帧、首尾帧、视频续写）

核心接口：
- POST /api/v1/services/aigc/video-generation/video-synthesis  创建异步任务
- GET  /api/v1/tasks/{task_id}  查询任务状态

用途：作为 Kling 官方 API 的兜底平台，单镜头 Kling 失败时自动接管。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from config import settings


logger = logging.getLogger(__name__)


class WanService:
    """通义万相 Wan 2.7 图生视频封装（DashScope 异步协议）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.WAN_API_KEY or settings.QWEN_API_KEY
        if not self.api_key:
            raise ValueError(
                "Wan 兜底需要 WAN_API_KEY 或 QWEN_API_KEY（DashScope 百炼），请在 .env 中配置"
            )
        self.base_url = (base_url or settings.WAN_BASE_URL or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
        self.model = model or settings.WAN_MODEL or "wan2.7-i2v-2026-04-25"
        self.resolution = resolution or settings.WAN_RESOLUTION or "720P"

    def _headers_create(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
            "Content-Type": "application/json",
        }

    def _headers_query(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ---------------------------------------------------------------
    # 创建图生视频任务
    # ---------------------------------------------------------------
    async def submit_image_to_video(
        self,
        prompt: str,
        first_frame_url: Optional[str] = None,
        last_frame_url: Optional[str] = None,
        negative_prompt: str = "",
        duration: int = 5,
        timeout: float = 30.0,
    ) -> str:
        """提交 wan2.7-i2v 任务，返回 task_id。

        媒体组合规则：
        - 仅 first_frame：首帧生视频
        - first_frame + last_frame：首尾帧生视频（本项目 Kling 双锚定对齐）
        """
        if not first_frame_url:
            raise ValueError("Wan 图生视频至少需要 first_frame_url（首帧）")

        media = [{"type": "first_frame", "url": first_frame_url}]
        if last_frame_url:
            media.append({"type": "last_frame", "url": last_frame_url})

        # wan2.7 duration 支持 5 / 10（与 Kling 对齐）
        wan_duration = 10 if int(duration) > 7 else 5

        body: dict = {
            "model": self.model,
            "input": {
                "prompt": (prompt or "")[:5000],
                "media": media,
            },
            "parameters": {
                "resolution": self.resolution,
                "duration": wan_duration,
                "prompt_extend": False,
                "watermark": False,
            },
        }
        if negative_prompt:
            body["input"]["negative_prompt"] = negative_prompt[:500]

        url = f"{self.base_url}/services/aigc/video-generation/video-synthesis"
        logger.info(
            f"[Wan] createTask url={url} model={self.model} duration={wan_duration} "
            f"resolution={self.resolution} last_frame={'Y' if last_frame_url else 'N'}"
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=self._headers_create(), json=body)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}

            logger.info(f"[Wan] createTask resp status={resp.status_code} body={data}")

            if resp.status_code != 200:
                raise RuntimeError(f"Wan createTask HTTP {resp.status_code}: {data}")
            task_id = (data.get("output") or {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"Wan createTask 响应缺少 task_id: {data}")
            return task_id

    # ---------------------------------------------------------------
    # 查询任务状态
    # ---------------------------------------------------------------
    async def query_task(self, task_id: str, timeout: float = 30.0) -> dict:
        """查询 wan2.7 任务状态，归一化为统一结构。

        DashScope 任务状态：PENDING / RUNNING / SUCCEEDED / FAILED / CANCELED / UNKNOWN
        """
        url = f"{self.base_url}/tasks/{task_id}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=self._headers_query())
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

            out = data.get("output") or {}
            raw = (out.get("task_status") or "").upper()
            state_map = {
                "PENDING": "PENDING",
                "RUNNING": "IN_PROGRESS",
                "SUCCEEDED": "SUCCESS",
                "FAILED": "FAILED",
                "CANCELED": "FAILED",
                "UNKNOWN": "UNKNOWN",
            }
            status = state_map.get(raw, "UNKNOWN")
            video_url = None
            error = None
            if status == "SUCCESS":
                video_url = out.get("video_url") or out.get("output_video_url")
            elif status == "FAILED":
                error = out.get("message") or out.get("code") or "Wan 任务失败"

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
        """轮询直到任务终态。返回 {"video_url", "last_frame_image_url": None} 对齐 VideoGenService。"""
        elapsed = 0
        current_interval = poll_interval
        unknown_count = 0
        while elapsed < max_wait:
            await asyncio.sleep(current_interval)
            elapsed += current_interval

            result = await self.query_task(task_id)
            status = result["status"]
            logger.info(
                f"[Wan] 任务 {task_id} 状态: {status} (raw={result.get('raw_status')}) 已等待 {elapsed}s"
            )

            if status == "SUCCESS":
                if not result["video_url"]:
                    raise RuntimeError(f"Wan 任务完成但无 video_url: {task_id}")
                return {"video_url": result["video_url"], "last_frame_image_url": None}
            if status == "FAILED":
                raise RuntimeError(f"Wan 任务失败: {result.get('error', '未知错误')}")
            if status == "UNKNOWN":
                unknown_count += 1
                if unknown_count >= 3:
                    raise RuntimeError(
                        f"Wan 任务连续 {unknown_count} 次返回未知状态: {result.get('raw_status')}"
                    )
            else:
                unknown_count = 0

            current_interval = min(int(current_interval * 1.2), 60)

        raise TimeoutError(f"Wan 任务超时: {task_id}，已等待 {elapsed}s")

    # ---------------------------------------------------------------
    # 组合接口：一步生成并等待完成
    # ---------------------------------------------------------------
    async def generate_and_wait(
        self,
        prompt: str,
        first_frame_url: Optional[str] = None,
        last_frame_url: Optional[str] = None,
        negative_prompt: str = "",
        duration: int = 5,
        poll_interval: int = 15,
        max_wait: int = 900,
    ) -> dict:
        """一步式：提交任务并等待完成，返回 {"video_url", "last_frame_image_url": None, "task_id"}。"""
        task_id = await self.submit_image_to_video(
            prompt=prompt,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            negative_prompt=negative_prompt,
            duration=duration,
        )
        result = await self.wait_for_completion(
            task_id=task_id, poll_interval=poll_interval, max_wait=max_wait
        )
        result["task_id"] = task_id
        return result
