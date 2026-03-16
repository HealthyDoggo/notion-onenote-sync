# Notion ↔ OneNote Class Notebook Sync System

**Architecture Document — March 2026 — v1.0**

| Direction | Trigger | Infrastructure |
|-----------|---------|----------------|
| Bidirectional | Scheduled + Manual | Raspberry Pi + Power Automate |

---

## 1. System Overview

This document describes a hybrid sync system that keeps a Notion database in sync with your personal section in a OneNote Class Notebook. The primary direction is Notion → OneNote (pushing your notes to the class notebook), with a secondary reverse flow for OneNote → Notion.

### 1.1 Why Hybrid?

School M365 accounts typically restrict Azure AD app registrations, which blocks direct Graph API access via custom apps. Power Automate sidesteps this because it uses delegated permissions under your school identity. However, Power Automate has no native Notion connector, so a Pi-hosted script handles the Notion side.

> **Key Insight:** Power Automate acts as an authenticated bridge to the Microsoft Graph API. Your Pi script talks to Notion directly (API key) and to OneNote indirectly (via Power Automate webhook URLs). This avoids Azure AD app registration entirely.

### 1.2 Architecture Overview

```
┌──────────────────┐       ┌──────────────────────┐       ┌──────────────────┐
│    Notion API    │       │    Raspberry Pi       │       │  Power Automate  │
│  Database+Pages  │ ◄───► │  Python Sync Engine   │ ◄───► │  Graph API Bridge │
└──────────────────┘       └──────────────────────┘       └──────────────────┘
                                     │
                                     ▼
                    ┌────────────────────────────────┐
                    │   OneNote Class Notebook        │
                    │   Your Personal Section         │
                    │   Microsoft Graph API           │
                    └────────────────────────────────┘
```

---

## 2. Data Flow

### 2.1 Notion → OneNote (Primary)

Your Notion database entries (lesson plans, notes) are converted to HTML and pushed into OneNote pages within your Class Notebook section.

| Step | Component | Action |
|------|-----------|--------|
| 1 | Pi – Cron / Manual | Triggers sync script (cron every 30 min, or CLI manual trigger) |
| 2 | Pi – Notion Client | Queries Notion database API for pages modified since last sync |
| 3 | Pi – Block Parser | Fetches full block tree for each changed page, converts to OneNote-compatible HTML |
| 4 | Pi – HTTP POST | Sends page title + HTML body to Power Automate webhook URL |
| 5 | Power Automate | Receives webhook, creates or updates OneNote page via Graph connector |
| 6 | Pi – State DB | Records sync timestamp + page mapping (Notion ID ↔ OneNote page ID) |

### 2.2 OneNote → Notion (Secondary)

Reverse sync is trickier because OneNote's API is append-heavy and change detection is limited. We use Power Automate's OneNote trigger to detect modifications, then relay content back to the Pi.

| Step | Component | Action |
|------|-----------|--------|
| 1 | Power Automate | Trigger: "When a page is modified" in your Class Notebook section |
| 2 | Power Automate | Fetches page content via OneNote connector, POSTs to Pi webhook (Cloudflare Tunnel) |
| 3 | Pi – Flask Webhook | Receives page data, parses OneNote HTML back to structured content |
| 4 | Pi – Notion Client | Updates corresponding Notion page (looked up via state DB mapping) |
| 5 | Pi – State DB | Updates sync record, marks as "source: onenote" to prevent echo loops |

> **Echo Loop Prevention:** Each sync record stores the source of the last modification (`notion` or `onenote`). When the forward sync runs, it skips pages where source = onenote and the content hash hasn't changed. Same logic in reverse. This prevents infinite ping-pong updates.

---

## 3. Component Details

### 3.1 Pi Sync Engine (Python)

**Language:** Python 3.11+  
**Location:** `~/notion-onenote-sync/`

| Module | Responsibility |
|--------|---------------|
| `sync_engine.py` | Main orchestrator. Runs forward and reverse sync, manages state. |
| `notion_api.py` | Wraps the Notion API. Queries database, fetches block trees, creates/updates pages. |
| `block_converter.py` | Converts Notion blocks ↔ OneNote HTML. Handles headings, paragraphs, lists, code blocks, callouts, toggles, images. |
| `pa_bridge.py` | HTTP client for Power Automate webhooks. Sends page data for forward sync, exposes Flask endpoint for reverse sync. |
| `state_db.py` | SQLite database tracking page mappings, last sync times, content hashes, and modification sources. |
| `cli.py` | Click-based CLI for manual sync, status checks, and forced full re-sync. |

