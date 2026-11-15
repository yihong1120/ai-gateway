"""Check the real WebUI path using its verified operator and a short-lived token."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path

from examples.shared.config import read_service_snapshot
import httpx


async def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-config',type=Path,help='TOML file or existing WebUI JSON snapshot; defaults to central config.')
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--llm-config',type=Path,help='Also verify end-user spend using this TOML file or LiteLLM JSON snapshot.')
    args=parser.parse_args(argv)
    values=read_service_snapshot('webui', args.web_config)
    os.environ.update({k:str(v) for k,v in values.items() if v is not None})
    os.environ['PYTHON_DOTENV_DISABLED']='1'
    os.environ['DEBUG']='false'
    os.environ['ENABLE_DB_MIGRATIONS']='false'
    # Match the deployed WebUI launcher before importing its path settings.
    os.environ.setdefault('FROM_INIT_PY','true')
    from open_webui.models.users import Users
    from open_webui.utils.auth import create_token
    from open_webui.utils.mfa import get_auth

    user=await Users.get_first_user()
    assert user and user.oauth.get('oidc',{}).get('sub'), 'Existing verified operator required'
    auth=await get_auth(user.id)
    token=create_token({'id':user.id,'typ':'automation','session_stamp':auth.session_stamp},timedelta(minutes=3))
    headers={'Authorization':'Bearer '+token}
    async with httpx.AsyncClient(base_url=args.base_url,timeout=120,trust_env=False) as client:
        response=await client.get('/health')
        assert response.status_code==200
        response=await client.get('/api/models',headers=headers)
        assert response.status_code==200
        assert 'gemini-3.5-flash-lite' in {x['id'] for x in response.json()['data']}
        content,done='',False
        call_id,completion_id=None,None
        async with client.stream('POST','/api/chat/completions',headers=headers,json={
            'model':'gemini-3.5-flash-lite','messages':[{'role':'user','content':'Reply with exactly OK.'}],
            'stream':True,'max_tokens':128,
        }) as response:
            assert response.status_code==200,f'WebUI chat returned HTTP {response.status_code}'
            call_id=response.headers.get('x-litellm-call-id')
            async for line in response.aiter_lines():
                if not line.startswith('data:'):continue
                value=line[5:].strip()
                if value=='[DONE]':done=True;break
                chunk=json.loads(value)
                assert 'error' not in chunk
                completion_id=chunk.get('id') or completion_id
                for choice in chunk.get('choices',[]):
                    content+=choice.get('delta',{}).get('content') or ''
        assert done and content.strip()=='OK','WebUI stream did not complete correctly'
    result={'webui_health':True,'authenticated_models':True,'verified_operator_stream':True,
            'session_stamp_checked':True,'credentials_logged':False}
    if args.llm_config:
        import psycopg
        llm=read_service_snapshot('litellm', args.llm_config)
        subject=user.oauth['oidc']['sub']
        request_ids=[value for value in [completion_id,call_id] if value]
        assert request_ids, 'The WebUI response did not expose a request ID for accounting verification'
        rows=[]
        for attempt in range(45):
            with psycopg.connect(llm['LITELLM_DATABASE_URL']) as db:
                # Spend logs prefer the provider completion ID. Match IDs without storing prompts.
                rows=db.execute('SELECT end_user,spend FROM "LiteLLM_SpendLogs" WHERE request_id=ANY(%s) AND status=%s',
                                (request_ids,'success')).fetchall()
            if rows:break
            await asyncio.sleep(2)
        assert rows and all(identity==subject and spend>0 for identity,spend in rows), 'WebUI request was not billed to its verified end user'
        result.update(verified_end_user_spend=True,matching_spend_rows=len(rows),spend_usd=sum(float(x[1]) for x in rows))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    asyncio.run(main())
