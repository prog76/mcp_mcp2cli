#!/usr/bin/env python3
"""Tests for mcp2cli.client.resolve_tool_id — pure, no server needed."""

import pytest

from mcp2cli.client import resolve_tool_id


class TestResolveToolIdExact:
    def test_exact_match_returns_unchanged(self):
        names = ["k8s_pods_get", "grafana_query"]
        assert resolve_tool_id("k8s_pods_get", names) == "k8s_pods_get"

    def test_exact_match_prefixed(self):
        names = ["grafana-infra_query_prometheus", "k8s_pods_get"]
        assert resolve_tool_id("grafana-infra_query_prometheus", names) == "grafana-infra_query_prometheus"


class TestResolveToolIdSuffix:
    def test_single_suffix_match_resolves(self):
        """Unprefixed 'pods_get' should match 'k8s_pods_get'."""
        names = ["k8s_pods_get", "grafana_query"]
        assert resolve_tool_id("pods_get", names) == "k8s_pods_get"

    def test_single_suffix_match_grafana(self):
        names = ["k8s_pods_get", "grafana-infra_query_prometheus"]
        assert resolve_tool_id("query_prometheus", names) == "grafana-infra_query_prometheus"


class TestResolveToolIdAmbiguous:
    def test_ambiguous_raises(self):
        """Two tools ending in '_create' → ambiguous."""
        names = ["grafana_create", "k8s_create"]
        with pytest.raises(ValueError, match="Ambiguous"):
            resolve_tool_id("create", names)

    def test_no_match_raises(self):
        names = ["grafana_query_prometheus", "k8s_pods_get"]
        with pytest.raises(ValueError, match="Tool not found"):
            resolve_tool_id("nonexistent_tool", names)


class TestEdgeCases:
    def test_empty_names_list_raises(self):
        with pytest.raises(ValueError, match="Tool not found"):
            resolve_tool_id("anything", [])

    def test_underscore_in_name_handled(self):
        """A tool id with multiple underscores should still resolve via suffix."""
        names = ["vscode_terminal_exec"]
        assert resolve_tool_id("terminal_exec", names) == "vscode_terminal_exec"
