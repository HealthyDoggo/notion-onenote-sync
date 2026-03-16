"""Tests for the bidirectional block converter."""

import pytest

from block_converter import (
    blocks_to_html,
    content_hash_blocks,
    content_hash_html,
    html_to_blocks,
    page_to_html,
    rich_text_to_html,
)


# ── Helper to build Notion rich_text ──────────────────────────────────────────

def _rt(text, bold=False, italic=False, code=False, strikethrough=False, underline=False, link=None):
    rt = {
        "type": "text",
        "plain_text": text,
        "text": {"content": text},
        "annotations": {
            "bold": bold,
            "italic": italic,
            "code": code,
            "strikethrough": strikethrough,
            "underline": underline,
            "color": "default",
        },
    }
    if link:
        rt["text"]["link"] = {"url": link}
        rt["href"] = link
    return rt


# ── Rich text tests ──────────────────────────────────────────────────────────

class TestRichTextToHtml:
    def test_plain_text(self):
        assert rich_text_to_html([_rt("hello")]) == "hello"

    def test_bold(self):
        assert "<b>bold</b>" in rich_text_to_html([_rt("bold", bold=True)])

    def test_italic(self):
        assert "<i>em</i>" in rich_text_to_html([_rt("em", italic=True)])

    def test_code(self):
        assert "<code>x</code>" in rich_text_to_html([_rt("x", code=True)])

    def test_strikethrough(self):
        assert "<s>del</s>" in rich_text_to_html([_rt("del", strikethrough=True)])

    def test_underline(self):
        assert "<u>u</u>" in rich_text_to_html([_rt("u", underline=True)])

    def test_link(self):
        html = rich_text_to_html([_rt("click", link="https://example.com")])
        assert 'href="https://example.com"' in html
        assert ">click</a>" in html

    def test_combined_annotations(self):
        html = rich_text_to_html([_rt("text", bold=True, italic=True)])
        assert "<b>" in html
        assert "<i>" in html

    def test_html_escaping(self):
        html = rich_text_to_html([_rt("<script>alert('xss')</script>")])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ── Forward conversion tests ─────────────────────────────────────────────────

class TestBlocksToHtml:
    def test_paragraph(self):
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("Hello world")]}}]
        html = blocks_to_html(blocks)
        assert "<p>Hello world</p>" in html

    def test_headings(self):
        for level in (1, 2, 3):
            block_type = f"heading_{level}"
            blocks = [{"type": block_type, block_type: {"rich_text": [_rt(f"H{level}")]}}]
            html = blocks_to_html(blocks)
            assert f"<h{level}>H{level}</h{level}>" in html

    def test_bulleted_list_grouped(self):
        blocks = [
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [_rt("A")]}},
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [_rt("B")]}},
        ]
        html = blocks_to_html(blocks)
        assert html.count("<ul>") == 1
        assert "<li>A</li>" in html
        assert "<li>B</li>" in html

    def test_numbered_list_grouped(self):
        blocks = [
            {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [_rt("1st")]}},
            {"type": "numbered_list_item", "numbered_list_item": {"rich_text": [_rt("2nd")]}},
        ]
        html = blocks_to_html(blocks)
        assert html.count("<ol>") == 1

    def test_to_do_unchecked(self):
        blocks = [{"type": "to_do", "to_do": {"rich_text": [_rt("Task")], "checked": False}}]
        html = blocks_to_html(blocks)
        assert 'data-tag="to-do"' in html

    def test_to_do_checked(self):
        blocks = [{"type": "to_do", "to_do": {"rich_text": [_rt("Done")], "checked": True}}]
        html = blocks_to_html(blocks)
        assert 'data-tag="to-do:completed"' in html

    def test_code_block(self):
        blocks = [{"type": "code", "code": {"rich_text": [_rt("print('hi')")], "language": "python"}}]
        html = blocks_to_html(blocks)
        assert "<pre" in html
        assert "<code>" in html
        assert "python" in html

    def test_quote(self):
        blocks = [{"type": "quote", "quote": {"rich_text": [_rt("A quote")]}}]
        html = blocks_to_html(blocks)
        assert "<blockquote>A quote</blockquote>" in html

    def test_divider(self):
        blocks = [{"type": "divider", "divider": {}}]
        html = blocks_to_html(blocks)
        assert "<hr />" in html

    def test_image(self):
        blocks = [{
            "type": "image",
            "image": {
                "type": "external",
                "external": {"url": "https://img.example.com/photo.jpg"},
                "caption": [],
            },
        }]
        html = blocks_to_html(blocks)
        assert '<img src="https://img.example.com/photo.jpg"' in html

    def test_callout_basic(self):
        blocks = [{
            "type": "callout",
            "callout": {
                "rich_text": [_rt("Important note")],
                "icon": {"type": "emoji", "emoji": "💡"},
                "color": "blue_background",
            },
        }]
        html = blocks_to_html(blocks)
        assert 'data-notion-type="callout"' in html
        assert 'data-notion-color="blue_background"' in html
        assert "Important note" in html
        assert "💡" in html

    def test_callout_with_children(self):
        blocks = [{
            "type": "callout",
            "callout": {
                "rich_text": [_rt("Parent")],
                "icon": {"type": "emoji", "emoji": "📝"},
                "color": "yellow_background",
                "children": [
                    {"type": "paragraph", "paragraph": {"rich_text": [_rt("Child para")]}},
                ],
            },
        }]
        html = blocks_to_html(blocks)
        assert "Parent" in html
        assert "Child para" in html


