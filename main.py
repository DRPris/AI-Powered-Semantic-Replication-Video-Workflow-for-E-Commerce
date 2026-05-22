"""
Video Replication Service - FastAPI 入口

语义复刻视频工作流服务
提供视频分析、脚本生成、提示词转换、视频生成、视频合成等 API
"""

import logging
import os
from typing import Any, Optional
import httpx

# 加载 .env 到 os.environ（确保代理变量等被 httpx 读取）
from dotenv import load_dotenv
load_dotenv(override=True)

from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from models.schemas import (
    AnalyzeVideoRequest,
    AnalyzeProductRequest,
    GenerateScriptRequest,
    ConvertPromptsRequest,
    GenerateShotsRequest,
    ComposeVideoRequest,
    JobStatusResponse,
    JobStatus,
    ReplicationMode,
    RhythmAnalysisRequest,
    RhythmAnalysisResponse,
    ClipEditorRequest,
    ClipEditorResponse,
)
from workflows import (
    run_stage1,
    run_stage2,
    run_stage3,
    run_stage3_5,
    run_stage4,
    run_clip_editing,
    run_stage5,
)
from services.airtable_service import AirtableService
from services.ffmpeg_service import FFmpegService
from services.oss_service import OSSService
from services.token_tracker import token_tracker

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================================================
# 请求模型定义
# ============================================================================

class StartWorkflowRequest(BaseModel):
    """启动工作流请求"""
    project_id: str = Field(..., description="项目ID")
    project_name: str = Field(default="", description="项目名称（可选）")
    video_url: str = Field(..., description="原始视频URL")
    product_image_url: str = Field(..., description="商品图片URL")
    mode: ReplicationMode = Field(default=ReplicationMode.FULL, description="复刻模式：simple 或 full")
    three_view_image_url: Optional[str] = Field(default=None, description="用户提供的三视图拼合图 URL")
    product_listing_url: Optional[str] = Field(default=None, description="商品详情页链接，用于提取卖点和产品形态信息")
    replicate_hook: Optional[bool] = Field(default=None, description="是否复刻 hook 镜头。True=复刻，False=跳过，None=自动检测后询问")


class ConfirmBriefRequest(BaseModel):
    """确认 Product Brief 请求：用户对待确认问题的答复"""
    user_answers: Optional[dict] = Field(default=None, description="用户答复字典，key=字段名，value=答复值。为空时从 Airtable “用户答复”字段读取")
    product_image_url: Optional[str] = Field(default=None, description="可选，指定商品图 URL；为空时从项目记录读取")
    product_listing_url: Optional[str] = Field(default=None, description="可选，指定商品链接")


# 创建 FastAPI 应用
app = FastAPI(
    title="Video Replication Service",
    description="语义复刻视频工作流服务 - 将成功视频创意复刻到新商品",
    version="1.0.0",
)

# 挂载静态文件目录（用于帧图片访问）
app.mount("/static", StaticFiles(directory="static"), name="static")

# 初始化 Airtable 服务
airtable_service = AirtableService(
    api_key=settings.AIRTABLE_API_KEY,
    base_id=settings.AIRTABLE_BASE_ID,
)

# 初始化 FFmpeg 服务
ffmpeg_service = FFmpegService(
    ffmpeg_bin=settings.FFMPEG_BIN_PATH,
    temp_dir=settings.FFMPEG_TEMP_DIR,
)

# 初始化 OSS 服务
oss_service = OSSService(
    access_key_id=settings.OSS_ACCESS_KEY_ID,
    access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
    bucket_name=settings.OSS_BUCKET_NAME,
    endpoint=settings.OSS_ENDPOINT,
    cdn_domain=settings.OSS_CDN_DOMAIN,
)


# ============================================================================
# 健康检查
# ============================================================================

@app.get("/health")
async def health_check() -> dict[str, str]:
    """服务健康检查"""
    return {"status": "healthy", "service": "video-replication-service"}


# ============================================================================
# 商品分析 Agent - Phase B 确认接口
# ============================================================================

