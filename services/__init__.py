"""
服务模块
封装各类外部 API 调用
"""

from .gemini_service import GeminiService
from .qwen_service import QwenService
from .video_gen_service import VideoGenService, VideoModel
from .kling_official_service import KlingOfficialService
from .airtable_service import AirtableService
from .ffmpeg_service import FFmpegService
from .oss_service import OSSService
from .job_manager import JobManager, JobStatus, get_job_manager
from .token_utils import (
    get_cache_key, get_cache, set_cache,
    estimate_tokens, log_token_usage,
    compress_video_analysis, compress_product_analysis,
    compress_script_for_constraints,
    extract_key_product_features, extract_key_video_style,
)
from .image_utils import (
    remove_background,
    remove_background_rembg,
    remove_background_api,
)

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
