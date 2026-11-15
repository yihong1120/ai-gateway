'use strict';
const $=id=>document.getElementById(id);
const pending={eip:false,nas:false};
const states={eip:null,nas:null};
async function mcpStatus(){
  try{
    const value=await api('mcp','/agents/mcp/status');
    $('mcp-status').textContent=value.enabled?'本人全部有效綁定預設可用；以下可撤回特定工具的存取。':'公司 MCP 尚未啟用。';
    $('mcp-clients').replaceChildren(...value.clients.map(client=>{
      const row=document.createElement('p'),label=document.createElement('span'),button=document.createElement('button');
      label.textContent=client.client_id+'：'+(client.enabled?'可使用本人綁定':'已撤回');
      button.type='button';button.textContent=client.enabled?'撤回存取':'恢復存取';
      button.onclick=async()=>{button.disabled=true;try{await api('mcp','/agents/mcp/access',{client_id:client.client_id,enabled:!client.enabled});await mcpStatus();}catch(error){$('mcp-status').textContent='存取設定未完成，請重新整理確認。';button.disabled=false;}};
      row.append(label,button);return row;
    }));
  }catch(error){$('mcp-status').textContent='目前無法讀取公司工具狀態。';}
}
mcpStatus();
function nasBinding(){return (states.nas?.bindings||[]).find(b=>b.binding_id===$('nas-binding').value);}
function nasOptions(id,bindings,empty){
  const select=$(id),selected=select.value;
  const options=bindings.map(b=>{const option=document.createElement('option');option.value=b.binding_id;option.textContent=`${b.label}（${b.account}）${b.usable?'':' — 需更新'}`;return option;});
  if(empty){const option=document.createElement('option');option.value='';option.textContent='新增 NAS 帳號';options.unshift(option);}
  select.replaceChildren(...options);
  select.value=bindings.some(b=>b.binding_id===selected)?selected:empty?'':bindings[0]?.binding_id||'';
}
async function api(provider,path,value){
  const header=provider==='eip'?'X-EIP-Action':'X-Company-Action';
  const response=await fetch(path,{method:value===undefined?'GET':'POST',credentials:'same-origin',
    headers:value===undefined?{}:{'Content-Type':'application/json',[header]:'1'},body:value===undefined?undefined:JSON.stringify(value)});
  if(!response.ok)throw response;
  return response.json();
}
async function identity(){
    const value=await api('identity','/connections/identity');
    $('identity-account').href=value.account_url;
    $('identity-admin').hidden=!value.admin;
    if(value.admin){
      $('identity-visionnaire-users').href=value.visionnaire_users_url;
      $('identity-openwebui-users').href=value.openwebui_users_url;
    }
    $('identity-status').textContent=value.admin?'你可以從此頁進入使用者、公司權限及 Open WebUI 應用資料管理。':'你可以管理自己的密碼與安全設定。';
}

