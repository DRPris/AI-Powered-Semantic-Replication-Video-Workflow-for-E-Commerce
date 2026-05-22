"""
四层约束 Prompt 模板
用于视频生成时通过 negative_prompt 和 prompt 增强来硬约束产品形象

优化说明：
- 约束生成使用精简版数据，减少约 50% token 消耗
- 只传入关键特征摘要，不传完整分析结果
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# 第一层：产品完整性约束（PRODUCT_INTEGRITY_PROMPT）
# 用于 negative_prompt，确保产品外观不被改变
# =============================================================================
PRODUCT_INTEGRITY_PROMPT = """基于以下产品分析结果，生成产品完整性约束：

{product_analysis}

请提取产品的关键视觉属性，并生成"绝对不能改变"的约束列表。

约束应该包括：
1. 颜色约束 - 不能改变产品的颜色、色调、色彩比例
2. 材质约束 - 不能改变产品的材质质感、光泽度、纹理
3. 形状约束 - 不能改变产品的整体形状、轮廓、比例
4. 品牌标识约束 - 不能移除、模糊或修改品牌logo、文字、图案
5. 结构约束 - 不能改变产品的结构、部件位置、开合方式

请输出简洁有力的否定句式，每条约束以"不要"、"禁止"、"不得"开头。
例如：
- 不要改变产品颜色
- 禁止修改品牌标识
- 不得改变产品形状
"""


# =============================================================================
# 第二层：电影语言约束（CINEMATOGRAPHY_PROMPT）
# 用于 prompt 增强，保持与原视频一致的镜头风格
# =============================================================================
CINEMATOGRAPHY_PROMPT = """基于以下视频分析结果，生成电影语言约束：

{video_analysis}

请提取原视频的电影语言特征，生成约束描述。

需要保持一致的元素：
1. 镜头运动 - 推/拉/摇/移/跟/升降/静态的具体方式
2. 构图风格 - 景别（特写/近景/中景/全景）、主体位置、画面平衡
3. 光线风格 - 光源方向、光质（硬光/柔光）、色温、明暗对比
4. 摄影风格 - 手持感/稳定器/固定机位、景深效果
5. 色彩风格 - 整体色调、饱和度、对比度风格

请输出自然语言描述，用于增强视频生成提示词。
格式示例：
"保持[具体镜头运动]的运镜方式，使用[具体景别]构图，[光线特征]的光线效果..."
"""


# =============================================================================
# 第三层：叙事结构约束（NARRATIVE_PROMPT）
# 用于 prompt 增强，保持场景节奏和叙事逻辑
# =============================================================================
NARRATIVE_PROMPT = """基于以下复刻脚本，生成叙事结构约束：

{script}

请分析脚本的叙事结构，生成约束描述。

需要保持一致的元素：
1. 场景节奏 - 每个镜头的时长比例、节奏快慢、停顿位置
2. 叙事逻辑 - 动作先后顺序、因果关系、起承转合
3. 视觉焦点 - 观众注意力引导路径、重点展示区域
4. 动作连贯性 - 镜头间动作的衔接方式、动作的流畅度
5. 情感节奏 - 紧张/舒缓的情绪变化曲线

请输出自然语言描述，用于增强视频生成提示词。
格式示例：
"按照[具体节奏]推进场景，保持[动作A]到[动作B]的连贯过渡，在[具体时刻]呈现视觉高潮..."
"""


# =============================================================================
# 第四层：替换边界约束（REPLACEMENT_BOUNDARY_PROMPT）
# 用于 negative_prompt，明确只允许替换产品部分
# =============================================================================
REPLACEMENT_BOUNDARY_PROMPT = """基于以下产品分析和视频分析结果，生成替换边界约束：

产品分析：
{product_analysis}

视频分析：
{video_analysis}

请明确界定"什么可以替换"和"什么必须保持原样"。

必须保持原样的元素（禁止改变）：
1. 场景环境 - 背景、地面、家具、装饰物
2. 人物/动物 - 出现的人物、宠物、手部的肤色和特征
3. 动作方式 - 手持位置、操作手势、身体动作
4. 光线环境 - 环境光、阴影方向、反光效果
5. 画面氛围 - 整体色调、氛围感、情绪基调