### 3.2 State Database Schema

SQLite database stored at `~/notion-onenote-sync/sync_state.db`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment row ID |
| `notion_page_id` | TEXT UNIQUE | Notion page UUID |
| `onenote_page_id` | TEXT | OneNote page ID (from Graph API) |
| `notion_title` | TEXT | Page title (for quick lookups) |
| `last_notion_edit` | DATETIME | Last modification time from Notion API |
| `last_onenote_edit` | DATETIME | Last modification time from Graph API |
| `last_synced` | DATETIME | When sync last ran for this page |
| `content_hash` | TEXT | SHA-256 hash of normalised content (for diff detection) |
| `last_source` | TEXT | Which side last modified: "notion" or "onenote" |
| `sync_status` | TEXT | Current status: synced, pending, conflict, error |

### 3.3 Power Automate Flows

You will need two separate flows in Power Automate, both running under your school M365 account.

**Flow 1: Notion → OneNote (Webhook Receiver)**

- **Trigger:** HTTP Request (When an HTTP request is received)
- Receives a JSON payload from the Pi containing the page title, HTML body, and an optional existing OneNote page ID
- If a page ID is provided, it updates; otherwise it creates a new page in your Class Notebook section
- Returns the OneNote page ID in the response

**Flow 2: OneNote → Notion (Change Relay)**

- **Trigger:** When a OneNote page is modified in a section
- Fetches the page content via the OneNote connector
- POSTs the HTML + page ID to your Pi's webhook endpoint (exposed via Cloudflare Tunnel)
- The Pi handles conversion and Notion update

### 3.4 Block Conversion Matrix

Mapping between Notion block types and OneNote HTML elements:

| Notion Block | OneNote HTML | Reverse? |
|-------------|-------------|----------|
| `paragraph` | `<p>text</p>` | Yes |
| `heading_1` | `<h1>text</h1>` | Yes |
| `heading_2` | `<h2>text</h2>` | Yes |
| `heading_3` | `<h3>text</h3>` | Yes |
| `bulleted_list_item` | `<ul><li>text</li></ul>` | Yes |
| `numbered_list_item` | `<ol><li>text</li></ol>` | Yes |
| `to_do` | `<p data-tag="to-do">text</p>` | Yes |
| `code` | `<pre><code>text</code></pre>` | Partial |
| `quote` | `<blockquote>text</blockquote>` | Partial |
| `callout` | Styled `<table>` with icon + colour + nested children (see §3.5) | Yes (round-trip) |
| `toggle` | Flattened to heading + indented content | No |
| `image` | `<img src="..." />` | Yes (re-upload) |
| `divider` | `<hr />` | Yes |
| `table` | `<table>...</table>` | Yes |

> **Rich Text Handling:** Notion's rich text annotations (bold, italic, code, strikethrough, underline, colour) map to inline HTML tags: `<b>`, `<i>`, `<code>`, `<s>`, `<u>`, and `<span style="color:...">`. Links become `<a href="...">`. The reverse parser strips OneNote-specific styling and maps back to Notion annotations.

### 3.5 Callout Conversion (Detailed)

Since callouts are the primary note-taking pattern, this is the most critical conversion in the system. The converter must handle icons, background colours, nested child blocks, and round-trip back to Notion with no data loss.

#### Notion Callout Structure

A Notion callout block looks like this from the API:

```json
{
  "type": "callout",
  "callout": {
    "rich_text": [{ "type": "text", "text": { "content": "Main callout text" } }],
    "icon": { "type": "emoji", "emoji": "💡" },
    "color": "blue_background",
    "children": [
      { "type": "paragraph", "paragraph": { "rich_text": [...] } },
      { "type": "bulleted_list_item", ... },
      { "type": "callout", ... }  // nested callouts are possible
    ]
  }
}
```

Key properties: `icon` (emoji or external URL), `color` (one of Notion's named background colours), `rich_text` (the callout's own text), and `children` (any nested blocks — paragraphs, lists, code, even other callouts).

#### Notion Colour → CSS Mapping

