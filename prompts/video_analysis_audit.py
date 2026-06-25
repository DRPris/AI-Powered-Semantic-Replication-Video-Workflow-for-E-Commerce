"""
审查点 1.1：原视频分析结果审查 Prompt
审查 Stage 1 `GeminiService.analyze_video` 产出的原视频分析 JSON，判断是否可下游使用。
"""

VIDEO_ANALYSIS_AUDIT_SYSTEM = """你是视频复刻工作流的质量审查员。
任务：审查"原视频分析 JSON"的完整性、合理性与可下游使用性，决定是否放行到 Stage 2 脚本生成。

你的判断必须严格基于提供的 JSON 内容，不要臆想未提供的信息。
输出必须是严格的 JSON，不要任何解释文字。
"""

VIDEO_ANALYSIS_AUDIT_TEMPLATE = """# 任务
审查下方"原视频分析 JSON"的质量，决定是否通过。

# 审查维度（按阻断级 > 警告级划分）

## 阻断级（critical_issues，命中即 passed=false）
C1. 分镜列表为空或只有 1 个镜头（无法做分镜级复刻）。
C2. 关键字段缺失：`total_duration` / `shots` / 单镜头的 `start_time` / `end_time` / `description` 任一缺失。
C3. 时间轴严重异常：镜头时间重叠、`start_time >= end_time`、或累计时长与 `total_duration` 偏差 > 30%。
C4. 镜头描述整段为占位符 / 空字符串 / 明显乱码。

## 警告级（warnings，不阻断但要记录）
W1. 镜头数异常多（>15）或异常少（2-3 个可继续但偏少）。
W2. 有镜头时长 < 0.5s 或 > 30s。
W3. 音轨信息完全缺失（`audio` / `voiceover` / `ost` 字段全部为空或未提及）。
W4. OST / 屏幕文字识别看起来像自动截断（全是省略号或重复字符）。
W5. 个别镜头 description 过短（< 10 字符）。

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
- 0.95+：全部字段齐全、时长对齐、分镜合理
- 0.85-0.94：无阻断，有少量 warnings
- 0.70-0.84：有 warnings 但未触发 critical；下游可尝试使用
- <0.70：存在 critical_issues 或多个 warnings

---

# 待审查的原视频分析 JSON
{video_analysis}
"""


def format_video_analysis_audit_prompt(video_analysis: str) -> str:
    """构造 1.1 审查 prompt"""
    return VIDEO_ANALYSIS_AUDIT_SYSTEM + "\n" + VIDEO_ANALYSIS_AUDIT_TEMPLATE.format(
        video_analysis=video_analysis or "{}"
    )
