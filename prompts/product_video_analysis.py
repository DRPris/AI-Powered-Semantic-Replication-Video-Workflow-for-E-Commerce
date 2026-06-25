"""
商品详情页视频分析 Prompt（差分模式）
用途：Product Brief Agent 在 Stage1 对商品页嵌入视频进行"补差"式语义理解：
    明确告知模型 listing 与商品主图已经提供了哪些信息，让视频分析只聚焦
    这些渠道无法呈现的视频独有证据——多角度外观、使用中动态状态、真实尺度、
    材质在动态下的表现等。

核心设计：
- Prompt 通过 {known_facts} 占位显式注入"已知事实"，反复强调 DO NOT REPEAT
- 输出结构围绕"视频独有信息"组织，与 listing 字段正交
- 仍保留 on_screen_text / confidence / limitations，便于下游对齐
"""

from typing import Optional


PRODUCT_VIDEO_ANALYSIS_PROMPT_TEMPLATE = """You are analyzing a SHORT promo video embedded on a product detail page
(e.g. Lazada / Amazon / Shopify main-image video).

YOUR TASK IS DIFFERENTIAL — NOT a general video analysis.
The listing page and the still product image ALREADY cover product name, brand,
generic selling points and obvious front-view appearance. You MUST ignore those
and extract ONLY the information the video uniquely reveals.

### KNOWN FACTS (from listing page + still product image — do NOT repeat these)
{known_facts}

### WHAT TO EXTRACT (video-unique evidence only)

1. MULTI-ANGLE PHYSICAL DETAILS that a single front-view image cannot show:
   top view, side profile, bottom, back/rear, interior / cross-section,
   close-up textures of specific parts (buttons, seams, mesh, logos, nozzles).

2. IN-USE DYNAMIC STATES the product exhibits WHILE being operated:
   deformation (fold / expand / collapse / bend), liquid / foam / mist behavior,
   light / sound / haptic responses, elasticity, transitions between form states,
   speed and force of motion, the moment something "clicks" or "pops".

3. REAL-WORLD SCALE & CONTEXT CUES inferable only from motion:
   size relative to a human hand, grip posture, placement on common objects
   (counter, sink, floor), lighting of the actual use environment.

4. MATERIAL BEHAVIOR UNDER MOTION & LIGHT:
   soft / rigid / transparent / reflective / matte — observed by how it bends,
   catches light, absorbs liquid, vibrates, etc.

5. USER INTERACTION QUALITY missed by static listings:
   ergonomics of the grip, one-handed vs two-handed use, speed of the effect,
   whether the user needs protective gear, whether hands get dirty / wet.

### RULES

- SKIP anything already stated in KNOWN FACTS. If the listing says "foldable",
  do NOT output "it is foldable" — instead describe HOW it folds (direction,
  number of steps, final flat thickness, sound of the snap).
- If a field has no video-unique evidence, return empty string "" or [].
  NEVER invent details just to fill fields.
- Every observation must be grounded in a visible moment (give a timecode hint
  if possible, e.g. "0:03").
- "new_info_not_in_listing" is the most important field — list every meaningful
  fact the listing page missed.

### OUTPUT (strict JSON, no markdown fences, no prose)

{{
  "duration_sec": number,
  "multi_angle_details": {{
    "top_view": string,
    "side_view": string,
    "bottom_view": string,
    "back_view": string,
    "interior_view": string,
    "close_up_textures": [
      {{"part": string, "texture": string, "timecode_hint": string}}
    ]
  }},
  "in_use_dynamic_states": [
    {{
      "moment": string,              // e.g. "when user squeezes the pouch"
      "observable": string,          // concrete visual / audible outcome
      "material_cue": string,        // e.g. "flexible silicone compresses and rebounds"
      "timecode_hint": string
    }}
  ],
  "scale_and_context_cues": {{
    "size_in_hand": string,          // e.g. "palm-sized, fits fully inside closed fist"
    "grip_posture": string,          // e.g. "pinch grip with thumb and index finger"
    "placement_context": string,     // e.g. "kitchen sink with running water"
    "environment_lighting": string   // e.g. "warm daylight through window"
  }},
  "material_behavior_under_motion": string,
  "user_interaction_quality": string, // ergonomic / speed / mess-level observations
  "new_info_not_in_listing": [string], // MOST IMPORTANT — facts absent from KNOWN FACTS
  "already_known_confirmed": [string], // listing claims you visually verified (for sources tracking)
  "on_screen_text": [
    {{"text": string, "role": string, "timecode_hint": string}}
  ],
  "tone_hints": {{
    "style": string,                 // professional / playful / warm / premium / minimal / energetic / lifestyle
    "evidence": string               // one sentence
  }},
  "target_audience_visual_cues": [string], // who holds it / where / emotion — grounded in frames
  "pain_points_shown": [string],     // problems the video visualizes before resolving
  "limitations": [string],           // what the video still does NOT clarify
  "confidence": number               // 0.0-1.0
}}

Output valid JSON only. No ```json fences. No commentary.
"""


