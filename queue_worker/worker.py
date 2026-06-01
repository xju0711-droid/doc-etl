"""
任务队列层。

新增：
  - request_shutdown()：标记关闭，拒绝新任务
  - wait_partial()：完成率 >= threshold AND 静默 >= stable_timeout 才算稳定
  - shutdown()：取消未开始的任务，等待运行中任务完成（优雅关闭）
"""

import threading
import time
import traceback
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from enum import Enum

from config import cfg

logger = logging.getLogger(__name__)

from parsers import parse
from extractor import extract_document, ExtractionSchema, ExtractionResult
from extractor.llm_client import LLMClient
from cache import get_cache
from evaluator import evaluate


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    FAILED  = "failed"


@dataclass
class JobResult:
    job_id: str
    source: str
    status: JobStatus
    result: ExtractionResult | None = None
    quality: dict | None = None
    error: str | None = None


_TERMINAL = {JobStatus.DONE, JobStatus.FAILED}


class DocumentQueue:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=cfg.WORKER_THREADS)
        self._jobs: dict[str, JobResult] = {}
        self._futures: dict[str, Future] = {}
        self._client = LLMClient()
        self._cache = get_cache()
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()          # 保护 _last_completion_time
        self._last_completion_time = time.time()

    # ── 提交 / 查询 ───────────────────────────────────────────

    def submit(self, source: str, schema: ExtractionSchema) -> str:
        """提交任务；若已请求关闭则拒绝并抛出 RuntimeError。"""
        if self._shutdown_event.is_set():
            raise RuntimeError(f"队列已请求关闭，拒绝任务: {source}")
        job_id = str(uuid.uuid4())[:8]
        self._jobs[job_id] = JobResult(job_id=job_id, source=source, status=JobStatus.PENDING)
        future = self._executor.submit(self._run, job_id, source, schema)
        self._futures[job_id] = future
        return job_id

    def get(self, job_id: str) -> JobResult | None:
        return self._jobs.get(job_id)

    def wait(self, job_id: str) -> JobResult:
        """阻塞等待单个任务完成（用于 run 命令）。"""
        future = self._futures.get(job_id)
        if future:
            future.result()
        return self._jobs[job_id]

    # ── 批次稳定等待 ─────────────────────────────────────────

    def wait_partial(
        self,
        job_ids: list[str],
        threshold: float = 0.95,
        stable_timeout: float = 30.0,
        poll_interval: float = 2.0,
    ) -> list[JobResult]:
        """
        等待批次达到稳定状态，不强求 100% 完成。

        退出条件（满足任一）：
          1. 所有任务进入终态（DONE / FAILED）
          2. 完成率 >= threshold  AND  最近 stable_timeout 秒无新进展
          3. 收到关闭信号（request_shutdown 被调用）
        """
        total = len(job_ids)
        last_done_count = 0
        last_progress_time = time.time()

        while True:
            done_count = sum(
                1 for jid in job_ids if self._jobs[jid].status in _TERMINAL
            )

            if done_count != last_done_count:
                last_done_count = done_count
                last_progress_time = time.time()
                logger.info("批次进度: %d/%d (%.0f%%)", done_count, total, done_count / total * 100)

            quiet_secs = time.time() - last_progress_time
            completion_rate = done_count / total if total else 1.0
            all_done = done_count == total
            is_stable = completion_rate >= threshold and quiet_secs >= stable_timeout
            shutdown_req = self._shutdown_event.is_set()

            if all_done or is_stable or shutdown_req:
                reason = (
                    "全部完成" if all_done else
                    f"稳定退出 ({done_count}/{total}, 静默 {quiet_secs:.0f}s)" if is_stable else
                    "收到关闭信号"
                )
                logger.info("批次结束 [%s]", reason)
                return [self._jobs[jid] for jid in job_ids]

            time.sleep(poll_interval)

    # ── 优雅关闭 ──────────────────────────────────────────────

    def request_shutdown(self) -> None:
        """标记关闭意图：拒绝新任务，当前任务继续跑完。"""
        self._shutdown_event.set()
        logger.info("关闭信号已接收，不再接受新任务，等待运行中任务完成...")

    def shutdown(self) -> None:
        """等待运行中任务完成，取消尚未开始的任务（Python 3.9+）。"""
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            # Python < 3.9 不支持 cancel_futures 参数
            self._executor.shutdown(wait=True)

    # ── 任务执行 ──────────────────────────────────────────────

    def _run(self, job_id: str, source: str, schema: ExtractionSchema) -> None:
        job = self._jobs[job_id]
        job.status = JobStatus.RUNNING
        logger.info("任务 %s 开始: %s", job_id, source)

        try:
            text = parse(source)
            logger.debug("任务 %s 解析完成，文本 %d 字符", job_id, len(text))

            cached = self._cache.get(text, schema)
            if cached:
                logger.info("任务 %s 缓存命中，跳过 LLM", job_id)
                job.result = cached
                job.quality = evaluate(cached, schema).__dict__
                job.status = JobStatus.DONE
                self._touch_progress()
                return

            result = extract_document(text, schema, self._client)
            self._cache.set(text, schema, result)

            quality = evaluate(result, schema)
            logger.info(
                "任务 %s 完成，模型: %s，质量分: %s，解析策略: %s",
                job_id, result.model_used, quality.score, result.parse_strategy,
            )

            job.result = result
            job.quality = quality.__dict__
            job.status = JobStatus.DONE

        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            logger.error("任务 %s 失败: %s", job_id, e, exc_info=True)

        finally:
            self._touch_progress()

    def _touch_progress(self) -> None:
        with self._lock:
            self._last_completion_time = time.time()