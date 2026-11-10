import test from 'node:test';
import assert from 'node:assert/strict';
import { createToolBridge } from './exec-bridge.mjs';
const names=['t3_thread_launch','t3_thread_send','t3_thread_wait','t3_thread_read'];
const toolMap=Object.fromEntries(names.map(n=>[n,async args=>({name:n,args})]));
const config={toolMap,journalPath:'/tmp/test space/completion-journal.py',journalDirectory:'/tmp/test store'};
test('bridge only routes four existing native callbacks',async()=>{
 const b=createToolBridge({...config,execCommand:async()=>{throw Error('unused');}});assert.equal((await b.callTool('t3_thread_wait',{runId:'run'})).name,'t3_thread_wait');await assert.rejects(()=>b.callTool('approve',{}),/Unsupported/);
});
test('journal command quotes untrusted JSON and paths and parses result',async()=>{
 let command;const b=createToolBridge({...config,execCommand:async args=>{command=args;return {exit_code:0,output:'{"record":{"state":"watching"}}'};}});
 assert.equal((await b.journal('claim',{args:{message:"a'; touch /tmp/bad; echo 'b"}})).record.state,'watching');assert.ok(command.cmd.includes("'\\''"));assert.ok(command.cmd.includes("'/tmp/test space/completion-journal.py'"));assert.equal(command.login,false);
});
test('journal failure preserves exact safe code and is not retried',async()=>{
 let n=0;const b=createToolBridge({...config,execCommand:async()=>{n++;return {exit_code:1,output:'{"ok":false,"error":{"code":"owner_busy"}}'};}});await assert.rejects(()=>b.journal('claim',{}),e=>e.code==='owner_busy');assert.equal(n,1);
});
test('uncertain journal output does not imply success',async()=>{
 const b=createToolBridge({...config,execCommand:async()=>({session_id:1,output:''})});await assert.rejects(()=>b.journal('claim',{}),e=>e.code==='journal_invalid_response');
});
