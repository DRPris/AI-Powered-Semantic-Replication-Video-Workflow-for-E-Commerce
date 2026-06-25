"""
异步任务管理器

提供简单的内存任务管理功能，用于跟踪长时间运行的视频生成任务。
"""

import uuid
import logging
from datetime import UTC, datetime
from typing import Any, Optional
from enum import Enum


logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    PROCESSING = "processing"
    WAITING_REVIEW = "waiting_review"
    WAITING_KEYFRAME_REVIEW = "waiting_keyframe_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobManager:
    """
    简单的内存任务管理器
    
    用于管理长时间运行的异步任务（如视频生成、视频合成等）。
    提供任务创建、状态更新、进度跟踪等功能。
    
    注意：这是一个内存实现，服务重启后任务数据会丢失。
    生产环境建议使用 Redis 或数据库持久化。
    """

    def __init__(self):
        """初始化任务管理器"""
        self._jobs: dict[str, dict[str, Any]] = {}
        self.logger = logging.getLogger(__name__)

    def create_job(
        self,
        job_type: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        创建新任务

        Args:
            job_type: 任务类型（如 "video_generation", "video_composition"）
            metadata: 任务元数据（如 project_id, user_id 等）

        Returns:
            任务 ID
        """
        job_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        
        self._jobs[job_id] = {
            "id": job_id,
            "type": job_type,
            "status": JobStatus.PENDING,
            "progress": 0.0,
            "result": None,
            "error": None,
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        
        self.logger.info(f"创建任务 {job_id}，类型: {job_type}")
        return job_id

    def update_job(
        self,
        job_id: str,
        status: Optional[JobStatus] = None,
        progress: Optional[float] = None,
        result: Optional[Any] = None,
        error: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """
        更新任务状态

        Args:
            job_id: 任务 ID
            status: 新状态
            progress: 进度（0-1）
            result: 任务结果
            error: 错误信息
            **kwargs: 其他要更新的字段

        Returns:
            是否更新成功
        """
        if job_id not in self._jobs:
            self.logger.warning(f"尝试更新不存在的任务: {job_id}")
            return False

        job = self._jobs[job_id]
        now = datetime.now(UTC).isoformat()

        if status is not None:
            status = JobStatus(status)
            job["status"] = status
            
            # 自动更新时间戳
            if status == JobStatus.PROCESSING and job["started_at"] is None:
                job["started_at"] = now
            elif status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                job["completed_at"] = now

        if progress is not None:
            job["progress"] = max(0.0, min(1.0, progress))

        if result is not None:
            job["result"] = result

        if error is not None:
            job["error"] = error

        # 更新其他字段
        for key, value in kwargs.items():
            job[key] = value

        job["updated_at"] = now
        
        self.logger.debug(
            f"更新任务 {job_id}，状态: {job['status']}，进度: {job['progress']:.1%}"
        )
        return True

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """
        获取任务信息

        Args:
            job_id: 任务 ID

        Returns:
            任务信息字典，不存在则返回 None
        """
        job = self._jobs.get(job_id)
        if job:
            return job.copy()
        return None

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        job_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        列出任务

        Args:
            status: 按状态筛选
            job_type: 按类型筛选
            limit: 返回数量限制

        Returns:
            任务列表
        """
        jobs = list(self._jobs.values())
        
        if status:
            jobs = [j for j in jobs if j["status"] == status]
        
        if job_type:
            jobs = [j for j in jobs if j["type"] == job_type]
        
        # 按创建时间倒序
        jobs.sort(key=lambda j: j["created_at"], reverse=True)
        
        return jobs[:limit]

    def delete_job(self, job_id: str) -> bool:
        """
        删除任务

        Args:
            job_id: 任务 ID

        Returns:
            是否删除成功
        """
        if job_id in self._jobs:
            del self._jobs[job_id]
            self.logger.info(f"删除任务 {job_id}")
            return True
        return False

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        清理旧任务

        Args:
            max_age_hours: 最大保留时间（小时）

        Returns:
            清理的任务数量
        """
        from datetime import timedelta
        
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        to_delete = []
        
        for job_id, job in self._jobs.items():
            created_at = datetime.fromisoformat(job["created_at"])
            if created_at < cutoff:
                to_delete.append(job_id)
        
        for job_id in to_delete:
            del self._jobs[job_id]
        
        if to_delete:
            self.logger.info(f"清理了 {len(to_delete)} 个旧任务")
        
        return len(to_delete)

    def get_stats(self) -> dict[str, Any]:
        """
        获取任务统计信息

        Returns:
            统计信息
        """
        total = len(self._jobs)
        status_counts = {
            JobStatus.PENDING: 0,
            JobStatus.PROCESSING: 0,
            JobStatus.COMPLETED: 0,
            JobStatus.FAILED: 0,
            JobStatus.CANCELLED: 0,
        }
        
        for job in self._jobs.values():
            status = job["status"]
            if status in status_counts:
                status_counts[status] += 1
        
        return {
            "total": total,
            "pending": status_counts[JobStatus.PENDING],
            "processing": status_counts[JobStatus.PROCESSING],
            "completed": status_counts[JobStatus.COMPLETED],
            "failed": status_counts[JobStatus.FAILED],
            "cancelled": status_counts[JobStatus.CANCELLED],
        }


# 全局单例实例
job_manager = JobManager()


def get_job_manager() -> JobManager:
    """获取全局任务管理器实例"""
    return job_manager
