"""Transactional repository for durable projects and jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import FailureEventRecord, JobRecord, ProjectRecord


class DurableJobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    WAITING_REVIEW = "waiting_review"
    WAITING_KEYFRAME_REVIEW = "waiting_keyframe_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    DurableJobStatus.COMPLETED,
    DurableJobStatus.FAILED,
    DurableJobStatus.CANCELLED,
}

ALLOWED_TRANSITIONS = {
    DurableJobStatus.PENDING: {
        DurableJobStatus.QUEUED,
        DurableJobStatus.CANCELLED,
    },
    DurableJobStatus.QUEUED: {
        DurableJobStatus.PROCESSING,
        DurableJobStatus.CANCELLED,
        DurableJobStatus.FAILED,
    },
    DurableJobStatus.PROCESSING: {
        DurableJobStatus.PROCESSING,
        DurableJobStatus.WAITING_REVIEW,
        DurableJobStatus.WAITING_KEYFRAME_REVIEW,
        DurableJobStatus.COMPLETED,
        DurableJobStatus.FAILED,
        DurableJobStatus.QUEUED,
        DurableJobStatus.CANCELLED,
    },
    DurableJobStatus.WAITING_REVIEW: {
        DurableJobStatus.QUEUED,
        DurableJobStatus.CANCELLED,
    },
    DurableJobStatus.WAITING_KEYFRAME_REVIEW: {
        DurableJobStatus.QUEUED,
        DurableJobStatus.CANCELLED,
    },
    DurableJobStatus.COMPLETED: set(),
    DurableJobStatus.FAILED: {DurableJobStatus.QUEUED},
    DurableJobStatus.CANCELLED: set(),
}


class InvalidJobTransition(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_transition(current: str, target: str) -> None:
    current_status = DurableJobStatus(current)
    target_status = DurableJobStatus(target)
    if target_status == current_status:
        return
    if target_status not in ALLOWED_TRANSITIONS[current_status]:
        raise InvalidJobTransition(
            f"invalid job transition: {current_status.value} -> {target_status.value}"
        )


class JobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_project(
        self,
        *,
        external_id: str,
        airtable_record_id: str,
        name: str,
        mode: str,
        original_video_url: str,
        product_image_url: str,
        product_listing_url: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> ProjectRecord:
        async with self._session_factory() as session:
            record = ProjectRecord(
                external_id=external_id,
                airtable_record_id=airtable_record_id,
                name=name,
                mode=mode,
                status="素材准备中",
                original_video_url=original_video_url,
                product_image_url=product_image_url,
                product_listing_url=product_listing_url,
                metadata_json=metadata or {},
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def find_project_by_external_id(
        self, external_id: str
    ) -> ProjectRecord | None:
        async with self._session_factory() as session:
            query = select(ProjectRecord).where(
                ProjectRecord.external_id == external_id
            )
            return await session.scalar(query)

    async def get_project(self, project_id: uuid.UUID) -> ProjectRecord | None:
        async with self._session_factory() as session:
            return await session.get(ProjectRecord, project_id)

    async def attach_external_id(
        self,
        project_id: uuid.UUID,
        *,
        external_id: str,
        product_listing_url: str | None,
    ) -> ProjectRecord:
        async with self._session_factory() as session:
            project = await session.get(ProjectRecord, project_id)
            if project is None:
                raise ValueError(f"Project not found: {project_id}")
            project.external_id = external_id
            project.product_listing_url = product_listing_url
            project.updated_at = _utcnow()
            await session.commit()
            await session.refresh(project)
            return project

    async def latest_job_for_project(
        self, project_id: uuid.UUID
    ) -> JobRecord | None:
        async with self._session_factory() as session:
            query = (
                select(JobRecord)
                .where(JobRecord.project_id == project_id)
                .order_by(JobRecord.created_at.desc())
                .limit(1)
            )
            return await session.scalar(query)

    async def create_job(
        self,
        *,
        project_id: uuid.UUID | None,
        job_type: str,
        queue_name: str,
        payload: dict[str, Any],
        max_attempts: int,
    ) -> JobRecord:
        async with self._session_factory() as session:
            record = JobRecord(
                project_id=project_id,
                job_type=job_type,
                queue_name=queue_name,
                status=DurableJobStatus.PENDING.value,
                payload=payload,
                max_attempts=max_attempts,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def mark_queued(self, job_id: uuid.UUID) -> JobRecord:
        return await self.update(job_id, status=DurableJobStatus.QUEUED)

    async def get(self, job_id: uuid.UUID) -> JobRecord | None:
        async with self._session_factory() as session:
            return await session.get(JobRecord, job_id)

    async def update(
        self,
        job_id: uuid.UUID,
        *,
        status: DurableJobStatus | str | None = None,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        clear_lease: bool = False,
    ) -> JobRecord:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id, with_for_update=True)
            if record is None:
                raise KeyError(f"job not found: {job_id}")

            now = _utcnow()
            if status is not None:
                target = DurableJobStatus(status)
                validate_transition(record.status, target.value)
                record.status = target.value
                if target == DurableJobStatus.PROCESSING and record.started_at is None:
                    record.started_at = now
                if target in TERMINAL_STATUSES:
                    record.completed_at = now
                    clear_lease = True
                if target in {
                    DurableJobStatus.WAITING_REVIEW,
                    DurableJobStatus.WAITING_KEYFRAME_REVIEW,
                }:
                    clear_lease = True

            if progress is not None:
                record.progress = max(0.0, min(1.0, progress))
            if result is not None:
                record.result = result
            if error_code is not None:
                record.error_code = error_code
            if error_message is not None:
                record.error_message = error_message
            if clear_lease:
                record.worker_id = None
                record.lease_expires_at = None
            record.updated_at = now

            await session.commit()
            await session.refresh(record)
            return record

    async def claim(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> JobRecord | None:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id, with_for_update=True)
            if record is None:
                return None
            now = _utcnow()
            lease_expired = (
                record.lease_expires_at is not None
                and record.lease_expires_at <= now
            )
            if record.status == DurableJobStatus.PROCESSING.value and not lease_expired:
                return None
            if record.status not in {
                DurableJobStatus.QUEUED.value,
                DurableJobStatus.PROCESSING.value,
            }:
                return None
            if record.attempt_count >= record.max_attempts:
                record.status = DurableJobStatus.FAILED.value
                record.error_code = "MAX_ATTEMPTS_EXCEEDED"
                record.error_message = "Job exceeded maximum execution attempts"
                record.completed_at = now
                await session.commit()
                return None

            record.status = DurableJobStatus.PROCESSING.value
            record.worker_id = worker_id
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.attempt_count += 1
            record.started_at = record.started_at or now
            record.updated_at = now
            await session.commit()
            await session.refresh(record)
            return record

    async def renew_lease(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id, with_for_update=True)
            if (
                record is None
                or record.status != DurableJobStatus.PROCESSING.value
                or record.worker_id != worker_id
            ):
                return False
            record.lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
            record.updated_at = _utcnow()
            await session.commit()
            return True

    async def find_expired_jobs(
        self,
        *,
        queue_name: str,
        limit: int = 100,
    ) -> list[uuid.UUID]:
        async with self._session_factory() as session:
            now = _utcnow()
            query = (
                select(JobRecord)
                .where(
                    JobRecord.queue_name == queue_name,
                    JobRecord.status == DurableJobStatus.PROCESSING.value,
                    JobRecord.lease_expires_at <= now,
                    JobRecord.attempt_count < JobRecord.max_attempts,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            records = list((await session.scalars(query)).all())
            ids: list[uuid.UUID] = []
            for record in records:
                record.status = DurableJobStatus.QUEUED.value
                record.worker_id = None
                record.lease_expires_at = None
                record.updated_at = now
                ids.append(record.id)
            await session.commit()
            return ids

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        stage: str = "workflow",
    ) -> JobRecord:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id, with_for_update=True)
            if record is None:
                raise KeyError(f"job not found: {job_id}")

            can_retry = retryable and record.attempt_count < record.max_attempts
            target = (
                DurableJobStatus.QUEUED
                if can_retry
                else DurableJobStatus.FAILED
            )
            validate_transition(record.status, target.value)
            record.status = target.value
            record.error_code = error_code
            record.error_message = error_message
            record.worker_id = None
            record.lease_expires_at = None
            record.updated_at = _utcnow()
            if target == DurableJobStatus.FAILED:
                record.completed_at = _utcnow()

            session.add(
                FailureEventRecord(
                    project_id=record.project_id,
                    job_id=record.id,
                    stage=stage,
                    failure_type=error_code,
                    severity="blocking",
                    retryable=can_retry,
                    retry_count=record.attempt_count,
                    message=error_message,
                    details={},
                )
            )
            await session.commit()
            await session.refresh(record)
            return record
