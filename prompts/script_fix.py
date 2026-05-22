"""
脚本自动修正 Prompt 模板
用于根据验证报告中的问题修正脚本，仅修改有问题的镜头
"""

SCRIPT_FIX_PROMPT = """You are a script repair specialist. A video replication script has been
generated but failed quality validation. Your job is to fix ONLY the specific issues identified,
while preserving everything else exactly as-is.

## REPAIR RULES

1. **Minimal changes**: ONLY modify the shots explicitly mentioned in the issues list.
   Do NOT touch shots that are not flagged.

2. **Deduplication allowed**: If duplicate_action issues are flagged, you MAY remove one of the
   duplicate shots (keep the one with better feature grounding). Adjust shot_number sequencing
   and total_shots count accordingly. Do NOT add new shots.

3. **Feature grounding**: Every product feature you mention MUST exist in the Product Listing Info.
   Do NOT invent or assume features. If unsure, use a simple showcase action (rotate product,
   show label, demonstrate form factor).

4. **Action differentiation**: If two shots were flagged as duplicates, ensure each shot
   demonstrates a DIFFERENT product feature or action. Refer to the Product Listing Info's
   functional_features list to pick distinct features for each shot.

5. **Structure preservation**: Keep the same JSON structure, field names, and shot_number values.
   Keep original_description unchanged. Only modify new_description, changes_made,
   forced_change, and forced_change_reason for the affected shots.

6. **Physical feasibility**: All described actions must be physically possible with the product
   as described in the Product Listing Info.

## INPUT DATA

### Current Script (with issues):
{script_result}

### Validation Issues to Fix:
{validation_issues}

### Product Listing Info (ground truth for features):
{product_listing_info}

### Original Video Analysis:
{video_analysis}

### Product Analysis:
{product_analysis}

## OUTPUT FORMAT
Output the COMPLETE fixed script in the same JSON structure as the input script.
Include ALL shots (both fixed and unchanged ones).

Please output your response in valid JSON format with the same structure:
{{
  "shots": [
    {{
      "shot_number": 1,
      "original_description": "...",
      "new_description": "...",
      "duration": "X seconds",
      "changes_made": "...",
      "forced_change": false,
      "forced_change_reason": ""
    }}
  ],
  "summary": {{
    "total_shots": 0,
    "forced_changes_count": 0,
    "notes": "..."
  }}
}}
"""


def format_script_fix_prompt(
    script_result: str,
    validation_issues: str,
    product_listing_info: str,
    video_analysis: str,
    product_analysis: str
) -> str:
    """
    格式化脚本修正 prompt

    Args:
        script_result: 当前脚本 JSON 字符串
        validation_issues: 验证报告中的 issues 列表 JSON 字符串
        product_listing_info: 商品详情页提取信息 JSON 字符串
        video_analysis: 原视频分析结果 JSON 字符串
        product_analysis: 商品属性分析结果 JSON 字符串

    Returns:
        格式化后的 prompt 字符串
    """
    if not product_listing_info:
        product_listing_info = "No product listing information available."
    if not video_analysis:
        video_analysis = "No video analysis available."
    if not product_analysis:
        product_analysis = "No product analysis available."

    return SCRIPT_FIX_PROMPT.format(
        script_result=script_result,
        validation_issues=validation_issues,
        product_listing_info=product_listing_info,
        video_analysis=video_analysis,
        product_analysis=product_analysis
    )
