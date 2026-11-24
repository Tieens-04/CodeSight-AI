# CodeSight-AI — Workspace & Jira Tracking Rule

@d:\Code\Project\CodeSight-AI\AGENTS.md

`AGENTS.md` at the repository root is the canonical rulebook for this workspace.

- **Jira Space:** Always track, inspect, and transition tasks exclusively in Jira Project **`CA`** (**CodeSight-AI** — `https://nguyentien191004.atlassian.net/jira/software/projects/CA/summary`, Board ID `74`) via **`atlassian-mcp-server`** (`cloudId: "nguyentien191004.atlassian.net"`) or **`jira-cwb`** (whichever is active in `<mcp_servers>`). Never create or modify issues in `DA` (`DocuMind-AI`) or `CWB` (`Co-Workspace Booking`) from this workspace.
- **Jira Status Transitions (`CA-*`):** Use transition `"21"` (`In Progress`) when starting a task, `"31"` (`Verify / Test`) during verification, and `"41"` (`Done`) after posting verification evidence comment.
- **AI Development Workflow (`.ai-workflow` v4.0):** When the user invokes `/ai-workflow`, `"/ai-workflow board"`, `"/ai-workflow status"`, starts or closes a `CA-*` ticket (e.g., `"Làm task CA-XX"`, `"/ai-workflow close CA-XX"`), or sends a step hand-off prompt, activate `.agents/skills/ai-workflow/SKILL.md` and strictly enforce `.ai-workflow/prompts/00-shared-rules.md` (`1 Turn = 1 Step`, `state.md` handoff, `verify.py` CLI, `2-Strike Blocked`, cross-model hand-off box, uncommitted working-tree diff review) without overriding any invariant in `AGENTS.md`.
