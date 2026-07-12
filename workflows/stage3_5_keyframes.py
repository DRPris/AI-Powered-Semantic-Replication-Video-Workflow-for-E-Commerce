"""
阶段 3.5：关键帧图片预生成

在提示词生成（Stage 3）之后、视频生成（Stage 4）之前，
为每个分镜生成关键帧参考图，供 Stage 4 作为首帧输入。

流程：
1. 检查 ENABLE_KEYFRAME_STAGE 配置开关
2. 从 Airtable 获取所有分镜（按镜头序号排序）
3. 获取产品参考图（真实照片 > 三视图 > 主图）+ 产品分析摘要
4. 逐镜头生成关键帧图片（顺序执行）：
   - 锚定模式（首帧/场景切换/周期性重锚定）：仅产品参考图，产品最保真
   - 续帧模式：产品参考图在前 + 前帧殿后，兼顾场景连贯
   - 级联审查（L1 Qwen-VL → L2 Gemini），驳回自动重生成（MAX_KEYFRAME_ATTEMPTS）
5. 每张图经 9:16 标准化 → OSS → Airtable
6. 更新项目状态为 KEYFRAME_REVIEW
"""

import ast
import json
import logging
import os
import re
import tempfile
from typing import Any, Optional

import httpx

from config import settings
from models.schemas import ProjectStatus
from prompts.keyframe_generation import (
    build_continuation_shot_prompt,
    build_first_shot_prompt,
)
from services.airtable_service import AirtableService
from services.audit_service import AuditService, AuditFailedException
from services.image_gen_service import ImageGenService
from services.image_utils import standardize_image_to_9_16
from services.oss_service import OSSService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------

def _parse_generation_prompt(raw_prompt: Any) -> dict:
    """
    将分镜记录中的 "生成提示词" 字段解析为 dict。

    支持 JSON 字符串、Python repr 字符串、原始 dict。
    解析失败时返回空 dict。
    """
    if not raw_prompt:
        return {}
    if isinstance(raw_prompt, dict):
        return raw_prompt
    if isinstance(raw_prompt, str):
        # JSON
        try:
            parsed = json.loads(raw_prompt)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        # Python repr（str(dict)）
        try:
            parsed = ast.literal_eval(raw_prompt)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return {}


def _extract_product_summary(product_analysis_content: str) -> str:
    """
    从产品分析内容中提取 layer1 + layer2 摘要。

    产品分析结果通常是 JSON 字符串，包含 layer1（基础属性）和 layer2（细节分析）。
    如果解析失败，直接返回原文（截断至 500 字符以控制 prompt 长度）。
    """
    if not product_analysis_content:
        return ""

    try:
        data = json.loads(product_analysis_content)
        parts = []
        if isinstance(data, dict):
            layer1 = data.get("layer1") or data.get("basic") or ""
            layer2 = data.get("layer2") or data.get("detail") or ""
            if layer1:
                parts.append(str(layer1) if not isinstance(layer1, str) else layer1)
            if layer2:
                parts.append(str(layer2) if not isinstance(layer2, str) else layer2)
        if parts:
            return "\n".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback：直接使用原文（截断）
    return product_analysis_content[:500]


def _extract_composition_details(product_analysis_content: str) -> str:
    """
    从产品分析内容中提取组件分解信息（layer_0_component_decomposition）。

    如果产品是组合产品（is_combo_product=True），返回格式化的组件信息字符串。
    如果不是组合产品或解析失败，返回空字符串。
    """
    if not product_analysis_content:
        return ""

    try:
        data = json.loads(product_analysis_content)
        if not isinstance(data, dict):
            return ""

        layer0 = data.get("layer_0_component_decomposition") or data.get("layer0") or {}
        if not isinstance(layer0, dict):
            return ""

        if not layer0.get("is_combo_product"):
            return ""

        components = layer0.get("components", [])
        relationships = layer0.get("relationships", {})

        if not components:
            return ""

        lines = []
        for comp in components:
            if not isinstance(comp, dict):
                continue
            name = comp.get("component_name", "unknown")
            lines.append(f"- {name}:")
            for key in ["shape", "color", "material", "texture", "role", "belongs_to"]:
                val = comp.get(key)
                if val:
                    lines.append(f"    {key}: {val}")

        if relationships and isinstance(relationships, dict):
            lines.append("\nComponent Relationships:")
            for key, val in relationships.items():
                if val:
                    lines.append(f"  - {key}: {val}")

        return "\n".join(lines) if lines else ""

    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


