# Visionnaire AI isolation and quotas

This deployment uses one PostgreSQL cluster but four isolated database
boundaries: `construction_hazard_detection`, `keycloak`, `openwebui`, and
`litellm`.  Each uses a different PostgreSQL login.  Neither AI service is
granted `CONNECT`, schema, or table access to the Visionnaire or Keycloak
databases.

## One-time PostgreSQL provisioning

The current `arthurfr` application role has neither `CREATEDB` nor
`CREATEROLE`, so it cannot safely create the two required boundaries. Run the
following as the PostgreSQL cluster administrator on this host. Generate and
record the passwords in a password manager; supply them to `psql` only for
this invocation.

```bash
read -rsp 'LiteLLM PostgreSQL password: ' LITELLM_DB_PASSWORD; echo
read -rsp 'Open WebUI PostgreSQL password: ' OPENWEBUI_DB_PASSWORD; echo
sudo -u postgres psql -d postgres \
  -v litellm_password="$LITELLM_DB_PASSWORD" \
  -v openwebui_password="$OPENWEBUI_DB_PASSWORD" \
  -f infra/postgres/bootstrap-ai-databases.sql
unset LITELLM_DB_PASSWORD OPENWEBUI_DB_PASSWORD
```

Then create the two ignored files from their examples, replace their
placeholders, and restrict them to their owner:

```bash
cp .env.litellm.example .env.litellm
cp .env.openwebui.example .env.openwebui
chmod 600 .env.litellm .env.openwebui
```

After `.env.litellm` is complete, uncomment the `general_settings` and
`litellm_settings` blocks at the end of `config.yaml`. Replace the current
user service with `infra/systemd/ai-gateway-litellm.service`, run
`systemctl --user daemon-reload`, and restart LiteLLM. Open WebUI is started
by the matching `infra/systemd/openwebui.service` unit and reads only
`.env.openwebui`.

## Keycloak client

The OIDC issuer is:

```text
https://changdar-server.mooo.com/keycloak/realms/visionnaire
```

The confidential Keycloak client `open-webui` has been created in the
`visionnaire` realm. Open WebUI 0.8.1 does not support a URL path prefix, so it
uses its own root vhost at `https://ai.changdar-server.mooo.com/`. Its exact
valid redirect URI is
`https://ai.changdar-server.mooo.com/oauth/oidc/callback`; set the same
public base URL in `WEBUI_URL`. Configure its dedicated client secret in
`.env.openwebui` only. Standard `openid`, `profile`, `email`, and `roles`
client scopes are attached. The available client roles are `openwebui-user`
and `openwebui-admin`. The token claim has this form:

```json
{"resource_access":{"open-webui":{"roles":["openwebui-user"]}}}
```

Assign those roles only to people allowed in the AI application. The
environment configuration maps the OIDC `sub` claim to the Open WebUI account
and deliberately disables account merging by email.
It also disables Open WebUI password authentication and its local login form,
so Keycloak remains the only password and sign-in authority.

The authenticated Open WebUI profile keeps its native change-password form,
but the local route is replaced by the company bridge. The current password is
verified through the confidential `open-webui` client's Direct Access Grant;
the replacement is written only to Keycloak, and existing Keycloak and Open
WebUI sessions are revoked after success. `directAccessGrantsEnabled` is
therefore enabled only on this server-held confidential client. The client
secret and both plaintext password values must remain server-side request data
and must never be logged or stored in the Open WebUI database.

