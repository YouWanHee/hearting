"""Non-blocking live refresh primitives for Fleet.

The curses thread must never execute a collector. ``RefreshPump`` owns one
daemon worker at a time, coalesces any number of refresh requests into at most
one follow-up run, and publishes only complete successful results.
"""

from dataclasses import dataclass, field
import os
import threading
import time


@dataclass
class LiveSnapshot:
    sessions: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    usage_snapshots: dict = field(default_factory=dict)
    malformed: int = 0
    memory: object = None
    governor: object = None
    hearting: dict = None


# Measured normal tick = 10-12s (attach_projections 5.64s + dispatch.collect
# 4.33s). At the default 2s interval, interval*6 == 12s would be
# indistinguishable from a normal tick, so a floor must dominate. 90s is
# ~8x the measured normal-tick ceiling: it absorbs a transient NFS stall
# while still landing well before a user perceives Fleet as stuck.
#
# The measurement above is for the snapshot pump only. The compute-host pump
# (remote GPU host queries) borrows the same 90s floor as a POLICY CHOICE, not
# a measurement -- its producer's normal duration was not profiled in this
# cycle. A false "stalled" there only costs one leaked daemon thread (capped at
# MAX_LEAKED_WORKERS) and a " · gpu stalled" header suffix; it never touches
# snapshot collection or display. Since the original defect was "never comes
# back", judging late is safer than judging early. See FLEET_REFRESH_STALL_AFTER
# to override either pump's threshold.
DEFAULT_STALL_FLOOR = 90.0
DEFAULT_STALL_MULTIPLIER = 6
MIN_STALL_AFTER = 5.0
MAX_LEAKED_WORKERS = 3


class RefreshWorkerDied(RuntimeError):
    """The worker thread exited without the pump observing completion."""


class RefreshWorkerStalled(RuntimeError):
    """The worker thread is still alive well past its expected duration."""


def _resolve_stall_after(interval):
    override = os.environ.get("FLEET_REFRESH_STALL_AFTER")
    if override:
        try:
            return max(MIN_STALL_AFTER, float(override))
        except ValueError:
            pass
    return max(DEFAULT_STALL_FLOOR, interval * DEFAULT_STALL_MULTIPLIER)


