"""Power Automate bridge.

Forward: HTTP client that POSTs page data to the PA webhook (Notion → OneNote).
Reverse: Flask app that receives page data from PA (OneNote → Notion).
"""

import hmac
import logging
from typing import Optional

import requests
from flask import Flask, Response, jsonify, request

import config

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  FORWARD: Pi → Power Automate → OneNote
# ══════════════════════════════════════════════════════════════════════════════


class PAForwardClient:
    """Sends page data to Power Automate's Notion→OneNote webhook."""

    def __init__(self, webhook_url: Optional[str] = None, timeout: int = 60):
        self._url = webhook_url or config.PA_FORWARD_WEBHOOK_URL
        self._timeout = timeout

    def send_page(
        self,
        title: str,
        html_body: str,
        onenote_page_id: Optional[str] = None,
    ) -> dict:
        """POST page to Power Automate. Returns response JSON with OneNote page ID."""
        payload = {
            "title": title,
            "html_body": html_body,
            "onenote_page_id": onenote_page_id,
        }
        logger.info("Sending page '%s' to Power Automate (update=%s)", title, bool(onenote_page_id))

        resp = requests.post(self._url, json=payload, timeout=self._timeout)
        resp.raise_for_status()

        try:
            data = resp.json()
        except requests.JSONDecodeError:
            data = {"raw_response": resp.text}

        logger.info("Power Automate responded: %s", data)
        return data


# ══════════════════════════════════════════════════════════════════════════════
#  REVERSE: Power Automate → Pi Flask webhook → Notion
# ══════════════════════════════════════════════════════════════════════════════

_reverse_callback = None


def set_reverse_callback(fn) -> None:
    """Register the function that processes incoming OneNote page data.

    The callback signature: fn(onenote_page_id: str, html_content: str) -> None
    """
    global _reverse_callback
    _reverse_callback = fn


def create_webhook_app() -> Flask:
    """Create the Flask app for the reverse sync webhook."""
    app = Flask(__name__)

    @app.route("/webhook/onenote", methods=["POST"])
    def receive_onenote_update():
        secret = request.headers.get("X-Webhook-Secret", "")
        expected = config.PA_REVERSE_WEBHOOK_SECRET
        if expected and not hmac.compare_digest(secret, expected):
            logger.warning("Rejected webhook: invalid secret")
            return Response("Unauthorized", status=401)

        data = request.get_json(silent=True)
        if not data:
            return Response("Bad Request: no JSON body", status=400)

        onenote_page_id = data.get("onenote_page_id")
        html_content = data.get("html_content", "")

        if not onenote_page_id:
            return Response("Bad Request: missing onenote_page_id", status=400)

        logger.info("Received OneNote update for page %s", onenote_page_id)

        if _reverse_callback:
            try:
                _reverse_callback(onenote_page_id, html_content)
                return jsonify({"status": "ok"})
            except Exception:
                logger.exception("Error processing reverse sync for %s", onenote_page_id)
                return Response("Internal Server Error", status=500)
        else:
            logger.warning("No reverse callback registered, discarding update")
            return jsonify({"status": "ok", "note": "no handler registered"})

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "healthy"})

    return app
