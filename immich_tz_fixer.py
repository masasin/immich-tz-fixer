#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "cyclopts",
#   "dotenv",
#   "requests",
#   "python-dateutil",
#   "rich",
#   "timezonefinder",
# ]
# ///

import os
import sys
from datetime import datetime, time
from typing import Annotated

import requests
from cyclopts import App, Parameter
from dateutil import parser
from dateutil.tz import gettz
from dotenv import load_dotenv
from rich.console import Console, Group
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    MofNCompleteColumn
)
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from timezonefinder import TimezoneFinder

load_dotenv()


console = Console()
app = App()
tf = TimezoneFinder()

def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        console.print(f"[bold red]Error:[/bold red] {name} environment variable is not set.")
        sys.exit(1)
    return value.rstrip("/")

IMMICH_URL = get_env_var("IMMICH_URL")
IMMICH_KEY = get_env_var("IMMICH_API_KEY")

HEADERS = {
    "Accept": "application/json",
    "x-api-key": IMMICH_KEY,
    "Content-Type": "application/json",
}

def check_connection() -> None:
    url_user = f"{IMMICH_URL}/api/users/me"
    try:
        response = requests.get(url_user, headers=HEADERS, timeout=10)
        response.raise_for_status()
        user_data = response.json()
        console.print(f"[bold green]Auth Success:[/bold green] {user_data.get('email')}")
    except requests.RequestException as e:
        console.print(f"[bold red]Connection Failed:[/bold red] {e}")
        sys.exit(1)

def get_total_count(body: dict) -> int | None:
    try:
        stats_body = body.copy()
        stats_body.pop("page", None)
        stats_body.pop("size", None)
        stats_body.pop("withExif", None)
        
        response = requests.post(f"{IMMICH_URL}/api/search/statistics", json=stats_body, headers=HEADERS, timeout=10)
        if response.status_code == 200:
             data = response.json()
             return data.get("total") or data.get("count")
    except requests.RequestException:
        pass
    return None

def resolve_timezone(lat: float, lng: float) -> str | None:
    return tf.timezone_at(lng=lng, lat=lat)

def update_asset_timezone(asset: dict, dry_run: bool) -> tuple[bool, str]:
    asset_id = asset["id"]
    filename = asset.get("originalFileName", "Unknown")
    exif = asset.get("exifInfo", {})
    original_str = exif.get("dateTimeOriginal")
    lat = exif.get("latitude")
    lng = exif.get("longitude")

    if not original_str:
        return False, f"[dim]Skip ({filename}): No timestamp[/dim]"
        
    if lat is None or lng is None:
        return False, f"[yellow]Skip ({filename}): No GPS[/yellow]"

    target_tz = resolve_timezone(float(lat), float(lng))
    if not target_tz:
        return False, f"[red]Fail ({filename}): TZ Lookup failed ({lat},{lng})[/red]"

    try:
        dt = parser.isoparse(original_str)
    except ValueError:
        return False, f"[red]Fail ({filename}): Invalid Date {original_str}[/red]"

    new_tz = gettz(target_tz)
    if not new_tz:
        return False, f"[red]Fail ({filename}): Invalid TZ {target_tz}[/red]"

    new_dt = dt.astimezone(new_tz)
    new_iso_string = new_dt.isoformat()

    current_tz_val = exif.get("timeZone")
    if current_tz_val == target_tz:
        return False, f"[green]Already correct ({filename}): {target_tz}[/green]"

    msg = f"[bold cyan]Update ({filename}):[/bold cyan] {current_tz_val} -> {target_tz}"

    if dry_run:
        return True, msg + " [dim](Dry Run)[/dim]"

    payload = {
        "dateTimeOriginal": new_iso_string,
        "timeZone": target_tz,
        "latitude": lat,
        "longitude": lng,
    }

    try:
        response = requests.put(
            f"{IMMICH_URL}/api/assets/{asset_id}", 
            json=payload, 
            headers=HEADERS,
            timeout=10
        )
        response.raise_for_status()
        return True, msg
    except requests.RequestException as e:
        return False, f"[bold red]Error ({filename}): API {e}[/bold red]"

def create_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", size=14),
        Layout(name="footer", ratio=1)
    )
    return layout

