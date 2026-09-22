"""Shared background-job machinery for the manual-refresh panes.

Breadth (Massive EOD) and Report (UW digest + LLM) both run a single
background job at a time, tracked by an in-process status dict polled over
HTTP. This module holds the lock/status/thread plumbing so each service only
supplies the actual work and its own secret-redaction function.

There is no scheduler (see AGENTS.md) — a job starts only from a manual
Refresh/Generate click via ``JobRunner.start``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any


class JobRunner:
    """One-at-a-time background job with an in-process, pollable status dict.

    ``base_status`` seeds pane-specific fields (e.g. breadth's progress
    counters). ``redact`` scrubs secrets from any exception before it reaches
    the status ``detail`` or the log.
    """

    def __init__(
        self,
        name: str,
        *,
        redact: Callable[[str], str],
        logger: logging.Logger,
        base_status: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._redact = redact
        self._logger = logger
        self._lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._running = False
        self._base: dict[str, Any] = {"state": "idle", "detail": None, **(base_status or {})}
        self._status: dict[str, Any] = dict(self._base)

    def set_status(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._status.update(kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(
        self,
        work: Callable[[], None],
        *,
        starting: dict[str, Any] | None = None,
    ) -> bool:
        """Run ``work`` on a daemon thread. Returns False if a job is running."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        self.set_status(state="running", **(starting or {}))

        def _target() -> None:
            try:
                work()
                self.set_status(state="idle", detail="Done")
            except Exception as exc:  # noqa: BLE001 - surface any failure as status
                detail = self._redact(str(exc))
                self._logger.exception("%s failed: %s", self._name, detail)
                self.set_status(state="error", detail=detail)
            finally:
                with self._lock:
                    self._running = False

        threading.Thread(target=_target, daemon=True).start()
        return True

    def reset_for_tests(self) -> None:
        """Test helper: clear the running flag and restore the base status."""
        with self._lock:
            self._running = False
        with self._status_lock:
            self._status = dict(self._base)
