# WechatVideoDL WebUI / History / Retry Upgrade Design

Date: 2026-10-03
Status: Approved conversational design; awaiting written-spec review

## 1. Goal

Upgrade the existing single-account WechatVideoDL Docker application into a polished, reliable content workstation without changing its core Browserless + Yuanbao + yt-dlp architecture.

The upgrade must solve these user-facing problems:

- transient parse/download failures currently require manual repeated clicks;
- the frontend may try to parse an HTML proxy/error page as JSON and show `Unexpected token '<'`;
- completed jobs are not persisted as a browsable history;
- output text is fixed to TXT;
- prompts must be typed manually each time;
- the current UI is visually generic and has no theme system or product identity;
- generated download URLs can be wrong behind Nginx/Caddy/Cloudflare reverse proxies;
- users cannot delete old jobs and their files.

The system remains deliberately single-account. `WEBUI_USERNAME` / `WEBUI_PASSWORD` protect one shared history. Multi-user registration and per-user isolation are out of scope.

## 2. Product Experience

### 2.1 Main workspace

The authenticated home screen becomes a two-column content workspace on desktop and a single-column layout on small screens.

Header:

- new WechatVideoDL product mark / logo;
- product name and short status line;
- theme switch: Light / Dark / System;
- History entry;
- Logout.

Left / primary panel:

- WeChat Channels URL input;
- prompt preset selector;
- editable prompt textarea populated from the selected preset;
- output format selector: Markdown (`.md`) or plain text (`.txt`);
- MP4 download checkbox;
- Parse button.

Right / result panel:

- current state: queued / parsing / retrying / downloading / completed / failed;
- retry progress such as `Attempt 2 / 3`;
- Yuanbao response preview;
- MP4 and TXT/MD download actions;
- source URL metadata in a collapsible section;
- human-readable failure message and final attempt information.

The result panel should not expose raw JSON by default. A collapsible developer/details block may show it.

### 2.2 Prompt presets

Presets are convenience templates only. Selecting one copies the preset into the textarea; the textarea is always editable before submission.

Initial curated presets:

1. **脚本 / 字幕 / 文案提炼**  
   提炼视频的完整口播脚本、字幕脉络、核心文案与结构；去除重复表达，保留重要信息和关键原句，并按主题分段整理。

2. **工程项目核心框架**  
   针对工程、技术或项目型视频，提炼项目目标、背景、关键方案、实施步骤、关键技术、资源需求、风险、结果与可复用框架，形成结构化项目笔记。

3. **知识库标签与元数据**  
   为知识库导入提取：主题、3-10 个核心标签、人物/组织/产品/技术名词、关键结论、适用场景、检索关键词，并给出一段不超过 200 字的摘要。

4. **短视频卖点与传播结构**  
   拆解视频的开场钩子、核心卖点、论证方式、情绪节奏、转折点、行动号召与可复用传播话术，指出最值得复用的表达结构。

5. **结构化知识笔记**  
   将视频整理成可长期保存的知识笔记：一句话结论、核心观点、关键事实/步骤、值得延伸研究的问题、可执行清单，并保留必要上下文。

No preset-management UI is required in this release. Presets live in application configuration/code and can be changed in future versions.

## 3. Retry and Error Handling

### 3.1 Retry policy

Each submitted job has one persistent history record and may run up to **3 total attempts**.

Retry automatically for transient failures, including:

- Browserless timeout / temporary connection failure;
- Yuanbao page element timeout;
- missing preview URL after an otherwise valid Yuanbao page load;
- WeChat preview page failing to expose a video URL;
- temporary upstream 5xx / gateway-style failures;
- yt-dlp network/download failure when the source URL was successfully resolved.

Do **not** retry deterministic/user-action failures such as:

- invalid input URL;
- authentication missing or invalid;
- Yuanbao login/session expired;
- malformed request payload;
- unsupported output format.

Backoff: short bounded delays (for example 1s, then 2s) so the UI is responsive but does not hammer Yuanbao/WeChat.

### 3.2 Retry visibility

The API result includes:

- `attempts_used`;
- `max_attempts`;
- `retry_errors` containing sanitized per-attempt summaries.

History stores the same information.

### 3.3 Frontend response parsing

Frontend fetch logic must inspect status and `Content-Type` before parsing JSON.

