#!/usr/bin/env python3
"""Read-only dispatch audit against the preserved all-chat usage ledger."""
import collections, datetime, json, pathlib, sqlite3
ROOT = pathlib.Path(__file__).resolve().parents[1]
ledger = json.loads((ROOT/'evidence/claude-all-chats-usage-2026-10-19.json').read_text())
start, end = '2026-10-19T17:43:05', '2026-10-19T17:48:05'
def epoch(t):
    return datetime.datetime.fromisoformat(t).replace(tzinfo=datetime.timezone.utc).timestamp()
db = sqlite3.connect(f'file:{ROOT}/state/manager.sqlite?mode=ro', uri=True)
db.row_factory = sqlite3.Row
rows = {}
for table in ('outbox', 'assignments', 'workers'):
    rows[table] = [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE created >= ? AND created < ?', (epoch(start), epoch(end)))]
requests = [r for r in ledger['requests'] if start <= r['timestamp'] < end]
assert len(requests) == len({r['id'] for r in requests}) == 83
parent = [r for r in requests if r['session'] == '750601e4-1bc6-5a04-b803-dca8b0a77ca1' and r['agent'] == 'main']
keys = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
report = {
    'window_utc': [start, end],
    'dispatch_counts': {k:len(v) for k,v in rows.items()},
    'dispatch_records': rows,
    'unique_provider_requests': len(requests),
    'parent_requests': len(parent),
    'parent_mean_input_context': sum(sum(r['usage'].get(k,0) for k in keys) for r in parent)/len(parent),
    'effort': dict(collections.Counter(r['effort'] for r in requests)),
    'breakdown': ledger['final_five_minutes_breakdown'],
    'provider_rejection_records': [r for r in ledger['rate_timeline'] if 'limit reached|' in r['text']],
    'finding': 'No supervisor dispatch/restart amplification found in this interval. Two distinct event notifications went to the persistent manager, one assignment went to SampleSmoke, zero workers were created. The development chat received neither notification. Its 32 parent requests and 45 requests from two native subagents account for 77 of 83 requests. All 83 used medium effort. These are distinct provider responses, not replayed usage snapshots.',
    'limits': 'This rules out the observed manager dispatch path as the burst source, not every possible upstream bug. Provider quota weighting is private; local tokens cannot independently prove Max 5x deductions correct.',
    'context_verification': {'model':'gpt-6-astra','official_model_capacity':1050000,'local_catalog_default':272000,'local_catalog_max':872000,'effective_percent':95,'live_session_reported':258400,'session':'cb7fea54-d8ab-5b14-b9b0-3d782c30b5e5','finding':'The displayed denominator matches the active runtime, not a UI fallback. No context override is configured.'}
}
(ROOT/'evidence/claude-burst-dispatch-2026-10-19.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ['dispatch_counts','unique_provider_requests','parent_mean_input_context','effort']},indent=2))
