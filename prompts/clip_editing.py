"""
复刻剪辑 Agent - 语义选段 Prompt 模板
=====================================

当前状态：Phase 2 预留。Phase 1 规则层不调用 LLM，此模板在 Phase 2 接入
Gemini 视频理解时使用，用于从"超长生成 clip"中挑出与原镜头语义最贴合的
最佳时间窗。

设计要点：
- 输入：生成 clip（视频直传 base64）+ 原镜头语义摘要 + 目标时长 T
- 输出严格 JSON：best_window(start/end) + semantic_anchors + confidence
- 任何异常降级为 trim_head（从 0 起裁到 T 秒）
"""

CLIP_SEMANTIC_PICK_PROMPT = """你是一位专业视频剪辑师，需要从一段生成的产品视频片段中，挑选出与"原镜头语义"最贴合、时长为 {target_duration:.2f} 秒的最佳连续时间窗。

## 原镜头语义摘要
- 镜头编号：{shot_number}
- 原镜头动作：{action_description}
- 原镜头时长：{original_duration:.2f} 秒
- 原镜头节奏：{pace}（快/慢切）
- 关键视觉锚点：{visual_anchors}

## 输入视频
上方是生成的超长 clip（{source_duration:.2f} 秒），你需要从中选出最能复刻原镜头语义的 {target_duration:.2f} 秒连续片段。

## 选段规则
1. **必须保留动作高潮**：识别视频中的动作起点 → 高潮 → 收尾，优先保留高潮帧周围的窗口。
2. **避免画面模糊 / 无关前摇**：如果开头 N 秒是静态等待或镜头预热，裁剪起点应跳过。
3. **避免切尾跳帧**：最后 0.3 秒若出现镜头跳切或 logo 帧，应避开。
4. **置信度评估**：如果找不到明确的动作高潮，置信度 ≤ 0.5，由规则层兜底。

## 输出格式（严格 JSON，不要任何多余文字）
```json
{{
  "best_window": {{
    "start_sec": 1.2,
    "end_sec": 3.0
  }},
  "semantic_anchors": [
    {{"timestamp_sec": 1.6, "description": "手部开始接触产品"}},
    {{"timestamp_sec": 2.3, "description": "产品完全入镜，进入高潮"}}
  ],
  "confidence": 0.85,
  "reasoning": "原镜头高潮在 60% 位置，所选窗口完整覆盖动作弧"
}}
```

## 注意
- `end_sec - start_sec` 必须等于 {target_duration:.2f}（±0.1 秒误差）
- `start_sec >= 0`，`end_sec <= {source_duration:.2f}`
- 不允许多段拼接，只选一个连续窗口
"""


def format_clip_semantic_pick_prompt(
    shot_number: int,
    source_duration: float,
    target_duration: float,
    original_duration: float,
    action_description: str,
    pace: str = "medium",
    visual_anchors: str = "",
) -> str:
    """格式化语义选段 Prompt"""
    return CLIP_SEMANTIC_PICK_PROMPT.format(
        shot_number=shot_number,
        source_duration=source_duration,
        target_duration=target_duration,
        original_duration=original_duration,
        action_description=action_description or "（无描述）",
        pace=pace or "medium",
        visual_anchors=visual_anchors or "（无明确锚点）",
    )