# ── Reverse conversion tests ─────────────────────────────────────────────────

class TestHtmlToBlocks:
    def test_paragraph(self):
        blocks = html_to_blocks("<p>Hello</p>")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"
        assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "Hello"

    def test_headings(self):
        for level in (1, 2, 3):
            blocks = html_to_blocks(f"<h{level}>Title</h{level}>")
            assert blocks[0]["type"] == f"heading_{level}"

    def test_bulleted_list(self):
        blocks = html_to_blocks("<ul><li>A</li><li>B</li></ul>")
        assert len(blocks) == 2
        assert all(b["type"] == "bulleted_list_item" for b in blocks)

    def test_numbered_list(self):
        blocks = html_to_blocks("<ol><li>First</li><li>Second</li></ol>")
        assert len(blocks) == 2
        assert all(b["type"] == "numbered_list_item" for b in blocks)

    def test_code_block(self):
        blocks = html_to_blocks('<pre data-notion-language="python"><code>print("hi")</code></pre>')
        assert blocks[0]["type"] == "code"
        assert blocks[0]["code"]["language"] == "python"

    def test_blockquote(self):
        blocks = html_to_blocks("<blockquote>Quoted text</blockquote>")
        assert blocks[0]["type"] == "quote"

    def test_divider(self):
        blocks = html_to_blocks("<hr />")
        assert blocks[0]["type"] == "divider"

    def test_to_do(self):
        blocks = html_to_blocks('<p data-tag="to-do">Task</p>')
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is False

    def test_to_do_completed(self):
        blocks = html_to_blocks('<p data-tag="to-do:completed">Done</p>')
        assert blocks[0]["type"] == "to_do"
        assert blocks[0]["to_do"]["checked"] is True

    def test_image(self):
        blocks = html_to_blocks('<img src="https://example.com/img.png" alt="Photo" />')
        assert blocks[0]["type"] == "image"
        assert blocks[0]["image"]["external"]["url"] == "https://example.com/img.png"

    def test_callout_roundtrip_attributes(self):
        html = (
            '<table data-notion-type="callout" data-notion-color="blue_background">'
            "<tr>"
            '<td data-notion-icon="💡">💡</td>'
            "<td><p>Important</p></td>"
            "</tr>"
            "</table>"
        )
        blocks = html_to_blocks(html)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "callout"
        assert blocks[0]["callout"]["color"] == "blue_background"
        assert blocks[0]["callout"]["icon"]["emoji"] == "💡"


# ── Round-trip tests ──────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_paragraph_roundtrip(self):
        original = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("Test paragraph")]}}]
        html = blocks_to_html(original)
        result = html_to_blocks(html)
        assert result[0]["type"] == "paragraph"
        assert "Test paragraph" in result[0]["paragraph"]["rich_text"][0]["text"]["content"]

    def test_heading_roundtrip(self):
        for level in (1, 2, 3):
            btype = f"heading_{level}"
            original = [{"type": btype, btype: {"rich_text": [_rt(f"Heading {level}")]}}]
            html = blocks_to_html(original)
            result = html_to_blocks(html)
            assert result[0]["type"] == btype

    def test_callout_roundtrip(self):
        original = [{
            "type": "callout",
            "callout": {
                "rich_text": [_rt("Callout text")],
                "icon": {"type": "emoji", "emoji": "🔥"},
                "color": "red_background",
                "children": [
                    {"type": "paragraph", "paragraph": {"rich_text": [_rt("Child")]}},
                ],
            },
        }]
        html = blocks_to_html(original)
        result = html_to_blocks(html)
        assert result[0]["type"] == "callout"
        assert result[0]["callout"]["color"] == "red_background"
        assert result[0]["callout"]["icon"]["emoji"] == "🔥"
        assert len(result[0]["callout"]["children"]) >= 1


# ── Content hashing tests ────────────────────────────────────────────────────

class TestContentHashing:
    def test_same_content_same_hash(self):
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("Hello")]}}]
        h1 = content_hash_blocks(blocks)
        h2 = content_hash_blocks(blocks)
        assert h1 == h2

    def test_different_content_different_hash(self):
        b1 = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("Hello")]}}]
        b2 = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("World")]}}]
        assert content_hash_blocks(b1) != content_hash_blocks(b2)

    def test_whitespace_normalized(self):
        b1 = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("hello   world")]}}]
        b2 = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("hello world")]}}]
        assert content_hash_blocks(b1) == content_hash_blocks(b2)

    def test_html_hash(self):
        h1 = content_hash_html("<p>Hello world</p>")
        h2 = content_hash_html("<p>Hello  world</p>")
        assert h1 == h2


# ── Page HTML generation ─────────────────────────────────────────────────────

class TestPageToHtml:
    def test_full_page(self):
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [_rt("Content")]}}]
        html = page_to_html("My Title", blocks)
        assert "<title>My Title</title>" in html
        assert "<p>Content</p>" in html
        assert "<!DOCTYPE html>" in html
