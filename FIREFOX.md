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
