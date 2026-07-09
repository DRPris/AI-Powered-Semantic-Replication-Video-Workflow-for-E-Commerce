"""
阶段五：视频合成

职责：
1. 收集所有生成的视频片段
2. 使用 FFmpeg 合成完整视频
3. 添加转场效果
4. 添加字幕（可选）
5. 添加背景音乐（可选）
6. OST (On-Screen Text) 叠加（可选）
7. 导出最终视频
8. 上传至 OSS
9. 更新项目状态和最终视频 URL
"""

import ast
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from config import settings
from services.ffmpeg_service import FFmpegService, detect_transition_frame, crop_video_from_time
from services.oss_service import OSSService
from services.airtable_service import AirtableService
from services.suno_service import SunoService
from services.sound_effect_service import SoundEffectService
from services.qwen_service import QwenService
from services.ost_service import apply_ost_overlay
from services.ost_localizer import (
    localize_osts,
    apply_localization_inplace,
    extract_manual_overrides_from_shots,
    save_localization_to_shots,
)
from services.token_tracker import token_tracker

logger = logging.getLogger(__name__)


def _get_shot_type_from_fields(shot_fields: dict[str, Any]) -> str:
    """
    从分镜字段中解析镜头类型（hook / demo 等）
    
    优先顺序：
    1. 生成提示词中的 shot_type
    2. metadata 中的 shot_type
    3. 默认值 "demo"
    """
    # 1. 从生成提示词中读取
    raw_prompt = shot_fields.get("生成提示词", "")
    if raw_prompt:
        parsed = None
        if isinstance(raw_prompt, str):
            # 方案1：JSON 解析
            try:
                parsed = json.loads(raw_prompt)
            except (json.JSONDecodeError, TypeError):
                pass
            # 方案2：Python dict 解析（处理单引号格式）
            if parsed is None:
                try:
                    parsed = ast.literal_eval(raw_prompt)
                except (ValueError, SyntaxError):
                    pass
            # 方案3：正则表达式提取 shot_type
            if parsed is None:
                match = re.search(r"['\"]shot_type['\"]\s*:\s*['\"]([\w]+)['\"]", raw_prompt)
                if match:
                    return match.group(1).lower()
        elif isinstance(raw_prompt, dict):
            parsed = raw_prompt
        if isinstance(parsed, dict) and "shot_type" in parsed:
            return parsed["shot_type"].lower()
    
    # 2. 从 metadata 中读取
    metadata_str = shot_fields.get("metadata", "")
    if metadata_str:
        try:
            parsed_meta = None
            if isinstance(metadata_str, str):
                try:
                    parsed_meta = json.loads(metadata_str)
                except (json.JSONDecodeError, TypeError):
                    pass
                if parsed_meta is None:
                    try:
                        parsed_meta = ast.literal_eval(metadata_str)
                    except (ValueError, SyntaxError):
                        pass
            else:
                parsed_meta = metadata_str
            if isinstance(parsed_meta, dict) and "shot_type" in parsed_meta:
                return parsed_meta["shot_type"].lower()
        except Exception:
            pass
    
    return "demo"