def _extract_product_keywords(product_analysis_content: str) -> list:
    """
    从产品分析内容中提取产品关键词列表，用于场景切换检测。

    提取来源：组件名称、产品整体形状描述中的关键词。
    返回小写关键词列表。
    """
    if not product_analysis_content:
        return []

    keywords = set()
    try:
        data = json.loads(product_analysis_content)
        if not isinstance(data, dict):
            return []

        # 从 layer_0 提取组件名称
        layer0 = data.get("layer_0_component_decomposition") or data.get("layer0") or {}
        if isinstance(layer0, dict):
            for comp in layer0.get("components", []):
                if isinstance(comp, dict):
                    name = comp.get("component_name", "")
                    if name:
                        # 拆分多词组件名（如 "bar soap" → ["bar", "soap"]）
                        for word in name.lower().split():
                            if len(word) > 2:  # 跳过 "a", "of" 等短词
                                keywords.add(word)

        # 从 layer_1 提取整体形状关键词
        layer1 = data.get("layer_1_physical_attributes") or {}
        if isinstance(layer1, dict):
            shape = layer1.get("overall_shape", "")
            if shape:
                for word in shape.lower().split():
                    if len(word) > 3:
                        keywords.add(word)

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return list(keywords)


def _is_scene_transition(
    prev_shot_desc: str,
    curr_shot_desc: str,
    product_keywords: list,
) -> bool:
    """
    检测两个镜头之间是否发生了场景切换。

    判断逻辑：
    1. 如果当前镜头描述不包含任何产品关键词 → 认为是场景切换（环境/过渡镜头）
    2. 如果前一镜头不包含产品关键词但当前镜头包含 → 也是场景切换
    3. 如果两者的场景类型明显不同（产品特写 vs 使用场景等） → 场景切换

    Args:
        prev_shot_desc: 前一镜头的场景描述
        curr_shot_desc: 当前镜头的场景描述
        product_keywords: 产品关键词列表

    Returns:
        True 表示发生了场景切换，当前镜头不应使用前帧作为参考
    """
    if not product_keywords:
        return False  # 没有关键词时无法判断，保持原行为

    curr_lower = curr_shot_desc.lower()
    prev_lower = prev_shot_desc.lower()

    # 当前镜头是否包含产品关键词
    curr_has_product = any(kw in curr_lower for kw in product_keywords)
    prev_has_product = any(kw in prev_lower for kw in product_keywords)

    # 情况 1：当前镜头不涉及产品 → 环境/过渡镜头，不应继承前帧的产品元素
    if not curr_has_product:
        return True

    # 情况 2：前一镜头不涉及产品，当前镜头涉及 → 从环境切回产品
    if not prev_has_product and curr_has_product:
        return True

    # 情况 3：场景环境关键词差异检测
    # 如果一个是水相关场景，另一个不是，可能是不同场景
    scene_indicators = [
        (["water", "faucet", "sink", "shower", "bathroom", "tap", "running water"], "water_scene"),
        (["table", "desk", "counter", "surface", "flat", "display"], "table_scene"),
        (["hand", "palm", "finger", "grip", "hold"], "hand_scene"),
    ]
    prev_scenes = set()
    curr_scenes = set()
    for indicators, scene_type in scene_indicators:
        if any(ind in prev_lower for ind in indicators):
            prev_scenes.add(scene_type)
        if any(ind in curr_lower for ind in indicators):
            curr_scenes.add(scene_type)

    # 如果场景类型完全不同且都有明确场景标记
    if prev_scenes and curr_scenes and not prev_scenes.intersection(curr_scenes):
        return True

    return False


