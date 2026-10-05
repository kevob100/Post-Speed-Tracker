"""X API v2 client: handle resolution, timeline pulls, public metrics.

App-only (Bearer Token) auth. Read-only. Handles 429 rate limits with
backoff that respects the x-rate-limit-reset header.
"""
from __future__ import annotations

import time
from typing import Iterator

import requests

from .config import env

BASE = "https://api.twitter.com/2"

TWEET_FIELDS = "created_at,public_metrics,referenced_tweets,entities,text,lang,author_id"


class XApiError(RuntimeError):
    pass


class XClient:
    def __init__(self, bearer_token: str | None = None, session: requests.Session | None = None):
        self.bearer = bearer_token or env("X_BEARER_TOKEN")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.bearer}"})

    def _get(self, path: str, params: dict | None = None, max_retries: int = 5) -> dict:
        url = f"{BASE}{path}"
        # Full-archive search pages for big accounts can take well over 30s to answer.
        timeout = 90 if path.startswith("/tweets/search") else 30
        for attempt in range(max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == max_retries - 1:
                    raise
                time.sleep(min(2 ** attempt * 5, 60))
                continue
            if resp.status_code == 429:
                reset = resp.headers.get("x-rate-limit-reset")
                wait = self._backoff_seconds(reset, attempt)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code >= 400:
                raise XApiError(f"{resp.status_code} {path}: {resp.text}")
            return resp.json()
        raise XApiError(f"Exhausted retries for {path}")

    @staticmethod
    def _backoff_seconds(reset_header: str | None, attempt: int) -> float:
        if reset_header:
            try:
                wait = float(reset_header) - time.time()
                if wait > 0:
                    return min(wait + 1, 900)
            except ValueError:
                pass
        return min(2 ** attempt, 60)

    def resolve_username(self, username: str) -> str:
        data = self._get(f"/users/by/username/{username}")
        if "data" not in data:
            raise XApiError(f"Could not resolve @{username}: {data}")
        return data["data"]["id"]

    def user_tweets(
        self,
        user_id: str,
        since_id: str | None = None,
        start_time: str | None = None,
        exclude: list[str] | None = None,
        max_pages: int | None = None,
        page_size: int = 100,
    ) -> Iterator[dict]:
        """Yield tweet objects newest-first, paginating until exhausted/caught up.

        page_size maps to the API's max_results (5-100); lower it to cap reads.
        """
        params: dict = {
            "max_results": max(5, min(page_size, 100)),
            "tweet.fields": TWEET_FIELDS,
        }
        if exclude:
            params["exclude"] = ",".join(exclude)
        if since_id:
            params["since_id"] = since_id
        if start_time:
            params["start_time"] = start_time

        pages = 0
        token: str | None = None
        while True:
            if token:
                params["pagination_token"] = token
            else:
                params.pop("pagination_token", None)
            payload = self._get(f"/users/{user_id}/tweets", params=params)
            for tweet in payload.get("data", []):
                yield tweet
            meta = payload.get("meta", {})
            token = meta.get("next_token")
            pages += 1
            if not token or (max_pages and pages >= max_pages):
                break

    def search_all(self, query: str, start_time: str, end_time: str, page_size: int = 500) -> Iterator[dict]:
        """Full-archive search (needs an X API plan with archive access). Yields tweets
        newest-first across all pages. The endpoint allows 1 request/second, so pages are
        spaced out; 429s are retried by _get."""
        params: dict = {
            "query": query, "start_time": start_time, "end_time": end_time,
            "max_results": max(10, min(page_size, 500)), "tweet.fields": TWEET_FIELDS,
        }
        token: str | None = None
        while True:
            if token:
                params["next_token"] = token
            payload = self._get("/tweets/search/all", params=params)
            for tweet in payload.get("data", []):
                yield tweet
            token = payload.get("meta", {}).get("next_token")
            if not token:
                break
            time.sleep(1.1)

    def users_metrics(self, ids: list[str]) -> dict[str, dict]:
        """Current public_metrics (followers_count, following_count, tweet_count, listed_count)
        for up to 100 user IDs. Returns {user_id: public_metrics}."""
        payload = self._get("/users", params={"ids": ",".join(ids), "user.fields": "public_metrics"})
        return {u["id"]: u.get("public_metrics", {}) for u in payload.get("data", [])}

    def users_by_ids(self, ids: list[str]) -> dict[str, str]:
        """Usernames for any number of user IDs, 100 per request. Returns {user_id: username}."""
        out: dict[str, str] = {}
        for i in range(0, len(ids), 100):
            payload = self._get("/users", params={"ids": ",".join(ids[i : i + 100])})
            for u in payload.get("data", []):
                out[u["id"]] = u["username"]
        return out

    def tweets_lookup(self, ids: list[str], fields: str = "public_metrics,referenced_tweets") -> dict[str, dict]:
        """Fetch tweet objects (with `fields`) for any number of IDs, 100 per request."""
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            payload = self._get("/tweets", params={"ids": ",".join(chunk), "tweet.fields": fields})
            for tweet in payload.get("data", []):
                out[tweet["id"]] = tweet
        return out

    def tweets_metrics(self, ids: list[str]) -> dict[str, dict]:
        """Fetch current public_metrics for up to 100 tweet IDs. Returns {id: public_metrics}."""
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            payload = self._get(
                "/tweets",
                params={"ids": ",".join(chunk), "tweet.fields": "public_metrics"},
            )
            for tweet in payload.get("data", []):
                out[tweet["id"]] = tweet.get("public_metrics", {})
        return out
