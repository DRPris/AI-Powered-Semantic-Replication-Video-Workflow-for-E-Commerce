import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from persistence.database import Base
from persistence.job_repository import (
    DurableJobStatus,
    InvalidJobTransition,
    JobRepository,
)
from persistence import models  # noqa: F401


async def _repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, JobRepository(factory)


def test_durable_job_lifecycle():
    async def scenario():
        engine, repository = await _repository()
        try:
            project = await repository.create_project(
                external_id="portfolio-001",
                airtable_record_id="rec001",
                name="production baseline",
                mode="full",
                original_video_url="https://cdn.example.com/original.mp4",
                product_image_url="https://cdn.example.com/product.png",
                product_listing_url=None,
            )
            job = await repository.create_job(
                project_id=project.id,
                job_type="full_workflow",
                queue_name="test-jobs",
                payload={"project_id": "rec001"},
                max_attempts=3,
            )
            await repository.mark_queued(job.id)
            claimed = await repository.claim(
                job.id,
                worker_id="worker-1",
                lease_seconds=60,
            )
            assert claimed is not None
            assert claimed.status == DurableJobStatus.PROCESSING.value
            assert claimed.attempt_count == 1

            completed = await repository.update(
                job.id,
                status=DurableJobStatus.COMPLETED,
                progress=1.0,
                result={"ok": True},
            )
            assert completed.status == DurableJobStatus.COMPLETED.value
            assert completed.worker_id is None
            assert completed.completed_at is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_invalid_transition_is_rejected():
    async def scenario():
        engine, repository = await _repository()
        try:
            job = await repository.create_job(
                project_id=None,
                job_type="full_workflow",
                queue_name="test-jobs",
                payload={},
                max_attempts=3,
            )
            with pytest.raises(InvalidJobTransition):
                await repository.update(
                    job.id,
                    status=DurableJobStatus.COMPLETED,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retryable_failure_requeues_until_attempt_limit():
    async def scenario():
        engine, repository = await _repository()
        try:
            job = await repository.create_job(
                project_id=None,
                job_type="full_workflow",
                queue_name="test-jobs",
                payload={},
                max_attempts=2,
            )
            await repository.mark_queued(job.id)
            await repository.claim(
                job.id,
                worker_id="worker-1",
                lease_seconds=60,
            )
            retried = await repository.fail(
                job.id,
                error_code="MODEL_TIMEOUT",
                error_message="upstream timeout",
                retryable=True,
            )
            assert retried.status == DurableJobStatus.QUEUED.value

            await repository.claim(
                job.id,
                worker_id="worker-2",
                lease_seconds=60,
            )
            failed = await repository.fail(
                job.id,
                error_code="MODEL_TIMEOUT",
                error_message="upstream timeout",
                retryable=True,
            )
            assert failed.status == DurableJobStatus.FAILED.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_repository_creates_project_scoped_stage_job():
    async def scenario():
        engine, repository = await _repository()
        try:
            project = await repository.create_project(
                external_id="portfolio-002",
                airtable_record_id="rec002",
                name="stage job",
                mode="full",
                original_video_url="https://cdn.example.com/original.mp4",
                product_image_url="https://cdn.example.com/product.png",
                product_listing_url=None,
            )

            job = await repository.create_job(
                project_id=project.id,
                job_type="stage4_to_final",
                queue_name="test-jobs",
                payload={"project_id": str(project.id), "platform": "seedance"},
                max_attempts=3,
            )
            queued = await repository.mark_queued(job.id)

            assert queued.project_id == project.id
            assert queued.job_type == "stage4_to_final"
            assert queued.status == DurableJobStatus.QUEUED.value
        finally:
            await engine.dispose()

    asyncio.run(scenario())
