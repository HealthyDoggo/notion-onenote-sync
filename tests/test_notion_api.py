"""Tests for Notion API wrapper — specifically tree building and parent extraction."""

import pytest
from unittest.mock import MagicMock

import config
from notion_api import NotionSync

_PARENT_PROP = config.NOTION_PARENT_PROPERTY


def _make_page(page_id, title="Untitled", parent_id=None):
    """Build a minimal Notion page dict for testing."""
    props = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
    }
    if parent_id is not None:
        props[_PARENT_PROP] = {
            "type": "relation",
            "relation": [{"id": parent_id}],
        }
    else:
        props[_PARENT_PROP] = {
            "type": "relation",
            "relation": [],
        }
    return {
        "id": page_id,
        "properties": props,
        "last_edited_time": "2026-03-16T00:00:00.000Z",
    }


@pytest.fixture
def notion():
    n = NotionSync.__new__(NotionSync)
    n._token = "fake"
    n._database_id = "fake"
    n._client = MagicMock()
    n._last_request_time = 0.0
    return n


class TestGetParentId:
    def test_no_parent(self, notion):
        page = _make_page("p1", "Root")
        assert notion.get_parent_id(page) is None

    def test_with_parent(self, notion):
        page = _make_page("p2", "Child", parent_id="p1")
        assert notion.get_parent_id(page) == "p1"

    def test_missing_property(self, notion):
        page = {"id": "p3", "properties": {"Name": {"type": "title", "title": []}}}
        assert notion.get_parent_id(page) is None

    def test_non_relation_property(self, notion):
        page = {
            "id": "p4",
            "properties": {
                "Parent": {"type": "rich_text", "rich_text": []},
            },
        }
        assert notion.get_parent_id(page) is None


class TestBuildPageTree:
    def test_single_root(self, notion):
        pages = [_make_page("r1", "Root")]
        roots = notion.build_page_tree(pages)
        assert len(roots) == 1
        assert roots[0]["id"] == "r1"
        assert roots[0]["_depth"] == 0
        assert roots[0]["_children"] == []

    def test_root_with_children(self, notion):
        pages = [
            _make_page("r1", "Social Influence"),
            _make_page("c1", "Conformity", parent_id="r1"),
            _make_page("c2", "Obedience", parent_id="r1"),
        ]
        roots = notion.build_page_tree(pages)
        assert len(roots) == 1
        root = roots[0]
        assert root["_depth"] == 0
        assert len(root["_children"]) == 2
        assert root["_children"][0]["_depth"] == 1
        assert root["_children"][1]["_depth"] == 1

    def test_nested_three_levels(self, notion):
        pages = [
            _make_page("r1", "Topic"),
            _make_page("c1", "Subtopic", parent_id="r1"),
            _make_page("gc1", "Detail", parent_id="c1"),
        ]
        roots = notion.build_page_tree(pages)
        assert len(roots) == 1
        assert roots[0]["_depth"] == 0
        child = roots[0]["_children"][0]
        assert child["_depth"] == 1
        grandchild = child["_children"][0]
        assert grandchild["_depth"] == 2
        assert grandchild["id"] == "gc1"

    def test_multiple_roots(self, notion):
        pages = [
            _make_page("r1", "Social Influence"),
            _make_page("r2", "Memory"),
            _make_page("c1", "Conformity", parent_id="r1"),
            _make_page("c2", "Models of Memory", parent_id="r2"),
        ]
        roots = notion.build_page_tree(pages)
        assert len(roots) == 2
        root_ids = {r["id"] for r in roots}
        assert root_ids == {"r1", "r2"}

    def test_orphan_treated_as_root(self, notion):
        """A page whose parent isn't in the query results becomes a root."""
        pages = [
            _make_page("c1", "Orphan Child", parent_id="missing-parent"),
        ]
        roots = notion.build_page_tree(pages)
        assert len(roots) == 1
        assert roots[0]["id"] == "c1"
        assert roots[0]["_depth"] == 0

    def test_parent_id_set_on_children(self, notion):
        pages = [
            _make_page("r1", "Root"),
            _make_page("c1", "Child", parent_id="r1"),
        ]
        roots = notion.build_page_tree(pages)
        child = roots[0]["_children"][0]
        assert child["_parent_id"] == "r1"

    def test_empty_pages(self, notion):
        roots = notion.build_page_tree([])
        assert roots == []
