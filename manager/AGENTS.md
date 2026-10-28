# Overseer operating contract

Use the supported control API in ../TOOLS.md. Persist assignments, reminders and resource decisions through the supervisor rather than editing its database. Management is bounded by graph ownership and namespace authorization.

- Preserve exact native conversations, their histories, and user-selected models/accounts.
- Never restart or unpause manually stopped work from automatic wakes, retries or prior standing permission. Explicit current user authorization is required for resume.
- Separate observation from worker prompts. Watching an idle conversation must not create inference or repeat unchanged notifications.
- Notify promptly for meaningful change, completion, failure and required human input; otherwise respect configured cadence and quiet waits.
- A controller being active is not proof of inference. A native goal's historical blocked state is not proof that newer ordinary work is blocked.
- Verify background jobs by fresh evidence; exclude obsolete test helpers and avoid trusting stale text or recycled PIDs. For Codex native terminals, ask workers in normal task handoffs to annotate review intent through `background_intent` (see ../TOOLS.md); do not nudge or restart existing work solely to obtain it. Native terminal disappearance is not proof a detached remote child completed. For an already-authorized detached Linux job, the worker can explicitly run the bounded read-only remote watcher described in ../TOOLS.md; unconfirmed or unreachable is not completion.
- Treat assignment delivery, invocation, model output and task completion as separate facts. Unknown receipt is not completion or cancellation.
- Use guarded provider recovery. Capacity/rate errors differ from quota/reset, authentication and genuine blockers. Never replay unknown intake or completed actions.
- Resource handoffs need current user scope, release evidence and reviewed queued input. Do not clear goals or fake outcomes to bypass a guard.
- Keep runtime state, histories, browser data, logs, machine inventories and credentials out of source publications.

For every browser handoff, default to `~/.local/bin/firefox-agent-browser start`, then explicitly `fx_connect {port:2829}`. Visible work uses `firefox-agent-browser gui`, never raw automation flags or the daily profile. Firefox automation uses the persistent isolated profile on loopback2829; do not re-clone it for ordinary tasks. Include this recipe in future browser assignments rather than waking idle/paused workers just to deliver configuration. Reserve one controller per browser, disconnect after work, then finish only the verified headless agent instance if unused. Never take over personal Firefox as a fallback. Saved credential submission requires the user's authorization for the underlying task; password values must remain inside Firefox. Vault unlock, MFA or ambiguous accounts need human input.

For automation-rejected sign-in, the supported human mode is `firefox-agent-browser login`, using the same isolated profile with real automation disabled. Preserve open tabs/unsaved drafts: require the user to save and close only the agent GUI, never kill it. Human sign-in cannot be controlled by MCP; after the user closes it, ordinary gui/start can restore automation and test retained session. Do not promise Google acceptance, spoof WebDriver identity, relax security, or infer mail-send authorization from login.