```python
NOTION_COLOURS = {
    "default":           ("#F7F6F3", "#37352F"),  # (background, border/accent)
    "gray_background":   ("#F1F1EF", "#9B9A97"),
    "brown_background":  ("#F4EEEE", "#9F6B53"),
    "orange_background": ("#FBECDD", "#D9730D"),
    "yellow_background": ("#FBF3DB", "#DFAB01"),
    "green_background":  ("#EDF3EC", "#0F7B6C"),
    "blue_background":   ("#E7F3F8", "#0B6E99"),
    "purple_background": ("#F4F0F7", "#6940A5"),
    "pink_background":   ("#F9EEF3", "#AD1A72"),
    "red_background":    ("#FDEBEC", "#E03E3E"),
}
```

#### Forward Conversion: Notion Callout → OneNote HTML

OneNote's Graph API accepts a subset of HTML. It does **not** support `<div>` with arbitrary CSS classes, but it **does** support `<table>` styling reliably. The strategy is to render each callout as a single-row, two-column table: icon cell + content cell, with an inline background colour and a left-border accent.

```python
def callout_to_onenote_html(block: dict, depth: int = 0) -> str:
    """Convert a Notion callout block to OneNote-compatible HTML."""
    callout = block["callout"]
    
    # Extract properties
    icon = get_icon(callout.get("icon"))  # returns emoji string or <img> tag
    color_key = callout.get("color", "default")
    bg_color, accent_color = NOTION_COLOURS.get(color_key, NOTION_COLOURS["default"])
    rich_text_html = rich_text_to_html(callout["rich_text"])
    
    # Recursively convert children
    children_html = ""
    if "children" in callout:
        for child in callout["children"]:
            children_html += block_to_html(child, depth=depth + 1)
    
    # Build the callout table
    # data-notion-type and data-notion-color are round-trip metadata attributes
    return f'''
    <table data-notion-type="callout" data-notion-color="{color_key}"
           style="border-collapse:collapse; width:100%; margin:{4 if depth > 0 else 8}px 0;">
      <tr>
        <td data-notion-icon="{icon}" style="
            width:28px; vertical-align:top; padding:8px 4px 8px 8px;
            background:{bg_color};
            border-left:3px solid {accent_color};
            border-top:1px solid {bg_color};
            border-bottom:1px solid {bg_color};
            font-size:18px;">
          {icon}
        </td>
        <td style="
            vertical-align:top; padding:8px 12px;
            background:{bg_color};
            border-right:1px solid {bg_color};
            border-top:1px solid {bg_color};
            border-bottom:1px solid {bg_color};">
          <p style="margin:0 0 4px 0;">{rich_text_html}</p>
          {children_html}
        </td>
      </tr>
    </table>
    '''
```

**Why a table, not a div?** OneNote's HTML rendering engine strips most `<div>` styling and ignores CSS classes entirely. Tables with inline styles are the most reliable way to get consistent visual output in OneNote across desktop, web, and mobile clients.

#### Reverse Conversion: OneNote HTML → Notion Callout

The reverse parser identifies callouts using the `data-notion-type="callout"` attribute embedded during forward conversion. This makes round-tripping lossless for pages that originated in Notion.

```python
def onenote_html_to_callout(table_element) -> dict:
    """Parse a OneNote callout table back to a Notion callout block."""
    # Extract round-trip metadata
    color_key = table_element.get("data-notion-color", "default")
    
    # Icon is in the first <td>
    icon_cell = table_element.find("td")
    icon_raw = icon_cell.get("data-notion-icon", "💡")
    
    # Content is in the second <td>
    content_cell = table_element.find_all("td")[1]
    
    # First <p> is the callout's own rich text
    first_p = content_cell.find("p")
    rich_text = html_to_rich_text(first_p)
    
    # Remaining elements are children — recurse
    children = []
    for sibling in first_p.find_next_siblings():
        # Check if it's a nested callout (another table with data-notion-type)
        if sibling.name == "table" and sibling.get("data-notion-type") == "callout":
            children.append(onenote_html_to_callout(sibling))
        else:
            children.append(html_element_to_notion_block(sibling))
    
    return {
        "type": "callout",
        "callout": {
            "rich_text": rich_text,
            "icon": {"type": "emoji", "emoji": icon_raw},
            "color": color_key,
            "children": children,
        }
    }
```

