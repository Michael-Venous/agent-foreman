# Manager control tool

Run `python3 /home/user/Documents/code/agent-foreman/managerctl.py '<JSON>'`, or `managerctl.py --file /abs/path.json`, or pipe JSON to `managerctl.py -` (use a file for large payloads or text with quotes). Every request is one object with an `op`.

- `actor` is always stamped from the calling chat's `HAPI_SESSION_ID`; never supply it.
- `requested_at` is stamped automatically for `resume_chat`, `steer_assignment`, `steer_queued`, `supersede_assignment`, `resource_handoff`, `resource_reservation`, `gpu_unclaim`, `cancel_queued`, `recovery_process_authorize`. Ops that require "current user authorization" also need `"user_requested":true` and a `basis` quoting the user's current instruction; the authorization expires after 5 minutes.
- A timeout means the outcome is unknown: inspect `status`/the target before any retry. Never repeat a mutation blindly.
- In node mode a mid-level manager may use only `status, events, ack, sessions, read, read_native, requests, answer, check_status, digest, reporting, remind, reminders, cancel_reminder, assign, interrupt, assignment_delivery, steer_assignment, cancel_queued (assignment), node_graph, node_tree, node_edit, node_spawn, node_detach, node_sync, gpu`, and only on its own children/subtree. All other ops are overseer-only there. Ops marked **overseer** are overseer-only even outside node mode.

## Op reference

### Projects
- `register` `name, repo, objective?` — create a project (starts paused). Returns its ID.
- `update` `project, objective?, next_steps?, blockers?, checkpoint?, integration_owner?` — persist fields. A changed objective resets the autonomous budget (8 assignments).
- `resume` `project, checkpoint?` — set running and clear a project hold. Only on user instruction.
- `pause` `project, checkpoint?` — stop future assignments; the running turn continues. Does not clear a hold.
- `complete` `project, checkpoint` — mark done after verification. Refused while assignments are unresolved; releases the project's resource claims.

### Workers: launch, attach, adopt
- `launch` `project, runtime:"codex"|"claude", worktree?, permission?:"full"|"ask", model?, reasoning_effort?` — spawn a worker with no prompt. Project must be running. `worktree:true` (requires `integration_owner`) is mandatory when another worker owns the checkout. `reasoning_effort` maps to Codex `modelReasoningEffort` / Claude `effort`; acceptance does not prove the provider used it.
- `attach` `project, runtime, native_id, session_cwd?, permission?, user_requested?` — register an existing native conversation read-only (no resume, no controller). Refuses a conversation the user has open in HAPI unless `user_requested:true`. Claude only: `session_cwd` must exactly match the history's recorded cwd when it differs from the project repo; then tell the worker which repo to work in.
- `adopt` `worker` — resume a registered conversation under HAPI, preserving history. Only after external controllers are stopped; rejects busy native writers; for paginated Codex histories runs `inspect_native_history` first.
- `permissions` `worker, permission:"full"|"ask"` — change the worker's HAPI permission mode and stored preference. Default `full` = Claude `bypassPermissions` / Codex `yolo`. Grants no sudo, SSH access or wider scope.
- `workspace` `worker, path` — record the actual work directory (existing absolute dir) separately from the launch cwd. Only after verifying the worker's history; worker must be idle with no unresolved checkout assignment.
- `manage` `worker, user_requested?, resume_management?, owner?` — start/restore observation only (registry change; never sends, resumes, clears goals or holds). Idempotent. A released or `unmanage`d chat needs the user's explicit request (`user_requested:true, resume_management:true` after `unmanage`). In node mode adds an owner edge (`owner` defaults to overseer).
- `unmanage` `worker, session` (exact current HAPI ID) — stop oversight without aborting: watch off, reminders and usage recovery cancelled, pending events suppressed, assignment history kept. In node mode removes the owner edge and requires `user_requested:true` + `basis`.
- `retention` `worker, mode?:"until-idle"|"keep", reason?` — classic mode only: read/set whether a finished idle chat is auto-unmanaged. `keep` needs a reason.

