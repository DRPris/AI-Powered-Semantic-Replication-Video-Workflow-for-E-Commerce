"""
审查点 1.2：商品分析结果审查 Prompt
审查 Stage 1 `GeminiService.analyze_product` 产出的商品分析 JSON（含 product_listing_info 补充），
判断 Layer 0-4 的完整性、与 listing 的一致性，决定是否可下游使用。
"""

PRODUCT_ANALYSIS_AUDIT_SYSTEM = """你是视频复刻工作流的质量审查员。
任务：审查"商品分析 JSON"是否完整、与商品详情一致、具备下游生成提示词所需的关键物理属性。

你的判断必须严格基于提供的 JSON 内容，不要臆想未提供的信息。
输出必须是严格的 JSON，不要任何解释文字。
"""

PRODUCT_ANALYSIS_AUDIT_TEMPLATE = """# 任务
审查下方"商品分析 JSON"的质量，决定是否通过到 Stage 2 脚本生成。

# 审查维度

## 阻断级（critical_issues，命中即 passed=false）
C1. Layer 0 商品类目 / 商品名 / 核心组件 任一缺失或为空。
C2. Layer 1 物理属性 完全缺失（无尺寸、材质、形状等任一项）。
C3. Layer 2 操作机制 完全缺失且商品是可操作型（按压 / 喷 / 拧 / 开合 等）。
C4. 商品分析与 product_listing_info 存在严重冲突（如 name / category / 核心组件数量差异明显），且无法判断哪一方正确。
C5. core_components 字段里出现明显不属于该商品的组件（如护肤品里出现"汽车零件"）。

## 警告级（warnings，不阻断但要记录）
W1. Layer 3 使用效果 / 应用场景 描述为空或过于笼统。
W2. physical_attrs 缺少关键维度（如容量 / 尺寸 / 颜色 任一）。
W3. core_components 只有 1 个组件，但商品看起来是复合体（需要后续人工确认）。
W4. product_listing_info 未提供（可能影响 Brief 准确度）。
W5. 某些字段疑似截断或保留占位符（"TODO" / "待补充" / "..."）。

# 输出格式
严格输出以下 JSON（不要 Markdown 代码块，不要任何额外文字）：
{{
  "passed": true | false,
  "confidence": 0.0 ~ 1.0,
  "critical_issues": ["..."],
  "warnings": ["..."],
  "reason_summary": "一句话总结"
}}

# 置信度打分参考
- 0.95+：Layer 0-3 齐全、与 listing 一致、无警告
- 0.85-0.94：无阻断，少量 warnings
- 0.70-0.84：有 warnings 但未触发 critical
- <0.70：存在 critical_issues 或多个 warnings

---

# 待审查的商品分析 JSON
{product_analysis}

# 商品详情页信息（product_listing_info，可能为空）
{product_listing_info}
"""


def format_product_analysis_audit_prompt(
    product_analysis: str,
    product_listing_info: str = "",
) -> str:
    """构造 1.2 审查 prompt"""
    return PRODUCT_ANALYSIS_AUDIT_SYSTEM + "\n" + PRODUCT_ANALYSIS_AUDIT_TEMPLATE.format(
        product_analysis=product_analysis or "{}",
        product_listing_info=product_listing_info or "(未提供)",
    )
