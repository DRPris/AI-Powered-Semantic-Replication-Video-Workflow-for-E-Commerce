"""
镜头语言转提示词 Prompt 模板
用于将复刻脚本转换为 AI 视频生成提示词
"""

PROMPT_CONVERSION_PROMPT = """把每个SHOT的导演脚本，转换成以下格式：

─────────────────────────────────
SHOT [X] 生成提示词

【首帧状态 / First Frame】
静态描述当前画面的构图和所有元素的位置状态
（这是图生视频的起始帧，或文生视频的初始画面）

格式要求：
- 用名词短语描述，不用动词
- 从背景到前景逐层描述
- 明确每个元素在画面中的位置

示例：
"Brown car leather seat as background, slightly blurred.
 One hand entering frame from right side, 
 gripping center of horizontal gray water bottle.
 White U-shaped trough faces left, empty.
 White dog head visible at bottom left, looking up."

【运动指令 / Motion】
描述从首帧开始发生的运动，只描述运动本身

格式要求：
- 一次只描述一个主要运动
- 用"从...到..."的结构
- 包含运动速度和幅度

示例：
"Thumb presses circular button on bottle side slowly,
 bottle tilts left 15 degrees,
 clear water flows steadily into U-shaped trough,
 dog lowers head toward trough"

【镜头指令 / Camera】
"Static camera / Slight handheld shake / Slow push in / etc."

【时长 / Duration】
"3.5 seconds"

【关键约束 / Hard Constraints】
列出这个镜头绝对不能出现的内容
"- Bottle shape must not change
 - Water must not splash or overflow
 - Dog must be Westie, white, medium size"

【负面约束 / Negative Constraints】
列出脚本中**没有提及**但 AI 模型可能自行添加的视觉元素，明确禁止生成。

规则：
- 仔细审查脚本描述，识别所有**已提及**的视觉元素
- 对于脚本中**完全未提及**的常见视觉效果（如泡沫、水花、蒸汽、烟雾、火花、光晕、粒子等），必须在此明确禁止
- 对于产品展示/对比类镜头（静态展示、对比展示、无动态效果），必须注明：
  "This is a STATIC product display scene. Do NOT add any dynamic effects, motion blur, particles, foam, bubbles, splashes, steam, smoke, sparkles, lens flares, or any visual effects not explicitly described."
- 遵循 "Only What's Described" 原则：视频内容必须严格限制在脚本描述的范围内，不得自行添加任何额外视觉元素

示例：
"- Do NOT generate foam, lather, or bubbles unless explicitly described
 - Do NOT add water splashes, steam, or smoke effects
 - Do NOT add sparkles, lens flares, or particle effects
 - Only render visual elements explicitly mentioned in the script"
─────────────────────────────────

## 核心原则：Only What's Described

**严格遵守以下原则：生成的视频内容必须且只能包含脚本中明确描述的视觉元素。**
- 如果脚本没有提到泡沫，就绝对不能出现泡沫
- 如果脚本没有提到水花，就绝对不能出现水花
- 如果脚本没有提到蒸汽/烟雾，就绝对不能出现蒸汽/烟雾
- AI 模型倾向于自行"脑补"视觉效果，你必须在约束中明确禁止所有未提及的效果

## 硬约束：Static Display 镜头（scene_type = static_display）

当一个镜头被识别为 **static_display**（如静态产品展示、对比图、CTA 字幕片等）时，必须遵守以下全部规则，禁止任何例外：

1. **motion 字段**：只允许填写类似以下表述之一：
   - `"Static scene. No product interaction. No subject motion."`
   - `"静态展示，无动作。"`
   - 严禁出现任何产品/手/人物/液体/泡沫之间的交互动词
   - 禁用动词示例（包括但不限于）：press / push / rub / scrub / squeeze / shake / tilt / insert / pour / drop / spray / lather / foam / splash / drip / flow / bubble / emerge / dispense / compress / release / bounce / wobble / stretch / bend / twist / swing / rotate / fold / open / close / pump / click
   - 禁用文字/UI 动画（包括但不限于）：fade in / fade out / slide in / slide out / pop up / pop in / zoom in / zoom out / flash / blink / reveal / appear / disappear / typewriter / bounce / glow / pulse

2. **camera 字段**：只允许温和的纯摄影机微动（产品本体必须完全静止）：
   - 允许：Static camera · Very slow push-in · Very slow pull-out · Slight orbit · Subtle parallax
   - 禁止：快速推拉 / handheld shake / whip pan / snap zoom / 任何伴随产品动作的跟随镜头

3. **first_frame 字段**：只允许静态名词短语描述。禁用任何暗含运动的形容词/分词（如 mid-press, mid-pour, foaming, splashing, falling, rising, spinning）。文字内容在静态镜头中必须已经完全显示（非动画过程）。

4. **constraints 字段**：必须包含至少 2 条鉴定性硬约束，例如：
   - `"Static display scene — product and all subjects must remain completely still"`
   - `"Text and graphics must appear as final state only — no fade-in, slide-in, or any animation"`

5. **negative_constraints 字段**：必须包含：
   - `"This is a STATIC product display scene. Do NOT add any dynamic effects, motion blur, particles, foam, bubbles, splashes, steam, smoke, sparkles, lens flares, or any visual effects not explicitly described."`
   - `"Do NOT animate any text or graphic elements (no fade-in/out, no slide-in, no pop-in, no typewriter effect)."`
   - `"Do NOT introduce any hand-product interaction, finger press, grip adjustment, or subject motion."`

❗ **判定标准：若你为一个 scene_type=static_display 的镜头写出了包含上述任何禁用动词的 motion，该镜头会被下游审核模型判为 C3 static-vs-dynamic 冲突自动驳回。**

## 硬约束：镜头内环境音（audio.ambient）—— 三步决策法

每个镜头必须产出一个英文、≤20 词的 `audio.ambient` 字段。这是 ElevenLabs Sound Effects 的直接输入，用于为无声视频混入匹配新场景的现场环境声。

### 三步决策规则（按顺序执行）

**Step 1. 判定新场景类型**  
基于本镜头 `first_frame` 描述 + 下方「新商品场景上下文」推断新场景类型（bathroom / kitchen / bedroom / office / car_interior / outdoor_urban / outdoor_nature / studio_clean / retail_store / cafe / gym / 其他）。

**Step 2. 对比原视频参考声场（reference_sound_effects）**  
从下方「原视频分析」里取对应 shot_number 的 `audio.sound_effects` 作为参考：
- **场景匹配（≥70% 重合，如浴室→浴室、厨房→厨房）**：沿用参考声场的具体声源与声场调性（例如原参考是 "water drip, soft echo"，新场景仍是浴室 → 继承为 "soft water drip, gentle bathroom echo"）。
- **场景不匹配（如浴室→车内、厨房→办公桌）**：**完全重新生成**符合新场景的 ambient，**不得**保留原参考中与场景绑定的具体声源（如水声、油烟机声、户外鸟鸣等）。可保留的只是**抽象声场意图**：安静私密 / 生活嘈杂 / 户外通透 / 都市快节奏 / 清冷极简，将其迁移到新场景（如原"安静浴室"迁移到"安静车内" → "quiet car interior, soft fabric rustle"）。

**Step 3. 输出约束**  
- 仅描述**非音乐、非人声、非旁白**的真实环境声（room tone / 物理声响 / 场景背景声）
- 严禁包含：music / song / BGM / score / voiceover / narration / dialogue / speech / singing / humming  
- 与本镜头 `first_frame` 视觉元素**场景一致**，禁止出现视觉里没有的物体声源（如视觉是办公桌，不能出现 "waves crashing"）  
- 纯静物产品展示镜头（scene_type=static_display）→ 固定输出 `"subtle room tone, soft ambience"`  
- 英文、小写、用逗号分隔声源短语、总长 ≤20 词

### 示例

| 原参考声场 | 新商品/新场景 | 输出 audio.ambient |
|---|---|---|
| "running water, splashing, soft bathroom echo" | 同款洗面奶（仍在浴室） | "soft running water, gentle bathroom echo, faint droplets" |
| "kitchen sizzle, frying sound, family chatter" | 办公桌陶瓷杯 | "quiet open office, faint keyboard typing, distant chatter" |
| "outdoor wind, bird chirps" | 车载香薰 | "quiet car interior, soft fabric rustle, faint road hum" |
| "static product showcase" | 任何 static_display | "subtle room tone, soft ambience" |

## 上下文输入

### 复刻脚本（必选）

{replicated_script}

### 原视频分析（参考声场来源，按 shot_number 对齐）

{original_video_analysis}

### 新商品场景上下文（决定新场景类型）

{product_scene_context}

---

请基于以上输入，为每个 SHOT 生成对应的视频生成提示词。

重要：对于每个镜头，请特别注意：
1. 仔细分析脚本描述中**实际提到了哪些视觉元素**
2. 在 negative_constraints 中列出所有**未提及但可能被 AI 错误添加**的视觉效果
3. 如果镜头是静态产品展示/对比（无运动描述或仅有简单展示），在 scene_type 中标记为 "static_display"，并必须严格遵守上文「Static Display 镜头硬约束」中的全部 5 条规则（尤其注意 motion 字段不得含有任何交互/动画词汇）
4. 严格执行上文「镜头内环境音 三步决策法」产出 `audio.ambient` 字段；`audio.scene_match` 取值为 "match" 或 "mismatch"，用于标记本镜头新场景是否与原参考场景匹配

Please output your response in valid JSON format with the following structure:
{{
  "shots": [
    {{
      "shot_number": 1,
      "shot_type": "hook / demo / cta / other",
      "scene_type": "dynamic / static_display",
      "first_frame": "首帧状态描述...",
      "motion": "运动指令描述...",
      "camera": "镜头指令...",
      "duration": "X seconds",
      "constraints": ["约束1", "约束2", ...],
      "negative_constraints": ["Do NOT generate foam or bubbles", "Do NOT add water splashes", ...],
      "audio": {{
        "ambient": "english lowercase ambient phrase, comma separated, <= 20 words",
        "scene_match": "match | mismatch",
        "scene_type": "bathroom | kitchen | bedroom | office | car_interior | outdoor_urban | outdoor_nature | studio_clean | retail_store | cafe | gym | other"
      }}
    }}
  ]
}}
"""


def format_prompt_conversion(
    replicated_script: str,
    original_video_analysis: str = "",
    product_scene_context: str = "",
) -> str:
    """
    格式化提示词转换 prompt

    Args:
        replicated_script: 复刻后的脚本（JSON 字符串或文本）
        original_video_analysis: 原视频分析 JSON（含各 shot 的 audio.sound_effects，
            作为环境音参考声场）。可选，缺失时走"场景不匹配"分支基于新商品场景从零生成。
        product_scene_context: 新商品场景上下文 JSON/文本（品类、使用场景、典型环境）。
            可选，缺失时由模型从脚本中推断。

    Returns:
        格式化后的 prompt 字符串
    """
    return PROMPT_CONVERSION_PROMPT.format(
        replicated_script=replicated_script,
        original_video_analysis=original_video_analysis.strip() or "(not provided, infer from script and apply scene-mismatch branch)",
        product_scene_context=product_scene_context.strip() or "(not provided, infer product scene from script visuals)",
    )
