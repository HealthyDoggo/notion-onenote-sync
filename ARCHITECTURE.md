# Notion → OneNote Class Notebook Sync System

**Architecture Document — March 2026 — v2.0**


| Direction                  | Trigger            | Infrastructure                |
| -------------------------- | ------------------ | ----------------------------- |
| One-way (Notion → OneNote) | Scheduled + Manual | Raspberry Pi + Power Automate |


---

## 1. System Overview

This system pushes notes from a Notion database into your personal section group within a OneNote Class Notebook. Notion is the source of truth; OneNote is a read-only destination.

### 1.1 Why Hybrid?

School M365 accounts restrict Azure AD app registrations, which blocks direct Graph API access. Power Automate sidesteps this because it uses delegated permissions under your school identity. However, Power Automate has no native Notion connector, so a Pi-hosted script handles the Notion side.

> **Key constraint:** The OneNote (Business) connector's "Create section" action targets notebooks but cannot target a specific *section group* within a Class Notebook. The SharePoint HTTP proxy (`_api/v2.0`) does not route OneNote Graph endpoints. The workaround: create sections in a personal OneDrive notebook, then copy the `.one` file into the Class Notebook's SharePoint folder.

### 1.2 Architecture Diagram

```
┌──────────────────┐       ┌──────────────────────┐       ┌───────────────────────┐
│    Notion API    │       │    Raspberry Pi       │       │   Power Automate      │
│  Database+Pages  │ ────► │  Python Sync Engine   │ ────► │   OneNote Bridge      │
└──────────────────┘       └──────────────────────┘       └───────────────────────┘
                                     │                              │
                                     │                              ▼
                                     │                   ┌──────────────────────┐
                                     │                   │  OneNote Class       │
                                     └──────────────────►│  Notebook Section    │
                                      (via PA webhook)   │  Group               │
                                                         └──────────────────────┘
```

### 1.3 Key IDs


| Resource                                   | ID                                                                                               |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Class Notebook                             | `1-af3a0c0f-09c4-469a-bcbd-63668f2d4fc4`                                                         |
| Section Group (Sean Cassidy - LDE Learner) | `1-8874ae71-b532-4325-8454-9f796173a39e`                                                         |
| SharePoint site collection                 | `0436759b-4ed4-4e50-8500-6bb97acd351d`                                                           |
| SharePoint site                            | `72c4d145-1ad1-49fc-b664-53275e3cf578`                                                           |
| SharePoint destination path                | `/sites/PsychologyKS525-27/SiteAssets/Psychology KS5 25-27 Notebook/Sean Cassidy - LDE Learner/` |
| Personal OneDrive notebook path            | `Documents/Sean @ London Design and Engineering UTC/`                                            |


---

## 2. Data Flow (Notion → OneNote)


| Step | Component            | Action                                                                                                |
| ---- | -------------------- | ----------------------------------------------------------------------------------------------------- |
| 1    | Pi — Cron / Manual   | Triggers sync script (cron every 30 min, or CLI)                                                      |
| 2    | Pi — Notion Client   | Queries Notion database for pages modified since last sync                                            |
| 3    | Pi — Tree Builder    | Builds parent-child hierarchy; root pages → OneNote sections, children → indented pages               |
| 4    | Pi — Block Converter | Converts Notion blocks to OneNote-compatible HTML                                                     |
| 5    | Pi — Webhook Caller  | POSTs `{ section_name, page_title, html_body, notion_page_id, action, ... }` to PA                    |
| 6    | Power Automate       | Receives webhook, handles section creation (via .one copy workaround if needed), creates/updates page |
| 7    | PA → Pi              | Returns `{ onenote_page_id, onenote_section_id, status }`                                             |
| 8    | Pi — State DB        | Records page mapping, content hash, section ID                                                        |


### 2.1 Hierarchy Mapping

Notion's database has a self-referencing "Parent item" relation. The sync engine builds a tree and maps it to OneNote **section groups (folders)** with a `**Main`** section inside the innermost group for leaf pages:


| Notion shape                    | `section_group_path` (sent to PA) | OneNote target                                                           |
| ------------------------------- | --------------------------------- | ------------------------------------------------------------------------ |
| Root topic page                 | `[RootTitle]`                     | Section group `RootTitle` → section `**Main**` → page                    |
| Leaf child under root           | `[RootTitle]`                     | Same `**Main**` as siblings (multiple pages in one section)              |
| Intermediate page with children | `[Root, …, SelfTitle]`            | Nested group per segment → `**Main**` under self for that page’s content |
| Leaf under nested parent        | `[Root, …, ParentTitle]`          | `**Main**` in the parent’s innermost group                               |


