"""
title: Keycloak subject for LiteLLM budget
author: Visionnaire
version: 1.1.0
required_open_webui_version: 0.11.1
description: Sends the verified Keycloak OIDC sub as the LiteLLM end-user identity.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from examples.identity.providers.subjects import linked_subject


class Filter:
    """A global, server-side Open WebUI inlet filter.

    Open WebUI passes its authenticated UserModel (including the locally stored
    OIDC link) to a filter. This filter overwrites the OpenAI-compatible
    request's `user` field, so a browser cannot select a different quota.

    LiteLLM maps this field to its `end_user` accounting identity.  The value
    is the immutable Keycloak subject, not the mutable Open WebUI local user
    ID, username, or email address.
    """

    class Valves(BaseModel):
        oidc_provider: str = Field(
            default='oidc',
            description="Open WebUI's configured OIDC provider key.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def inlet(self, body: dict[str, Any], __user__: dict[str, Any] | None = None):
        subject = linked_subject((__user__ or {}).get('oauth'), self.valves.oidc_provider)

        if subject is None:
            # Fail closed: sending a request without an identity would evade
            # LiteLLM's per-end-user budget.
            raise ValueError(
                'A verified Keycloak OIDC subject is required for model access',
            )

        supplied_user = body.get('user')
        if supplied_user not in (None, '', subject):
            raise ValueError(
                'The request user does not match the authenticated Keycloak subject',
            )

        body['user'] = subject
        return body
