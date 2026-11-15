// Exercise the real renderer without a browser, network or cloud requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Node {
  constructor(tag='div') {this.tag=tag;this.children=[];this.text='';}
  set textContent(value) {this.text=String(value);this.children=[];}
  get textContent() {return this.text+this.children.map(n=>n.textContent).join('');}
  set innerHTML(_) {throw Error('Untrusted content must not be interpreted as HTML');}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.text='';this.children=nodes;}
}
const nodes=new Map();
const get=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
const context={URLSearchParams,Date,Set,
  document:{getElementById:get,createElement:tag=>new Node(tag)},
  location:{search:'',replace(){}},setInterval(){},
  // Initial status polling stays pending. No HTTP request is made.
  fetch:()=>new Promise(()=>{}),
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../examples/official_document_workflow/frontend/assets/app.js'),'utf8'),context);
assert.match(context.taipeiTimestamp('2026-09-03T08:26:59Z'),/2026\/09\/03.*16:26:59/);
assert.equal(context.taipeiTimestamp(null),null);
assert.equal(context.taipeiTimestamp('<script>'),null);
assert.equal(context.taipeiTimestamp(true),null);
const timing=context.taskTimes({eip_recorded_at:'2026-09-03T16:26:59+08:00',first_detected_at:1788441600,last_processed_at:1788441601});
assert.match(timing.textContent,/EIP 文件匣時間：2026\/09\/03.*16:26:59/);
assert.match(timing.textContent,/最後處理時間：/);
assert.doesNotMatch(timing.textContent,/非處理完成時間/);
assert.doesNotMatch(timing.textContent,/時間詳情|簽核收件時間|首次偵測時間|未提供|未記錄/);
assert.equal(timing.children[0].children.length,2);
assert.doesNotMatch(timing.children[0].textContent,/未提供|未記錄|首次偵測|收件時間/);
assert.equal(timing.children.length,1);
assert.equal(timing.children[0].tag,'div');
const legacy=context.taskTimes({updated:1788441600});
assert.equal(legacy.children[0].children.length,1);
assert.equal(legacy.children[0].textContent,'尚無已記錄的時間');
assert.doesNotMatch(legacy.children[0].textContent,/處理完成|最後處理/);
assert.equal(context.taskTimes({}).children[0].textContent,'尚無已記錄的時間');
const pdf={id:'11111111-1111-4111-8111-111111111111',name:'附件<img src=x onerror=alert(1)>.pdf',kind:'document_pdf'};
const csv={id:'22222222-2222-4222-8222-222222222222',name:'行事曆.csv',kind:'calendar_csv'};
context.renderJob({status:'attention',summary:'文件完成，提醒待確認',
  result:'## raw Markdown [duplicate](https://evil.test)',
  notices:['沒有明確期限'],warnings:['無法建立相對提醒'],errors:[],
  files:[pdf,pdf,csv,{id:'https://evil.test',name:'bad.pdf'}]});
assert.equal(get('result').textContent,'文件完成，提醒待確認');
assert.equal(get('downloads').children.length,2);
assert.equal(get('downloads-title').textContent,'下載檔案（2 個）');
assert.equal(get('result-errors').children.length,0);
assert.match(get('result-warnings').textContent,/無法建立相對提醒/);
const rows=get('downloads').children;
const link=rows[0].children.find(n=>n.tag==='a');
assert.equal(link.textContent,pdf.name);
assert.equal(link.href,`/api/v1/files/${pdf.id}/content?attachment=true`);
assert.equal(link.rel,'noopener');
assert.equal(rows[0].children.filter(n=>n.tag==='a').length,1);
const preview=rows[1].children.find(n=>n.tag==='button');
assert.equal(preview.textContent,'預覽匯入');
assert.equal(typeof preview.onclick,'function');
// Older job messages aren't re-parsed as Markdown or duplicated under links.
context.renderJob({status:'partial',progress:'部分完成',result:'## old [file](/api/...)',
  files:[pdf],errors:['Word 轉換失敗']});
assert.equal(get('result').textContent,'部分完成');
assert.match(get('result-errors').textContent,/Word 轉換失敗/);
assert.equal(get('result-warnings').children.length,0);
assert.equal(get('downloads').children.length,1);
// Switching to a queued/failed job clears the previous job's file list.
context.renderJob({status:'error',progress:'服務中斷'});
assert.equal(get('downloads').children.length,0);
assert.equal(get('downloads-title').hidden,true);
assert.equal(get('result-errors').children.length,0);
// Historical OCR derivatives are hidden without deleting their stored records.
const ocr={id:'33333333-3333-4333-8333-333333333333',name:'可搜尋OCR_公文.pdf',kind:'searchable_ocr_pdf'};
context.renderJob({files:[pdf,ocr,csv,{...ocr,kind:undefined}]});
assert.equal(get('downloads').children.length,2);
assert.equal(context.visibleDocumentExport(ocr),false);
assert.equal(context.visibleDocumentExport({...ocr,kind:undefined}),false);
assert.equal(context.visibleDocumentExport({...ocr,kind:'document_pdf'}),true);
(async()=>{
  let prevented=false;
  context.confirm=()=>false;
  const backup=context.downloadLink(csv,true);
  backup.onclick({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.match(backup.textContent,/已同步・CSV 備份/);
  prevented=false;context.confirm=()=>true;
  backup.onclick({preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
  assert.equal(context.validSyncCounts({created:0,skipped:1}),true);
  assert.equal(context.validSyncCounts({created:0,skipped:0}),false);
  assert.equal(Boolean(context.validSyncCounts(null)),false);
  get('csv').value=csv.id;
  const calendarRow={'Subject':'會勘[AI產生]','Start Date':'2026/09/10','End Date':'2026/09/10','Start Time(24hr)':'10:00','End Time(24hr)':'11:00','All day event':'假'};
  for(const [statuses,disabled] of [[['existing'],true],[['done'],true],[['new','existing'],false],[['new','conflict'],true],[['unknown'],true]]){
    context.fetch=async()=>({ok:true,json:async()=>({token:'preview',account:'fixture',visibility:'私人',limitations:'',events:statuses.map(status=>({status,row:calendarRow,reason:status==='conflict'?'請核對原行程':''}))})});
    await context.preview();assert.equal(get('confirm').disabled,disabled);
    if(statuses.includes('conflict'))assert.match(get('events').textContent,/請核對原行程/);
    if(statuses[0]==='existing')assert.match(get('notice').textContent,/無須再次匯入/);
  }
  context.fetch=async()=>({ok:true,json:async()=>({jobs:[],automatic_results:[{phase:'needs_review',subject:'公文',errors:['CSV 證據不足 <script>'],files:[pdf,ocr,csv,{...ocr,kind:undefined}]}]})});
  await context.status();
  const links=get('automatic-results').children[0].children.filter(n=>n.tag==='a');
  assert.equal(links.length,2);
  assert.deepEqual(links.map(n=>n.textContent),[pdf.name,`CSV 備份：${csv.name}`]);
  assert.match(get('automatic-results').textContent,/CSV 證據不足 <script>/);
  context.fetch=async()=>({ok:true,json:async()=>({jobs:[],automatic_results:[{
    phase:'needs_review',subject:'行程已同步但附件待核對',calendar_sync:{created:0,skipped:1},files:[pdf,csv]}]})});
  await context.status();
  assert.match(get('automatic-results').textContent,/新增 0 筆、略過 1 筆/);
  assert.match(get('automatic-results').textContent,/無須再次上傳 CSV/);
  assert.match(get('automatic-results').textContent,/已同步・CSV 備份/);
  context.fetch=async()=>({ok:true,json:async()=>({jobs:[],automatic_results:[{
    phase:'done',subject:'舊版部分完成紀錄',notice:'文件輸出仍有待核對項目',
    errors:['時間條件未核對'],calendar_pending:['時間條件未核對'],files:[pdf,csv]}]})});
  await context.status();
  assert.match(get('automatic-results').textContent,/部分完成：舊版部分完成紀錄/);
  assert.match(get('automatic-results').textContent,/仍有行事曆項目待核對/);
  assert.doesNotMatch(get('automatic-results').textContent,/文件輸出仍有待核對項目/);
  const requests=[];
  const response=(page,total=12,size=5)=>({jobs:[],automatic_pagination:{page,page_size:size,pages:Math.max(1,Math.ceil(total/size)),total},
    automatic_results:Array.from({length:Math.min(size,Math.max(0,total-(page-1)*size))},(_,i)=>({phase:'done',subject:`第${(page-1)*size+i+1}筆`,files:[pdf]}))});
  context.fetch=async url=>{
    requests.push(url);const query=new URLSearchParams(url.split('?')[1]);
    return {ok:true,json:async()=>response(Number(query.get('automatic_page')),12,Number(query.get('automatic_page_size')))};
  };
  await context.status(1);
  assert.equal(get('automatic-results').children.length,5);
  assert.equal(get('automatic-previous').disabled,true);
  assert.equal(get('automatic-next').disabled,false);
  await get('automatic-next').onclick();
  assert.match(get('automatic-page-info').textContent,/第 2／3 頁/);
  assert.match(get('automatic-results').textContent,/第6筆/);
  await context.status(); // Polling preserves the selected page.
  assert.match(requests.at(-1),/automatic_page=2&automatic_page_size=5/);
  await get('automatic-next-bottom').onclick();
  assert.equal(get('automatic-results').children.length,2);
  assert.equal(get('automatic-next').disabled,true);
  assert.equal(get('automatic-next-bottom').disabled,true);
  get('automatic-page-size').value='10';await get('automatic-page-size').onchange();
  assert.match(get('automatic-page-info').textContent,/第 1／2 頁/);
  assert.equal(get('automatic-results').children.length,10);
  // Late polling responses must not overwrite a newer navigation response.
  const pending=[];context.fetch=()=>new Promise(resolve=>pending.push(resolve));
  const first=context.status(1,5),second=context.status(2,5);
  pending[1]({ok:true,json:async()=>response(2)});await second;
  pending[0]({ok:true,json:async()=>response(1)});await first;
  assert.match(get('automatic-page-info').textContent,/第 2／3 頁/);
  context.fetch=async()=>{throw Error('offline');};
  await assert.rejects(()=>context.status(3,5),/offline/);
  assert.match(get('automatic-page-info').textContent,/第 2／3 頁/);
  assert.equal(get('automatic-next').disabled,false);
  context.fetch=async()=>({ok:true,json:async()=>response(1,0)});
  await context.status();
  assert.match(get('automatic-results').textContent,/尚無自動處理紀錄/);
  assert.equal(get('automatic-next').disabled,true);assert.equal(get('automatic-previous').disabled,true);
  // The manual action sends all ten files, and blocks eleven before HTTP.
  let submitted=null;
  get('process').dataset={maxAttachments:'10'};
  context.document.querySelectorAll=()=>Array.from({length:10},(_,i)=>({value:`file-${i}`}));
  context.api=async(path,data)=>{assert.equal(path,'process');submitted=data;return {job_id:'limit-test'};};
  context.showJob=async()=>{};
  await get('process').onclick();
  assert.equal(submitted.handles.length,10);
  submitted=null;
  context.document.querySelectorAll=()=>Array.from({length:11},(_,i)=>({value:`file-${i}`}));
  await get('process').onclick();
  assert.equal(submitted,null);
  assert.match(get('notice').textContent,/1 至 10/);
  console.log('Dashboard result, hourly download filtering and safe-text tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