@app.post("/api/v1/projects/{project_id}/confirm-brief")
async def confirm_product_brief(project_id: str, request: ConfirmBriefRequest):
    """用户确认 Product Brief 后触发 Phase B，产出最终 Brief。

    流程：
    1. 从 Airtable 读取项目中的 Brief 草稿
    2. 合并用户答复（优先用请求体，其次从 Airtable 用户答复字段读取）
    3. 运行 ProductBriefAgent.run_finalize 完成补全
    4. 保存最终 Brief，状态回到 ANALYZING（由外部编排继续工作流）
    """
    from agents import ProductBriefAgent
    from services.gemini_service import GeminiService
    from models.schemas import ProductBrief

    # 1) 读取项目与 Brief 状态
    project = await airtable_service.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

    state = await airtable_service.get_product_brief_state(project_id)
    draft_dict = state.get("draft")
    if not draft_dict:
        raise HTTPException(
            status_code=400,
            detail="No draft Product Brief found for this project; Phase A may not have run.",
        )

    # 2) 合并用户答复
    user_answers = request.user_answers or state.get("user_answers") or {}

    # 3) 解决商品图与链接 URL
    fields = project.get("fields", {}) or {}
    product_image_url = request.product_image_url or fields.get("产品图链接") or ""
    product_listing_url = request.product_listing_url

    try:
        draft_brief = ProductBrief(**draft_dict)
    except Exception as e:
        logger.warning(f"[{project_id}] draft brief schema invalid, using raw dict: {e}")
        draft_brief = ProductBrief()

    # 4) 运行 Phase B
    gemini = GeminiService()
    agent = ProductBriefAgent(
        gemini_service=gemini,
        product_image_url=product_image_url,
        product_listing_url=product_listing_url,
    )
    final_brief = await agent.run_finalize(draft_brief, user_answers=user_answers)
    final_dict = final_brief.model_dump()

    # 5) 写回 Airtable + 恢复状态
    await airtable_service.save_product_brief_finalized(project_id, final_dict)
    try:
        await airtable_service.update_project_status(
            project_id=project_id,
            status="ANALYZING",
        )
    except Exception as e:
        logger.warning(f"[{project_id}] failed to reset status after confirm-brief: {e}")

    return {
        "success": True,
        "project_id": project_id,
        "product_brief": final_dict,
        "message": "Phase B finalized. You may now resume the workflow.",
    }


# ============================================================================
# 工作流一键启动 API
# ============================================================================

