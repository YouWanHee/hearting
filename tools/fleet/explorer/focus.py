"""Single selection-scoped asynchronous collection boundary."""
from dataclasses import replace
import threading
import time
from .types import FocusResult, Health
from ..refresh import RefreshPump


class SelectionReader:
    def __init__(self, providers):
        self.providers = providers

    def read(self, request):
        result = self.providers(request)
        if isinstance(result, FocusResult):
            return result
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           tabs=tuple(result.get("tabs", ())), ledger=tuple(result.get("ledger", ())),
                           output=tuple(result.get("output", ())), overview=tuple(result.get("overview", ())),
                           health=Health("success", time.time()))


class SelectionPump:
    """RefreshPump-backed single flight; publish only the current focus identity."""
    def __init__(self, reader, interval=1.0, clock=time.monotonic):
        self.reader = reader if isinstance(reader, SelectionReader) else SelectionReader(reader)
        self._lock = threading.Lock()
        self._request = None
        self._epoch = 0
        self._generation = 0
        self._latest = None
        self._pump = RefreshPump(self._produce, interval, clock=clock, name="fleet-selection")

    def select(self, key, snapshot_generation=0, expanded_sessions=()):
        from .types import FocusRequest
        with self._lock:
            self._epoch += 1
            self._request = FocusRequest(key, self._epoch, snapshot_generation, tuple(expanded_sessions))
            return self._request

    def _produce(self):
        with self._lock:
            request = self._request
        if request is None:
            return None
        return self.reader.read(request)

    def request(self, force=True):
        return self._pump.request(force=force)

    def request_due(self, now=None):
        return self._pump.request_due(now)

    def poll(self):
        result = self._pump.poll(self._generation)
        if result is None:
            return self._latest
        self._generation, value = result
        with self._lock:
            current = self._request
        if value is not None and current and (value.key, value.epoch, value.snapshot_generation) == (current.key, current.epoch, current.snapshot_generation):
            self._latest = value
        if current is None or self._latest is None:
            return self._latest
        if (self._latest.key, self._latest.epoch, self._latest.snapshot_generation) != (current.key, current.epoch, current.snapshot_generation):
            return None
        return self._latest

    def health(self, now=None):
        status = self._pump.health(now)
        return Health(status["state"], status.get("last_success_at"), status.get("error"))

    def stop(self):
        self._pump.stop()
