#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "cyclopts",
#   "requests",
#   "python-dateutil",
#   "rich",
#   "timezonefinder",
#   "python-dotenv",
# ]
# ///

__version__ = "0.1.0"

import os
import sys
from bisect import bisect_left
from datetime import datetime, time
from typing import Annotated, Literal

import requests
from cyclopts import App, Parameter
from dateutil import parser
from dateutil.tz import gettz
from dotenv import load_dotenv
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.layout import Layout
from rich.text import Text
from timezonefinder import TimezoneFinder

# Load environment variables from .env file
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

def resolve_timezone(lat: float, lng: float) -> str | None:
    return tf.timezone_at(lng=lng, lat=lat)

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

def fetch_all_assets(base_body: dict, progress: Progress) -> list[dict]:
    task_id = progress.add_task("[cyan]Fetching Metadata...", total=None)
    
    all_assets = []
    page = 1
    size = 250 # Max safe page size
    
    # Try to get total for progress bar
    total = get_total_count(base_body)
    if total:
        progress.update(task_id, total=total)

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
                
            all_assets.extend(items)
            progress.update(task_id, advance=len(items))
            
            if len(items) < size:
                break
            page += 1
            
        except requests.RequestException as e:
            console.print(f"[bold red]Fetch Error:[/bold red] {e}")
            break
            
    progress.remove_task(task_id)
    return all_assets

def apply_interpolation(assets: list[dict], method: str) -> list[dict]:
    # Filter valid assets (must have timestamp)
    valid_assets = []
    for asset in assets:
        ts_str = asset.get("exifInfo", {}).get("dateTimeOriginal")
        if ts_str:
            try:
                dt = parser.isoparse(ts_str)
                asset["_dt"] = dt 
                valid_assets.append(asset)
            except ValueError:
                continue

    # Sort by time is critical for interpolation
    valid_assets.sort(key=lambda x: x["_dt"])

    # Separate Anchors (Have GPS)
    anchors = []
    for asset in valid_assets:
        exif = asset.get("exifInfo", {})
        lat, lng = exif.get("latitude"), exif.get("longitude")
        if lat is not None and lng is not None:
            anchors.append({
                "time": asset["_dt"],
                "lat": float(lat),
                "lng": float(lng)
            })

    if not anchors:
        return valid_assets # Nothing to interpolate from

    # Apply Logic
    for asset in valid_assets:
        exif = asset.get("exifInfo", {})
        if exif.get("latitude") is not None:
            continue # Already has GPS

        target_time = asset["_dt"]
        
        if method == "FF":
            # Forward Fill
            # Find closest anchor in the past
            idx = bisect_left([a["time"] for a in anchors], target_time)
            if idx > 0:
                anchor = anchors[idx - 1]
                asset["_interpolated"] = True
                asset["_new_lat"] = anchor["lat"]
                asset["_new_lng"] = anchor["lng"]
                asset["_method"] = "FF"

        elif method == "NN":
            # Nearest Neighbor
            times = [a["time"] for a in anchors]
            idx = bisect_left(times, target_time)
            
            left_anchor = anchors[idx - 1] if idx > 0 else None
            right_anchor = anchors[idx] if idx < len(anchors) else None
            
            chosen = None
            if left_anchor and right_anchor:
                diff_left = abs((target_time - left_anchor["time"]).total_seconds())
                diff_right = abs((right_anchor["time"] - target_time).total_seconds())
                chosen = left_anchor if diff_left <= diff_right else right_anchor
            elif left_anchor:
                chosen = left_anchor
            elif right_anchor:
                chosen = right_anchor
            
            if chosen:
                asset["_interpolated"] = True
                asset["_new_lat"] = chosen["lat"]
                asset["_new_lng"] = chosen["lng"]
                asset["_method"] = "NN"

    return valid_assets

