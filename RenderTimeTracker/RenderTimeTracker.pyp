"""
Render Time Tracker - Cinema 4D Plugin

Tracks render queue (Batch Render) job durations.
Shows per-job timing and total render time for the session.
Supports JSON report export.

Installation:
    Copy the 'RenderTimeTracker' folder into your Cinema 4D plugins directory:
        Windows: C:\\Program Files\\Maxon Cinema 4D <version>\\plugins\\
        macOS:   /Applications/Maxon Cinema 4D <version>/plugins/
    Restart Cinema 4D.

Usage:
    Extensions > Render Time Tracker
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
PLUGIN_ID_MSG = 1060510   # MessageData (background monitor)
PLUGIN_ID_CMD = 1060511   # CommandData (menu entry / dialog)

# =============================================================================
# Settings
# =============================================================================
POLL_MS = 2000  # Render queue poll interval in milliseconds

# =============================================================================
# Dialog widget IDs
# =============================================================================
ID_GRP_JOBS     = 10000
ID_LBL_TOTAL    = 10001
ID_LBL_COUNT    = 10002
ID_BTN_CLEAR    = 10003
ID_BTN_EXPORT   = 10004
ID_BTN_REFRESH  = 10005


# =============================================================================
# Helpers
# =============================================================================

def fmt_duration(seconds):
    """Format seconds as HH:MM:SS."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return "{:02d}:{:02d}:{:02d}".format(h, m, s)


def fmt_time(timestamp):
    """Format a unix timestamp as HH:MM:SS."""
    if timestamp is None:
        return ""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def fmt_date(timestamp):
    """Format a unix timestamp as YYYY-MM-DD HH:MM:SS."""
    if timestamp is None:
        return ""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def scene_name_from_path(path):
    """Extract a readable scene name from a file path."""
    if not path:
        return "Unknown"
    name = os.path.basename(str(path))
    return name if name else "Unknown"


# Status code to human-readable label mapping.
# Cinema 4D constants: RM_PROGRESS, RM_FINISHED, RM_FAILED, RM_STOPPED, etc.
STATUS_LABELS = {}

def _init_status_labels():
    """Build status label map from available c4d constants."""
    mapping = {
        "RM_PROGRESS":    "Rendering",
        "RM_FINISHED":    "Finished",
        "RM_FAILED":      "Failed",
        "RM_STOPPED":     "Stopped",
        "RM_DEACTIVATED": "Deactivated",
        "RM_WAITING":     "Waiting",
    }
    for attr, label in mapping.items():
        val = getattr(c4d, attr, None)
        if val is not None:
            STATUS_LABELS[val] = label

_init_status_labels()


def status_label(rm_status):
    return STATUS_LABELS.get(rm_status, "Unknown ({})".format(rm_status))


# =============================================================================
# Data model
# =============================================================================

class RenderJob(object):
    """Stores timing data for a single render queue job."""

    def __init__(self, queue_index, name):
        self.queue_index = queue_index
        self.name = name
        self.start_time = None   # unix timestamp
        self.end_time = None     # unix timestamp
        self.status = "Waiting"

    @property
    def duration(self):
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time else time.time()
        return max(0.0, end - self.start_time)

    @property
    def duration_str(self):
        return fmt_duration(self.duration)

    @property
    def is_active(self):
        return self.status == "Rendering"

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "start": fmt_date(self.start_time),
            "end": fmt_date(self.end_time),
            "duration_seconds": round(self.duration, 2),
            "duration_formatted": self.duration_str,
        }


# =============================================================================
# Tracker engine
# =============================================================================

