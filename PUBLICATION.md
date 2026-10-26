# Public development history

This repository began with a clean, source-only snapshot. Private development history, runtime evidence, chat transcripts, account/usage records, machine inventories, databases, browser profiles and credentials were excluded. Initial fixture identifiers and machine-specific examples are synthetic.

This is now the canonical development repository. Commit reviewed source changes normally; do not merge or force-push private archive history into it. Runtime data belongs outside the checkout, accessed through ignored paths where needed. Local deployment configuration is ignored state/local-config.json; local operator notes are ignored state/LOCAL.md. Never add these files to Git.

Before pushing, scan staged changes/history for secrets and check for personal or operational data that scanners cannot recognize. Private historical repositories remain archival only. A clean scan is not a guarantee that arbitrary future files are safe.
