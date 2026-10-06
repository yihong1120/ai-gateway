'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
class Element{constructor(tag){this.tag=tag;this.children=[];this.hidden=false;this.value='';this.textContent='';}append(...x){this.children.push(...x);}replaceChildren(...x){this.children=x;}}
const nodes={};const calls=[];let redirect;
const context={console,URL,URLSearchParams,Date,JSON,encodeURIComponent,Error,crypto:{randomUUID:()=> 'request-id'},
 document:{getElementById:id=>nodes[id]??=new Element(id),createElement:tag=>new Element(tag)},
 location:{search:'',replace:value=>{redirect=value;}},history:{replaceState(){}},setInterval(){},
 fetch:async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>({jobs:[]})};}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('company_agent/ui/web_dashboard.js','utf8')+'\nglobalThis.test={render,api,previewCalendar,refresh,select:id=>{selected=id;}};',context);
(async()=>{
 await new Promise(setImmediate);
 context.test.render({id:'job',status:'done',progress:'done',answer:'<img src=x onerror=alert(1)>',errors:['<script>bad</script>'],files:[{id:'file',name:'<svg onload=alert(1)>'}],delegations:[],proposals:[]});
 assert.equal(nodes.answer.textContent,'<img src=x onerror=alert(1)>');
 assert.equal(nodes.files.children[0].textContent,'<svg onload=alert(1)>');
 assert.equal(nodes.files.children[0].href,'/agents/artifacts/file');
 assert.equal(nodes.resume.hidden,true);
 assert.equal(nodes.extend,undefined);
 context.test.render({id:'budget-job',status:'running',files:[],budget:{used:40,limit:40}});
 assert.ok(nodes.budget.textContent.includes('40 / 40'));
 const resumeStart=calls.length;
 for(const job of [
   {status:'error',resumable:false}, {status:'running',resumable:true},
   {status:'awaiting_approval',resumable:true}, {status:'error',resumable:true,requires_restart:true},
   {status:'interrupted',resumable:true,budget:{used:40,limit:40}}]){
   context.test.render({id:'resume-test',files:[],...job});assert.equal(nodes.resume.hidden,true);
 }
 context.test.render({id:'resume-test',status:'error',resumable:true,files:[],budget:{used:3,limit:40}});
 assert.equal(nodes.resume.hidden,false);assert.equal(calls.length,resumeStart);
 context.test.select('resume-test');await nodes.resume.onclick();context.test.select('');
 const resumeCall=calls.slice(resumeStart).find(c=>c.path==='/agents/jobs/resume-test/resume');
 assert.ok(resumeCall);assert.deepEqual(JSON.parse(resumeCall.options.body),{});
 assert.equal(resumeCall.options.headers['X-Company-Action'],'1');
 context.test.render({id:'cad',status:'done',files:[],notices:['CAD：未知圖元需校對。<script>']});
 assert.equal(nodes.notices.children[0].textContent,'CAD：未知圖元需校對。<script>');
 context.test.render({id:'web',status:'done',files:[],web_sources:[
   {url:'javascript:alert(1)',title:'unsafe'},
   {url:'https://example.com/',title:'<script>not html</script>',kind:'search_snippet'}]});
 assert.equal(nodes['web-sources'].children.length,1);
 assert.equal(nodes['web-sources'].children[0].rel,'noopener noreferrer');
 assert.ok(nodes['web-sources'].children[0].textContent.includes('<script>not html</script>'));
 context.test.render({id:'job',status:'running',progress:'reading',files:[],errors:[],delegations:[]});
 assert.equal(nodes.followup.hidden,true);
 assert.equal(nodes.cancel.hidden,false);
 context.test.select('async-job');await nodes.cancel.onclick();context.test.select('');
 const cancelCall=calls.find(c=>c.path==='/agents/jobs/async-job/cancel');
 assert.ok(cancelCall);assert.deepEqual(JSON.parse(cancelCall.options.body),{});
 assert.equal(cancelCall.options.headers['X-Company-Action'],'1');
 context.test.render({id:'pdf',kind:'document',status:'done',files:[{id:'private csv',name:'calendar.csv',kind:'calendar_csv'}]});
 assert.equal(nodes.cancel.hidden,true);assert.equal(nodes.followup.hidden,true);assert.equal(nodes.resume.hidden,true);
 assert.equal(nodes.files.children[0].href,'/api/v1/files/private%20csv/content?attachment=true');
 assert.equal(nodes.files.children[1].href,'/eip?csv=private%20csv');
 await context.test.api('/agents/jobs/job/resume',{});
 assert.equal(calls.at(-1).options.credentials,'same-origin');
 assert.equal(calls.at(-1).options.headers['X-Company-Action'],'1');
 const listed={id:'listed',status:'done',answer:'Fresh snapshot',files:[]};
 context.fetch=async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>path==='/agents/jobs'?{jobs:[listed]}:{id:'older',status:'done',answer:'Older snapshot',files:[]}};};
 context.test.select('listed');let before=calls.length;await context.test.refresh();
 assert.equal(calls.length-before,1);assert.equal(nodes.answer.textContent,'Fresh snapshot');
 context.test.select('older');before=calls.length;await context.test.refresh();
 assert.equal(calls.length-before,2);assert.equal(calls.at(-1).path,'/agents/jobs/older');
 assert.equal(nodes.answer.textContent,'Older snapshot');context.test.select('');
 context.test.render({id:'job',status:'done',files:[{id:'csv',name:'calendar.csv',calendar_preview:true}]});
 assert.equal(nodes.files.children[1].textContent,'預覽行事曆匯入');
 context.fetch=async(path,options)=>{calls.push({path,options});return {ok:true,status:200,json:async()=>path.endsWith('/preview')?{token:'private-token',account:'本人',notice:'確認',events:[{row:{Subject:'<script>data</script>'}}]}:{created:1,skipped:0}};};
 await context.test.previewCalendar('csv');
 assert.equal(calls.at(-1).path,'/agents/calendar/preview');
 assert.ok(nodes['calendar-preview'].children[2].textContent.includes('<script>data</script>'));
 const approve=nodes['calendar-preview'].children[3];await approve.onclick();
 assert.equal(calls.at(-1).path,'/agents/calendar/confirm');
 assert.deepEqual(JSON.parse(calls.at(-1).options.body),{token:'private-token',confirm:true});
 context.fetch=async()=>({ok:false,status:401});
 await assert.rejects(context.test.api('/agents/jobs'),e=>e.status===401);
 assert.equal(redirect,undefined);
 console.log('Company Agent dashboard safe rendering, status, CSRF and native HTTP errors passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
