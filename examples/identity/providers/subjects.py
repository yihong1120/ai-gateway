"""Read stable subjects from trusted provider links; never authenticate claims."""
from __future__ import annotations

from collections.abc import Mapping


def linked_subject(identities, provider: str = 'oidc') -> str | None:
    """Return the original nonempty subject without normalising caller policy."""
    claims = identities.get(provider) if isinstance(identities, Mapping) else None
    subject = claims.get('sub') if isinstance(claims, Mapping) else None
    return subject if isinstance(subject, str) and subject.strip() else None