If the reverse proxy returns HTML/plain text, show a message such as:

`服务器返回了非 JSON 错误（HTTP 502）。可能是反向代理或上游服务超时。`

A short response excerpt may be available in the developer details panel, but raw HTML must not be injected into the page.

The UI must never show the JavaScript parser exception `Unexpected token '<'` as the primary user-facing error.

## 4. History and Persistence

### 4.1 Storage

Use SQLite at:

`/data/history.db`

SQLite is appropriate because the application is single-account, single Gunicorn worker, and already serializes Browserless processing with a process lock.

### 4.2 Job record

`jobs` table fields:

- `id` — existing short job id such as `wxv_000012`;
- `created_at`, `updated_at`;
- `input_url`;
- `prompt`;
- `preset_id` / `preset_name` (nullable for custom prompt);
- `output_format` (`txt` or `md`);
- `download_requested`;
- `status` (`running`, `completed`, `failed`);
- `attempts_used`;
- `max_attempts`;
- `retry_errors_json`;
- `error_code`, `error_message`;
- `yuanbao_content`;
- `preview_url`;
- `source_direct_url`;
- `text_filename`;
- `video_filename`;
- `text_bytes`, `video_bytes`;
- `elapsed_ms`.

The database stores filenames, not trusted arbitrary filesystem paths.

### 4.3 History UI

History screen/drawer supports:

- newest-first list;
- status badge;
- created time;
- source URL;
- short prompt preview;
- output format;
- MP4 availability;
- TXT/MD download;
- MP4 download;
- detail view / expanded Yuanbao content;
- delete.

No search/filter system is required for the first release beyond simple status or text filtering if trivial to add.

### 4.4 Delete behavior

Deleting a history item is destructive and must require a clear confirmation in the UI.

`DELETE /api/history/<job_id>`:

- validates the job id format;
- looks up filenames from SQLite;
- deletes only files located under `/data/downloads` and owned by that record;
- deletes the database record;
- succeeds even when one optional file is already missing;
- never accepts a filesystem path from the client.

## 5. TXT / Markdown Output

Request field:

`output_format: "txt" | "md"`

Default: `md` in WebUI; API remains backward-compatible by defaulting to `txt` when the field is omitted if necessary to avoid breaking existing clients. If compatibility is not important, both may default to `md`; implementation plan should choose one explicitly.

TXT output remains close to the current format.

Markdown output should be human-readable and knowledge-base friendly:

```markdown
# WechatVideoDL Analysis

- ID: wxv_000012
- Source: https://weixin.qq.com/sph/...
- Created: 2026-10-03T...
- Preset: 知识库标签与元数据

## Prompt
...

## Yuanbao Response
...
```

The Yuanbao response itself is not rewritten merely to force Markdown; it is embedded as returned.

## 6. Reverse Proxy and Public URLs

### 6.1 Priority

Absolute URL generation follows this priority:

1. explicit `PUBLIC_BASE_URL` when configured;
2. trusted forwarded request headers (`X-Forwarded-Proto`, `X-Forwarded-Host`, optionally `X-Forwarded-Prefix`);
3. the direct Flask request scheme/host.

The UI should prefer relative paths such as `/files/wxv_000012.mp4`, which automatically remain on the current public domain.

API responses should return both:

- `download_path` — relative, proxy-safe;
- `download_url` — absolute convenience URL derived by the rules above.

### 6.2 Flask proxy handling

Use Werkzeug `ProxyFix` only when `TRUST_PROXY_HEADERS=true` (or equivalent) is set, with a documented one-proxy default. This avoids blindly trusting spoofed forwarding headers when the Flask port is directly exposed to untrusted networks.

README will include Nginx/Caddy examples that pass `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`.

## 7. API Surface

Existing:

- `POST /api/parse`
- `GET /health`
- `GET /files/<filename>`

Add:

- `GET /api/presets` — prompt preset list;
- `GET /api/history` — history list;
- `GET /api/history/<job_id>` — full record/detail;
- `DELETE /api/history/<job_id>` — delete record and owned files.

All `/api/*` history and parse routes require the same existing session/Basic authentication.

`POST /api/parse` gains:

- `output_format`;
- optional `preset_id` for history metadata.

