"""
统一模型审查层（AuditService）

四个审查点共用同一套抽象：
- 1.1 原视频分析审查    audit_video_analysis
- 1.2 商品分析审查      audit_product_analysis
- 3.5 关键帧视觉审查    audit_keyframe
- 4.4 生成视频抽帧审查  audit_generated_video

统一输出 AuditResult，统一置信度阈值 settings.AUDIT_CONFIDENCE_THRESHOLD；
失败语义：passed=False 或 confidence < 阈值 → should_block=True → 上层阻断并转人审。

模型选型：
- 1.1/1.2 → Qwen 文本（settings.QWEN_MODEL）
- 3.5/4.4 → Qwen-VL 多模态（settings.QWEN_VL_MODEL）
"""

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from config import settings
from services.qwen_service import QwenService

logger = logging.getLogger(__name__)


class AuditResult(BaseModel):
    """统一审查结果结构，所有 prompt 必须输出此结构"""

    passed: bool = Field(..., description="模型判定是否通过")
    confidence: float = Field(..., ge=0.0, le=1.0, description="整体置信度 0.0-1.0")
    critical_issues: list[str] = Field(
        default_factory=list, description="阻断级问题，非空时必须转人审"
    )
    warnings: list[str] = Field(
        default_factory=list, description="警告级问题，不阻断"
    )
    reason_summary: str = Field(default="", description="一句话总结")

    @property
    def should_block(self) -> bool:
        """统一阻断判定：未通过 / 置信度不达标 / 存在 critical_issues，任一条件即阻断"""
        threshold = float(getattr(settings, "AUDIT_CONFIDENCE_THRESHOLD", 0.85))
        if not self.passed:
            return True
        if self.confidence < threshold:
            return True
        if self.critical_issues:
            return True
        return False

    def to_review_comment(self) -> str:
        """生成可直接写入 Airtable 审核意见字段的可读文案"""
        lines: list[str] = []
        if self.reason_summary:
            lines.append(f"[总结] {self.reason_summary}")
        lines.append(f"[置信度] {self.confidence:.2f} (阈值 {getattr(settings, 'AUDIT_CONFIDENCE_THRESHOLD', 0.85)})")
        if self.critical_issues:
            lines.append("[阻断项]")
            lines.extend(f"  - {x}" for x in self.critical_issues)
        if self.warnings:
            lines.append("[警告]")
            lines.extend(f"  - {x}" for x in self.warnings)
        return "\n".join(lines)


class AuditFailedException(Exception):
    """
    审查失败专用异常，上层工作流捕获后统一执行"写回状态+转人审+返回"的收敛动作。

    Attributes:
        stage: 审查发生的阶段标识（如 "stage1"、"stage3_5"、"stage4"）
        scope: 审查细分维度（如 "video_analysis"、"product_analysis"、"keyframe"、"generated_video"）
        result: 对应的 AuditResult（含失败详情）
    """

    def __init__(self, stage: str, scope: str, result: AuditResult) -> None:
        self.stage = stage
        self.scope = scope
        self.result = result
        super().__init__(
            f"[{stage}/{scope}] 审查未通过: passed={result.passed}, "
            f"confidence={result.confidence:.2f}, "
            f"critical_issues={len(result.critical_issues)}"
        )