def process_updates(
    assets: list[dict], 
    fix: bool, 
    dry_run: bool, 
    console: Console
):
    stats = {"Updated": 0, "Already correct": 0, "Skipped": 0, "Errors": 0, "Total": len(assets)}
    log_lines = []

    def generate_stats_table(final: bool = False) -> Table:
        title = "Final Statistics" if final else "Live Statistics"
        table = Table(title=title)
        table.add_column("Status", style="bold")
        table.add_column("Count")
        table.add_row("Updated", str(stats["Updated"]), style="green")
        table.add_row("Already Correct", str(stats["Already correct"]), style="blue")
        table.add_row("Skipped", str(stats["Skipped"]), style="yellow")
        table.add_row("Errors", str(stats["Errors"]), style="red")
        table.add_section()
        table.add_row("Total Processed", str(stats["Total"]), style="white")
        return table

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", size=14),
        Layout(name="footer", ratio=1)
    )
    
    layout["header"].update(Panel(f"Immich Timezone Fixer - {'[red]DRY RUN[/red]' if dry_run else '[red]LIVE MODE[/red]'}", style="bold white"))

    job_progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )
    task_id = job_progress.add_task("[green]Analyzing & Updating...", total=len(assets))

    with Live(layout, refresh_per_second=10, console=console):
        for asset in assets:
            layout["body"].split_row(
                Panel(job_progress, title="Progress"),
                Panel(generate_stats_table(), title="Stats")
            )
            layout["footer"].update(Panel(Group(*log_lines), title="Log", border_style="blue"))
            
            job_progress.advance(task_id)

            # Determine Coordinates (Original or Interpolated)
            exif = asset.get("exifInfo", {})
            lat = asset.get("_new_lat", exif.get("latitude"))
            lng = asset.get("_new_lng", exif.get("longitude"))
            is_interpolated = asset.get("_interpolated", False)
            method_tag = f"[{asset.get('_method', '')}]" if is_interpolated else ""

            filename = asset.get("originalFileName", "Unknown")
            original_str = exif.get("dateTimeOriginal")

            if not original_str:
                stats["Skipped"] += 1
                continue
                
            if lat is None or lng is None:
                stats["Skipped"] += 1
                continue

            target_tz = resolve_timezone(float(lat), float(lng))
            if not target_tz:
                stats["Errors"] += 1
                msg = f"[red]Fail ({filename}): TZ Lookup failed ({lat},{lng})[/red]"
                log_lines.append(Text.from_markup(msg))
                if len(log_lines) > 100: log_lines.pop(0)
                continue

            try:
                dt = parser.isoparse(original_str)
                new_tz = gettz(target_tz)
                if not new_tz: raise ValueError
                new_dt = dt.astimezone(new_tz)
                new_iso_string = new_dt.isoformat()
            except ValueError:
                stats["Errors"] += 1
                continue

            current_tz_val = exif.get("timeZone")
            
            if current_tz_val == target_tz:
                stats["Already correct"] += 1
                continue

            # Update required
            msg = f"[bold cyan]Update {method_tag} ({filename}):[/bold cyan] {current_tz_val} -> {target_tz}"
            if dry_run:
                msg += " [dim](Dry Run)[/dim]"
                stats["Updated"] += 1
            else:
                # Execute Update
                payload = {
                    "dateTimeOriginal": new_iso_string,
                    "timeZone": target_tz,
                    "latitude": lat,
                    "longitude": lng,
                }
                try:
                    res = requests.put(f"{IMMICH_URL}/api/assets/{asset['id']}", json=payload, headers=HEADERS, timeout=10)
                    res.raise_for_status()
                    stats["Updated"] += 1
                except requests.RequestException as e:
                    stats["Errors"] += 1
                    msg = f"[bold red]API Error ({filename}): {e}[/bold red]"

            log_lines.append(Text.from_markup(msg))
            if len(log_lines) > 100: log_lines.pop(0)

    console.print(generate_stats_table(final=True))
    console.print(f"[bold green]Job Complete![/bold green] Processed {len(assets)} assets.")

@app.default
def main(
    filename: Annotated[str | None, Parameter(group="Filtering")] = None,
    taken_after: Annotated[datetime | None, Parameter(name="--start", group="Filtering")] = None,
    taken_before: Annotated[datetime | None, Parameter(name="--end", group="Filtering")] = None,
    interpolate: Annotated[bool, Parameter(name="--interpolate", group="Interpolation")] = False,
    method: Annotated[Literal["NN", "FF"], Parameter(name="--method", group="Interpolation")] = "NN",
    check_conn: Annotated[bool, Parameter(name="--check-conn", group="Troubleshooting")] = False,
    fix: bool = False,
    dry_run: Annotated[bool, Parameter(name="--dry-run")] = False,
):
    """
    Immich Timezone Fixer.
    
    Args:
        taken_after: The start date or datetime of the range to filter for.
        taken_before: The end date or datetime of the range to filter for.
        interpolate: Enable coordinate interpolation. Defaults to Nearest Neighbor (NN).
        method: Interpolation method: "NN" (Nearest Neighbor) or "FF" (Forward Fill).
    """
    if check_conn:
        check_connection()

    if not any([filename, taken_after, taken_before, fix, dry_run, interpolate]):
        console.print("[yellow]No action specified. Use --fix, --dry-run, or filters.[/yellow]")
        return

    # Date normalization
    if taken_before and taken_before.time() == time.min and taken_before.tzinfo is None:
        taken_before = taken_before.replace(hour=23, minute=59, second=59, microsecond=999999)

    base_body = {"isVisible": True}
    if filename: base_body["originalFileName"] = filename
    if taken_after: base_body["takenAfter"] = taken_after.isoformat()
    if taken_before: base_body["takenBefore"] = taken_before.isoformat()

    # Phase 1: Fetch
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TimeRemainingColumn(), console=console
    ) as p:
        assets = fetch_all_assets(base_body, p)

    if not assets:
        console.print("[yellow]No assets found matching criteria.[/yellow]")
        return

    # Phase 2: Interpolate
    if interpolate:
        console.print(f"[cyan]Interpolating coordinates using method: [bold]{method}[/bold]...[/cyan]")
        assets = apply_interpolation(assets, method)

    # Phase 3: Update
    if fix or dry_run:
        process_updates(assets, fix, dry_run, console)
    else:
        console.print("[yellow]Review mode complete (No fix/dry-run specified).[/yellow]")

if __name__ == "__main__":
    app()