async def _download_image(url: str) -> bytes:
    """从 URL 下载图片并返回字节数据。"""
    async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(60.0)) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# 安全措辞替换映射表：将可能触发 OpenAI 内容安全过滤的个人护理/清洁场景措辞
# 替换为语义等价但更中性的表述。按长度降序排列以优先匹配更长的短语。
_SAFETY_REPLACEMENTS = [
    # 淋浴相关（长短语优先）
    ("under the shower", "under running water"),
    ("shower stream", "running water"),
    ("shower background", "bathroom background"),
    ("shower", "running water"),
    # 皮肤接触相关（长短语优先）
    ("pressed against the skin of a human arm", "gliding across the arm surface"),
    ("pressed against the skin", "applied to the surface"),
    ("transferred to the skin", "applied to the surface"),
    ("Skin should appear wet and clean", "Surface should appear freshly cleansed"),
    ("skin should appear wet", "surface should appear clean"),
    ("appear wet and clean", "appear freshly cleansed"),
    ("against the skin", "on the surface"),
    ("skin of a human", "surface of the"),
    ("human arm skin", "forearm surface"),
    ("of a human arm", "of the arm"),
    ("across the forearm", "along the arm"),
    ("rubbing palm-to-palm", "lathering between hands"),
    ("palm-to-palm", "between hands"),
    ("wet and clean", "freshly cleansed"),
    ("human skin", "surface"),
    ("human arm", "arm area"),
    ("bare skin", "surface area"),
    ("the skin", "the surface"),
    ("on the skin", "on the surface"),
    # 身体动作（长短语优先）
    ("movement from arm to leg", "movement along different areas"),
    ("from arm to leg", "across different areas"),
    ("arm to leg", "different body areas"),
    # 潮湿/接触相关
    ("water-soaked", "dampened"),
    ("wet palms", "damp hands"),
    ("wet hands", "damp hands"),
    # 身体部位——上肢
    ("forearm", "arm area"),
    ("arm surface", "product application area"),
    ("across the arm", "along the area"),
    # 身体部位——下肢（避免 OpenAI 安全策略误判为不当接触）
    ("Human lower leg (shin)", "lower limb application area"),
    ("human lower leg (shin)", "lower limb application area"),
    ("Human lower leg", "lower limb application area"),
    ("human lower leg", "lower limb application area"),
    ("the lower leg (shin area)", "the lower limb surface"),
    ("lower leg (shin area)", "lower limb surface"),
    ("lower leg (shin)", "lower limb surface"),
    ("lower leg", "lower limb surface"),
    ("shin area", "lower limb surface"),
    ("the shin", "the lower limb surface"),
    ("shin", "lower limb surface"),
    ("thigh", "upper limb surface"),
    ("calf", "lower limb surface"),
    ("knee", "limb joint area"),
    ("ankle", "limb end area"),
    # 接触/推拿动作的中性化（仅在上述身体部位上下文中更安全）
    ("touching the lower limb surface", "moving along the lower limb surface"),
    ("touching the limb", "moving along the limb"),
    ("scrub the lower limb surface", "glide along the lower limb surface"),
    ("scrubs the lower limb surface", "glides along the lower limb surface"),
    ("scrub up and down the", "move up and down along the"),
    ("scrubbing motion", "gliding motion"),
]


