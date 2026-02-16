# Render Time Tracker v2 - Cinema 4D Plugin

Tracks render queue (Batch Render) job durations with persistent logging.
Built for render farms where tracking billable machine time matters.

## Key Features

- **Persistent append-only log** - survives Cinema 4D crashes and power outages
- **Project grouping** - scenes grouped by parent folder (project)
- **Monthly log rotation** - separate log file per month, easy cleanup
- **Crash detection** - unfinished renders marked as "Interrupted"
- **Graceful shutdown** - active renders saved as "Stopped" when C4D closes
- **Live duration counter** - ticks in real-time during active rendering
- **Background monitoring** - tracks even when the dialog window is closed
- **Month filter** - view data for a specific month or all time
- **JSON export** - grouped by project, includes full paths and durations
- **Log cleanup** - delete logs older than 1/3/6/12 months
- **Throttled polling** - minimal performance impact (1 poll per 1.5s max)

## Installation

1. Copy the `RenderTimeTracker` folder into Cinema 4D's plugins directory:
   - **Windows:** `C:\Program Files\Maxon Cinema 4D <version>\plugins\`
   - **macOS:** `/Applications/Maxon Cinema 4D <version>/plugins/`
2. Restart Cinema 4D

## Usage

1. Open: **Extensions > Render Time Tracker**
2. Add scenes to Render Queue as usual
3. Start the queue — the plugin tracks automatically

### Dialog Layout

```
Month: [All months ▼]  [Refresh]
─────────────────────────────────────────────
#   Scene              Status     Start        Duration
─────────────────────────────────────────────
--- ClientX ---                                01:30:00
1   scene_01.c4d       Completed  01-15 03:12  00:45:00
2   scene_02.c4d       Completed  01-15 03:57  00:45:00
--- ClientY ---                                02:10:00
3   shot_01.c4d        Rendering  01-15 06:02  01:00:00
4   shot_02.c4d        Interrupted             00:00:00
─────────────────────────────────────────────
Jobs: 3 / 4    Total: 03:40:00

[Export JSON]   [Older than 3 months ▼] [Clean Up]
```

### Statuses

| Status | Meaning |
|--------|---------|
| Rendering | Currently in progress (duration updates live) |
| Completed | Finished successfully |
| Failed | Render error |
| Stopped | Stopped by user or C4D shutdown |
| Interrupted | START logged but no FINISH (crash / power loss) |

### Buttons

- **Refresh** — re-read render queue and log files
- **Export JSON** — save report (respects month filter)
- **Clean Up** — delete log files older than selected period

## Log Files

Stored in Cinema 4D preferences:
```
<C4D Prefs>/RenderTimeTracker/render_log_2025-01.log
<C4D Prefs>/RenderTimeTracker/render_log_2025-02.log
```

Format (one event per line, append-only):
```
2025-01-15T03:12:05|START|D:/Projects/ClientX/scene_01.c4d
2025-01-15T03:45:18|FINISH|D:/Projects/ClientX/scene_01.c4d|1993.2
2025-01-15T03:45:19|FAIL|D:/Projects/ClientX/scene_02.c4d|504.0
2025-01-15T03:50:00|STOP|D:/Projects/ClientX/scene_03.c4d|120.5
```

Each write is flushed + fsynced. If power is lost mid-write, at most one line is damaged — all previous entries are safe.

## Crash / Power Loss Recovery

- Every event is written to disk immediately (not buffered)
- On restart, the plugin reads the log and reconstructs history
- A `START` without a matching `FINISH/FAIL/STOP` = **Interrupted**
- When Cinema 4D shuts down normally, active renders get a `STOP` entry

## JSON Report Format

```json
{
  "exported_at": "2025-01-15T14:30:00",
  "total_duration_seconds": 13200.50,
  "total_duration": "03:40:00",
  "projects": {
    "ClientX": {
      "total_seconds": 5400.00,
      "total": "01:30:00",
      "scenes": [
        {
          "name": "scene_01.c4d",
          "path": "D:/Projects/ClientX/scene_01.c4d",
          "status": "Completed",
          "start": "2025-01-15T03:12:05",
          "end": "2025-01-15T03:57:05",
          "duration_seconds": 2700.00,
          "duration": "00:45:00"
        }
      ]
    }
  }
}
```

## Project Grouping

Scenes are grouped by the **parent folder** of the `.c4d` file:

| File path | Project name |
|-----------|-------------|
| `D:/Projects/ClientX/scene_01.c4d` | ClientX |
| `D:/Projects/ClientY/shot_01.c4d` | ClientY |
| `D:/Work/test.c4d` | Work |

## Requirements

- Cinema 4D R23+ (Python 3)
- Standard `c4d` Python API only, no external dependencies

## Plugin IDs

Uses IDs `1060510` and `1060511`. For production use, register your own at [plugincafe.maxon.net](https://plugincafe.maxon.net/pluginids).