It returns the final persisted job record plus file paths/URLs and retry metadata.

## 8. UI Architecture and Visual Direction

The UI will move out of large Python string literals into dedicated templates/static files:

- `templates/login.html`
- `templates/index.html`
- `templates/history.html` or an in-app history view
- `static/app.css`
- `static/app.js`
- `static/logo.*`

Visual direction:

- product-like editorial utility rather than generic admin dashboard;
- high readability and generous spacing;
- one restrained accent color;
- soft surfaces, deliberate borders, minimal gradients;
- typography hierarchy instead of oversized cards everywhere;
- responsive desktop/mobile layout;
- accessible focus states and contrast.

Themes:

- Light;
- Dark;
- System.

The preference is stored in `localStorage` and applied before paint when possible to avoid flashing.

A new logo/product mark is required and should work at favicon, navigation, and README sizes. The implementation phase will generate/select the asset and include it under `static/` / `docs/images/`.

## 9. Security

- Cookie/session files remain mounted secrets and ignored by Git.
- History DB is runtime data and ignored by Git.
- Download deletion is restricted to validated filenames under the download directory.
- HTML from upstream errors is escaped/text-only.
- User prompt/Yuanbao content are rendered as text unless passed through a safe Markdown renderer; no arbitrary HTML execution.
- Authentication behavior remains unchanged: one configured username/password.
- Browserless stays private to Docker networking where the local project instance is used.

## 10. Docker / Deployment Changes

Persist `/data` as already done; history DB naturally survives container recreation.

Add environment options with safe defaults:

- `MAX_PARSE_ATTEMPTS=3`
- `TRUST_PROXY_HEADERS=false`
- `PROXY_HOPS=1`

Existing `PUBLIC_BASE_URL` stays supported.

No new database container, Redis, Node runtime, or frontend build pipeline is introduced.

## 11. Migration

On startup:

- create `history.db` and schema if absent;
- optionally scan existing `wxv_*.txt`, `wxv_*.md`, `wxv_*.mp4` only if a safe/import feature is intentionally implemented.

For this release, automatic import of old files is **not required**. Existing files remain downloadable by direct path; new history begins with jobs created after the upgrade. This avoids guessing prompts/URLs for old files.

## 12. Testing and Verification

Automated tests should cover at minimum:

- retry succeeds on attempt 2/3;
- non-retryable error stops immediately;
- final retry failure persists a failed history record;
- TXT and MD output generation;
- history list/detail;
- delete removes DB record and owned files;
- deletion cannot escape download directory;
- proxy URL generation with direct Host and forwarded Host/Proto;
- `PUBLIC_BASE_URL` override;
- frontend/API returns JSON for application errors;
- existing Docker deployment safety tests continue to pass.

Manual/VPS verification:

1. login;
2. switch light/dark/system themes;
3. choose each prompt preset and confirm editable text population;
4. parse a real WeChat Channels URL;
5. verify retry state using a controlled transient failure test;
6. verify TXT and MD output;
7. verify MP4 download;
8. verify history detail and delete;
9. verify reverse-proxy URL behavior using forwarded headers or the actual proxy;
10. capture updated WebUI screenshots;
11. update root README and Docker README;
12. run secret scan and push only non-secret files to `samni728/WechatVideoDL`.

## 13. Out of Scope

- multiple user accounts / registration;
- quotas/billing;
- distributed workers / Redis queue;
- prompt preset CRUD UI;
- full-text search/indexing of historical content;
- automatic transcription independent of Yuanbao;
- cloud object storage.

## 14. Acceptance Criteria

The upgrade is accepted when:

- a transient failure is automatically retried up to 3 total attempts without manual clicking;
- the frontend never exposes raw JSON parser exceptions for HTML proxy errors;
- successful and failed jobs appear in persistent history;
- users can download `.txt` or `.md` plus optional `.mp4`;
- users can delete a history record and its owned files;
- five editable prompt presets are available;
- light/dark/system themes work;
- the UI uses the new product identity/logo and is responsive;
- reverse-proxy downloads use the correct public domain/path;
- VPS Docker deployment passes real end-to-end testing;
- README screenshots/features/configuration are updated;
- secrets are not committed;
- the final verified changes are pushed to `https://github.com/samni728/WechatVideoDL.git`.