async def run_stage5(
    project_id: str,
    skip_clip_editing: bool = False,
) -> dict[str, Any]:
    """
    阶段五：视频合成

    流程：
    1. 从 Airtable 获取所有视频审核已通过的镜头
    2. 按镜头序号排序，提取视频 URL 列表
    3. 若 Shots 表存在“剪辑指令”（由 clip_editor_agent 写入）则优先执行精确裁剪 / 变速
    4. 调用 FFmpeg 合成视频
    5. 上传至 OSS
    6. 将成品视频 URL 写回 Airtable 项目表
    7. 更新项目状态为“已完成”

    Args:
        project_id: 项目 ID
        skip_clip_editing: 是否跳过 edit_plan 的应用（回滚开关）

    Returns:
        包含最终视频 URL 和合成信息的字典
    """
    logger.info(
        f"开始阶段五：视频合成 - 项目 {project_id}, skip_clip_editing={skip_clip_editing}"
    )

    # 初始化服务
    airtable_service = AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
    )
    ffmpeg_service = FFmpegService(
        ffmpeg_bin=settings.FFMPEG_BIN_PATH,
        temp_dir=settings.FFMPEG_TEMP_DIR,
    )
    oss_service = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )

    # 1. 获取项目所有分镜
    shots = await airtable_service.get_project_shots(project_id)
    if not shots:
        raise ValueError(f"项目 {project_id} 没有找到任何分镜")

    logger.info(f"项目 {project_id} 共有 {len(shots)} 个分镜")

    # 2. 筛选审核通过的分镜，并按序号排序
    approved_shots = []
    for shot in shots:
        shot_fields = shot.get("fields", {})
        if shot_fields.get("视频审核状态") == "已通过":
            approved_shots.append(shot)

    if not approved_shots:
        raise ValueError(f"项目 {project_id} 没有审核通过的分镜")

    # 按镜头序号排序
    approved_shots.sort(key=lambda x: x.get("fields", {}).get("镜头序号", 0))

    logger.info(f"项目 {project_id} 有 {len(approved_shots)} 个审核通过的分镜")

    # 3. 提取视频 URL 列表（使用 OSS 生成新鲜签名 URL）
    video_urls = []
    for shot in approved_shots:
        shot_fields = shot.get("fields", {})
        shot_number = shot_fields.get("镜头序号", 0)
        
        # 使用 OSS 生成新鲜的签名 URL（避免 Airtable 中存储的 URL 过期）
        oss_key = f"videos/{project_id}/shot_{shot_number}.mp4"
        try:
            video_url = oss_service.get_signed_url(oss_key, expires=3600)
            logger.info(f"镜头 {shot_number}: 生成新鲜 OSS URL")
        except Exception as e:
            logger.warning(f"镜头 {shot_number}: 生成 OSS URL 失败: {e}，尝试使用 Airtable 中的 URL")
            # 降级：使用 Airtable 中存储的 URL
            generated_video = shot_fields.get("生成视频", "")
            if isinstance(generated_video, str):
                video_url = generated_video
            elif isinstance(generated_video, list) and len(generated_video) > 0:
                if isinstance(generated_video[0], dict):
                    video_url = generated_video[0].get("url")
                elif isinstance(generated_video[0], str):
                    video_url = generated_video[0]
        
        if not video_url:
            raise ValueError(f"分镜 {shot.get('id')} (镜头{shot_number}) 没有生成视频 URL")
        video_urls.append(video_url)

    logger.info(f"准备合成 {len(video_urls)} 个视频片段")

    # 3.5 从 Airtable 素材表读取节奏分析数据和视频分析数据（可选）
    rhythm_analysis = await _load_rhythm_analysis(airtable_service, project_id)
    video_analysis = await _load_video_analysis(airtable_service, project_id)

    # 3.6 OST 本地化（基于 ProductBrief 将原商品专有名词改写为新商品文案）
    # 优先级：Shots 表用户手动 OST本地化 > 缓存 > Gemini
    # 直接在 video_analysis 原地改写 on_screen_text.content，后续 OST 叠加逻辑无需修改
    if settings.ENABLE_OST_OVERLAY and settings.ENABLE_OST_LOCALIZATION and video_analysis:
        try:
            brief_state = await airtable_service.get_product_brief_state(project_id)
            product_brief = (brief_state or {}).get("draft")
            if product_brief and product_brief.get("product_name"):
                # 构造当前原 OST 映射，用于校验手动覆盖的时效性
                current_original_map: dict[int, str] = {}
                _shots_for_ost = video_analysis.get("shots") if isinstance(video_analysis, dict) else video_analysis
                for _idx, _s in enumerate(_shots_for_ost or []):
                    _ost = (_s or {}).get("on_screen_text") or {}
                    _c = (_ost.get("content") or "").strip()
                    if _c:
                        current_original_map[_s.get("shot_id") or (_idx + 1)] = _c

                manual_overrides = extract_manual_overrides_from_shots(
                    approved_shots, original_ost_map=current_original_map
                )

                from services.gemini_service import GeminiService
                gemini_service = GeminiService()
                try:
                    localized_map = await localize_osts(
                        gemini_service=gemini_service,
                        product_brief=product_brief,
                        video_analysis=video_analysis,
                        manual_overrides=manual_overrides,
                    )
                    if localized_map:
                        modified = apply_localization_inplace(video_analysis, localized_map)
                        logger.info(f"OST 本地化完成：{modified} 条被改写/删除")

                        # 回写到 Shots 表（跳过 user_manual_override）
                        try:
                            await save_localization_to_shots(
                                airtable_service, approved_shots, localized_map
                            )
                        except Exception as _e:
                            logger.warning(f"OST 本地化结果回写 Shots 表失败（不阻塞）: {_e}")
                finally:
                    await gemini_service.close()
            else:
                logger.info("未找到有效 ProductBrief（或 product_name 为空），跳过 OST 本地化")
        except Exception as e:
            logger.warning(f"OST 本地化失败（不阻塞主流程，将使用原 OST）: {e}")

    rhythm_plan = None
    if rhythm_analysis:
        rhythm_plan = _build_rhythm_plan(approved_shots, rhythm_analysis)
        logger.info(f"节奏时间轴控制已启用，共 {len(rhythm_plan)} 个镜头计划")
    else:
        logger.info("未找到节奏分析数据，使用默认合成参数")

    # 4. 下载视频到本地，并对第一个镜头进行智能裁剪
    temp_dir = Path(settings.FFMPEG_TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    downloaded_files = []
    local_video_path = None
    try:
        # 使用更长的超时时间和重试机制
        timeout = httpx.Timeout(300.0, connect=60.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for i, url in enumerate(video_urls):
                logger.info(f"下载视频 {i+1}/{len(video_urls)}")
                
                # 重试机制
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = await client.get(url)
                        response.raise_for_status()
                        break
                    except (httpx.ReadError, httpx.ConnectError) as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"下载视频 {i+1} 失败 (尝试 {attempt+1}/{max_retries}): {e}，重试中...")
                            await asyncio.sleep(2)
                        else:
                            raise
                
                tmp_file = temp_dir / f"stage5_input_{i:03d}.mp4"
                tmp_file.write_bytes(response.content)
                logger.info(f"视频 {i+1} 下载完成: {tmp_file} ({len(response.content)} bytes)")
                downloaded_files.append(str(tmp_file))
        
        # 4.1 优先应用 clip_editor_agent 产出的 edit_plan（精确裁剪 + 变速）
        # 设计：已应用 edit_plan 的镜头会被后续"第一个非 Hook 镜头裁剪"逻辑跳过，
        # 且 rhythm_plan 的 target_duration 也不再生效（文件已裁至目标时长）。
        edit_plan_applied: list[bool] = [False] * len(downloaded_files)
        if not skip_clip_editing:
            for i, (shot, video_path) in enumerate(zip(approved_shots, downloaded_files)):
                shot_fields = shot.get("fields", {}) or {}
                raw_plan = shot_fields.get("剪辑指令", "")
                if not raw_plan:
                    continue
                plan_dict = None
                if isinstance(raw_plan, str):
                    try:
                        plan_dict = json.loads(raw_plan)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"镜头 {i+1} 剪辑指令 JSON 解析失败: {e}")
                        continue
                elif isinstance(raw_plan, dict):
                    plan_dict = raw_plan
                if not plan_dict:
                    continue

                strategy = plan_dict.get("strategy", "no_op")

                # Phase 2 门控：语义选段策略需检查剪辑审核状态
                if strategy == "trim_semantic":
                    review_status = shot_fields.get("剪辑审核状态", "")
                    if review_status != "已通过":
                        # 降级为 trim_head: 从头裁剪到 target_duration
                        target_dur = plan_dict.get("target_duration", 5.0)
                        plan_dict["strategy"] = "trim_head"
                        plan_dict["trim"] = {"start_sec": 0.0, "end_sec": target_dur}
                        strategy = "trim_head"
                        logger.info(
                            f"镜头 {i+1} 语义选段未批准 (状态={review_status})，降级 trim_head"
                        )

                if strategy == "no_op":
                    edit_plan_applied[i] = True  # 标记为已处理，跳过旧裁剪
                    continue

                try:
                    edited_path = await ffmpeg_service.apply_edit_plan(
                        input_path=video_path,
                        edit_plan=plan_dict,
                        output_path=str(temp_dir / f"stage5_input_{i:03d}_edited.mp4"),
                    )
                    if edited_path and edited_path != video_path:
                        downloaded_files[i] = edited_path
                    edit_plan_applied[i] = True
                    logger.info(
                        f"镜头 {i+1} 应用 edit_plan 完成: strategy={strategy}, 产物 {downloaded_files[i]}"
                    )
                except Exception as e:
                    logger.warning(
                        f"镜头 {i+1} 应用 edit_plan 失败，回落到旧逻辑: {e}"
                    )
        else:
            logger.info("skip_clip_editing=True，跳过 edit_plan 应用")

        # 过渡检测：第一个非 Hook 镜头需要裁剪掉三视图首帧。
        # 设计：
        # - 已应用 edit_plan 且 trim.start_sec > 0 的镜头跳过（edit_plan 已包含裁剪起点）
        # - 未应用 edit_plan 的镜头走原有独立 crop 逻辑
        # - 已应用 edit_plan 但 trim.start_sec == 0 的镜头：
        #   将过渡检测结果合并进 edit_plan 的 start_sec（避免多余编码）
        first_product_shot_found = False
        for i, (shot, video_path) in enumerate(zip(approved_shots, downloaded_files)):
            shot_fields = shot.get("fields", {})
            shot_type = _get_shot_type_from_fields(shot_fields)

            if not first_product_shot_found and shot_type != "hook":
                needs_crop = True
                first_product_shot_found = True
            else:
                needs_crop = False
                if shot_type != "hook":
                    first_product_shot_found = True

            if not needs_crop:
                continue

            # edit_plan 已处理且 trim 起点 > 0：说明 Agent 已选择了非零起点段落，无需过渡检测
            if edit_plan_applied[i]:
                raw_plan = (shot.get("fields", {}) or {}).get("剪辑指令", "")
                plan_dict = None
                if isinstance(raw_plan, str):
                    try:
                        plan_dict = json.loads(raw_plan)
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif isinstance(raw_plan, dict):
                    plan_dict = raw_plan

                trim_start = float((plan_dict or {}).get("trim", {}).get("start_sec", 0.0) or 0.0)
                if trim_start > 0.1:
                    logger.info(
                        f"镜头 {i+1} edit_plan 已从 {trim_start:.2f}s 开始裁剪，跳过过渡检测"
                    )
                    continue

                logger.info(f"镜头 {i+1} edit_plan trim.start_sec=0，仍需检测过渡点")

            shot_number = shot_fields.get("镜头序号", i + 1)
            logger.info(f"镜头 {shot_number} 标记为需要裁剪，检测过渡点: {video_path}")

            transition_time = detect_transition_frame(video_path)

            if transition_time <= 0:
                transition_time = 1.0
                logger.info(f"镜头 {shot_number} 未检测到过渡点，使用 fallback 时间 1.0s")

            logger.info(f"镜头 {shot_number} 检测到过渡点 {transition_time:.3f}s，开始裁剪")
            cropped_path = await crop_video_from_time(
                ffmpeg_bin=settings.FFMPEG_BIN_PATH,
                input_path=video_path,
                start_time=transition_time,
                output_path=str(temp_dir / f"stage5_input_{i:03d}_cropped.mp4"),
            )
            downloaded_files[i] = cropped_path
            edit_plan_applied[i] = True  # 标记为已处理，避免 rhythm_plan 重复裁剪
            logger.info(f"镜头 {shot_number} 过渡裁剪完成: {cropped_path}")
        
        # 4.5 逐镜头生成并混入环境音（可选，仅 Kling 无声视频需要）
        if settings.ENABLE_AMBIENT_AUDIO:
            try:
                downloaded_files = await _apply_ambient_audio_per_shot(
                    project_id=project_id,
                    approved_shots=approved_shots,
                    downloaded_files=downloaded_files,
                    ffmpeg_service=ffmpeg_service,
                    temp_dir=temp_dir,
                )
            except Exception as e:
                logger.warning(f"镜头环境音混音整体失败（不阻塞主流程）: {e}")
        else:
            logger.info("镜头环境音功能已禁用 (ENABLE_AMBIENT_AUDIO=false)")
        
        # 5. 调用 FFmpeg 拼接视频（节奏感知 / 默认）
        if rhythm_plan and len(rhythm_plan) == len(downloaded_files):
            # 已应用 edit_plan 的镜头把 target_duration 置为 None，避免重复裁剪
            for i, applied in enumerate(edit_plan_applied):
                if applied and i < len(rhythm_plan):
                    rhythm_plan[i]["target_duration"] = None
            logger.info("使用节奏感知合成 (concatenate_videos_with_rhythm)")
            local_video_path = await ffmpeg_service.concatenate_videos_with_rhythm(
                video_paths=downloaded_files,
                rhythm_plan=rhythm_plan,
            )
        else:
            local_video_path = await ffmpeg_service.concatenate_videos(
                video_paths=downloaded_files,
                transition_duration=0.5,
            )
        
        # 获取视频信息
        duration = await ffmpeg_service._get_video_duration(local_video_path)
        file_size = os.path.getsize(local_video_path)
        render_id = f"stage5_{project_id}_{asyncio.get_event_loop().time()}"
        
        logger.info(f"FFmpeg 合成完成: {render_id}, 本地文件: {local_video_path}, "
                   f"时长: {duration:.2f}s, 大小: {file_size} bytes")

        # 5.5 BGM 生成与混入（可选）
        if settings.ENABLE_BGM and rhythm_analysis:
            try:
                bgm_video_path = await _generate_and_mix_bgm(
                    ffmpeg_service=ffmpeg_service,
                    video_path=local_video_path,
                    rhythm_analysis=rhythm_analysis,
                    project_id=project_id,
                    temp_dir=temp_dir,
                )
                if bgm_video_path:
                    old_path = local_video_path
                    local_video_path = bgm_video_path
                    # 清理混入前的文件
                    try:
                        if os.path.exists(old_path) and old_path != local_video_path:
                            os.unlink(old_path)
                    except OSError:
                        pass
                    logger.info(f"BGM 混入成功: {local_video_path}")
            except Exception as e:
                logger.warning(f"BGM 生成/混入失败（不阻塞主流程）: {e}")
        elif not settings.ENABLE_BGM:
            logger.info("BGM 功能已禁用 (ENABLE_BGM=false)")
        elif not rhythm_analysis:
            logger.info("未找到节奏分析数据，跳过 BGM 生成")

        # 5.6 OST + 字幕叠加（可选）
        if (settings.ENABLE_OST_OVERLAY or settings.ENABLE_SUBTITLE_OVERLAY) and video_analysis:
            try:
                # 构建各镜头时长和转场时长列表
                shot_durations = []
                for f in downloaded_files:
                    d = await ffmpeg_service._get_video_duration(f)
                    shot_durations.append(d)

                if rhythm_plan and len(rhythm_plan) == len(downloaded_files):
                    t_durations = [p.get("transition_duration", 0.0) for p in rhythm_plan]
                else:
                    t_durations = [0.0] + [0.5] * (len(downloaded_files) - 1)

                # 获取视频宽高用于自适应字号（宽度用于避免横向溢出）
                v_width, v_height = await ffmpeg_service._get_video_resolution(local_video_path)

                ost_output = str(temp_dir / f"stage5_ost_{project_id}.mp4")
                ost_result = await apply_ost_overlay(
                    ffmpeg_bin=settings.FFMPEG_BIN_PATH,
                    input_video_path=local_video_path,
                    output_video_path=ost_output,
                    video_analysis=video_analysis,
                    approved_shots=approved_shots,
                    shot_durations=shot_durations,
                    transition_durations=t_durations,
                    video_height=v_height,
                    video_width=v_width,
                    enable_subtitle=settings.ENABLE_SUBTITLE_OVERLAY,
                )
                if ost_result:
                    # 替换为 OST 叠加后的视频
                    old_path = local_video_path
                    local_video_path = ost_result
                    duration = await ffmpeg_service._get_video_duration(local_video_path)
                    file_size = os.path.getsize(local_video_path)
                    # 清理拼接原始文件
                    try:
                        if os.path.exists(old_path) and old_path != local_video_path:
                            os.unlink(old_path)
                    except OSError:
                        pass
                    logger.info(f"OST+字幕叠加完成，时长: {duration:.2f}s")
            except Exception as e:
                logger.warning(f"OST+字幕叠加失败（不阻塞主流程）: {e}")
        else:
            if not settings.ENABLE_OST_OVERLAY and not settings.ENABLE_SUBTITLE_OVERLAY:
                logger.info("OST 和字幕叠加均已禁用")
            elif not video_analysis:
                logger.info("未找到视频分析数据，跳过 OST/字幕叠加")
        
    finally:
        # 清理下载的临时文件（保留最终合成文件）
        for f in downloaded_files:
            try:
                if os.path.exists(f) and f != local_video_path:
                    os.unlink(f)
                    logger.debug(f"清理临时文件: {f}")
            except OSError as e:
                logger.warning(f"清理临时文件失败: {f}, 错误: {e}")

    # 5. 更新项目状态为合成中
    await airtable_service.update_project(
        project_id=project_id,
        data={
            "状态": "COMPOSING",
        },
    )

    # 6. 上传至 OSS
    logger.info(f"开始上传视频到 OSS: {local_video_path}")
    file_name = os.path.basename(local_video_path)
    oss_key = f"final_videos/{project_id}/{file_name}"
    final_video_url = await oss_service.upload_file(local_video_path, oss_key)
    logger.info(f"OSS 上传完成: {final_video_url}")

    # 7. 清理临时文件
    try:
        if local_video_path and os.path.exists(local_video_path):
            os.unlink(local_video_path)
            logger.info(f"临时文件清理完成: {local_video_path}")
    except OSError as e:
        logger.warning(f"清理临时文件失败: {e}")

    # 8. 更新项目状态为已完成，并把成片 URL 写回项目记录（控制台成品页读取）
    await airtable_service.update_project(
        project_id=project_id,
        data={
            "状态": "COMPLETED",
            "成片链接": final_video_url,
            "成片时长": duration,
        },
    )

    logger.info(f"项目 {project_id} 视频合成完成")

    # 9. 同步 Token 消耗统计到 Airtable
    try:
        summary = token_tracker.get_project_summary(project_id)
        if summary.get("call_count", 0) > 0:
            await airtable_service.sync_token_usage(project_id, summary)
    except Exception as sync_err:
        logger.warning(f"Token usage sync failed (non-blocking): {sync_err}")

    return {
        "final_video_url": final_video_url,
        "duration": duration,
        "file_size": file_size,
        "status": "completed",
    }


