# Outcome calibration pilot

Observed: 2026-10-26T20:36:45.909222+00:00

Selected 30 explicit tasks across 13 session IDs. Classifier outcome changed for 28 tasks.

Old outcomes are reconstructed from v5 invocation semantics. These are not freshly read stored labels.
The table is a classifier transition matrix. Independent human acceptance remains unknown; it is not a ground-truth accuracy claim.

| Old classifier | New classifier | Tasks | Independent acceptance |
|---|---|---:|---|
| partial | partial | 1 | unknown |
| pushed | committed | 2 | unknown |
| pushed | partial | 5 | unknown |
| shipped | committed | 16 | unknown |
| shipped | partial | 2 | unknown |
| shipped | pushed | 3 | unknown |
| shipped | shipped | 1 | unknown |

## Per-task evidence

Evidence refs use exact transcript paths and parsed record numbers; no transcript text is exported.

| Task | Old | New | Reported task result | Operation coverage | Acceptance |
|---|---|---|---|---|---|
| t-6828438ec7 | shipped | committed | DONE | complete-results | unknown |
| t-4baf4d012c | shipped | committed | DONE | complete-results | unknown |
| t-5b6fba4a48 | shipped | committed | DONE | complete-results | unknown |
| t-f9011ee454 | shipped | committed | DONE | complete-results | unknown |
| t-78566f566d | pushed | partial | DONE | partial-results | unknown |
| t-3ecee3ed90 | shipped | committed | DONE | complete-results | unknown |
| t-d41597a07f | pushed | partial | DONE | partial-results | unknown |
| t-dcaf146f1d | shipped | committed | DONE | complete-results | unknown |
| t-c2b503785b | pushed | partial | PARTIAL | partial-results | unknown |
| t-d1643614af | pushed | partial | DONE | partial-results | unknown |
| t-0dd2690085 | pushed | partial | DONE | partial-results | unknown |
| t-c62186076e | partial | partial | DONE | no-delivery-operations | unknown |
| t-a066717b59 | shipped | pushed | DONE | complete-results | unknown |
| t-d94034e47f | shipped | pushed | DONE | complete-results | unknown |
| t-225c786cb4 | shipped | pushed | PARTIAL | complete-results | unknown |
| t-38595a057c | shipped | committed | DONE | complete-results | unknown |
| t-b20538b767 | shipped | committed | DONE | complete-results | unknown |
| t-f12be3fbf6 | shipped | committed | DONE | complete-results | unknown |
| t-49325bbfd0 | shipped | committed | DONE | partial-results | unknown |
| t-4febcee68a | shipped | committed | DONE | partial-results | unknown |
| t-a5ab636b13 | shipped | committed | DONE | complete-results | unknown |
| t-50d395594c | shipped | committed | PARTIAL | partial-results | unknown |
| t-daed83de4e | shipped | partial | DONE | complete-results | unknown |
| t-356e418522 | shipped | shipped | DONE | complete-results | unknown |
| t-ce3012bcae | shipped | committed | DONE | complete-results | unknown |
| t-a130a2228b | shipped | committed | DONE | complete-results | unknown |
| t-3381c514d1 | shipped | partial | DONE | partial-results | unknown |
| t-77f6662e0f | shipped | committed | DONE | complete-results | unknown |
| t-054c274b4e | pushed | committed | DONE | complete-results | unknown |
| t-53121da53e | pushed | committed | DONE | complete-results | unknown |

## Machine-readable evidence and cohort sensitivity

