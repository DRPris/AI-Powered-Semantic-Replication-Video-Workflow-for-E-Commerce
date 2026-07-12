"""
阶段四：视频生成（含首尾帧衔接）

职责：
1. 从 Airtable 获取已审核通过的分镜提示词
2. 从 Airtable 获取三视图作为初始参考图
3. 串行生成每个镜头（首尾帧衔接）：
   - 镜头1: 三视图作为 first_frame → 生成视频1 → 提取最后一帧
   - 镜头2: 视频1最后一帧作为 first_frame → 生成视频2 → 提取最后一帧
   - 镜头N: 依次类推
4. 每个镜头生成完成后立即写入 Airtable
5. 错误处理：单个镜头失败不影响后续，fallback 到三视图
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Any, Optional

import httpx

from services.video_gen_service import VideoGenService, VideoModel
from services.kling_official_service import KlingOfficialService
from services.wan_service import WanService
from services.airtable_service import AirtableService
from services.oss_service import OSSService
from services.audit_service import AuditService, AuditFailedException
from services.ffmpeg_service import FFmpegService
from config import settings


logger = logging.getLogger(__name__)


def _get_shot_type(shot_fields: dict[str, Any]) -> str:
    """
    从分镜字段中获取镜头类型
    
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
            try:
                parsed = json.loads(raw_prompt)
            except (json.JSONDecodeError, TypeError):
                try:
                    import ast
                    parsed = ast.literal_eval(raw_prompt)
                except (ValueError, SyntaxError):
                    pass
        elif isinstance(raw_prompt, dict):
            parsed = raw_prompt
        if isinstance(parsed, dict) and "shot_type" in parsed:
            return parsed["shot_type"]
    
    # 2. 从 metadata 中读取
    metadata_str = shot_fields.get("metadata", "")
    if metadata_str:
        try:
            if isinstance(metadata_str, str):
                metadata = json.loads(metadata_str)
            else:
                metadata = metadata_str
            if isinstance(metadata, dict) and "shot_type" in metadata:
                return metadata["shot_type"]
        except (json.JSONDecodeError, TypeError):
            pass
    
    return "demo"


def _parse_duration(duration_value: Any) -> int:
    """
    解析时长值，支持多种格式：
    - 数字: 3.5, 5, 10
    - 字符串: "3.5 seconds", "3.5s", "3.5", "5"
    - 空值: 返回默认值 5
    
    Args:
        duration_value: 原始时长值
        
    Returns:
        整数秒（向上取整），最小返回 5（可灵最小时长）
    """
    if duration_value is None:
        return 5
    
    # 如果是数字类型，直接处理
    if isinstance(duration_value, (int, float)):
        return max(5, int(duration_value))
    
    # 如果是字符串，解析各种格式
    if isinstance(duration_value, str):
        duration_str = duration_value.strip()
        if not duration_str:
            return 5
        
        # 移除 "seconds", "second", "s" 等后缀
        duration_str = re.sub(r'\s*(seconds?|s)\s*$', '', duration_str, flags=re.IGNORECASE)
        
        try:
            # 尝试解析为浮点数
            duration_float = float(duration_str)
            return max(5, int(duration_float))
        except (ValueError, TypeError):
            logger.warning(f"无法解析时长值: {duration_value}，使用默认值 5")
            return 5
    
    # 其他类型，尝试转字符串
    try:
        return max(5, int(float(str(duration_value))))
    except (ValueError, TypeError):
        logger.warning(f"无法解析时长值: {duration_value}，使用默认值 5")
        return 5


def _get_shot_duration(shot_fields: dict[str, Any], shot_number: int) -> int:
    """
    从分镜字段中获取时长
    
    优先顺序：
    1. metadata JSON 字段中的 duration
    2. shot_fields 中的 "duration" 或 "时长" 字段
    3. 默认值 5
    
    Args:
        shot_fields: 分镜字段字典
        shot_number: 镜头序号（用于日志）
        
    Returns:
        整数秒
    """
    raw_duration = None
    
    # 1. 尝试从 metadata 字段读取
    metadata_str = shot_fields.get("metadata", "")
    if metadata_str:
        try:
            if isinstance(metadata_str, str):
                metadata = json.loads(metadata_str)
            else:
                metadata = metadata_str
            
            if isinstance(metadata, dict) and "duration" in metadata:
                raw_duration = metadata["duration"]
                logger.debug(f"镜头 {shot_number} 从 metadata 读取时长: {raw_duration}")
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug(f"镜头 {shot_number} 解析 metadata 失败: {e}")
    
    # 2. 如果 metadata 中没有，尝试直接读取字段
    if raw_duration is None:
        for field_name in ["duration", "时长", "镜头时长"]:
            if field_name in shot_fields:
                raw_duration = shot_fields[field_name]
                logger.debug(f"镜头 {shot_number} 从字段 '{field_name}' 读取时长: {raw_duration}")
                break
    
    # 3. 解析时长值
    parsed_duration = _parse_duration(raw_duration)
    
    # 4. 根据平台处理最小时长（在调用处根据 platform 处理）
    logger.info(f"镜头 {shot_number} 原始时长: {raw_duration}, 解析后: {parsed_duration}s")
    
    return parsed_duration


