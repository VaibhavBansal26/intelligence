"""Prometheus metrics bound to a per-app registry (no process-global state)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "intelligence_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=self.registry
        )
        self.latency = Histogram("intelligence_http_request_seconds", "HTTP latency", ["route"], registry=self.registry)
        self.jobs = Counter("intelligence_jobs_total", "Background jobs", ["type", "status"], registry=self.registry)
        self.sync_rows = Counter("intelligence_sync_rows_total", "Rows landed by sync", ["resource"], registry=self.registry)
        self.llm = Counter("intelligence_llm_narrations_total", "Narrations by source", ["source"], registry=self.registry)

    def render(self) -> bytes:
        return generate_latest(self.registry)
