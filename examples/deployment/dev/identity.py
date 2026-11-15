"""Provision only the dedicated dev OIDC client and its operator roles."""
import json
from pathlib import Path
import httpx

BASE = 'https://changdar-server.mooo.com/keycloak'
CLIENT = 'open-webui-dev'
ORIGIN = 'https://ai-dev.changdar-server.mooo.com'
MARKER = 'ai-gateway-local-dev-v1'


def provision():
    values = json.loads((Path.home() / 'service-migration-phase5/source-keycloak-env.json').read_text())
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post(BASE + '/realms/master/protocol/openid-connect/token', data={
            'client_id': 'admin-cli', 'grant_type': 'password',
            'username': values['KC_BOOTSTRAP_ADMIN_USERNAME'],
            'password': values['KC_BOOTSTRAP_ADMIN_PASSWORD'],
        })
        response.raise_for_status()
        client.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
        prefix = BASE + '/admin/realms/visionnaire'

        def api(method, path, **kwargs):
            r = client.request(method, prefix + path, **kwargs)
            r.raise_for_status()
            return r.json() if r.content else None

        existing = api('GET', '/clients', params={'clientId': CLIENT})
        desired = {
            'clientId': CLIENT, 'name': 'Open WebUI DEV (A)', 'enabled': True,
            'protocol': 'openid-connect', 'publicClient': False,
            'standardFlowEnabled': True, 'directAccessGrantsEnabled': False,
            'serviceAccountsEnabled': False, 'implicitFlowEnabled': False,
            'fullScopeAllowed': False,
            'redirectUris': [ORIGIN + '/oauth/oidc/callback'], 'webOrigins': [ORIGIN],
            'attributes': {'ai-gateway-managed': MARKER, 'post.logout.redirect.uris': ORIGIN},
        }
        if existing:
            if len(existing) != 1 or existing[0].get('attributes', {}).get('ai-gateway-managed') != MARKER:
                raise RuntimeError('Existing dev client is not owned by this deployment.')
            cid = existing[0]['id']
            api('PUT', '/clients/' + cid, json={**existing[0], **desired})
        else:
            api('POST', '/clients', json=desired)
            cid = api('GET', '/clients', params={'clientId': CLIENT})[0]['id']
        path = '/clients/' + cid
        for name in ('dev-user', 'dev-admin'):
            roles = api('GET', path + '/roles')
            if not any(r['name'] == name for r in roles):
                api('POST', path + '/roles', json={'name': name})
        roles = [r for r in api('GET', path + '/roles') if r['name'] in ('dev-user', 'dev-admin')]
        users = api('GET', '/users', params={'username': 'ChangDar', 'exact': 'true'})
        if len(users) != 1 or not users[0]['enabled']:
            raise RuntimeError('Expected one enabled ChangDar operator.')
        user = users[0]
        api('POST', '/users/' + user['id'] + '/role-mappings/clients/' + cid, json=roles)
        api('POST', path + '/scope-mappings/clients/' + cid, json=roles)
        mapper = {
            'name': 'dev-client-roles', 'protocol': 'openid-connect',
            'protocolMapper': 'oidc-usermodel-client-role-mapper',
            'config': {
                'usermodel.clientRoleMapping.clientId': CLIENT,
                'claim.name': 'dev_roles', 'jsonType.label': 'String', 'multivalued': 'true',
                'access.token.claim': 'true', 'id.token.claim': 'true', 'userinfo.token.claim': 'true',
            },
        }
        mappers = api('GET', path + '/protocol-mappers/models')
        old = next((m for m in mappers if m['name'] == mapper['name']), None)
        if old:
            api('PUT', path + '/protocol-mappers/models/' + old['id'], json={**mapper, 'id': old['id']})
        else:
            api('POST', path + '/protocol-mappers/models', json=mapper)
        secret = api('GET', path + '/client-secret')['value']
        return secret, {'id': user['id'], 'email': user.get('email') or f"oidc@{user['id']}.local",
                        'name': user.get('firstName') or user['username']}
