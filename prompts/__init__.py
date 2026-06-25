"""
提示词模板模块
存放各类 AI 提示词模板

支持从外部文件加载 prompt（优先级：外部文件 > 内置常量）
"""

import os
import logging
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


def _load_prompt_from_file(prompt_name: str, default_prompt: str) -> str:
    """
    从外部文件加载 prompt，如果不存在则返回默认值

    Args:
        prompt_name: prompt 文件名（不含扩展名）
        default_prompt: 默认 prompt 内容

    Returns:
        prompt 内容
    """
    prompts_dir = settings.PROMPTS_DIR
    if not prompts_dir:
        return default_prompt

    file_path = Path(prompts_dir) / f"{prompt_name}.txt"
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            logger.info(f"Loaded prompt '{prompt_name}' from external file: {file_path}")
            return content
        except Exception as e:
            logger.warning(f"Failed to load prompt from {file_path}: {e}, using default")
            return default_prompt
    else:
        return default_prompt


# 导入内置 prompt
from .video_analysis import VIDEO_ANALYSIS_PROMPT as _VIDEO_ANALYSIS_PROMPT
from .product_analysis import PRODUCT_ANALYSIS_PROMPT as _PRODUCT_ANALYSIS_PROMPT
from .script_replication import SCRIPT_REPLICATION_PROMPT as _SCRIPT_REPLICATION_PROMPT
from .prompt_conversion import PROMPT_CONVERSION_PROMPT as _PROMPT_CONVERSION_PROMPT
from .product_listing_extraction import PRODUCT_LISTING_EXTRACTION_PROMPT as _PRODUCT_LISTING_EXTRACTION_PROMPT
from .action_compatibility import ACTION_COMPATIBILITY_PROMPT as _ACTION_COMPATIBILITY_PROMPT
from .script_validation import SCRIPT_VALIDATION_PROMPT as _SCRIPT_VALIDATION_PROMPT
from .script_fix import SCRIPT_FIX_PROMPT as _SCRIPT_FIX_PROMPT
from .shot_prompt_audit import SHOT_PROMPT_AUDIT_PROMPT as _SHOT_PROMPT_AUDIT_PROMPT
from .constraints import (
    PRODUCT_INTEGRITY_PROMPT as _PRODUCT_INTEGRITY_PROMPT,
    CINEMATOGRAPHY_PROMPT as _CINEMATOGRAPHY_PROMPT,
    NARRATIVE_PROMPT as _NARRATIVE_PROMPT,
    REPLACEMENT_BOUNDARY_PROMPT as _REPLACEMENT_BOUNDARY_PROMPT,
)

# 加载 prompt（优先从外部文件加载）
VIDEO_ANALYSIS_PROMPT = _load_prompt_from_file("video_analysis", _VIDEO_ANALYSIS_PROMPT)
PRODUCT_ANALYSIS_PROMPT = _load_prompt_from_file("product_analysis", _PRODUCT_ANALYSIS_PROMPT)
SCRIPT_REPLICATION_PROMPT = _load_prompt_from_file("script_replication", _SCRIPT_REPLICATION_PROMPT)
PROMPT_CONVERSION_PROMPT = _load_prompt_from_file("prompt_conversion", _PROMPT_CONVERSION_PROMPT)
PRODUCT_INTEGRITY_PROMPT = _load_prompt_from_file("product_integrity", _PRODUCT_INTEGRITY_PROMPT)
CINEMATOGRAPHY_PROMPT = _load_prompt_from_file("cinematography", _CINEMATOGRAPHY_PROMPT)
NARRATIVE_PROMPT = _load_prompt_from_file("narrative", _NARRATIVE_PROMPT)
REPLACEMENT_BOUNDARY_PROMPT = _load_prompt_from_file("replacement_boundary", _REPLACEMENT_BOUNDARY_PROMPT)
PRODUCT_LISTING_EXTRACTION_PROMPT = _load_prompt_from_file("product_listing_extraction", _PRODUCT_LISTING_EXTRACTION_PROMPT)
ACTION_COMPATIBILITY_PROMPT = _load_prompt_from_file("action_compatibility", _ACTION_COMPATIBILITY_PROMPT)
SCRIPT_VALIDATION_PROMPT = _load_prompt_from_file("script_validation", _SCRIPT_VALIDATION_PROMPT)
SCRIPT_FIX_PROMPT = _load_prompt_from_file("script_fix", _SCRIPT_FIX_PROMPT)
SHOT_PROMPT_AUDIT_PROMPT = _load_prompt_from_file("shot_prompt_audit", _SHOT_PROMPT_AUDIT_PROMPT)

# 重新导出格式化函数
from .script_replication import format_script_replication_prompt
from .prompt_conversion import format_prompt_conversion
from .product_listing_extraction import format_product_listing_extraction_prompt
from .action_compatibility import format_action_compatibility_prompt
from .script_validation import format_script_validation_prompt
from .script_fix import format_script_fix_prompt
from .shot_prompt_audit import format_shot_prompt_audit_prompt
from .constraints import (
    generate_constraints,
    format_product_integrity_prompt,
    format_cinematography_prompt,
    format_narrative_prompt,
    format_replacement_boundary_prompt,
)

__all__ = [
    "VIDEO_ANALYSIS_PROMPT",
    "PRODUCT_ANALYSIS_PROMPT",
    "SCRIPT_REPLICATION_PROMPT",
    "PROMPT_CONVERSION_PROMPT",
    "PRODUCT_INTEGRITY_PROMPT",
    "CINEMATOGRAPHY_PROMPT",
    "NARRATIVE_PROMPT",
    "REPLACEMENT_BOUNDARY_PROMPT",
    "PRODUCT_LISTING_EXTRACTION_PROMPT",
    "ACTION_COMPATIBILITY_PROMPT",
    "format_script_replication_prompt",
    "format_prompt_conversion",
    "format_product_listing_extraction_prompt",
    "format_action_compatibility_prompt",
    "SCRIPT_VALIDATION_PROMPT",
    "SCRIPT_FIX_PROMPT",
    "SHOT_PROMPT_AUDIT_PROMPT",
    "format_script_validation_prompt",
    "format_script_fix_prompt",
    "format_shot_prompt_audit_prompt",
    "generate_constraints",
    "format_product_integrity_prompt",
    "format_cinematography_prompt",
    "format_narrative_prompt",
    "format_replacement_boundary_prompt",
    "_load_prompt_from_file",
]
