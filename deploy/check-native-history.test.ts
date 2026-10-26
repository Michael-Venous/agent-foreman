import { expect, test } from 'bun:test';
import { checkNativeHistory } from './check-native-history';
import type { CodexAppServer } from '../vendor/hapi-source/hub/src/codexFolders';
const id = 'd1221ff7-242c-58c4-9d5a-65c245f25cb1';
test('reports missing lineage with exact identity and only read operations', async () => {
    const calls: string[] = [];
    const server: CodexAppServer = work => work(async method => {
        calls.push(method);
        if (method === 'thread/read') return { thread: { id, historyMode: 'paginated' } };
        throw new Error('invalid paginated history lineage: missing source rollout');
    });
    const result = await checkNativeHistory(server, id);
    expect(result.status).toBe('blocked'); expect(result.error).toContain('missing source rollout');
    expect(result.nativeId).toBe(id); expect(calls).toEqual(['thread/read', 'thread/turns/list']);
});
test('checks the first native page without exporting history or claiming a full audit', async () => {
    const server: CodexAppServer = work => work(async method => method === 'thread/read' ? { thread: { id, historyMode: 'paginated' } } : { data: [{ privateHistory: 'not exported' }] });
    const result = await checkNativeHistory(server, id);
    expect(result.status).toBe('ready'); expect(result.check).toBe('first-page-lineage'); expect(JSON.stringify(result)).not.toContain('privateHistory');
});
test('refuses mismatched native identities and invalid IDs before any mutation', async () => {
    const server: CodexAppServer = work => work(async () => ({ thread: { id: 'other' } }));
    expect((await checkNativeHistory(server, id)).status).toBe('blocked');
    await expect(checkNativeHistory(server, '../not-a-thread')).rejects.toThrow('Invalid native');
});
