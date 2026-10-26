/** Read an existing namespace-bound Codex queue; never load a thread or send input. */
import { initializeToken } from '../vendor/hapi-source/cli/src/ui/tokenInit';
import { ApiClient } from '../vendor/hapi-source/cli/src/api/api';
import { findRuntime, runtimeAlive } from '../vendor/hapi-source/cli/src/codex/shared/registry';
import { CodexAppServerClient } from '../vendor/hapi-source/cli/src/codex/codexAppServerClient';
import { initializeSharedClient } from '../vendor/hapi-source/cli/src/codex/shared/launch';
import { SubmissionSchema } from '../vendor/hapi-source/cli/src/codex/shared/queue';

const [sessionId, nativeId] = process.argv.slice(2);
try {
    if (!sessionId || !nativeId) throw new Error('Exact session/native IDs required');
    await initializeToken();
    const runtime = await findRuntime(sessionId);
    if (!runtime) throw new Error('No live existing runtime');
    const binding = runtime.sessions[sessionId];
    const session = await (await ApiClient.create()).getSession(sessionId);
    if (binding.namespace !== session.namespace || binding.threadId !== nativeId
        || session.metadata?.codexSessionId !== nativeId) throw new Error('Binding changed');
    const client = new CodexAppServerClient({ endpoint: runtime.endpoint, token: runtime.token });
    try {
        await initializeSharedClient(client);
        const items: ReturnType<typeof SubmissionSchema.parse>[] = [];
        const cursors = new Set<string>();
        let cursor: string | undefined;
        do {
            const page = await client.request('thread/queue/list', { threadId: nativeId, cursor }) as { data: unknown[]; nextCursor?: string | null };
            if (!Array.isArray(page.data)) throw new Error('Invalid queue');
            items.push(...page.data.map(value => SubmissionSchema.parse(value)));
            if (items.length > 1000) throw new Error('Queue exceeds inspection bound');
            cursor = page.nextCursor ?? undefined;
            if (cursor && cursors.has(cursor)) throw new Error('Stalled queue cursor');
            if (cursor) cursors.add(cursor);
        } while (cursor);
        const current = await findRuntime(sessionId);
        if (!current || !runtimeAlive(runtime) || current.id !== runtime.id || current.marker !== runtime.marker
            || current.sessions[sessionId]?.threadId !== nativeId) throw new Error('Runtime changed during inspection');
        console.log(JSON.stringify({ session: sessionId, native_id: nativeId, readOnly: true, items }));
    } finally { await client.disconnect(); }
} catch {
    // Never print credential-bearing transport/config exceptions.
    console.error('Native queue inspection failed or binding changed; no input or lifecycle action attempted.');
    process.exitCode = 1;
}