#### Handling Pages Created Directly in OneNote

For pages that were **not** created by the sync engine (e.g. a teacher adds content, or you write directly in OneNote), there are no `data-notion-type` attributes to key off. In this case, the reverse parser uses heuristic detection:

- **Single-row, two-column table** where the first column is narrow (< 40px) and contains only an emoji or small image → treat as a callout
- **Background colour on cells** → map to the nearest Notion colour using colour distance calculation
- **Fallback:** if the heuristic isn't confident, import as a regular Notion paragraph with a note in the state DB (`conversion_notes: "possible callout, imported as paragraph"`)

#### Nested Callout Depth Limit

Notion supports arbitrary nesting of callouts. The converter handles up to **3 levels of nesting** (which covers virtually all real usage). Beyond that, nested callouts are flattened into indented paragraphs with a visual marker to prevent excessively deep HTML tables that render poorly in OneNote.

#### Visual Example

A Notion callout like:

```
💡 blue_background
├── "Key concept: Newton's Third Law"
├── paragraph: "Every action has an equal and opposite reaction."
├── bulleted_list_item: "Force pairs act on different objects"
└── 📝 yellow_background (nested callout)
    └── "Remember: forces are vectors!"
```

Renders in OneNote as a styled table with a blue background, left accent border, 💡 icon, and the nested 📝 callout as an indented yellow table within the content cell.

---

## 4. Setup Guide

### 4.1 Notion Integration Setup

1. **Create Integration:** Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and click "New Integration"
2. **Name it:** Something like "OneNote Sync". Select your workspace. Give it Read content, Update content, and Insert content permissions.
3. **Copy the token:** Save the Internal Integration Token (starts with `ntn_`). Store this in a `.env` file on the Pi.
4. **Share your database:** Open your Notion database, click ••• → Connections → add your integration. This grants the integration access to that specific database.
5. **Note the database ID:** From the database URL: `notion.so/{workspace}/{database_id}?v=...` — copy the 32-character hex ID.

### 4.2 Power Automate Flow Setup

**Flow 1 — Forward Sync (Notion → OneNote):**

1. **Create a new Instant flow** with trigger "When an HTTP request is received"
2. **Set the JSON schema** for the request body: `{ title: string, html_body: string, onenote_page_id: string|null }`
3. **Add a Condition** action: check if `onenote_page_id` is null
4. **If null → Create Page**: Use the OneNote connector "Create page in a section". Select your Class Notebook and your personal section. Pass the HTML body.
5. **If not null → Update Page**: Use an HTTP action calling `PATCH https://graph.microsoft.com/v1.0/me/onenote/pages/{id}/content` with the update payload.
6. **Response**: Return the OneNote page ID in the HTTP response body so the Pi can store the mapping.

> **Important: OneNote Update Limitations.** The Graph API's PATCH endpoint for OneNote pages only supports appending content and replacing specific elements by ID. You cannot replace the full page body. For significant changes, the sync engine uses a strategy of: (1) append new content, (2) mark old sections for removal. For major rewrites, it's more reliable to delete and recreate the page.

**Flow 2 — Reverse Sync (OneNote → Notion):**

1. **Create an Automated flow** with trigger "When a OneNote page is modified". Select your Class Notebook section.
2. **Get page content**: Use the OneNote connector to fetch the full page content as HTML.
3. **HTTP POST**: Send the page ID + HTML body to your Pi's webhook endpoint (e.g. `https://your-tunnel.trycloudflare.com/webhook/onenote`)
4. **The Pi handles the rest**: conversion + Notion update.

### 4.3 Pi Environment Setup

**Prerequisites:** Python 3.11+, pip, SQLite3 (pre-installed on Raspberry Pi OS)

| Environment Variable | Source | Example |
|---------------------|--------|---------|
| `NOTION_TOKEN` | notion.so/my-integrations | `ntn_abc123...` |
| `NOTION_DATABASE_ID` | Database URL | `a1b2c3d4e5f6...` |
| `PA_FORWARD_WEBHOOK_URL` | Power Automate Flow 1 | `https://prod-xx.westeurope.logic.azure.com/...` |
| `PA_REVERSE_WEBHOOK_SECRET` | Your chosen secret | `supersecretkey123` |
| `FLASK_PORT` | Your choice | `5123` |

