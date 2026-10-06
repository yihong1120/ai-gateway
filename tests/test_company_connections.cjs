'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node{constructor(){this.value='';this.checked=false;this.children=[];}replaceChildren(...children){this.children=children;}set innerHTML(v){throw Error('unsafe HTML');}}
const html=fs.readFileSync('company_agent/ui/connections.html','utf8');
const nodes=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Node()]));
const calls=[];let redirect,failEIP=false;
const status={eip:{linked:false,session:{saved_credentials:true},automatic:{enabled:true,scan_start:1,next_run:2}},nas:{configured:true,bound:true,usable:true}};
const context={URLSearchParams,Date,Promise,Error,Boolean,
 document:{getElementById:id=>{assert.ok(nodes[id],'missing HTML node '+id);return nodes[id];},createElement:()=>new Node()},
 location:{replace:url=>redirect=url},localStorage:{getItem(){throw Error('Do not access localStorage');}},
 fetch:async(path,options)=>{calls.push({path,options});const eip=path.startsWith('/api/v1/eip/');return {status:failEIP&&eip?503:200,ok:!(failEIP&&eip),json:async()=>failEIP&&eip?{detail:'EIP unavailable'}:{...(eip?status.eip:status.nas),shares:[{name:'<img src=x onerror=alert(1)>'}]}};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('company_agent/ui/connections.js','utf8')+'\nglobalThis.test={api,refresh};',context);
(async()=>{
 await new Promise(setImmediate);
 assert.equal(calls.length,3);assert.ok(calls.every(c=>c.options.method==='GET'));
 assert.equal(nodes['eip-auto'].checked,false);assert.equal(nodes['nas-consent'].checked,false);
 assert.match(nodes['eip-status'].textContent,/已綁定/);
 for(const provider of ['eip','nas']){
  nodes[provider+'-account'].value=provider+'-account';nodes[provider+'-password'].value=provider+'-secret';
 }
 nodes['eip-http'].checked=true;
 await nodes['eip-form'].onsubmit({preventDefault(){assert.equal(nodes['eip-password'].value,'eip-secret');}});
 assert.equal(nodes['eip-password'].value,'');assert.equal(nodes['nas-password'].value,'nas-secret');
 const ep=calls.find(c=>c.path==='/api/v1/eip/link');assert.equal(ep.options.headers['X-EIP-Action'],'1');
 assert.equal(JSON.parse(ep.options.body).enable_auto,false);assert.ok(!ep.options.body.includes('nas-secret'));
 nodes['nas-consent'].checked=true;
 const nasAction=nodes['nas-form'].onsubmit({preventDefault(){}});assert.equal(nodes['nas-password'].value,'');await nasAction;
 const np=calls.find(c=>c.path==='/agents/nas/bind');assert.equal(np.options.headers['X-Company-Action'],'1');
 assert.equal(JSON.parse(np.options.body).password,'nas-secret');assert.ok(!np.options.body.includes('eip-secret'));
 assert.equal(nodes['nas-shares'].children[0].textContent,'<img src=x onerror=alert(1)>');
 assert.equal(nodes['nas-consent'].checked,false);
 const before=calls.length;await nodes['nas-unlink'].onclick();
 assert.ok(calls.slice(before).every(c=>c.path.startsWith('/agents/nas')));
 failEIP=true;await assert.rejects(context.test.refresh('eip'),e=>e.status===503);await context.test.refresh('nas');
 assert.match(nodes['nas-status'].textContent,/已綁定/);assert.equal(nodes['eip-test'].disabled,false);
 context.fetch=async()=>({status:401,ok:false});
 await assert.rejects(context.test.api('nas','/agents/nas'),e=>e.status===401);
 assert.equal(redirect,undefined);
 console.log('Unified connections: existing binding, separate consent/credentials/actions, partial outage, safe DOM and native HTTP errors passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
