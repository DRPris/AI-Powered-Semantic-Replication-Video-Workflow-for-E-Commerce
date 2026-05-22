"""
ElevenLabs Sound Effects 服务封装。

核心能力：
1. 根据英文 prompt 生成短时环境音（mp3），最长 22s（ElevenLabs 限制）。
2. 本地内容寻址缓存（hash(prompt+duration) → mp3），避免重复调用。
3. 可复用项目的 HTTP 代理（SOCKS5）。

API 参考：POST {base_url}/v1/sound-generation
    Headers: xi-api-key: <key>
    Body:
        {
            "text": "coffee shop chatter, espresso machine hiss",
            "duration_seconds": 5.0,        // 0.5 ~ 22.0
            "prompt_influence": 0.3         // 0.0 ~ 1.0
        }
    Response: audio/mpeg binary
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


class SoundEffectService:
    """ElevenLabs Sound Effects API 封装（带本地缓存）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        cache_dir: Optional[str] = None,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key or settings.ELEVENLABS_API_KEY
        self.base_url = (
            base_url or settings.ELEVENLABS_BASE_URL or "https://api.elevenlabs.io"
        ).rstrip("/")
        self.timeout = timeout

        cache_root = cache_dir or os.path.join(settings.FFMPEG_TEMP_DIR, "cache", "ambient_audio")
        self.cache_dir = Path(cache_root)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # 内部：缓存路径
    # ------------------------------------------------------------------
    def _cache_path(self, prompt: str, duration_sec: float, prompt_influence: float) -> Path:
        raw = f"{prompt.strip().lower()}|dur={duration_sec:.2f}|pi={prompt_influence:.2f}"
        h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"sfx_{h}.mp3"

    # ------------------------------------------------------------------
    # 主调用
    # ------------------------------------------------------------------
    async def generate_sound_effect(
        self,
        prompt: str,
        duration_sec: float,
        prompt_influence: float = 0.3,
        max_retries: int = 2,
    ) -> Optional[str]:
        """
        根据 prompt 生成环境音 mp3，返回本地文件路径。

        Args:
            prompt: 英文环境音描述（如 "kitchen range hood humming, oil sizzling"）
            duration_sec: 目标时长（秒），自动 clamp 到 [0.5, AMBIENT_MAX_DURATION_SEC]
            prompt_influence: 0.0-1.0，越高越贴 prompt；默认 0.3 保持自然质感
            max_retries: 失败重试次数

        Returns:
            本地 mp3 绝对路径；未配置 / 失败返回 None。
        """
        if not self.is_configured:
            logger.warning("ELEVENLABS_API_KEY 未配置，跳过环境音生成")
            return None

        if not prompt or not prompt.strip():
            logger.warning("ambient prompt 为空，跳过环境音生成")
            return None

        # 时长 clamp
        max_dur = float(settings.AMBIENT_MAX_DURATION_SEC or 22.0)
        dur = max(0.5, min(float(duration_sec), max_dur))

        # 命中缓存
        cache_file = self._cache_path(prompt, dur, prompt_influence)
        if cache_file.exists() and cache_file.stat().st_size > 1024:
            logger.info(f"命中环境音缓存: {cache_file.name} (prompt='{prompt[:40]}...', dur={dur:.2f}s)")
            return str(cache_file)

        url = f"{self.base_url}/v1/sound-generation"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": prompt.strip(),
            "duration_seconds": round(dur, 2),
            "prompt_influence": max(0.0, min(1.0, prompt_influence)),
        }

        last_err: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                logger.info(
                    f"[ElevenLabs SFX] attempt={attempt + 1}/{max_retries + 1} "
                    f"prompt='{prompt[:60]}...' dur={dur:.2f}s"
                )
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"ElevenLabs HTTP {resp.status_code}: {resp.text[:300]}"
                        )
                    audio_bytes = resp.content

                if not audio_bytes or len(audio_bytes) < 1024:
                    raise RuntimeError(f"ElevenLabs returned too-small payload ({len(audio_bytes)} bytes)")

                cache_file.write_bytes(audio_bytes)
                logger.info(
                    f"[ElevenLabs SFX] saved -> {cache_file} ({len(audio_bytes)} bytes)"
                )
                return str(cache_file)

            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    f"[ElevenLabs SFX] attempt {attempt + 1} failed: {e}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))

        logger.error(f"[ElevenLabs SFX] all retries failed, last error: {last_err}")
        return None
