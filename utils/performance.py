"""Opt-in, low-overhead timing events for production workflow measurements."""

from __future__ import annotations

from functools import wraps
import os
import time


PERFORMANCE_METRICS_ENV_VAR = "PERFORMANCE_METRICS_ENABLED"


def metrics_enabled() -> bool:
    return str(os.getenv(PERFORMANCE_METRICS_ENV_VAR, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def record_timing(operation: str, started_at: float, **fields) -> None:
    if not metrics_enabled():
        return
    record_event(
        operation,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        **fields,
    )


def record_event(operation: str, **fields) -> None:
    if not metrics_enabled():
        return
    details = " ".join(
        f"{key}={_safe_metric_value(value)}"
        for key, value in fields.items()
        if value is not None
    )
    suffix = f" {details}" if details else ""
    print(f"[perf] operation={operation}{suffix}")


def timed(operation: str):
    """Measure a synchronous function without changing its result or errors."""

    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            started_at = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                record_timing(operation, started_at)

        return wrapper

    return decorator


def _safe_metric_value(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace(" ", "_")[:100]
