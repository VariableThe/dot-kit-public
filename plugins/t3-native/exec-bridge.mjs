/** Adapter for the existing tools execution isolate; no transport or credentials. */
const quoteShell = value => "'"+value.replaceAll("'", "'\\''")+"'";
const toolNames = ['t3_thread_launch','t3_thread_send','t3_thread_wait','t3_thread_read'];
const batchError = code => Object.assign(new Error('Journal batch reply is incomplete'),{code});
const assertBatchReply = (result,steps,fence) => {
  if (!result || result.ok!==true || result.completed!==steps.length ||
      !Array.isArray(result.results) || result.results.length!==steps.length)
    throw batchError('batch_invalid_response');
  steps.forEach((step,index)=>{
    if (!['checkpoint','release'].includes(step.action)) throw batchError('batch_invalid_response');
    const record=result.results[index]?.record;
    if (!record) throw batchError('batch_invalid_record');
    if (record.operationId!==fence.operationId || record.owner!==fence.owner || record.generation!==fence.generation)
      throw batchError('batch_record_mismatch');
    if (step.action==='checkpoint' && record.state!==step.fields?.state)
      throw batchError('batch_record_mismatch');
    if (step.action==='checkpoint')
      for (const [key,value] of Object.entries(step.fields ?? {}))
        if (record[key]!==value) throw batchError('batch_record_mismatch');
    if (step.action==='release' && record.leaseUntil!==0)
      throw batchError('batch_record_mismatch');
  });
  return result;
};
export function createToolBridge({execCommand,toolMap,journalPath,journalDirectory,persistenceOptions={},batchJournalPath}) {
  if (typeof execCommand!=='function' || !toolMap ||
      !toolNames.every(name=>typeof toolMap[name]==='function')) throw new TypeError('Existing execution and native T3 callbacks are required');
  if (![journalPath,journalDirectory].every(p=>typeof p==='string' && p.startsWith('/') && !p.includes('\0')))
    throw new TypeError('Verified absolute local journal paths are required');
  if (batchJournalPath!==undefined && !(typeof batchJournalPath==='string' && batchJournalPath.startsWith('/') && !batchJournalPath.includes('\0')))
    throw new TypeError('A verified absolute batch journal path is required');
  const options=Object.fromEntries(['sandbox_permissions','justification'].filter(k=>persistenceOptions[k]!==undefined).map(k=>[k,persistenceOptions[k]]));
  return {
    callTool:async(name,args)=>{
      if (!toolNames.includes(name)) throw Object.assign(new Error('Unsupported tool'),{code:'unsupported_tool'});
      return toolMap[name](args);
    },
    ...(batchJournalPath ? { journalBatch:async(steps,fence)=>{
      if (!fence || ![fence.operationId,fence.owner].every(v=>typeof v==='string' && v.trim().length>0) ||
          !Number.isInteger(fence.generation)) throw batchError('batch_invalid_response');
      const request={action:'batch',operationId:fence.operationId,owner:fence.owner,generation:fence.generation,steps};
      const command="printf '%s' "+quoteShell(JSON.stringify(request))+" | python3 "+quoteShell(batchJournalPath)+" --directory "+quoteShell(journalDirectory);
      const reply=await execCommand({cmd:command,login:false,max_output_tokens:20000,...options});
      let result;
      try { result=JSON.parse(reply.output); }
      catch { throw Object.assign(new Error('Journal batch response unavailable'),{code:'journal_invalid_response'}); }
      if (reply.exit_code!==0 || result.ok!==true) throw Object.assign(new Error('Journal batch failed'),{code:result.error?.code ?? 'batch_error',batch:{step:result.step,completed:result.completed,uncertain:result.uncertain===true}});
      return assertBatchReply(result,steps,fence);
    }} : {}),
    journal:async(action,fields={})=>{
      // The journal stores hashes, but this shell command carries the full request
      // JSON (including message/context). Shell or exec logs may retain that text.
      const command="printf '%s' "+quoteShell(JSON.stringify({action,...fields}))+" | python3 "+quoteShell(journalPath)+" --directory "+quoteShell(journalDirectory);
      const reply=await execCommand({cmd:command,login:false,max_output_tokens:5000,...options});
      let result;
      try { result=JSON.parse(reply.output); }
      catch { throw Object.assign(new Error('Journal response unavailable'),{code:'journal_invalid_response'}); }
      if (reply.exit_code!==0 || result.ok===false) throw Object.assign(new Error('Journal operation failed'),{code:result.error?.code ?? 'journal_error'});
      return result;
    },
  };
}
