# Agent Instructions

## Change Authorization

- Treat the user's approval to implement a change as authorization only for the explicitly approved scope.
- If implementation or testing reveals a separate bug, regression, design issue, or improvement, do not fix it under the earlier approval.
- Report the newly discovered issue, explain its impact, and suggest a solution. Wait for separate explicit approval before changing code, tests, configuration, schemas, prompts, or documentation to address it.
- Changes strictly required to make the approved implementation correct and complete are still within scope. When that boundary is unclear, report the issue and ask before editing.
