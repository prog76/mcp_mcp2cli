#!/usr/bin/env python3
"""Tests for mcp2cli.cli._set_dotted_key — pure, no server needed."""

import pytest

from mcp2cli.cli import _set_dotted_key


class TestSetDottedKey:
    def test_simple_key(self):
        root: dict = {}
        _set_dotted_key(root, "name", "test")
        assert root == {"name": "test"}

    def test_nested_dotted_key(self):
        root: dict = {}
        _set_dotted_key(root, "a.b.c", 42)
        assert root == {"a": {"b": {"c": 42}}}

    def test_array_append(self):
        root: dict = {}
        _set_dotted_key(root, "labels[]", "value1")
        _set_dotted_key(root, "labels[]", "value2")
        assert root == {"labels": ["value1", "value2"]}

    def test_invalid_array_key_empty(self):
        root: dict = {}
        with pytest.raises(ValueError, match="Invalid array key"):
            _set_dotted_key(root, "[]", "value")

    def test_unsupported_array_nesting(self):
        root: dict = {}
        with pytest.raises(ValueError, match="Unsupported array nesting"):
            _set_dotted_key(root, "a[].b", "value")

    def test_key_collision(self):
        root: dict = {"a": "string"}
        with pytest.raises(ValueError, match="Key collision"):
            _set_dotted_key(root, "a.b", "value")

    def test_integer_value(self):
        root: dict = {}
        _set_dotted_key(root, "count", 42)
        assert root["count"] == 42

    def test_boolean_true_value(self):
        root: dict = {}
        _set_dotted_key(root, "enabled", True)
        assert root["enabled"] is True

    def test_none_value(self):
        root: dict = {}
        _set_dotted_key(root, "optional", None)
        assert root["optional"] is None

    def test_float_value(self):
        root: dict = {}
        _set_dotted_key(root, "ratio", 3.14)
        assert root["ratio"] == 3.14
