# Architecture

The HAPI hub owns the web-visible session graph; node_graph.py validates and projects oversight into the durable supervisor. coordination.py handles observation, reminders and handoff dependencies. assignment_delivery.py and queued_input.py distinguish stored input from confirmed invocation. transient_recovery.py provides guarded provider-failure recovery.

Keep runtime records out of Git. The Session Manager list is distinct from graph ownership: dismissing a list entry is not deleting history, and releasing management is not stopping a conversation. Explicit manual pauses override automatic continuation. Unknown dispatch outcomes require inspection rather than retry.

See README.md, TOOLS.md, manager/NODES.md and the regression tests for setup and supported operations. The private development diary and live evidence are intentionally not published.