### Assignments and delivery
- `assign` `worker, text, key, goal?, scope?, delivery_mode?, user_requested?` — durable, deduplicated task. Same `key` returns the existing assignment plus its delivery state; never resend with a new key to resolve uncertain delivery.
  - Requires: running project, active idle worker, no unresolved assignment or pending recovery, no unhandled user instruction queued for the manager, budget left, < 3 failed assignments, free checkout.
  - `user_requested:true` (only when the user asked for this message): bypasses project pause/hold, budget, busy chat (queued), prior unresolved task, pending recoveries and released state; consumes no budget. Never bypasses Stop latches, `unmanage`, dedupe keys, disconnection or checkout exclusion.
  - `goal:true` sends `/goal <text>` (≤ 4000 chars). The assignment finishes when the native goal completes, not when a turn ends.
  - `scope:"checkout"` (default) excludes other unresolved checkout work in the same, parent or child directory. `scope:"external"` only for work that never touches a repo (email, browser, messaging); it is an instruction, not a sandbox.
  - `delivery_mode:"queue"` (default) | `"steer"` (Codex, Claude with native priority) | `"next"` (Claude only). Priority, not proof of compliance. Slash/goal commands must use `queue`.
  - Slash-command text (e.g. `/goal clear`, `/goal resume`) is sent verbatim with `goal:false`; slash + `goal:true` is rejected. Ordinary text gets one appended line each for project resource claims, personal reservations, a non-default workspace, or external scope.
  - Returns `warning` when text exceeds 1200 chars: send the user's request plus needed context; label your own ideas optional.
- `assignment_delivery` `worker, assignment, historical?` — read queue/receipt evidence. States: `sent` (dispatched), `queued` (unconsumed), `consumed`/`steered`/`control-consumed` (runtime intake), `indeterminate`/`unconfirmed` (investigate). `sessionReceipt` for an early spawn proves intake only. `historical:true` paginates for older local IDs.
- `steer_assignment` `worker, assignment` + current user authorization — promote an existing queued assignment to the running turn via the priority endpoint (same ID; never resends). Refused for control/goal commands, stops, holds, released chats, identity changes.
- `cancel_queued` `assignment` — atomically cancel an uninvoked queued assignment; only a `cancelled` reply updates the record.
- `interrupt` `worker, pause?` — abort the current turn (HAPI abort). `pause` defaults to true (pauses the project). Not the user's Stop button.
- `supersede_assignment` `worker, session, native_id, assignment, assignment_hash, resource, superseded_by, replacement_local_id, replacement_hash, reason, commit?` + current user authorization — retire an ordinary GPU-allocation instruction the user has superseded. `commit:false` reviews; `commit:true` marks only that assignment `superseded` with an audit. Not completion; sends/cancels nothing.

### Queued input (exact items)
- `queued_input` `worker, message?` — list public inputs (pending, or one exact message) with `message`, `local_id`, `input_hash`, content and queue state. Paginates up to 20000 messages. Missing ≠ consumed/cancelled.
- `native_queue` `worker` — read the bound Codex native queue without resuming.
- `cancel_queued` (exact input) `worker, session, native_id, message, local_id, input_hash, reason` + current user authorization, plus either `disposition:"cancel"` (user explicitly cancels this item) or `superseded_by, replacement_hash` (a later consumed ordinary prompt supersedes it). Only a native `cancelled` ACK counts.
- `steer_queued` `worker, session, native_id, message, local_id, input_hash, reason (≥16 chars)` + current user authorization — promote an existing ordinary queued input; never a resend.

### Questions and approvals
- `requests` `worker|session` — pending approvals/questions with text and options.
- `answer` `worker|session, request, basis` + `answers:[[...],...]` (one list per question) or `decision:"approved"|"approved_for_session"|"denied"|"abort"`. `basis` must quote the user instruction or delegation.

### Observation, events, cadence
- `status` — everything: projects, workers, assignments, resources, all events, outbox, recoveries, handoff watches, reminders.
- `events` — unacked events. `ack` `ids:[...]` after handling.
- `check_status` / `digest` `worker` — read-only: watch policy, pending events, latest reply, progress snapshot, input delivery, native terminals/intent. Never prompts the child.
- `sessions` — HAPI sessions. `read` `session` — latest 100 messages. `read_native` `worker` — last 100 native records, no controller. `discover` `runtime` — native session metadata.
- `watch` `worker|project, mode:"events"|"quiet"|"off", note?` — `events` wakes you; `quiet` tracks without wakes (use `digest`); `off` stops scanning. `note` (user preference) shows on every wake.
- `reporting` `worker?, session? (guard; requires worker), enabled?, important?, status_changes?, routine_enabled?, semantic_updates?, fallback_minutes? (≥5), reset?, replace_reminder?` — read/set adaptive reporting. Default: status changes/important on, routine/semantic off, fallback 30 min. Setting `fallback_minutes` enables routine unless `routine_enabled:false`. `reset:true` drops a chat's overrides; `replace_reminder` migrates a same-cadence reminder. Mid-level managers: direct children only. `cadence_mode`/`cadence_instructions` are user-only (UI); managers cannot change a user-selected `fixed`/`off` cadence.
- `manager_reporting` — user/UI only (refused with an actor): a manager node's default cadence for its direct children.
- `remind` `in_minutes|at, every_minutes? (≥5), worker?, project?, note?, progress?, id?` — scheduled `check-in` wake (works for quiet chats). `progress:true` = one read-only progress schedule per worker; `id` updates an existing reminder without changing omitted timing.
- `reminders` `include_inactive?, id?, history?` — list schedules with delivery/backoff state and history. `cancel_reminder` `id`.