def generate_stats_table(stats: dict, title: str) -> Table:
    table = Table(title=title)
    table.add_column("Status", style="bold")
    table.add_column("Count")
    table.add_row("Updated (Fixed)", str(stats["Updated"]), style="green")
    table.add_row("Already correct", str(stats["Already correct"]), style="blue")
    table.add_row("Skipped (No timestamp/GPS)", str(stats["Skipped"]), style="yellow")
    table.add_row("Errors", str(stats["Errors"]), style="red")
    if "Total" in stats:
        table.add_section()
        table.add_row("Total Processed", str(stats["Total"]), style="bold white")
    return table

def process_batch(
    taken_after: datetime | None, 
    taken_before: datetime | None, 
    filename: str | None,
    fix: bool, 
    dry_run: bool
):
    if taken_before and taken_before.time() == time.min and taken_before.tzinfo is None:
        taken_before = taken_before.replace(hour=23, minute=59, second=59, microsecond=999999)

    base_body = {"isVisible": True}
    if filename:
        base_body["originalFileName"] = filename
    if taken_after:
        base_body["takenAfter"] = taken_after.isoformat()
    if taken_before:
        base_body["takenBefore"] = taken_before.isoformat()

    console.print("[cyan]Estimating workload...[/cyan]")
    total_assets = get_total_count(base_body)

    job_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )
    
    task_id = job_progress.add_task(
        "[green]Processing Assets...", 
        total=total_assets if total_assets else None
    )

    stats = {"Updated": 0, "Already correct": 0, "Skipped": 0, "Errors": 0, "Total": 0}
    log_lines = []

    layout = create_layout()
    layout["header"].update(Panel(f"Immich Timezone Fixer - {'[red]DRY RUN[/red]' if dry_run else '[red]LIVE MODE[/red]'}", style="bold white"))
    
    with Live(layout, refresh_per_second=4, console=console):
        page = 1
        size = 250
        
        while True:
            req_body = base_body.copy()
            req_body.update({"page": page, "size": size, "withExif": True})

            try:
                response = requests.post(f"{IMMICH_URL}/api/search/metadata", json=req_body, headers=HEADERS, timeout=30)
                response.raise_for_status()
                data = response.json()
                items = data.get("assets", {}).get("items", []) if isinstance(data, dict) else []
                
                if not items:
                    break
                
                for asset in items:
                    stats["Total"] += 1
                    if fix or dry_run:
                        changed, msg = update_asset_timezone(asset, dry_run)
                        
                        log_lines.append(Text.from_markup(msg))
                        if len(log_lines) > 100:
                            log_lines.pop(0)
                        
                        if changed:
                            stats["Updated"] += 1
                        elif "Fail" in msg or "Error" in msg:
                            stats["Errors"] += 1
                        elif "Already" in msg:
                            stats["Already correct"] += 1
                        else:
                            stats["Skipped"] += 1
                    
                    job_progress.advance(task_id)

                    log_group = Group(*log_lines)
                    layout["footer"].update(Panel(log_group, title="Log", border_style="blue"))
                    layout["body"].split_row(
                        Panel(job_progress, title="Progress"),
                        Panel(generate_stats_table(stats, "Live Statistics"), title="Stats")
                    )
                
                if len(items) < size:
                    break
                page += 1

            except requests.RequestException as e:
                log_lines.append(Text.from_markup(f"[bold red]Critical Batch Error: {e}[/bold red]"))
                break

    console.print(generate_stats_table(stats, "Final Statistics"))
    console.print(f"[bold green]Job Complete![/bold green] {stats['Total']} files processed.")

def get_summary_stats():
    try:
        url = f"{IMMICH_URL}/api/search/metadata"
        res = requests.post(url, json={"isVisible": True, "size": 1}, headers=HEADERS)
        res.raise_for_status()
        total = res.json().get("assets", {}).get("total", 0)
        console.print(f"Total Assets: {total}")
        console.print("[yellow]Detailed GPS distribution requires a full scan.[/yellow]")
    except Exception as e:
        console.print(f"[red]Failed to retrieve summary stats: {e}[/red]")

@app.default
def main(
    filename: str | None = None,
    taken_after: Annotated[datetime | None, Parameter(name="--taken-after")] = None,
    taken_before: Annotated[datetime | None, Parameter(name="--taken-before")] = None,
    check_conn: Annotated[bool, Parameter(name="--check-conn")] = False,
    fix: bool = False,
    dry_run: Annotated[bool, Parameter(name="--dry-run")] = False,
):
    if check_conn:
        check_connection()

    if not any([filename, taken_after, taken_before, fix, dry_run]):
        get_summary_stats()
        return

    process_batch(taken_after, taken_before, filename, fix, dry_run)

if __name__ == "__main__":
    app()
