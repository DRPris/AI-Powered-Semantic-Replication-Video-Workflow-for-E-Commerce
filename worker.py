"""Durable workflow worker.

Run with:
    python worker.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import uuid
from contextlib import suppress
from typing import Any

from config import settings
from persistence.database import close_database, get_session_factory
from persistence.job_repository import DurableJobStatus, JobRepository
from services.durable_queue import DurableJobQueue
from workflows.stage4_generation import run_stage4
from workflows.stage5_composition import run_stage5
from workflows.full_workflow import run_full_workflow
from workflows.full_workflow import run_post_generation

# 周期性维护间隔：租约回收 + queued 孤儿 reconcile
MAINTENANCE_INTERVAL_SECONDS = 60
# queued 状态超过此时长仍未被消费则视为孤儿（Redis 通知丢失），重新入队
STALE_QUEUED_SECONDS = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class WorkflowWorker:
    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.repository = JobRepository(get_session_factory())
        self.queue = DurableJobQueue(settings.REDIS_URL, settings.JOB_QUEUE_NAME)
        self._stopping = asyncio.Event()

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        # 注意：不检查 _stopping —— 优雅关闭期间当前 job 仍在执行，
        # 心跳必须持续续租，否则租约过期会被其他 worker 重复认领。
        # 任务结束时由 _execute 的 finally 统一 cancel。
        interval = max(10, settings.JOB_LEASE_SECONDS // 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await self.repository.renew_lease(
                job_id,
                worker_id=self.worker_id,
                lease_seconds=settings.JOB_LEASE_SECONDS,
            )
            if not renewed:
                return

    async def _execute(self, job_id: uuid.UUID) -> None:
        job = await self.repository.claim(
            job_id,
            worker_id=self.worker_id,
            lease_seconds=settings.JOB_LEASE_SECONDS,
        )
        if job is None:
            return

        payload = job.payload
        failure_handled = False

        async def update_job(**values: Any) -> None:
            nonlocal failure_handled
            status = values.get("status")
            if status == DurableJobStatus.FAILED.value:
                failure_handled = True
                failed_job = await self.repository.fail(
                    job.id,
                    error_code=values.get(
                        "error_code", "WORKFLOW_EXECUTION_FAILED"
                    ),
                    error_message=values.get("error_message", "Workflow failed"),
                    retryable=True,
                )
                if failed_job.status == DurableJobStatus.QUEUED.value:
                    await self.queue.enqueue(failed_job.id)
                return
            await self.repository.update(
                job.id,
                status=status,
                progress=values.get("progress"),
                result=values.get("result"),
                error_code=values.get("error_code"),
                error_message=values.get("error_message"),
            )

        heartbeat = asyncio.create_task(self._heartbeat(job.id))
        try:
            if job.job_type == "full_workflow":
                await run_full_workflow(
                    project_id=payload["project_id"],
                    video_url=payload["video_url"],
                    product_image_url=payload["product_image_url"],
                    product_image_urls=payload.get("product_image_urls"),
                    mode=payload["mode"],
                    product_listing_url=payload.get("product_listing_url"),
                    replicate_hook=payload.get("replicate_hook"),
                    update_job=update_job,
                )
            elif job.job_type == "stage4_to_final":
                await update_job(
                    status=DurableJobStatus.PROCESSING.value,
                    progress=0.1,
                    result={"message": "Starting video generation"},
                )
                await run_stage4(
                    project_id=payload["project_id"],
                    platform=payload.get("platform", "seedance"),
                )
                await update_job(
                    status=DurableJobStatus.PROCESSING.value,
                    progress=0.6,
                    result={
                        "message": (
                            "Video generation done, starting AI clip editing "
                            "and composition"
                        )
                    },
                )
                stage5_result = await run_post_generation(
                    payload["project_id"],
                    update_job,
                )
                await update_job(
                    status=DurableJobStatus.COMPLETED.value,
                    progress=1.0,
                    result={
                        "project_id": payload["project_id"],
                        "message": "视频生成 + AI 剪辑 + 合成全部完成",
                        "final_video_url": stage5_result.get("final_video_url"),
                        "duration": stage5_result.get("duration"),
                    },
                )
            elif job.job_type == "stage5_composition":
                await update_job(
                    status=DurableJobStatus.PROCESSING.value,
                    progress=0.1,
                    result={"message": "Starting video composition"},
                )
                result = await run_stage5(
                    project_id=payload["project_id"],
                    skip_clip_editing=payload.get("skip_clip_editing", False),
                )
                await update_job(
                    status=DurableJobStatus.COMPLETED.value,
                    progress=1.0,
                    result=result,
                )
            else:
                await self.repository.fail(
                    job.id,
                    error_code="UNSUPPORTED_JOB_TYPE",
                    error_message=f"Unsupported job type: {job.job_type}",
                    retryable=False,
                )
        except Exception as exc:
            if not failure_handled:
                failed_job = await self.repository.fail(
                    job.id,
                    error_code="WORKER_EXECUTION_FAILED",
                    error_message=str(exc),
                    retryable=True,
                )
                if failed_job.status == DurableJobStatus.QUEUED.value:
                    await self.queue.enqueue(failed_job.id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def recover_expired(self) -> None:
        """回收租约过期的 processing job + 滞留的 queued 孤儿 job。"""
        expired = await self.repository.find_expired_jobs(
            queue_name=settings.JOB_QUEUE_NAME
        )
        for job_id in expired:
            await self.queue.enqueue(job_id)
        if expired:
            logger.warning("Requeued %d jobs with expired leases", len(expired))

        # queued 孤儿：Redis 通知丢失（出队后崩溃 / enqueue 失败）时的兜底。
        # claim() 有行锁+状态检查，重复入队是安全的。
        stale = await self.repository.find_stale_queued_jobs(
            queue_name=settings.JOB_QUEUE_NAME,
            stale_seconds=STALE_QUEUED_SECONDS,
        )
        for job_id in stale:
            await self.queue.enqueue(job_id)
        if stale:
            logger.warning("Re-enqueued %d stale queued jobs", len(stale))

    async def _maintenance_loop(self) -> None:
        """周期执行租约回收，防止 worker 崩溃后 job 卡死到进程重启。"""
        while not self._stopping.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=MAINTENANCE_INTERVAL_SECONDS,
                )
            if self._stopping.is_set():
                return
            try:
                await self.recover_expired()
            except Exception:
                logger.exception("Periodic lease recovery failed")

    def request_stop(self) -> None:
        logger.info("Shutdown signal received, finishing current job...")
        self._stopping.set()

    async def run(self) -> None:
        logger.info("Worker started: %s", self.worker_id)
        await self.recover_expired()
        maintenance = asyncio.create_task(self._maintenance_loop())
        try:
            while not self._stopping.is_set():
                job_id = await self.queue.dequeue(
                    timeout_seconds=settings.WORKER_POLL_TIMEOUT_SECONDS
                )
                if job_id is not None:
                    # dequeue 后立即执行；claim 失败（已被其他 worker 认领/终态）会静默返回
                    await self._execute(job_id)
        finally:
            maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance
            await self.queue.close()
            await close_database()
            logger.info("Worker stopped: %s", self.worker_id)


async def main() -> None:
    worker = WorkflowWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
