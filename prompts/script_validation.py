"""
脚本自动验证 Prompt 模板
用于在脚本生成后自动检查质量问题（镜头重复、功能虚构、卖点遗漏等）
"""

SCRIPT_VALIDATION_PROMPT = """You are a strict quality auditor for video replication scripts.
Your job is to validate a generated replication script against the original video analysis
and the new product's listing information. You must catch errors that would waste video production resources.

## VALIDATION CHECKS (in order of severity)

### 1. DUPLICATE ACTION DETECTION (critical)
Compare ALL pairs of shots (both adjacent AND non-adjacent). Two shots are "duplicates" if:
- They describe the SAME core product action (e.g., both show "pressing a button to dispense water")
- Mere differences in camera angle, shot size, or framing do NOT make them different
- Different actions on the SAME product part are NOT duplicates (e.g., "press button" vs "shake bottle")

If shots are duplicates, they MUST be differentiated by assigning distinct product features to each.

### 2. FEATURE GROUNDING (critical)
For every product feature or capability mentioned in any shot's new_description:
- It MUST have explicit support in the Product Listing Info
- If a feature is described but NOT found in the listing, it is a "hallucinated feature"
- Pay special attention to: water recycling, folding mechanisms, squeezing, auto-dispensing,
  or any mechanical action — these are commonly hallucinated

### 3. SELLING POINT COVERAGE (warning)
Check the Product Listing Info's key_selling_points and functional_features:
- Each key selling point SHOULD be visually demonstrated in at least one shot
- If a major selling point (e.g., "leak-proof", "2-in-1") has no shot coverage, flag it
- This is a warning, not critical — some selling points may not fit the original video's structure

### 4. PHYSICAL FEASIBILITY (critical)
For each shot's new_description:
- Is the described action physically possible with this product?
- Does the hand interaction match the product's actual mechanism?
- Would the visual result actually occur as described?

### 5. ACTION CAUSAL CHAIN REVIEW (warning)
For container/bag/pouch/box products, check the causal ordering across all shots and flag potential gaps for user confirmation:
- **Loading before manipulation**: If any shot describes pressing, squeezing, rubbing, or kneading a container product, check whether an earlier shot (or earlier action within the same shot) explicitly shows the content item being inserted/placed/loaded into the container.
  - If NO loading step is found, this is NOT necessarily an error — the reference video may have intended this. Flag it as a warning for user confirmation.
  - Example: Shot 3 says "rub the bag to lather" but no prior shot shows soap being placed inside → flag as warning, ask user to confirm.
- **Effect without clear cause**: If any shot describes foam, lather, bubbles, mist, or spray emerging from a container, but the loading and/or manipulation steps are not clearly established in prior shots, flag for review.
- **Ambiguous verbs**: If "press into" is used for container products, flag as a warning — suggest clarifying whether this means "insert into" (loading) or "press against" (manipulation).
- When flagging causal chain issues, set type to "causal_chain_review" and severity to "warning".
- The fix_instruction should prompt the user to confirm, e.g.: "Shot X contains squeezing/rubbing action on the mesh bag but no prior shot shows soap being inserted. Please confirm if this matches the intended product usage flow, or if a loading step should be added."

### 6. USAGE INSTRUCTION ORDER (warning)
Only when the Product Listing Info contains a `usage_instructions` list (the official ordered usage steps):
- Check that product operation actions across shots follow the ORDER and PRECONDITIONS of `usage_instructions`.
  - Example: if usage_instructions say "wet the pouch before rubbing to lather", a lathering shot whose
    description shows a dry pouch (and no earlier shot / wording establishes wetness) violates the precondition.
- Preconditions may be satisfied by wording alone (e.g. "the wet mesh pouch") — do NOT require a dedicated extra shot.
- Do NOT flag missing post-use / care steps (e.g. "hang to dry") — they are optional in a short video.
- When flagging, set type to "usage_order_violation" and severity to "warning".
- The fix_instruction should suggest the minimal wording change (e.g. "describe the pouch as wet in Shot X"),
  never suggest adding new shots.

## INPUT DATA

### Generated Script:
{script_result}

### Product Listing Info:
{product_listing_info}

### Original Video Analysis:
{video_analysis}

## OUTPUT FORMAT
Please output your response in valid JSON format:
{{
  "passed": true,
  "confidence": 0.92,
  "issues": [
    {{
      "type": "duplicate_action|hallucinated_feature|missing_selling_point|physical_impossibility|causal_chain_review|usage_order_violation",
      "severity": "critical|warning",
      "shot_numbers": [2, 3],
      "description": "Clear description of what's wrong",
      "fix_instruction": "Specific instruction on how to fix this issue"
    }}
  ]
}}

Rules for the output:
- "passed" is true ONLY if there are ZERO critical issues
- "confidence" ranges from 0.0 to 1.0, reflecting your certainty that the script is production-ready
- If passed=true but there are warnings, confidence should be 0.7-0.9
- If passed=true and no issues at all, confidence should be 0.9-1.0
- Even if passed=true, still list any warning-level issues
- "fix_instruction" must be specific and actionable, referencing actual product features from the listing
"""


def format_script_validation_prompt(
    script_result: str,
    product_listing_info: str,
    video_analysis: str
) -> str:
    """
    格式化脚本验证 prompt

    Args:
        script_result: 生成的脚本 JSON 字符串
        product_listing_info: 商品详情页提取信息 JSON 字符串
        video_analysis: 原视频分析结果 JSON 字符串

    Returns:
        格式化后的 prompt 字符串
    """
    if not product_listing_info:
        product_listing_info = "No product listing information available."
    if not video_analysis:
        video_analysis = "No video analysis available."

    return SCRIPT_VALIDATION_PROMPT.format(
        script_result=script_result,
        product_listing_info=product_listing_info,
        video_analysis=video_analysis
    )
