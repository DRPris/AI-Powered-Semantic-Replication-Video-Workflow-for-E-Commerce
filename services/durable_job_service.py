"""Facade used by API and workers for durable job lifecycle."""

from __future__ import annotations

import uuid
from typing import Any

from config import settings
from persistence.database import get_session_factory
from persistence.job_repository import DurableJobStatus, JobRepository
from persistence.models import JobRecord, ProjectRecord
from services.durable_queue import DurableJobQueue


class DurableJobService:
    def __init__(
        self,
        repository: JobRepository | None = None,
        queue: DurableJobQueue | None = None,
    ) -> None:
        self.repository = repository or JobRepository(get_session_factory())
        self.queue = queue or DurableJobQueue(
            settings.REDIS_URL,
            settings.JOB_QUEUE_NAME,
        )

    async def create_workflow_job(
        self,
        *,
        external_project_id: str,
        airtable_record_id: str,
        name: str,
        mode: str,
        video_url: str,
        product_image_url: str,
        product_listing_url: str | None,
        payload: dict[str, Any],
    ) -> tuple[ProjectRecord, JobRecord]:
        project = await self.repository.create_project(
            external_id=external_project_id,
            airtable_record_id=airtable_record_id,
            name=name,
            mode=mode,
            original_video_url=video_url,
            product_image_url=product_image_url,
            product_listing_url=product_listing_url,
        )
        job = await self.repository.create_job(
            project_id=project.id,
            job_type="full_workflow",
            queue_name=settings.JOB_QUEUE_NAME,
            payload=payload,
            max_attempts=settings.JOB_MAX_ATTEMPTS,
        )
        await self.repository.mark_queued(job.id)
        await self.queue.enqueue(job.id)
        return project, job

    async def create_workflow_job_for_existing_project(
        self,
        *,
        project_id: str | uuid.UUID,
        external_project_id: str,
        product_listing_url: str | None,
        payload: dict[str, Any],
    ) -> tuple[ProjectRecord, JobRecord]:
        project_uuid = uuid.UUID(str(project_id))
        project = await self.repository.attach_external_id(
            project_uuid,
            external_id=external_project_id,
            product_listing_url=product_listing_url,
        )
        job = await self.repository.create_job(
            project_id=project.id,
            job_type="full_workflow",
            queue_name=settings.JOB_QUEUE_NAME,
            payload=payload,
            max_attempts=settings.JOB_MAX_ATTEMPTS,
        )
        await self.repository.mark_queued(job.id)
        await self.queue.enqueue(job.id)
        return project, job

    async def create_project_job(
        self,
        *,
        project_id: str | uuid.UUID,
        job_type: str,
        payload: dict[str, Any],
    ) -> JobRecord:
        project_uuid = uuid.UUID(str(project_id))
        project = await self.repository.get_project(project_uuid)
        if project is None:
            raise ValueError(f"Project not found: {project_id}")
        job = await self.repository.create_job(
            project_id=project.id,
            job_type=job_type,
            queue_name=settings.JOB_QUEUE_NAME,
            payload=payload,
            max_attempts=settings.JOB_MAX_ATTEMPTS,
        )
        await self.repository.mark_queued(job.id)
        await self.queue.enqueue(job.id)
        return job

    async def find_existing_workflow(
        self, external_project_id: str
    ) -> tuple[ProjectRecord, JobRecord] | None:
        project = await self.repository.find_project_by_external_id(
            external_project_id
        )
        if project is None:
            return None
        job = await self.repository.latest_job_for_project(project.id)
        if job is None:
            return None
        return project, job

    async def get(self, job_id: str | uuid.UUID) -> JobRecord | None:
        return await self.repository.get(uuid.UUID(str(job_id)))

    async def update(
        self,
        job_id: str | uuid.UUID,
        *,
        status: DurableJobStatus | str | None = None,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JobRecord:
        return await self.repository.update(
            uuid.UUID(str(job_id)),
            status=status,
            progress=progress,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )

    async def close(self) -> None:
        await self.queue.close()
