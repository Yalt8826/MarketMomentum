"""Console progress reporting for the long-running pipeline stages.

A 2500-day download or ETL runs for hours, so every stage should say what it is
working on right now, how far along it is, and how long is left.

Two shapes, depending on the call site:

* :func:`track` wraps a sequential loop::

      for date in track(dates, "Downloading bhavcopy", label=fmt_date):
          ...

* :func:`progress_stage` is a context manager for the concurrent case, where
  work does not complete in iteration order::

      with progress_stage("Downloading bhavcopy", total=len(dates)) as bar:
          for fut in as_completed(futures):
              bar.advance(label=futures[fut])

Both degrade to periodic log lines when stderr is not a TTY, so
``mktmtm etl > etl_run.log`` produces a readable log rather than megabytes of
ANSI escapes. Set ``MKTMTM_NO_PROGRESS=1`` to force that mode.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = ["progress_stage", "track", "use_rich"]


# Emit at most one log line per this many seconds in non-TTY mode.
_LOG_INTERVAL_SECONDS = 15.0


class _Bar(Protocol):
    def advance(self, n: int = 1, label: str | None = None) -> None: ...
    def set_label(self, label: str) -> None: ...


def use_rich() -> bool:
    """True when a live progress bar makes sense (interactive TTY, rich installed)."""
    if os.environ.get("MKTMTM_NO_PROGRESS"):
        return False
    if not sys.stderr.isatty():
        return False
    try:
        import rich.progress  # noqa: F401
    except ImportError:
        return False
    return True


class _LoggingBar:
    """Non-TTY fallback: periodic single-line progress at INFO level."""

    def __init__(self, description: str, total: int | None, unit: str) -> None:
        self._description = description
        self._total = total
        self._unit = unit
        self._done = 0
        self._label: str | None = None
        self._started = time.monotonic()
        self._last_log = 0.0
        self._lock = threading.Lock()
        logger.info(
            "%s: starting (%s)",
            description,
            f"{total} {unit}" if total else f"unknown number of {unit}",
        )

    def set_label(self, label: str) -> None:
        with self._lock:
            self._label = label

    def advance(self, n: int = 1, label: str | None = None) -> None:
        with self._lock:
            self._done += n
            if label is not None:
                self._label = label
            now = time.monotonic()
            if now - self._last_log < _LOG_INTERVAL_SECONDS and self._done != self._total:
                return
            self._last_log = now
            elapsed = now - self._started
            rate = self._done / elapsed if elapsed > 0 else 0.0
            parts = [f"{self._description}:"]
            if self._total:
                pct = 100.0 * self._done / self._total
                parts.append(f"{self._done}/{self._total} ({pct:.1f}%)")
                if rate > 0:
                    eta = (self._total - self._done) / rate
                    parts.append(f"eta {_fmt_duration(eta)}")
            else:
                parts.append(f"{self._done} {self._unit}")
            parts.append(f"[{_fmt_duration(elapsed)} elapsed]")
            if self._label:
                parts.append(f"— {self._label}")
            logger.info(" ".join(parts))

    def finish(self) -> None:
        elapsed = time.monotonic() - self._started
        logger.info(
            "%s: done — %d %s in %s",
            self._description, self._done, self._unit, _fmt_duration(elapsed),
        )


class _RichBar:
    """Live bar: description, bar, count, percentage, elapsed, ETA, current item."""

    def __init__(self, progress: Any, task_id: Any) -> None:
        self._progress = progress
        self._task_id = task_id

    def set_label(self, label: str) -> None:
        self._progress.update(self._task_id, current=_truncate(label))

    def advance(self, n: int = 1, label: str | None = None) -> None:
        if label is not None:
            self._progress.update(self._task_id, advance=n, current=_truncate(label))
        else:
            self._progress.update(self._task_id, advance=n)


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _truncate(label: str, width: int = 34) -> str:
    label = str(label)
    return label if len(label) <= width else label[: width - 1] + "…"


@contextmanager
def progress_stage(
    description: str,
    total: int | None = None,
    unit: str = "items",
) -> Iterator[_Bar]:
    """Progress context for concurrent or manually-advanced work.

    Yields a handle with ``advance(n=1, label=...)`` and ``set_label(...)``.
    Safe to call from multiple threads.
    """
    if not use_rich():
        bar = _LoggingBar(description, total, unit)
        try:
            yield bar
        finally:
            bar.finish()
        return

    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    columns = [
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(compact=True),
        TextColumn("[dim]{task.fields[current]}"),
    ]
    with Progress(*columns, transient=False, refresh_per_second=8) as progress:
        task_id = progress.add_task(description, total=total, current="")
        yield _RichBar(progress, task_id)


def track[T](
    iterable: Iterable[T],
    description: str,
    total: int | None = None,
    label: Callable[[T], str] | None = None,
    unit: str = "items",
) -> Iterator[T]:
    """Iterate ``iterable`` with a progress bar naming the current item.

    ``label`` maps an item to the text shown alongside the bar; it is applied
    *before* the item is yielded, so the bar names what is being worked on
    rather than what just finished.
    """
    if total is None:
        try:
            total = len(iterable)  # type: ignore[arg-type]
        except TypeError:
            total = None

    with progress_stage(description, total=total, unit=unit) as bar:
        for item in iterable:
            if label is not None:
                # a broken label must never kill the run
                with suppress(Exception):
                    bar.set_label(label(item))
            yield item
            bar.advance()
