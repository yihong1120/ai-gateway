'use strict';
const $=id=>document.getElementById(id);
let page=0, previewToken=null, selectedJob=null, polling=false;
let automaticPage=1,automaticPageSize=5,automaticPages=1,automaticTotal=0,automaticLoading=false,statusRequest=0;
function automaticPager(){
  for(const suffix of ['','-bottom']){
    $('automatic-previous'+suffix).disabled=automaticLoading||automaticPage<=1;
    $('automatic-next'+suffix).disabled=automaticLoading||automaticPage>=automaticPages;
    const first=automaticTotal?(automaticPage-1)*automaticPageSize+1:0;
    const last=Math.min(automaticPage*automaticPageSize,automaticTotal);
    $('automatic-page-info'+suffix).textContent=`第 ${automaticPage}／${automaticPages} 頁 · 第 ${first}–${last} 筆，共 ${automaticTotal} 筆`;
  }
  $('automatic-page-size').disabled=automaticLoading;
  $('automatic-page-size').value=String(automaticPageSize);
}
for(const suffix of ['','-bottom']){
  $('automatic-previous'+suffix).onclick=()=>action(()=>status(Math.max(1,automaticPage-1)));
  $('automatic-next'+suffix).onclick=()=>action(()=>status(Math.min(automaticPages,automaticPage+1)));
}
$('automatic-page-size').onchange=()=>action(()=>status(1,Number($('automatic-page-size').value)));
function notice(message){$('notice').textContent=message;}
function loginRedirect(){
  let destination='/eip';
  const csv=new URLSearchParams(location.search).get('csv');
  if(csv&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(csv))destination+='?'+new URLSearchParams({csv});
  location.replace('/auth?'+new URLSearchParams({redirect:destination}));
}
async function api(path,data){
  const headers={};
  // Use the same HttpOnly WebUI session cookie as the server-side page gate.
  // A stale localStorage token must not override a freshly authenticated cookie.
  if(data!==undefined){headers['Content-Type']='application/json';headers['X-EIP-Action']='1';}
  const response=await fetch(`/api/v1/eip/${path}`,{method:data===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers,body:data===undefined?undefined:JSON.stringify(data)});
  const value=await response.json();
  if(!response.ok){if(response.status===401)loginRedirect();throw Error(response.status===401?'登入已失效，正在前往 Open WebUI 登入頁。':typeof value.detail==='string'?value.detail:'操作失敗。');}
  return value;
}
function element(tag,value){const el=document.createElement(tag);if(value!==undefined)el.textContent=value;return el;}
function taipeiTimestamp(value){
  if(value===null||value===undefined||value===''||typeof value==='boolean')return null;
  if(typeof value==='number'&&(!Number.isFinite(value)||value<=0))return null;
  const stamp=new Date(typeof value==='number'?value*1000:value);
  if(!Number.isFinite(stamp.getTime()))return null;
  return stamp.toLocaleString('zh-TW',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
}
function taskTimes(r){
  const box=element('div');box.className='task-times muted';
  const recorded=taipeiTimestamp(r.eip_recorded_at),detected=taipeiTimestamp(r.first_detected_at),processed=taipeiTimestamp(r.last_processed_at);
  const overview=element('div');overview.className='task-time-summary';
  if(recorded)overview.append(element('span',`EIP 文件匣時間：${recorded}`));
  if(processed)overview.append(element('span',`最後處理時間：${processed}`));
  else if(detected)overview.append(element('span',`首次偵測時間：${detected}`));
  if(!overview.children.length)overview.append(element('span','尚無已記錄的時間'));
  box.append(overview);
  return box;
}
async function action(fn){try{await fn();}catch(e){notice(e.message);}}
async function status(targetPage=automaticPage,targetSize=automaticPageSize){
  const request=++statusRequest;
  automaticLoading=true;automaticPager();
  try{
  const value=await api(`status?automatic_page=${targetPage}&automatic_page_size=${targetSize}`);
  if(request!==statusRequest)return;
  const pagination=value.automatic_pagination||{page:1,page_size:5,pages:1,total:(value.automatic_results||[]).length};
  automaticPage=pagination.page;automaticPageSize=pagination.page_size;
  automaticPages=pagination.pages;automaticTotal=pagination.total;
  $('connection').textContent=value.linked?`已連結：${value.account}；有效至 ${new Date(value.expires*1000).toLocaleString('zh-TW')}`:'尚未連結 EIP 帳號';
  const a=value.automatic;
  const saved=Boolean(value.session?.saved_credentials);
  $('session-state').textContent=value.session?.error||(
    saved?(value.linked?'手動操作連線可用。Session 過期時會自動重新登入，不需重填密碼。':'已保存帳密；按「查詢」時會自動重新登入，也可至「帳號與連線」測試連線。'):
    value.linked?'目前僅短期登入，未保存密碼；過期後需重新綁定。':'請先綁定 EIP 帳號。');
  $('automatic-status').textContent=a?`${a.enabled?'自動處理已啟用':'自動處理已暫停'}；待處理文件不限日期、已完成不重做；其他文件匣固定抓取起點（台北時間）：${taipeiTimestamp(a.scan_start)}；${a.message||'等待首次執行'}${a.enabled?` 下次排程：${taipeiTimestamp(a.next_run)}`:''}`:'尚未啟用。待處理文件不限日期；其他文件匣以首次啟用前 24 小時作為固定抓取起點。';
  if(a)$('connection').textContent=`已保存加密憑證（EIP 身分 ${a.identity}）；${a.enabled?'每小時自動處理已啟用':'自動排程已暫停，手動查詢仍可自動登入'}。不需重新輸入帳密。`;
  $('automatic-results').replaceChildren();
  const phases={done:'完成',needs_review:'待檢查',no_pdf:'無 PDF／Word 附件（待一次補件確認）',no_pdf_final:'無 PDF／Word 附件（已完成補件確認）',outside_scope:'早於抓取起點',discovered:'待處理',downloading:'下載／Word 轉換中',downloaded:'等待轉換',processing:'轉換中',converted:'等待匯入',importing:'匯入中',attaching:'行事曆附件夾帶中'};
  for(const r of (value.automatic_results||[]).slice(0,automaticPageSize)){
    const partial=r.phase==='done'&&(r.errors?.length||r.calendar_pending?.length);
    const notice=partial&&r.calendar_pending?.length?'已完成部分處理；仍有行事曆項目待核對，既有檔案與匯入紀錄保留。':r.notice;
    const box=element('div');box.className='document';box.append(element('strong',`${partial?'部分完成':phases[r.phase]||r.phase}：${r.subject}`),taskTimes(r),element('p',notice));
    if(r.errors?.length){const list=element('ul');for(const error of r.errors)list.append(element('li',error));box.append(list);}
    const synced=validSyncCounts(r.calendar_sync);
    if(synced)box.append(element('p',`行事曆已同步：新增 ${r.calendar_sync.created} 筆、略過 ${r.calendar_sync.skipped} 筆既有行程。無須再次上傳 CSV。`));
    for(const f of r.files||[]){if(!visibleDocumentExport(f)||!/^[0-9a-f-]{36}$/i.test(f.id))continue;const link=downloadLink(f,synced);link.className='download';box.append(link);}
    $('automatic-results').append(box);
  }
  if(!value.automatic_results?.length)$('automatic-results').textContent='尚無自動處理紀錄。';
  $('jobs').replaceChildren();
  for(const j of value.jobs){const b=element('button',j.progress||'查看工作結果');b.onclick=()=>action(()=>showJob(j.id));$('jobs').append(b);}
  }finally{if(request===statusRequest){automaticLoading=false;automaticPager();}}
}

async function search(){
  const value=await api('documents',{folder:$('folder').value,start:$('start').value,end:$('end').value,page});
  $('documents').replaceChildren();$('previous').disabled=page===0;$('next').disabled=!value.has_more;
  if(!value.documents.length)$('documents').textContent='此範圍沒有文件。';
  for(const d of value.documents){
    const box=element('div');box.className='document';box.append(element('div',`${d.date}　${d.subject}`));
    const b=element('button','查看 PDF／Word 附件');box.append(b);$('documents').append(box);
    b.onclick=()=>action(async()=>{
      b.disabled=true;
      try{const result=await api('attachments',{handle:d.handle});
        if(!result.attachments.length)box.append(element('p','沒有可下載的 PDF／DOCX／DOC 附件（EIP 表單本身不等於附件）。'));
        for(const a of result.attachments){const label=element('label');label.className='attachment';const check=element('input');check.type='checkbox';check.value=a.handle;check.name='attachment';label.append(check,document.createTextNode(` ${a.name}`));box.append(label);}
      }catch(e){b.disabled=false;throw e;}
    });
  }
  notice(`第 ${page+1} 頁，共 ${value.documents.length} 份文件。`);
}
$('search').onclick=()=>action(async()=>{page=0;await search();});
$('previous').onclick=()=>action(async()=>{if(page>0)page--;await search();});
$('next').onclick=()=>action(async()=>{page++;await search();});
$('process').onclick=()=>action(async()=>{
  const handles=[...document.querySelectorAll('input[name=attachment]:checked')].map(el=>el.value);
  const maximum=Number($('process').dataset.maxAttachments);
  if(!handles.length||handles.length>maximum)throw Error(`請選取 1 至 ${maximum} 個附件。`);
  const value=await api('process',{handles,instruction:$('instruction').value});
  notice('工作已送出；可以留在此頁查看進度。');await showJob(value.job_id);
});
async function showJob(id){
  selectedJob=id;const job=await api(`jobs/${encodeURIComponent(id)}`);
  renderJob(job);
  await status();
  polling=['queued','running'].includes(job.status);
}
function renderMessages(id,title,messages){
  const box=$(id);box.replaceChildren();
  if(!messages?.length)return;
  if(title)box.append(element('h4',title));
  const list=element('ul');for(const message of messages)list.append(element('li',message));box.append(list);
}
function visibleDocumentExport(file){
  // Preserve historical files/journal; omit only the old generated OCR copy.
  return file.kind!=='searchable_ocr_pdf' && !(!file.kind && /^可搜尋OCR_.*\.pdf$/i.test(file.name||''));
}
function validSyncCounts(value){
  return value&&Number.isInteger(value.created)&&value.created>=0&&Number.isInteger(value.skipped)&&value.skipped>=0&&value.created+value.skipped>0;
}
function downloadLink(file,synced=false){
  const csv=file.kind==='calendar_csv'||file.name.toLowerCase().endsWith('.csv');
  const label=csv?`${synced?'已同步・CSV 備份':'CSV 備份'}：${file.name}`:file.name;
  const link=element('a',label);link.href=`/api/v1/files/${file.id}/content?attachment=true`;link.target='_blank';link.rel='noopener';
  if(csv)link.onclick=event=>{
    if(!globalThis.confirm('此 CSV 僅供備份。已同步的行程請勿再次上傳 EIP，否則可能重複。尚未同步時，請優先使用本站「預覽匯入」。仍要下載備份嗎？'))event.preventDefault();
  };
  return link;
}
function renderJob(job){
  $('job-result').hidden=false;
  // Older jobs may contain Markdown in result; their structured file list and
  // progress remain usable without displaying the duplicate raw link dump.
  $('result').textContent=job.summary||job.progress||'處理結果已回傳';
  renderMessages('result-notices','',job.notices);
  renderMessages('result-warnings','提醒事項',job.warnings);
  renderMessages('result-errors','未完成項目',job.errors);
  $('downloads').replaceChildren();
  const kinds={document_pdf:'PDF',document_docx:'可編輯 Word',searchable_ocr_pdf:'可搜尋 PDF',calendar_csv:'行事曆 CSV'};
  const seen=new Set();
  // Only same-origin, UUID-scoped file links. Model/document HTML is never rendered.
  for(const file of (job.files||[]).filter(visibleDocumentExport)){
    if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(file.id)||seen.has(file.id)||typeof file.name!=='string')continue;
    seen.add(file.id);
    const row=element('div');row.className='download';
    const kind=kinds[file.kind];if(kind)row.append(element('strong',kind));
    row.append(downloadLink(file));
    if(file.name.toLowerCase().endsWith('.csv')){const b=element('button','預覽匯入');b.onclick=()=>action(async()=>{$('csv').value=file.id;await preview();});row.append(b);}
    $('downloads').append(row);
  }
  $('downloads-title').hidden=seen.size===0;
  if(seen.size)$('downloads-title').textContent=`下載檔案（${seen.size} 個）`;
}
function csvId(){const s=$('csv').value.trim();const m=s.match(/(?:^|\/api\/v1\/files\/)([0-9a-f-]{36})(?:\/content(?:\?.*)?)?$/i);if(!m)throw Error('請貼入 CSV 檔案 ID 或本站下載網址。');return m[1];}
async function preview(){
  previewToken=null;$('confirm').disabled=true;
  const value=await api('preview',{file_id:csvId()});previewToken=value.token;
  $('target').textContent=`匯入帳號：${value.account}。${value.visibility}。預覽十分鐘內有效。${value.limitations}`;
  $('events').replaceChildren();
  const labels={done:'已同步／將略過',existing:'EIP 已有相同行程／將略過',unknown:'結果不明／停止重送',conflict:'相似行程待核對／停止新增',new:'待新增'};
  for(const item of value.events){const box=element('div');box.className='event';box.append(element('strong',`${labels[item.status]||'狀態待核對'}：${item.row.Subject}`));
    if(item.reason)box.append(element('p',item.reason));
    box.append(element('p',`${item.row['Start Date']} ${item.row['Start Time(24hr)']||item.row['Start Time']} ～ ${item.row['End Date']} ${item.row['End Time(24hr)']||item.row['End Time']}；全天：${item.row['All day event']}`));
    const details=element('details');details.append(element('summary','檢查全部 CSV 欄位'));for(const [k,v] of Object.entries(item.row))details.append(element('div',`${k}：${v}`));box.append(details);$('events').append(box);
  }
  const blocked=value.events.some(e=>!['new','done','existing'].includes(e.status));
  const fresh=value.events.filter(e=>e.status==='new').length;
  $('confirm').disabled=blocked||!fresh;
  notice(blocked?'有行程需要人工核對，已停止新增；請先到 EIP 查看原行程。':fresh?`預覽完成：${fresh} 筆待新增，其餘已存在的行程將略過。確認時會再次核對。`:'所有行程均已同步或已存在於 EIP，無須再次匯入，也請勿重新上傳 CSV。');
}
$('preview').onclick=()=>action(preview);
$('confirm').onclick=()=>action(async()=>{
  if(!previewToken)return;
  $('confirm').disabled=true;
  const value=await api('confirm',{token:previewToken,confirm:true});previewToken=null;
  notice(`行事曆已同步：新增 ${value.created} 項；略過 ${value.skipped} 項既有行程。無須再次上傳 CSV。`);
});
const now=new Date();const before=new Date(now);before.setDate(now.getDate()-30);
function localDate(d){return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
$('start').value=localDate(before);$('end').value=localDate(now);
const initial=new URLSearchParams(location.search).get('csv');if(initial){$('csv').value=initial;$('advanced').open=true;}
action(status);
setInterval(()=>{if(polling&&selectedJob)action(()=>showJob(selectedJob));},5000);
setInterval(()=>{if(!polling&&!automaticLoading)action(status);},30000);
