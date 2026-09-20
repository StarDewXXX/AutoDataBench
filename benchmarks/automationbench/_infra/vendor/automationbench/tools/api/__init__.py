# Copyright 2026 Zapier, Inc.
# SPDX-License-Identifier: MIT

"""API tools: generic REST-style interface to world state."""

from automationbench.tools.api.encode import base64_encode
from automationbench.tools.api.fetch import api_fetch
from automationbench.tools.api.search import api_search

API_TOOLS = [api_search, api_fetch, base64_encode]

__all__ = ["api_search", "api_fetch", "base64_encode", "API_TOOLS"]