Visionnaire is the authoritative user-lifecycle service. Its UI and the
authenticated Open WebUI account page both provide an entry to the same
management flow. Creating a user first provisions the Keycloak identity, then stores the immutable
Keycloak `sub` in Visionnaire `user_identities` together with the business
permissions. Open WebUI creates its own application record only at the first
successful OIDC login, or immediately after its native administrator form
successfully provisions the same identity through the internal Visionnaire
bridge. Native create, lifecycle, role, password and delete operations verify
the administrator's encrypted Open WebUI OIDC session, resolve its immutable
`sub` to a Visionnaire administrator, and apply Visionnaire tenant/group/site
scope before updating Open WebUI. The native form has no business-group field,
so a missing `group_id` always inherits the administrator's Visionnaire group,
including when the administrator is the platform super administrator. Site
access remains derived from that group. An Open WebUI `pending` user is stored
as suspended and disabled in Keycloak until an administrator activates it.
Do not share either application's database
tables. If an old
Open WebUI record has the same email but a different or missing `sub`, login is
rejected for explicit reconciliation; email alone never proves identity.

Install `infra/nginx/openwebui-ai.conf` as a separate Nginx site, then
run `sudo nginx -t && sudo systemctl reload nginx`. It proxies the root path of
that vhost to Open WebUI while keeping port 8080 loopback-only. If desired,
`infra/nginx/openwebui.conf` may be included in the main site only to redirect
the retired `/openwebui/` path to the AI subdomain. Port 8443 is disabled.

Visionnaire remains the source of truth for business roles, groups, sites and
features. Keycloak carries the login identity and the claims provisioned for
the `open-webui` client. Its `visionnaire-groups` mapper emits full Keycloak
paths in the `groups` claim, and `.env.openwebui` enables group creation and
membership sync. Open WebUI creates a local mirror for each path solely to
hold its own chat/knowledge-base ACLs; it does not replace Visionnaire scope or
function permissions.

There are currently no Keycloak Groups in the `visionnaire` realm. Existing
Visionnaire business groups therefore need provisioning into Keycloak before
there is any membership to synchronize. Manage business membership in
Visionnaire and propagate it to Keycloak; Open WebUI updates normal users'
mirrored membership at every OIDC login. Avoid manually editing those mirrored
group memberships in Open WebUI.

Open WebUI 0.8.1 deliberately does not synchronize groups for users whose
Open WebUI role is `admin`. Keep `openwebui-admin` limited to the few people
who administer Open WebUI; normal Visionnaire users should have the
`openwebui-user` client role and receive synchronized groups.

## LiteLLM $4 per Keycloak subject per day

The supplied Kimi routes already map to LiteLLM's Azure AI cost catalog. Once
the documented settings block is uncommented, `openwebui-daily-usd-4` applies
to every end-user identity. After the database is running, restart LiteLLM,
then use its master key
**only on the host** to create the
budget and Open WebUI's restricted service virtual key:

```bash
source .env.litellm
curl --fail-with-body http://127.0.0.1:4000/budget/new \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"budget_id":"openwebui-daily-usd-4","max_budget":4,"budget_duration":"1d"}'

curl --fail-with-body http://127.0.0.1:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"key_alias":"openwebui-backend","models":["Kimi-K2.5","Kimi-K2.6"],"max_budget":1000,"budget_duration":"30d"}'
```

Put only the returned virtual key in `OPENAI_API_KEYS` in `.env.openwebui`.
Do not give Open WebUI the master key. The second budget is a service-key
safety ceiling; it is not the per-person quota.

### Required Keycloak-subject Filter

Open WebUI's default OpenAI-compatible client does not forward a `user` field
to LiteLLM. Its optional `X-OpenWebUI-User-Id` header is its own local UUID,
not Keycloak's OIDC `sub`. Import
`openwebui/functions/keycloak_subject_budget_filter.py` as an **active global
Filter** in Open WebUI's Admin Panel after the first `openwebui-admin` login.

The Filter runs inside Open WebUI's backend. It retrieves the authenticated
user's stored `oauth.oidc.sub`, rejects absent or forged identities, and
overwrites the outgoing request with:

```json
{"user":"<verified Keycloak sub>"}
```

The Open WebUI backend then uses only the `openwebui-backend` LiteLLM virtual
key. LiteLLM applies `openwebui-daily-usd-4` independently to every `user`
value and resets at midnight in `Asia/Taipei`; it does not read either
Keycloak or Visionnaire PostgreSQL.
