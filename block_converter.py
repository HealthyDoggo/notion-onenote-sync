"""Bidirectional converter between Notion blocks and OneNote-compatible HTML.

Forward:  Notion block tree  → HTML string (for OneNote Graph API)
Reverse:  OneNote HTML string → Notion block list (for Notion API)
"""

import hashlib
import math
import re
from html import escape
from typing import Optional

from bs4 import BeautifulSoup, Tag

from config import MAX_CALLOUT_NESTING_DEPTH, NOTION_COLOURS

# ── Colour reverse-mapping (hex bg → Notion colour key) ───────────────────────

_HEX_TO_NOTION_COLOUR: dict[str, str] = {bg: key for key, (bg, _) in NOTION_COLOURS.items()}


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _nearest_notion_colour(hex_bg: str) -> str:
    """Find the closest Notion colour key by Euclidean distance in RGB space."""
    if hex_bg in _HEX_TO_NOTION_COLOUR:
        return _HEX_TO_NOTION_COLOUR[hex_bg]
    r, g, b = _hex_to_rgb(hex_bg)
    best_key, best_dist = "default", float("inf")
    for key, (bg, _) in NOTION_COLOURS.items():
        cr, cg, cb = _hex_to_rgb(bg)
        dist = math.sqrt((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2)
        if dist < best_dist:
            best_key, best_dist = key, dist
    return best_key


# ══════════════════════════════════════════════════════════════════════════════
#  FORWARD: Notion → HTML
# ══════════════════════════════════════════════════════════════════════════════


def rich_text_to_html(rich_texts: list[dict]) -> str:
    """Convert Notion rich_text array to inline HTML."""
    parts: list[str] = []
    for rt in rich_texts:
        text = escape(rt.get("plain_text", ""))
        ann = rt.get("annotations", {})
        href = rt.get("href") or (rt.get("text", {}).get("link") or {}).get("url")

        if ann.get("code"):
            text = f"<code>{text}</code>"
        if ann.get("bold"):
            text = f"<b>{text}</b>"
        if ann.get("italic"):
            text = f"<i>{text}</i>"
        if ann.get("strikethrough"):
            text = f"<s>{text}</s>"
        if ann.get("underline"):
            text = f"<u>{text}</u>"

        color = ann.get("color", "default")
        if color and color != "default":
            text = f'<span style="color:{color}">{text}</span>'

        if href:
            text = f'<a href="{escape(href)}">{text}</a>'

        parts.append(text)
    return "".join(parts)


def _get_icon_html(icon: Optional[dict]) -> str:
    if not icon:
        return "💡"
    if icon.get("type") == "emoji":
        return icon["emoji"]
    if icon.get("type") == "external":
        url = icon["external"]["url"]
        return f'<img src="{escape(url)}" style="width:20px;height:20px;" />'
    return "💡"


def _callout_to_html(block: dict, depth: int = 0) -> str:
    callout = block["callout"]
    icon = _get_icon_html(callout.get("icon"))
    color_key = callout.get("color", "default")
    bg_color, accent_color = NOTION_COLOURS.get(color_key, NOTION_COLOURS["default"])
    rich_text_html = rich_text_to_html(callout.get("rich_text", []))

    children_html = ""
    children = callout.get("children") or block.get("children", [])
    if children:
        if depth >= MAX_CALLOUT_NESTING_DEPTH:
            # Flatten deeply nested callouts into indented paragraphs
            for child in children:
                children_html += _block_to_html_inner(child, depth + 1)
        else:
            for child in children:
                children_html += _block_to_html_inner(child, depth + 1)

    icon_attr = escape(icon) if not icon.startswith("<") else escape(icon)
    margin = 4 if depth > 0 else 8

    return (
        f'<table data-notion-type="callout" data-notion-color="{escape(color_key)}"'
        f' style="border-collapse:collapse;width:100%;margin:{margin}px 0;">'
        f"<tr>"
        f'<td data-notion-icon="{icon_attr}" style="'
        f"width:28px;vertical-align:top;padding:8px 4px 8px 8px;"
        f"background:{bg_color};"
        f"border-left:3px solid {accent_color};"
        f"border-top:1px solid {bg_color};"
        f'border-bottom:1px solid {bg_color};font-size:18px;">'
        f"{icon}"
        f"</td>"
        f'<td style="'
        f"vertical-align:top;padding:8px 12px;"
        f"background:{bg_color};"
        f"border-right:1px solid {bg_color};"
        f"border-top:1px solid {bg_color};"
        f'border-bottom:1px solid {bg_color};">'
        f'<p style="margin:0 0 4px 0;">{rich_text_html}</p>'
        f"{children_html}"
        f"</td>"
        f"</tr>"
        f"</table>"
    )


def _list_items_to_html(blocks: list[dict], start_idx: int, list_type: str) -> tuple[str, int]:
    """Consume consecutive list items of the same type and wrap in <ul>/<ol>."""
    tag = "ul" if list_type == "bulleted_list_item" else "ol"
    items: list[str] = []
    i = start_idx
    while i < len(blocks) and blocks[i].get("type") == list_type:
        block_data = blocks[i][list_type]
        text = rich_text_to_html(block_data.get("rich_text", []))
        children_html = ""
        children = block_data.get("children") or blocks[i].get("children", [])
        if children:
            children_html = blocks_to_html(children)
        items.append(f"<li>{text}{children_html}</li>")
        i += 1
    return f"<{tag}>{''.join(items)}</{tag}>", i


def _block_to_html_inner(block: dict, depth: int = 0) -> str:
    """Convert a single Notion block to HTML. Used internally for recursion."""
    btype = block.get("type", "")

    if btype == "paragraph":
        text = rich_text_to_html(block["paragraph"].get("rich_text", []))
        children_html = ""
        children = block["paragraph"].get("children") or block.get("children", [])
        if children:
            children_html = blocks_to_html(children)
        return f"<p>{text}</p>{children_html}" if text else f"<p>&nbsp;</p>{children_html}"

    if btype in ("heading_1", "heading_2", "heading_3"):
        level = btype[-1]
        text = rich_text_to_html(block[btype].get("rich_text", []))
        return f"<h{level}>{text}</h{level}>"

    if btype == "bulleted_list_item":
        text = rich_text_to_html(block[btype].get("rich_text", []))
        children_html = ""
        children = block[btype].get("children") or block.get("children", [])
        if children:
            children_html = blocks_to_html(children)
        return f"<ul><li>{text}{children_html}</li></ul>"

    if btype == "numbered_list_item":
        text = rich_text_to_html(block[btype].get("rich_text", []))
        children_html = ""
        children = block[btype].get("children") or block.get("children", [])
        if children:
            children_html = blocks_to_html(children)
        return f"<ol><li>{text}{children_html}</li></ol>"

    if btype == "to_do":
        todo = block["to_do"]
        text = rich_text_to_html(todo.get("rich_text", []))
        checked = todo.get("checked", False)
        data_tag = "to-do:completed" if checked else "to-do"
        return f'<p data-tag="{data_tag}">{text}</p>'

    if btype == "code":
        code_data = block["code"]
        text = rich_text_to_html(code_data.get("rich_text", []))
        lang = code_data.get("language", "")
        return f'<pre data-notion-language="{escape(lang)}"><code>{text}</code></pre>'

    if btype == "quote":
        text = rich_text_to_html(block["quote"].get("rich_text", []))
        children_html = ""
        children = block["quote"].get("children") or block.get("children", [])
        if children:
            children_html = blocks_to_html(children)
        return f"<blockquote>{text}{children_html}</blockquote>"

    if btype == "callout":
        return _callout_to_html(block, depth)

    if btype == "toggle":
        toggle = block["toggle"]
        text = rich_text_to_html(toggle.get("rich_text", []))
        children_html = ""
        children = toggle.get("children") or block.get("children", [])
        if children:
            children_html = blocks_to_html(children)
        return f"<h3>{text}</h3>{children_html}"

    if btype == "image":
        img = block["image"]
        url = ""
        if img.get("type") == "external":
            url = img["external"]["url"]
        elif img.get("type") == "file":
            url = img["file"]["url"]
        caption = rich_text_to_html(img.get("caption", []))
        html = f'<img src="{escape(url)}" alt="{escape(caption)}" />'
        if caption:
            html += f"<p><em>{caption}</em></p>"
        return html

    if btype == "divider":
        return "<hr />"

    if btype == "table":
        table_data = block["table"]
        children = block.get("children", [])
        has_header = table_data.get("has_column_header", False)
        rows_html: list[str] = []
        for i, row_block in enumerate(children):
            cells = row_block.get("table_row", {}).get("cells", [])
            cell_tag = "th" if (has_header and i == 0) else "td"
            cells_html = "".join(
                f"<{cell_tag}>{rich_text_to_html(cell)}</{cell_tag}>"
                for cell in cells
            )
            rows_html.append(f"<tr>{cells_html}</tr>")
        return f'<table data-notion-type="table">{"".join(rows_html)}</table>'

    if btype == "column_list":
        children = block.get("children", [])
        parts = []
        for col in children:
            col_children = col.get("children", [])
            parts.append(blocks_to_html(col_children))
        return "".join(parts)

    if btype == "bookmark":
        url = block["bookmark"].get("url", "")
        caption = rich_text_to_html(block["bookmark"].get("caption", []))
        label = caption or escape(url)
        return f'<p><a href="{escape(url)}">{label}</a></p>'

    # Unsupported block type — render as empty paragraph
    return f'<p data-notion-unsupported="{escape(btype)}">&nbsp;</p>'


def blocks_to_html(blocks: list[dict]) -> str:
    """Convert a list of Notion blocks to HTML, grouping consecutive list items."""
    html_parts: list[str] = []
    i = 0
    while i < len(blocks):
        btype = blocks[i].get("type", "")
        if btype in ("bulleted_list_item", "numbered_list_item"):
            chunk, i = _list_items_to_html(blocks, i, btype)
            html_parts.append(chunk)
        else:
            html_parts.append(_block_to_html_inner(blocks[i]))
            i += 1
    return "".join(html_parts)


def page_to_html(title: str, blocks: list[dict]) -> str:
    """Build a full OneNote page HTML document from a Notion page."""
    body = blocks_to_html(blocks)
    return (
        '<!DOCTYPE html>\n'
        '<html>\n'
        '<head>\n'
        f'  <title>{escape(title)}</title>\n'
        '</head>\n'
        '<body>\n'
        f'{body}\n'
        '</body>\n'
        '</html>'
    )


# ══════════════════════════════════════════════════════════════════════════════
#  REVERSE: HTML → Notion blocks
# ══════════════════════════════════════════════════════════════════════════════


def _html_inline_to_rich_text(element) -> list[dict]:
    """Parse inline HTML children into Notion rich_text array."""
    rich_texts: list[dict] = []

    for child in (element.children if hasattr(element, "children") else [element]):
        if isinstance(child, str):
            text = child
            if not text.strip() and not text:
                continue
            rich_texts.append({
                "type": "text",
                "text": {"content": text},
                "annotations": _default_annotations(),
            })
            continue

        if not isinstance(child, Tag):
            continue

        text_content = child.get_text()
        annotations = _default_annotations()
        href = None

        tags_to_check = [child] + list(child.parents)
        for tag in _iter_inline_tags(child):
            name = tag.name if isinstance(tag, Tag) else ""
            if name == "b" or name == "strong":
                annotations["bold"] = True
            elif name == "i" or name == "em":
                annotations["italic"] = True
            elif name == "s" or name == "del":
                annotations["strikethrough"] = True
            elif name == "u" or name == "ins":
                annotations["underline"] = True
            elif name == "code":
                annotations["code"] = True
            elif name == "a":
                href = tag.get("href")

        rt: dict = {
            "type": "text",
            "text": {"content": text_content},
            "annotations": annotations,
        }
        if href:
            rt["text"]["link"] = {"url": href}

        if text_content:
            rich_texts.append(rt)

    return rich_texts if rich_texts else [{"type": "text", "text": {"content": ""}, "annotations": _default_annotations()}]


def _iter_inline_tags(tag: Tag):
    """Yield the tag and all its parent inline tags up to block-level."""
    yield tag
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            break
        if parent.name in ("p", "h1", "h2", "h3", "li", "td", "th", "blockquote", "pre", "body", "html", "[document]"):
            break
        yield parent


def _default_annotations() -> dict:
    return {
        "bold": False,
        "italic": False,
        "strikethrough": False,
        "underline": False,
        "code": False,
        "color": "default",
    }


def _parse_element_to_block(el: Tag) -> Optional[dict]:
    """Convert an HTML element to a Notion block dict."""
    if not isinstance(el, Tag):
        return None

    name = el.name

    # Callout detection (table with data-notion-type="callout")
    if name == "table":
        if el.get("data-notion-type") == "callout":
            return _parse_callout_table(el)
        if el.get("data-notion-type") == "table":
            return _parse_data_table(el)
        # Heuristic: single-row, two-column table with narrow first col and emoji
        if _looks_like_callout(el):
            return _parse_callout_table_heuristic(el)
        return _parse_data_table(el)

    if name in ("h1", "h2", "h3"):
        level = name[1]
        heading_type = f"heading_{level}"
        return {
            "type": heading_type,
            heading_type: {"rich_text": _html_inline_to_rich_text(el)},
        }

    if name == "p":
        data_tag = el.get("data-tag", "")
        if data_tag.startswith("to-do"):
            checked = "completed" in data_tag
            return {
                "type": "to_do",
                "to_do": {
                    "rich_text": _html_inline_to_rich_text(el),
                    "checked": checked,
                },
            }
        return {
            "type": "paragraph",
            "paragraph": {"rich_text": _html_inline_to_rich_text(el)},
        }

    if name == "ul":
        items = []
        for li in el.find_all("li", recursive=False):
            items.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _html_inline_to_rich_text(li)},
            })
        return items  # type: ignore[return-value]

    if name == "ol":
        items = []
        for li in el.find_all("li", recursive=False):
            items.append({
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _html_inline_to_rich_text(li)},
            })
        return items  # type: ignore[return-value]

    if name == "pre":
        code_el = el.find("code")
        text = code_el.get_text() if code_el else el.get_text()
        lang = el.get("data-notion-language", "plain text")
        return {
            "type": "code",
            "code": {
                "rich_text": [{"type": "text", "text": {"content": text}}],
                "language": lang,
            },
        }

    if name == "blockquote":
        return {
            "type": "quote",
            "quote": {"rich_text": _html_inline_to_rich_text(el)},
        }

    if name == "hr":
        return {"type": "divider", "divider": {}}

    if name == "img":
        src = el.get("src", "")
        alt = el.get("alt", "")
        if src:
            return {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": src},
                    "caption": [{"type": "text", "text": {"content": alt}}] if alt else [],
                },
            }

    return None


