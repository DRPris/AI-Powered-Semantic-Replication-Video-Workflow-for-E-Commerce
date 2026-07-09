"""Application readiness checks that do not call paid external APIs."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth import parse_api_keys


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checks: dict[str, dict[str, Any]]
    blocking_issues: list[str]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": self.checks,
            "blocking_issues": self.blocking_issues,
            "warnings": self.warnings,
        }


def _configured(value: str) -> bool:
    value = (value or "").strip()
    return bool(value and not value.startswith("your_"))


def build_readiness_report(settings: Any, project_root: Path) -> ReadinessReport:
    """Check local runtime and required configuration without network calls."""
    checks: dict[str, dict[str, Any]] = {}
    blocking: list[str] = []
    warnings: list[str] = []

    required_values = {
        "GEMINI_API_KEY": settings.GEMINI_API_KEY,
        "QWEN_API_KEY": settings.QWEN_API_KEY,
        "OSS_ACCESS_KEY_ID": settings.OSS_ACCESS_KEY_ID,
        "OSS_ACCESS_KEY_SECRET": settings.OSS_ACCESS_KEY_SECRET,
        "OSS_BUCKET_NAME": settings.OSS_BUCKET_NAME,
        "OSS_ENDPOINT": settings.OSS_ENDPOINT,
    }
    if getattr(settings, "DATA_BACKEND", "postgres") == "airtable":
        required_values.update(
            {
                "AIRTABLE_API_KEY": settings.AIRTABLE_API_KEY,
                "AIRTABLE_BASE_ID": settings.AIRTABLE_BASE_ID,
            }
        )
    if bool(getattr(settings, "API_AUTH_ENABLED", True)):
        required_values["API_KEYS"] = getattr(settings, "API_KEYS", "")
    missing = [name for name, value in required_values.items() if not _configured(value)]
    checks["core_configuration"] = {"passed": not missing, "missing": missing}
    if missing:
        blocking.append(f"缺少核心配置: {', '.join(missing)}")

    checks["api_auth"] = {
        "passed": not bool(getattr(settings, "API_AUTH_ENABLED", True))
        or bool(parse_api_keys(getattr(settings, "API_KEYS", ""))),
        "enabled": bool(getattr(settings, "API_AUTH_ENABLED", True)),
    }

    video_provider_ready = any(
        (
            _configured(settings.SEEDANCE_API_KEY),
            _configured(settings.KIE_API_KEY),
            _configured(settings.KLING_ACCESS_KEY)
            and _configured(settings.KLING_SECRET_KEY),
        )
    )
    checks["video_provider"] = {"passed": video_provider_ready}
    if not video_provider_ready:
        blocking.append("至少需要配置一个视频生成平台：Seedance、KIE 或 Kling")

    keyframe_enabled = bool(settings.ENABLE_KEYFRAME_STAGE)
    keyframe_provider_ready = _configured(settings.KIE_API_KEY)
    checks["keyframe_provider"] = {
        "passed": not keyframe_enabled or keyframe_provider_ready,
        "enabled": keyframe_enabled,
    }
    if keyframe_enabled and not keyframe_provider_ready:
        blocking.append("当前关键帧实现启用时需要 KIE_API_KEY")

    ffmpeg_path = shutil.which(settings.FFMPEG_BIN_PATH)
    checks["ffmpeg"] = {
        "passed": bool(ffmpeg_path),
        "configured_path": settings.FFMPEG_BIN_PATH,
        "resolved_path": ffmpeg_path,
    }
    if not ffmpeg_path:
        blocking.append(f"找不到 FFmpeg: {settings.FFMPEG_BIN_PATH}")

    runtime_dirs = [
        project_root / "static" / "frames",
        Path(settings.FFMPEG_TEMP_DIR),
    ]
    unwritable: list[str] = []
    for directory in runtime_dirs:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".readiness"
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
        except OSError:
            unwritable.append(str(directory))
    checks["runtime_directories"] = {
        "passed": not unwritable,
        "unwritable": unwritable,
    }
    if unwritable:
        blocking.append(f"运行目录不可写: {', '.join(unwritable)}")

    if not _configured(settings.ELEVENLABS_API_KEY) and settings.ENABLE_AMBIENT_AUDIO:
        warnings.append("环境音已启用但未配置 ELEVENLABS_API_KEY，将自动跳过")
    if not _configured(settings.SUNO_API_KEY) and settings.ENABLE_BGM:
        warnings.append("BGM 已启用但未配置 SUNO_API_KEY，将自动跳过")

    job_backend = getattr(settings, "JOB_BACKEND", "durable")
    checks["job_backend"] = {"passed": True, "backend": job_backend}
    if job_backend == "memory":
        warnings.append(
            "JOB_BACKEND=memory 仅限本地开发：任务重启即丢，生产必须使用 durable"
        )

    return ReadinessReport(
        ready=not blocking,
        checks=checks,
        blocking_issues=blocking,
        warnings=warnings,
    )


async def check_durable_infrastructure(settings: Any) -> dict[str, dict[str, Any]]:
    """Ping PostgreSQL and Redis when the durable job backend is enabled."""
    if settings.JOB_BACKEND != "durable":
        return {}

    checks: dict[str, dict[str, Any]] = {}
    try:
        from sqlalchemy import text

        from persistence.database import get_session_factory

        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = {"passed": True}
    except Exception as exc:
        checks["database"] = {
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        from services.durable_queue import DurableJobQueue

        queue = DurableJobQueue(settings.REDIS_URL, settings.JOB_QUEUE_NAME)
        try:
            passed = await queue.ping()
        finally:
            await queue.close()
        checks["redis"] = {"passed": passed}
    except Exception as exc:
        checks["redis"] = {
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return checks
