/** Adapter for the existing tools execution isolate; no transport or credentials. */
const quoteShell = value => "'"+value.replaceAll("'", "'\\''")+"'";
const toolNames = ['t3_thread_launch','t3_thread_send','t3_thread_wait','t3_thread_read'];
export function createToolBridge({execCommand,toolMap,journalPath,journalDirectory,persistenceOptions={}}) {
  if (typeof execCommand!=='function' || !toolMap ||
      !toolNames.every(name=>typeof toolMap[name]==='function')) throw new TypeError('Existing execution and native T3 callbacks are required');
  if (![journalPath,journalDirectory].every(p=>typeof p==='string' && p.startsWith('/') && !p.includes('\0')))
    throw new TypeError('Verified absolute local journal paths are required');
  const options=Object.fromEntries(['sandbox_permissions','justification'].filter(k=>persistenceOptions[k]!==undefined).map(k=>[k,persistenceOptions[k]]));
  return {
    callTool:async(name,args)=>{
      if (!toolNames.includes(name)) throw Object.assign(new Error('Unsupported tool'),{code:'unsupported_tool'});
      return toolMap[name](args);
    },
    journal:async(action,fields={})=>{
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
