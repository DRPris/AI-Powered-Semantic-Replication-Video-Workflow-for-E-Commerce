"""
阶段 4.5：复刻剪辑 Agent 编排入口
=================================

位置：Stage 4（视频生成）完成后、Stage 5（合成）开始前。

职责：
1. 从 Airtable 获取审核通过的镜头列表（顺序）。
2. 并发探测每个生成 clip 的实际时长（ffprobe / _get_video_duration）。
3. 加载 rhythm_analysis + video_analysis。
4. 调用 ClipEditorAgent.plan() 为每个镜头产出 edit_plan。
5. 将 edit_plan 写回 Airtable Shots 表"剪辑指令"字段，Stage 5 读取并执行。

任何异常都降级为：不写入 edit_plan → Stage 5 走老逻辑（向下兼容）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from config import settings
from agents.clip_editor_agent import ClipEditorAgent
from services.airtable_service import AirtableService
from services.ffmpeg_service import FFmpegService
from services.oss_service import OSSService

logger = logging.getLogger(__name__)


async def run_clip_editing(
    project_id: str,
    enable_llm_semantic_pick: bool = False,
    enable_speed_adjust: bool = False,
) -> dict[str, Any]:
    """阶段 4.5：为项目产出 edit_plans 并写回 Airtable。

    Args:
        project_id: 项目 ID
        enable_llm_semantic_pick: 是否启用 Gemini 语义选段（Phase 2+）
        enable_speed_adjust: 是否启用变速策略（Phase 3+）

    Returns:
        {
            "project_id": ...,
            "total_shots": ...,
            "success_count": ...,
            "source_total_duration": ...,
            "target_total_duration": ...,
            "expected_output_duration": ...,
            "edit_plans": [ {...}, ... ],
            "warnings": [ ... ],
            "airtable_write_count": N,  # 成功写入 Airtable 的镜头数
        }
    """
    logger.info(f"[Stage 4.5] 开始复刻剪辑规划 - 项目 {project_id}")

    # 1. 初始化服务
    airtable = AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
    )
    ffmpeg = FFmpegService(
        ffmpeg_bin=settings.FFMPEG_BIN_PATH,
        temp_dir=settings.FFMPEG_TEMP_DIR,
    )
    oss = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )

    # 2. 获取审核通过镜头
    all_shots = await airtable.get_project_shots(project_id)
    if not all_shots:
        raise ValueError(f"项目 {project_id} 没有找到任何分镜")

    approved_shots = [
        s for s in all_shots
        if (s.get("fields", {}) or {}).get("视频审核状态") == "已通过"
    ]
    if not approved_shots:
        raise ValueError(f"项目 {project_id} 没有审核通过的分镜")

    approved_shots.sort(key=lambda x: (x.get("fields", {}) or {}).get("镜头序号", 0))
    logger.info(f"[Stage 4.5] {len(approved_shots)} 个审核通过镜头待规划")

    # 3. 加载 rhythm_analysis + video_analysis（复用 stage5 的加载函数）
    # 注：这里 lazy import 避免循环引用
    from workflows.stage5_composition import _load_rhythm_analysis, _load_video_analysis

    rhythm_analysis = await _load_rhythm_analysis(airtable, project_id)
    video_analysis = await _load_video_analysis(airtable, project_id)

    if not rhythm_analysis:
        logger.warning(
            f"[Stage 4.5] 项目 {project_id} 未找到节奏分析数据，将使用默认时长兜底"
        )

    # 4. 并发探测每个生成 clip 的时长
    source_durations = await _probe_source_durations(
        approved_shots=approved_shots,
        project_id=project_id,
        oss=oss,
        ffmpeg=ffmpeg,
    )

    # 5. 调用 Agent 规划
    agent = ClipEditorAgent(
        rhythm_analysis=rhythm_analysis,
        video_analysis=video_analysis,
        enable_llm_semantic_pick=enable_llm_semantic_pick,
        enable_speed_adjust=enable_speed_adjust,
    )
    response = await agent.plan(
        approved_shots=approved_shots,
        source_durations=source_durations,
        project_id=project_id,
    )

    # 6. 写回 Airtable
    airtable_write_count = 0
    for shot, plan in zip(approved_shots, response.edit_plans):
        shot_id = shot.get("id")
        if not shot_id:
            continue
        try:
            ok = await airtable.update_shot_edit_plan(
                shot_id=shot_id,
                edit_plan=plan.model_dump(),
                source_duration=plan.source_duration,
                target_duration=plan.target_duration,
            )
            if ok:
                airtable_write_count += 1
        except Exception as e:
            logger.warning(
                f"[Stage 4.5] 镜头 {plan.shot_number} 写回 Airtable 失败（不阻塞）: {e}"
            )

    logger.info(
        f"[Stage 4.5] 完成：规划 {response.total_shots} 镜头，"
        f"写入 Airtable {airtable_write_count}/{response.total_shots}，"
        f"源时长 {response.source_total_duration:.2f}s -> "
        f"目标 {response.target_total_duration:.2f}s "
        f"(预估输出 {response.expected_output_duration:.2f}s)"
    )

    result = response.model_dump()
    result["airtable_write_count"] = airtable_write_count
    return result


async def _probe_source_durations(
    approved_shots: list[dict],
    project_id: str,
    oss: OSSService,
    ffmpeg: FFmpegService,
) -> list[float]:
    """并发下载每个镜头的 clip 并探测实际时长。

    使用 OSS 签名 URL 下载到 tmp/clip_probe_{N}.mp4，探测完立即删除。
    """
    temp_dir = Path(settings.FFMPEG_TEMP_DIR) / "clip_probe"
    temp_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(3)  # 最多 3 并发下载

    async def _probe_one(idx: int, shot: dict) -> float:
        shot_fields = shot.get("fields", {}) or {}
        shot_number = int(shot_fields.get("镜头序号", idx + 1) or (idx + 1))
        oss_key = f"videos/{project_id}/shot_{shot_number}.mp4"

        async with sem:
            try:
                url = oss.get_signed_url(oss_key, expires=3600)
            except Exception as e:
                logger.warning(f"[Stage 4.5] 镜头 {shot_number} 生成 OSS URL 失败: {e}")
                return 0.0

            tmp_path = temp_dir / f"probe_{project_id}_{shot_number}.mp4"
            try:
                timeout = httpx.Timeout(120.0, connect=30.0)
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    tmp_path.write_bytes(resp.content)
                duration = await ffmpeg._get_video_duration(str(tmp_path))
                return float(duration or 0.0)
            except Exception as e:
                logger.warning(f"[Stage 4.5] 镜头 {shot_number} 探测时长失败: {e}")
                return 0.0
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass

    durations = await asyncio.gather(
        *[_probe_one(i, s) for i, s in enumerate(approved_shots)]
    )

    logger.info(
        f"[Stage 4.5] 探测镜头时长: {[f'{d:.2f}s' for d in durations]}"
    )
    return list(durations)


# 对外暴露的别名，保持与其它 stage 命名一致
stage4_5_clip_editing = run_clip_editing
