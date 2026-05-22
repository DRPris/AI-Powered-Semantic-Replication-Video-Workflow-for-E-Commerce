"""
阶段一：素材准备
并行执行视频分析、商品分析和三视图生成
"""

import asyncio
import base64
import json
import logging
import uuid
from typing import TYPE_CHECKING, Optional

from services import GeminiService, AirtableService
from services.oss_service import OSSService
from services.audit_service import AuditService, AuditResult, AuditFailedException
from config import settings
from models.schemas import ProjectStatus, AssetType

if TYPE_CHECKING:
    from models.schemas import ReplicationMode

logger = logging.getLogger(__name__)


async def _maybe_run_product_brief_agent(
    project_id: str,
    gemini: GeminiService,
    product_image_url: str,
    product_listing_url: Optional[str],
    preliminary_analysis: Optional[dict],
    product_listing_info: Optional[dict],
) -> Optional[dict]:
    """灰度开关下运行商品分析 Agent（Phase A），失败不阻塞工作流

    Returns:
        ProductBrief.model_dump() dict，或 None（开关关闭 / 异常）
    """
    if not getattr(settings, "ENABLE_PRODUCT_AGENT", False):
        return None
    try:
        # 懒导入，避免全局 import 依赖
        from agents import ProductBriefAgent

        logger.info(f"[{project_id}] Product Brief Agent enabled, running preflight...")
        agent = ProductBriefAgent(
            gemini_service=gemini,
            product_image_url=product_image_url,
            product_listing_url=product_listing_url,
        )
        brief = await agent.run_preflight(
            preliminary_analysis=preliminary_analysis,
            product_listing_info=product_listing_info,
        )
        brief_dict = brief.model_dump()
        logger.info(
            f"[{project_id}] Product Brief Agent completed: "
            f"confidence={brief.confidence_score:.2f}, phase={brief.phase}, "
            f"clarifications={len(brief.clarification_items)}, gaps={brief.info_gaps}"
        )
        return brief_dict
    except Exception as e:
        logger.warning(f"[{project_id}] Product Brief Agent failed (non-blocking): {e}")
        return None


async def _maybe_analyze_and_inject_product_video(
    project_id: str,
    gemini: GeminiService,
    product_listing_info: Optional[dict],
    preliminary_analysis: Optional[dict] = None,
) -> None:
    """如果 product_listing_info 包含 product_video_urls 候选，则调用 Gemini 进行
    商品视频的差分式理解（仅提取 listing / 主图 没有的信息：
    多角度细节、使用中动态状态、材质在运动中的表现），
    并将结果（含选中的 video_url + analysis JSON）原地注入到
    product_listing_info。失败非阻塞。
    """
    if not getattr(settings, "ENABLE_PRODUCT_VIDEO_ANALYSIS", True):
        return
    if not isinstance(product_listing_info, dict):
        return
    video_urls = product_listing_info.get("product_video_urls") or []
    if not video_urls:
        return

    # 构造 known_facts（listing + 主图已知信息），告知 LLM 不要重复
    try:
        from prompts.product_video_analysis import build_known_facts_from_sources
        known_facts = build_known_facts_from_sources(
            product_listing_info=product_listing_info,
            preliminary_analysis=preliminary_analysis if isinstance(preliminary_analysis, dict) else None,
        )
    except Exception as _e:
        logger.debug(f"[{project_id}] build_known_facts 失败，回退到空事实: {_e}")
        known_facts = None

    selected_url = video_urls[0]
    logger.info(
        f"[{project_id}] 检测到商品视频，开始差分分析: {selected_url} "
        f"(known_facts_fields={len((known_facts or {}))})"
    )
    try:
        analysis = await gemini.analyze_product_video(selected_url, known_facts=known_facts)
    except Exception as e:
        logger.warning(f"[{project_id}] analyze_product_video 异常: {e}")
        analysis = None
    if not analysis:
        logger.info(f"[{project_id}] 商品视频分析未返回结果，跳过")
        return
    product_listing_info["product_video_url"] = selected_url
    product_listing_info["product_video_analysis"] = analysis
    logger.info(
        f"[{project_id}] 商品视频差分理解已注入 product_listing_info："
        f"new_info={len(analysis.get('new_info_not_in_listing', []))}, "
        f"angles_seen={sum(1 for v in (analysis.get('multi_angle_details') or {}).values() if v)}, "
        f"dynamic_states={len(analysis.get('in_use_dynamic_states', []))}"
    )


