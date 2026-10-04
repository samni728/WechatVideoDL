# WechatVideoDL WebUI / History / Retry Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade WechatVideoDL into a reliable single-account content workstation with 3-attempt retry, SQLite history, TXT/Markdown outputs, editable prompt presets, proxy-safe URLs, deleteable artifacts, and a polished themeable WebUI.

**Architecture:** Keep Flask + Browserless + Yuanbao + yt-dlp. Split persistence, retry/domain logic, and UI assets out of the current monolithic `server_docker/app.py`; persist jobs in `/data/history.db`; keep Browserless private and use the existing process lock for one-at-a-time parsing. Serve template/static assets directly from Flask with no Node/frontend build pipeline.

**Tech Stack:** Python 3.12, Flask 3, sqlite3 stdlib, Werkzeug ProxyFix, requests, Gunicorn, yt-dlp, Browserless Chromium, vanilla HTML/CSS/JS, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-03-webui-history-retry-design.md`

## Global Constraints

- Single configured account only; no registration or per-user isolation.
- Up to **3 total attempts** per submitted parse job; deterministic/user-action failures are not retried.
- Runtime data stays under `/data`; history database is `/data/history.db`.
- Output formats are exactly `txt` and `md`; WebUI defaults to `md`, API omission defaults to `txt` for backward compatibility.
- Existing `POST /api/parse`, `GET /health`, and `GET /files/<filename>` remain available.
- Add `GET /api/presets`, `GET /api/history`, `GET /api/history/<job_id>`, `DELETE /api/history/<job_id>`.
- UI prefers relative `download_path`; absolute `download_url` honors `PUBLIC_BASE_URL`, then trusted forwarded headers when enabled, then request host.
- `ProxyFix` is enabled only when `TRUST_PROXY_HEADERS=true`; default `PROXY_HOPS=1`.
- No Redis, database container, Node runtime, or frontend build system.
- Cookie/session secrets, SQLite DB, downloads, `.env`, and deployment baselines must remain untracked.

## Review Focus

1. Browserless returns HTML/502/plain text instead of JSON: API must still emit structured JSON and frontend must show a proxy/upstream message, never a raw `Unexpected token '<'` parser exception.
2. Reverse proxy supplies forwarded scheme/host/prefix: file links must stay on the public domain and preserve any configured prefix while direct requests remain safe when proxy trust is disabled.
3. A transient attempt creates partial text/video files: retries and final failure must not leak orphaned artifacts or create multiple history rows for one submission.
4. Delete is called after one artifact was manually removed: remaining owned file and DB row are still removed, but traversal/arbitrary filename input can never escape `/data/downloads`.
5. App restarts during/after jobs: schema creation is idempotent, completed history persists, and stale `running` jobs are made intelligible rather than breaking list/detail APIs.

---

### Task 1: Persistence model, output writers, and proxy-safe file URLs

**Files:**
- Create: `server_docker/history_store.py`
- Create: `server_docker/job_files.py`
- Modify: `server_docker/app.py`
- Modify: `server_docker/.env.example`
- Modify: `server_docker/docker-compose.yml`
- Test: `server_docker/tests/test_history_store.py`
- Test: `server_docker/tests/test_job_files.py`
- Test: `server_docker/tests/test_proxy_urls.py`

**Interfaces:**
- Produces: `HistoryStore(db_path: Path)`, `HistoryStore.create_job(...)`, `update_job(job_id, **fields)`, `list_jobs(limit=100)`, `get_job(job_id)`, `delete_job(job_id)`.
- Produces: `write_analysis_file(download_dir, job, format) -> Path`, `safe_owned_path(download_dir, filename) -> Path | None`.
- Produces in app: `build_file_links(filename) -> {"download_path": str, "download_url": str}`.

- [ ] **Step 1: Write failing persistence tests** for schema creation, newest-first list/detail, JSON retry-error round trip, persistence across a second store instance, and stale `running` jobs being marked/returned safely on startup.
- [ ] **Step 2: Run** `python -m unittest server_docker.tests.test_history_store -v` and verify the new tests fail because the store module does not exist.
- [ ] **Step 3: Implement `history_store.py`** with sqlite3, idempotent schema initialization, explicit allowed update fields, JSON serialization for retry errors, row-to-dict conversion, and startup reconciliation for stale `running` records.
- [ ] **Step 4: Run the history-store tests** and verify all pass.
- [ ] **Step 5: Write failing output-file tests** asserting TXT and Markdown exact structural headers, UTF-8 content, `wxv_XXXXXX.txt|md` naming, and `safe_owned_path()` rejecting traversal or mismatched basenames.
- [ ] **Step 6: Implement `job_files.py`** with `txt`/`md` validation and safe filename/path handling.
- [ ] **Step 7: Run** `python -m unittest server_docker.tests.test_job_files -v` and verify pass.
- [ ] **Step 8: Write failing proxy URL tests** covering direct host, untrusted spoofed forwarded host ignored, trusted forwarded proto/host, forwarded prefix, and `PUBLIC_BASE_URL` override.
- [ ] **Step 9: Implement proxy configuration and `build_file_links()`** in `app.py`; wire `TRUST_PROXY_HEADERS`, `PROXY_HOPS`, and `PUBLIC_BASE_URL`; add env defaults to `.env.example` and Compose.
- [ ] **Step 10: Run** `python -m unittest server_docker.tests.test_proxy_urls -v` and verify pass.
- [ ] **Step 11: Commit** `feat: add persistent history and proxy-safe file links`.

### Task 2: Retry engine and durable parse-job lifecycle

**Files:**
- Create: `server_docker/job_runner.py`
- Modify: `server_docker/app.py`
- Modify: `server_docker/job_files.py`
- Test: `server_docker/tests/test_job_runner.py`
- Test: `server_docker/tests/test_parse_api.py`

**Interfaces:**
- Consumes: `HistoryStore`, `write_analysis_file()`, existing `analyze_with_yuanbao()`, `download_video()`.
- Produces: `run_parse_job(job_id, input_url, prompt, preset, output_format, download_requested) -> dict`.
- Produces: retry classification `is_retryable_error(exc) -> bool` and sanitized retry summaries.

- [ ] **Step 1: Write failing retry tests** for success on attempt 2, success on attempt 3, deterministic invalid/session errors stopping after attempt 1, final transient failure after attempt 3, 1s/2s backoff injection, and one history row across all attempts.
- [ ] **Step 2: Run** `python -m unittest server_docker.tests.test_job_runner -v` and verify fail.
- [ ] **Step 3: Implement `job_runner.py`** with `MAX_PARSE_ATTEMPTS` default 3, explicit retryable code/status classification, injectable sleeper for tests, per-attempt state/history updates, bounded backoff, and cleanup of partial owned artifacts before retry/final failure.
- [ ] **Step 4: Run job-runner tests** and verify pass.
- [ ] **Step 5: Write failing parse-API tests** asserting omitted `output_format` => `txt`, Web/API supported `md`, invalid format => 400/no retry, final response contains attempts/max/retry_errors and relative+absolute file links, and application exceptions always return JSON.
- [ ] **Step 6: Refactor `POST /api/parse`** to create one history row, invoke `run_parse_job()` under the existing process lock, and return the persisted final record.
- [ ] **Step 7: Run** `python -m unittest server_docker.tests.test_parse_api -v` and verify pass.
- [ ] **Step 8: Commit** `feat: retry transient parse failures and persist job lifecycle`.

### Task 3: Presets, history APIs, and secure deletion

**Files:**
- Create: `server_docker/presets.py`
- Modify: `server_docker/app.py`
- Test: `server_docker/tests/test_history_api.py`
- Test: `server_docker/tests/test_presets.py`

**Interfaces:**
- Produces: `PRESETS` list with exactly five stable ids/names/prompts; `get_preset(preset_id)`.
- Produces endpoints: `GET /api/presets`, `GET /api/history`, `GET /api/history/<job_id>`, `DELETE /api/history/<job_id>`.

- [ ] **Step 1: Write failing preset tests** checking five curated presets, stable ids, non-empty editable text, and invalid preset id handled as custom/null metadata rather than arbitrary injection.
- [ ] **Step 2: Implement `presets.py` and `/api/presets`**; validate optional `preset_id` in parse requests and persist matching name/id.
- [ ] **Step 3: Run** `python -m unittest server_docker.tests.test_presets -v` and verify pass.
- [ ] **Step 4: Write failing history API tests** for authentication, newest-first list, detail, 404, delete confirmation endpoint semantics, missing optional file tolerance, owned-file deletion, traversal/malformed job id rejection, and DB record deletion.
- [ ] **Step 5: Implement history endpoints** using only filenames loaded from SQLite and `safe_owned_path()`; never accept filesystem paths from the client.
- [ ] **Step 6: Run** `python -m unittest server_docker.tests.test_history_api -v` and verify pass.
- [ ] **Step 7: Commit** `feat: add prompt presets and history management APIs`.

### Task 4: Product UI, themes, logo, resilient frontend, and history experience

**Files:**
- Create: `server_docker/templates/base.html`
- Create: `server_docker/templates/login.html`
- Create: `server_docker/templates/index.html`
- Create: `server_docker/templates/history.html`
- Create: `server_docker/static/app.css`
- Create: `server_docker/static/app.js`
- Create: `server_docker/static/logo.svg`
- Modify: `server_docker/app.py`
- Modify: `server_docker/Dockerfile`
- Test: `server_docker/tests/test_ui_routes.py`
- Test: `server_docker/tests/test_frontend_contract.py`

**Interfaces:**
- Consumes the API endpoints from Tasks 2-3.
- Produces browser UI with Light/Dark/System, preset-to-editable-prompt behavior, output-format selector, retry/status presentation, result downloads, history list/detail/delete, responsive styling, and safe error rendering.

- [ ] **Step 1: Write failing UI route/contract tests** for authenticated template routes, static logo/css/js availability, main controls/ids, history controls, theme options, and absence of legacy inline `INDEX_HTML`/`LOGIN_HTML` rendering.
- [ ] **Step 2: Create Flask templates/static routing** and copy static/templates into the Docker image; keep login/logout behavior unchanged.
- [ ] **Step 3: Implement the new logo and visual system** with responsive two-column workspace, restrained accent palette, accessible focus states, Light/Dark/System tokens, and localStorage theme preference.
- [ ] **Step 4: Implement prompt preset UX**: load `/api/presets`, selecting copies text into textarea, manual edits remain untouched until another preset is selected, WebUI defaults output format to `md`.
- [ ] **Step 5: Implement resilient fetch helper** that checks HTTP status and content type before JSON parsing, returns a human-readable non-JSON/proxy error with escaped excerpt in developer details, and never surfaces parser exceptions as primary UI errors.
- [ ] **Step 6: Implement result/status UI** showing attempts used/max, retry summaries, content preview, relative MP4/text download actions, and collapsible technical metadata.
- [ ] **Step 7: Implement history page** with newest-first rows/cards, status/format/file availability, detail expansion, download actions, explicit delete confirmation, delete refresh, and empty state.
- [ ] **Step 8: Run** `python -m unittest server_docker.tests.test_ui_routes server_docker.tests.test_frontend_contract -v` and verify pass.
- [ ] **Step 9: Commit** `feat: redesign WebUI with themes presets and history`.

### Task 5: Docker regression, real VPS deployment, proxy verification, and screenshots

**Files:**
- Modify as needed: `server_docker/scripts/deploy.sh`
- Modify as needed: `server_docker/tests/test_*.sh`
- Create/update: `docs/images/webui-login.png`
- Create/update: `docs/images/webui-home.png`
- Create: `docs/images/webui-history.png`
- Create: `docs/images/webui-light.png`

**Interfaces:**
- Deployment remains `./scripts/deploy.sh --dry-run` then `./scripts/deploy.sh`.

- [ ] **Step 1: Run all Python tests** with `python -m unittest discover -s server_docker/tests -p 'test_*.py' -v`; expected 0 failures/errors.
- [ ] **Step 2: Run all existing shell deployment tests** `for t in server_docker/tests/test_*.sh; do bash "$t"; done`; expected every test prints PASS/exits 0.
- [ ] **Step 3: Run syntax/config checks** for Python compile, `bash -n` on shell scripts, and Docker Compose config with safe sample env.
- [ ] **Step 4: Deploy the verified tree to the existing Ubuntu VPS** without changing its Docker daemon; run dry-run first and confirm unrelated containers remain present.
- [ ] **Step 5: Verify real login and UI routes** on the VPS, including Light/Dark/System switching and preset population in a browser.
- [ ] **Step 6: Run a real parse with a WeChat Channels URL** and verify successful Yuanbao content, MD output, optional MP4, history row, file downloads, and media validity.
- [ ] **Step 7: Exercise retry behavior** using a controlled transient Browserless/upstream failure or test hook and verify automatic attempt progression without repeated button presses.
- [ ] **Step 8: Verify reverse-proxy headers** with trusted forwarded host/proto/prefix and verify returned absolute URL + relative path; confirm spoofed headers are ignored when trust is false.
- [ ] **Step 9: Verify history deletion** removes the selected DB row and owned MP4/text file but leaves neighboring jobs untouched.
- [ ] **Step 10: Capture final screenshots** for login, dark workspace, light workspace, and history; ensure no credentials/secrets are visible.
- [ ] **Step 11: Commit** `test: verify upgraded Docker workflow on VPS` if deployment/test artifacts require source changes; otherwise record results in README task.

### Task 6: README release documentation, secret scan, and GitHub push

**Files:**
- Modify: `README.md`
- Modify: `server_docker/README.md`
- Modify: `.gitignore`
- Modify: `server_docker/.dockerignore`
- Use: `docs/images/*.png`

**Interfaces:**
- Documentation must describe the shipped interfaces exactly; no undocumented setup dependency may remain.

- [ ] **Step 1: Update root README** with new UI screenshots, retry/history/TXT-MD/presets/theme/delete/proxy features, current Docker installation flow, Browserless behavior, Cookie/session placement, and API response examples including `download_path` and retry metadata.
- [ ] **Step 2: Update Docker README** with environment variables `MAX_PARSE_ATTEMPTS`, `TRUST_PROXY_HEADERS`, `PROXY_HOPS`, reverse-proxy Nginx/Caddy examples, migration behavior, and history DB location.
- [ ] **Step 3: Verify README links and commands** programmatically; confirm every referenced image/file exists and deploy commands point at tracked scripts.
- [ ] **Step 4: Run full verification again**: Python tests, shell tests, compile/syntax, Compose config, and a local/remote health check.
- [ ] **Step 5: Run secret/path scan** to confirm no Cookie/session, `.env`, history DB, downloads, VPS/WebUI passwords, Browserless token, or SSH credentials are tracked or present in the release diff.
- [ ] **Step 6: Review `git diff --check`, `git status`, and commit history**; ensure only intended release files are included.
- [ ] **Step 7: Commit documentation/release changes** with `docs: document history retry and proxy-safe WebUI`.
- [ ] **Step 8: Push `main` to `https://github.com/samni728/WechatVideoDL.git`** and verify remote HEAD/file contents through GitHub after push.
