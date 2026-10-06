'use strict';
const $=id=>document.getElementById(id);
const pending={eip:false,nas:false};
const states={eip:null,nas:null};
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
    $('nas-test').disabled=pending.nas||!s?.bound||!s?.usable;
    $('nas-unlink').disabled=pending.nas||!s?.bound;
  }else{
    $('eip-test').disabled=pending.eip||!s?.session?.saved_credentials||Boolean(s?.session?.retry_after*1000>Date.now());
    $('eip-pause').disabled=pending.eip||!s?.automatic?.enabled;
    $('eip-unlink').disabled=pending.eip||!(s?.linked||s?.session?.saved_credentials);
  }
}
function render(provider,s){
  states[provider]=s;
  if(provider==='nas'){
    $('nas-status').textContent=!s.configured?'NAS 端點尚未設定。':!s.bound?'未綁定 NAS。':s.usable?'NAS 已綁定（唯讀）。':'NAS 需要重新登入：請更新本人綁定。';
    if(!s.bound||!s.usable)$('nas-shares').replaceChildren();
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
  finally{pending[provider]=false;buttons(provider);}
  await refresh(provider);
}
function shares(value){$('nas-shares').replaceChildren(...(value.shares||[]).map(s=>{const p=document.createElement('p');p.textContent=s.name==='home'?'home（本人個人空間）':s.name;return p;}));}
for(const provider of ['eip','nas']){
  $(provider+'-refresh').onclick=()=>action(provider,async()=> '已更新連線狀態。');
  $(provider+'-form').onsubmit=e=>{
    e.preventDefault();if(pending[provider])return;
    const payload=provider==='eip'?{account:$('eip-account').value,password:$('eip-password').value,accept_http:$('eip-http').checked,enable_auto:$('eip-auto').checked}:
      {account:$('nas-account').value,password:$('nas-password').value,consent:$('nas-consent').checked};
    $(provider+'-password').value='';
    return action(provider,async()=>{
      try{
        const result=await api(provider,provider==='eip'?'/api/v1/eip/link':'/agents/nas/bind',payload);
        if(provider==='nas'){shares(result);$('nas-consent').checked=false;return 'NAS 已綁定，請另開公司助理工作使用。';}
        const message=payload.enable_auto?'EIP 已綁定並啟用每小時公文處理與私人行事曆匯入。':'已建立 EIP 短期 Session，未保存密碼；自動處理已停用。';
        $('eip-auto').checked=false;return message;
      }finally{payload.password='';}
    });
  };
}
$('eip-test').onclick=()=>action('eip',async()=>{await api('eip','/api/v1/eip/session/restore',{});return 'EIP 已重新連線；排程與抓取起點未變更。';});
$('eip-pause').onclick=()=>action('eip',async()=>{await api('eip','/api/v1/eip/automatic/pause',{});return 'EIP 排程已暫停，憑證保留；已送出的請求可能仍會完成。';});
$('eip-unlink').onclick=()=>action('eip',async()=>{await api('eip','/api/v1/eip/unlink',{});return 'EIP 綁定已解除、排程已停止；既有成果與去重紀錄保留，NAS 不受影響。';});
$('nas-test').onclick=()=>action('nas',async()=>{shares(await api('nas','/agents/nas/test',{}));return 'NAS 連線正常，已列出可用資料夾。';});
$('nas-unlink').onclick=()=>action('nas',async()=>{await api('nas','/agents/nas/unlink',{});$('nas-shares').replaceChildren();return 'NAS 憑證已移除，既有私人成果保留，EIP 不受影響。';});
Promise.allSettled(['eip','nas'].map(refresh));
identity();
