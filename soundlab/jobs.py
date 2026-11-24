"""Bounded background work for the local studio."""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock
from typing import Callable

from .cancellation import check, current
from .store import uid


@dataclass(frozen=True)
class Job:
    id: str
    label: str
    future: Future[str]
    key: str | None
    stop: Event


def job_route(job):
    """Return the operation, generation round and owning room."""
    kind, _, rest = (job.key or '').partition(':')
    target, _, owner = rest.partition(':')
    return kind, target, owner or target


class Jobs:
    def __init__(self, workers=2, capacity=12):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='motionSound')
        self.capacity = capacity
        self._jobs: list[Job] = []
        self._lock = Lock()

    def submit(self, label: str, work: Callable[[], str], *, key: str | None = None,
               after: Future | None = None, reserve: Callable[[], None] | None = None):
        with self._lock:
            active = [job for job in self._jobs if not job.future.done()]
            if key is not None and any(job.key == key for job in active):
                raise ValueError('この音の再実行はすでに登録されています')
            if len(active) >= self.capacity:
                raise ValueError('生成の待ち行列がいっぱいです。完了までお待ちください')
            finished = [job for job in self._jobs if job not in active][:11]
            if reserve is not None:
                reserve()
            stop = Event()

            def execute():
                token = current.set(stop)
                try:
                    check()
                    return work()
                finally:
                    current.reset(token)

            future = Future() if after is not None else self.executor.submit(execute)
            job = Job(uid(), label, future, key, stop)
            self._jobs = [job, *sorted([*active, *finished], key=self._jobs.index)]

        if after is not None:
            def run_after():
                if future.set_running_or_notify_cancel():
                    try:
                        result = execute()
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)

            def schedule(_):
                if not future.cancelled():
                    self.executor.submit(run_after)

            after.add_done_callback(schedule)
        return job.id

    def cancel(self, job):
        if job.future.done():
            return
        job.stop.set()
        job.future.cancel()

    def snapshot(self):
        with self._lock:
            return tuple(self._jobs)