`section_name` in the payload remains the **root topic title** for logging and compatibility. Power Automate should walk `section_group_path` outer → inner, then resolve `**Main`**.


| Notion Level                 | OneNote Equivalent                                                 | `page_level` value             |
| ---------------------------- | ------------------------------------------------------------------ | ------------------------------ |
| Root page (depth 0)          | OneNote section + level-0 page                                     | -1 (section marker)            |
| Child (depth 1)              | Page at level 0 in parent's section                                | 0                              |
| Grandchild (depth 2)         | Page at level 1                                                    | 1                              |
| Great-grandchild+ (depth 3+) | Page at level ≥2 (capped by `PAGE_LEVEL_MAX` in `.env`, default 2) | `min(depth-1, PAGE_LEVEL_MAX)` |


---

## 3. Component Details

### 3.1 Pi Sync Engine (Python)

**Runtime:** Python 3.9+
**Location:** `~/notion-onenote-sync/`


| Module               | Responsibility                                                                                                          |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `sync_engine.py`     | Forward-only orchestrator. Builds page tree, drives section/page creation. Two-step merge for updates.                  |
| `notion_api.py`      | Notion API wrapper. Queries database, fetches blocks, builds page tree.                                                 |
| `block_converter.py` | Notion blocks → OneNote HTML with fingerprint style. Callout tables, rich text, headings, lists, code, toggles, images. |
| `html_merge.py`      | Merges fresh Notion HTML with teacher feedback from OneNote. Fingerprint + red text detection.                          |
| `pa_bridge.py`       | HTTP client for PA webhook. Supports create/read/replace actions. Retry with exponential backoff.                       |
| `state_db.py`        | SQLite state tracking: page mappings, content hashes, section IDs.                                                      |
| `cli.py`             | Click CLI: `sync`, `status`, `pages`, `retry-errors`, `init`.                                                           |
| `config.py`          | Loads `.env`, defines constants.                                                                                        |


### 3.2 State Database Schema

SQLite at `~/notion-onenote-sync/sync_state.db`


| Column               | Type        | Description                                                         |
| -------------------- | ----------- | ------------------------------------------------------------------- |
| `id`                 | INTEGER PK  | Auto-increment                                                      |
| `notion_page_id`     | TEXT UNIQUE | Notion page UUID                                                    |
| `onenote_page_id`    | TEXT        | OneNote page ID (from PA response)                                  |
| `notion_title`       | TEXT        | Page title                                                          |
| `last_notion_edit`   | DATETIME    | Last modification from Notion API                                   |
| `last_synced`        | DATETIME    | When this page was last synced                                      |
| `content_hash`       | TEXT        | SHA-256 of normalised block content                                 |
| `last_source`        | TEXT        | Always "notion" (one-way sync)                                      |
| `sync_status`        | TEXT        | synced, pending, error                                              |
| `parent_notion_id`   | TEXT        | Notion page ID of the parent page                                   |
| `onenote_section_id` | TEXT        | OneNote section ID (set on root pages)                              |
| `page_level`         | INTEGER     | -1 for sections; else 0…`PAGE_LEVEL_MAX` (default 2, set in `.env`) |


### 3.3 Webhook Payload

The Pi sends this JSON to Power Automate:

**Create** (new page):

```json
{
  "section_name": "Social Influence",
  "section_group_path": ["Social Influence"],
  "page_title": "Conformity",
  "html_body": "<html>...<body>...</body></html>",
  "notion_page_id": "abc123-def456",
  "action": "create",
  "onenote_page_id": null,
  "onenote_section_id": "1-abc-def",
  "page_level": 0
}
```

Nested example (leaf under a subfolder page `Neurons` under root `Biopsychology`): `"section_group_path": ["Biopsychology", "Neurons"]`.

**Read** (get current page for merge):

```json
{
  "action": "read",
  "onenote_page_id": "on-page-789"
}
```

**Replace** (merged update):

```json
{
  "section_name": "Social Influence",
  "page_title": "Conformity",
  "html_body": "<html>...<body>(merged HTML)...</body></html>",
  "notion_page_id": "abc123-def456",
  "action": "replace",
  "onenote_page_id": "on-page-789",
  "onenote_section_id": "1-abc-def",
  "page_level": 0
}
```


