"""
Render Time Tracker v2 - Cinema 4D Plugin

Tracks render queue (Batch Render) job durations with persistent logging.
Groups scenes by project (parent folder). Survives crashes and restarts.
Monthly log rotation with cleanup.

Log format (append-only, one event per line):
    2025-01-15T03:12:05|START|D:/Projects/ClientX/scene_01.c4d
    2025-01-15T03:45:18|FINISH|D:/Projects/ClientX/scene_01.c4d|1993.2
    2025-01-15T03:45:19|FAIL|D:/Projects/ClientX/scene_02.c4d|504.0
    2025-01-15T03:45:19|STOP|D:/Projects/ClientX/scene_03.c4d|120.5

Installation:
    Copy the 'RenderTimeTracker' folder into Cinema 4D's plugins directory.
    Restart Cinema 4D.  Menu: Extensions > Render Time Tracker
"""

import c4d
from c4d import plugins, gui, storage
import time
import datetime
import os
import json

# =============================================================================
# Plugin IDs - register your own at https://plugincafe.maxon.net/pluginids
# =============================================================================
PLUGIN_ID_MSG = 1060510
PLUGIN_ID_CMD = 1060511

# =============================================================================
# Settings
# =============================================================================
POLL_MS = 2000           # Timer interval (ms) for MessageData and Dialog
THROTTLE_SEC = 1.5       # Min seconds between actual queue polls

# =============================================================================
# Dialog widget IDs
# =============================================================================
ID_GRP_JOBS      = 10000
ID_LBL_TOTAL     = 10001
ID_LBL_COUNT     = 10002
ID_BTN_CLEANUP   = 10003
ID_BTN_EXPORT    = 10004
ID_BTN_REFRESH   = 10005
ID_CMB_MONTH     = 10006
ID_CMB_CLEANUP   = 10007


# =============================================================================
# Helpers
# =============================================================================

def fmt_duration(seconds):
    """Format seconds as HH:MM:SS."""
    if not seconds or seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return "{:02d}:{:02d}:{:02d}".format(h, m, s)


def extract_project(filepath):
    """Extract project name from scene file path.

    Uses the parent directory name as the project identifier.
    E.g. 'D:/Projects/ClientX/scenes/shot01.c4d' -> 'scenes'
         'D:/Projects/ClientX/shot01.c4d'         -> 'ClientX'
    """
    parent = os.path.dirname(str(filepath))
    name = os.path.basename(parent)
    return name if name else "Unknown"


