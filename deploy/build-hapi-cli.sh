#!/usr/bin/env bash
# Build/install CLI-side patches after build-hapi-web.sh has applied the patch stack.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE="$ROOT/vendor/hapi-source"
BUN="${BUN_BIN:-$HOME/.bun/bin/bun}"
cd "$SOURCE"
if [[ ! -f shared/tools/tunwg/tunwg-x64-linux ]]; then
    curl -fL --silent --show-error https://github.com/tiann/tunwg/releases/latest/download/tunwg -o shared/tools/tunwg/tunwg-x64-linux
fi
cd cli
"$BUN" run typecheck
"$BUN" run test --maxWorkers=2 src/persistence.test.ts src/utils/process.test.ts src/codex/shared/registry.test.ts src/runner/sessionScope.test.ts src/commands/runCli.test.ts src/runner/buildCliArgs.test.ts src/runner/controlClient.test.ts src/codex/shared/refreshTools.test.ts src/commands/refreshTools.test.ts src/commands/registry.test.ts src/api/apiSession.goalAck.test.ts src/codex/shared/queue.test.ts src/codex/shared/root.test.ts src/modules/pingPeer/pingPeer.test.ts src/claude/utils/startHappyServer.test.ts src/codex/utils/manualPause.test.ts src/modules/common/remote/RemoteLauncherBase.test.ts src/claude/textStreaming.test.ts src/codex/utils/appServerEventConverter.test.ts src/claude/readyMessages.test.ts src/claude/priorityInput.test.ts src/claude/sdk/query.test.ts src/claude/claudeRemote.test.ts src/claude/claudeRemoteLauncher.test.ts src/claude/claudeRemoteLauncher.modeGate.test.ts
"$BUN" scripts/build-executable.ts --outdir "$ROOT/state/cli-build"
"$ROOT/state/cli-build/bun-linux-x64-baseline/hapi" --version
cp "$ROOT/vendor/hapi" "$ROOT/state/hapi-before-context-patch"
cp "$ROOT/state/cli-build/bun-linux-x64-baseline/hapi" "$ROOT/vendor/hapi-next"
chmod +x "$ROOT/vendor/hapi-next"
mv "$ROOT/vendor/hapi-next" "$ROOT/vendor/hapi"
# Existing controllers keep their loaded executable. New/resumed controllers use this build.
