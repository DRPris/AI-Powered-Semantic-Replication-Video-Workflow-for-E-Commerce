"""
Pydantic 数据模型定义
对应 Airtable 表结构和 API 请求/响应模型
"""

from datetime import datetime
from typing import Any, List, Optional
from enum import Enum

from pydantic import BaseModel, Field, model_validator


# ============================================================================
# 枚举类型定义
# ============================================================================

class VideoModel(str, Enum):
    """视频生成模型枚举"""
    KLING_V3 = "kling-3.0"
    KLING_V2_6 = "kling-2.6/image-to-video"  # 图生视频
    KLING_V2_6_TEXT = "kling-2.6/text-to-video"  # 文生视频
    SEEDANCE_2 = "doubao-seedance-2-0-260128"  # Seedance 2.0（火山方舟直连）
    SEEDANCE_1_5 = "bytedance/seedance-1.5-pro"  # Seedance 1.5 Pro（KIE AI）


class ProjectStatus(str, Enum):
    """项目状态枚举 - 与 Airtable 字段选项值保持一致"""
    PENDING = "素材准备中"
    ANALYZING = "ANALYZING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"  # Agent Phase A 完成，等待用户确认 brief
    SCRIPT_GENERATING = "SCRIPT_GENERATING"
    PROMPT_CONVERTING = "PROMPT_CONVERTING"
    KEYFRAME_GENERATING = "KEYFRAME_GENERATING"
    KEYFRAME_REVIEW = "KEYFRAME_REVIEW"
    GENERATING = "GENERATING"
    COMPOSING = "COMPOSING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReplicationMode(str, Enum):
    """复刻模式枚举"""
    SIMPLE = "simple"  # 简单模式：跳过复杂分析，直接调用视频生成API
    FULL = "full"      # 完整模式：走全流程（视频分析→脚本复刻→提示词生成→逐镜头生成→合成）


class ShotType(str, Enum):
    """镜头类型枚举"""
    HOOK = "hook"        # 开场吸引镜头（通常不含产品）
    DEMO = "demo"        # 产品展示/演示镜头
    CTA = "cta"          # 行动号召/结尾镜头
    OTHER = "other"      # 其他类型


