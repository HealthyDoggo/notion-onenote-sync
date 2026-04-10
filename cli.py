"""Click CLI for manual sync operations and status checks."""

import logging
import sys

import click

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


@cli.command()
@click.option("--full", is_flag=True, help="Ignore last-sync timestamps and sync everything.")
@click.option("--force", is_flag=True, help="Alias for --full.")
def sync(full: bool, force: bool):
    """Run a forward sync (Notion -> OneNote)."""
    full = full or force
    engine = SyncEngine()

    click.echo("Running forward sync (Notion -> OneNote)...")
    stats = engine.forward_sync(full=full)
    click.echo(
        f"  Created: {stats['created']}  Updated: {stats['updated']}  "
        f"Skipped: {stats['skipped']}  Errors: {stats['errors']}  "
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
    stats = engine.forward_sync(full=True)
    click.echo(
        f"  Created: {stats['created']}  Updated: {stats['updated']}  "
        f"Skipped: {stats['skipped']}  Errors: {stats['errors']}"
    )


@cli.command()
def init():
    """Initialise the sync state database."""
    db = SyncStateDB()
    click.echo(f"Database initialised at {config.DB_PATH}")


if __name__ == "__main__":
    cli()
