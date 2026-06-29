"""FastAPI middleware for metrics collection.

Automatically:
- Counts all HTTP requests (method + endpoint + status)
- Measures request duration
- Tracks in-flight requests
"""

from __future__ import annotations

import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.utils.metrics import request_duration, requests_in_flight, requests_total


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware that collects HTTP request metrics."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        method = request.method
        # Use the route path template, not the actual URL path
        # This prevents unbounded label cardinality from dynamic paths
        endpoint = request.scope.get("route")
        if endpoint:
            endpoint_str = endpoint.path
        else:
            endpoint_str = request.url.path

        # Track in-flight
        inflight_label = requests_in_flight.labels(method=method, endpoint=endpoint_str)
        inflight_label.inc()

        start = time.monotonic()
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.monotonic() - start
            status = getattr(response, "status_code", 500) if "response" in dir() else 500

            request_duration.labels(method=method, endpoint=endpoint_str).observe(duration)
            requests_total.labels(
                method=method, endpoint=endpoint_str, status=str(status)
            ).inc()
            inflight_label.dec()