| Field                | Description                                            |
| -------------------- | ------------------------------------------------------ |
| `section_name`       | Topic name — becomes the OneNote section name          |
| `page_title`         | Page title within the section                          |
| `html_body`          | Full OneNote-compatible HTML                           |
| `notion_page_id`     | Tracking ID                                            |
| `action`             | `"create"`, `"read"`, or `"replace"`                   |
| `onenote_page_id`    | Existing page ID (for updates, null for creates)       |
| `onenote_section_id` | Known section ID (skip lookup if provided)             |
| `page_level`         | 0…`PAGE_LEVEL_MAX` — page indent level (default max 2) |


Power Automate responds with:

```json
{
  "onenote_page_id": "1-xyz-789",
  "onenote_section_id": "1-abc-def",
  "status": "success"
}
```

---

## 4. Power Automate Flow (Single Flow)

### 4.1 Overview

One flow handles everything: section creation (with .one file copy workaround), page creation, page reading (for merge), and page replacement.

The Pi sends three action types:

- **create** — new page in a section (section resolution may trigger .one copy)
- **read** — return current page HTML (used before updates, so the Pi can merge teacher feedback)
- **replace** — delete old page (if possible) + create new page with merged content

```
HTTP trigger (webhook from Pi)
│
├─ action = "read"?
│   └─ Get page content → return { current_html }
│
├─ Section group + Main resolution (create / replace / read when id empty):
│   ├─ Is onenote_section_id provided?
│   │   ├─ YES → use it directly (cached Main section Key)
│   │   └─ NO  → Walk section_group_path[] outer→inner:
│   │            get-or-create each section group, then get-or-create section "Main"
│   │            └─ If API cannot create → .one copy workaround (per segment / Main)
│   │
│   ├─ action = "create"?
│   │   └─ Create page in section with html_body
│   │
│   └─ action = "replace"?
│       ├─ Delete old page (if supported, otherwise orphaned)
│       └─ Create new page with merged html_body
│
└─ Response: { onenote_page_id, onenote_section_id, status }
```

### 4.2 Block-by-Block Flow Design

#### Trigger

**When an HTTP request is received**

- Method: POST
- JSON schema:

```json
{
  "type": "object",
  "properties": {
    "section_name":       { "type": "string" },
    "section_group_path": { "type": "array", "items": { "type": "string" } },
    "page_title":         { "type": "string" },
    "html_body":          { "type": "string" },
    "notion_page_id":     { "type": "string" },
    "action":             { "type": "string" },
    "onenote_page_id":    { "type": "string" },
    "onenote_section_id": { "type": "string" },
    "page_level":         { "type": "integer" }
  }
}
```

#### Step 1: Initialize Variables

- `var_section_id` (String) = `triggerBody()?['onenote_section_id']`
- `var_onenote_page_id` (String) = `triggerBody()?['onenote_page_id']`

#### Step 2: Section Resolution

**Condition**: `empty(var_section_id)` is true

**If YES — Main section Key not known:**

> **Section groups model:** Use `triggerBody()?['section_group_path']` (array, outer→inner). For each segment, ensure a **section group** exists under the current parent (notebook root, then prior group). In the **innermost** group, ensure a normal section named `**Main`**, then set `var_section_id` to that section’s **Key** (or Identifier, per connector). The steps below describe the **single-segment** list/filter pattern; repeat the list/create pattern per path level when `length(section_group_path) > 1`.

1. **Get sections** (OneNote Business connector)
  - Action: "Get sections in notebook"
  - Notebook: Class Notebook (`1-af3a0c0f-09c4-469a-bcbd-63668f2d4fc4`)
2. **Filter array**
  - From: sections list
  - Where: `item()?['displayName']` equals `triggerBody()?['section_name']`
