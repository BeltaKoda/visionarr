"""
Visionarr Queue Manager

Job queue with worker for controlled processing of conversion jobs.
Prevents race conditions and resource exhaustion.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


class JobStatus(Enum):
    """Status of a conversion job."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ConversionJob:
    """A conversion job in the queue."""
    file_path: Path
    media_id: int
    title: str
    monitor_type: Optional[str] = None  # "radarr" or "sonarr"
    status: JobStatus = JobStatus.PENDING
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    @property
    def duration_seconds(self) -> Optional[float]:
        """Get processing duration in seconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None


class QueueManager:
    """
    Manages the conversion job queue with worker threads.
    
    Features:
    - FIFO queue with configurable concurrency
    - Progress tracking per job
    - Automatic retry with exponential backoff
    """
    
    def __init__(
        self,
        process_callback: Callable[[ConversionJob], bool],
        on_complete_callback: Optional[Callable[[ConversionJob], None]] = None,
        on_fail_callback: Optional[Callable[[ConversionJob], None]] = None,
        max_workers: int = 1,
        max_retries: int = 2
    ):
        """
        Initialize the queue manager.
        
        Args:
            process_callback: Function to call to process each job
            on_complete_callback: Optional callback when job completes
            on_fail_callback: Optional callback when job fails
            max_workers: Maximum concurrent workers (default 1)
            max_retries: Maximum retry attempts for failed jobs
        """
        self.process_callback = process_callback
        self.on_complete_callback = on_complete_callback
        self.on_fail_callback = on_fail_callback
        self.max_workers = max_workers
        self.max_retries = max_retries
        
        self._queue: queue.Queue[ConversionJob] = queue.Queue()
        self._workers: List[threading.Thread] = []
        self._running = False
        self._jobs: List[ConversionJob] = []
        self._lock = threading.Lock()
    
    def add_job(self, file_path: Path, media_id: int, title: str, monitor_type: Optional[str] = None) -> ConversionJob:
        """Add a new job to the queue."""
        job = ConversionJob(
            file_path=file_path,
            media_id=media_id,
            title=title,
            monitor_type=monitor_type
        )
        
        with self._lock:
            # Check for duplicates
            for existing in self._jobs:
                if existing.file_path == file_path and existing.status in (
                    JobStatus.PENDING, JobStatus.PROCESSING
                ):
                    logger.debug(f"Job already queued: {file_path}")
                    return existing

            self._jobs.append(job)
            self._queue.put(job)

        logger.info(f"Job queued: {title}")
        return job
    
    def start(self) -> None:
        """Start the worker threads."""
        if self._running:
            return
        
        self._running = True
        
        for i in range(self.max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"QueueWorker-{i}",
                daemon=True
            )
            worker.start()
            self._workers.append(worker)
        
        logger.info(f"Started {self.max_workers} queue worker(s)")
    
    def stop(self, wait: bool = True) -> None:
        """Stop the worker threads."""
        self._running = False
        
        # Add None poison pills to unblock workers
        for _ in self._workers:
            self._queue.put(None)  # type: ignore
        
        if wait:
            for worker in self._workers:
                worker.join(timeout=5)
        
        self._workers.clear()
        logger.info("Queue workers stopped")
    
    def _worker_loop(self) -> None:
        """Worker thread main loop."""
        while self._running:
            try:
                job = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            
            if job is None:  # Poison pill
                break
            
            self._process_job(job)
            self._queue.task_done()
    
    def _process_job(self, job: ConversionJob, retry_count: int = 0) -> None:
        """Process a single job with retry logic."""
        with self._lock:
            job.status = JobStatus.PROCESSING
            job.started_at = datetime.now()

        logger.info(f"Processing: {job.title}")

        try:
            success = self.process_callback(job)

            with self._lock:
                if success:
                    job.status = JobStatus.COMPLETED
                    job.completed_at = datetime.now()
                else:
                    job.status = JobStatus.SKIPPED
                    job.completed_at = datetime.now()

            if success:
                duration_str = f" ({job.duration_seconds:.1f}s)" if job.duration_seconds else ""
                logger.info(f"Completed: {job.title}{duration_str}")
                if self.on_complete_callback:
                    self.on_complete_callback(job)
            else:
                logger.info(f"Skipped: {job.title}")

        except Exception as e:
            with self._lock:
                job.error_message = str(e)

            if retry_count < self.max_retries:
                # Exponential backoff - uses shorter intervals
                wait_time = min(2 ** retry_count * 5, 30)  # 5s, 10s, 20s, max 30s
                logger.warning(
                    f"Job failed, retrying in {wait_time}s "
                    f"(attempt {retry_count + 1}/{self.max_retries}): {e}"
                )
                time.sleep(wait_time)
                self._process_job(job, retry_count + 1)
            else:
                with self._lock:
                    job.status = JobStatus.FAILED
                    job.completed_at = datetime.now()
                logger.error(f"Job failed after {self.max_retries} retries: {job.title}")

                if self.on_fail_callback:
                    self.on_fail_callback(job)
    
    def get_pending_count(self) -> int:
        """Get count of pending jobs."""
        return self._queue.qsize()
    
    def get_jobs(self, status: Optional[JobStatus] = None) -> List[ConversionJob]:
        """Get list of jobs, optionally filtered by status."""
        with self._lock:
            if status:
                return [j for j in self._jobs if j.status == status]
            return list(self._jobs)
    
    def clear_completed(self) -> int:
        """Clear completed jobs from history. Returns count cleared."""
        with self._lock:
            before = len(self._jobs)
            self._jobs = [
                j for j in self._jobs 
                if j.status not in (JobStatus.COMPLETED, JobStatus.SKIPPED)
            ]
            return before - len(self._jobs)
    
    def wait_for_completion(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for all queued jobs to complete.
        
        Returns True if completed, False if timed out.
        """
        try:
            self._queue.join()
            return True
        except Exception:
            return False
