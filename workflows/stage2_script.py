"""
阶段二：分镜脚本生成
基于视频分析和商品分析结果生成复刻脚本
"""

import logging
from typing import TYPE_CHECKING

from services import GeminiService, AirtableService
from config import settings
from models.schemas import ProjectStatus, ShotStatus

if TYPE_CHECKING:
    from models.schemas import ReplicationMode

logger = logging.getLogger(__name__)


async def run_stage2(
    project_id: str,
    mode: "ReplicationMode" = "full",
    replicate_hook: bool = True
) -> dict:
    """
    阶段二：分镜脚本生成

    Args:
        project_id: 项目 ID
        mode: 复刻模式，simple 或 full
        replicate_hook: 是否复刻 hook 镜头

    Returns:
        阶段执行结果
    """
    logger.info(f"Starting Stage 2: Script Generation for project {project_id}, mode={mode}")

    # 初始化服务
    gemini = GeminiService()
    gemini.set_context(project_id, "stage2")
    airtable = AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID
    )

    try:
        # 简单模式：跳过脚本生成阶段
        if mode == "simple":
            logger.info(f"Skipping Stage 2 in SIMPLE mode for project {project_id}")
            return {
                "success": True,
                "project_id": project_id,
                "mode": "simple",
                "skipped": True,
                "message": "Script generation skipped in simple mode"
            }

        # 幂等检查：job 重试时若分镜记录已创建，跳过重新生成，避免产生重复分镜
        existing_shots = await airtable.get_project_shots(project_id)
        if existing_shots:
            logger.info(
                f"[{project_id}] Stage 2 已有 {len(existing_shots)} 条分镜记录，"
                "跳过重新生成（幂等重试）"
            )
            # 尝试从项目记录恢复脚本内容（Stage 2 完成时会写入 script_content）
            script_result = {}
            try:
                import json
                project = await airtable.get_project(project_id)
                raw_script = (project or {}).get("fields", {}).get("script_content", "")
                if raw_script:
                    script_result = json.loads(raw_script)
            except Exception as e:
                logger.warning(f"[{project_id}] 恢复 script_content 失败（不阻塞）: {e}")
            # 重放最终状态更新（本身幂等）
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.PROMPT_CONVERTING
            )
            return {
                "success": True,
                "project_id": project_id,
                "mode": "full",
                "skipped": True,
                "total_shots": len(existing_shots),
                "script": script_result,
            }

        # 从 Airtable 获取素材
        assets = await airtable.get_project_assets(project_id)
        logger.info(f"Retrieved {len(assets)} assets for project {project_id}")

        # 查找视频分析、商品分析、节奏分析结果和商品链接提取信息
        video_analysis = None
        product_analysis = None
        rhythm_analysis = None
        product_listing_info = None
        product_brief = None

        for asset in assets:
            fields = asset.get("fields", {})
            content = fields.get("内容", "")
            # 尝试解析内容为 JSON
            metadata = {}
            try:
                import json
                if content:
                    # 尝试直接解析
                    try:
                        metadata = json.loads(content)
                    except json.JSONDecodeError as e:
                        # 如果解析失败，尝试修复常见的截断问题
                        logger.warning(f"JSON parse error for asset: {e}")
                        
                        # 策略1: 移除末尾的截断标记和之后的内容
                        fixed_content = content
                        truncate_marker = "...(content truncated"
                        if truncate_marker in content:
                            fixed_content = content[:content.find(truncate_marker)]
                        
                        # 策略2: 清理可能的无效控制字符（保留 \n, \t, \r）
                        # JSON 字符串中不允许某些控制字符
                        cleaned_content = ""
                        for char in fixed_content:
                            code = ord(char)
                            # 允许的可打印字符和空白字符
                            if code >= 32 or code in (9, 10, 13):  # tab, newline, carriage return
                                cleaned_content += char
                        
                        # 策略3: 尝试找到最后一个完整的JSON结构
                        for end_marker in ['"}', '"]', '}', ']']:
                            last_idx = cleaned_content.rfind(end_marker)
                            if last_idx > 0:
                                candidate = cleaned_content[:last_idx+len(end_marker)]
                                try:
                                    metadata = json.loads(candidate)
                                    logger.warning(f"Fixed truncated JSON using marker {repr(end_marker)}")
                                    break
                                except:
                                    continue
                        
                        # 策略4: 使用括号平衡法找到完整的 JSON 对象
                        if not metadata:
                            # 找到 analysis_result 后的第一个 {
                            ar_pos = cleaned_content.find('"analysis_result"')
                            if ar_pos > 0:
                                json_start = cleaned_content.find('{', ar_pos)
                                if json_start > 0:
                                    # 使用括号平衡找到匹配的 }
                                    depth = 0
                                    for i in range(json_start, len(cleaned_content)):
                                        if cleaned_content[i] == '{':
                                            depth += 1
                                        elif cleaned_content[i] == '}':
                                            depth -= 1
                                            if depth == 0:
                                                # 找到了完整的 JSON
                                                try:
                                                    analysis_data = json.loads(cleaned_content[json_start:i+1])
                                                    metadata = {"analysis_result": analysis_data}
                                                    logger.warning(f"Extracted analysis_result using bracket balancing (ended at {i})")
                                                    break
                                                except:
                                                    pass
                                                break
                        
                        # 策略5: 如果仍然失败，尝试正则提取
                        if not metadata:
                            import re
                            # 尝试匹配 analysis_result 字段的内容
                            match = re.search(r'"analysis_result"\s*:\s*({.*?})(?:,\s*"|$)', cleaned_content, re.DOTALL)
                            if match:
                                try:
                                    analysis_json = match.group(1)
                                    # 尝试补全不完整的 JSON
                                    metadata = {"analysis_result": json.loads(analysis_json)}
                                    logger.warning("Extracted analysis_result from truncated content")
                                except:
                                    pass
            except Exception as e:
                logger.warning(f"Failed to parse asset content: {e}")
                metadata = {}
            
            asset_type = fields.get("素材类型", "")
            if asset_type == "video" and "analysis_result" in metadata:
                video_analysis = metadata["analysis_result"]
            elif asset_type == "product" and "analysis_result" in metadata:
                product_analysis = metadata["analysis_result"]
                # 提取商品链接信息（如果存在）
                if "product_listing_info" in metadata and metadata["product_listing_info"]:
                    product_listing_info = metadata["product_listing_info"]
                # 提取 Product Brief Agent 产出的 brief（如果存在）
                if "product_brief" in metadata and metadata["product_brief"]:
                    product_brief = metadata["product_brief"]
            elif asset_type == "rhythm" and "analysis_result" in metadata:
                rhythm_analysis = metadata["analysis_result"]

        if not video_analysis:
            raise ValueError(f"Video analysis result not found for project {project_id}")
        if not product_analysis:
            raise ValueError(f"Product analysis result not found for project {project_id}")

        logger.info(f"Found analysis results for project {project_id} (rhythm={'yes' if rhythm_analysis else 'no'}, listing={'yes' if product_listing_info else 'no'}, brief={'yes' if product_brief else 'no'}, replicate_hook={replicate_hook})")

        # 将 Product Brief 的丰富字段注入 listing_info，供脚本生成提示词使用
        if product_brief and isinstance(product_brief, dict):
            listing_merged = dict(product_listing_info or {})
            for k in (
                "key_selling_points",
                "target_audience",
                "tone",
                "competitor_differentiators",
                "constraints",
                "brand",
            ):
                v = product_brief.get(k)
                if v:
                    # brief 优先覆盖（Agent 信息更丰富）
                    listing_merged[k] = v
            product_listing_info = listing_merged
            logger.info(
                f"[{project_id}] Product Brief injected into listing_info: "
                f"target_audience={bool(product_brief.get('target_audience'))}, "
                f"tone={bool(product_brief.get('tone'))}, "
                f"selling_points={len(product_brief.get('key_selling_points') or [])}"
            )

        # 动作兼容性检测：在脚本生成前检测原视频动作与新产品的兼容性
        action_compatibility = None
        try:
            logger.info(f"Running action compatibility check for project {project_id}")
            action_compatibility = await gemini.check_action_compatibility(
                video_analysis=video_analysis,
                product_analysis=product_analysis,
                product_listing_info=product_listing_info
            )
            if action_compatibility:
                overall = action_compatibility.get("overall_compatibility", "unknown")
                incompatible_count = action_compatibility.get("incompatible_shot_count", 0)
                logger.info(
                    f"Action compatibility check completed for project {project_id}: "
                    f"overall={overall}, incompatible_shots={incompatible_count}"
                )
        except Exception as e:
            logger.warning(f"Action compatibility check failed (non-blocking): {e}")
            action_compatibility = None

        # 调用 Gemini 生成复刻脚本（注入节奏数据、兼容性检测结果和 hook 复刻标志）
        logger.info(f"Generating script for project {project_id} (replicate_hook={replicate_hook})")
        script_result = await gemini.generate_script(
            video_analysis=video_analysis,
            product_analysis=product_analysis,
            rhythm_analysis=rhythm_analysis,
            action_compatibility=action_compatibility,
            product_listing_info=product_listing_info,
            replicate_hook=replicate_hook
        )

        shots = script_result.get("shots", [])
        logger.info(f"Generated {len(shots)} shots for project {project_id}")

        # ========== 自动验证 + 自修正循环（Stage 2.5） ==========
        validation_result = None
        max_fix_rounds = 2

        if product_listing_info:
            # 仅当有商品详情页信息时才做验证（否则无法做功能接地检查）
            for fix_round in range(max_fix_rounds + 1):  # 0=首次验证, 1-2=修正后重新验证
                try:
                    validation_result = await gemini.validate_script(
                        script_result=script_result,
                        product_listing_info=product_listing_info,
                        video_analysis=video_analysis
                    )

                    passed = validation_result.get("passed", False)
                    confidence = validation_result.get("confidence", 0.0)
                    issues = validation_result.get("issues", [])
                    critical_issues = [i for i in issues if i.get("severity") == "critical"]

                    logger.info(
                        f"[Validation round {fix_round}] project={project_id}: "
                        f"passed={passed}, confidence={confidence:.2f}, "
                        f"critical_issues={len(critical_issues)}, total_issues={len(issues)}"
                    )

                    if passed or not critical_issues:
                        # 验证通过或无严重问题，退出循环
                        logger.info(f"Script validation passed for project {project_id} (round {fix_round})")
                        break

                    if fix_round < max_fix_rounds:
                        # 还有修正机会，调用 fix_script
                        logger.info(
                            f"Attempting script fix (round {fix_round + 1}/{max_fix_rounds}) "
                            f"for {len(critical_issues)} critical issues"
                        )
                        script_result = await gemini.fix_script(
                            script_result=script_result,
                            validation_issues=critical_issues,
                            video_analysis=video_analysis,
                            product_analysis=product_analysis,
                            product_listing_info=product_listing_info
                        )
                        shots = script_result.get("shots", [])
                        logger.info(f"Script fixed: {len(shots)} shots after round {fix_round + 1}")
                    else:
                        # 最大修正轮次用完，标记需人审
                        logger.warning(
                            f"Script still has {len(critical_issues)} critical issues after "
                            f"{max_fix_rounds} fix rounds for project {project_id}"
                        )
                except Exception as e:
                    logger.warning(f"Validation/fix round {fix_round} failed (non-blocking): {e}")
                    # 验证失败不阻塞流程，标记为需人审
                    if not validation_result:
                        validation_result = {
                            "passed": False, "confidence": 0.0,
                            "issues": [{"type": "validation_error", "severity": "critical",
                                        "shot_numbers": [], "description": str(e), "fix_instruction": ""}]
                        }
                    break
        else:
            logger.info(f"Skipping script validation (no product listing info) for project {project_id}")
            # 无详情页信息时默认需要人审
            validation_result = {"passed": False, "confidence": 0.0, "issues": []}

        # 将验证结果保存到脚本中，供后续阶段读取
        script_result["_validation"] = validation_result
        # ========== 验证循环结束 ==========

        # ========== §14 品牌尾镜后处理兜底过滤（万一 Gemini 未识别）==========
        import re as _re
        _brand_logo_kws = ["lazada", "shopee", "tiktok shop", "tiktokshop", "brand logo", "platform logo", "heart logo"]
        _cta_kws = ["shop now", "buy now", "click to buy", "add to cart", "立即购买", "一键下单"]
        _product_motion_kws = ["hand", "finger", "squeez", "press", "rub", "scrub", "pour", "apply", "hold", "grip", "twist", "手", "按", "揉", "搔", "涂", "抜"]

        def _is_brand_outro(shot: dict) -> bool:
            text = " ".join(str(shot.get(k, "") or "") for k in ("new_description", "original_description", "changes_made"))
            tl = text.lower()
            has_brand = any(k in tl for k in _brand_logo_kws)
            has_cta = any(k in tl for k in _cta_kws)
            has_motion = any(k in tl for k in _product_motion_kws)
            # 规则：品牌 Logo + (CTA OR 无产品动作)
            return has_brand and (has_cta or not has_motion)

        if shots and len(shots) > 3:
            # 仅扫描末尾连续的品牌尾镜（从最后向前，命中即移除，遇到正常镜头停止）
            # 总镜头数 ≤ 3 时不自动过滤，避免残留内容太少
            filtered_original_numbers = []
            while shots and _is_brand_outro(shots[-1]):
                bad = shots.pop()
                filtered_original_numbers.append(bad.get("shot_number"))
                logger.warning(
                    f"[§14 Post-filter] 移除品牌尾镜 #{bad.get('shot_number')}: "
                    f"{str(bad.get('new_description', ''))[:100]}"
                )

            if filtered_original_numbers:
                # 重新连续编号
                for new_idx, shot in enumerate(shots, start=1):
                    shot["shot_number"] = new_idx
                # 更新 summary
                summary = script_result.setdefault("summary", {})
                summary["filtered_brand_outros_count"] = int(summary.get("filtered_brand_outros_count") or 0) + len(filtered_original_numbers)
                existing_notes = summary.get("notes", "") or ""
                post_note = f"[Post-filter] 已移除 {len(filtered_original_numbers)} 个品牌尾镜（原镜号 {filtered_original_numbers}）"
                summary["notes"] = f"{existing_notes} | {post_note}" if existing_notes else post_note
                script_result["shots"] = shots
                logger.info(f"[§14 Post-filter] 最终 shots={len(shots)}, 过滤品牌尾镜={len(filtered_original_numbers)}")
        # ========== 后处理过滤结束 ==========

        # 逐镜头写入 Airtable 分镜表
        shot_records = []
        for i, shot in enumerate(shots):
            shot_data = {
                "project_id": project_id,
                "shot_number": shot.get("shot_number", i + 1),
                "original_shot_description": shot.get("original_description", ""),
                "new_shot_description": shot.get("new_description", ""),
                "status": ShotStatus.PENDING,
                "metadata": {
                    "duration": shot.get("duration", ""),
                    "changes_made": shot.get("changes_made", ""),
                    "forced_change": shot.get("forced_change", False),
                    "forced_change_reason": shot.get("forced_change_reason", ""),
                    "shot_type": shot.get("shot_type", "demo")
                }
            }
            shot_records.append(shot_data)

        # 批量创建分镜记录
        if shot_records:
            await airtable.batch_create_shots(shot_records)
            logger.info(f"Created {len(shot_records)} shot records for project {project_id}")

        # 更新项目状态为提示词转换中
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.PROMPT_CONVERTING
        )
        await airtable.update_project(
            project_id=project_id,
            data={"script_content": json.dumps(script_result, ensure_ascii=False)}
        )
        logger.info(f"Project {project_id} status updated to PROMPT_CONVERTING")

        return {
            "success": True,
            "project_id": project_id,
            "mode": "full",
            "total_shots": len(shots),
            "forced_changes_count": script_result.get("summary", {}).get("forced_changes_count", 0),
            "script": script_result
        }

    except Exception as e:
        logger.error(f"Stage 2 failed for project {project_id}: {e}")
        # 更新项目状态为失败
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.FAILED
        )
        raise


# 保持向后兼容的别名
stage2_script = run_stage2
