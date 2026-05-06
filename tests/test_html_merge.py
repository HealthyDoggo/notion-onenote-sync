"""Tests for HTML merge logic — preserving teacher feedback during updates."""

import pytest

from config import SYNC_FINGERPRINT_STYLE
from html_merge import classify_elements, merge_html

_FP = SYNC_FINGERPRINT_STYLE


def _page(body_content: str) -> str:
    return (
        '<!DOCTYPE html><html><head><title>Test</title></head>'
        f'<body>{body_content}</body></html>'
    )


def _fp(tag: str, content: str, extra_style: str = "") -> str:
    style = f"{_FP};{extra_style}" if extra_style else _FP
    return f'<{tag} style="{style}">{content}</{tag}>'


class TestClassifyElements:
    def test_all_synced(self):
        html = _page(
            f'<p style="{_FP}">Para 1</p>'
            f'<p style="{_FP}">Para 2</p>'
        )
        classified = classify_elements(html)
        assert all(c["type"] == "synced" for c in classified)

    def test_teacher_paragraph(self):
        html = _page(
            f'<p style="{_FP}">Synced para</p>'
            '<p>Teacher comment</p>'
        )
        classified = classify_elements(html)
        assert classified[0]["type"] == "synced"
        assert classified[1]["type"] == "teacher"

    def test_red_text_detected_as_teacher(self):
        html = _page(
            f'<p style="{_FP}">My notes</p>'
            '<p style="color:red">Good work! - Teacher</p>'
        )
        classified = classify_elements(html)
        assert classified[1]["type"] == "teacher"

    def test_mixed_element_with_red_span(self):
        html = _page(
            f'<p style="{_FP}">My text <span style="color:red">teacher inline</span></p>'
        )
        classified = classify_elements(html)
        assert classified[0]["type"] == "mixed"
        assert len(classified[0]["red_spans"]) == 1

    def test_red_hex_variants(self):
        html = _page(
            '<p style="color:#ff0000">Feedback 1</p>'
            '<p style="color:#FF0000">Feedback 2</p>'
            '<p style="color:#e03e3e">Feedback 3</p>'
        )
        classified = classify_elements(html)
        for c in classified:
            assert c["type"] == "teacher"


class TestMergeHtml:
    def test_no_teacher_content(self):
        notion = _page(_fp("p", "Updated text"))
        onenote = _page(f'<p style="{_FP}">Old text</p>')
        result = merge_html(notion, onenote)
        assert "Updated text" in result
        assert "Old text" not in result

    def test_preserves_teacher_paragraph_between_synced(self):
        notion = _page(
            _fp("p", "Para A updated") + _fp("p", "Para B updated")
        )
        onenote = _page(
            f'<p style="{_FP}">Para A old</p>'
            '<p>Teacher says: nice work</p>'
            f'<p style="{_FP}">Para B old</p>'
        )
        result = merge_html(notion, onenote)
        assert "Para A updated" in result
        assert "Para B updated" in result
        assert "Teacher says: nice work" in result

    def test_preserves_teacher_paragraph_at_end(self):
        notion = _page(_fp("p", "Content"))
        onenote = _page(
            f'<p style="{_FP}">Content old</p>'
            '<p style="color:red">Final feedback</p>'
        )
        result = merge_html(notion, onenote)
        assert "Content" in result
        assert "Final feedback" in result

    def test_preserves_teacher_paragraph_at_top(self):
        notion = _page(_fp("p", "My content"))
        onenote = _page(
            '<p style="color:red">Please review this</p>'
            f'<p style="{_FP}">My content old</p>'
        )
        result = merge_html(notion, onenote)
        assert "My content" in result
        assert "Please review this" in result

    def test_mixed_element_preserves_red_spans(self):
        notion = _page(_fp("p", "Fresh text"))
        onenote = _page(
            f'<p style="{_FP}">Old text <span style="color:red">good!</span></p>'
        )
        result = merge_html(notion, onenote)
        assert "Fresh text" in result
        assert "good!" in result

    def test_empty_onenote_returns_notion(self):
        notion = _page(_fp("p", "New content"))
        result = merge_html(notion, "")
        assert "New content" in result

    def test_multiple_teacher_insertions(self):
        notion = _page(
            _fp("p", "A") + _fp("p", "B") + _fp("p", "C")
        )
        onenote = _page(
            f'<p style="{_FP}">A old</p>'
            '<p>Teacher note 1</p>'
            f'<p style="{_FP}">B old</p>'
            '<p>Teacher note 2</p>'
            f'<p style="{_FP}">C old</p>'
        )
        result = merge_html(notion, onenote)
        assert "A" in result
        assert "B" in result
        assert "C" in result
        assert "Teacher note 1" in result
        assert "Teacher note 2" in result

    def test_notion_adds_new_block_teacher_preserved(self):
        notion = _page(
            _fp("p", "Para A") + _fp("p", "New Para") + _fp("p", "Para B")
        )
        onenote = _page(
            f'<p style="{_FP}">Para A</p>'
            '<p>Teacher comment</p>'
            f'<p style="{_FP}">Para B</p>'
        )
        result = merge_html(notion, onenote)
        assert "Para A" in result
        assert "New Para" in result
        assert "Para B" in result
        assert "Teacher comment" in result

    def test_notion_deletes_block_teacher_preserved(self):
        notion = _page(_fp("p", "Para B"))
        onenote = _page(
            f'<p style="{_FP}">Para A old</p>'
            '<p>Teacher comment after A</p>'
            f'<p style="{_FP}">Para B old</p>'
        )
        result = merge_html(notion, onenote)
        assert "Para B" in result
        assert "Teacher comment after A" in result
        assert "Para A old" not in result


class TestNoRedInOutput:
    def test_notion_red_remapped(self):
        from block_converter import rich_text_to_html
        rt = [{"plain_text": "alert", "text": {"content": "alert"},
               "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                "underline": False, "code": False, "color": "red"}}]
        html = rich_text_to_html(rt)
        assert "color:red" not in html.lower()
        assert "#C23B22" in html


class TestFingerprintInOutput:
    def test_paragraph_has_fingerprint(self):
        from block_converter import blocks_to_html
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [
            {"plain_text": "Test", "text": {"content": "Test"}}
        ]}}]
        html = blocks_to_html(blocks)
        assert SYNC_FINGERPRINT_STYLE in html

    def test_heading_has_fingerprint(self):
        from block_converter import blocks_to_html
        blocks = [{"type": "heading_1", "heading_1": {"rich_text": [
            {"plain_text": "Title", "text": {"content": "Title"}}
        ]}}]
        html = blocks_to_html(blocks)
        assert SYNC_FINGERPRINT_STYLE in html
