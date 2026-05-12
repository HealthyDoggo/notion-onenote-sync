"""Tests for HTML merge logic — preserving red teacher text during updates."""

import pytest

from html_merge import extract_red_items, merge_html


def _page(body_content: str) -> str:
    return (
        '<!DOCTYPE html><html><head><title>Test</title></head>'
        f'<body>{body_content}</body></html>'
    )


class TestExtractRedItems:
    def test_no_red_text(self):
        html = _page('<p>Normal text</p><p>More text</p>')
        assert extract_red_items(html) == []

    def test_standalone_red_paragraph(self):
        html = _page(
            '<p>Before</p>'
            '<p style="color:red">Teacher comment</p>'
            '<p>After</p>'
        )
        items = extract_red_items(html)
        assert len(items) == 1
        assert "Teacher comment" in items[0]["html"]
        assert items[0]["prev_text"] == "Before"
        assert items[0]["next_text"] == "After"
        assert items[0]["position"] == "middle"

    def test_red_at_start(self):
        html = _page(
            '<p style="color:red">Top feedback</p>'
            '<p>Content</p>'
        )
        items = extract_red_items(html)
        assert len(items) == 1
        assert items[0]["position"] == "start"
        assert items[0]["prev_text"] is None
        assert items[0]["next_text"] == "Content"

    def test_red_at_end(self):
        html = _page(
            '<p>Content</p>'
            '<p style="color:red">Final note</p>'
        )
        items = extract_red_items(html)
        assert len(items) == 1
        assert items[0]["position"] == "end"
        assert items[0]["prev_text"] == "Content"
        assert items[0]["next_text"] is None

    def test_red_span_inside_paragraph(self):
        html = _page(
            '<p>Some text <span style="color:red">inline feedback</span> more text</p>'
        )
        items = extract_red_items(html)
        assert len(items) == 1
        assert "inline feedback" in items[0]["html"]

    def test_red_hex_variants(self):
        html = _page(
            '<p style="color:#ff0000">Feedback 1</p>'
            '<p style="color:#FF0000">Feedback 2</p>'
            '<p style="color:#e03e3e">Feedback 3</p>'
        )
        items = extract_red_items(html)
        assert len(items) == 3

    def test_skips_non_red_neighbours_for_anchors(self):
        html = _page(
            '<p>Anchor A</p>'
            '<p style="color:red">Red 1</p>'
            '<p style="color:red">Red 2</p>'
            '<p>Anchor B</p>'
        )
        items = extract_red_items(html)
        assert len(items) == 2
        assert items[0]["prev_text"] == "Anchor A"
        assert items[0]["next_text"] == "Anchor B"
        assert items[1]["prev_text"] == "Anchor A"
        assert items[1]["next_text"] == "Anchor B"


class TestMergeHtml:
    def test_no_red_text_returns_notion(self):
        notion = _page('<p>Updated text</p>')
        onenote = _page('<p>Old text</p>')
        result = merge_html(notion, onenote)
        assert "Updated text" in result
        assert "Old text" not in result

    def test_preserves_red_between_paragraphs(self):
        notion = _page('<p>Para A updated</p><p>Para B updated</p>')
        onenote = _page(
            '<p>Para A old</p>'
            '<p style="color:red">Teacher says: nice work</p>'
            '<p>Para B old</p>'
        )
        result = merge_html(notion, onenote)
        assert "Para A updated" in result
        assert "Para B updated" in result
        assert "Teacher says: nice work" in result
        assert "Para A old" not in result

    def test_preserves_red_at_end(self):
        notion = _page('<p>Content</p>')
        onenote = _page(
            '<p>Content old</p>'
            '<p style="color:red">Final feedback</p>'
        )
        result = merge_html(notion, onenote)
        assert "Content" in result
        assert "Final feedback" in result

    def test_preserves_red_at_start(self):
        notion = _page('<p>My content</p>')
        onenote = _page(
            '<p style="color:red">Please review this</p>'
            '<p>My content old</p>'
        )
        result = merge_html(notion, onenote)
        assert "My content" in result
        assert "Please review this" in result

    def test_red_span_extracted_from_paragraph(self):
        notion = _page('<p>Fresh text</p>')
        onenote = _page(
            '<p>Old text <span style="color:red">good!</span></p>'
        )
        result = merge_html(notion, onenote)
        assert "Fresh text" in result
        assert "good!" in result

    def test_empty_onenote_returns_notion(self):
        notion = _page('<p>New content</p>')
        result = merge_html(notion, "")
        assert "New content" in result

    def test_multiple_red_insertions(self):
        notion = _page('<p>A</p><p>B</p><p>C</p>')
        onenote = _page(
            '<p>A</p>'
            '<p style="color:red">Teacher note 1</p>'
            '<p>B</p>'
            '<p style="color:red">Teacher note 2</p>'
            '<p>C</p>'
        )
        result = merge_html(notion, onenote)
        assert "Teacher note 1" in result
        assert "Teacher note 2" in result

    def test_notion_adds_new_block_teacher_preserved(self):
        notion = _page('<p>Para A</p><p>New Para</p><p>Para B</p>')
        onenote = _page(
            '<p>Para A</p>'
            '<p style="color:red">Teacher comment</p>'
            '<p>Para B</p>'
        )
        result = merge_html(notion, onenote)
        assert "Para A" in result
        assert "New Para" in result
        assert "Para B" in result
        assert "Teacher comment" in result

    def test_notion_deletes_block_teacher_appended(self):
        notion = _page('<p>Para B</p>')
        onenote = _page(
            '<p>Para A old</p>'
            '<p style="color:red">Teacher comment after A</p>'
            '<p>Para B old</p>'
        )
        result = merge_html(notion, onenote)
        assert "Para B" in result
        assert "Teacher comment after A" in result

    def test_text_matching_with_similar_content(self):
        notion = _page(
            '<p>The quick brown fox jumps over the lazy dog</p>'
            '<p>Another paragraph here</p>'
        )
        onenote = _page(
            '<p>The quick brown fox jumps over the lazy dog</p>'
            '<p style="color:red">Great sentence!</p>'
            '<p>Another paragraph here</p>'
        )
        result = merge_html(notion, onenote)
        pos_fox = result.find("quick brown fox")
        pos_red = result.find("Great sentence!")
        pos_another = result.find("Another paragraph")
        assert pos_fox < pos_red < pos_another


    def test_skips_red_text_already_in_notion(self):
        notion = _page('<p>Some intro</p><p>Important warning</p>')
        onenote = _page(
            '<p>Some intro</p>'
            '<p style="color:red">Important warning</p>'
        )
        result = merge_html(notion, onenote)
        assert result.count("Important warning") == 1

    def test_keeps_red_text_not_in_notion(self):
        notion = _page('<p>Some intro</p><p>Regular content</p>')
        onenote = _page(
            '<p>Some intro</p>'
            '<p style="color:red">Teacher feedback here</p>'
            '<p>Regular content</p>'
        )
        result = merge_html(notion, onenote)
        assert "Teacher feedback here" in result


class TestNoRedInOutput:
    def test_notion_red_remapped(self):
        from block_converter import rich_text_to_html
        rt = [{"plain_text": "alert", "text": {"content": "alert"},
               "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                "underline": False, "code": False, "color": "red"}}]
        html = rich_text_to_html(rt)
        assert "color:red" not in html.lower()
        assert "#C23B22" in html