def _looks_like_callout(table: Tag) -> bool:
    """Heuristic: single-row, two-column table with a narrow first col holding an emoji."""
    rows = table.find_all("tr", recursive=False)
    if not rows:
        tbody = table.find("tbody")
        rows = tbody.find_all("tr", recursive=False) if tbody else []
    if len(rows) != 1:
        return False
    cells = rows[0].find_all("td", recursive=False)
    if len(cells) != 2:
        return False
    first_text = cells[0].get_text(strip=True)
    # Check if first cell is a single emoji or very short
    if len(first_text) <= 2:
        return True
    return False


def _parse_callout_table(table: Tag) -> dict:
    """Parse a callout from a table with data-notion-type='callout' attributes."""
    color_key = table.get("data-notion-color", "default")
    cells = table.find_all("td")
    icon_cell = cells[0] if cells else None
    content_cell = cells[1] if len(cells) > 1 else None

    icon_raw = icon_cell.get("data-notion-icon", "💡") if icon_cell else "💡"
    # Clean up HTML-escaped icon
    if "&" in icon_raw:
        icon_raw = BeautifulSoup(icon_raw, "html.parser").get_text()

    rich_text = []
    children = []

    if content_cell:
        first_p = content_cell.find("p")
        if first_p:
            rich_text = _html_inline_to_rich_text(first_p)
            for sibling in first_p.find_next_siblings():
                result = _parse_element_to_block(sibling)
                if result is None:
                    continue
                if isinstance(result, list):
                    children.extend(result)
                else:
                    children.append(result)
        else:
            rich_text = _html_inline_to_rich_text(content_cell)

    return {
        "type": "callout",
        "callout": {
            "rich_text": rich_text,
            "icon": {"type": "emoji", "emoji": icon_raw},
            "color": color_key,
            "children": children,
        },
    }


