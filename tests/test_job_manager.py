from services.job_manager import JobManager, JobStatus


def test_job_lifecycle_and_status_coercion():
    manager = JobManager()
    job_id = manager.create_job(job_type="workflow")

    assert manager.update_job(job_id, status="processing", progress=0.4)
    job = manager.get_job(job_id)
    assert job is not None
    assert job["status"] == JobStatus.PROCESSING
    assert job["progress"] == 0.4
    assert job["started_at"] is not None

    assert manager.update_job(job_id, status="waiting_review", progress=0.7)
    job = manager.get_job(job_id)
    assert job is not None
    assert job["status"] == JobStatus.WAITING_REVIEW

    assert manager.update_job(job_id, status="completed", progress=1.0)
    job = manager.get_job(job_id)
    assert job is not None
    assert job["status"] == JobStatus.COMPLETED
    assert job["completed_at"] is not None


def test_job_progress_is_clamped():
    manager = JobManager()
    job_id = manager.create_job()
    manager.update_job(job_id, progress=2.0)

    job = manager.get_job(job_id)
    assert job is not None
    assert job["progress"] == 1.0