def _convert_prompt_to_string(raw_prompt: Any) -> str:
    """
    将提示词转换为字符串格式
    
    支持 dict 或 string 格式的提示词，统一转换为字符串
    
    Args:
        raw_prompt: 原始提示词（dict 或 string）
        
    Returns:
        格式化后的提示词字符串
    """
    if not raw_prompt:
        return ""
    
    # 如果是字符串，尝试解析为 dict（支持 JSON 和 Python repr 格式）
    if isinstance(raw_prompt, str):
        parsed = None
        # 先尝试 JSON 解析
        try:
            parsed = json.loads(raw_prompt)
        except (json.JSONDecodeError, TypeError):
            pass
        # JSON 失败则尝试 ast.literal_eval（处理 Python str(dict) 的单引号格式）
        if parsed is None:
            try:
                import ast
                parsed = ast.literal_eval(raw_prompt)
            except (ValueError, SyntaxError):
                pass
        if isinstance(parsed, dict):
            raw_prompt = parsed
        elif parsed is not None:
            return str(parsed)
        else:
            return raw_prompt
    
    # 如果是字典，转换为格式化字符串
    if isinstance(raw_prompt, dict):
        prompt_parts = [
            f"First Frame: {raw_prompt.get('first_frame', '')}",
            f"Motion: {raw_prompt.get('motion', '')}",
            f"Camera: {raw_prompt.get('camera', '')}",
            f"Duration: {raw_prompt.get('duration', '')}",
        ]
        constraints = raw_prompt.get("constraints") or []
        if constraints:
            if isinstance(constraints, list):
                prompt_parts.append("Constraints: " + "; ".join(constraints))
            else:
                prompt_parts.append(f"Constraints: {constraints}")
        
        # 处理负面约束（防止 AI 自行添加视觉元素）
        negative_constraints = raw_prompt.get("negative_constraints") or []
        if negative_constraints:
            if isinstance(negative_constraints, list):
                prompt_parts.append("FORBIDDEN - Do NOT generate: " + "; ".join(negative_constraints))
            else:
                prompt_parts.append(f"FORBIDDEN - Do NOT generate: {negative_constraints}")
        
        # 处理静态场景标记
        scene_type = raw_prompt.get("scene_type", "")
        if scene_type == "static_display":
            prompt_parts.append("IMPORTANT: This is a STATIC product display scene. Do NOT add any dynamic effects, motion blur, particles, foam, bubbles, splashes, steam, smoke, sparkles, lens flares, or any visual effects not explicitly described in the script.")
        
        # 追加 "Only What's Described" 通用约束
        prompt_parts.append("Only render visual elements explicitly mentioned in the script. Do NOT hallucinate or add extra visual effects.")
        
        result = "\n".join(prompt_parts)
        
        # KIE AI prompt 最大 2500 字符，若超限则逐步裁剪
        max_len = 2300  # 留 200 字符余量给 iPhone 风格后缀等
        if len(result) > max_len:
            # 策略一：去掉 constraints
            prompt_parts_no_constraints = [
                f"First Frame: {raw_prompt.get('first_frame', '')}",
                f"Motion: {raw_prompt.get('motion', '')}",
                f"Camera: {raw_prompt.get('camera', '')}",
                f"Duration: {raw_prompt.get('duration', '')}",
            ]
            result = "\n".join(prompt_parts_no_constraints)
            logger.warning(f"Prompt 超过 {max_len} 字符，已移除 constraints，当前长度: {len(result)}")
        if len(result) > max_len:
            # 策略二：截断并加省略号
            result = result[:max_len - 3] + "..."
            logger.warning(f"Prompt 仍超限，已截断至 {max_len} 字符")
        return result
    
    # 其他类型转为字符串
    return str(raw_prompt)


async def _download_and_upload_to_oss(
    video_url: str,
    oss_service: OSSService,
    oss_key: str,
) -> str:
    """
    从临时 URL 下载视频并上传到 OSS
    
    Args:
        video_url: KIE AI 返回的临时视频 URL
        oss_service: OSS 服务实例
        oss_key: OSS 上的存储路径
        
    Returns:
        OSS 签名 URL
    """
    # 确保 tmp 目录存在
    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    
    temp_path = os.path.join(tmp_dir, f"temp_video_{os.urandom(4).hex()}.mp4")
    
    try:
        # 1. 下载 KIE AI 临时视频（不走代理）
        async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(120.0)) as client:
            logger.info(f"下载视频: {video_url[:60]}...")
            video_response = await client.get(video_url)
            video_response.raise_for_status()
        
        # 2. 保存到临时文件
        with open(temp_path, "wb") as f:
            f.write(video_response.content)
        logger.info(f"视频已保存到临时文件: {temp_path}, 大小: {len(video_response.content)} bytes")
        
        # 3. 上传到 OSS
        oss_url = await oss_service.upload_file(
            local_path=temp_path,
            oss_key=oss_key,
            content_type="video/mp4",
            expires=86400 * 7,  # 7天有效期
        )
        logger.info(f"视频已上传到 OSS: {oss_key}")
        
        return oss_url
        
    finally:
        # 4. 清理临时文件
        if os.path.exists(temp_path):
            os.remove(temp_path)
            logger.debug(f"临时文件已删除: {temp_path}")