@app.post("/api/v1/start-workflow")
async def start_workflow(request: StartWorkflowRequest, background_tasks: BackgroundTasks):
    """
    一键启动工作流。
    1. 创建 Airtable 项目记录
    2. 后台启动阶段一（素材分析）
    3. 返回 project_id 和 job_id
    """
    from services.job_manager import job_manager

    try:
        # 创建项目记录
        project = await airtable_service.create_project(
            name=request.project_name if request.project_name else f"project_{request.project_id}",
            video_url=request.video_url,
            product_image_url=request.product_image_url,
            mode=request.mode.value if hasattr(request.mode, 'value') else str(request.mode),
        )
        project_id = project["id"]

        # 如果用户上传了三视图，下载并上传到 OSS，然后创建素材记录
        if request.three_view_image_url:
            logger.info(f"[{project_id}] 检测到用户上传的三视图，开始处理: {request.three_view_image_url}")
            try:
                # 下载图片
                async with httpx.AsyncClient() as client:
                    response = await client.get(request.three_view_image_url, timeout=60)
                    response.raise_for_status()
                    image_bytes = response.content

                # 获取文件扩展名
                content_type = response.headers.get("content-type", "image/png")
                ext = content_type.split("/")[-1] if "/" in content_type else "png"
                if ext not in ["png", "jpg", "jpeg", "webp"]:
                    ext = "png"

                # 上传到 OSS
                oss_key = f"three_views/{project_id}/user_uploaded.{ext}"
                oss_url = await oss_service.upload_bytes(
                    data=image_bytes,
                    oss_key=oss_key,
                    content_type=content_type,
                    expires=86400 * 7  # 7天有效
                )
                logger.info(f"[{project_id}] 用户三视图已上传到 OSS: {oss_key}")

                # 在 Airtable Assets 表创建记录
                await airtable_service.create_asset(
                    project_id=project_id,
                    asset_type="三视图",
                    attachment_url=oss_url,
                    content='{"source": "user_uploaded", "description": "用户上传的三视图拼合图"}'
                )
                logger.info(f"[{project_id}] 用户三视图素材记录已创建")

            except Exception as e:
                logger.error(f"[{project_id}] 处理用户上传的三视图失败: {e}", exc_info=True)
                # 不阻塞工作流，继续执行

        # 创建后台任务
        job_id = job_manager.create_job()

        async def _run_workflow():
            try:
                job_manager.update_job(job_id, status="running", progress=0.1)

                # 阶段一：素材分析
                logger.info(f"[{project_id}] 开始阶段一：素材分析")
                stage1_result = await run_stage1(
                    project_id=project_id,
                    video_url=request.video_url,
                    product_image_url=request.product_image_url,
                    mode=request.mode,
                    product_listing_url=request.product_listing_url
                )
                job_manager.update_job(job_id, status="running", progress=0.3)

                # Hook 检测逻辑：确定是否复刻 hook
                replicate_hook = request.replicate_hook
                has_hook = False
                hook_shot_numbers = []

                # 从 Stage 1 结果中提取 hook 信息
                video_analysis_result = stage1_result.get("video_analysis", {})
                if isinstance(video_analysis_result, dict):
                    has_hook = video_analysis_result.get("has_hook", False)
                    hook_shot_numbers = video_analysis_result.get("hook_shot_numbers", [])
                    # 也检查 shots 中的 shot_type
                    if not has_hook:
                        for shot in video_analysis_result.get("shots", []):
                            action = shot.get("action", {})
                            if isinstance(action, dict) and action.get("shot_type") == "hook":
                                has_hook = True
                                hook_shot_numbers.append(shot.get("shot_number", 0))

                # 详细的 shot_type 分类日志
                shots_list = video_analysis_result.get("shots", [])
                shot_type_summary = []
                for shot in shots_list:
                    sn = shot.get("shot_number", "?")
                    action = shot.get("action", {})
                    st = action.get("shot_type", "unknown") if isinstance(action, dict) else "unknown"
                    shot_type_summary.append(f"shot_{sn}={st}")
                logger.info(
                    f"[{project_id}] Shot type 分类详情: {', '.join(shot_type_summary)}"
                )

                logger.info(
                    f"[{project_id}] Hook 检测结果: has_hook={has_hook}, "
                    f"hook_shots={hook_shot_numbers}, replicate_hook={replicate_hook}"
                )

                # 如果用户未指定且检测到 hook，默认复刻 hook
                if replicate_hook is None:
                    if has_hook:
                        replicate_hook = True
                        logger.info(f"[{project_id}] 检测到 hook 镜头，默认复刻 hook")
                    else:
                        replicate_hook = False
                        logger.info(f"[{project_id}] 未检测到 hook 镜头")

                # 阶段二：脚本生成
                logger.info(f"[{project_id}] 开始阶段二：脚本生成 (replicate_hook={replicate_hook})")
                stage2_result = await run_stage2(
                    project_id=project_id,
                    mode=request.mode,
                    replicate_hook=replicate_hook
                )
                job_manager.update_job(job_id, status="running", progress=0.5)

                # 阶段三：提示词生成
                logger.info(f"[{project_id}] 开始阶段三：提示词生成")
                stage3_result = await run_stage3(project_id=project_id, mode=request.mode)
                job_manager.update_job(job_id, status="running", progress=0.65)

                # 阶段 3.5：关键帧生成（如果启用）
                if settings.ENABLE_KEYFRAME_STAGE:
                    logger.info(f"[{project_id}] 开始阶段 3.5：关键帧生成")
                    stage3_5_result = await run_stage3_5(project_id=project_id)
                    skipped = stage3_5_result.get("skipped", False)
                    if not skipped:
                        # 关键帧生成完成，暂停等待人工审核关键帧
                        job_manager.update_job(
                            job_id,
                            status="waiting_keyframe_review",
                            progress=0.7,
                            result={
                                "project_id": project_id,
                                "message": "关键帧生成完成，请在 Airtable 中审核关键帧",
                                "next_step": "审核通过后调用 POST /api/v1/projects/{project_id}/approve-keyframes 继续",
                                "keyframe_result": {
                                    "total": stage3_5_result.get("total_shots", 0),
                                    "successful": stage3_5_result.get("successful", 0),
                                    "failed": stage3_5_result.get("failed", 0),
                                },
                            }
                        )
                        logger.info(f"[{project_id}] 关键帧生成完成，等待人工审核")
                        return  # 暂停工作流，等待 approve-keyframes 端点触发后续阶段
                    else:
                        logger.info(f"[{project_id}] 关键帧阶段已跳过（配置关闭）")
                else:
                    logger.info(f"[{project_id}] ENABLE_KEYFRAME_STAGE=False，跳过阶段 3.5")

                job_manager.update_job(job_id, status="running", progress=0.7)

                # 阶段三完成后，检查验证结果决定是否自动跳过人审
                validation = {}
                script_data = stage2_result.get("script", {}) if stage2_result else {}
                validation = script_data.get("_validation", {})
                # Stage 3 分镜提示词自审核结果
                audit_summary = stage3_result.get("audit_summary", {}) if isinstance(stage3_result, dict) else {}
                rejected_count = audit_summary.get("rejected_count", 0)
                script_passed = (
                    validation.get("passed", False)
                    and validation.get("confidence", 0.0) >= 0.85
                )
                auto_passed = script_passed and rejected_count == 0

                if auto_passed:
                    # 验证通过且置信度足够高，且分镜自审核无驳回，自动触发 Stage 4
                    logger.info(
                        f"[{project_id}] 脚本验证通过 (confidence={validation.get('confidence', 0):.2f}) "
                        f"且分镜自审核全部通过（rejected=0），自动跳过人审，触发 Stage 4"
                    )
                    job_manager.update_job(
                        job_id, status="running", progress=0.75,
                        result={"project_id": project_id, "message": "脚本验证与分镜自审核均通过，自动进入视频生成"}
                    )

                    # 注：Stage 3 已按镜头写入 “已通过/已驳回” 状态；这里不再无差别覆写。
                    # Stage 4 仅拉取 “提示词审核状态==已通过” 的分镜（stage4_generation.py 已有过滤）。

                    # 阶段四：视频生成
                    logger.info(f"[{project_id}] 开始阶段四：视频生成（自动触发）")
                    await run_stage4(project_id=project_id, platform="seedance")
                    job_manager.update_job(
                        job_id, status="completed", progress=1.0,
                        result={
                            "project_id": project_id,
                            "message": "全流程自动完成（验证通过，跳过人审）",
                            "auto_validated": True,
                            "validation_confidence": validation.get("confidence", 0.0)
                        }
                    )
                    logger.info(f"[{project_id}] 全流程自动完成")
                else:
                    # 验证未通过、置信度不够、或分镜自审核有驳回 → 进入人工审核
                    if not script_passed:
                        reason = "无脚本验证结果" if not validation else (
                            f"脚本验证 passed={validation.get('passed')}, confidence={validation.get('confidence', 0):.2f}"
                        )
                    else:
                        reason = (
                            f"分镜提示词自审核驳回 {rejected_count} 个镜头: "
                            f"{audit_summary.get('rejected_shot_numbers', [])}"
                        )
                    logger.info(f"[{project_id}] 需要人工审核: {reason}")
                    job_manager.update_job(
                        job_id,
                        status="waiting_review",
                        progress=0.7,
                        result={
                            "project_id": project_id,
                            "message": "阶段一~三已完成，请在 Airtable 中审核提示词",
                            "next_step": "审核通过后调用 POST /api/v1/generate-shots 开始视频生成",
                            "validation": validation,
                            "audit_summary": audit_summary,
                        }
                    )
                    logger.info(f"[{project_id}] 阶段一~三完成，等待人工审核提示词")

            except Exception as e:
                logger.error(f"[{project_id}] 工作流执行失败: {e}", exc_info=True)
                job_manager.update_job(job_id, status="failed", error=str(e))

        background_tasks.add_task(_run_workflow)

        return {
            "project_id": project_id,
            "job_id": job_id,
            "status": "started",
            "message": "工作流已启动，阶段一~三将在后台执行",
            "replicate_hook": request.replicate_hook,
        }

    except Exception as e:
        logger.error(f"启动工作流失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"启动工作流失败: {str(e)}")


# ============================================================================
# 节奏分析 API
# ============================================================================

@app.post("/api/v1/analyze-rhythm", response_model=RhythmAnalysisResponse)
async def analyze_rhythm(request: RhythmAnalysisRequest) -> RhythmAnalysisResponse:
    """
    视频节奏分析

    对视频的画面与声音进行联合分析，提取剪辑节奏结构。
    输出可供复刻剥辑阶段对照使用，确保复刻视频与原片节奏感对等。

    支持两种输入方式：
    - **video_url**：直接传公网可访问的视频 URL
    - **project_id**：传 Airtable 项目 ID，自动读取该项目的原始视频 URL
    """
    from services.gemini_service import GeminiService

    # 解析视频 URL
    video_url = request.video_url

    if not video_url and request.project_id:
        # 方式二：从 Airtable 项目表读取原始视频 URL
        logger.info(f"Fetching video URL from project: {request.project_id}")
        project = await airtable_service.get_project(request.project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"项目 {request.project_id} 不存在")

        fields = project.get("fields", {})
        # Airtable 附件字段格式：[{"url": "..."}]
        original_video = fields.get("原视频", [])
        if not original_video or not isinstance(original_video, list):
            raise HTTPException(
                status_code=422,
                detail=f"项目 {request.project_id} 未找到原始视频，请确认 Airtable 中已上传视频"
            )
        video_url = original_video[0].get("url", "")
        if not video_url:
            raise HTTPException(
                status_code=422,
                detail=f"项目 {request.project_id} 的视频附件缺少 URL"
            )
        logger.info(f"Resolved video URL from project {request.project_id}: {video_url}")

    logger.info(f"Starting rhythm analysis for: {video_url}")

    try:
        gemini = GeminiService()
        result = await gemini.analyze_rhythm(video_url)

        return RhythmAnalysisResponse(
            video_url=video_url,
            overview=result["overview"],
            audio=result["audio"],
            shots=result["shots"],
            rhythm_timeline=result["rhythm_timeline"],
            replication_rhythm_guide=result["replication_rhythm_guide"],
        )

    except KeyError as e:
        logger.error(f"Rhythm analysis response missing field: {e}")
        raise HTTPException(
            status_code=422,
            detail=f"节奏分析结果缺少字段: {e}，请确认视频内容有效"
        )
    except Exception as e:
        logger.error(f"Rhythm analysis failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"节奏分析失败: {str(e)}")


@app.post("/api/v1/analyze-rhythm/upload", response_model=RhythmAnalysisResponse)
async def analyze_rhythm_upload(
    file: UploadFile = File(..., description="视频文件，支持 mp4/mov/avi/webm")
) -> RhythmAnalysisResponse:
    """
    视频节奏分析（文件直传）

    直接上传本地视频文件进行节奏分析，无需公网 URL。
    适用于直接调试或视频暂无公网链接的场景。
    
    支持格式：mp4 / mov / avi / webm
    """
    from services.gemini_service import GeminiService

    # 校验文件类型
    ALLOWED_MIME_PREFIXES = ("video/",)
    content_type = file.content_type or "video/mp4"
    if not any(content_type.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型: {content_type}，请上传视频文件"
        )

    logger.info(f"Starting rhythm analysis from uploaded file: {file.filename} ({content_type})")

    try:
        video_bytes = await file.read()
        if not video_bytes:
            raise HTTPException(status_code=400, detail="上传的文件为空")

        gemini = GeminiService()
        result = await gemini.analyze_rhythm_from_bytes(video_bytes, mime_type=content_type)

        return RhythmAnalysisResponse(
            video_url=f"upload://{file.filename}",
            overview=result["overview"],
            audio=result["audio"],
            shots=result["shots"],
            rhythm_timeline=result["rhythm_timeline"],
            replication_rhythm_guide=result["replication_rhythm_guide"],
        )

    except HTTPException:
        raise
    except KeyError as e:
        logger.error(f"Rhythm analysis (upload) response missing field: {e}")
        raise HTTPException(
            status_code=422,
            detail=f"节奏分析结果缺少字段: {e}"
        )
    except Exception as e:
        logger.error(f"Rhythm analysis (upload) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"节奏分析失败: {str(e)}")


# ============================================================================
# 阶段一：素材分析 API
# ============================================================================

@app.post("/api/v1/analyze-video")
async def analyze_video(
    request: AnalyzeVideoRequest,
    background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """
    分析原始视频

    对原始视频进行深度分析，提取镜头结构、场景描述、运镜方式等信息
    """
    # 生成任务 ID
    import uuid
    job_id = str(uuid.uuid4())

    logger.info(f"Creating video analysis job {job_id} for video: {request.video_url}")

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING,
        "message": "Video analysis job created",
        "video_url": request.video_url,
    }


@app.post("/api/v1/analyze-product")
async def analyze_product(
    request: AnalyzeProductRequest,
    background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """
    分析商品图片

    分析商品图片，提取商品属性、卖点等信息
    """
    import uuid
    job_id = str(uuid.uuid4())

    logger.info(f"Creating product analysis job {job_id} for image: {request.product_image_url}")

    return {
        "job_id": job_id,
        "status": JobStatus.PENDING,
        "message": "Product analysis job created",
        "product_image_url": request.product_image_url,
    }


# ============================================================================
# 阶段二：脚本生成 API
# ============================================================================

@app.post("/api/v1/generate-script")
async def generate_script(
    request: GenerateScriptRequest,
    background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """
    生成视频复刻脚本

    基于视频分析和商品分析结果，生成适配新商品的复刻脚本
    """
    import uuid
    job_id = str(uuid.uuid4())

    logger.info(f"Creating script generation job {job_id} for project: {request.project_id}")

    # 在后台执行脚本生成
    background_tasks.add_task(
        run_stage2,
        project_id=request.project_id,
        mode=ReplicationMode.FULL
    )

    return {
        "job_id": job_id,
        "status": JobStatus.PROCESSING,
        "message": "Script generation job started",
        "project_id": request.project_id,
    }


# ============================================================================
# 阶段三：提示词转换 API
# ============================================================================

@app.post("/api/v1/convert-prompts")
async def convert_prompts(
    request: ConvertPromptsRequest,
    background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """
    转换提示词

    将脚本中的分镜描述转换为 AI 视频生成提示词
    """
    import uuid
    job_id = str(uuid.uuid4())

    logger.info(f"Creating prompt conversion job {job_id} for project: {request.project_id}")

    # 在后台执行提示词转换
    background_tasks.add_task(
        run_stage3,
        project_id=request.project_id,
        mode=ReplicationMode.FULL
    )

    return {
        "job_id": job_id,
        "status": JobStatus.PROCESSING,
        "message": "Prompt conversion job started",
        "project_id": request.project_id,
    }


# ============================================================================
# 阶段四：视频生成 API
# ============================================================================

@app.post("/api/v1/generate-shots")
async def generate_shots(request: GenerateShotsRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """
    触发视频生成（含首尾帧衔接）

    基于提示词批量生成所有分镜的视频片段
    """
    import uuid
    job_id = str(uuid.uuid4())

    # 初始化任务状态
    _generation_jobs[job_id] = {
        "job_id": job_id,
        "project_id": request.project_id,
        "status": JobStatus.PROCESSING,
        "progress": 0.0,
        "result": None,
        "message": "Video generation started",
    }

    logger.info(f"Creating video generation job {job_id} for project: {request.project_id}")

    async def _run():
        try:
            _generation_jobs[job_id]["progress"] = 0.1
            _generation_jobs[job_id]["message"] = "Starting video generation..."

            await run_stage4(
                project_id=request.project_id,
                platform=request.platform if hasattr(request, 'platform') else "seedance",
            )

            _generation_jobs[job_id].update({
                "status": JobStatus.COMPLETED,
                "progress": 1.0,
                "message": "Video generation completed successfully",
            })
        except Exception as e:
            logger.error(f"Generation job {job_id} failed: {str(e)}")
            _generation_jobs[job_id].update({
                "status": JobStatus.FAILED,
                "message": f"Video generation failed: {str(e)}",
            })

    background_tasks.add_task(_run)

    return {
        "job_id": job_id,
        "status": JobStatus.PROCESSING,
        "message": "Shot generation job started",
        "project_id": request.project_id,
    }


@app.get("/api/v1/generation-status/{job_id}")
async def get_generation_status(job_id: str) -> JobStatusResponse:
    """
    查询视频生成任务状态

    获取指定任务 ID 的生成进度和状态
    """
    if job_id not in _generation_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = _generation_jobs[job_id]
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        result=job.get("result"),
        message=job.get("message"),
    )


# ============================================================================
# 阶段五：视频合成 API
# ============================================================================

# 内存中的任务状态存储（生产环境应使用 Redis）
_composition_jobs: dict[str, dict[str, Any]] = {}
_generation_jobs: dict[str, dict[str, Any]] = {}


@app.post("/api/v1/edit-clips", response_model=ClipEditorResponse)
async def edit_clips(request: ClipEditorRequest) -> ClipEditorResponse:
    """阶段 4.5：复刻剪辑 Agent

    基于原视频节奏分析，为每个审核通过的镜头产出剪辑指令（edit_plan），
    写回 Airtable Shots 表 “剪辑指令” 字段；Stage 5 合成时读取并执行。
    应在 Stage 4 完成后、Stage 5 开始前调用。
    """
    logger.info(
        f"[edit-clips] project_id={request.project_id} "
        f"llm={request.enable_llm_semantic_pick} speed={request.enable_speed_adjust}"
    )
    try:
        result = await run_clip_editing(
            project_id=request.project_id,
            enable_llm_semantic_pick=request.enable_llm_semantic_pick,
            enable_speed_adjust=request.enable_speed_adjust,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"[edit-clips] failed: {e}")
        raise HTTPException(status_code=500, detail=f"clip editing failed: {e}")

    # result 包含 airtable_write_count，ClipEditorResponse 未声明该字段，值会被忽略
    return ClipEditorResponse(**{
        k: v for k, v in result.items() if k in ClipEditorResponse.model_fields
    })


@app.post("/api/v1/compose-video")
async def compose_video(
    request: ComposeVideoRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    触发视频合成

    将所有审核通过的分镜视频片段合成为完整的最终视频
    """
    import uuid
    job_id = str(uuid.uuid4())

    logger.info(f"Creating video composition job {job_id} for project: {request.project_id}")

    # 初始化任务状态
    _composition_jobs[job_id] = {
        "job_id": job_id,
        "project_id": request.project_id,
        "status": JobStatus.PROCESSING,
        "progress": 0.0,
        "result": None,
        "message": "Video composition started",
    }

    # 在后台执行视频合成
    background_tasks.add_task(
        _run_composition_task,
        job_id=job_id,
        project_id=request.project_id,
        skip_clip_editing=request.skip_clip_editing,
    )

    return {
        "job_id": job_id,
        "status": JobStatus.PROCESSING,
        "message": "Video composition job started",
        "project_id": request.project_id,
        "skip_clip_editing": request.skip_clip_editing,
    }


async def _run_composition_task(
    job_id: str,
    project_id: str,
    skip_clip_editing: bool = False,
) -> None:
    """
    后台执行视频合成任务
    """
    try:
        _composition_jobs[job_id]["progress"] = 0.1
        _composition_jobs[job_id]["message"] = "Fetching approved shots..."

        # 执行阶段五工作流
        result = await run_stage5(
            project_id=project_id,
            skip_clip_editing=skip_clip_editing,
        )

        # 更新任务状态为完成
        _composition_jobs[job_id].update({
            "status": JobStatus.COMPLETED,
            "progress": 1.0,
            "result": result,
            "message": "Video composition completed successfully",
        })

        logger.info(f"Composition job {job_id} completed: {result['final_video_url']}")

    except Exception as e:
        logger.error(f"Composition job {job_id} failed: {str(e)}")
        _composition_jobs[job_id].update({
            "status": JobStatus.FAILED,
            "message": f"Video composition failed: {str(e)}",
        })


@app.get("/api/v1/compose-status/{job_id}")
async def get_compose_status(job_id: str) -> JobStatusResponse:
    """
    查询合成任务状态

    获取指定任务 ID 的合成进度和状态
    """
    if job_id not in _composition_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = _composition_jobs[job_id]
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        result=job.get("result"),
        message=job.get("message"),
    )


# ============================================================================
# 审核相关 API
# ============================================================================

@app.get("/api/v1/project/{project_id}/review-status")
async def get_review_status(
    project_id: str,
    review_type: str = Query(default="prompt", description="审核类型: prompt 或 video"),
) -> dict[str, Any]:
    """
    查询项目的审核状态，供 n8n 轮询使用

    Args:
        project_id: 项目 ID
        review_type: 审核类型，可选 "prompt" (提示词审核) 或 "video" (视频审核)

    Returns:
        审核状态汇总信息
    """
    try:
        # 获取项目详情
        project = await airtable_service.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")

        # 获取所有分镜
        shots = await airtable_service.get_shots(project_id)

        # 根据审核类型确定状态字段
        status_field = (
            "提示词审核状态" if review_type == "prompt" else "视频审核状态"
        )
        comment_field = (
            "提示词审核意见" if review_type == "prompt" else "视频审核意见"
        )

        # 统计各状态数量
        total_shots = len(shots)
        approved_shots = 0
        rejected_shots = 0
        pending_shots = 0
        needs_revision_shots = 0

        shot_statuses = []
        for shot in shots:
            fields = shot.get("fields", {})
            status = fields.get(status_field, "待审核")
            shot_info = {
                "shot_id": shot["id"],
                "shot_number": fields.get("镜头序号"),
                "status": status,
                "review_comment": fields.get(comment_field, ""),
            }
            shot_statuses.append(shot_info)

            if status == "已通过":
                approved_shots += 1
            elif status == "需修改" or status == "需重新生成":
                needs_revision_shots += 1
                rejected_shots += 1
            elif status == "待审核":
                pending_shots += 1

        # 确定整体状态
        if total_shots == 0:
            overall_status = "no_shots"
        elif approved_shots == total_shots:
            overall_status = "all_approved"
        elif needs_revision_shots > 0:
            overall_status = "needs_revision"
        else:
            overall_status = "pending"

        return {
            "project_id": project_id,
            "review_type": review_type,
            "overall_status": overall_status,
            "total_shots": total_shots,
            "approved_shots": approved_shots,
            "rejected_shots": rejected_shots,
            "pending_shots": pending_shots,
            "needs_revision_shots": needs_revision_shots,
            "all_approved": approved_shots == total_shots and total_shots > 0,
            "shots": shot_statuses,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询审核状态失败: {str(e)}")


# ============================================================================
# 关键帧相关 API
# ============================================================================

# 内存中的关键帧任务状态存储
_keyframe_jobs: dict[str, dict[str, Any]] = {}


@app.post("/api/v1/projects/{project_id}/generate-keyframes")
async def generate_keyframes(
    project_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    触发 Stage 3.5 关键帧生成

    为指定项目的所有分镜生成关键帧参考图。
    关键帧生成完成后，项目进入 KEYFRAME_REVIEW 状态，等待人工审核。
    """
    import uuid
    job_id = str(uuid.uuid4())

    logger.info(f"Creating keyframe generation job {job_id} for project: {project_id}")

    # 初始化任务状态
    _keyframe_jobs[job_id] = {
        "job_id": job_id,
        "project_id": project_id,
        "status": JobStatus.PROCESSING,
        "progress": 0.0,
        "result": None,
        "message": "Keyframe generation started",
    }

    async def _run():
        try:
            _keyframe_jobs[job_id]["progress"] = 0.1
            _keyframe_jobs[job_id]["message"] = "Starting keyframe generation..."

            result = await run_stage3_5(project_id=project_id)

            _keyframe_jobs[job_id].update({
                "status": JobStatus.COMPLETED,
                "progress": 1.0,
                "result": result,
                "message": "Keyframe generation completed successfully",
            })
            logger.info(f"Keyframe job {job_id} completed for project {project_id}")

        except Exception as e:
            logger.error(f"Keyframe job {job_id} failed: {str(e)}", exc_info=True)
            _keyframe_jobs[job_id].update({
                "status": JobStatus.FAILED,
                "message": f"Keyframe generation failed: {str(e)}",
            })

    background_tasks.add_task(_run)

    return {
        "job_id": job_id,
        "status": JobStatus.PROCESSING,
        "message": "Keyframe generation job started",
        "project_id": project_id,
    }


@app.get("/api/v1/keyframe-status/{job_id}")
async def get_keyframe_status(job_id: str) -> JobStatusResponse:
    """
    查询关键帧生成任务状态

    获取指定任务 ID 的关键帧生成进度和状态
    """
    if job_id not in _keyframe_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = _keyframe_jobs[job_id]
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        result=job.get("result"),
        message=job.get("message"),
    )


@app.post("/api/v1/projects/{project_id}/approve-keyframes")
async def approve_keyframes(
    project_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """
    确认关键帧人工审核通过，触发 Stage 4 视频生成

    将项目状态从 KEYFRAME_REVIEW 更新为 GENERATING，
    然后在后台启动 Stage 4 视频生成。
    """
    import uuid
    from models.schemas import ProjectStatus

    logger.info(f"Approving keyframes for project: {project_id}")

    try:
        # 验证项目存在
        project = await airtable_service.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")

        # 验证项目状态
        fields = project.get("fields", {})
        current_status = fields.get("状态", "")
        if current_status and current_status != ProjectStatus.KEYFRAME_REVIEW.value:
            logger.warning(
                f"[{project_id}] 当前状态为 {current_status}，"
                f"预期为 {ProjectStatus.KEYFRAME_REVIEW.value}，仍继续执行"
            )

        # 更新项目状态为 GENERATING
        await airtable_service.update_project_status(
            project_id=project_id,
            status=ProjectStatus.GENERATING,
        )
        logger.info(f"[{project_id}] 项目状态 → GENERATING")

        # 校验分镜的提示词审核状态：Stage 3 已经写入 "已通过/已驳回"，
        # 不再无差别覆写。存在“未通过”分镜则阻止进入 Stage 4。
        shots = await airtable_service.get_project_shots(project_id)
        not_approved: list[dict[str, Any]] = []
        for shot in shots:
            sfields = shot.get("fields", {})
            status = sfields.get("提示词审核状态", "")
            if status != "已通过":
                not_approved.append({
                    "shot_id": shot.get("id"),
                    "shot_number": sfields.get("镜头序号"),
                    "status": status or "未设置",
                    "review_comment": sfields.get("提示词审核意见", ""),
                })

        if not_approved:
            rejected_numbers = [item["shot_number"] for item in not_approved]
            logger.warning(
                f"[{project_id}] 存在 {len(not_approved)} 个未通过分镜: {rejected_numbers}，拒绝进入 Stage 4"
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "存在提示词审核未通过的分镜，请先修改后手动将其设为‘已通过’再触发视频生成",
                    "project_id": project_id,
                    "rejected_count": len(not_approved),
                    "rejected_shots": not_approved,
                },
            )
        logger.info(f"[{project_id}] 分镜提示词审核全部通过（{len(shots)} 个），继续 Stage 4")

        # 创建后台任务触发 Stage 4
        job_id = str(uuid.uuid4())

        _generation_jobs[job_id] = {
            "job_id": job_id,
            "project_id": project_id,
            "status": JobStatus.PROCESSING,
            "progress": 0.0,
            "result": None,
            "message": "Video generation started (after keyframe approval)",
        }

        async def _run():
            try:
                _generation_jobs[job_id]["progress"] = 0.1
                _generation_jobs[job_id]["message"] = "Starting video generation..."

                await run_stage4(
                    project_id=project_id,
                    platform="seedance",
                )

                _generation_jobs[job_id].update({
                    "status": JobStatus.COMPLETED,
                    "progress": 1.0,
                    "message": "Video generation completed successfully",
                })
            except Exception as e:
                logger.error(f"Generation job {job_id} failed: {str(e)}", exc_info=True)
                _generation_jobs[job_id].update({
                    "status": JobStatus.FAILED,
                    "message": f"Video generation failed: {str(e)}",
                })

        background_tasks.add_task(_run)

        return {
            "job_id": job_id,
            "status": JobStatus.PROCESSING,
            "message": "关键帧审核通过，视频生成已启动",
            "project_id": project_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Approve keyframes failed for project {project_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"关键帧审核通过操作失败: {str(e)}")


# ============================================================================
# Token 消耗统计 API
# ============================================================================


@app.get("/api/v1/token-usage/{project_id}")
async def get_token_usage_by_project(project_id: str):
    """查询单个项目的 Token 消耗明细"""
    summary = token_tracker.get_project_summary(project_id)
    records = token_tracker.get_project_records(project_id)
    return {
        "summary": summary,
        "records": records,
    }


@app.get("/api/v1/token-usage")
async def get_token_usage_global(limit: int = Query(default=50, ge=1, le=200)):
    """查询全局 Token 消耗汇总（按项目统计）"""
    result = token_tracker.get_all_summary(limit=limit)
    return result


# ============================================================================
# 启动入口
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.SERVICE_HOST,
        port=settings.SERVICE_PORT,
        reload=True,
    )