class RefreshPump:
    """Run an arbitrary producer off-thread with last-good atomic handoff."""

    def __init__(self, producer, interval, clock=time.monotonic,
                 thread_factory=threading.Thread, name="fleet-refresh",
                 stall_after=None):
        self._producer = producer
        self._interval = max(0.1, float(interval))
        self._clock = clock
        self._thread_factory = thread_factory
        self._name = str(name)
        self._lock = threading.RLock()
        self._thread = None
        self._running = False
        self._pending = False
        self._stopped = False
        self._next_due = self._clock()
        self._generation = 0
        self._latest = None
        self._last_error = None
        self._started_at = None
        self._last_success_at = None
        self._leaked_workers = 0
        self._stall_after = (
            float(stall_after) if stall_after is not None
            else _resolve_stall_after(self._interval)
        )
        self._thread_token = None

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def running(self):
        with self._lock:
            return self._running

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    def start(self):
        return self.request(force=True)

    def request_due(self, now=None):
        return self.request(force=False, now=now)

    def request(self, force=False, now=None):
        """Schedule a run without waiting for the producer.

        Returns True only when this call starts a new worker. Requests received
        while a worker is active collapse into one pending follow-up.
        """
        current = self._clock() if now is None else float(now)
        with self._lock:
            if self._stopped:
                return False
            self._reap_locked(current)
            if not force and current < self._next_due:
                return False
            if self._running:
                # A periodic deadline that expires during a slow collection is
                # already represented by that in-flight collection. Queuing it
                # would make a producer slower than ``interval`` run forever
                # with no idle gap. Only an explicit user refresh earns one
                # coalesced follow-up.
                if force:
                    self._pending = True
                return False
            self._running = True
            self._next_due = current + self._interval
            self._start_locked()
            return True

    def _reap_locked(self, now):
        """Recognize a worker that died or hung without the pump noticing.

        Must be called with ``self._lock`` held.
        """
        if not self._running:
            return
        thread = self._thread
        if thread is None or not thread.is_alive():
            if self._last_error is None:
                self._last_error = RefreshWorkerDied(
                    "worker thread exited without clearing state")
            self._running = False
            return
        if self._started_at is not None and now - self._started_at > self._stall_after:
            self._leaked_workers += 1
            self._last_error = RefreshWorkerStalled(
                "worker has run for %.1fs, exceeding stall_after=%.1fs"
                % (now - self._started_at, self._stall_after))
            if self._leaked_workers < MAX_LEAKED_WORKERS:
                # The old worker is abandoned as a daemon thread; a fresh
                # worker takes over the schedule. If the old one eventually
                # returns, the token check in _run discards its result.
                self._running = False
            # else: at the cap, stop spawning more daemon threads -- the
            # blocking cause has not gone away, so piling on threads is not
            # honest recovery. Leave `_running` True: request() will keep
            # refusing until the operator notices via health().

    def poll(self, after_generation=0):
        """Return ``(generation, value)`` only when a newer success exists."""
        with self._lock:
            if self._generation <= after_generation:
                return None
            return self._generation, self._latest

    def health(self, now=None):
        """Single read surface for render. Do not read ``running``/``last_error``
        directly outside this method for display purposes.

        This is a pure read: it never reaps or restarts a worker. It reports
        "stalled" the moment a running worker crosses ``stall_after``, even if
        nothing has called ``request()``/``request_due()`` yet to trigger
        recovery -- so a caller can observe a hang independently of when the
        next scheduled request happens to land.
        """
        current = self._clock() if now is None else float(now)
        with self._lock:
            last_success_at = self._last_success_at
            age = None if last_success_at is None else current - last_success_at
            stalled = (
                self._leaked_workers >= MAX_LEAKED_WORKERS
                or (self._running and self._started_at is not None
                    and current - self._started_at > self._stall_after)
            )
            if stalled:
                state = "stalled"
            elif self._last_error is not None:
                state = "failed"
            elif self._running:
                state = "running"
            else:
                state = "idle"
            last_error = None
            if self._last_error is not None:
                last_error = "%s: %s" % (
                    type(self._last_error).__name__, str(self._last_error)[:120])
            return {
                "state": state,
                "last_success_at": last_success_at,
                "age": age,
                "last_error": last_error,
                "leaked_workers": self._leaked_workers,
                "stall_after": self._stall_after,
            }

    def stop(self, join_timeout=1.0):
        """Prevent follow-ups and wait only a bounded time for the active worker."""
        with self._lock:
            self._stopped = True
            self._pending = False
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(join_timeout)))

    def _start_locked(self):
        token = object()
        self._thread_token = token
        self._started_at = self._clock()
        thread = self._thread_factory(
            target=self._run,
            args=(token,),
            name=self._name,
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _run(self, token):
        value = None
        error = None
        try:
            value = self._producer()
        except BaseException as exc:  # noqa: BLE001
            # Recorded, then swallowed rather than re-raised. The worker is a
            # daemon thread with no join()-ing caller on this path: a
            # re-raise would only reach threading.excepthook, which prints to
            # stderr and can corrupt the curses screen. KeyboardInterrupt and
            # SystemExit are still visible -- they land in `_last_error` and
            # surface through `health()` -- so they never disappear silently.
            error = exc

        with self._lock:
            # This check MUST run before any of _latest/_generation/_last_error
            # are published, and before _next_due/_pending are touched. A
            # worker declared stalled and superseded by a new one must not be
            # allowed to publish a result after the fact: that result is
            # necessarily staler than whatever the new worker already
            # published, so honoring it would make the display regress.
            if self._thread_token is not token:
                return
            if error is None:
                self._latest = value
                self._generation += 1
                self._last_error = None
                self._last_success_at = self._clock()
            else:
                self._last_error = error
            self._running = False
            # Schedule from completion, not start. A slow producer therefore
            # gets a real cooldown instead of immediately chasing elapsed ticks.
            self._next_due = self._clock() + self._interval
            if self._pending and not self._stopped:
                self._pending = False
                self._running = True
                self._start_locked()
