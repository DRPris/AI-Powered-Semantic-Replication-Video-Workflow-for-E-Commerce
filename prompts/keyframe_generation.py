"""
关键帧生成 Prompt 模板
用于生成各镜头关键帧图片的提示词（首帧 & 后续帧）
"""

import re


def _sanitize_description(desc: str) -> str:
    """
    清洗场景描述：移除 on-screen text / 字幕 / overlay 等文本类描述，
    避免负面情绪文案或品牌文字触发图片生成平台的内容审核。
    """
    if not desc:
        return desc
    # ── 文字/字幕/覆层 ──
    desc = re.sub(r"On-screen[^.]*\.\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"Text\s+overlay[^.;]*[.;]\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"(?:The\s+)?text\s+overlay[^;.]*[;.]\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"Subtitle[^.]*\.\s*", "", desc, flags=re.IGNORECASE)
    # 残留引号片段（如 White ..' at the bottom ...）
    desc = re.sub(r"['\"]\s*positioned\s+in[^.]*\.\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"\bWhite\s+\.*['\"][^.]*\.\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"(?:at|in|near)\s+the\s+(?:bottom|top|center|upper|lower)[^.]*(?:frame|screen)[^.]*\.\s*", "", desc, flags=re.IGNORECASE)
    # ── 覆层特效（confetti / emoji / hearts）──
    desc = re.sub(r"(?:Heart\s+)?emojis?\s+and\s+confetti[^;.]*[;.]\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"Confetti\s+and\s+hearts?[^;.]*[;.]\s*", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"(?:hearts?|confetti|emojis?)\s+must\s+appear[^;.]*[;.]\s*", "", desc, flags=re.IGNORECASE)
    # ── 身份证件敏感词 ──
    desc = re.sub(r"Identification\s+card", "card", desc, flags=re.IGNORECASE)
    desc = re.sub(r"\bID\s+card", "card", desc, flags=re.IGNORECASE)
    # ── 版权角色/品牌 ──
    _copyright_map = {
        r"spider[- ]?man": "character-themed",
        r"bat[- ]?man": "wing-shaped",
        r"bat[- ]?themed\s+mask": "wing-shaped design",
        r"mask\s+silhouette": "folded silhouette",
        r"marvel": "comic-themed",
        r"\bDC\b": "comic-themed",
        r"avengers?": "character-themed",
        r"iron[- ]?man": "character-themed",
        r"captain\s+america": "character-themed",
        r"super[- ]?hero": "character-themed",
    }
    for pattern, replacement in _copyright_map.items():
        desc = re.sub(pattern, replacement, desc, flags=re.IGNORECASE)
    # 清理多余空格
    desc = re.sub(r"\s{2,}", " ", desc).strip()
    return desc


def build_first_shot_prompt(
    first_frame_description: str,
    camera_instruction: str,
    hard_constraints: str,
    product_analysis_summary: str,
    product_composition_details: str = "",
) -> str:
    """
    构建首帧关键帧生成 prompt（Shot 1，input_urls 仅含三视图）

    Args:
        first_frame_description: 首帧场景描述
        camera_instruction: 镜头指令（如 static / handheld shake 等）
        hard_constraints: 硬约束条件，为空时省略该段落
        product_analysis_summary: 产品物理属性摘要（layer1 + layer2）
        product_composition_details: 产品组件分解信息（多组件产品时提供）

    Returns:
        格式化后的 prompt 字符串
    """
    constraints_block = ""
    if hard_constraints:
        constraints_block = (
            f"\n[Hard Constraints]: {_sanitize_description(hard_constraints)}; "
            "This is a product demonstration image for e-commerce. "
            "All scenes depict normal product usage in a clean, professional setting."
        )
    else:
        constraints_block = (
            "\n[Hard Constraints]: This is a product demonstration image for e-commerce. "
            "All scenes depict normal product usage in a clean, professional setting."
        )

    composition_block = ""
    if product_composition_details:
        composition_block = (
            "\n[Product Composition & Fidelity]:\n"
            "This product consists of multiple separable components. Follow these rules STRICTLY:\n"
            "- Each component MUST maintain its own distinct color, shape, and material as described below.\n"
            "- Do NOT blend or merge colors between components (e.g., soap color must not tint the mesh bag).\n"
            "- Do NOT transfer physical features between components (e.g., if only the bag has a drawstring cord, the soap must NOT have any cord or string).\n"
            "- Do NOT alter any component's shape to match another component (each keeps its own geometry).\n"
            "- Effects like foam/lather require a physical cause (rubbing/squeezing) — they do NOT appear spontaneously.\n"
            "- The reference image shows the actual product — match EACH component precisely.\n"
            f"\nComponent Details:\n{product_composition_details}\n"
        )

    return (
        "Generate a photorealistic 9:16 portrait photograph.\n"
        "The image MUST be in 9:16 vertical/portrait aspect ratio (width < height).\n"
        "\n"
        "[Product Reference]: The attached image shows the product from multiple angles.\n"
        "The product in the generated image MUST exactly match this reference in shape, color, texture, and proportions.\n"
        "\n"
        f"[Scene Description]: {_sanitize_description(first_frame_description)}\n"
        f"[Camera]: {camera_instruction}\n"
        f"{constraints_block}"
        f"\n[Product Physical Properties]: {_sanitize_description(product_analysis_summary)}\n"
        f"{composition_block}"
        "\n"
        "[Hand & Object Interaction Constraints]:\n"
        "- Hands MUST be anatomically correct: exactly 5 fingers per hand, proper joint proportions, natural texture.\n"
        "- Fingers must not merge, split, or have extra/missing digits.\n"
        "- Hand-product interaction must be physically natural and ergonomic.\n"
        "- Tone and texture must be consistent across all visible areas.\n"
        "\n"
        "Style: Real smartphone-shot video frame, natural lighting, casual composition.\n"
        "Do NOT generate illustrations, renders, or overly polished studio shots."
    )


def build_continuation_shot_prompt(
    first_frame_description: str,
    camera_instruction: str,
    hard_constraints: str,
    product_analysis_summary: str,
    product_composition_details: str = "",
) -> str:
    """
    构建后续帧关键帧生成 prompt（Shot N，input_urls 含前序关键帧 + 三视图）

    Args:
        first_frame_description: 当前镜头的首帧场景描述
        camera_instruction: 镜头指令
        hard_constraints: 硬约束条件，为空时省略该段落
        product_analysis_summary: 产品物理属性摘要（layer1 + layer2）
        product_composition_details: 产品组件分解信息（多组件产品时提供）

    Returns:
        格式化后的 prompt 字符串
    """
    constraints_block = ""
    if hard_constraints:
        constraints_block = (
            f"\n[Hard Constraints]: {_sanitize_description(hard_constraints)}; "
            "This is a product demonstration image for e-commerce. "
            "All scenes depict normal product usage in a clean, professional setting."
        )
    else:
        constraints_block = (
            "\n[Hard Constraints]: This is a product demonstration image for e-commerce. "
            "All scenes depict normal product usage in a clean, professional setting."
        )
    
    composition_block = ""
    if product_composition_details:
        composition_block = (
            "\n[Product Composition & Fidelity]:\n"
            "This product consists of multiple separable components. Follow these rules STRICTLY:\n"
            "- Each component MUST maintain its own distinct color, shape, and material as described below.\n"
            "- Do NOT blend or merge colors between components (e.g., soap color must not tint the mesh bag).\n"
            "- Do NOT transfer physical features between components (e.g., if only the bag has a drawstring cord, the soap must NOT have any cord or string).\n"
            "- Do NOT alter any component's shape to match another component (each keeps its own geometry).\n"
            "- Effects like foam/lather require a physical cause (rubbing/squeezing) \u2014 they do NOT appear spontaneously.\n"
            "- The reference image shows the actual product \u2014 match EACH component precisely.\n"
            f"\nComponent Details:\n{product_composition_details}\n"
        )
    
    return (
        "Generate a photorealistic 9:16 portrait photograph that naturally continues from the previous scene.\n"
        "The image MUST be in 9:16 vertical/portrait aspect ratio (width < height).\n"
        "\n"
        "[Previous Frame]: The first attached image shows the previous shot's scene.\n"
        "Maintain the SAME environment, lighting, and person/hand position continuity.\n"
        "\n"
        "[Product Reference]: The second attached image shows the product from multiple angles.\n"
        "The product MUST exactly match this reference.\n"
        "\n"
        f"[Scene Description]: {_sanitize_description(first_frame_description)}\n"
        f"[Camera]: {camera_instruction}\n"
        f"{constraints_block}"
        f"\n[Product Physical Properties]: {_sanitize_description(product_analysis_summary)}\n"
        f"{composition_block}"
        "\n"
        "[Hand & Object Interaction Constraints]:\n"
        "- Hands MUST be anatomically correct: exactly 5 fingers per hand, proper joint proportions, natural texture.\n"
        "- Fingers must not merge, split, or have extra/missing digits.\n"
        "- Hand-product interaction must be physically natural and ergonomic.\n"
        "- Tone and texture must be consistent across all visible areas.\n"
        "\n"
        "[Transition Note]: This shot follows the previous one. "
        "The starting pose should be a natural continuation of where the previous shot would have ended."
    )
