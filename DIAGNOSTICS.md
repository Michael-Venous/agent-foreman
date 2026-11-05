# Hub diagnostics

The hub writes compact `[diagnostics]` JSON records to its user-service journal.
They cover slow or failed HTTP requests, upload start/finish (including size and
elapsed time), stalled event-stream drops, event-loop pauses, and a memory
sample approximately every five minutes. Upload records use a short correlation
ID and session ID prefix; they do not include file names or contents. HTTP
records use fixed route categories, never request URLs, query strings, headers,
or bodies. Memory figures describe the hub process, not the entire machine.

Read recent diagnostics with:

```sh
journalctl --user -u hapi-hub.service --since '1 hour ago' -o cat --no-pager | rg '^\[diagnostics\]'
```

`sse_drop` gives the limit or timeout, backlog counts, idle time, and memory.
`upload_start` and `upload_finish` share a request ID; a start without a finish
can indicate a pending operation or an interrupted hub. `event_loop_stall`
records timer delay, not its cause. Slow requests are logged at 1.5 seconds or
longer; `http_pending` records important requests still waiting after five seconds,
and shares a request ID with their eventual completion record. The sampler logs
at startup, every five minutes, or after a substantial loop stall;
it is not a heap profiler and cannot identify an allocation source. A phone
that cannot reach the hub at all may leave no server-side record.

These records are governed by the systemd journal's configured retention. Older
logs from before this change may contain full request URLs; avoid sharing them.
