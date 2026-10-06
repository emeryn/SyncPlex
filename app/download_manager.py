"""Server-side download queue.

Files are written to `<name>.part` and renamed when complete, so an interrupted
download (network drop, pause, restart of the worker) resumes with an HTTP Range request.
"""
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

import httpx

import config
import plex_service as plex

log = logging.getLogger("syncplex.downloads")

QUEUED, DOWNLOADING, COMPLETED, FAILED, CANCELLED = "queued", "downloading", "completed", "failed", "cancelled"
ACTIVE_STATES = (QUEUED, DOWNLOADING)
MAX_ATTEMPTS = 5
CHUNK_SIZE = 1024 * 1024


@dataclass
class Task:
    server_id: str
    media_key: str
    user: str
    token: str = field(repr=False)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = QUEUED
    title: str = ""
    thumb: str = ""
    server_name: str = ""
    path: str = ""
    total: int = 0
    downloaded: int = 0
    speed: float = 0
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float = 0

    def public(self):
        data = asdict(self)
        del data["token"]
        data["progress"] = round(self.downloaded * 100 / self.total, 1) if self.total else 0
        return data


class _Cancelled(Exception):
    pass


class _Paused(Exception):
    pass


class DownloadManager:
    def __init__(self, workers):
        self.tasks: dict[str, Task] = {}
        self.queue: list[str] = []
        self.paused = False
        self.lock = threading.Lock()
        self.wakeup = threading.Condition(self.lock)
        self.client = httpx.Client(headers=config.PLEX_HEADERS, timeout=httpx.Timeout(30, connect=15), follow_redirects=True)
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------ public API

    def status(self):
        with self.lock:
            tasks = sorted(self.tasks.values(), key=lambda t: t.created, reverse=True)
            return {
                "paused": self.paused,
                "queued": len(self.queue),
                "active": sum(t.status == DOWNLOADING for t in tasks),
                "tasks": [t.public() for t in tasks],
            }

    def enqueue(self, server_id, media_key, user, token):
        with self.lock:
            for t in self.tasks.values():
                if t.server_id == server_id and t.media_key == media_key and t.status in ACTIVE_STATES:
                    return False
            task = Task(server_id=server_id, media_key=media_key, user=user, token=token, title=f"Item {media_key}")
            self.tasks[task.id] = task
            self.queue.append(task.id)
            self.wakeup.notify()
        return True

    def set_paused(self, paused):
        with self.lock:
            self.paused = paused
            self.wakeup.notify_all()

    def cancel_all(self):
        with self.lock:
            for task_id in self.queue:
                self.tasks[task_id].status = CANCELLED
            self.queue.clear()
            for t in self.tasks.values():
                if t.status == DOWNLOADING:
                    t.status = CANCELLED  # the worker notices and removes the partial file

    def clear_finished(self):
        with self.lock:
            self.tasks = {tid: t for tid, t in self.tasks.items() if t.status in ACTIVE_STATES}

    def cancel(self, task_id):
        with self.lock:
            task = self._get(task_id)
            if task.status in ACTIVE_STATES:
                if task_id in self.queue:
                    self.queue.remove(task_id)
                task.status = CANCELLED

    def retry(self, task_id):
        with self.lock:
            task = self._get(task_id)
            if task.status in (FAILED, CANCELLED):
                task.status, task.error, task.speed = QUEUED, "", 0
                self.queue.append(task_id)
                self.wakeup.notify()

    def remove(self, task_id):
        with self.lock:
            task = self._get(task_id)
            if task.status in ACTIVE_STATES:
                if task_id in self.queue:
                    self.queue.remove(task_id)
                task.status = CANCELLED
            self.tasks.pop(task_id, None)

    def _get(self, task_id):
        task = self.tasks.get(task_id)
        if not task:
            raise KeyError(task_id)
        return task

    # ------------------------------------------------------------ worker

    def _worker(self):
        while True:
            with self.lock:
                while self.paused or not self.queue:
                    self.wakeup.wait()
                task = self.tasks.get(self.queue.pop(0))
                if not task or task.status != QUEUED:
                    continue
                task.status = DOWNLOADING
            try:
                self._process(task)
            except _Paused:
                with self.lock:
                    if task.status == DOWNLOADING:
                        task.status, task.speed = QUEUED, 0
                        self.queue.insert(0, task.id)
            except _Cancelled:
                part_path = os.path.join(config.DOWNLOAD_DIR, task.path) + ".part"
                if task.path and os.path.exists(part_path):
                    os.remove(part_path)
                log.info("Cancelled %s", task.title)
            except Exception as e:
                if isinstance(e, (plex.PlexError, httpx.HTTPError)):
                    log.warning("Download failed: %s (%s)", task.title, e)
                else:
                    log.exception("Download failed: %s", task.title)
                message = f"HTTP {e.response.status_code} from the Plex server" if isinstance(e, httpx.HTTPStatusError) else str(e)
                with self.lock:
                    if task.status == DOWNLOADING:
                        task.status, task.error, task.speed = FAILED, message[:200], 0

    def _check_state(self, task):
        if task.status == CANCELLED or task.id not in self.tasks:
            raise _Cancelled()
        if self.paused:
            raise _Paused()

    def _process(self, task):
        info = plex.get_file_info(task.token, task.server_id, task.media_key)
        task.title, task.thumb, task.server_name = info["title"], info["thumb"], info["server_name"]
        task.total = info["size"] or 0

        final_path = os.path.join(config.DOWNLOAD_DIR, info["rel_path"])
        part_path = final_path + ".part"
        task.path = info["rel_path"]
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        if os.path.exists(final_path) and (not task.total or os.path.getsize(final_path) == task.total):
            task.downloaded = task.total or os.path.getsize(final_path)
            self._finish(task, COMPLETED)
            return

        attempt = 0
        while True:
            self._check_state(task)
            offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if task.total and offset >= task.total:
                break
            try:
                self._stream(task, info, part_path, offset)
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                    raise
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise
                log.warning("Network error on %s (%s), retrying %d/%d", task.title, e, attempt, MAX_ATTEMPTS)
                task.speed = 0
                time.sleep(min(30, 2 ** attempt))

        os.replace(part_path, final_path)
        task.downloaded = os.path.getsize(final_path)
        self._finish(task, COMPLETED)
        log.info("Downloaded %s -> %s", task.title, final_path)

    def _stream(self, task, info, part_path, offset):
        headers = {"X-Plex-Token": info["server_token"]}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        with self.client.stream("GET", info["url"], headers=headers) as r:
            r.raise_for_status()
            if offset and r.status_code != 206:
                offset = 0  # server ignored the Range header, start over
            if not task.total:
                task.total = offset + int(r.headers.get("content-length", 0))
            task.downloaded = offset
            window_start, window_bytes = time.monotonic(), 0
            with open(part_path, "ab" if offset else "wb") as f:
                for chunk in r.iter_bytes(CHUNK_SIZE):
                    self._check_state(task)
                    f.write(chunk)
                    task.downloaded += len(chunk)
                    window_bytes += len(chunk)
                    elapsed = time.monotonic() - window_start
                    if elapsed >= 1:
                        task.speed = window_bytes / elapsed
                        window_start, window_bytes = time.monotonic(), 0

    def _finish(self, task, status):
        with self.lock:
            if task.status == DOWNLOADING:
                task.status, task.speed, task.finished = status, 0, time.time()


manager = DownloadManager(config.MAX_CONCURRENT_DOWNLOADS)
