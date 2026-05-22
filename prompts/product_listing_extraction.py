"""
商品链接内容提取 Prompt 模板
用于从商品详情页提取结构化产品信息（卖点、形态、功能特性等）
"""

PRODUCT_LISTING_EXTRACTION_PROMPT = """You are a product analyst specializing in e-commerce product research.
Given the following product listing page content, extract structured product information
that will be used for video production planning.

Focus on:
1. Physical form and any shape-changing capabilities (foldable, expandable, retractable, etc.)
2. Functional features that require specific actions to demonstrate
3. Key selling points that should be visually highlighted in a video
4. Usage scenarios that show the product in action

### CRITICAL RULES:

**Rule A – Canonical Product Name:**
Before filling any field, first determine the single canonical name for the product (e.g. "soap foaming pouch") and, if applicable, canonical names for each sub-component (e.g. "drawstring cord", "mesh body"). Use ONLY these canonical names throughout ALL fields. Do NOT alternate between synonyms (e.g. do not switch between "net", "bag", "pouch", "mesh", "sleeve" for the same item).

**Rule B – Action Sequence & Causality for functional_features:**
When describing `action_required`, you MUST specify the full operational sequence with cause-and-effect, not a vague single verb. Follow this pattern:
  1. Preparation step – what is placed/loaded into the product (if applicable)
  2. User action – the specific motion performed on the product
  3. Resulting effect – what observable outcome is produced and why
Example (correct): "Insert a bar of soap into the pouch, then squeeze and knead the pouch with wet hands; the mesh generates rich lather from the soap inside."
Example (wrong): "Rub it vigorously between the palms."
Also distinguish between: (a) actions performed ON the product itself, and (b) actions the product enables on the contained/loaded item. The description must include the state of any item loaded inside the product.

### Page Content:
{page_content}

Please output your response in valid JSON format with the following structure:
{{
  "product_name": "product name",
  "category": "product category",
  "key_selling_points": ["selling point 1", "selling point 2"],
  "physical_form": {{
    "shape": "describe the product's 3D form",
    "deformable": false,
    "deformation_type": "none|foldable|expandable|retractable|adjustable|other",
    "form_states": ["state 1: description", "state 2: description"],
    "size_description": "size relative to hand or common objects"
  }},
  "functional_features": [
    {{
      "feature_name": "feature name",
      "action_required": "FULL action sequence: [preparation step] → [user motion on product] → [resulting effect]. Must include any loaded item's state.",
      "visual_result": "what the viewer sees when this feature is activated"
    }}
  ],
  "usage_scenarios": ["scenario 1", "scenario 2"],
  "differentiators": "what makes this product unique compared to similar products",
  "video_production_notes": "any special considerations for filming this product (e.g., needs to show folding sequence, water flow, light effects)"
}}
"""


def format_product_listing_extraction_prompt(page_content: str) -> str:
    """
    格式化商品链接提取 prompt

    Args:
        page_content: 网页提取的文本内容

    Returns:
        格式化后的 prompt 字符串
    """
    # 截断过长的页面内容，防止超出 token 限制
    max_content_length = 15000
    if len(page_content) > max_content_length:
        page_content = page_content[:max_content_length] + "\n\n...(content truncated)"

    return PRODUCT_LISTING_EXTRACTION_PROMPT.format(
        page_content=page_content
    )