### Recovery and repair
- `resume_chat` `worker|session, reopen?` + current user authorization — clear that chat's Stop latch and, if inactive and not duplicated, reopen its controller (`reopen:false` clears only). Sends no input, resumes no goal, changes no holds/models. A pending earlier resume is reconciled, never duplicated.
- `reconcile_adopt` `worker, rebind?` — resolve a worker stuck `adopting` or misbound from evidence only; `rebind:true` repairs a deleted/inactive binding from exact live native metadata. Refuses running/ambiguous cases. Then `adopt` once.
- `reconcile_inactive` `worker, reason` — for an inactive controller with exact native identity and no live process: marks confirmed-sent assignments interrupted (never complete/replayed). Then `adopt`.
- `stop_cleared_controller` `worker, session` — archive an idle Claude controller whose binding was lost after `/clear` (exact `--resume` owner, last message `/clear`, nothing pending). Then reconcile/adopt the original history.
- `inspect_native_history` `worker` — read-only Codex first-page lineage check (`ready`/`blocked`).
- `recovery_process_review` **overseer** `worker, pid, container_id` — read-only review when a capacity/rate retry is held only by an apparently idle Distrobox monitor. `recovery_process_authorize` adds `digest, reviewed_at, review_token` + current user authorization: allows one retry attempt for that episode (5-minute expiry, lost on supervisor restart).

### Resources and GPUs
- `claim` / `release` `project, resource` — exclusive recorded ownership (`machine/GPU-UUID`). `release` refused while the project has unresolved assignments. Both refused for a personally reserved resource.
- `resource_handoff` `project, to_project, resource, acknowledgements:[{worker, session, native_id, message, quote}], incidental_clients?, commit?` — default read-only review (`ready`, `blockers`, `gpu`). `commit:true` + current user authorization re-probes and transfers atomically. Local `main/GPU-UUID` NVIDIA compute only.
- `resource_reservation` `resource` (`main/GPU-UUID`), `reserved?` + current user authorization to change — personal user reservation. Reserving removes the project lease; releasing does not restore it or start work.
- `gpu_unclaim` **overseer** `resource, project` → preview with `claim, reviewed_at, review_token`; resubmit those with `commit:true` + current user authorization to remove that exact claim row. Does not inspect or change GPU use.
- `handoff_watch` `key, source_worker, waiting_worker, resource` + `user_requested, basis` — register a future handoff dependency (idempotent; watch only). `{"op":"handoff_watch","key":K,"cancel":true}` cancels. A cancelled key never re-arms. `handoff_watches` lists (`waiting`, `review-required`, `cancelled`, `superseded`, `transferred`).
- `gpu` `action:"list"|"reserve"|"release"|"request", gpu, holder?, note?, force?, all?` — advisory GPU ledger by name (warnings, not blocks). Mid-level `reserve` becomes a `gpu-request` to the overseer.

### Node graph (see manager/NODES.md)
- `node_graph` — active graph with worker mapping, managed/asleep flags.
- `node_tree` `workspace?, summary?` — all workspaces (mid-level: own subtree).
- `node_edit` `workspace, version, changes[1..100], dryRun?` — actions `add, remove, layout, connect, disconnect, routing, annotation, rename` (mid-level: `layout, connect, disconnect, remove` below itself). Atomic, version-checked. Existing connections are preserved unless `user_requested:true` + `basis`.
- `node_spawn` `name, directory, runtime?, model?, reasoning_effort?, permission?, worktree?, objective?, text?, key?, owner?` — register + launch a child under you (or `owner` in your subtree) and optionally assign `text`.
- `node_edge` **overseer** (with an actor) `owner, child` — attach an existing chat under an owner.
- `node_detach` `child` + `user_requested, basis` — remove a child edge below you.
- `node_sync` — reconcile after UI edits.

