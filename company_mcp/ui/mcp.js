'use strict';
const $=id=>document.getElementById(id);
async function api(path,value){
  const response=await fetch(path,{method:value===undefined?'GET':'POST',credentials:'same-origin',
    headers:value===undefined?{}:{'Content-Type':'application/json','X-Company-Action':'1'},
    body:value===undefined?undefined:JSON.stringify(value)});
  if(response.status===401){location.assign('/auth?redirect='+encodeURIComponent(location.pathname+location.search));throw Error('請重新登入公司帳號。');}
  if(!response.ok)throw Error('操作未完成；請檢查帳號綁定、授權與既有處理紀錄。未自動重送。');
  return response.json();
}
async function main(){
  const query=new URLSearchParams(location.search),operation=query.get('operation'),calendar=query.get('calendar');
  if(operation&&/^[a-f0-9]{32}$/.test(operation)&&!calendar){
    const value=await api('/agents/mcp/operations/'+operation);
    $('operation').hidden=false;$('description').textContent=`${value.attachments} 份附件；工具：${value.client_id}；模型設定：${value.model_profile||'公司預設'}。`;
    $('instruction').textContent=value.instruction||'依既有公文流程判讀。';
    $('analyze').disabled=value.status==='submitted';$('status').textContent=value.status==='submitted'?'已送出，請查看既有進度；不會重複建立。':'請核對內容後確認。';
    $('analyze').onclick=async()=>{const button=$('analyze');button.disabled=true;
      try{await api('/agents/mcp/operations/'+operation+'/start',{confirm:true});$('status').textContent='已送出公文分析，請至工作台查看進度。';}
      catch(error){$('status').textContent=error.message;}};
  }else if(calendar&&/^[a-f0-9-]{36}$/.test(calendar)&&!operation){
    const preview=await api('/agents/calendar/preview',{artifact_id:calendar});
    $('calendar').hidden=false;$('calendar-notice').textContent=preview.notice;$('events').textContent=JSON.stringify(preview.events,null,2);
    $('status').textContent='尚未寫入，請核對行程內容。';
    $('import').onclick=async()=>{const button=$('import');button.disabled=true;
      try{const result=await api('/agents/calendar/confirm',{token:preview.token,confirm:true});$('status').textContent=JSON.stringify(result);}
      catch(error){$('status').textContent=error.message;}};
  }else throw Error('請從本人 MCP 操作回條開啟有效連結。');
}
main().catch(error=>{$('status').textContent=error.message;});
