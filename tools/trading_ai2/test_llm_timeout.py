"""Focused unit tests for the swarm OpenAI client's request deadline."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import openai

from tools.trading_ai2.swarm import llm_factory as lf


class LLMRequestTimeoutTests(unittest.TestCase):
    def setUp(self):
        lf.LLMFactory.reset_clients()

    def tearDown(self):
        lf.LLMFactory.reset_clients()

    def test_cached_client_receives_configured_timeout_and_default(self):
        route = lf.LLMRoute(
            model="gpt-4o", provider="openai", api_key="unit-test-key",
            base_url="https://example.invalid/v1")
        values = {"LLM_REQUEST_TIMEOUT_S": "37"}
        constructed = []

        def make_client(**kwargs):
            constructed.append(kwargs)
            return object()

        with patch.object(lf, "env", lambda name: values.get(name, "")), \
                patch.object(openai, "OpenAI", side_effect=make_client):
            lf.LLMFactory._cached_client(route)
            values["LLM_REQUEST_TIMEOUT_S"] = ""
            lf.LLMFactory._cached_client(route)

        self.assertEqual([item["timeout"] for item in constructed],
                         [37.0, 120.0])

    def test_configured_timeout_is_bounded(self):
        route = lf.LLMRoute(
            model="gpt-4o", provider="openai", api_key="unit-test-key",
            base_url="https://example.invalid/v1")
        constructed = []

        with patch.object(lf, "env", lambda name: "9999"), \
                patch.object(openai, "OpenAI",
                             side_effect=lambda **kwargs:
                             constructed.append(kwargs) or object()):
            lf.LLMFactory._cached_client(route)

        self.assertEqual(constructed[0]["timeout"], 300.0)


if __name__ == "__main__":
    unittest.main()
