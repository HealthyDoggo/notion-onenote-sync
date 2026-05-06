"""Merge fresh Notion HTML with teacher feedback preserved from OneNote.

Strategy:
    1. Parse the current OneNote page HTML.
    2. Classify each element as "synced" (has our fingerprint style) or
       "teacher" (no fingerprint, or contains red text).
    3. Record teacher elements and their positions relative to synced neighbours
       (by ordinal index, not text content).
    4. Parse the fresh Notion HTML (all fingerprinted).
    5. Re-insert teacher elements at their relative positions.
    6. Return the merged HTML.
"""

import colorsys
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString, Tag

from config import SYNC_FINGERPRINT_STYLE

logger = logging.getLogger(__name__)

_FP_KEY = SYNC_FINGERPRINT_STYLE.split(":")[0].strip()
_FP_VAL = SYNC_FINGERPRINT_STYLE.split(":")[1].strip()


def _has_fingerprint(el: Tag) -> bool:
    """Check if an element has our sync fingerprint in its style attribute."""
    style = el.get("style", "")
    return _FP_KEY in style and _FP_VAL in style


_CSS_NAMED_REDS = {
    "red", "darkred", "firebrick", "crimson", "indianred",
    "orangered", "tomato", "maroon",
}

_RE_HEX6 = re.compile(r"#([0-9a-f]{6})\b")
_RE_HEX3 = re.compile(r"#([0-9a-f]{3})\b")
_RE_RGB = re.compile(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
_RE_COLOR_PROP = re.compile(r"(?:^|;)\s*color\s*:\s*([^;]+)", re.IGNORECASE)


def _is_red_rgb(r: int, g: int, b: int) -> bool:
    """Return True if the RGB colour is a shade of red."""
    if r < 150 or r <= g or r <= b:
        return False
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    # Red hue wraps around 0/1: accept hue < 30° or > 330°, with minimum saturation
    return (h <= 0.083 or h >= 0.917) and s >= 0.3


def _style_has_red(style: str) -> bool:
    """Check whether a CSS style string sets the text colour to a shade of red."""
    m = _RE_COLOR_PROP.search(style)
    if not m:
        return False
    value = m.group(1).strip().lower()

    if value in _CSS_NAMED_REDS:
        return True

    m_rgb = _RE_RGB.search(value)
    if m_rgb:
        return _is_red_rgb(int(m_rgb.group(1)), int(m_rgb.group(2)), int(m_rgb.group(3)))

    m_hex6 = _RE_HEX6.search(value)
    if m_hex6:
        h = m_hex6.group(1)
        return _is_red_rgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    m_hex3 = _RE_HEX3.search(value)
    if m_hex3:
        h = m_hex3.group(1)
        return _is_red_rgb(int(h[0]*2, 16), int(h[1]*2, 16), int(h[2]*2, 16))

    return False


def _has_red_text(el: Tag) -> bool:
    """Check if an element or any of its descendants contain red-coloured text."""
    if _style_has_red(el.get("style", "")):
        return True
    for child in el.find_all(style=True):
        if _style_has_red(child.get("style", "")):
            return True
    return False


def _extract_red_spans(el: Tag) -> list:
    """Extract red-coloured spans/elements from inside a fingerprinted element."""
    red_parts = []
    for child in el.find_all(True):
        if _style_has_red(child.get("style", "")):
            red_parts.append(child)
    return red_parts


def _get_body_children(html: str) -> list:
    """Parse HTML and return direct children of the body (or top-level div)."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body")
    if not body:
        return list(soup.children)

    div = body.find("div", recursive=False)
    container = div if div else body

    return [c for c in container.children if isinstance(c, Tag)]


def classify_elements(onenote_html: str) -> list[dict]:
    """Classify elements in the OneNote page as synced or teacher content.

    Returns a list of dicts:
    {
        "element": Tag,
        "type": "synced" | "teacher" | "mixed",
        "red_spans": list[Tag]  (only for "mixed")
    }
    """
    elements = _get_body_children(onenote_html)
    classified = []

    for el in elements:
        if not isinstance(el, Tag):
            continue

        fp = _has_fingerprint(el)
        red = _has_red_text(el)

        if fp and red:
            classified.append({
                "element": el,
                "type": "mixed",
                "red_spans": _extract_red_spans(el),
            })
        elif fp:
            classified.append({
                "element": el,
                "type": "synced",
            })
        else:
            classified.append({
                "element": el,
                "type": "teacher",
            })

    return classified


def _build_insertion_map(classified: list[dict]) -> dict:
    """Build a map of where teacher content should be inserted.

    Returns a dict mapping synced-element ordinal index to a list of
    teacher elements that follow it. Key -1 means "before the first synced element".

    Example: if the OneNote page has [synced0, teacher_a, synced1, teacher_b],
    the map is {0: [teacher_a], 1: [teacher_b]}.
    """
    insertion_map = {}
    synced_count = -1
    pending_teacher = []

    for item in classified:
        if item["type"] == "synced":
            if pending_teacher:
                insertion_map[synced_count] = pending_teacher
                pending_teacher = []
            synced_count += 1
        else:
            pending_teacher.append(item)

    if pending_teacher:
        insertion_map[synced_count] = pending_teacher

    return insertion_map


def _teacher_item_to_html(item: dict) -> str:
    """Convert a teacher classified item to HTML string."""
    if item["type"] == "mixed":
        parts = []
        for red_span in item.get("red_spans", []):
            parts.append(f"<p>{red_span}</p>")
        return "".join(parts)
    return str(item["element"])


def merge_html(notion_html: str, onenote_html: str) -> str:
    """Merge fresh Notion HTML with teacher feedback from the current OneNote page.

    Args:
        notion_html: Fresh HTML generated from current Notion content (all fingerprinted).
        onenote_html: Current OneNote page HTML (mix of synced + teacher content).

    Returns:
        Merged HTML with Notion content updated and teacher feedback preserved.
    """
    if not onenote_html or not onenote_html.strip():
        return notion_html

    classified = classify_elements(onenote_html)

    teacher_items = [c for c in classified if c["type"] in ("teacher", "mixed")]
    if not teacher_items:
        logger.debug("No teacher content found, using fresh Notion HTML")
        return notion_html

    insertion_map = _build_insertion_map(classified)
    if not insertion_map:
        return notion_html

    notion_elements = _get_body_children(notion_html)

    merged_parts = []

    if -1 in insertion_map:
        for item in insertion_map[-1]:
            merged_parts.append(_teacher_item_to_html(item))

    for i, el in enumerate(notion_elements):
        merged_parts.append(str(el))

        if i in insertion_map:
            for item in insertion_map[i]:
                merged_parts.append(_teacher_item_to_html(item))

    max_notion_idx = len(notion_elements) - 1
    for idx, items in insertion_map.items():
        if idx > max_notion_idx:
            for item in items:
                merged_parts.append(_teacher_item_to_html(item))

    notion_soup = BeautifulSoup(notion_html, "lxml")
    title_el = notion_soup.find("title")
    title = title_el.string if title_el else ""

    body_content = "\n".join(merged_parts)
    return (
        '<!DOCTYPE html>\n'
        '<html>\n'
        '<head>\n'
        f'  <title>{title}</title>\n'
        '</head>\n'
        '<body>\n'
        f'{body_content}\n'
        '</body>\n'
        '</html>'
    )