3. **Condition**: filter result length > 0
  **If section exists:**
  - Set `var_section_id` = `first(body('Filter_array'))?['id']`
   **If section does NOT exist → .one file copy workaround:**
   a. **Create section in personal OneDrive notebook** (OneNote Business connector)
      - Action: "Create section"
      - Notebook: your personal notebook ("Sean @ London Design and Engineering UTC")
      - Section name: `triggerBody()?['section_name']`
   b. **Copy file** (SharePoint connector — "Copy file")
      - Source site: your OneDrive (or use OneDrive connector "Copy file")
      - Source path: `Documents/Sean @ London Design and Engineering UTC/@{triggerBody()?['section_name']}.one`
      - Destination site: `PsychologyKS525-27`
      - Destination path: `/SiteAssets/Psychology KS5 25-27 Notebook/Sean Cassidy - LDE Learner/@{triggerBody()?['section_name']}.one`
   c. **Delay** — 45 seconds
      - Allows OneNote to register the new `.one` file as a section
   d. **Get sections in notebook** (again)
      - Same notebook as Step 2.1
   e. **Filter array** (again)
      - Match on `section_name`
   f. **Set `var_section_id`** = `first(body('Filter_array_2'))?['id']`
   g. **Delete section from personal notebook** (cleanup)
      - Use OneDrive connector "Delete file"
      - Path: `Documents/Sean @ London Design and Engineering UTC/@{triggerBody()?['section_name']}.one`

**If NO — section ID was provided:**

- Skip to Step 3 (section ID already in `var_section_id`)

#### Step 3: Action Routing

**Switch** on `triggerBody()?['action']`:

**If READ:**

1. **Get page content** (OneNote Business connector)
  - Page ID: `triggerBody()?['onenote_page_id']`
2. **Response** (HTTP 200):
  ```json
   {
     "current_html": "<html>...</html>",
     "onenote_page_id": "@{triggerBody()?['onenote_page_id']}",
     "status": "success"
   }
  ```
   Return immediately (no section resolution needed).

**If CREATE:**

1. **Create page in a section** (OneNote Business connector)
  - Section: `var_section_id`
  - Page content: `triggerBody()?['html_body']`
2. **Set `var_onenote_page_id`** = page ID from create response

**If REPLACE:**

1. **Delete old page** (if the connector supports it)
  - Page ID: `triggerBody()?['onenote_page_id']`
  - If delete is not available, the old page remains orphaned
2. **Create page in section** (OneNote Business connector)
  - Section: `var_section_id`
  - Page content: `triggerBody()?['html_body']` (merged HTML from Pi)
3. **Set `var_onenote_page_id`** = new page ID from create response
  > **Note:** The html_body for replace actions contains the merged output from `html_merge.py` — fresh Notion content with teacher feedback preserved at the correct positions.

#### Step 4: Response

**Response** (HTTP 200):

```json
{
  "onenote_page_id": "@{var_onenote_page_id}",
  "onenote_section_id": "@{var_section_id}",
  "status": "success"
}
```

### 4.3 Section Resolution Flowchart

```
┌─────────────────────────────────────────┐
│ Is onenote_section_id in payload?       │
│                                         │
│  YES ──► Use it. Done.                  │
│                                         │
│  NO ──► Get all sections in notebook    │
│         Filter by section_name          │
│                                         │
│         Found? ──► Use existing ID      │
│                                         │
│         Not found? ──►                  │
│           1. Create section in personal │
│              OneDrive notebook          │
│           2. Copy .one file to Class    │
│              Notebook SharePoint path   │
│           3. Wait 45s                   │
│           4. Get sections again         │
│           5. Use new section ID         │
│           6. Delete from personal NB    │
└─────────────────────────────────────────┘
```

---

## 5. Block Conversion Matrix


| Notion Block         | OneNote HTML                                | Notes                              |
| -------------------- | ------------------------------------------- | ---------------------------------- |
| `paragraph`          | `<p>text</p>`                               |                                    |
| `heading_1`          | `<h1>text</h1>`                             |                                    |
| `heading_2`          | `<h2>text</h2>`                             |                                    |
| `heading_3`          | `<h3>text</h3>`                             |                                    |
| `bulleted_list_item` | `<ul><li>text</li></ul>`                    | Adjacent items grouped             |
| `numbered_list_item` | `<ol><li>text</li></ol>`                    | Adjacent items grouped             |
| `to_do`              | `<p data-tag="to-do">text</p>`              | Checked state preserved            |
| `code`               | `<pre><code>text</code></pre>`              | Language in `data-notion-language` |
| `quote`              | `<blockquote>text</blockquote>`             |                                    |
| `callout`            | Styled `<table>` (icon + colour + children) | See §5.1                           |
| `toggle`             | Heading + indented content (flattened)      | Lossy                              |
| `image`              | `<img src="..." />`                         |                                    |
| `divider`            | `<hr />`                                    |                                    |
| `table`              | `<table>...</table>`                        |                                    |


