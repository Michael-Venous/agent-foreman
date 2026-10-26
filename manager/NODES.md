# Node graph management

Each workspace has an overseer root. Directed ownership edges define the sessions a node may oversee; junctions organize connections, while frames and text organize the view. Frame membership and visual layout must not change conversation history or restart work.

The hub is authoritative for graph state. The supervisor validates unique ownership and cycles before projecting oversight. A parent operates only within its subtree. Read-only node_tree observation must not send prompts or resume controllers. Inspect queued-versus-invoked delivery before assuming a user correction reached a busy turn.

Idle/completed nodes remain connected until an authorized graph edit removes them. Idle edges can sleep without routine polling. Explicitly paused nodes and owners stay paused. A transient graph-read failure must not revert to classic management, drop ownership, or release resources.

Use managerctl.py with the supported node_tree, node_edit and node_graph controls documented in ../TOOLS.md. Runtime graphs, identities and assignments belong under ignored state/, not in this source tree.

All browser nodes inherit the agent launch default: `~/.local/bin/firefox-agent-browser start`, then `fx_connect {port:2829}`. A visible agent window uses the same helper's `gui` mode. Do not substitute personal Firefox or overwrite an existing clone. Propagate this during natural task handoffs; a configuration update is not permission to restart, interrupt or unpause a node.