async def _audit_generated_video(
    project_id: str,
    shot_id: str,
    shot_number: int,
    shot_prompt: str,
    oss_video_url: str,
    first_frame_url: Optional[str],
    oss_service: OSSService,
    airtable: AirtableService,
) -> bool:
    """模型审查点 4.4：下载生成视频 → ffmpeg 抽 4 帧 → 上传 OSS → 调 Qwen-VL 审查 → 写回 Airtable。

    Returns:
        True  = 通过（视频审核状态 = 已通过）
        False = 未通过（状态 = 已驳回，给人审）

    本函数不抛异常：Audit 任何异常均降级为“已驳回”，绝不影响镜头主流程的继续执行。
    """
    if not getattr(settings, "ENABLE_AUDIT_GENERATED_VIDEO", True):
        return True

    # 1. 下载视频到本地
    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    local_video_path = os.path.join(
        tmp_dir, f"audit_video_{project_id}_shot{shot_number}_{os.urandom(4).hex()}.mp4"
    )
    sample_frames_local: list[str] = []
    sample_frame_urls: list[str] = []
    try:
        try:
            async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(120.0)) as client:
                resp = await client.get(oss_video_url)
                resp.raise_for_status()
            with open(local_video_path, "wb") as f:
                f.write(resp.content)
            logger.info(
                f"[Audit4.4] 镜头 {shot_number} 已下载本地 ({len(resp.content)} bytes)"
            )
        except Exception as e:
            logger.warning(
                f"[Audit4.4] 镜头 {shot_number} 下载视频失败，降级为驳回: {e}"
            )
            try:
                await airtable.update_shot_video_status(
                    shot_id=shot_id,
                    status="已驳回",
                    review_comment=f"审查抽帧前视频下载失败: {e}",
                )
            except Exception:
                pass
            return False

        # 2. ffmpeg 抽帧
        ffmpeg_svc = FFmpegService()
        num_frames = int(getattr(settings, "AUDIT_VIDEO_SAMPLE_FRAMES", 4) or 4)
        try:
            sample_frames_local = await ffmpeg_svc.extract_sample_frames(
                video_path=local_video_path,
                num_frames=num_frames,
            )
        except Exception as e:
            logger.warning(f"[Audit4.4] 镜头 {shot_number} 抽帧异常: {e}")
            sample_frames_local = []

        if not sample_frames_local:
            logger.warning(
                f"[Audit4.4] 镜头 {shot_number} 未抽到任何帧，降级为驳回"
            )
            try:
                await airtable.update_shot_video_status(
                    shot_id=shot_id,
                    status="已驳回",
                    review_comment="审查抽帧失败，无法自动审查，请人工复核",
                )
            except Exception:
                pass
            return False

        # 3. 帧上传 OSS
        for idx, local_path in enumerate(sample_frames_local):
            oss_key = f"audit_frames/{project_id}/shot_{shot_number}/frame_{idx:02d}.png"
            try:
                frame_url = await oss_service.upload_file(
                    local_path=local_path,
                    oss_key=oss_key,
                    content_type="image/png",
                    expires=86400 * 3,
                )
                sample_frame_urls.append(frame_url)
            except Exception as e:
                logger.warning(
                    f"[Audit4.4] 镜头 {shot_number} frame {idx} 上传 OSS 失败: {e}"
                )

        if not sample_frame_urls:
            logger.warning(
                f"[Audit4.4] 镜头 {shot_number} 所有帧 OSS 上传失败，降级为驳回"
            )
            try:
                await airtable.update_shot_video_status(
                    shot_id=shot_id,
                    status="已驳回",
                    review_comment="审查帧图 OSS 上传失败，请人工复核",
                )
            except Exception:
                pass
            return False

        # 4. 调审查
        audit_svc = AuditService()
        audit_svc.set_context(project_id=project_id, stage=f"4.4/shot_{shot_number}")
        try:
            audit_result = await audit_svc.audit_generated_video(
                project_id=project_id,
                shot_number=shot_number,
                shot_prompt=shot_prompt or "",
                sample_frame_urls=sample_frame_urls,
                first_frame_url=first_frame_url or None,
            )
        except AuditFailedException:
            raise
        except Exception as e:
            logger.warning(
                f"[Audit4.4] 镜头 {shot_number} 审查调用异常，降级为驳回: {e}"
            )
            try:
                await airtable.update_shot_video_status(
                    shot_id=shot_id,
                    status="已驳回",
                    review_comment=f"审查调用异常: {e}",
                )
            except Exception:
                pass
            return False

        # 5. 写回 Airtable
        audit_status = "已驳回" if audit_result.should_block else "已通过"
        try:
            await airtable.update_shot_video_status(
                shot_id=shot_id,
                status=audit_status,
                review_comment=audit_result.to_review_comment(),
            )
        except Exception as write_err:
            logger.warning(
                f"[Audit4.4] 镜头 {shot_number} 写回审查结果失败: {write_err}"
            )

        if audit_result.should_block:
            logger.warning(
                f"[Audit4.4] 镜头 {shot_number} 审查未通过: "
                f"confidence={audit_result.confidence:.2f}, "
                f"issues={audit_result.critical_issues}"
            )
            return False
        logger.info(
            f"[Audit4.4] 镜头 {shot_number} 审查通过: "
            f"confidence={audit_result.confidence:.2f}"
        )
        return True
    finally:
        # 清理本地临时文件
        if os.path.exists(local_video_path):
            try:
                os.remove(local_video_path)
            except Exception:
                pass
        for fp in sample_frames_local:
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass


