"""Top-level workflow orchestration shared by API background tasks and workers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from config import settings
from models.schemas import JobStatus, ReplicationMode
from workflows.stage1_preparation import run_stage1
from workflows.stage2_script import run_stage2
from workflows.stage3_5_keyframes import run_stage3_5
from workflows.stage3_prompts import run_stage3
from workflows.stage4_generation import run_stage4

logger = logging.getLogger(__name__)

JobUpdate = Callable[..., Awaitable[None]]


async def run_full_workflow(
    *,
    project_id: str,
    video_url: str,
    product_image_url: str,
    mode: ReplicationMode | str,
    product_listing_url: str | None,
    replicate_hook: bool | None,
    update_job: JobUpdate,
) -> None:
    """Run stages 1-4 and persist progress through the supplied job adapter."""
    mode_value = ReplicationMode(mode)
    try:
        await update_job(status=JobStatus.PROCESSING.value, progress=0.1)

        logger.info("[%s] 开始阶段一：素材分析", project_id)
        stage1_result = await run_stage1(
            project_id=project_id,
            video_url=video_url,
            product_image_url=product_image_url,
            mode=mode_value,
            product_listing_url=product_listing_url,
        )
        await update_job(status=JobStatus.PROCESSING.value, progress=0.3)

        has_hook = False
        hook_shot_numbers: list[int] = []
        video_analysis_result = stage1_result.get("video_analysis", {})
        if isinstance(video_analysis_result, dict):
            has_hook = video_analysis_result.get("has_hook", False)
            hook_shot_numbers = video_analysis_result.get("hook_shot_numbers", [])
            if not has_hook:
                for shot in video_analysis_result.get("shots", []):
                    action = shot.get("action", {})
                    if isinstance(action, dict) and action.get("shot_type") == "hook":
                        has_hook = True
                        hook_shot_numbers.append(shot.get("shot_number", 0))

        shot_type_summary = []
        for shot in video_analysis_result.get("shots", []):
            shot_number = shot.get("shot_number", "?")
            action = shot.get("action", {})
            shot_type = (
                action.get("shot_type", "unknown")
                if isinstance(action, dict)
                else "unknown"
            )
            shot_type_summary.append(f"shot_{shot_number}={shot_type}")
        logger.info(
            "[%s] Shot type 分类详情: %s",
            project_id,
            ", ".join(shot_type_summary),
        )

        if replicate_hook is None:
            replicate_hook = has_hook
        logger.info(
            "[%s] Hook 检测结果: has_hook=%s hook_shots=%s replicate_hook=%s",
            project_id,
            has_hook,
            hook_shot_numbers,
            replicate_hook,
        )

        logger.info("[%s] 开始阶段二：脚本生成", project_id)
        stage2_result = await run_stage2(
            project_id=project_id,
            mode=mode_value,
            replicate_hook=replicate_hook,
        )
        await update_job(status=JobStatus.PROCESSING.value, progress=0.5)

        logger.info("[%s] 开始阶段三：提示词生成", project_id)
        stage3_result = await run_stage3(project_id=project_id, mode=mode_value)
        await update_job(status=JobStatus.PROCESSING.value, progress=0.65)

        if settings.ENABLE_KEYFRAME_STAGE:
            logger.info("[%s] 开始阶段 3.5：关键帧生成", project_id)
            stage3_5_result = await run_stage3_5(project_id=project_id)
            if not stage3_5_result.get("skipped", False):
                await update_job(
                    status=JobStatus.WAITING_KEYFRAME_REVIEW.value,
                    progress=0.7,
                    result={
                        "project_id": project_id,
                        "message": "关键帧生成完成，请在 Airtable 中审核关键帧",
                        "next_step": (
                            "审核通过后调用 POST "
                            f"/api/v1/projects/{project_id}/approve-keyframes 继续"
                        ),
                        "keyframe_result": {
                            "total": stage3_5_result.get("total_shots", 0),
                            "successful": stage3_5_result.get("successful", 0),
                            "failed": stage3_5_result.get("failed", 0),
                        },
                    },
                )
                return

        await update_job(status=JobStatus.PROCESSING.value, progress=0.7)

        script_data = stage2_result.get("script", {}) if stage2_result else {}
        validation = script_data.get("_validation", {})
        audit_summary = (
            stage3_result.get("audit_summary", {})
            if isinstance(stage3_result, dict)
            else {}
        )
        rejected_count = audit_summary.get("rejected_count", 0)
        script_passed = (
            validation.get("passed", False)
            and validation.get("confidence", 0.0) >= 0.85
        )

        if script_passed and rejected_count == 0:
            await update_job(
                status=JobStatus.PROCESSING.value,
                progress=0.75,
                result={
                    "project_id": project_id,
                    "message": "脚本验证与分镜自审核均通过，自动进入视频生成",
                },
            )
            await run_stage4(project_id=project_id, platform="seedance")
            await update_job(
                status=JobStatus.COMPLETED.value,
                progress=1.0,
                result={
                    "project_id": project_id,
                    "message": "全流程自动完成（验证通过，跳过人审）",
                    "auto_validated": True,
                    "validation_confidence": validation.get("confidence", 0.0),
                },
            )
            return

        if not script_passed:
            reason = (
                "无脚本验证结果"
                if not validation
                else (
                    f"脚本验证 passed={validation.get('passed')}, "
                    f"confidence={validation.get('confidence', 0):.2f}"
                )
            )
        else:
            reason = (
                f"分镜提示词自审核驳回 {rejected_count} 个镜头: "
                f"{audit_summary.get('rejected_shot_numbers', [])}"
            )
        logger.info("[%s] 需要人工审核: %s", project_id, reason)
        await update_job(
            status=JobStatus.WAITING_REVIEW.value,
            progress=0.7,
            result={
                "project_id": project_id,
                "message": "阶段一~三已完成，请在 Airtable 中审核提示词",
                "next_step": "审核通过后调用 POST /api/v1/generate-shots",
                "validation": validation,
                "audit_summary": audit_summary,
            },
        )
    except Exception as exc:
        logger.exception("[%s] 工作流执行失败", project_id)
        await update_job(
            status=JobStatus.FAILED.value,
            error_code="WORKFLOW_EXECUTION_FAILED",
            error_message=str(exc),
        )
        raise
