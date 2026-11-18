# GitHub App & Cloudflare Tunnel Setup Guide (5-Minute Milestone 1 Runbook)

This runbook connects a personal development **GitHub App** to your local **CodeSight-AI** stack (`codesight-api` + `codesight-worker`) through **Cloudflare Quick Tunnel** (`cloudflared`) in under 5 minutes without exposing your host firewall or modifying unrelated Docker stacks.

---

## Architecture & Port Coexistence Invariants

```text
GitHub PR Event (HMAC-SHA256)
  │
  ▼  POST https://<subdomain>.trycloudflare.com/api/v1/webhooks/github
cloudflared Quick Tunnel ──► codesight-api (host :8082 -> container :8000)
                                 │  • Verifies HMAC-SHA256 (<100ms ACK -> HTTP 202)
                                 │  • Records idempotency in Postgres (:5433)
                                 │  • Claims Lua Cancel-on-Supersede lock in Redis (:6381)
                                 ▼
                           codesight-worker (Celery)
                             • Loads .codesight.yaml from default_branch
                             • Creates GitHub Check Run (in_progress)
                             • Fetches & filters diff in RAM (NFR-3: zero disk persistence)
                             • Runs pre_publish supersede guard -> publishes batch PR Review
                             • Completes Check Run (success / neutral if superseded)
```

> **Host Docker Isolation Rule (`AGENTS.md`):**
> Always scope Compose commands with `-p codesight`. **Never** run global `docker compose down` or touch **FlintFlow** (`3001`, `6379`, `8080`) or **DocuMind** (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`) containers.
> CodeSight-AI uses strictly isolated host ports: PostgreSQL `5433`, Redis `6381`, Qdrant `6333`/`6334`, FastAPI `8082`.

---

## Step 1 (1 min) — Install & Start Cloudflare Quick Tunnel (`cloudflared`)

Install `cloudflared` on your host machine if it is not already present:

- **Windows (Winget):**
  ```powershell
  winget install --id Cloudflare.cloudflared
  ```
- **macOS (Homebrew):**
  ```bash
  brew install cloudflared
  ```

Ensure the CodeSight stack is running, then open a dedicated terminal and start the tunnel pointing to `codesight-api` on port `8082`:

```powershell
cloudflared tunnel --url http://localhost:8082
```

Copy the generated public HTTPS URL printed in the terminal (for example `https://amber-valley-demo.trycloudflare.com`). Keep this terminal open while testing.

> **Note:** Each Quick Tunnel restart assigns a new `*.trycloudflare.com` hostname. If you restart `cloudflared`, update the GitHub App **Webhook URL** before pushing new PR events.

---

## Step 2 (2 min) — Register & Install the Development GitHub App

1. Open **GitHub** -> **Settings** -> **Developer settings** -> **GitHub Apps** -> **New GitHub App**.
2. Fill in the core configuration:
   - **GitHub App name:** e.g. `codesight-ai-dev-<your-handle>` (must be globally unique on GitHub).
   - **Homepage URL:** `https://github.com/<your-handle>/CodeSight-AI`
   - **Webhook:** Checked (**Active**).
   - **Webhook URL:** `https://<your-trycloudflare-host>.trycloudflare.com/api/v1/webhooks/github`
   - **Webhook secret:** Generate a 32-byte hex secret locally:
     ```powershell
     uv run python -c "import secrets; print(secrets.token_hex(32))"
     ```
3. Set **Repository permissions**:
   - **Checks:** `Read and write`
   - **Contents:** `Read-only`
   - **Pull requests:** `Read and write`
   - **Metadata:** `Read-only` *(selected automatically)*
4. Under **Subscribe to events**, check:
   - `Pull request`
   - `Push`
5. Select **Only on this account** and click **Create GitHub App**.
6. On the created App page:
   - Copy the numeric **App ID**.
   - Scroll to **Private keys** -> click **Generate a private key** (downloads a `.pem` file; store it outside the repository).
   - Go to **Install App** (left sidebar) -> install it on your test/sandbox repository.

---

## Step 3 (1 min) — Configure `.env` & Rebuild CodeSight Containers

1. Copy `.env.example` to `.env` if you have not already (`.env` and `*.pem` are excluded by `.gitignore` — **never** commit them).
2. Convert your downloaded `.pem` file into a single-line Base64 string in PowerShell so Docker Compose and Pydantic `Settings` parse it cleanly without multiline quoting issues:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\your-app.private-key.pem"))
   ```
3. Update `.env`:
   ```dotenv
   GITHUB_APP_ID=<your-app-id>
   GITHUB_PRIVATE_KEY=<single-line-base64-pem>
   GITHUB_WEBHOOK_SECRET=<your-webhook-secret>
   WEEK1_SMOKE_INLINE_COMMENT=true
   ```
4. Rebuild and start the isolated CodeSight-AI containers so `codesight-api` and `codesight-worker` pick up your updated code and `.env`:
   ```powershell
   docker compose -p codesight up -d --build
   ```
5. Verify gateway health and worker readiness:
   ```powershell
   curl.exe -s http://localhost:8082/health
   docker compose -p codesight logs -f codesight-api codesight-worker
   ```

---

## Step 4 (1 min) — Live Milestone 1 Acceptance Verification

1. **Test Noise Filtering & Zero-422 Inline Comment:**
   - Open a Pull Request on your sandbox repository modifying both a real code file (e.g. `src/app.py`) and a lockfile (`package-lock.json`).
   - Verify in **GitHub App -> Advanced -> Recent Deliveries** that `/api/v1/webhooks/github` returned **`202 Accepted`** cleanly under the `< 300ms` GitHub SLA (warm gateway latency `< 100ms`).
   - Verify on the PR **Checks** tab that `CodeSight-AI Review` transitioned from `in_progress` to `completed` (`success`).
   - Verify on the PR **Files changed** / **Conversation** tabs that:
     - `package-lock.json` was skipped and listed under **Filtered Noise / Excluded Files** with token reduction percentage.
     - Exactly 1 Milestone 1 inline verification comment is anchored to a valid `RIGHT`-side line of `src/app.py` with **`0` `HTTP 422` errors**.
2. **Test Rapid 2-Commit `Cancel-on-Supersede`:**
   - Push two commits in rapid succession (`git commit ... && git push`, followed immediately by another commit + push within 1–2 seconds).
   - Verify in `codesight-worker` logs (`worker_pr_publish_aborted_superseded` / `worker_pr_check_run_superseded`) and on GitHub that the superseded commit's Check Run closes as `neutral` with `0` stale review comments, and only the latest `HEAD` commit publishes a PR Review.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
| :--- | :--- | :--- |
| GitHub Delivery shows `401 Unauthorized` | `GITHUB_WEBHOOK_SECRET` mismatch between GitHub App and `.env` | Re-sync `GITHUB_WEBHOOK_SECRET` in `.env` and run `docker compose -p codesight up -d codesight-api`. |
| Delivery times out / `502 Bad Gateway` | `cloudflared` restarted or `codesight-api` not up on `8082` | Restart `cloudflared tunnel --url http://localhost:8082` and update Webhook URL in GitHub App settings. |
| Worker logs `GitHubAuthError (403)` | App lacks `Pull requests: Read & write` or `Checks: Read & write`, or wasn't installed on target repo | Update App permissions, accept new permissions on installation, and retry delivery. |
| Check Run ends `neutral` (`Superseded`) | Another commit arrived while processing the previous SHA | Expected behavior from `Cancel-on-Supersede`; verify latest commit receives the `success` Check Run. |