class ShotStatus(str, Enum):
    """分镜状态枚举"""
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewStatus(str, Enum):
    """审核状态枚举"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class JobStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    WAITING_REVIEW = "waiting_review"
    WAITING_KEYFRAME_REVIEW = "waiting_keyframe_review"
    COMPLETED = "completed"
    FAILED = "failed"


class AssetType(str, Enum):
    """素材类型枚举"""
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    PRODUCT = "product"
    THREE_VIEW = "三视图"


# ============================================================================
# 项目模型 (Projects)
# ============================================================================

class ProjectCreate(BaseModel):
    """创建项目请求模型"""
    name: str = Field(..., description="项目名称")
    description: Optional[str] = Field(None, description="项目描述")
    original_video_url: Optional[str] = Field(None, description="原始视频URL")
    product_image_url: Optional[str] = Field(None, description="商品图片URL")
    status: ProjectStatus = Field(default=ProjectStatus.PENDING, description="项目状态")
    mode: ReplicationMode = Field(default=ReplicationMode.FULL, description="复刻模式：simple 或 full")


class ProjectResponse(BaseModel):
    """项目响应模型"""
    id: str = Field(..., description="项目ID")
    name: str = Field(..., description="项目名称")
    description: Optional[str] = Field(None, description="项目描述")
    original_video_url: Optional[str] = Field(None, description="原始视频URL")
    product_image_url: Optional[str] = Field(None, description="商品图片URL")
    status: ProjectStatus = Field(..., description="项目状态")
    mode: ReplicationMode = Field(..., description="复刻模式")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
    analysis_result: Optional[dict] = Field(None, description="视频分析结果")
    script_content: Optional[str] = Field(None, description="脚本内容")


# ============================================================================
# 素材模型 (Assets)
# ============================================================================

class AssetCreate(BaseModel):
    """创建素材请求模型"""
    project_id: str = Field(..., description="所属项目ID")
    asset_type: AssetType = Field(..., description="素材类型")
    url: str = Field(..., description="素材URL")
    filename: Optional[str] = Field(None, description="文件名")
    metadata: Optional[dict] = Field(None, description="素材元数据")


class AssetResponse(BaseModel):
    """素材响应模型"""
    id: str = Field(..., description="素材ID")
    project_id: str = Field(..., description="所属项目ID")
    asset_type: AssetType = Field(..., description="素材类型")
    url: str = Field(..., description="素材URL")
    filename: Optional[str] = Field(None, description="文件名")
    metadata: Optional[dict] = Field(None, description="素材元数据")
    created_at: datetime = Field(..., description="创建时间")


# ============================================================================
# 分镜模型 (Shots)
# ============================================================================

class ShotCreate(BaseModel):
    """创建分镜请求模型"""
    project_id: str = Field(..., description="所属项目ID")
    sequence_number: int = Field(..., description="分镜序号")
    original_shot_description: Optional[str] = Field(None, description="原镜头描述")
    new_shot_description: Optional[str] = Field(None, description="新镜头描述")
    generation_prompt: Optional[str] = Field(None, description="生成提示词")
    keyframe_image_url: Optional[str] = Field(None, description="关键帧图片URL")
    status: ShotStatus = Field(default=ShotStatus.PENDING, description="分镜状态")
    generated_video_url: Optional[str] = Field(None, description="生成视频URL")


class ShotResponse(BaseModel):
    """分镜响应模型"""
    id: str = Field(..., description="分镜ID")
    project_id: str = Field(..., description="所属项目ID")
    sequence_number: int = Field(..., description="分镜序号")
    original_shot_description: Optional[str] = Field(None, description="原镜头描述")
    new_shot_description: Optional[str] = Field(None, description="新镜头描述")
    generation_prompt: Optional[str] = Field(None, description="生成提示词")
    keyframe_image_url: Optional[str] = Field(None, description="关键帧图片URL")
    status: ShotStatus = Field(..., description="分镜状态")
    generated_video_url: Optional[str] = Field(None, description="生成视频URL")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


# ============================================================================
# 审核记录模型 (Reviews)
# ============================================================================

class ReviewCreate(BaseModel):
    """创建审核记录请求模型"""
    project_id: str = Field(..., description="所属项目ID")
    shot_id: Optional[str] = Field(None, description="关联分镜ID")
    reviewer: Optional[str] = Field(None, description="审核人")
    status: ReviewStatus = Field(..., description="审核状态")
    comments: Optional[str] = Field(None, description="审核意见")


class ReviewResponse(BaseModel):
    """审核记录响应模型"""
    id: str = Field(..., description="审核记录ID")
    project_id: str = Field(..., description="所属项目ID")
    shot_id: Optional[str] = Field(None, description="关联分镜ID")
    reviewer: Optional[str] = Field(None, description="审核人")
    status: ReviewStatus = Field(..., description="审核状态")
    comments: Optional[str] = Field(None, description="审核意见")
    created_at: datetime = Field(..., description="创建时间")


# ============================================================================
# API 请求/响应模型
# ============================================================================

class AnalyzeVideoRequest(BaseModel):
    """视频分析请求"""
    video_url: str = Field(..., description="视频URL")


class AnalyzeProductRequest(BaseModel):
    """商品分析请求"""
    product_image_url: str = Field(..., description="商品图片URL")


class GenerateScriptRequest(BaseModel):
    """生成脚本请求"""
    project_id: str = Field(..., description="项目ID")


class ConvertPromptsRequest(BaseModel):
    """转换提示词请求"""
    project_id: str = Field(..., description="项目ID")


class GenerateShotsRequest(BaseModel):
    """生成分镜视频请求"""
    project_id: str = Field(..., description="项目ID")
    platform: str = Field(default="seedance", description="视频生成平台: seedance 或 kling")


class ComposeVideoRequest(BaseModel):
    """合成视频请求"""
    project_id: str = Field(..., description="项目ID")
    skip_clip_editing: bool = Field(
        default=False,
        description="是否跳过 clip_editor_agent 的 edit_plan，直接走老逻辑（回滚开关）",
    )


# ============================================================================
# 复刻剪辑 Agent（clip_editor_agent）模型
# ============================================================================

class EditTrim(BaseModel):
    """裁剪区间（秒）"""
    start_sec: float = Field(0.0, ge=0.0, description="裁剪起始时间")
    end_sec: float = Field(..., gt=0.0, description="裁剪结束时间")


class EditPlan(BaseModel):
    """单个镜头的剪辑指令

    由 clip_editor_agent 产出，写回 Airtable Shots 表"剪辑指令"字段，
    Stage 5 合成时读取并执行。
    """
    shot_number: int = Field(..., description="镜头序号")
    source_duration: float = Field(..., ge=0.0, description="生成 clip 实际时长（秒）")
    target_duration: float = Field(..., gt=0.0, description="复刻目标时长（秒）")
    strategy: str = Field(
        ...,
        description="剪辑策略：no_op | trim_head | trim_semantic | speed_up | trim_and_speed",
    )
    trim: Optional[EditTrim] = Field(None, description="裁剪区间；no_op / speed_up 时可为空")
    speed: float = Field(1.0, gt=0.0, le=2.0, description="变速倍率，1.0 表示不变速")
    keep_anchors: List[str] = Field(default_factory=list, description="需要保留的语义锚点描述")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="策略置信度")
    reasoning: str = Field(default="", description="策略推理说明")
    fallback: Optional[dict] = Field(None, description="兜底策略（当主策略失败时使用）")


class ClipEditorRequest(BaseModel):
    """复刻剪辑 Agent 请求"""
    project_id: str = Field(..., description="项目 ID")
    enable_llm_semantic_pick: bool = Field(
        default=False,
        description="是否启用 Gemini 语义选段（Phase 2+），Phase 1 默认 false",
    )
    enable_speed_adjust: bool = Field(
        default=False,
        description="是否启用变速策略（Phase 3+），Phase 1 默认 false",
    )


class ClipEditorResponse(BaseModel):
    """复刻剪辑 Agent 响应"""
    project_id: str = Field(..., description="项目 ID")
    total_shots: int = Field(..., description="处理的镜头总数")
    success_count: int = Field(..., description="成功生成剪辑指令的镜头数")
    source_total_duration: float = Field(..., description="生成 clip 总时长（秒）")
    target_total_duration: float = Field(..., description="目标总时长（秒）")
    expected_output_duration: float = Field(..., description="执行剪辑后的预估总时长（秒）")
    edit_plans: List[EditPlan] = Field(default_factory=list, description="每个镜头的剪辑指令")
    warnings: List[str] = Field(default_factory=list, description="警告信息列表")


# ============================================================================
# 节奏分析模型 (Rhythm Analysis)
# ============================================================================

class RhythmAnalysisRequest(BaseModel):
    """视频节奏分析请求，支持三种输入方式（至少传其中一种）"""
    video_url: Optional[str] = Field(None, description="方式1：视频URL，直接传公网可访问的视频链接")
    project_id: Optional[str] = Field(None, description="方式2：项目id，自动从 Airtable 读取对应项目的原始视频URL")

    @model_validator(mode="after")
    def check_at_least_one_input(self) -> "RhythmAnalysisRequest":
        if not self.video_url and not self.project_id:
            raise ValueError("请提供 video_url 或 project_id 之一")
        return self


class RhythmOverview(BaseModel):
    """节奏总览"""
    total_duration_sec: float = Field(..., description="视频总时长（秒）")
    total_shots: int = Field(..., description="总镜头数")
    avg_shot_duration_sec: float = Field(..., description="平均镜头时长（秒）")
    overall_pace: str = Field(..., description="整体节奏：fast/medium/slow")
    pace_pattern: str = Field(..., description="节奏变化模式：steady/accelerating/decelerating/wave")


class AudioSegment(BaseModel):
    """音频情绪段落"""
    start_sec: float = Field(..., description="开始时间（秒）")
    end_sec: float = Field(..., description="结束时间（秒）")
    mood: str = Field(..., description="情绪描述")
    energy: str = Field(..., description="能量级别：low/medium/high")
    description: str = Field(..., description="段落描述")


class AudioAnalysis(BaseModel):
    """音频节奏分析"""
    music_type: str = Field(..., description="音乐类型/风格")
    estimated_bpm: int = Field(..., description="估算BPM")
    beat_positions_sec: List[float] = Field(default_factory=list, description="强拍时间戳列表（秒）")
    audio_segments: List[AudioSegment] = Field(default_factory=list, description="音频情绪分段")


class ShotRhythm(BaseModel):
    """单个镜头节奏数据"""
    shot_number: int = Field(..., description="镜头编号")
    start_sec: float = Field(..., description="开始时间（秒）")
    end_sec: float = Field(..., description="结束时间（秒）")
    duration_sec: float = Field(..., description="时长（秒）")
    pace: str = Field(..., description="节奏分类：slow/medium/fast")
    visual_intensity: float = Field(..., ge=0.0, le=1.0, description="视觉强度 0-1")
    audio_intensity: float = Field(..., ge=0.0, le=1.0, description="音频强度 0-1")
    cut_type: str = Field(..., description="入场切换类型")
    motion: str = Field(..., description="镜头运动方式")
    beat_aligned: bool = Field(..., description="是否对齐节拍")
    sync_description: str = Field(..., description="音画同步描述")


class RhythmTimelineEvent(BaseModel):
    """节奏时间轴关键事件"""
    timestamp_sec: float = Field(..., description="发生时间（秒）")
    type: str = Field(..., description="事件类型：beat_sync/transition/climax/pause/text_reveal/music_drop")
    visual_trigger: str = Field(..., description="视觉触发描述")
    audio_trigger: str = Field(..., description="听觉触发描述")
    combined_impact: str = Field(..., description="综合冲击力：high/medium/low")
    replication_note: str = Field(..., description="复刻操作指引")


class PaceZone(BaseModel):
    """节奏分区"""
    zone: str = Field(..., description="时间区间，如 '0-10s'")
    target_cuts: int = Field(..., description="目标剪辑次数")
    target_pace: str = Field(..., description="目标节奏描述")
    description: str = Field(..., description="该区间的节奏特征")


class ReplicationRhythmGuide(BaseModel):
    """复刻节奏指南"""
    rhythmic_contract: str = Field(..., description="整体节奏契约：描述编辑者必须复刻的节奏感")
    must_sync_moments: List[str] = Field(default_factory=list, description="必须对齐节拍的关键时刻")
    pace_zones: List[PaceZone] = Field(default_factory=list, description="各时段节奏分区")
    audio_cut_alignment_rule: str = Field(..., description="音频-剪切对齐规则")
    patterns_to_avoid: List[str] = Field(default_factory=list, description="需要避免的节奏模式")


class RhythmAnalysisResponse(BaseModel):
    """视频节奏分析响应"""
    video_url: str = Field(..., description="被分析的视频URL")
    overview: RhythmOverview = Field(..., description="节奏总览")
    audio: AudioAnalysis = Field(..., description="音频节奏分析")
    shots: List[ShotRhythm] = Field(..., description="逐镜头节奏数据")
    rhythm_timeline: List[RhythmTimelineEvent] = Field(..., description="强节奏点时间轴")
    replication_rhythm_guide: ReplicationRhythmGuide = Field(..., description="复刻节奏指南")


class JobStatusResponse(BaseModel):
    """任务状态响应"""
    job_id: str = Field(..., description="任务ID")
    status: JobStatus = Field(..., description="任务状态")
    progress: float = Field(..., ge=0.0, le=1.0, description="进度 (0-1)")
    result: Optional[Any] = Field(None, description="任务结果")
    message: Optional[str] = Field(None, description="状态消息")
    created_at: Optional[datetime] = Field(None, description="创建时间")
    updated_at: Optional[datetime] = Field(None, description="更新时间")


# ============================================================================
# 商品分析 Agent：Product Brief 模型
# ============================================================================

class ClarificationItem(BaseModel):
    """Agent 生成的待用户确认问题"""
    field: str = Field(..., description="对应 ProductBrief 的目标字段，如 target_audience / tone")
    question: str = Field(..., description="向用户提出的问题")
    suggestions: List[str] = Field(default_factory=list, description="可选项或建议答复")
    default_value: Optional[str] = Field(None, description="超时后使用的默认值")


class ProductBrief(BaseModel):
    """统一的商品 Brief，贯穿 Stage1-5 所有阶段
    Phase A 产出基础信息 + clarification_items；Phase B 完成全部补全字段
    """
    # 基础信息（Phase A 产出，复用 product_analysis 的 Layer 0-4）
    product_name: str = Field(default="", description="产品规范名称")
    brand: Optional[str] = Field(None, description="品牌")
    category: str = Field(default="", description="产品类别")
    core_components: List[dict] = Field(default_factory=list, description="沿用 layer_0_component_decomposition.components 结构")
    physical_attrs: dict = Field(default_factory=dict, description="沿用 layer_1_physical_attributes")
    operation_mechanics: dict = Field(default_factory=dict, description="沿用 layer_2_operation_mechanics")
    use_effect: dict = Field(default_factory=dict, description="沿用 layer_3_use_effect")

    # Agent 补全信息（Phase B 产出）
    key_selling_points: List[str] = Field(default_factory=list, description="核心卖点（3-5 条）")
    target_audience: str = Field(default="", description="目标人群画像")
    tone: str = Field(default="", description="品牌调性，如 professional/playful/warm")
    competitor_differentiators: List[str] = Field(default_factory=list, description="与竞品的差异化点")
    constraints: List[str] = Field(default_factory=list, description="不能出现的元素/负向约束")

    # 商品视频理解（可选：Stage1 在商品页检测到嵌入视频时填充）
    product_video_url: Optional[str] = Field(None, description="商品详情页嵌入视频的直链 URL，通常从 extract_product_listing 提取")
    product_video_analysis: Optional[dict] = Field(None, description="商品视频语义理解结果，来自 PRODUCT_VIDEO_ANALYSIS_PROMPT")

    # 元信息
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Brief 整体置信度")
    info_gaps: List[str] = Field(default_factory=list, description="Agent 认为仍缺失的信息项")
    sources: List[str] = Field(default_factory=list, description="信息来源追溯，如 image / listing / product_video / web_search / user")
    clarification_items: List[ClarificationItem] = Field(default_factory=list, description="Phase A 生成的待用户确认问题")
    phase: str = Field(default="draft", description="当前阶段：draft / awaiting_user / finalized")