```json
{
  "acceptance_matrix": [
    {
      "new": "partial",
      "old": "partial",
      "tasks": 1,
      "truth": "unknown"
    },
    {
      "new": "committed",
      "old": "pushed",
      "tasks": 2,
      "truth": "unknown"
    },
    {
      "new": "partial",
      "old": "pushed",
      "tasks": 5,
      "truth": "unknown"
    },
    {
      "new": "committed",
      "old": "shipped",
      "tasks": 16,
      "truth": "unknown"
    },
    {
      "new": "partial",
      "old": "shipped",
      "tasks": 2,
      "truth": "unknown"
    },
    {
      "new": "pushed",
      "old": "shipped",
      "tasks": 3,
      "truth": "unknown"
    },
    {
      "new": "shipped",
      "old": "shipped",
      "tasks": 1,
      "truth": "unknown"
    }
  ],
  "changed_tasks": 28,
  "format": "outcome-calibration/1",
  "limit": 30,
  "limitations": [
    "Old outcomes are reconstructed v5 classifier outputs, not a fresh database snapshot",
    "Transition matrix is not a human-ground-truth confusion matrix",
    "Task DONE and integration_first_pass are reported claims, not independent acceptance",
    "Skill sensitivity is this exact transcript cohort, not the full production ranking"
  ],
  "measurement": {
    "copied_history_excluded": true,
    "database_calls": 0,
    "elapsed_seconds": 2.157,
    "new_model_calls": 0
  },
  "observed_at": "2026-10-26T20:36:45.909222+00:00",
  "rows": [
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "ee7f58ff04e00bcd857d63de",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 5,
        "denied_push_main": 1,
        "edit": 10,
        "unknown_deploy": 3,
        "unknown_pr_create": 7,
        "unknown_pr_merge": 7,
        "unknown_push": 1,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 308,
          "denied": true,
          "operations": [
            "push_main"
          ],
          "result_line": 311,
          "tool_error": true
        },
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 313,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 314,
          "tool_error": true
        },
        {
          "call_id": "toolu_01YRaRve8qsLwfGWxbhom5SJ",
          "call_line": 341,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 345,
          "tool_error": false
        },
        {
          "call_id": "toolu_01M49td1hiV2CJc9WYVWzxHS",
          "call_line": 1229,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1233,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lzpj4xLxVP35XsF4J5pEL2",
          "call_line": 1490,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1495,
          "tool_error": false
        },
        {
          "call_id": "toolu_01BQ84gA1y9AcDzDpBUAFaCv",
          "call_line": 1508,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1512,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SjdpyPfGoGLTUNjkB7A6b9",
          "call_line": 1551,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 1553,
          "tool_error": false
        },
        {
          "call_id": "toolu_01C3CLh4HzRSju7dpQjp2fej",
          "call_line": 1687,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1691,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FRko7uTZ2Napy4ngrJUHJL",
          "call_line": 1697,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WkY7MNUefkyaPMw6F7bTzv",
          "call_line": 2295,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 2297,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VvVtDnVvByDnqgAvtyoSDs",
          "call_line": 2685,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2690,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N8D4ABGDDDPpu1BWVzb9H9",
          "call_line": 2696,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 2699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3011,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3022,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3025,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3026,
          "tool_error": false
        },
        {
          "call_id": "toolu_015VzjmrFstLHNo8bfWQPiwQ",
          "call_line": 3586,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3597,
          "tool_error": false
        },
        {
          "call_id": "toolu_014QdY9MNYyew7n8piRWuXfq",
          "call_line": 3603,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 3605,
          "tool_error": false
        },
        {
          "call_id": "toolu_0185iacfCfy23MJh7D93tZgr",
          "call_line": 4219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 4231,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EzT2LVCPKG4eKMg1KVMQew",
          "call_line": 5684,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 5696,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WnkMDno8Nush5VQrTVJC5z",
          "call_line": 5775,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 5779,
          "tool_error": false
        },
        {
          "call_id": "toolu_01UyVWsuE13KkofZC41KHmQW",
          "call_line": 6455,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 6467,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VaX67Qq4oU5wCdD3UDCnwv",
          "call_line": 6473,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 6475,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "fa013987-e17b-4aa7-83e9-deadad994bbb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-6828438ec7",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/fa013987-e17b-4aa7-83e9-deadad994bbb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "c6e59c6da143a6d1aed64e45",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 5,
        "denied_push_main": 1,
        "edit": 10,
        "unknown_deploy": 3,
        "unknown_pr_create": 7,
        "unknown_pr_merge": 7,
        "unknown_push": 1,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 308,
          "denied": true,
          "operations": [
            "push_main"
          ],
          "result_line": 311,
          "tool_error": true
        },
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 313,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 314,
          "tool_error": true
        },
        {
          "call_id": "toolu_01YRaRve8qsLwfGWxbhom5SJ",
          "call_line": 341,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 345,
          "tool_error": false
        },
        {
          "call_id": "toolu_01M49td1hiV2CJc9WYVWzxHS",
          "call_line": 1229,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1233,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lzpj4xLxVP35XsF4J5pEL2",
          "call_line": 1490,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1495,
          "tool_error": false
        },
        {
          "call_id": "toolu_01BQ84gA1y9AcDzDpBUAFaCv",
          "call_line": 1508,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1512,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SjdpyPfGoGLTUNjkB7A6b9",
          "call_line": 1551,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 1553,
          "tool_error": false
        },
        {
          "call_id": "toolu_01C3CLh4HzRSju7dpQjp2fej",
          "call_line": 1687,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1691,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FRko7uTZ2Napy4ngrJUHJL",
          "call_line": 1697,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WkY7MNUefkyaPMw6F7bTzv",
          "call_line": 2295,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 2297,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VvVtDnVvByDnqgAvtyoSDs",
          "call_line": 2685,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2690,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N8D4ABGDDDPpu1BWVzb9H9",
          "call_line": 2696,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 2699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3011,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3022,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3025,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3026,
          "tool_error": false
        },
        {
          "call_id": "toolu_015VzjmrFstLHNo8bfWQPiwQ",
          "call_line": 3586,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3597,
          "tool_error": false
        },
        {
          "call_id": "toolu_014QdY9MNYyew7n8piRWuXfq",
          "call_line": 3603,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 3605,
          "tool_error": false
        },
        {
          "call_id": "toolu_0185iacfCfy23MJh7D93tZgr",
          "call_line": 4219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 4231,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EzT2LVCPKG4eKMg1KVMQew",
          "call_line": 5684,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 5696,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WnkMDno8Nush5VQrTVJC5z",
          "call_line": 5775,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 5779,
          "tool_error": false
        },
        {
          "call_id": "toolu_01UyVWsuE13KkofZC41KHmQW",
          "call_line": 6455,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 6467,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VaX67Qq4oU5wCdD3UDCnwv",
          "call_line": 6473,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 6475,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "fa013987-e17b-4aa7-83e9-deadad994bbb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-4baf4d012c",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/fa013987-e17b-4aa7-83e9-deadad994bbb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "f4a6de6469dd0e9774404999",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 5,
        "denied_push_main": 1,
        "edit": 10,
        "unknown_deploy": 3,
        "unknown_pr_create": 7,
        "unknown_pr_merge": 7,
        "unknown_push": 1,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 308,
          "denied": true,
          "operations": [
            "push_main"
          ],
          "result_line": 311,
          "tool_error": true
        },
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 313,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 314,
          "tool_error": true
        },
        {
          "call_id": "toolu_01YRaRve8qsLwfGWxbhom5SJ",
          "call_line": 341,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 345,
          "tool_error": false
        },
        {
          "call_id": "toolu_01M49td1hiV2CJc9WYVWzxHS",
          "call_line": 1229,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1233,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lzpj4xLxVP35XsF4J5pEL2",
          "call_line": 1490,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1495,
          "tool_error": false
        },
        {
          "call_id": "toolu_01BQ84gA1y9AcDzDpBUAFaCv",
          "call_line": 1508,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1512,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SjdpyPfGoGLTUNjkB7A6b9",
          "call_line": 1551,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 1553,
          "tool_error": false
        },
        {
          "call_id": "toolu_01C3CLh4HzRSju7dpQjp2fej",
          "call_line": 1687,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1691,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FRko7uTZ2Napy4ngrJUHJL",
          "call_line": 1697,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WkY7MNUefkyaPMw6F7bTzv",
          "call_line": 2295,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 2297,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VvVtDnVvByDnqgAvtyoSDs",
          "call_line": 2685,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2690,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N8D4ABGDDDPpu1BWVzb9H9",
          "call_line": 2696,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 2699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3011,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3022,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3025,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3026,
          "tool_error": false
        },
        {
          "call_id": "toolu_015VzjmrFstLHNo8bfWQPiwQ",
          "call_line": 3586,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3597,
          "tool_error": false
        },
        {
          "call_id": "toolu_014QdY9MNYyew7n8piRWuXfq",
          "call_line": 3603,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 3605,
          "tool_error": false
        },
        {
          "call_id": "toolu_0185iacfCfy23MJh7D93tZgr",
          "call_line": 4219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 4231,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EzT2LVCPKG4eKMg1KVMQew",
          "call_line": 5684,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 5696,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WnkMDno8Nush5VQrTVJC5z",
          "call_line": 5775,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 5779,
          "tool_error": false
        },
        {
          "call_id": "toolu_01UyVWsuE13KkofZC41KHmQW",
          "call_line": 6455,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 6467,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VaX67Qq4oU5wCdD3UDCnwv",
          "call_line": 6473,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 6475,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "fa013987-e17b-4aa7-83e9-deadad994bbb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-5b6fba4a48",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/fa013987-e17b-4aa7-83e9-deadad994bbb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "e9293cfaac8baebb4a490914",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 5,
        "denied_push_main": 1,
        "edit": 10,
        "unknown_deploy": 3,
        "unknown_pr_create": 7,
        "unknown_pr_merge": 7,
        "unknown_push": 1,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 308,
          "denied": true,
          "operations": [
            "push_main"
          ],
          "result_line": 311,
          "tool_error": true
        },
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 313,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 314,
          "tool_error": true
        },
        {
          "call_id": "toolu_01YRaRve8qsLwfGWxbhom5SJ",
          "call_line": 341,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 345,
          "tool_error": false
        },
        {
          "call_id": "toolu_01M49td1hiV2CJc9WYVWzxHS",
          "call_line": 1229,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1233,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lzpj4xLxVP35XsF4J5pEL2",
          "call_line": 1490,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1495,
          "tool_error": false
        },
        {
          "call_id": "toolu_01BQ84gA1y9AcDzDpBUAFaCv",
          "call_line": 1508,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1512,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SjdpyPfGoGLTUNjkB7A6b9",
          "call_line": 1551,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 1553,
          "tool_error": false
        },
        {
          "call_id": "toolu_01C3CLh4HzRSju7dpQjp2fej",
          "call_line": 1687,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1691,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FRko7uTZ2Napy4ngrJUHJL",
          "call_line": 1697,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WkY7MNUefkyaPMw6F7bTzv",
          "call_line": 2295,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 2297,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VvVtDnVvByDnqgAvtyoSDs",
          "call_line": 2685,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2690,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N8D4ABGDDDPpu1BWVzb9H9",
          "call_line": 2696,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 2699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3011,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3022,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3025,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3026,
          "tool_error": false
        },
        {
          "call_id": "toolu_015VzjmrFstLHNo8bfWQPiwQ",
          "call_line": 3586,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3597,
          "tool_error": false
        },
        {
          "call_id": "toolu_014QdY9MNYyew7n8piRWuXfq",
          "call_line": 3603,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 3605,
          "tool_error": false
        },
        {
          "call_id": "toolu_0185iacfCfy23MJh7D93tZgr",
          "call_line": 4219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 4231,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EzT2LVCPKG4eKMg1KVMQew",
          "call_line": 5684,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 5696,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WnkMDno8Nush5VQrTVJC5z",
          "call_line": 5775,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 5779,
          "tool_error": false
        },
        {
          "call_id": "toolu_01UyVWsuE13KkofZC41KHmQW",
          "call_line": 6455,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 6467,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VaX67Qq4oU5wCdD3UDCnwv",
          "call_line": 6473,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 6475,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "fa013987-e17b-4aa7-83e9-deadad994bbb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-f9011ee454",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/fa013987-e17b-4aa7-83e9-deadad994bbb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "a35f92278864c66ee760e8d0",
      "changed": true,
      "copied_records_excluded": 439,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 24,
        "unknown_pr_create": 4,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1651,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1661,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1664,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1665,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DMr7FovjGDZrVZxKvsXkh1",
          "call_line": 2943,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2947,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WAeRgvby12pTUrMHsvDiiG",
          "call_line": 3516,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3520,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PKMM7WTM6bTnDk1ePhkHLx",
          "call_line": 5232,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5236,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5373,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 5375,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5378,
          "operations": [
            "push"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "a75c1bed-3da5-4966-928f-d6ba34011911",
      "skills_observed": [],
      "task_id": "t-78566f566d",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/a75c1bed-3da5-4966-928f-d6ba34011911.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "47a67e89b4226e2719fd11e3",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 5,
        "denied_push_main": 1,
        "edit": 10,
        "unknown_deploy": 3,
        "unknown_pr_create": 7,
        "unknown_pr_merge": 7,
        "unknown_push": 1,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 308,
          "denied": true,
          "operations": [
            "push_main"
          ],
          "result_line": 311,
          "tool_error": true
        },
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 313,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 314,
          "tool_error": true
        },
        {
          "call_id": "toolu_01YRaRve8qsLwfGWxbhom5SJ",
          "call_line": 341,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 345,
          "tool_error": false
        },
        {
          "call_id": "toolu_01M49td1hiV2CJc9WYVWzxHS",
          "call_line": 1229,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1233,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lzpj4xLxVP35XsF4J5pEL2",
          "call_line": 1490,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1495,
          "tool_error": false
        },
        {
          "call_id": "toolu_01BQ84gA1y9AcDzDpBUAFaCv",
          "call_line": 1508,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1512,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SjdpyPfGoGLTUNjkB7A6b9",
          "call_line": 1551,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 1553,
          "tool_error": false
        },
        {
          "call_id": "toolu_01C3CLh4HzRSju7dpQjp2fej",
          "call_line": 1687,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1691,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FRko7uTZ2Napy4ngrJUHJL",
          "call_line": 1697,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WkY7MNUefkyaPMw6F7bTzv",
          "call_line": 2295,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 2297,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VvVtDnVvByDnqgAvtyoSDs",
          "call_line": 2685,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2690,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N8D4ABGDDDPpu1BWVzb9H9",
          "call_line": 2696,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 2699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3011,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3022,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3025,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3026,
          "tool_error": false
        },
        {
          "call_id": "toolu_015VzjmrFstLHNo8bfWQPiwQ",
          "call_line": 3586,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3597,
          "tool_error": false
        },
        {
          "call_id": "toolu_014QdY9MNYyew7n8piRWuXfq",
          "call_line": 3603,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 3605,
          "tool_error": false
        },
        {
          "call_id": "toolu_0185iacfCfy23MJh7D93tZgr",
          "call_line": 4219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 4231,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EzT2LVCPKG4eKMg1KVMQew",
          "call_line": 5684,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 5696,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WnkMDno8Nush5VQrTVJC5z",
          "call_line": 5775,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 5779,
          "tool_error": false
        },
        {
          "call_id": "toolu_01UyVWsuE13KkofZC41KHmQW",
          "call_line": 6455,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 6467,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VaX67Qq4oU5wCdD3UDCnwv",
          "call_line": 6473,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 6475,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "fa013987-e17b-4aa7-83e9-deadad994bbb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-3ecee3ed90",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/fa013987-e17b-4aa7-83e9-deadad994bbb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "a15b95d97955c6984572f064",
      "changed": true,
      "copied_records_excluded": 439,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 24,
        "unknown_pr_create": 4,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1651,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1661,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1664,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1665,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DMr7FovjGDZrVZxKvsXkh1",
          "call_line": 2943,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2947,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WAeRgvby12pTUrMHsvDiiG",
          "call_line": 3516,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3520,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PKMM7WTM6bTnDk1ePhkHLx",
          "call_line": 5232,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5236,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5373,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 5375,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5378,
          "operations": [
            "push"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "a75c1bed-3da5-4966-928f-d6ba34011911",
      "skills_observed": [],
      "task_id": "t-d41597a07f",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/a75c1bed-3da5-4966-928f-d6ba34011911.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "edb017b2140137dacd818df0",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 5,
        "denied_push_main": 1,
        "edit": 10,
        "unknown_deploy": 3,
        "unknown_pr_create": 7,
        "unknown_pr_merge": 7,
        "unknown_push": 1,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 308,
          "denied": true,
          "operations": [
            "push_main"
          ],
          "result_line": 311,
          "tool_error": true
        },
        {
          "call_id": "toolu_01PaLMSkwd34Sr7P2Tir4rWp",
          "call_line": 313,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 314,
          "tool_error": true
        },
        {
          "call_id": "toolu_01YRaRve8qsLwfGWxbhom5SJ",
          "call_line": 341,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 345,
          "tool_error": false
        },
        {
          "call_id": "toolu_01M49td1hiV2CJc9WYVWzxHS",
          "call_line": 1229,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1233,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lzpj4xLxVP35XsF4J5pEL2",
          "call_line": 1490,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1495,
          "tool_error": false
        },
        {
          "call_id": "toolu_01BQ84gA1y9AcDzDpBUAFaCv",
          "call_line": 1508,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1512,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SjdpyPfGoGLTUNjkB7A6b9",
          "call_line": 1551,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 1553,
          "tool_error": false
        },
        {
          "call_id": "toolu_01C3CLh4HzRSju7dpQjp2fej",
          "call_line": 1687,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1691,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FRko7uTZ2Napy4ngrJUHJL",
          "call_line": 1697,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WkY7MNUefkyaPMw6F7bTzv",
          "call_line": 2295,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 2297,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VvVtDnVvByDnqgAvtyoSDs",
          "call_line": 2685,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2690,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N8D4ABGDDDPpu1BWVzb9H9",
          "call_line": 2696,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 2699,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3011,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3022,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QM3DcVtFwGbDH3HFv6iAyY",
          "call_line": 3025,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3026,
          "tool_error": false
        },
        {
          "call_id": "toolu_015VzjmrFstLHNo8bfWQPiwQ",
          "call_line": 3586,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3597,
          "tool_error": false
        },
        {
          "call_id": "toolu_014QdY9MNYyew7n8piRWuXfq",
          "call_line": 3603,
          "denied": false,
          "operations": [
            "pr_merge",
            "deploy"
          ],
          "result_line": 3605,
          "tool_error": false
        },
        {
          "call_id": "toolu_0185iacfCfy23MJh7D93tZgr",
          "call_line": 4219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 4231,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EzT2LVCPKG4eKMg1KVMQew",
          "call_line": 5684,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 5696,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WnkMDno8Nush5VQrTVJC5z",
          "call_line": 5775,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 5779,
          "tool_error": false
        },
        {
          "call_id": "toolu_01UyVWsuE13KkofZC41KHmQW",
          "call_line": 6455,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 6467,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VaX67Qq4oU5wCdD3UDCnwv",
          "call_line": 6473,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 6475,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "fa013987-e17b-4aa7-83e9-deadad994bbb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-dcaf146f1d",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/fa013987-e17b-4aa7-83e9-deadad994bbb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "6161a730486905bfeb951181",
      "changed": true,
      "copied_records_excluded": 439,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 24,
        "unknown_pr_create": 4,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1651,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1661,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1664,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1665,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DMr7FovjGDZrVZxKvsXkh1",
          "call_line": 2943,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2947,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WAeRgvby12pTUrMHsvDiiG",
          "call_line": 3516,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3520,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PKMM7WTM6bTnDk1ePhkHLx",
          "call_line": 5232,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5236,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5373,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 5375,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5378,
          "operations": [
            "push"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "PARTIAL",
      "session_id": "a75c1bed-3da5-4966-928f-d6ba34011911",
      "skills_observed": [],
      "task_id": "t-c2b503785b",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/a75c1bed-3da5-4966-928f-d6ba34011911.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "615d04ce746e304326b75736",
      "changed": true,
      "copied_records_excluded": 439,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 24,
        "unknown_pr_create": 4,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1651,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1661,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1664,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1665,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DMr7FovjGDZrVZxKvsXkh1",
          "call_line": 2943,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2947,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WAeRgvby12pTUrMHsvDiiG",
          "call_line": 3516,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3520,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PKMM7WTM6bTnDk1ePhkHLx",
          "call_line": 5232,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5236,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5373,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 5375,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5378,
          "operations": [
            "push"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "a75c1bed-3da5-4966-928f-d6ba34011911",
      "skills_observed": [],
      "task_id": "t-d1643614af",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/a75c1bed-3da5-4966-928f-d6ba34011911.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "486e8e07ff2afb96c4f7efc2",
      "changed": true,
      "copied_records_excluded": 439,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 24,
        "unknown_pr_create": 4,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1651,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1661,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KZaV4uUW7PnTP8kqGLvfJp",
          "call_line": 1664,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1665,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DMr7FovjGDZrVZxKvsXkh1",
          "call_line": 2943,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2947,
          "tool_error": false
        },
        {
          "call_id": "toolu_01WAeRgvby12pTUrMHsvDiiG",
          "call_line": 3516,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3520,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PKMM7WTM6bTnDk1ePhkHLx",
          "call_line": 5232,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5236,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5373,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 5375,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NCaZLnGmNL9MnbFcUKuAeP",
          "call_line": 5378,
          "operations": [
            "push"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "a75c1bed-3da5-4966-928f-d6ba34011911",
      "skills_observed": [],
      "task_id": "t-0dd2690085",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/a75c1bed-3da5-4966-928f-d6ba34011911.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "9f92e8cc846429f95e1c5378",
      "changed": false,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 7
      },
      "old_classifier_outcome": "partial",
      "operation_coverage": "no-delivery-operations",
      "operation_evidence": [],
      "reported_task_status": "DONE",
      "session_id": "62b08e16-df54-4888-8268-f29c296cf2eb",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-c62186076e",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/62b08e16-df54-4888-8268-f29c296cf2eb.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "23de5d20ca863b4877597639",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "pushed",
      "new_operation_signals": {
        "commit": 3,
        "edit": 18,
        "pr_create": 3,
        "unknown_push_main": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01D2zNPPT8QVBcWhpZggMGw7",
          "call_line": 1614,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1624,
          "tool_error": false
        },
        {
          "call_id": "toolu_01LYWM5dXhMdCkWRXAx6Y9vD",
          "call_line": 2281,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2285,
          "tool_error": false
        },
        {
          "call_id": "toolu_012DawVLMWnMWdFGPQqTPnLW",
          "call_line": 3453,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3457,
          "tool_error": false
        },
        {
          "call_id": "toolu_014EZeu8W2U1ggDY9xvToDZT",
          "call_line": 4462,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 4466,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "47457bc5-b7f8-499b-9b95-c21560e59c8d",
      "skills_observed": [
        "anthropic-skills:built-in-browser",
        "dispatch",
        "recon"
      ],
      "task_id": "t-a066717b59",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/47457bc5-b7f8-499b-9b95-c21560e59c8d.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "0e28277ed5636efeac29711e",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "pushed",
      "new_operation_signals": {
        "commit": 3,
        "edit": 18,
        "pr_create": 3,
        "unknown_push_main": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01D2zNPPT8QVBcWhpZggMGw7",
          "call_line": 1614,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1624,
          "tool_error": false
        },
        {
          "call_id": "toolu_01LYWM5dXhMdCkWRXAx6Y9vD",
          "call_line": 2281,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2285,
          "tool_error": false
        },
        {
          "call_id": "toolu_012DawVLMWnMWdFGPQqTPnLW",
          "call_line": 3453,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3457,
          "tool_error": false
        },
        {
          "call_id": "toolu_014EZeu8W2U1ggDY9xvToDZT",
          "call_line": 4462,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 4466,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "47457bc5-b7f8-499b-9b95-c21560e59c8d",
      "skills_observed": [
        "anthropic-skills:built-in-browser",
        "dispatch",
        "recon"
      ],
      "task_id": "t-d94034e47f",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/47457bc5-b7f8-499b-9b95-c21560e59c8d.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "537613858d8cc923fe9a552a",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "pushed",
      "new_operation_signals": {
        "commit": 3,
        "edit": 18,
        "pr_create": 3,
        "unknown_push_main": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01D2zNPPT8QVBcWhpZggMGw7",
          "call_line": 1614,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1624,
          "tool_error": false
        },
        {
          "call_id": "toolu_01LYWM5dXhMdCkWRXAx6Y9vD",
          "call_line": 2281,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2285,
          "tool_error": false
        },
        {
          "call_id": "toolu_012DawVLMWnMWdFGPQqTPnLW",
          "call_line": 3453,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3457,
          "tool_error": false
        },
        {
          "call_id": "toolu_014EZeu8W2U1ggDY9xvToDZT",
          "call_line": 4462,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 4466,
          "tool_error": false
        }
      ],
      "reported_task_status": "PARTIAL",
      "session_id": "47457bc5-b7f8-499b-9b95-c21560e59c8d",
      "skills_observed": [
        "anthropic-skills:built-in-browser",
        "dispatch",
        "recon"
      ],
      "task_id": "t-225c786cb4",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/47457bc5-b7f8-499b-9b95-c21560e59c8d.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "e75c17d0c6a0ae01fe0fc7f6",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 4,
        "denied_pr_create": 1,
        "denied_push": 1,
        "edit": 1,
        "unknown_deploy": 1,
        "unknown_pr_create": 3,
        "unknown_pr_merge": 3,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01FYENx8iJdYWZmShNZzymKV",
          "call_line": 369,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 372,
          "tool_error": false
        },
        {
          "call_id": "toolu_01YG5skSMbvufenuNZtfAYir",
          "call_line": 518,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 527,
          "tool_error": false
        },
        {
          "call_id": "toolu_01YG5skSMbvufenuNZtfAYir",
          "call_line": 530,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 531,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QQvbJYFWS86UM9PTHkZVXv",
          "call_line": 1633,
          "denied": true,
          "operations": [
            "push",
            "pr_create"
          ],
          "result_line": 1635,
          "tool_error": true
        },
        {
          "call_id": "toolu_01WTCtBRoPzsWUXTKXrJxsZ4",
          "call_line": 2009,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2013,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Cs7hgNUZuoKKR92h3dsh9p",
          "call_line": 2018,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2021,
          "tool_error": false
        },
        {
          "call_id": "toolu_018guXqVgrDZQ2aX5qRyoxu5",
          "call_line": 2038,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2040,
          "tool_error": false
        },
        {
          "call_id": "toolu_018guXqVgrDZQ2aX5qRyoxu5",
          "call_line": 2043,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2044,
          "tool_error": false
        },
        {
          "call_id": "toolu_01RoMT1UFQfHM942cv2mKLgg",
          "call_line": 2085,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2088,
          "tool_error": false
        },
        {
          "call_id": "toolu_01HBbhGYvEBZZGofQwxmatEa",
          "call_line": 2376,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2379,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PPxUcWAMZBe8JWZuYqjtka",
          "call_line": 2523,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2527,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QbkHhAdUUKWpuw2awBnyCe",
          "call_line": 2532,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2536,
          "tool_error": false
        },
        {
          "call_id": "toolu_0173C31A7yZ1ZvccnNQesD3u",
          "call_line": 2813,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2826,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Kw7XR1TaeeRoJD5EBtwhD4",
          "call_line": 2831,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2835,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "eb284b34-106a-4298-a11e-9e5b4d48c71c",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-38595a057c",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/eb284b34-106a-4298-a11e-9e5b4d48c71c.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "210b0951e0d9ed39aa587342",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 4,
        "denied_pr_create": 1,
        "denied_push": 1,
        "edit": 1,
        "unknown_deploy": 1,
        "unknown_pr_create": 3,
        "unknown_pr_merge": 3,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01FYENx8iJdYWZmShNZzymKV",
          "call_line": 369,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 372,
          "tool_error": false
        },
        {
          "call_id": "toolu_01YG5skSMbvufenuNZtfAYir",
          "call_line": 518,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 527,
          "tool_error": false
        },
        {
          "call_id": "toolu_01YG5skSMbvufenuNZtfAYir",
          "call_line": 530,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 531,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QQvbJYFWS86UM9PTHkZVXv",
          "call_line": 1633,
          "denied": true,
          "operations": [
            "push",
            "pr_create"
          ],
          "result_line": 1635,
          "tool_error": true
        },
        {
          "call_id": "toolu_01WTCtBRoPzsWUXTKXrJxsZ4",
          "call_line": 2009,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2013,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Cs7hgNUZuoKKR92h3dsh9p",
          "call_line": 2018,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2021,
          "tool_error": false
        },
        {
          "call_id": "toolu_018guXqVgrDZQ2aX5qRyoxu5",
          "call_line": 2038,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2040,
          "tool_error": false
        },
        {
          "call_id": "toolu_018guXqVgrDZQ2aX5qRyoxu5",
          "call_line": 2043,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2044,
          "tool_error": false
        },
        {
          "call_id": "toolu_01RoMT1UFQfHM942cv2mKLgg",
          "call_line": 2085,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2088,
          "tool_error": false
        },
        {
          "call_id": "toolu_01HBbhGYvEBZZGofQwxmatEa",
          "call_line": 2376,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2379,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PPxUcWAMZBe8JWZuYqjtka",
          "call_line": 2523,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2527,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QbkHhAdUUKWpuw2awBnyCe",
          "call_line": 2532,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2536,
          "tool_error": false
        },
        {
          "call_id": "toolu_0173C31A7yZ1ZvccnNQesD3u",
          "call_line": 2813,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2826,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Kw7XR1TaeeRoJD5EBtwhD4",
          "call_line": 2831,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2835,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "eb284b34-106a-4298-a11e-9e5b4d48c71c",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-b20538b767",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/eb284b34-106a-4298-a11e-9e5b4d48c71c.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "8d53a85ccf51bd7f3a39ad2d",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 4,
        "denied_pr_create": 1,
        "denied_push": 1,
        "edit": 1,
        "unknown_deploy": 1,
        "unknown_pr_create": 3,
        "unknown_pr_merge": 3,
        "unknown_push_main": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01FYENx8iJdYWZmShNZzymKV",
          "call_line": 369,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 372,
          "tool_error": false
        },
        {
          "call_id": "toolu_01YG5skSMbvufenuNZtfAYir",
          "call_line": 518,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 527,
          "tool_error": false
        },
        {
          "call_id": "toolu_01YG5skSMbvufenuNZtfAYir",
          "call_line": 530,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 531,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QQvbJYFWS86UM9PTHkZVXv",
          "call_line": 1633,
          "denied": true,
          "operations": [
            "push",
            "pr_create"
          ],
          "result_line": 1635,
          "tool_error": true
        },
        {
          "call_id": "toolu_01WTCtBRoPzsWUXTKXrJxsZ4",
          "call_line": 2009,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2013,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Cs7hgNUZuoKKR92h3dsh9p",
          "call_line": 2018,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2021,
          "tool_error": false
        },
        {
          "call_id": "toolu_018guXqVgrDZQ2aX5qRyoxu5",
          "call_line": 2038,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2040,
          "tool_error": false
        },
        {
          "call_id": "toolu_018guXqVgrDZQ2aX5qRyoxu5",
          "call_line": 2043,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2044,
          "tool_error": false
        },
        {
          "call_id": "toolu_01RoMT1UFQfHM942cv2mKLgg",
          "call_line": 2085,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2088,
          "tool_error": false
        },
        {
          "call_id": "toolu_01HBbhGYvEBZZGofQwxmatEa",
          "call_line": 2376,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2379,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PPxUcWAMZBe8JWZuYqjtka",
          "call_line": 2523,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2527,
          "tool_error": false
        },
        {
          "call_id": "toolu_01QbkHhAdUUKWpuw2awBnyCe",
          "call_line": 2532,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2536,
          "tool_error": false
        },
        {
          "call_id": "toolu_0173C31A7yZ1ZvccnNQesD3u",
          "call_line": 2813,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2826,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Kw7XR1TaeeRoJD5EBtwhD4",
          "call_line": 2831,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2835,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "eb284b34-106a-4298-a11e-9e5b4d48c71c",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-f12be3fbf6",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/eb284b34-106a-4298-a11e-9e5b4d48c71c.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "fb35cf5263c1fdda054bd92e",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 8,
        "edit": 4,
        "unknown_deploy": 4,
        "unknown_pr_create": 4,
        "unknown_pr_merge": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013moH4gXaZUPWVLVYka6867",
          "call_line": 2096,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2100,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SZzbD8t9Mb99Nt3kb4ymZ3",
          "call_line": 2114,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2117,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lh9FZB6ZX5jj1Gt8k3aGem",
          "call_line": 2142,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2144,
          "tool_error": false
        },
        {
          "call_id": "toolu_019Spi2rKqZ67wBGijjrYEKP",
          "call_line": 3489,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3493,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VfZM8uWhFYYoLDKgw9ac5d",
          "call_line": 3500,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 3503,
          "tool_error": false
        },
        {
          "call_id": "toolu_0146MhTC1vNjvwoRddxmSToa",
          "call_line": 4997,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5008,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KFRWRn7EUWGrFDy4FEY6TM",
          "call_line": 5015,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 5018,
          "tool_error": false
        },
        {
          "call_id": "toolu_019Pucd8oxgZbr7JrNTUPKTQ",
          "call_line": 5034,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5036,
          "tool_error": false
        },
        {
          "call_id": "toolu_012j5HAydAoVBzQoSyZLEo5X",
          "call_line": 5159,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5161,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EGMJ4VxVFecQMFFox33VoG",
          "call_line": 5517,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5521,
          "tool_error": false
        },
        {
          "call_id": "toolu_01CuB87MW8qCUSvGYMiGNuyJ",
          "call_line": 5526,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 5529,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Krss6enN1uFFxEVDcSXA6F",
          "call_line": 5544,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5546,
          "tool_error": false
        },
        {
          "call_id": "toolu_013moH4gXaZUPWVLVYka6867",
          "call_line": 2103,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01SZzbD8t9Mb99Nt3kb4ymZ3",
          "call_line": 2120,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01Lh9FZB6ZX5jj1Gt8k3aGem",
          "call_line": 2147,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_019Spi2rKqZ67wBGijjrYEKP",
          "call_line": 3496,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01VfZM8uWhFYYoLDKgw9ac5d",
          "call_line": 3506,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_0146MhTC1vNjvwoRddxmSToa",
          "call_line": 5011,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01KFRWRn7EUWGrFDy4FEY6TM",
          "call_line": 5021,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_019Pucd8oxgZbr7JrNTUPKTQ",
          "call_line": 5039,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_012j5HAydAoVBzQoSyZLEo5X",
          "call_line": 5164,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01CuB87MW8qCUSvGYMiGNuyJ",
          "call_line": 5532,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01Krss6enN1uFFxEVDcSXA6F",
          "call_line": 5549,
          "operations": [
            "deploy"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "233a4676-14f8-4699-9ab1-0ed4ac78ab34",
      "skills_observed": [
        "dispatch",
        "handoff"
      ],
      "task_id": "t-49325bbfd0",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/233a4676-14f8-4699-9ab1-0ed4ac78ab34.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "2677a31ed5df529dc02d79a0",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 8,
        "edit": 4,
        "unknown_deploy": 4,
        "unknown_pr_create": 4,
        "unknown_pr_merge": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013moH4gXaZUPWVLVYka6867",
          "call_line": 2096,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2100,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SZzbD8t9Mb99Nt3kb4ymZ3",
          "call_line": 2114,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2117,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lh9FZB6ZX5jj1Gt8k3aGem",
          "call_line": 2142,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2144,
          "tool_error": false
        },
        {
          "call_id": "toolu_019Spi2rKqZ67wBGijjrYEKP",
          "call_line": 3489,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3493,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VfZM8uWhFYYoLDKgw9ac5d",
          "call_line": 3500,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 3503,
          "tool_error": false
        },
        {
          "call_id": "toolu_0146MhTC1vNjvwoRddxmSToa",
          "call_line": 4997,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5008,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KFRWRn7EUWGrFDy4FEY6TM",
          "call_line": 5015,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 5018,
          "tool_error": false
        },
        {
          "call_id": "toolu_019Pucd8oxgZbr7JrNTUPKTQ",
          "call_line": 5034,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5036,
          "tool_error": false
        },
        {
          "call_id": "toolu_012j5HAydAoVBzQoSyZLEo5X",
          "call_line": 5159,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5161,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EGMJ4VxVFecQMFFox33VoG",
          "call_line": 5517,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5521,
          "tool_error": false
        },
        {
          "call_id": "toolu_01CuB87MW8qCUSvGYMiGNuyJ",
          "call_line": 5526,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 5529,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Krss6enN1uFFxEVDcSXA6F",
          "call_line": 5544,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5546,
          "tool_error": false
        },
        {
          "call_id": "toolu_013moH4gXaZUPWVLVYka6867",
          "call_line": 2103,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01SZzbD8t9Mb99Nt3kb4ymZ3",
          "call_line": 2120,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01Lh9FZB6ZX5jj1Gt8k3aGem",
          "call_line": 2147,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_019Spi2rKqZ67wBGijjrYEKP",
          "call_line": 3496,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01VfZM8uWhFYYoLDKgw9ac5d",
          "call_line": 3506,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_0146MhTC1vNjvwoRddxmSToa",
          "call_line": 5011,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01KFRWRn7EUWGrFDy4FEY6TM",
          "call_line": 5021,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_019Pucd8oxgZbr7JrNTUPKTQ",
          "call_line": 5039,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_012j5HAydAoVBzQoSyZLEo5X",
          "call_line": 5164,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01CuB87MW8qCUSvGYMiGNuyJ",
          "call_line": 5532,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01Krss6enN1uFFxEVDcSXA6F",
          "call_line": 5549,
          "operations": [
            "deploy"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "233a4676-14f8-4699-9ab1-0ed4ac78ab34",
      "skills_observed": [
        "dispatch",
        "handoff"
      ],
      "task_id": "t-4febcee68a",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/233a4676-14f8-4699-9ab1-0ed4ac78ab34.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "beca67a7c03eb247b7ca2cbd",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 2,
        "edit": 22,
        "unknown_pr_create": 3,
        "unknown_pr_merge": 3,
        "unknown_push_main": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01DqpYo6eR5pQnQVwUFRBYaW",
          "call_line": 1305,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1309,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DqpYo6eR5pQnQVwUFRBYaW",
          "call_line": 1312,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1313,
          "tool_error": false
        },
        {
          "call_id": "toolu_013894ZEoWGjjkDAdWTNSAH8",
          "call_line": 1395,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 1401,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EkoDGkCP2DXoWfsh6dP8bj",
          "call_line": 1553,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1556,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EkoDGkCP2DXoWfsh6dP8bj",
          "call_line": 1559,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1560,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PenqpdWMVZboZjg1cfNm2x",
          "call_line": 2218,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2222,
          "tool_error": false
        },
        {
          "call_id": "toolu_01PenqpdWMVZboZjg1cfNm2x",
          "call_line": 2225,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2226,
          "tool_error": false
        },
        {
          "call_id": "toolu_01JSA6XvB93Gv2yhHueN744u",
          "call_line": 2244,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2247,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DbbYNJE5W3meKHN9cEphUr",
          "call_line": 2660,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2670,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DbbYNJE5W3meKHN9cEphUr",
          "call_line": 2673,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2674,
          "tool_error": false
        },
        {
          "call_id": "toolu_01W4oTgwPrvDewiPC7cH6YgV",
          "call_line": 2729,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2738,
          "tool_error": false
        },
        {
          "call_id": "toolu_01W4oTgwPrvDewiPC7cH6YgV",
          "call_line": 2741,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2742,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "969587c2-134d-4f1b-ad7d-4b6f0753a60b",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-a5ab636b13",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-agent-config/969587c2-134d-4f1b-ad7d-4b6f0753a60b.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "18bae97f87a9583c67c1166e",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 8,
        "edit": 4,
        "unknown_deploy": 4,
        "unknown_pr_create": 4,
        "unknown_pr_merge": 4
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013moH4gXaZUPWVLVYka6867",
          "call_line": 2096,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2100,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SZzbD8t9Mb99Nt3kb4ymZ3",
          "call_line": 2114,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2117,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Lh9FZB6ZX5jj1Gt8k3aGem",
          "call_line": 2142,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 2144,
          "tool_error": false
        },
        {
          "call_id": "toolu_019Spi2rKqZ67wBGijjrYEKP",
          "call_line": 3489,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 3493,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VfZM8uWhFYYoLDKgw9ac5d",
          "call_line": 3500,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 3503,
          "tool_error": false
        },
        {
          "call_id": "toolu_0146MhTC1vNjvwoRddxmSToa",
          "call_line": 4997,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5008,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KFRWRn7EUWGrFDy4FEY6TM",
          "call_line": 5015,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 5018,
          "tool_error": false
        },
        {
          "call_id": "toolu_019Pucd8oxgZbr7JrNTUPKTQ",
          "call_line": 5034,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5036,
          "tool_error": false
        },
        {
          "call_id": "toolu_012j5HAydAoVBzQoSyZLEo5X",
          "call_line": 5159,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5161,
          "tool_error": false
        },
        {
          "call_id": "toolu_01EGMJ4VxVFecQMFFox33VoG",
          "call_line": 5517,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 5521,
          "tool_error": false
        },
        {
          "call_id": "toolu_01CuB87MW8qCUSvGYMiGNuyJ",
          "call_line": 5526,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 5529,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Krss6enN1uFFxEVDcSXA6F",
          "call_line": 5544,
          "denied": false,
          "operations": [
            "deploy"
          ],
          "result_line": 5546,
          "tool_error": false
        },
        {
          "call_id": "toolu_013moH4gXaZUPWVLVYka6867",
          "call_line": 2103,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01SZzbD8t9Mb99Nt3kb4ymZ3",
          "call_line": 2120,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01Lh9FZB6ZX5jj1Gt8k3aGem",
          "call_line": 2147,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_019Spi2rKqZ67wBGijjrYEKP",
          "call_line": 3496,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01VfZM8uWhFYYoLDKgw9ac5d",
          "call_line": 3506,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_0146MhTC1vNjvwoRddxmSToa",
          "call_line": 5011,
          "operations": [
            "pr_create"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01KFRWRn7EUWGrFDy4FEY6TM",
          "call_line": 5021,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_019Pucd8oxgZbr7JrNTUPKTQ",
          "call_line": 5039,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_012j5HAydAoVBzQoSyZLEo5X",
          "call_line": 5164,
          "operations": [
            "deploy"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01CuB87MW8qCUSvGYMiGNuyJ",
          "call_line": 5532,
          "operations": [
            "pr_merge"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01Krss6enN1uFFxEVDcSXA6F",
          "call_line": 5549,
          "operations": [
            "deploy"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "PARTIAL",
      "session_id": "233a4676-14f8-4699-9ab1-0ed4ac78ab34",
      "skills_observed": [
        "dispatch",
        "handoff"
      ],
      "task_id": "t-50d395594c",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/233a4676-14f8-4699-9ab1-0ed4ac78ab34.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "333097f74a3ccda492047484",
      "changed": true,
      "copied_records_excluded": 2011,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 8,
        "unknown_pr_create": 2,
        "unknown_pr_merge": 2
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01LWHfKPsoGL1sZroUrrGwoA",
          "call_line": 3800,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3804,
          "tool_error": false
        },
        {
          "call_id": "toolu_018AbghrtyoueHbFGpkc1s4m",
          "call_line": 3965,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3968,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "2ef1b06a-bdf5-433d-91c2-557a1f925171",
      "skills_observed": [],
      "task_id": "t-daed83de4e",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/2ef1b06a-bdf5-433d-91c2-557a1f925171.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "20cb5b7f53a1819dfc1dba58",
      "changed": false,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "shipped",
      "new_operation_signals": {
        "commit": 5,
        "edit": 62,
        "pr_merge": 1,
        "unknown_pr_create": 2,
        "unknown_pr_merge": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013MLRx24bc7gQQu3Bfpznxk",
          "call_line": 2931,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2935,
          "tool_error": false
        },
        {
          "call_id": "toolu_013MLRx24bc7gQQu3Bfpznxk",
          "call_line": 2938,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 2939,
          "tool_error": false
        },
        {
          "call_id": "toolu_01W1Qjvxi3S7Hds87GRJf6Ci",
          "call_line": 2951,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 2954,
          "tool_error": false
        },
        {
          "call_id": "toolu_012as3XfPbPBjMMAStz2fPW5",
          "call_line": 3572,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3576,
          "tool_error": false
        },
        {
          "call_id": "toolu_012as3XfPbPBjMMAStz2fPW5",
          "call_line": 3579,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3580,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "312595d7-6948-4c62-a1d0-5095976f038b",
      "skills_observed": [
        "plugin-authoring"
      ],
      "task_id": "t-356e418522",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-agent-config/312595d7-6948-4c62-a1d0-5095976f038b.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "fe575932ad2583b616f2cac4",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 3,
        "edit": 13,
        "unknown_pr_create": 5,
        "unknown_pr_merge": 5,
        "unknown_push_main": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01GJ7UbeomFTYeYEMSp3B3jd",
          "call_line": 1887,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 1893,
          "tool_error": false
        },
        {
          "call_id": "toolu_015r8NcRBvhcpmR2AeEu1z4h",
          "call_line": 2219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 2223,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DSixXc6QCH2PFbAxHZG6pD",
          "call_line": 2791,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2801,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DSixXc6QCH2PFbAxHZG6pD",
          "call_line": 2804,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2805,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Rex1mYDV318BF6eZ54PkG2",
          "call_line": 3706,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3710,
          "tool_error": false
        },
        {
          "call_id": "toolu_01T7zu8nRLX1qKwJzYFfrWVM",
          "call_line": 3752,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3756,
          "tool_error": false
        },
        {
          "call_id": "toolu_019S9GDGeqDPXH1jrUQsdbtq",
          "call_line": 3934,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3938,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "ed051ea3-1c79-4eae-bec1-5c5f21bd7f14",
      "skills_observed": [
        "dispatch",
        "handoff"
      ],
      "task_id": "t-ce3012bcae",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/ed051ea3-1c79-4eae-bec1-5c5f21bd7f14.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "c21037728c8077d91ebf4c9a",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 3,
        "edit": 13,
        "unknown_pr_create": 5,
        "unknown_pr_merge": 5,
        "unknown_push_main": 1
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01GJ7UbeomFTYeYEMSp3B3jd",
          "call_line": 1887,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 1893,
          "tool_error": false
        },
        {
          "call_id": "toolu_015r8NcRBvhcpmR2AeEu1z4h",
          "call_line": 2219,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 2223,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DSixXc6QCH2PFbAxHZG6pD",
          "call_line": 2791,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2801,
          "tool_error": false
        },
        {
          "call_id": "toolu_01DSixXc6QCH2PFbAxHZG6pD",
          "call_line": 2804,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2805,
          "tool_error": false
        },
        {
          "call_id": "toolu_01Rex1mYDV318BF6eZ54PkG2",
          "call_line": 3706,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3710,
          "tool_error": false
        },
        {
          "call_id": "toolu_01T7zu8nRLX1qKwJzYFfrWVM",
          "call_line": 3752,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3756,
          "tool_error": false
        },
        {
          "call_id": "toolu_019S9GDGeqDPXH1jrUQsdbtq",
          "call_line": 3934,
          "denied": false,
          "operations": [
            "pr_create",
            "pr_merge"
          ],
          "result_line": 3938,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "ed051ea3-1c79-4eae-bec1-5c5f21bd7f14",
      "skills_observed": [
        "dispatch",
        "handoff"
      ],
      "task_id": "t-a130a2228b",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-Projects-agent-memory/ed051ea3-1c79-4eae-bec1-5c5f21bd7f14.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "5cd55373185f91ec73ab9905",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "partial",
      "new_operation_signals": {
        "edit": 34,
        "unknown_push_main": 3
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "partial-results",
      "operation_evidence": [
        {
          "call_id": "toolu_01K4tLP5yhx2BRAS1Xa2hoMV",
          "call_line": 1478,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1481,
          "tool_error": false
        },
        {
          "call_id": "toolu_0155khbWCiW6D42Udf377vZi",
          "call_line": 1859,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 1864,
          "tool_error": false
        },
        {
          "call_id": "toolu_01SazSj1J32m5QTuDfavZf2h",
          "call_line": 2361,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 2363,
          "tool_error": false
        },
        {
          "call_id": "toolu_01K4tLP5yhx2BRAS1Xa2hoMV",
          "call_line": 1484,
          "operations": [
            "push_main"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_0155khbWCiW6D42Udf377vZi",
          "call_line": 1868,
          "operations": [
            "push_main"
          ],
          "result_line": null
        },
        {
          "call_id": "toolu_01SazSj1J32m5QTuDfavZf2h",
          "call_line": 2367,
          "operations": [
            "push_main"
          ],
          "result_line": null
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "2dac9611-f909-4ab4-b01a-1b7054197ecf",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-3381c514d1",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-agent-config/2dac9611-f909-4ab4-b01a-1b7054197ecf.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "d89575e7bd4f5ffefa0075f5",
      "changed": true,
      "copied_records_excluded": 0,
      "integration_first_pass_reported": false,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 1,
        "edit": 22,
        "unknown_pr_create": 1,
        "unknown_pr_merge": 2,
        "unknown_push": 3,
        "unknown_push_main": 2
      },
      "old_classifier_outcome": "shipped",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013qS7E1qFQef8FD1dMzAczj",
          "call_line": 3881,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 3884,
          "tool_error": false
        },
        {
          "call_id": "toolu_01KbzqH1YFHPSg6q3unmRK8z",
          "call_line": 4162,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 4164,
          "tool_error": false
        },
        {
          "call_id": "toolu_01FKHzn9h1jqhzcpa1o9fodb",
          "call_line": 4256,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 4259,
          "tool_error": false
        },
        {
          "call_id": "toolu_01S6sF5tTpSRCtJEBVSqHFa2",
          "call_line": 4268,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 4271,
          "tool_error": false
        },
        {
          "call_id": "toolu_01VqFvWmAnoR4QfxuwBsggqJ",
          "call_line": 4368,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 4376,
          "tool_error": false
        },
        {
          "call_id": "toolu_011d51DWkZ1ThekQ31NB7G7r",
          "call_line": 4388,
          "denied": false,
          "operations": [
            "pr_merge"
          ],
          "result_line": 4391,
          "tool_error": false
        },
        {
          "call_id": "toolu_01XvcZKNsRWuZnSupaKsQRVE",
          "call_line": 4619,
          "denied": false,
          "operations": [
            "push_main"
          ],
          "result_line": 4628,
          "tool_error": false
        },
        {
          "call_id": "toolu_01N25NjopKa1w57qVrXiSL5o",
          "call_line": 4635,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 4637,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "7118d460-58e0-41f3-ab11-6fc7f2ec0533",
      "skills_observed": [
        "dispatch"
      ],
      "task_id": "t-77f6662e0f",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-agent-config/7118d460-58e0-41f3-ab11-6fc7f2ec0533.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "ce8afcb7a54bd03d87e12ea8",
      "changed": true,
      "copied_records_excluded": 642,
      "integration_first_pass_reported": null,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 1,
        "edit": 4,
        "unknown_pr_create": 1,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013DtmbP71csBPUCAjVhHCJB",
          "call_line": 1258,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 1265,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NXmhEU1jVTNTBSxMCxyQ5k",
          "call_line": 1529,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1533,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "96b61694-9239-48d0-aef0-5343e3d71cf3",
      "skills_observed": [],
      "task_id": "t-054c274b4e",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/96b61694-9239-48d0-aef0-5343e3d71cf3.jsonl"
    },
    {
      "acceptance": "unknown",
      "acceptance_reason": "no independent requirement/result binding",
      "attempt_id": "8d4fe9cd87da9cb0349386a1",
      "changed": true,
      "copied_records_excluded": 642,
      "integration_first_pass_reported": true,
      "new_classifier_outcome": "committed",
      "new_operation_signals": {
        "commit": 1,
        "edit": 4,
        "unknown_pr_create": 1,
        "unknown_push": 1
      },
      "old_classifier_outcome": "pushed",
      "operation_coverage": "complete-results",
      "operation_evidence": [
        {
          "call_id": "toolu_013DtmbP71csBPUCAjVhHCJB",
          "call_line": 1258,
          "denied": false,
          "operations": [
            "push"
          ],
          "result_line": 1265,
          "tool_error": false
        },
        {
          "call_id": "toolu_01NXmhEU1jVTNTBSxMCxyQ5k",
          "call_line": 1529,
          "denied": false,
          "operations": [
            "pr_create"
          ],
          "result_line": 1533,
          "tool_error": false
        }
      ],
      "reported_task_status": "DONE",
      "session_id": "96b61694-9239-48d0-aef0-5343e3d71cf3",
      "skills_observed": [],
      "task_id": "t-53121da53e",
      "transcript_ref": "/Users/vilmerfrost/.claude/projects/-Users-vilmerfrost-StupidHuman-Workspace-pipeline-poc/96b61694-9239-48d0-aef0-5343e3d71cf3.jsonl"
    }
  ],
  "selected_tasks": 30,
  "skill_sensitivity": [
    {
      "matched_sessions": 1,
      "new_cohort_score": 0.6333,
      "new_scored": 1,
      "old_cohort_score": 0.6667,
      "old_scored": 1,
      "skill": "anthropic-skills:built-in-browser"
    },
    {
      "matched_sessions": 9,
      "new_cohort_score": 0.6545,
      "new_scored": 9,
      "old_cohort_score": 0.8545,
      "old_scored": 9,
      "skill": "dispatch"
    },
    {
      "matched_sessions": 2,
      "new_cohort_score": 0.625,
      "new_scored": 2,
      "old_cohort_score": 0.75,
      "old_scored": 2,
      "skill": "handoff"
    },
    {
      "matched_sessions": 1,
      "new_cohort_score": 0.6667,
      "new_scored": 1,
      "old_cohort_score": 0.6667,
      "old_scored": 1,
      "skill": "plugin-authoring"
    },
    {
      "matched_sessions": 1,
      "new_cohort_score": 0.6333,
      "new_scored": 1,
      "old_cohort_score": 0.6667,
      "old_scored": 1,
      "skill": "recon"
    }
  ],
  "transition_matrix": [
    {
      "new": "partial",
      "old": "partial",
      "tasks": 1
    },
    {
      "new": "committed",
      "old": "pushed",
      "tasks": 2
    },
    {
      "new": "partial",
      "old": "pushed",
      "tasks": 5
    },
    {
      "new": "committed",
      "old": "shipped",
      "tasks": 16
    },
    {
      "new": "partial",
      "old": "shipped",
      "tasks": 2
    },
    {
      "new": "pushed",
      "old": "shipped",
      "tasks": 3
    },
    {
      "new": "shipped",
      "old": "shipped",
      "tasks": 1
    }
  ],
  "unique_sessions": 13
}
```