async def stage5_composition(
    project_id: str,
    shot_videos: list[dict[str, Any]],
    options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    阶段五：视频合成

    该阶段负责将所有生成的视频片段合成为完整视频，包括：
    - 按顺序收集所有分镜视频片段
    - 使用 FFmpeg 进行视频合成
    - 对第一个镜头进行智能裁剪（去除三视图静态画面）
    - 添加转场效果（淡入淡出、滑动等）
    - 添加字幕（如需要）
    - 添加背景音乐（如需要）
    - 导出最终视频
    - 上传至 OSS
    - 更新项目状态为已完成

    Args:
        project_id: 项目 ID
        shot_videos: 分镜视频列表（包含 URL 和顺序）
            每个元素应包含: sequence_number, video_url
        options: 合成选项（转场类型、字幕、音频等）
            - width: 输出宽度 (默认 1920)
            - height: 输出高度 (默认 1080)
            - output_format: 输出格式 (默认 mp4)
            - transition_type: 转场类型 (默认 fade)
            - transition_duration: 转场时长 (默认 0.5)

    Returns:
        包含最终视频 URL 和合成信息的字典
        {
            "final_video_url": "...",
            "composition_info": {...},
            "status": "completed"
        }
    """
    logger.info(f"开始视频合成 - 项目 {project_id}")

    if not shot_videos:
        raise ValueError("没有提供视频片段")

    # 按序号排序
    shot_videos.sort(key=lambda x: x.get("sequence_number", 0))

    # 提取视频 URL
    video_urls = []
    for shot in shot_videos:
        video_url = shot.get("video_url") or shot.get("generated_video_url")
        if not video_url:
            raise ValueError(f"分镜 {shot.get('sequence_number')} 没有视频 URL")
        video_urls.append(video_url)

    # 初始化 FFmpeg 和 OSS 服务
    ffmpeg_service = FFmpegService(
        ffmpeg_bin=settings.FFMPEG_BIN_PATH,
        temp_dir=settings.FFMPEG_TEMP_DIR,
    )
    oss_service = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )

    # 合成选项
    options = options or {}
    transition_duration = options.get("transition_duration", 0.5)

    # 下载视频到本地，并对第一个镜头进行智能裁剪
    temp_dir = Path(settings.FFMPEG_TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    downloaded_files = []
    local_video_path = None
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for i, url in enumerate(video_urls):
                logger.info(f"下载视频 {i+1}/{len(video_urls)}: {url}")
                response = await client.get(url)
                response.raise_for_status()
                
                tmp_file = temp_dir / f"stage5_comp_input_{i:03d}.mp4"
                tmp_file.write_bytes(response.content)
                downloaded_files.append(str(tmp_file))
        
        # 智能裁剪：第一个非 Hook 镜头需要裁剪（使用了三视图首帧）
        # 支持显式 needs_crop 标记，同时提供规则推断作为备用
        first_product_shot_found = False
        for i, (shot, video_path) in enumerate(zip(shot_videos, downloaded_files)):
            needs_crop = shot.get("needs_crop", False)
            shot_type = shot.get("shot_type", "demo")
            
            # 备用逻辑：如果没有显式标记，通过规则推断
            if not needs_crop and not first_product_shot_found and shot_type != "hook":
                needs_crop = True
                first_product_shot_found = True
            elif shot_type != "hook":
                first_product_shot_found = True
            
            if not needs_crop:
                continue
            
            shot_number = shot.get("sequence_number", i + 1)
            logger.info(f"镜头 {shot_number} 标记为需要裁剪，检测过渡点: {video_path}")
            
            transition_time = detect_transition_frame(video_path)
            
            # 如果检测不到过渡点（返回 0.0），使用 fallback 时间 1.0 秒
            if transition_time <= 0:
                transition_time = 1.0
                logger.info(f"镜头 {shot_number} 未检测到过渡点，使用 fallback 时间 1.0s")
            
            logger.info(f"镜头 {shot_number} 检测到过渡点 {transition_time:.3f}s，开始裁剪")
            cropped_path = await crop_video_from_time(
                ffmpeg_bin=settings.FFMPEG_BIN_PATH,
                input_path=video_path,
                start_time=transition_time,
                output_path=str(temp_dir / f"stage5_comp_input_{i:03d}_cropped.mp4")
            )
            # 替换为裁剪后的视频
            downloaded_files[i] = cropped_path
            logger.info(f"镜头 {shot_number} 裁剪完成: {cropped_path}")
        
        # 调用 FFmpeg 拼接视频
        local_video_path = await ffmpeg_service.concatenate_videos(
            video_paths=downloaded_files,
            transition_duration=transition_duration,
        )
        
        # 获取视频信息
        duration = await ffmpeg_service._get_video_duration(local_video_path)
        file_size = os.path.getsize(local_video_path)
        render_id = f"stage5_{project_id}_{asyncio.get_event_loop().time()}"
        
        logger.info(f"FFmpeg 合成完成: {render_id}, 本地文件: {local_video_path}")
        
    finally:
        # 清理下载的临时文件（保留最终合成文件）
        for f in downloaded_files:
            try:
                if os.path.exists(f) and f != local_video_path:
                    os.unlink(f)
                    logger.debug(f"清理临时文件: {f}")
            except OSError as e:
                logger.warning(f"清理临时文件失败: {f}, 错误: {e}")

    # 上传至 OSS
    file_name = os.path.basename(local_video_path)
    oss_key = f"final_videos/{project_id}/{file_name}"
    final_video_url = await oss_service.upload_file(local_video_path, oss_key)
    logger.info(f"OSS 上传完成: {final_video_url}")

    # 清理最终合成文件
    try:
        if local_video_path and os.path.exists(local_video_path):
            os.unlink(local_video_path)
            logger.debug(f"清理最终合成文件: {local_video_path}")
    except OSError as e:
        logger.warning(f"清理最终合成文件失败: {local_video_path}, 错误: {e}")

    return {
        "final_video_url": final_video_url,
        "composition_info": {
            "render_id": render_id,
            "duration": duration,
            "file_size": file_size,
        },
        "status": "completed",
    }


async def prepare_composition_data(
    shot_videos: list[dict[str, Any]],
    options: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    准备视频合成所需的数据

    Args:
        shot_videos: 分镜视频列表
            每个元素应包含: sequence_number, video_url
        options: 合成选项
            - width: 输出宽度 (默认 1920)
            - height: 输出高度 (默认 1080)
            - output_format: 输出格式 (默认 mp4)
            - transition_type: 转场类型 (默认 fade)
            - transition_duration: 转场时长 (默认 0.5)

    Returns:
        FFmpeg 合成所需的参数数据
    """
    if not shot_videos:
        raise ValueError("没有提供视频片段")

    # 按序号排序
    shot_videos.sort(key=lambda x: x.get("sequence_number", 0))

    # 提取视频 URL
    video_urls = []
    for shot in shot_videos:
        video_url = shot.get("video_url") or shot.get("generated_video_url")
        if video_url:
            video_urls.append(video_url)

    options = options or {}
    width = options.get("width", 1920)
    height = options.get("height", 1080)
    output_format = options.get("output_format", "mp4")
    transition_type = options.get("transition_type", "fade")
    transition_duration = options.get("transition_duration", 0.5)

    return {
        "video_urls": video_urls,
        "output_format": output_format,
        "width": width,
        "height": height,
        "transition_type": transition_type,
        "transition_duration": transition_duration,
    }


async def add_subtitles_to_video(
    video_url: str,
    subtitle_data: list[dict[str, Any]],
) -> str:
    """
    为视频添加字幕

    注意：字幕叠加已集成到 apply_ost_overlay() 中，
    在 Stage 5 主流程 run_stage5() 内自动处理。
    本函数保留为独立调用入口，用于外部直接传入字幕数据的场景。

    Args:
        video_url: 视频 URL
        subtitle_data: 字幕数据（时间戳和文本）
            每个元素应包含: text, start, end

    Returns:
        添加字幕后的视频 URL
    """
    from services.ost_service import overlay_ost_on_video, smart_fontsize
    if not subtitle_data:
        return video_url

    # 下载视频
    temp_dir = Path(settings.FFMPEG_TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)
    input_path = str(temp_dir / f"subtitle_input_{id(subtitle_data)}.mp4")
    output_path = str(temp_dir / f"subtitle_output_{id(subtitle_data)}.mp4")

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(video_url)
        resp.raise_for_status()
        Path(input_path).write_bytes(resp.content)

    # 构建 subtitle items
    ffmpeg_service = FFmpegService(
        ffmpeg_bin=settings.FFMPEG_BIN_PATH,
        temp_dir=settings.FFMPEG_TEMP_DIR,
    )
    v_width, v_height = await ffmpeg_service._get_video_resolution(input_path)

    items = []
    for sub in subtitle_data:
        fontsize = smart_fontsize(sub["text"], v_height, video_width=v_width, role="subtitle")
        items.append({
            "content": sub["text"],
            "x": "(w-text_w)/2",
            "y": "h-text_h-30",
            "start": sub["start"],
            "end": sub["end"],
            "fontsize": fontsize,
            "fontcolor": "white",
            "boxcolor": "black@0.5",
            "boxborderw": 10,
            "fade_in": 0.2,
            "fade_out": 0.2,
            "is_subtitle": True,
        })

    result_path = await overlay_ost_on_video(
        ffmpeg_bin=settings.FFMPEG_BIN_PATH,
        input_path=input_path,
        ost_items=items,
        output_path=output_path,
    )

    # 上传到 OSS
    oss_service = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )
    file_name = os.path.basename(result_path)
    oss_key = f"subtitled_videos/{file_name}"
    final_url = await oss_service.upload_file(result_path, oss_key)

    # 清理临时文件
    for p in [input_path, output_path]:
        try:
            if os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass

    return final_url


