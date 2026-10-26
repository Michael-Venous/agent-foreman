/** Read-only native preflight. Never resume/fork, inject input, rewrite rollout, or start a turn. */
import { createStdioAppServer, type CodexAppServer } from '../vendor/hapi-source/hub/src/codexFolders';
export async function checkNativeHistory(server: CodexAppServer, threadId: string) {
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(threadId)) throw new Error('Invalid native thread ID');
    return server(async call => {
        let mode: unknown;
        try {
            const thread = (await call('thread/read', { threadId, includeTurns: false })).thread as Record<string, unknown>;
            if (thread?.id !== threadId) throw new Error('Native history identity mismatch');
            mode = thread.historyMode;
            if (mode === 'paginated') {
                const page = await call('thread/turns/list', { threadId, limit: 1, sortDirection: 'asc', itemsView: 'full' });
                if (!Array.isArray(page.data)) throw new Error('Invalid native history page');
            }
            return { nativeId: threadId, status: 'ready', mode, check: mode === 'paginated' ? 'first-page-lineage' : 'metadata-only' };
        } catch (error) {
            return { nativeId: threadId, status: 'blocked', mode, error: String(error), action: 'Restore the verified original rollout lineage, then check again. No thread replacement, input or history rewrite performed.' };
        }
    });
}
if (import.meta.main) {
    try { console.log(JSON.stringify(await checkNativeHistory(createStdioAppServer(process.env.HAPI_CODEX_APP_SERVER_BIN || 'codex'), process.argv[2]))); }
    catch (error) { console.log(JSON.stringify({ status: 'blocked', error: String(error) })); process.exitCode = 1; }
}
