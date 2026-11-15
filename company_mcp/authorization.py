"""Owner/client policy with injected identity and source-account lookups."""
from examples.shared.runtime.io import run_sync

from .storage import ResourceError


class AccessPolicy:
    def __init__(self, configuration, repository, identity, eip_account):
        self.configuration, self.repository = configuration, repository
        self.identity, self.eip_account = identity, eip_account

    async def authorize(self, person, workspace, *, need_eip=False, confirmed_operation=False):
        if person.client not in self.configuration.clients:
            raise ResourceError('client_not_allowed', '此公司工具已停用。')
        snapshot = await run_sync(self.repository.workspace, person, workspace, allow_expired=confirmed_operation)
        if need_eip:
            if not self.configuration.eip_transport_acknowledged:
                raise ResourceError('eip_transport_not_approved', 'EIP 傳輸路徑尚未由管理員確認；NAS 不受影響。')
            account = await self.eip_account(person.owner)
            generation = snapshot.get('eip_generation')
            if not account or not generation or account['generation'] != generation:
                raise ResourceError('binding_changed', 'EIP 尚未保存憑證或綁定已變更，請確認後建立新工作。')
        user = await self.identity(person.owner)
        if (user.get('id') != person.owner or user.get('role') not in {'user', 'admin'}
                or (user.get('oauth') or {}).get('oidc', {}).get('sub') != person.subject):
            raise ResourceError('authentication_required', '本人公司身分已變更，未繼續操作。')
        return snapshot
