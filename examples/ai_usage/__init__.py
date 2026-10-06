"""Private, per-user LiteLLM usage API for Visionnaire integrations.

The package is intentionally independent of Open WebUI and the Visionnaire
business database.  It verifies a Keycloak access token, uses its immutable
OIDC ``sub`` to query LiteLLM, and returns only the authenticated user's
aggregated daily usage.
"""
from __future__ import annotations
