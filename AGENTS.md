# Public development checkout

This is the canonical application source repository. Commit code normally here; do not merge private historical repositories. Runtime state, native transcripts, browser profiles, inventories, logs and credentials must remain ignored/outside Git. Local deployment notes, if present, are in ignored state/LOCAL.md. Never copy them into public documentation.

Preserve running controllers, native conversation identities and explicit manual pauses. Observing or deploying a UI must not send worker prompts or resume tasks. Use supported management APIs, not direct database edits. Python validation: python3 -m unittest discover. The ordered HAPI patch stack is built and validated by deploy/build-hapi-web.sh; vendor/ is generated and ignored.