# ---------------------------------------------------------------------------
# 节奏时间轴控制 —— 辅助函数
# ---------------------------------------------------------------------------

async def _load_video_analysis(
    airtable: AirtableService, project_id: str
) -> Optional[dict]:
    """
    从 Airtable 素材表中读取 asset_type='video' 的视频分析结果。
    返回视频分析 dict（含 shots 数组），未找到则返回 None。
    """
    try:
        assets = await airtable.get_project_assets(project_id)
        for asset in assets:
            fields = asset.get("fields", {})
            if fields.get("素材类型", "") != "video":
                continue
            content = fields.get("内容", "")
            if not content:
                continue
            metadata = json.loads(content) if isinstance(content, str) else content
            if "analysis_result" in metadata:
                logger.info(f"已加载视频分析数据 (project {project_id})")
                return metadata["analysis_result"]
    except Exception as e:
        logger.warning(f"读取视频分析数据失败 (project {project_id}): {e}")
    return None


async def _load_rhythm_analysis(
    airtable: AirtableService, project_id: str
) -> Optional[dict]:
    """
    从 Airtable 素材表中读取 asset_type='rhythm' 的分析结果。
    返回 rhythm_analysis 字典，未找到则返回 None。
    """
    try:
        assets = await airtable.get_project_assets(project_id)
        for asset in assets:
            fields = asset.get("fields", {})
            if fields.get("素材类型", "") != "rhythm":
                continue
            content = fields.get("内容", "")
            if not content:
                continue
            metadata = json.loads(content)
            if "analysis_result" in metadata:
                logger.info(f"已加载节奏分析数据 (project {project_id})")
                return metadata["analysis_result"]
    except Exception as e:
        logger.warning(f"读取节奏分析数据失败 (project {project_id}): {e}")
    return None


