"""Merge fresh Notion HTML with teacher feedback (red text) from OneNote.

Strategy:
    1. Parse the current OneNote page HTML.
    2. Find all elements containing red-coloured text.
    3. For each red element, record the plain text of its preceding and
       following sibling elements as anchors.
    4. Parse the fresh Notion HTML.
    5. For each red element, find the best matching position in the fresh
       HTML by comparing anchor text to element text (fuzzy substring match).
    6. Insert red elements at their matched positions; append at the end
       if no match is found.
    7. Return the merged HTML.
"""

import colorsys
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

from bs4 import BeautifulSoup, NavigableString, Tag

logger = logging.getLogger(__name__)

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


def _is_entirely_red(el: Tag) -> bool:
    """Check if the element itself (not just a child) is red-styled."""
    return _style_has_red(el.get("style", ""))


def _extract_red_spans(el: Tag) -> list:
    """Extract red-coloured child elements from inside an element."""
    red_parts = []
    for child in el.find_all(True):
        if _style_has_red(child.get("style", "")):
            red_parts.append(child)
    return red_parts


def _get_text(el: Tag) -> str:
    """Get normalised plain text from an element."""
    return el.get_text(separator=" ", strip=True)


def _get_body_children(html: str) -> list:
    """Parse HTML and return direct children of the body (or top-level div)."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body")
    if not body:
        return [c for c in soup.children if isinstance(c, Tag)]

    div = body.find("div", recursive=False)
    container = div if div else body

    return [c for c in container.children if isinstance(c, Tag)]


def _text_similarity(a: str, b: str) -> float:
    """Return similarity ratio between two strings (0.0 to 1.0)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

_MATCH_THRESHOLD = 0.5


def _find_best_anchor(anchor_text: str, notion_texts: list[str]) -> Optional[int]:
    """Find the index in notion_texts that best matches anchor_text."""
    if not anchor_text:
        return None
    best_idx = None
    best_score = _MATCH_THRESHOLD
    for i, nt in enumerate(notion_texts):
        score = _text_similarity(anchor_text, nt)
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def extract_red_items(onenote_html: str) -> list[dict]:
    """Find red text elements in OneNote HTML with their neighboring context.

    Returns a list of dicts:
    {
        "html": str,                 # the red element(s) as HTML
        "prev_text": str | None,     # plain text of previous sibling
        "next_text": str | None,     # plain text of next sibling
        "position": "start" | "middle" | "end",
    }
    """
    elements = _get_body_children(onenote_html)
    items = []

    for i, el in enumerate(elements):
        if not isinstance(el, Tag):
            continue
        if not _has_red_text(el):
            continue

        if _is_entirely_red(el):
            html_str = str(el)
        else:
            red_spans = _extract_red_spans(el)
            if not red_spans:
                continue
            html_str = "".join(f"<p>{span}</p>" for span in red_spans)

        prev_el = None
        for j in range(i - 1, -1, -1):
            if isinstance(elements[j], Tag) and not _has_red_text(elements[j]):
                prev_el = elements[j]
                break

        next_el = None
        for j in range(i + 1, len(elements)):
            if isinstance(elements[j], Tag) and not _has_red_text(elements[j]):
                next_el = elements[j]
                break

        if prev_el is None:
            position = "start"
        elif next_el is None:
            position = "end"
        else:
            position = "middle"
        
        prev_text = _get_text(prev_el) if prev_el is not None else None
        next_text = _get_text(next_el) if next_el is not None else None
        items.append({
            "html": html_str,
            "prev_text": prev_text,
            "next_text": next_text,
            "position": position,
        })

        logger.info(f"Found teacher note: ...{prev_text} {html_str} {next_text}...")

    return items


def _html_to_text(html_str: str) -> str:
    """Extract plain text from an HTML fragment."""
    soup = BeautifulSoup(html_str, "lxml")
    return soup.get_text(separator=" ", strip=True)


def _insert_red_items(notion_html: str, red_items: list[dict]) -> str:
    """Place red items into fresh Notion HTML using text-matching anchors."""
    notion_elements = _get_body_children(notion_html)
    notion_texts = [_get_text(el) for el in notion_elements]
    notion_full_text = " ".join(notion_texts).lower()

    insertion_map: dict[int, list[str]] = {}

    for item in red_items:
        red_text = _html_to_text(item["html"]).strip()
        if red_text and red_text.lower() in notion_full_text:
            logger.debug(
                "Skipping red text already in Notion content: %s",
                red_text[:80],
            )
            continue

        insert_after = None

        if item["position"] == "start":
            insert_after = -1
        elif item["prev_text"]:
            insert_after = _find_best_anchor(item["prev_text"], notion_texts)

        if insert_after is None and item["next_text"]:
            next_match = _find_best_anchor(item["next_text"], notion_texts)
            if next_match is not None:
                insert_after = next_match - 1

        if insert_after is None:
            insert_after = len(notion_elements) - 1

        insertion_map.setdefault(insert_after, []).append(item["html"])

    merged_parts = []

    if -1 in insertion_map:
        merged_parts.extend(insertion_map[-1])

    for i, el in enumerate(notion_elements):
        merged_parts.append(str(el))
        if i in insertion_map:
            merged_parts.extend(insertion_map[i])

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


def merge_html(notion_html: str, onenote_html: str) -> str:
    """Merge fresh Notion HTML with red teacher feedback from OneNote.

    Args:
        notion_html: Fresh HTML generated from current Notion content.
        onenote_html: Current OneNote page HTML (may contain red teacher text).

    Returns:
        Merged HTML with Notion content updated and red teacher text preserved.
    """
    if not onenote_html or not onenote_html.strip():
        return notion_html

    red_items = extract_red_items(onenote_html)
    if not red_items:
        logger.debug("No red teacher text found, using fresh Notion HTML")
        return notion_html

    return _insert_red_items(notion_html, red_items)


def merge_harvested(notion_html: str, harvested_items: list[dict]) -> str:
    """Apply previously harvested red items to fresh Notion HTML.

    Args:
        notion_html: Fresh HTML generated from current Notion content.
        harvested_items: Red items from a prior extract_red_items() call,
            each with "html", "prev_text", "next_text", "position" keys.

    Returns:
        HTML with red teacher text inserted at matched positions.
    """
    if not harvested_items:
        return notion_html
    return _insert_red_items(notion_html, harvested_items)