> **Rich text annotations:** bold→`<b>`, italic→`<i>`, code→`<code>`, strike→`<s>`, underline→`<u>`, colour→`<span style>`, link→`<a href>`.

### 5.1 Callout Conversion

Callouts render as a single-row, two-column `<table>`: icon cell + content cell, with inline background colour and left-border accent. The `data-notion-type="callout"` and `data-notion-color` attributes allow lossless round-trip identification.

Nested callouts are supported up to 3 levels deep. Beyond that, content is flattened to indented paragraphs.

### 5.2 Teacher Feedback Merge Strategy

When a Notion page is updated, the sync engine preserves teacher feedback that was added in OneNote. This uses a two-step flow:

1. **Read**: Pi asks PA to read the current OneNote page HTML
2. **Merge**: Pi combines fresh Notion HTML with preserved teacher content
3. **Replace**: Pi asks PA to delete the old page and create a new one with the merged HTML

#### How It Works

**Fingerprinting:** Every HTML element generated from Notion includes an invisible CSS marker: `letter-spacing:0.01pt`. This distinguishes synced content from teacher additions.

**Red text convention:** The teacher uses red text (`color:red`) for feedback. The converter never generates red, so any red text in OneNote is definitively from the teacher.

**Classification rules:**


| Element has fingerprint? | Contains red text? | Classification                   | Action                       |
| ------------------------ | ------------------ | -------------------------------- | ---------------------------- |
| Yes                      | No                 | Synced (Notion)                  | Replace with fresh version   |
| No                       | No                 | Teacher addition                 | Preserve at same position    |
| No                       | Yes                | Teacher feedback                 | Always preserve              |
| Yes                      | Yes                | Mixed (synced + inline feedback) | Replace text, keep red spans |


**Positional preservation:** Teacher content is tracked by its ordinal position between synced elements. If teacher feedback sits between the 2nd and 3rd Notion paragraphs, it's re-inserted between the 2nd and 3rd paragraphs in the updated output — even if Notion's content changed.

**Edge cases:**