def _parse_callout_table_heuristic(table: Tag) -> dict:
    """Parse a callout from a table that looks like one based on heuristics."""
    rows = table.find_all("tr", recursive=False)
    if not rows:
        tbody = table.find("tbody")
        rows = tbody.find_all("tr", recursive=False) if tbody else []
    cells = rows[0].find_all("td", recursive=False) if rows else []
    icon_text = cells[0].get_text(strip=True) if cells else "💡"
    content_cell = cells[1] if len(cells) > 1 else None

    # Try to detect background colour from inline styles
    color_key = "default"
    style = cells[1].get("style", "") if content_cell else ""
    bg_match = re.search(r"background:\s*(#[0-9a-fA-F]{6})", style)
    if bg_match:
        color_key = _nearest_notion_colour(bg_match.group(1))

    rich_text = _html_inline_to_rich_text(content_cell) if content_cell else []
    return {
        "type": "callout",
        "callout": {
            "rich_text": rich_text,
            "icon": {"type": "emoji", "emoji": icon_text or "💡"},
            "color": color_key,
            "children": [],
        },
    }


def _parse_data_table(table: Tag) -> dict:
    """Parse a regular HTML table into a Notion table block."""
    rows = table.find_all("tr")
    if not rows:
        return {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": table.get_text()}}]}}

    has_header = bool(rows[0].find("th"))
    table_rows = []
    col_count = 0

    for row in rows:
        cells = row.find_all(["td", "th"])
        col_count = max(col_count, len(cells))
        table_rows.append({
            "type": "table_row",
            "table_row": {
                "cells": [_html_inline_to_rich_text(cell) for cell in cells],
            },
        })

    return {
        "type": "table",
        "table": {
            "table_width": col_count,
            "has_column_header": has_header,
            "has_row_header": False,
            "children": table_rows,
        },
    }


def html_to_blocks(html_content: str) -> list[dict]:
    """Convert OneNote HTML to a list of Notion blocks."""
    soup = BeautifulSoup(html_content, "lxml")
    body = soup.find("body") or soup

    blocks: list[dict] = []
    for el in body.children:
        if not isinstance(el, Tag):
            text = str(el).strip()
            if text:
                blocks.append({
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
                })
            continue
        result = _parse_element_to_block(el)
        if result is None:
            continue
        if isinstance(result, list):
            blocks.extend(result)
        else:
            blocks.append(result)
    return blocks


# ══════════════════════════════════════════════════════════════════════════════
#  CONTENT HASHING (for diff detection / echo loop prevention)
# ══════════════════════════════════════════════════════════════════════════════


def _normalize_text(text: str) -> str:
    """Strip whitespace and formatting diffs for content comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def content_hash_blocks(blocks: list[dict]) -> str:
    """Compute SHA-256 of normalised text content from a Notion block tree."""
    texts: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        block_data = block.get(btype, {})
        if isinstance(block_data, dict):
            for rt in block_data.get("rich_text", []):
                texts.append(rt.get("plain_text", rt.get("text", {}).get("content", "")))
            children = block_data.get("children") or block.get("children", [])
            if children:
                texts.append(content_hash_blocks(children))
    normalised = _normalize_text(" ".join(texts))
    return hashlib.sha256(normalised.encode()).hexdigest()


def content_hash_html(html_content: str) -> str:
    """Compute SHA-256 of normalised text content from HTML."""
    soup = BeautifulSoup(html_content, "lxml")
    text = _normalize_text(soup.get_text())
    return hashlib.sha256(text.encode()).hexdigest()
