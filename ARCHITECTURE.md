# Notion → OneNote Class Notebook Sync System

**Architecture Document — March 2026 — v2.0**

| Direction | Trigger | Infrastructure |
|-----------|---------|----------------|
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

| Resource | ID |
|----------|-----|
| Class Notebook | `1-af3a0c0f-09c4-469a-bcbd-63668f2d4fc4` |
| Section Group (Sean Cassidy - LDE Learner) | `1-8874ae71-b532-4325-8454-9f796173a39e` |
| SharePoint site collection | `0436759b-4ed4-4e50-8500-6bb97acd351d` |
| SharePoint site | `72c4d145-1ad1-49fc-b664-53275e3cf578` |
| SharePoint destination path | `/sites/PsychologyKS525-27/SiteAssets/Psychology KS5 25-27 Notebook/Sean Cassidy - LDE Learner/` |
| Personal OneDrive notebook path | `Documents/Sean @ London Design and Engineering UTC/` |

---

## 2. Data Flow (Notion → OneNote)

| Step | Component | Action |
|------|-----------|--------|
| 1 | Pi — Cron / Manual | Triggers sync script (cron every 30 min, or CLI) |
| 2 | Pi — Notion Client | Queries Notion database for pages modified since last sync |
| 3 | Pi — Tree Builder | Builds parent-child hierarchy; root pages → OneNote sections, children → indented pages |
| 4 | Pi — Block Converter | Converts Notion blocks to OneNote-compatible HTML |
| 5 | Pi — Webhook Caller | POSTs `{ section_name, page_title, html_body, notion_page_id, action, ... }` to PA |
| 6 | Power Automate | Receives webhook, handles section creation (via .one copy workaround if needed), creates/updates page |
| 7 | PA → Pi | Returns `{ onenote_page_id, onenote_section_id, status }` |
| 8 | Pi — State DB | Records page mapping, content hash, section ID |

### 2.1 Hierarchy Mapping

Notion's database has a self-referencing "Parent item" relation. The sync engine builds a tree and maps it to OneNote:

| Notion Level | OneNote Equivalent | `page_level` value |
|-------------|-------------------|-------------------|
| Root page (depth 0) | OneNote section + level-0 page | -1 (section marker) |
| Child (depth 1) | Page at level 0 in parent's section | 0 |
| Grandchild (depth 2) | Page at level 1 | 1 |
| Great-grandchild+ (depth 3+) | Page at level 2 (capped) | 2 |

---

## 3. Component Details

### 3.1 Pi Sync Engine (Python)

**Runtime:** Python 3.9+
**Location:** `~/notion-onenote-sync/`

| Module | Responsibility |
|--------|---------------|
| `sync_engine.py` | Forward-only orchestrator. Builds page tree, drives section/page creation. |
| `notion_api.py` | Notion API wrapper. Queries database, fetches blocks, builds page tree. |
| `block_converter.py` | Notion blocks → OneNote HTML. Callout tables, rich text, headings, lists, code, toggles, images. |
| `pa_bridge.py` | HTTP client for PA webhook. Retry with exponential backoff. |
| `state_db.py` | SQLite state tracking: page mappings, content hashes, section IDs. |
| `cli.py` | Click CLI: `sync`, `status`, `pages`, `retry-errors`, `init`. |
| `config.py` | Loads `.env`, defines constants. |

### 3.2 State Database Schema

SQLite at `~/notion-onenote-sync/sync_state.db`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `notion_page_id` | TEXT UNIQUE | Notion page UUID |
| `onenote_page_id` | TEXT | OneNote page ID (from PA response) |
| `notion_title` | TEXT | Page title |
| `last_notion_edit` | DATETIME | Last modification from Notion API |
| `last_synced` | DATETIME | When this page was last synced |
| `content_hash` | TEXT | SHA-256 of normalised block content |
| `last_source` | TEXT | Always "notion" (one-way sync) |
| `sync_status` | TEXT | synced, pending, error |
| `parent_notion_id` | TEXT | Notion page ID of the parent page |
| `onenote_section_id` | TEXT | OneNote section ID (set on root pages) |
| `page_level` | INTEGER | -1 for sections, 0–2 for page indent level |