def _build_rhythm_plan(
    approved_shots: list[dict],
    rhythm_analysis: dict,
) -> list[dict]:
    """
    将节奏分析数据转化为逐镜头的合成控制参数列表。

    每个元素结构:
        {
            "target_duration": float | None,
            "transition_duration": float,
            "transition_type": str,
        }

    策略:
    - 如果 rhythm_analysis.shots 中的镜头数量 >= 审核通过镜头数，则按序一一匹配。
    - 否则用 replication_rhythm_guide.pace_zones 做时段级控制。
    - 第一个镜头的 transition_duration=0（无前序镜头）。
    - hard_cut 转场时长压缩为 0.05s（极短 fade）。
    """
    from services.ffmpeg_service import FFmpegService

    rhythm_shots = rhythm_analysis.get("shots", [])
    guide = rhythm_analysis.get("replication_rhythm_guide", {})
    overview = rhythm_analysis.get("overview", {})
    total_approved = len(approved_shots)

    plan: list[dict] = []

    if len(rhythm_shots) >= total_approved:
        # ---- 镜头级精确匹配 ----
        for idx in range(total_approved):
            rs = rhythm_shots[idx]
            cut_type = rs.get("cut_type", "fade")
            xfade_type = FFmpegService.map_cut_type_to_xfade(cut_type)

            if idx == 0:
                t_dur = 0.0  # 第一个镜头无前序转场
            elif cut_type in ("hard_cut", "smash_cut"):
                t_dur = 0.05  # 极短转场 ≈ 硬切
            else:
                t_dur = 0.5   # 默认淡入淡出

            plan.append({
                "target_duration": rs.get("duration_sec"),
                "transition_duration": t_dur,
                "transition_type": xfade_type,
            })
    else:
        # ---- 无法一一匹配，使用 pace_zones 做时段级控制 ----
        pace_zones = guide.get("pace_zones", [])
        overall_pace = overview.get("overall_pace", "medium")

        # 构建时段查找函数
        def _get_zone_pace(shot_index: int) -> str:
            """Rough mapping: 将镜头序号按比例投射到节奏时段"""
            if not pace_zones:
                return overall_pace
            ratio = shot_index / max(total_approved - 1, 1)
            zone_idx = min(int(ratio * len(pace_zones)), len(pace_zones) - 1)
            return pace_zones[zone_idx].get("target_pace", overall_pace)

        PACE_TRANSITION = {"fast": 0.15, "medium": 0.5, "slow": 0.8}

        for idx in range(total_approved):
            pace = _get_zone_pace(idx)
            t_dur = 0.0 if idx == 0 else PACE_TRANSITION.get(pace, 0.5)

            plan.append({
                "target_duration": None,  # 无精确时长，不裁剪
                "transition_duration": t_dur,
                "transition_type": "fade",
            })

    logger.info(f"节奏合成计划: {json.dumps(plan, ensure_ascii=False)[:300]}")
    return plan


