"""
镜头内环境音 (ambient sound) 关键词抽取 Prompt。

输入：Stage 3 产出的单个镜头视觉提示词 / 场景描述。
输出：一句英文的环境声描述，用于 ElevenLabs Sound Effects API。

原则：
1. 只保留【非音乐、非人声对话】的真实环境声源
   （如 "coffee shop chatter"、"kitchen range hood"、"seaside wind and waves"）
2. 画面为纯静物/特写时输出 "subtle room tone, soft ambience"
3. 单句英文，长度 5-20 词，逗号分隔多个声源
4. 不要包含 BGM / music / voiceover / narration 等指令性词汇
"""

AMBIENT_EXTRACTION_PROMPT = """You are a professional sound designer. Given a single shot's visual prompt
(scene, subject, action, environment) from a short-form product video, extract the most
plausible *ambient* sound description for this shot.

## Rules (strict)
- Focus ONLY on diegetic / in-scene environmental sound sources.
- NEVER include music, BGM, score, voiceover, narration, dialogue, or speech.
- Prefer concrete natural/mechanical sources (e.g. "coffee shop chatter, espresso machine hiss",
  "kitchen range hood humming, oil sizzling", "seaside wind, distant waves", "office keyboard
  typing, HVAC hum").
- If the shot is a pure product close-up / static object with no clear environment,
  output: "subtle room tone, soft ambience".
- Output a SINGLE English phrase, 5-20 words, comma-separated sources, no trailing period.
- Output MUST be valid JSON exactly like: {{"ambient_prompt": "..."}}

## Input shot visual prompt
{visual_prompt}

## Extra context (optional, may be empty)
{extra_context}

## Output
Return ONLY the JSON object, nothing else.
"""


def format_ambient_extraction_prompt(visual_prompt: str, extra_context: str = "") -> str:
    """填充 prompt 模板。"""
    return AMBIENT_EXTRACTION_PROMPT.format(
        visual_prompt=visual_prompt or "(empty)",
        extra_context=extra_context or "(none)",
    )