async def _run_stage1_audits(
    project_id: str,
    video_analysis: dict,
    product_analysis: dict,
    product_listing_info: Optional[dict],
    airtable: AirtableService,
) -> None:
    """模型审查点 1.1 / 1.2 的统一插入点。

    行为：
    - 并行调 AuditService 对 video_analysis / product_analysis 做审查
    - 成功通过且置信度达标 → 写入对应字段为 "已通过" 后返回
    - 任一 should_block → 写入 "已驳回" + review_comment，项目状态置 REVIEWING，
      抛 AuditFailedException（上层捕获后直接返回 waiting_review）

    字段缺失 / 开关关闭 / Qwen 自己异常已在下层兵底（降级为 passed=False 转人审）。
    """
    enable_video = getattr(settings, "ENABLE_AUDIT_STAGE1_VIDEO", True)
    enable_product = getattr(settings, "ENABLE_AUDIT_STAGE1_PRODUCT", True)
    if not enable_video and not enable_product:
        logger.info(f"[{project_id}] Stage1 审查全部关闭，跳过")
        return

    audit = AuditService()

    tasks = []
    task_scopes: list[str] = []
    if enable_video and isinstance(video_analysis, dict):
        tasks.append(audit.audit_video_analysis(project_id, video_analysis))
        task_scopes.append("video")
    if enable_product and isinstance(product_analysis, dict):
        tasks.append(
            audit.audit_product_analysis(project_id, product_analysis, product_listing_info)
        )
        task_scopes.append("product")

    if not tasks:
        logger.info(f"[{project_id}] Stage1 审查输入不可用，跳过")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)

    scope_to_result: dict[str, AuditResult] = {}
    scope_to_error: dict[str, Exception] = {}
    for scope, res in zip(task_scopes, results):
        if isinstance(res, Exception):
            scope_to_error[scope] = res
            logger.warning(f"[{project_id}] Stage1 audit {scope} 异常: {res}")
        else:
            scope_to_result[scope] = res

    # 先写回通过项（方便 Airtable 看板展示）
    for scope, r in scope_to_result.items():
        if not r.should_block:
            try:
                await airtable.save_stage1_audit_result(
                    project_id=project_id,
                    scope=scope,
                    status="已通过",
                    review_comment=r.to_review_comment(),
                )
            except Exception as e:
                logger.warning(f"[{project_id}] 写入 Stage1 审查已通过状态失败 ({scope}): {e}")

    blocked: list[tuple[str, AuditResult]] = [
        (scope, r) for scope, r in scope_to_result.items() if r.should_block
    ]
    for scope, err in scope_to_error.items():
        fallback = AuditResult(
            passed=False,
            confidence=0.0,
            critical_issues=[f"审查调用异常: {type(err).__name__}: {err}"],
            warnings=[],
            reason_summary="Stage1 审查执行异常，转人审",
        )
        blocked.append((scope, fallback))

    if not blocked:
        logger.info(f"[{project_id}] Stage1 审查全部通过")
        return

    for scope, r in blocked:
        try:
            await airtable.save_stage1_audit_result(
                project_id=project_id,
                scope=scope,
                status="已驳回",
                review_comment=r.to_review_comment(),
            )
        except Exception as e:
            logger.warning(f"[{project_id}] 写入 Stage1 审查驳回状态失败 ({scope}): {e}")

    try:
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.REVIEWING.value,
        )
    except Exception as e:
        logger.warning(f"[{project_id}] 置 REVIEWING 状态失败: {e}")

    _, first_result = blocked[0]
    raise AuditFailedException(
        stage="stage1",
        scope=",".join(s for s, _ in blocked),
        result=first_result,
    )


async def _handle_awaiting_user_branch(
    project_id: str,
    airtable: AirtableService,
    product_brief: Optional[dict],
) -> bool:
    """如果 Agent 返回 phase=='awaiting_user'，写入草稿、更新状态，返回 True 表示应提前结束。
    否则返回 False。
    """
    if not product_brief or product_brief.get("phase") != "awaiting_user":
        return False
    try:
        await airtable.save_product_brief_draft(project_id, product_brief)
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.AWAITING_CONFIRMATION,
        )
        logger.info(
            f"[{project_id}] Product Brief Agent awaiting user confirmation, "
            f"status=AWAITING_CONFIRMATION"
        )
    except Exception as e:
        logger.warning(f"[{project_id}] failed to persist awaiting_user state: {e}")
    return True

