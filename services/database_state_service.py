"""PostgreSQL-backed workflow state service.

This service intentionally exposes an Airtable-shaped API during migration:
records are returned as {"id": "...", "fields": {...}}. Existing workflow
stages can therefore move off Airtable without a risky full rewrite.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from persistence.database import get_session_factory
from persistence.models import (
    AssetRecord,
    ProjectRecord,
    ReviewRecord,
    ShotRecord,
    utcnow,
)

logger = logging.getLogger(__name__)


def _json_loads(value: Any, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _attachment_url(value: Any) -> str:
    if isinstance(value, list) and value:
        first = value[0] or {}
        if isinstance(first, dict):
            return str(first.get("url") or "")
    if isinstance(value, str):
        return value
    return ""


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValueError(
            f"Database backend expects UUID record ids, got {value!r}"
        ) from exc


class DatabaseStateService:
    """Database implementation of the legacy Airtable service interface."""

    BRIEF_DRAFT_FIELD = "Brief草稿"
    BRIEF_CLARIFICATION_FIELD = "待确认问题"
    BRIEF_CONFIDENCE_FIELD = "Brief置信度"
    BRIEF_USER_ANSWERS_FIELD = "用户答复"

    SHOT_EDIT_PLAN_FIELD = "剪辑指令"
    SHOT_SRC_DURATION_FIELD = "原镜头时长"
    SHOT_TARGET_DURATION_FIELD = "目标时长"
    SHOT_EDIT_REVIEW_STATUS_FIELD = "剪辑审核状态"
    SHOT_KEYFRAME_AUDIT_STATUS_FIELD = "关键帧审查状态"
    SHOT_KEYFRAME_AUDIT_COMMENT_FIELD = "关键帧审查意见"
    SHOT_KEYFRAME_AUDIT_ATTEMPT_FIELD = "关键帧审查尝试次数"
    SHOT_OST_ORIGINAL_FIELD = "OST原文"
    SHOT_OST_LOCALIZED_FIELD = "OST本地化"
    SHOT_OST_CATEGORY_FIELD = "OST分类"

    PROJECT_AUDIT_VIDEO_STATUS_FIELD = "视频分析审查状态"
    PROJECT_AUDIT_VIDEO_COMMENT_FIELD = "视频分析审查意见"
    PROJECT_AUDIT_PRODUCT_STATUS_FIELD = "商品分析审查状态"
    PROJECT_AUDIT_PRODUCT_COMMENT_FIELD = "商品分析审查意见"

    def __init__(
        self,
        session_factory: async_sessionmaker | None = None,
        *_: Any,
        **__: Any,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()

    def _project_record(self, project: ProjectRecord) -> dict[str, Any]:
        fields = dict(project.metadata_json.get("fields", {}) if project.metadata_json else {})
        fields.setdefault("项目名称", project.name)
        fields.setdefault("状态", project.status)
        fields.setdefault("产品图链接", project.product_image_url)
        fields.setdefault("模式", project.mode)
        if project.original_video_url:
            fields.setdefault("原视频", [{"url": project.original_video_url}])
        if project.product_listing_url:
            fields.setdefault("商品链接", project.product_listing_url)
        return {
            "id": str(project.id),
            "createdTime": project.created_at.isoformat(),
            "fields": fields,
        }

    def _asset_record(self, asset: AssetRecord) -> dict[str, Any]:
        metadata = asset.metadata_json or {}
        fields = dict(metadata.get("fields", {}))
        fields.setdefault("项目", [str(asset.project_id)])
        fields.setdefault("素材类型", asset.asset_type)
        fields.setdefault("内容", metadata.get("content", ""))
        if asset.url:
            fields.setdefault("附件", [{"url": asset.url}])
        return {
            "id": str(asset.id),
            "createdTime": asset.created_at.isoformat(),
            "fields": fields,
        }

    def _shot_record(self, shot: ShotRecord) -> dict[str, Any]:
        metadata = shot.quality_scores or {}
        fields = dict(metadata.get("fields", {}))
        fields.setdefault("项目", [str(shot.project_id)])
        fields.setdefault("镜头序号", shot.sequence_number)
        fields.setdefault("生成状态", shot.status)
        if shot.script:
            fields.setdefault("原镜头描述", shot.script.get("original_description", ""))
            fields.setdefault("新镜头描述", shot.script.get("new_description", ""))
        if shot.generation_prompt:
            fields.setdefault("生成提示词", shot.generation_prompt.get("text", ""))
        if shot.keyframe_url:
            fields.setdefault("关键帧图片", shot.keyframe_url)
        if shot.generated_video_url:
            fields.setdefault("生成视频", shot.generated_video_url)
        return {
            "id": str(shot.id),
            "createdTime": shot.created_at.isoformat(),
            "fields": fields,
        }

    def _review_record(self, review: ReviewRecord) -> dict[str, Any]:
        fields = {
            "关联镜头": [str(review.shot_id)] if review.shot_id else [],
            "项目": [str(review.project_id)],
            "审核类型": review.review_type,
            "审核结果": review.status,
            "问题描述": review.comments or "",
            "修改建议": review.suggested_action or "",
        }
        return {
            "id": str(review.id),
            "createdTime": review.created_at.isoformat(),
            "fields": fields,
        }

    async def create_project(
        self,
        name: str,
        video_url: str = "",
        product_image_url: str = "",
        mode: str = "full",
    ) -> dict[str, Any]:
        fields = {
            "项目名称": name,
            "状态": "素材准备中",
            "产品图链接": product_image_url,
            "模式": mode,
        }
        if video_url:
            fields["原视频"] = [{"url": video_url}]
        async with self.session_factory() as session:
            project = ProjectRecord(
                name=name,
                status="素材准备中",
                mode=mode,
                original_video_url=video_url,
                product_image_url=product_image_url,
                metadata_json={"fields": fields},
            )
            session.add(project)
            await session.commit()
            await session.refresh(project)
            logger.info("Created database project: %s - %s", project.id, name)
            return self._project_record(project)

    async def get_project(self, project_id: str) -> Optional[dict[str, Any]]:
        async with self.session_factory() as session:
            project = await session.get(ProjectRecord, _as_uuid(project_id))
            return self._project_record(project) if project else None

    async def update_project_status(self, project_id: str, status: str) -> dict[str, Any]:
        return await self.update_project(project_id, {"状态": status})

    async def update_project(self, project_id: str, data: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory() as session:
            project = await session.get(ProjectRecord, _as_uuid(project_id))
            if project is None:
                raise ValueError(f"Project not found: {project_id}")
            metadata = dict(project.metadata_json or {})
            fields = dict(metadata.get("fields", {}))
            fields.update(data)
            metadata["fields"] = fields
            project.metadata_json = metadata
            project.status = str(fields.get("状态") or project.status)
            project.name = str(fields.get("项目名称") or project.name)
            project.product_image_url = str(fields.get("产品图链接") or project.product_image_url)
            original_video_url = _attachment_url(fields.get("原视频"))
            if original_video_url:
                project.original_video_url = original_video_url
            project.product_listing_url = fields.get("商品链接") or project.product_listing_url
            project.updated_at = utcnow()
            await session.commit()
            await session.refresh(project)
            return self._project_record(project)

    async def list_projects(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            stmt = select(ProjectRecord).order_by(ProjectRecord.created_at.desc()).limit(limit)
            if status:
                stmt = stmt.where(ProjectRecord.status == status)
            projects = (await session.scalars(stmt)).all()
            return [self._project_record(project) for project in projects]

    async def save_product_brief_draft(
        self, project_id: str, brief: dict[str, Any]
    ) -> bool:
        clarifications = brief.get("clarification_items") or []
        await self.update_project(
            project_id,
            {
                self.BRIEF_DRAFT_FIELD: _json_dumps(brief),
                self.BRIEF_CLARIFICATION_FIELD: _json_dumps(clarifications),
                self.BRIEF_CONFIDENCE_FIELD: float(brief.get("confidence_score", 0.0)),
            },
        )
        return True

    async def get_product_brief_state(self, project_id: str) -> dict[str, Any]:
        project = await self.get_project(project_id)
        fields = (project or {}).get("fields", {})
        answers = _json_loads(fields.get(self.BRIEF_USER_ANSWERS_FIELD), None)
        return {
            "draft": _json_loads(fields.get(self.BRIEF_DRAFT_FIELD), None),
            "user_answers": answers,
            "clarifications": _json_loads(fields.get(self.BRIEF_CLARIFICATION_FIELD), []),
        }

    async def save_product_brief_finalized(
        self, project_id: str, brief: dict[str, Any]
    ) -> bool:
        await self.update_project(
            project_id,
            {
                self.BRIEF_DRAFT_FIELD: _json_dumps(brief),
                self.BRIEF_CLARIFICATION_FIELD: "",
                self.BRIEF_CONFIDENCE_FIELD: float(brief.get("confidence_score", 0.0)),
            },
        )
        return True

    async def create_asset(
        self,
        project_id: str,
        asset_type: str,
        content: str = "",
        attachment_url: str = "",
    ) -> dict[str, Any]:
        fields = {"项目": [project_id], "素材类型": asset_type, "内容": content}
        if attachment_url:
            fields["附件"] = [{"url": attachment_url}]
        async with self.session_factory() as session:
            asset = AssetRecord(
                project_id=_as_uuid(project_id),
                asset_type=asset_type,
                url=attachment_url,
                metadata_json={"content": content, "fields": fields},
            )
            session.add(asset)
            await session.commit()
            await session.refresh(asset)
            return self._asset_record(asset)

    async def create_asset_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        return await self.create_asset(
            project_id=data.get("project_id", ""),
            asset_type=data.get("asset_type", ""),
            content=data.get("content", ""),
            attachment_url=data.get("attachment_url", ""),
        )

    async def update_asset_content(
        self,
        project_id: str,
        asset_type: str,
        content: str,
    ) -> Optional[dict[str, Any]]:
        created_needed = False
        async with self.session_factory() as session:
            stmt = (
                select(AssetRecord)
                .where(AssetRecord.project_id == _as_uuid(project_id))
                .where(AssetRecord.asset_type == asset_type)
                .order_by(AssetRecord.created_at.asc())
            )
            asset = (await session.scalars(stmt)).first()
            if asset is None:
                created_needed = True
            else:
                metadata = dict(asset.metadata_json or {})
                fields = dict(metadata.get("fields", {}))
                fields["内容"] = content
                metadata["content"] = content
                metadata["fields"] = fields
                asset.metadata_json = metadata
                await session.commit()
                await session.refresh(asset)
                return self._asset_record(asset)
        if created_needed:
            return await self.create_asset(project_id, asset_type, content)
        return None

    async def get_project_assets(self, project_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            assets = (
                await session.scalars(
                    select(AssetRecord)
                    .where(AssetRecord.project_id == _as_uuid(project_id))
                    .order_by(AssetRecord.created_at.asc())
                )
            ).all()
            return [self._asset_record(asset) for asset in assets]

    async def get_assets(self, project_id: str) -> list[dict[str, Any]]:
        return await self.get_project_assets(project_id)

    async def create_shot(
        self,
        project_id: str,
        shot_number: int,
        original_description: str = "",
        new_description: str = "",
        generation_prompt: str = "",
    ) -> dict[str, Any]:
        fields = {
            "项目": [project_id],
            "镜头序号": shot_number,
            "原镜头描述": original_description,
            "新镜头描述": new_description,
            "生成提示词": generation_prompt,
            "提示词审核状态": "待审核",
            "生成状态": "待生成",
        }
        async with self.session_factory() as session:
            shot = ShotRecord(
                project_id=_as_uuid(project_id),
                sequence_number=int(shot_number),
                status="待生成",
                script={
                    "original_description": original_description,
                    "new_description": new_description,
                },
                generation_prompt={"text": generation_prompt} if generation_prompt else None,
                quality_scores={"fields": fields},
            )
            session.add(shot)
            await session.commit()
            await session.refresh(shot)
            return self._shot_record(shot)

    async def create_shot_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        return await self.create_shot(
            project_id=data.get("project_id", ""),
            shot_number=data.get("sequence_number", data.get("shot_number", 0)),
            original_description=data.get("original_shot_description", ""),
            new_description=data.get("new_shot_description", ""),
            generation_prompt=data.get("generation_prompt", ""),
        )

    async def batch_create_shots(self, shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for shot in shots:
            results.append(await self.create_shot_from_dict(shot))
        return results

    async def get_project_shots(self, project_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            shots = (
                await session.scalars(
                    select(ShotRecord)
                    .where(ShotRecord.project_id == _as_uuid(project_id))
                    .order_by(ShotRecord.sequence_number.asc())
                )
            ).all()
            return [self._shot_record(shot) for shot in shots]

    async def get_shots(self, project_id: str) -> list[dict[str, Any]]:
        return await self.get_project_shots(project_id)

    async def update_shot(self, shot_id: str, data: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory() as session:
            shot = await session.get(ShotRecord, _as_uuid(shot_id))
            if shot is None:
                raise ValueError(f"Shot not found: {shot_id}")
            metadata = dict(shot.quality_scores or {})
            fields = dict(metadata.get("fields", {}))
            fields.update(data)
            metadata["fields"] = fields
            shot.quality_scores = metadata
            shot.status = str(fields.get("生成状态") or shot.status)
            if fields.get("镜头序号") is not None:
                shot.sequence_number = int(fields["镜头序号"])
            shot.keyframe_url = fields.get("关键帧图片") or shot.keyframe_url
            shot.generated_video_url = _attachment_url(fields.get("生成视频")) or fields.get("生成视频") or shot.generated_video_url
            if fields.get("生成提示词") is not None:
                shot.generation_prompt = {"text": fields.get("生成提示词")}
            shot.script = {
                "original_description": fields.get("原镜头描述", ""),
                "new_description": fields.get("新镜头描述", ""),
            }
            shot.updated_at = utcnow()
            await session.commit()
            await session.refresh(shot)
            return self._shot_record(shot)

    async def update_shot_prompt(self, shot_id: str, prompt: str) -> dict[str, Any]:
        return await self.update_shot(
            shot_id, {"生成提示词": prompt, "提示词审核状态": "待审核"}
        )

    async def update_shot_prompt_status(
        self, shot_id: str, status: str, review_comment: str = ""
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"提示词审核状态": status}
        if review_comment:
            fields["提示词审核意见"] = review_comment
        return await self.update_shot(shot_id, fields)

    async def update_shot_keyframe(
        self, shot_id: str, keyframe_image_url: str
    ) -> dict[str, Any]:
        return await self.update_shot(shot_id, {"关键帧图片": keyframe_image_url})

    async def update_shot_video(self, shot_id: str, video_url: str) -> dict[str, Any]:
        return await self.update_shot(
            shot_id, {"生成视频": video_url, "视频审核状态": "待审核"}
        )

    async def update_shot_edit_plan(
        self,
        shot_id: str,
        edit_plan: dict,
        source_duration: Optional[float] = None,
        target_duration: Optional[float] = None,
        edit_review_status: Optional[str] = None,
    ) -> bool:
        fields: dict[str, Any] = {self.SHOT_EDIT_PLAN_FIELD: _json_dumps(edit_plan)}
        if source_duration is not None:
            fields[self.SHOT_SRC_DURATION_FIELD] = float(source_duration)
        if target_duration is not None:
            fields[self.SHOT_TARGET_DURATION_FIELD] = float(target_duration)
        if edit_review_status:
            fields[self.SHOT_EDIT_REVIEW_STATUS_FIELD] = edit_review_status
        await self.update_shot(shot_id, fields)
        return True

    async def save_shot_ost_localization(
        self,
        shot_id: str,
        original: str,
        localized: str,
        category: Optional[str] = None,
    ) -> bool:
        fields: dict[str, Any] = {
            self.SHOT_OST_ORIGINAL_FIELD: original or "",
            self.SHOT_OST_LOCALIZED_FIELD: localized or "",
        }
        if category:
            fields[self.SHOT_OST_CATEGORY_FIELD] = category
        await self.update_shot(shot_id, fields)
        return True

    async def save_stage1_audit_result(
        self,
        project_id: str,
        scope: str,
        status: str,
        review_comment: str = "",
    ) -> bool:
        if scope == "video":
            fields = {self.PROJECT_AUDIT_VIDEO_STATUS_FIELD: status}
            if review_comment:
                fields[self.PROJECT_AUDIT_VIDEO_COMMENT_FIELD] = review_comment
        elif scope == "product":
            fields = {self.PROJECT_AUDIT_PRODUCT_STATUS_FIELD: status}
            if review_comment:
                fields[self.PROJECT_AUDIT_PRODUCT_COMMENT_FIELD] = review_comment
        else:
            raise ValueError(f"Unknown audit scope: {scope}")
        await self.update_project(project_id, fields)
        return True

    async def save_keyframe_audit_result(
        self,
        shot_id: str,
        status: str,
        review_comment: str = "",
        attempt: Optional[int] = None,
    ) -> bool:
        fields: dict[str, Any] = {self.SHOT_KEYFRAME_AUDIT_STATUS_FIELD: status}
        if review_comment:
            fields[self.SHOT_KEYFRAME_AUDIT_COMMENT_FIELD] = review_comment
        if attempt is not None:
            fields[self.SHOT_KEYFRAME_AUDIT_ATTEMPT_FIELD] = int(attempt)
        await self.update_shot(shot_id, fields)
        return True

    async def update_shot_video_status(
        self, shot_id: str, status: str, review_comment: str = ""
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"视频审核状态": status}
        if review_comment:
            fields["视频审核意见"] = review_comment
        return await self.update_shot(shot_id, fields)

    async def update_shot_status(
        self,
        shot_id: str,
        status: str,
        video_url: Optional[str] = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"生成状态": status}
        if video_url:
            fields["生成视频"] = video_url
        return await self.update_shot(shot_id, fields)

    async def check_all_shots_approved(
        self, project_id: str, review_type: str
    ) -> dict[str, Any]:
        shots = await self.get_shots(project_id)
        status_field = "提示词审核状态" if review_type == "prompt" else "视频审核状态"
        total = len(shots)
        approved = sum(1 for shot in shots if shot["fields"].get(status_field) == "已通过")
        return {
            "all_approved": approved == total and total > 0,
            "total": total,
            "approved": approved,
            "pending": total - approved,
        }

    async def create_review(
        self,
        shot_id: str,
        review_type: str,
        result: str,
        description: str = "",
        suggestion: str = "",
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            shot = await session.get(ShotRecord, _as_uuid(shot_id))
            if shot is None:
                raise ValueError(f"Shot not found: {shot_id}")
            review = ReviewRecord(
                project_id=shot.project_id,
                shot_id=shot.id,
                review_type=review_type,
                status=result,
                comments=description,
                suggested_action=suggestion,
            )
            session.add(review)
            await session.commit()
            await session.refresh(review)
            return self._review_record(review)

    async def create_review_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        return await self.create_review(
            shot_id=data.get("shot_id", ""),
            review_type=data.get("review_type", ""),
            result=data.get("result", ""),
            description=data.get("description", ""),
            suggestion=data.get("suggestion", ""),
        )

    async def get_project_reviews(self, project_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            reviews = (
                await session.scalars(
                    select(ReviewRecord)
                    .where(ReviewRecord.project_id == _as_uuid(project_id))
                    .order_by(ReviewRecord.created_at.asc())
                )
            ).all()
            return [self._review_record(review) for review in reviews]

    async def get_shot_reviews(self, shot_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            reviews = (
                await session.scalars(
                    select(ReviewRecord)
                    .where(ReviewRecord.shot_id == _as_uuid(shot_id))
                    .order_by(ReviewRecord.created_at.asc())
                )
            ).all()
            return [self._review_record(review) for review in reviews]

    async def sync_token_usage(self, project_id: str, summary: dict[str, Any]) -> None:
        await self.update_project(
            project_id,
            {
                "TokenUsageSummary": _json_dumps(
                    {
                        **summary,
                        "synced_at": datetime.now().isoformat(),
                    }
                )
            },
        )
