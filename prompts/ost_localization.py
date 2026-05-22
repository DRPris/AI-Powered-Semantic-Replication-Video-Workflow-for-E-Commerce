"""
OST 本地化 Prompt —— 把原视频的屏幕文字改写为适合新商品的文案

输入:
  - 原 OST 列表（Stage 1 视频分析产出）
  - ProductBrief（Product Brief Agent 产出，含 product_name / key_selling_points / target_audience / tone / constraints）

输出:
  - 每条 OST 的分类 + 本地化后内容

使用场景:
  Stage 5 OST 叠加前置步骤，避免把原商品专有词（如 "Light Fury" / "Night Fury"）
  原封不动渲染到新商品（如摩托车头盔）视频上。
"""

OST_LOCALIZATION_PROMPT = """你是跨境电商短视频文案本地化专家。

# 任务
原视频是为【旧商品】拍的，其屏幕文字（OST）含有旧商品的角色名/型号/品牌名。
现在我们把视频复刻给【新商品】，需要对每条原 OST 做分类并按需改写，
保持原视频的节奏感和爆款钩子结构，同时让文案贴合新商品。

# 新商品信息
- 商品名称: {product_name}
- 品牌: {brand}
- 类别: {category}
- 核心卖点: {key_selling_points}
- 目标人群: {target_audience}
- 文案调性: {tone}
- 负向约束（不能出现）: {constraints}

# 原视频 OST 列表
{original_osts_json}

# 分类规则（必须选且只选一类）
1. **generic_hook** —— 通用钩子/催看句（可直接保留）
   示例: "Every biker needs this", "Wait for it 🤫", "POV:", "Must-have!"

2. **product_specific** —— 含有旧商品专有名词（必须改写）
   示例: "Light Fury", "Night Fury", "iPhone 15 Pro Max", "Lipstick #27"
   判断要点: 出现了旧商品特有的角色名、系列名、型号、专有词

3. **promo** —— 促销/价格信息
   示例: "50% OFF", "Limited stock", "Buy 1 Get 1"
   处理: 通用促销词保留；带旧商品信息的改写为新商品可用的版本

4. **emotional** —— 情绪/感受/动作描述（保留）
   示例: "So satisfying 😌", "Trust me", "Game-changer ✨", "It works!"

5. **brand_badge** —— 原品牌水印/logo 文字（必须替换为新品牌）
   示例: "NIKE", "Apple", "Samsung"

# 改写规则（仅对 product_specific / brand_badge / 部分 promo 生效）
- 长度: 不超过原 content 字符数的 1.3 倍（避免排版溢出）
- emoji: 保持原文 emoji 风格一致（数量可 ±1）
- 语义角色: 保持原 OST 在剧情中的作用（hook / 卖点 / CTA / 角色介绍）
- 调性: 严格符合 {tone} 文案调性
- 禁止: 不引入原文没有的品牌名/人名；避免 {constraints} 中的元素
- 语言: 与原 OST 语言保持一致（原是英文就输出英文，原是中文就输出中文）

# 特殊处理
- 若原 OST 是多个商品名的并列介绍（如 "Light Fury 🤍 & Night Fury 🖤"），
  改写成新商品的多变体/多颜色/多卖点的并列介绍（如 "Day Mode 🤍 & Night Mode 🖤"）
- 若原 OST 完全无法对应到新商品（如剧情已偏离），输出 localized_content="" 标记为删除

# 输出要求
必须是严格合法的 JSON 数组（不要 markdown 代码块包裹），每个元素对应一条原 OST，顺序一致：

[
  {{
    "shot_id": <原镜头编号>,
    "original_content": "<原文>",
    "category": "generic_hook|product_specific|promo|emotional|brand_badge",
    "localized_content": "<本地化后内容，若保留则填原文；若删除则填空串>",
    "action": "keep|rewrite|delete",
    "rewrite_reason": "<简短说明为何这样处理，≤30字>"
  }}
]
"""


def build_ost_localization_prompt(
    product_brief: dict,
    original_osts: list[dict],
) -> str:
    """
    构造 OST 本地化的完整 prompt 字符串

    Args:
        product_brief: ProductBrief JSON 字典，需包含:
            - product_name, brand, category
            - key_selling_points (list)
            - target_audience, tone
            - constraints (list)
        original_osts: 原 OST 列表，每条含:
            - shot_id: int
            - content: str
            - position: str
            - timing: str

    Returns:
        完整 prompt 字符串
    """
    import json

    key_sps = product_brief.get("key_selling_points") or []
    if isinstance(key_sps, list):
        key_sps_str = "; ".join(str(s) for s in key_sps[:5]) or "（未提供）"
    else:
        key_sps_str = str(key_sps)

    constraints = product_brief.get("constraints") or []
    if isinstance(constraints, list):
        constraints_str = "; ".join(str(c) for c in constraints[:5]) or "（无）"
    else:
        constraints_str = str(constraints) or "（无）"

    return OST_LOCALIZATION_PROMPT.format(
        product_name=product_brief.get("product_name") or "（未提供）",
        brand=product_brief.get("brand") or "（未提供）",
        category=product_brief.get("category") or "（未提供）",
        key_selling_points=key_sps_str,
        target_audience=product_brief.get("target_audience") or "（未提供）",
        tone=product_brief.get("tone") or "playful",
        constraints=constraints_str,
        original_osts_json=json.dumps(original_osts, ensure_ascii=False, indent=2),
    )
