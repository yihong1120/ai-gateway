# Identity core

Installation, testing and packaging follow the root [contributor guide](../../CONTRIBUTING.md).

[繁體中文](README-zh-tw.md)

The canonical account, authorization and token/session implementation lives here.
The management application mounts `routers/`; BFF remains the browser adapter.
Old `examples.auth` and management-owned authentication module paths are removed.
HTTP paths, schemas, cookie/claim contracts and database tables stay unchanged.

- `accounts/`: account management, passwords, recovery, verification and consent.
- `sessions/`: login, JWT issuance, atomic refresh/revocation and session storage.
- `providers/`: OIDC/Keycloak and social identity verification/exchange.
- `authorization/`: groups, features and site entitlements.
- `adapters/`: explicit host boundaries; `webui.verified_user` lazily calls the host resolver.
- `runtime.py`, `lifespan.py`: application-owned configuration and identity resources.
- `models/`: the existing single SQLAlchemy metadata; no duplicate registry or migration.

`providers/subjects.py` exposes the pure `linked_subject` operation for trusted
provider links. It preserves the linked subject and does not verify JWTs or
establish an authentication authority. OIDC/Keycloak verification remains in
this package; the WebUI resolver lives in `adapters/webui.py`, while the host
keeps its Filter/Valves and request-body identity checks. Usage services receive the verified subject rather than a
WebUI request. This boundary does not change login, session or HTTP contracts.

Core identity modules do not import adapters or host code. Only `adapters/` may
depend on a host, and importing the WebUI adapter does not load Open WebUI.
Management and BFF implementations remain forbidden throughout this package.
Playback and media cleanup belong to the management host. Device credentials remain in company AI access.
The Keycloak-only sibling backend retains its own identity authority; synchronize
applicable behavior and public contracts rather than copying the local issuer.

See [cross-repository authentication contracts](../../docs/authentication-cross-repo.md).
