# Agent Foreman

A self-hosted session manager and node-graph interface for Codex and Claude, built on [HAPI](https://github.com/tiann/hapi). A durable Python supervisor coordinates existing conversations while HAPI supplies chat, streaming, controller and web interfaces.

## Features

- Multiple conversations, folders, pinning and a persistent Session Manager.
- Movable node graphs with frames, junctions, annotations and nested oversight.
- Distinct running, background, waiting and failed states with inspectable evidence.
- Queue, steer and interrupt controls, with explicit manual-pause preservation.
- Durable assignments, reminders, dependency watches and resource-handoff review.
- Guarded transient-error recovery with backoff, deduplication and uncertain-intake protection.
- Separate cloned Firefox profiles for agent work through native Marionette.

## Status and requirements

This is a Linux-oriented development snapshot, not a turnkey hosted service. It requires Python 3, Bun, Git, and configured Codex/Claude CLI accounts for the providers you use. Firefox automation is optional. The scripts apply an ordered patch stack to HAPI v0.30.7; sample systemd units require local configuration. No accounts, browser data, credentials or native conversation history are included.

## Development

```sh
git clone https://github.com/Michael-Venous/agent-foreman.git
cd agent-foreman
python3 -m unittest discover
```

The application state belongs under `state/`, HAPI state under `~/.hapi/`, and native provider histories in their own CLI stores. All are private and ignored by Git. The supervisor reads its local HAPI credential from `~/.hapi/settings.json`; never commit this file or expose an unprotected local hub.

For the patched UI, install Bun and run `bash deploy/build-hapi-web.sh`; it clones HAPI v0.30.7 under ignored `vendor/`, installs dependencies, applies patches, validates and builds the web bundle. `deploy/build-hapi-cli.sh` builds the matching CLI after the web patch stack is present. Set `VITE_MANAGER_SESSION_ID` to your own overseer session before building. Review the build scripts and all local paths before deployment.

Use the templates in `deploy/` to configure the hub, runner and Python supervisor as user services. Set checkout/executable paths and the local machine/catalog binding for your environment before enabling them. Service installation is intentionally not automatic. A running provider CLI may hold work or background jobs: don't restart controllers just to update web code.

## Management and safety

See [TOOLS.md](TOOLS.md) for the control API and [manager/NODES.md](manager/NODES.md) for graph ownership. A management edge does not grant new account permissions. Read-only observation must not send worker prompts or resume paused work. Keep explicit user pauses until a fresh authorized resume; unknown delivery must be reconciled rather than replayed. Resource locks are advisory and must be checked against actual device use.

[Firefox setup](FIREFOX.md) uses a separate browser profile and port2829 so personal browsing can continue independently. Profiles still run under the same OS user; this is not filesystem sandboxing.

## Source and licensing

This is a clean source-only publication. The original private operational history, runtime evidence, accounts and machine inventories are excluded; see [PUBLICATION.md](PUBLICATION.md). Future releases must preserve this boundary rather than merge private history.

HAPI is distributed under the GNU Affero General Public License v3; this distribution includes its modified-source patches and preserves that license in [LICENSE](LICENSE). Upstream source is fetched from the pinned HAPI release by the build scripts. No proprietary provider binaries, accounts or browser profiles are bundled.
