# Isolated Firefox automation

Use the installed Firefox binary with a separate profile/process, not the personal profile. Native Marionette serves loopback2829; Playwright is not required. Firefox supports one active Marionette controller per instance. Coordinate ownership and never replace a busy controller.

## Default for already-configured agents

Run `~/.local/bin/firefox-agent-browser start`, then explicitly `fx_connect {port:2829}`. For visible work, use `~/.local/bin/firefox-agent-browser gui` with the same isolated profile. Do not launch raw `firefox --marionette`, attach to personal2828, or use a fresh profile as an automatic fallback. If busy, coordinate; never replace its controller. Leave an existing legacy controller alone until its current task finishes. The clone persists: the setup steps below are not repeated for each task.

## Initial setup

1. Choose your normal profile from about:profiles. With Firefox closed, a complete profile copy is simplest; when it remains open, the supplied cloner uses per-database snapshots but cannot promise a transaction across all site files.
2. Run `python3 deploy/firefox/clone-profile.py --source /path/to/your/profile`. The destination is `~/.local/share/agent-foreman/firefox/profile`; an existing destination is never overwritten. No password values are decoded. Encrypted logins, cookies, extensions and site storage are copied once; Firefox Sync/session restoration/runtime locks are omitted. Some sites and vaults require sign-in or unlocking again.
3. Review and install with `python3 deploy/firefox/install-launcher.py`, then `systemctl --user daemon-reload`. Templates target Linux/systemd and may require platform-specific path adjustments. The service is on-demand, not enabled at boot.
4. Set `FX_MARIONETTE_PORT=2829` in your Firefox MCP registration. Register the native Marionette server separately; see deploy/firefox/marionette-local.patch for the expected upstream integration. Existing MCP processes need reload or an explicit `fx_connect {port:2829}`.
5. Start headless with `firefox-agent-browser start`, or use the Firefox — Agents (isolated) shortcut for visible work. Connect through MCP, perform authorized actions, and call `fx_disconnect` when done. Then use `firefox-agent-browser finish` to stop only the verified unused headless service. Graphical/personal Firefox is never automatically stopped.

The clone is same-user profile isolation, not an OS sandbox. Site actions still affect the signed-in online account. Profile copies and private configuration backups contain sensitive access data and must remain outside Git. Browser automation can be detected or refused by websites. No passwords should be extracted or logged; MFA and vault unlocking may require a human.

The cloner includes committed SQLite WAL state, checks copied database integrity where system SQLite supports Firefox's collations, and uses stable DB/WAL/journal group copies for exclusive locks. A changing encrypted credential group aborts publication. Custom-collation checks are explicitly counted as unavailable, not silently claimed successful. Add-on and NSS metadata paths are rewritten to avoid source-profile file sharing.

## Human sign-in when a site rejects automation

`firefox-agent-browser login [URL]` opens the **same persistent isolated profile** with Marionette genuinely disabled and without automation/headless flags. The installer also provides **Firefox — Agents (manual sign-in)**. It is for human interaction: MCP cannot control that instance. This does not disguise WebDriver, bypass account checks, modify user-agent identity, or relax TLS/cookie/account security settings.

1. In the existing isolated agent window, confirm drafts are saved and preserve any unsaved form text yourself. Quit only that agent Firefox normally. **The launcher will not close a GUI for you.** A disconnected controller alone is not enough: the browser must release its profile lock. Personal Firefox stays open.
2. Run `~/.local/bin/firefox-agent-browser login` (optionally provide the site's normal sign-in URL), or use the manual sign-in shortcut. The command refuses an open/locked profile or running agent service without changing settings. Sign in manually, including any MFA/vault unlock required by the site.
3. Save work, then close the manual agent window yourself. Run `~/.local/bin/firefox-agent-browser gui` for ordinary visible automation, or `start` for headless work, then connect `fx_connect {port:2829}`.
4. Verify the site still recognizes its session before continuing an authorized task. If it requests sign-in or rejects automation again, stop and report it; do not keep retrying, spoof signals or weaken security. Browser session retention does not authorize sending email or any other account action.

Mode changes edit only the agent profile's startup preferences after closed-profile checks. They retain the real `marionette.enabled` preference for the selected mode, keep port2829, and request restoring the previous session. Cookies/site storage/login files are not replaced. A private backup of the prior `user.js` is kept inside the profile. Human-mode launch strips inherited Marionette/headless controls. Normal automated launches re-enable real automation. Unsaved drafts/form state cannot be guaranteed across any restart; save and verify them before quitting.

Google explicitly lists software-automated browsers among sign-in restrictions: [supported-browser guidance](https://support.google.com/accounts/answer/7675428?hl=en). Mozilla documents the exposed automation signal: [`navigator.webdriver`](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/webdriver). These explain a plausible cause of rejection, not a guarantee that Google will accept manual sign-in or a later automated session. Extensions, account policy and other risk checks can also matter.

Profile-lock safety: a leftover lock symlink is ignored only when it names a loopback address and a conclusively absent PID, the existing advisory lock can be acquired, no process uses the exact isolated profile, and ownership remains unchanged on recheck. Live PIDs (including reused PIDs), remote/malformed owners, inspection failures or missing advisory evidence remain blocked. The launcher never deletes profile locks.
