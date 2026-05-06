"""Power Automate bridge — one-way forward sync (Notion -> OneNote).

POSTs page data to the PA webhook. Supports three actions:
  - create:  Create a new page in a section
  - read:    Read current page HTML (for merge before update)
  - replace: Delete old page + create new page with merged content
"""

import logging
import threading
import time
from typing import Optional, Sequence

import requests

import config

logger = logging.getLogger(__name__)


class RateLimitedError(Exception):
    """Raised when PA reports a OneNote 429."""

    def __init__(self, retry_after: float, failed_action: str = ""):
        self.retry_after = retry_after
        self.failed_action = failed_action
        super().__init__(
            f"OneNote rate-limited (retry after {retry_after}s, action={failed_action})"
        )


class RateLimitGate:
    """Shared gate that makes all threads pause when any thread hits a 429."""

    def __init__(self):
        self._lock = threading.Lock()
        self._blocked_until = 0.0

    def wait(self):
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._blocked_until:
                    return
                wait_time = self._blocked_until - now
            logger.info("Rate-limit gate: sleeping %.1fs", wait_time)
            time.sleep(wait_time)

    def set_backoff(self, seconds: float):
        with self._lock:
            new_deadline = time.monotonic() + seconds
            self._blocked_until = max(self._blocked_until, new_deadline)


class PAForwardClient:
    """Sends page data to Power Automate's Notion->OneNote webhook."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        timeout: int = 120,
        gate: Optional[RateLimitGate] = None,
    ):
        self._url = webhook_url or config.PA_FORWARD_WEBHOOK_URL
        self._timeout = timeout
        self.gate = gate or RateLimitGate()

    def _post(self, payload: dict) -> dict:
        """POST to PA with retry and exponential backoff.

        Raises RateLimitedError on 429 so the caller can back off and
        retry the full operation (not just the HTTP call).
        """
        self.gate.wait()
        time.sleep(config.PA_CALL_DELAY)

        last_err = None
        for attempt in range(config.PA_RETRY_ATTEMPTS):
            try:
                resp = requests.post(self._url, json=payload, timeout=self._timeout)

                if resp.status_code == 429:
                    try:
                        body = resp.json()
                    except requests.JSONDecodeError:
                        body = {}
                    retry_after = float(
                        body.get("retry_after", config.PA_RATE_LIMIT_DEFAULT_BACKOFF)
                    )
                    failed_action = body.get("failed_action", "")
                    self.gate.set_backoff(retry_after)
                    raise RateLimitedError(retry_after, failed_action)

                if resp.status_code == 409:
                    try:
                        data = resp.json()
                    except requests.JSONDecodeError:
                        data = {}
                    logger.info(
                        "Duplicate page detected by PA (notion_page_id=%s): %s",
                        payload.get("notion_page_id"), data,
                    )
                    return data

                resp.raise_for_status()
                try:
                    data = resp.json()
                except requests.JSONDecodeError:
                    data = {"raw_response": resp.text}
                return data
            except RateLimitedError:
                raise
            except requests.RequestException as e:
                last_err = e
                wait = config.PA_RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "PA request failed (attempt %d/%d): %s — retrying in %ds (name=%s)",
                    attempt + 1, config.PA_RETRY_ATTEMPTS, e, wait, payload.get("page_title", "unknown")
                )
                time.sleep(wait)
        raise RuntimeError(f"PA webhook failed after {config.PA_RETRY_ATTEMPTS} attempts: {last_err}")

    def send_page(
        self,
        section_name: str,
        title: str,
        html_body: str,
        notion_page_id: str,
        action: str = "create",
        onenote_page_id: Optional[str] = None,
        onenote_section_id: Optional[str] = None,
        section_group_path: Optional[Sequence[str]] = None,
        use_flat_section: bool = False,
    ) -> dict:
        """Create or replace a page in OneNote via Power Automate.

        For action="create": creates a new page.
        For action="replace": PA deletes old page, creates new with html_body.

        section_group_path:
            Folder names from the Notion root topic outward (section groups in OneNote).
            PA walks these to build or resolve section groups.

        use_flat_section:
            When True, the target is a plain notebook section (.one file) named
            section_name inside the innermost section group.  When False, the
            target is a section group (folder) named section_name containing a
            Main section.  section_group_path is respected in both modes.
        """
        path_list = list(section_group_path) if section_group_path is not None else []
        payload = {
            "section_name": section_name,
            "page_title": title,
            "html_body": html_body,
            "notion_page_id": notion_page_id,
            "action": action,
            "onenote_page_id": onenote_page_id,
            "onenote_section_id": onenote_section_id,
            "section_group_path": path_list,
            "use_flat_section": use_flat_section,
        }
        logger.info(
            "Sending %s '%s' to PA (section=%s, path=%s)",
            action, title, section_name, path_list,
        )
        data = self._post(payload)
        logger.info("PA responded: %s", data)
        return data

    def read_page(self, onenote_page_id: str, onenote_section_id: Optional[str] = None) -> dict:
        """Read current OneNote page HTML for merge.

        Returns dict with 'current_html' and 'onenote_section_id'.
        """
        payload = {
            "action": "read",
            "onenote_page_id": onenote_page_id,
            "onenote_section_id": onenote_section_id,
        }
        logger.info("Reading OneNote page %s for merge", onenote_page_id)
        data = self._post(payload)
        logger.info("PA returned %d chars of HTML", len(data.get("current_html", "")))
        return data
