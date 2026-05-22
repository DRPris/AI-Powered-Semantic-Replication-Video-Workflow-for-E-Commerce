"""
OST 本地化服务 —— 把原视频的屏幕文字改写为适合新商品的文案

调用 Gemini Flash（快模型即可，因 OST 本地化为简单分类+改写任务），
带缓存（hash = 原 OST 内容 + 商品关键信息），失败时 fallback 到原文。

典型用法（Stage 5 OST 叠加前置）:
    localized_map = await localize_osts(
        gemini_service=gemini,
        product_brief=brief_dict,
        video_analysis=video_analysis,
    )
    # 把本地化结果写回 video_analysis 的 on_screen_text.content
    apply_localization_inplace(video_analysis, localized_map)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from config import settings
from prompts.ost_localization import build_ost_localization_prompt
from services.token_utils import get_cache_key, get_cache, set_cache

logger = logging.getLogger(__name__)


_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_original_osts(video_analysis: dict | list) -> list[dict]:
    """
    从 video_analysis 里抽取原始 OST 列表（只保留有 content 的镜头）

    Args:
        video_analysis: Stage 1 视频分析数据，格式 {"shots": [...]} 或直接是 shots 列表

    Returns:
        [
            {"shot_id": 1, "content": "Light Fury 🤍", "position": "Top center", "timing": "00:16→00:20"},
            ...
        ]
    """
    if isinstance(video_analysis, dict):
        shots = video_analysis.get("shots") or []
    elif isinstance(video_analysis, list):
        shots = video_analysis
    else:
        return []

    original = []
    for idx, shot in enumerate(shots):
        ost = (shot or {}).get("on_screen_text") or {}
        content = (ost.get("content") or "").strip()
        if not content:
            continue
        original.append({
            "shot_id": shot.get("shot_id") or idx + 1,
            "content": content,
            "position": ost.get("position") or "",
            "timing": ost.get("timing") or "",
        })
    return original


def _build_cache_key(product_brief: dict, original_osts: list[dict]) -> str:
    """基于商品核心字段 + 原 OST 列表生成缓存 key"""
    key_sps = product_brief.get("key_selling_points") or []
    payload = {
        "product_name": product_brief.get("product_name") or "",
        "brand": product_brief.get("brand") or "",
        "key_selling_points": list(key_sps)[:5],
        "tone": product_brief.get("tone") or "",
        "osts": [{"sid": o["shot_id"], "c": o["content"]} for o in original_osts],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return get_cache_key(serialized, prefix="ost_localization")


def _parse_localization_response(raw_text: str) -> Optional[list[dict]]:
    """
    解析 Gemini 返回的 JSON 数组（容忍 markdown 代码块包裹）

    Returns:
        list[dict] 成功解析, None 失败
    """
    if not raw_text:
        return None

    text = raw_text.strip()

    # 若被 markdown 代码块包裹，先剥离
    match = _JSON_BLOCK_PATTERN.search(text)
    if match:
        text = match.group(1).strip()

    # 直接尝试解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "osts" in data:
            return data["osts"] if isinstance(data["osts"], list) else None
    except json.JSONDecodeError:
        pass

    # 尝试从文本中定位第一个 [ 到最后一个 ] 截取
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    return None


async def localize_osts(
    gemini_service,
    product_brief: dict,
    video_analysis: dict | list,
    *,
    manual_overrides: dict[int, str] | None = None,
    use_cache: bool = True,
) -> dict[int, dict]:
    """
    对 video_analysis 里的原 OST 做本地化，返回 {shot_id: localized_entry} 映射

    优先级：manual_overrides > 缓存 > Gemini

    Args:
        gemini_service: GeminiService 实例（需提供 _generate_content 方法）
        product_brief: ProductBrief JSON 字典
        video_analysis: Stage 1 视频分析数据
        manual_overrides: 用户手动指定的本地化版本 {shot_id: localized_text}，
            存在的镜头将跳过 Gemini 调用
        use_cache: 是否使用缓存

    Returns:
        {shot_id: {"shot_id", "original_content", "category", "localized_content", "action", "rewrite_reason"}}
        失败时返回空字典（调用方应 fallback 到原 OST）
    """
    original_osts = _extract_original_osts(video_analysis)
    if not original_osts:
        logger.info("[ost_localizer] 无原始 OST，跳过本地化")
        return {}

    if not product_brief or not product_brief.get("product_name"):
        logger.warning("[ost_localizer] ProductBrief 缺失 product_name，跳过本地化")
        return {}

    manual_overrides = manual_overrides or {}

    # 先把用户手动覆盖的条目合入结果，不经过 Gemini
    localized_map: dict[int, dict] = {}
    pending_osts: list[dict] = []
    for o in original_osts:
        try:
            sid_int = int(o["shot_id"])
        except (TypeError, ValueError):
            pending_osts.append(o)
            continue
        manual = manual_overrides.get(sid_int)
        if isinstance(manual, str) and manual.strip():
            content = o["content"]
            localized_map[sid_int] = {
                "shot_id": sid_int,
                "original_content": content,
                "category": "manual",
                "localized_content": manual.strip(),
                "action": "rewrite" if manual.strip() != content else "keep",
                "rewrite_reason": "user_manual_override",
            }
        else:
            pending_osts.append(o)

    if not pending_osts:
        logger.info(
            f"[ost_localizer] 全部 {len(localized_map)} 条 OST 均有用户手动版本，跳过 Gemini 调用"
        )
        return localized_map

    if localized_map:
        logger.info(
            f"[ost_localizer] {len(localized_map)} 条使用用户手动版本，{len(pending_osts)} 条交给 Gemini 处理"
        )

    # 缓存检查（只针对未被手动覆盖的 OST）
    cache_key = _build_cache_key(product_brief, pending_osts)
    if use_cache:
        cached = get_cache(cache_key)
        if cached and isinstance(cached, dict) and "items" in cached:
            logger.info(f"[ost_localizer] 命中缓存 {cache_key}，{len(cached['items'])} 条")
            for item in cached["items"]:
                sid = item.get("shot_id")
                if sid is not None and sid not in localized_map:
                    localized_map[sid] = item
            return localized_map

    # 构造 prompt（只包含 pending）
    prompt = build_ost_localization_prompt(product_brief, pending_osts)

    # 调用 Gemini（用 flash 模型，OST 本地化为简单任务）
    model_name = getattr(settings, "OST_LOCALIZATION_MODEL", "gemini-2.0-flash")
    logger.info(
        f"[ost_localizer] 调用 Gemini ({model_name}) 本地化 {len(pending_osts)} 条 OST, "
        f"商品={product_brief.get('product_name')}"
    )

    try:
        response = await gemini_service._generate_content(
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            model=model_name,
            generation_config={"responseMimeType": "application/json"},
            context="ost_localization",
            max_retries=3,
        )
    except Exception as e:
        logger.warning(f"[ost_localizer] Gemini 调用失败（不阻塞主流程）: {e}")
        return localized_map  # 返回已有的手动覆盖

    # 提取文本
    try:
        text = response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        logger.warning(f"[ost_localizer] Gemini 响应结构异常: {e}")
        return localized_map

    parsed = _parse_localization_response(text)
    if not parsed:
        logger.warning(f"[ost_localizer] 无法解析 Gemini 输出为 JSON 数组: {text[:200]}")
        return localized_map

    # 规范化 + 对齐
    gemini_items: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sid = item.get("shot_id")
        if sid is None:
            continue
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        # 手动覆盖优先级更高，不覆盖
        if sid_int in localized_map:
            continue
        entry = {
            "shot_id": sid_int,
            "original_content": item.get("original_content") or "",
            "category": item.get("category") or "unknown",
            "localized_content": (item.get("localized_content") or "").strip(),
            "action": item.get("action") or "keep",
            "rewrite_reason": item.get("rewrite_reason") or "",
        }
        localized_map[sid_int] = entry
        gemini_items.append(entry)

    if not gemini_items and not localized_map:
        logger.warning("[ost_localizer] 解析后无有效 OST 映射")
        return {}

    # 写缓存（只存 Gemini 返回的部分，手动覆盖不存缓存）
    if use_cache and gemini_items:
        set_cache(cache_key, {"items": gemini_items})

    # 日志摘要
    action_counts: dict[str, int] = {}
    for item in localized_map.values():
        action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1
    logger.info(f"[ost_localizer] 本地化完成: {action_counts} (共 {len(localized_map)} 条)")

    return localized_map


def apply_localization_inplace(
    video_analysis: dict | list,
    localized_map: dict[int, dict],
) -> int:
    """
    把本地化结果原地写回 video_analysis 的 on_screen_text.content

    Args:
        video_analysis: Stage 1 视频分析数据（会被原地修改）
        localized_map: localize_osts() 返回值

    Returns:
        实际被修改的 OST 条数
    """
    if not localized_map:
        return 0

    if isinstance(video_analysis, dict):
        shots = video_analysis.get("shots") or []
    elif isinstance(video_analysis, list):
        shots = video_analysis
    else:
        return 0

    modified = 0
    for idx, shot in enumerate(shots):
        ost = (shot or {}).get("on_screen_text") or {}
        content = (ost.get("content") or "").strip()
        if not content:
            continue
        sid = shot.get("shot_id") or idx + 1
        localized = localized_map.get(sid)
        if not localized:
            continue

        action = localized.get("action", "keep")
        new_content = localized.get("localized_content", "")

        # 保留原文追溯
        ost["_original_content"] = content
        ost["_localization_category"] = localized.get("category", "unknown")
        ost["_localization_action"] = action

        if action == "delete" or not new_content:
            ost["content"] = ""  # 空字符串会被 extract_ost_entries 自动跳过
            modified += 1
            logger.debug(f"[ost_localizer] shot_{sid} 删除 OST '{content}'")
        elif action == "rewrite" and new_content != content:
            ost["content"] = new_content
            modified += 1
            logger.info(
                f"[ost_localizer] shot_{sid} 改写: '{content}' → '{new_content}' "
                f"({localized.get('category')})"
            )
        # action=keep 或 new_content==content 时保持原文不动

    return modified


def extract_manual_overrides_from_shots(
    approved_shots: list[dict] | None,
    original_ost_map: dict[int, str] | None = None,
) -> dict[int, str]:
    """从 Shots 表提取用户手动填写的 OST 本地化内容。

    判定规则：
    - 只有“OST本地化”字段有值且非空白时才计入
    - 若提供了 original_ost_map，还要求“OST原文”字段与当前 video_analysis 原文一致，
      避免上一次项目残留的旧值被误用

    Args:
        approved_shots: AirtableService.get_project_shots() 的返回值
        original_ost_map: {shot_id: 当前原 OST 内容}，用于验证手动覆盖的时效性

    Returns:
        {shot_number: localized_text}
    """
    overrides: dict[int, str] = {}
    if not approved_shots:
        return overrides

    for shot in approved_shots:
        fields = (shot or {}).get("fields") or {}
        shot_num = fields.get("镜头序号")
        localized = fields.get("OST本地化")
        if shot_num is None or not localized:
            continue
        if not isinstance(localized, str) or not localized.strip():
            continue
        try:
            sid_int = int(shot_num)
        except (TypeError, ValueError):
            continue

        # 若提供了原文映射，校验 Airtable 中的 OST原文 与当前 video_analysis 一致才生效
        if original_ost_map is not None:
            at_original = fields.get("OST原文")
            current_original = original_ost_map.get(sid_int)
            if at_original and current_original and at_original.strip() != current_original.strip():
                logger.info(
                    f"[ost_localizer] shot_{sid_int} 手动覆盖失效（原文已变更: "
                    f"'{at_original[:30]}' ≠ '{current_original[:30]}'），重走 Gemini"
                )
                continue

        overrides[sid_int] = localized.strip()

    if overrides:
        logger.info(f"[ost_localizer] 检测到 {len(overrides)} 条用户手动 OST 本地化覆盖")
    return overrides


async def save_localization_to_shots(
    airtable_service,
    approved_shots: list[dict] | None,
    localized_map: dict[int, dict],
) -> int:
    """把本地化结果批量回写到 Airtable Shots 表。

    跳过条件：
    - localized entry 的 rewrite_reason == 'user_manual_override'（用户自己填的，不覆盖）
    - approved_shots 里找不到对应 record_id

    Args:
        airtable_service: AirtableService 实例
        approved_shots: get_project_shots() 返回的 shots 列表（包含 id 和 fields）
        localized_map: localize_osts() 返回值

    Returns:
        成功回写的记录数
    """
    if not localized_map or not approved_shots or not airtable_service:
        return 0

    # 构造 shot_number → record_id 映射
    num_to_record: dict[int, str] = {}
    for shot in approved_shots:
        record_id = shot.get("id")
        shot_num = (shot.get("fields") or {}).get("镜头序号")
        if not record_id or shot_num is None:
            continue
        try:
            num_to_record[int(shot_num)] = record_id
        except (TypeError, ValueError):
            continue

    saved = 0
    for sid, entry in localized_map.items():
        # 跳过用户手动版本——他们填的就不要覆盖
        if entry.get("rewrite_reason") == "user_manual_override":
            continue
        record_id = num_to_record.get(sid)
        if not record_id:
            continue

        original = entry.get("original_content", "")
        new_content = entry.get("localized_content", "")
        action = entry.get("action", "keep")
        # delete 动作存空字符串，方便人工核对时看到 “被删除”
        if action == "delete":
            new_content = ""

        category = entry.get("category")
        category_val = category if category and category not in ("unknown", "manual") else None

        ok = await airtable_service.save_shot_ost_localization(
            shot_id=record_id,
            original=original,
            localized=new_content,
            category=category_val,
        )
        if ok:
            saved += 1

    if saved:
        logger.info(f"[ost_localizer] 回写 Shots 表 OST 本地化完成: {saved} 条")
    return saved