def _sanitize_scene_description(text: str) -> str:
    """
    替换可能触发内容安全过滤的措辞，同时保持语义不变。

    针对个人护理/清洁产品的正常使用场景（淋浴、皮肤接触等），
    将可能被 OpenAI 安全系统误判的词汇替换为更中性的等价表述。
    """
    if not text:
        return text
    result = text
    for old, new in _SAFETY_REPLACEMENTS:
        result = re.sub(re.escape(old), new, result, flags=re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def run_stage3_5(project_id: str) -> dict:
    """
    Stage 3.5：关键帧图片预生成

    Args:
        project_id: 项目 ID

    Returns:
        生成结果摘要 dict
    """
    logger.info(f"Starting Stage 3.5: Keyframe Generation for project {project_id}")

    # ---- 0. 开关检查 ----
    if not settings.ENABLE_KEYFRAME_STAGE:
        logger.info(f"Stage 3.5 disabled by ENABLE_KEYFRAME_STAGE, skipping for project {project_id}")
        return {
            "success": True,
            "project_id": project_id,
            "skipped": True,
            "message": "Keyframe stage disabled by configuration",
        }

    # ---- 初始化服务 ----
    image_gen = ImageGenService(api_key=settings.KIE_API_KEY)
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
        # ---- 1. 更新项目状态 ----
        await airtable.update_project_status(
            project_id=project_id,
            status=ProjectStatus.KEYFRAME_GENERATING,
        )
        logger.info(f"Project {project_id} status → KEYFRAME_GENERATING")

        # ---- 2. 获取所有分镜（已按镜头序号排序） ----
        shots = await airtable.get_project_shots(project_id)
        if not shots:
            logger.warning(f"Project {project_id} 没有分镜记录，跳过关键帧生成")
            # 无分镜时直接推进到 KEYFRAME_REVIEW，避免状态停滞在 KEYFRAME_GENERATING
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.KEYFRAME_REVIEW,
            )
            logger.info(f"Project {project_id} status → KEYFRAME_REVIEW (no shots)")
            return {
                "success": True,
                "project_id": project_id,
                "total_shots": 0,
                "successful": 0,
                "failed": 0,
                "skipped": True,
                "message": "No shots found, skipped to KEYFRAME_REVIEW",
            }

        logger.info(f"Project {project_id} 共 {len(shots)} 个分镜")

        # ---- 3. 获取产品参考图（真图优先） ----
        # 优先级：用户上传的多角度真实照片(product_image) > 三视图 > 商品分析素材附件。
        # 真实照片没有 AI 幻觉，是最可靠的产品形态锚点；
        # 三视图是 Gemini 从单图脑补生成的，侧面/顶面可能失真。
        assets = await airtable.get_project_assets(project_id)
        real_image_urls: list[str] = []      # 用户上传的产品真实照片
        three_view_url: Optional[str] = None
        product_fallback_url: Optional[str] = None  # 商品分析素材的附件（通常=主图）
        product_analysis_content: str = ""

        for asset in assets:
            af = asset.get("fields", {})
            asset_type = af.get("素材类型", "").lower()

            # 产品真实照片（可多张）
            if asset_type == "product_image":
                attachments = af.get("附件", [])
                if attachments:
                    url = attachments[0].get("url")
                    if url and url not in real_image_urls:
                        real_image_urls.append(url)

            # 三视图
            if not three_view_url and any(
                kw in asset_type for kw in ["三视图", "three_view", "three-view"]
            ):
                attachments = af.get("附件", [])
                if attachments:
                    three_view_url = attachments[0].get("url")
                    logger.info(f"Found three-view URL: {three_view_url[:60]}...")

            # 产品分析
            if not product_analysis_content and asset_type == "product":
                product_analysis_content = af.get("内容", "")
                attachments = af.get("附件", [])
                if attachments:
                    product_fallback_url = attachments[0].get("url")

        # 组装最终产品参考图列表（生成与审核共用同一套锚点）
        max_refs = max(1, int(getattr(settings, "MAX_PRODUCT_REF_IMAGES", 3)))
        if real_image_urls:
            product_ref_urls = real_image_urls[:max_refs]
            logger.info(
                f"Project {project_id} 使用 {len(product_ref_urls)} 张商品真实照片作为产品锚点"
            )
        elif three_view_url:
            product_ref_urls = [three_view_url]
            logger.info(f"Project {project_id} 无商品真实照片，使用三视图作为产品锚点")
        elif product_fallback_url:
            product_ref_urls = [product_fallback_url]
            logger.info(f"Project {project_id} 使用商品分析素材附件作为产品锚点（兜底）")
        else:
            product_ref_urls = []
            logger.warning(f"Project {project_id} 未找到任何产品参考图，关键帧将使用纯文生图模式")

        product_summary = _extract_product_summary(product_analysis_content)
        composition_details = _extract_composition_details(product_analysis_content)
        product_keywords = _extract_product_keywords(product_analysis_content)
        if product_summary:
            logger.info(f"Product analysis summary: {len(product_summary)} chars")
        else:
            logger.warning(f"Product analysis not found for project {project_id}")
        if composition_details:
            logger.info(f"Product composition details extracted: {len(composition_details)} chars")
        else:
            logger.info(f"Product is single-component or no composition data available")
        if product_keywords:
            logger.info(f"Product keywords for scene detection: {product_keywords}")

        # ---- 4. 逐镜头顺序生成关键帧 ----
        results = []
        successful_count = 0
        failed_count = 0
        prev_keyframe_url: Optional[str] = None  # 上一帧的关键帧 OSS URL
        prev_shot_desc: str = ""  # 上一镜头的场景描述，用于场景切换检测
        # 抗漂移状态：
        # frames_since_anchor —— 连续使用"前帧续帧模式"的次数，达到阈值后强制重锚定，
        #   打断"前一帧的轻微形变被下一帧继承并放大"的累积链（雪球效应）
        # scene_anchor_url —— 当前场景的锚定帧（锚定模式生成且过审的帧），
        #   审核时作为额外参考图，让模型直接对比"待审帧 vs 场景起点"的累积漂移
        frames_since_anchor: int = 0
        scene_anchor_url: Optional[str] = None
        reanchor_interval = int(getattr(settings, "KEYFRAME_REANCHOR_INTERVAL", 0))
        max_attempts = max(1, int(getattr(settings, "MAX_KEYFRAME_ATTEMPTS", 1)))

        for idx, shot in enumerate(shots):
            shot_id = shot.get("id")
            shot_fields = shot.get("fields", {})
            shot_number = shot_fields.get("镜头序号", idx + 1)

            # 幂等检查：job 重试时跳过已生成关键帧的镜头，避免重复生成和重复计费。
            # Airtable 后端该字段是附件数组，PostgreSQL 后端是 URL 字符串，两种都兼容。
            existing_keyframe = shot_fields.get("关键帧图片")
            if isinstance(existing_keyframe, list):
                existing_keyframe = (
                    (existing_keyframe[0] or {}).get("url", "")
                    if existing_keyframe else ""
                )
            if existing_keyframe:
                logger.info(
                    f"[Keyframe] 镜头 {shot_number} 已有关键帧，跳过生成（幂等重试）"
                )
                successful_count += 1
                # 维持续帧上下文，让后续镜头仍能以本帧为参考
                prev_keyframe_url = existing_keyframe
                if scene_anchor_url is None:
                    scene_anchor_url = existing_keyframe
                parsed_prompt = _parse_generation_prompt(shot_fields.get("生成提示词", ""))
                prev_shot_desc = _sanitize_scene_description(
                    parsed_prompt.get("first_frame", "")
                    or shot_fields.get("新镜头描述", "")
                )
                results.append({
                    "shot_id": shot_id,
                    "shot_number": shot_number,
                    "status": "skipped_existing",
                    "keyframe_url": existing_keyframe,
                })
                continue

            logger.info(f"[Keyframe] 开始生成镜头 {shot_number} 的关键帧 ({idx + 1}/{len(shots)})")

            try:
                # 解析生成提示词中的参数
                parsed_prompt = _parse_generation_prompt(shot_fields.get("生成提示词", ""))
                first_frame_desc = (
                    parsed_prompt.get("first_frame", "")
                    or shot_fields.get("新镜头描述", "")
                )
                camera_instruction = parsed_prompt.get("camera", "static")
                raw_constraints = parsed_prompt.get("constraints", [])
                if isinstance(raw_constraints, list):
                    hard_constraints = "; ".join(raw_constraints)
                else:
                    hard_constraints = str(raw_constraints) if raw_constraints else ""

                if not first_frame_desc:
                    raise ValueError(f"镜头 {shot_number} 缺少场景描述，无法生成关键帧")

                # 安全措辞替换：避免触发 OpenAI 内容安全过滤
                first_frame_desc = _sanitize_scene_description(first_frame_desc)
                hard_constraints = _sanitize_scene_description(hard_constraints)
                camera_instruction = _sanitize_scene_description(camera_instruction)

                # ---- 决定生成模式：锚定模式 vs 续帧模式 ----
                # 锚定模式：仅产品参考图（Shot 1 / 场景切换 / 周期性重锚定），产品最保真
                # 续帧模式：产品参考图在前 + 前帧殿后，兼顾产品保真与场景连贯
                anchor_mode = False
                anchor_reason = ""
                scene_hint = ""
                if idx == 0 or not prev_keyframe_url:
                    anchor_mode = True
                    anchor_reason = "首帧" if idx == 0 else "无可用前帧"
                else:
                    scene_changed = _is_scene_transition(
                        prev_shot_desc=prev_shot_desc,
                        curr_shot_desc=first_frame_desc,
                        product_keywords=product_keywords,
                    )
                    if scene_changed:
                        anchor_mode = True
                        anchor_reason = "场景切换"
                    elif reanchor_interval > 0 and frames_since_anchor >= reanchor_interval:
                        # 周期性重锚定：连续续帧达到阈值，强制回到仅产品参考图模式。
                        # 前帧图片会把已发生的轻微形变传给下一帧，文字描述则不会，
                        # 所以用上一镜头的文字描述（scene_hint）替代前帧图片来保持场景连贯。
                        anchor_mode = True
                        anchor_reason = f"周期性重锚定(连续续帧{frames_since_anchor}次)"
                        scene_hint = prev_shot_desc

                if anchor_mode:
                    prompt_text = build_first_shot_prompt(
                        first_frame_description=first_frame_desc,
                        camera_instruction=camera_instruction,
                        hard_constraints=hard_constraints,
                        product_analysis_summary=product_summary,
                        product_composition_details=composition_details,
                        product_ref_count=len(product_ref_urls),
                        previous_scene_hint=scene_hint,
                    )
                    input_urls = list(product_ref_urls) or None
                    logger.info(
                        f"[Keyframe] 镜头 {shot_number}: 锚定模式({anchor_reason}), "
                        f"产品参考图={len(product_ref_urls)}张"
                    )
                else:
                    # 续帧模式：产品参考图在前（形态唯一依据）、前帧最后（只管场景连贯）。
                    # 图生图模型对前排图片注意力更强，产品锚点前置可减轻
                    # "抄前帧里已变形产品"导致的逐帧漂移。
                    prompt_text = build_continuation_shot_prompt(
                        first_frame_description=first_frame_desc,
                        camera_instruction=camera_instruction,
                        hard_constraints=hard_constraints,
                        product_analysis_summary=product_summary,
                        product_composition_details=composition_details,
                        product_ref_count=len(product_ref_urls),
                    )
                    input_urls = list(product_ref_urls) + [prev_keyframe_url]
                    logger.info(
                        f"[Keyframe] 镜头 {shot_number}: 续帧模式, "
                        f"产品参考图={len(product_ref_urls)}张 + 前帧1张"
                    )

                # ---- 生成 + 审查（审查驳回自动重新生成，最多 max_attempts 次）----
                # 有参考图时使用配置的 image-to-image 模型；无参考图时传 None 让服务自动选 text-to-image
                model_override = (settings.KEYFRAME_IMAGE_MODEL or None) if input_urls else None
                audit_enabled = getattr(settings, "ENABLE_AUDIT_KEYFRAME", True)
                oss_url: Optional[str] = None
                audit_result = None

                for attempt in range(1, max_attempts + 1):
                    if attempt > 1:
                        logger.info(
                            f"[Keyframe] 镜头 {shot_number} 第 {attempt}/{max_attempts} 次生成"
                            f"（上次审查驳回，自动重试）"
                        )
                    result_urls = await image_gen.generate_and_wait(
                        prompt=prompt_text,
                        input_urls=input_urls,
                        model=model_override,
                        poll_interval=5.0,
                        timeout=300.0,
                    )

                    if not result_urls:
                        raise RuntimeError(f"镜头 {shot_number} 图片生成返回空结果")

                    generated_image_url = result_urls[0]
                    logger.info(f"[Keyframe] 镜头 {shot_number} 图片生成完成: {generated_image_url[:60]}...")

                    # 下载图片
                    image_bytes = await _download_image(generated_image_url)
                    logger.info(f"[Keyframe] 镜头 {shot_number} 图片已下载, 大小: {len(image_bytes)} bytes")

                    # 9:16 标准化
                    standardized_bytes = standardize_image_to_9_16(image_bytes)
                    logger.info(f"[Keyframe] 镜头 {shot_number} 已标准化为 9:16, 大小: {len(standardized_bytes)} bytes")

                    # 保存到临时文件
                    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "tmp")
                    os.makedirs(tmp_dir, exist_ok=True)
                    tmp_path = os.path.join(tmp_dir, f"keyframe_{project_id}_shot{shot_number}_{os.urandom(4).hex()}.png")

                    try:
                        with open(tmp_path, "wb") as f:
                            f.write(standardized_bytes)

                        # 上传到 OSS（重试时覆盖同一 key，始终保留最新一版）
                        oss_key = f"keyframes/{project_id}/shot_{shot_number}.png"
                        oss_url = await oss_service.upload_file(
                            local_path=tmp_path,
                            oss_key=oss_key,
                            content_type="image/png",
                            expires=86400 * 7,  # 7 天有效期
                        )
                        logger.info(f"[Keyframe] 镜头 {shot_number} 已上传 OSS: {oss_key}")
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

                    # 更新 Airtable
                    await airtable.update_shot_keyframe(
                        shot_id=shot_id,
                        keyframe_image_url=oss_url,
                    )
                    logger.info(f"[Keyframe] 镜头 {shot_number} Airtable 关键帧字段已更新")

                    # ---- 模型审查点 3.5：关键帧与参考图一致性审查（级联 L1→L2）----
                    if not audit_enabled:
                        audit_result = None
                        break

                    try:
                        audit_svc = AuditService()
                        audit_svc.set_context(
                            project_id=project_id,
                            stage=f"3.5/shot_{shot_number}",
                        )
                        # 审核基准与生成锚点共用同一套产品参考图（真图优先）。
                        # 续帧模式额外附上场景锚定帧：让审核模型直接对比
                        # "待审帧 vs 场景起点帧"，识别逐帧独立对比看不出的累积漂移
                        reference_urls: list[str] = list(product_ref_urls)
                        if (
                            not anchor_mode
                            and scene_anchor_url
                            and scene_anchor_url not in reference_urls
                        ):
                            reference_urls.append(scene_anchor_url)
                        audit_result = await audit_svc.audit_keyframe_cascade(
                            project_id=project_id,
                            shot_number=shot_number,
                            first_frame_description=first_frame_desc,
                            keyframe_url=oss_url,
                            reference_image_urls=reference_urls,
                        )
                        audit_status = "已驳回" if audit_result.should_block else "已通过"
                        try:
                            await airtable.save_keyframe_audit_result(
                                shot_id=shot_id,
                                status=audit_status,
                                review_comment=audit_result.to_review_comment(),
                                attempt=attempt,
                            )
                        except Exception as write_err:
                            logger.warning(
                                f"[Keyframe] 镜头 {shot_number} 审查状态写回失败: {write_err}"
                            )
                        if not audit_result.should_block:
                            logger.info(
                                f"[Keyframe] 镜头 {shot_number} 模型审查通过: "
                                f"confidence={audit_result.confidence:.2f} (attempt {attempt})"
                            )
                            break
                        logger.warning(
                            f"[Keyframe] 镜头 {shot_number} 模型审查未通过 "
                            f"(attempt {attempt}/{max_attempts}): "
                            f"confidence={audit_result.confidence:.2f}, "
                            f"issues={audit_result.critical_issues}"
                        )
                        # 未达重试上限则回到循环顶部重新生成
                    except AuditFailedException:
                        raise
                    except Exception as audit_err:
                        # 审查流程自身异常不阻断主流程，留给下游人审后处理
                        logger.warning(
                            f"[Keyframe] 镜头 {shot_number} 审查过程异常（不阻断）: {audit_err}"
                        )
                        audit_result = None
                        break

                # 重试耗尽仍被驳回：标记 audit_rejected，转人审
                if audit_result is not None and audit_result.should_block:
                    failed_count += 1
                    # 不更新 prev_keyframe_url / scene_anchor_url，避免污染后续参考
                    results.append({
                        "shot_id": shot_id,
                        "shot_number": shot_number,
                        "status": "audit_rejected",
                        "keyframe_url": oss_url,
                        "attempts": max_attempts,
                        "audit_result": audit_result.model_dump(),
                    })
                    continue

                # 记录当前帧作为后续帧的参考，并维护抗漂移状态
                prev_keyframe_url = oss_url
                prev_shot_desc = first_frame_desc
                if anchor_mode:
                    scene_anchor_url = oss_url
                    frames_since_anchor = 0
                else:
                    frames_since_anchor += 1
                successful_count += 1
                results.append({
                    "shot_id": shot_id,
                    "shot_number": shot_number,
                    "status": "completed",
                    "keyframe_url": oss_url,
                })

            except Exception as e:
                failed_count += 1
                error_msg = str(e)
                logger.error(f"[Keyframe] 镜头 {shot_number} 关键帧生成失败: {error_msg}")

                # 单个镜头失败不中断整体流程
                # prev_keyframe_url 保持不变（使用上一个成功的帧）
                results.append({
                    "shot_id": shot_id,
                    "shot_number": shot_number,
                    "status": "failed",
                    "error": error_msg,
                })
                continue

        # ---- 5. 更新项目状态 ----
        if successful_count > 0:
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.KEYFRAME_REVIEW,
            )
            logger.info(f"Project {project_id} status → KEYFRAME_REVIEW")
        else:
            # 全部失败则标记为 FAILED
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.FAILED,
            )
            logger.error(f"Project {project_id} 所有关键帧生成失败，status → FAILED")

        logger.info(
            f"Stage 3.5 完成: project={project_id}, "
            f"total={len(shots)}, success={successful_count}, failed={failed_count}"
        )

        return {
            "success": successful_count > 0,
            "project_id": project_id,
            "total_shots": len(shots),
            "successful": successful_count,
            "failed": failed_count,
            "shots": results,
            "status": "completed" if failed_count == 0 else ("partial" if successful_count > 0 else "failed"),
        }

    except Exception as e:
        logger.error(f"Stage 3.5 failed for project {project_id}: {e}")
        try:
            await airtable.update_project_status(
                project_id=project_id,
                status=ProjectStatus.FAILED,
            )
        except Exception:
            pass
        raise


# 向后兼容别名
stage3_5_keyframes = run_stage3_5