# ---------------------------------------------------------------------------
# BGM 自动生成与混入 —— 辅助函数
# ---------------------------------------------------------------------------

def _build_bgm_style_from_rhythm(rhythm_analysis: dict) -> str:
    """
    从节奏分析数据中提取 BGM 风格描述，用于 Suno API 的 style 参数。

    输入示例:
        rhythm_analysis.audio.music_type = "electronic"
        rhythm_analysis.audio.estimated_bpm = 128
        rhythm_analysis.audio.audio_segments[0].mood = "energetic"
        rhythm_analysis.overview.overall_pace = "fast"

    输出示例: "Electronic, Energetic, 128 BPM, Fast-paced, Commercial"
    """
    parts = []

    # 音乐类型
    audio = rhythm_analysis.get("audio", {})
    music_type = audio.get("music_type", "")
    if music_type:
        parts.append(music_type.title())

    # 情绪/氛围
    segments = audio.get("audio_segments", [])
    if segments:
        mood = segments[0].get("mood", "")
        if mood:
            parts.append(mood.title())

    # BPM
    bpm = audio.get("estimated_bpm", 0)
    if bpm and bpm > 0:
        parts.append(f"{bpm} BPM")

    # 整体节奏
    overview = rhythm_analysis.get("overview", {})
    pace = overview.get("overall_pace", "")
    if pace:
        parts.append(f"{pace.title()}-paced")

    # 默认追加商业感标签
    parts.append("Instrumental")
    parts.append("Commercial")

    style = ", ".join(parts) if parts else "Upbeat, Electronic, Instrumental, Commercial"
    return style