只允许替换的元素：
- 原始产品 → 新产品（保持相同的使用方式和位置）

请输出简洁有力的否定句式，明确禁止改变非产品元素。
例如：
- 不要改变背景环境
- 禁止修改人物特征
- 不得改变光线条件
- 不要替换场景中的其他物体
"""


# =============================================================================
# 约束生成主函数
# =============================================================================

CONSTRAINTS_GENERATION_PROMPT = """你是一个视频生成约束专家。基于以下产品和视频风格信息，生成用于AI视频生成的约束。

## 产品关键特征
{product_summary}

## 视频风格要点
{video_summary}

## 脚本概览
{script_summary}

## 任务

生成两类约束：

### 1. Negative Prompt（<300字）
简洁有力的禁止项：
- 产品完整性：禁止改变颜色、材质、品牌标识
- 替换边界：禁止改变场景、背景、人物
- **视觉元素幻觉禁止（重要）**：禁止生成脚本中未描述的任何视觉效果。AI 模型经常自行添加泡沫(foam)、水花(splashes)、蒸汽(steam)、烟雾(smoke)、火花(sparkles)、光晕(lens flares)、粒子效果(particles)等不存在于原始脚本中的视觉元素。必须在 negative_prompt 中明确禁止这些。
- **"Only What's Described" 原则**：视频中只允许出现脚本明确描述的视觉内容，不得自行添加任何额外元素

### 2. Prompt Enhancement
注入到提示词前的强化描述：
- 产品外观关键特征
- 镜头风格要求
- **在末尾追加**："IMPORTANT: Only render visual elements explicitly described in the scene. Do NOT hallucinate or add foam, bubbles, splashes, steam, smoke, sparkles, particles, or any other visual effects not mentioned in the script."

