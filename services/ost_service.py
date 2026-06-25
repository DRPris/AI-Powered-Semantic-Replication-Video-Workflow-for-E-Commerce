"""
OST (On-Screen Text) & 字幕叠加服务

职责：
1. 从视频分析结果提取 OST 数据
2. 从视频分析结果提取字幕数据（voiceover/dialogue）
3. 构建合成视频的时间轴映射
4. 将自然语言位置描述映射为 FFmpeg 坐标
5. 使用 FFmpeg drawtext 滤镜叠加文字到视频上
6. 支持多语言字体自动检测（英文/越南语/泰语等）
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 字体管理
# ---------------------------------------------------------------------------

FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"

# 角色专属字体（按使用场景差异化）
# - ost / ost_headline / ost_accent: Archivo Black — 超粗圆润，屏幕上文字辨识度高
# - cta:       Oswald-Bold   — 轻压缩粗体，长短句都能饱满（CTA 字号已很大，不再需 Archivo Black 叠加视觉量）
# - subtitle:  Archivo Black — 口播长句可读性最好（可通过 SUBTITLE_FONT=anton 切换）
ROLE_FONT_FILES = {
    "ost":          "ArchivoBlack-Regular.ttf",
    "ost_headline": "ArchivoBlack-Regular.ttf",
    "ost_accent":   "ArchivoBlack-Regular.ttf",
    "cta":          "Oswald-Bold.ttf",
    "subtitle":     "ArchivoBlack-Regular.ttf",
}

# 英文主字体优先级（角色专属字体缺失时的 fallback）
FONT_PRIORITY = [
    FONT_DIR / "Poppins-Black.ttf",       # 主字体：几何圆润 + 黑体粗重
    FONT_DIR / "Montserrat-Bold.ttf",     # 备选
]

# 多语言字体映射
LANG_FONT_MAP = {
    "thai":       FONT_DIR / "NotoSansThai-Bold.ttf",
    "vietnamese": FONT_DIR / "NotoSans-Bold.ttf",
    "korean":     FONT_DIR / "NotoSansKR-Bold.ttf",
    "japanese":   FONT_DIR / "NotoSansJP-Bold.ttf",
    "chinese":    FONT_DIR / "NotoSansSC-Bold.ttf",
}

# 系统字体兜底
SYSTEM_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    "/System/Library/Fonts/Helvetica.ttc",                     # macOS
    "C:/Windows/Fonts/arial.ttf",                               # Windows
]

# Unicode 语言检测模式
_LANG_PATTERNS = {
    "thai":       re.compile(r"[\u0E00-\u0E7F]"),
    "vietnamese": re.compile(
        r"[\u00C0-\u00FF\u0100-\u01B0\u1EA0-\u1EF9"
        r"\u0300-\u0303\u0306\u0309\u0323]"
    ),
    "korean":     re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF]"),
    "japanese":   re.compile(r"[\u3040-\u30FF\u31F0-\u31FF]"),
    "chinese":    re.compile(r"[\u4E00-\u9FFF]"),
}


def detect_text_language(text: str) -> str:
    """检测文字中是否包含特殊语言字符，返回语言标识"""
    for lang, pattern in _LANG_PATTERNS.items():
        if pattern.search(text):
            return lang
    return "english"


def _resolve_subtitle_font_file() -> str:
    """根据配置解析字幕字体文件名（延迟导入避免循环依赖）"""
    try:
        from config import settings
        choice = getattr(settings, "SUBTITLE_FONT", "archivo_black").lower().strip()
    except Exception:
        choice = "archivo_black"
    if choice in ("anton", "anton_regular"):
        return "Anton-Regular.ttf"
    return "ArchivoBlack-Regular.ttf"


def get_font_path(text: str = "", role: str = "") -> str:
    """
    按文字内容和角色自动选择字体。

    优先级：
    1. 文字含特殊语言（越南/泰/日/韩/中文） → 语言专属字体
    2. 角色专属字体（按 ROLE_FONT_FILES 映射）
    3. 英文主字体优先级 FONT_PRIORITY
    4. 系统字体兑底
    """
    # 1. 语言检测 → 特殊语言字体
    if text:
        lang = detect_text_language(text)
        if lang != "english":
            lang_font = LANG_FONT_MAP.get(lang)
            if lang_font and os.path.exists(lang_font):
                return str(lang_font)

    # 2. 角色专属字体
    if role:
        if role == "subtitle":
            # 字幕字体通过配置切换（archivo_black / anton）
            subtitle_file = _resolve_subtitle_font_file()
            subtitle_fp = FONT_DIR / subtitle_file
            if subtitle_fp.exists():
                return str(subtitle_fp)
        else:
            role_file = ROLE_FONT_FILES.get(role)
            if role_file:
                role_fp = FONT_DIR / role_file
                if role_fp.exists():
                    return str(role_fp)

    # 3. 英文主字体
    for fp in FONT_PRIORITY:
        if os.path.exists(fp):
            return str(fp)

    # 4. 系统字体
    for fp in SYSTEM_FONTS:
        if os.path.exists(fp):
            return str(fp)

    return ""


# ---------------------------------------------------------------------------
# 位置坐标映射
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 位置坐标映射（抖音/TikTok 竖屏安全区）
# ---------------------------------------------------------------------------
#
# 抖音/TikTok 9:16 竖屏 UI 遮挡区：
#   - 顶部状态栏 + 搜索按钮 ≈ 视频高度 10-12%
#   - 底部文案/作者/分享按钮 ≈ 视频高度 22-28%
#   - 右侧互动按钮列（点赞/评论/分享） ≈ 100-140px
#
# 所有坐标改用比例表达式，自适应各种分辨率。

_SAFE_TOP_Y = "h*0.12"             # 顶部安全区上缘（12% 高度）
_SAFE_BOTTOM_Y = "h*0.72-text_h"   # OST 底部安全区上缘（72% 高度，给字幕留出空间）
_SUBTITLE_Y = "h*0.80-text_h"      # 字幕位置（比 OST bottom 更低，仍在底部安全区内）
_SAFE_SIDE_PAD = 40                 # 左右内边距（像素）
_SAFE_RIGHT_PAD = 140               # 右侧规避互动按钮列

POSITION_MAP = {
    # 顶部（在安全区上缘）
    "top left":      {"x": str(_SAFE_SIDE_PAD),               "y": _SAFE_TOP_Y},
    "top center":    {"x": "(w-text_w)/2",                    "y": _SAFE_TOP_Y},
    "top right":     {"x": f"w-text_w-{_SAFE_RIGHT_PAD}",     "y": _SAFE_TOP_Y},
    # 中部
    "center left":   {"x": str(_SAFE_SIDE_PAD),               "y": "(h-text_h)/2"},
    "center":        {"x": "(w-text_w)/2",                    "y": "(h-text_h)/2"},
    "center right":  {"x": f"w-text_w-{_SAFE_RIGHT_PAD}",     "y": "(h-text_h)/2"},
    # 底部（在安全区上缘）
    "bottom left":   {"x": str(_SAFE_SIDE_PAD),               "y": _SAFE_BOTTOM_Y},
    "bottom center": {"x": "(w-text_w)/2",                    "y": _SAFE_BOTTOM_Y},
    "bottom right":  {"x": f"w-text_w-{_SAFE_RIGHT_PAD}",     "y": _SAFE_BOTTOM_Y},
    # 简写
    "top":           {"x": "(w-text_w)/2",                    "y": _SAFE_TOP_Y},
    "bottom":        {"x": "(w-text_w)/2",                    "y": _SAFE_BOTTOM_Y},
}

# "相对商品" 类位置描述关键词（FFmpeg 无法直接表达）
# 命中时 fallback 到顶部安全区居中，避免放在中心遮挡商品本身。
_PRODUCT_RELATIVE_KEYWORDS = [
    # 独立关键词（不含介词结构，直接子串包含即命中）
    "floating", "callout", "sticker",
    "pointing at", "annotation", "speech bubble", "thought bubble",
    "product area", "subject area",
]

# 介词短语正则：匹配 "near/around/above/below/on/beside/next to ... (the/this) ... product/subject/item"
_PRODUCT_RELATIVE_PATTERN = re.compile(
    r"\b(?:near|around|above|below|over|under|beside|on|atop|next\s+to)\s+"
    r"(?:the\s+|this\s+|that\s+)?(?:product|subject|item|object)s?\b",
    flags=re.IGNORECASE,
)


def is_product_relative_position(position_desc: str) -> bool:
    """
    判断 position 描述是否为“相对商品”类型（无法用固定坐标表达）。
    如 "Near product", "Floating above product", "Callout near subject",
        "Above the product", "Next to the item"。
    """
    if not position_desc:
        return False
    lower = position_desc.lower()
    # 规则1：独立关键词直接包含
    if any(kw in lower for kw in _PRODUCT_RELATIVE_KEYWORDS):
        return True
    # 规则2：介词短语 + 商品名词
    if _PRODUCT_RELATIVE_PATTERN.search(lower):
        return True
    return False


# ---------------------------------------------------------------------------
# OST 子类型分类（基于位置 + 时长）
# ---------------------------------------------------------------------------

# 顶部位置识别关键词
_TOP_POSITION_KEYWORDS = ("top", "upper", "header", "top-", "upper-")

# 长期 / 短时阈值（秒）
_HEADLINE_MIN_DURATION_SEC = 3.0   # ≧ 3s 视为顶部长期
_ACCENT_MAX_DURATION_SEC = 2.0     # < 2s 视为短时辅助

# timing 字段格式：“00:01-00:03” / “0:00 - 0:02.5” / “00:00:01-00:00:03”
# 两段格式下第二段是秒（允许小数）；三段格式下第三段是秒
_TIMING_PATTERN = re.compile(
    r"(?P<h1>\d+):(?P<m1>\d+(?:\.\d+)?)(?::(?P<s1>\d+(?:\.\d+)?))?\s*[-\u2013\u2014to\u2192\uff5e~]+\s*"
    r"(?P<h2>\d+):(?P<m2>\d+(?:\.\d+)?)(?::(?P<s2>\d+(?:\.\d+)?))?",
    flags=re.IGNORECASE,
)


def _timing_to_seconds(parts: tuple[Optional[str], Optional[str], Optional[str]]) -> float:
    """将 (h, m, s) 转为秒数。若只有两段则视为 mm:ss（第二段允许小数）；三段则为 hh:mm:ss。"""
    h, m, s = parts
    # 只有两段数字 (mm:ss)——第二段是秒，允许小数
    if s is None:
        return int(h or 0) * 60 + float(m or 0)
    # 三段 (hh:mm:ss)
    return int(h or 0) * 3600 + int(float(m or 0)) * 60 + float(s or 0)


def parse_timing_duration(timing: str) -> float:
    """
    解析 timing 字符串成秒数（end - start）。

    支持格式：
    - "00:01-00:03"   → 2.0
    - "0:00 - 0:02.5" → 2.5
    - "00:00:05-00:00:08.2" → 3.2

    无法解析或非法区间 → 0.0
    """
    if not timing:
        return 0.0
    m = _TIMING_PATTERN.search(timing)
    if not m:
        return 0.0
    start = _timing_to_seconds((m.group("h1"), m.group("m1"), m.group("s1")))
    end = _timing_to_seconds((m.group("h2"), m.group("m2"), m.group("s2")))
    return max(0.0, end - start)


def is_top_position(position_desc: str) -> bool:
    """判断位置描述是否为顶部区域（顶部安全区）。"""
    if not position_desc:
        return False
    desc = position_desc.lower().strip()
    # 商品相对类——虽然会降级到顶部居中，但语义上它不是“顶部 OST”
    if is_product_relative_position(desc):
        return False
    # 以顶部关键词开头，或出现在描述开头多词中
    for kw in _TOP_POSITION_KEYWORDS:
        if desc.startswith(kw):
            return True
    return False


def classify_ost_role(entry: dict) -> str:
    """
    根据 OST 的 shot_type / position / timing 按位置+时长维度分类。

    优先级：
    1. shot_type == "cta" → "cta"
    2. 顶部位置 + 时长 ≧ 3.0s → "ost_headline"
    3. 商品附近位置 + 0 < 时长 < 2.0s → "ost_accent"
    4. 默认 → "ost"
    """
    if (entry or {}).get("shot_type") == "cta":
        return "cta"

    position = (entry or {}).get("position") or ""
    timing = (entry or {}).get("timing") or ""
    duration = parse_timing_duration(timing)

    # 顶部 + 长期 → headline
    if is_top_position(position) and duration >= _HEADLINE_MIN_DURATION_SEC:
        return "ost_headline"

    # 商品附近 + 短时 → accent（duration=0 时视为信息不足不分类为 accent）
    if is_product_relative_position(position) and 0 < duration < _ACCENT_MAX_DURATION_SEC:
        return "ost_accent"

    return "ost"


def resolve_position(position_desc: str) -> dict[str, str]:
    """
    将自然语言位置描述解析为 FFmpeg 坐标。

    解析优先级：
    1. “相对商品”类描述 → 安全降级到顶部安全区居中（不遮挡商品）
    2. 匹配 POSITION_MAP 中的精确位置描述（按 key 长度倒序，优先匹配更精确的）
    3. 均未命中 → 默认底部居中
    """
    desc = (position_desc or "").lower().strip()

    # 1. 商品相对位置 → 降级到顶部安全区居中
    if is_product_relative_position(desc):
        logger.info(
            f"product-relative position '{position_desc}' → fallback 到 top center【安全区内】"
        )
        return POSITION_MAP["top center"]

    # 2. 精确匹配
    for key in sorted(POSITION_MAP, key=len, reverse=True):
        if key in desc:
            return POSITION_MAP[key]

    # 3. 兑底
    return POSITION_MAP["bottom center"]


# ---------------------------------------------------------------------------
# 字号计算（基于视频高度比例）
# ---------------------------------------------------------------------------

# 样式预设（参照 video-subtitle skill 的 styles.md）
# fontsize_ratio: 字号占视频高度的比例
#
# OST 子类型分工（按位置 + 出现时长自动分类）：
# - ost          : 默认型，6% 视频高度
# - ost_headline : 顶部长期出现（如品牌 slogan / 标题标签），沿用默认字号但描边更粗，更稳定明显
# - ost_accent   : 商品附近短时辅助标签（如“NEW / HOT” 贴纸），字号略小且不抢戏
STYLE_PRESETS = {
    "ost": {       # 默认 OST：6% 视频高度，白字黑边，克制不抢眼
        "fontsize_ratio": 0.06,
        "fontcolor": "white",
        "borderw": 2,
        "bordercolor": "black@0.6",
        "shadowx": 1,
        "shadowy": 1,
        "shadowcolor": "black@0.3",
        "fade_in": 0.3,
        "fade_out": 0.3,
    },
    "ost_headline": {  # 顶部长期出现：字号略大，描边更粗，稳定明显
        "fontsize_ratio": 0.06,
        "fontcolor": "white",
        "borderw": 3,
        "bordercolor": "black@0.75",
        "shadowx": 2,
        "shadowy": 2,
        "shadowcolor": "black@0.45",
        "fade_in": 0.4,
        "fade_out": 0.4,
    },
    "ost_accent": {    # 商品附近短时辅助：字号明显偏小，淡入淡出更快
        "fontsize_ratio": 0.045,
        "fontcolor": "white",
        "borderw": 2,
        "bordercolor": "black@0.55",
        "shadowx": 1,
        "shadowy": 1,
        "shadowcolor": "black@0.25",
        "fade_in": 0.2,
        "fade_out": 0.2,
    },
    "cta": {       # CTA：字号 = 视频高度 × 10%，强行动召唤
        "fontsize_ratio": 0.10,
        "fontcolor": "white",
        "borderw": 4,
        "bordercolor": "black@0.8",
        "shadowx": 2,
        "shadowy": 2,
        "shadowcolor": "black@0.5",
        "fade_in": 0.3,
        "fade_out": 0.3,
    },
    "subtitle": {  # ← modern 风格：半透明背景框，纯净易读
        "fontsize_ratio": 0.04,
        "fontcolor": "white",
        "boxcolor": "black@0.5",
        "boxborderw": 8,
        "fade_in": 0.2,
        "fade_out": 0.2,
    },
}


def adaptive_fontsize(base_size: int, video_height: int) -> int:
    """按视频高度自适应字号，基准: 480p → base_size px"""
    return max(12, int(base_size * video_height / 480))


def clamp_fontsize_by_width(
    fontsize: int,
    text: str,
    video_width: int,
    padding: int = 40,
) -> int:
    """
    根据视频宽度约束字号，避免文字横向溢出画面边界。

    估算字符宽度系数（相对于 fontsize）:
    - CJK 字符（中日韩）: 1.0
    - 英文/拉丁: 0.55
    - 混合: 按比例加权
    """
    if not text or video_width <= 0:
        return fontsize

    # 去除 FFmpeg 转义符 / emoji 用于估算字符数
    clean = _EMOJI_RE.sub("", text)
    clean = re.sub(r"[\\':]", "", clean)
    if not clean:
        return fontsize

    # 统计 CJK 比例
    cjk_count = sum(
        1 for c in clean
        if "\u4e00" <= c <= "\u9fff"
        or "\u3040" <= c <= "\u30ff"
        or "\uac00" <= c <= "\ud7af"
    )
    total = len(clean)
    cjk_ratio = cjk_count / total
    avg_width_ratio = 1.0 * cjk_ratio + 0.55 * (1 - cjk_ratio)

    available = max(video_width - 2 * padding, 100)
    max_fontsize = int(available / (total * avg_width_ratio))

    return max(12, min(fontsize, max_fontsize))


def smart_fontsize(
    text: str,
    video_height: int,
    video_width: int = 0,
    role: str = "ost",
) -> int:
    """
    根据文字内容、视频分辨率、角色自动判断最佳字号。

    核心原则：合适且不遮挡主要视频内容，不横向溢出画面。

    字号 = video_height × fontsize_ratio × 长度缩放系数
    若提供 video_width，额外按宽度约束，避免竖屏视频文字溢出。

    长度缩放：
    - ≤10 字符  → 100%
    - 10-25 字符 → 线性缩至 80%
    - 25-50 字符 → 线性缩至 65%
    - >50 字符   → 65%
    """
    preset = STYLE_PRESETS.get(role, STYLE_PRESETS["ost"])
    ratio = preset["fontsize_ratio"]

    # 基础字号 = 视频高度 × 比例
    base_size = int(video_height * ratio)

    # 按文字长度缩放
    char_count = len(text)
    if char_count <= 10:
        length_ratio = 1.0
    elif char_count <= 25:
        length_ratio = 1.0 - 0.2 * (char_count - 10) / 15
    elif char_count <= 50:
        length_ratio = 0.8 - 0.15 * (char_count - 25) / 25
    else:
        length_ratio = 0.65

    result = max(12, int(base_size * length_ratio))

    # 按视频宽度二次约束，避免文字超出画面
    if video_width > 0:
        result = clamp_fontsize_by_width(result, text, video_width)

    return result


# ---------------------------------------------------------------------------
# OST 数据提取
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Meta 描述过滤（识别 Gemini 对装饰元素的位置描述）
# ---------------------------------------------------------------------------

_META_POSITION_KEYWORDS = [
    "top left", "top right", "top center", "top-left", "top-right",
    "bottom left", "bottom right", "bottom center",
    "bottom-left", "bottom-right",
    "center left", "center right", "left side", "right side",
    "upper left", "upper right", "lower left", "lower right",
    "right of frame", "left of frame", "right center", "left center",
]

# 装饰元素关键词（平台水印、角标等非真实 OST）
_META_ELEMENT_KEYWORDS = [
    "logo", "badge", "watermark", "icon", "overlay",
    "fixed overlay", "brand mark",
]


def is_meta_description(content: str, position: str = "") -> bool:
    """
    判断 content 是否是 Gemini 输出的 meta 描述（装饰元素位置列表），
    而非真实的屏幕文字内容。

    典型 meta 模式:
    - "Lazada logo (Top Left), Promotion badge (Right center)"
    - "Brand logo (top), Promotion (bottom)"
    - position 字段为 "Fixed overlays" 或多位置描述

    判定规则（满足任一）:
    1. 括号数 ≥ 2 且含位置关键词 → meta 列表
    2. 含装饰元素关键词 + 括号位置标注 → meta
    3. position 字段含 "fixed overlay" / "multiple" → meta
    """
    if not content:
        return False

    lower_content = content.lower()
    lower_position = position.lower()

    # 规则 3: position 字段提示是多元素标注
    if any(kw in lower_position for kw in ("fixed overlay", "multiple", "various")):
        return True

    # 规则 1: 多个括号
    open_brackets = content.count("(") + content.count("（")
    if open_brackets >= 2:
        return True

    # 规则 2: 括号内含位置关键词 + 装饰元素关键词
    has_bracket = "(" in content or "（" in content
    if has_bracket:
        has_position_kw = any(kw in lower_content for kw in _META_POSITION_KEYWORDS)
        has_element_kw = any(kw in lower_content for kw in _META_ELEMENT_KEYWORDS)
        if has_position_kw or has_element_kw:
            return True

    return False


def extract_ost_entries(video_analysis: dict) -> list[dict]:
    """
    从视频分析结果提取所有有效 OST 条目。

    过滤规则：
    1. 跳过 content 为空/None/n\u002fa 的镜头
    2. 跳过 meta 描述（如 "Logo (Top Left), Badge (Right)"）

    Args:
        video_analysis: Stage 1 的视频分析 JSON（含 shots 数组）

    Returns:
        OST 条目列表，每个元素包含:
        - shot_number, content, position, timing, shot_type
    """
    entries = []
    for shot in video_analysis.get("shots", []):
        ost = shot.get("on_screen_text", {})
        if not ost or not isinstance(ost, dict):
            continue
        content = ost.get("content", "")
        position = ost.get("position", "")
        # 跳过无文字的镜头
        if not content or content.strip().lower() in ("none", "n/a", ""):
            continue
        # 跳过 Gemini 的 meta 描述（装饰元素位置列表）
        if is_meta_description(content, position):
            logger.info(
                f"跳过疑似 meta 描述 (shot {shot.get('shot_number', '?')}): "
                f"{content[:80]}"
            )
            continue
        entries.append({
            "shot_number": shot.get("shot_number", 0),
            "content": content,
            "position": position or "bottom center",
            "timing": ost.get("timing", ""),
            "shot_type": shot.get("action", {}).get("shot_type", "demo"),
        })
    return entries


# ---------------------------------------------------------------------------
# 字幕数据提取
# ---------------------------------------------------------------------------

def extract_subtitle_entries(video_analysis: dict) -> list[dict]:
    """
    从视频分析结果提取所有有效字幕条目（voiceover/dialogue）。

    Args:
        video_analysis: Stage 1 的视频分析 JSON（含 shots 数组）

    Returns:
        字幕条目列表，每个元素包含:
        - shot_number, content, timing
    """
    entries = []
    for shot in video_analysis.get("shots", []):
        audio = shot.get("audio", {})
        if not audio or not isinstance(audio, dict):
            continue
        voiceover = audio.get("voiceover", "")
        # 跳过无旁白的镜头
        if not voiceover or voiceover.strip().lower() in ("none", "n/a", "", "no voiceover", "no dialogue"):
            continue
        entries.append({
            "shot_number": shot.get("shot_number", 0),
            "content": voiceover.strip(),
            "timing": shot.get("timestamp", ""),
        })
    return entries


# ---------------------------------------------------------------------------
# 时间轴映射
# ---------------------------------------------------------------------------

def build_timeline_map(
    approved_shots: list[dict],
    shot_durations: list[float],
    transition_durations: list[float],
) -> dict[int, tuple[float, float]]:
    """
    构建 shot_number → (start_sec, end_sec) 在合成视频中的时间映射。

    Args:
        approved_shots: Airtable 审核通过的分镜列表
        shot_durations: 每个镜头在合成视频中的实际时长(秒)
        transition_durations: 每个镜头前的转场时长(秒)

    Returns:
        {shot_number: (start_in_composed, end_in_composed)}
    """
    timeline: dict[int, tuple[float, float]] = {}
    cursor = 0.0
    for i, (shot, dur, t_dur) in enumerate(
        zip(approved_shots, shot_durations, transition_durations)
    ):
        shot_num = shot.get("fields", {}).get("镜头序号", i + 1)
        start = cursor
        end = cursor + dur - t_dur
        timeline[shot_num] = (start, end)
        cursor = end
    return timeline


def map_ost_to_composed_time(
    ost_entry: dict,
    timeline: dict[int, tuple[float, float]],
) -> tuple[float, float]:
    """
    将 OST 的原始 timing 映射到合成视频时间。

    简化策略：OST 覆盖该镜头在合成视频中的完整时段。
    """
    shot_num = ost_entry["shot_number"]
    shot_start, shot_end = timeline.get(shot_num, (0, 0))
    return (round(shot_start, 3), round(shot_end, 3))


# ---------------------------------------------------------------------------
# Emoji 清理
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001f926-\U0001f937"
    "\U00010000-\U0010FFFF"
    "\u2640-\u2642"
    "\u2600-\u2B55"
    "\u200d\ufe0f"
    "✨✅❌⭐🔥💯🎉🎊👏💪🙌🤝👍"
    "]+",
    flags=re.UNICODE,
)


def sanitize_text_for_drawtext(text: str) -> str:
    """清理文字以确保 FFmpeg drawtext 兼容"""
    # 1. 移除 emoji
    text = _EMOJI_RE.sub("", text)
    # 2. 转义 drawtext 特殊字符
    text = text.replace("'", "'\\''")
    text = text.replace(":", "\\:")
    text = text.replace("\\", "\\\\")
    # 3. 清理多余空格
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# FFmpeg drawtext 渲染
# ---------------------------------------------------------------------------

async def overlay_ost_on_video(
    ffmpeg_bin: str,
    input_path: str,
    ost_items: list[dict[str, Any]],
    output_path: str,
    font_path_override: str = "",
) -> str:
    """
    使用 FFmpeg drawtext 滤镜将 OST 叠加到视频上。

    Args:
        ffmpeg_bin: FFmpeg 可执行文件路径
        input_path: 输入视频文件路径
        ost_items: 已映射好时间和位置的 OST 列表，每个元素结构:
            {
                "content": str,       # 文字内容
                "x": str,             # FFmpeg x 表达式
                "y": str,             # FFmpeg y 表达式
                "start": float,       # 开始时间(秒)
                "end": float,         # 结束时间(秒)
                "fontsize": int,      # 字号
                "fontcolor": str,     # 字色
                "borderw": int,       # 描边宽度
                "bordercolor": str,   # 描边颜色
                "fade_in": float,     # 淡入时长
                "fade_out": float,    # 淡出时长
            }
        output_path: 输出视频路径
        font_path_override: 强制指定字体路径（覆盖自动检测）

    Returns:
        输出视频路径
    """
    if not ost_items:
        return input_path

    # 构建 drawtext filter chain
    filters = []
    for item in ost_items:
        text = sanitize_text_for_drawtext(item["content"])
        if not text:
            continue

        parts = [
            f"drawtext=text='{text}'",
            f"x={item['x']}",
            f"y={item['y']}",
            f"fontsize={item.get('fontsize', 36)}",
            f"fontcolor={item.get('fontcolor', 'white')}",
            f"enable='between(t,{item['start']},{item['end']})'",
        ]
        
        # 字幕样式：半透明背景框（box），无描边
        if item.get("is_subtitle"):
            parts.append("box=1")
            parts.append(f"boxcolor={item.get('boxcolor', 'black@0.5')}")
            parts.append(f"boxborderw={item.get('boxborderw', 8)}")
        else:
            # OST 样式：描边 + 阴影
            parts.append(f"borderw={item.get('borderw', 2)}")
            parts.append(f"bordercolor={item.get('bordercolor', 'black@0.6')}")
            if item.get('shadowx') or item.get('shadowy'):
                parts.append(f"shadowx={item.get('shadowx', 1)}")
                parts.append(f"shadowy={item.get('shadowy', 1)}")
                parts.append(f"shadowcolor={item.get('shadowcolor', 'black@0.3')}")

        # 字体（按角色 + 文字内容自动选择）
        item_role = "subtitle" if item.get("is_subtitle") else item.get("role", "ost")
        font = font_path_override or get_font_path(item["content"], role=item_role)
        if font:
            parts.append(f"fontfile='{font}'")

        # 淡入淡出效果
        fade_in = item.get("fade_in", 0.3)
        fade_out = item.get("fade_out", 0.3)
        if fade_in > 0 or fade_out > 0:
            alpha_expr = []
            if fade_in > 0:
                alpha_expr.append(
                    f"if(lt(t-{item['start']},{fade_in}),"
                    f"(t-{item['start']})/{fade_in},1)"
                )
            if fade_out > 0:
                alpha_expr.append(
                    f"if(gt(t,{item['end']}-{fade_out}),"
                    f"({item['end']}-t)/{fade_out},1)"
                )
            if len(alpha_expr) == 2:
                full_alpha = f"alpha='min({alpha_expr[0]},{alpha_expr[1]})'"
            else:
                full_alpha = f"alpha='{alpha_expr[0]}'"
            parts.append(full_alpha)

        filters.append(":".join(parts))

    if not filters:
        logger.info("OST 清理后无有效文字，跳过叠加")
        return input_path

    filter_complex = ",".join(filters)

    cmd = [
        ffmpeg_bin, "-y", "-nostdin",
        "-i", input_path,
        "-vf", filter_complex,
        "-c:a", "copy",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ]

    logger.info(f"执行 OST 叠加: {len(filters)} 条文字, 输入: {input_path}")
    import subprocess
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True,
        timeout=300,
    )

    if result.returncode != 0:
        logger.error(f"FFmpeg drawtext 失败: {result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg drawtext 失败: {result.stderr[:500]}")

    logger.info(f"OST 叠加完成: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# 一站式入口：从视频分析到 OST 叠加
# ---------------------------------------------------------------------------

async def apply_ost_overlay(
    ffmpeg_bin: str,
    input_video_path: str,
    output_video_path: str,
    video_analysis: dict,
    approved_shots: list[dict],
    shot_durations: list[float],
    transition_durations: list[float],
    video_height: int = 480,
    video_width: int = 0,
    enable_subtitle: bool = True,
) -> Optional[str]:
    """
    一站式 OST + 字幕叠加入口。

    从视频分析数据提取 OST 和字幕，映射时间轴，生成叠加视频。
    OST 和字幕合并在同一次 FFmpeg 渲染中完成，避免二次编码。

    Args:
        ffmpeg_bin: FFmpeg 路径
        input_video_path: 合成后的视频（拼接完成，尚未上传）
        output_video_path: OST 叠加后的输出路径
        video_analysis: Stage 1 视频分析结果 dict
        approved_shots: 审核通过的 Airtable 分镜列表
        shot_durations: 各镜头在合成视频中的实际时长
        transition_durations: 各镜头前的转场时长
        video_height: 视频高度（用于自适应字号）
        enable_subtitle: 是否启用字幕叠加

    Returns:
        叠加后的视频路径，若无 OST 且无字幕则返回 None
    """
    # 1. 提取 OST
    ost_entries = extract_ost_entries(video_analysis)
    logger.info(f"提取到 {len(ost_entries)} 条 OST")

    # 2. 提取字幕
    subtitle_entries = []
    if enable_subtitle:
        subtitle_entries = extract_subtitle_entries(video_analysis)
        logger.info(f"提取到 {len(subtitle_entries)} 条字幕")

    if not ost_entries and not subtitle_entries:
        logger.info("视频分析中未找到有效 OST 或字幕，跳过叠加")
        return None

    if ost_entries:
        logger.info(f"OST 内容: {[e['content'][:30] for e in ost_entries]}")
    if subtitle_entries:
        logger.info(f"字幕内容: {[e['content'][:30] for e in subtitle_entries]}")

    # 3. 构建时间轴映射
    timeline = build_timeline_map(
        approved_shots, shot_durations, transition_durations
    )

    # 4. 组装 ost_items（OST + 字幕合并）
    all_items = []

    # 4a. OST 条目
    for entry in ost_entries:
        start, end = map_ost_to_composed_time(entry, timeline)
        if end <= start:
            logger.warning(f"OST 时间无效 [{start:.2f}, {end:.2f}]，跳过: {entry['content'][:30]}")
            continue

        pos = resolve_position(entry["position"])

        # 智能字号：根据文字长度、镜头类型、视频宽高自动计算
        # OST 角色分类：cta > ost_headline(顶部长期) > ost_accent(商品附近短时) > ost(默认)
        role = classify_ost_role(entry)
        style = STYLE_PRESETS.get(role, STYLE_PRESETS["ost"])
        fontsize = smart_fontsize(
            entry["content"], video_height,
            video_width=video_width, role=role,
        )
        logger.debug(
            f"OST shot_{entry.get('shot_number')} role={role} "
            f"(position='{entry.get('position')}', timing='{entry.get('timing')}', "
            f"duration={parse_timing_duration(entry.get('timing', '')):.2f}s)"
        )

        all_items.append({
            "content": entry["content"],
            "x": pos["x"],
            "y": pos["y"],
            "start": start,
            "end": end,
            "fontsize": fontsize,
            "fontcolor": style["fontcolor"],
            "borderw": style.get("borderw", 2),
            "bordercolor": style.get("bordercolor", "black@0.6"),
            "shadowx": style.get("shadowx", 1),
            "shadowy": style.get("shadowy", 1),
            "shadowcolor": style.get("shadowcolor", "black@0.3"),
            "fade_in": style["fade_in"],
            "fade_out": style["fade_out"],
            "is_subtitle": False,
            "role": role,
        })

    # 4b. 字幕条目
    for entry in subtitle_entries:
        start, end = map_ost_to_composed_time(entry, timeline)
        if end <= start:
            logger.warning(f"字幕时间无效 [{start:.2f}, {end:.2f}]，跳过: {entry['content'][:30]}")
            continue

        # 字幕位置：底部安全区内部（h*0.80），比 OST bottom (h*0.72) 更低
        sub_style = STYLE_PRESETS["subtitle"]
        subtitle_fontsize = smart_fontsize(
            entry["content"], video_height,
            video_width=video_width, role="subtitle",
        )

        all_items.append({
            "content": entry["content"],
            "x": "(w-text_w)/2",
            "y": _SUBTITLE_Y,
            "start": start,
            "end": end,
            "fontsize": subtitle_fontsize,
            "fontcolor": sub_style["fontcolor"],
            "boxcolor": sub_style["boxcolor"],
            "boxborderw": sub_style["boxborderw"],
            "fade_in": sub_style["fade_in"],
            "fade_out": sub_style["fade_out"],
            "is_subtitle": True,
        })

    if not all_items:
        logger.info("映射后无有效 OST/字幕条目，跳过叠加")
        return None

    # 5. 执行叠加
    result = await overlay_ost_on_video(
        ffmpeg_bin=ffmpeg_bin,
        input_path=input_video_path,
        ost_items=all_items,
        output_path=output_video_path,
    )

    ost_count = sum(1 for i in all_items if not i.get("is_subtitle"))
    sub_count = sum(1 for i in all_items if i.get("is_subtitle"))
    logger.info(f"OST+字幕叠加成功: {ost_count} 条 OST + {sub_count} 条字幕 → {result}")
    return result
