"""Service package with lazy exports.

Importing a lightweight service such as ``services.job_manager`` must not load
all model, image, storage, and network dependencies as a side effect.
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "GeminiService": (".gemini_service", "GeminiService"),
    "QwenService": (".qwen_service", "QwenService"),
    "VideoGenService": (".video_gen_service", "VideoGenService"),
    "VideoModel": (".video_gen_service", "VideoModel"),
    "KlingOfficialService": (".kling_official_service", "KlingOfficialService"),
    "AirtableService": (".airtable_service", "AirtableService"),
    "FFmpegService": (".ffmpeg_service", "FFmpegService"),
    "OSSService": (".oss_service", "OSSService"),
    "JobManager": (".job_manager", "JobManager"),
    "JobStatus": (".job_manager", "JobStatus"),
    "get_job_manager": (".job_manager", "get_job_manager"),
    "get_cache_key": (".token_utils", "get_cache_key"),
    "get_cache": (".token_utils", "get_cache"),
    "set_cache": (".token_utils", "set_cache"),
    "estimate_tokens": (".token_utils", "estimate_tokens"),
    "log_token_usage": (".token_utils", "log_token_usage"),
    "compress_video_analysis": (".token_utils", "compress_video_analysis"),
    "compress_product_analysis": (".token_utils", "compress_product_analysis"),
    "compress_script_for_constraints": (".token_utils", "compress_script_for_constraints"),
    "extract_key_product_features": (".token_utils", "extract_key_product_features"),
    "extract_key_video_style": (".token_utils", "extract_key_video_style"),
    "remove_background": (".image_utils", "remove_background"),
    "remove_background_rembg": (".image_utils", "remove_background_rembg"),
    "remove_background_api": (".image_utils", "remove_background_api"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "GeminiService",
    "QwenService",
    "VideoGenService",
    "VideoModel",
    "KlingOfficialService",
    "AirtableService",
    "FFmpegService",
    "OSSService",
    "JobManager",
    "JobStatus",
    "get_job_manager",
    # Token 优化工具
    "get_cache_key",
    "get_cache",
    "set_cache",
    "estimate_tokens",
    "log_token_usage",
    "compress_video_analysis",
    "compress_product_analysis",
    "compress_script_for_constraints",
    "extract_key_product_features",
    "extract_key_video_style",
    # 图像处理工具
    "remove_background",
    "remove_background_rembg",
    "remove_background_api",
]
