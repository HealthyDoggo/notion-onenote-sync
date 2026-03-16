"""Click CLI for manual sync operations, status checks, and conflict resolution."""

import logging
import sys
import threading

import click

import config
from pa_bridge import create_webhook_app, set_reverse_callback
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
    """Notion ↔ OneNote Class Notebook sync tool."""
    pass


@cli.command()
@click.option("--direction", type=click.Choice(["forward", "reverse", "both"]), default="forward")
@click.option("--full", is_flag=True, help="Ignore last-sync timestamps and sync everything.")
@click.option("--force", is_flag=True, help="Alias for --full.")
def sync(direction: str, full: bool, force: bool):
    """Run a sync cycle."""
    full = full or force
    engine = SyncEngine()

    if direction in ("forward", "both"):
        click.echo("Running forward sync (Notion → OneNote)...")
        stats = engine.forward_sync(full=full)
        click.echo(
            f"  Created: {stats['created']}  Updated: {stats['updated']}  "
            f"Skipped: {stats['skipped']}  Errors: {stats['errors']}"
        )

    if direction in ("reverse", "both"):
        click.echo("Reverse sync runs via the webhook server.")
        click.echo("Use 'cli.py serve' to start the webhook listener.")


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
    for s in ("synced", "pending", "conflict", "error"):
        click.echo(f"  {s:>10}: {counts.get(s, 0)}")

    errors = db.get_errors()
    if errors:
        click.echo()
        click.echo("Recent errors:")
        for e in errors[:5]:
            click.echo(f"  - {e['notion_title'] or e['notion_page_id']} (last synced: {e['last_synced']})")


@cli.command()
def conflicts():
    """List all pages in conflict state."""
    db = SyncStateDB()
    rows = db.get_conflicts()
    if not rows:
        click.echo("No conflicts.")
        return

    click.echo(f"{len(rows)} conflicted page(s):\n")
    for r in rows:
        click.echo(f"  Notion ID:  {r['notion_page_id']}")
        click.echo(f"  Title:      {r['notion_title'] or '(untitled)'}")
        click.echo(f"  Last edit:  Notion={r['last_notion_edit']}  OneNote={r['last_onenote_edit']}")
        click.echo(f"  Last sync:  {r['last_synced']}")
        click.echo()


@cli.command()
@click.argument("notion_page_id")
@click.option("--keep", type=click.Choice(["notion", "onenote"]), required=True)
def resolve(notion_page_id: str, keep: str):
    """Resolve a conflict by choosing which side to keep."""
    engine = SyncEngine()
    try:
        engine.resolve_conflict(notion_page_id, keep=keep)
        click.echo(f"Resolved: keeping {keep} version for {notion_page_id}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
def pages():
    """List all tracked pages."""
    db = SyncStateDB()
    rows = db.get_all()
    if not rows:
        click.echo("No pages tracked yet.")
        return

    click.echo(f"{'Title':<40} {'Status':<10} {'Source':<10} {'Last Synced'}")
    click.echo("-" * 90)
    for r in rows:
        title = (r["notion_title"] or "(untitled)")[:38]
        click.echo(
            f"{title:<40} {r['sync_status']:<10} {r['last_source'] or '-':<10} {r['last_synced'] or '-'}"
        )


@cli.command()
@click.option("--port", default=None, type=int, help=f"Port to listen on (default: {config.FLASK_PORT})")
def serve(port):
    """Start the reverse sync webhook server."""
    port = port or config.FLASK_PORT
    engine = SyncEngine()
    set_reverse_callback(engine.reverse_sync_page)

    app = create_webhook_app()
    click.echo(f"Starting webhook server on port {port}...")
    click.echo(f"  POST /webhook/onenote — OneNote change receiver")
    click.echo(f"  GET  /health          — Health check")
    app.run(host="0.0.0.0", port=port)


@cli.command()
def init():
    """Initialise the sync state database."""
    db = SyncStateDB()
    click.echo(f"Database initialised at {config.DB_PATH}")


if __name__ == "__main__":
    cli()