- Teacher adds content at the very top or bottom → preserved
- Notion deletes a paragraph that had teacher content after it → teacher content moves to nearest surviving position
- Teacher edits text *inside* a fingerprinted paragraph (without red) → lost on next sync (Notion's version wins)
- Teacher adds inline red text inside a fingerprinted paragraph → red spans are extracted and preserved

---

## 6. Setup Guide

### 6.1 Notion Integration

1. Create at [notion.so/my-integrations](https://www.notion.so/my-integrations) — Read + Update + Insert permissions
2. Copy the Internal Integration Token (`ntn_...`) into `.env`
3. Share your database with the integration
4. Note the database ID from the URL

### 6.2 Pi Environment

**Prerequisites:** Python 3.9+, pip, SQLite3

```bash
cd ~/notion-onenote-sync
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your tokens and webhook URL
python cli.py init
```


| Environment Variable     | Source                                                   |
| ------------------------ | -------------------------------------------------------- |
| `NOTION_TOKEN`           | Notion integration page                                  |
| `NOTION_DATABASE_ID`     | Notion database URL                                      |
| `NOTION_PARENT_PROPERTY` | Name of the parent relation property (default: "Parent") |
| `PA_FORWARD_WEBHOOK_URL` | Power Automate flow HTTP trigger URL                     |


### 6.3 Power Automate Setup

1. Create a new Instant flow with "When an HTTP request is received" trigger
2. Paste the JSON schema from §4.2
3. Build the flow as described in §4.2
4. Copy the generated HTTP POST URL into your `.env` as `PA_FORWARD_WEBHOOK_URL`
5. Test with: `python cli.py sync --full`

---

## 7. Scheduling & Error Handling

### 7.1 Triggers


| Trigger        | Mechanism   | Command                                                                 |
| -------------- | ----------- | ----------------------------------------------------------------------- |
| Scheduled sync | Pi cron job | `*/30 * * * * cd ~/notion-onenote-sync && .venv/bin/python cli.py sync` |
| Manual sync    | CLI         | `python cli.py sync`                                                    |
| Full re-sync   | CLI         | `python cli.py sync --full`                                             |
| Retry errors   | CLI         | `python cli.py retry-errors`                                            |
| Status check   | CLI         | `python cli.py status`                                                  |


### 7.2 Retry Logic

- PA webhook calls retry 3 times with exponential backoff (5s, 10s, 20s)
- Failed pages are marked `sync_status = "error"` in the state DB
- `python cli.py retry-errors` resets errored pages and triggers a full sync
- Structured logging on Pi; flow run history in Power Automate portal

---

## 8. Risks & Mitigations


| Risk                                                     | Impact                             | Mitigation                                                                                                                           |
| -------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| OneNote doesn't pick up copied .one file                 | Section creation fails             | 45s delay; retry the full create-copy-wait cycle                                                                                     |
| Timing — section not recognised after copy               | Page creation in new section fails | PA retries with longer delay; Pi retries the whole request                                                                           |
| Deleting personal notebook section affects Class NB copy | Data loss                          | Test thoroughly; the copy is independent once complete                                                                               |
| OneNote update API is append-only                        | Can't replace page content cleanly | Two-step merge: read page → merge on Pi → replace (delete + create). Teacher feedback preserved via fingerprint + red text detection |
| Teacher edits non-red text inside fingerprinted element  | Those edits are lost on next sync  | Teacher convention: always use red for feedback. Non-red edits in synced elements are treated as Notion content                      |
| OneNote page delete not available in connector           | Old pages accumulate as orphans    | New page gets the tracked ID; old pages can be manually cleaned. If connector supports delete, it's used automatically               |
| Notion API rate limit (3 req/sec)                        | Slow for large databases           | Rate limiting, Retry-After, incremental sync                                                                                         |
| Toggle blocks no OneNote equivalent                      | Lossy conversion                   | Flatten to heading + indented content                                                                                                |
| PA HTTP trigger URL changes if flow is recreated         | Sync breaks                        | Store URL in .env; re-copy if flow is rebuilt                                                                                        |


---

## 9. Dropped from v1 Architecture


| Feature                         | Reason                                                                                                             |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Reverse sync (OneNote → Notion) | .one file copy is one-way; detecting granular OneNote changes requires Graph API access (blocked by school tenant) |
| Conflict resolution             | Not needed — Notion is the single source of truth                                                                  |
| Flask webhook on Pi             | No reverse sync means no inbound webhook needed                                                                    |
| Cloudflare Tunnel / ngrok       | No inbound traffic to Pi required                                                                                  |


These can be revisited if the school grants app registration access in the future.

---

## 10. Project Structure

```
notion-onenote-sync/
├── sync_engine.py          # Forward-only orchestrator (two-step merge for updates)
├── notion_api.py           # Notion API wrapper + tree builder
├── block_converter.py      # Notion blocks → OneNote HTML (with fingerprint)
├── html_merge.py           # Merge fresh Notion HTML with teacher feedback
├── pa_bridge.py            # Power Automate webhook client (create/read/replace + retry)
├── state_db.py             # SQLite state management
├── cli.py                  # Click CLI (sync, status, pages, retry-errors, init)
├── config.py               # Loads .env, constants, fingerprint/feedback config
├── .env                    # Secrets (gitignored)
├── .env.example            # Template
├── sync_state.db           # SQLite database (gitignored)
├── requirements.txt        # Python deps
├── tests/                  # Unit tests
│   ├── test_sync_engine.py
│   ├── test_notion_api.py
│   ├── test_state_db.py
│   ├── test_block_converter.py
│   └── test_html_merge.py
├── docs/                   # Mermaid flow diagrams
│   ├── flow-main.mmd              # PA main flow (create/read/replace)
│   ├── flow-update-merge.mmd      # Two-step merge detail
│   ├── flow-section-resolution.mmd # Section resolution detail
│   └── flow-system-overview.mmd   # System architecture overview
└── ARCHITECTURE.md         # This file
```

---

## 11. Implementation Phases


| Phase                             | Tasks                                                            | Estimate |
| --------------------------------- | ---------------------------------------------------------------- | -------- |
| **Phase 1** — Pi Engine           | Notion client, block converter, state DB, PA webhook caller, CLI | Done     |
| **Phase 2** — Power Automate Flow | Build the single flow per §4.2, test section creation workaround | 1 day    |
| **Phase 3** — Scheduling          | Cron setup, incremental sync, structured logging                 | 0.5 day  |
| **Phase 4** — Hardening           | Test .one copy timing, retry tuning, edge cases                  | 0.5 day  |


> **Recommendation:** Test the .one file copy workaround manually first (Phase 2) to validate timing and confirm that deleting the personal notebook section doesn't affect the Class Notebook copy. Once that's solid, the rest is straightforward.

