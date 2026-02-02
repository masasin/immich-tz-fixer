# Immich Timezone Fixer

A high-performance metadata synchronization tool designed to correct timezone offsets in Immich libraries. It specifically addresses issues where assets imported via Google Takeout or external sources default to the server's local time instead of the geographic context of the capture.

## Core Functionality

The tool operates by mapping UTC timestamps to administrative timezone boundaries using geographic coordinates. For assets lacking GPS metadata, it employs temporal interpolation to infer the most likely geographic context based on surrounding "Anchor" assets.

### Features

- **Automated Timezone Resolution**: Converts GPS coordinates to IANA Timezone IDs (e.g., `Asia/Tokyo`) using a local shapefile database.
- **Spatio-Temporal Interpolation**:
  - **Nearest Neighbor (NN)**: Snaps orphan assets to the chronologically closest GPS anchor. This is optimized for international travel and flight patterns.
  - **Forward Fill (FF)**: Propagates the last known GPS location forward in time.
- **TUI Dashboard**: Real-time progress monitoring, live log streaming, and processing statistics powered by `rich`.
- **Atomic Updates**: Modifies only the metadata fields via the Immich API, preserving original files and existing database relations like albums and person tags.

## Prerequisites

- **Python 3.13+**
- **uv**: For dependency management and script execution.
- **Immich Server**: Tested on version 2.5.2.

## Environment Configuration

Create a `.env` file in the project root with the following variables:

```env
IMMICH_URL=http://your-immich-instance:2283
IMMICH_API_KEY=your_admin_api_key
```

## Usage

The script uses `uv` to handle dependencies automatically via PEP 723 metadata.

### Connection Test
Verify API connectivity and authentication:

```bash
./immich_tz_fixer.py --check-conn
```

### Dry Run (Recommended)
Analyze a specific date range and preview changes without modifying the database:

```bash
./immich_tz_fixer.py --start 2024-01-01 --end 2024-12-31 --interpolate --method FF --dry-run
```

### Applying Fixes
Apply timezone corrections to all assets within a range:

```bash
./immich_tz_fixer.py --start 2023-05-19 --fix
```

Or to all files:

```bash
./immich_tz_fixer.py --interpolate --fix
```

### Target Specific File
Process a single asset by its original filename:

```bash
./immich_tz_fixer.py --filename "IMG_20260202_1800.jpg" --fix
```

## Technical Architecture

### Metadata Search
The tool utilizes `POST /api/search/metadata` with `withExif: true` to perform bulk retrieval of assets. This reduces API overhead by fetching coordinates and existing timezone strings in a single paginated request.

### Update Mechanism
Updates are pushed via `PUT /api/assets/{id}`. The tool calculates the correct ISO 8601 string by applying the resolved timezone offset to the absolute UTC timestamp, ensuring the "moment in time" remains constant while the "wall clock time" is corrected for the UI.