function buttons(provider){
  const s=states[provider];
  for(const id of ['save','refresh'])$(provider+'-'+id).disabled=pending[provider];
  if(provider==='nas'){
    const selected=nasBinding();
    $('nas-test').disabled=pending.nas||!selected?.usable;
    $('nas-unlink').disabled=pending.nas||!selected;
    $('nas-binding').disabled=pending.nas||!s?.bindings?.length;
    $('nas-edit-binding').disabled=pending.nas;
  }else{
    $('eip-test').disabled=pending.eip||!s?.session?.saved_credentials||Boolean(s?.session?.retry_after*1000>Date.now());
    $('eip-pause').disabled=pending.eip||!s?.automatic?.enabled;
    $('eip-unlink').disabled=pending.eip||!(s?.linked||s?.session?.saved_credentials);
  }
}
function render(provider,s){
  const previousBinding=provider==='nas'?nasBinding():null;
  states[provider]=s;
  if(provider==='nas'){
    const bindings=s.bindings||[];
    $('nas-status').textContent=!s.configured?'NAS 端點尚未設定。':!s.bound?'未綁定 NAS。':`NAS 已綁定 ${bindings.length} 組帳號（唯讀）；新工作可使用所有可用連線。`;
    nasOptions('nas-binding',bindings,false);nasOptions('nas-edit-binding',bindings,true);
    if(!nasBinding()?.usable||previousBinding?.revision!==nasBinding()?.revision)$('nas-shares').replaceChildren();
  }else{
    const saved=Boolean(s.session?.saved_credentials),a=s.automatic;
    $('eip-status').textContent=s.session?.error?'EIP 連線需要處理。':s.linked?'EIP 已連線。':saved?'EIP 已綁定；使用時自動重新登入。':'未綁定 EIP。';
    $('eip-session').textContent=s.session?.error||(saved?'已保存加密憑證，不需重填密碼。':s.linked?'目前是短期 Session，沒有保存密碼。':'請使用下方表單綁定本人帳號。');
    $('eip-schedule').textContent=a?`每小時自動處理：${a.enabled?'已啟用':'已暫停'}；待處理文件不限日期、已完成不重做；其他文件匣固定抓取起點：${new Date(a.scan_start*1000).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})}${a.enabled?'；下次排程：'+new Date(a.next_run*1000).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'}):''}`:'尚未啟用自動處理。綁定與排程／行事曆匯入同意不會自動勾選。';
  }
  buttons(provider);
}
async function refresh(provider){
  render(provider,await api(provider,provider==='eip'?'/api/v1/eip/status':'/agents/nas'));
}
async function action(provider,fn){
  if(pending[provider])return;
  pending[provider]=true;buttons(provider);$(provider+'-notice').textContent='處理中…';
  try{const message=await fn();$(provider+'-notice').textContent=message;}
  catch(error){
    $(provider+'-notice').textContent=error.status===401?'登入狀態已失效，請重新登入。':
      provider==='nas'?'NAS 操作未完成。新增或更新帳號每分鐘限一次；請稍候再試，並確認帳密與連線。':'操作未完成，請確認連線後重試。';
    throw error;
  }
  finally{pending[provider]=false;buttons(provider);}
  await refresh(provider);
}
function shares(value){$('nas-shares').replaceChildren(...(value.shares||[]).map(s=>{const p=document.createElement('p');p.textContent=s.name==='home'?'home（本人個人空間）':s.name;return p;}));}
for(const provider of ['eip','nas']){
  $(provider+'-refresh').onclick=()=>action(provider,async()=> '已更新連線狀態。');
  $(provider+'-form').onsubmit=e=>{
    e.preventDefault();if(pending[provider])return;
    if(provider==='eip'&&$('eip-auto').checked&&!$('eip-persist').checked){$('eip-notice').textContent='啟用自動處理前，請先同意保存 EIP 憑證。';return;}
    const payload=provider==='eip'?{account:$('eip-account').value,password:$('eip-password').value,accept_http:$('eip-http').checked,enable_auto:$('eip-auto').checked,save_credentials:$('eip-persist').checked}:
      {account:$('nas-account').value,password:$('nas-password').value,consent:$('nas-consent').checked};
    if(provider==='nas'){
      if($('nas-edit-binding').value)payload.binding_id=$('nas-edit-binding').value;
      if($('nas-label').value.trim())payload.label=$('nas-label').value.trim();
    }
    $(provider+'-password').value='';
    return action(provider,async()=>{
      try{
        const result=await api(provider,provider==='eip'?'/api/v1/eip/link':'/agents/nas/bind',payload);
        if(provider==='nas'){
          render('nas',{...states.nas,...result});$('nas-binding').value=result.binding_id;
          shares(result);$('nas-consent').checked=false;
          return 'NAS 已綁定；新工作可使用所有已綁定的帳號，既有工作維持原來源範圍。';
        }
        const message=payload.enable_auto?'EIP 已綁定並啟用每小時公文處理與私人行事曆匯入。':payload.save_credentials?'EIP 憑證已加密保存，可供公司工具使用；自動處理未啟用。':'已建立 EIP 短期 Session，未保存密碼；自動處理已停用。';
        $('eip-auto').checked=false;$('eip-persist').checked=false;return message;
      }finally{payload.password='';}
    });
  };
}
$('eip-test').onclick=()=>action('eip',async()=>{await api('eip','/api/v1/eip/session/restore',{});return 'EIP 已重新連線；排程與抓取起點未變更。';});
$('eip-pause').onclick=()=>action('eip',async()=>{await api('eip','/api/v1/eip/automatic/pause',{});return 'EIP 排程已暫停，憑證保留；已送出的請求可能仍會完成。';});
$('eip-unlink').onclick=()=>action('eip',async()=>{await api('eip','/api/v1/eip/unlink',{});return 'EIP 綁定已解除、排程已停止；既有成果與去重紀錄保留，NAS 不受影響。';});
$('nas-binding').onchange=()=>{$('nas-shares').replaceChildren();buttons('nas');};
$('nas-edit-binding').onchange=()=>{
  const selected=(states.nas?.bindings||[]).find(b=>b.binding_id===$('nas-edit-binding').value);
  $('nas-account').value=selected?.account||'';$('nas-label').value=selected?.label||'';
  $('nas-password').value='';$('nas-consent').checked=false;
};
$('nas-test').onclick=()=>action('nas',async()=>{shares(await api('nas','/agents/nas/test',{binding_id:$('nas-binding').value}));return '選取的 NAS 連線正常，已列出可用資料夾。';});
$('nas-unlink').onclick=()=>action('nas',async()=>{await api('nas','/agents/nas/unlink',{binding_id:$('nas-binding').value});$('nas-shares').replaceChildren();return '選取的 NAS 憑證已移除，其他連線、既有私人成果及 EIP 保留。';});
Promise.allSettled(['eip','nas'].map(refresh));
identity();
