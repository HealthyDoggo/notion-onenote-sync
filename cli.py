"""Click CLI for manual sync operations and status checks."""

import json
import logging
import shutil
import sys
from pathlib import Path

import click
from tqdm import tqdm

import config
from state_db import SyncStateDB
from sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Notion -> OneNote Class Notebook sync tool."""
    pass


def _run_sync(engine: SyncEngine, full: bool) -> dict:
    """Fetch pages, show a progress bar, and sync."""
    click.echo("Fetching pages from Notion...")
    roots, total = engine.fetch_pages(full=full)
    click.echo(f"  {total} page(s) in {len(roots)} root topic(s)")

    with tqdm(total=total, desc="Syncing", unit="page") as bar:
        stats = engine.sync_fetched(
            roots, full=full, progress_callback=lambda: bar.update(1),
        )
    return stats


@cli.command()
@click.option("--full", is_flag=True, help="Ignore last-sync timestamps and sync everything.")
@click.option("--force", is_flag=True, help="Alias for --full.")
def sync(full: bool, force: bool):
    """Run a forward sync (Notion -> OneNote)."""
    full = full or force
    engine = SyncEngine()

    stats = _run_sync(engine, full=full)
    click.echo(
        f"  Created: {stats['created']}  Updated: {stats['updated']}  "
        f"Skipped (folders/duplicates): {stats['skipped']}  Errors: {stats['errors']}  "
        f"Sections: {stats['sections_created']}"
    )
    if stats["errors"] > 0:
        sys.exit(1)


@cli.command()
def status():
    """Show sync status summary."""
    db = SyncStateDB()
    counts = db.count_by_status()
    total = sum(counts.values())
    last_sync = db.get_last_sync_time()

    click.echo(f"Total tracked pages: {total}")
    click.echo(f"Last sync:           {last_sync or 'never'}")
    click.echo()
    for s in ("synced", "pending", "error"):
        click.echo(f"  {s:>10}: {counts.get(s, 0)}")

    errors = db.get_errors()
    if errors:
        click.echo()
        click.echo("Recent errors:")
        for e in errors[:5]:
            click.echo(f"  - {e['notion_title'] or e['notion_page_id']} (last synced: {e['last_synced']})")


@cli.command()
def pages():
    """List all tracked pages."""
    db = SyncStateDB()
    rows = db.get_all()
    if not rows:
        click.echo("No pages tracked yet.")
        return

    click.echo(f"{'Title':<40} {'Status':<10} {'Section':<10} {'Last Synced'}")
    click.echo("-" * 90)
    for r in rows:
        title = (r["notion_title"] or "(untitled)")[:38]
        level = r.get("page_level", -1)
        indent = "  " * max(0, level + 1) if level >= 0 else ""
        section = "section" if level == -1 else f"L{level}"
        click.echo(
            f"{indent}{title:<{40 - len(indent)}} {r['sync_status']:<10} {section:<10} {r['last_synced'] or '-'}"
        )


@cli.command()
def retry_errors():
    """Re-sync all pages currently in error state."""
    db = SyncStateDB()
    errors = db.get_errors()
    if not errors:
        click.echo("No pages in error state.")
        return

    click.echo(f"Resetting {len(errors)} errored page(s) to pending...")
    for e in errors:
        db.set_status(e["notion_page_id"], "pending")

    engine = SyncEngine()
    stats = _run_sync(engine, full=True)
    click.echo(
        f"  Created: {stats['created']}  Updated: {stats['updated']}  "
        f"Skipped (folders/duplicates): {stats['skipped']}  Errors: {stats['errors']}"
    )


@cli.command()
@click.option("--errors", is_flag=True, help="Show only failed runs.")
@click.option("--page", type=str, default=None, help="Filter by page title (substring match).")
@click.option("-n", "--limit", type=int, default=20, help="Number of runs to show.")
def runs(errors: bool, page: str, limit: int):
    """Show recent Power Automate run history."""
    db = SyncStateDB()

    if errors:
        rows = db.get_error_runs(limit=limit)
    else:
        rows = db.get_recent_runs(limit=limit)

    if page:
        rows = [r for r in rows if page.lower() in (r.get("page_title") or "").lower()]

    if not rows:
        click.echo("No runs found.")
        return

    for r in rows:
        ts = (r["timestamp"] or "")[:19]
        title = (r["page_title"] or "?")[:35]
        action = r["action"]
        status = r["status"] or "success"
        run_url = r["run_url"] or ""

        status_marker = "x" if status == "error" else " "
        line = f"  [{status_marker}] {ts}  {action:<8} {title:<35}"
        if run_url:
            line += f"  {run_url}"
        click.echo(line)

        if status == "error" and r.get("error_message"):
            click.echo(f"      Error: {r['error_message'][:120]}")


@cli.command()
def init():
    """Initialise the sync state database."""
    db = SyncStateDB()
    click.echo(f"Database initialised at {config.DB_PATH}")


@cli.command()
@click.argument("onenote_page_id")
@click.option("--section-id", default=None, help="OneNote section ID (optional).")
def delete(onenote_page_id: str, section_id: str):
    """Delete a single OneNote page by its page ID."""
    from pa_bridge import PAForwardClient
    pa = PAForwardClient()
    click.echo(f"Deleting page {onenote_page_id}...")
    resp = pa.delete_page(onenote_page_id, onenote_section_id=section_id)
    click.echo(f"  Response: {resp}")

    db = SyncStateDB()
    rec = db.get_by_onenote_id(onenote_page_id)
    if rec:
        db.delete_page(rec["notion_page_id"])
        click.echo(f"  Removed DB record for '{rec['notion_title']}'")


def _find_orphans(backup_path: Path) -> list[dict]:
    """Compare backup DB with current DB to find orphaned OneNote pages."""
    current_db = SyncStateDB()
    backup_db = SyncStateDB(db_path=backup_path)

    current_onenote_ids = {
        r["onenote_page_id"]
        for r in current_db.get_all()
        if r.get("onenote_page_id")
    }

    old_pages = backup_db.get_all_with_onenote_ids()
    return [
        p for p in old_pages
        if p["onenote_page_id"] not in current_onenote_ids
    ]


def _delete_orphans(orphans: list[dict]) -> tuple:
    """Delete orphaned pages via PA. Returns (deleted, errors)."""
    from pa_bridge import PAForwardClient
    pa = PAForwardClient()
    deleted = 0
    errors = 0

    with tqdm(total=len(orphans), desc="Deleting old pages", unit="page") as bar:
        for p in orphans:
            try:
                pa.delete_page(
                    p["onenote_page_id"],
                    onenote_section_id=p.get("onenote_section_id"),
                )
                deleted += 1
            except Exception:
                logger.warning(
                    "Failed to delete '%s'", p.get("notion_title"), exc_info=True,
                )
                errors += 1
            bar.update(1)

    return deleted, errors


@cli.command()
@click.option("--dry-run", is_flag=True, help="List pages that would be deleted without deleting.")
@click.confirmation_option(prompt="Delete all orphaned OneNote pages not in the current sync state?")
def purge_orphans(dry_run: bool):
    """Delete OneNote pages that were orphaned by a reset.

    Compares the backup DB (sync_state.db.bak) with the current DB to find
    old OneNote page IDs that are no longer tracked, then deletes them.
    """
    backup_path = config.DB_PATH.with_suffix(".db.bak")
    if not backup_path.exists():
        click.echo("No backup DB found (sync_state.db.bak). Run 'reset' first.")
        return

    orphans = _find_orphans(backup_path)

    if not orphans:
        click.echo("No orphaned pages found.")
        return

    click.echo(f"Found {len(orphans)} orphaned page(s):")
    for p in orphans:
        click.echo(f"  - {p.get('notion_title', '?')}")

    if dry_run:
        click.echo("\nDry run — no pages deleted.")
        return

    deleted, errors = _delete_orphans(orphans)
    click.echo(f"\n  Deleted: {deleted}  Errors: {errors}")


@cli.command()
@click.option("--skip-harvest", is_flag=True, help="Skip reading teacher notes from OneNote before reset.")
@click.option("--harvest-file", type=click.Path(), default=None,
              help="Path to save/load harvested teacher notes JSON. Default: teacher_notes_backup.json")
@click.option("--purge", is_flag=True, help="Delete old orphaned OneNote pages after recreating.")
@click.confirmation_option(prompt="This will reset all OneNote tracking and recreate all pages. Continue?")
def reset(skip_harvest: bool, harvest_file: str, purge: bool):
    """Reset sync state and recreate all pages, preserving teacher notes.

    Steps:
      1. Read all current OneNote pages and extract red teacher text
      2. Back up the sync state database
      3. Clear OneNote tracking fields (page IDs, section IDs, content hashes)
      4. Run a full sync to recreate all pages with teacher notes merged in
      5. (--purge) Delete old orphaned OneNote pages
    """
    harvest_path = Path(harvest_file) if harvest_file else Path("teacher_notes_backup.json")
    engine = SyncEngine()
    db = SyncStateDB()

    # Step 1: Harvest teacher notes
    if skip_harvest:
        click.echo("Skipping harvest.")
        if harvest_path.exists():
            click.echo(f"Loading previously harvested notes from {harvest_path}")
            with open(harvest_path) as f:
                harvested = json.load(f)
            click.echo(f"  Loaded notes for {len(harvested)} page(s)")
        else:
            harvested = {}
    else:
        tracked = db.get_all_with_onenote_ids()
        harvest_count = len([p for p in tracked if p.get("onenote_section_id")])
        click.echo(f"Harvesting teacher notes from {harvest_count} page(s)...")

        with tqdm(total=harvest_count, desc="Harvesting", unit="page") as bar:
            harvested = engine.harvest_teacher_notes(
                progress_callback=lambda: bar.update(1),
            )
        click.echo(f"  Found teacher notes on {len(harvested)} page(s)")

        if harvested:
            with open(harvest_path, "w") as f:
                json.dump(harvested, f, indent=2)
            click.echo(f"  Saved to {harvest_path}")

    # Step 2: Back up the database
    backup_path = config.DB_PATH.with_suffix(".db.bak")
    shutil.copy2(config.DB_PATH, backup_path)
    click.echo(f"Database backed up to {backup_path}")

    # Step 3: Reset tracking fields
    db.reset_all()
    click.echo("Reset all OneNote tracking fields")

    # Step 4: Full sync with harvested notes
    engine._harvested_red = harvested
    stats = _run_sync(engine, full=True)
    click.echo(
        f"  Created: {stats['created']}  Updated: {stats['updated']}  "
        f"Skipped: {stats['skipped']}  Errors: {stats['errors']}  "
        f"Sections: {stats['sections_created']}"
    )

    if stats["errors"] > 0:
        click.echo("\nSome pages failed. Run 'retry-errors' after resolving issues.")
        if purge:
            click.echo("Skipping purge due to sync errors.")
        sys.exit(1)

    # Step 5: Purge old pages
    if purge:
        orphans = _find_orphans(backup_path)
        if orphans:
            click.echo(f"\nDeleting {len(orphans)} old orphaned page(s)...")
            deleted, errors = _delete_orphans(orphans)
            click.echo(f"  Deleted: {deleted}  Errors: {errors}")
        else:
            click.echo("\nNo orphaned pages to delete.")
    elif harvested:
        click.echo(
            "\nOld pages are still in OneNote — run 'purge-orphans' or "
            "use '--purge' next time to delete them."
        )


if __name__ == "__main__":
    cli()