async def run_stage4(
    project_id: str,
    platform: str = "seedance",
) -> dict[str, Any]:
    """
    阶段四：AI视频生成（含首尾帧衔接）
    
    流程：
    1. 从 Airtable 获取该项目所有已审核通过的分镜提示词
    2. 从 Airtable 获取三视图（作为第一个镜头的参考图）
    3. 串行生成每个镜头：
       - 镜头1: 三视图作为 first_frame + 提示词 → 生成视频1 → 提取最后一帧
       - 镜头2: 视频1最后一帧作为 first_frame + 提示词 → 生成视频2 → 提取最后一帧
       - 镜头N: 依次类推
    4. 每个镜头生成完成后立即写入 Airtable（视频附件、状态更新）
    5. 所有镜头生成完成后，更新项目状态
    
    错误处理：
    - 单个镜头生成失败不影响后续镜头（跳过并标记失败）
    - 如果上一个镜头失败，下一个镜头使用三视图作为 fallback 参考图
    
    Args:
        project_id: 项目 ID
        platform: 视频生成平台 ("kling" 或 "seedance")
        
    Returns:
        生成结果汇总
        {
            "project_id": "...",
            "total_shots": N,
            "successful_shots": N,
            "failed_shots": N,
            "shots": [...],
            "status": "completed|partial|failed"
        }
    """
    logger.info(f"开始阶段四：视频生成，项目: {project_id}，平台: {platform}")
    
    # 初始化服务（按平台选择）
    # - platform=kling    -> 走可灵官方 API（klingai.com，JWT 签名，支持 image + image_tail 首尾双锚定）
    # - platform=seedance -> 走火山方舟直连端点（Seedance 不支持 last_frame_url，但支持 generate_audio）
    if platform == "kling":
        if not settings.KLING_ACCESS_KEY or not settings.KLING_SECRET_KEY:
            raise RuntimeError("[Stage4] platform=kling 但 KLING_ACCESS_KEY / KLING_SECRET_KEY 未配置，请在 .env 中填写官方可灵平台的 AK/SK")
        video_gen = KlingOfficialService(
            access_key=settings.KLING_ACCESS_KEY,
            secret_key=settings.KLING_SECRET_KEY,
            base_url=settings.KLING_BASE_URL,
            model=settings.KLING_MODEL,
        )
        logger.info(f"[Stage4] 使用可灵官方 API 调用 Kling（首尾双锚定模式，model={settings.KLING_MODEL}, mode={settings.KLING_MODE}）")
    elif settings.SEEDANCE_API_KEY:
        video_gen = VideoGenService(
            api_key=settings.SEEDANCE_API_KEY,
            base_url=settings.SEEDANCE_BASE_URL,
        )
        logger.info("[Stage4] 使用火山方舟 Seedance 直连 API")
    else:
        video_gen = VideoGenService(
            api_key=settings.KIE_API_KEY,
            base_url=settings.KIE_BASE_URL,
        )
        logger.warning("[Stage4] SEEDANCE_API_KEY 未配置，回退使用 KIE AI")
    airtable = AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
    )
    oss_service = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=getattr(settings, "OSS_CDN_DOMAIN", ""),
    )
    
    try:
        # 1. 获取分镜数据
        shots = await airtable.get_project_shots(project_id)
        
        # 过滤已审核通过的分镜
        approved_shots = []
        for s in shots:
            fields = s.get("fields", {})
            if fields.get("提示词审核状态") == "已通过" or fields.get("status") == "approved":
                approved_shots.append(s)
        
        if not approved_shots:
            logger.warning(f"项目 {project_id} 没有已审核通过的分镜")
            return {
                "project_id": project_id,
                "total_shots": 0,
                "successful_shots": 0,
                "failed_shots": 0,
                "shots": [],
                "status": "completed",
                "message": "没有已审核通过的分镜",
            }
        
        # 按分镜序号排序
        approved_shots.sort(key=lambda s: s.get("fields", {}).get("镜头序号", 0))
        
        logger.info(f"项目 {project_id} 共有 {len(approved_shots)} 个已审核通过的分镜")
        
        # 2. 获取三视图作为初始参考图
        assets = await airtable.get_project_assets(project_id)
        three_view_url = None
        
        # 改进查询逻辑，兼容多种素材类型标记
        for asset in assets:
            asset_fields = asset.get("fields", {})
            asset_type = asset_fields.get("素材类型", "").lower()
            
            if any(keyword in asset_type for keyword in ["三视图", "three_view", "three-view"]):
                # 从附件字段获取 URL
                attachments = asset_fields.get("附件", [])
                if attachments and len(attachments) > 0:
                    three_view_url = attachments[0].get("url")
                    logger.info(f"Found three-view asset with type '{asset_type}'")
                    break
            elif asset_type == "image":
                # fallback: 检查内容字段是否包含三视图信息
                content = str(asset_fields.get("内容", "")).lower()
                if "three_view" in content or "三视图" in content:
                    attachments = asset_fields.get("附件", [])
                    if attachments:
                        three_view_url = attachments[0].get("url")
                        logger.info(f"Found three-view asset via content fallback")
                        break
        
        # 如果找不到三视图，优先使用用户上传的商品真实照片（product_image），
        # 再退回商品分析素材附件。多图模式下项目没有三视图素材，真图就是产品锚点。
        if not three_view_url:
            logger.warning(f"项目 {project_id} 未找到三视图，尝试查找商品真实照片/原始产品图作为 fallback")
            for asset in assets:
                asset_fields = asset.get("fields", {})
                asset_type = asset_fields.get("素材类型", "").lower()
                if asset_type == "product_image":
                    attachments = asset_fields.get("附件", [])
                    if attachments and len(attachments) > 0:
                        three_view_url = attachments[0].get("url")
                        logger.info(f"Using user product photo as reference: {three_view_url[:60]}...")
                        break
            if not three_view_url:
                for asset in assets:
                    asset_fields = asset.get("fields", {})
                    asset_type = asset_fields.get("素材类型", "").lower()
                    if asset_type == "product":
                        # 从产品素材获取 URL
                        attachments = asset_fields.get("附件", [])
                        if attachments and len(attachments) > 0:
                            three_view_url = attachments[0].get("url")
                            logger.info(f"Using product image as fallback reference: {three_view_url[:60]}...")
                            break
        
        if not three_view_url:
            logger.warning(f"项目 {project_id} 未找到三视图或产品图，将不使用参考图生成")
        else:
            logger.info(f"项目 {project_id} 参考图 URL: {three_view_url[:60]}...")
        
        # 3. 串行生成每个镜头
        # 将 platform 字符串映射到模型名称
        # Seedance 优先使用 config 中的 SEEDANCE_MODEL（支持 ep-xxx 推理接入点）
        if platform == "seedance":
            model = settings.SEEDANCE_MODEL
        else:
            model_map = {
                "kling": VideoModel.KLING_V3,
            }
            model = model_map.get(platform, VideoModel.KLING_V3)
        
        # 确认日志：打印模型映射信息
        model_value = model.value if hasattr(model, 'value') else str(model)
        logger.info(f"[Model Mapping] platform={platform}, mapped_model={model_value}")
        
        current_reference_frame = three_view_url
        last_successful_frame = three_view_url  # 追踪最后一个成功提取的帧，用于失败回退
        
        results = []
        successful_count = 0
        failed_count = 0
        
        # ===== 预处理：跳过已完成镜头，准备提示词，按类型分组 =====
        prepared_shots = []  # 存储预处理后的待生成镜头
        project_keyframe_map: dict = {}  # 全局关键帧映射 {shot_number: keyframe_url}，含 completed 镜头
        product_shot_numbers: list = []  # 项目内所有产品镜头的序号列表
        
        def _extract_keyframe_url(fields: dict) -> str:
            v = fields.get("关键帧图片", "") or ""
            if isinstance(v, list) and v:
                v = v[0].get("url", "") if isinstance(v[0], dict) else str(v[0])
            return v.strip() if isinstance(v, str) else ""
        
        for i, shot in enumerate(approved_shots):
            shot_fields = shot.get("fields", {})
            shot_number = shot_fields.get("镜头序号", i + 1)
            shot_id = shot.get("id")
            gen_status = shot_fields.get("生成状态")
            
            # 先收集全局关键帧映射与产品镜头序号（无论是否 completed）
            _kf = _extract_keyframe_url(shot_fields)
            if _kf:
                project_keyframe_map[shot_number] = _kf
            if _get_shot_type(shot_fields) != "hook":
                product_shot_numbers.append(shot_number)
            
            # 跳过已完成的镜头
            if gen_status == "completed":
                logger.info(f"镜头 {shot_number} 已生成完成，跳过")
                continue
            
            # 转换提示词格式：优先使用"生成提示词"，回退到"新镜头描述"
            raw_prompt = shot_fields.get("生成提示词", "")
            if not raw_prompt:
                raw_prompt = shot_fields.get("新镜头描述", "")
                if raw_prompt:
                    logger.info(f"镜头 {shot_number} 使用'新镜头描述'作为生成提示词")
            
            # 读取约束字段
            negative_prompt = shot_fields.get("negative_prompt", "")
            prompt_enhancement = shot_fields.get("prompt_enhancement", "")
            
            # 提示词增强：将 prompt_enhancement 拼接到 prompt 前面
            if prompt_enhancement:
                final_prompt = f"{prompt_enhancement}\n\n{_convert_prompt_to_string(raw_prompt)}"
            else:
                final_prompt = _convert_prompt_to_string(raw_prompt)
            
            # 追加 iPhone 拍摄风格后缀
            iphone_style = "Shot on iPhone camera, 4K, realistic, slightly imperfect, authentic and raw feel, no professional lighting"
            final_prompt = f"{final_prompt}\n\n{iphone_style}"
            prompt = final_prompt
            
            if not prompt:
                error_msg = f"镜头 {shot_number} 没有生成提示词或新镜头描述，跳过"
                logger.warning(error_msg)
                
                # 更新 Airtable 状态为 failed 并写入错误信息
                try:
                    await airtable.update_shot_status(
                        shot_id=shot_id,
                        status="failed",
                    )
                    await airtable.create_review(
                        shot_id=shot_id,
                        review_type="视频审核",
                        result="需修改",
                        description=f"生成失败: {error_msg}",
                    )
                except Exception as update_error:
                    logger.error(f"更新空提示词镜头状态失败: {update_error}")
                
                results.append({
                    "shot_id": shot_id,
                    "shot_number": shot_number,
                    "status": "failed",
                    "error": error_msg,
                })
                continue
            
            # 获取镜头类型和时长
            shot_type = _get_shot_type(shot_fields)
            parsed_duration = _get_shot_duration(shot_fields, shot_number)
            duration = max(5, parsed_duration)
            
            # 提取关键帧图片 URL（来自 Airtable "关键帧图片" 字段）
            keyframe_url = shot_fields.get("关键帧图片", "") or ""
            # 兼容 Airtable 附件格式（数组）和纯 URL 字符串
            if isinstance(keyframe_url, list) and keyframe_url:
                keyframe_url = keyframe_url[0].get("url", "") if isinstance(keyframe_url[0], dict) else str(keyframe_url[0])
            keyframe_url = keyframe_url.strip() if isinstance(keyframe_url, str) else ""
            
            if keyframe_url:
                logger.info(f"镜头 {shot_number} 检测到关键帧图片: {keyframe_url[:60]}...")
            
            prepared_shots.append({
                "index": i,
                "shot": shot,
                "shot_id": shot_id,
                "shot_number": shot_number,
                "shot_type": shot_type,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "duration": duration,
                "keyframe_url": keyframe_url,
            })
        
        # ===== 双路径并行生成 =====
        hook_shots_list = [s for s in prepared_shots if s["shot_type"] == "hook"]
        product_shots_list = [s for s in prepared_shots if s["shot_type"] != "hook"]
        
        logger.info(f"镜头分组完成: Hook={len(hook_shots_list)}个, 产品={len(product_shots_list)}个")
        
        # --- 路径A：Hook 镜头生成（并发） ---
        async def generate_hook_shots():
            hook_results = []
            
            async def generate_single_hook(shot_info):
                s_id = shot_info["shot_id"]
                s_num = shot_info["shot_number"]
                s_prompt = shot_info["prompt"]
                s_neg = shot_info["negative_prompt"]
                s_dur = shot_info["duration"]
                
                # Hook 镜头关键帧策略：有关键帧且开启关键帧阶段 → 图生视频；否则 → 纯文生视频
                s_keyframe = shot_info.get("keyframe_url", "")
                if s_keyframe and settings.ENABLE_KEYFRAME_STAGE:
                    hook_first_frame = s_keyframe
                    logger.info(f"[Hook路径] Hook 镜头 {s_num} 使用关键帧图片作为首帧 (图生视频模式)")
                else:
                    hook_first_frame = None
                    if s_keyframe:
                        logger.info(f"[Hook路径] Hook 镜头 {s_num} 存在关键帧但 ENABLE_KEYFRAME_STAGE=False，退回纯文生视频")
                    else:
                        logger.info(f"[Hook路径] 开始生成 Hook 镜头 {s_num} (纯文生视频)")
                
                try:
                    # 生成视频（Kling 主 + wan 兜底）
                    try:
                        task_id = await video_gen.generate_video(
                            model=model,
                            prompt=s_prompt,
                            first_frame_url=hook_first_frame,  # 有关键帧时使用，否则为 None
                            last_frame_url=(s_keyframe if platform == "kling" and s_keyframe else None),  # Kling 模式下用关键帧锚定尾帧
                            negative_prompt=s_neg,
                            duration=s_dur,
                        )
                        
                        logger.info(f"[Hook路径] Hook 镜头 {s_num} 任务已提交，task_id: {task_id}")
                        await airtable.update_shot_status(shot_id=s_id, status="generating")
                        
                        video_result = await video_gen.wait_for_completion(
                            task_id=task_id, poll_interval=10, max_wait=600,
                        )
                        video_url = video_result["video_url"]
                    except Exception as kling_err:
                        # wan 兜底：仅 platform=kling 且 hook_first_frame 可用（wan 图生视频必须有首帧）
                        _wan_key = settings.WAN_API_KEY or settings.QWEN_API_KEY
                        if (platform == "kling" and settings.ENABLE_WAN_FALLBACK
                                and _wan_key and hook_first_frame):
                            logger.warning(
                                f"[Hook路径] Hook 镜头 {s_num} Kling 生成失败: {kling_err}，切换 wan2.7 兜底"
                            )
                            wan = WanService()
                            wan_result = await wan.generate_and_wait(
                                prompt=s_prompt,
                                first_frame_url=hook_first_frame,
                                last_frame_url=(s_keyframe if s_keyframe else None),
                                negative_prompt=s_neg,
                                duration=s_dur,
                            )
                            video_url = wan_result["video_url"]
                            logger.info(f"[Hook路径] Hook 镜头 {s_num} wan 兜底成功")
                            await airtable.update_shot_status(shot_id=s_id, status="generating")
                        else:
                            raise
                    
                    logger.info(f"[Hook路径] Hook 镜头 {s_num} 生成完成: {video_url}")
                    
                    # 上传到 OSS
                    oss_key = f"videos/{project_id}/shot_{s_num}.mp4"
                    oss_video_url = await _download_and_upload_to_oss(
                        video_url=video_url, oss_service=oss_service, oss_key=oss_key,
                    )
                    logger.info(f"[Hook路径] Hook 镜头 {s_num} 视频已上传到 OSS")
                    
                    await airtable.update_shot_status(shot_id=s_id, status="completed", video_url=oss_video_url)
                    await airtable.update_shot_video_status(shot_id=s_id, status="待审核")

                    # ---- 模型审查点 4.4：生成视频抽帧审查（Hook 镜头） ----
                    try:
                        await _audit_generated_video(
                            project_id=project_id,
                            shot_id=s_id,
                            shot_number=s_num,
                            shot_prompt=s_prompt,
                            oss_video_url=oss_video_url,
                            first_frame_url=hook_first_frame,
                            oss_service=oss_service,
                            airtable=airtable,
                        )
                    except Exception as audit_err:
                        logger.warning(
                            f"[Hook路径] Hook 镜头 {s_num} 审查流程异常（不阻断）: {audit_err}"
                        )

                    hook_results.append({
                        "shot_id": s_id,
                        "shot_number": s_num,
                        "status": "completed",
                        "video_url": video_url,
                    })
                    logger.info(f"[Hook路径] Hook 镜头 {s_num} 生成完成")
                    
                except Exception as e:
                    logger.error(f"[Hook路径] Hook 镜头 {s_num} 生成失败: {e!r} ({type(e).__name__})", exc_info=True)
                    try:
                        await airtable.update_shot_status(shot_id=s_id, status="failed")
                        await airtable.create_review(
                            shot_id=s_id, review_type="视频审核",
                            result="需修改", description=f"生成失败: {str(e)}",
                        )
                    except Exception:
                        pass
                    hook_results.append({
                        "shot_id": s_id,
                        "shot_number": s_num,
                        "status": "failed",
                        "error": str(e),
                    })
            
            # 并发生成所有 Hook 镜头
            if hook_shots_list:
                await asyncio.gather(*[
                    generate_single_hook(shot_info) for shot_info in hook_shots_list
                ])
            
            return hook_results
        
        # --- 路径B：产品镜头生成（顺序，首尾帧衔接） ---
        async def generate_product_shots():
            product_results = []
            prod_current_ref = three_view_url  # 初始为三视图
            prod_last_successful = three_view_url
            
            for pi, shot_info in enumerate(product_shots_list):
                s_id = shot_info["shot_id"]
                s_num = shot_info["shot_number"]
                s_prompt = shot_info["prompt"]
                s_neg = shot_info["negative_prompt"]
                s_dur = shot_info["duration"]
                
                # 首帧策略（优先级：关键帧 > 尾帧衔接/三视图）
                s_keyframe = shot_info.get("keyframe_url", "")
                
                # Kling 模式下开启首尾双锚定：首帧=上一尾帧/三视图，尾帧=当前关键帧
                kling_dual_anchor = (platform == "kling" and s_keyframe and settings.ENABLE_KEYFRAME_STAGE)
                
                if kling_dual_anchor:
                    # Kling 首尾双锚定模式
                    # 仅项目全局首个产品镜头用三视图；其他 pi==0（补跑场景）用上一镜头关键帧
                    min_prod_num = min(product_shot_numbers) if product_shot_numbers else s_num
                    is_global_first_product = (s_num == min_prod_num)
                    
                    if pi == 0 and is_global_first_product:
                        shot_reference_frame = three_view_url
                        logger.info(f"[产品路径][Kling双锚定] 镜头 {s_num} 首帧=三视图（项目首个产品镜头）、尾帧=关键帧")
                    elif pi == 0:
                        # 补跑场景：从 project_keyframe_map 找上一镜头的关键帧
                        prev_kf = project_keyframe_map.get(s_num - 1) or ""
                        if prev_kf:
                            shot_reference_frame = prev_kf
                            logger.info(f"[产品路径][Kling双锚定] 镜头 {s_num} 首帧=镜头{s_num - 1}关键帧、尾帧=关键帧（补跑衔接）")
                        else:
                            shot_reference_frame = three_view_url
                            logger.warning(f"[产品路径][Kling双锚定] 镜头 {s_num} 未找到镜头{s_num - 1}关键帧，退回三视图首帧")
                    else:
                        shot_reference_frame = prod_current_ref
                        logger.info(f"[产品路径][Kling双锚定] 镜头 {s_num} 首帧=上一尾帧、尾帧=关键帧")
                    shot_last_frame = s_keyframe
                elif s_keyframe and settings.ENABLE_KEYFRAME_STAGE:
                    # 最高优先级（非 Kling）：使用关键帧图片作为首帧
                    shot_reference_frame = s_keyframe
                    shot_last_frame = None
                    logger.info(f"[产品路径] 镜头 {s_num} 使用关键帧图片作为首帧 (keyframe优先)")
                elif pi == 0:
                    # 第一个产品镜头：使用三视图作为首帧
                    shot_reference_frame = three_view_url
                    shot_last_frame = None
                    if s_keyframe:
                        logger.info(f"[产品路径] 镜头 {s_num} 存在关键帧但 ENABLE_KEYFRAME_STAGE=False，退回三视图")
                    else:
                        logger.info(f"[产品路径] 镜头 {s_num} 无关键帧，使用三视图作为首帧参考（首个产品镜头）")
                else:
                    # 后续产品镜头：使用前一镜头尾帧
                    shot_reference_frame = prod_current_ref
                    shot_last_frame = None
                    if s_keyframe:
                        logger.info(f"[产品路径] 镜头 {s_num} 存在关键帧但 ENABLE_KEYFRAME_STAGE=False，退回尾帧衔接")
                    else:
                        logger.info(f"[产品路径] 镜头 {s_num} 无关键帧，使用上一产品镜头尾帧作为首帧参考")
                
                try:
                    # 生成视频（Kling 主 + wan 兜底）
                    try:
                        task_id = await video_gen.generate_video(
                            model=model,
                            prompt=s_prompt,
                            first_frame_url=shot_reference_frame,
                            last_frame_url=shot_last_frame,
                            negative_prompt=s_neg,
                            duration=s_dur,
                        )
                        
                        logger.info(f"[产品路径] 镜头 {s_num} 任务已提交，task_id: {task_id}")
                        await airtable.update_shot_status(shot_id=s_id, status="generating")
                        
                        video_result = await video_gen.wait_for_completion(
                            task_id=task_id, poll_interval=10, max_wait=600,
                        )
                        video_url = video_result["video_url"]
                        api_last_frame_url = video_result.get("last_frame_image_url")
                    except Exception as kling_err:
                        _wan_key = settings.WAN_API_KEY or settings.QWEN_API_KEY
                        if (platform == "kling" and settings.ENABLE_WAN_FALLBACK
                                and _wan_key and shot_reference_frame):
                            logger.warning(
                                f"[产品路径] 镜头 {s_num} Kling 生成失败: {kling_err}，切换 wan2.7 兜底"
                            )
                            wan = WanService()
                            wan_result = await wan.generate_and_wait(
                                prompt=s_prompt,
                                first_frame_url=shot_reference_frame,
                                last_frame_url=shot_last_frame,
                                negative_prompt=s_neg,
                                duration=s_dur,
                            )
                            video_url = wan_result["video_url"]
                            api_last_frame_url = None
                            logger.info(f"[产品路径] 镜头 {s_num} wan 兜底成功")
                            await airtable.update_shot_status(shot_id=s_id, status="generating")
                        else:
                            raise
                    
                    logger.info(f"[产品路径] 镜头 {s_num} 生成完成: {video_url}")
                    if api_last_frame_url:
                        logger.info(f"[产品路径] 镜头 {s_num} API 直接返回尾帧: {api_last_frame_url[:60]}...")
                    
                    # 上传到 OSS
                    oss_key = f"videos/{project_id}/shot_{s_num}.mp4"
                    oss_video_url = await _download_and_upload_to_oss(
                        video_url=video_url, oss_service=oss_service, oss_key=oss_key,
                    )
                    logger.info(f"[产品路径] 镜头 {s_num} 视频已上传到 OSS")
                    
                    # 注：第一个产品镜头使用了三视图首帧，需要在 Stage 5 裁剪
                    # 裁剪标记通过 Stage 5 中的规则推断（第一个非 Hook 镜头），不再写入 Airtable
                    if pi == 0:
                        logger.info(f"[产品路径] 镜头 {s_num} 为首个产品镜头（三视图首帧），Stage 5 将自动裁剪")
                    
                    # 提取尾帧作为下一个产品镜头的参考
                    if pi < len(product_shots_list) - 1:
                        try:
                            # 优先使用 API 直接返回的尾帧图片 URL（Seedance 直连支持）
                            if api_last_frame_url:
                                prod_current_ref = api_last_frame_url
                                prod_last_successful = api_last_frame_url
                                logger.info(f"[产品路径] 镜头 {s_num} 直接使用 API 返回的尾帧图片（无需视频提取）")
                            else:
                                # 回退：从视频中提取最后一帧
                                logger.info(f"[产品路径] 提取镜头 {s_num} 的最后一帧（API 未返回尾帧）")
                                last_frame_path = await video_gen.extract_last_frame(video_url)
                                oss_frame_key = f"frames/{project_id}/shot_{s_num}_last_frame.png"
                                oss_frame_url = await oss_service.upload_file(
                                    local_path=last_frame_path,
                                    oss_key=oss_frame_key,
                                    content_type="image/png",
                                    expires=86400,
                                )
                                prod_current_ref = oss_frame_url
                                prod_last_successful = oss_frame_url
                                logger.info(f"[产品路径] 镜头 {s_num} 尾帧已提取并上传")
                                
                                if os.path.exists(last_frame_path):
                                    os.remove(last_frame_path)
                        except Exception as frame_error:
                            logger.warning(f"[产品路径] 镜头 {s_num} 尾帧提取失败: {frame_error}，使用上一个成功帧")
                            prod_current_ref = prod_last_successful
                    
                    # 更新 Airtable 状态
                    await airtable.update_shot_status(shot_id=s_id, status="completed", video_url=oss_video_url)
                    await airtable.update_shot_video_status(shot_id=s_id, status="待审核")

                    # ---- 模型审查点 4.4：生成视频抽帧审查（产品镜头） ----
                    try:
                        await _audit_generated_video(
                            project_id=project_id,
                            shot_id=s_id,
                            shot_number=s_num,
                            shot_prompt=s_prompt,
                            oss_video_url=oss_video_url,
                            first_frame_url=shot_reference_frame,
                            oss_service=oss_service,
                            airtable=airtable,
                        )
                    except Exception as audit_err:
                        logger.warning(
                            f"[产品路径] 镜头 {s_num} 审查流程异常（不阻断）: {audit_err}"
                        )

                    product_results.append({
                        "shot_id": s_id,
                        "shot_number": s_num,
                        "status": "completed",
                        "video_url": video_url,
                    })
                    
                except Exception as e:
                    logger.error(f"[产品路径] 镜头 {s_num} 生成失败: {e!r} ({type(e).__name__})", exc_info=True)
                    prod_current_ref = prod_last_successful
                    
                    try:
                        await airtable.update_shot_status(shot_id=s_id, status="failed")
                        await airtable.create_review(
                            shot_id=s_id, review_type="视频审核",
                            result="需修改", description=f"生成失败: {str(e)}",
                        )
                    except Exception:
                        pass
                    
                    product_results.append({
                        "shot_id": s_id,
                        "shot_number": s_num,
                        "status": "failed",
                        "error": str(e),
                    })
            
            return product_results
        
        # ===== 并发执行两条路径 =====
        hook_results_list, product_results_list = await asyncio.gather(
            generate_hook_shots(),
            generate_product_shots(),
        )
        
        # 合并结果
        all_gen_results = (hook_results_list or []) + (product_results_list or [])
        results.extend(all_gen_results)
        
        # 统一统计成功/失败计数（避免预处理阶段与此处重复累加）
        successful_count = sum(1 for r in results if r.get("status") == "completed")
        failed_count = sum(1 for r in results if r.get("status") != "completed")
        
        # 4. 更新项目状态
        overall_status = "completed"
        if failed_count > 0:
            if successful_count == 0:
                overall_status = "failed"
            else:
                overall_status = "partial"
        
        try:
            from models.schemas import ProjectStatus
            status_value = ProjectStatus.REVIEWING.value if overall_status != "failed" else ProjectStatus.FAILED.value
            await airtable.update_project_status(
                project_id=project_id,
                status=status_value
            )
            await airtable.update_project(
                project_id=project_id,
                data={"generation_status": overall_status}
            )
        except Exception as e:
            logger.error(f"更新项目状态失败: {e}")
        
        logger.info(
            f"阶段四完成，项目: {project_id}，"
            f"成功: {successful_count}，失败: {failed_count}"
        )
        
        return {
            "project_id": project_id,
            "total_shots": len(approved_shots),
            "successful_shots": successful_count,
            "failed_shots": failed_count,
            "shots": results,
            "status": overall_status,
        }
        
    except Exception as e:
        logger.error(f"阶段四执行失败: {e}")
        raise