输出JSON：
```json
{{
  "negative_prompt": "...",
  "prompt_enhancement": "..."
}}
```
"""


def _extract_product_summary(product_analysis: str) -> str:
    """
    从产品分析中提取关键摘要
    将完整分析压缩为简短描述，减少 token 消耗
    """
    if not product_analysis:
        return "无产品信息"
    
    try:
        data = json.loads(product_analysis)
        if isinstance(data, dict):
            # 尝试从嵌套结构中提取
            analysis = data.get("analysis_result", data)
            
            parts = []
            if "name" in analysis:
                parts.append(f"产品: {analysis['name']}")
            if "category" in analysis:
                parts.append(f"类别: {analysis['category']}")
            if "colors" in analysis and isinstance(analysis["colors"], list):
                parts.append(f"颜色: {', '.join(str(c) for c in analysis['colors'][:3])}")
            if "materials" in analysis and isinstance(analysis["materials"], list):
                parts.append(f"材质: {', '.join(str(m) for m in analysis['materials'][:2])}")
            if "brand_elements" in analysis:
                parts.append(f"品牌: {analysis['brand_elements']}")
            if "features" in analysis and isinstance(analysis["features"], list):
                parts.append(f"特征: {', '.join(str(f) for f in analysis['features'][:3])}")
            
            if parts:
                return " | ".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # 如果解析失败，返回截断的原文
    return product_analysis[:300] + "..." if len(product_analysis) > 300 else product_analysis


def _extract_video_summary(video_analysis: str) -> str:
    """
    从视频分析中提取关键风格摘要
    """
    if not video_analysis:
        return "无视频风格信息"
    
    try:
        data = json.loads(video_analysis)
        if isinstance(data, dict):
            analysis = data.get("analysis_result", data)
            
            parts = []
            
            # 提取镜头数量
            shots = analysis.get("shots", [])
            if shots:
                parts.append(f"共{len(shots)}个镜头")
            
            # 提取关键保留项
            brief = analysis.get("replication_brief", {})
            must_preserve = brief.get("must_preserve", [])
            if must_preserve:
                if isinstance(must_preserve, list):
                    parts.append(f"保留: {', '.join(str(p) for p in must_preserve[:3])}")
                else:
                    parts.append(f"保留: {str(must_preserve)[:100]}")
            
            if parts:
                return " | ".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    
    return video_analysis[:300] + "..." if len(video_analysis) > 300 else video_analysis


def _extract_script_summary(script: str) -> str:
    """
    从脚本中提取概要信息
    """
    if not script:
        return "无脚本信息"
    
    try:
        data = json.loads(script)
        if isinstance(data, dict):
            shots = data.get("shots", [])
            if shots:
                return f"共{len(shots)}个镜头的复刻脚本"
    except (json.JSONDecodeError, TypeError):
        pass
    
    return f"脚本长度: {len(script)}字符"


async def generate_constraints(
    product_analysis: str,
    video_analysis: str,
    script: str,
    gemini_service,
) -> dict:
    """
    生成四层约束集合。
    
    优化版本：使用压缩后的数据摘要，减少约 50% token 消耗

    Args:
        product_analysis: 产品分析结果（JSON字符串或文本）
        video_analysis: 视频分析结果（JSON字符串或文本）
        script: 复刻脚本（JSON字符串或文本）
        gemini_service: GeminiService 实例，用于调用Gemini生成约束

    Returns:
        {
            "negative_prompt": str,  # 用于 KIE AI 的 negative_prompt 参数
            "prompt_enhancement": str,  # 注入到生成提示词前面的产品外观强化描述
        }
    """
    # 提取压缩后的摘要
    product_summary = _extract_product_summary(product_analysis)
    video_summary = _extract_video_summary(video_analysis)
    script_summary = _extract_script_summary(script)
    
    logger.info(
        f"[Token优化] 约束生成数据压缩: "
        f"product {len(product_analysis)}→{len(product_summary)}, "
        f"video {len(video_analysis)}→{len(video_summary)}, "
        f"script {len(script)}→{len(script_summary)}"
    )
    
    # 构建约束生成prompt（使用压缩后的数据）
    prompt = CONSTRAINTS_GENERATION_PROMPT.format(
        product_summary=product_summary,
        video_summary=video_summary,
        script_summary=script_summary,
    )

    # 构建请求内容
    contents = [
        {
            "role": "user",
            "parts": [{"text": prompt}],
        }
    ]

    # 调用Gemini API生成约束
    response = await gemini_service._generate_content(contents)

    # 提取文本响应
    text = gemini_service._extract_text_from_response(response)

    # 解析JSON输出
    result = gemini_service._parse_structured_output(text)

    # 确保返回结果包含必要的字段
    return {
        "negative_prompt": result.get("negative_prompt", ""),
        "prompt_enhancement": result.get("prompt_enhancement", ""),
    }


def format_product_integrity_prompt(product_analysis: str) -> str:
    """
    格式化产品完整性约束prompt

    Args:
        product_analysis: 产品分析结果

    Returns:
        格式化后的prompt字符串
    """
    return PRODUCT_INTEGRITY_PROMPT.format(product_analysis=product_analysis)


def format_cinematography_prompt(video_analysis: str) -> str:
    """
    格式化电影语言约束prompt

    Args:
        video_analysis: 视频分析结果

    Returns:
        格式化后的prompt字符串
    """
    return CINEMATOGRAPHY_PROMPT.format(video_analysis=video_analysis)


def format_narrative_prompt(script: str) -> str:
    """
    格式化叙事结构约束prompt

    Args:
        script: 复刻脚本

    Returns:
        格式化后的prompt字符串
    """
    return NARRATIVE_PROMPT.format(script=script)


def format_replacement_boundary_prompt(product_analysis: str, video_analysis: str) -> str:
    """
    格式化替换边界约束prompt

    Args:
        product_analysis: 产品分析结果
        video_analysis: 视频分析结果

    Returns:
        格式化后的prompt字符串
    """
    return REPLACEMENT_BOUNDARY_PROMPT.format(
        product_analysis=product_analysis,
        video_analysis=video_analysis,
    )
