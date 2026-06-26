"""Base Schwab client: one place that owns auth headers, token refresh on 401,
and transient-error retry. Subclasses just call `self.get/post/delete`."""
import time

import requests

from .tokens import get_token_manager


class SchwabAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Schwab API {status}: {message}")


class BaseClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self._tokens = get_token_manager()
        self._session = requests.Session()

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._tokens.get_access_token()}",
             "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def request(self, method: str, path: str, *, params=None, json=None,
                headers=None, timeout: int = 15, retries: int = 3):
        """Issue a request. Refreshes the token once on 401, retries transient
        network/5xx/429 errors with backoff, and raises SchwabAPIError on a
        non-recoverable 4xx so callers never silently get bad data."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_exc = None
        for attempt in range(retries):
            try:
                resp = self._session.request(
                    method, url, params=params, json=json,
                    headers=self._headers(headers), timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 401 and attempt == 0:
                continue  # token refreshed inside _headers next loop
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                last_exc = SchwabAPIError(resp.status_code, resp.text[:200])
                continue
            if resp.status_code == 404:
                return None
            if not resp.ok:
                raise SchwabAPIError(resp.status_code, resp.text[:300])
            if not resp.text:
                return {}
            return resp.json()

        if isinstance(last_exc, SchwabAPIError):
            raise last_exc
        raise SchwabAPIError(0, f"request failed after {retries} attempts: {last_exc}")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)
