"""
项目配置模块
使用 pydantic-settings 管理所有环境变量配置
"""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置类"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini API
    GEMINI_API_KEY: str = ""

    # Qwen (阿里云百炼 / DashScope OpenAI 兼容模式)
    # 用途：分镜提示词物理一致性自审核、统一审查层（文本+多模态）
    QWEN_API_KEY: str = ""
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_MODEL: str = "qwen-plus"
    QWEN_VL_MODEL: str = "qwen-vl-plus"  # 视觉审查模型（Stage 3.5 / Stage 4.4）

    # 统一模型审查层（AuditService）
    # 4 个审查点：1.1 原视频分析 / 1.2 商品分析 / 3.5 关键帧 / 4.4 生成视频
    AUDIT_CONFIDENCE_THRESHOLD: float = 0.80  # 统一置信度阈值，与 Stage 2 脚本验证对齐
    ENABLE_AUDIT_STAGE1_VIDEO: bool = True   # 1.1 原视频分析审查开关
    ENABLE_AUDIT_STAGE1_PRODUCT: bool = True  # 1.2 商品分析审查开关
    ENABLE_AUDIT_KEYFRAME: bool = True       # 3.5 关键帧审查开关
    ENABLE_AUDIT_GENERATED_VIDEO: bool = True  # 4.4 生成视频审查开关
    AUDIT_VIDEO_SAMPLE_FRAMES: int = 4       # 4.4 抽帧数（首/25%/75%/尾）

    # 3.5 关键帧级联审查（L1 Qwen-VL 快筛 → L2 Gemini 精审 + 自动重试）
    ENABLE_CASCADE_AUDIT: bool = True          # 是否启用级联审查；False 时退化为原单模型 audit_keyframe
    CASCADE_CONFIDENCE_THRESHOLD: float = 0.80 # L1 confidence 低于此值且 L1 判定不一致时升级到 L2
    MAX_KEYFRAME_ATTEMPTS: int = 3             # 单镜头关键帧最多生成+审查次数（含首次）
    GEMINI_AUDIT_MODEL: str = "gemini-2.5-pro" # L2 精审使用的 Gemini 模型（视觉理解能力更强）

    # Gemini 温度统一管控（三档分类）
    GEMINI_TEMPERATURE_AUDIT: float = 0.1     # 审查/验证类：结果稳定性优先（script_validation / shot_prompt_audit 等）
    GEMINI_TEMPERATURE_ANALYSIS: float = 0.2  # 分析/抽取类：结构化输出一致性（video_analysis / product_analysis 等）
    GEMINI_TEMPERATURE_CREATIVE: float = 0.3  # 创作类：脚本生成/Prompt转换（需遍历 ground truth 别太随机）

    # KIE AI (统一调用可灵/Seedance) - 备选
    KIE_API_KEY: str = ""
    KIE_BASE_URL: str = "https://api.kie.ai/api/v1"

    # Seedance 直连 API（火山引擎方舟平台）
    SEEDANCE_API_KEY: str = ""
    SEEDANCE_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/v3"
    SEEDANCE_MODEL: str = "doubao-seedance-2-0-260128"  # 支持: doubao-seedance-2-0-fast-260128 或推理接入点 ep-xxx
    SEEDANCE_RESOLUTION: str = "480p"  # 视频生成分辨率: 480p / 720p / 1080p

    # Kling 官方 API（国内版 klingai.com）- 首尾帧双锚定平台
    KLING_ACCESS_KEY: str = ""
    KLING_SECRET_KEY: str = ""
    KLING_BASE_URL: str = "https://api-beijing.klingai.com"
    KLING_MODEL: str = "kling-v1-6"
    KLING_MODE: str = "std"  # std 标准模式 / pro 高清模式

    # 通义万相 Wan 2.7 图生视频（兜底平台，DashScope 百炼）
    WAN_API_KEY: str = ""  # 可复用 QWEN_API_KEY（同源阿里云百炼）
    WAN_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"
    WAN_MODEL: str = "wan2.7-i2v-2026-04-25"
    WAN_RESOLUTION: str = "720P"  # 480P / 720P / 1080P
    ENABLE_WAN_FALLBACK: bool = True  # Kling 单镜头失败时是否自动切 wan 兜底

    # State storage backend
    # postgres = production default, airtable = legacy prototype adapter
    DATA_BACKEND: str = "postgres"

    # Airtable legacy adapter (only required when DATA_BACKEND=airtable)
    AIRTABLE_API_KEY: str = ""
    AIRTABLE_BASE_ID: str = ""

    # Production persistence and durable jobs
    DATABASE_URL: str = "postgresql+asyncpg://video:video@postgres:5432/video_replication"
    REDIS_URL: str = "redis://redis:6379/0"
    JOB_BACKEND: str = "memory"  # memory | durable
    JOB_QUEUE_NAME: str = "video-replication:jobs"
    JOB_LEASE_SECONDS: int = 900
    JOB_MAX_ATTEMPTS: int = 3
    WORKER_POLL_TIMEOUT_SECONDS: int = 5

    # FFmpeg
    FFMPEG_BIN_PATH: str = "ffmpeg"
    FFMPEG_TEMP_DIR: str = "/tmp/ffmpeg_renders"

    # 阿里云 OSS
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_BUCKET_NAME: str = ""
    OSS_ENDPOINT: str = ""  # 如 oss-cn-hangzhou.aliyuncs.com
    OSS_CDN_DOMAIN: str = ""  # 可选，自定义域名

    # Background Removal (抠图)
    REMOVEBG_API_KEY: str = ""  # remove.bg API 密钥（可选，作为 rembg 的降级方案）

    # Keyframe Generation (Stage 3.5)
    KEYFRAME_IMAGE_MODEL: str = ""  # 关键帧生成使用的模型名（空=使用服务默认值，KIE: gpt-image-2-image-to-image，OpenRouter: openai/gpt-5.4-image-2）
    ENABLE_KEYFRAME_STAGE: bool = True  # 是否启用 Stage 3.5 关键帧阶段

    # IP-Adapter 配置（仅在 fal.ai 后端生效，用于产品身份保持）
    ENABLE_IP_ADAPTER: bool = os.getenv("ENABLE_IP_ADAPTER", "false").lower() == "true"
    IP_ADAPTER_SCALE: float = float(os.getenv("IP_ADAPTER_SCALE", "0.6"))

    # 图片生成后端选择: "fal" | "gemini" | "openai" | "openrouter" | "kie" | "dashscope"
    IMAGE_GEN_PROVIDER: str = "fal"
    # fal.ai (GPT Image 2, 无区域限制)
    FAL_KEY: str = ""
    # OpenAI 官方 API 配置
    OPENAI_API_KEY: str = ""

    # OpenRouter 配置
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_IMAGE_MODEL: str = "openai/gpt-5.4-image-2"

    # DashScope（通义万相）图生图参考强度
    # 范围 0-1，值越高产品参考图对生成结果的锚定越强（推荐 0.75 强化产品外观一致性）
    DASHSCOPE_REF_STRENGTH: float = float(os.getenv("DASHSCOPE_REF_STRENGTH", "0.75"))

    # OST Overlay (Stage 5 后处理)
    ENABLE_OST_OVERLAY: bool = True  # 是否启用 OST (On-Screen Text) 叠加
    ENABLE_SUBTITLE_OVERLAY: bool = True  # 是否启用字幕叠加（voiceover/dialogue）
        # 字幕字体：archivo_black（饱满可读，默认） / anton（超粗压缩，冲击力）
    SUBTITLE_FONT: str = "archivo_black"
    # OST 本地化（基于 ProductBrief 将原商品专有名词改写为新商品文案）
    ENABLE_OST_LOCALIZATION: bool = True  # 是否启用 OST 本地化（需有 ProductBrief）
    OST_LOCALIZATION_MODEL: str = "gemini-2.0-flash"  # OST 本地化使用的快模型

    # Product Brief Agent (Stage 1.0) - 商品分析 Agent
    ENABLE_PRODUCT_AGENT: bool = False  # 是否启用商品分析 Agent（灰度开关，默认关闭）
    PRODUCT_AGENT_MAX_LOOPS: int = 3  # Agent Loop 最大轮次
    PRODUCT_AGENT_TIMEOUT_SEC: int = 120  # Agent Loop 整体超时
    PRODUCT_AGENT_ENABLE_WEB_SEARCH: bool = False  # 是否启用 web_search 工具（需额外 API）
    PRODUCT_AGENT_REQUIRE_USER_CONFIRMATION: bool = False  # 是否需要用户确认 Phase A 结果（Task3启用）

    # 商品视频理解（Stage 1）——从商品详情页提取嵌入视频并调用 Gemini 分析
    # 与 ENABLE_PRODUCT_AGENT 解耦：即使 Agent 未启用，视频理解结果仍会写入 product_listing_info
    ENABLE_PRODUCT_VIDEO_ANALYSIS: bool = True
    PRODUCT_VIDEO_MAX_BYTES: int = 18_874_368  # 18MB；Gemini inline_data 上限 ~20MB，留缓冲

    # Web Search Provider（供 Product Agent 的 web_search_brand 工具使用）
    TAVILY_API_KEY: str = ""  # Tavily 搜索 API Key（https://tavily.com/），请放在 .env 中
    SERPER_API_KEY: str = ""  # Serper 搜索 API Key（备选，https://serper.dev/）

    # Suno AI 音乐生成（BGM）
    SUNO_API_KEY: str = ""
    SUNO_BASE_URL: str = "https://api.sunoapi.org"
    SUNO_MODEL: str = "V4_5ALL"  # V4 / V4_5 / V4_5ALL / V5 / V5_5
    ENABLE_BGM: bool = False  # 是否启用 Stage 5 自动 BGM 生成（默认关闭，优先走镜头内环境音）
    BGM_VOLUME: float = 0.3  # BGM 混入音量（0.0-1.0）

    # ElevenLabs Sound Effects（镜头内环境音）
    ELEVENLABS_API_KEY: str = ""  # https://elevenlabs.io 申请（仅开 Sound Effects 权限）
    ELEVENLABS_BASE_URL: str = "https://api.elevenlabs.io"
    ENABLE_AMBIENT_AUDIO: bool = True  # 是否启用 Stage 5 镜头内环境音生成与混音
    AMBIENT_VOLUME: float = 0.3  # 环境音混入音量（0.0-1.0），建议 0.25-0.4
    AMBIENT_MAX_DURATION_SEC: float = 22.0  # ElevenLabs sound-generation 单次最长 22s

    # 复刻剪辑 Agent (Stage 4.5)
    CLIP_EDITOR_SEMANTIC_PICK: bool = False  # 是否启用 Phase 2 AI 语义选段（需 Gemini 视频理解）

    # Service Configuration
    SERVICE_HOST: str = "0.0.0.0"
    SERVICE_PORT: int = 8000

    # Prompts Configuration
    PROMPTS_DIR: str = ""  # 外部 prompts 目录路径，为空则使用内置 prompt


# 全局配置实例
settings = Settings()
