"""Reject multiline single-run edits before execution and recover in the real graph."""
import json
import unittest

from pydantic import ValidationError

from company_agent.runtime.execution import DocumentAgent
from company_agent.services.office.schemas import Edit, EditArtifact
from company_agent.tests.test_agent import Fixture, call
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_multi_agent import RawRoleModel


class RunReplacementSchemaTests(unittest.TestCase):
    def test_both_source_and_artifact_edits_reject_actual_control_characters(self):
        examples=[(Edit,{'root':'project','path':'source.docx','sha256':'0'*64,'location':'word/document.xml#t:0','expected':'原文','name':'revised.docx'}),
                  (EditArtifact,{'artifact_id':'owned','sha256':'0'*64,'location':'word/document.xml#t:0','expected':'原文','name':'summary.docx'})]
        for schema,args in examples:
            for char in ('\r','\n','\t'):
                with self.assertRaisesRegex(ValidationError,'單一文字run'):
                    schema.model_validate({**args,'replacement':'甲'+char+'乙'})
            self.assertEqual(schema.model_validate({**args,'replacement':'甲；乙'}).replacement,'甲；乙')
            self.assertEqual(schema.model_validate({**args,'replacement':r'明確要求的字面\n'}).replacement,r'明確要求的字面\n')


class RunReplacementGraphTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_schema_error_does_not_leave_receipt_and_correct_revision_completes(self):
        self.config.structured_completion=True
        self.config.require_artifact_review=True
        self.config.require_delivery_plan=True
        self.config.orchestration_mode='hybrid'
        self.config.office_execution_mode='specialist'
        self.config.max_steps=20
        args={};ids=[]
        def inspect(messages):
            artifact=json.loads(messages[-1].content);ids.append(artifact['id'])
            return call('inspect_artifact',{'artifact_id':artifact['id']},'inspect')
        def bad(messages):
            inspected=json.loads(messages[-1].content)
            run=next(r for b in inspected['blocks'] for r in b['runs'] if r['text']=='版本日期為2026/10/15')
            args.update(artifact_id=ids[0],sha256=inspected['sha256'],location=run['location'],expected=run['text'],name='summary.docx')
            return call('edit_artifact_text_run',{**args,'replacement':'來源new.xlsx\n版本日期：2026/10/15'},'bad')
        def good(_):
            return call('edit_artifact_text_run',{**args,'replacement':'來源new.xlsx；版本日期：2026/10/15'},'good')
        def done(messages):
            ids.append(json.loads(messages[-1].content)['id'])
            return finish('complete_assignment',ids=[ids[-1]])
        model=RawRoleModel(coordinator=[
            call('plan_delivery',{'files':[{'name':'summary.docx','texts':[{'text':'版本日期：2026/10/15'}]}]},'plan'),
            call('delegate_document',{'task':'建立並精確修正日期標籤','work_key':'doc','outputs':['summary.docx']},'doc'),
            call('delegate_reviewer',{'task':'審核有效新版','work_key':'review','outputs':[]},'review'),
            lambda _:finish(ids=[ids[-1]])],document=[
            call('create_word',{'name':'summary.docx','title':'版本說明','paragraphs':[{'text':'版本日期為2026/10/15','heading':False}]},'create'),
            inspect,bad,good,done],reviewer=[
            lambda _:call('inspect_artifact',{'artifact_id':ids[-1]},'review-read'),
            lambda _:finish('complete_assignment',ids=[ids[-1]])])
        result=await DocumentAgent(self.config,model).run('alice','交付summary.docx，文字「版本日期：2026/10/15」，獨立審核。')
        self.assertEqual(result['status'],'done',result)
        self.assertEqual([f['id'] for f in result['files']],[ids[-1]])
        self.assertNotEqual(ids[0],ids[-1])
        with self.store.db() as db:
            count=db.execute("SELECT count(*) FROM tool_receipts WHERE owner=%s AND job=%s AND name='edit_artifact_text_run'",('alice',result['job_id'])).fetchone()[0]
        self.assertEqual(count,1)
        self.assertTrue(result['tool_feedback'])