def _truncate_content(content: str, max_length: int = 90000) -> str:
    """截断超长内容，防止超出 Airtable Long Text 字段限制
    
    注意：如果内容是JSON，需要确保截断后仍然是有效的JSON。
    这里采用智能截断策略：尝试解析为JSON，如果失败则进行结构化截断。
    """
    if len(content) <= max_length:
        return content
    
    import json
    try:
        # 尝试解析为JSON
        data = json.loads(content)
        # 如果是JSON，进行结构化截断
        if isinstance(data, dict):
            # 对于字典，保留关键字段，截断长文本值
            truncated_data = {}
            for key, value in data.items():
                if isinstance(value, str) and len(value) > 10000:
                    truncated_data[key] = value[:10000] + "...(truncated)"
                elif isinstance(value, (list, dict)):
                    # 递归处理嵌套结构
                    str_value = json.dumps(value, ensure_ascii=False)
                    if len(str_value) > 10000:
                        truncated_data[key] = "...(nested content truncated)"
                    else:
                        truncated_data[key] = value
                else:
                    truncated_data[key] = value
            return json.dumps(truncated_data, ensure_ascii=False)
        elif isinstance(data, list):
            # 对于列表，截断元素数量
            truncated_list = data[:50]  # 最多保留50个元素
            if len(data) > 50:
                truncated_list.append("...(more items truncated)")
            return json.dumps(truncated_list, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # 非JSON内容，直接截断（确保不截断在多字节字符中间）
    # 找到最后一个完整的UTF-8字符边界
    truncated = content[:max_length]
    while truncated and ord(truncated[-1]) >= 0x80:
        # 多字节字符的中间字节
        truncated = truncated[:-1]
    return truncated + "\n\n...(content truncated, original length: {})".format(len(content))


async def run_stage1(
    project_id: str,
    video_url: str,
    product_image_url: str,
    mode: "ReplicationMode" = "full",
    product_listing_url: str = None
) -> dict:
    """
    阶段一：素材准备

    Args:
        project_id: 项目 ID
        video_url: 原始视频 URL
        product_image_url: 商品图片 URL
        mode: 复刻模式，simple 或 full
        product_listing_url: 商品详情页链接（可选），用于提取卖点和产品形态信息

    Returns:
        阶段执行结果
    """
    logger.info(f"Starting Stage 1: Preparation for project {project_id}, mode={mode}")

    # 初始化服务
    gemini = GeminiService()
    gemini.set_context(project_id, "stage1")
    airtable = AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID
    )

    try:
        # 更新项目状态为分析中
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.ANALYZING
        )
        logger.info(f"Project {project_id} status updated to ANALYZING")

        # 根据模式决定执行路径
        if mode == "simple":
            # 简单模式：只进行基础分析，不生成三视图
            logger.info(f"Running in SIMPLE mode for project {project_id}")

            # 并行执行视频分析、商品分析、节奏分析和商品链接提取
            video_analysis_task = gemini.analyze_video(video_url)
            product_analysis_task = gemini.analyze_product(product_image_url)
            rhythm_analysis_task = gemini.analyze_rhythm(video_url)

            # 商品链接提取（可选，与其他分析并行）
            parallel_tasks = [video_analysis_task, product_analysis_task, rhythm_analysis_task]
            has_listing_task = False
            if product_listing_url:
                logger.info(f"[{project_id}] 检测到商品链接，将并行提取: {product_listing_url}")
                listing_extraction_task = gemini.extract_product_listing(product_listing_url)
                parallel_tasks.append(listing_extraction_task)
                has_listing_task = True

            results = await asyncio.gather(*parallel_tasks, return_exceptions=True)

            video_analysis = results[0]
            product_analysis = results[1]
            rhythm_analysis = results[2]
            product_listing_info = results[3] if has_listing_task else None

            # 检查必要分析的异常（视频+商品失败则抛出）
            if isinstance(video_analysis, Exception):
                logger.error(f"Video analysis failed: {video_analysis}")
                raise video_analysis
            if isinstance(product_analysis, Exception):
                logger.error(f"Product analysis failed: {product_analysis}")
                raise product_analysis

            # 节奏分析失败不阻塞主流程
            if isinstance(rhythm_analysis, Exception):
                logger.warning(f"Rhythm analysis failed (non-blocking): {rhythm_analysis}")
                rhythm_analysis = None

            # 商品链接提取失败不阻塞主流程
            if isinstance(product_listing_info, Exception):
                logger.warning(f"Product listing extraction failed (non-blocking): {product_listing_info}")
                product_listing_info = None

            if product_listing_info:
                logger.info(f"[{project_id}] 商品链接提取成功: {product_listing_info.get('product_name', 'unknown')}")

            # 商品视频差分理解（如商品页存在嵌入视频）——传入 listing + 主图作为"已知事实"
            await _maybe_analyze_and_inject_product_video(
                project_id=project_id,
                gemini=gemini,
                product_listing_info=product_listing_info if isinstance(product_listing_info, dict) else None,
                preliminary_analysis=product_analysis if isinstance(product_analysis, dict) else None,
            )

            # 灰度运行商品分析 Agent（Phase A）
            product_brief = await _maybe_run_product_brief_agent(
                project_id=project_id,
                gemini=gemini,
                product_image_url=product_image_url,
                product_listing_url=product_listing_url,
                preliminary_analysis=product_analysis if isinstance(product_analysis, dict) else None,
                product_listing_info=product_listing_info,
            )

            # 如果启用用户确认且 Agent 产出需等待确认，提前结束 Stage 1
            if await _handle_awaiting_user_branch(project_id, airtable, product_brief):
                return {
                    "success": True,
                    "project_id": project_id,
                    "mode": "simple",
                    "awaiting_user_confirmation": True,
                    "product_brief": product_brief,
                }

            # 模型审查点 1.1 / 1.2：视频分析 + 商品分析（simple 模式）
            try:
                await _run_stage1_audits(
                    project_id=project_id,
                    video_analysis=video_analysis if isinstance(video_analysis, dict) else {},
                    product_analysis=product_analysis if isinstance(product_analysis, dict) else {},
                    product_listing_info=product_listing_info if isinstance(product_listing_info, dict) else None,
                    airtable=airtable,
                )
            except AuditFailedException as e:
                logger.warning(
                    f"[{project_id}] Stage1 审查未通过，转人审 (simple): scope={e.scope}"
                )
                return {
                    "success": False,
                    "project_id": project_id,
                    "mode": "simple",
                    "needs_human_review": True,
                    "audit_stage": e.stage,
                    "audit_scope": e.scope,
                    "audit_result": e.result.model_dump(),
                }

            # 保存分析结果到素材表（内容截断处理）
            video_content = _truncate_content(json.dumps({
                "analysis_result": video_analysis,
                "analysis_type": "simple"
            }, ensure_ascii=False))
            await airtable.create_asset_from_dict({
                "project_id": project_id,
                "asset_type": AssetType.VIDEO,
                "attachment_url": video_url,
                "content": video_content
            })
            logger.info(f"Video analysis asset created for project {project_id}")

            product_content = _truncate_content(json.dumps({
                "analysis_result": product_analysis,
                "analysis_type": "simple",
                "product_listing_info": product_listing_info,
                "product_brief": product_brief
            }, ensure_ascii=False))
            await airtable.create_asset_from_dict({
                "project_id": project_id,
                "asset_type": AssetType.PRODUCT,
                "attachment_url": product_image_url,
                "content": product_content
            })
            logger.info(f"Product analysis asset created for project {project_id}")

            # 保存节奏分析结果（如果成功）
            if rhythm_analysis:
                try:
                    rhythm_content = _truncate_content(json.dumps({
                        "analysis_result": rhythm_analysis,
                        "analysis_type": "simple"
                    }, ensure_ascii=False))
                    await airtable.create_asset_from_dict({
                        "project_id": project_id,
                        "asset_type": "rhythm",
                        "attachment_url": video_url,
                        "content": rhythm_content
                    })
                    logger.info(f"Rhythm analysis asset created for project {project_id}")
                except Exception as e:
                    logger.warning(f"Failed to save rhythm analysis asset (non-blocking): {e}")

            # 简单模式直接跳到生成阶段
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.GENERATING
            )
            await airtable.update_project(
                project_id=project_id,
                data={
                    "analysis_result": _truncate_content(json.dumps({
                        "video_analysis": video_analysis,
                        "product_analysis": product_analysis,
                        "rhythm_analysis_available": rhythm_analysis is not None,
                        "product_listing_available": product_listing_info is not None,
                        "product_brief_available": product_brief is not None,
                        "mode": "simple"
                    }, ensure_ascii=False))
                }
            )
            logger.info(f"Project {project_id} status updated to GENERATING (simple mode)")

            return {
                "success": True,
                "project_id": project_id,
                "mode": "simple",
                "video_analysis": video_analysis,
                "product_analysis": product_analysis,
                "rhythm_analysis": rhythm_analysis,
                "product_listing_info": product_listing_info,
                "product_brief": product_brief,
                "three_views": None
            }

        else:
            # 完整模式：执行所有分析并生成三视图
            logger.info(f"Running in FULL mode for project {project_id}")

            # 并行执行视频分析、商品分析、节奏分析和商品链接提取
            video_analysis_task = gemini.analyze_video(video_url)
            product_analysis_task = gemini.analyze_product(product_image_url)
            rhythm_analysis_task = gemini.analyze_rhythm(video_url)

            # 商品链接提取（可选，与其他分析并行）
            parallel_tasks = [video_analysis_task, product_analysis_task, rhythm_analysis_task]
            has_listing_task = False
            if product_listing_url:
                logger.info(f"[{project_id}] 检测到商品链接，将并行提取: {product_listing_url}")
                listing_extraction_task = gemini.extract_product_listing(product_listing_url)
                parallel_tasks.append(listing_extraction_task)
                has_listing_task = True

            results = await asyncio.gather(*parallel_tasks, return_exceptions=True)

            video_analysis = results[0]
            product_analysis = results[1]
            rhythm_analysis = results[2]
            product_listing_info = results[3] if has_listing_task else None

            # 检查必要分析的异常
            if isinstance(video_analysis, Exception):
                logger.error(f"Video analysis failed: {video_analysis}")
                raise video_analysis
            if isinstance(product_analysis, Exception):
                logger.error(f"Product analysis failed: {product_analysis}")
                raise product_analysis

            # 节奏分析失败不阻塞主流程
            if isinstance(rhythm_analysis, Exception):
                logger.warning(f"Rhythm analysis failed (non-blocking): {rhythm_analysis}")
                rhythm_analysis = None

            # 商品链接提取失败不阻塞主流程
            if isinstance(product_listing_info, Exception):
                logger.warning(f"Product listing extraction failed (non-blocking): {product_listing_info}")
                product_listing_info = None

            if product_listing_info:
                logger.info(f"[{project_id}] 商品链接提取成功: {product_listing_info.get('product_name', 'unknown')}")

            # 商品视频差分理解（如商品页存在嵌入视频）——传入 listing + 主图作为"已知事实"
            await _maybe_analyze_and_inject_product_video(
                project_id=project_id,
                gemini=gemini,
                product_listing_info=product_listing_info if isinstance(product_listing_info, dict) else None,
                preliminary_analysis=product_analysis if isinstance(product_analysis, dict) else None,
            )

            # 灰度运行商品分析 Agent（Phase A）
            product_brief = await _maybe_run_product_brief_agent(
                project_id=project_id,
                gemini=gemini,
                product_image_url=product_image_url,
                product_listing_url=product_listing_url,
                preliminary_analysis=product_analysis if isinstance(product_analysis, dict) else None,
                product_listing_info=product_listing_info,
            )

            # 如果启用用户确认且 Agent 产出需等待确认，提前结束 Stage 1
            if await _handle_awaiting_user_branch(project_id, airtable, product_brief):
                return {
                    "success": True,
                    "project_id": project_id,
                    "mode": "full",
                    "awaiting_user_confirmation": True,
                    "product_brief": product_brief,
                }

            # 模型审查点 1.1 / 1.2：视频分析 + 商品分析（full 模式）
            try:
                await _run_stage1_audits(
                    project_id=project_id,
                    video_analysis=video_analysis if isinstance(video_analysis, dict) else {},
                    product_analysis=product_analysis if isinstance(product_analysis, dict) else {},
                    product_listing_info=product_listing_info if isinstance(product_listing_info, dict) else None,
                    airtable=airtable,
                )
            except AuditFailedException as e:
                logger.warning(
                    f"[{project_id}] Stage1 审查未通过，转人审 (full): scope={e.scope}"
                )
                return {
                    "success": False,
                    "project_id": project_id,
                    "mode": "full",
                    "needs_human_review": True,
                    "audit_stage": e.stage,
                    "audit_scope": e.scope,
                    "audit_result": e.result.model_dump(),
                }

            # 检查是否已有用户上传的三视图
            existing_assets = await airtable.get_project_assets(project_id)
            has_three_views = False
            user_uploaded_three_view_url = None
            for asset in existing_assets:
                asset_type = asset.get("fields", {}).get("素材类型", "")
                if asset_type == "三视图":
                    has_three_views = True
                    # 获取用户上传的三视图 URL
                    attachments = asset.get("fields", {}).get("附件", [])
                    if attachments and len(attachments) > 0:
                        user_uploaded_three_view_url = attachments[0].get("url", "")
                    logger.info(f"[{project_id}] 发现用户上传的三视图，跳过 Gemini 生成")
                    break

            three_views = []
            if has_three_views and user_uploaded_three_view_url:
                # 使用用户上传的三视图
                three_views = [user_uploaded_three_view_url]
                logger.info(f"[{project_id}] 使用用户上传的三视图: {user_uploaded_three_view_url}")
            else:
                # 三视图生成 - 使用 try/except 包裹，失败不阻塞工作流
                # 先将产品分析结果转换为文本描述，用于指导三视图生成
                product_analysis_text = ""
                if isinstance(product_analysis, dict):
                    # 提取关键产品特征信息
                    product_parts = []
                    if "name" in product_analysis:
                        product_parts.append(f"Product Name: {product_analysis['name']}")
                    if "category" in product_analysis:
                        product_parts.append(f"Category: {product_analysis['category']}")
                    if "description" in product_analysis:
                        product_parts.append(f"Description: {product_analysis['description']}")
                    if "features" in product_analysis and isinstance(product_analysis["features"], list):
                        product_parts.append(f"Features: {', '.join(product_analysis['features'])}")
                    if "colors" in product_analysis and isinstance(product_analysis["colors"], list):
                        product_parts.append(f"Colors: {', '.join(product_analysis['colors'])}")
                    if "materials" in product_analysis and isinstance(product_analysis["materials"], list):
                        product_parts.append(f"Materials: {', '.join(product_analysis['materials'])}")
                    product_analysis_text = "\n".join(product_parts)
                elif isinstance(product_analysis, str):
                    product_analysis_text = product_analysis

                try:
                    logger.info(f"Generating three views with product analysis: {product_analysis_text[:100]}...")
                    three_views = await gemini.generate_three_views(
                        product_image_url,
                        product_description=product_analysis_text
                    )
                    if not three_views:
                        logger.warning("三视图生成返回空结果，使用原始产品图作为替代")
                        three_views = [product_image_url]  # fallback
                except Exception as e:
                    logger.warning(f"三视图生成失败，使用原始产品图: {e}")
                    three_views = [product_image_url]

            # 保存分析结果到素材表（内容截断处理）
            video_content = _truncate_content(json.dumps({
                "analysis_result": video_analysis,
                "analysis_type": "full"
            }, ensure_ascii=False))
            await airtable.create_asset_from_dict({
                "project_id": project_id,
                "asset_type": AssetType.VIDEO,
                "attachment_url": video_url,
                "content": video_content
            })
            logger.info(f"Video analysis asset created for project {project_id}")

            product_content = _truncate_content(json.dumps({
                "analysis_result": product_analysis,
                "analysis_type": "full",
                "three_views": three_views,
                "product_listing_info": product_listing_info,
                "product_brief": product_brief
            }, ensure_ascii=False))
            await airtable.create_asset_from_dict({
                "project_id": project_id,
                "asset_type": AssetType.PRODUCT,
                "attachment_url": product_image_url,
                "content": product_content
            })
            logger.info(f"Product analysis asset created for project {project_id}")

            # 保存节奏分析结果（如果成功）
            if rhythm_analysis:
                try:
                    rhythm_content = _truncate_content(json.dumps({
                        "analysis_result": rhythm_analysis,
                        "analysis_type": "full"
                    }, ensure_ascii=False))
                    await airtable.create_asset_from_dict({
                        "project_id": project_id,
                        "asset_type": "rhythm",
                        "attachment_url": video_url,
                        "content": rhythm_content
                    })
                    logger.info(f"Rhythm analysis asset created for project {project_id}")
                except Exception as e:
                    logger.warning(f"Failed to save rhythm analysis asset (non-blocking): {e}")

            # 保存三视图作为独立素材（仅当不是用户上传时才创建）
            if not has_three_views:
                # 初始化 OSS 服务用于上传 base64 图片
                oss_service = OSSService(
                    access_key_id=settings.OSS_ACCESS_KEY_ID,
                    access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
                    bucket_name=settings.OSS_BUCKET_NAME,
                    endpoint=settings.OSS_ENDPOINT,
                    cdn_domain=settings.OSS_CDN_DOMAIN
                )

                uploaded_view_urls = []
                for i, view_image in enumerate(three_views):
                    view_names = ["front", "side", "top"]
                    view_name = view_names[i] if i < len(view_names) else f"view_{i}"

                    # 如果是 base64 data URI，上传到 OSS
                    view_url = view_image
                    if view_image.startswith("data:"):
                        try:
                            # 解析 data URI: data:image/png;base64,xxxx
                            header, b64_data = view_image.split(",", 1)
                            mime_type = header.split(";")[0].split(":")[1]  # image/png
                            ext = mime_type.split("/")[1]  # png
                            image_bytes = base64.b64decode(b64_data)

                            # 上传到 OSS
                            oss_key = f"three_views/{project_id}/{uuid.uuid4().hex[:8]}_{view_name}.{ext}"
                            view_url = await oss_service.upload_bytes(
                                data=image_bytes,
                                oss_key=oss_key,
                                content_type=mime_type,
                                expires=86400 * 7  # 7天有效
                            )
                            logger.info(f"Three-view {view_name} uploaded to OSS: {oss_key}")
                        except Exception as e:
                            logger.warning(f"Failed to upload three-view {view_name} to OSS: {e}")
                            view_url = product_image_url  # fallback to original product image

                    uploaded_view_urls.append(view_url)

                    view_content = _truncate_content(json.dumps({
                        "view_type": view_name,
                        "source_product_image": product_image_url,
                        "filename": f"three_view_{view_name}.png"
                    }, ensure_ascii=False))
                    await airtable.create_asset_from_dict({
                        "project_id": project_id,
                        "asset_type": AssetType.THREE_VIEW,  # 使用正确的素材类型
                        "attachment_url": view_url,
                        "content": view_content
                    })

                # 更新 three_views 为实际的 OSS URLs
                three_views = uploaded_view_urls
                logger.info(f"Three-view assets created for project {project_id} with type 'three_view'")
            else:
                logger.info(f"[{project_id}] 使用用户上传的三视图，跳过创建新的三视图素材记录")

            # 更新项目状态为脚本生成中
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.SCRIPT_GENERATING
            )
            await airtable.update_project(
                project_id=project_id,
                data={
                    "analysis_result": _truncate_content(json.dumps({
                        "video_analysis": video_analysis,
                        "product_analysis": product_analysis,
                        "three_views_count": len(three_views),
                        "rhythm_analysis_available": rhythm_analysis is not None,
                        "product_listing_available": product_listing_info is not None,
                        "product_brief_available": product_brief is not None,
                        "mode": "full"
                    }, ensure_ascii=False))
                }
            )
            logger.info(f"Project {project_id} status updated to SCRIPT_GENERATING")

            return {
                "success": True,
                "project_id": project_id,
                "mode": "full",
                "video_analysis": video_analysis,
                "product_analysis": product_analysis,
                "rhythm_analysis": rhythm_analysis,
                "product_listing_info": product_listing_info,
                "product_brief": product_brief,
                "three_views": three_views
            }

    except Exception as e:
        logger.error(f"Stage 1 failed for project {project_id}: {e}")
        # 更新项目状态为失败
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.FAILED
        )
        raise


# 保持向后兼容的别名
stage1_preparation = run_stage1
