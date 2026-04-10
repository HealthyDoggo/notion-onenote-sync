import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_PARENT_PROPERTY = os.environ.get("NOTION_PARENT_PROPERTY", "Parent")
PA_FORWARD_WEBHOOK_URL = os.environ.get("PA_FORWARD_WEBHOOK_URL", "")

DB_PATH = BASE_DIR / "sync_state.db"

NOTION_API_RATE_LIMIT = 3  # requests per second
PA_RETRY_ATTEMPTS = 3
PA_RETRY_BACKOFF_BASE = 5  # seconds; retries at 5s, 10s, 20s

NOTION_COLOURS = {
    "default":           ("#F7F6F3", "#37352F"),
    "gray_background":   ("#F1F1EF", "#9B9A97"),
    "brown_background":  ("#F4EEEE", "#9F6B53"),
    "orange_background": ("#FBECDD", "#D9730D"),
    "yellow_background": ("#FBF3DB", "#DFAB01"),
    "green_background":  ("#EDF3EC", "#0F7B6C"),
    "blue_background":   ("#E7F3F8", "#0B6E99"),
    "purple_background": ("#F4F0F7", "#6940A5"),
    "pink_background":   ("#F9EEF3", "#AD1A72"),
    "red_background":    ("#FDEBEC", "#E03E3E"),
}

MAX_CALLOUT_NESTING_DEPTH = 3
