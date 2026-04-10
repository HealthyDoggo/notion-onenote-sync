"""Power Automate bridge — one-way forward sync (Notion -> OneNote).

POSTs page data to the PA webhook, which handles section creation
(via the OneDrive/.one copy workaround) and page creation/updates.
"""

import logging
import time
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


class PAForwardClient:
    """Sends page data to Power Automate's Notion->OneNote webhook."""

    def __init__(self, webhook_url: Optional[str] = None, timeout: int = 120):
        self._url = webhook_url or config.PA_FORWARD_WEBHOOK_URL
        self._timeout = timeout

    def send_page(
        self,
        section_name: str,
        title: str,
        html_body: str,
        notion_page_id: str,
        action: str = "create",
        onenote_page_id: Optional[str] = None,
        onenote_section_id: Optional[str] = None,
        page_level: int = 0,
    ) -> dict:
        """POST page to Power Automate with retry.

        Args:
            section_name: The topic/section name in OneNote.
            title: Page title.
            html_body: Full HTML body for the page.
            notion_page_id: Notion page UUID (for tracking).
            action: "create" or "update".
            onenote_page_id: Existing OneNote page ID (for updates).
            onenote_section_id: Existing section ID (skip section lookup).
            page_level: OneNote page indent level (0, 1, 2).

        Returns:
            Response dict with onenote_page_id and onenote_section_id.
        """
        payload = {
            "section_name": section_name,
            "page_title": title,
            "html_body": html_body,
            "notion_page_id": notion_page_id,
            "action": action,
            "onenote_page_id": onenote_page_id,
            "onenote_section_id": onenote_section_id,
            "page_level": page_level,
        }

        logger.info(
            "Sending %s '%s' to PA (section=%s, level=%d)",
            action, title, section_name, page_level,
        )

        last_err = None
        for attempt in range(config.PA_RETRY_ATTEMPTS):
            try:
                resp = requests.post(self._url, json=payload, timeout=self._timeout)
                resp.raise_for_status()

                try:
                    data = resp.json()
                except requests.JSONDecodeError:
                    data = {"raw_response": resp.text}

                logger.info("PA responded: %s", data)
                return data

            except requests.RequestException as e:
                last_err = e
                wait = config.PA_RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "PA request failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1, config.PA_RETRY_ATTEMPTS, e, wait,
                )
                time.sleep(wait)

        raise RuntimeError(f"PA webhook failed after {config.PA_RETRY_ATTEMPTS} attempts: {last_err}")
