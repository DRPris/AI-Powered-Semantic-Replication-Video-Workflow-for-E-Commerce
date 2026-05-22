"""
Suno AI 音乐生成服务
通过 Suno API 生成纯音乐 BGM，供 Stage 5 成片混入
"""

import asyncio
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


class SunoService:
    """Suno AI BGM 生成服务封装"""

    def __init__(self):
        self.api_key = settings.SUNO_API_KEY
        self.base_url = settings.SUNO_BASE_URL.rstrip("/")
        self.model = settings.SUNO_MODEL

    @property
    def is_configured(self) -> bool:
        """检查 Suno API 是否已配置"""
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def generate_bgm(
        self,
        style: str,
        title: str = "Background Music",
        prompt: str = "",
    ) -> Optional[str]:
        """
        生成纯音乐 BGM，返回音频下载 URL。

        Args:
            style: 音乐风格描述，如 "Electronic, Upbeat, 120 BPM, Commercial"
            title: 音乐标题
            prompt: 额外提示词（customMode=True + instrumental=True 时不需要 lyrics prompt）

        Returns:
            音频下载 URL (mp3)，失败返回 None
        """
        if not self.is_configured:
            logger.warning("[Suno] API Key 未配置，跳过 BGM 生成")
            return None

        try:
            task_id = await self._submit_generation(style, title)
            if not task_id:
                return None

            audio_url = await self._poll_until_complete(task_id)
            return audio_url

        except Exception as e:
            logger.error(f"[Suno] BGM 生成异常: {e}")
            return None

    async def _submit_generation(self, style: str, title: str) -> Optional[str]:
        """提交音乐生成任务，返回 taskId"""
        payload = {
            "customMode": True,
            "instrumental": True,
            "model": self.model,
            "style": style,
            "title": title,
        }

        logger.info(f"[Suno] 提交 BGM 生成任务: style='{style}', title='{title}', model={self.model}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/generate",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 200:
            logger.error(f"[Suno] 提交任务失败: {data}")
            return None

        task_id = data.get("data", {}).get("taskId")
        logger.info(f"[Suno] 任务已提交: taskId={task_id}")
        return task_id

    async def _poll_until_complete(
        self, task_id: str, max_wait_sec: int = 300, poll_interval: int = 10
    ) -> Optional[str]:
        """
        轮询任务状态直到完成，返回第一首歌的音频 URL。

        Args:
            task_id: 生成任务 ID
            max_wait_sec: 最大等待时间（秒），默认 5 分钟
            poll_interval: 轮询间隔（秒）

        Returns:
            音频下载 URL，超时或失败返回 None
        """
        elapsed = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while elapsed < max_wait_sec:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                try:
                    response = await client.get(
                        f"{self.base_url}/api/v1/generate/record-info",
                        headers=self._headers(),
                        params={"taskId": task_id},
                    )
                    response.raise_for_status()
                    data = response.json()
                except Exception as e:
                    logger.warning(f"[Suno] 轮询失败 (elapsed={elapsed}s): {e}")
                    continue

                if data.get("code") != 200:
                    logger.warning(f"[Suno] 轮询响应异常: {data}")
                    continue

                records = data.get("data", {}).get("data", [])
                if not records:
                    logger.debug(f"[Suno] 等待生成中... ({elapsed}s)")
                    continue

                # 检查第一首歌的状态
                first_record = records[0]
                status = first_record.get("status", "")

                if status == "complete":
                    audio_url = first_record.get("audio_url") or first_record.get("song_url")
                    if audio_url:
                        logger.info(f"[Suno] BGM 生成完成 ({elapsed}s): {audio_url[:80]}...")
                        return audio_url
                    else:
                        logger.error(f"[Suno] 生成完成但未获取到音频 URL: {first_record}")
                        return None
                elif status in ("error", "failed"):
                    logger.error(f"[Suno] 生成失败: {first_record.get('error_message', 'unknown')}")
                    return None
                else:
                    logger.debug(f"[Suno] 生成中 status={status} ({elapsed}s)")

        logger.error(f"[Suno] BGM 生成超时 ({max_wait_sec}s)")
        return None
