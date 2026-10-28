# Administrative GPU claim removal

`gpu_unclaim` removes one exact recorded GPU claim under a current explicit user request. It does not inspect or change GPU activity, stop processes, infer idle or completion, change assignments or goals, or wake a chat. An unclaimed GPU can still be in use; removing a claim is not permission to start competing work.

First request a read-only preview with the exact resource and owning project:

```json
{"op":"gpu_unclaim","resource":"main/GPU-UUID","project":"EXACT_PROJECT_ID"}
```

For the same current user instruction, submit the same `resource` and `project`, together with the preview's unchanged `claim`, `reviewed_at` and `review_token`, plus `commit:true`, `user_requested:true`, and a `basis` quoting the user request. `managerctl` supplies a fresh submission timestamp. Only the overseer may execute this when an actor is present. Preview and authorization expire after five minutes and the preview does not survive a supervisor restart. Inspect current status after a timeout before considering another request.

The supervisor atomically rechecks the exact row and its persistent mutation generation, removes that row, and writes a durable `gpu-unclaim-audit:` record. A changed owner, delete/recreate, different resource, stale or modified preview, personal reservation, or missing current authorization is refused. Non-GPU resources, goals, jobs, workers, pauses, and assignment outcomes are not modified. Existing `release` and evidence-based `resource_handoff` retain their guards. This operation is only available after its supervisor code and schema migration are deployed; a source commit alone does not activate it.