async def generate_single_shot(
    shot: dict[str, Any],
    video_gen: VideoGenService,
    airtable: AirtableService,
    platform: VideoModel,
    reference_frame: Optional[str] = None,
) -> dict[str, Any]:
    """
    生成单个分镜的视频
    
    Args:
        shot: 分镜数据（包含生成提示词）
        video_gen: 视频生成服务实例
        airtable: Airtable 服务实例
        platform: 视频生成平台
        reference_frame: 参考帧 URL（可选）
        
    Returns:
        生成结果
        {
            "success": True/False,
            "video_url": "..." or None,
            "error": "..." or None,
            "last_frame_url": "..." or None,
        }
    """
    shot_id = shot.get("id")
    shot_fields = shot.get("fields", {})
    raw_prompt = shot_fields.get("生成提示词", "")
    prompt = _convert_prompt_to_string(raw_prompt)
    
    if not prompt:
        return {
            "success": False,
            "video_url": None,
            "error": "没有生成提示词",
            "last_frame_url": None,
        }
    
    try:
        # 获取镜头时长
        shot_number = shot_fields.get("镜头序号", 0)
        parsed_duration = _get_shot_duration(shot_fields, shot_number)
        # 可灵最小时长是 5 秒，Seedance 最小 8 秒（在 video_gen_service.py 中已处理）
        duration = max(5, parsed_duration)
        logger.info(f"镜头 {shot_number} 原始时长: {shot_fields.get('metadata', {}).get('duration') if isinstance(shot_fields.get('metadata'), dict) else shot_fields.get('metadata')}, 实际生成时长: {duration}s")
        
        # 提交生成任务
        task_id = await video_gen.generate_video(
            model=platform,
            prompt=prompt,
            first_frame_url=reference_frame,
            duration=duration,
        )
        
        # 更新状态为生成中
        await airtable.update_shot_status(
            shot_id=shot_id,
            status="generating",
        )
        
        # 等待完成
        completion_result = await video_gen.wait_for_completion(
            task_id=task_id,
        )
        video_url = completion_result["video_url"]
        api_last_frame_url = completion_result.get("last_frame_image_url")
        
        # 更新状态为完成
        await airtable.update_shot_status(
            shot_id=shot_id,
            status="completed",
            video_url=video_url,
        )
        
        # 提取最后一帧（优先使用 API 返回的 last_frame_image_url）
        if api_last_frame_url:
            last_frame_url = api_last_frame_url
        else:
            last_frame_path = await video_gen.extract_last_frame(video_url)
            last_frame_url = await video_gen.upload_frame(last_frame_path)
        
        return {
            "success": True,
            "video_url": video_url,
            "error": None,
            "last_frame_url": last_frame_url,
        }
        
    except Exception as e:
        logger.error(f"生成单个分镜失败: {e}")
        
        # 更新状态为失败
        try:
            await airtable.update_shot_status(
                shot_id=shot_id,
                status="failed",
            )
        except Exception:
            pass
        
        return {
            "success": False,
            "video_url": None,
            "error": str(e),
            "last_frame_url": None,
        }


