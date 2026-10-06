'use strict';
const $=id=>document.getElementById(id);
let selected=new URLSearchParams(location.search).get('job'),busy=false,mutation=false;
let requestId=crypto.randomUUID();
let calendarJob=null;
async function previewCalendar(artifact){
  const value=await api('/agents/calendar/preview',{artifact_id:artifact});
  calendarJob=selected;
  let box=$('calendar-preview');
  if(!box){box=el('div');box.id='calendar-preview';$('detail').append(box);}
  box.replaceChildren(el('h3','確認私人行事曆：'+value.account),el('p',value.notice),el('pre',JSON.stringify(value.events,null,2)));
  const approve=el('button','確認以上內容，新增到我的 EIP 行事曆');
  approve.onclick=()=>action(async()=>{
    approve.disabled=true;
    const result=await api('/agents/calendar/confirm',{token:value.token,confirm:true});box.replaceChildren(el('p',`行事曆新增 ${result.created} 筆；略過既有 ${result.skipped} 筆。`));
  });
  const cancel=el('button','取消');cancel.onclick=()=>{box.replaceChildren();calendarJob=null;};box.append(approve,cancel);
}
const labels={queued:'等待執行',running:'執行中',done:'完成',partial:'部分完成',error:'執行失敗',needs_input:'需要補充資料',blocked:'受阻',interrupted:'已中斷',awaiting_approval:'待本人確認',cancelled:'已取消'};
function el(tag,text){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;return x;}
async function api(path,value){
  const response=await fetch(path,{method:value===undefined?'GET':'POST',credentials:'same-origin',headers:value===undefined?{}:{'Content-Type':'application/json','X-Company-Action':'1'},body:value===undefined?undefined:JSON.stringify(value)});
  if(!response.ok)throw response;return response.json();
}
async function action(fn){if(mutation)return;mutation=true;try{await fn();$('notice').textContent='操作已接收。';}finally{mutation=false;}}
function render(job){
  if(calendarJob && calendarJob!==job.id){$('calendar-preview')?.replaceChildren();calendarJob=null;}
  $('detail').hidden=false;$('status').textContent=(labels[job.status]||job.status)+' — '+job.id;
  $('progress').textContent=job.progress;$('answer').textContent=job.answer||'目前尚無文字結果。';
  $('notices').replaceChildren(...(job.notices||[]).map(x=>el('p',x)));
  if(job.tool_feedback?.length){
    const feedback=el('details');
    feedback.append(el('summary','工具回饋紀錄（不代表最終工作失敗）'));
    feedback.append(...job.tool_feedback.map(x=>el('p',`${x.tool}：${x.message}`)));
    $('notices').append(feedback);
  }
  $('web-sources').replaceChildren();for(const source of job.web_sources||[]){
    if(!URL.canParse(source.url))continue;const url=new URL(source.url);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)continue;
      const a=el('a',`${source.title||url.hostname}（${source.kind==='page_extract'?'網頁擷取':'搜尋摘要'}；${source.retrieved_at||''}）`);
      a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';$('web-sources').append(a);
    }
  $('files').replaceChildren();for(const file of job.files||[]){const a=el('a',file.name);a.href=job.kind==='document'?'/api/v1/files/'+encodeURIComponent(file.id)+'/content?attachment=true':'/agents/artifacts/'+encodeURIComponent(file.id);$('files').append(a);
    if(job.kind==='document' && file.kind==='calendar_csv'){const preview=el('a','預覽行事曆匯入');preview.href='/eip?csv='+encodeURIComponent(file.id);$('files').append(preview);}
    if(file.calendar_preview){const b=el('button','預覽行事曆匯入');b.onclick=()=>action(()=>previewCalendar(file.id));$('files').append(b);}}
  if(!job.files?.length)$('files').append(el('p','尚無產出；文字答覆或缺資料的工作不一定產生檔案。'));
  $('delegations').replaceChildren(...(job.delegations||[]).map(x=>el('p',`${x.agent}：${labels[x.status]||x.status}`)));
  $('proposals').replaceChildren();
  if(job.status==='awaiting_approval'&&!job.requires_restart)for(const p of job.proposals||[]){
    const box=el('div');box.className='proposal';box.append(el('h3','確認新增檔案'),el('pre',JSON.stringify(p,null,2)));
    for(const decision of ['approve','reject']){const b=el('button',decision==='approve'?'確認目的地與檔案，核准新增':'拒絕存回');b.onclick=()=>action(async()=>{await api(`/agents/jobs/${selected}/resume`,{decision:{proposal_id:p.proposal_id,decision}});await refresh();});box.append(b);}$('proposals').append(box);
  }
  $('budget').textContent=job.budget?.limit?`模型呼叫：已用 ${job.budget.used} / ${job.budget.limit} 次；扣本人既有每日額度。`:'';
  $('cancel').hidden=!['queued','running'].includes(job.status);
  $('resume').hidden=!(['error','interrupted'].includes(job.status)&&job.resumable===true&&!job.requires_restart&&(!job.budget?.limit||job.budget.used<job.budget.limit));
  $('followup').hidden=job.kind==='document'||['queued','running','awaiting_approval'].includes(job.status);
}
async function refresh(){
  if(busy)return;busy=true;
  try{const data=await api('/agents/jobs');$('jobs').replaceChildren();for(const job of data.jobs){const b=el('button',`${new Date(job.created*1000).toLocaleString('zh-TW')}　${labels[job.status]||job.status}　${job.id}`);b.onclick=()=>{selected=job.id;history.replaceState(null,'','/agents?job='+selected);requestId=crypto.randomUUID();render(job);};$('jobs').append(b);}
    if(!data.jobs.length)$('jobs').append(el('p','尚無工作。請回 Open WebUI 選擇公司工作助理或公文 PDF。'));
    if(selected)render(data.jobs.find(job=>job.id===selected)||await api('/agents/jobs/'+encodeURIComponent(selected)));
  }finally{busy=false;}
}
$('refresh').onclick=refresh;
$('cancel').onclick=()=>action(async()=>{await api(`/agents/jobs/${encodeURIComponent(selected)}/cancel`,{});await refresh();});
$('resume').onclick=()=>action(async()=>{await api(`/agents/jobs/${encodeURIComponent(selected)}/resume`,{});await refresh();});
$('instruction').oninput=()=>{requestId=crypto.randomUUID();};
$('followup').onsubmit=e=>{e.preventDefault();action(async()=>{const value=await api(`/agents/jobs/${selected}/followup`,{instruction:$('instruction').value,request_id:requestId});selected=value.id;history.replaceState(null,'','/agents?job='+selected);$('instruction').value='';requestId=crypto.randomUUID();await refresh();});};
refresh();setInterval(refresh,5000);