class RenderTimeTracker(object):
    """Polls the Cinema 4D Batch Render queue and tracks job durations."""

    def __init__(self):
        self.jobs = []
        self._prev = {}       # queue_index -> (rm_status, name)
        self._changed = True  # flag: UI needs refresh

    # -- public API -----------------------------------------------------------

    def clear(self):
        """Clear all tracked jobs."""
        self.jobs = []
        self._prev = {}
        self._changed = True
        _log("Log cleared")

    @property
    def total_duration(self):
        return sum(j.duration for j in self.jobs if j.start_time)

    @property
    def total_duration_str(self):
        return fmt_duration(self.total_duration)

    @property
    def completed_count(self):
        return sum(1 for j in self.jobs if j.status in ("Finished", "Completed"))

    @property
    def has_active(self):
        return any(j.is_active for j in self.jobs)

    def needs_refresh(self):
        """Return True and reset flag if the UI should refresh."""
        if self._changed:
            self._changed = False
            return True
        return False

    def poll(self):
        """Check render queue; detect status changes and update jobs."""
        br = c4d.documents.GetBatchRender()
        if br is None:
            return

        count = br.GetElementCount()
        for i in range(count):
            rm_status = br.GetElementStatus(i)
            path = br.GetElement(i)
            name = scene_name_from_path(path)

            job = self._get_or_create(i, name)
            prev_entry = self._prev.get(i)
            prev_rm = prev_entry[0] if prev_entry else None

            if rm_status != prev_rm:
                self._on_transition(job, prev_rm, rm_status)
                self._prev[i] = (rm_status, name)
                self._changed = True

    def export_json(self, filepath):
        """Write a JSON report to *filepath*."""
        report = {
            "exported_at": datetime.datetime.now().isoformat(),
            "total_duration_seconds": round(self.total_duration, 2),
            "total_duration_formatted": self.total_duration_str,
            "job_count": len(self.jobs),
            "completed_count": self.completed_count,
            "jobs": [j.to_dict() for j in self.jobs],
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    # -- internals ------------------------------------------------------------

    def _get_or_create(self, index, name):
        for j in self.jobs:
            if j.queue_index == index:
                if j.name != name:
                    j.name = name
                return j
        job = RenderJob(index, name)
        self.jobs.append(job)
        return job

    def _on_transition(self, job, old_rm, new_rm):
        progress = getattr(c4d, "RM_PROGRESS", None)
        finished = getattr(c4d, "RM_FINISHED", None)
        failed   = getattr(c4d, "RM_FAILED", None)
        stopped  = getattr(c4d, "RM_STOPPED", None)

        if new_rm == progress:
            # Render started
            job.start_time = time.time()
            job.end_time = None
            job.status = "Rendering"
            _log("Started: {}".format(job.name))
            c4d.StatusSetText("[RenderTimer] Started: {}".format(job.name))

        elif new_rm == finished:
            if old_rm == progress and job.start_time:
                job.end_time = time.time()
                job.status = "Completed"
                _log("Completed: {} ({})".format(job.name, job.duration_str))
                c4d.StatusSetText(
                    "[RenderTimer] {} done - {}".format(job.name, job.duration_str))
            else:
                job.status = "Finished"

        elif new_rm == failed:
            if old_rm == progress and job.start_time:
                job.end_time = time.time()
            job.status = "Failed"
            _log("Failed: {}".format(job.name))
            c4d.StatusSetText("[RenderTimer] Failed: {}".format(job.name))

        elif new_rm == stopped:
            if old_rm == progress and job.start_time:
                job.end_time = time.time()
            job.status = "Stopped"
            _log("Stopped: {}".format(job.name))
            c4d.StatusSetText("[RenderTimer] Stopped: {}".format(job.name))


def _log(msg):
    """Print a timestamped message to the Cinema 4D Python console."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print("[RenderTimer {}] {}".format(ts, msg))


# =============================================================================
# Global instances
# =============================================================================

_tracker = None   # RenderTimeTracker
_dialog = None    # RenderTimeDialog


def get_tracker():
    global _tracker
    if _tracker is None:
        _tracker = RenderTimeTracker()
    return _tracker


# =============================================================================
# Dialog
# =============================================================================

class RenderTimeDialog(gui.GeDialog):
    """Non-modal dialog showing render time statistics."""

    def CreateLayout(self):
        self.SetTitle("Render Time Tracker")

        # Main container
        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0, "")
        self.GroupBorderSpace(10, 10, 10, 10)

        # Column headers
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 0, "")
        self.AddStaticText(0, c4d.BFH_LEFT,     30,  13, "#")
        self.AddStaticText(0, c4d.BFH_SCALEFIT,  0,  13, "Scene")
        self.AddStaticText(0, c4d.BFH_LEFT,     80,  13, "Status")
        self.AddStaticText(0, c4d.BFH_LEFT,     70,  13, "Start")
        self.AddStaticText(0, c4d.BFH_LEFT,     80,  13, "Duration")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # Scrollable job list
        self.ScrollGroupBegin(0,
                              c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT)
        self.GroupBegin(ID_GRP_JOBS,
                        c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "")
        self.GroupEnd()
        self.ScrollGroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # Summary row
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "")
        self.AddStaticText(0,            c4d.BFH_LEFT, 0, 0, "Jobs:")
        self.AddStaticText(ID_LBL_COUNT, c4d.BFH_LEFT, 60, 0, "0")
        self.AddStaticText(0,            c4d.BFH_LEFT, 0, 0, "Total time:")
        self.AddStaticText(ID_LBL_TOTAL, c4d.BFH_LEFT, 100, 0, "00:00:00")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # Buttons
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "")
        self.AddButton(ID_BTN_REFRESH, c4d.BFH_LEFT, 90, 0, "Refresh")
        self.AddButton(ID_BTN_EXPORT,  c4d.BFH_LEFT, 90, 0, "Export JSON")
        self.AddButton(ID_BTN_CLEAR,   c4d.BFH_LEFT, 90, 0, "Clear Log")
        self.GroupEnd()

        self.GroupEnd()  # main container
        return True

    def InitValues(self):
        self._rebuild()
        return True

    def Timer(self, msg):
        """Called periodically when SetTimer is active."""
        tracker = get_tracker()
        tracker.poll()
        # Refresh if data changed or if jobs are actively rendering
        # (so the duration counters keep ticking)
        if tracker.needs_refresh() or tracker.has_active:
            self._rebuild()

    def Command(self, id, msg):
        if id == ID_BTN_CLEAR:
            get_tracker().clear()
            self._rebuild()

        elif id == ID_BTN_EXPORT:
            self._export()

        elif id == ID_BTN_REFRESH:
            get_tracker().poll()
            self._rebuild()

        return True

    # -- display --------------------------------------------------------------

    def _rebuild(self):
        """Rebuild the job list group."""
        tracker = get_tracker()

        self.LayoutFlushGroup(ID_GRP_JOBS)

        if not tracker.jobs:
            self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0,
                               "No render jobs tracked yet. "
                               "Add scenes to the Render Queue and start rendering.")
        else:
            for idx, job in enumerate(tracker.jobs):
                self.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 0, "")
                self.AddStaticText(0, c4d.BFH_LEFT,     30, 0, str(idx + 1))
                self.AddStaticText(0, c4d.BFH_SCALEFIT,  0, 0, job.name)
                self.AddStaticText(0, c4d.BFH_LEFT,     80, 0, job.status)
                self.AddStaticText(0, c4d.BFH_LEFT,     70, 0,
                                   fmt_time(job.start_time))
                self.AddStaticText(0, c4d.BFH_LEFT,     80, 0, job.duration_str)
                self.GroupEnd()

        self.LayoutChanged(ID_GRP_JOBS)

        # Update summary
        self.SetString(ID_LBL_TOTAL,
                       tracker.total_duration_str)
        self.SetString(ID_LBL_COUNT,
                       "{} / {}".format(tracker.completed_count, len(tracker.jobs)))

    def _export(self):
        path = storage.SaveDialog(
            c4d.FILESELECTTYPE_ANYTHING,
            "Save Render Time Report",
            "json")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            get_tracker().export_json(path)
            gui.MessageDialog("Report saved:\n{}".format(path))
        except Exception as e:
            gui.MessageDialog("Export failed:\n{}".format(e))


# =============================================================================
# Command plugin  (Extensions > Render Time Tracker)
# =============================================================================

class RenderTimeTrackerCmd(plugins.CommandData):
    """Opens / closes the Render Time Tracker dialog."""

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
                defaultw=600,
                defaulth=350,
            )
            _dialog.SetTimer(POLL_MS)
        return True

    def RestoreLayout(self, sec_ref):
        """Restore dialog when Cinema 4D reloads its layout."""
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
    """Polls the render queue in the background."""

    def GetTimer(self):
        return POLL_MS

    def CoreMessage(self, id, bc):
        get_tracker().poll()
        return True


# =============================================================================
# Plugin registration
# =============================================================================

if __name__ == "__main__":
    # Make sure tracker exists from the start
    get_tracker()

    # Background monitor
    plugins.RegisterMessagePlugin(
        PLUGIN_ID_MSG,
        "Render Time Tracker Monitor",
        c4d.PLUGINFLAG_HIDEPLUGINMENU,
        RenderTimerMsg(),
    )

    # Menu command
    plugins.RegisterCommandPlugin(
        PLUGIN_ID_CMD,
        "Render Time Tracker",
        0,
        None,   # icon bitmap (None = no icon)
        "Track render queue job times and export reports",
        RenderTimeTrackerCmd(),
    )

    _log("Plugin loaded - Extensions > Render Time Tracker")
