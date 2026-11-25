"""Bounded workers that coalesce cache misses into model inference batches."""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Condition, Thread
from time import monotonic


class PredictionWorkers:
    """One shared scheduler and bounded pools, independent of the model protocol."""

    def __init__(self):
        self.rows = ThreadPoolExecutor(
            max_workers=64, thread_name_prefix="prediction-cache"
        )
        self.models = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="prediction-model"
        )
        self.condition = Condition()
        self.groups = {}
        self.closing = False
        self.scheduler = Thread(
            target=self._dispatch, name="prediction-batcher", daemon=True
        )
        self.scheduler.start()

    def infer(self, key, features, run_batch, maxsize: int, wait_seconds: float):
        future = Future()
        with self.condition:
            if self.closing:
                raise RuntimeError("Prediction workers are shutting down")
            group_key = (key, maxsize, wait_seconds)
            group = self.groups.setdefault(
                group_key,
                {
                    "rows": deque(),
                    "run": run_batch,
                    "maxsize": maxsize,
                    "wait": wait_seconds,
                },
            )
            group["rows"].append((monotonic(), features, future))
            self.condition.notify()
        return future.result()

    def _dispatch(self):
        while True:
            with self.condition:
                while True:
                    if self.closing and not self.groups:
                        return
                    now = monotonic()
                    ready = None
                    delay = None
                    for key, group in self.groups.items():
                        remaining = group["rows"][0][0] + group["wait"] - now
                        if (
                            self.closing
                            or len(group["rows"]) >= group["maxsize"]
                            or remaining <= 0
                        ):
                            ready = key
                            break
                        delay = remaining if delay is None else min(delay, remaining)
                    if ready is not None:
                        group = self.groups[ready]
                        batch = [
                            group["rows"].popleft()
                            for _ in range(min(len(group["rows"]), group["maxsize"]))
                        ]
                        if not group["rows"]:
                            del self.groups[ready]
                        break
                    self.condition.wait(delay)
            self.models.submit(self._complete, group["run"], batch)

    @staticmethod
    def _complete(run_batch, batch):
        try:
            # run_batch must validate the entire response before returning.
            results = run_batch([features for _, features, _ in batch])
            if len(results) != len(batch):
                raise RuntimeError("Model batch returned an incorrect result count")
        except BaseException as error:  # noqa: BLE001 - Propagate worker failures to every waiting future.
            for _, _, future in batch:
                future.set_exception(error)
        else:
            for (_, _, future), result in zip(batch, results):
                future.set_result(result)

    def shutdown(self):
        # Row workers can wait on the scheduler, so drain them before stopping it.
        self.rows.shutdown(wait=True)
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.scheduler.join()
        self.models.shutdown(wait=True)