**Exposing the Pi webhook:**

- **Option A: Cloudflare Tunnel** (recommended) — you already use Cloudflare for DNS. Run `cloudflared tunnel` to expose your Flask endpoint. Free, stable, no port forwarding needed.
- **Option B: Tailscale Funnel** — if your Pi is on Tailscale, use Funnel to expose the endpoint. Simple but requires Tailscale.
- **Option C: ngrok** — quick for testing but the free tier URL changes on restart.

---

## 5. Scheduling & Triggers

| Trigger | Mechanism | Detail |
|---------|-----------|--------|
| Scheduled (forward) | Pi cron job | `*/30 * * * *` — every 30 minutes. Runs `sync_engine.py --direction forward` |
| Manual (forward) | CLI command | `python cli.py sync --direction forward --force` |
| Manual (full) | CLI command | `python cli.py sync --direction both --full` (ignores last-sync timestamps) |
| Automatic (reverse) | Power Automate trigger | Fires on page modification in your OneNote section |
| Status check | CLI command | `python cli.py status` (shows pending syncs, errors, last run time) |

---

## 6. Conflict Resolution

Conflicts occur when a page is modified on both sides between syncs. The system handles this conservatively:

- **Last-write-wins with manual override:** By default, the most recently modified version wins. If both sides changed within 5 minutes of each other, the page is marked as "conflict" and skipped until you resolve it via the CLI.
- **Conflict CLI:** `python cli.py conflicts` lists all conflicted pages. `python cli.py resolve <id> --keep notion|onenote` resolves a conflict by choosing a side.
- **Content hashing:** SHA-256 of normalised content (stripped of whitespace / formatting differences) prevents false positives where the content is identical but timestamps differ.

---

## 7. Known Limitations & Mitigations

| Limitation | Impact | Mitigation |
|-----------|--------|------------|
| OneNote pages are append-only (PATCH API) | Cannot fully replace page content | Delete + recreate for major changes; append for minor updates |
| No native Notion connector in Power Automate | Cannot trigger on Notion changes directly | Pi polls Notion on a schedule; manual trigger for immediate sync |
| OneNote change trigger may have delay | Reverse sync not instant | Power Automate triggers typically fire within 1–5 minutes |
| Notion API rate limit: 3 requests/sec | Slow for large databases | Batch queries, respect Retry-After header, incremental sync only |
| Toggle blocks have no OneNote equivalent | Lossy conversion | Flatten to heading + indented content; marked in state DB as lossy |
| OneNote images are hosted by Microsoft | Need re-upload to Notion | Download from OneNote URL, upload to Notion via file upload endpoint |
| School account may restrict PA premium connectors | HTTP connector might be premium | Test first; fallback is using standard connectors only |

---

## 8. Project Structure

```
notion-onenote-sync/
├── sync_engine.py          # Main orchestrator
├── notion_api.py           # Notion API wrapper
├── block_converter.py      # Notion blocks ↔ HTML
├── pa_bridge.py            # Power Automate HTTP client + Flask webhook
├── state_db.py             # SQLite state management
├── cli.py                  # Click CLI for manual ops
├── config.py               # Loads .env, constants
├── .env                    # Secrets (gitignored)
├── sync_state.db           # SQLite database
├── requirements.txt        # Python deps
├── tests/                  # Unit tests
└── README.md
```

---

## 9. Implementation Plan

| Phase | Tasks | Estimate |
|-------|-------|----------|
| **Phase 1** — Core Forward Sync | Notion client + block converter + state DB + PA Flow 1 + CLI manual trigger | 1–2 days |
| **Phase 2** — Scheduled Sync | Cron setup, incremental sync (last_edited_time filtering), error handling + retries | 0.5 day |
| **Phase 3** — Reverse Sync | Flask webhook on Pi + Cloudflare Tunnel + PA Flow 2 + HTML → Notion parser | 1–2 days |
| **Phase 4** — Conflict Handling | Content hashing, conflict detection, CLI resolution, echo loop prevention | 0.5–1 day |

> **Recommendation:** Start with Phase 1. Get a single page syncing from Notion to OneNote via Power Automate. Once that pipeline is solid, the other phases build on top of it incrementally. The reverse sync (Phase 3) is the most complex piece and can be deferred.