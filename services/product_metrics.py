"""Small, fail-safe persistence layer for product and reliability metrics."""
from __future__ import annotations

import json
from typing import Any

from models import ProductMetric


def record_metric(db, event_name: str, *, user_id: str | None = None,
                  duration_ms: float | None = None, success: bool | None = None,
                  status_code: int | None = None, path: str | None = None,
                  metadata: dict[str, Any] | None = None) -> None:
    """Record telemetry without allowing metrics to break a money request."""
    try:
        db.add(ProductMetric(
            user_id=user_id,
            event_name=event_name[:80],
            duration_ms=duration_ms,
            success=success,
            status_code=status_code,
            path=path[:200] if path else None,
            metadata_json=json.dumps(metadata or {}, separators=(",", ":"))[:2000],
        ))
        db.commit()
    except Exception:
        db.rollback()