async def _generate_and_mix_bgm(
    ffmpeg_service: FFmpegService,
    video_path: str,
    rhythm_analysis: dict,
    project_id: str,
    temp_dir: Path,
) -> Optional[str]:
    """
    生成 BGM 并混入视频。

    Args:
        ffmpeg_service: FFmpeg 服务实例
        video_path: 合成后的视频路径
        rhythm_analysis: 节奏分析数据
        project_id: 项目 ID
        temp_dir: 临时文件目录

    Returns:
        混入 BGM 后的视频路径，失败返回 None
    """
    suno_service = SunoService()

    if not suno_service.is_configured:
        logger.warning("Suno API 未配置，跳过 BGM 生成")
        return None

    # 1. 构建 BGM 风格描述
    bgm_style = _build_bgm_style_from_rhythm(rhythm_analysis)
    logger.info(f"[项目 {project_id}] BGM 风格: {bgm_style}")

    # 2. 调用 Suno 生成 BGM
    bgm_url = await suno_service.generate_bgm(
        style=bgm_style,
        title=f"BGM_{project_id[:8]}",
    )
    if not bgm_url:
        logger.warning(f"[项目 {project_id}] Suno BGM 生成失败，跳过")
        return None

    # 3. 下载 BGM 到本地
    bgm_local_path = str(temp_dir / f"bgm_{project_id}.mp3")
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(bgm_url)
            response.raise_for_status()
            with open(bgm_local_path, "wb") as f:
                f.write(response.content)
        logger.info(f"BGM 下载完成: {bgm_local_path} ({len(response.content)} bytes)")
    except Exception as e:
        logger.error(f"BGM 下载失败: {e}")
        return None

    # 4. 混入成片
    output_path = str(temp_dir / f"stage5_final_bgm_{project_id}.mp4")
    try:
        result = await ffmpeg_service.mix_bgm(
            video_path=video_path,
            bgm_path=bgm_local_path,
            volume=settings.BGM_VOLUME,
            fade_out_sec=2.0,
            output_path=output_path,
        )
        return result
    except Exception as e:
        logger.error(f"BGM 混入失败: {e}")
        return None
    finally:
        # 清理 BGM 临时文件
        try:
            if os.path.exists(bgm_local_path):
                os.unlink(bgm_local_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 镜头内环境音 —— 辅助函数
# ---------------------------------------------------------------------------

def _extract_ambient_from_shot(shot_fields: dict[str, Any]) -> str:
    """
    从 shot 的「生成提示词」里优先读取 Stage 3 直接产出的 `audio.ambient`。

    返回值：
    - 读到有效字符串 -> 直接返回（Stage 5 跳过 Qwen兄底）
    - 读不到 / 空字符串 -> 返回 ""，Stage 5 会降级到 Qwen.extract_ambient_prompt()
    """
    raw_prompt = shot_fields.get("生成提示词", "")
    parsed: Any = None
    if isinstance(raw_prompt, dict):
        parsed = raw_prompt
    elif isinstance(raw_prompt, str) and raw_prompt.strip():
        try:
            parsed = json.loads(raw_prompt)
        except (json.JSONDecodeError, TypeError):
            try:
                parsed = ast.literal_eval(raw_prompt)
            except (ValueError, SyntaxError):
                parsed = None
    if isinstance(parsed, dict):
        audio = parsed.get("audio")
        if isinstance(audio, dict):
            ambient = audio.get("ambient")
            if isinstance(ambient, str) and ambient.strip():
                return ambient.strip()
    return ""


def _extract_visual_prompt_text(shot_fields: dict[str, Any]) -> str:
    """
    从 shot 字段中抽取可读的视觉提示词文本，用于送给 Qwen 抽环境音。

    优先顺序：生成提示词（努力解 JSON/dict 里的 visual / scene / environment）
    → 分镜描述 → 镜头说明 → 原始字段。
    """
    raw_prompt = shot_fields.get("生成提示词", "")
    parsed: Any = None
    if isinstance(raw_prompt, dict):
        parsed = raw_prompt
    elif isinstance(raw_prompt, str) and raw_prompt.strip():
        try:
            parsed = json.loads(raw_prompt)
        except (json.JSONDecodeError, TypeError):
            try:
                parsed = ast.literal_eval(raw_prompt)
            except (ValueError, SyntaxError):
                parsed = None

    pieces: list[str] = []
    if isinstance(parsed, dict):
        for key in ("visual_prompt", "prompt", "scene", "environment",
                    "setting", "action", "description", "summary"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                pieces.append(v.strip())
        # 补充其他可读字符串值
        if not pieces:
            for v in parsed.values():
                if isinstance(v, str) and v.strip():
                    pieces.append(v.strip())
    elif isinstance(raw_prompt, str) and raw_prompt.strip():
        pieces.append(raw_prompt.strip())

    for fallback_key in ("分镜描述", "镜头说明", "场景描述"):
        v = shot_fields.get(fallback_key)
        if isinstance(v, str) and v.strip():
            pieces.append(v.strip())

    text = "\n".join(pieces).strip()
    # 限长，避免送太多 Token
    return text[:1500]


async def _apply_ambient_audio_per_shot(
    project_id: str,
    approved_shots: list[dict],
    downloaded_files: list[str],
    ffmpeg_service: FFmpegService,
    temp_dir: Path,
) -> list[str]:
    """
    为每个镜头：抽环境音 prompt → 调 ElevenLabs 生成音效 → FFmpeg 混入。

    Returns:
        替换后的本地视频路径列表。失败的镜头保留原路径（无声）。
    """
    sfx_service = SoundEffectService()
    if not sfx_service.is_configured:
        logger.warning("ELEVENLABS_API_KEY 未配置，跳过镜头环境音生成")
        return downloaded_files

    try:
        qwen = QwenService()
        qwen.set_context(project_id, "stage5_ambient")
    except Exception as e:
        logger.warning(f"QwenService 初始化失败，跳过镜头环境音生成: {e}")
        return downloaded_files

    new_files: list[str] = list(downloaded_files)
    success_cnt = 0
    skip_cnt = 0
    fail_cnt = 0

    for i, (shot, video_path) in enumerate(zip(approved_shots, downloaded_files)):
        shot_fields = shot.get("fields", {})
        shot_number = shot_fields.get("镜头序号", i + 1)

        # 1) 检测视频是否已有音轨（避免 Seedance 双重混音）
        try:
            has_audio = await _video_has_audio_track(
                ffmpeg_service.ffmpeg_bin, video_path
            )
        except Exception as e:
            logger.warning(f"镜头 {shot_number} 音轨检测失败，跳过环境音: {e}")
            skip_cnt += 1
            continue
        if has_audio:
            logger.info(f"镜头 {shot_number} 视频已有音轨，跳过环境音混入")
            skip_cnt += 1
            continue

        # 2) 优先直读 Stage 3 产出的 audio.ambient；没有再降级到 Qwen兄底
        ambient_prompt = _extract_ambient_from_shot(shot_fields)
        if ambient_prompt:
            logger.info(
                f"镜头 {shot_number} 直读 Stage 3 audio.ambient→ {ambient_prompt}"
            )
        else:
            visual_text = _extract_visual_prompt_text(shot_fields)
            if not visual_text:
                logger.info(f"镜头 {shot_number} 无可用视觉提示词，使用 fallback ambience")
                ambient_prompt = "subtle room tone, soft ambience"
            else:
                ambient_prompt = await qwen.extract_ambient_prompt(visual_text)
            logger.info(f"镜头 {shot_number} ambient prompt (兄底抽取): {ambient_prompt}")

        # 3) 获取镜头时长并生成音效
        try:
            duration = await ffmpeg_service._get_video_duration(video_path)
        except Exception as e:
            logger.warning(f"镜头 {shot_number} 获取时长失败，跳过环境音: {e}")
            fail_cnt += 1
            continue

        sfx_path = await sfx_service.generate_sound_effect(
            prompt=ambient_prompt,
            duration_sec=duration,
        )
        if not sfx_path:
            logger.warning(f"镜头 {shot_number} 环境音生成失败，保留无声版本")
            fail_cnt += 1
            continue

        # 4) 混入
        out_path = str(temp_dir / f"stage5_input_{i:03d}_ambient.mp4")
        try:
            mixed_path = await ffmpeg_service.mux_video_with_ambient(
                video_path=video_path,
                audio_path=sfx_path,
                volume=float(settings.AMBIENT_VOLUME),
                output_path=out_path,
            )
        except Exception as e:
            logger.warning(f"镜头 {shot_number} FFmpeg 环境音混入失败，保留无声版本: {e}")
            fail_cnt += 1
            continue

        new_files[i] = mixed_path
        success_cnt += 1

    logger.info(
        f"[项目 {project_id}] 镜头环境音汇总: 成功={success_cnt} 跳过={skip_cnt} 失败={fail_cnt} 总计={len(downloaded_files)}"
    )
    return new_files


async def _video_has_audio_track(ffmpeg_bin: str, video_path: str) -> bool:
    """
    判断视频是否含有音轨，使用同目录的 ffprobe（与 ffmpeg 同包安装）。

    实现思路：调 ffprobe 列出音频流，输出非空则认为有音轨。
    如果 ffprobe 不存在（使用 imageio_ffmpeg 纯 ffmpeg），退化为 False。
    """
    # 根据 ffmpeg 路径推断 ffprobe 路径
    ffprobe_bin = ffmpeg_bin.replace("ffmpeg", "ffprobe")
    if not os.path.exists(ffprobe_bin):
        # 临街没有 ffprobe 时保守判断为无音轨（Kling 默认场景）
        return False

    proc = await asyncio.create_subprocess_exec(
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "default=nw=1:nk=1",
        video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return bool(stdout and stdout.strip())
