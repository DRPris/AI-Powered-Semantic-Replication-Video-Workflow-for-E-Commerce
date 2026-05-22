"""
Token 优化工具模块
提供缓存、数据压缩、Token 统计等功能
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# 缓存目录
CACHE_DIR = Path(__file__).parent.parent / "tmp" / "cache"


def get_cache_key(content: str, prefix: str = "") -> str:
    """
    生成缓存键
    
    Args:
        content: 用于生成 hash 的内容
        prefix: 缓存键前缀
        
    Returns:
        缓存键字符串
    """
    hash_part = hashlib.md5(content.encode()).hexdigest()[:16]
    return f"{prefix}_{hash_part}" if prefix else hash_part


def get_cache(cache_key: str) -> Optional[dict]:
    """
    从缓存获取数据
    
    Args:
        cache_key: 缓存键
        
    Returns:
        缓存的数据，如果不存在返回 None
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key}.json"
    
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            logger.info(f"[Cache] HIT: {cache_key}")
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[Cache] Read failed for {cache_key}: {e}")
    
    return None


def set_cache(cache_key: str, data: dict) -> None:
    """
    写入缓存
    
    Args:
        cache_key: 缓存键
        data: 要缓存的数据
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key}.json"
    
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[Cache] SET: {cache_key}")
    except IOError as e:
        logger.warning(f"[Cache] Write failed for {cache_key}: {e}")


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数
    英文约 4 字符/token，中文约 2 字符/token
    
    Args:
        text: 输入文本
        
    Returns:
        估算的 token 数
    """
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return chinese_chars // 2 + other_chars // 4


def log_token_usage(context: str, input_data: Any, response: Optional[dict] = None) -> None:
    """
    记录 Token 使用情况
    
    Args:
        context: 调用上下文描述
        input_data: 输入数据
        response: API 响应（可选，用于获取实际 token 数）
    """
    input_str = json.dumps(input_data, ensure_ascii=False) if isinstance(input_data, (dict, list)) else str(input_data)
    estimated_input = estimate_tokens(input_str)
    
    if response:
        usage = response.get("usageMetadata", {})
        actual_input = usage.get("promptTokenCount", "N/A")
        actual_output = usage.get("candidatesTokenCount", "N/A")
        logger.info(f"[Token] {context}: estimated_input={estimated_input}, actual_input={actual_input}, output={actual_output}")
    else:
        logger.info(f"[Token] {context}: estimated_input={estimated_input}")


# =============================================================================
# 数据压缩函数 - 减少传入 AI 的数据量
# =============================================================================

def compress_video_analysis(analysis: dict) -> dict:
    """
    压缩视频分析结果，只保留脚本生成需要的关键字段
    
    预估节省：约 40% token
    """
    if not analysis:
        return {}
    
    shots = analysis.get("shots", [])
    compressed_shots = []
    
    for shot in shots:
        compressed_shots.append({
            "shot_number": shot.get("shot_number"),
            "duration": shot.get("duration"),
            "timestamp": shot.get("timestamp"),
            "frame_composition": shot.get("frame_composition"),
            "action": shot.get("action"),
            # 省略：environment, audio, on_screen_text 等次要字段
            "product_specific_elements": shot.get("product_specific_elements"),
            "scene_elements": shot.get("scene_elements"),
        })
    
    brief = analysis.get("replication_brief", {})
    
    return {
        "shots": compressed_shots,
        "replication_brief": {
            "must_preserve": brief.get("must_preserve"),
            "can_adapt": brief.get("can_adapt"),
        }
    }


def compress_product_analysis(analysis: dict) -> dict:
    """
    压缩产品分析结果，只保留约束生成需要的关键字段
    
    预估节省：约 30% token
    """
    if not analysis:
        return {}
    
    return {
        "name": analysis.get("name"),
        "category": analysis.get("category"),
        "colors": analysis.get("colors"),
        "materials": analysis.get("materials"),
        "features": analysis.get("features"),
        "brand_elements": analysis.get("brand_elements"),
        # 省略：description, dimensions, packaging 等详细字段
    }


def compress_script_for_constraints(script: dict) -> dict:
    """
    压缩脚本数据用于约束生成，只需要镜头级别的简要信息
    
    预估节省：约 60% token
    """
    if not script:
        return {}
    
    shots = script.get("shots", [])
    compressed_shots = []
    
    for shot in shots:
        compressed_shots.append({
            "shot_number": shot.get("shot_number") or shot.get("镜头序号"),
            "duration": shot.get("duration"),
            "camera": shot.get("camera_movement") or shot.get("frame_composition", {}).get("camera_movement"),
            # 省略详细描述
        })
    
    return {
        "total_shots": len(compressed_shots),
        "shots_summary": compressed_shots[:5],  # 只传前5个镜头作为参考
    }


def extract_key_product_features(analysis: dict) -> str:
    """
    从产品分析中提取关键特征，生成简洁的文本描述
    用于约束生成时作为简短参考
    
    Args:
        analysis: 产品分析结果字典
        
    Returns:
        简洁的产品特征描述（约 100-200 字符）
    """
    if not analysis:
        return ""
    
    parts = []
    
    if "name" in analysis:
        parts.append(analysis["name"])
    if "category" in analysis:
        parts.append(f"({analysis['category']})")
    if "colors" in analysis and isinstance(analysis["colors"], list):
        parts.append(f"颜色: {', '.join(analysis['colors'][:3])}")
    if "materials" in analysis and isinstance(analysis["materials"], list):
        parts.append(f"材质: {', '.join(analysis['materials'][:2])}")
    if "brand_elements" in analysis:
        parts.append(f"品牌: {analysis['brand_elements']}")
    
    return " | ".join(parts)


def extract_key_video_style(analysis: dict) -> str:
    """
    从视频分析中提取关键风格信息
    
    Args:
        analysis: 视频分析结果字典
        
    Returns:
        简洁的风格描述（约 100-200 字符）
    """
    if not analysis:
        return ""
    
    brief = analysis.get("replication_brief", {})
    
    parts = []
    if brief.get("must_preserve"):
        must_preserve = brief["must_preserve"]
        if isinstance(must_preserve, list):
            parts.append(f"保留: {', '.join(must_preserve[:3])}")
        else:
            parts.append(f"保留: {str(must_preserve)[:100]}")
    
    shots = analysis.get("shots", [])
    if shots:
        parts.append(f"共{len(shots)}个镜头")
    
    return " | ".join(parts)
