import { initializeToken } from '../vendor/hapi-source/cli/src/ui/tokenInit';
import { ApiClient } from '../vendor/hapi-source/cli/src/api/api';
import { findRuntime, runtimeAlive } from '../vendor/hapi-source/cli/src/codex/shared/registry';
import { CodexAppServerClient } from '../vendor/hapi-source/cli/src/codex/codexAppServerClient';
import { initializeSharedClient } from '../vendor/hapi-source/cli/src/codex/shared/launch';
import { readNativeBackground } from '../vendor/hapi-source/cli/src/codex/shared/nativeBackground';
const [sid,thread]=process.argv.slice(2);
try {
 await initializeToken(); const rt=await findRuntime(sid); if(!rt) throw Error('no runtime');
 const session=await (await ApiClient.create()).getSession(sid), binding=rt.sessions[sid];
 if(!binding || binding.namespace!==session.namespace || binding.threadId!==thread || session.metadata?.codexSessionId!==thread) throw Error('binding');
 const client=new CodexAppServerClient({endpoint:rt.endpoint,token:rt.token});
 try { await initializeSharedClient(client); const terminals=await readNativeBackground(client,thread); const current=await findRuntime(sid);
 if(!current || !runtimeAlive(rt) || current.id!==rt.id || current.marker!==rt.marker || current.sessions[sid]?.threadId!==thread) throw Error('changed');
 console.log(JSON.stringify({readOnly:true,terminalCount:terminals.length,terminals}));
 } finally {await client.disconnect()}
} catch {console.error('Native inspection failed or binding changed; no input or lifecycle action attempted.');process.exitCode=1}