### Worker-side and CLI helpers
- `background_intent` `process_id, intent:"review"|"independent"|"unknown", note? (≤500)` — run by a managed worker from its own session to annotate its own listed Codex native terminal. Annotation only.
- `vendor/hapi refresh-tools <HAPI-session-UUID> [--status] [--server NAME] [--tool NAME]...` (CLI) — inspect (`--status`) or refresh a live Codex thread's tool catalog without a turn. Reload ACK alone is not success; check the actual inventory.
- `python3 deploy/watch-remote-process.py --host H --pid P --identify`, then `--boot-id B --start-ticks T --interval 30 --timeout 21600` as a background command — bounded read-only watch of a detached remote process. Exit 0 = process gone (not success); 75 = unknown.
- `python3 deploy/run-dev-helper.py --timeout 600 -- CMD ARGS` — bounded dev driver (124 on deadline; reaps only its child).

## Behaviour the supervisor enforces

### Stops, holds, releases
- HAPI **Stop**/**Archive** set a durable per-chat pause latch (survives restarts). While the manager or the target worker is latched: no wakes, reconnects, assignments (even `user_requested`), answers, adoption or recovery. Events and reminders are kept for later.
- Only the user's Resume button or `resume_chat` under a *current* explicit user instruction clears a latch. Never base it on an event, an old assignment, standing autonomy or `user_requested` on an assignment. `resume`, `manage`, watch/reporting changes do not clear it.
- Clearing a latch sends nothing and does not resume a native goal; the user can then send, or `/goal resume` a paused Codex goal.
- Stop/abort does not kill detached jobs, subagents or consumed prompts; never promise it does or clear goals to simulate a pause.
- Project `pause` lets the current invocation finish and does not stop a native goal's internal loop; use `interrupt` (or the user's Stop) to end the turn.
- A project hold (`manual` flag) blocks automatic work; only `resume` clears it.
- Classic mode: if the user stops and restarts a managed chat themselves, the worker becomes `released` (no scans, assignments marked released). Node children stay managed. Chats the user opens in HAPI are never monitored without their request.
- `unmanage` is final until the user explicitly asks to restore management. Already-delivered wakes cannot be recalled; ignore them after release.

### Identity and delivery
- Delivery, invocation, model output and task completion are separate facts. A turn end or ready event is not task completion; unknown receipt is neither completion nor cancellation.
- Never replay unknown intake, completed work or uncertain launches/resumes. Reconcile with `assignment_delivery`, `queued_input`, `reconcile_*` first. After a confirmed reconciliation, reuse the original task key.
- Resume/adopt/recovery require exact native identity; a mismatch blocks. Process arguments alone do not prove identity (`/clear` removes the anchor). Never edit the supervisor database or native storage, delete `history_base`, replace native IDs or create a fresh thread to bypass a guard; restore missing lineage from a verified backup.
- A queued, unconsumed user instruction in the manager chat blocks automatic assignments until handled. Direct user input in a worker emits one `user-input` event per gap between assignments and cancels pending usage recovery: read that input and the worker's latest turn before assigning more. Queue/control consumption is not proof the running turn was steered.
- Peer messages (structured `source=peer`) are not human steering but remain real pending input. Never infer sender or permission from a text prefix, PID or matching text. Use `hapi ping-peer` for authorized coordination.
- Free-form worker text asking the user something is not auto-classified: read it and surface the question.
- Inactive/disconnected workers are reported, not restarted mid-assignment.

### Native goals
- Check native goal state before treating an adopted or idle chat as paused or done; Claude restores goals from history and may trigger one even after a read-only prompt.
- A worker saying "paused" does not pause its goal. `pause` does not cancel a goal loop.
- Never clear, replace, recreate or mark complete a goal yourself. `/goal clear` needs a separate explicit user request (preserve the objective/checkpoint first, then verify the goal is null). Never send it as a resource workaround.
- `create_goal` is not a restart; it is refused while an unfinished goal exists. `/goal resume` (via `assign`, `goal:false`) only under current user authorization, never to fix a label; then verify the goal ACK and real activity.
- Changing a goal objective does not steer the current turn; send the correction as a separate prompt.
- `native-goal-blocked` and `native-wait-loop` events report a blocked or looping goal; acknowledging is not recovery. Report a wait loop to the user promptly and ask whether to pause or clear.
- A "draft for approval" deliverable is finished when the verified draft exists; approval is a separate human step.

### Provider limits and transient failures
- Usage/quota limit messages are tentative. The supervisor probes the same chat at the stated reset + 60 s or after 20 minutes, whichever is sooner, backing off to every 2 hours while rejected; any real output or tool activity clears the block (`usage-cleared`). Only the first limit of a streak wakes you. It never switches accounts/models or opens fresh conversations; the overseer's own quota is probed the same way.
- 429/rate and capacity/502/503 failures get one checkpoint continuation in the same active conversation after 60 s, doubling to 30 min (±20% jitter; longer retry-after honored), backoff shared per provider and across a 2 h streak; reported on the first failure and every 4th. Only recent failures (≤10 min when first seen) arm it.
- Recovery is blocked by: Stop latches, holds, pause, release/watch off, pending user input or approvals, active inference/background work, changed identity or inactive controller. No recovery launches a controller or replays an assignment. An uncertain send is inspected, never resent.
- While recovery is pending, do not send a competing retry. If the user wants a sooner test, one `user_requested` continuation is the test.

### Resources and GPUs
- Claims and reservations coordinate agents; they are not OS isolation. Inspect real compute use, user reservations, release evidence and pending input before any handoff.
- Never clear a goal, invent completion, edit SQLite or unmanage to free a claim.
- `resource_handoff` evidence: an exact public worker reply (≤30 min old, no newer instruction) that actually relinquishes this GPU, for every managed source worker; all assignments confirmed invoked; no queued/indeterminate input. Do not nudge workers just to obtain it; work moving to another machine is fine.
- Only the verified desktop compositor is exempt. Chrome may coexist only via `incidental_clients` with all real identity fields from the review (`gpu.processes[].incidentalIdentity`), its reviewed purpose and current user authorization; every later handoff needs a fresh review.
- Queued input is real future input: queue absence, matching prose or a separately steered equivalent is not consumption proof. Never discard genuine input to satisfy a guard; cancel or steer only exact reviewed items under the user's current instruction. Don't read private reasoning.
- When the user changes an allocation, cancel the affected `handoff_watch` before sending new direction. New user instructions, binding changes, pauses or ownership changes invalidate a watch automatically.
- Latest user directions govern old hardware wording in goals; do not silently rewrite goals.
- Sessions outside management do not wait for manager GPU claims or the advisory ledger.

### Node mode
- Idle/completed nodes stay connected and sleep; do not `unmanage`/`node_detach` as completion cleanup. Removing an agent or connection needs `user_requested:true` and a `basis` quoting a current explicit user request.
- Events route to each child's direct owner. A transient graph-read failure does not drop ownership or resources.
- A peer ping or old handoff does not restore a removed edge or authorize oversight.

### Reporting
- Observation is read-only and makes no model calls; never prompt a worker for a status update. Idle, waiting, held or user-controlled chats produce no routine updates. Snapshots are observed evidence, not percent-complete or success.
- A user-selected `fixed`/`off` cadence is binding. On a `cadence-policy` event, read `reporting` for the instructions and choose `fallback_minutes`.
- Wakes wait until the manager is idle with no pending approval; unsent observations coalesce.
- Classic mode only: a finished idle chat is auto-unmanaged after terminal evidence and two idle checks, unless retention is `keep`, input is requested, or goal/background/assignments are unsettled. Undoing it needs explicit `manage`.

### Background work and processes
- A living process proves liveness, not progress; a vanished process or native terminal proves only that it is gone, not success. Remote detached jobs may be invisible.
- Verify jobs by handle/child PID plus process-start (and boot) identity, exit status and outputs; never by name, `pgrep -f` pattern, age, CPU use, saved PID, GPU utilization or stale prose.
- `HAPI_PROCESS_ROLE=helper` is only for persistent support infrastructure; never label renders, builds, real tests or user jobs as helpers. It is an accounting hint, not permission to stop anything.
- Never auto-kill unknown processes. Verify exact ownership before signalling one obsolete helper. Never wrap an existing user job to give it a timeout.
- Never restart running controllers just to load new features or tools.
- Runner repair: no `doctor clean`, broad `pkill`, worker restarts or replayed assignments. After an OOM, inspect controller/job state first; reattach rather than spawn if a controller may live; honor Stop latches.

### Browser
- Browser work uses `~/.local/bin/firefox-agent-browser start` then `fx_connect {port:2829}` (`gui` for a visible window), on the persistent isolated profile. Never the personal profile/2828, raw automation flags or a re-clone. One controller per browser; coordinate if busy; disconnect after work and `finish` only an unused headless instance.
- Saved logins are filled by Firefox; never read password values. Vault unlock, MFA or account choice need the user.
- Automation-rejected sign-in: `firefox-agent-browser login` after the user has saved drafts and closed the agent browser themselves. Never close a GUI automatically, spoof automation signals or weaken site security. Login does not authorize sending mail.
- Put this recipe in browser assignments; don't wake idle/paused workers just to deliver it.
