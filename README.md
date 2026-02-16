# Render Time Tracker - Cinema 4D Plugin

Plugin for Cinema 4D that tracks render queue (Batch Render) job durations.
Shows per-scene render times and total render time for the whole session.

## Features

- Automatic tracking of all Render Queue jobs
- Per-job timing: start time, duration, status
- Total render time across all jobs
- Live updating during rendering (duration counter ticks in real-time)
- Export reports to JSON
- Background monitoring (tracks even when the dialog is closed)
- Status bar notifications on render start/complete/fail
- Python console logging with timestamps

## Installation

1. Copy the `RenderTimeTracker` folder into Cinema 4D's plugins directory:
   - **Windows:** `C:\Program Files\Maxon Cinema 4D <version>\plugins\`
   - **macOS:** `/Applications/Maxon Cinema 4D <version>/plugins/`
   - **Linux:** `<Cinema 4D path>/plugins/`
2. Restart Cinema 4D

## Usage

1. Open the plugin: **Extensions > Render Time Tracker**
2. Add scenes to the Render Queue as usual (Render > Add to Render Queue)
3. Start the Render Queue
4. The plugin automatically detects when each job starts and finishes
5. The dialog shows:
   - Job number
   - Scene file name
   - Status (Waiting / Rendering / Completed / Failed / Stopped)
   - Start time
   - Duration (updates live during rendering)
6. Bottom summary shows completed job count and total render time

### Buttons

- **Refresh** - Force re-read of the render queue
- **Export JSON** - Save a detailed report to a JSON file
- **Clear Log** - Clear all tracked jobs

### JSON Report Format

```json
{
  "exported_at": "2025-01-15T14:30:00",
  "total_duration_seconds": 3725.50,
  "total_duration_formatted": "01:02:05",
  "job_count": 3,
  "completed_count": 3,
  "jobs": [
    {
      "name": "scene_01.c4d",
      "status": "Completed",
      "start": "2025-01-15 13:00:00",
      "end": "2025-01-15 13:20:15",
      "duration_seconds": 1215.30,
      "duration_formatted": "00:20:15"
    }
  ]
}
```

## How It Works

The plugin registers two components:

1. **MessageData plugin** - Runs in the background, polls the Render Queue every 2 seconds to detect status changes (job started, completed, failed, stopped)
2. **CommandData plugin** - Provides the menu entry and manages the dialog window

When a job transitions from "waiting" to "rendering", the plugin records the start time.
When it transitions from "rendering" to "finished/failed/stopped", it records the end time and calculates the duration.

## Requirements

- Cinema 4D R20+ (Python 3 API)
- Uses standard `c4d` Python API, no external dependencies

## Plugin IDs

This plugin uses IDs `1060510` and `1060511`. For production use, register your own unique IDs at [plugincafe.maxon.net](https://plugincafe.maxon.net/pluginids).