### 3.3 Webhook Payload

The Pi sends this JSON to Power Automate:

```json
{
  "section_name": "Social Influence",
  "page_title": "Conformity",
  "html_body": "<html>...<body>...</body></html>",
  "notion_page_id": "abc123-def456",
  "action": "create",
  "onenote_page_id": null,
  "onenote_section_id": "1-abc-def",
  "page_level": 0
}
```

| Field | Description |
|-------|-------------|
| `section_name` | Topic name — becomes the OneNote section name |
| `page_title` | Page title within the section |
| `html_body` | Full OneNote-compatible HTML |
| `notion_page_id` | Tracking ID |
| `action` | `"create"` or `"update"` |
| `onenote_page_id` | Existing page ID (for updates, null for creates) |
| `onenote_section_id` | Known section ID (skip lookup if provided) |
| `page_level` | 0, 1, or 2 — page indent level |

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

One flow handles everything: section creation (with .one file copy workaround), page creation, and page updates.

```
HTTP trigger (webhook from Pi)
│
├─ Is onenote_section_id provided?
│   ├─ YES → use it directly
│   └─ NO  → Get sections in Class Notebook
│            ├─ Filter by section_name
│            ├─ Found? → use existing section ID
│            └─ Not found? → Create section via .one copy
│
├─ Is action "create"?
│   └─ Create page in section with html_body
│
├─ Is action "update"?
│   └─ Update page content (append/replace)
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

**If YES — section ID not known:**

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

#### Step 3: Page Create or Update

**Condition**: `triggerBody()?['action']` equals `"create"`

**If CREATE:**

1. **Create page in a section** (OneNote Business connector)
   - Section: `var_section_id`
   - Page content: `triggerBody()?['html_body']`
   
2. **Set `var_onenote_page_id`** = page ID from create response

**If UPDATE:**

1. **Update page content** (OneNote Business connector)
   - Action: "Update page content"
   - Page ID: `var_onenote_page_id`
   - Content: append `triggerBody()?['html_body']`

   > **Note:** OneNote's update API is append-only. For full content replacement, the Pi should send a delete-and-recreate instruction instead, or use a target element ID. For now, appending works for incremental notes.

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

| Notion Block | OneNote HTML | Notes |
|-------------|-------------|-------|
| `paragraph` | `<p>text</p>` | |
| `heading_1` | `<h1>text</h1>` | |
| `heading_2` | `<h2>text</h2>` | |
| `heading_3` | `<h3>text</h3>` | |
| `bulleted_list_item` | `<ul><li>text</li></ul>` | Adjacent items grouped |
| `numbered_list_item` | `<ol><li>text</li></ol>` | Adjacent items grouped |
| `to_do` | `<p data-tag="to-do">text</p>` | Checked state preserved |
| `code` | `<pre><code>text</code></pre>` | Language in `data-notion-language` |
| `quote` | `<blockquote>text</blockquote>` | |
| `callout` | Styled `<table>` (icon + colour + children) | See §5.1 |
| `toggle` | Heading + indented content (flattened) | Lossy |
| `image` | `<img src="..." />` | |
| `divider` | `<hr />` | |
| `table` | `<table>...</table>` | |

> **Rich text annotations:** bold→`<b>`, italic→`<i>`, code→`<code>`, strike→`<s>`, underline→`<u>`, colour→`<span style>`, link→`<a href>`.

### 5.1 Callout Conversion

Callouts render as a single-row, two-column `<table>`: icon cell + content cell, with inline background colour and left-border accent. The `data-notion-type="callout"` and `data-notion-color` attributes allow lossless round-trip identification.

Nested callouts are supported up to 3 levels deep. Beyond that, content is flattened to indented paragraphs.

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

| Environment Variable | Source |
|---------------------|--------|
| `NOTION_TOKEN` | Notion integration page |
| `NOTION_DATABASE_ID` | Notion database URL |
| `NOTION_PARENT_PROPERTY` | Name of the parent relation property (default: "Parent") |
| `PA_FORWARD_WEBHOOK_URL` | Power Automate flow HTTP trigger URL |

### 6.3 Power Automate Setup

1. Create a new Instant flow with "When an HTTP request is received" trigger
2. Paste the JSON schema from §4.2
3. Build the flow as described in §4.2
4. Copy the generated HTTP POST URL into your `.env` as `PA_FORWARD_WEBHOOK_URL`
5. Test with: `python cli.py sync --full`

---

## 7. Scheduling & Error Handling

### 7.1 Triggers

| Trigger | Mechanism | Command |
|---------|-----------|---------|
| Scheduled sync | Pi cron job | `*/30 * * * * cd ~/notion-onenote-sync && .venv/bin/python cli.py sync` |
| Manual sync | CLI | `python cli.py sync` |
| Full re-sync | CLI | `python cli.py sync --full` |
| Retry errors | CLI | `python cli.py retry-errors` |
| Status check | CLI | `python cli.py status` |

### 7.2 Retry Logic

- PA webhook calls retry 3 times with exponential backoff (5s, 10s, 20s)
- Failed pages are marked `sync_status = "error"` in the state DB
- `python cli.py retry-errors` resets errored pages and triggers a full sync
- Structured logging on Pi; flow run history in Power Automate portal

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| OneNote doesn't pick up copied .one file | Section creation fails | 45s delay; retry the full create-copy-wait cycle |
| Timing — section not recognised after copy | Page creation in new section fails | PA retries with longer delay; Pi retries the whole request |
| Deleting personal notebook section affects Class NB copy | Data loss | Test thoroughly; the copy is independent once complete |
| OneNote update API is append-only | Can't replace page content cleanly | Delete + recreate for major changes; incremental append otherwise |
| Notion API rate limit (3 req/sec) | Slow for large databases | Rate limiting, Retry-After, incremental sync |
| Toggle blocks no OneNote equivalent | Lossy conversion | Flatten to heading + indented content |
| PA HTTP trigger URL changes if flow is recreated | Sync breaks | Store URL in .env; re-copy if flow is rebuilt |

---

## 9. Dropped from v1 Architecture

| Feature | Reason |
|---------|--------|
| Reverse sync (OneNote → Notion) | .one file copy is one-way; detecting granular OneNote changes requires Graph API access (blocked by school tenant) |
| Conflict resolution | Not needed — Notion is the single source of truth |
| Flask webhook on Pi | No reverse sync means no inbound webhook needed |
| Cloudflare Tunnel / ngrok | No inbound traffic to Pi required |

These can be revisited if the school grants app registration access in the future.

---

## 10. Project Structure

```
notion-onenote-sync/
├── sync_engine.py          # Forward-only orchestrator
├── notion_api.py           # Notion API wrapper + tree builder
├── block_converter.py      # Notion blocks → OneNote HTML
├── pa_bridge.py            # Power Automate webhook client + retry
├── state_db.py             # SQLite state management
├── cli.py                  # Click CLI (sync, status, pages, retry-errors, init)
├── config.py               # Loads .env, constants
├── .env                    # Secrets (gitignored)
├── .env.example            # Template
├── sync_state.db           # SQLite database (gitignored)
├── requirements.txt        # Python deps
├── tests/                  # Unit tests
│   ├── test_sync_engine.py
│   ├── test_notion_api.py
│   ├── test_state_db.py
│   └── test_block_converter.py
└── ARCHITECTURE.md         # This file
```

---

## 11. Implementation Phases

| Phase | Tasks | Estimate |
|-------|-------|----------|
| **Phase 1** — Pi Engine | Notion client, block converter, state DB, PA webhook caller, CLI | Done |
| **Phase 2** — Power Automate Flow | Build the single flow per §4.2, test section creation workaround | 1 day |
| **Phase 3** — Scheduling | Cron setup, incremental sync, structured logging | 0.5 day |
| **Phase 4** — Hardening | Test .one copy timing, retry tuning, edge cases | 0.5 day |

> **Recommendation:** Test the .one file copy workaround manually first (Phase 2) to validate timing and confirm that deleting the personal notebook section doesn't affect the Class Notebook copy. Once that's solid, the rest is straightforward.
