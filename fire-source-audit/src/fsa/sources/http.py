"""Polite HTTP with retries and a fixed pause between requests."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class Http:
    def __init__(self, timeout_s: float = 60, retries: int = 4, backoff_s: float = 2.0,
                 pause_s: float = 0.3, user_agent: str = "fire-source-audit"):
        self.timeout = timeout_s
        self.retries = retries
        self.backoff = backoff_s
        self.pause = pause_s
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._last = 0.0

    @classmethod
    def from_settings(cls, settings) -> "Http":
        h = settings.raw.get("http", {})
        return cls(h.get("timeout_s", 60), h.get("retries", 4), h.get("backoff_s", 2.0),
                   h.get("pause_between_requests_s", 0.3), h.get("user_agent", "fire-source-audit"))

    def _wait(self) -> None:
        dt = time.monotonic() - self._last
        if dt < self.pause:
            time.sleep(self.pause - dt)
        self._last = time.monotonic()

    def get(self, url: str, params: dict[str, Any] | None = None, *, method: str = "GET") -> requests.Response:
        err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                if method == "POST":
                    r = self.session.post(url, data=params, timeout=self.timeout)
                else:
                    r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    break
                wait = self.backoff * (2 ** attempt)
                log.warning("request failed (%s), retry %d in %.1fs: %s", e, attempt + 1, wait, url)
                time.sleep(wait)
        assert err is not None
        raise err

    def get_json(self, url: str, params: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
        return self.get(url, params, method=method).json()
