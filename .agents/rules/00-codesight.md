---
trigger: always_on
---

# CodeSight-AI — Workspace & Jira Tracking Rule

@/AGENTS.md

`AGENTS.md` at the repository root is the canonical rulebook for this workspace.

- **Jira Space:** Always track, inspect, and transition tasks exclusively in Jira Project **`CA`** (**CodeSight-AI** — `https://nguyentien191004.atlassian.net/jira/software/projects/CA/summary`, Board ID `74`) via the `jira-cwb` MCP server. Never create or modify issues in `DA` (`DocuMind-AI`) or `CWB` (`Co-Workspace Booking`) from this workspace.
- **Jira Status Transitions (`CA-*`):** Use transition ID `21` (`In Progress`) when starting a task, `31` (`Verify / Test`) during verification, and `41` (`Done`) after posting verification evidence via `jira_add_comment`.