async def poll_generation_status(
    task_id: str,
    video_gen: VideoGenService,
    platform: VideoModel,
    timeout: int = 600,
    poll_interval: int = 10,
) -> dict[str, Any]:
    """
    轮询视频生成任务状态
    
    Args:
        task_id: 任务 ID
        video_gen: 视频生成服务实例
        platform: 视频生成平台
        timeout: 超时时间（秒）
        poll_interval: 轮询间隔（秒）
        
    Returns:
        任务状态和结果
        {
            "status": "completed|failed|timeout",
            "video_url": "..." or None,
            "error": "..." or None,
        }
    """
    try:
        completion_result = await video_gen.wait_for_completion(
            task_id=task_id,
            poll_interval=poll_interval,
            max_wait=timeout,
        )
        
        return {
            "status": "completed",
            "video_url": completion_result["video_url"],
            "last_frame_image_url": completion_result.get("last_frame_image_url"),
            "error": None,
        }
        
    except TimeoutError:
        return {
            "status": "timeout",
            "video_url": None,
            "error": f"等待超时（{timeout}秒）",
        }
        
    except Exception as e:
        return {
            "status": "failed",
            "video_url": None,
            "error": str(e),
        }


# 兼容旧接口的别名
stage4_generation = run_stage4
stage4_generation = run_stage4
