# Returning native model decisions

Use only names and argument schemas offered in the current request. Tool names
and schemas may change across native Codex versions and thread settings.

Function call example, when `exec_command` with these fields is offered:

```json
{
  "request_id": "req_from_next_model_request",
  "output_text": "I will run the relevant tests.",
  "tool_calls": [
    {
      "name": "exec_command",
      "kind": "function",
      "arguments": "{\"cmd\":\"python -m pytest tests/test_feature.py -q\",\"workdir\":\"/workspace/project\",\"max_output_tokens\":4000}"
    }
  ]
}
```

Freeform patch example, when `apply_patch` is offered as a custom tool:

```json
{
  "request_id": "req_from_next_model_request",
  "tool_calls": [
    {
      "name": "apply_patch",
      "kind": "custom",
      "input": "*** Begin Patch\n*** Add File: example.txt\n+hello\n*** End Patch"
    }
  ]
}
```

To answer a native compaction request, return a concise `output_text` summary of
the objective, constraints, completed work, important files, verification evidence,
open issues and next steps. Codex performs the actual history compaction.

To finish an ordinary native turn, return its final answer in `output_text` with
no tool calls. During a goal, use the offered native goal tools to update the
goal's actual state. A final message alone can leave an active goal running.

An identical repeated submission is idempotent. A changed answer for an already
answered request is rejected. If the native connection closed, resume the saved
thread and answer the new request; do not replay an uncertain previous operation.