class AuditService:
    """统一审查服务。负责构造 prompt、调 Qwen、解析结果。
    不负责失败流转（由各 workflow 捕获 AuditResult.should_block 自行决策）。
    """

    def __init__(self, qwen: Optional[QwenService] = None) -> None:
        self._qwen = qwen or QwenService()

    def set_context(self, project_id: str, stage: str) -> None:
        """传递给底层 QwenService，用于 Token 追踪"""
        self._qwen.set_context(project_id, stage)

    # ------------------------------------------------------------------
    # 1.1 原视频分析审查
    # ------------------------------------------------------------------
    async def audit_video_analysis(
        self,
        project_id: str,
        video_analysis: dict[str, Any],
    ) -> AuditResult:
        """审查 Stage 1 产出的原视频分析 JSON 是否完整、合理、可下游使用。"""
        from prompts.video_analysis_audit import format_video_analysis_audit_prompt

        self.set_context(project_id, "stage1_audit_video")
        prompt = format_video_analysis_audit_prompt(
            video_analysis=json.dumps(video_analysis, ensure_ascii=False, indent=2)
        )
        data = await self._qwen.audit_text(
            prompt=prompt,
            context="stage1_video_analysis_audit",
        )
        result = AuditResult(**data)
        logger.info(
            f"[{project_id}] audit_video_analysis: passed={result.passed}, "
            f"confidence={result.confidence:.2f}, "
            f"criticals={len(result.critical_issues)}, warnings={len(result.warnings)}"
        )
        return result

    # ------------------------------------------------------------------
    # 1.2 商品分析审查
    # ------------------------------------------------------------------
    async def audit_product_analysis(
        self,
        project_id: str,
        product_analysis: dict[str, Any],
        product_listing_info: Optional[dict[str, Any]] = None,
    ) -> AuditResult:
        """审查 Stage 1 产出的商品分析 JSON（含 listing 补充信息）完整性与一致性。"""
        from prompts.product_analysis_audit import format_product_analysis_audit_prompt

        self.set_context(project_id, "stage1_audit_product")
        prompt = format_product_analysis_audit_prompt(
            product_analysis=json.dumps(product_analysis, ensure_ascii=False, indent=2),
            product_listing_info=(
                json.dumps(product_listing_info, ensure_ascii=False, indent=2)
                if isinstance(product_listing_info, dict)
                else ""
            ),
        )
        data = await self._qwen.audit_text(
            prompt=prompt,
            context="stage1_product_analysis_audit",
        )
        result = AuditResult(**data)
        logger.info(
            f"[{project_id}] audit_product_analysis: passed={result.passed}, "
            f"confidence={result.confidence:.2f}, "
            f"criticals={len(result.critical_issues)}, warnings={len(result.warnings)}"
        )
        return result

    # ------------------------------------------------------------------
    # 3.5 关键帧视觉审查
    # ------------------------------------------------------------------
    async def audit_keyframe(
        self,
        project_id: str,
        shot_number: int,
        first_frame_description: str,
        keyframe_url: str,
        reference_image_urls: list[str],
    ) -> AuditResult:
        """
        审查关键帧图片：商品形态与参考图一致、构图符合 first_frame 描述、无明显瑕疵。

        Args:
            reference_image_urls: 参考图 URL 列表（建议 [三视图, 商品主图]）
        """
        from prompts.keyframe_audit import format_keyframe_audit_prompt

        self.set_context(project_id, "stage3_5_audit_keyframe")
        prompt = format_keyframe_audit_prompt(
            shot_number=shot_number,
            first_frame_description=first_frame_description or "",
        )
        # 参考图在前、待审图在后，让模型最后一张图作为"被审查对象"更清晰
        image_urls = [u for u in (reference_image_urls or []) if u] + [keyframe_url]
        data = await self._qwen.audit_images(
            prompt=prompt,
            image_urls=image_urls,
            context="stage3_5_keyframe_audit",
        )
        result = AuditResult(**data)
        logger.info(
            f"[{project_id}] audit_keyframe shot={shot_number}: passed={result.passed}, "
            f"confidence={result.confidence:.2f}, "
            f"criticals={len(result.critical_issues)}, warnings={len(result.warnings)}"
        )
        return result

    async def audit_keyframe_cascade(
        self,
        project_id: str,
        shot_number: int,
        first_frame_description: str,
        keyframe_url: str,
        reference_image_urls: list[str],
    ) -> AuditResult:
        """
        级联关键帧审查：L1 Qwen-VL 快筛 → （仅在 L1 判定阻断时）L2 Gemini 精审复核。

        为什么要级联：Qwen-VL 便宜快速但对细微几何差异分辨力有限，
        单模型直接阻断会有误杀（浪费重试费用）也会有漏放（漂移帧过审）。
        L1 放行则直接放行（省钱）；L1 阻断时用视觉能力更强的 Gemini 复核，
        以 L2 结论为准——既减少误杀，又让真问题被更强模型确认。

        ENABLE_CASCADE_AUDIT=False 时退化为单模型 audit_keyframe。
        L2 调用失败时保守起见维持 L1 的阻断结论。
        """
        l1 = await self.audit_keyframe(
            project_id=project_id,
            shot_number=shot_number,
            first_frame_description=first_frame_description,
            keyframe_url=keyframe_url,
            reference_image_urls=reference_image_urls,
        )
        if not getattr(settings, "ENABLE_CASCADE_AUDIT", False):
            return l1
        if not l1.should_block:
            return l1

        # L1 判定阻断 → 升级 L2 Gemini 精审
        from prompts.keyframe_audit import format_keyframe_audit_prompt
        from services.gemini_service import GeminiService

        logger.info(
            f"[{project_id}] audit_keyframe shot={shot_number}: L1 阻断"
            f"(confidence={l1.confidence:.2f})，升级 L2 Gemini 精审"
        )
        prompt = format_keyframe_audit_prompt(
            shot_number=shot_number,
            first_frame_description=first_frame_description or "",
        )
        image_urls = [u for u in (reference_image_urls or []) if u] + [keyframe_url]
        gemini = GeminiService()
        gemini.set_context(project_id, f"stage3_5_audit_keyframe_l2/shot_{shot_number}")
        try:
            data = await gemini.audit_keyframe_images(
                image_urls=image_urls,
                prompt=prompt,
                context="stage3_5_keyframe_audit_l2",
            )
            l2 = AuditResult(**data)
        except Exception as e:
            logger.warning(
                f"[{project_id}] L2 Gemini 精审异常，维持 L1 阻断结论: {e}"
            )
            return l1
        finally:
            await gemini.close()

        # L2 自身调用失败会返回 passed=False/confidence=0.0（转人审），同样维持阻断
        logger.info(
            f"[{project_id}] audit_keyframe shot={shot_number} L2 结论: "
            f"passed={l2.passed}, confidence={l2.confidence:.2f}, "
            f"criticals={len(l2.critical_issues)}（以 L2 为准）"
        )
        return l2

    # ------------------------------------------------------------------
    # 4.4 生成视频抽帧审查
    # ------------------------------------------------------------------
    async def audit_generated_video(
        self,
        project_id: str,
        shot_number: int,
        shot_prompt: str,
        sample_frame_urls: list[str],
        first_frame_url: Optional[str] = None,
    ) -> AuditResult:
        """
        审查生成视频抽帧：商品形态一致、动作无畸变、与 first_frame 衔接合理。

        Args:
            sample_frame_urls: 从生成视频按 首/25%/75%/尾 抽出并上传 OSS 的图片 URL 列表
            first_frame_url: 关键帧 URL（可选，用于衔接对比）
        """
        from prompts.generated_video_audit import format_generated_video_audit_prompt

        self.set_context(project_id, "stage4_audit_generated_video")
        prompt = format_generated_video_audit_prompt(
            shot_number=shot_number,
            shot_prompt=shot_prompt or "",
            has_first_frame=bool(first_frame_url),
            sample_frame_count=len(sample_frame_urls),
        )
        # 若有 first_frame，先放 first_frame 作为参考，再放抽帧序列
        image_urls: list[str] = []
        if first_frame_url:
            image_urls.append(first_frame_url)
        image_urls.extend(u for u in sample_frame_urls if u)

        data = await self._qwen.audit_images(
            prompt=prompt,
            image_urls=image_urls,
            context="stage4_generated_video_audit",
        )
        result = AuditResult(**data)
        logger.info(
            f"[{project_id}] audit_generated_video shot={shot_number}: passed={result.passed}, "
            f"confidence={result.confidence:.2f}, "
            f"criticals={len(result.critical_issues)}, warnings={len(result.warnings)}"
        )
        return result