def _log(msg):
    """Print a timestamped message to the Cinema 4D Python console."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print("[RenderTimer {}] {}".format(ts, msg))


# =============================================================================
# Persistent log  (append-only, monthly rotation, crash-safe)
# =============================================================================

class RenderLog(object):
    """Append-only log writer/reader with monthly file rotation.

    Each event is a single line written atomically with flush + fsync.
    If the machine loses power mid-write, at most one line is lost.
    """

    def __init__(self, log_dir):
        self.log_dir = log_dir
        self._ensure_dir()

    # -- writing --------------------------------------------------------------

    def write_event(self, event, filepath, duration=None):
        """Append one event line and force it to disk."""
        ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        parts = [ts, event, str(filepath)]
        if duration is not None:
            parts.append("{:.1f}".format(duration))
        line = "|".join(parts) + "\n"

        self._ensure_dir()
        log_path = self._month_path()
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            _log("WARNING: Failed to write log: {}".format(e))

    # -- reading --------------------------------------------------------------

    def read_entries(self, months=None):
        """Read and parse log entries. *months*: list of 'YYYY-MM' or None for all."""
        entries = []
        for date_str, fpath in self._list_files():
            if months is not None and date_str not in months:
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line_raw in f:
                        entry = self._parse_line(line_raw.strip())
                        if entry:
                            entries.append(entry)
            except Exception:
                continue
        return entries

    def available_months(self):
        """Return sorted list of 'YYYY-MM' strings for which log files exist."""
        return [ds for ds, _ in self._list_files()]

    # -- cleanup --------------------------------------------------------------

    def delete_before(self, cutoff_ym):
        """Delete log files older than *cutoff_ym* ('YYYY-MM'). Returns count."""
        deleted = 0
        for ds, fpath in self._list_files():
            if ds < cutoff_ym:
                try:
                    os.remove(fpath)
                    deleted += 1
                except Exception:
                    pass
        return deleted

    # -- internals ------------------------------------------------------------

    def _ensure_dir(self):
        if not os.path.isdir(self.log_dir):
            try:
                os.makedirs(self.log_dir)
            except OSError:
                pass

    def _month_path(self, dt=None):
        if dt is None:
            dt = datetime.datetime.now()
        name = "render_log_{}.log".format(dt.strftime("%Y-%m"))
        return os.path.join(self.log_dir, name)

    def _list_files(self):
        """Return sorted list of (date_str, filepath) for all log files."""
        result = []
        if not os.path.isdir(self.log_dir):
            return result
        for name in sorted(os.listdir(self.log_dir)):
            if name.startswith("render_log_") and name.endswith(".log"):
                date_str = name[len("render_log_"):-len(".log")]
                result.append((date_str, os.path.join(self.log_dir, name)))
        return result

    def _parse_line(self, line):
        """Parse a single log line. Returns dict or None on failure."""
        if not line:
            return None
        parts = line.split("|")
        if len(parts) < 3:
            return None
        try:
            return {
                "time": datetime.datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%S"),
                "event": parts[1],
                "path": parts[2],
                "duration": float(parts[3]) if len(parts) > 3 else None,
            }
        except (ValueError, IndexError):
            return None  # corrupted line, skip


# =============================================================================
# Tracker engine
# =============================================================================

class RenderTimeTracker(object):
    """Monitors the Batch Render queue and writes events to the persistent log.

    Uses file path (not queue index) as the stable job identifier.
    Detects transitions: Waiting -> Rendering -> Completed/Failed/Stopped.
    Tracks active renders in memory; everything else lives on disk.
    """

    def __init__(self, log):
        self.log = log                  # RenderLog instance
        self._prev = {}                 # queue_index -> (rm_status, path)
        self._active = {}               # queue_index -> (start_time, path)
        self._last_poll_time = 0.0
        self._changed = True
        self._cache = None              # (months_key, jobs_list)

    # -- public API -----------------------------------------------------------

    @property
    def has_active(self):
        return len(self._active) > 0

    def needs_refresh(self):
        if self._changed:
            self._changed = False
            return True
        return False

    def invalidate_cache(self):
        self._cache = None
        self._changed = True

    def poll(self):
        """Check the render queue for status changes. Throttled."""
        now = time.time()
        if now - self._last_poll_time < THROTTLE_SEC:
            return
        self._last_poll_time = now

        br = c4d.documents.GetBatchRender()
        if br is None:
            return

        count = br.GetElementCount()
        current = {}

        for i in range(count):
            rm_status = br.GetElementStatus(i)
            path = str(br.GetElement(i) or "")
            current[i] = (rm_status, path)

        # Detect transitions at each index
        for i, (rm_status, path) in current.items():
            prev = self._prev.get(i)
            prev_status = prev[0] if prev else None
            prev_path = prev[1] if prev else None

            # Path changed at this index -> old job was removed/replaced
            if prev_path and prev_path != path and i in self._active:
                self._finish_active(i, "STOP")

            if rm_status != prev_status or (prev_path and prev_path != path):
                self._on_transition(i, path, prev_status, rm_status)

        # Indices that disappeared (queue got shorter or items removed)
        for i in list(self._prev.keys()):
            if i not in current and i in self._active:
                self._finish_active(i, "STOP")

        self._prev = current

    def stop_all_active(self):
        """Write STOP for every currently active render. Called on C4D shutdown."""
        for i in list(self._active.keys()):
            self._finish_active(i, "STOP")

    def get_display_data(self, months=None):
        """Build display data from log + active renders.

        Returns (projects_dict, all_jobs_list) where:
            projects_dict = { "ProjectName": {"jobs": [...], "total": float}, ... }
            all_jobs_list = flat list of all job dicts
        """
        months_key = tuple(months) if months else None

        # Use cached historical data if available
        if self._cache and self._cache[0] == months_key:
            historical = self._cache[1]
        else:
            historical = self._build_jobs_from_log(months)
            self._cache = (months_key, historical)

        # Merge with currently active renders
        all_jobs = list(historical)
        for _idx, (start_time, path) in self._active.items():
            all_jobs.append({
                "path": path,
                "name": os.path.basename(path),
                "project": extract_project(path),
                "start": datetime.datetime.fromtimestamp(start_time),
                "end": None,
                "duration": time.time() - start_time,
                "status": "Rendering",
            })

        # Group by project
        projects = {}
        for job in all_jobs:
            pname = job["project"]
            if pname not in projects:
                projects[pname] = {"jobs": [], "total": 0.0}
            projects[pname]["jobs"].append(job)
            projects[pname]["total"] += job["duration"]

        return projects, all_jobs

    # -- internals ------------------------------------------------------------

    def _on_transition(self, index, path, old_rm, new_rm):
        progress = getattr(c4d, "RM_PROGRESS", None)
        finished = getattr(c4d, "RM_FINISHED", None)
        failed = getattr(c4d, "RM_FAILED", None)
        stopped = getattr(c4d, "RM_STOPPED", None)

        name = os.path.basename(path)

        if new_rm == progress and old_rm != progress:
            # Render started
            self._active[index] = (time.time(), path)
            self.log.write_event("START", path)
            self._changed = True
            self._cache = None
            _log("Started: {}".format(name))
            c4d.StatusSetText("[RenderTimer] Started: {}".format(name))

        elif new_rm == finished and old_rm == progress:
            self._finish_active(index, "FINISH")

        elif new_rm == failed and old_rm == progress:
            self._finish_active(index, "FAIL")

        elif new_rm == stopped and old_rm == progress:
            self._finish_active(index, "STOP")

    def _finish_active(self, index, event):
        """Complete an active render: calculate duration, write to log."""
        info = self._active.pop(index, None)
        if info is None:
            return
        start_time, path = info
        duration = max(0.0, time.time() - start_time)
        self.log.write_event(event, path, duration)
        self._changed = True
        self._cache = None

        name = os.path.basename(path)
        dur_str = fmt_duration(duration)
        if event == "FINISH":
            _log("Completed: {} ({})".format(name, dur_str))
            c4d.StatusSetText(
                "[RenderTimer] {} done - {}".format(name, dur_str))
        elif event == "FAIL":
            _log("Failed: {} ({})".format(name, dur_str))
            c4d.StatusSetText("[RenderTimer] Failed: {}".format(name))
        elif event == "STOP":
            _log("Stopped: {} ({})".format(name, dur_str))

    def _build_jobs_from_log(self, months=None):
        """Parse log entries into a list of job dicts.

        Matches each START with its next FINISH/FAIL/STOP for the same path
        (FIFO order). Unmatched STARTs become 'Interrupted'.
        """
        entries = self.log.read_entries(months)
        jobs = []
        pending = {}  # path -> [entry, ...] (FIFO queue per path)

        for e in entries:
            path = e["path"]
            if e["event"] == "START":
                pending.setdefault(path, []).append(e)

            elif e["event"] in ("FINISH", "FAIL", "STOP"):
                start_entry = None
                if path in pending and pending[path]:
                    start_entry = pending[path].pop(0)
                    if not pending[path]:
                        del pending[path]

                status_map = {
                    "FINISH": "Completed",
                    "FAIL": "Failed",
                    "STOP": "Stopped",
                }
                jobs.append({
                    "path": path,
                    "name": os.path.basename(path),
                    "project": extract_project(path),
                    "start": start_entry["time"] if start_entry else e["time"],
                    "end": e["time"],
                    "duration": e["duration"] or 0.0,
                    "status": status_map.get(e["event"], "Unknown"),
                })

        # Unmatched STARTs = renders interrupted by crash / power loss
        for path, starts in pending.items():
            for s in starts:
                jobs.append({
                    "path": path,
                    "name": os.path.basename(path),
                    "project": extract_project(path),
                    "start": s["time"],
                    "end": None,
                    "duration": 0.0,
                    "status": "Interrupted",
                })

        return jobs


# =============================================================================
# Global instances
# =============================================================================

_log_instance = None
_tracker = None
_dialog = None


def get_log():
    global _log_instance
    if _log_instance is None:
        prefs = c4d.storage.GeGetC4DPath(c4d.C4D_PATH_PREFS)
        log_dir = os.path.join(prefs, "RenderTimeTracker")
        _log_instance = RenderLog(log_dir)
    return _log_instance


def get_tracker():
    global _tracker
    if _tracker is None:
        _tracker = RenderTimeTracker(get_log())
    return _tracker


# =============================================================================
# Dialog
# =============================================================================

class RenderTimeDialog(gui.GeDialog):
    """Non-modal dialog showing render time statistics grouped by project."""

    def CreateLayout(self):
        self.SetTitle("Render Time Tracker")

        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0, "")
        self.GroupBorderSpace(10, 10, 10, 10)

        # -- Filter bar -------------------------------------------------------
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "")
        self.AddStaticText(0, c4d.BFH_LEFT, 0, 0, "Month:")
        self.AddComboBox(ID_CMB_MONTH, c4d.BFH_LEFT, 130, 0)
        self.AddButton(ID_BTN_REFRESH, c4d.BFH_LEFT, 80, 0, "Refresh")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # -- Column headers ---------------------------------------------------
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 0, "")
        self.AddStaticText(0, c4d.BFH_LEFT,     30,  13, "#")
        self.AddStaticText(0, c4d.BFH_SCALEFIT,  0,  13, "Scene")
        self.AddStaticText(0, c4d.BFH_LEFT,     85,  13, "Status")
        self.AddStaticText(0, c4d.BFH_LEFT,    105,  13, "Start")
        self.AddStaticText(0, c4d.BFH_LEFT,     80,  13, "Duration")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # -- Scrollable job list ----------------------------------------------
        self.ScrollGroupBegin(0,
                              c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT)
        self.GroupBegin(ID_GRP_JOBS,
                        c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "")
        self.GroupEnd()
        self.ScrollGroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # -- Summary ----------------------------------------------------------
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "")
        self.AddStaticText(0,            c4d.BFH_LEFT, 0, 0, "Jobs:")
        self.AddStaticText(ID_LBL_COUNT, c4d.BFH_LEFT, 80, 0, "0")
        self.AddStaticText(0,            c4d.BFH_LEFT, 0, 0, "Total:")
        self.AddStaticText(ID_LBL_TOTAL, c4d.BFH_LEFT, 100, 0, "00:00:00")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # -- Buttons ----------------------------------------------------------
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "")
        self.AddButton(ID_BTN_EXPORT, c4d.BFH_LEFT, 110, 0, "Export JSON")
        self.AddStaticText(0, c4d.BFH_LEFT, 20, 0, "")
        self.AddComboBox(ID_CMB_CLEANUP, c4d.BFH_LEFT, 160, 0)
        self.AddButton(ID_BTN_CLEANUP, c4d.BFH_LEFT, 110, 0, "Clean Up")
        self.GroupEnd()

        self.GroupEnd()  # main
        return True

    def InitValues(self):
        self._fill_month_combo()
        self._fill_cleanup_combo()
        self._rebuild()
        return True

    def Timer(self, msg):
        tracker = get_tracker()
        tracker.poll()
        if tracker.needs_refresh() or tracker.has_active:
            self._rebuild()

    def Command(self, id, msg):
        if id == ID_BTN_REFRESH:
            get_tracker().invalidate_cache()
            self._fill_month_combo()
            self._rebuild()

        elif id == ID_CMB_MONTH:
            get_tracker().invalidate_cache()
            self._rebuild()

        elif id == ID_BTN_EXPORT:
            self._do_export()

        elif id == ID_BTN_CLEANUP:
            self._do_cleanup()

        return True

    # -- helpers --------------------------------------------------------------

    def _selected_months(self):
        """Return list of 'YYYY-MM' from the filter combo, or None for all."""
        val = self.GetInt32(ID_CMB_MONTH)
        if val == 0:
            return None
        months = get_log().available_months()
        idx = val - 1
        if 0 <= idx < len(months):
            return [months[idx]]
        return None

    def _fill_month_combo(self):
        self.FreeChildren(ID_CMB_MONTH)
        self.AddChild(ID_CMB_MONTH, 0, "All months")
        for i, ym in enumerate(get_log().available_months()):
            self.AddChild(ID_CMB_MONTH, i + 1, ym)
        self.SetInt32(ID_CMB_MONTH, 0)

    def _fill_cleanup_combo(self):
        self.FreeChildren(ID_CMB_CLEANUP)
        self.AddChild(ID_CMB_CLEANUP, 1, "Older than 1 month")
        self.AddChild(ID_CMB_CLEANUP, 3, "Older than 3 months")
        self.AddChild(ID_CMB_CLEANUP, 6, "Older than 6 months")
        self.AddChild(ID_CMB_CLEANUP, 12, "Older than 1 year")
        self.SetInt32(ID_CMB_CLEANUP, 3)

    def _rebuild(self):
        """Rebuild the job list grouped by project."""
        tracker = get_tracker()
        months = self._selected_months()
        projects, all_jobs = tracker.get_display_data(months)

        self.LayoutFlushGroup(ID_GRP_JOBS)

        if not all_jobs:
            self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0,
                               "No render jobs recorded yet.")
        else:
            counter = 0
            for proj_name in sorted(projects.keys()):
                proj = projects[proj_name]

                # Project header
                self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "")
                self.AddStaticText(
                    0, c4d.BFH_SCALEFIT, 0, 15,
                    "--- {} ---".format(proj_name))
                self.AddStaticText(
                    0, c4d.BFH_LEFT, 80, 15,
                    fmt_duration(proj["total"]))
                self.GroupEnd()

                # Scenes in this project
                for job in proj["jobs"]:
                    counter += 1
                    self.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 0, "")

                    self.AddStaticText(
                        0, c4d.BFH_LEFT, 30, 0, str(counter))
                    self.AddStaticText(
                        0, c4d.BFH_SCALEFIT, 0, 0, job["name"])
                    self.AddStaticText(
                        0, c4d.BFH_LEFT, 85, 0, job["status"])

                    start_str = ""
                    if job["start"]:
                        start_str = job["start"].strftime("%m-%d %H:%M")
                    self.AddStaticText(
                        0, c4d.BFH_LEFT, 105, 0, start_str)

                    self.AddStaticText(
                        0, c4d.BFH_LEFT, 80, 0, fmt_duration(job["duration"]))

                    self.GroupEnd()

        self.LayoutChanged(ID_GRP_JOBS)

        # Summary
        total_dur = sum(j["duration"] for j in all_jobs)
        completed = sum(1 for j in all_jobs if j["status"] == "Completed")
        self.SetString(ID_LBL_TOTAL, fmt_duration(total_dur))
        self.SetString(ID_LBL_COUNT,
                       "{} / {}".format(completed, len(all_jobs)))

    def _do_export(self):
        path = storage.SaveDialog(
            c4d.FILESELECTTYPE_ANYTHING,
            "Save Render Time Report", "json")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"

        tracker = get_tracker()
        months = self._selected_months()
        projects, all_jobs = tracker.get_display_data(months)

        total_dur = sum(j["duration"] for j in all_jobs)
        report = {
            "exported_at": datetime.datetime.now().isoformat(),
            "total_duration_seconds": round(total_dur, 2),
            "total_duration": fmt_duration(total_dur),
            "projects": {},
        }
        for pname in sorted(projects.keys()):
            pdata = projects[pname]
            report["projects"][pname] = {
                "total_seconds": round(pdata["total"], 2),
                "total": fmt_duration(pdata["total"]),
                "scenes": [{
                    "name": j["name"],
                    "path": j["path"],
                    "status": j["status"],
                    "start": j["start"].isoformat() if j["start"] else None,
                    "end": j["end"].isoformat() if j["end"] else None,
                    "duration_seconds": round(j["duration"], 2),
                    "duration": fmt_duration(j["duration"]),
                } for j in pdata["jobs"]],
            }

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            gui.MessageDialog("Report saved:\n{}".format(path))
        except Exception as e:
            gui.MessageDialog("Export failed:\n{}".format(e))

    def _do_cleanup(self):
        months_back = self.GetInt32(ID_CMB_CLEANUP)
        if not months_back:
            months_back = 3
        cutoff_dt = datetime.datetime.now() - datetime.timedelta(days=months_back * 30)
        cutoff_ym = cutoff_dt.strftime("%Y-%m")

        count = get_log().delete_before(cutoff_ym)
        get_tracker().invalidate_cache()
        self._fill_month_combo()
        self._rebuild()

        if count > 0:
            gui.MessageDialog(
                "Deleted {} log file(s) older than {}.".format(count, cutoff_ym))
        else:
            gui.MessageDialog("No log files to clean up.")


# =============================================================================
# Command plugin  (Extensions > Render Time Tracker)
# =============================================================================

class RenderTimeTrackerCmd(plugins.CommandData):

    def Execute(self, doc):
        global _dialog
        if _dialog is None:
            _dialog = RenderTimeDialog()

        if _dialog.IsOpen():
            _dialog.Close()
        else:
            _dialog.Open(
                dlgtype=c4d.DLG_TYPE_ASYNC,
                pluginid=PLUGIN_ID_CMD,
                defaultw=650,
                defaulth=400,
            )
            _dialog.SetTimer(POLL_MS)
        return True

    def RestoreLayout(self, sec_ref):
        global _dialog
        if _dialog is None:
            _dialog = RenderTimeDialog()
        _dialog.SetTimer(POLL_MS)
        return _dialog.RestoreLayout(PLUGIN_ID_CMD, sec_ref)

    def GetState(self, doc):
        return c4d.CMD_ENABLED


# =============================================================================
# Message plugin  (background polling even when dialog is closed)
# =============================================================================

class RenderTimerMsg(plugins.MessageData):

    def GetTimer(self):
        return POLL_MS

    def CoreMessage(self, id, bc):
        get_tracker().poll()
        return True


# =============================================================================
# Lifecycle: handle Cinema 4D shutdown gracefully
# =============================================================================

def PluginMessage(id, data):
    if id == c4d.C4DPL_ENDACTIVITY:
        # Cinema 4D is shutting down - save any active renders as STOP
        tracker = get_tracker()
        if tracker.has_active:
            _log("C4D shutting down, stopping active render tracking")
            tracker.stop_all_active()
    return True


# =============================================================================
# Registration
# =============================================================================

if __name__ == "__main__":
    get_tracker()

    plugins.RegisterMessagePlugin(
        PLUGIN_ID_MSG,
        "Render Time Tracker Monitor",
        c4d.PLUGINFLAG_HIDEPLUGINMENU,
        RenderTimerMsg(),
    )

    plugins.RegisterCommandPlugin(
        PLUGIN_ID_CMD,
        "Render Time Tracker",
        0,
        None,
        "Track render queue job times and export reports",
        RenderTimeTrackerCmd(),
    )

    _log("Plugin loaded - Extensions > Render Time Tracker")
    _log("Logs: {}".format(get_log().log_dir))