def format_product_video_analysis_prompt(known_facts: Optional[dict] = None) -> str:
    """格式化商品视频差分分析 prompt

    Args:
        known_facts: 已从 listing / 商品主图分析中获取的"已知事实"摘要，
            结构建议：
              {
                "product_name": str,
                "category": str,
                "key_selling_points": [str],
                "physical_form": {...},          # from listing
                "functional_features": [str],    # feature names only, no actions
                "main_image_components": [str],  # from preliminary layer 0 (component names)
                "main_image_physical_attrs": {...},  # from preliminary layer 1
              }
            传入后 Prompt 会注入这些字段，并强制模型 SKIP 这些内容。
            为节省 token，每个字段会被截短到合理长度。

    Returns:
        完整 prompt 字符串
    """
    import json as _json

    if not known_facts:
        known_block = "(no prior facts — analyze the video as ground truth)"
    else:
        # 裁剪到关键字段，避免 prompt 膨胀
        slim: dict = {}
        for k in (
            "product_name",
            "category",
            "key_selling_points",
            "physical_form",
            "functional_features",
            "main_image_components",
            "main_image_physical_attrs",
            "usage_scenarios",
        ):
            v = known_facts.get(k)
            if v in (None, "", [], {}):
                continue
            slim[k] = v
        try:
            known_block = _json.dumps(slim, ensure_ascii=False, indent=2)
        except Exception:
            known_block = str(slim)
        # 硬裁剪，防止超长
        if len(known_block) > 3000:
            known_block = known_block[:3000] + "\n...(truncated)"

    return PRODUCT_VIDEO_ANALYSIS_PROMPT_TEMPLATE.format(known_facts=known_block)


# 兼容旧引用：保留常量名，但现在是"无 known_facts"的默认版本
PRODUCT_VIDEO_ANALYSIS_PROMPT = format_product_video_analysis_prompt(None)


def build_known_facts_from_sources(
    product_listing_info: Optional[dict],
    preliminary_analysis: Optional[dict],
) -> dict:
    """把 listing 结构化数据 + 商品主图 5 层分析压缩成 Prompt 友好的 known_facts

    原则：
    - 只保留"listing/主图已经明确给出"的事实，这些是视频分析需要跳过的
    - 不传入 listing 中含糊的 video_production_notes（那是对视频的建议，不是已知事实）
    - functional_features 只保留 feature_name，不保留 action_required，因为视频正是
      要验证/补充 action 的地方
    """
    facts: dict = {}
    listing = product_listing_info or {}
    preliminary = preliminary_analysis or {}

    if listing.get("product_name"):
        facts["product_name"] = listing["product_name"]
    if listing.get("category"):
        facts["category"] = listing["category"]
    if listing.get("key_selling_points"):
        facts["key_selling_points"] = list(listing["key_selling_points"])[:8]
    if listing.get("physical_form"):
        facts["physical_form"] = listing["physical_form"]
    if listing.get("functional_features"):
        facts["functional_features"] = [
            f.get("feature_name", "") if isinstance(f, dict) else str(f)
            for f in listing["functional_features"]
        ][:8]
    if listing.get("usage_scenarios"):
        facts["usage_scenarios"] = list(listing["usage_scenarios"])[:6]

    # 商品主图 5 层分析：只抽"已知"的层 0 / 层 1
    layer0 = preliminary.get("layer_0_component_decomposition") or {}
    components = layer0.get("components") or []
    if components:
        facts["main_image_components"] = [
            c.get("name", "") if isinstance(c, dict) else str(c)
            for c in components
        ][:8]

    layer1 = preliminary.get("layer_1_physical_attributes")
    if layer1:
        facts["main_image_physical_attrs"] = layer1

    return facts
