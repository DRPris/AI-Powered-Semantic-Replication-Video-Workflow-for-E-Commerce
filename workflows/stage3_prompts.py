"""
阶段三：提示词生成
将复刻脚本转换为逐镜头生成提示词
"""

import json
import logging
from typing import TYPE_CHECKING, Optional

from services import GeminiService, AirtableService
from config import settings
from models.schemas import ProjectStatus
from prompts.constraints import generate_constraints

if TYPE_CHECKING:
    from models.schemas import ReplicationMode

logger = logging.getLogger(__name__)


async def run_stage3(
    project_id: str,
    mode: "ReplicationMode" = "full"
) -> dict:
    """
    阶段三：提示词生成

    Args:
        project_id: 项目 ID
        mode: 复刻模式，simple 或 full

    Returns:
        阶段执行结果
    """
    logger.info(f"Starting Stage 3: Prompt Conversion for project {project_id}, mode={mode}")

    # 初始化服务
    gemini = GeminiService()
    gemini.set_context(project_id, "stage3")
    airtable = AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID
    )

    try:
        # 简单模式：跳过提示词转换阶段
        if mode == "simple":
            logger.info(f"Skipping Stage 3 in SIMPLE mode for project {project_id}")
            return {
                "success": True,
                "project_id": project_id,
                "mode": "simple",
                "skipped": True,
                "message": "Prompt conversion skipped in simple mode"
            }

        # 从 Airtable 获取复刻脚本
        project = await airtable.get_project(project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")

        fields = project.get("fields", {})
        script_content = fields.get("script_content")
        if not script_content:
            # script_content 字段可能不存在，从 Shots 表重构脚本摘要
            logger.warning(f"Script content not found in project {project_id}, reconstructing from Shots...")
            shots_for_script = await airtable.get_project_shots(project_id)
            script_summary = []
            for shot in shots_for_script:
                sf = shot.get("fields", {})
                script_summary.append({
                    "镜头序号": sf.get("镜头序号"),
                    "原镜头描述": sf.get("原镜头描述", ""),
                    "新镜头描述": sf.get("新镜头描述", ""),
                })
            if not script_summary:
                raise ValueError(f"Script content not found and no shots to reconstruct for project {project_id}")
            script_content = json.dumps({"shots": script_summary}, ensure_ascii=False)
            logger.info(f"Reconstructed script from {len(script_summary)} shots")
        else:
            logger.info(f"Retrieved script content for project {project_id}")

        # 从 Assets 提前拉取原视频分析和商品分析，供 Stage 3 生成时做
        # 镜头内环境音三步决策用（有则传，无则模型会走场景不匹配分支）。
        pre_assets_cache: list[dict] = []
        try:
            pre_assets_cache = await airtable.get_project_assets(project_id)
        except Exception as e:
            logger.warning(f"拉取 assets 失败，ambient 三步决策将没有参考声场: {e}")

        original_video_analysis_obj: Optional[dict] = None
        product_scene_context_obj: Optional[dict] = None
        for asset in pre_assets_cache:
            asset_fields = asset.get("fields", {})
            asset_type = asset_fields.get("素材类型", "")
            content = asset_fields.get("内容", "") or ""
            if not content:
                continue
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except Exception:
                parsed = None
            if asset_type == "video" and isinstance(parsed, dict):
                original_video_analysis_obj = parsed
            elif asset_type == "product" and isinstance(parsed, dict):
                # 仅精选与新商品场景相关的字段，避免整个 product_analysis 进 prompt
                product_scene_context_obj = {
                    "product_name": parsed.get("product_name") or parsed.get("name", ""),
                    "category": parsed.get("category") or parsed.get("product_category", ""),
                    "use_scenarios": parsed.get("use_scenarios")
                        or parsed.get("usage_scenarios")
                        or parsed.get("typical_environment", ""),
                    "form_factor": parsed.get("form_factor", ""),
                    "listing_highlights": (parsed.get("product_listing_info") or {}).get("scene")
                        if isinstance(parsed.get("product_listing_info"), dict) else "",
                }

        # 调用 Gemini 转换提示词（带上原视频音效参考 + 新商品场景）
        logger.info(f"Converting prompts for project {project_id}")
        prompt_results = await gemini.convert_to_prompts(
            script_content,
            original_video_analysis=original_video_analysis_obj,
            product_scene_context=product_scene_context_obj,
        )

        logger.info(f"Converted {len(prompt_results)} prompts for project {project_id}")

        # 从 Airtable 获取所有分镜
        shots = await airtable.get_project_shots(project_id)
        shots_by_number = {}
        for shot in shots:
            shot_fields = shot.get("fields", {})
            shot_number = shot_fields.get("镜头序号")
            if shot_number:
                shots_by_number[shot_number] = shot

        # 更新每个镜头的生成提示词字段
        updated_count = 0
        audit_shot_inputs: list[dict] = []  # 送入自审核的结构化提示词
        shot_id_by_number: dict[int, str] = {}
        for prompt_result in prompt_results:
            shot_number = prompt_result.get("shot_number")
            if shot_number and shot_number in shots_by_number:
                shot = shots_by_number[shot_number]
                shot_id = shot.get("id")
                shot_fields = shot.get("fields", {})

                # 构建生成提示词
                generation_prompt = {
                    "first_frame": prompt_result.get("first_frame", ""),
                    "motion": prompt_result.get("motion", ""),
                    "camera": prompt_result.get("camera", ""),
                    "duration": prompt_result.get("duration", ""),
                    "constraints": prompt_result.get("constraints", []),
                    "shot_type": prompt_result.get("shot_type", "demo")
                }
                # 保留 audio 字段（包含 ambient/scene_match/scene_type），Stage 5 会优先读取
                audio_field = prompt_result.get("audio")
                if isinstance(audio_field, dict) and audio_field:
                    generation_prompt["audio"] = {
                        "ambient": audio_field.get("ambient", ""),
                        "scene_match": audio_field.get("scene_match", ""),
                        "scene_type": audio_field.get("scene_type", ""),
                    }

                # 更新分镜记录 - 使用 Airtable 实际字段名
                await airtable.update_shot(
                    shot_id=shot_id,
                    data={
                        "生成提示词": str(generation_prompt)
                    }
                )
                updated_count += 1
                shot_id_by_number[shot_number] = shot_id

                # 收集送审数据（含 scene_type 等辅助字段，供审核 prompt 判定静态/动态冲突）
                audit_shot_inputs.append({
                    "shot_number": shot_number,
                    "shot_type": prompt_result.get("shot_type", "demo"),
                    "scene_type": prompt_result.get("scene_type", "dynamic"),
                    "first_frame": prompt_result.get("first_frame", ""),
                    "motion": prompt_result.get("motion", ""),
                    "camera": prompt_result.get("camera", ""),
                    "duration": prompt_result.get("duration", ""),
                    "constraints": prompt_result.get("constraints", []),
                    "negative_constraints": prompt_result.get("negative_constraints", []),
                    "audio": prompt_result.get("audio", {}) if isinstance(prompt_result.get("audio"), dict) else {},
                })

                # 创建审核记录，设置状态为待审核
                await airtable.create_review(
                    shot_id=shot_id,
                    review_type="提示词审核",
                    result="待审核",
                    description="提示词已生成，等待审核",
                )

        logger.info(f"Updated {updated_count} shots with generation prompts for project {project_id}")

        # ==========================================================================
        # 分镜提示词自审核子步骤
        # 目的：逐镜头校验 first_frame/motion 与产品物理属性的一致性，
        # 将审核结果落地到每个分镜的“提示词审核状态”。
        # ==========================================================================
        audit_summary = {
            "total": len(audit_shot_inputs),
            "passed_count": 0,
            "rejected_count": 0,
            "warning_count": 0,
            "rejected_shot_numbers": [],
            "rejected_details": [],  # [{shot_number, reason_summary, critical_issues}]
        }

        try:
            # 从 Assets 表拉取审核上下文（product_analysis / video_analysis / product_listing_info）
            assets = await airtable.get_project_assets(project_id)
            product_analysis_str = ""
            video_analysis_str = ""
            product_listing_str = ""
            for asset in assets:
                asset_fields = asset.get("fields", {})
                asset_type = asset_fields.get("素材类型", "")
                content = asset_fields.get("内容", "") or ""
                if asset_type == "product":
                    product_analysis_str = content
                    # 尝试从 content 中解析出 product_listing_info
                    try:
                        metadata = json.loads(content) if content else {}
                        listing_info = metadata.get("product_listing_info") if isinstance(metadata, dict) else None
                        if listing_info:
                            product_listing_str = (
                                json.dumps(listing_info, ensure_ascii=False)
                                if not isinstance(listing_info, str)
                                else listing_info
                            )
                    except Exception:
                        # content 不是合法 JSON，忽略。product_listing 允许为空
                        pass
                elif asset_type == "video":
                    video_analysis_str = content

            # 调用审核：优先使用 Qwen（百炼 qwen-plus），Gemini 作为兜底
            audit_result: dict = {"shots": []}
            audit_provider_used = "qwen"
            try:
                from services.qwen_service import QwenService
                qwen = QwenService()
                qwen.set_context(project_id, "stage3")
                audit_result = await qwen.audit_shot_prompts(
                    shots=audit_shot_inputs,
                    product_analysis=product_analysis_str,
                    product_listing_info=product_listing_str,
                    video_analysis=video_analysis_str,
                    script=script_content or "",
                )
                if not audit_result.get("shots"):
                    raise RuntimeError("Qwen 审核返回空结果，降级到 Gemini")
            except Exception as qwen_err:
                logger.warning(
                    f"Qwen 审核不可用或无有效输出，回退到 Gemini: {qwen_err}"
                )
                audit_provider_used = "gemini"
                audit_result = await gemini.audit_shot_prompts(
                    shots=audit_shot_inputs,
                    product_analysis=product_analysis_str,
                    product_listing_info=product_listing_str,
                    video_analysis=video_analysis_str,
                    script=script_content or "",
                )

            audited_shots = audit_result.get("shots", []) if isinstance(audit_result, dict) else []
            logger.info(
                f"Shot prompt audit provider={audit_provider_used}, "
                f"returned {len(audited_shots)} shot verdicts"
            )

            if not audited_shots:
                # 审核失败或返回空：兜底按 “全部通过” 处理，保持原有行为
                logger.warning(
                    f"Shot prompt audit returned empty result for project {project_id}, "
                    f"fallback: marking all {len(audit_shot_inputs)} shots as passed"
                )
                for shot_input in audit_shot_inputs:
                    sid = shot_id_by_number.get(shot_input["shot_number"])
                    if sid:
                        try:
                            await airtable.update_shot_prompt_status(
                                sid, "已通过", review_comment="[审核异常兜底通过] 自审核未返回有效结果"
                            )
                            audit_summary["passed_count"] += 1
                        except Exception as inner_e:
                            logger.warning(
                                f"Fallback prompt status update failed for shot {sid}: {inner_e}"
                            )
            else:
                # 逐镜头落库审核结果
                for shot_verdict in audited_shots:
                    shot_number = shot_verdict.get("shot_number")
                    sid = shot_id_by_number.get(shot_number)
                    if not sid:
                        continue

                    passed = bool(shot_verdict.get("passed", True))
                    critical_issues = shot_verdict.get("critical_issues", []) or []
                    warnings = shot_verdict.get("warnings", []) or []
                    reason_summary = shot_verdict.get("reason_summary", "") or ""

                    if critical_issues:
                        passed = False  # 安全兵：有 critical 一律当作驳回

                    # 构建审核意见文本
                    comment_parts: list[str] = []
                    if reason_summary:
                        comment_parts.append(reason_summary)
                    if critical_issues:
                        comment_parts.append(
                            "Critical: "
                            + "; ".join(
                                f"[{i.get('type', 'unknown')}] {i.get('description', '')}"
                                for i in critical_issues
                            )
                        )
                    if warnings:
                        comment_parts.append(
                            "Warning: "
                            + "; ".join(
                                f"[{w.get('type', 'unknown')}] {w.get('description', '')}"
                                for w in warnings
                            )
                        )
                    review_comment = " | ".join(comment_parts)[:1000]  # Airtable 单行文本宽限

                    try:
                        if not passed:
                            # 驳回：尝试写“已驳回”；如 Airtable 选项缺失则降级为“待审核” + 前缀标记
                            try:
                                await airtable.update_shot_prompt_status(
                                    sid, "已驳回", review_comment=review_comment
                                )
                            except Exception as e_rej:
                                logger.warning(
                                    f"update_shot_prompt_status=已驳回 failed for shot {sid}, "
                                    f"fallback to 待审核: {e_rej}"
                                )
                                await airtable.update_shot_prompt_status(
                                    sid,
                                    "待审核",
                                    review_comment=f"[自动驳回] {review_comment}",
                                )
                            audit_summary["rejected_count"] += 1
                            audit_summary["rejected_shot_numbers"].append(shot_number)
                            audit_summary["rejected_details"].append({
                                "shot_number": shot_number,
                                "reason_summary": reason_summary,
                                "critical_issues": critical_issues,
                            })
                        else:
                            # 通过（可能带 warning）
                            final_comment = review_comment or "自动审核通过"
                            await airtable.update_shot_prompt_status(
                                sid, "已通过", review_comment=final_comment
                            )
                            audit_summary["passed_count"] += 1
                            if warnings:
                                audit_summary["warning_count"] += 1
                    except Exception as e:
                        logger.warning(
                            f"Update shot prompt status failed for shot {sid} (shot_number={shot_number}): {e}"
                        )

            logger.info(
                f"Shot prompt audit summary for project {project_id}: "
                f"total={audit_summary['total']}, passed={audit_summary['passed_count']}, "
                f"rejected={audit_summary['rejected_count']} (shots={audit_summary['rejected_shot_numbers']}), "
                f"warning_only={audit_summary['warning_count']}"
            )
        except Exception as e:
            # 审核整体异常不阻断主流程：记录告警，并给全部镜头打上兜底“已通过”
            logger.warning(
                f"Shot prompt audit block failed for project {project_id}: {e}. "
                f"Fallback: auto-approving all shots to preserve legacy behavior."
            )
            for shot_input in audit_shot_inputs:
                sid = shot_id_by_number.get(shot_input["shot_number"])
                if sid:
                    try:
                        await airtable.update_shot_prompt_status(
                            sid, "已通过", review_comment="[审核异常兜底通过]"
                        )
                        audit_summary["passed_count"] += 1
                    except Exception:
                        pass

        # ==========================================================================
        # 约束生成子步骤
        # ==========================================================================
        try:
            # 从 Assets 表获取产品分析和视频分析结果
            assets = await airtable.get_project_assets(project_id)
            
            product_analysis = ""
            video_analysis = ""
            
            for asset in assets:
                asset_fields = asset.get("fields", {})
                asset_type = asset_fields.get("素材类型", "")
                content = asset_fields.get("内容", "")
                
                if asset_type == "product":
                    product_analysis = content
                elif asset_type == "video":
                    video_analysis = content
            
            # Fallback: 如果 product_analysis 为空，记录警告
            if not product_analysis:
                logger.warning(f"Product analysis not found in Assets for project {project_id}")
            
            # Fallback: 如果 video_analysis 为空，记录警告
            if not video_analysis:
                logger.warning(f"Video analysis not found in Assets for project {project_id}")
            
            # Fallback: 如果 script_content 为空，从 Shots 表重构脚本摘要
            if not script_content:
                logger.info(f"Script content not found in project, reconstructing from Shots for project {project_id}")
                shots_for_script = await airtable.get_project_shots(project_id)
                script_summary = []
                for shot in shots_for_script:
                    sf = shot.get("fields", {})
                    script_summary.append({
                        "镜头序号": sf.get("镜头序号"),
                        "原镜头描述": sf.get("原镜头描述", ""),
                        "新镜头描述": sf.get("新镜头描述", ""),
                    })
                script_content = json.dumps({"shots": script_summary}, ensure_ascii=False)
                logger.info(f"从 Shots 表重构脚本摘要: {len(script_summary)} 个镜头")
            
            # 检查三个数据源是否都为空
            if not product_analysis and not video_analysis and script_content == json.dumps({"shots": []}, ensure_ascii=False):
                logger.warning(
                    f"All constraint data sources are empty for project {project_id}: "
                    f"product_analysis={bool(product_analysis)}, "
                    f"video_analysis={bool(video_analysis)}, "
                    f"script_content={bool(script_content)}"
                )
            
            # 调用约束生成器（项目级别，只需生成一次）
            logger.info(
                f"Calling generate_constraints for project {project_id} with: "
                f"product_analysis={len(product_analysis) if product_analysis else 0} chars, "
                f"video_analysis={len(video_analysis) if video_analysis else 0} chars, "
                f"script_content={len(script_content) if script_content else 0} chars"
            )
            
            constraints = await generate_constraints(
                product_analysis=product_analysis,
                video_analysis=video_analysis,
                script=script_content,
                gemini_service=gemini,
            )
            
            negative_prompt = constraints.get("negative_prompt", "")
            prompt_enhancement = constraints.get("prompt_enhancement", "")
            
            logger.info(
                f"Generated constraints for project {project_id}: "
                f"negative_prompt={len(negative_prompt)} chars, "
                f"prompt_enhancement={len(prompt_enhancement)} chars"
            )
            
            # 将约束写入每个镜头的 Airtable 记录
            for shot in shots:
                shot_id = shot.get("id")
                if shot_id:
                    await airtable.update_shot(
                        shot_id=shot_id,
                        data={
                            "negative_prompt": negative_prompt,
                            "prompt_enhancement": prompt_enhancement,
                        }
                    )
            
            logger.info(f"Updated {len(shots)} shots with constraints for project {project_id}")
            
        except Exception as e:
            # 约束生成失败不阻断主流程，记录警告并继续
            logger.warning(f"Constraint generation failed for project {project_id}: {e}")
            logger.warning("Continuing without constraints - using empty defaults")

        # 更新项目状态为生成中
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.GENERATING
        )
        logger.info(f"Project {project_id} status updated to GENERATING")

        return {
            "success": True,
            "project_id": project_id,
            "mode": "full",
            "total_prompts": len(prompt_results),
            "updated_shots": updated_count,
            "audit_summary": audit_summary,
        }

    except Exception as e:
        logger.error(f"Stage 3 failed for project {project_id}: {e}")
        # 更新项目状态为失败
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.FAILED
        )
        raise


# 保持向后兼容的别名
stage3_prompts = run_stage3
