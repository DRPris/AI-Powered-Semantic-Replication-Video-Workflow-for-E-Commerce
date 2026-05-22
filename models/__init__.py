"""
数据模型模块
"""

from .schemas import (
    ProjectCreate,
    ProjectResponse,
    AssetCreate,
    AssetResponse,
    ShotCreate,
    ShotResponse,
    ReviewCreate,
    ReviewResponse,
    AnalyzeVideoRequest,
    AnalyzeProductRequest,
    GenerateScriptRequest,
    ConvertPromptsRequest,
    GenerateShotsRequest,
    ComposeVideoRequest,
    JobStatusResponse,
)

__all__ = [
    "ProjectCreate",
    "ProjectResponse",
    "AssetCreate",
    "AssetResponse",
    "ShotCreate",
    "ShotResponse",
    "ReviewCreate",
    "ReviewResponse",
    "AnalyzeVideoRequest",
    "AnalyzeProductRequest",
    "GenerateScriptRequest",
    "ConvertPromptsRequest",
    "GenerateShotsRequest",
    "ComposeVideoRequest",
    "JobStatusResponse",
]
