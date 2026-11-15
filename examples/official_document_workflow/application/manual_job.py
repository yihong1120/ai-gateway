"""Background work started by authenticated manual API requests."""
from __future__ import annotations

import time

from . import integration
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.eip.sessions import manual_session
from examples.official_document_workflow.services.output_delivery import output_part


class ManualJob:
    def __init__(self, owner, generation, selected, instruction, job, state, repository):
        self.owner = owner
        self.generation = generation
        self.selected = selected
        self.instruction = instruction
        self.job = job
        self.state, self.repository = state, repository

    async def emit(self, event):
        if event.get('type') == 'status':
            self.job['progress'] = event.get('data', {}).get('description', '處理中')

    async def run(self):
        async with self.state.processing.slot(self.owner):
            self.job['status'] = 'running'
            ids = []
            groups = {}
            self.job['source_ids'] = ids
            async with manual_session(self.owner, self.state, self.repository, factory=Netask) as current:
                if current.link.generation != self.generation:
                    raise ValueError('EIP 帳號已變更，工作未執行。')
                for item in self.selected:
                    self.job['progress'] = '正在下載 EIP 附件；Word 將先進行隔離轉換'
                    document = item['document']
                    attachments = await current.read('attachments', document['id'], document['folder'])
                    if not any(value == {'name': item['name'], 'url': item['url']} for value in attachments):
                        raise ValueError('附件已變更或不再可存取。')
                    payload = await current.read('download', item['url'], item['name'])
                    with output_part(item['url']):
                        file = await integration.source_file(self.owner, payload, item['name'])
                    ids.append(file.id)
                    groups.setdefault(document['id'], []).append(file.id)
            result = await integration.process(
                self.owner,
                ids,
                self.instruction,
                self.emit,
                groups=list(groups.values()),
            )
            self.job.update(
                result=result['message'],
                files=result['files'],
                errors=result['errors'],
                summary=result.get('summary', ''),
                notices=result.get('notices', []),
                warnings=result.get('warnings', []),
                orchestration=result.get('orchestration', {}),
                status=result['status'],
            )
            self.job['progress'] = {
                'done': '已完成；點此查看結果與下載',
                'attention': '文件完成，提醒待確認；點此下載檔案與查看原因',
                'partial': '部分完成；點此下載成功檔案並查看未完成原因',
                'error': '未完成；點此查看原因',
            }[result['status']]

    def finished(self, task):
        self.state.tasks.discard(task)
        self.job['expires'] = time.time() + 86400
        error = None if task.cancelled() else task.exception()
        if task.cancelled() or error is not None:
            self.job.update(
                status='error',
                progress='公文處理中止，請檢查服務狀態。',
            )
