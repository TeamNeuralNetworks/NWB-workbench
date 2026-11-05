from __future__ import annotations
from GUI.project_paths import external_call as _pp_external_call, external_paths as _pp_external_paths, compact_external_configs as _pp_compact, encode as _pp_encode, active_root as _pp_active, portable_phy as _pp_phy
from GUI.project_paths import json_dump as _pp_json_dump, json_load as _pp_json_load, read_excel as _pp_read_excel

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import traceback
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from GUI.qt_compat import QtCore, QtGui, QtWidgets

from GUI.panels.spike_sorting_preprocessing_discovery import discover_preprocessings


try:
    import pyqtgraph as pg
except Exception as exc:
    pg = None
    _PYQTGRAPH_IMPORT_ERROR = exc
else:
    _PYQTGRAPH_IMPORT_ERROR = None


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _QtAppendLogStream:
    """File-like stream that forwards stdout/stderr text to a Qt log widget.

    This is mainly used around SpikeInterface export/GUI calls, because tqdm
    and warnings write directly to stderr instead of returning strings.

    Carriage-return progress updates (tqdm) are forwarded as replacement lines
    by prefixing the emitted text with ``\r``. The receiver can then replace the
    last log line instead of appending thousands of progress lines.
    """

    def __init__(self, append_func, *, process_events=True, min_interval=0.10):
        self._append_func = append_func
        self._process_events = bool(process_events)
        self._min_interval = float(min_interval)
        self._buffer = ""
        self._last_emit = 0.0
        self._last_replace = ""

    def writable(self):
        return True

    def isatty(self):
        # Keep False so libraries do not try terminal-only control sequences.
        return False

    def write(self, text):
        if text is None:
            return 0
        raw = str(text)
        if not raw:
            return 0
        s = _ANSI_ESCAPE_RE.sub("", raw)

        # tqdm redraws the same terminal line using \r. Do not convert \r to a
        # newline. Emit these pieces as replacement lines instead.
        if "\r" in s:
            parts = s.split("\r")
            if parts[0]:
                self._buffer += parts[0]
                self._emit_complete_lines()
            for part in parts[1:]:
                if self._buffer.strip():
                    self._emit(self._buffer, replace_last=True)
                    self._buffer = ""
                if not part:
                    continue
                # A tqdm update can contain a trailing newline when it finishes.
                if "\n" in part:
                    first, rest = part.split("\n", 1)
                    if first.strip():
                        self._emit(first, replace_last=True)
                    self._buffer += rest
                    self._emit_complete_lines()
                else:
                    now = time.monotonic()
                    if part.strip() and (part != self._last_replace or (now - self._last_emit) >= self._min_interval):
                        self._emit(part, replace_last=True)
                        self._last_replace = part
            return len(raw)

        self._buffer += s
        self._emit_complete_lines()
        return len(raw)

    def flush(self):
        if self._buffer.strip():
            self._emit(self._buffer)
        self._buffer = ""

    def _emit_complete_lines(self):
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)

    def _emit(self, line, *, replace_last=False):
        line = _ANSI_ESCAPE_RE.sub("", str(line)).strip()
        if not line:
            return
        try:
            prefix = "\r" if replace_last else ""
            self._append_func(prefix + line)
            self._last_emit = time.monotonic()
            if self._process_events:
                QtWidgets.QApplication.processEvents()
        except Exception:
            pass

def _natural_text_key(value):
    """Natural-sort helper for channel labels such as ch2 before ch10."""
    parts = re.split(r"(\d+)", str(value))
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


def _curation_natural_sort_key(value):
    """Comparable, case-insensitive natural key used by Curation headers."""
    parts = re.split(r"(\d+)", str(value or "").strip())
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in parts
        if part != ""
    )


class _CurationSortableItem(QtWidgets.QTableWidgetItem):
    """Table item whose sort key can be numeric, chronological, or natural text."""

    def __init__(self, text="", *, sort_key=None):
        super().__init__(str(text))
        self._curation_sort_key = sort_key

    def __lt__(self, other):
        if isinstance(other, _CurationSortableItem):
            try:
                return self._curation_sort_key < other._curation_sort_key
            except Exception:
                pass
        return super().__lt__(other)


def _check_sorter_installed_locally(sorter_name: str, emit_func=None) -> bool:
    """Return True only when SpikeInterface reports a local sorter install.

    If the check is inconclusive, return False so the caller can use Docker.
    This matches the intended run policy: local install first, Docker otherwise.
    """
    sorter_name = str(sorter_name or "").strip()
    if not sorter_name:
        return False

    def _emit(msg):
        if callable(emit_func):
            try:
                emit_func(str(msg))
            except Exception:
                pass

    try:
        from spikeinterface.sorters import is_sorter_installed
        if callable(is_sorter_installed):
            return bool(is_sorter_installed(sorter_name))
    except Exception as exc:
        _emit(f"Could not use is_sorter_installed({sorter_name}): {exc}")

    try:
        import spikeinterface.sorters as ss
        installed_sorters = getattr(ss, "installed_sorters", None)
        if callable(installed_sorters):
            installed = installed_sorters() or []
            return sorter_name in {str(x) for x in installed}
    except Exception as exc:
        _emit(f"Could not query installed_sorters(): {exc}")

    try:
        from spikeinterface.sorters import sorterlist
        sorter_cls = (getattr(sorterlist, "sorter_dict", {}) or {}).get(sorter_name)
        if sorter_cls is not None:
            checker = getattr(sorter_cls, "is_installed", None)
            if callable(checker):
                return bool(checker())
    except Exception as exc:
        _emit(f"Could not query sorter class installation for {sorter_name}: {exc}")

    return False



@contextlib.contextmanager
def _suppress_gui_autolaunch_for_child_processes():
    """Prevent multiprocessing/subprocess children from opening another NWB GUI.

    Some sorter backends start a fresh Python process. On Windows, that process
    can re-import the script used to launch the GUI. If that launcher calls
    appLauncher() at import time, a second main GUI window appears. The child
    process inherits these environment variables, and GUI/project_launcher.py
    now checks them before creating a QApplication/window.
    """
    keys = {
        "NWB_GUI_SUPPRESS_AUTOLAUNCH": "1",
        "NWB_GUI_CHILD_PROCESS": "1",
        "MPLBACKEND": "Agg",
    }
    old_values = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in keys.items():
            os.environ[k] = v
        yield
    finally:
        for k, old in old_values.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

@contextlib.contextmanager
def _suppress_matplotlib_windows_for_worker():
    """Run Matplotlib code off-screen from worker code.

    Some plotting functions or SpikeInterface widgets may try to use the Qt
    Matplotlib backend if pyplot has already been imported elsewhere in the
    application. During sorter execution we only want files saved to disk, not
    new GUI windows, so temporarily force an off-screen backend and suppress
    show() calls.
    """
    plt = None
    figure_mod = None
    old_pyplot_show = None
    old_figure_show = None
    was_interactive = None

    try:
        try:
            import matplotlib
            try:
                matplotlib.use("Agg", force=True)
            except Exception:
                pass
        except Exception:
            pass

        try:
            import matplotlib.pyplot as plt  # type: ignore
            import matplotlib.figure as figure_mod  # type: ignore
            old_pyplot_show = getattr(plt, "show", None)
            old_figure_show = getattr(figure_mod.Figure, "show", None)
            try:
                was_interactive = bool(plt.isinteractive())
                plt.ioff()
            except Exception:
                was_interactive = None

            try:
                plt.show = lambda *args, **kwargs: None
            except Exception:
                pass
            try:
                figure_mod.Figure.show = lambda self, *args, **kwargs: None
            except Exception:
                pass
        except Exception:
            plt = None
            figure_mod = None

        yield

    finally:
        try:
            if plt is not None:
                plt.close("all")
        except Exception:
            pass

        try:
            if plt is not None and old_pyplot_show is not None:
                plt.show = old_pyplot_show
        except Exception:
            pass

        try:
            if figure_mod is not None and old_figure_show is not None:
                figure_mod.Figure.show = old_figure_show
        except Exception:
            pass

        try:
            if plt is not None and was_interactive:
                plt.ion()
        except Exception:
            pass


def _available_multiprocessing_jobs(n_jobs=None):
    """Return the worker count used by spike sorting multiprocessing.

    ``n_jobs=None`` means all CPU cores except one, with one worker as the
    minimum. Explicit values are clamped to at least one worker.
    """
    if n_jobs is not None:
        try:
            return max(1, int(n_jobs))
        except Exception:
            return 1
    try:
        cores = int(os.cpu_count() or 1)
    except Exception:
        cores = 1
    return max(1, cores - 1)


def _env_flag(name, default=True):
    value = os.environ.get(str(name), None)
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    return bool(default)


def _env_optional_int(name, default=None):
    value = os.environ.get(str(name), None)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def _launcher_multiprocessing_enabled():
    """Global spike-sorting multiprocessing setting from appLauncher()."""
    return _env_flag("NWB_SPIKE_SORTING_MULTIPROCESSING", True)


def _launcher_n_jobs():
    """Global spike-sorting n_jobs setting from appLauncher()."""
    return _env_optional_int("NWB_SPIKE_SORTING_N_JOBS", None)


def _spikeinterface_job_kwargs(enabled=True, n_jobs=None):
    """Job kwargs shared by SpikeInterface compute/summary calls when supported."""
    if not bool(enabled):
        return {}
    return {"n_jobs": _available_multiprocessing_jobs(n_jobs)}


def _apply_worker_count_to_params(params, *, enabled=True, n_jobs=None):
    """Set common worker-count parameters when a sorter exposes them.

    This does not invent a worker parameter for sorters that do not support one.
    It only updates keys that are already present in the sorter parameters.
    """
    out = dict(params or {})
    if not bool(enabled):
        return out, []
    worker_count = _available_multiprocessing_jobs(n_jobs)
    changed = []
    for key in ("n_jobs", "n_workers", "num_workers", "n_processes", "num_processes", "parallel_jobs"):
        if key in out:
            out[key] = worker_count
            changed.append(key)
    job_kwargs = out.get("job_kwargs", None)
    if isinstance(job_kwargs, dict):
        job_kwargs = dict(job_kwargs)
        job_kwargs["n_jobs"] = worker_count
        out["job_kwargs"] = job_kwargs
        changed.append("job_kwargs.n_jobs")
    return out, changed


def _sorter_param_descriptions(sorter_name: str) -> dict:
    """Return SpikeInterface sorter parameter descriptions when available.

    SpikeInterface has changed this API over versions, so this helper tries the
    public module functions first and then falls back to sorter-class metadata.
    """
    sorter_name = str(sorter_name or "").strip()
    if not sorter_name:
        return {}

    def _normalize(obj):
        if not obj:
            return {}
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if isinstance(v, (list, tuple)):
                    v = "\n".join(str(x) for x in v)
                out[str(k)] = str(v)
            return out
        return {}

    # Newer/common public helpers.
    for module_name in ("spikeinterface.sorters", "spikeinterface.sorters.sorterlist"):
        try:
            mod = __import__(module_name, fromlist=["dummy"])
        except Exception:
            continue
        for func_name in (
            "get_sorter_params_description",
            "get_sorter_params_descriptions",
            "get_params_description",
            "get_params_descriptions",
        ):
            func = getattr(mod, func_name, None)
            if callable(func):
                try:
                    desc = _normalize(func(sorter_name))
                    if desc:
                        return desc
                except Exception:
                    pass

    # Sorter class fallback.
    try:
        from spikeinterface.sorters import sorterlist
        sorter_cls = (getattr(sorterlist, "sorter_dict", {}) or {}).get(sorter_name)
        if sorter_cls is not None:
            for attr in (
                "params_description",
                "params_descriptions",
                "_params_description",
                "_params_descriptions",
                "sorter_params_description",
            ):
                obj = getattr(sorter_cls, attr, None)
                try:
                    obj = obj() if callable(obj) else obj
                except TypeError:
                    try:
                        obj = obj(sorter_name)
                    except Exception:
                        obj = None
                except Exception:
                    obj = None
                desc = _normalize(obj)
                if desc:
                    return desc
    except Exception:
        pass

    return {}


# -----------------------------
# PyQtGraph preprocessing ViewBox
# -----------------------------

class _PreprocessingTraceViewBox(pg.ViewBox if pg is not None else object):
    """ViewBox used by the preprocessing browser.

    Left drag keeps the normal PyQtGraph x-pan behavior. Right drag is used by
    the owner widget: horizontal movement changes the visible time window and
    vertical movement changes trace gain.
    """

    def __init__(self, owner=None, *args, **kwargs):
        if pg is None:
            return
        kwargs.setdefault("enableMenu", False)
        super().__init__(*args, **kwargs)
        self.owner = owner
        try:
            self.setMouseMode(pg.ViewBox.PanMode)
        except Exception:
            pass

    def mouseDragEvent(self, ev, axis=None):
        try:
            button = ev.button()
        except Exception:
            button = None

        if button == _qt_right_button():
            if self.owner is not None and not ev.isStart():
                try:
                    dx = float(ev.pos().x() - ev.lastPos().x())
                except Exception:
                    dx = 0.0
                try:
                    dy = float(ev.pos().y() - ev.lastPos().y())
                except Exception:
                    dy = 0.0
                self.owner._apply_right_drag(dx_pixels=dx, dy_pixels=dy)
            ev.accept()
            return

        # Let PyQtGraph pan normally for left drag, but the widget has y mouse
        # disabled, so this is x-only panning. Then trigger the buffered loader
        # while dragging if the visible range gets close to the buffer edge.
        try:
            super().mouseDragEvent(ev, axis=axis)
        finally:
            if button == _qt_left_button() and self.owner is not None:
                self.owner._maybe_schedule_reload_from_current_range(delay_ms=35)

    def mouseClickEvent(self, ev):
        try:
            button = ev.button()
        except Exception:
            button = None

        # Short click only. PyQtGraph sends real drags to mouseDragEvent, so
        # this does not fire when the user pans or changes gain/window size.
        if self.owner is not None and button == _qt_left_button():
            try:
                pos = self.mapSceneToView(ev.scenePos())
                self.owner._set_time_marker(float(pos.x()))
            except Exception:
                pass
            ev.accept()
            return

        if self.owner is not None and button == _qt_right_button():
            self.owner._clear_time_marker()
            ev.accept()
            return

        return super().mouseClickEvent(ev)


# -----------------------------
# small Qt helpers
# -----------------------------

def _qt_user_role():
    return QtCore.Qt.ItemDataRole.UserRole if hasattr(QtCore.Qt, "ItemDataRole") else QtCore.Qt.UserRole


def _qt_checked():
    return QtCore.Qt.CheckState.Checked if hasattr(QtCore.Qt, "CheckState") else QtCore.Qt.Checked


def _qt_unchecked():
    return QtCore.Qt.CheckState.Unchecked if hasattr(QtCore.Qt, "CheckState") else QtCore.Qt.Unchecked


def _qt_left_button():
    return QtCore.Qt.MouseButton.LeftButton if hasattr(QtCore.Qt, "MouseButton") else QtCore.Qt.LeftButton


def _qt_right_button():
    return QtCore.Qt.MouseButton.RightButton if hasattr(QtCore.Qt, "MouseButton") else QtCore.Qt.RightButton


def _safe_float(value, default=0.0):
    try:
        out = float(value)
        if not math.isfinite(out):
            return default
        return out
    except Exception:
        return default



def _safe_folder_name(value):
    text = str(value or "recording").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = text.strip("._")
    return text or "recording"


def _safe_filename(value, default="recording"):
    """Return a safe filename stem while preserving readable run labels."""
    text = str(value or default or "recording").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = text.strip("._")
    return text or str(default or "recording")


def _json_safe(value):
    """Return a deterministic JSON-safe representation for run signatures."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        try:
            return value.item()
        except Exception:
            return str(value)
    if isinstance(value, np.ndarray):
        try:
            return _json_safe(value.tolist())
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _canonical_json_text(value):
    return json.dumps(_pp_encode(_json_safe(value), _pp_active()), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _signature_hash(value):
    return hashlib.sha256(_canonical_json_text(value).encode("utf-8")).hexdigest()


def _sorter_parameter_pipeline_key(sorter_name, sorter_params):
    """Return the same sorter/parameter identity used by the Curation view."""
    sorter_params = sorter_params if isinstance(sorter_params, dict) else {}
    return _canonical_json_text({
        "sorter": str(sorter_name or ""),
        "params": dict(sorter_params or {}),
    })


def _sorter_parameter_pipeline_numbers(configurations):
    """Assign Pipeline 1, Pipeline 2, ... to unique sorter parameter sets."""
    numbers = {}
    for configuration in configurations or []:
        if not isinstance(configuration, dict):
            continue
        key = _sorter_parameter_pipeline_key(
            configuration.get("sorter_name", configuration.get("sorter", "")),
            configuration.get("sorter_params", configuration.get("params", {})),
        )
        if key not in numbers:
            numbers[key] = len(numbers) + 1
    return numbers


def _enabled_pipeline_for_signature(pipeline):
    enabled = [s for s in (dict(pipeline or {}).get("enabled_pipeline", []) or []) if isinstance(s, dict)]
    out = []
    for step in enabled:
        out.append({
            "name": str(step.get("name", "") or "").strip(),
            "order": _json_safe(step.get("order", None)),
            "params": _json_safe(dict(step.get("params", {}) or {})),
        })
    return out


def _preprocessing_pipeline_key(pipeline):
    """Return the preprocessing identity displayed by Sorting and Curation."""
    return _canonical_json_text(_enabled_pipeline_for_signature(pipeline))


def _preprocessing_pipeline_numbers(configurations):
    """Assign Pipeline 1, Pipeline 2, ... to configured preprocessing sets."""
    numbers = {}
    for configuration in configurations or []:
        if not isinstance(configuration, dict):
            continue
        pipeline = configuration.get("preprocessing_pipeline", {})
        if not isinstance(pipeline, dict):
            pipeline = {}
        key = _preprocessing_pipeline_key(pipeline)
        if key not in numbers:
            numbers[key] = len(numbers) + 1
    return numbers


def _build_sorting_run_signature(*, label, group, recipe, pipeline, sorter_name, sorter_params, summary_config):
    """Describe the scientific/run configuration used to identify duplicate runs.

    The hash deliberately includes the selected recording files, the recording recipe,
    enabled preprocessing steps and parameters, sorter name/parameters, and unit-summary
    configuration. If any of these changes, the GUI creates a distinct run folder.
    """
    group = dict(group or {})
    recipe = dict(recipe or {})
    return {
        "signature_version": 1,
        "recording_label": str(label or "recording"),
        "recording_group": _json_safe(group),
        "recording_files": [str(p) for p in (group.get("files", []) or [])],
        "recording_recipe": _json_safe(recipe),
        "enabled_preprocessing": _enabled_pipeline_for_signature(pipeline),
        "sorter_name": str(sorter_name or "").strip(),
        "sorter_params": _json_safe(dict(sorter_params or {})),
        "unit_summary_config": _json_safe(dict(summary_config or {})),
    }


def _sorting_run_folder_name(signature_hash):
    return f"run_{str(signature_hash)[:12]}"


def _sorting_runs_root(sorting_path):
    """Root folder for sorting runs.

    New runs are stored directly as:
        <sorting_project>/sorter_output/run_<hash>

    Older nested layouts are still discovered by scanning recursively under this
    same root.
    """
    return Path(sorting_path) / "sorter_output"


def _sorter_work_folder(output_folder):
    """Short SpikeInterface work folder inside one run folder."""
    return Path(output_folder) / "si"


def _sorter_work_folder_candidates(output_folder):
    output_folder = Path(output_folder)
    return [
        _sorter_work_folder(output_folder),
        output_folder / "SorterRun",  # legacy layout
    ]


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = _pp_json_load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _run_signature_hash_from_summary(payload):
    if not isinstance(payload, dict):
        return ""
    if _pp_active() and isinstance(payload.get("run_signature"), dict):
        return _signature_hash(payload["run_signature"])
    h = str(payload.get("run_signature_hash", "") or "").strip()
    if h:
        return h
    sig = payload.get("run_signature")
    if isinstance(sig, dict):
        return _signature_hash(sig)
    return ""


def _find_matching_sorting_runs(base_folder, signature_hash):
    base_folder = Path(base_folder)
    signature_hash = str(signature_hash or "").strip()
    if not signature_hash or not base_folder.exists():
        return []
    matches = []
    for pattern in ("sorting_run.json", "run_config.json"):
        for run_json in base_folder.rglob(pattern):
            payload = _load_json_file(run_json)
            if _run_signature_hash_from_summary(payload) == signature_hash:
                matches.append(run_json.parent)
    return sorted(set(matches), key=lambda p: _natural_text_key(str(p)))


def _unique_sorting_run_folder(base_folder, signature_hash):
    base_folder = Path(base_folder)
    preferred = base_folder / _sorting_run_folder_name(signature_hash)
    if not preferred.exists():
        return preferred
    # This is a safety fallback for legacy or incomplete folders without a matching
    # sorting_run.json. Normal duplicate handling happens before this point.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for idx in range(1, 1000):
        candidate = base_folder / f"{_sorting_run_folder_name(signature_hash)}_{stamp}_{idx:02d}"
        if not candidate.exists():
            return candidate
    return base_folder / f"{_sorting_run_folder_name(signature_hash)}_{stamp}"


def _native_scrollbar_without_arrow_buttons_stylesheet():
    """Keep the platform/native scrollbar look, but hide the end arrow buttons."""
    return """
QScrollBar::add-line:horizontal {
    width: 0px;
    border: none;
    background: transparent;
}
QScrollBar::sub-line:horizontal {
    width: 0px;
    border: none;
    background: transparent;
}
"""


# -----------------------------
# background worker
# -----------------------------

class _RecordingLoadWorker(QtCore.QObject):
    finished = QtCore.Signal(str, object, str)
    failed = QtCore.Signal(str, str)
    progress = QtCore.Signal(str)

    def __init__(self, label: str, item: dict, recipe: dict):
        super().__init__()
        self.label = str(label)
        self.item = dict(item or {})
        self.recipe = dict(recipe or {})

    def run(self):
        try:
            files = [str(p) for p in (self.item.get("files", []) or []) if str(p).strip()]
            data_path = str((self.recipe.get("data", {}) or {}).get("path", "")).strip()
            self.progress.emit(f"Loading recording: {self.label}")
            self.progress.emit(f"Files: {len(files)}")
            if data_path:
                self.progress.emit(f"ElectricalSeries: {data_path}")
            if bool(self.recipe.get("remove_bad_channels", False)):
                self.progress.emit("Bad-channel removal enabled.")
            recording = self._build_recording_from_recipe_item(self.item, self.recipe, progress=self.progress.emit)
            info = self._recording_info(recording)
            self.progress.emit(f"Loaded: {info}")
            self.finished.emit(self.label, recording, info)
        except Exception:
            self.failed.emit(self.label, traceback.format_exc())

    @staticmethod
    def _read_nwb_recording(*, file_path: str, electrical_series_path: str):
        errors = []
        try:
            import spikeinterface.extractors as se
            return se.read_nwb(file_path=file_path, electrical_series_path=electrical_series_path)
        except Exception as exc:
            errors.append(f"spikeinterface.extractors.read_nwb: {exc}")

        try:
            import spikeinterface.full as si
            return si.read_nwb(file_path=file_path, electrical_series_path=electrical_series_path)
        except Exception as exc:
            errors.append(f"spikeinterface.full.read_nwb: {exc}")

        raise RuntimeError(
            "Could not read NWB recording:\n"
            f"file_path={file_path}\n"
            f"electrical_series_path={electrical_series_path}\n\n"
            "Tried:\n- " + "\n- ".join(errors)
        )

    @staticmethod
    def _clean_channel_text(value):
        try:
            if value is None:
                return ""
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            text = str(value).strip()
            if text.lower() in {"", "nan", "none", "nat"}:
                return ""
            return text
        except Exception:
            return ""

    @staticmethod
    def _bad_channel_bool_series(electrodes):
        bad = electrodes["bad_channel"].fillna(False)
        if bad.dtype == object:
            return bad.astype(str).str.strip().str.lower().isin(("1", "true", "yes", "y", "bad"))
        return bad.astype(bool)

    @classmethod
    def _bad_channel_metadata_from_nwb(cls, nwb_file_path, recording_channel_ids=None):
        """Return bad/good channel metadata from the NWB electrodes table.

        The returned channel IDs are mapped onto the SpikeInterface recording
        channel IDs when those are provided.  Without recording_channel_ids this
        still returns readable labels, which is used by the GUI to warn the user
        before starting a concatenated sorting run.
        """
        meta = {
            "path": str(nwb_file_path),
            "has_electrodes": False,
            "has_bad_channel_column": False,
            "all_labels": [],
            "bad_labels": [],
            "good_labels": [],
            "all_channels": [],
            "bad_channels": [],
            "good_channels": [],
        }
        try:
            from pynwb import NWBHDF5IO
        except Exception:
            return meta

        try:
            with NWBHDF5IO(str(nwb_file_path), mode="r") as io:
                nwb = io.read()
                if not hasattr(nwb, "electrodes") or nwb.electrodes is None:
                    return meta
                electrodes = nwb.electrodes.to_dataframe()
        except Exception:
            return meta

        if electrodes is None or getattr(electrodes, "empty", True):
            return meta

        meta["has_electrodes"] = True
        if "bad_channel" not in electrodes.columns:
            return meta

        meta["has_bad_channel_column"] = True
        bad = cls._bad_channel_bool_series(electrodes)

        candidate_columns = [
            "contact_ids",
            "channel_name",
            "channel_id",
            "id",
            "label",
            "electrode_label",
            "group_name",
        ]

        # recording_channel_ids can be a numpy array from SpikeInterface.
        # Avoid ``recording_channel_ids or []`` because numpy arrays cannot be
        # evaluated as a single True/False value.
        if recording_channel_ids is None:
            recording_channel_ids = []
        else:
            try:
                recording_channel_ids = list(recording_channel_ids)
            except Exception:
                recording_channel_ids = [recording_channel_ids]
        by_text = {cls._clean_channel_text(ch): ch for ch in recording_channel_ids if cls._clean_channel_text(ch)}

        rows = []
        for idx, row in electrodes.iterrows():
            label = ""
            candidates = []
            for col in candidate_columns:
                if col in electrodes.columns:
                    value = row.get(col, None)
                    txt = cls._clean_channel_text(value)
                    if txt and not label:
                        label = txt
                    if txt:
                        candidates.append(value)
            idx_text = cls._clean_channel_text(idx)
            if idx_text:
                candidates.append(idx)
            if not label:
                label = idx_text or str(len(rows))

            channel_id = None
            if recording_channel_ids:
                for value in candidates:
                    if value in recording_channel_ids:
                        channel_id = value
                        break
                    txt = cls._clean_channel_text(value)
                    if txt in by_text:
                        channel_id = by_text[txt]
                        break
            else:
                channel_id = label

            try:
                is_bad = bool(bad.loc[idx])
            except Exception:
                try:
                    is_bad = bool(bad.iloc[len(rows)])
                except Exception:
                    is_bad = False
            rows.append((label, channel_id, is_bad))

        all_labels, bad_labels, good_labels = [], [], []
        all_channels, bad_channels, good_channels = [], [], []
        for label, channel_id, is_bad in rows:
            if label not in all_labels:
                all_labels.append(label)
            if channel_id is not None and channel_id not in all_channels:
                all_channels.append(channel_id)
            if is_bad:
                if label not in bad_labels:
                    bad_labels.append(label)
                if channel_id is not None and channel_id not in bad_channels:
                    bad_channels.append(channel_id)
            else:
                if label not in good_labels:
                    good_labels.append(label)
                if channel_id is not None and channel_id not in good_channels:
                    good_channels.append(channel_id)

        # If the electrode table could not be mapped to the recording IDs, treat
        # the current recording channels as good instead of accidentally removing
        # every channel.
        if recording_channel_ids and not all_channels:
            meta["all_channels"] = list(recording_channel_ids)
            meta["good_channels"] = list(recording_channel_ids)
            meta["bad_channels"] = []
        else:
            meta["all_channels"] = all_channels
            meta["good_channels"] = good_channels
            meta["bad_channels"] = bad_channels
        meta["all_labels"] = all_labels
        meta["good_labels"] = good_labels
        meta["bad_labels"] = bad_labels
        return meta

    @staticmethod
    def _select_channels_if_possible(recording, channel_ids):
        channel_ids = list(channel_ids or [])
        if not channel_ids:
            return None
        try:
            return recording.select_channels(channel_ids)
        except Exception:
            return None

    @classmethod
    def _select_good_channels_from_metadata(cls, recording, meta):
        if not bool((meta or {}).get("has_bad_channel_column", False)):
            return recording
        selected = cls._select_channels_if_possible(recording, (meta or {}).get("good_channels", []))
        if selected is None:
            return recording
        return selected

    @staticmethod
    def _ordered_channel_texts_from_first_recording(first_recording, wanted_texts):
        wanted_texts = set(str(t) for t in (wanted_texts or []) if str(t).strip())
        out = []
        try:
            first_ids = list(first_recording.get_channel_ids())
        except Exception:
            first_ids = []
        for ch in first_ids:
            txt = _RecordingLoadWorker._clean_channel_text(ch)
            if txt in wanted_texts and txt not in out:
                out.append(txt)
        # Include metadata-only labels as a fallback. select_channels will later
        # ignore labels that cannot be mapped onto a given recording.
        for txt in wanted_texts:
            if txt not in out:
                out.append(txt)
        return out

    @classmethod
    def _select_common_channels_for_concatenation(cls, recordings, metas, policy, log):
        recordings = list(recordings or [])
        metas = list(metas or [])
        if len(recordings) <= 1:
            return recordings

        policy = str(policy or "per_file").strip().lower()
        if policy not in {"good_in_all_files", "good_in_any_file"}:
            return recordings

        good_sets = []
        recording_text_sets = []
        for rec, meta in zip(recordings, metas):
            try:
                channel_ids = list(rec.get_channel_ids())
            except Exception:
                channel_ids = []
            rec_texts = {cls._clean_channel_text(ch) for ch in channel_ids if cls._clean_channel_text(ch)}
            recording_text_sets.append(rec_texts)

            if bool((meta or {}).get("has_bad_channel_column", False)):
                good = {cls._clean_channel_text(ch) for ch in (meta.get("good_channels", []) or []) if cls._clean_channel_text(ch)}
                if not good:
                    good = set(rec_texts)
            else:
                good = set(rec_texts)
            good_sets.append(good)

        if not good_sets:
            return recordings

        present_in_all = set.intersection(*recording_text_sets) if recording_text_sets else set()
        if policy == "good_in_any_file":
            wanted = set.union(*good_sets) & present_in_all
            policy_label = "good in at least one file"
        else:
            wanted = set.intersection(*good_sets) & present_in_all
            policy_label = "good in every file"

        ordered_texts = cls._ordered_channel_texts_from_first_recording(recordings[0], wanted)
        if not ordered_texts:
            raise RuntimeError(
                "No common channel remains after bad-channel handling for concatenation.\n"
                "Use a less strict bad-channel option or check the bad_channel metadata."
            )

        out = []
        for rec in recordings:
            try:
                by_text = {cls._clean_channel_text(ch): ch for ch in list(rec.get_channel_ids()) if cls._clean_channel_text(ch)}
                ids = [by_text[t] for t in ordered_texts if t in by_text]
            except Exception:
                ids = []
            selected = cls._select_channels_if_possible(rec, ids)
            if selected is None:
                raise RuntimeError("Could not select the common channel set after bad-channel handling.")
            out.append(selected)

        log(f"Bad-channel mismatch handling: kept {len(ordered_texts)} channel(s) that are {policy_label}.")
        return out

    @staticmethod
    def _concatenate_recordings(recordings):
        if len(recordings) == 1:
            return recordings[0]

        errors = []
        max_diff = 1e-3

        def _try(func, label):
            try:
                return func(recordings, sampling_frequency_max_diff=max_diff)
            except TypeError as exc_with_tolerance:
                try:
                    return func(recordings)
                except Exception as exc_without_tolerance:
                    errors.append(
                        f"{label}: with tolerance={exc_with_tolerance}; "
                        f"without tolerance={exc_without_tolerance}"
                    )
            except Exception as exc:
                errors.append(f"{label}: {exc}")
            return None

        try:
            import spikeinterface as si
            if hasattr(si, "concatenate_recordings"):
                out = _try(si.concatenate_recordings, "spikeinterface.concatenate_recordings")
                if out is not None:
                    return out
        except Exception as exc:
            errors.append(f"spikeinterface import/call: {exc}")

        try:
            from spikeinterface.core import concatenate_recordings
            out = _try(concatenate_recordings, "spikeinterface.core.concatenate_recordings")
            if out is not None:
                return out
        except Exception as exc:
            errors.append(f"spikeinterface.core.concatenate_recordings: {exc}")

        try:
            import spikeinterface.full as si
            if hasattr(si, "concatenate_recordings"):
                out = _try(si.concatenate_recordings, "spikeinterface.full.concatenate_recordings")
                if out is not None:
                    return out
        except Exception as exc:
            errors.append(f"spikeinterface.full.concatenate_recordings: {exc}")

        raise RuntimeError("Could not concatenate recordings:\n- " + "\n- ".join(errors))

    @staticmethod
    def _remove_bad_channels_from_nwb_metadata(nwb_file_path, recording):
        try:
            from pynwb import NWBHDF5IO
        except Exception as exc:
            raise RuntimeError(f"Could not import pynwb for bad-channel filtering: {exc}")

        with NWBHDF5IO(str(nwb_file_path), mode="r") as io:
            nwb = io.read()
            if not hasattr(nwb, "electrodes") or nwb.electrodes is None:
                return recording
            electrodes = nwb.electrodes.to_dataframe()

        if "bad_channel" not in electrodes.columns:
            return recording

        bad = electrodes["bad_channel"].fillna(False)
        if bad.dtype == object:
            bad = bad.astype(str).str.strip().str.lower().isin(("1", "true", "yes", "y", "bad"))
        else:
            bad = bad.astype(bool)

        good_electrodes = electrodes.loc[~bad].copy()
        if good_electrodes.empty:
            return None

        recording_channel_ids = list(recording.get_channel_ids())
        by_text = {str(ch): ch for ch in recording_channel_ids}
        good_channels = []

        candidate_columns = [
            "contact_ids",
            "channel_name",
            "channel_id",
            "id",
            "label",
            "electrode_label",
            "group_name",
        ]

        for col in candidate_columns:
            if col not in good_electrodes.columns:
                continue
            matched = []
            for value in good_electrodes[col].dropna().tolist():
                if value in recording_channel_ids:
                    matched.append(value)
                elif str(value) in by_text:
                    matched.append(by_text[str(value)])
            if matched:
                good_channels = list(dict.fromkeys(matched))
                break

        if not good_channels:
            matched = []
            for value in list(good_electrodes.index):
                if value in recording_channel_ids:
                    matched.append(value)
                elif str(value) in by_text:
                    matched.append(by_text[str(value)])
            if matched:
                good_channels = list(dict.fromkeys(matched))

        if not good_channels:
            return recording

        return recording.select_channels(good_channels)

    @classmethod
    def _build_recording_from_recipe_item(cls, item: dict, recipe: dict, progress=None):
        def log(message: str):
            if progress is not None:
                try:
                    progress(str(message))
                except Exception:
                    pass

        files = [str(p) for p in (item.get("files", []) or []) if str(p).strip()]
        if not files:
            raise FileNotFoundError(f"No NWB file listed for recording {item.get('label')}")

        data = dict(recipe.get("data", {}) or {})
        data_path = str(data.get("path", "")).strip()
        if not data_path:
            raise RuntimeError("sorting_recipe.json is missing data.path")

        remove_bad_channels = bool(recipe.get("remove_bad_channels", False))
        bad_policy = str(recipe.get("bad_channel_mismatch_policy", "per_file") or "per_file").strip().lower()
        recordings = []
        bad_channel_metas = []
        skipped = []

        for i, file_path in enumerate(files, start=1):
            log(f"Reading NWB {i}/{len(files)}: {Path(file_path).name}")
            rec = cls._read_nwb_recording(file_path=file_path, electrical_series_path=data_path)
            if remove_bad_channels:
                log(f"Reading bad-channel metadata from: {Path(file_path).name}")
                meta = cls._bad_channel_metadata_from_nwb(file_path, rec.get_channel_ids())
                bad_channel_metas.append(meta)

                if len(files) > 1 and bad_policy in {"good_in_all_files", "good_in_any_file"}:
                    # Defer channel selection until all recordings are loaded so
                    # every concatenated chunk receives the exact same channel set.
                    pass
                else:
                    log(f"Removing bad channels from: {Path(file_path).name}")
                    rec = cls._select_good_channels_from_metadata(rec, meta)
                    try:
                        if len(rec.get_channel_ids()) <= 0:
                            rec = None
                    except Exception:
                        pass
                    if rec is None:
                        log(f"Skipped, no good channels left: {Path(file_path).name}")
                        skipped.append(file_path)
                        continue
            recordings.append(rec)

        if not recordings:
            raise RuntimeError(
                "No valid recording left after loading/filtering.\n"
                f"Skipped files: {skipped}"
            )

        if remove_bad_channels and len(recordings) > 1 and bad_policy in {"good_in_all_files", "good_in_any_file"}:
            recordings = cls._select_common_channels_for_concatenation(recordings, bad_channel_metas, bad_policy, log)

        if len(recordings) > 1:
            log(f"Concatenating {len(recordings)} recording chunks/files...")

        return cls._concatenate_recordings(recordings)

    @staticmethod
    def _recording_info(recording) -> str:
        try:
            fs = float(recording.get_sampling_frequency())
        except Exception:
            fs = float("nan")
        try:
            n_frames = int(recording.get_num_frames(segment_index=0))
        except Exception:
            try:
                n_frames = int(recording.get_num_frames())
            except Exception:
                n_frames = 0
        try:
            n_channels = len(recording.get_channel_ids())
        except Exception:
            n_channels = 0
        dur = n_frames / fs if fs and math.isfinite(fs) and fs > 0 else 0.0
        return f"{n_channels} channel(s), {fs:g} Hz, {dur:.2f} s"


# -----------------------------
# channel/probe dialogs
# -----------------------------

class _ChannelSelectorDialog(QtWidgets.QDialog):
    def __init__(self, parent, channel_ids, selected_ids):
        super().__init__(parent)
        self.setWindowTitle("Select channels")
        self.resize(360, 560)
        self.channel_ids = list(channel_ids or [])
        self.selected_ids = set(selected_ids or [])
        self.checks = []
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        all_btn = QtWidgets.QPushButton("All", self)
        none_btn = QtWidgets.QPushButton("None", self)
        invert_btn = QtWidgets.QPushButton("Invert", self)
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        invert_btn.clicked.connect(self._invert)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addWidget(invert_btn)
        outer.addLayout(row)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget(scroll)
        form = QtWidgets.QVBoxLayout(body)
        form.setContentsMargins(4, 4, 4, 4)
        form.setSpacing(2)
        for ch in self.channel_ids:
            cb = QtWidgets.QCheckBox(str(ch), body)
            cb.setChecked(ch in self.selected_ids or str(ch) in {str(x) for x in self.selected_ids})
            cb._channel_id = ch
            self.checks.append(cb)
            form.addWidget(cb)
        form.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _set_all(self, value: bool):
        for cb in self.checks:
            cb.setChecked(bool(value))

    def _invert(self):
        for cb in self.checks:
            cb.setChecked(not cb.isChecked())

    def selected_channel_ids(self):
        return [cb._channel_id for cb in self.checks if cb.isChecked()]


class _ProbeDialog(QtWidgets.QDialog):
    def __init__(self, parent, recording, selected_ids=None):
        super().__init__(parent)
        self.setWindowTitle("Probe geometry")
        self.resize(620, 720)
        self.recording = recording
        self.selected_ids = {str(x) for x in (selected_ids or [])}
        self._build()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        if pg is None:
            layout.addWidget(QtWidgets.QLabel(f"PyQtGraph is not available:\n{_PYQTGRAPH_IMPORT_ERROR}"))
            return

        self.plot = pg.PlotWidget(self)
        self.plot.setBackground("w")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setAspectLocked(True)
        self.plot.getPlotItem().hideButtons()
        self.plot.setMenuEnabled(False)
        layout.addWidget(self.plot, stretch=1)

        try:
            locs = np.asarray(self.recording.get_channel_locations(), dtype=float)
            channel_ids = list(self.recording.get_channel_ids())
        except Exception as exc:
            layout.addWidget(QtWidgets.QLabel(f"No channel locations available:\n{exc}"))
            return

        if locs.ndim != 2 or locs.shape[0] == 0 or locs.shape[1] < 2:
            layout.addWidget(QtWidgets.QLabel("No 2D channel locations available."))
            return

        x = locs[:, 0]
        y = locs[:, 1]
        selected_mask = np.array([str(ch) in self.selected_ids for ch in channel_ids], dtype=bool)

        pen = pg.mkPen("k", width=1.5)
        normal_brush = pg.mkBrush(60, 130, 230, 210)
        selected_brush = pg.mkBrush(240, 70, 70, 230)
        self.plot.plot(x[~selected_mask], y[~selected_mask], pen=None, symbol="o", symbolSize=14, symbolBrush=normal_brush, symbolPen=pen)
        if selected_mask.any():
            self.plot.plot(x[selected_mask], y[selected_mask], pen=None, symbol="o", symbolSize=22, symbolBrush=selected_brush, symbolPen=pg.mkPen("k", width=2.5))

        for xi, yi, ch in zip(x, y, channel_ids):
            txt = pg.TextItem(str(ch), color=(20, 20, 20), anchor=(0.5, -0.35))
            try:
                font = txt.textItem.font()
                font.setPointSize(9)
                font.setBold(True)
                txt.textItem.setFont(font)
            except Exception:
                pass
            txt.setPos(float(xi), float(yi))
            self.plot.addItem(txt)

        self.plot.autoRange()




class _LogWriter:
    def __init__(self, owner):
        self.owner = owner
        self._buffer = ""

    def write(self, text):
        text = str(text or "")
        if not text:
            return
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            try:
                self.owner._append_log(line)
            except Exception:
                pass

    def flush(self):
        if self._buffer:
            try:
                self.owner._append_log(self._buffer)
            except Exception:
                pass
            self._buffer = ""


class _SignalLogWriter:
    def __init__(self, emit_func):
        self.emit_func = emit_func
        self._buffer = ""

    def _emit(self, text):
        try:
            self.emit_func(str(text))
        except Exception:
            pass

    def _emit_complete_lines(self):
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)

    def write(self, text):
        text = str(text or "")
        if not text:
            return

        # Progress bars such as tqdm often redraw one terminal line using \r.
        # Send these as replacement lines so the Qt log updates the last line
        # instead of appending one new line at every progress tick.
        if "\r" in text:
            parts = text.split("\r")
            if parts[0]:
                self._buffer += parts[0]
                self._emit_complete_lines()
            for part in parts[1:]:
                if self._buffer:
                    self._emit("\r" + self._buffer)
                    self._buffer = ""
                if not part:
                    continue
                if "\n" in part:
                    first, rest = part.split("\n", 1)
                    if first:
                        self._emit("\r" + first)
                    self._buffer += rest
                    self._emit_complete_lines()
                else:
                    self._emit("\r" + part)
            return

        self._buffer += text
        self._emit_complete_lines()

    def flush(self):
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""


class _SorterParamsDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent,
        sorter_name: str,
        params: dict,
        param_descriptions: Optional[dict] = None,
        accept_text: str = "Apply parameters",
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Edit sorter parameters - {sorter_name}")
        self.resize(860, 680)
        self.params = dict(params or {})
        self.param_descriptions = {str(k): str(v) for k, v in (param_descriptions or {}).items()}
        self.accept_text = str(accept_text or "Apply parameters")
        self.editors = {}
        self.extra_name_edit = None
        self.extra_value_edit = None
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("Sorter parameters", self)
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        outer.addWidget(title)

        note = QtWidgets.QLabel(
            "Each value below is editable. Numbers, true/false, lists and dictionaries are parsed automatically "
            "when you confirm the dialog. Example: 10, 0.5, true, [1, 2], {\"key\": \"value\"}."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #333;")
        outer.addWidget(note)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame if hasattr(QtWidgets.QFrame, "Shape") else QtWidgets.QFrame.NoFrame)
        holder = QtWidgets.QWidget(scroll)
        form = QtWidgets.QFormLayout(holder)
        form.setContentsMargins(4, 4, 12, 4)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow if hasattr(QtWidgets.QFormLayout, "FieldGrowthPolicy") else QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        for key, value in sorted(self.params.items(), key=lambda kv: str(kv[0]).lower()):
            name = QtWidgets.QLabel(str(key), holder)
            name.setMinimumWidth(220)
            name.setStyleSheet("font-weight: 600;")
            tooltip = self.param_descriptions.get(str(key), "")
            if tooltip:
                name.setToolTip(tooltip)
            edit = QtWidgets.QLineEdit(holder)
            edit.setText(self._value_to_text(value))
            edit.setMinimumHeight(28)
            edit.setPlaceholderText("Edit value")
            if tooltip:
                edit.setToolTip(tooltip)
            else:
                edit.setToolTip(f"Parameter: {key}")
            self.editors[str(key)] = edit
            form.addRow(name, edit)

        scroll.setWidget(holder)
        outer.addWidget(scroll, stretch=1)

        self.setStyleSheet(
            "QLineEdit { background: white; color: black; border: 1px solid #9aa7b1; border-radius: 4px; padding: 4px 6px; }"
            "QLineEdit:focus { border: 2px solid #2b7cff; }"
            "QGroupBox { font-weight: 600; }"
        )

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText(self.accept_text)
        except Exception:
            pass
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _value_to_text(value):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    @staticmethod
    def _parse_value(text):
        text = str(text).strip()
        if text == "":
            return ""
        try:
            return json.loads(text)
        except Exception:
            pass
        low = text.lower()
        if low in {"true", "yes", "y"}:
            return True
        if low in {"false", "no", "n"}:
            return False
        if low in {"none", "null"}:
            return None
        try:
            if any(ch in text for ch in [".", "e", "E"]):
                return float(text)
            return int(text)
        except Exception:
            return text

    def get_params(self):
        out = {}
        for key, edit in self.editors.items():
            if not key:
                continue
            out[key] = self._parse_value(edit.text())
        return out


class _SummaryPlotEventRow(QtWidgets.QFrame):
    removed = QtCore.Signal(object)

    def __init__(self, parent, *, options: list[str], event_config: Optional[dict] = None):
        super().__init__(parent)
        self.options = [str(x) for x in (options or []) if str(x).strip()]
        self.event_config = dict(event_config or {})
        self._color = self._clean_color(self.event_config.get("color", "#d62728"))
        self._alpha = self._clean_alpha(self.event_config.get("alpha", 0.18))
        self._build()

    @staticmethod
    def _clean_color(value):
        text = str(value or "").strip()
        if not text:
            return "#d62728"
        color = QtGui.QColor(text)
        if color.isValid():
            return color.name()
        return "#d62728"

    @staticmethod
    def _clean_alpha(value):
        try:
            out = float(value)
            if math.isfinite(out):
                return max(0.0, min(1.0, out))
        except Exception:
            pass
        return 0.18

    def _build(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.event_combo = QtWidgets.QComboBox(self)
        self.event_combo.addItems(self.options)
        marker = str(self.event_config.get("event_marker", "") or "").strip()
        if marker and self.event_combo.findText(marker) < 0:
            self.event_combo.addItem(marker)
        if marker:
            self.event_combo.setCurrentText(marker)
        elif self.options:
            self.event_combo.setCurrentIndex(0)
        self.event_combo.setMinimumWidth(170)

        self.display_combo = QtWidgets.QComboBox(self)
        self.display_combo.addItems(["duration", "start", "stop", "center"])
        display = str(self.event_config.get("display", self.event_config.get("edge", "duration")) or "duration").strip().lower()
        if display not in {"duration", "start", "stop", "center"}:
            display = "duration"
        self.display_combo.setCurrentText(display)
        self.display_combo.setToolTip(
            "duration draws an axvspan from start_time to stop_time. "
            "start/stop/center draw vertical lines. Timestamp-only events are drawn as lines."
        )

        self.color_btn = QtWidgets.QPushButton("Color", self)
        self.color_btn.setMinimumWidth(72)
        self.color_btn.clicked.connect(self._choose_color)
        self._update_color_button()

        self.alpha_spin = QtWidgets.QDoubleSpinBox(self)
        self.alpha_spin.setRange(0.0, 1.0)
        self.alpha_spin.setDecimals(2)
        self.alpha_spin.setSingleStep(0.05)
        self.alpha_spin.setValue(float(self._alpha))
        self.alpha_spin.setMinimumWidth(72)
        self.alpha_spin.setToolTip("Transparency of this event overlay. 0 is invisible, 1 is opaque.")

        self.remove_btn = QtWidgets.QPushButton("Remove", self)
        self.remove_btn.setObjectName("secondaryActionButton")
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self))

        layout.addWidget(QtWidgets.QLabel("Event:", self))
        layout.addWidget(self.event_combo, stretch=1)
        layout.addWidget(QtWidgets.QLabel("Display:", self))
        layout.addWidget(self.display_combo)
        layout.addWidget(self.color_btn)
        layout.addWidget(QtWidgets.QLabel("Alpha:", self))
        layout.addWidget(self.alpha_spin)
        layout.addWidget(self.remove_btn)

    def _update_color_button(self):
        self.color_btn.setStyleSheet(
            f"QPushButton {{ background: {self._color}; color: white; border: 1px solid #444; border-radius: 4px; padding: 4px 8px; }}"
        )
        self.color_btn.setText(self._color)

    def _choose_color(self):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self._color), self, "Choose event color")
        if color.isValid():
            self._color = color.name()
            self._update_color_button()

    def get_config(self):
        marker = str(self.event_combo.currentText() or "").strip()
        display = str(self.display_combo.currentText() or "duration").strip().lower()
        if display not in {"duration", "start", "stop", "center"}:
            display = "duration"
        return {
            "enabled": True,
            "event_marker": marker,
            "display": display,
            "color": self._clean_color(self._color),
            "alpha": self._clean_alpha(self.alpha_spin.value()),
        }


class _UnitSummaryParamsDialog(QtWidgets.QDialog):
    def __init__(self, parent, *, options: list[str], config: dict, default_config: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Unit summary parameters")
        self.resize(680, 460)
        self.options = [str(x) for x in (options or []) if str(x).strip()]
        self.config = dict(config or {})
        self.default_config = dict(default_config or {})
        self.plot_event_rows = []
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("Unit summary parameters", self)
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        outer.addWidget(title)

        note = QtWidgets.QLabel(
            "Alignment defines the main time reference of the unit-summary plots. "
            "Plot events are optional markers drawn inside the summary plots.",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #333;")
        outer.addWidget(note)

        alignment_box = QtWidgets.QGroupBox("Main alignment", self)
        alignment_layout = QtWidgets.QFormLayout(alignment_box)
        alignment_layout.setHorizontalSpacing(12)
        alignment_layout.setVerticalSpacing(8)
        alignment_layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow if hasattr(QtWidgets.QFormLayout, "FieldGrowthPolicy") else QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        self.event_combo = QtWidgets.QComboBox(alignment_box)
        self.event_combo.addItem("")
        self.event_combo.addItems(self.options)
        marker = str(self.config.get("event_marker", "") or "").strip()
        if marker and self.event_combo.findText(marker) < 0:
            self.event_combo.addItem(marker)
        if marker:
            self.event_combo.setCurrentText(marker)

        self.edge_combo = QtWidgets.QComboBox(alignment_box)
        self.edge_combo.addItems(["start", "stop", "center"])
        edge = str(self.config.get("edge", "start") or "start").strip().lower()
        if self.edge_combo.findText(edge) >= 0:
            self.edge_combo.setCurrentText(edge)

        self.before_spin = QtWidgets.QDoubleSpinBox(alignment_box)
        self.before_spin.setRange(0.0, 3600.0)
        self.before_spin.setDecimals(3)
        self.before_spin.setSingleStep(0.1)
        self.before_spin.setValue(self._safe_float(self.config.get("before_s", 0.0), 0.0))

        self.after_spin = QtWidgets.QDoubleSpinBox(alignment_box)
        self.after_spin.setRange(0.0, 3600.0)
        self.after_spin.setDecimals(3)
        self.after_spin.setSingleStep(0.1)
        self.after_spin.setValue(self._safe_float(self.config.get("after_s", 2.0), 2.0))

        alignment_layout.addRow("Align to:", self.event_combo)
        alignment_layout.addRow("Edge:", self.edge_combo)
        alignment_layout.addRow("Before (s):", self.before_spin)
        alignment_layout.addRow("After (s):", self.after_spin)
        outer.addWidget(alignment_box)

        plot_events_box = QtWidgets.QGroupBox("Events displayed inside the plot", self)
        plot_events_outer = QtWidgets.QVBoxLayout(plot_events_box)
        plot_events_outer.setContentsMargins(10, 10, 10, 10)
        plot_events_outer.setSpacing(8)

        plot_events_note = QtWidgets.QLabel(
            "Add event markers here to overlay them on the unit-summary plots. Interval events can be shown as full-duration axvspan regions; point/timestamp events are shown as vertical lines.",
            plot_events_box,
        )
        plot_events_note.setWordWrap(True)
        plot_events_note.setStyleSheet("color: #555;")
        plot_events_outer.addWidget(plot_events_note)

        self.plot_events_container = QtWidgets.QWidget(plot_events_box)
        self.plot_events_layout = QtWidgets.QVBoxLayout(self.plot_events_container)
        self.plot_events_layout.setContentsMargins(0, 0, 0, 0)
        self.plot_events_layout.setSpacing(6)
        plot_events_outer.addWidget(self.plot_events_container)

        add_row = QtWidgets.QHBoxLayout()
        self.add_plot_event_btn = QtWidgets.QPushButton("Add event", plot_events_box)
        self.add_plot_event_btn.setObjectName("secondaryActionButton")
        self.add_plot_event_btn.clicked.connect(lambda: self._add_plot_event_row())
        add_row.addWidget(self.add_plot_event_btn)
        add_row.addStretch(1)
        plot_events_outer.addLayout(add_row)
        outer.addWidget(plot_events_box, stretch=1)

        saved_plot_events = self.config.get("plot_events", [])
        if not isinstance(saved_plot_events, list):
            saved_plot_events = []
        for event_config in saved_plot_events:
            if isinstance(event_config, dict):
                self._add_plot_event_row(event_config)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Apply parameters")
        except Exception:
            pass
        try:
            reset_btn = buttons.addButton("Use defaults", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole)
            reset_btn.clicked.connect(self._apply_defaults)
        except Exception:
            reset_btn = None
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self.setStyleSheet(
            "QComboBox, QDoubleSpinBox { background: white; color: black; border: 1px solid #9aa7b1; border-radius: 4px; padding: 4px 6px; }"
            "QComboBox:focus, QDoubleSpinBox:focus { border: 2px solid #2b7cff; }"
            "QGroupBox { font-weight: 700; border: 1px solid #c9d2da; border-radius: 6px; margin-top: 8px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )

    @staticmethod
    def _safe_float(value, default):
        try:
            out = float(value)
            if math.isfinite(out):
                return out
        except Exception:
            pass
        return float(default)

    def _add_plot_event_row(self, event_config: Optional[dict] = None):
        row = _SummaryPlotEventRow(self.plot_events_container, options=self.options, event_config=event_config)
        row.removed.connect(self._remove_plot_event_row)
        self.plot_event_rows.append(row)
        self.plot_events_layout.addWidget(row)
        return row

    def _remove_plot_event_row(self, row):
        try:
            self.plot_event_rows.remove(row)
        except Exception:
            pass
        try:
            self.plot_events_layout.removeWidget(row)
        except Exception:
            pass
        row.hide()
        row.deleteLater()

    def _apply_defaults(self):
        cfg = dict(self.default_config or {})
        marker = str(cfg.get("event_marker", "") or "").strip()
        if marker and self.event_combo.findText(marker) < 0:
            self.event_combo.addItem(marker)
        self.event_combo.setCurrentText(marker)
        edge = str(cfg.get("edge", "start") or "start").strip().lower()
        if self.edge_combo.findText(edge) >= 0:
            self.edge_combo.setCurrentText(edge)
        self.before_spin.setValue(self._safe_float(cfg.get("before_s", 0.0), 0.0))
        self.after_spin.setValue(self._safe_float(cfg.get("after_s", 2.0), 2.0))

        for row in list(self.plot_event_rows):
            self._remove_plot_event_row(row)
        for event_config in cfg.get("plot_events", []) or []:
            if isinstance(event_config, dict):
                self._add_plot_event_row(event_config)

    def get_config(self):
        edge = str(self.edge_combo.currentText() or "start").strip().lower()
        if edge not in {"start", "stop", "center"}:
            edge = "start"
        plot_events = []
        for row in list(self.plot_event_rows):
            cfg = row.get_config()
            if cfg.get("event_marker"):
                plot_events.append(cfg)
        return {
            "event_marker": str(self.event_combo.currentText() or "").strip(),
            "edge": edge,
            "before_s": max(0.0, float(self.before_spin.value())),
            "after_s": max(0.0, float(self.after_spin.value())),
            "plot_events": plot_events,
        }


class _SortingRunWorker(QtCore.QObject):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(int, int, str, str)
    finished = QtCore.Signal(list)
    failed = QtCore.Signal(str)

    def __init__(self, *, sorting_path, recipe, pipeline, preprocessing_specs, groups, sorter_name, params, existing_policy="cancel", summary_config=None, use_multiprocessing=True, n_jobs=None, skip_failed_sorting=True, configuration_name="", configuration_id=""):
        super().__init__()
        self.sorting_path = Path(sorting_path)
        self.recipe = dict(recipe or {})
        self.pipeline = dict(pipeline or {})
        self.preprocessing_specs = dict(preprocessing_specs or {})
        self.groups = list(groups or [])
        self.sorter_name = str(sorter_name or "").strip()
        self.params = dict(params or {})
        self.configuration_name = str(configuration_name or "").strip()
        self.configuration_id = str(configuration_id or "").strip()
        self.existing_policy = str(existing_policy or "cancel")
        self.summary_config = dict(summary_config or {})
        self.use_multiprocessing = bool(use_multiprocessing)
        self.n_jobs = _available_multiprocessing_jobs(n_jobs) if self.use_multiprocessing else 1
        self.skip_failed_sorting = bool(skip_failed_sorting)
        self.job_kwargs = _spikeinterface_job_kwargs(self.use_multiprocessing, self.n_jobs)
        self._active_output_folder = None
        self.failed_records = []

    def _emit(self, text=""):
        try:
            self.log.emit(str(text))
        except Exception:
            pass

    def _emit_progress(self, current, total, label, state="running"):
        try:
            self.progress.emit(int(current), int(total), str(label or ""), str(state or "running"))
        except Exception:
            pass

    def run(self):
        try:
            summary_paths = []
            failed_records = []
            with contextlib.redirect_stdout(_SignalLogWriter(self._emit)), contextlib.redirect_stderr(_SignalLogWriter(self._emit)):
                self._emit(f"Sorter: {self.sorter_name}")
                self._emit(f"Recording groups: {len(self.groups)}")
                self._emit(f"Parameters: {self.params}")
                self._emit(f"Unit summary: {self.summary_config}")
                if self.use_multiprocessing:
                    self._emit(f"Multiprocessing: enabled where supported (n_jobs={self.job_kwargs.get('n_jobs', 1)})")
                else:
                    self._emit("Multiprocessing: disabled")
                if self.skip_failed_sorting:
                    self._emit("Skip failed sorting: enabled")
                else:
                    self._emit("Skip failed sorting: disabled")
                self._emit("")

                total_groups = len(self.groups)
                for i, group in enumerate(self.groups, start=1):
                    label = str(group.get("label", f"recording_{i}")).strip() or f"recording_{i}"
                    output_folder = None
                    recording = None
                    sorting = None
                    sorting_analyzer = None
                    self._active_output_folder = None
                    self._emit_progress(i, total_groups, label, "running")

                    try:
                        base_folder = _sorting_runs_root(self.sorting_path)
                        run_signature = _build_sorting_run_signature(
                            label=label,
                            group=group,
                            recipe=self.recipe,
                            pipeline=self.pipeline,
                            sorter_name=self.sorter_name,
                            sorter_params=self.params,
                            summary_config=self.summary_config,
                        )
                        run_hash = _signature_hash(run_signature)
                        matching_runs = _find_matching_sorting_runs(base_folder, run_hash)
                        if matching_runs:
                            if self.existing_policy == "skip":
                                skipped_folder = matching_runs[0]
                                self._emit(f"Skipping identical existing run: {skipped_folder}")
                                existing_summary = None
                                for old_folder in matching_runs:
                                    candidate = Path(old_folder) / "sorting_run.json"
                                    if candidate.exists():
                                        existing_summary = candidate
                                        break
                                if existing_summary is not None:
                                    existing_summary_text = str(existing_summary)
                                    if existing_summary_text not in summary_paths:
                                        summary_paths.append(existing_summary_text)
                                    self._emit(f"Using existing unit-summary viewer source: {existing_summary}")
                                else:
                                    self._emit("No completed sorting_run.json was found for the skipped run, so no summary image can be loaded for it.")
                                self._emit_progress(i, total_groups, label, "skipped")
                                continue
                            if self.existing_policy == "delete":
                                for old_folder in matching_runs:
                                    self._emit(f"Deleting identical existing run: {old_folder}")
                                    shutil.rmtree(old_folder, ignore_errors=True)
                            else:
                                raise RuntimeError(f"An identical sorting run already exists: {matching_runs[0]}")

                        output_folder = _unique_sorting_run_folder(base_folder, run_hash)
                        output_folder.mkdir(parents=True, exist_ok=False)
                        self._active_output_folder = output_folder
                        self._write_run_config_snapshot(
                            label=label,
                            group=group,
                            output_folder=output_folder,
                            run_signature=run_signature,
                            run_signature_hash=run_hash,
                        )

                        self._emit("=" * 80)
                        self._emit(f"Recording {i}/{len(self.groups)}: {label}")
                        self._emit(f"Run hash: {run_hash[:12]}")
                        self._emit(f"Output: {output_folder}")
                        self._emit("=" * 80)

                        self._emit("Loading recording from sorting_recipe.json...")
                        recording = _RecordingLoadWorker._build_recording_from_recipe_item(group, self.recipe, progress=self._emit)
                        self._write_sorting_time_map_for_group(group=group, output_folder=output_folder)
                        self._emit("Applying saved preprocessing pipeline...")
                        recording = self._apply_pipeline(recording)
                        self._emit(_RecordingLoadWorker._recording_info(recording))

                        self._emit("Running sorter...")
                        sorting = self._run_sorter(recording=recording, output_folder=output_folder)

                        self._emit("Creating SortingAnalyzer...")
                        sorting_analyzer, analyzer_folder = self._create_sorting_analyzer(sorting=sorting, recording=recording, output_folder=output_folder)

                        sorting_summary_folder = None
                        summary_event_map_path = None
                        if self.summary_config.get("enabled", True):
                            self._emit("Creating unit summary figures...")
                            sorting_summary_folder, summary_event_map_path = self._try_plot_sorting_summary(
                                sorting_analyzer=sorting_analyzer,
                                output_folder=output_folder,
                                group=group,
                            )
                        else:
                            self._emit("Unit summary generation is disabled.")

                        run_summary = self._write_run_summary(
                            label=label,
                            group=group,
                            output_folder=output_folder,
                            analyzer_folder=analyzer_folder,
                            sorting_summary_folder=sorting_summary_folder,
                            summary_event_map_path=summary_event_map_path,
                            run_signature=run_signature,
                            run_signature_hash=run_hash,
                            sorting_analyzer=sorting_analyzer,
                        )
                        summary_paths.append(str(run_summary))
                        try:
                            self._update_run_config_status(output_folder, "finished", summary_path=str(run_summary))
                        except Exception:
                            pass
                        self._emit(f"Finished: {label}")
                        self._emit_progress(i, total_groups, label, "finished")
                        self._emit(f"Run summary: {run_summary}")
                        self._emit("")

                    except Exception:
                        error_text = traceback.format_exc()
                        failed_records.append((label, error_text))
                        self._emit("")
                        self._emit("ERROR: Sorting failed for recording: " + label)
                        self._emit(error_text)
                        try:
                            self._update_run_config_status(output_folder, "failed", error_text=error_text)
                        except Exception:
                            pass
                        self._emit_progress(i, total_groups, label, "failed")

                        if not self.skip_failed_sorting:
                            raise

                        self._emit("Skip failed sorting is enabled. Continuing with the next recording.")
                        self._emit("")

                    finally:
                        try:
                            del recording, sorting, sorting_analyzer
                        except Exception:
                            pass

                if failed_records:
                    self._emit("")
                    self._emit(f"Finished with {len(failed_records)} failed recording(s).")
                    for failed_label, _error in failed_records:
                        self._emit(f"Failed recording: {failed_label}")

            self.failed_records = list(failed_records)
            self.finished.emit(summary_paths)
        except Exception:
            error_text = traceback.format_exc()
            self.failed_records = list(locals().get("failed_records", []) or [])
            try:
                self._update_run_config_status(getattr(self, "_active_output_folder", None), "failed", error_text=error_text)
            except Exception:
                pass
            self.failed.emit(error_text)

    def _apply_pipeline(self, recording):
        enabled = [s for s in (self.pipeline.get("enabled_pipeline", []) or []) if isinstance(s, dict)]
        if not enabled:
            self._emit("No preprocessing step enabled.")
            return recording
        by_name = {}
        for name, spec in (self.preprocessing_specs or {}).items():
            if not isinstance(spec, dict):
                continue
            by_name[str(spec.get("name", name)).strip()] = spec
            by_name[str(name).strip()] = spec
        out = recording
        for step in enabled:
            name = str(step.get("name", "")).strip()
            spec = by_name.get(name)
            func = None if spec is None else spec.get("func")
            params = dict(step.get("params", {}) or {})
            if not callable(func):
                self._emit(f"Skipping preprocessing step because function was not found: {name}")
                continue
            self._emit(f"Applying preprocessing: {name}")
            self._emit(f"Parameters: {params}")
            res = func(out, **params)
            out = res[0] if isinstance(res, tuple) else res
        self._emit("Preprocessing applied.")
        return out

    def _is_sorter_installed_locally(self):
        return _check_sorter_installed_locally(self.sorter_name, emit_func=self._emit)

    def _run_sorter(self, *, recording, output_folder: Path):
        output_folder = Path(output_folder)
        sorter_run_folder = _sorter_work_folder(output_folder)
        if sorter_run_folder.exists():
            shutil.rmtree(sorter_run_folder)

        try:
            from spikeinterface.sorters import run_sorter
        except Exception:
            import spikeinterface.sorters as ss
            run_sorter = getattr(ss, "run_sorter", None)
        if not callable(run_sorter):
            raise RuntimeError("spikeinterface.sorters.run_sorter is not available.")

        params = dict(self.params or {})
        for key in ["docker_image", "singularity_image", "folder", "output_folder", "verbose"]:
            params.pop(key, None)
        params, worker_keys = _apply_worker_count_to_params(params, enabled=self.use_multiprocessing, n_jobs=self.n_jobs)
        if self.use_multiprocessing and worker_keys:
            self._emit(f"Sorter worker-count parameter(s) set to {self.n_jobs}: {', '.join(worker_keys)}")
        elif self.use_multiprocessing:
            self._emit("Sorter has no generic worker-count parameter to update; running sorter with its own parallel settings.")

        installed = bool(self._is_sorter_installed_locally())
        docker_image = False if installed else True
        if installed:
            self._emit(f"Sorter execution: local installation detected for {self.sorter_name}; running without Docker")
        else:
            self._emit(f"Sorter execution: {self.sorter_name} not installed locally; running with Docker")

        # A lazy preprocessed recording is serialized as the complete chain of
        # SpikeInterface preprocessing classes.  Reconstructing that chain in
        # a sorter container is fragile when the host and container use
        # different SpikeInterface versions (for example, one version may add
        # a FilterRecording keyword that the other does not know).  Give the
        # container a plain binary recording instead.  The folder is temporary
        # and is removed after the sorter has finished, so queued parameter
        # sets do not leave duplicate copies of the preprocessed traces.
        recording_for_sorter = recording
        container_recording_folder = None
        if not installed:
            container_recording_folder = output_folder / "docker_preprocessed_recording"
            if container_recording_folder.exists():
                shutil.rmtree(container_recording_folder)
            self._emit("Materializing the preprocessed recording for Docker...")
            save_kwargs = dict(self.job_kwargs or {})
            save_kwargs["progress_bar"] = True
            try:
                recording_for_sorter = recording.save(
                    folder=str(container_recording_folder),
                    format="binary",
                    overwrite=True,
                    **save_kwargs,
                )
            except Exception:
                # A disk-space or preprocessing failure can leave a partial
                # binary folder.  It must not be mistaken for a reusable input
                # on the next attempt.
                shutil.rmtree(container_recording_folder, ignore_errors=True)
                raise
            self._emit(f"Docker recording prepared: {container_recording_folder}")

        self._emit(f"Docker image argument: {docker_image}")
        self._emit(f"SpikeInterface work folder: {sorter_run_folder}")

        sorting = None
        try:
            with _suppress_gui_autolaunch_for_child_processes():
                try:
                    sorting = run_sorter(
                        sorter_name=self.sorter_name,
                        recording=recording_for_sorter,
                        docker_image=docker_image,
                        folder=str(sorter_run_folder),
                        verbose=True,
                        **params,
                    )
                except TypeError as exc:
                    # SpikeInterface renamed output_folder to folder.  Retry
                    # only for that API mismatch; a TypeError raised by the
                    # sorter or its preprocessing must remain visible.
                    message = str(exc)
                    folder_argument_error = (
                        "unexpected keyword argument 'folder'" in message
                        or 'unexpected keyword argument "folder"' in message
                    )
                    if not folder_argument_error:
                        raise
                    sorting = run_sorter(
                        sorter_name=self.sorter_name,
                        recording=recording_for_sorter,
                        docker_image=docker_image,
                        output_folder=str(sorter_run_folder),
                        verbose=True,
                        **params,
                    )

            if recording_for_sorter is not recording:
                register_recording = getattr(sorting, "register_recording", None)
                if callable(register_recording):
                    try:
                        register_recording(recording)
                    except TypeError:
                        register_recording(recording, check_spike_frames=True)
            return sorting
        finally:
            if container_recording_folder is not None:
                # Drop the BinaryFolderRecording reference before removing its
                # files.  If Windows still holds a file briefly, keep the run
                # valid and report the leftover folder instead of masking the
                # sorter result with a cleanup exception.
                recording_for_sorter = None
                try:
                    shutil.rmtree(container_recording_folder)
                    self._emit("Removed temporary Docker recording.")
                except Exception as exc:
                    self._emit(
                        "Warning: could not remove the temporary Docker recording "
                        f"{container_recording_folder}: {exc}"
                    )

    def _create_sorting_analyzer(self, *, sorting, recording, output_folder: Path):
        analyzer_folder = Path(output_folder) / "SorterOutput"
        if analyzer_folder.exists():
            shutil.rmtree(analyzer_folder)
        try:
            from spikeinterface.core import create_sorting_analyzer
        except Exception:
            from spikeinterface.full import create_sorting_analyzer
        analyzer_kwargs = dict(
            sorting=sorting,
            recording=recording,
            format="binary_folder",
            return_scaled=True,
            sparse=True,
            overwrite=True,
            folder=str(analyzer_folder),
            ms_before=2,
            ms_after=5,
        )
        job_kwargs = _spikeinterface_job_kwargs(self.use_multiprocessing, self.n_jobs)
        if job_kwargs:
            self._emit(f"Creating SortingAnalyzer with multiprocessing where supported: {job_kwargs}")
        try:
            sorting_analyzer = create_sorting_analyzer(**analyzer_kwargs, **job_kwargs)
        except TypeError:
            if job_kwargs:
                self._emit("create_sorting_analyzer() does not accept n_jobs in this SpikeInterface version; retrying without it.")
            sorting_analyzer = create_sorting_analyzer(**analyzer_kwargs)
        self._emit(f"SorterOutput folder: {analyzer_folder}")
        return sorting_analyzer, analyzer_folder

    def _try_plot_sorting_summary(self, *, sorting_analyzer, output_folder: Path, group: dict):
        summary_folder = Path(output_folder) / "sorting_summary"
        summary_folder.mkdir(parents=True, exist_ok=True)

        event_frames = None
        map_path = None
        marker = str(self.summary_config.get("event_marker", "") or "").strip()
        if marker:
            try:
                event_frames, map_path = self._map_event_frames_for_group(group=group, output_folder=output_folder)
                self._emit(f"Mapped {len(event_frames)} summary alignment event(s) to recording frames.")
                if map_path:
                    self._emit(f"Summary alignment event map: {map_path}")
            except Exception:
                self._emit("Could not map summary alignment events; summary will be created without event alignment.")
                self._emit(traceback.format_exc())
                event_frames = None

        plot_events = self._map_summary_plot_events_for_group(group=group, output_folder=output_folder)

        try:
            from Tools.General.plotting.plot_unit_summary import plot_sorting_summary
        except Exception as exc:
            self._emit(f"Could not import plot_sorting_summary: {exc}")
            return str(summary_folder), map_path

        kwargs = {
            "save_folder": str(summary_folder),
            "event_frames": event_frames,
            "pad_before_s": float(self.summary_config.get("before_s", 0.0) if self.summary_config.get("before_s", None) is not None else 0.0),
            "pad_after_s": float(self.summary_config.get("after_s", 2.0) if self.summary_config.get("after_s", None) is not None else 2.0),
            "event_label": marker or None,
        }
        if plot_events:
            kwargs["plot_events"] = plot_events
        if self.use_multiprocessing:
            kwargs["job_kwargs"] = dict(self.job_kwargs)
            kwargs["acelerate"] = True
            self._emit(f"Unit summary multiprocessing enabled where supported: {self.job_kwargs}")

        with _suppress_gui_autolaunch_for_child_processes(), _suppress_matplotlib_windows_for_worker():
            try:
                plot_sorting_summary(
                    sorting_analyzer,
                    self.sorter_name,
                    **kwargs,
                )
            except TypeError:
                if plot_events:
                    self._emit("plot_sorting_summary does not accept plot_events yet; summary will be generated without extra plot-event overlays.")
                    kwargs.pop("plot_events", None)
                    try:
                        plot_sorting_summary(
                            sorting_analyzer,
                            self.sorter_name,
                            **kwargs,
                        )
                    except TypeError:
                        # Compatibility with older plot_unit_summary versions.
                        kwargs.pop("job_kwargs", None)
                        kwargs.pop("acelerate", None)
                        plot_sorting_summary(
                            sorting_analyzer,
                            self.sorter_name,
                            save_folder=str(summary_folder),
                        )
                else:
                    # Compatibility with older plot_unit_summary versions.
                    kwargs.pop("job_kwargs", None)
                    kwargs.pop("acelerate", None)
                    plot_sorting_summary(
                        sorting_analyzer,
                        self.sorter_name,
                        save_folder=str(summary_folder),
                    )
        self._emit(f"Unit summary folder: {summary_folder}")
        return str(summary_folder), map_path

    def _map_event_frames_for_group(self, *, group: dict, output_folder: Path):
        marker = str(self.summary_config.get("event_marker", "") or "").strip()
        edge = str(self.summary_config.get("edge", "start") or "start").strip().lower()
        return self._map_event_frames_for_marker(
            group=group,
            output_folder=output_folder,
            marker=marker,
            edge=edge,
            map_filename="summary_event_alignment_map.json",
            label_for_log="summary alignment events",
        )

    def _map_event_frames_for_marker(self, *, group: dict, output_folder: Path, marker: str, edge: str = "start", map_filename: str = "summary_event_alignment_map.json", label_for_log: str = "events"):
        marker = str(marker or "").strip()
        edge = str(edge or "start").strip().lower()
        if edge not in {"start", "stop", "center"}:
            edge = "start"
        if not marker:
            return None, None

        files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
        data_path = str((self.recipe.get("data", {}) or {}).get("path", "") or "").strip()
        if not files or not data_path:
            return np.array([], dtype=np.int64), None

        time_map_recording = self._load_sorting_time_map_for_group(group, output_folder=output_folder)
        if time_map_recording is not None:
            self._emit(f"Mapping {label_for_log} with sorting_time_map.json, so NWB timestamp gaps are handled.")
            return self._map_event_frames_for_group_with_time_map(
                group=group,
                files=files,
                marker=marker,
                edge=edge,
                output_folder=output_folder,
                time_map_recording=time_map_recording,
                map_filename=map_filename,
            )

        self._emit(f"No usable sorting_time_map.json found for this recording; using simple cumulative frame offsets for {label_for_log}.")
        self._emit("WARNING: simple event mapping is not gap-safe if NWB timestamps contain gaps.")
        return self._map_event_frames_for_group_simple(
            group=group,
            files=files,
            data_path=data_path,
            marker=marker,
            edge=edge,
            output_folder=output_folder,
            map_filename=map_filename,
        )

    def _map_summary_plot_events_for_group(self, *, group: dict, output_folder: Path):
        out = []
        plot_event_cfgs = self.summary_config.get("plot_events", [])
        if not isinstance(plot_event_cfgs, list):
            return out
        for idx, event_cfg in enumerate(plot_event_cfgs, start=1):
            if not isinstance(event_cfg, dict) or not bool(event_cfg.get("enabled", True)):
                continue
            marker = str(event_cfg.get("event_marker", "") or "").strip()
            if not marker:
                continue
            display = str(event_cfg.get("display", event_cfg.get("edge", "duration")) or "duration").strip().lower()
            if display not in {"duration", "start", "stop", "center"}:
                display = "duration"
            color = str(event_cfg.get("color", "#d62728") or "#d62728").strip()
            try:
                qcolor = QtGui.QColor(color)
                color = qcolor.name() if qcolor.isValid() else "#d62728"
            except Exception:
                color = "#d62728"
            try:
                alpha = float(event_cfg.get("alpha", 0.18))
                if not math.isfinite(alpha):
                    alpha = 0.18
                alpha = max(0.0, min(1.0, alpha))
            except Exception:
                alpha = 0.18
            safe_name = _safe_folder_name(f"{idx}_{marker}_{display}")
            try:
                if display == "duration":
                    intervals, event_map_path = self._map_event_intervals_for_marker(
                        group=group,
                        output_folder=output_folder,
                        marker=marker,
                        map_filename=f"summary_plot_event_{safe_name}_map.json",
                        label_for_log=f"plot event '{marker}'",
                    )
                    if intervals is not None and np.asarray(intervals).size:
                        intervals = np.asarray(intervals, dtype=np.int64).reshape(-1, 2)
                        self._emit(f"Mapped {len(intervals)} plot event interval(s): {marker} (duration, {color})")
                        out.append({
                            "label": marker,
                            "event_marker": marker,
                            "display": "duration",
                            "color": color,
                            "alpha": alpha,
                            "event_intervals": intervals,
                            "map_path": event_map_path,
                        })
                        continue
                    # Timestamp-only events have no start/stop duration. Fall back to a line at their timestamp.
                    self._emit(f"No duration intervals found for plot event '{marker}'; falling back to start/timestamp lines.")
                    display_for_line = "start"
                else:
                    display_for_line = display

                frames, event_map_path = self._map_event_frames_for_marker(
                    group=group,
                    output_folder=output_folder,
                    marker=marker,
                    edge=display_for_line,
                    map_filename=f"summary_plot_event_{safe_name}_map.json",
                    label_for_log=f"plot event '{marker}'",
                )
                if frames is None:
                    continue
                self._emit(f"Mapped {len(frames)} plot event line(s): {marker} ({display_for_line}, {color})")
                out.append({
                    "label": marker,
                    "event_marker": marker,
                    "display": display_for_line,
                    "color": color,
                    "alpha": alpha,
                    "event_frames": frames,
                    "map_path": event_map_path,
                })
            except Exception:
                self._emit(f"Could not map plot event '{marker}'.")
                self._emit(traceback.format_exc())
        return out

    def _map_event_intervals_for_marker(self, *, group: dict, output_folder: Path, marker: str, map_filename: str = "summary_event_intervals_map.json", label_for_log: str = "event intervals"):
        marker = str(marker or "").strip()
        if not marker:
            return None, None

        files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
        data_path = str((self.recipe.get("data", {}) or {}).get("path", "") or "").strip()
        if not files or not data_path:
            return np.empty((0, 2), dtype=np.int64), None

        time_map_recording = self._load_sorting_time_map_for_group(group, output_folder=output_folder)
        if time_map_recording is not None:
            self._emit(f"Mapping {label_for_log} with sorting_time_map.json, so NWB timestamp gaps are handled.")
            return self._map_event_intervals_for_group_with_time_map(
                group=group,
                files=files,
                marker=marker,
                output_folder=output_folder,
                time_map_recording=time_map_recording,
                map_filename=map_filename,
            )

        self._emit(f"No usable sorting_time_map.json found for this recording; using simple cumulative frame offsets for {label_for_log}.")
        self._emit("WARNING: simple interval mapping is not gap-safe if NWB timestamps contain gaps.")
        return self._map_event_intervals_for_group_simple(
            group=group,
            files=files,
            data_path=data_path,
            marker=marker,
            output_folder=output_folder,
            map_filename=map_filename,
        )

    def _map_event_intervals_for_group_with_time_map(self, *, group: dict, files: list, marker: str, output_folder: Path, time_map_recording: dict, map_filename: str = "summary_event_intervals_map.json"):
        chunks = [c for c in (time_map_recording.get("chunks", []) or []) if isinstance(c, dict)]
        chunks_by_file = {}
        for c in chunks:
            fp = self._norm_path_for_time_map(c.get("file_path", ""))
            if not fp:
                continue
            chunks_by_file.setdefault(fp, []).append(c)
        for fp in chunks_by_file:
            chunks_by_file[fp].sort(key=lambda c: (int(c.get("source_start_frame", 0)), int(c.get("concat_start_frame", 0))))

        interval_rows = []
        event_intervals = []
        for file_path in files:
            norm_fp = self._norm_path_for_time_map(file_path)
            file_chunks = chunks_by_file.get(norm_fp, [])
            if not file_chunks:
                self._emit(f"No time-map chunks found for {Path(file_path).name}; intervals from this file are skipped.")
                continue
            try:
                spans = self._event_spans_from_nwb(file_path=file_path, marker=marker)
                if spans.size == 0:
                    continue
                for start_t, stop_t in np.asarray(spans, dtype=float).reshape(-1, 2):
                    if not np.isfinite(start_t) or not np.isfinite(stop_t) or stop_t <= start_t:
                        continue
                    mapped_start = self._map_one_event_time_to_sorter_frame_with_chunks(float(start_t), file_chunks)
                    mapped_stop = self._map_one_event_time_to_sorter_frame_with_chunks(float(np.nextafter(stop_t, start_t)), file_chunks)
                    if mapped_start is None or mapped_stop is None:
                        continue
                    start_frame, start_chunk, source_start_frame = mapped_start
                    stop_frame, stop_chunk, source_stop_frame = mapped_stop
                    if int(stop_frame) <= int(start_frame):
                        continue
                    event_intervals.append((int(start_frame), int(stop_frame)))
                    interval_rows.append({
                        "file_path": str(file_path),
                        "event_marker": marker,
                        "start_time": float(start_t),
                        "stop_time": float(stop_t),
                        "source_start_frame": int(source_start_frame),
                        "source_stop_frame": int(source_stop_frame),
                        "concat_start_frame": int(start_frame),
                        "concat_stop_frame": int(stop_frame),
                        "start_chunk_index": int(start_chunk.get("chunk_index", -1)),
                        "stop_chunk_index": int(stop_chunk.get("chunk_index", -1)),
                        "mapping_mode": "sorting_time_map",
                    })
            except Exception as exc:
                self._emit(f"Could not map event intervals for {Path(file_path).name} with sorting_time_map.json: {exc}")

        event_intervals = np.asarray(sorted(set((int(a), int(b)) for a, b in event_intervals)), dtype=np.int64).reshape(-1, 2) if event_intervals else np.empty((0, 2), dtype=np.int64)
        map_path = Path(output_folder) / str(map_filename or "summary_event_intervals_map.json")
        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "recording_label": str(group.get("label", "")),
            "event_marker": marker,
            "display": "duration",
            "mapping_mode": "sorting_time_map",
            "gap_safe": True,
            "n_intervals_mapped": int(event_intervals.shape[0]),
            "event_intervals": [[int(a), int(b)] for a, b in event_intervals.tolist()],
            "events": interval_rows,
        }
        with open(map_path, "w", encoding="utf-8") as f:
            _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
        return event_intervals, str(map_path)

    def _map_event_intervals_for_group_simple(self, *, group: dict, files: list, data_path: str, marker: str, output_folder: Path, map_filename: str = "summary_event_intervals_map.json"):
        interval_rows = []
        event_intervals = []
        frame_offset = 0
        for file_path in files:
            try:
                spans = self._event_spans_from_nwb(file_path=file_path, marker=marker)
                n_frames, fs, start_time = self._recording_frame_info_from_nwb(file_path=file_path, electrical_series_path=data_path)
                if spans.size:
                    local_start = np.rint((spans[:, 0] - float(start_time)) * float(fs)).astype(np.int64)
                    local_stop = np.rint((spans[:, 1] - float(start_time)) * float(fs)).astype(np.int64)
                    valid = (local_start >= 0) & (local_start < int(n_frames)) & (local_stop > local_start) & (local_stop <= int(n_frames))
                    for (start_t, stop_t), ls, le in zip(spans[valid], local_start[valid], local_stop[valid]):
                        cs = int(frame_offset + int(ls))
                        ce = int(frame_offset + int(le))
                        if ce <= cs:
                            continue
                        event_intervals.append((cs, ce))
                        interval_rows.append({
                            "file_path": str(file_path),
                            "event_marker": marker,
                            "start_time": float(start_t),
                            "stop_time": float(stop_t),
                            "local_start_frame": int(ls),
                            "local_stop_frame": int(le),
                            "concat_start_frame": cs,
                            "concat_stop_frame": ce,
                            "mapping_mode": "simple_cumulative_frame_offset",
                        })
                frame_offset += int(n_frames)
            except Exception as exc:
                self._emit(f"Could not map event intervals for {Path(file_path).name}: {exc}")
                try:
                    n_frames = int(_RecordingLoadWorker._read_nwb_recording(file_path=file_path, electrical_series_path=data_path).get_num_frames())
                    frame_offset += n_frames
                except Exception:
                    pass

        event_intervals = np.asarray(sorted(set((int(a), int(b)) for a, b in event_intervals)), dtype=np.int64).reshape(-1, 2) if event_intervals else np.empty((0, 2), dtype=np.int64)
        map_path = Path(output_folder) / str(map_filename or "summary_event_intervals_map.json")
        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "recording_label": str(group.get("label", "")),
            "event_marker": marker,
            "display": "duration",
            "mapping_mode": "simple_cumulative_frame_offset",
            "gap_safe": False,
            "n_intervals_mapped": int(event_intervals.shape[0]),
            "event_intervals": [[int(a), int(b)] for a, b in event_intervals.tolist()],
            "events": interval_rows,
        }
        with open(map_path, "w", encoding="utf-8") as f:
            _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
        return event_intervals, str(map_path)

    @staticmethod
    def _norm_path_for_time_map(path):
        try:
            return str(Path(str(path))).replace("\\", "/").lower()
        except Exception:
            return str(path).replace("\\", "/").lower()

    def _load_sorting_time_map_for_group(self, group: dict, output_folder: Path | None = None):
        """Return the time-map recording entry matching this group, or None.

        The sorter run writes sorting_time_map.json into the current run folder.
        Older projects may also have a project-level sorting_time_map.json, so
        this loader checks the run folder first and then falls back to the old
        project-level location.
        """
        candidate_paths = []
        if output_folder is not None:
            candidate_paths.append(Path(output_folder) / "sorting_time_map.json")
        if self.sorting_path is not None:
            candidate_paths.append(Path(self.sorting_path) / "sorting_time_map.json")

        payload = None
        map_path = None
        for candidate in candidate_paths:
            try:
                candidate = Path(candidate)
            except Exception:
                continue
            if not candidate.exists():
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    payload = _pp_json_load(f)
                map_path = candidate
                break
            except Exception as exc:
                self._emit(f"Could not read sorting_time_map.json at {candidate}: {exc}")
        if payload is None:
            return None
        recordings = payload.get("recordings", {}) if isinstance(payload, dict) else {}
        if not isinstance(recordings, dict) or not recordings:
            return None

        label = str(group.get("label", "") or "").strip()
        if label in recordings and isinstance(recordings[label], dict):
            rec = recordings[label]
            if rec.get("chunks"):
                return rec

        # Fallback: find the recording whose source files overlap the recipe group.
        group_files = {self._norm_path_for_time_map(p) for p in (group.get("files", []) or [])}
        best_key = None
        best_score = 0
        for key, rec in recordings.items():
            if not isinstance(rec, dict):
                continue
            source_files = {self._norm_path_for_time_map(p) for p in (rec.get("source_files", []) or [])}
            if not source_files:
                source_files = {self._norm_path_for_time_map(c.get("file_path", "")) for c in (rec.get("chunks", []) or []) if isinstance(c, dict)}
            score = len(group_files & source_files)
            if score > best_score:
                best_key = key
                best_score = score
        if best_key is not None and best_score > 0:
            rec = recordings.get(best_key)
            if isinstance(rec, dict) and rec.get("chunks"):
                return rec

        # Last fallback: only one recording map exists.
        if len(recordings) == 1:
            rec = next(iter(recordings.values()))
            if isinstance(rec, dict) and rec.get("chunks"):
                return rec
        return None

    def _write_sorting_time_map_for_group(self, *, group: dict, output_folder: Path):
        """Build a gap-safe mapping from NWB event times to sorter frames.

        SpikeInterface recordings use compact frame indices, while NWB
        ElectricalSeries timestamps can contain jumps/gaps. If events are mapped
        with a simple ``(time - start_time) * sampling_rate`` formula, every
        event after a timestamp gap is shifted. This run-level map records the
        continuous timestamp chunks of each source NWB file so summary alignment
        events and overlay events can be mapped through the same frame coordinate
        system used by the sorter.
        """
        data_path = str((self.recipe.get("data", {}) or {}).get("path", "") or "").strip()
        files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
        if not data_path or not files:
            return None

        output_folder = Path(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)

        chunks = []
        source_files = []
        frame_offset = 0
        total_gaps = 0
        for file_path in files:
            try:
                file_chunks, n_frames, gap_count = self._time_map_chunks_from_nwb_file(
                    file_path=file_path,
                    electrical_series_path=data_path,
                    concat_frame_offset=frame_offset,
                    first_chunk_index=len(chunks),
                )
                chunks.extend(file_chunks)
                source_files.append(str(file_path))
                frame_offset += int(n_frames)
                total_gaps += int(gap_count)
            except Exception as exc:
                self._emit(f"Could not build sorting time map for {Path(file_path).name}: {exc}")
                try:
                    n_frames = int(_RecordingLoadWorker._read_nwb_recording(file_path=file_path, electrical_series_path=data_path).get_num_frames())
                    frame_offset += n_frames
                except Exception:
                    pass

        if not chunks:
            self._emit("No sorting time-map chunk could be built; summary event mapping will fall back to simple frame offsets.")
            return None

        label = str(group.get("label", "") or "recording").strip() or "recording"
        recording_map = {
            "label": label,
            "source_files": source_files,
            "electrical_series_path": data_path,
            "total_concat_frames": int(frame_offset),
            "n_chunks": int(len(chunks)),
            "n_timestamp_gaps_detected": int(total_gaps),
            "chunks": chunks,
        }
        payload = {
            "version": 2,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "description": "Maps NWB ElectricalSeries time to sorter/concatenated frame indices. Used for gap-safe unit-summary event alignment.",
            "recordings": {label: recording_map},
        }
        map_path = output_folder / "sorting_time_map.json"
        try:
            with open(map_path, "w", encoding="utf-8") as f:
                _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
            if total_gaps:
                self._emit(f"Wrote gap-safe sorting_time_map.json with {total_gaps} timestamp gap(s): {map_path}")
            else:
                self._emit(f"Wrote sorting_time_map.json: {map_path}")
        except Exception as exc:
            self._emit(f"Could not save sorting_time_map.json: {exc}")
            return None
        return str(map_path)

    @classmethod
    def _time_map_chunks_from_nwb_file(cls, *, file_path: str, electrical_series_path: str, concat_frame_offset: int = 0, first_chunk_index: int = 0):
        from pynwb import NWBHDF5IO
        with NWBHDF5IO(str(file_path), mode="r") as io:
            nwb = io.read()
            es = cls._resolve_nwb_object_path(nwb, electrical_series_path)
            try:
                n_frames = int(es.data.shape[0])
            except Exception:
                n_frames = len(es.data)

            rate = getattr(es, "rate", None)
            if rate is not None:
                fs = float(rate)
                start_time = float(getattr(es, "starting_time", 0.0) or 0.0)
                if n_frames <= 0:
                    return [], 0, 0
                chunk = {
                    "chunk_index": int(first_chunk_index),
                    "file_path": str(file_path),
                    "electrical_series_path": str(electrical_series_path),
                    "source_start_frame": 0,
                    "source_stop_frame": int(n_frames),
                    "concat_start_frame": int(concat_frame_offset),
                    "concat_stop_frame": int(concat_frame_offset + n_frames),
                    "source_start_time": float(start_time),
                    "source_stop_time_exclusive": float(start_time + (int(n_frames) / fs)),
                    "source_last_time": float(start_time + ((int(n_frames) - 1) / fs)),
                    "sampling_frequency": float(fs),
                    "n_frames": int(n_frames),
                    "has_timestamps": False,
                    "timestamp_gap_detected": False,
                }
                return [chunk], int(n_frames), 0

            timestamps = getattr(es, "timestamps", None)
            if timestamps is None:
                # Last-resort rate-less data. Keep compatibility with the old
                # simple behavior, but still provide a formal map.
                fs = 1.0
                start_time = 0.0
                chunk = {
                    "chunk_index": int(first_chunk_index),
                    "file_path": str(file_path),
                    "electrical_series_path": str(electrical_series_path),
                    "source_start_frame": 0,
                    "source_stop_frame": int(n_frames),
                    "concat_start_frame": int(concat_frame_offset),
                    "concat_stop_frame": int(concat_frame_offset + n_frames),
                    "source_start_time": float(start_time),
                    "source_stop_time_exclusive": float(start_time + int(n_frames)),
                    "source_last_time": float(start_time + max(0, int(n_frames) - 1)),
                    "sampling_frequency": float(fs),
                    "n_frames": int(n_frames),
                    "has_timestamps": False,
                    "timestamp_gap_detected": False,
                }
                return [chunk], int(n_frames), 0

            ts = np.asarray(timestamps[:], dtype=float).reshape(-1)
            n = min(int(n_frames), int(ts.size))
            if n <= 0:
                return [], int(n_frames), 0
            ts = ts[:n]
            good = np.isfinite(ts)
            if not np.all(good):
                # Keep only the largest contiguous finite stretch if malformed.
                finite_idx = np.flatnonzero(good)
                if finite_idx.size == 0:
                    return [], int(n_frames), 0
                ts = ts[finite_idx]
                n = int(ts.size)

            diffs = np.diff(ts)
            positive_diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
            dt = float(np.nanmedian(positive_diffs)) if positive_diffs.size else 1.0
            if not np.isfinite(dt) or dt <= 0:
                dt = 1.0
            fs = 1.0 / dt

            # Split at timestamp jumps. A jump means the timestamp difference is
            # much larger than the regular sample period. This is the gap that
            # invalidates simple cumulative frame-offset mapping.
            gap_threshold = max(dt * 1.5, dt + 1e-9)
            gap_after = np.flatnonzero(diffs > gap_threshold)
            starts = np.r_[0, gap_after + 1]
            stops = np.r_[gap_after + 1, n]
            chunks = []
            for local_i, (start, stop) in enumerate(zip(starts, stops)):
                start = int(start)
                stop = int(stop)
                if stop <= start:
                    continue
                last_time = float(ts[stop - 1])
                chunk_dt = dt
                if stop - start > 1:
                    local_diffs = np.diff(ts[start:stop])
                    local_pos = local_diffs[np.isfinite(local_diffs) & (local_diffs > 0)]
                    if local_pos.size:
                        chunk_dt = float(np.nanmedian(local_pos))
                if not np.isfinite(chunk_dt) or chunk_dt <= 0:
                    chunk_dt = dt
                chunk_fs = 1.0 / chunk_dt
                chunks.append({
                    "chunk_index": int(first_chunk_index + len(chunks)),
                    "file_path": str(file_path),
                    "electrical_series_path": str(electrical_series_path),
                    "source_start_frame": int(start),
                    "source_stop_frame": int(stop),
                    "concat_start_frame": int(concat_frame_offset + start),
                    "concat_stop_frame": int(concat_frame_offset + stop),
                    "source_start_time": float(ts[start]),
                    "source_stop_time_exclusive": float(last_time + chunk_dt),
                    "source_last_time": float(last_time),
                    "sampling_frequency": float(chunk_fs),
                    "n_frames": int(stop - start),
                    "has_timestamps": True,
                    "timestamp_gap_detected": bool(len(gap_after) > 0),
                })
            return chunks, int(n_frames), int(len(gap_after))

    def _map_event_frames_for_group_with_time_map(self, *, group: dict, files: list, marker: str, edge: str, output_folder: Path, time_map_recording: dict, map_filename: str = "summary_event_alignment_map.json"):
        chunks = [c for c in (time_map_recording.get("chunks", []) or []) if isinstance(c, dict)]
        chunks_by_file = {}
        for c in chunks:
            fp = self._norm_path_for_time_map(c.get("file_path", ""))
            if not fp:
                continue
            chunks_by_file.setdefault(fp, []).append(c)
        for fp in chunks_by_file:
            chunks_by_file[fp].sort(key=lambda c: (int(c.get("source_start_frame", 0)), int(c.get("concat_start_frame", 0))))

        event_rows = []
        event_frames = []
        for file_path in files:
            norm_fp = self._norm_path_for_time_map(file_path)
            file_chunks = chunks_by_file.get(norm_fp, [])
            if not file_chunks:
                self._emit(f"No time-map chunks found for {Path(file_path).name}; events from this file are skipped.")
                continue
            try:
                times = self._event_times_from_nwb(file_path=file_path, marker=marker, edge=edge)
                if not times.size:
                    continue
                for t in np.asarray(times, dtype=float).reshape(-1):
                    mapped = self._map_one_event_time_to_sorter_frame_with_chunks(float(t), file_chunks)
                    if mapped is None:
                        # Event is outside the ElectricalSeries or falls inside a timestamp gap.
                        continue
                    concat_frame, chunk, source_frame = mapped
                    event_frames.append(int(concat_frame))
                    event_rows.append({
                        "file_path": str(file_path),
                        "event_marker": marker,
                        "edge": edge,
                        "event_time": float(t),
                        "source_frame": int(source_frame),
                        "concat_frame": int(concat_frame),
                        "chunk_index": int(chunk.get("chunk_index", -1)),
                        "source_start_frame": int(chunk.get("source_start_frame", 0)),
                        "source_stop_frame": int(chunk.get("source_stop_frame", 0)),
                        "concat_start_frame": int(chunk.get("concat_start_frame", 0)),
                        "concat_stop_frame": int(chunk.get("concat_stop_frame", 0)),
                        "source_start_time": float(chunk.get("source_start_time", 0.0)),
                        "source_stop_time_exclusive": float(chunk.get("source_stop_time_exclusive", chunk.get("source_last_time", 0.0))),
                        "mapping_mode": "sorting_time_map",
                    })
            except Exception as exc:
                self._emit(f"Could not map events for {Path(file_path).name} with sorting_time_map.json: {exc}")

        event_frames = np.asarray(sorted(set(int(x) for x in event_frames)), dtype=np.int64)
        map_path = Path(output_folder) / str(map_filename or "summary_event_alignment_map.json")
        payload = {
            "version": 2,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "recording_label": str(group.get("label", "")),
            "event_marker": marker,
            "edge": edge,
            "mapping_mode": "sorting_time_map",
            "gap_safe": True,
            "time_map_n_chunks": int(len(chunks)),
            "time_map_n_timestamp_gaps_detected": int(time_map_recording.get("n_timestamp_gaps_detected", 0) or 0),
            "time_map_total_concat_frames": int(time_map_recording.get("total_concat_frames", 0) or 0),
            "n_events_mapped": int(event_frames.size),
            "event_frames": [int(x) for x in event_frames.tolist()],
            "events": event_rows,
        }
        with open(map_path, "w", encoding="utf-8") as f:
            _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
        return event_frames, str(map_path)

    @staticmethod
    def _map_one_event_time_to_sorter_frame_with_chunks(event_time: float, chunks: list):
        # Use each continuous chunk as a half-open interval in original NWB time.
        # The small tolerance handles floating point timestamp/rate rounding.
        for c in chunks:
            try:
                start_t = float(c.get("source_start_time"))
                stop_t = float(c.get("source_stop_time_exclusive", c.get("source_last_time")))
                fs = float(c.get("sampling_frequency"))
                src_start = int(c.get("source_start_frame"))
                src_stop = int(c.get("source_stop_frame"))
                concat_start = int(c.get("concat_start_frame"))
                concat_stop = int(c.get("concat_stop_frame"))
            except Exception:
                continue
            tol = max(1e-9, 0.51 / fs)
            if event_time < start_t - tol or event_time >= stop_t + tol:
                continue
            source_frame = src_start + int(round((event_time - start_t) * fs))
            if source_frame < src_start or source_frame >= src_stop:
                continue
            concat_frame = concat_start + (source_frame - src_start)
            if concat_frame < concat_start or concat_frame >= concat_stop:
                continue
            return int(concat_frame), c, int(source_frame)
        return None

    def _map_event_frames_for_group_simple(self, *, group: dict, files: list, data_path: str, marker: str, edge: str, output_folder: Path, map_filename: str = "summary_event_alignment_map.json"):
        event_rows = []
        event_frames = []
        frame_offset = 0
        for file_path in files:
            try:
                times = self._event_times_from_nwb(file_path=file_path, marker=marker, edge=edge)
                n_frames, fs, start_time = self._recording_frame_info_from_nwb(file_path=file_path, electrical_series_path=data_path)
                if times.size:
                    local_frames = np.rint((times - float(start_time)) * float(fs)).astype(np.int64)
                    valid = (local_frames >= 0) & (local_frames < int(n_frames))
                    for t, lf in zip(times[valid], local_frames[valid]):
                        cf = int(frame_offset + int(lf))
                        event_frames.append(cf)
                        event_rows.append({
                            "file_path": str(file_path),
                            "event_marker": marker,
                            "edge": edge,
                            "event_time": float(t),
                            "local_frame": int(lf),
                            "concat_frame": cf,
                            "mapping_mode": "simple_cumulative_frame_offset",
                        })
                frame_offset += int(n_frames)
            except Exception as exc:
                self._emit(f"Could not map events for {Path(file_path).name}: {exc}")
                try:
                    n_frames = int(_RecordingLoadWorker._read_nwb_recording(file_path=file_path, electrical_series_path=data_path).get_num_frames())
                    frame_offset += n_frames
                except Exception:
                    pass

        event_frames = np.asarray(sorted(set(int(x) for x in event_frames)), dtype=np.int64)
        map_path = Path(output_folder) / str(map_filename or "summary_event_alignment_map.json")
        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "recording_label": str(group.get("label", "")),
            "event_marker": marker,
            "edge": edge,
            "mapping_mode": "simple_cumulative_frame_offset",
            "gap_safe": False,
            "n_events_mapped": int(event_frames.size),
            "event_frames": [int(x) for x in event_frames.tolist()],
            "events": event_rows,
        }
        with open(map_path, "w", encoding="utf-8") as f:
            _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
        return event_frames, str(map_path)

    @staticmethod
    def _resolve_nwb_object_path(nwb, path: str):
        path = str(path or "").strip().strip("/")
        if not path:
            raise KeyError("Empty NWB object path")
        parts = [p for p in path.split("/") if p]
        obj = nwb
        for part in parts:
            if hasattr(obj, "get"):
                nxt = obj.get(part)
                if nxt is not None:
                    obj = nxt
                    continue
            if hasattr(obj, part):
                obj = getattr(obj, part)
                continue
            if hasattr(obj, "__getitem__"):
                obj = obj[part]
                continue
            raise KeyError(path)
        return obj

    @classmethod
    def _recording_frame_info_from_nwb(cls, *, file_path: str, electrical_series_path: str):
        from pynwb import NWBHDF5IO
        with NWBHDF5IO(str(file_path), mode="r") as io:
            nwb = io.read()
            es = cls._resolve_nwb_object_path(nwb, electrical_series_path)
            try:
                n_frames = int(es.data.shape[0])
            except Exception:
                n_frames = len(es.data)
            rate = getattr(es, "rate", None)
            if rate is None:
                ts = np.asarray(es.timestamps[:], dtype=float)
                fs = 1.0 / float(np.nanmedian(np.diff(ts))) if ts.size > 1 else 1.0
                start_time = float(ts[0]) if ts.size else 0.0
            else:
                fs = float(rate)
                start_time = float(getattr(es, "starting_time", 0.0) or 0.0)
            return int(n_frames), float(fs), float(start_time)

    @staticmethod
    def _event_times_from_nwb(*, file_path: str, marker: str, edge: str):
        from pynwb import NWBHDF5IO
        from pynwb.misc import AnnotationSeries
        from pynwb.epoch import TimeIntervals
        from pynwb.behavior import BehavioralEvents, BehavioralEpochs
        from pynwb.ecephys import EventDetection
        marker = str(marker)
        out = []

        def ann_times(ann):
            if getattr(ann, "timestamps", None) is not None:
                try:
                    return np.asarray(ann.timestamps[:], dtype=float)
                except Exception:
                    return np.asarray(ann.timestamps, dtype=float)
            rate = getattr(ann, "rate", None)
            if rate:
                n = len(ann.data)
                return float(getattr(ann, "starting_time", 0.0) or 0.0) + np.arange(n, dtype=float) / float(rate)
            return np.array([], dtype=float)

        def interval_times(ti):
            try:
                df = ti.to_dataframe()
                start = df["start_time"].to_numpy(dtype=float)
                stop = df["stop_time"].to_numpy(dtype=float)
            except Exception:
                return np.array([], dtype=float)
            if edge == "stop":
                return stop
            if edge == "center":
                return (start + stop) / 2.0
            return start

        with NWBHDF5IO(str(file_path), mode="r") as io:
            nwb = io.read()

            if marker.strip().lower() == "trials":
                trials = getattr(nwb, "trials", None)
                if trials is not None:
                    out.append(interval_times(trials))

            for container_name in ("acquisition", "stimulus"):
                cont = getattr(nwb, container_name, None)
                if cont is not None and marker in cont:
                    obj = cont[marker]
                    if isinstance(obj, AnnotationSeries):
                        out.append(ann_times(obj))
            intervals = getattr(nwb, "intervals", None)
            if intervals is not None and marker in intervals:
                obj = intervals[marker]
                if isinstance(obj, TimeIntervals):
                    out.append(interval_times(obj))
            for mod in getattr(nwb, "processing", {}).values():
                data_interfaces = getattr(mod, "data_interfaces", {})
                if marker not in data_interfaces:
                    continue
                obj = data_interfaces[marker]
                if isinstance(obj, AnnotationSeries):
                    out.append(ann_times(obj))
                elif isinstance(obj, TimeIntervals):
                    out.append(interval_times(obj))
                elif isinstance(obj, BehavioralEvents):
                    times = []
                    for ts in getattr(obj, "time_series", {}).values():
                        if getattr(ts, "timestamps", None) is not None:
                            times.extend(np.asarray(ts.timestamps[:], dtype=float).tolist())
                    if times:
                        out.append(np.asarray(times, dtype=float))
                elif isinstance(obj, BehavioralEpochs):
                    times = []
                    for ti in getattr(obj, "time_intervals", {}).values():
                        times.extend(interval_times(ti).tolist())
                    if times:
                        out.append(np.asarray(times, dtype=float))
                elif isinstance(obj, EventDetection):
                    try:
                        out.append(np.asarray(obj.timestamps[:], dtype=float))
                    except Exception:
                        pass

        if not out:
            return np.array([], dtype=float)
        return np.sort(np.concatenate([np.asarray(x, dtype=float).reshape(-1) for x in out if np.asarray(x).size]))

    @staticmethod
    def _event_spans_from_nwb(*, file_path: str, marker: str):
        from pynwb import NWBHDF5IO
        from pynwb.epoch import TimeIntervals
        from pynwb.behavior import BehavioralEpochs
        marker = str(marker)
        out = []

        def interval_spans(ti):
            try:
                df = ti.to_dataframe()
                start = df["start_time"].to_numpy(dtype=float)
                stop = df["stop_time"].to_numpy(dtype=float)
            except Exception:
                return np.empty((0, 2), dtype=float)
            spans = np.column_stack([start, stop]) if len(start) else np.empty((0, 2), dtype=float)
            spans = spans[np.isfinite(spans).all(axis=1)] if spans.size else spans
            spans = spans[spans[:, 1] > spans[:, 0]] if spans.size else spans
            return spans

        with NWBHDF5IO(str(file_path), mode="r") as io:
            nwb = io.read()

            if marker.strip().lower() == "trials":
                trials = getattr(nwb, "trials", None)
                if trials is not None:
                    out.append(interval_spans(trials))

            intervals = getattr(nwb, "intervals", None)
            if intervals is not None and marker in intervals:
                obj = intervals[marker]
                if isinstance(obj, TimeIntervals):
                    out.append(interval_spans(obj))

            for mod in getattr(nwb, "processing", {}).values():
                data_interfaces = getattr(mod, "data_interfaces", {})
                if marker not in data_interfaces:
                    continue
                obj = data_interfaces[marker]
                if isinstance(obj, TimeIntervals):
                    out.append(interval_spans(obj))
                elif isinstance(obj, BehavioralEpochs):
                    for attr_name in ("time_intervals", "intervals"):
                        maybe = getattr(obj, attr_name, None)
                        if maybe is None:
                            continue
                        try:
                            iterable = maybe.values() if hasattr(maybe, "values") else maybe
                            for ti in iterable:
                                if isinstance(ti, TimeIntervals):
                                    out.append(interval_spans(ti))
                        except Exception:
                            pass

        if not out:
            return np.empty((0, 2), dtype=float)
        spans = [np.asarray(x, dtype=float).reshape(-1, 2) for x in out if np.asarray(x).size]
        if not spans:
            return np.empty((0, 2), dtype=float)
        spans = np.vstack(spans)
        if spans.size == 0:
            return np.empty((0, 2), dtype=float)
        spans = spans[np.isfinite(spans).all(axis=1)]
        spans = spans[spans[:, 1] > spans[:, 0]]
        return spans[np.argsort(spans[:, 0])] if spans.size else np.empty((0, 2), dtype=float)


    def _write_run_config_snapshot(self, *, label, group, output_folder: Path, run_signature: dict, run_signature_hash: str):
        """Save the exact run configuration before computation starts.

        This makes failed and completed attempts traceable and allows the GUI to
        detect identical parameter sets later.
        """
        output_folder = Path(output_folder)
        payload = {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "running",
            "recording_label": str(label),
            "recording_group": _json_safe(dict(group or {})),
            "queue_configuration": _json_safe({
                "id": self.configuration_id,
                "name": self.configuration_name,
            }),
            "sorter_name": self.sorter_name,
            "sorter_params": _json_safe(dict(self.params or {})),
            "preprocessing_pipeline": _json_safe(dict(self.pipeline or {})),
            "enabled_preprocessing": _enabled_pipeline_for_signature(self.pipeline),
            "unit_summary_config": _json_safe(dict(self.summary_config or {})),
            "runtime_config": _json_safe({
                "use_multiprocessing": bool(self.use_multiprocessing),
                "n_jobs": self.n_jobs if self.use_multiprocessing else 1,
                "skip_failed_sorting": bool(getattr(self, "skip_failed_sorting", True)),
            }),
            "run_signature": _json_safe(dict(run_signature or {})),
            "run_signature_hash": str(run_signature_hash or ""),
        }
        for name, obj in (
            ("run_config.json", payload),
            ("sorter_params.json", payload["sorter_params"]),
            ("preprocessing_pipeline.json", payload["preprocessing_pipeline"]),
            ("unit_summary_config.json", payload["unit_summary_config"]),
            ("recording_group.json", payload["recording_group"]),
        ):
            try:
                with open(output_folder / name, "w", encoding="utf-8") as f:
                    _pp_json_dump(obj, f, indent=2, ensure_ascii=False)
            except Exception as exc:
                self._emit(f"Could not save {name}: {exc}")

    def _update_run_config_status(self, output_folder, status, **extra):
        """Update run_config.json so the Curation tab can distinguish running vs failed runs."""
        if output_folder is None:
            return
        output_folder = Path(output_folder)
        path = output_folder / "run_config.json"
        payload = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    obj = _pp_json_load(f)
                if isinstance(obj, dict):
                    payload = obj
            except Exception:
                payload = {}
        payload["status"] = str(status or "")
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if str(status or "").lower() in {"failed", "error"}:
            payload["failed_at"] = payload["updated_at"]
        elif str(status or "").lower() in {"finished", "done", "completed"}:
            payload["finished_at"] = payload["updated_at"]
        for key, value in (extra or {}).items():
            payload[str(key)] = _json_safe(value)
        try:
            with open(path, "w", encoding="utf-8") as f:
                _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            self._emit(f"Could not update run_config.json status: {exc}")

        # Keep the traceback in a plain-text file as well as in run_config.json.
        # Curation can open this directly when the Units cell says
        # "Sorting Failed".  The JSON fallback below keeps older failed runs
        # (created before this file was added) readable too.
        if str(status or "").lower() in {"failed", "error"}:
            error_text = str((extra or {}).get("error_text", "") or "").strip()
            if error_text:
                try:
                    with open(output_folder / "sorting_error.log", "w", encoding="utf-8") as f:
                        f.write(error_text)
                        if not error_text.endswith("\n"):
                            f.write("\n")
                except Exception as exc:
                    self._emit(f"Could not save sorting_error.log: {exc}")

    def _write_run_summary(self, *, label, group, output_folder: Path, analyzer_folder: Path, sorting_summary_folder=None, summary_event_map_path=None, run_signature=None, run_signature_hash=None, sorting_analyzer=None):
        output_folder = Path(output_folder)
        if run_signature is None:
            run_signature = _build_sorting_run_signature(
                label=label,
                group=group,
                recipe=self.recipe,
                pipeline=self.pipeline,
                sorter_name=self.sorter_name,
                sorter_params=self.params,
                summary_config=self.summary_config,
            )
        if run_signature_hash is None:
            run_signature_hash = _signature_hash(run_signature)

        path = output_folder / "sorting_run.json"
        unit_count = None
        if sorting_analyzer is not None:
            for getter in (
                lambda x: x.unit_ids,
                lambda x: x.sorting.get_unit_ids(),
                lambda x: x.get_unit_ids(),
            ):
                try:
                    ids = getter(sorting_analyzer)
                    unit_count = len(list(ids))
                    break
                except Exception:
                    pass
        payload = {
            "version": 2,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "recording_label": str(label),
            "recording_group": _json_safe(dict(group or {})),
            "queue_configuration": _json_safe({
                "id": self.configuration_id,
                "name": self.configuration_name,
            }),
            "sorter_name": self.sorter_name,
            "sorter_params": _json_safe(dict(self.params or {})),
            "preprocessing_pipeline": _json_safe(dict(self.pipeline or {})),
            "enabled_preprocessing": _enabled_pipeline_for_signature(self.pipeline),
            "unit_summary_config": _json_safe(dict(self.summary_config or {})),
            "runtime_config": _json_safe({
                "use_multiprocessing": bool(self.use_multiprocessing),
                "n_jobs": self.n_jobs if self.use_multiprocessing else 1,
                "skip_failed_sorting": bool(getattr(self, "skip_failed_sorting", True)),
            }),
            "run_signature": _json_safe(dict(run_signature or {})),
            "run_signature_hash": str(run_signature_hash or ""),
            "output_folder": str(output_folder),
            "sorter_run_folder": str(_sorter_work_folder(output_folder)),
            "sorting_analyzer_folder": str(analyzer_folder),
            "sorting_summary_folder": None if sorting_summary_folder is None else str(sorting_summary_folder),
            "summary_event_map_path": summary_event_map_path,
            "unit_count": unit_count,
        }
        with open(path, "w", encoding="utf-8") as f:
            _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
        return path


class _SortingQueueWorker(QtCore.QObject):
    """Run several sorter/parameter configurations sequentially.

    A configuration is applied to every selected recording group before the
    next configuration starts. Running sorters sequentially avoids competing
    for the same CPU, RAM, GPU or Docker resources while still allowing each
    sorter to use the application's multiprocessing settings internally.
    """

    log = QtCore.Signal(str)
    progress = QtCore.Signal(int, int, str, str)
    finished = QtCore.Signal(list, int)
    failed = QtCore.Signal(str)

    def __init__(
        self,
        *,
        sorting_path,
        recipe,
        pipeline,
        preprocessing_specs,
        groups,
        configurations,
        existing_policy="cancel",
        summary_config=None,
        use_multiprocessing=True,
        n_jobs=None,
        skip_failed_sorting=True,
    ):
        super().__init__()
        self.sorting_path = Path(sorting_path)
        self.recipe = dict(recipe or {})
        self.pipeline = dict(pipeline or {})
        self.preprocessing_specs = dict(preprocessing_specs or {})
        self.groups = list(groups or [])
        self.configurations = [dict(c) for c in (configurations or []) if isinstance(c, dict)]
        self.existing_policy = str(existing_policy or "cancel")
        self.summary_config = dict(summary_config or {})
        self.use_multiprocessing = bool(use_multiprocessing)
        self.n_jobs = _available_multiprocessing_jobs(n_jobs) if self.use_multiprocessing else 1
        self.skip_failed_sorting = bool(skip_failed_sorting)

    def _emit(self, text=""):
        try:
            self.log.emit(str(text))
        except Exception:
            pass

    def run(self):
        try:
            summary_paths = []
            failed_count = 0
            total_configurations = len(self.configurations)
            groups_per_configuration = len(self.groups)
            total_runs = total_configurations * groups_per_configuration

            for config_index, configuration in enumerate(self.configurations, start=1):
                config_name = str(configuration.get("name", "")).strip() or f"Configuration {config_index}"
                sorter_name = str(configuration.get("sorter_name", "")).strip()
                params = dict(configuration.get("sorter_params", {}) or {})
                configuration_pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
                if not isinstance(configuration_pipeline, dict):
                    configuration_pipeline = self.pipeline

                self._emit("")
                self._emit("#" * 80)
                self._emit(f"Configuration {config_index}/{total_configurations}: {config_name}")
                self._emit(f"Sorter: {sorter_name}")
                self._emit(
                    f"Preprocessing steps enabled: "
                    f"{len(_enabled_pipeline_for_signature(configuration_pipeline))}"
                )
                self._emit("#" * 80)

                result = {"summaries": [], "error": ""}
                worker = _SortingRunWorker(
                    sorting_path=self.sorting_path,
                    recipe=self.recipe,
                    pipeline=configuration_pipeline,
                    preprocessing_specs=self.preprocessing_specs,
                    groups=self.groups,
                    sorter_name=sorter_name,
                    params=params,
                    configuration_name=config_name,
                    configuration_id=str(configuration.get("id", "")),
                    existing_policy=self.existing_policy,
                    summary_config=self.summary_config,
                    use_multiprocessing=self.use_multiprocessing,
                    n_jobs=self.n_jobs,
                    skip_failed_sorting=self.skip_failed_sorting,
                )
                worker.log.connect(self.log.emit)

                def _forward_progress(current, _total, label, state, *, queue_index=config_index, queue_name=config_name):
                    try:
                        current = int(current)
                    except Exception:
                        current = 0
                    global_current = ((queue_index - 1) * groups_per_configuration) + current
                    display_label = f"{queue_name} | {str(label or '').strip()}".rstrip(" |")
                    self.progress.emit(global_current, total_runs, display_label, str(state or "running"))

                worker.progress.connect(_forward_progress)
                worker.finished.connect(lambda paths, target=result: target["summaries"].extend(list(paths or [])))
                worker.failed.connect(lambda error, target=result: target.__setitem__("error", str(error or "")))
                worker.run()

                for path in result["summaries"]:
                    path = str(path)
                    if path not in summary_paths:
                        summary_paths.append(path)
                failed_count += len(getattr(worker, "failed_records", []) or [])

                if result["error"]:
                    failed_count += 1
                    if not self.skip_failed_sorting:
                        self.failed.emit(result["error"])
                        return
                    self._emit(f"Configuration failed: {config_name}")
                    self._emit("Skip failed sorting is enabled. Continuing with the next configuration.")

            self.finished.emit(summary_paths, int(failed_count))
        except Exception:
            self.failed.emit(traceback.format_exc())


class _PreprocessingOptionsDialog(QtWidgets.QDialog):
    """Small Qt editor for preprocessing_pipeline.json.

    It mirrors the Tk behavior: discovered preprocessing tools are displayed as
    ordered checkbox rows with their parameters.  The dialog can return a
    serializable config for JSON and a runtime config that keeps the callable
    functions for preview/application.
    """

    def __init__(self, parent, steps, saved_pipeline=None):
        super().__init__(parent)
        self.setWindowTitle("Preprocessing options")
        self.resize(900, 620)
        self.steps = list(steps or [])
        self.saved_pipeline = saved_pipeline or {"pipeline": [], "enabled_pipeline": []}
        self.enabled_checks = {}
        self.order_spins = {}
        self.param_widgets = {}
        self.param_specs = {}
        self.list_display_to_value = {}
        self._build()
        self._restore_saved_pipeline()

    @staticmethod
    def _to_bool(value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "checked"}

    @staticmethod
    def _pretty_name(name):
        return str(name).replace("_", " ").strip().capitalize()

    @staticmethod
    def _normalize_param_spec(raw):
        if isinstance(raw, dict):
            spec = dict(raw)
        else:
            spec = {"default": raw}
        default = spec.get("default", "")
        if "type" not in spec:
            if isinstance(default, bool):
                spec["type"] = "bool"
            elif isinstance(default, int) and not isinstance(default, bool):
                spec["type"] = "int"
            elif isinstance(default, float):
                spec["type"] = "float"
            elif isinstance(default, (list, tuple)):
                spec["type"] = "list"
            else:
                spec["type"] = "str"
        return spec

    @staticmethod
    def _choice_values(spec):
        for key in ("values", "choices", "options"):
            values = spec.get(key, None)
            if values is not None:
                if isinstance(values, dict):
                    return list(values.items())
                if isinstance(values, (list, tuple)):
                    return list(values)
        default = spec.get("default", None)
        if isinstance(default, (list, tuple)):
            return list(default)
        return []

    @staticmethod
    def _make_choice_map(values):
        out = {}
        for value in values:
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                out[str(value[0])] = value[1]
            else:
                out[str(value)] = value
        return out

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        if not self.steps:
            msg = QtWidgets.QLabel(
                "No preprocessing scripts were discovered.\n\n"
                "Expected folders include:\n"
                "  <project>/Script/Spike_sorting/Preprocessing\n"
                "  <package>/Spike_sorting/Preprocessing"
            )
            msg.setWordWrap(True)
            outer.addWidget(msg)
        else:
            scroll = QtWidgets.QScrollArea(self)
            scroll.setWidgetResizable(True)
            body = QtWidgets.QWidget(scroll)
            self.body_layout = QtWidgets.QVBoxLayout(body)
            self.body_layout.setContentsMargins(6, 6, 6, 6)
            self.body_layout.setSpacing(6)
            for step in self.steps:
                self._add_step_row(step)
            self.body_layout.addStretch(1)
            scroll.setWidget(body)
            outer.addWidget(scroll, stretch=1)

        buttons = QtWidgets.QDialogButtonBox(self)
        self.apply_button = buttons.addButton("Apply preprocessing", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QDialogButtonBox, "ButtonRole") else QtWidgets.QDialogButtonBox.AcceptRole)
        self.reset_button = buttons.addButton("Reset", QtWidgets.QDialogButtonBox.ButtonRole.ResetRole if hasattr(QtWidgets.QDialogButtonBox, "ButtonRole") else QtWidgets.QDialogButtonBox.ResetRole)
        self.close_button = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Close if hasattr(QtWidgets.QDialogButtonBox, "StandardButton") else QtWidgets.QDialogButtonBox.Close)
        self.apply_button.clicked.connect(self.accept)
        self.reset_button.clicked.connect(self._reset_to_defaults)
        self.close_button.clicked.connect(self.reject)
        outer.addWidget(buttons)

    def _add_step_row(self, step):
        key = step["key"]
        group = QtWidgets.QGroupBox(self)
        layout = QtWidgets.QGridLayout(group)
        layout.setColumnStretch(3, 1)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(4)

        enabled = QtWidgets.QCheckBox(group)
        enabled.setChecked(self._to_bool(step.get("default_enabled", False)))
        self.enabled_checks[key] = enabled
        layout.addWidget(enabled, 0, 0, 2, 1)

        order = QtWidgets.QSpinBox(group)
        order.setRange(1, 999)
        order.setValue(int(step.get("default_order", step.get("display_index", 1)) or 1))
        order.setMaximumWidth(60)
        self.order_spins[key] = order
        layout.addWidget(order, 0, 1, 2, 1)

        name = QtWidgets.QLabel(str(step.get("name", key)))
        f = name.font(); f.setBold(True); name.setFont(f)
        layout.addWidget(name, 0, 2)

        source_bits = []
        if step.get("source"):
            source_bits.append(str(step.get("source")))
        if step.get("source_file"):
            source_bits.append(Path(str(step.get("source_file"))).name)
        source_lbl = QtWidgets.QLabel(" | ".join(source_bits))
        source_lbl.setStyleSheet("color: #777;")
        layout.addWidget(source_lbl, 1, 2)

        params_box = QtWidgets.QWidget(group)
        params_layout = QtWidgets.QGridLayout(params_box)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setHorizontalSpacing(10)
        params_layout.setVerticalSpacing(4)
        params = dict(step.get("params", {}) or {})
        self.param_widgets[key] = {}
        self.param_specs[key] = {}
        self.list_display_to_value[key] = {}
        if params:
            for i, (param_name, raw_spec) in enumerate(params.items()):
                self._add_param_widget(params_layout, key, param_name, raw_spec, i)
        else:
            lbl = QtWidgets.QLabel("No parameter")
            lbl.setStyleSheet("color: #777;")
            params_layout.addWidget(lbl, 0, 0)
        layout.addWidget(params_box, 0, 3, 2, 1)

        desc = str(step.get("description", "")).strip()
        if desc:
            desc_lbl = QtWidgets.QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color: #666;")
            layout.addWidget(desc_lbl, 2, 3)

        self.body_layout.addWidget(group)

    def _add_param_widget(self, layout, step_key, param_name, raw_spec, index):
        spec = self._normalize_param_spec(raw_spec)
        ptype = str(spec.get("type", "str")).lower()
        default = spec.get("default", "")
        self.param_specs[step_key][param_name] = spec
        row = index // 3
        col = (index % 3) * 2
        label_text = str(spec.get("label", self._pretty_name(param_name)))

        if ptype in ("bool", "boolean"):
            widget = QtWidgets.QCheckBox(label_text)
            widget.setChecked(self._to_bool(default))
            layout.addWidget(widget, row, col, 1, 2)
        else:
            layout.addWidget(QtWidgets.QLabel(label_text + ":"), row, col)
            if ptype in ("choice", "list"):
                combo = QtWidgets.QComboBox()
                display_map = self._make_choice_map(self._choice_values(spec))
                if not display_map:
                    display_map = {str(default): default}
                self.list_display_to_value[step_key][param_name] = display_map
                combo.addItems(list(display_map.keys()))
                chosen = None
                for display, real in display_map.items():
                    if real == default or str(real) == str(default):
                        chosen = display
                        break
                if chosen is not None:
                    combo.setCurrentText(chosen)
                widget = combo
            else:
                edit = QtWidgets.QLineEdit()
                edit.setText("" if default is None else str(default))
                edit.setMaximumWidth(120)
                widget = edit
            layout.addWidget(widget, row, col + 1)

        self.param_widgets[step_key][param_name] = widget

    def _reset_to_defaults(self):
        for step in self.steps:
            key = step["key"]
            self.enabled_checks[key].setChecked(self._to_bool(step.get("default_enabled", False)))
            self.order_spins[key].setValue(int(step.get("default_order", step.get("display_index", 1)) or 1))
            for param_name, widget in self.param_widgets.get(key, {}).items():
                spec = self.param_specs.get(key, {}).get(param_name, {})
                default = spec.get("default", "")
                ptype = str(spec.get("type", "str")).lower()
                if ptype in ("bool", "boolean") and hasattr(widget, "setChecked"):
                    widget.setChecked(self._to_bool(default))
                elif ptype in ("choice", "list") and hasattr(widget, "setCurrentText"):
                    display_map = self.list_display_to_value.get(key, {}).get(param_name, {})
                    for display, real in display_map.items():
                        if real == default or str(real) == str(default):
                            widget.setCurrentText(display)
                            break
                elif hasattr(widget, "setText"):
                    widget.setText("" if default is None else str(default))

    def _restore_saved_pipeline(self):
        saved_steps = {}
        for step in self.saved_pipeline.get("pipeline", []) or []:
            if isinstance(step, dict):
                if step.get("key"):
                    saved_steps[str(step.get("key"))] = step
        # Fallback by name, useful if key generation changed.
        saved_by_name = {str(s.get("name", "")): s for s in (self.saved_pipeline.get("pipeline", []) or []) if isinstance(s, dict)}

        for step in self.steps:
            key = step["key"]
            saved = saved_steps.get(key) or saved_by_name.get(str(step.get("name", "")))
            if not saved:
                continue
            self.enabled_checks[key].setChecked(self._to_bool(saved.get("enabled", False)))
            try:
                self.order_spins[key].setValue(int(float(saved.get("order", self.order_spins[key].value()))))
            except Exception:
                pass
            params = dict(saved.get("params", {}) or {})
            for param_name, value in params.items():
                widget = self.param_widgets.get(key, {}).get(param_name)
                if widget is None:
                    continue
                spec = self.param_specs.get(key, {}).get(param_name, {})
                ptype = str(spec.get("type", "str")).lower()
                if ptype in ("bool", "boolean") and hasattr(widget, "setChecked"):
                    widget.setChecked(self._to_bool(value))
                elif ptype in ("choice", "list") and hasattr(widget, "setCurrentText"):
                    display_map = self.list_display_to_value.get(key, {}).get(param_name, {})
                    matched = None
                    for display, real in display_map.items():
                        if real == value or str(real) == str(value):
                            matched = display
                            break
                    widget.setCurrentText(matched if matched is not None else str(value))
                elif hasattr(widget, "setText"):
                    widget.setText("" if value is None else str(value))

    def _cast_param_value(self, step_key, param_name):
        widget = self.param_widgets.get(step_key, {}).get(param_name)
        spec = self.param_specs.get(step_key, {}).get(param_name, {})
        ptype = str(spec.get("type", "str")).lower()
        if widget is None:
            return spec.get("default")
        if ptype in ("bool", "boolean"):
            return bool(widget.isChecked())
        if ptype in ("choice", "list"):
            display = str(widget.currentText())
            return self.list_display_to_value.get(step_key, {}).get(param_name, {}).get(display, display)
        raw = widget.text() if hasattr(widget, "text") else ""
        raw = str(raw).strip()
        if ptype == "int":
            return None if raw == "" else int(float(raw))
        if ptype == "float":
            return None if raw == "" else float(raw)
        return raw

    def runtime_config(self):
        pipeline = []
        for step in self.steps:
            key = step["key"]
            params = {}
            for param_name in self.param_widgets.get(key, {}):
                params[param_name] = self._cast_param_value(key, param_name)
            item = {
                "key": key,
                "name": step.get("name"),
                "enabled": bool(self.enabled_checks[key].isChecked()),
                "order": int(self.order_spins[key].value()),
                "display_index": int(step.get("display_index", 999999)),
                "source": step.get("source"),
                "source_file": step.get("source_file"),
                "func": step.get("func"),
                "params": params,
            }
            pipeline.append(item)
        pipeline.sort(key=lambda d: (int(d.get("order", 999999)), int(d.get("display_index", 999999))))
        return {"pipeline": pipeline, "enabled_pipeline": [s for s in pipeline if bool(s.get("enabled"))]}

    def serializable_config(self):
        cfg = self.runtime_config()
        def clean(step):
            out = dict(step)
            out.pop("func", None)
            return out
        return {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "lazy_from_nwb_recipe",
            "pipeline": [clean(s) for s in cfg["pipeline"]],
            "enabled_pipeline": [clean(s) for s in cfg["enabled_pipeline"]],
        }

# -----------------------------
# main right views
# -----------------------------

class _BaseSpikeSortingRightView(QtWidgets.QWidget):
    title = "Spike sorting"
    busyStateChanged = QtCore.Signal(bool, str)

    def __init__(self, sorting_info: dict, parent=None):
        super().__init__(parent)
        self.sorting_info = sorting_info or {}
        self._busy = False
        self._build()

    def is_busy(self) -> bool:
        """Return True while this view has a live background job.

        SpikeSortingPanel uses this to keep the GUI browsable without ever
        deleting a widget that still owns a QThread/PyQtGraph object.
        """
        try:
            if bool(getattr(self, "_busy", False)):
                return True
        except Exception:
            pass
        for attr in ("_loader_thread", "_thread"):
            thread = getattr(self, attr, None)
            if thread is not None:
                try:
                    if thread.isRunning():
                        return True
                except Exception:
                    pass
        return False

    def _emit_busy_state(self, busy: bool, text: str = ""):
        try:
            self.busyStateChanged.emit(bool(busy), str(text or ""))
        except Exception:
            pass

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QtWidgets.QLabel(self.title)
        f = title.font(); f.setPointSize(max(f.pointSize(), 13)); f.setBold(True); title.setFont(f)
        layout.addWidget(title)

        name = self.sorting_info.get("name", "") or self.sorting_info.get("sorting_name", "")
        path = self.sorting_info.get("path", None)

        lbl = QtWidgets.QLabel(f"Sorting: {name}")
        f2 = lbl.font(); f2.setBold(True); lbl.setFont(f2)
        layout.addWidget(lbl)

        if path is not None:
            path_lbl = QtWidgets.QLabel(f"Path: {path}")
            path_lbl.setWordWrap(True)
            layout.addWidget(path_lbl)

        self.build_body(layout)

    def build_body(self, layout):
        pass

    @staticmethod
    def _readonly_param_scalar_text(value):
        if isinstance(value, str):
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return str(value)
        try:
            return json.dumps(_json_safe(value), ensure_ascii=False)
        except Exception:
            return str(value)

    def _add_readonly_param_tree_item(self, parent_item, key, value):
        key_text = str(key)
        if isinstance(value, dict):
            item = QtWidgets.QTreeWidgetItem([key_text, f"dict ({len(value)})"])
            parent_item.addChild(item)
            for child_key in sorted(value.keys(), key=lambda x: str(x)):
                self._add_readonly_param_tree_item(item, child_key, value.get(child_key))
            item.setExpanded(True)
            return
        if isinstance(value, (list, tuple)):
            item = QtWidgets.QTreeWidgetItem([key_text, f"list ({len(value)})"])
            parent_item.addChild(item)
            for index, child_value in enumerate(value):
                self._add_readonly_param_tree_item(item, f"[{index}]", child_value)
            item.setExpanded(True)
            return
        parent_item.addChild(
            QtWidgets.QTreeWidgetItem([key_text, self._readonly_param_scalar_text(value)])
        )

    def _show_readonly_sorter_params(self, title, value):
        """Show parameters with the same modeless tree layout as Curation."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(str(title))
        dialog.resize(820, 600)
        vbox = QtWidgets.QVBoxLayout(dialog)
        vbox.setContentsMargins(12, 12, 12, 12)
        vbox.setSpacing(8)

        header = QtWidgets.QLabel(str(title), dialog)
        header.setStyleSheet("font-size: 15px; font-weight: 700;")
        vbox.addWidget(header)

        tree = QtWidgets.QTreeWidget(dialog)
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Parameter", "Value"])
        tree.setAlternatingRowColors(True)
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(True)
        root = tree.invisibleRootItem()
        safe_value = _json_safe(value)
        if isinstance(safe_value, dict):
            for child_key in sorted(safe_value.keys(), key=lambda x: str(x)):
                self._add_readonly_param_tree_item(root, child_key, safe_value.get(child_key))
        elif isinstance(safe_value, list):
            for index, child_value in enumerate(safe_value):
                self._add_readonly_param_tree_item(root, f"[{index}]", child_value)
        else:
            self._add_readonly_param_tree_item(root, "value", safe_value)
        tree.expandToDepth(1)
        tree.resizeColumnToContents(0)
        vbox.addWidget(tree, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close
            if hasattr(QtWidgets.QDialogButtonBox, "StandardButton")
            else QtWidgets.QDialogButtonBox.Close,
            dialog,
        )
        buttons.rejected.connect(dialog.close)
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).clicked.connect(dialog.close)
        except Exception:
            buttons.accepted.connect(dialog.close)
        vbox.addWidget(buttons)

        try:
            flags = dialog.windowFlags()
            context_hint = QtCore.Qt.WindowType.WindowContextHelpButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowContextHelpButtonHint
            maximize_hint = QtCore.Qt.WindowType.WindowMaximizeButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowMaximizeButtonHint
            minimize_hint = QtCore.Qt.WindowType.WindowMinimizeButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowMinimizeButtonHint
            dialog.setWindowFlags((flags & ~context_hint) | maximize_hint | minimize_hint)
        except Exception:
            pass
        try:
            dialog.setWindowModality(
                QtCore.Qt.WindowModality.NonModal
                if hasattr(QtCore.Qt, "WindowModality")
                else QtCore.Qt.NonModal
            )
        except Exception:
            pass
        try:
            dialog.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DeleteOnClose
                if hasattr(QtCore.Qt, "WidgetAttribute")
                else QtCore.Qt.WA_DeleteOnClose,
                True,
            )
        except Exception:
            pass
        if not hasattr(self, "_param_windows"):
            self._param_windows = []
        self._param_windows.append(dialog)
        try:
            dialog.destroyed.connect(
                lambda *_: self._param_windows.remove(dialog)
                if dialog in self._param_windows
                else None
            )
        except Exception:
            pass
        dialog.show()
        try:
            dialog.raise_()
            dialog.activateWindow()
        except Exception:
            pass

    def _read_recipe(self) -> Optional[dict]:
        path = self.sorting_info.get("path", None)
        if path is None:
            return None
        recipe_path = Path(path) / "sorting_recipe.json"
        if not recipe_path.exists():
            return None
        try:
            with open(recipe_path, "r", encoding="utf-8") as f:
                obj = _pp_json_load(f)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _read_pipeline(self) -> Optional[dict]:
        path = self.sorting_info.get("path", None)
        if path is None:
            return None
        pipeline_path = Path(path) / "preprocessing_pipeline.json"
        if not pipeline_path.exists():
            return None
        try:
            with open(pipeline_path, "r", encoding="utf-8") as f:
                obj = _pp_json_load(f)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None


class SpikeSortingSummaryView(_BaseSpikeSortingRightView):
    title = "Spike sorting"

    def build_body(self, layout):
        recipe = self._read_recipe()
        pipeline = self._read_pipeline()

        if recipe is None:
            layout.addWidget(QtWidgets.QLabel("No sorting_recipe.json found yet."))
            layout.addStretch(1)
            return

        data = recipe.get("data", {}) or {}
        groups = recipe.get("recording_groups", []) or []
        files = recipe.get("files", []) or []

        rows = [
            ("Mode", recipe.get("mode", "")),
            ("Data", data.get("label") or data.get("name") or ""),
            ("NWB path", data.get("path", "")),
            ("Files", str(len(files))),
            ("Recording groups", str(len(groups))),
            ("Concatenate per subject", str(bool(recipe.get("concatenate_per_subject", False)))),
            ("Remove bad channels", str(bool(recipe.get("remove_bad_channels", False)))),
        ]

        form = QtWidgets.QFormLayout()
        for k, v in rows:
            value = QtWidgets.QLabel(str(v))
            value.setWordWrap(True)
            form.addRow(QtWidgets.QLabel(k + ":"), value)
        layout.addLayout(form)

        if pipeline is not None:
            enabled = pipeline.get("enabled_pipeline", []) or []
            layout.addWidget(QtWidgets.QLabel(f"Preprocessing steps enabled: {len(enabled)}"))

        if groups:
            group_box = QtWidgets.QGroupBox("Recording groups")
            group_layout = QtWidgets.QVBoxLayout(group_box)
            table = QtWidgets.QTableWidget(group_box)
            table.setColumnCount(4)
            table.setHorizontalHeaderLabels(["Label", "Subject", "Files", "Concatenated"])
            table.setRowCount(len(groups))
            table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            table.setAlternatingRowColors(True)
            table.verticalHeader().setVisible(False)

            for r, g in enumerate(groups):
                group_files = [str(p) for p in (g.get("files", []) or []) if str(p).strip()]
                values = [
                    g.get("label", ""),
                    g.get("subject", ""),
                    str(len(group_files)),
                    str(bool(g.get("concatenated", False))),
                ]
                for c, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                    if c == 2:
                        item.setData(QtCore.Qt.UserRole, group_files)
                        item.setToolTip("Click to show concatenated NWB files")
                    table.setItem(r, c, item)

            header = table.horizontalHeader()
            try:
                header.setStretchLastSection(False)
                header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
                header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
                header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
                header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
                table.setColumnWidth(0, 220)
            except Exception:
                table.resizeColumnsToContents()
                try:
                    table.setColumnWidth(0, min(table.columnWidth(0), 220))
                except Exception:
                    pass

            row_h = table.verticalHeader().defaultSectionSize()
            header_h = table.horizontalHeader().height()
            visible_rows = max(6, min(len(groups), 14))
            min_height = header_h + (row_h * visible_rows) + 18
            table.setMinimumHeight(min_height)
            table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.MinimumExpanding)
            table.cellDoubleClicked.connect(lambda row, col, t=table: self._on_recording_groups_cell_clicked(t, row, col))

            hint = QtWidgets.QLabel("Double-click any row to show the NWB files included in that recording group.")
            hint.setWordWrap(True)
            group_layout.addWidget(hint)
            group_layout.addWidget(table)
            layout.addWidget(group_box)

        layout.addStretch(1)

    def _on_recording_groups_cell_clicked(self, table, row: int, col: int):
        self._show_recording_group_files(table, row)

    def _show_recording_group_files(self, table, row: int):
        item = table.item(int(row), 2)
        if item is None:
            return
        files = item.data(QtCore.Qt.UserRole) or []
        if not files:
            QtWidgets.QMessageBox.information(self, "Recording group files", "No NWB file is listed for this recording group.")
            return

        label_item = table.item(int(row), 0)
        group_label = label_item.text() if label_item is not None else f"group {int(row) + 1}"

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"Files in {group_label}")
        dialog.resize(760, 360)
        dlg_layout = QtWidgets.QVBoxLayout(dialog)

        title = QtWidgets.QLabel(f"{len(files)} NWB file(s) in recording group: {group_label}")
        title.setWordWrap(True)
        dlg_layout.addWidget(title)

        file_list = QtWidgets.QListWidget(dialog)
        file_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        for path in files:
            p = Path(str(path))
            text = p.name if p.name else str(path)
            entry = QtWidgets.QListWidgetItem(text)
            entry.setToolTip(str(path))
            entry.setData(QtCore.Qt.UserRole, str(path))
            file_list.addItem(entry)
        dlg_layout.addWidget(file_list, 1)

        full_path = QtWidgets.QPlainTextEdit(dialog)
        full_path.setReadOnly(True)
        full_path.setMaximumHeight(86)
        full_path.setPlaceholderText("Select a file to show its full path here.")
        dlg_layout.addWidget(full_path)

        def _show_selected_path():
            items = file_list.selectedItems()
            full_path.setPlainText("\n".join([str(it.data(QtCore.Qt.UserRole) or it.text()) for it in items]))

        file_list.itemSelectionChanged.connect(_show_selected_path)
        if file_list.count():
            file_list.setCurrentRow(0)
            _show_selected_path()

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        dlg_layout.addWidget(buttons)
        dialog.exec()


class SpikeSortingPreprocessingView(_BaseSpikeSortingRightView):
    title = "Preprocessing"

    def build_body(self, layout):
        self.recipe = self._read_recipe() or {}
        self.pipeline = self._read_pipeline() or {"pipeline": [], "enabled_pipeline": []}
        self.preprocessing_specs = self._discover_preprocessing_specs()
        self.preprocessing_steps = self._make_preprocessing_steps()
        self.step_data_by_key = {str(step.get("key")): step for step in self.preprocessing_steps}
        self.recording_items = self._discover_recording_items()
        self.recording_cache = {}
        self.current_recording = None
        self.current_label = ""
        self.channel_ids = []
        self.selected_channel_ids = []
        self._buffer_start = 0
        self._buffer_end = 0
        self._buffer_x = None
        self._buffer_y = None
        self._buffer_raw = None
        self._buffer_offsets = None
        self._buffer_spacing = 1.0
        self._buffer_channels_key = None
        self._window_s = 5.0
        self._buffer_margin_windows = 6.0
        self._buffer_edge_fraction = 0.30
        self._trace_gain = 1.2
        self._user_gain_touched = False
        self._time_marker = None
        self._time_marker_time_s = None
        self._setting_range = False
        self._updating_window_spin = False
        self._stable_y_range = None
        self._curves = []
        self._loader_thread = None
        self._loader_worker = None

        if pg is None:
            err = QtWidgets.QLabel(f"PyQtGraph is not available:\n{_PYQTGRAPH_IMPORT_ERROR}")
            err.setWordWrap(True)
            layout.addWidget(err)
            layout.addStretch(1)
            return

        if not self.recipe:
            msg = QtWidgets.QLabel("No sorting_recipe.json found. Create a spike sorting first.")
            msg.setWordWrap(True)
            layout.addWidget(msg)
            layout.addStretch(1)
            return

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(6)
        controls.addWidget(QtWidgets.QLabel("Recording:"))
        self.recording_combo = QtWidgets.QComboBox(self)
        for item in self.recording_items:
            self.recording_combo.addItem(str(item.get("label", "")), item)
        self.recording_combo.currentIndexChanged.connect(self.on_recording_change)
        controls.addWidget(self.recording_combo, stretch=1)

        self.options_btn = QtWidgets.QPushButton("Preprocessing options", self)
        self.channels_btn = QtWidgets.QPushButton("Channels", self)
        self.probe_btn = QtWidgets.QPushButton("Probe", self)
        self.options_btn.clicked.connect(self.open_preprocessing_options)
        self.channels_btn.clicked.connect(self.open_channel_selector)
        self.probe_btn.clicked.connect(self.open_probe_viewer)
        self.channels_btn.setEnabled(False)
        self.probe_btn.setEnabled(False)
        controls.addWidget(self.options_btn)
        controls.addWidget(self.channels_btn)
        controls.addWidget(self.probe_btn)
        layout.addLayout(controls)

        pipeline_summary = self._pipeline_summary_text()
        self.pipeline_label = QtWidgets.QLabel(pipeline_summary)
        self.pipeline_label.setWordWrap(True)
        self.pipeline_label.setStyleSheet("color: #555;")
        layout.addWidget(self.pipeline_label)

        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.view_box = _PreprocessingTraceViewBox(self)
        self.plot_widget = pg.PlotWidget(self, viewBox=self.view_box)
        self.plot_widget.setBackground("w")
        self.plot_widget.showGrid(x=False, y=False)
        self.plot_widget.setMenuEnabled(False)
        try:
            self.plot_widget.setMouseEnabled(x=True, y=False)
        except Exception:
            pass
        self.plot_item = self.plot_widget.getPlotItem()
        try:
            self.plot_item.disableAutoRange()
        except Exception:
            pass
        try:
            self.plot_item.hideButtons()
        except Exception:
            pass
        self.plot_item.setLabel("bottom", "Time", units="s")
        self.plot_item.setLabel("left", "Channels")
        try:
            pg.setConfigOptions(antialias=True)
        except Exception:
            pass

        self.log_view = QtWidgets.QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap if hasattr(QtWidgets.QPlainTextEdit, "LineWrapMode") else QtWidgets.QPlainTextEdit.NoWrap)
        self.log_view.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; "
            "font-size: 10pt; background: white; color: black; "
            "border: 1px solid #cfcfcf;"
        )

        self.plot_stack = QtWidgets.QStackedWidget(self)
        self.plot_stack.addWidget(self.log_view)
        self.plot_stack.addWidget(self.plot_widget)
        layout.addWidget(self.plot_stack, stretch=1)
        nav = QtWidgets.QHBoxLayout()
        self.window_spin = QtWidgets.QDoubleSpinBox(self)
        self.window_spin.setDecimals(3)
        self.window_spin.setRange(0.005, 3600.0)
        self.window_spin.setSingleStep(0.5)
        self.window_spin.setValue(float(self._window_s))
        self.window_spin.setSuffix(" s")
        self.window_spin.setMinimumWidth(95)
        self.window_spin.valueChanged.connect(self.on_window_spin_changed)

        # Use QScrollBar instead of QSlider: the handle/thumb width can represent
        # the current visible window size relative to the full recording duration.
        self.position_slider = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Horizontal if hasattr(QtCore.Qt, "Orientation") else QtCore.Qt.Horizontal, self)
        self.position_slider.setMinimum(0)
        self.position_slider.setMaximum(0)
        self.position_slider.setPageStep(1)
        self.position_slider.setSingleStep(100)
        self.position_slider.setStyleSheet(_native_scrollbar_without_arrow_buttons_stylesheet())
        self.position_slider.sliderReleased.connect(self.on_slider_released)
        nav.addWidget(QtWidgets.QLabel("Window:"))
        nav.addWidget(self.window_spin)
        nav.addWidget(self.position_slider, stretch=1)
        layout.addLayout(nav)

        self._show_plot_log("Ready. Select a recording to load.", clear=True)

        self.reload_timer = QtCore.QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(self._reload_for_visible_range)
        self.plot_item.sigXRangeChanged.connect(self._on_xrange_changed)

        if self.recording_items:
            # Delay the first load until this widget has been inserted in the right panel
            # and Qt had one event-loop pass to paint the log/controls. This avoids a
            # blank right panel when switching between spike-sorting projects.
            QtCore.QTimer.singleShot(0, self.load_current_recording)
        else:
            self.status_label.setText("No recording group found in sorting_recipe.json.")

    # ---------- display/log helpers ----------

    def _show_plot_log(self, message: str = "", clear: bool = False):
        try:
            self.plot_stack.setCurrentWidget(self.log_view)
        except Exception:
            pass
        if message:
            self._append_log(message, clear=clear)
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

    def _show_trace_plot(self):
        try:
            self.plot_stack.setCurrentWidget(self.plot_widget)
        except Exception:
            pass

    def _append_log(self, message: str, clear: bool = False):
        try:
            if clear:
                self.log_view.clear()
            self.log_view.appendPlainText(str(message))
            scrollbar = self.log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            # Force pending paint events while long lazy preprocessing objects are built.
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

    # ---------- recipe/pipeline ----------

    def _pipeline_summary_text(self):
        pipeline = self.pipeline or {}
        enabled = pipeline.get("enabled_pipeline", []) or []
        if not enabled:
            return "Preprocessing pipeline: no step enabled."
        labels = []
        for step in enabled:
            try:
                labels.append(f"{step.get('order', '')}: {step.get('name', '')}")
            except Exception:
                pass
        return "Enabled: " + "  |  ".join([x for x in labels if x.strip()])

    def _get_project_root(self):
        gui = self.sorting_info.get("GUI_instance", None)
        if gui is not None and hasattr(gui, "project_root"):
            return Path(gui.project_root)
        path = self.sorting_info.get("path", None)
        if path is not None:
            try:
                return Path(path).resolve().parents[2]
            except Exception:
                pass
        return None

    @staticmethod
    def _slug(text):
        import re
        text = str(text or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        return text.strip("_") or "step"

    @staticmethod
    def _to_bool(value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "checked"}

    def _discover_preprocessing_specs(self):
        if discover_preprocessings is None:
            return {}
        try:
            return discover_preprocessings(project_root=self._get_project_root()) or {}
        except Exception as exc:
            try:
                self.status_label.setText(f"Could not discover preprocessing scripts: {exc}")
            except Exception:
                pass
            return {}

    def _make_preprocessing_steps(self):
        steps = []
        used = set()
        scope_order = {"package": 0, "project": 1}
        items = list((self.preprocessing_specs or {}).items())
        items.sort(key=lambda item: (
            scope_order.get(str(item[1].get("source_scope", "")), 99),
            int(item[1].get("discovery_order", 999999)),
            str(item[0]).lower(),
        ))
        for display_index, (name, spec) in enumerate(items, start=1):
            spec = dict(spec or {})
            base = f"{spec.get('source_scope', 'unknown')}_{self._slug(name)}"
            key = base
            n = 2
            while key in used:
                key = f"{base}_{n}"
                n += 1
            used.add(key)
            steps.append({
                "key": key,
                "display_index": display_index,
                "name": str(spec.get("name", name)),
                "description": str(spec.get("description", "")),
                "source": str(spec.get("source_scope", "")),
                "source_file": spec.get("source_file"),
                "func": spec.get("func"),
                "default_enabled": self._to_bool(spec.get("default", False)),
                "default_order": spec.get("order", display_index),
                "params": dict(spec.get("params", {}) or {}),
                "debug_mode": bool(spec.get("debug_mode", False)),
                "raw_spec": spec,
            })
        return steps

    def _pipeline_path(self):
        path = self.sorting_info.get("path", None)
        if path is None:
            return None
        return Path(path) / "preprocessing_pipeline.json"

    def _write_pipeline_config(self, config: dict):
        path = self._pipeline_path()
        if path is None:
            raise RuntimeError("Missing sorting folder path.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _pp_json_dump(config, f, indent=2, ensure_ascii=False)
        self.pipeline = dict(config or {})
        self.pipeline_label.setText(self._pipeline_summary_text())
        return path

    def _runtime_pipeline_from_saved(self):
        saved = self.pipeline or {}
        saved_steps = [s for s in (saved.get("pipeline", []) or []) if isinstance(s, dict)]
        by_key = {str(s.get("key")): s for s in saved_steps if s.get("key")}
        by_name = {str(s.get("name", "")): s for s in saved_steps}
        runtime_pipeline = []
        for step in self.preprocessing_steps:
            key = str(step.get("key"))
            saved_step = by_key.get(key) or by_name.get(str(step.get("name", "")))
            if saved_step is None:
                continue
            item = dict(saved_step)
            item["func"] = step.get("func")
            item["debug_mode"] = bool(step.get("debug_mode", False) or item.get("debug_mode", False))
            item.setdefault("display_index", step.get("display_index", 999999))
            runtime_pipeline.append(item)
        runtime_pipeline.sort(key=lambda d: (int(d.get("order", 999999)), int(d.get("display_index", 999999))))
        return {
            "pipeline": runtime_pipeline,
            "enabled_pipeline": [s for s in runtime_pipeline if bool(s.get("enabled"))],
        }

    def _apply_pipeline_to_recording(self, recording, runtime_config: dict):
        processed = recording
        enabled = list((runtime_config or {}).get("enabled_pipeline", []) or [])
        if not enabled:
            return processed
        self._append_log("Applying active preprocessing pipeline for preview...")
        for step in enabled:
            name = step.get("name", "preprocessing step")
            func = step.get("func")
            params = dict(step.get("params", {}) or {})
            self._append_log(f"Applying preprocessing: {name}")
            if step.get("debug_mode", False):
                self._append_log("Debug mode enabled for this preprocessing step.")
            self._append_log(f"Parameters: {params}")
            if not callable(func):
                self._append_log(f"Skipped {name}: function is not callable")
                continue
            with contextlib.redirect_stdout(_LogWriter(self)), contextlib.redirect_stderr(_LogWriter(self)):
                out = func(processed, **params)
            processed = out[0] if isinstance(out, tuple) else out
        self._append_log("Preprocessing preview ready.")
        return processed

    def _raw_recording_for_label(self, label: str):
        return self.recording_cache.get(str(label))

    def _apply_saved_pipeline_to_current_raw(self, *, reload_buffer=True):
        label = str(self.current_label or self.recording_combo.currentText()).strip()
        raw = self._raw_recording_for_label(label)
        if raw is None:
            raw = self.current_recording
        if raw is None:
            return
        runtime_config = self._runtime_pipeline_from_saved()
        self.current_recording = self._apply_pipeline_to_recording(raw, runtime_config)
        self._update_channels_from_recording(self.current_recording)
        self._buffer_channels_key = None
        self._stable_y_range = None
        if not self._user_gain_touched:
            self._trace_gain = 1.2
        if reload_buffer:
            try:
                x0, _x1 = self.plot_item.viewRange()[0]
            except Exception:
                x0 = 0.0
            self._load_buffer_for_time(float(x0), keep_xrange=False)
            self._show_trace_plot()
            self.channels_btn.setEnabled(True)
            self.probe_btn.setEnabled(True)

    def open_preprocessing_options(self):
        dlg = _PreprocessingOptionsDialog(self, self.preprocessing_steps, self.pipeline)
        result = dlg.exec() if hasattr(dlg, "exec") else dlg.exec_()
        accepted = QtWidgets.QDialog.DialogCode.Accepted if hasattr(QtWidgets.QDialog, "DialogCode") else QtWidgets.QDialog.Accepted
        if result != accepted:
            return
        serializable = dlg.serializable_config()
        runtime_config = dlg.runtime_config()
        try:
            path = self._write_pipeline_config(serializable)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Preprocessing", f"Could not save preprocessing pipeline:\n{exc}")
            return
        self._show_plot_log("Saved preprocessing pipeline:", clear=True)
        self._append_log(str(path))
        for step in serializable.get("pipeline", []) or []:
            self._append_log(
                f"- order={step.get('order')} | enabled={step.get('enabled')} | "
                f"name={step.get('name')} | params={step.get('params')}"
            )
        raw = self._raw_recording_for_label(str(self.current_label or self.recording_combo.currentText()).strip())
        if raw is not None:
            try:
                self.current_recording = self._apply_pipeline_to_recording(raw, runtime_config)
                self._update_channels_from_recording(self.current_recording)
                self.selected_channel_ids = list(self.channel_ids)
                self._buffer_channels_key = None
                self._stable_y_range = None
                self._user_gain_touched = False
                self._load_buffer_for_time(0.0, keep_xrange=False)
                self._show_trace_plot()
                self.channels_btn.setEnabled(True)
                self.probe_btn.setEnabled(True)
            except Exception as exc:
                self._append_log("ERROR while applying preprocessing:")
                self._append_log(traceback.format_exc())
                QtWidgets.QMessageBox.critical(self, "Preprocessing", f"Could not apply preprocessing:\n{exc}")
        self.status_label.setText(self._pipeline_summary_text())

    def _discover_recording_items(self):
        recipe = self.recipe or {}
        groups = recipe.get("recording_groups", []) or recipe.get("groups", []) or []
        items = []
        seen = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
            if not files:
                continue
            label = str(group.get("label", "")).strip() or Path(files[0]).stem
            base = label
            i = 2
            while label in seen:
                label = f"{base}_{i}"
                i += 1
            seen.add(label)
            item = dict(group)
            item.update({
                "label": label,
                "files": files,
                "subject": str(group.get("subject", "")).strip(),
                "concatenated": bool(group.get("concatenated", len(files) > 1)),
                "mode": "recipe",
            })
            items.append(item)
        return items

    # ---------- loading ----------

    def _current_item_and_label(self):
        idx = self.recording_combo.currentIndex()
        if idx < 0:
            return None, ""
        item = self.recording_combo.itemData(idx)
        label = self.recording_combo.currentText().strip()
        return item, label

    def on_recording_change(self, _index=None):
        self.current_recording = None
        self.current_label = ""
        self.channel_ids = []
        self.selected_channel_ids = []
        self._buffer_start = 0
        self._buffer_end = 0
        self._buffer_x = None
        self._buffer_y = None
        self._buffer_raw = None
        self._buffer_offsets = None
        self._stable_y_range = None
        self._trace_gain = 1.2
        self._user_gain_touched = False
        self._time_marker = None
        self._time_marker_time_s = None
        self._set_window_spin_value(self._window_s)
        self.channels_btn.setEnabled(False)
        self.probe_btn.setEnabled(False)
        self._clear_plot()
        try:
            label = self.recording_combo.currentText().strip()
        except Exception:
            label = ""
        self._show_plot_log(f"Preparing recording: {label}" if label else "Preparing recording...", clear=True)
        # Let the new right-panel view render before launching the load.
        QtCore.QTimer.singleShot(0, self.load_current_recording)

    def load_current_recording(self):
        item, label = self._current_item_and_label()
        if not item or not label:
            return

        if label in self.recording_cache:
            self._show_plot_log(f"Loading recording: {label}", clear=True)
            self._append_log("Using cached recording.")
            self._finish_recording_load(label, self.recording_cache[label], "loaded from cache")
            return

        self._show_plot_log(f"Loading recording: {label}", clear=True)
        self._set_busy(True, f"Loading recording: {label} ...")
        thread = QtCore.QThread(self)
        worker = _RecordingLoadWorker(label, item, self.recipe)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._append_log)
        worker.finished.connect(self._on_worker_finished)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._loader_thread = thread
        self._loader_worker = worker
        thread.start()

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = bool(busy)
        # Load/Refresh buttons were removed; recording selection now auto-loads.
        self.recording_combo.setEnabled(not busy)
        if hasattr(self, "channels_btn"):
            self.channels_btn.setEnabled((not busy) and self.current_recording is not None)
        if hasattr(self, "probe_btn"):
            self.probe_btn.setEnabled((not busy) and self.current_recording is not None)
        if text:
            self.status_label.setText(text)
            self._append_log(text)
        self._emit_busy_state(bool(busy), text or ("Preprocessing running..." if busy else "Preprocessing ready."))

    def _on_worker_finished(self, label, recording, info):
        self.recording_cache.clear()
        self.recording_cache[str(label)] = recording
        self._finish_recording_load(str(label), recording, str(info))

    def _on_worker_failed(self, label, error_text):
        self._set_busy(False, f"Failed to load recording: {label}")
        self._show_plot_log(f"ERROR while loading {label}:", clear=False)
        self._append_log(str(error_text))
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Critical)
        box.setWindowTitle("Preprocessing")
        box.setText("Could not load recording.")
        box.setDetailedText(str(error_text))
        box.exec()

    def _finish_recording_load(self, label, recording, info=""):
        self._set_busy(False)
        self.recording_cache[str(label)] = recording
        self.current_recording = recording
        self.current_label = str(label)
        self.status_label.setText(f"{label}: {info}")
        self._set_window_spin_value(self._window_s)
        self._append_log(f"Loaded raw recording: {label}")
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass
        try:
            runtime_config = self._runtime_pipeline_from_saved()
            if runtime_config.get("enabled_pipeline"):
                self.current_recording = self._apply_pipeline_to_recording(recording, runtime_config)
        except Exception:
            self._append_log("ERROR while applying active preprocessing pipeline:")
            self._append_log(traceback.format_exc())
        self._update_channels_from_recording(self.current_recording)
        self.selected_channel_ids = list(self.channel_ids)
        self._update_slider_range(value_s=0.0)
        self._append_log("Reading initial display buffer...")
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass
        self._load_buffer_for_time(0.0, keep_xrange=False)
        self._show_trace_plot()
        self.channels_btn.setEnabled(True)
        self.probe_btn.setEnabled(True)

    # ---------- channels/probe ----------

    def _update_channels_from_recording(self, recording):
        try:
            self.channel_ids = list(recording.get_channel_ids())
        except Exception:
            self.channel_ids = []
        # Display channels in physical/probe proximity order rather than raw
        # channel-id order. This makes the stacked traces follow the probe
        # geometry, so neighboring traces are neighboring contacts.
        self.channel_ids = self._order_channels_by_proximity(self.channel_ids)
        self.selected_channel_ids = list(self.channel_ids)

    def _channel_location_map(self):
        """Return {str(channel_id): location} when probe locations are available."""
        rec = self.current_recording
        if rec is None:
            return {}
        try:
            channel_ids = list(rec.get_channel_ids())
            locs = np.asarray(rec.get_channel_locations(), dtype=float)
        except Exception:
            return {}
        if locs.ndim != 2 or locs.shape[0] != len(channel_ids) or locs.shape[0] == 0:
            return {}
        out = {}
        for ch, loc in zip(channel_ids, locs):
            loc = np.asarray(loc, dtype=float).ravel()
            if loc.size == 0 or not np.all(np.isfinite(loc)):
                continue
            out[str(ch)] = loc
        return out

    def _order_channels_by_proximity(self, channels):
        """Sort channels by physical probe position when coordinates exist.

        For a linear/staggered probe this follows the long spatial axis. For
        grids it sorts by the axis with the largest spatial spread, then by the
        secondary axis. If no valid locations are available, the input order is
        preserved.
        """
        channels = list(channels or [])
        if len(channels) <= 1:
            return channels
        loc_map = self._channel_location_map()
        if not loc_map:
            return channels

        sortable = []
        missing = []
        for idx, ch in enumerate(channels):
            loc = loc_map.get(str(ch))
            if loc is None:
                missing.append((idx, ch))
            else:
                sortable.append((idx, ch, loc))
        if len(sortable) <= 1:
            return channels

        # Use only dimensions that actually vary. Most probes expose x/y or
        # x/y/z; for plotting order we only need a stable spatial ordering.
        coords = np.vstack([v[2] for v in sortable])
        if coords.ndim != 2 or coords.shape[0] <= 1:
            return channels
        ranges = np.nanmax(coords, axis=0) - np.nanmin(coords, axis=0)
        valid_dims = [i for i, r in enumerate(ranges) if np.isfinite(r) and r > 0]
        if not valid_dims:
            return channels
        primary = max(valid_dims, key=lambda i: float(ranges[i]))
        secondary_dims = [i for i in valid_dims if i != primary]
        secondary = secondary_dims[0] if secondary_dims else None

        def sort_key(item):
            original_idx, ch, loc = item
            main = float(loc[primary])
            side = float(loc[secondary]) if secondary is not None and secondary < len(loc) else 0.0
            return (main, side, _natural_text_key(str(ch)), original_idx)

        ordered = [ch for _, ch, _ in sorted(sortable, key=sort_key)]

        # Append channels without coordinates at the end in their original order.
        # This avoids losing channels when a recording has partial probe metadata.
        ordered.extend([ch for _, ch in sorted(missing, key=lambda x: x[0])])
        return ordered

    def open_channel_selector(self):
        if self.current_recording is None:
            return
        dlg = _ChannelSelectorDialog(self, self.channel_ids, self.selected_channel_ids)
        result = dlg.exec() if hasattr(dlg, "exec") else dlg.exec_()
        accepted = QtWidgets.QDialog.DialogCode.Accepted if hasattr(QtWidgets.QDialog, "DialogCode") else QtWidgets.QDialog.Accepted
        if result == accepted:
            selected = dlg.selected_channel_ids()
            if not selected:
                QtWidgets.QMessageBox.information(self, "Channels", "At least one channel must be selected.")
                return
            self.selected_channel_ids = self._order_channels_by_proximity(selected)
            self._user_gain_touched = False
            self._stable_y_range = None
            self._buffer_channels_key = None
            self._load_buffer_for_visible_range(keep_xrange=True)

    def open_probe_viewer(self):
        if self.current_recording is None:
            return
        dlg = _ProbeDialog(self, self.current_recording, selected_ids=self.selected_channel_ids)
        dlg.show()
        self._probe_dialog = dlg

    # ---------- plotting ----------

    def _clear_plot(self):
        try:
            self.plot_item.clear()
        except Exception:
            pass
        self._curves = []
        # plot_item.clear() also removes the marker item from the scene.
        # Keep _time_marker_time_s separately so _render_buffer() can recreate it.
        self._time_marker = None

    def _get_fs_and_frames(self):
        rec = self.current_recording
        if rec is None:
            return 1.0, 0

        fs = 1.0
        try:
            fs = _safe_float(rec.get_sampling_frequency(), 1.0)
        except Exception:
            fs = 1.0
        if fs <= 0:
            fs = 1.0

        n = 0
        # SpikeInterface changed naming across versions and some wrappers expose
        # only one of these methods. Try the common single-segment methods first,
        # then fall back to summing segments.
        for method_name in ("get_num_frames", "get_num_samples", "get_total_samples"):
            meth = getattr(rec, method_name, None)
            if meth is None:
                continue
            for call in (
                lambda m=meth: m(segment_index=0),
                lambda m=meth: m(),
            ):
                try:
                    val = int(call())
                    if val > 0:
                        return fs, val
                except Exception:
                    pass

        try:
            n_segments = int(rec.get_num_segments())
        except Exception:
            n_segments = 0
        if n_segments > 0:
            total = 0
            for seg in range(n_segments):
                got = None
                for method_name in ("get_num_frames", "get_num_samples"):
                    meth = getattr(rec, method_name, None)
                    if meth is None:
                        continue
                    try:
                        got = int(meth(segment_index=seg))
                        break
                    except Exception:
                        pass
                if got is not None and got > 0:
                    total += got
            if total > 0:
                n = total

        return fs, max(0, int(n))

    def _duration_s(self):
        fs, n = self._get_fs_and_frames()
        return n / fs if fs > 0 else 0.0

    def _set_window_spin_value(self, value_s: float):
        if not hasattr(self, "window_spin"):
            return
        try:
            value_s = float(value_s)
            if not math.isfinite(value_s) or value_s <= 0:
                return
            dur = self._duration_s()
            max_value = max(0.005, dur) if dur > 0 else 3600.0
            self._updating_window_spin = True
            self.window_spin.blockSignals(True)
            self.window_spin.setMaximum(max_value)
            self.window_spin.setValue(max(0.005, min(max_value, value_s)))
            self.window_spin.blockSignals(False)
        except Exception:
            try:
                self.window_spin.blockSignals(False)
            except Exception:
                pass
        finally:
            self._updating_window_spin = False

    def on_window_spin_changed(self, value_s: float):
        if self._updating_window_spin or self.current_recording is None:
            return
        try:
            new_width = float(value_s)
        except Exception:
            return
        dur = self._duration_s()
        if dur <= 0:
            return
        new_width = max(0.005, min(float(dur), new_width))
        try:
            x0, x1 = self.plot_item.viewRange()[0]
            center = 0.5 * (float(x0) + float(x1))
        except Exception:
            center = self.position_slider.value() / 1000.0 + 0.5 * float(self._window_s)
        new_start = max(0.0, min(float(dur) - new_width, center - 0.5 * new_width))
        new_stop = new_start + new_width
        self._window_s = float(new_width)
        self._update_slider_range(value_s=new_start)
        self._setting_range = True
        try:
            self.plot_item.setXRange(float(new_start), float(new_stop), padding=0)
        finally:
            self._setting_range = False
        self._set_slider_to_time(new_start)
        self._maybe_schedule_reload_from_current_range(delay_ms=20)
        self._restore_y_range()
        self.status_label.setText(f"{self.current_label}: window {self._window_s:.3f} s")

    def _update_slider_range(self, value_s: Optional[float] = None):
        dur = self._duration_s()
        dur_ms = max(1, int(round(max(0.0, dur) * 1000.0)))
        window_ms = max(1, int(round(max(0.005, min(max(0.005, dur), float(self._window_s))) * 1000.0)))
        max_start = max(0, dur_ms - window_ms)
        if value_s is None:
            value = self.position_slider.value() if self.position_slider.maximum() > 0 else 0
        else:
            value = int(round(max(0.0, float(value_s)) * 1000.0))
        value = max(0, min(max_start, value))
        self.position_slider.blockSignals(True)
        self.position_slider.setMinimum(0)
        self.position_slider.setMaximum(max_start)
        # QScrollBar uses pageStep to determine the visual handle/thumb width.
        # So when the window is large, the handle is large; when zoomed in, it
        # becomes smaller, matching the displayed portion of the recording.
        self.position_slider.setPageStep(max(1, window_ms))
        self.position_slider.setSingleStep(max(1, min(window_ms // 10, 1000)))
        self.position_slider.setValue(value)
        self.position_slider.blockSignals(False)

    def _read_traces(self, start_frame: int, end_frame: int, channel_ids):
        rec = self.current_recording
        if rec is None:
            return np.empty((0, 0), dtype=float)
        try:
            traces = rec.get_traces(start_frame=int(start_frame), end_frame=int(end_frame), channel_ids=list(channel_ids))
        except TypeError:
            traces = rec.get_traces(start_frame=int(start_frame), end_frame=int(end_frame), channel_ids=list(channel_ids), segment_index=0)
        traces = np.asarray(traces)
        if traces.ndim == 1:
            traces = traces[:, None]
        return traces.astype(float, copy=False)

    def _decimate_for_display(self, x, y):
        max_points = 180_000
        if y.size == 0 or y.shape[0] <= max_points:
            return x, y
        step = int(math.ceil(y.shape[0] / max_points))
        n = (y.shape[0] // step) * step
        if n <= 0:
            return x[::step], y[::step]
        yy = y[:n].reshape(-1, step, y.shape[1]).mean(axis=1)
        xx = x[:n].reshape(-1, step).mean(axis=1)
        return xx, yy

    def _make_display_traces(self, traces):
        if traces.size == 0:
            return traces, 1.0, np.array([], dtype=float)
        low = np.nanpercentile(traces, 2, axis=0)
        high = np.nanpercentile(traces, 98, axis=0)
        spread = np.nanmedian(high - low)
        if not math.isfinite(float(spread)) or spread <= 0:
            spread = np.nanstd(traces)
        if not math.isfinite(float(spread)) or spread <= 0:
            spread = 1.0
        spacing = float(spread) * 4.0
        offsets = np.arange(traces.shape[1], dtype=float) * spacing
        return traces, spacing, offsets

    def _estimate_initial_trace_gain(self, traces, spacing):
        """Choose a useful first gain from the loaded signal itself.

        The previous fixed gain was often too small for low-amplitude / low-rate
        recordings, so the user had to drag the gain a little before seeing
        details.  This keeps the channels separated but starts with a visible
        waveform amplitude.
        """
        try:
            arr = np.asarray(traces, dtype=float)
            if arr.size == 0:
                return float(self._trace_gain)
            low = np.nanpercentile(arr, 2, axis=0)
            high = np.nanpercentile(arr, 98, axis=0)
            p2p = float(np.nanmedian(high - low))
            if not math.isfinite(p2p) or p2p <= 0:
                p2p = float(np.nanstd(arr))
            if not math.isfinite(p2p) or p2p <= 0:
                return float(self._trace_gain)
            # Keep the first view conservative. A target close to channel spacing
            # makes preprocessed traces look over-amplified and crowded. The user
            # can still increase gain with right-drag, but the automatic gain should
            # start readable and stable.
            target = float(spacing) * 0.30
            gain = target / p2p
            return max(0.05, min(3.0, float(gain)))
        except Exception:
            return float(self._trace_gain)

    def _load_buffer_for_visible_range(self, keep_xrange=True):
        try:
            x0, x1 = self.plot_item.viewRange()[0]
        except Exception:
            x0, x1 = 0.0, self._window_s
        self._load_buffer_for_time(float(x0), keep_xrange=keep_xrange, visible_range=(float(x0), float(x1)))

    def _load_buffer_for_time(self, start_s: float, keep_xrange: bool, visible_range=None):
        rec = self.current_recording
        if rec is None:
            return
        fs, n_frames = self._get_fs_and_frames()
        if n_frames <= 0:
            return
        visible_start = max(0.0, float(start_s))
        visible_stop = visible_start + self._window_s
        if visible_range is not None:
            visible_start = max(0.0, float(visible_range[0]))
            visible_stop = max(visible_start + 1e-6, float(visible_range[1]))
            self._window_s = max(0.005, float(visible_stop - visible_start))
            self._set_window_spin_value(self._window_s)
        margin = self._window_s * self._buffer_margin_windows
        buf_start_s = max(0.0, visible_start - margin)
        buf_stop_s = min(self._duration_s(), visible_stop + margin)
        start_frame = max(0, int(math.floor(buf_start_s * fs)))
        end_frame = min(n_frames, int(math.ceil(buf_stop_s * fs)))
        if end_frame <= start_frame:
            return

        channels = self._order_channels_by_proximity(list(self.selected_channel_ids or self.channel_ids))
        if not channels:
            return
        # Keep the public selected list in the same order used for plotting so
        # axis labels and curves always match.
        if self.selected_channel_ids:
            self.selected_channel_ids = list(channels)
        channels_key = tuple(str(c) for c in channels)

        self.status_label.setText(f"Reading {self.current_label}: {buf_start_s:.2f}-{buf_stop_s:.2f} s ...")
        if hasattr(self, "plot_stack") and hasattr(self, "log_view") and self.plot_stack.currentWidget() is self.log_view:
            self._append_log(f"Reading display buffer: {buf_start_s:.2f}-{buf_stop_s:.2f} s")
        try:
            traces = self._read_traces(start_frame, end_frame, channels)
        except Exception as exc:
            self.status_label.setText(f"Could not read traces: {exc}")
            return

        # Keep the full-resolution loaded buffer in memory.  PyQtGraph then
        # performs adaptive display downsampling from the original samples, so
        # zooming in recovers the real detail instead of zooming into an already
        # downsampled trace.
        x = (np.arange(start_frame, start_frame + traces.shape[0], dtype=float) / fs)
        raw, spacing, offsets = self._make_display_traces(traces)
        if not getattr(self, "_user_gain_touched", False):
            self._trace_gain = self._estimate_initial_trace_gain(traces, spacing)

        self._buffer_start = start_frame
        self._buffer_end = end_frame
        self._buffer_x = x
        self._buffer_raw = raw
        self._buffer_spacing = float(spacing)
        self._buffer_offsets = offsets
        self._buffer_y = None
        self._buffer_channels_key = channels_key

        # Set the visible X range BEFORE creating/updating the curves.  When
        # curves are created while the view still spans the full loaded buffer,
        # PyQtGraph's adaptive downsampling can choose a very coarse display
        # step and the first plot looks over-smoothed.  A tiny manual gain drag
        # then forced a redraw at the correct X range, which is why details
        # suddenly appeared.  Setting the range first makes the initial display
        # use the same detail level as after a redraw.
        self._setting_range = True
        try:
            max_start = max(0.0, self._duration_s() - max(1e-6, visible_stop - visible_start))
            visible_start = min(max_start, max(0.0, visible_start))
            visible_stop = min(self._duration_s(), max(visible_start + 1e-6, visible_stop))
            self.plot_item.setXRange(visible_start, visible_stop, padding=0)
        finally:
            self._setting_range = False

        self._render_buffer(update_y=self._stable_y_range is None)

        self._set_slider_to_time(visible_start)
        self.status_label.setText(
            f"{self.current_label}: {len(channels)} channel(s), buffer {buf_start_s:.2f}-{buf_stop_s:.2f} s"
        )

    def _compute_display_y_range(self, y, offsets):
        """Return a stable channel-based Y range.

        Do not derive the Y range from signal amplitude. Doing that made the
        view jump during pan/gain changes and sometimes pushed channels outside
        the visible frame. The gain can change the waveform amplitude, but the
        frame itself stays locked to the channel offsets.
        """
        spacing = float(self._buffer_spacing) if self._buffer_spacing else 1.0
        if offsets is None or len(offsets) == 0:
            return (-spacing, spacing)
        offsets = np.asarray(offsets, dtype=float)
        if offsets.size == 1:
            center = float(offsets[0])
            margin = max(spacing * 1.5, 1.0)
            return (center - margin, center + margin)
        margin = max(spacing * 1.4, (float(offsets[-1]) - float(offsets[0])) * 0.04)
        low = float(offsets[0] - margin)
        high = float(offsets[-1] + margin)
        if not math.isfinite(low) or not math.isfinite(high) or high <= low:
            return (float(offsets[0] - spacing), float(offsets[-1] + spacing))
        return (low, high)

    def _current_visible_indices(self, extra_fraction: float = 0.12):
        """Return buffer indices for the current visible x-range.

        The full-resolution buffer stays in memory, but the curves only receive
        the visible part of the buffer plus a small margin.  This avoids the
        PyQtGraph adaptive downsampling cache using the whole 30–40 s buffer for
        the first draw, which is what made the trace look over-smoothed until a
        tiny gain change forced a redraw.
        """
        x = self._buffer_x
        if x is None or len(x) == 0:
            return 0, 0
        try:
            x0, x1 = self.plot_item.viewRange()[0]
        except Exception:
            x0, x1 = float(x[0]), float(x[0]) + float(self._window_s)
        x0 = float(x0)
        x1 = float(x1)
        if not math.isfinite(x0) or not math.isfinite(x1) or x1 <= x0:
            x0 = float(x[0])
            x1 = x0 + float(self._window_s)
        width = max(1e-9, x1 - x0)
        pad = width * float(extra_fraction)
        lo = max(float(x[0]), x0 - pad)
        hi = min(float(x[-1]), x1 + pad)
        i0 = int(np.searchsorted(x, lo, side="left"))
        i1 = int(np.searchsorted(x, hi, side="right"))
        i0 = max(0, min(i0, len(x) - 1))
        i1 = max(i0 + 1, min(i1, len(x)))
        return i0, i1

    def _set_curve_downsampling_for_slice(self, curve, n_points: int):
        """Use full detail for normal visible windows, downsample only if huge."""
        try:
            if int(n_points) <= 350_000:
                curve.setDownsampling(auto=False)
            else:
                curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        except Exception:
            pass

    def _update_curves_for_visible_range(self):
        x = self._buffer_x
        raw = self._buffer_raw
        offsets = self._buffer_offsets
        if x is None or raw is None or raw.size == 0:
            return
        if not getattr(self, "_curves", None):
            return
        i0, i1 = self._current_visible_indices(extra_fraction=0.15)
        if i1 <= i0:
            return
        xs = x[i0:i1]
        ys = raw[i0:i1, :] * float(self._trace_gain) + offsets[None, :]
        n_curves = min(len(self._curves), ys.shape[1])
        for i in range(n_curves):
            try:
                self._curves[i].setData(xs, ys[:, i])
                self._set_curve_downsampling_for_slice(self._curves[i], len(xs))
            except Exception:
                pass

    def _set_time_marker(self, x_s: float):
        """Place/move the red time marker from a short left click."""
        try:
            x_s = float(x_s)
        except Exception:
            return
        if not math.isfinite(x_s):
            return
        dur = self._duration_s()
        if dur > 0:
            x_s = max(0.0, min(float(dur), x_s))
        self._time_marker_time_s = x_s
        self._sync_time_marker()
        try:
            self.status_label.setText(f"{self.current_label}: marker at {x_s:.3f} s")
        except Exception:
            pass

    def _clear_time_marker(self):
        """Remove the red time marker from a short right click."""
        self._time_marker_time_s = None
        marker = getattr(self, "_time_marker", None)
        if marker is not None:
            try:
                self.plot_item.removeItem(marker)
            except Exception:
                pass
        self._time_marker = None
        try:
            self.status_label.setText(f"{self.current_label}: marker removed")
        except Exception:
            pass

    def _sync_time_marker(self):
        """Recreate the marker after plot redraws/buffer reloads."""
        if self._time_marker_time_s is None or pg is None:
            return
        try:
            x_s = float(self._time_marker_time_s)
        except Exception:
            return
        marker = getattr(self, "_time_marker", None)
        if marker is None:
            try:
                marker = pg.InfiniteLine(
                    pos=x_s,
                    angle=90,
                    movable=False,
                    pen=pg.mkPen((220, 0, 0), width=1),
                )
                marker.setZValue(10000)
                self.plot_item.addItem(marker, ignoreBounds=True)
                self._time_marker = marker
            except Exception:
                self._time_marker = None
                return
        else:
            try:
                marker.setValue(x_s)
            except Exception:
                pass

    def _render_buffer(self, update_y=False):
        self._clear_plot()
        x = self._buffer_x
        raw = self._buffer_raw
        offsets = self._buffer_offsets
        if x is None or raw is None or raw.size == 0:
            return
        if offsets is None or len(offsets) != raw.shape[1]:
            offsets = np.arange(raw.shape[1], dtype=float) * float(self._buffer_spacing)
            self._buffer_offsets = offsets

        self._buffer_y = None

        pen = pg.mkPen((0, 0, 0), width=1, cosmetic=True)
        self._curves = []
        for _i in range(raw.shape[1]):
            curve = self.plot_item.plot([], [], pen=pen)
            self._curves.append(curve)

        self._update_channel_axis_ticks(offsets)

        if update_y or self._stable_y_range is None:
            # Y frame is channel-offset based, not amplitude/downsampling based.
            self._stable_y_range = self._compute_display_y_range(None, offsets)
        if self._stable_y_range is not None:
            self.plot_item.setYRange(float(self._stable_y_range[0]), float(self._stable_y_range[1]), padding=0)

        # Important: draw only the current visible slice from the full-resolution
        # buffer.  Panning updates this slice from memory; loading from disk only
        # happens when the visible range approaches the edge of the buffer.
        self._update_curves_for_visible_range()
        self._sync_time_marker()

    def _restore_y_range(self):
        if self._stable_y_range is None:
            return
        try:
            self.plot_item.setYRange(float(self._stable_y_range[0]), float(self._stable_y_range[1]), padding=0)
        except Exception:
            pass

    def _update_channel_axis_ticks(self, offsets):
        """Show channel names on the left axis at their trace offsets."""
        try:
            axis = self.plot_item.getAxis("left")
        except Exception:
            return
        try:
            channels = list(self.selected_channel_ids or self.channel_ids)
            if offsets is None or len(channels) == 0:
                axis.setTicks(None)
                return
            n = min(len(channels), len(offsets))
            ticks = [(float(offsets[i]), str(channels[i])) for i in range(n)]
            axis.setTicks([ticks])
            axis.setStyle(tickTextOffset=8)
        except Exception:
            pass

    def _apply_right_drag(self, dx_pixels: float = 0.0, dy_pixels: float = 0.0):
        """Right-drag interaction: horizontal changes x-scale, vertical changes gain."""
        did_gain = False
        did_scale = False

        # Vertical movement: trace gain.
        if abs(float(dy_pixels)) >= 0.5:
            did_gain = self._apply_gain_drag(dy_pixels, restore_xrange=False)

        # Horizontal movement: x-scale/window size around the current center.
        # Drag right -> wider time window. Drag left -> narrower time window.
        if abs(float(dx_pixels)) >= 0.5:
            did_scale = self._apply_x_scale_drag(dx_pixels)

        if did_gain or did_scale:
            self._maybe_schedule_reload_from_current_range(delay_ms=35)
            try:
                x0, x1 = self.plot_item.viewRange()[0]
                self._set_slider_to_time(float(x0))
            except Exception:
                pass
            self._restore_y_range()
            parts = []
            if did_scale:
                try:
                    x0, x1 = self.plot_item.viewRange()[0]
                    parts.append(f"window {float(x1) - float(x0):.3f} s")
                except Exception:
                    pass
            if did_gain:
                parts.append(f"gain ×{self._trace_gain:.2f}")
            if parts:
                self.status_label.setText(f"{self.current_label}: " + ", ".join(parts))

    def _apply_x_scale_drag(self, dx_pixels: float):
        try:
            x0, x1 = self.plot_item.viewRange()[0]
            x0 = float(x0)
            x1 = float(x1)
        except Exception:
            return False
        width = max(1e-6, x1 - x0)
        center = 0.5 * (x0 + x1)
        try:
            factor = math.exp(float(dx_pixels) * 0.010)
        except Exception:
            factor = 1.0
        if not math.isfinite(factor) or factor <= 0:
            return False
        dur = max(1e-6, self._duration_s())
        min_width = max(0.005, 10.0 / max(1.0, self._get_fs_and_frames()[0]))
        max_width = max(min_width, dur)
        new_width = max(min_width, min(max_width, width * factor))
        new_start = center - new_width * 0.5
        new_stop = center + new_width * 0.5
        if new_start < 0:
            new_stop -= new_start
            new_start = 0.0
        if new_stop > dur:
            shift = new_stop - dur
            new_start = max(0.0, new_start - shift)
            new_stop = dur
        if new_stop <= new_start:
            return False
        self._window_s = float(new_stop - new_start)
        self._set_window_spin_value(self._window_s)
        self._update_slider_range(value_s=new_start)
        self._setting_range = True
        try:
            self.plot_item.setXRange(float(new_start), float(new_stop), padding=0)
        finally:
            self._setting_range = False
        return True

    def _apply_gain_drag(self, dy_pixels: float, restore_xrange: bool = True):
        # Drag up increases gain, drag down decreases it. Exponential scaling
        # makes it smooth and independent of the absolute current gain.
        try:
            factor = math.exp(-float(dy_pixels) * 0.012)
        except Exception:
            factor = 1.0
        if not math.isfinite(factor) or factor <= 0:
            return False
        old_gain = float(self._trace_gain)
        self._trace_gain = max(0.05, min(50.0, old_gain * factor))
        self._user_gain_touched = True
        if abs(self._trace_gain - old_gain) < 1e-12:
            return False
        x_range = None
        if restore_xrange:
            try:
                x_range = tuple(float(v) for v in self.plot_item.viewRange()[0])
            except Exception:
                x_range = None
        self._render_buffer(update_y=True)
        if restore_xrange and x_range is not None:
            try:
                self.plot_item.setXRange(float(x_range[0]), float(x_range[1]), padding=0)
            except Exception:
                pass
            self.status_label.setText(f"{self.current_label}: gain ×{self._trace_gain:.2f}")
        return True

    def _maybe_schedule_reload_from_current_range(self, delay_ms: int = 80):
        if self._setting_range or self.current_recording is None:
            return
        try:
            x0, x1 = self.plot_item.viewRange()[0]
        except Exception:
            return
        fs, _n = self._get_fs_and_frames()
        if fs <= 0:
            return
        buf_start_s = self._buffer_start / fs
        buf_end_s = self._buffer_end / fs
        width = max(1e-6, float(x1) - float(x0))
        edge = width * self._buffer_edge_fraction
        if float(x0) < buf_start_s + edge or float(x1) > buf_end_s - edge:
            self.reload_timer.start(max(1, int(delay_ms)))

    def _on_xrange_changed(self, _plot_item, _range):
        if self._setting_range or self.current_recording is None:
            return
        try:
            x0, x1 = self.plot_item.viewRange()[0]
        except Exception:
            return
        width = max(0.005, float(x1) - float(x0))
        if math.isfinite(width):
            self._window_s = width
            self._set_window_spin_value(width)
        self._set_slider_to_time(float(x0))
        self._update_curves_for_visible_range()
        self._restore_y_range()
        self._maybe_schedule_reload_from_current_range(delay_ms=80)

    def _reload_for_visible_range(self):
        self._load_buffer_for_visible_range(keep_xrange=True)

    def _set_slider_to_time(self, start_s: float):
        try:
            value = int(round(max(0.0, float(start_s)) * 1000.0))
            value = max(self.position_slider.minimum(), min(self.position_slider.maximum(), value))
            self.position_slider.blockSignals(True)
            self.position_slider.setValue(value)
            self.position_slider.blockSignals(False)
        except Exception:
            pass

    def on_slider_released(self):
        start_s = self.position_slider.value() / 1000.0
        self._load_buffer_for_time(start_s, keep_xrange=False)

    def step_position(self, direction: int):
        try:
            x0, x1 = self.plot_item.viewRange()[0]
        except Exception:
            x0, x1 = 0.0, self._window_s
        width = max(1e-6, float(x1) - float(x0))
        new_start = max(0.0, float(x0) + float(direction) * width * 0.8)
        max_start = max(0.0, self._duration_s() - width)
        new_start = min(max_start, new_start)
        self._setting_range = True
        try:
            self.plot_item.setXRange(new_start, new_start + width, padding=0)
        finally:
            self._setting_range = False
        self._set_slider_to_time(new_start)
        self._load_buffer_for_visible_range(keep_xrange=True)

    def refresh_current_plot(self):
        self._stable_y_range = None
        self._load_buffer_for_visible_range(keep_xrange=True)


class SpikeSortingSortingView(_BaseSpikeSortingRightView):
    title = "Sorting"

    ALL_RECORDINGS_LABEL = "All"

    def build_body(self, layout):
        self.recipe = self._read_recipe() or {}
        self.pipeline = self._read_pipeline() or {"pipeline": [], "enabled_pipeline": []}
        self.sorting_path = Path(self.sorting_info.get("path")) if self.sorting_info.get("path") is not None else None
        self.preprocessing_specs = self._discover_preprocessing_specs()
        self.preprocessing_steps = self._make_sorting_preprocessing_steps()
        self.saved_config = self._load_sorting_config()
        self.sorter_params_by_name = {}
        self._loading_configuration_editor = False
        self._refreshing_configuration_table = False
        self.sorting_configurations = self._initial_sorting_configurations()
        saved_active_id = str(self.saved_config.get("active_configuration_id", "") or "").strip()
        known_ids = {str(c.get("id", "")) for c in self.sorting_configurations}
        self.active_configuration_id = saved_active_id if saved_active_id in known_ids else (
            str(self.sorting_configurations[0].get("id", "")) if self.sorting_configurations else ""
        )
        self._thread = None
        self._worker = None
        self._busy = False

        if not self.recipe:
            msg = QtWidgets.QLabel("No sorting_recipe.json found. Create a spike sorting first.")
            msg.setWordWrap(True)
            layout.addWidget(msg)
            layout.addStretch(1)
            return

        controls = QtWidgets.QGroupBox("Sorting run", self)
        controls.setStyleSheet(
            "QGroupBox { font-weight: 700; }"
            "QPushButton#primaryRunButton {"
            "  background: #1f6feb; color: white; font-weight: 700; font-size: 13px;"
            "  border-radius: 6px; padding: 6px 18px;"
            "}"
            "QPushButton#primaryRunButton:hover { background: #1557bd; }"
            "QPushButton#secondaryActionButton { padding: 6px 10px; }"
        )
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(10, 10, 10, 10)
        controls_layout.setSpacing(10)

        self.sorter_combo = QtWidgets.QComboBox(controls)
        self.sorter_combo.addItems(self._available_sorters())
        active_configuration = self._active_sorting_configuration()
        selected_sorter = str((active_configuration or {}).get("sorter_name", "") or self.saved_config.get("selected_sorter", "")).strip()
        if selected_sorter and self.sorter_combo.findText(selected_sorter) < 0:
            self.sorter_combo.addItem(selected_sorter)
        if selected_sorter:
            self.sorter_combo.setCurrentText(selected_sorter)
        elif self.sorter_combo.findText("mountainsort5") >= 0:
            self.sorter_combo.setCurrentText("mountainsort5")
        selected_sorter = str(self.sorter_combo.currentText()).strip()
        selected_params = dict((active_configuration or {}).get("sorter_params", {}) or {})
        if not selected_params:
            selected_params = dict(self.saved_config.get("sorter_params", {}) or {})
        if not selected_params:
            selected_params = self._default_params_for_sorter(selected_sorter)
        self.sorter_params_by_name[selected_sorter] = dict(selected_params or {})
        if active_configuration is not None:
            active_configuration["sorter_name"] = selected_sorter
            active_configuration["sorter_params"] = dict(selected_params or {})
        self.sorter_combo.currentTextChanged.connect(self._on_sorter_changed)

        self._recording_label_list = self._available_recording_labels()
        self.selected_recording_labels = self._initial_selected_recording_labels()
        self.recording_select_btn = QtWidgets.QPushButton(controls)
        self.recording_select_btn.setObjectName("secondaryActionButton")
        self.recording_select_btn.clicked.connect(self._open_recording_selection_dialog)

        # Keep the sorter and recording selectors compact and visually matched.
        combo_width = 220
        try:
            self.sorter_combo.setFixedWidth(combo_width)
        except Exception:
            self.sorter_combo.setMinimumWidth(combo_width)
            self.sorter_combo.setMaximumWidth(combo_width)
        try:
            self.recording_select_btn.setFixedWidth(combo_width)
        except Exception:
            self.recording_select_btn.setMinimumWidth(combo_width)
            self.recording_select_btn.setMaximumWidth(combo_width)
        self._update_recording_select_button_text()

        # Keep the attribute for internal compatibility, but do not show the
        # parameter-count summary in the compact sorter row.
        self.params_summary_label = QtWidgets.QLabel("", controls)
        self.params_summary_label.setVisible(False)

        self.params_btn = QtWidgets.QPushButton("Sorter parameters", controls)
        self.params_btn.setObjectName("secondaryActionButton")
        self.add_configuration_btn = QtWidgets.QPushButton("Add configuration", controls)
        self.add_configuration_btn.setObjectName("secondaryActionButton")
        self.add_configuration_btn.setToolTip(
            "Copy the preprocessing pipeline, sorter and parameters currently shown into a new queue configuration. "
            "The new row is selected so you can then choose another sorter or edit its parameters."
        )
        self.summary_params_btn = QtWidgets.QPushButton("Unit summary parameters", controls)
        self.summary_params_btn.setObjectName("secondaryActionButton")

        # Multiprocessing is now controlled globally from appLauncher(
        #     multiprocessing=True, n_jobs=None
        # ).  The sorter panel only keeps the per-run failure behavior visible.
        self.use_multiprocessing = bool(_launcher_multiprocessing_enabled())
        self.n_jobs = _available_multiprocessing_jobs(_launcher_n_jobs()) if self.use_multiprocessing else 1

        saved_skip_failed_sorting = True
        if isinstance(self.saved_config, dict) and "skip_failed_sorting" in self.saved_config:
            saved_skip_failed_sorting = bool(self.saved_config.get("skip_failed_sorting"))
        self.skip_failed_sorting_check = QtWidgets.QCheckBox("Skip failed sorting", controls)
        self.skip_failed_sorting_check.setChecked(saved_skip_failed_sorting)
        self.skip_failed_sorting_check.setToolTip(
            "If a recording or configuration fails, show the error in the log and continue with the remaining queue."
        )

        # Keep parameter buttons compact. Do not let the row layout stretch them.
        button_width = 190
        for btn in (self.params_btn, self.add_configuration_btn, self.summary_params_btn):
            try:
                btn.setFixedWidth(button_width)
            except Exception:
                btn.setMinimumWidth(button_width)
                btn.setMaximumWidth(button_width)
            btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed if hasattr(QtWidgets.QSizePolicy, "Policy") else QtWidgets.QSizePolicy.Fixed,
                              QtWidgets.QSizePolicy.Policy.Fixed if hasattr(QtWidgets.QSizePolicy, "Policy") else QtWidgets.QSizePolicy.Fixed)

        self.run_btn = QtWidgets.QPushButton("Run sorting queue", controls)
        self.run_btn.setObjectName("primaryRunButton")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.setMaximumWidth(180)
        try:
            cursor_shape = QtCore.Qt.CursorShape.PointingHandCursor
        except AttributeError:
            cursor_shape = QtCore.Qt.PointingHandCursor
        try:
            self.run_btn.setCursor(QtGui.QCursor(cursor_shape))
        except Exception:
            pass

        self.params_btn.clicked.connect(lambda: self._edit_params(add_as_configuration=True))
        self.add_configuration_btn.clicked.connect(self._add_sorting_configuration)
        self.summary_params_btn.clicked.connect(self._open_summary_params)
        self.run_btn.clicked.connect(self._run_sorting)

        # Use simple left-aligned rows rather than a grid. This prevents the
        # compact combos/buttons from drifting to the right when the panel is wide.
        label_width = 68
        sorter_label = QtWidgets.QLabel("Sorter:", controls)
        sorter_label.setFixedWidth(label_width)
        recording_label = QtWidgets.QLabel("Recording:", controls)
        recording_label.setFixedWidth(label_width)

        sorter_row = QtWidgets.QHBoxLayout()
        sorter_row.setSpacing(4)
        sorter_row.addWidget(sorter_label)
        sorter_row.addWidget(self.sorter_combo)
        sorter_row.addWidget(self.params_btn)
        sorter_row.addWidget(self.add_configuration_btn)
        sorter_row.addStretch(1)

        recording_row = QtWidgets.QHBoxLayout()
        recording_row.setSpacing(4)
        recording_row.addWidget(recording_label)
        recording_row.addWidget(self.recording_select_btn)
        recording_row.addWidget(self.summary_params_btn)
        recording_row.addStretch(1)

        compute_row = QtWidgets.QHBoxLayout()
        compute_row.setSpacing(4)
        compute_label = QtWidgets.QLabel("Compute:", controls)
        compute_label.setFixedWidth(label_width)
        compute_row.addWidget(compute_label)
        compute_row.addWidget(self.skip_failed_sorting_check)
        compute_row.addStretch(1)

        controls_layout.addLayout(sorter_row)

        queue_box = QtWidgets.QGroupBox("Configurations to run", controls)
        queue_layout = QtWidgets.QVBoxLayout(queue_box)
        queue_layout.setContentsMargins(8, 8, 8, 8)
        queue_layout.setSpacing(6)

        queue_note = QtWidgets.QLabel(
            "Each checked configuration runs on every selected recording, in the order shown. "
            "Each row keeps its own preprocessing pipeline, sorter and sorter parameters. "
            "Double-click the name to rename a configuration, or a pipeline cell to edit it. "
            "Use Ctrl or Shift to select several rows.",
            queue_box,
        )
        queue_note.setWordWrap(True)
        queue_note.setStyleSheet("color: #444;")
        queue_layout.addWidget(queue_note)

        self.configuration_table = QtWidgets.QTableWidget(queue_box)
        self.configuration_table.setColumnCount(5)
        self.configuration_table.setHorizontalHeaderLabels(
            ["Run", "Name", "Preprocessing", "Sorter", "Sorter params"]
        )
        self.configuration_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior")
            else QtWidgets.QAbstractItemView.SelectRows
        )
        self.configuration_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
            if hasattr(QtWidgets.QAbstractItemView, "SelectionMode")
            else QtWidgets.QAbstractItemView.ExtendedSelection
        )
        self.configuration_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            if hasattr(QtWidgets.QAbstractItemView, "EditTrigger")
            else QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.configuration_table.setAlternatingRowColors(True)
        self.configuration_table.verticalHeader().setVisible(False)
        self.configuration_table.setMinimumHeight(125)
        self.configuration_table.setMaximumHeight(190)
        header = self.configuration_table.horizontalHeader()
        try:
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        except AttributeError:
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
            header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
            header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
            header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.configuration_table.setColumnWidth(1, 180)
        queue_layout.addWidget(self.configuration_table)

        configuration_actions = QtWidgets.QHBoxLayout()
        configuration_actions.addStretch(1)
        self.rename_configuration_btn = QtWidgets.QPushButton("Rename", queue_box)
        self.duplicate_configuration_btn = QtWidgets.QPushButton("Duplicate", queue_box)
        self.delete_configuration_btn = QtWidgets.QPushButton("Delete selected", queue_box)
        for button in (
            self.rename_configuration_btn,
            self.duplicate_configuration_btn,
            self.delete_configuration_btn,
        ):
            button.setObjectName("secondaryActionButton")
            configuration_actions.addWidget(button)
        queue_layout.addLayout(configuration_actions)

        self.configuration_table.itemChanged.connect(self._on_configuration_table_item_changed)
        self.configuration_table.itemSelectionChanged.connect(self._on_configuration_table_selection_changed)
        self.configuration_table.cellDoubleClicked.connect(self._on_configuration_table_cell_double_clicked)
        self.rename_configuration_btn.clicked.connect(self._rename_selected_sorting_configuration)
        self.duplicate_configuration_btn.clicked.connect(self._duplicate_selected_sorting_configuration)
        self.delete_configuration_btn.clicked.connect(self._delete_selected_sorting_configuration)

        controls_layout.addWidget(queue_box)
        controls_layout.addLayout(recording_row)
        controls_layout.addLayout(compute_row)

        run_row = QtWidgets.QHBoxLayout()
        run_row.addStretch(1)
        run_row.addWidget(self.run_btn)
        run_row.addStretch(1)

        saved_summary = self.saved_config.get("unit_summary", {}) if isinstance(self.saved_config, dict) else {}
        if not isinstance(saved_summary, dict):
            saved_summary = {}
        self._summary_event_options_cache = []
        self._summary_events_loaded = False
        self._summary_saved_marker = str(saved_summary.get("event_marker", "") or "").strip()
        saved_plot_events = saved_summary.get("plot_events", [])
        if not isinstance(saved_plot_events, list):
            saved_plot_events = []
        self.unit_summary_config = {
            "event_marker": self._summary_saved_marker,
            "edge": str(saved_summary.get("edge", "start") or "start").strip().lower(),
            "before_s": self._safe_summary_float(saved_summary.get("before_s", 0.0), 0.0),
            "after_s": self._safe_summary_float(saved_summary.get("after_s", 2.0), 2.0),
            "plot_events": saved_plot_events,
        }

        controls_layout.addLayout(run_row)

        layout.addWidget(controls)

        self.status_label = QtWidgets.QLabel("Ready.", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.output_stack = QtWidgets.QStackedWidget(self)

        self.log_text = QtWidgets.QPlainTextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap if hasattr(QtWidgets.QPlainTextEdit, "LineWrapMode") else QtWidgets.QPlainTextEdit.NoWrap)
        self.log_text.setStyleSheet("QPlainTextEdit { background: white; color: black; font-family: Consolas, monospace; }")
        self.output_stack.addWidget(self.log_text)

        self.summary_records = []
        self.summary_record_index = 0
        self.summary_unit_index = 0
        self.summary_variant_index = 0
        self._summary_pixmap_original = None
        self.summary_browser = self._build_summary_browser_widget()
        self.output_stack.addWidget(self.summary_browser)

        layout.addWidget(self.output_stack, stretch=1)

        self._load_active_configuration_into_editor()
        self._refresh_configuration_table(select_id=self.active_configuration_id)
        # Persist migration of older queue rows that used one global
        # preprocessing pipeline, so Curation can reuse the same numbering.
        self._save_sorting_config()
        self.status_label.setText("Ready.")
        self._append_log("Sorting panel ready.")
        self._append_log(f"Recording groups: {len(self._recording_groups())}")
        enabled = self.pipeline.get("enabled_pipeline", []) or []
        self._append_log(f"Enabled preprocessing steps: {len(enabled)}")
        # Let the panel paint first, then retrieve event markers. This avoids a
        # blank right panel when switching between spike-sorting projects.
        try:
            QtCore.QTimer.singleShot(50, self._populate_summary_events_after_show)
        except Exception:
            self._populate_summary_events_after_show()

    def _append_log(self, text=""):
        if not hasattr(self, "log_text"):
            return
        text = str(text)
        replace_last = text.startswith("\r")
        if replace_last:
            text = text.lstrip("\r")
            if not text:
                return
            try:
                cursor = self.log_text.textCursor()
                end = QtGui.QTextCursor.MoveOperation.End if hasattr(QtGui.QTextCursor, "MoveOperation") else QtGui.QTextCursor.End
                start_block = QtGui.QTextCursor.MoveOperation.StartOfBlock if hasattr(QtGui.QTextCursor, "MoveOperation") else QtGui.QTextCursor.StartOfBlock
                keep = QtGui.QTextCursor.MoveMode.KeepAnchor if hasattr(QtGui.QTextCursor, "MoveMode") else QtGui.QTextCursor.KeepAnchor
                cursor.movePosition(end)
                cursor.movePosition(start_block, keep)
                cursor.removeSelectedText()
                cursor.insertText(text)
                self.log_text.setTextCursor(cursor)
            except Exception:
                self.log_text.appendPlainText(text)
        else:
            self.log_text.appendPlainText(text)
        try:
            bar = self.log_text.verticalScrollBar()
            bar.setValue(bar.maximum())
        except Exception:
            pass

    def _show_log_panel(self):
        if hasattr(self, "output_stack") and hasattr(self, "log_text"):
            try:
                self.output_stack.setCurrentWidget(self.log_text)
            except Exception:
                pass

    def _show_summary_panel(self):
        if hasattr(self, "output_stack") and hasattr(self, "summary_browser"):
            try:
                self.output_stack.setCurrentWidget(self.summary_browser)
            except Exception:
                pass

    def _build_summary_browser_widget(self):
        page = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)

        self.summary_recording_combo = QtWidgets.QComboBox(page)
        self.summary_recording_combo.setMinimumWidth(180)
        self.summary_prev_unit_btn = QtWidgets.QPushButton("◀ Unit", page)
        self.summary_next_unit_btn = QtWidgets.QPushButton("Unit ▶", page)
        self.summary_show_log_btn = QtWidgets.QPushButton("Show log", page)

        for btn in (
            self.summary_prev_unit_btn,
            self.summary_next_unit_btn,
            self.summary_show_log_btn,
        ):
            try:
                btn.setMinimumHeight(28)
                btn.setMaximumWidth(90)
            except Exception:
                pass

        toolbar.addWidget(QtWidgets.QLabel("Recording:", page))
        toolbar.addWidget(self.summary_recording_combo, stretch=1)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.summary_prev_unit_btn)
        toolbar.addWidget(self.summary_next_unit_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self.summary_show_log_btn)
        layout.addLayout(toolbar)

        self.summary_image_info = QtWidgets.QLabel("No unit summary image loaded.", page)
        self.summary_image_info.setWordWrap(True)
        self.summary_image_info.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter
        )
        self.summary_image_info.setStyleSheet("QLabel { font-weight: 600; padding: 4px; }")
        layout.addWidget(self.summary_image_info)

        self.summary_scroll = QtWidgets.QScrollArea(page)
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter
        )
        self.summary_image_label = QtWidgets.QLabel("No summary image available.", self.summary_scroll)
        self.summary_image_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter
        )
        self.summary_image_label.setStyleSheet("QLabel { background: #f6f8fa; color: #57606a; }")
        self.summary_image_label.setMinimumSize(300, 220)
        self.summary_scroll.setWidget(self.summary_image_label)
        layout.addWidget(self.summary_scroll, stretch=1)

        self.summary_recording_combo.currentIndexChanged.connect(self._on_summary_recording_combo_changed)
        # Sorting-page summary browser only shows the uncurated/original summary.
        # The Uncurated/Curated selector is only used in the Curation summary window.
        self.summary_prev_unit_btn.clicked.connect(self._summary_previous_unit)
        self.summary_next_unit_btn.clicked.connect(self._summary_next_unit)
        self.summary_show_log_btn.clicked.connect(self._show_log_panel)

        try:
            self.summary_scroll.viewport().installEventFilter(self)
        except Exception:
            pass

        self._update_summary_browser_controls()
        return page

    def eventFilter(self, obj, event):
        try:
            if hasattr(self, "summary_scroll") and obj is self.summary_scroll.viewport():
                event_type = QtCore.QEvent.Type.Resize if hasattr(QtCore.QEvent, "Type") else QtCore.QEvent.Resize
                if event.type() == event_type and self._summary_pixmap_original is not None:
                    QtCore.QTimer.singleShot(0, self._render_summary_pixmap)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _reset_summary_browser(self, records=None):
        self.summary_records = list(records or [])
        self.summary_record_index = 0
        self.summary_unit_index = 0
        self.summary_variant_index = 0
        self._summary_pixmap_original = None
        self._refresh_summary_recording_combo()
        self._update_summary_image()

    def _summary_record_display_label(self, record, index=None):
        record = dict(record or {})
        recording = str(record.get("recording", "recording") or "recording")
        configuration = str(record.get("configuration", "") or "")
        sorter = str(record.get("sorter", "") or "")
        run_name = str(record.get("run_name", "") or "")
        variants = self._summary_variants_for_record(record)
        prefix = f"{recording}"
        suffix_parts = []
        if configuration:
            suffix_parts.append(configuration)
        if sorter:
            suffix_parts.append(sorter)
        if run_name:
            suffix_parts.append(run_name)
        if variants:
            suffix_parts.append(f"{len(variants)} summary version{'s' if len(variants) != 1 else ''}")
        label = prefix + " | " + " | ".join(suffix_parts)
        if index is not None:
            label = f"{index + 1}. {label}"
        return label

    def _summary_variants_for_record(self, record):
        record = dict(record or {})
        variants = record.get("variants", None)
        if isinstance(variants, list) and variants:
            clean = []
            for idx, variant in enumerate(variants):
                if not isinstance(variant, dict):
                    continue
                v = dict(variant)
                v.setdefault("label", "Original" if idx == 0 else f"Version {idx + 1}")
                v.setdefault("images", [])
                v.setdefault("summary_folder", record.get("summary_folder", ""))
                clean.append(v)
            if clean:
                return clean
        # Compatibility with old records where images were stored directly on the record.
        return [{
            "kind": record.get("kind", "original"),
            "label": record.get("variant_label", "Original"),
            "summary_folder": record.get("summary_folder", ""),
            "images": list(record.get("images", []) or []),
        }]

    def _preferred_variant_index_for_current_record(self, variant_kind=None):
        """Return the variant index to select for the current record.

        Curation-table shortcuts use this to open the same summary viewer with
        either the original/uncurated summary or the curated summary already
        selected in the Summary combo.  If the requested variant does not exist,
        the viewer falls back to the first available summary.
        """
        kind = str(variant_kind or "").strip().lower()
        if not kind:
            return 0
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        aliases = {
            "original": {"original", "uncurated", "raw"},
            "uncurated": {"original", "uncurated", "raw"},
            "curated": {"curated"},
        }
        wanted = aliases.get(kind, {kind})
        for idx, variant in enumerate(variants):
            vkind = str(variant.get("kind", "") or "").strip().lower()
            vlabel = str(variant.get("label", "") or "").strip().lower()
            if vkind in wanted or vlabel in wanted:
                return idx
        return 0

    def _current_summary_variant(self):
        record = self._current_summary_record()
        if record is None:
            return None
        variants = self._summary_variants_for_record(record)
        if not variants:
            return None
        self.summary_variant_index = max(0, min(int(getattr(self, "summary_variant_index", 0)), len(variants) - 1))
        return variants[self.summary_variant_index]

    def _refresh_summary_variant_combo(self):
        combo = getattr(self, "summary_variant_combo", None)
        if combo is None:
            return
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        try:
            combo.blockSignals(True)
            combo.clear()
            if not variants:
                combo.addItem("No summary", -1)
                combo.setEnabled(False)
            else:
                for idx, variant in enumerate(variants):
                    images = list(variant.get("images", []) or [])
                    label = str(variant.get("label", f"Version {idx + 1}"))
                    if images:
                        label = f"{label} ({len(images)} unit{'s' if len(images) != 1 else ''})"
                    combo.addItem(label, idx)
                current = max(0, min(int(getattr(self, "summary_variant_index", 0)), len(variants) - 1))
                combo.setCurrentIndex(current)
                combo.setEnabled(len(variants) > 1)
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _sync_summary_variant_combo(self):
        combo = getattr(self, "summary_variant_combo", None)
        if combo is None:
            return
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        if not variants:
            return
        current = max(0, min(int(getattr(self, "summary_variant_index", 0)), len(variants) - 1))
        try:
            combo.blockSignals(True)
            if combo.currentIndex() != current:
                combo.setCurrentIndex(current)
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _on_summary_variant_combo_changed(self, index):
        combo = getattr(self, "summary_variant_combo", None)
        if combo is None:
            return
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        if not variants:
            return
        try:
            value = combo.itemData(int(index))
            idx = int(value) if value is not None else int(index)
        except Exception:
            idx = int(index) if index is not None else 0
        idx = max(0, min(idx, len(variants) - 1))
        if idx == int(getattr(self, "summary_variant_index", 0)):
            return
        self.summary_variant_index = idx
        self.summary_unit_index = 0
        self._update_summary_image()

    def _refresh_summary_recording_combo(self):
        combo = getattr(self, "summary_recording_combo", None)
        if combo is None:
            return
        records = list(getattr(self, "summary_records", []) or [])
        try:
            combo.blockSignals(True)
            combo.clear()
            if not records:
                combo.addItem("No recording summary", -1)
                combo.setEnabled(False)
            else:
                for idx, record in enumerate(records):
                    combo.addItem(self._summary_record_display_label(record, idx), idx)
                current = max(0, min(int(getattr(self, "summary_record_index", 0)), len(records) - 1))
                combo.setCurrentIndex(current)
                combo.setEnabled(True)
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _sync_summary_recording_combo(self):
        combo = getattr(self, "summary_recording_combo", None)
        records = list(getattr(self, "summary_records", []) or [])
        if combo is None or not records:
            return
        current = max(0, min(int(getattr(self, "summary_record_index", 0)), len(records) - 1))
        try:
            combo.blockSignals(True)
            if combo.currentIndex() != current:
                combo.setCurrentIndex(current)
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _on_summary_recording_combo_changed(self, index):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return
        try:
            value = self.summary_recording_combo.itemData(int(index))
            idx = int(value) if value is not None else int(index)
        except Exception:
            idx = int(index) if index is not None else 0
        idx = max(0, min(idx, len(records) - 1))
        if idx == int(getattr(self, "summary_record_index", 0)):
            return
        self.summary_record_index = idx
        self.summary_variant_index = 0
        self.summary_unit_index = 0
        self._refresh_summary_variant_combo()
        self._update_summary_image()

    def _load_summary_browser_from_run_summaries(self, summary_paths):
        records = []
        seen_folders = set()
        for summary_path in summary_paths or []:
            try:
                run_json = Path(summary_path)
            except Exception:
                continue
            if not run_json.exists():
                continue
            try:
                with open(run_json, "r", encoding="utf-8") as f:
                    payload = _pp_json_load(f)
            except Exception as exc:
                self._append_log(f"Could not read run summary for viewer: {run_json} ({exc})")
                continue
            if not isinstance(payload, dict):
                continue

            summary_folder_text = payload.get("sorting_summary_folder", None)
            if not summary_folder_text:
                continue
            summary_folder = Path(summary_folder_text)
            if not summary_folder.exists():
                continue
            key = str(summary_folder.resolve())
            if key in seen_folders:
                continue
            seen_folders.add(key)

            images = self._find_summary_images(summary_folder)
            records.append({
                "recording": str(payload.get("recording_label", summary_folder.parent.parent.name if summary_folder.parent.parent else "recording") or "recording"),
                "configuration": str((payload.get("queue_configuration", {}) or {}).get("name", ""))
                if isinstance(payload.get("queue_configuration", {}), dict)
                else "",
                "sorter": str(payload.get("sorter_name", "") or ""),
                "run_name": summary_folder.parent.name,
                "summary_folder": summary_folder,
                "images": images,
            })

        records.sort(key=lambda r: _natural_text_key(str(r.get("recording", ""))))
        self._reset_summary_browser(records)
        return len(records) > 0

    def _find_summary_images(self, summary_folder):
        summary_folder = Path(summary_folder)
        candidates = []
        preferred = summary_folder / "png_version"
        if preferred.exists():
            candidates.extend(preferred.glob("Unit_*.png"))
            if not candidates:
                candidates.extend(preferred.glob("*.png"))
        if not candidates:
            candidates.extend(summary_folder.glob("Unit_*.png"))
            candidates.extend(summary_folder.glob("*.png"))
        if not candidates:
            for suffix in ("*.jpg", "*.jpeg", "*.webp"):
                candidates.extend(summary_folder.rglob(suffix))
        # Avoid duplicates while preserving natural order.
        unique = []
        seen = set()
        for p in sorted(candidates, key=lambda p: _natural_text_key(p.name)):
            try:
                key = str(p.resolve())
            except Exception:
                key = str(p)
            if key not in seen and p.is_file():
                unique.append(p)
                seen.add(key)
        return unique

    def _current_summary_record(self):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return None
        self.summary_record_index = max(0, min(int(getattr(self, "summary_record_index", 0)), len(records) - 1))
        return records[self.summary_record_index]

    def _current_summary_image_path(self):
        record = self._current_summary_record()
        if record is None:
            return None
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        if not images:
            return None
        self.summary_unit_index = max(0, min(int(getattr(self, "summary_unit_index", 0)), len(images) - 1))
        return images[self.summary_unit_index]

    def _summary_previous_recording(self):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return
        self.summary_record_index = max(0, int(getattr(self, "summary_record_index", 0)) - 1)
        self.summary_unit_index = 0
        self._update_summary_image()

    def _summary_next_recording(self):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return
        self.summary_record_index = min(len(records) - 1, int(getattr(self, "summary_record_index", 0)) + 1)
        self.summary_unit_index = 0
        self._update_summary_image()

    def _summary_previous_unit(self):
        record = self._current_summary_record()
        if record is None:
            return
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        if not images:
            return
        self.summary_unit_index = max(0, int(getattr(self, "summary_unit_index", 0)) - 1)
        self._update_summary_image()

    def _summary_next_unit(self):
        record = self._current_summary_record()
        if record is None:
            return
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        if not images:
            return
        self.summary_unit_index = min(len(images) - 1, int(getattr(self, "summary_unit_index", 0)) + 1)
        self._update_summary_image()

    def _update_summary_browser_controls(self):
        records = list(getattr(self, "summary_records", []) or [])
        record = self._current_summary_record()
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        ri = int(getattr(self, "summary_record_index", 0))
        ui = int(getattr(self, "summary_unit_index", 0))

        for btn_name, enabled in (
            ("summary_prev_unit_btn", bool(images) and ui > 0),
            ("summary_next_unit_btn", bool(images) and ui < len(images) - 1),
        ):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                try:
                    btn.setEnabled(bool(enabled))
                except Exception:
                    pass

    def _update_summary_image(self):
        record = self._current_summary_record()
        self._sync_summary_recording_combo()
        self._sync_summary_variant_combo()
        image_path = self._current_summary_image_path()
        self._summary_pixmap_original = None

        if record is None:
            if hasattr(self, "summary_image_info"):
                self.summary_image_info.setText("No unit summary available yet.")
            if hasattr(self, "summary_image_label"):
                self.summary_image_label.setPixmap(QtGui.QPixmap())
                self.summary_image_label.setText("Run sorting to generate unit summary PNG files.")
            self._update_summary_browser_controls()
            return

        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        recording = str(record.get("recording", "recording") or "recording")
        configuration = str(record.get("configuration", "") or "")
        sorter = str(record.get("sorter", "") or "")
        run_name = str(record.get("run_name", "") or "")
        variant_label = str(variant.get("label", "Original") if variant is not None else "Original")
        summary_folder = variant.get("summary_folder", record.get("summary_folder", "")) if variant is not None else record.get("summary_folder", "")

        if image_path is None:
            if hasattr(self, "summary_image_info"):
                self.summary_image_info.setText(
                    f"Recording {self.summary_record_index + 1}/{len(self.summary_records)}: {recording}"
                    + (f" | {configuration}" if configuration else "")
                    + (f" | {sorter}" if sorter else "")
                    + (f" | {run_name}" if run_name else "")
                )
            if hasattr(self, "summary_image_label"):
                self.summary_image_label.setPixmap(QtGui.QPixmap())
                self.summary_image_label.setText(f"No PNG unit summaries found in:\n{summary_folder}")
            self._update_summary_browser_controls()
            return

        pixmap = QtGui.QPixmap(str(image_path))
        if pixmap.isNull():
            if hasattr(self, "summary_image_label"):
                self.summary_image_label.setPixmap(QtGui.QPixmap())
                self.summary_image_label.setText(f"Could not load image:\n{image_path}")
            self._update_summary_browser_controls()
            return

        self._summary_pixmap_original = pixmap
        unit_label = Path(image_path).stem.replace("Unit_", "Unit ")
        if hasattr(self, "summary_image_info"):
            self.summary_image_info.setText(
                f"Recording {self.summary_record_index + 1}/{len(self.summary_records)}: {recording}"
                + (f" | {configuration}" if configuration else "")
                + (f" | {sorter}" if sorter else "")
                + (f" | {run_name}" if run_name else "")
                + f" | unit {self.summary_unit_index + 1}/{len(images)}: {unit_label}"
            )
        self._render_summary_pixmap()
        self._update_summary_browser_controls()

    def _render_summary_pixmap(self):
        pixmap = getattr(self, "_summary_pixmap_original", None)
        if pixmap is None or pixmap.isNull() or not hasattr(self, "summary_image_label"):
            return
        try:
            viewport_size = self.summary_scroll.viewport().size()
            max_w = max(300, int(viewport_size.width()) - 20)
            transform = QtCore.Qt.TransformationMode.SmoothTransformation if hasattr(QtCore.Qt, "TransformationMode") else QtCore.Qt.SmoothTransformation
            # Scale to the available width only. Fitting both width and height
            # made wide summary figures too small, which made raster dots hard
            # to see. The scroll area can handle the extra vertical height.
            if pixmap.width() > max_w:
                scaled = pixmap.scaledToWidth(max_w, transform)
            else:
                scaled = pixmap
            self.summary_image_label.setPixmap(scaled)
            self.summary_image_label.setText("")
            self.summary_image_label.resize(scaled.size())
        except Exception:
            self.summary_image_label.setPixmap(pixmap)
            self.summary_image_label.setText("")

    def _initial_sorting_configurations(self):
        raw = self.saved_config.get("sorting_configurations", []) if isinstance(self.saved_config, dict) else []
        configurations = []
        used_ids = set()
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    continue
                sorter_name = str(item.get("sorter_name", item.get("sorter", "")) or "").strip()
                if not sorter_name:
                    continue
                params = item.get("sorter_params", item.get("params", {}))
                if not isinstance(params, dict):
                    params = {}
                preprocessing_pipeline = item.get("preprocessing_pipeline", self.pipeline)
                if not isinstance(preprocessing_pipeline, dict):
                    preprocessing_pipeline = self.pipeline
                config_id = str(item.get("id", "") or "").strip()
                if not config_id or config_id in used_ids:
                    seed = {"index": index, "sorter": sorter_name, "params": params, "name": item.get("name", "")}
                    config_id = f"config_{index + 1}_{_signature_hash(seed)[:8]}"
                while config_id in used_ids:
                    config_id += "_copy"
                used_ids.add(config_id)
                name = str(item.get("name", "") or "").strip() or f"{sorter_name} parameters {index + 1}"
                configurations.append({
                    "id": config_id,
                    "name": name,
                    "sorter_name": sorter_name,
                    "sorter_params": dict(params),
                    "preprocessing_pipeline": _json_safe(dict(preprocessing_pipeline or {})),
                    "enabled": bool(item.get("enabled", True)),
                })

        if configurations or (isinstance(self.saved_config, dict) and "sorting_configurations" in self.saved_config):
            return configurations

        sorter_name = str(self.saved_config.get("selected_sorter", "") if isinstance(self.saved_config, dict) else "").strip()
        if not sorter_name:
            sorter_name = "mountainsort5"
        params = self.saved_config.get("sorter_params", {}) if isinstance(self.saved_config, dict) else {}
        if not isinstance(params, dict):
            params = {}
        return [{
            "id": f"config_1_{_signature_hash({'sorter': sorter_name, 'params': params})[:8]}",
            "name": f"{sorter_name} parameters 1",
            "sorter_name": sorter_name,
            "sorter_params": dict(params),
            "preprocessing_pipeline": _json_safe(dict(self.pipeline or {})),
            "enabled": True,
        }]

    def _new_configuration_id(self):
        existing = {str(c.get("id", "")) for c in (self.sorting_configurations or [])}
        while True:
            candidate = f"config_{time.time_ns():x}"
            if candidate not in existing:
                return candidate

    def _configuration_by_id(self, config_id):
        config_id = str(config_id or "")
        for configuration in self.sorting_configurations or []:
            if str(configuration.get("id", "")) == config_id:
                return configuration
        return None

    def _active_sorting_configuration(self):
        return self._configuration_by_id(getattr(self, "active_configuration_id", ""))

    def _configuration_for_table_row(self, row):
        table = getattr(self, "configuration_table", None)
        if table is None:
            return None
        try:
            row = int(row)
        except Exception:
            return None
        if row < 0 or row >= table.rowCount():
            return None
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is None:
                continue
            config_id = item.data(_qt_user_role())
            if config_id:
                return self._configuration_by_id(config_id)
        return None

    def _selected_sorting_configurations(self):
        """Return selected configurations in their visible table order."""
        table = getattr(self, "configuration_table", None)
        if table is None:
            return []
        rows = set()
        used_selection_model = False
        try:
            selection_model = table.selectionModel()
            if selection_model is not None:
                used_selection_model = True
                rows.update(index.row() for index in selection_model.selectedRows())
        except Exception:
            pass
        if not used_selection_model and not rows and table.currentRow() >= 0:
            rows.add(table.currentRow())
        configurations = []
        for row in sorted(rows):
            configuration = self._configuration_for_table_row(row)
            if configuration is not None:
                configurations.append(configuration)
        return configurations

    def _selected_sorting_configuration(self):
        table = getattr(self, "configuration_table", None)
        if table is None:
            return None
        current = self._configuration_for_table_row(table.currentRow())
        selected = self._selected_sorting_configurations()
        if current is not None and any(
            str(configuration.get("id", "")) == str(current.get("id", ""))
            for configuration in selected
        ):
            return current
        return selected[0] if selected else None

    def _unique_configuration_name(self, base_name, *, exclude_id=None):
        base_name = str(base_name or "Configuration").strip() or "Configuration"
        existing = {
            str(c.get("name", "")).strip().casefold()
            for c in (self.sorting_configurations or [])
            if str(c.get("id", "")) != str(exclude_id or "")
        }
        if base_name.casefold() not in existing:
            return base_name
        index = 2
        while f"{base_name} {index}".casefold() in existing:
            index += 1
        return f"{base_name} {index}"

    @staticmethod
    def _configuration_params_summary(params):
        params = dict(params or {})
        if not params:
            return "Defaults not loaded"
        parts = []
        for key, value in list(sorted(params.items(), key=lambda item: str(item[0]).lower()))[:3]:
            try:
                value_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                value_text = str(value)
            parts.append(f"{key}={value_text}")
        if len(params) > 3:
            parts.append(f"... ({len(params)} total)")
        return ", ".join(parts)

    def _configuration_parameter_pipeline_numbers(self):
        return _sorter_parameter_pipeline_numbers(self.sorting_configurations or [])

    def _configuration_parameter_pipeline_label(self, configuration):
        if not isinstance(configuration, dict):
            return "Pipeline ?"
        key = _sorter_parameter_pipeline_key(
            configuration.get("sorter_name", ""),
            configuration.get("sorter_params", {}),
        )
        number = self._configuration_parameter_pipeline_numbers().get(key)
        return f"Pipeline {number}" if number is not None else "Pipeline ?"

    def _configuration_preprocessing_pipeline_numbers(self):
        return _preprocessing_pipeline_numbers(self.sorting_configurations or [])

    def _configuration_preprocessing_pipeline_label(self, configuration):
        if not isinstance(configuration, dict):
            return "Pipeline ?"
        pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
        if not isinstance(pipeline, dict):
            pipeline = self.pipeline
        number = self._configuration_preprocessing_pipeline_numbers().get(
            _preprocessing_pipeline_key(pipeline)
        )
        return f"Pipeline {number}" if number is not None else "Pipeline ?"

    def _refresh_configuration_table(self, *, select_id=None):
        table = getattr(self, "configuration_table", None)
        if table is None:
            return
        select_id = str(select_id or "")
        self._refreshing_configuration_table = True
        try:
            table.blockSignals(True)
            table.setRowCount(len(self.sorting_configurations or []))
            selected_row = -1
            for row, configuration in enumerate(self.sorting_configurations or []):
                config_id = str(configuration.get("id", ""))
                if config_id == select_id:
                    selected_row = row

                run_item = QtWidgets.QTableWidgetItem("")
                try:
                    flags = run_item.flags()
                    user_checkable = QtCore.Qt.ItemFlag.ItemIsUserCheckable if hasattr(QtCore.Qt, "ItemFlag") else QtCore.Qt.ItemIsUserCheckable
                    run_item.setFlags(flags | user_checkable)
                except Exception:
                    pass
                run_item.setCheckState(_qt_checked() if bool(configuration.get("enabled", True)) else _qt_unchecked())
                run_item.setTextAlignment(
                    QtCore.Qt.AlignmentFlag.AlignCenter
                    if hasattr(QtCore.Qt, "AlignmentFlag")
                    else QtCore.Qt.AlignCenter
                )

                name_item = QtWidgets.QTableWidgetItem(str(configuration.get("name", "")))
                preprocessing_pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
                if not isinstance(preprocessing_pipeline, dict):
                    preprocessing_pipeline = self.pipeline
                preprocessing_item = QtWidgets.QTableWidgetItem(
                    self._configuration_preprocessing_pipeline_label(configuration)
                )
                try:
                    preprocessing_item.setToolTip(
                        json.dumps(_enabled_pipeline_for_signature(preprocessing_pipeline), indent=2, ensure_ascii=False)
                    )
                except Exception:
                    preprocessing_item.setToolTip(str(preprocessing_pipeline))
                sorter_item = QtWidgets.QTableWidgetItem(str(configuration.get("sorter_name", "")))
                params = dict(configuration.get("sorter_params", {}) or {})
                params_item = QtWidgets.QTableWidgetItem(
                    self._configuration_parameter_pipeline_label(configuration)
                )
                try:
                    params_item.setToolTip(json.dumps(params, indent=2, ensure_ascii=False))
                except Exception:
                    params_item.setToolTip(str(params))

                for column, item in enumerate(
                    (run_item, name_item, preprocessing_item, sorter_item, params_item)
                ):
                    item.setData(_qt_user_role(), config_id)
                    table.setItem(row, column, item)

            if selected_row >= 0:
                table.selectRow(selected_row)
            elif table.rowCount() > 0:
                table.selectRow(0)
        finally:
            table.blockSignals(False)
            self._refreshing_configuration_table = False
        self._update_configuration_action_state()

    def _update_configuration_action_state(self):
        selected = self._selected_sorting_configurations()
        busy = bool(getattr(self, "_busy", False))
        single_enabled = bool(len(selected) == 1 and not busy)
        delete_enabled = bool(selected and not busy)
        for button_name in ("rename_configuration_btn", "duplicate_configuration_btn"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.setEnabled(single_enabled)
        delete_button = getattr(self, "delete_configuration_btn", None)
        if delete_button is not None:
            delete_button.setEnabled(delete_enabled)

    def _on_configuration_table_selection_changed(self):
        self._update_configuration_action_state()
        if self._refreshing_configuration_table or getattr(self, "_busy", False):
            return
        configuration = self._selected_sorting_configuration()
        if configuration is None:
            return
        config_id = str(configuration.get("id", ""))
        if config_id == str(getattr(self, "active_configuration_id", "")):
            return
        self.active_configuration_id = config_id
        self._load_active_configuration_into_editor()
        self._save_sorting_config()
        if hasattr(self, "status_label"):
            selected_count = len(self._selected_sorting_configurations())
            if selected_count > 1:
                self.status_label.setText(f"{selected_count} configurations selected")
            else:
                self.status_label.setText(f"Selected configuration: {configuration.get('name', '')}")

    def _on_configuration_table_item_changed(self, item):
        if self._refreshing_configuration_table or item is None or item.column() != 0:
            return
        configuration = self._configuration_by_id(item.data(_qt_user_role()))
        if configuration is None:
            return
        configuration["enabled"] = item.checkState() == _qt_checked()
        self._save_sorting_config()

    def _on_configuration_table_cell_double_clicked(self, row, column):
        configuration = self._configuration_for_table_row(row)
        if configuration is None:
            return
        if int(column) == 1:
            self._rename_sorting_configuration(configuration)
            return
        if int(column) == 2:
            self._edit_preprocessing_pipeline_for_configuration(configuration)
            return
        if int(column) == 4:
            self._edit_sorter_parameters_for_configuration(configuration)
            return

    def _load_active_configuration_into_editor(self):
        configuration = self._active_sorting_configuration()
        if configuration is None or not hasattr(self, "sorter_combo"):
            return
        sorter_name = str(configuration.get("sorter_name", "")).strip()
        if not sorter_name:
            return
        self._loading_configuration_editor = True
        try:
            if self.sorter_combo.findText(sorter_name) < 0:
                self.sorter_combo.addItem(sorter_name)
            self.sorter_combo.setCurrentText(sorter_name)
            params = dict(configuration.get("sorter_params", {}) or {})
            if not params:
                params = self._default_params_for_sorter(sorter_name)
                configuration["sorter_params"] = dict(params or {})
            self.sorter_params_by_name[sorter_name] = dict(params or {})
            if hasattr(self, "params_summary_label"):
                self.params_summary_label.setText(self._format_params_summary(sorter_name))
        finally:
            self._loading_configuration_editor = False

    def _edit_sorter_parameters_for_configuration(self, configuration):
        if configuration is None:
            return
        selected_id = str(configuration.get("id", ""))
        # Always reload the row before editing. The sorter combo can contain a
        # draft for a future configuration even when this row is still active.
        self.active_configuration_id = selected_id
        self._load_active_configuration_into_editor()
        self._refresh_configuration_table(select_id=self.active_configuration_id)
        self._save_sorting_config()
        if hasattr(self, "status_label"):
            self.status_label.setText(
                f"Editing sorter parameters: {configuration.get('name', '')}"
            )
        self._edit_params(add_as_configuration=False)

    def _edit_selected_sorting_configuration(self):
        """Compatibility wrapper for code that still invokes the old action."""
        self._edit_sorter_parameters_for_configuration(
            self._selected_sorting_configuration()
        )

    def _edit_preprocessing_pipeline_for_configuration(self, configuration):
        if configuration is None:
            return
        current_pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
        if not isinstance(current_pipeline, dict):
            current_pipeline = self.pipeline
        dialog = _PreprocessingOptionsDialog(
            self,
            self.preprocessing_steps,
            current_pipeline,
        )
        pipeline_label = self._configuration_preprocessing_pipeline_label(configuration)
        dialog.setWindowTitle(
            f"Edit preprocessing - {configuration.get('name', '')} - {pipeline_label}"
        )
        try:
            dialog.apply_button.setText("Save changes")
            dialog.close_button.setText("Cancel")
        except Exception:
            pass
        result = dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()
        accepted = (
            QtWidgets.QDialog.DialogCode.Accepted
            if hasattr(QtWidgets.QDialog, "DialogCode")
            else QtWidgets.QDialog.Accepted
        )
        if result != accepted:
            return
        configuration["preprocessing_pipeline"] = dialog.serializable_config()
        self.active_configuration_id = str(configuration.get("id", ""))
        self._refresh_configuration_table(select_id=self.active_configuration_id)
        self._save_sorting_config()
        if hasattr(self, "status_label"):
            self.status_label.setText(
                f"Updated preprocessing for: {configuration.get('name', '')}"
            )

    def _edit_selected_preprocessing_pipeline(self):
        """Compatibility wrapper for code that still invokes the old action."""
        self._edit_preprocessing_pipeline_for_configuration(
            self._selected_sorting_configuration()
        )

    def _add_sorting_configuration(self):
        sorter_name = str(self.sorter_combo.currentText()).strip()
        if not sorter_name:
            QtWidgets.QMessageBox.warning(self, "Sorting configuration", "Please select a sorter first.")
            return
        params = dict(self.sorter_params_by_name.get(sorter_name, {}) or {})
        sorter_count = sum(
            1 for existing in (self.sorting_configurations or [])
            if str(existing.get("sorter_name", "")) == sorter_name
        )
        name = self._unique_configuration_name(f"{sorter_name} parameters {sorter_count + 1}")
        active = self._active_sorting_configuration()
        source_pipeline = (
            active.get("preprocessing_pipeline", self.pipeline)
            if isinstance(active, dict)
            else self.pipeline
        )
        if not isinstance(source_pipeline, dict):
            source_pipeline = self.pipeline
        configuration = {
            "id": self._new_configuration_id(),
            "name": name,
            "sorter_name": sorter_name,
            "sorter_params": params,
            "preprocessing_pipeline": _json_safe(dict(source_pipeline or {})),
            "enabled": True,
        }
        self.sorting_configurations.append(configuration)
        self.active_configuration_id = configuration["id"]
        self._refresh_configuration_table(select_id=configuration["id"])
        self._save_sorting_config()
        if hasattr(self, "status_label"):
            self.status_label.setText(f"Added configuration: {name}")

    def _rename_selected_sorting_configuration(self):
        selected = self._selected_sorting_configurations()
        if len(selected) != 1:
            return
        self._rename_sorting_configuration(selected[0])

    def _rename_sorting_configuration(self, configuration):
        if configuration is None:
            return
        old_name = str(configuration.get("name", ""))
        new_name, accepted = QtWidgets.QInputDialog.getText(self, "Rename sorting configuration", "Configuration name:", text=old_name)
        if not accepted:
            return
        new_name = str(new_name or "").strip()
        if not new_name:
            QtWidgets.QMessageBox.warning(self, "Rename sorting configuration", "The configuration name cannot be empty.")
            return
        duplicate = any(
            str(other.get("id", "")) != str(configuration.get("id", ""))
            and str(other.get("name", "")).strip().casefold() == new_name.casefold()
            for other in (self.sorting_configurations or [])
        )
        if duplicate:
            QtWidgets.QMessageBox.warning(self, "Rename sorting configuration", "Another configuration already uses that name.")
            return
        configuration["name"] = new_name
        self._refresh_configuration_table(select_id=configuration.get("id"))
        self._save_sorting_config()

    def _duplicate_selected_sorting_configuration(self):
        selected = self._selected_sorting_configurations()
        if len(selected) != 1:
            return
        configuration = selected[0]
        duplicate = {
            "id": self._new_configuration_id(),
            "name": self._unique_configuration_name(f"{configuration.get('name', 'Configuration')} copy"),
            "sorter_name": str(configuration.get("sorter_name", "")),
            "sorter_params": dict(configuration.get("sorter_params", {}) or {}),
            "preprocessing_pipeline": _json_safe(dict(
                configuration.get("preprocessing_pipeline", self.pipeline) or {}
            )),
            "enabled": True,
        }
        self.sorting_configurations.append(duplicate)
        self.active_configuration_id = duplicate["id"]
        self._load_active_configuration_into_editor()
        self._refresh_configuration_table(select_id=duplicate["id"])
        self._save_sorting_config()

    def _delete_selected_sorting_configuration(self):
        selected = self._selected_sorting_configurations()
        if not selected:
            return
        names = [str(configuration.get("name", "Configuration")) for configuration in selected]
        if len(names) == 1:
            question_text = f"Delete the configuration '{names[0]}'?"
        else:
            displayed_names = names[:12]
            name_lines = "\n".join(f"  - {name}" for name in displayed_names)
            if len(names) > len(displayed_names):
                name_lines += f"\n  - ... and {len(names) - len(displayed_names)} more"
            question_text = (
                f"Delete these {len(names)} configurations?\n\n{name_lines}"
            )
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete sorting configurations",
            question_text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
            if hasattr(QtWidgets.QMessageBox, "StandardButton")
            else QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        yes = QtWidgets.QMessageBox.StandardButton.Yes if hasattr(QtWidgets.QMessageBox, "StandardButton") else QtWidgets.QMessageBox.Yes
        if answer != yes:
            return
        config_ids = {str(configuration.get("id", "")) for configuration in selected}
        self.sorting_configurations = [
            configuration
            for configuration in (self.sorting_configurations or [])
            if str(configuration.get("id", "")) not in config_ids
        ]
        if self.active_configuration_id in config_ids:
            self.active_configuration_id = str(self.sorting_configurations[0].get("id", "")) if self.sorting_configurations else ""
            self._load_active_configuration_into_editor()
        self._refresh_configuration_table(select_id=self.active_configuration_id)
        self._save_sorting_config()
        if hasattr(self, "status_label"):
            count = len(config_ids)
            self.status_label.setText(
                f"Deleted {count} configuration{'s' if count != 1 else ''}"
            )

    def _enabled_sorting_configurations(self):
        return [dict(c) for c in (self.sorting_configurations or []) if bool(c.get("enabled", True))]

    def _set_busy(self, busy: bool):
        self._busy = bool(busy)
        for w in [
            getattr(self, "sorter_combo", None),
            getattr(self, "recording_select_btn", None),
            getattr(self, "params_btn", None),
            getattr(self, "add_configuration_btn", None),
            getattr(self, "summary_params_btn", None),
            getattr(self, "skip_failed_sorting_check", None),
            getattr(self, "configuration_table", None),
            getattr(self, "run_btn", None),
        ]:
            if w is not None:
                w.setEnabled(not self._busy)
        if hasattr(self, "run_btn"):
            self.run_btn.setText("Running queue..." if self._busy else "Run sorting queue")
        self._update_configuration_action_state()
        self._emit_busy_state(bool(busy), "Sorting running..." if busy else "Sorting ready.")

    def _recording_groups(self):
        return [g for g in (self.recipe.get("recording_groups", []) or []) if isinstance(g, dict)]

    def _available_recording_labels(self):
        labels = []
        for g in self._recording_groups():
            label = str(g.get("label", "")).strip()
            if label:
                labels.append(label)
        return list(dict.fromkeys(labels))

    def _normalise_recording_selection(self, labels=None):
        available = list(getattr(self, "_recording_label_list", []) or self._available_recording_labels())
        if labels is None:
            return available
        selected = []
        for label in labels or []:
            label = str(label).strip()
            if label in available and label not in selected:
                selected.append(label)
        return selected or available

    def _initial_selected_recording_labels(self):
        available = list(getattr(self, "_recording_label_list", []) or self._available_recording_labels())
        saved_multi = self.saved_config.get("selected_recordings", None) if isinstance(self.saved_config, dict) else None
        if isinstance(saved_multi, list):
            return self._normalise_recording_selection(saved_multi)

        # Backward compatibility with the old single-combo config.
        saved_single = str(self.saved_config.get("selected_recording", self.ALL_RECORDINGS_LABEL) if isinstance(self.saved_config, dict) else self.ALL_RECORDINGS_LABEL).strip()
        if not saved_single or saved_single == self.ALL_RECORDINGS_LABEL:
            return available
        return self._normalise_recording_selection([saved_single])

    def _selected_recording_labels(self):
        return self._normalise_recording_selection(getattr(self, "selected_recording_labels", None))

    def _selected_recording_text(self):
        selected = self._selected_recording_labels()
        available = list(getattr(self, "_recording_label_list", []) or self._available_recording_labels())
        if not available:
            return "No recordings"
        if len(selected) == len(available):
            return f"All recordings ({len(available)})"
        if len(selected) == 1:
            return selected[0]
        return f"{len(selected)} recordings selected"

    def _legacy_selected_recording_value(self):
        selected = self._selected_recording_labels()
        available = list(getattr(self, "_recording_label_list", []) or self._available_recording_labels())
        if not selected or len(selected) == len(available):
            return self.ALL_RECORDINGS_LABEL
        if len(selected) == 1:
            return selected[0]
        return "Multiple"

    def _update_recording_select_button_text(self):
        if hasattr(self, "recording_select_btn") and self.recording_select_btn is not None:
            self.recording_select_btn.setText(self._selected_recording_text())

    def _open_recording_selection_dialog(self):
        available = list(getattr(self, "_recording_label_list", []) or self._available_recording_labels())
        if not available:
            QtWidgets.QMessageBox.information(self, "Recording selection", "No recording groups are available.")
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Select recordings")
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        info = QtWidgets.QLabel("Select the recording group(s) to run spike sorting on.", dlg)
        info.setWordWrap(True)
        layout.addWidget(info)

        list_widget = QtWidgets.QListWidget(dlg)
        list_widget.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection if hasattr(QtWidgets.QAbstractItemView, "SelectionMode") else QtWidgets.QAbstractItemView.NoSelection)
        selected = set(self._selected_recording_labels())
        checked = QtCore.Qt.CheckState.Checked if hasattr(QtCore.Qt, "CheckState") else QtCore.Qt.Checked
        unchecked = QtCore.Qt.CheckState.Unchecked if hasattr(QtCore.Qt, "CheckState") else QtCore.Qt.Unchecked
        user_checkable = QtCore.Qt.ItemFlag.ItemIsUserCheckable if hasattr(QtCore.Qt, "ItemFlag") else QtCore.Qt.ItemIsUserCheckable
        enabled = QtCore.Qt.ItemFlag.ItemIsEnabled if hasattr(QtCore.Qt, "ItemFlag") else QtCore.Qt.ItemIsEnabled
        for label in available:
            item = QtWidgets.QListWidgetItem(label)
            item.setFlags(enabled | user_checkable)
            item.setCheckState(checked if label in selected else unchecked)
            list_widget.addItem(item)
        layout.addWidget(list_widget, stretch=1)

        quick_row = QtWidgets.QHBoxLayout()
        all_btn = QtWidgets.QPushButton("Select all", dlg)
        none_btn = QtWidgets.QPushButton("Clear", dlg)
        quick_row.addWidget(all_btn)
        quick_row.addWidget(none_btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)

        def _set_all(state):
            for i in range(list_widget.count()):
                list_widget.item(i).setCheckState(state)

        all_btn.clicked.connect(lambda: _set_all(checked))
        none_btn.clicked.connect(lambda: _set_all(unchecked))

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            if hasattr(QtWidgets.QDialogButtonBox, "StandardButton")
            else QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            parent=dlg,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        accepted_code = QtWidgets.QDialog.DialogCode.Accepted if hasattr(QtWidgets.QDialog, "DialogCode") else QtWidgets.QDialog.Accepted
        if dlg.exec() != accepted_code:
            return

        new_selected = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item.checkState() == checked:
                new_selected.append(item.text())

        if not new_selected:
            QtWidgets.QMessageBox.warning(self, "Recording selection", "At least one recording must be selected.")
            return

        self.selected_recording_labels = self._normalise_recording_selection(new_selected)
        self._update_recording_select_button_text()
        self._save_sorting_config()

    def _selected_groups(self):
        selected = set(self._selected_recording_labels())
        groups = self._recording_groups()
        return [g for g in groups if str(g.get("label", "")).strip() in selected]

    def _get_project_root(self):
        gui = self.sorting_info.get("GUI_instance", None)
        if gui is not None and hasattr(gui, "project_root"):
            try:
                return Path(gui.project_root)
            except Exception:
                pass
        path = self.sorting_info.get("path", None)
        if path is not None:
            try:
                return Path(path).resolve().parents[2]
            except Exception:
                pass
        return None

    def _discover_preprocessing_specs(self):
        if not callable(discover_preprocessings):
            return {}
        project_root = self._get_project_root()
        try:
            specs = discover_preprocessings(project_root=project_root) or {}
        except TypeError:
            # Compatibility with older discovery functions, if any.
            try:
                specs = discover_preprocessings(project_root) or {}
            except Exception as exc:
                if hasattr(self, "log_text"):
                    self._append_log(f"Could not discover preprocessing functions: {exc}")
                return {}
        except Exception as exc:
            if hasattr(self, "log_text"):
                self._append_log(f"Could not discover preprocessing functions: {exc}")
            return {}

        if not isinstance(specs, dict):
            return {}
        return specs

    def _make_sorting_preprocessing_steps(self):
        """Build the editor rows used for per-configuration preprocessing."""
        steps = []
        used = set()
        scope_order = {"package": 0, "project": 1}
        items = list((self.preprocessing_specs or {}).items())
        items.sort(key=lambda item: (
            scope_order.get(str((item[1] or {}).get("source_scope", "")), 99),
            int((item[1] or {}).get("discovery_order", 999999)),
            str(item[0]).lower(),
        ))
        for display_index, (name, raw_spec) in enumerate(items, start=1):
            spec = dict(raw_spec or {})
            slug = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_") or "step"
            base = f"{spec.get('source_scope', 'unknown')}_{slug}"
            key = base
            copy_index = 2
            while key in used:
                key = f"{base}_{copy_index}"
                copy_index += 1
            used.add(key)
            steps.append({
                "key": key,
                "display_index": display_index,
                "name": str(spec.get("name", name)),
                "description": str(spec.get("description", "")),
                "source": str(spec.get("source_scope", "")),
                "source_file": spec.get("source_file"),
                "func": spec.get("func"),
                "default_enabled": _PreprocessingOptionsDialog._to_bool(spec.get("default", False)),
                "default_order": spec.get("order", display_index),
                "params": dict(spec.get("params", {}) or {}),
                "debug_mode": bool(spec.get("debug_mode", False)),
                "raw_spec": spec,
            })
        return steps

    def _available_sorters(self):
        names = []
        try:
            import spikeinterface.sorters as ss
            available_sorters = getattr(ss, "available_sorters", None)
            if callable(available_sorters):
                names.extend(str(x) for x in (available_sorters() or []))
        except Exception:
            pass
        try:
            from spikeinterface.sorters import sorterlist
            sorter_dict = getattr(sorterlist, "sorter_dict", {}) or {}
            names.extend(str(k) for k in sorter_dict.keys())
        except Exception:
            pass
        if not names:
            names = ["mountainsort5", "kilosort2_5", "kilosort3", "kilosort4", "spykingcircus2", "tridesclous", "waveclus"]
        return sorted(dict.fromkeys(names), key=lambda s: s.lower())

    def _default_params_for_sorter(self, sorter_name: str):
        sorter_name = str(sorter_name).strip()
        errors = []
        try:
            from spikeinterface.sorters import get_default_sorter_params
            return dict(get_default_sorter_params(sorter_name) or {})
        except Exception as exc:
            errors.append(str(exc))
        try:
            import spikeinterface.sorters as ss
            f = getattr(ss, "get_default_sorter_params", None)
            if callable(f):
                return dict(f(sorter_name) or {})
        except Exception as exc:
            errors.append(str(exc))
        try:
            from spikeinterface.sorters import sorterlist
            sorter_cls = (getattr(sorterlist, "sorter_dict", {}) or {}).get(sorter_name)
            if sorter_cls is not None:
                f = getattr(sorter_cls, "default_params", None) or getattr(sorter_cls, "get_default_params", None)
                if callable(f):
                    return dict(f() or {})
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            self._append_log(f"Could not load default parameters for {sorter_name}: {'; '.join(errors)}")
        return {}

    def _param_descriptions_for_sorter(self, sorter_name: str) -> dict:
        try:
            return _sorter_param_descriptions(sorter_name)
        except Exception:
            return {}

    def _format_params_summary(self, sorter_name: str) -> str:
        params = self.sorter_params_by_name.get(str(sorter_name or "").strip(), {}) or {}
        n = len(params)
        if n == 0:
            return "No parameters loaded."
        preview = ", ".join(str(k) for k in list(params.keys())[:4])
        if n > 4:
            preview += ", ..."
        return f"{n} parameter(s): {preview}"

    def _update_sorter_status_label(self, sorter_name: str):
        # No persistent local/Docker status text in the UI.
        # The sorter installation is still checked when running; the log then
        # reports whether local execution or Docker was used.
        return

    def _on_sorter_changed(self, sorter_name):
        sorter_name = str(sorter_name or "").strip()
        if not sorter_name:
            return
        if getattr(self, "_loading_configuration_editor", False):
            return
        active = self._active_sorting_configuration()
        previous_sorter = str((active or {}).get("sorter_name", "") or "").strip()
        if active is not None and previous_sorter == sorter_name:
            params = dict(active.get("sorter_params", {}) or {})
        else:
            # A sorter change starts from that sorter's own defaults. Reusing
            # parameters from another queue row with the same sorter would make
            # the new configuration change silently and be hard to audit.
            params = self._default_params_for_sorter(sorter_name)
        self.sorter_params_by_name[sorter_name] = dict(params or {})
        if hasattr(self, "params_summary_label"):
            self.params_summary_label.setText(self._format_params_summary(sorter_name))
        self._update_sorter_status_label(sorter_name)
        self._save_sorting_config()
        if hasattr(self, "status_label"):
            self.status_label.setText(
                f"Prepared sorter '{sorter_name}'. Open Sorter parameters to add a configuration."
            )

    def _edit_params(self, *, add_as_configuration=True):
        sorter_name = str(self.sorter_combo.currentText()).strip()
        if not sorter_name:
            return
        params = self.sorter_params_by_name.get(sorter_name)
        if params is None:
            params = self._default_params_for_sorter(sorter_name)
        descriptions = self._param_descriptions_for_sorter(sorter_name)
        dlg = _SorterParamsDialog(
            self,
            sorter_name,
            params,
            param_descriptions=descriptions,
            accept_text="Add configuration" if add_as_configuration else "Save changes",
        )
        if not add_as_configuration:
            active = self._active_sorting_configuration()
            if active is not None:
                pipeline_label = self._configuration_parameter_pipeline_label(active)
                dlg.setWindowTitle(
                    f"Edit sorter parameters - {active.get('name', '')} - {pipeline_label}"
                )
        accepted = QtWidgets.QDialog.DialogCode.Accepted if hasattr(QtWidgets.QDialog, "DialogCode") else QtWidgets.QDialog.Accepted
        if dlg.exec() == accepted:
            edited_params = dlg.get_params()
            self.sorter_params_by_name[sorter_name] = dict(edited_params or {})
            active = self._active_sorting_configuration()
            if add_as_configuration:
                sorter_count = sum(
                    1 for existing in (self.sorting_configurations or [])
                    if str(existing.get("sorter_name", "")) == sorter_name
                )
                source_pipeline = (
                    active.get("preprocessing_pipeline", self.pipeline)
                    if isinstance(active, dict)
                    else self.pipeline
                )
                if not isinstance(source_pipeline, dict):
                    source_pipeline = self.pipeline
                name = self._unique_configuration_name(
                    f"{sorter_name} parameters {sorter_count + 1}"
                )
                active = {
                    "id": self._new_configuration_id(),
                    "name": name,
                    "sorter_name": sorter_name,
                    "sorter_params": dict(edited_params or {}),
                    "preprocessing_pipeline": _json_safe(dict(source_pipeline or {})),
                    "enabled": True,
                }
                self.sorting_configurations.append(active)
                self.active_configuration_id = str(active.get("id", ""))
                if hasattr(self, "status_label"):
                    self.status_label.setText(f"Added configuration: {name}")
            elif active is not None:
                active["sorter_name"] = sorter_name
                active["sorter_params"] = dict(edited_params or {})
            if hasattr(self, "params_summary_label"):
                self.params_summary_label.setText(self._format_params_summary(sorter_name))
            if active is not None:
                self._refresh_configuration_table(select_id=active.get("id"))
            self._save_sorting_config()


    def _safe_summary_float(self, value, default):
        try:
            out = float(value)
            if math.isfinite(out):
                return out
        except Exception:
            pass
        return float(default)

    def _populate_summary_events_after_show(self):
        """Populate the cached unit-summary event list from the inventory only.

        The main sorting panel only shows a checkbox and a parameters button.
        Event choices are cached here and shown later in the parameters dialog.
        """
        if getattr(self, "_summary_events_loaded", False):
            return
        self._summary_events_loaded = True
        try:
            options = self._summary_event_options()
            self._summary_event_options_cache = list(options or [])
            self._apply_default_summary_alignment(options)
            self._append_log(f"Loaded {len(options)} event marker name(s) from inventory.")
            self._save_sorting_config()
        except Exception:
            self._append_log("ERROR while reading event marker names from inventory:")
            self._append_log(traceback.format_exc())
        finally:
            if hasattr(self, "status_label"):
                self.status_label.setText("Ready.")

    def _selected_summary_files(self):
        groups = []
        try:
            groups = self._selected_groups()
        except Exception:
            groups = []
        if not groups:
            try:
                groups = self._recording_groups()
            except Exception:
                groups = []
        files = []
        for group in groups or []:
            for file_path in group.get("files", []) or []:
                text = str(file_path).strip()
                if text:
                    files.append(text)
        return list(dict.fromkeys(files))

    def _max_trial_length_for_summary_defaults(self):
        """Deprecated fast default.

        Older versions opened the selected NWB files to compute the maximum
        trial duration. That made the Sorting tab slow to display, especially
        when many NWB files were selected. Keep the method for compatibility,
        but do not open any NWB file here.
        """
        return 1.0

    def _default_summary_config(self, options=None):
        options = list(options if options is not None else getattr(self, "_summary_event_options_cache", []) or [])
        marker = ""
        for opt in options:
            if str(opt).strip().lower() == "trials":
                marker = str(opt).strip()
                break
        if not marker and options:
            marker = str(options[0]).strip()

        return {
            "event_marker": marker,
            "edge": "start",
            "before_s": 1.0,
            "after_s": 1.0,
            "plot_events": [],
        }

    def _apply_default_summary_alignment(self, options=None):
        # If no marker is stored yet, use trials by default when available;
        # otherwise use the first available event marker. The default window is
        # fixed at 1 s before and 1 s after so opening the Sorting tab never
        # requires reading NWB files.
        marker = str((getattr(self, "unit_summary_config", {}) or {}).get("event_marker", "") or "").strip()
        if marker:
            return
        default_cfg = self._default_summary_config(options)
        existing_plot_events = list((getattr(self, "unit_summary_config", {}) or {}).get("plot_events", []) or [])
        self.unit_summary_config.update(default_cfg)
        if existing_plot_events:
            self.unit_summary_config["plot_events"] = existing_plot_events

    def _open_summary_params(self):
        if not getattr(self, "_summary_events_loaded", False):
            try:
                self._populate_summary_events_after_show()
            except Exception:
                pass
        options = list(getattr(self, "_summary_event_options_cache", []) or [])
        self._apply_default_summary_alignment(options)
        default_cfg = self._default_summary_config(options)
        dlg = _UnitSummaryParamsDialog(
            self,
            options=options,
            config=dict(getattr(self, "unit_summary_config", {}) or {}),
            default_config=default_cfg,
        )
        accepted = QtWidgets.QDialog.DialogCode.Accepted if hasattr(QtWidgets.QDialog, "DialogCode") else QtWidgets.QDialog.Accepted
        if dlg.exec() == accepted:
            self.unit_summary_config.update(dlg.get_config())
            self._save_sorting_config()

    def _summary_event_options(self):
        """Return event marker names from nwb_inventory/content_index.

        This intentionally does not open any NWB file. It only reads the
        already generated inventory tables. Event timestamps are read later by
        _map_event_frames_for_group(), and only if unit-summary generation is
        enabled during sorting.

        Important: include ``trials`` even if it is not listed as a normal
        event object in content_index. The inventory sheet usually contains
        ``number_of_trials`` for this case, and _event_times_from_nwb() knows
        how to read nwbfile.trials when marker == "trials".
        """
        event_types = {
            "TimeIntervals",
            "AnnotationSeries",
            "BehavioralEvents",
            "BehavioralEpochs",
            "EventDetection",
        }
        gui = self.sorting_info.get("GUI_instance", None)
        cdf = getattr(gui, "content_df", None) if gui is not None else None
        inv_df = None
        if gui is not None:
            inv_df = getattr(gui, "full_df", None)
            if inv_df is None or getattr(inv_df, "empty", True):
                inv_df = getattr(gui, "df", None)

        project_root = self._get_project_root()
        if (cdf is None or getattr(cdf, "empty", True)) and project_root is not None:
            inv_path = Path(project_root) / "Data" / "nwb_inventory.xlsx"
            if inv_path.exists():
                try:
                    import pandas as pd
                    cdf = _pp_read_excel(inv_path, sheet_name="content_index")
                except Exception:
                    cdf = None
                if inv_df is None or getattr(inv_df, "empty", True):
                    try:
                        import pandas as pd
                        inv_df = _pp_read_excel(inv_path, sheet_name="inventory")
                    except Exception:
                        inv_df = None

        files = []
        for group in self._recording_groups():
            for file_path in group.get("files", []) or []:
                text = str(file_path).strip()
                if text:
                    files.append(text)
        files = list(dict.fromkeys(files))
        if not files:
            return []

        options = set()

        # ``trials`` is a valid alignment target, but it is not always stored as
        # an event-like row in content_index. Detect it from either content_index
        # or the inventory sheet without opening any NWB file.
        def _add_trials_from_table(df):
            if df is None or getattr(df, "empty", True) or "file_path" not in getattr(df, "columns", []):
                return
            try:
                sub_df = df[df["file_path"].astype(str).isin(files)].copy()
            except Exception:
                return
            if sub_df.empty:
                return
            for col in ("name", "nwb_path", "neurodata_type"):
                if col in sub_df.columns:
                    try:
                        values = sub_df[col].astype(str).str.strip().str.lower()
                        if values.eq("trials").any() or values.str.endswith("/trials").any():
                            options.add("trials")
                            return
                    except Exception:
                        pass
            if "number_of_trials" in sub_df.columns:
                try:
                    ntr = np.asarray(sub_df["number_of_trials"].fillna(0), dtype=float)
                    if np.any(ntr > 0):
                        options.add("trials")
                        return
                except Exception:
                    # Some inventories store numeric columns as strings.
                    try:
                        ntr = sub_df["number_of_trials"].astype(str).str.extract(r"(\d+)")[0].fillna("0").astype(int)
                        if (ntr > 0).any():
                            options.add("trials")
                            return
                    except Exception:
                        pass

        _add_trials_from_table(cdf)
        _add_trials_from_table(inv_df)

        if cdf is None or getattr(cdf, "empty", True):
            return sorted(options, key=lambda x: _natural_text_key(x))

        required_any = {"file_path", "neurodata_type"}
        if not required_any.issubset(set(getattr(cdf, "columns", []))):
            return sorted(options, key=lambda x: _natural_text_key(x))

        try:
            sub = cdf[cdf["file_path"].astype(str).isin(files)].copy()
        except Exception:
            return sorted(options, key=lambda x: _natural_text_key(x))
        if sub.empty:
            return sorted(options, key=lambda x: _natural_text_key(x))

        try:
            sub["neurodata_type"] = sub["neurodata_type"].astype(str).str.strip()
            evt_sub = sub[sub["neurodata_type"].isin(event_types)].copy()
        except Exception:
            return sorted(options, key=lambda x: _natural_text_key(x))
        if evt_sub.empty:
            return sorted(options, key=lambda x: _natural_text_key(x))

        for _, row in evt_sub.iterrows():
            marker = ""
            # In the inventory, the "name" column is the actual NWB object key
            # used by _event_times_from_nwb(). Prefer it over the display path.
            if "name" in evt_sub.columns:
                marker = str(row.get("name", "") or "").strip()
            if not marker and "nwb_path" in evt_sub.columns:
                path = str(row.get("nwb_path", "") or "").strip().replace("\\\\", "/").replace("\\", "/")
                marker = path.strip("/").split("/")[-1] if path else ""
            if marker:
                options.add(marker)
        return sorted(options, key=lambda x: _natural_text_key(x))

    def _get_summary_config(self):
        enabled = True

        try:
            if not getattr(self, "_summary_events_loaded", False):
                self._populate_summary_events_after_show()
        except Exception:
            pass
        try:
            self._apply_default_summary_alignment(getattr(self, "_summary_event_options_cache", []) or [])
        except Exception:
            pass

        cfg = dict(getattr(self, "unit_summary_config", {}) or {})
        marker = str(cfg.get("event_marker", "") or "").strip()
        edge = str(cfg.get("edge", "start") or "start").strip().lower()
        if edge not in {"start", "stop", "center"}:
            edge = "start"
        before = self._safe_summary_float(cfg.get("before_s", 0.0), 0.0)
        after = self._safe_summary_float(cfg.get("after_s", 2.0), 2.0)
        plot_events = []
        raw_plot_events = cfg.get("plot_events", [])
        if isinstance(raw_plot_events, list):
            for event_cfg in raw_plot_events:
                if not isinstance(event_cfg, dict):
                    continue
                event_marker = str(event_cfg.get("event_marker", "") or "").strip()
                if not event_marker:
                    continue
                display = str(event_cfg.get("display", event_cfg.get("edge", "duration")) or "duration").strip().lower()
                if display not in {"duration", "start", "stop", "center"}:
                    display = "duration"
                color = str(event_cfg.get("color", "#d62728") or "#d62728").strip()
                try:
                    qcolor = QtGui.QColor(color)
                    color = qcolor.name() if qcolor.isValid() else "#d62728"
                except Exception:
                    color = "#d62728"
                try:
                    alpha = float(event_cfg.get("alpha", 0.18))
                    if not math.isfinite(alpha):
                        alpha = 0.18
                    alpha = max(0.0, min(1.0, alpha))
                except Exception:
                    alpha = 0.18
                plot_events.append({
                    "enabled": bool(event_cfg.get("enabled", True)),
                    "event_marker": event_marker,
                    "display": display,
                    "color": color,
                    "alpha": alpha,
                })
        return {
            "enabled": enabled,
            "event_marker": marker,
            "edge": edge,
            "before_s": max(0.0, before),
            "after_s": max(0.0, after),
            "plot_events": plot_events,
        }

    def _sorting_config_path(self):
        if self.sorting_path is None:
            return None
        return self.sorting_path / "sorting_config.json"

    def _load_sorting_config(self):
        path = self._sorting_config_path()
        if path is None or not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = _pp_json_load(f)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _save_sorting_config(self):
        if not hasattr(self, "sorter_combo"):
            return
        sorter_name = str(self.sorter_combo.currentText()).strip()
        if not sorter_name:
            return
        params = dict(self.sorter_params_by_name.get(sorter_name, {}) or {})
        payload = {
            "version": 4,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "selected_sorter": sorter_name,
            "selected_recording": self._legacy_selected_recording_value(),
            "selected_recordings": self._selected_recording_labels(),
            "sorter_params": params,
            "active_configuration_id": str(getattr(self, "active_configuration_id", "") or ""),
            "sorting_configurations": [
                {
                    "id": str(configuration.get("id", "")),
                    "name": str(configuration.get("name", "")),
                    "sorter_name": str(configuration.get("sorter_name", "")),
                    "sorter_params": _json_safe(dict(configuration.get("sorter_params", {}) or {})),
                    "preprocessing_pipeline": _json_safe(dict(
                        configuration.get("preprocessing_pipeline", self.pipeline) or {}
                    )),
                    "enabled": bool(configuration.get("enabled", True)),
                }
                for configuration in (getattr(self, "sorting_configurations", []) or [])
            ],
            "unit_summary": self._get_summary_config(),
            "use_multiprocessing": bool(self.use_multiprocessing),
            "skip_failed_sorting": bool(getattr(self, "skip_failed_sorting_check", None).isChecked()) if getattr(self, "skip_failed_sorting_check", None) is not None else True,
            "n_jobs": self.n_jobs if self.use_multiprocessing else 1,
        }
        path = self._sorting_config_path()
        if path is None:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                _pp_json_dump(payload, f, indent=2, ensure_ascii=False)
            self.saved_config = payload
            if hasattr(self, "status_label"):
                self.status_label.setText(f"Saved config: {path.name}")
        except Exception as exc:
            if hasattr(self, "status_label"):
                self.status_label.setText(f"Could not save config: {exc}")

    def _existing_output_policy(self, groups, sorter_name, *, params=None, summary_config=None, recipe=None):
        """Ask only when the exact same recording/preprocessing/sorter config exists."""
        if self.sorting_path is None:
            return "cancel"
        existing = []
        signature_recipe = dict(self.recipe if recipe is None else recipe)
        for g in groups:
            label = str(g.get("label", "recording")).strip() or "recording"
            base_folder = _sorting_runs_root(self.sorting_path)
            run_signature = _build_sorting_run_signature(
                label=label,
                group=g,
                recipe=signature_recipe,
                pipeline=self.pipeline,
                sorter_name=sorter_name,
                sorter_params=dict(params or {}),
                summary_config=dict(summary_config or {}),
            )
            run_hash = _signature_hash(run_signature)
            existing.extend(_find_matching_sorting_runs(base_folder, run_hash))
        existing = sorted(set(existing), key=lambda p: _natural_text_key(str(p)))
        if not existing:
            return "none"
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Identical sorting run already exists")
        box.setText(f"{len(existing)} identical sorting run folder(s) already exist.")
        box.setInformativeText(
            "The selected recording, preprocessing parameters, sorter, sorter parameters, "
            "and unit-summary parameters match an existing run."
        )
        box.setDetailedText("\n".join(str(p) for p in existing))
        delete_btn = box.addButton("Delete and run again", QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole)
        skip_btn = box.addButton("Skip identical run(s)", QtWidgets.QMessageBox.ButtonRole.ActionRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.ActionRole)
        cancel_btn = box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is delete_btn:
            return "delete"
        if clicked is skip_btn:
            return "skip"
        return "cancel"

    def _existing_queue_output_policy(self, groups, configurations, *, summary_config=None, recipe=None):
        """Ask once when one or more exact queue runs already exist."""
        if self.sorting_path is None:
            return "cancel"
        signature_recipe = dict(self.recipe if recipe is None else recipe)
        existing = []
        for configuration in configurations or []:
            config_name = str(configuration.get("name", "Configuration"))
            sorter_name = str(configuration.get("sorter_name", "")).strip()
            params = dict(configuration.get("sorter_params", {}) or {})
            configuration_pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
            if not isinstance(configuration_pipeline, dict):
                configuration_pipeline = self.pipeline
            for group in groups or []:
                label = str(group.get("label", "recording")).strip() or "recording"
                run_signature = _build_sorting_run_signature(
                    label=label,
                    group=group,
                    recipe=signature_recipe,
                    pipeline=configuration_pipeline,
                    sorter_name=sorter_name,
                    sorter_params=params,
                    summary_config=dict(summary_config or {}),
                )
                run_hash = _signature_hash(run_signature)
                for folder in _find_matching_sorting_runs(_sorting_runs_root(self.sorting_path), run_hash):
                    existing.append((config_name, label, Path(folder)))

        unique = []
        seen = set()
        for config_name, label, folder in existing:
            key = str(folder)
            if key in seen:
                continue
            seen.add(key)
            unique.append((config_name, label, folder))
        if not unique:
            return "none"

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Identical sorting runs already exist")
        box.setText(f"{len(unique)} queued sorting run(s) already have an identical output.")
        box.setInformativeText(
            "The recording, preprocessing parameters, sorter, sorter parameters, and unit-summary parameters "
            "match existing runs. Choose one action for every identical run in this queue."
        )
        box.setDetailedText("\n".join(f"{name} | {label}\n  {folder}" for name, label, folder in unique))
        delete_btn = box.addButton(
            "Delete and run again",
            QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole,
        )
        skip_btn = box.addButton(
            "Skip identical run(s)",
            QtWidgets.QMessageBox.ButtonRole.ActionRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.ActionRole,
        )
        box.addButton(
            "Cancel",
            QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole,
        )
        box.exec()
        if box.clickedButton() is delete_btn:
            return "delete"
        if box.clickedButton() is skip_btn:
            return "skip"
        return "cancel"

    def _duplicate_queue_run_details(self, groups, configurations, *, summary_config=None, recipe=None):
        signature_recipe = dict(self.recipe if recipe is None else recipe)
        first_by_hash = {}
        duplicates = []
        for configuration in configurations or []:
            config_name = str(configuration.get("name", "Configuration"))
            sorter_name = str(configuration.get("sorter_name", "")).strip()
            params = dict(configuration.get("sorter_params", {}) or {})
            configuration_pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
            if not isinstance(configuration_pipeline, dict):
                configuration_pipeline = self.pipeline
            for group in groups or []:
                label = str(group.get("label", "recording")).strip() or "recording"
                signature = _build_sorting_run_signature(
                    label=label,
                    group=group,
                    recipe=signature_recipe,
                    pipeline=configuration_pipeline,
                    sorter_name=sorter_name,
                    sorter_params=params,
                    summary_config=dict(summary_config or {}),
                )
                run_hash = _signature_hash(signature)
                current = (config_name, label)
                if run_hash in first_by_hash:
                    duplicates.append((first_by_hash[run_hash], current))
                else:
                    first_by_hash[run_hash] = current
        return duplicates

    def _bad_channel_mismatch_policy_for_sorting(self, groups):
        """Ask how to handle inconsistent bad-channel metadata before concatenation."""
        if not bool((self.recipe or {}).get("remove_bad_channels", False)):
            return "per_file"

        mismatched = []
        detail_lines = []
        for group in list(groups or []):
            files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
            if len(files) <= 1:
                continue

            per_file = []
            for file_path in files:
                meta = _RecordingLoadWorker._bad_channel_metadata_from_nwb(file_path)
                bad_labels = [str(x) for x in (meta.get("bad_labels", []) or [])]
                has_bad_column = bool(meta.get("has_bad_channel_column", False))
                key = tuple(sorted(set(bad_labels), key=_natural_text_key))
                per_file.append((file_path, key, has_bad_column, bad_labels))

            keys = {entry[1] for entry in per_file}
            if len(keys) <= 1:
                continue

            label = str(group.get("label", "recording")).strip() or "recording"
            mismatched.append(label)
            detail_lines.append(f"Recording group: {label}")
            for file_path, _key, has_bad_column, bad_labels in per_file:
                if has_bad_column:
                    text = ", ".join(bad_labels[:40]) if bad_labels else "none"
                    if len(bad_labels) > 40:
                        text += f", ... ({len(bad_labels)} total)"
                else:
                    text = "no bad_channel column found"
                detail_lines.append(f"  {Path(file_path).name}: {text}")
            detail_lines.append("")

        if not mismatched:
            return "per_file"

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Different bad channels across files")
        box.setText("Some concatenated NWB files do not have the same bad channels.")
        box.setInformativeText(
            "Choose one channel rule for the concatenated recording.\n\n"
            "Recommended: keep only channels that are good in every file. This removes any channel marked bad in at least one file.\n\n"
            "Alternative: keep channels that are good in at least one file. This keeps more channels, but channels marked bad in some files will remain in those parts of the concatenated recording."
        )
        box.setDetailedText("\n".join(detail_lines).strip())
        keep_all_good_btn = box.addButton(
            "Keep channels good in every file",
            QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole,
        )
        keep_any_good_btn = box.addButton(
            "Keep channels good in at least one file",
            QtWidgets.QMessageBox.ButtonRole.ActionRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.ActionRole,
        )
        cancel_btn = box.addButton(
            "Cancel sorting",
            QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole,
        )
        try:
            box.setDefaultButton(keep_all_good_btn)
        except Exception:
            pass
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep_all_good_btn:
            return "good_in_all_files"
        if clicked is keep_any_good_btn:
            return "good_in_any_file"
        return None

    def _enabled_preprocessing_debug_names(self, pipeline=None):
        pipeline = pipeline if isinstance(pipeline, dict) else self.pipeline
        enabled = [s for s in (pipeline.get("enabled_pipeline", []) or []) if isinstance(s, dict)]
        if not enabled:
            return []

        by_name = {}
        for name, spec in (self.preprocessing_specs or {}).items():
            if not isinstance(spec, dict):
                continue
            spec_name = str(spec.get("name", name)).strip()
            by_name[spec_name] = spec
            by_name[str(name).strip()] = spec

        debug_names = []
        for step in enabled:
            name = str(step.get("name", "")).strip()
            spec = by_name.get(name, {})
            func = spec.get("func") if isinstance(spec, dict) else None
            if bool(step.get("debug_mode", False) or getattr(func, "debug_mode", False) or spec.get("debug_mode", False)):
                debug_names.append(name or "preprocessing step")
        return debug_names

    def _run_sorting(self):
        if self._busy:
            return
        groups = self._selected_groups()
        configurations = self._enabled_sorting_configurations()
        if not configurations:
            QtWidgets.QMessageBox.warning(self, "Sorting", "Select at least one configuration in the Run column.")
            return
        if not groups:
            QtWidgets.QMessageBox.warning(self, "Sorting", "Please select a recording.")
            return
        if self.sorting_path is None:
            QtWidgets.QMessageBox.warning(self, "Sorting", "Missing spike sorting folder path.")
            return
        run_recipe = dict(self.recipe or {})
        bad_channel_policy = str(run_recipe.get("bad_channel_mismatch_policy", "per_file") or "per_file").strip().lower()
        if bad_channel_policy not in {"per_file", "good_in_all_files", "good_in_any_file"}:
            bad_channel_policy = "per_file"
            run_recipe["bad_channel_mismatch_policy"] = bad_channel_policy

        self._save_sorting_config()
        use_multiprocessing = bool(getattr(self, "use_multiprocessing", _launcher_multiprocessing_enabled()))
        n_jobs = int(getattr(self, "n_jobs", _available_multiprocessing_jobs(_launcher_n_jobs()))) if use_multiprocessing else 1
        skip_failed_sorting = bool(getattr(self, "skip_failed_sorting_check", None).isChecked()) if getattr(self, "skip_failed_sorting_check", None) is not None else True
        effective_configurations = []
        worker_keys_by_configuration = {}
        for configuration in configurations:
            sorter_name = str(configuration.get("sorter_name", "")).strip()
            if not sorter_name:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Sorting",
                    f"Configuration '{configuration.get('name', 'Configuration')}' has no sorter.",
                )
                return
            params = dict(configuration.get("sorter_params", {}) or {})
            if use_multiprocessing:
                params, worker_keys = _apply_worker_count_to_params(params, enabled=True, n_jobs=n_jobs)
            else:
                worker_keys = []
            effective = dict(configuration)
            effective["sorter_name"] = sorter_name
            effective["sorter_params"] = params
            configuration_pipeline = configuration.get("preprocessing_pipeline", self.pipeline)
            if not isinstance(configuration_pipeline, dict):
                configuration_pipeline = self.pipeline
            effective["preprocessing_pipeline"] = _json_safe(dict(configuration_pipeline or {}))
            effective_configurations.append(effective)
            worker_keys_by_configuration[str(configuration.get("id", ""))] = list(worker_keys)

        summary_config = self._get_summary_config()
        duplicate_runs = self._duplicate_queue_run_details(
            groups,
            effective_configurations,
            summary_config=summary_config,
            recipe=run_recipe,
        )
        if duplicate_runs:
            details = "\n".join(
                f"{first_name} and {duplicate_name} | {label}"
                for (first_name, label), (duplicate_name, _duplicate_label) in duplicate_runs
            )
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Warning)
            box.setWindowTitle("Duplicate queue configurations")
            box.setText("Some enabled configurations would create identical sorting runs.")
            box.setInformativeText("Edit, disable, or delete one of each identical pair before launching the queue.")
            box.setDetailedText(details)
            box.exec()
            return

        policy = self._existing_queue_output_policy(
            groups,
            effective_configurations,
            summary_config=summary_config,
            recipe=run_recipe,
        )
        if policy == "cancel":
            return
        self._show_log_panel()
        self.log_text.clear()
        self._reset_summary_browser([])
        debug_preprocessing_names = []
        for configuration in effective_configurations:
            for name in self._enabled_preprocessing_debug_names(
                configuration.get("preprocessing_pipeline", self.pipeline)
            ):
                if name not in debug_preprocessing_names:
                    debug_preprocessing_names.append(name)

        self._append_log(f"Starting sorting queue: {len(effective_configurations)} configuration(s)")
        self._append_log(f"Selected recordings: {', '.join(self._selected_recording_labels())}")
        self._append_log("Configurations:")
        for index, configuration in enumerate(effective_configurations, start=1):
            config_name = str(configuration.get("name", "Configuration"))
            sorter_name = str(configuration.get("sorter_name", ""))
            self._append_log(f"  {index}. {config_name} | sorter={sorter_name}")
            self._append_log(
                f"     preprocessing={self._configuration_preprocessing_pipeline_label(configuration)}"
            )
            self._append_log(f"     parameters={configuration.get('sorter_params', {})}")
        if debug_preprocessing_names:
            self._append_log(
                "Debug mode enabled by preprocessing step(s): "
                + ", ".join(debug_preprocessing_names)
            )
            self._append_log("Sorting will run on the GUI thread so breakpoints can stop inside preprocessing code.")
        self._append_log(f"Unit summary: {summary_config}")
        if use_multiprocessing:
            self._append_log(f"Multiprocessing enabled where supported: n_jobs={n_jobs}")
            for configuration in effective_configurations:
                worker_keys = worker_keys_by_configuration.get(str(configuration.get("id", "")), [])
                if worker_keys:
                    self._append_log(
                        f"{configuration.get('name', 'Configuration')} worker-count parameter(s) updated: "
                        + ", ".join(worker_keys)
                    )
        else:
            self._append_log("Multiprocessing disabled.")
        if skip_failed_sorting:
            self._append_log("Skip failed sorting enabled.")
        else:
            self._append_log("Skip failed sorting disabled.")
        if bad_channel_policy == "good_in_all_files":
            self._append_log("Bad-channel mismatch rule: keep only channels good in every concatenated file.")
        elif bad_channel_policy == "good_in_any_file":
            self._append_log("Bad-channel mismatch rule: keep channels good in at least one concatenated file.")
        self._append_log("")
        self._set_busy(True)
        total_runs = len(groups) * len(effective_configurations)
        self.status_label.setText(f"Running sorting queue... 0/{total_runs} run(s) completed")

        self._worker = _SortingQueueWorker(
            sorting_path=self.sorting_path,
            recipe=run_recipe,
            pipeline=self.pipeline,
            preprocessing_specs=self.preprocessing_specs,
            groups=groups,
            configurations=effective_configurations,
            existing_policy=policy,
            summary_config=summary_config,
            use_multiprocessing=use_multiprocessing,
            n_jobs=n_jobs,
            skip_failed_sorting=skip_failed_sorting,
        )
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._on_sorting_progress)
        self._worker.finished.connect(self._on_sorting_finished)
        self._worker.failed.connect(self._on_sorting_failed)

        if debug_preprocessing_names:
            self._thread = None
            self._worker.run()
            return

        self._thread = QtCore.QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_sorting_progress(self, current, total, label, state):
        if not hasattr(self, "status_label"):
            return
        try:
            current = int(current)
            total = int(total)
        except Exception:
            current, total = 0, 0
        label = str(label or "").strip()
        state = str(state or "running").strip().lower()

        if state == "finished":
            text = f"Running sorting queue... {current}/{total} run(s) completed"
            if label:
                text += f" ({label})"
        elif state == "skipped":
            text = f"Running sorting queue... {current}/{total} run(s), current run skipped"
            if label:
                text += f" ({label})"
        elif state == "failed":
            text = f"Running sorting queue... {current}/{total} run(s), current run failed"
            if label:
                text += f" ({label})"
        else:
            text = f"Running sorting queue... run {current}/{total}"
            if label:
                text += f": {label}"
        self.status_label.setText(text)

    def _on_sorting_finished(self, summary_paths, failed_count=0):
        if self.sorting_path is not None:
            _pp_compact(self.sorting_path)
        self._append_log("")
        failed_count = int(failed_count or 0)
        if failed_count:
            self._append_log(f"Sorting queue finished with {failed_count} failed run(s).")
        else:
            self._append_log("Sorting queue finished.")
        for p in summary_paths or []:
            self._append_log(f"Run summary: {p}")
        has_summary_images = self._load_summary_browser_from_run_summaries(summary_paths)
        if has_summary_images:
            suffix = f" {failed_count} run(s) failed." if failed_count else ""
            self.status_label.setText("Sorting queue finished. Unit summaries are ready to browse." + suffix)
            self._show_summary_panel()
        else:
            suffix = f" {failed_count} run(s) failed." if failed_count else ""
            self.status_label.setText("Sorting queue finished. No unit-summary PNG files were found." + suffix)
            self._show_log_panel()
        self._set_busy(False)
        self._thread = None
        self._worker = None

    def _on_sorting_failed(self, error_text):
        self._show_log_panel()
        self._append_log("")
        self._append_log("ERROR: Sorting failed")
        self._append_log(str(error_text))
        self.status_label.setText("Sorting failed.")
        self._set_busy(False)
        self._thread = None
        self._worker = None




class _UnitSummaryImageBrowserWidget(QtWidgets.QWidget):
    """Simple PNG summary browser used by the sorting and curation views."""

    def __init__(self, records=None, *, start_index=0, start_variant_kind=None, show_log_callback=None, parent=None):
        super().__init__(parent)
        self.summary_records = list(records or [])
        self.summary_record_index = max(0, int(start_index or 0))
        if self.summary_records:
            self.summary_record_index = min(self.summary_record_index, len(self.summary_records) - 1)
        self.summary_unit_index = 0
        self.summary_variant_index = 0
        self._preferred_summary_variant_kind = str(start_variant_kind or "").strip().lower()
        self._summary_pixmap_original = None
        self._show_log_callback = show_log_callback
        self._build_ui()
        self.summary_variant_index = self._preferred_variant_index_for_current_record(self._preferred_summary_variant_kind)
        self._refresh_summary_recording_combo()
        self._update_summary_image()

    @staticmethod
    def find_summary_images(summary_folder):
        summary_folder = Path(summary_folder)
        candidates = []
        preferred = summary_folder / "png_version"
        if preferred.exists():
            candidates.extend(preferred.glob("Unit_*.png"))
            if not candidates:
                candidates.extend(preferred.glob("*.png"))
        if not candidates:
            candidates.extend(summary_folder.glob("Unit_*.png"))
            candidates.extend(summary_folder.glob("*.png"))
        if not candidates:
            for suffix in ("*.jpg", "*.jpeg", "*.webp"):
                candidates.extend(summary_folder.rglob(suffix))
        unique = []
        seen = set()
        for p in sorted(candidates, key=lambda p: _natural_text_key(p.name)):
            try:
                key = str(p.resolve())
            except Exception:
                key = str(p)
            if key not in seen and p.is_file():
                unique.append(p)
                seen.add(key)
        return unique

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)

        self.summary_recording_combo = QtWidgets.QComboBox(self)
        self.summary_recording_combo.setMinimumWidth(220)
        self.summary_variant_combo = QtWidgets.QComboBox(self)
        self.summary_variant_combo.setMinimumWidth(150)
        self.summary_prev_unit_btn = QtWidgets.QPushButton("◀ Unit", self)
        self.summary_next_unit_btn = QtWidgets.QPushButton("Unit ▶", self)

        for btn in (self.summary_prev_unit_btn, self.summary_next_unit_btn):
            try:
                btn.setMinimumHeight(28)
                btn.setMaximumWidth(90)
            except Exception:
                pass

        toolbar.addWidget(QtWidgets.QLabel("Recording:", self))
        toolbar.addWidget(self.summary_recording_combo, stretch=1)
        toolbar.addWidget(QtWidgets.QLabel("Summary:", self))
        toolbar.addWidget(self.summary_variant_combo)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.summary_prev_unit_btn)
        toolbar.addWidget(self.summary_next_unit_btn)
        toolbar.addStretch(1)

        if callable(self._show_log_callback):
            self.summary_show_log_btn = QtWidgets.QPushButton("Show log", self)
            try:
                self.summary_show_log_btn.setMinimumHeight(28)
                self.summary_show_log_btn.setMaximumWidth(90)
            except Exception:
                pass
            self.summary_show_log_btn.clicked.connect(self._show_log_callback)
            toolbar.addWidget(self.summary_show_log_btn)
        else:
            self.summary_show_log_btn = None

        layout.addLayout(toolbar)

        self.summary_image_info = QtWidgets.QLabel("No unit summary image loaded.", self)
        self.summary_image_info.setWordWrap(True)
        self.summary_image_info.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter
        )
        self.summary_image_info.setStyleSheet("QLabel { font-weight: 600; padding: 4px; }")
        layout.addWidget(self.summary_image_info)

        self.summary_scroll = QtWidgets.QScrollArea(self)
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter
        )
        self.summary_image_label = QtWidgets.QLabel("No summary image available.", self.summary_scroll)
        self.summary_image_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter
        )
        self.summary_image_label.setStyleSheet("QLabel { background: #f6f8fa; color: #57606a; }")
        self.summary_image_label.setMinimumSize(300, 220)
        self.summary_scroll.setWidget(self.summary_image_label)
        layout.addWidget(self.summary_scroll, stretch=1)

        self.summary_recording_combo.currentIndexChanged.connect(self._on_summary_recording_combo_changed)
        self.summary_variant_combo.currentIndexChanged.connect(self._on_summary_variant_combo_changed)
        self.summary_prev_unit_btn.clicked.connect(self._summary_previous_unit)
        self.summary_next_unit_btn.clicked.connect(self._summary_next_unit)

        try:
            self.summary_scroll.viewport().installEventFilter(self)
        except Exception:
            pass

        self._update_summary_browser_controls()

    def eventFilter(self, obj, event):
        try:
            if hasattr(self, "summary_scroll") and obj is self.summary_scroll.viewport():
                event_type = QtCore.QEvent.Type.Resize if hasattr(QtCore.QEvent, "Type") else QtCore.QEvent.Resize
                if event.type() == event_type and self._summary_pixmap_original is not None:
                    QtCore.QTimer.singleShot(0, self._render_summary_pixmap)
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def set_records(self, records=None, *, start_index=0):
        self.summary_records = list(records or [])
        self.summary_record_index = max(0, int(start_index or 0))
        if self.summary_records:
            self.summary_record_index = min(self.summary_record_index, len(self.summary_records) - 1)
        self.summary_unit_index = 0
        self.summary_variant_index = 0
        self._summary_pixmap_original = None
        self._refresh_summary_recording_combo()
        self._update_summary_image()

    def _summary_record_display_label(self, record, index=None):
        record = dict(record or {})
        recording = str(record.get("recording", "recording") or "recording")
        configuration = str(record.get("configuration", "") or "")
        sorter = str(record.get("sorter", "") or "")
        run_name = str(record.get("run_name", "") or "")
        variants = self._summary_variants_for_record(record)
        prefix = f"{recording}"
        suffix_parts = []
        if configuration:
            suffix_parts.append(configuration)
        if sorter:
            suffix_parts.append(sorter)
        if run_name:
            suffix_parts.append(run_name)
        if variants:
            suffix_parts.append(f"{len(variants)} summary version{'s' if len(variants) != 1 else ''}")
        label = prefix + " | " + " | ".join(suffix_parts)
        if index is not None:
            label = f"{index + 1}. {label}"
        return label

    def _summary_variants_for_record(self, record):
        record = dict(record or {})
        variants = record.get("variants", None)
        if isinstance(variants, list) and variants:
            clean = []
            for idx, variant in enumerate(variants):
                if not isinstance(variant, dict):
                    continue
                v = dict(variant)
                v.setdefault("label", "Original" if idx == 0 else f"Version {idx + 1}")
                v.setdefault("images", [])
                v.setdefault("summary_folder", record.get("summary_folder", ""))
                clean.append(v)
            if clean:
                return clean
        # Compatibility with old records where images were stored directly on the record.
        return [{
            "kind": record.get("kind", "original"),
            "label": record.get("variant_label", "Original"),
            "summary_folder": record.get("summary_folder", ""),
            "images": list(record.get("images", []) or []),
        }]

    def _preferred_variant_index_for_current_record(self, variant_kind=None):
        """Return the variant index to select for the current record.

        Curation-table shortcuts use this to open the same summary viewer with
        either the original/uncurated summary or the curated summary already
        selected in the Summary combo.  If the requested variant does not exist,
        the viewer falls back to the first available summary.
        """
        kind = str(variant_kind or "").strip().lower()
        if not kind:
            return 0
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        aliases = {
            "original": {"original", "uncurated", "raw"},
            "uncurated": {"original", "uncurated", "raw"},
            "curated": {"curated"},
        }
        wanted = aliases.get(kind, {kind})
        for idx, variant in enumerate(variants):
            vkind = str(variant.get("kind", "") or "").strip().lower()
            vlabel = str(variant.get("label", "") or "").strip().lower()
            if vkind in wanted or vlabel in wanted:
                return idx
        return 0

    def _current_summary_variant(self):
        record = self._current_summary_record()
        if record is None:
            return None
        variants = self._summary_variants_for_record(record)
        if not variants:
            return None
        self.summary_variant_index = max(0, min(int(getattr(self, "summary_variant_index", 0)), len(variants) - 1))
        return variants[self.summary_variant_index]

    def _refresh_summary_variant_combo(self):
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        try:
            self.summary_variant_combo.blockSignals(True)
            self.summary_variant_combo.clear()
            if not variants:
                self.summary_variant_combo.addItem("No summary", -1)
                self.summary_variant_combo.setEnabled(False)
            else:
                for idx, variant in enumerate(variants):
                    images = list(variant.get("images", []) or [])
                    label = str(variant.get("label", f"Version {idx + 1}"))
                    if images:
                        label = f"{label} ({len(images)} unit{'s' if len(images) != 1 else ''})"
                    self.summary_variant_combo.addItem(label, idx)
                current = max(0, min(int(getattr(self, "summary_variant_index", 0)), len(variants) - 1))
                self.summary_variant_combo.setCurrentIndex(current)
                self.summary_variant_combo.setEnabled(len(variants) > 1)
        finally:
            try:
                self.summary_variant_combo.blockSignals(False)
            except Exception:
                pass

    def _sync_summary_variant_combo(self):
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        if not variants:
            return
        current = max(0, min(int(getattr(self, "summary_variant_index", 0)), len(variants) - 1))
        try:
            self.summary_variant_combo.blockSignals(True)
            if self.summary_variant_combo.currentIndex() != current:
                self.summary_variant_combo.setCurrentIndex(current)
        finally:
            try:
                self.summary_variant_combo.blockSignals(False)
            except Exception:
                pass

    def _on_summary_variant_combo_changed(self, index):
        record = self._current_summary_record()
        variants = self._summary_variants_for_record(record) if record is not None else []
        if not variants:
            return
        try:
            value = self.summary_variant_combo.itemData(int(index))
            idx = int(value) if value is not None else int(index)
        except Exception:
            idx = int(index) if index is not None else 0
        idx = max(0, min(idx, len(variants) - 1))
        if idx == int(getattr(self, "summary_variant_index", 0)):
            return
        self.summary_variant_index = idx
        self.summary_unit_index = 0
        self._update_summary_image()

    def _refresh_summary_recording_combo(self):
        records = list(getattr(self, "summary_records", []) or [])
        try:
            self.summary_recording_combo.blockSignals(True)
            self.summary_recording_combo.clear()
            if not records:
                self.summary_recording_combo.addItem("No recording summary", -1)
                self.summary_recording_combo.setEnabled(False)
            else:
                for idx, record in enumerate(records):
                    self.summary_recording_combo.addItem(self._summary_record_display_label(record, idx), idx)
                current = max(0, min(int(getattr(self, "summary_record_index", 0)), len(records) - 1))
                self.summary_recording_combo.setCurrentIndex(current)
                self.summary_recording_combo.setEnabled(True)
        finally:
            try:
                self.summary_recording_combo.blockSignals(False)
            except Exception:
                pass
        self._refresh_summary_variant_combo()

    def _sync_summary_recording_combo(self):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return
        current = max(0, min(int(getattr(self, "summary_record_index", 0)), len(records) - 1))
        try:
            self.summary_recording_combo.blockSignals(True)
            if self.summary_recording_combo.currentIndex() != current:
                self.summary_recording_combo.setCurrentIndex(current)
        finally:
            try:
                self.summary_recording_combo.blockSignals(False)
            except Exception:
                pass

    def _on_summary_recording_combo_changed(self, index):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return
        try:
            value = self.summary_recording_combo.itemData(int(index))
            idx = int(value) if value is not None else int(index)
        except Exception:
            idx = int(index) if index is not None else 0
        idx = max(0, min(idx, len(records) - 1))
        if idx == int(getattr(self, "summary_record_index", 0)):
            return
        self.summary_record_index = idx
        self.summary_variant_index = 0
        self.summary_unit_index = 0
        self._refresh_summary_variant_combo()
        self._update_summary_image()

    def _current_summary_record(self):
        records = list(getattr(self, "summary_records", []) or [])
        if not records:
            return None
        self.summary_record_index = max(0, min(int(getattr(self, "summary_record_index", 0)), len(records) - 1))
        return records[self.summary_record_index]

    def _current_summary_image_path(self):
        record = self._current_summary_record()
        if record is None:
            return None
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        if not images:
            return None
        self.summary_unit_index = max(0, min(int(getattr(self, "summary_unit_index", 0)), len(images) - 1))
        return images[self.summary_unit_index]

    def _summary_previous_unit(self):
        record = self._current_summary_record()
        if record is None:
            return
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        if not images:
            return
        self.summary_unit_index = max(0, int(getattr(self, "summary_unit_index", 0)) - 1)
        self._update_summary_image()

    def _summary_next_unit(self):
        record = self._current_summary_record()
        if record is None:
            return
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        if not images:
            return
        self.summary_unit_index = min(len(images) - 1, int(getattr(self, "summary_unit_index", 0)) + 1)
        self._update_summary_image()

    def _update_summary_browser_controls(self):
        record = self._current_summary_record()
        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        ui = int(getattr(self, "summary_unit_index", 0))
        for btn_name, enabled in (
            ("summary_prev_unit_btn", bool(images) and ui > 0),
            ("summary_next_unit_btn", bool(images) and ui < len(images) - 1),
        ):
            btn = getattr(self, btn_name, None)
            if btn is not None:
                try:
                    btn.setEnabled(bool(enabled))
                except Exception:
                    pass

    def _update_summary_image(self):
        record = self._current_summary_record()
        self._sync_summary_recording_combo()
        self._sync_summary_variant_combo()
        image_path = self._current_summary_image_path()
        self._summary_pixmap_original = None

        if record is None:
            self.summary_image_info.setText("No unit summary available yet.")
            self.summary_image_label.setPixmap(QtGui.QPixmap())
            self.summary_image_label.setText("No unit summary PNG files were found.")
            self._update_summary_browser_controls()
            return

        variant = self._current_summary_variant()
        images = list(variant.get("images", []) or []) if variant is not None else []
        recording = str(record.get("recording", "recording") or "recording")
        configuration = str(record.get("configuration", "") or "")
        sorter = str(record.get("sorter", "") or "")
        run_name = str(record.get("run_name", "") or "")
        variant_label = str(variant.get("label", "Original") if variant is not None else "Original")
        summary_folder = variant.get("summary_folder", record.get("summary_folder", "")) if variant is not None else record.get("summary_folder", "")

        if image_path is None:
            self.summary_image_info.setText(
                f"Recording {self.summary_record_index + 1}/{len(self.summary_records)}: {recording}"
                + (f" | {configuration}" if configuration else "")
                + (f" | {sorter}" if sorter else "")
                + (f" | {run_name}" if run_name else "")
                + f" | {variant_label}"
            )
            self.summary_image_label.setPixmap(QtGui.QPixmap())
            self.summary_image_label.setText(f"No PNG unit summaries found in:\n{summary_folder}")
            self._update_summary_browser_controls()
            return

        pixmap = QtGui.QPixmap(str(image_path))
        if pixmap.isNull():
            self.summary_image_label.setPixmap(QtGui.QPixmap())
            self.summary_image_label.setText(f"Could not load image:\n{image_path}")
            self._update_summary_browser_controls()
            return

        self._summary_pixmap_original = pixmap
        unit_label = Path(image_path).stem.replace("Unit_", "Unit ")
        self.summary_image_info.setText(
            f"Recording {self.summary_record_index + 1}/{len(self.summary_records)}: {recording}"
            + (f" | {configuration}" if configuration else "")
            + (f" | {sorter}" if sorter else "")
            + (f" | {run_name}" if run_name else "")
            + f" | {variant_label}"
            + f" | unit {self.summary_unit_index + 1}/{len(images)}: {unit_label}"
        )
        self._render_summary_pixmap()
        self._update_summary_browser_controls()

    def _render_summary_pixmap(self):
        pixmap = getattr(self, "_summary_pixmap_original", None)
        if pixmap is None or pixmap.isNull():
            return
        try:
            viewport_size = self.summary_scroll.viewport().size()
            max_w = max(300, int(viewport_size.width()) - 20)
            transform = QtCore.Qt.TransformationMode.SmoothTransformation if hasattr(QtCore.Qt, "TransformationMode") else QtCore.Qt.SmoothTransformation
            if pixmap.width() > max_w:
                scaled = pixmap.scaledToWidth(max_w, transform)
            else:
                scaled = pixmap
            self.summary_image_label.setPixmap(scaled)
            self.summary_image_label.setText("")
            self.summary_image_label.resize(scaled.size())
        except Exception:
            self.summary_image_label.setPixmap(pixmap)
            self.summary_image_label.setText("")

    def suggested_dialog_size(self, *, max_width=1400, extra_height=58):
        """Return a window size close to the displayed image size, avoiding empty vertical space."""
        try:
            pixmap = getattr(self, "_summary_pixmap_original", None)
            if pixmap is None or pixmap.isNull():
                image_path = self._current_summary_image_path()
                pixmap = QtGui.QPixmap(str(image_path)) if image_path is not None else QtGui.QPixmap()
            if pixmap.isNull():
                return QtCore.QSize(1200, 750)

            screen = None
            try:
                screen = self.window().screen()
            except Exception:
                screen = None
            if screen is None:
                screen = QtWidgets.QApplication.primaryScreen()
            available = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1600, 900)

            max_w = min(int(max_width), max(800, int(available.width()) - 80))
            # Account for dialog frame/margins and a possible vertical scrollbar.
            image_w = max(300, max_w - 36)
            image_h = int(round(pixmap.height() * (image_w / max(1, pixmap.width()))))

            # Toolbar + info label + dialog margins + close button area.
            controls_h = 96 + int(extra_height or 0)
            desired_h = image_h + controls_h
            desired_w = image_w + 36

            desired_h = min(max(520, desired_h), max(520, int(available.height()) - 80))
            desired_w = min(max(900, desired_w), max(900, int(available.width()) - 80))
            return QtCore.QSize(int(desired_w), int(desired_h))
        except Exception:
            return QtCore.QSize(1200, 750)


class SpikeSortingCurationView(_BaseSpikeSortingRightView):
    title = "Curation"

    def build_body(self, layout):
        self.sorting_path = None
        raw_path = self.sorting_info.get("path", None)
        if raw_path is not None:
            try:
                self.sorting_path = Path(raw_path)
            except Exception:
                self.sorting_path = None
        self.output_rows = []
        self.current_analyzer = None
        self.current_analyzer_folder = None
        self._pending_analyzer_variant = None
        self._curation_windows = []
        self._phy_processes = []
        self._summary_windows = []
        self._param_windows = []
        self._curation_sort_column = None
        self._curation_sort_order = None

        toolbar = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.delete_run_btn = QtWidgets.QPushButton("Delete")
        self.open_summary_btn = QtWidgets.QPushButton("Open summary")
        self.open_gui_btn = QtWidgets.QPushButton("Open curation")
        self.apply_curation_btn = QtWidgets.QPushButton("Export")
        self.open_gui_btn.setToolTip(
            "Open the selected sorting run in SpikeInterface GUI or Phy."
        )
        self.apply_curation_btn.setToolTip(
            "Export spike times/templates or write selected units back into the source NWB file(s). Ctrl/Shift selects multiple rows."
        )
        self.delete_run_btn.setToolTip("Delete the selected sorting run folder(s) from disk. Ctrl/Shift selects multiple rows.")
        try:
            self.delete_run_btn.setStyleSheet(
                "QPushButton { color: #b00020; font-weight: 600; }"
            )
        except Exception:
            pass

        for btn in (
            self.refresh_btn,
            self.delete_run_btn,
            self.open_summary_btn,
            self.open_gui_btn,
            self.apply_curation_btn,
        ):
            toolbar.addWidget(btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.status_label = QtWidgets.QLabel("Select a sorter output to inspect, curate, or delete it.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.output_table = QtWidgets.QTableWidget(self)
        self.output_table.setColumnCount(10)
        self.output_table.setHorizontalHeaderLabels([
            "Recording", "Run date", "Configuration", "Preprocessing pipeline", "Sorter params", "Sorter", "Units", "Curated", "Curated units", "Folder"
        ])
        # Select whole rows in the curation table. Ctrl/Shift multi-selection is enabled
        # so Delete and Export can operate on several sorting runs at once.
        self.output_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior") else QtWidgets.QAbstractItemView.SelectRows)
        self.output_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection if hasattr(QtWidgets.QAbstractItemView, "SelectionMode") else QtWidgets.QAbstractItemView.ExtendedSelection)
        self.output_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers if hasattr(QtWidgets.QAbstractItemView, "EditTrigger") else QtWidgets.QAbstractItemView.NoEditTriggers)
        try:
            self.output_table.setTextElideMode(QtCore.Qt.TextElideMode.ElideMiddle if hasattr(QtCore.Qt, "TextElideMode") else QtCore.Qt.ElideMiddle)
        except Exception:
            pass
        try:
            shortcut_cls = getattr(QtGui, "QShortcut", None) or getattr(QtWidgets, "QShortcut", None)
            if shortcut_cls is not None:
                try:
                    copy_sequence = QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Copy)
                except Exception:
                    copy_sequence = QtGui.QKeySequence.Copy
                self._copy_shortcut = shortcut_cls(copy_sequence, self.output_table)
                self._copy_shortcut.activated.connect(self._copy_selected_output_table_cells)
        except Exception:
            pass
        self._configure_output_table_columns()
        try:
            header = self.output_table.horizontalHeader()
            header.setSectionsClickable(True)
            header.setSortIndicatorShown(False)
            header.sectionClicked.connect(self._sort_output_table_by_column)
        except Exception:
            pass
        layout.addWidget(self.output_table, 2)

        self.log_text = QtWidgets.QPlainTextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(130)
        self.log_text.setStyleSheet("QPlainTextEdit { background: white; color: black; font-family: Consolas, monospace; }")
        layout.addWidget(self.log_text, 1)

        self.refresh_btn.clicked.connect(self.refresh_outputs)
        self.open_summary_btn.clicked.connect(self.open_selected_summary_viewer)
        self.apply_curation_btn.clicked.connect(self.show_selected_export_dialog)
        self.delete_run_btn.clicked.connect(self.delete_selected_sorting_run)
        self.open_gui_btn.clicked.connect(self.open_selected_in_curation_gui)
        self.output_table.itemSelectionChanged.connect(self._on_selection_changed)
        # Double-clicking parameter/folder cells opens the corresponding resource.
        # Other cells still open a small copyable text viewer.
        self.output_table.itemDoubleClicked.connect(self._on_output_table_cell_double_clicked)

        self._set_action_buttons_enabled(False)
        self._append_log("Scanning sorter output folders...")
        QtCore.QTimer.singleShot(0, self.refresh_outputs)

    def _configure_output_table_columns(self):
        """Keep the curation table compact while preserving the full path in tooltips.

        Resize-to-contents is useful for short columns, but it makes the Folder
        column as wide as the full absolute path.  That can push the table past
        the right edge of the window.  The Folder column is therefore fixed to a
        readable width and uses middle ellipsis plus tooltip/double-click access.
        """
        try:
            self.output_table.setWordWrap(False)
        except Exception:
            pass
        try:
            policy = QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded if hasattr(QtCore.Qt, "ScrollBarPolicy") else QtCore.Qt.ScrollBarAsNeeded
            self.output_table.setHorizontalScrollBarPolicy(policy)
        except Exception:
            pass
        try:
            mode = QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel if hasattr(QtWidgets.QAbstractItemView, "ScrollMode") else QtWidgets.QAbstractItemView.ScrollPerPixel
            self.output_table.setHorizontalScrollMode(mode)
        except Exception:
            pass
        try:
            header = self.output_table.horizontalHeader()
            header.setStretchLastSection(False)
            folder_col = self.output_table.columnCount() - 1
            resize_to_contents = QtWidgets.QHeaderView.ResizeMode.ResizeToContents if hasattr(QtWidgets.QHeaderView, "ResizeMode") else QtWidgets.QHeaderView.ResizeToContents
            interactive = QtWidgets.QHeaderView.ResizeMode.Interactive if hasattr(QtWidgets.QHeaderView, "ResizeMode") else QtWidgets.QHeaderView.Interactive
            for col in range(self.output_table.columnCount()):
                header.setSectionResizeMode(col, interactive if col == folder_col else resize_to_contents)
            if folder_col >= 0:
                current = max(220, int(self.output_table.columnWidth(folder_col) or 0))
                self.output_table.setColumnWidth(folder_col, min(current, 340))
        except Exception:
            pass

    def _fit_output_table_columns(self):
        """Apply final curation-table widths after data was inserted."""
        try:
            self.output_table.resizeColumnsToContents()
        except Exception:
            pass
        try:
            folder_col = self.output_table.columnCount() - 1
            if folder_col >= 0:
                # Keep the folder path visible enough to identify the run, but
                # never let it dominate the whole table width.
                self.output_table.setColumnWidth(folder_col, 340)
        except Exception:
            pass
        self._configure_output_table_columns()

    @staticmethod
    def _curation_row_data_role():
        return QtCore.Qt.ItemDataRole.UserRole if hasattr(QtCore.Qt, "ItemDataRole") else QtCore.Qt.UserRole

    @staticmethod
    def _curation_qt_sort_order(ascending):
        sort_order = QtCore.Qt.SortOrder if hasattr(QtCore.Qt, "SortOrder") else QtCore.Qt
        return sort_order.AscendingOrder if ascending else sort_order.DescendingOrder

    @staticmethod
    def _curation_date_timestamp(value):
        """Return a timestamp for common saved run-date formats, or None."""
        text = str(value or "").strip()
        if not text:
            return None
        candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            return float(datetime.fromisoformat(candidate).timestamp())
        except Exception:
            pass
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%m/%d/%Y",
            "%m-%d-%Y",
        ):
            try:
                return float(datetime.strptime(text.split(".")[0], fmt).timestamp())
            except Exception:
                pass
        return None

    def _curation_cell_sort_key(self, column, value):
        """Return one consistently comparable key for a Curation table cell."""
        text = str(value or "").strip()

        if int(column) == 1:  # Run date
            timestamp = self._curation_date_timestamp(text)
            if timestamp is not None:
                return (2, timestamp, ())
            if text:
                return (1, 0.0, _curation_natural_sort_key(text))
            return (0, 0.0, ())

        if int(column) in (6, 8):  # Units / Curated units
            try:
                number = float(value)
                if math.isfinite(number):
                    return (2, number, ())
            except Exception:
                pass
            if text:
                return (1, 0.0, _curation_natural_sort_key(text))
            return (0, 0.0, ())

        if text:
            return (1, _curation_natural_sort_key(text))
        return (0, ())

    def _sort_output_table_by_column(self, column):
        """Sort a clicked Curation column, toggling its direction on each click."""
        try:
            column = int(column)
        except Exception:
            return
        if column < 0 or column >= self.output_table.columnCount():
            return

        if self._curation_sort_column == column and self._curation_sort_order is not None:
            ascending = self._curation_sort_order != self._curation_qt_sort_order(True)
        else:
            # Counts and dates are most useful largest/newest first. Text starts A-Z.
            ascending = column not in (1, 6, 8)

        order = self._curation_qt_sort_order(ascending)
        self._curation_sort_column = column
        self._curation_sort_order = order
        try:
            self.output_table.sortItems(column, order)
            header = self.output_table.horizontalHeader()
            header.setSortIndicator(column, order)
            header.setSortIndicatorShown(True)
        except Exception:
            pass

    def _info_for_output_table_row(self, row):
        """Resolve the run attached to a visible row, including after sorting."""
        try:
            row = int(row)
        except Exception:
            return None
        if row < 0 or row >= self.output_table.rowCount():
            return None
        try:
            item = self.output_table.item(row, 0)
            source_row = item.data(self._curation_row_data_role()) if item is not None else None
            if source_row is not None:
                source_row = int(source_row)
                if 0 <= source_row < len(self.output_rows):
                    return self.output_rows[source_row]
        except Exception:
            pass
        if 0 <= row < len(self.output_rows):
            return self.output_rows[row]
        return None

    def _append_log(self, text=""):
        try:
            text = str(text)
            replace_last = text.startswith("\r")
            if replace_last:
                text = text.lstrip("\r")
                if not text:
                    return
                try:
                    cursor = self.log_text.textCursor()
                    end = QtGui.QTextCursor.MoveOperation.End if hasattr(QtGui.QTextCursor, "MoveOperation") else QtGui.QTextCursor.End
                    start_block = QtGui.QTextCursor.MoveOperation.StartOfBlock if hasattr(QtGui.QTextCursor, "MoveOperation") else QtGui.QTextCursor.StartOfBlock
                    keep = QtGui.QTextCursor.MoveMode.KeepAnchor if hasattr(QtGui.QTextCursor, "MoveMode") else QtGui.QTextCursor.KeepAnchor
                    cursor.movePosition(end)
                    cursor.movePosition(start_block, keep)
                    cursor.removeSelectedText()
                    cursor.insertText(text)
                    self.log_text.setTextCursor(cursor)
                except Exception:
                    self.log_text.appendPlainText(text)
            else:
                self.log_text.appendPlainText(text)
            bar = self.log_text.verticalScrollBar()
            bar.setValue(bar.maximum())
        except Exception:
            pass

    def _set_action_buttons_enabled(self, enabled: bool):
        """Enable actions according to the current row selection.

        The table supports Ctrl/Shift multi-selection. Single-run actions such
        as Open summary and Open curation stay restricted to one completed run,
        while Delete and Export can work on all selected rows.
        """
        enabled = bool(enabled) and not bool(getattr(self, "_busy", False))
        try:
            infos = self._selected_row_infos() if hasattr(self, "output_table") else []
        except Exception:
            infos = []
        has_selection = bool(enabled and infos)
        completed_infos = [
            info for info in infos
            if isinstance(info, dict)
            and not info.get("sorting_failed")
            and not info.get("sorting_in_progress")
        ]
        any_in_progress = any(bool(isinstance(info, dict) and info.get("sorting_in_progress")) for info in infos)
        single_completed = len(infos) == 1 and len(completed_infos) == 1

        states = {
            self.delete_run_btn: bool(has_selection and not any_in_progress),
            self.apply_curation_btn: bool(has_selection and completed_infos),
            self.open_summary_btn: bool(single_completed),
            self.open_gui_btn: bool(single_completed),
        }
        for btn, state in states.items():
            try:
                btn.setEnabled(state)
            except Exception:
                pass

    def _set_busy(self, busy: bool, text: str = ""):
        """Mark the Curation section as busy so the spike-sorting tree shows the green running state.

        A small counter is used because some operations call other curation helpers
        such as load_selected_analyzer(); nested busy sections should not clear the
        green indicator until the outer operation is also finished.
        """
        try:
            count = int(getattr(self, "_busy_count", 0) or 0)
        except Exception:
            count = 0
        if busy:
            count += 1
        else:
            count = max(0, count - 1)
        self._busy_count = count
        self._busy = count > 0

        try:
            self.refresh_btn.setEnabled(not self._busy)
        except Exception:
            pass
        try:
            self.output_table.setEnabled(not self._busy)
        except Exception:
            pass

        selected = self._selected_row_info() is not None
        self._set_action_buttons_enabled(selected)

        if text:
            try:
                self.status_label.setText(text)
            except Exception:
                pass
        self._emit_busy_state(
            bool(self._busy),
            text or ("Curation running..." if self._busy else "Curation ready."),
        )

    def _selected_row_info(self):
        row = int(self.output_table.currentRow())
        info = self._info_for_output_table_row(row)
        if info is None:
            infos = self._selected_row_infos()
            return infos[0] if infos else None
        return info

    def _selected_output_table_rows(self):
        """Return unique selected table row numbers, sorted in visual order."""
        rows = set()
        try:
            selection_model = self.output_table.selectionModel()
            if selection_model is not None:
                for ix in selection_model.selectedRows():
                    rows.add(int(ix.row()))
        except Exception:
            pass
        if not rows:
            try:
                for ix in self.output_table.selectedIndexes():
                    rows.add(int(ix.row()))
            except Exception:
                pass
        if not rows:
            try:
                row = int(self.output_table.currentRow())
                if row >= 0:
                    rows.add(row)
            except Exception:
                pass
        return [r for r in sorted(rows) if 0 <= r < self.output_table.rowCount()]

    def _selected_row_infos(self):
        """Return info dictionaries for all selected curation-table rows."""
        infos = [self._info_for_output_table_row(r) for r in self._selected_output_table_rows()]
        return [info for info in infos if isinstance(info, dict)]

    def _on_selection_changed(self):
        # A different row means a different SortingAnalyzer may be selected.
        # Drop the cached analyzer so "Open curation" always loads the selected row.
        self.current_analyzer = None
        self.current_analyzer_folder = None
        infos = self._selected_row_infos()
        self._set_action_buttons_enabled(bool(infos))
        if not infos:
            self.status_label.setText("Select one or more sorter outputs to inspect, export, or delete.")
            return
        if len(infos) > 1:
            in_progress = sum(1 for info in infos if isinstance(info, dict) and info.get("sorting_in_progress"))
            failed = sum(1 for info in infos if isinstance(info, dict) and info.get("sorting_failed"))
            completed = len(infos) - in_progress - failed
            parts = [f"Selected {len(infos)} sorter output(s)", f"{completed} completed"]
            if failed:
                parts.append(f"{failed} failed")
            if in_progress:
                parts.append(f"{in_progress} running")
            self.status_label.setText("; ".join(parts) + ". Export uses completed rows; Delete is disabled while a selected row is running.")
            return
        info = infos[0]
        if info.get("sorting_in_progress"):
            self.status_label.setText(
                f"Selected sorting in progress: {info.get('display_recording', info.get('recording', ''))} / {info.get('sorter', '')} / {info.get('run_name', '')}"
            )
        elif info.get("sorting_failed"):
            self.status_label.setText(
                f"Selected failed sorting: {info.get('display_recording', info.get('recording', ''))} / {info.get('sorter', '')} / {info.get('run_name', '')}"
            )
        else:
            self.status_label.setText(
                f"Selected: {info.get('display_recording', info.get('recording', ''))} / {info.get('sorter', '')} / {info.get('run_name', '')}"
            )

    def _copy_selected_output_table_cells(self):
        """Copy the currently selected curation table cell(s) as tab-separated text."""
        try:
            indexes = list(self.output_table.selectedIndexes())
        except Exception:
            indexes = []
        if not indexes:
            try:
                item = self.output_table.currentItem()
                if item is not None:
                    QtWidgets.QApplication.clipboard().setText(item.text())
            except Exception:
                pass
            return

        indexes = sorted(indexes, key=lambda ix: (int(ix.row()), int(ix.column())))
        rows = {}
        for ix in indexes:
            try:
                item = self.output_table.item(int(ix.row()), int(ix.column()))
                rows.setdefault(int(ix.row()), {})[int(ix.column())] = "" if item is None else item.text()
            except Exception:
                pass
        lines = []
        for row in sorted(rows):
            cols = rows[row]
            lines.append("\t".join(cols[c] for c in sorted(cols)))
        try:
            QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        except Exception:
            pass

    def _show_output_table_cell_text(self, item):
        """Show the double-clicked curation table cell in a selectable/copyable window."""
        if item is None:
            return
        try:
            row = int(item.row())
            col = int(item.column())
            header_item = self.output_table.horizontalHeaderItem(col)
            header = header_item.text() if header_item is not None else f"Column {col + 1}"
            text = item.text()
        except Exception:
            header = "Cell value"
            text = str(item.text()) if item is not None else ""

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(header)
        layout = QtWidgets.QVBoxLayout(dialog)

        label = QtWidgets.QLabel(f"{header}:", dialog)
        label.setWordWrap(True)
        layout.addWidget(label)

        text_edit = QtWidgets.QPlainTextEdit(dialog)
        text_edit.setPlainText(text)
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap if hasattr(QtWidgets.QPlainTextEdit, "LineWrapMode") else QtWidgets.QPlainTextEdit.NoWrap)
        text_edit.selectAll()
        layout.addWidget(text_edit)

        buttons = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy", dialog)
        close_btn = QtWidgets.QPushButton("Close", dialog)
        buttons.addStretch(1)
        buttons.addWidget(copy_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(text_edit.toPlainText()))
        close_btn.clicked.connect(dialog.close)

        try:
            dialog.resize(850, 220)
        except Exception:
            pass
        try:
            self._param_windows.append(dialog)
        except Exception:
            pass
        dialog.show()
        try:
            text_edit.setFocus()
        except Exception:
            pass

    def _on_output_table_cell_double_clicked(self, item):
        """Open row-specific resources from the curation table by double-clicking cells."""
        if item is None:
            return
        try:
            row = int(item.row())
            col = int(item.column())
            self.output_table.selectRow(row)
            header_item = self.output_table.horizontalHeaderItem(col)
            header = header_item.text() if header_item is not None else ""
        except Exception:
            header = ""

        header_norm = str(header).strip().lower()
        if header_norm == "preprocessing pipeline":
            self.show_selected_preprocessing_params()
            return
        if header_norm == "sorter params":
            self.show_selected_sorter_params()
            return
        if header_norm == "folder":
            self.open_selected_output_folder()
            return
        if header_norm == "units":
            info = self._info_for_output_table_row(row)
            if info is None:
                info = self._selected_row_info()
            if isinstance(info, dict) and info.get("sorting_failed"):
                self.open_selected_sorting_failure_log()
                return
            self.open_selected_summary_viewer(variant_kind="original")
            return
        if header_norm == "sorter":
            self.open_selected_summary_viewer(variant_kind="original")
            return
        if header_norm in {"curated", "curated units"}:
            self.open_selected_summary_viewer(variant_kind="curated")
            return

        self._show_output_table_cell_text(item)

    def _json_for_row(self, info, path_key, payload_key=None):
        """Load a row-specific JSON file, then fall back to run_config/sorting_run payload."""
        if not isinstance(info, dict):
            return {}
        path = info.get(path_key)
        if path is not None:
            try:
                p = Path(path)
                if p.exists():
                    obj = _load_json_file(p)
                    if obj:
                        return obj
            except Exception:
                pass
        if payload_key:
            for fallback_key in ("run_config_path", "run_json"):
                try:
                    p = Path(info.get(fallback_key))
                    if not p.exists():
                        continue
                    payload = _load_json_file(p)
                    value = payload.get(payload_key, {}) if isinstance(payload, dict) else {}
                    if isinstance(value, dict):
                        return value
                    if isinstance(value, list):
                        return {"items": value}
                except Exception:
                    pass
        return {}

    def _run_payload_for_row(self, info):
        for key in ("run_json", "run_config_path"):
            try:
                p = Path(info.get(key))
                if p.exists():
                    obj = _load_json_file(p)
                    if obj:
                        return obj
            except Exception:
                pass
        return {}

    def _sorting_failure_log_for_row(self, info):
        """Return the saved failure traceback and its source for one run.

        New runs save ``sorting_error.log``.  Existing failed runs are still
        supported through the ``error_text`` stored in ``run_config.json``.
        A SpikeInterface log is used only as a last-resort compatibility
        fallback for older partial run folders.
        """
        if not isinstance(info, dict):
            return "", None

        try:
            output_folder = Path(info.get("output_folder"))
        except Exception:
            output_folder = None

        if output_folder is not None:
            error_log = output_folder / "sorting_error.log"
            if error_log.exists():
                try:
                    text = error_log.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        return text, error_log
                except Exception:
                    pass

        for key in ("run_config_path", "run_json"):
            try:
                path = Path(info.get(key))
            except Exception:
                continue
            if not path.exists():
                continue
            try:
                payload = _load_json_file(path)
            except Exception:
                payload = {}
            for error_key in ("error_text", "traceback", "error", "failure_reason"):
                value = payload.get(error_key, "") if isinstance(payload, dict) else ""
                if isinstance(value, (dict, list)):
                    value = self._pretty_json_text(value)
                text = str(value or "").strip()
                if text:
                    return text, path

        if output_folder is not None:
            candidates = []
            for work_folder in _sorter_work_folder_candidates(output_folder):
                candidates.extend([
                    Path(work_folder) / "spikeinterface_log.json",
                    Path(work_folder) / "spikeinterface_log.txt",
                    Path(work_folder) / "sorter_log.json",
                    Path(work_folder) / "sorter_log.txt",
                ])
            for path in candidates:
                if not path.exists() or not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace").strip()
                    if path.suffix.lower() == ".json":
                        try:
                            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                        except Exception:
                            pass
                    if text:
                        return text, path
                except Exception:
                    pass

        return "", None

    def open_selected_sorting_failure_log(self):
        """Open a copyable log window for the selected failed sorting run."""
        info = self._selected_row_info()
        if info is None:
            return

        error_text, source_path = self._sorting_failure_log_for_row(info)
        if not error_text:
            QtWidgets.QMessageBox.information(
                self,
                "Sorting failure log",
                "This run is marked as failed, but no saved error traceback was found.\n\n"
                "Older interrupted runs may not contain an error log.",
            )
            return

        recording = str(info.get("display_recording", info.get("recording", "")) or "")
        sorter = str(info.get("sorter", "") or "")
        configuration = str(info.get("configuration_name", "") or "")

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Sorting failure log")
        dialog.resize(1050, 700)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header_parts = [part for part in (recording, configuration, sorter) if part]
        header = QtWidgets.QLabel(" | ".join(header_parts) or "Failed sorting run", dialog)
        header.setWordWrap(True)
        header.setStyleSheet("font-size: 14px; font-weight: 700; color: #b00020;")
        layout.addWidget(header)

        if source_path is not None:
            source = QtWidgets.QLabel(f"Source: {source_path}", dialog)
            source.setWordWrap(True)
            source.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
                if hasattr(QtCore.Qt, "TextInteractionFlag")
                else QtCore.Qt.TextSelectableByMouse
            )
            source.setStyleSheet("color: #555;")
            layout.addWidget(source)

        text_edit = QtWidgets.QPlainTextEdit(dialog)
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(
            QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
            if hasattr(QtWidgets.QPlainTextEdit, "LineWrapMode")
            else QtWidgets.QPlainTextEdit.NoWrap
        )
        text_edit.setStyleSheet(
            "QPlainTextEdit { background: white; color: black; font-family: Consolas, monospace; }"
        )
        text_edit.setPlainText(error_text)
        layout.addWidget(text_edit, 1)

        buttons = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy log", dialog)
        open_folder_btn = QtWidgets.QPushButton("Open run folder", dialog)
        close_btn = QtWidgets.QPushButton("Close", dialog)
        buttons.addStretch(1)
        buttons.addWidget(copy_btn)
        buttons.addWidget(open_folder_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(text_edit.toPlainText())
        )
        open_folder_btn.clicked.connect(
            lambda: self._open_path(info.get("output_folder"))
        )
        close_btn.clicked.connect(dialog.close)

        # Put the last exception line in view immediately while preserving the
        # full traceback above it for scrolling and copying.
        try:
            cursor = text_edit.textCursor()
            end = (
                QtGui.QTextCursor.MoveOperation.End
                if hasattr(QtGui.QTextCursor, "MoveOperation")
                else QtGui.QTextCursor.End
            )
            cursor.movePosition(end)
            text_edit.setTextCursor(cursor)
        except Exception:
            pass

        self._show_modeless_param_dialog(dialog)
        self.status_label.setText(
            f"Showing sorting failure log: {recording or info.get('run_name', '')}"
        )

    def _summary_unit_count_for_row(self, info, run_payload=None):
        run_payload = run_payload if isinstance(run_payload, dict) else {}
        for key in ("unit_count", "n_units", "num_units"):
            try:
                value = run_payload.get(key, None)
                if value is not None and str(value).strip() != "":
                    return int(value)
            except Exception:
                pass
        try:
            summary_folder = Path(info.get("summary_folder"))
            images = _UnitSummaryImageBrowserWidget.find_summary_images(summary_folder)
            if images:
                return len(images)
        except Exception:
            pass
        return ""

    def _curation_info_for_row(self, info, run_payload=None):
        """Return saved curated analyzer metadata for a curation-table row.

        There is intentionally only one curated result per sorting run, no matter
        whether the curation came from Phy or from SpikeInterface GUI.
        New curated outputs use these fixed names inside the run folder:
            - SorterOutput_curated
            - curated_unit_summary
        Older source-specific folders are still detected as a fallback so old
        projects remain readable, but they are exposed as one generic Curated
        result in the GUI.
        """
        out = {
            "has_curated": False,
            "curated_folder": None,
            "curated_source": "",
            "curated_unit_count": "",
            "curated_validation_report": "",
            "curated_summary_folder": None,
        }
        if not isinstance(info, dict):
            return out
        run_payload = run_payload if isinstance(run_payload, dict) else {}

        try:
            output_folder = Path(info.get("output_folder"))
        except Exception:
            output_folder = None

        last = run_payload.get("last_curation", {})
        if not isinstance(last, dict):
            last = {}

        def _summary_folder_for(output_folder, entry=None):
            candidates = []
            if output_folder is not None:
                candidates.append(output_folder / "curated_unit_summary")
            if isinstance(entry, dict):
                for key in ("unit_summary_folder", "curated_summary_folder"):
                    value = entry.get(key)
                    if value:
                        try:
                            candidates.append(Path(value))
                        except Exception:
                            pass
            for key in ("curated_unit_summary_folder", "curated_summary_folder"):
                value = run_payload.get(key)
                if value:
                    try:
                        candidates.append(Path(value))
                    except Exception:
                        pass
            if output_folder is not None:
                try:
                    legacy = sorted(
                        [p for p in output_folder.glob("curated_unit_summary_*") if p.is_dir()],
                        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                        reverse=True,
                    )
                    candidates.extend(legacy)
                except Exception:
                    pass
            seen = set()
            for folder in candidates:
                try:
                    folder = Path(folder)
                    key = str(folder.resolve())
                except Exception:
                    key = str(folder)
                if key in seen:
                    continue
                seen.add(key)
                if folder.exists():
                    return str(folder)
            # Preferred path even before images exist.
            if output_folder is not None:
                return str(output_folder / "curated_unit_summary")
            return ""

        # Prefer the current canonical metadata/folder first.
        candidate_entries = []
        folder_text = run_payload.get("curated_sorting_analyzer_folder")
        if folder_text:
            candidate_entries.append((folder_text, last))
        if output_folder is not None:
            candidate_entries.append((output_folder / "SorterOutput_curated", last))

        # Backward compatibility with older source-specific metadata.
        entries = []
        curations = run_payload.get("curations", [])
        if isinstance(curations, list):
            entries.extend([entry for entry in curations if isinstance(entry, dict)])
        if last:
            entries.append(last)
        for entry in reversed(entries):
            folder_text = entry.get("sorting_analyzer_folder") or entry.get("folder") or entry.get("curated_folder")
            if folder_text:
                candidate_entries.append((folder_text, entry))

        if output_folder is not None:
            try:
                legacy = sorted(
                    [p for p in output_folder.glob("SorterOutput_curated_*") if p.is_dir()],
                    key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                    reverse=True,
                )
                for folder in legacy:
                    candidate_entries.append((folder, last))
            except Exception:
                pass

        seen = set()
        for folder_text, entry in candidate_entries:
            try:
                folder = Path(folder_text)
                key = str(folder.resolve())
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            if folder.exists():
                out.update({
                    "has_curated": True,
                    "curated_folder": folder,
                    "curated_source": str((entry or {}).get("source", "") or "curated"),
                    "curated_unit_count": (entry or {}).get("unit_count", run_payload.get("curated_unit_count", "")),
                    "curated_validation_report": (
                        (entry or {}).get("validation_report", "")
                        or run_payload.get("curated_validation_report", "")
                        or (str(folder / "curation_validation.json") if (folder / "curation_validation.json").exists() else "")
                    ),
                    "curated_summary_folder": _summary_folder_for(output_folder, entry),
                })
                return out

        return out

    def _original_analyzer_variant_for_row(self, info):
        return {
            "kind": "original",
            "label": "Original",
            "folder": Path(info.get("analyzer_folder")),
            "unit_count": info.get("unit_count", ""),
        }

    def _available_analyzer_variants_for_row(self, info):
        variants = []
        if not isinstance(info, dict):
            return variants
        try:
            original = self._original_analyzer_variant_for_row(info)
            if (not info.get("sorting_failed")) and self._is_valid_sorting_analyzer_folder(original.get("folder")):
                variants.append(original)
        except Exception:
            pass
        try:
            curated_folder = Path(info.get("curated_folder"))
            if info.get("has_curated") and curated_folder.exists():
                count = info.get("curated_unit_count", "")
                count_text = f" ({count} unit(s))" if str(count).strip() else ""
                variants.append({
                    "kind": "curated",
                    "label": f"Curated{count_text}",
                    "folder": curated_folder,
                    "unit_count": count,
                    "source": str(info.get("curated_source", "") or "curated"),
                })
        except Exception:
            pass
        return variants

    def _choose_analyzer_variant(self, info, *, title="Choose analyzer", text="Choose which SortingAnalyzer to open."):
        variants = self._available_analyzer_variants_for_row(info)
        if not variants:
            return None
        if len(variants) == 1:
            return variants[0]
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setIcon(QtWidgets.QMessageBox.Icon.Question if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Question)
        msg.setText(text)
        msg.setInformativeText(
            f"Recording: {info.get('display_recording', info.get('recording', ''))}\n"
            f"Sorter: {info.get('sorter', '')}\n"
            f"Preprocessing: {info.get('preprocessing_pipeline_id', 'Pipeline ?')}\n"
            f"Sorter params: {info.get('sorter_params_id', 'Pipeline ?')}"
        )
        role = QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole
        reject_role = QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole
        buttons = []
        for variant in variants:
            btn = msg.addButton(str(variant.get("label", variant.get("kind", "analyzer"))), role)
            buttons.append((btn, variant))
        cancel_btn = msg.addButton("Cancel", reject_role)
        msg.setDefaultButton(buttons[-1][0] if buttons else cancel_btn)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is cancel_btn:
            return None
        for btn, variant in buttons:
            if clicked is btn:
                return variant
        return None

    def _is_valid_sorting_analyzer_folder(self, folder):
        """Return True when a SorterOutput folder looks like a readable SortingAnalyzer.

        Failed sorting attempts can leave a run folder, a SpikeInterface work folder, or even
        an empty/partial SorterOutput folder.  The curation table should show those
        attempts, but mark them as failed instead of treating them as loadable runs.
        """
        if folder is None:
            return False
        try:
            folder = Path(folder)
        except Exception:
            return False
        if not folder.exists() or not folder.is_dir():
            return False

        # SpikeInterface binary SortingAnalyzer folders normally contain a sorting
        # subfolder.  Keep a few compatibility checks for older saved analyzers.
        expected = (
            folder / "sorting",
            folder / "sorting_info.json",
            folder / "sorting.json",
            folder / "spikeinterface_info.json",
        )
        if any(p.exists() for p in expected):
            return True

        # Some versions write metadata at the root plus an extensions folder.  An
        # empty SorterOutput or a folder containing only sorter logs is not valid.
        try:
            child_names = {p.name for p in folder.iterdir()}
        except Exception:
            return False
        if {"extensions", "recording_info"}.intersection(child_names) and ("sorting" in child_names):
            return True
        return False

    def _make_curation_row_from_folder(self, output_folder, root, payload=None, *, run_json=None, run_config_path=None):
        """Build a curation-table row from a run folder, completed or failed."""
        output_folder = Path(output_folder)
        root = Path(root)
        payload = payload if isinstance(payload, dict) else {}

        analyzer_folder = output_folder / "SorterOutput"
        summary_folder = output_folder / "sorting_summary"
        run_date = ""
        signature_hash = ""
        recording_group = {}

        try:
            if output_folder.parent == root and output_folder.name.startswith("run_"):
                # New compact layout: <sorting_project>/sorter_output/run_<hash>
                label = output_folder.name
                sorter = ""
                run_name = output_folder.name
            elif output_folder.parent.parent == root:
                # Legacy layout: <root>/<recording>/<sorter> or <root>/<recording>/<sorter>/<run>
                label = output_folder.parent.name
                sorter = output_folder.name
                run_name = "legacy"
            else:
                label = output_folder.parent.parent.name
                sorter = output_folder.parent.name
                run_name = output_folder.name
        except Exception:
            label = output_folder.parent.name
            sorter = output_folder.name
            run_name = output_folder.name

        label = str(payload.get("recording_label", label) or label)
        sorter = str(payload.get("sorter_name", sorter) or sorter)
        run_date = str(payload.get("created_at", "") or "")
        signature_hash = str(payload.get("run_signature_hash", "") or "")
        queue_configuration = payload.get("queue_configuration", {})
        configuration_name = (
            str(queue_configuration.get("name", "") or "").strip()
            if isinstance(queue_configuration, dict)
            else ""
        )
        if payload.get("sorting_analyzer_folder"):
            try:
                analyzer_folder = Path(payload.get("sorting_analyzer_folder"))
            except Exception:
                pass
        if payload.get("sorting_summary_folder"):
            try:
                summary_folder = Path(payload.get("sorting_summary_folder"))
            except Exception:
                pass
        if isinstance(payload.get("recording_group", None), dict):
            recording_group = dict(payload.get("recording_group") or {})

        valid_analyzer = self._is_valid_sorting_analyzer_folder(analyzer_folder)
        run_status = str(payload.get("status", "") or "").strip().lower()
        sorting_in_progress = (not valid_analyzer) and run_status in {"running", "in_progress", "processing", "started"}
        return {
            "recording": label,
            "sorter": sorter,
            "configuration_name": configuration_name,
            "run_name": run_name,
            "run_signature_hash": signature_hash,
            "output_folder": output_folder,
            "run_json": Path(run_json) if run_json is not None else output_folder / "sorting_run.json",
            "run_config_path": Path(run_config_path) if run_config_path is not None else output_folder / "run_config.json",
            "sorter_params_path": output_folder / "sorter_params.json",
            "preprocessing_pipeline_path": output_folder / "preprocessing_pipeline.json",
            "unit_summary_config_path": output_folder / "unit_summary_config.json",
            "recording_group_path": output_folder / "recording_group.json",
            "recording_group": recording_group,
            "analyzer_folder": analyzer_folder,
            "summary_folder": summary_folder,
            "run_date": run_date,
            "has_analyzer": valid_analyzer,
            "has_summary": summary_folder.exists(),
            "run_status": run_status,
            "sorting_in_progress": bool(sorting_in_progress),
            "sorting_failed": (not valid_analyzer) and not bool(sorting_in_progress),
        }

    def _decorate_curation_rows(self, rows):
        """Add display labels, unit counts and arbitrary parameter-pipeline IDs."""
        rows = list(rows or [])
        pre_map = {}
        sorter_map = {}
        # Use the current Sorting queue as the numbering source so the same
        # sorter and parameters have the same Pipeline N label in both views.
        # Parameter sets from older runs that are no longer in the queue are
        # appended after the configured pipelines.
        try:
            sorting_config = _load_json_file(Path(self.sorting_path) / "sorting_config.json")
            configured = sorting_config.get("sorting_configurations", []) if isinstance(sorting_config, dict) else []
            pre_map.update(_preprocessing_pipeline_numbers(configured))
            sorter_map.update(_sorter_parameter_pipeline_numbers(configured))
        except Exception:
            pass
        any_subject_id_group = False

        for info in rows:
            run_payload = self._run_payload_for_row(info)
            group = info.get("recording_group") if isinstance(info.get("recording_group"), dict) else None
            if not group:
                group = self._json_for_row(info, "recording_group_path", "recording_group")
            if not group and isinstance(run_payload, dict):
                maybe_group = run_payload.get("recording_group", {})
                group = maybe_group if isinstance(maybe_group, dict) else {}
            group = group if isinstance(group, dict) else {}
            info["recording_group"] = group

            files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
            subject_id = str(group.get("subject_id", group.get("subject", "")) or "").strip()
            is_subject_concat = bool(group.get("concatenated", False)) and bool(subject_id)
            # Older recipes sometimes only imply subject concatenation by having several files under one subject.
            if not is_subject_concat and len(files) > 1 and subject_id:
                is_subject_concat = True
            info["is_subject_concatenated"] = bool(is_subject_concat)
            if is_subject_concat:
                any_subject_id_group = True
                info["display_recording"] = subject_id
            else:
                info["display_recording"] = str(info.get("recording", "") or "")

            valid_analyzer = self._is_valid_sorting_analyzer_folder(info.get("analyzer_folder"))
            info["has_analyzer"] = bool(valid_analyzer)
            run_status = str((run_payload or {}).get("status", info.get("run_status", "")) or "").strip().lower()
            sorting_in_progress = (not bool(valid_analyzer)) and run_status in {"running", "in_progress", "processing", "started"}
            info["run_status"] = run_status
            info["sorting_in_progress"] = bool(sorting_in_progress)
            info["sorting_failed"] = (not bool(valid_analyzer)) and not bool(sorting_in_progress)
            if info.get("sorting_in_progress"):
                info["unit_count"] = "In Progress"
            elif info.get("sorting_failed"):
                info["unit_count"] = "Sorting Failed"
            else:
                info["unit_count"] = self._summary_unit_count_for_row(info, run_payload)

            curation_info = self._curation_info_for_row(info, run_payload)
            info.update(curation_info)

            enabled_pre = run_payload.get("enabled_preprocessing", None) if isinstance(run_payload, dict) else None
            if enabled_pre is None:
                pre_json = self._json_for_row(info, "preprocessing_pipeline_path", "preprocessing_pipeline")
                enabled_pre = _enabled_pipeline_for_signature(pre_json)
            pre_key = _canonical_json_text(enabled_pre)
            if pre_key not in pre_map:
                pre_map[pre_key] = max(pre_map.values(), default=0) + 1
            info["preprocessing_pipeline_id"] = f"Pipeline {pre_map[pre_key]}"

            sorter_params = run_payload.get("sorter_params", None) if isinstance(run_payload, dict) else None
            if sorter_params is None:
                sorter_params = self._json_for_row(info, "sorter_params_path", "sorter_params")
            sorter_key = _sorter_parameter_pipeline_key(
                info.get("sorter", ""),
                sorter_params if sorter_params is not None else {},
            )
            if sorter_key not in sorter_map:
                sorter_map[sorter_key] = max(sorter_map.values(), default=0) + 1
            info["sorter_params_id"] = f"Pipeline {sorter_map[sorter_key]}"

        self._curation_identity_column_title = "subject_id" if any_subject_id_group else "Recording"
        return rows

    def refresh_outputs(self):
        self.output_rows = []
        self.current_analyzer = None
        self.current_analyzer_folder = None
        self.output_table.setRowCount(0)
        self._set_action_buttons_enabled(False)

        if self.sorting_path is None:
            self._append_log("No sorting path available.")
            return
        root = _sorting_runs_root(self.sorting_path)
        if not root.exists():
            self._append_log(f"No sorter_output folder yet: {root}")
            self.status_label.setText("No sorter output found yet.")
            return

        self._append_log(f"Scanning: {root}")
        rows = []
        run_json_paths = sorted(root.rglob("sorting_run.json"), key=lambda p: _natural_text_key(str(p)))

        # Completed runs have sorting_run.json.  Failed runs usually only have
        # run_config.json plus a partial folder.  Add both kinds so failed
        # attempts are visible in the curation table.
        seen = set()
        for run_json in run_json_paths:
            output_folder = run_json.parent
            payload = {}
            try:
                with open(run_json, "r", encoding="utf-8") as f:
                    payload = _pp_json_load(f)
                if not isinstance(payload, dict):
                    payload = {}
            except Exception as exc:
                self._append_log(f"Could not read {run_json.name} in {output_folder}: {exc}")
            rows.append(self._make_curation_row_from_folder(
                output_folder,
                root,
                payload,
                run_json=run_json,
                run_config_path=output_folder / "run_config.json",
            ))
            seen.add(Path(output_folder))

        run_config_paths = sorted(root.rglob("run_config.json"), key=lambda p: _natural_text_key(str(p)))
        for run_config in run_config_paths:
            output_folder = run_config.parent
            if output_folder in seen:
                continue
            payload = {}
            try:
                with open(run_config, "r", encoding="utf-8") as f:
                    payload = _pp_json_load(f)
                if not isinstance(payload, dict):
                    payload = {}
            except Exception as exc:
                self._append_log(f"Could not read {run_config.name} in {output_folder}: {exc}")
            rows.append(self._make_curation_row_from_folder(
                output_folder,
                root,
                payload,
                run_json=output_folder / "sorting_run.json",
                run_config_path=run_config,
            ))
            seen.add(Path(output_folder))

        # Last-resort compatibility: show run folders that were created before
        # run_config.json could be written, as long as they look like sorter-run
        # folders or contain partial sorter output.
        direct_candidate_dirs = []
        try:
            direct_candidate_dirs = [p for p in root.iterdir() if p.is_dir()]
        except Exception:
            direct_candidate_dirs = []

        for output_folder in sorted(direct_candidate_dirs, key=lambda p: _natural_text_key(str(p))):
            if output_folder in seen:
                continue
            if not (
                output_folder.name.startswith("run_")
                or (output_folder / "SorterOutput").exists()
                or any(p.exists() for p in _sorter_work_folder_candidates(output_folder))
                or (output_folder / "sorting_summary").exists()
            ):
                continue
            rows.append(self._make_curation_row_from_folder(output_folder, root, {}))
            seen.add(Path(output_folder))

        # Legacy nested layout: <root>/<recording>/<sorter>/<run>
        for recording_dir in sorted(direct_candidate_dirs, key=lambda p: _natural_text_key(p.name)):
            try:
                sorter_dirs = [p for p in recording_dir.iterdir() if p.is_dir()]
            except Exception:
                continue
            for sorter_dir in sorted(sorter_dirs, key=lambda p: _natural_text_key(p.name)):
                candidate_dirs = []
                if (
                    (sorter_dir / "SorterOutput").exists()
                    or any(p.exists() for p in _sorter_work_folder_candidates(sorter_dir))
                    or (sorter_dir / "sorting_summary").exists()
                ):
                    candidate_dirs.append(sorter_dir)
                try:
                    candidate_dirs.extend([p for p in sorter_dir.iterdir() if p.is_dir()])
                except Exception:
                    pass
                for output_folder in sorted(set(candidate_dirs), key=lambda p: _natural_text_key(str(p))):
                    if output_folder in seen:
                        continue
                    if not (
                        output_folder.name.startswith("run_")
                        or (output_folder / "SorterOutput").exists()
                        or any(p.exists() for p in _sorter_work_folder_candidates(output_folder))
                        or (output_folder / "sorting_summary").exists()
                    ):
                        continue
                    rows.append(self._make_curation_row_from_folder(output_folder, root, {}))
                    seen.add(Path(output_folder))

        rows = self._decorate_curation_rows(rows)
        self.output_rows = rows
        self.output_table.setHorizontalHeaderLabels([
            getattr(self, "_curation_identity_column_title", "Recording"),
            "Run date",
            "Configuration",
            "Preprocessing pipeline",
            "Sorter params",
            "Sorter",
            "Units",
            "Curated",
            "Curated units",
            "Folder",
        ])
        self.output_table.setRowCount(len(rows))
        for r, info in enumerate(rows):
            values = [
                info.get("display_recording", info.get("recording", "")),
                info.get("run_date", ""),
                info.get("configuration_name", ""),
                info.get("preprocessing_pipeline_id", ""),
                info.get("sorter_params_id", ""),
                info.get("sorter", ""),
                info.get("unit_count", ""),
                "Yes" if info.get("has_curated") else "No",
                info.get("curated_unit_count", ""),
                info.get("output_folder", ""),
            ]
            for c, value in enumerate(values):
                item = _CurationSortableItem(
                    str(value),
                    sort_key=self._curation_cell_sort_key(c, value),
                )
                try:
                    # The source-row token moves with the item when Qt sorts it,
                    # keeping every action connected to the correct run.
                    item.setData(self._curation_row_data_role(), r)
                except Exception:
                    pass
                if c in (5, 7, 8):
                    try:
                        item.setTextAlignment((QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter))
                    except Exception:
                        pass
                if info.get("sorting_in_progress"):
                    try:
                        item.setForeground(QtGui.QBrush(QtGui.QColor("#2e7d32")))
                    except Exception:
                        pass
                elif info.get("sorting_failed"):
                    try:
                        item.setForeground(QtGui.QBrush(QtGui.QColor("#b00020")))
                    except Exception:
                        pass
                elif c == 6 and info.get("has_curated"):
                    try:
                        item.setForeground(QtGui.QBrush(QtGui.QColor("#0a7a20")))
                    except Exception:
                        pass
                try:
                    if c == 6 and info.get("sorting_failed"):
                        item.setToolTip("Sorting Failed. Double-click to open the saved error log.")
                    else:
                        item.setToolTip(str(value))
                except Exception:
                    pass
                self.output_table.setItem(r, c, item)
        if self._curation_sort_column is not None and self._curation_sort_order is not None:
            try:
                self.output_table.sortItems(
                    self._curation_sort_column,
                    self._curation_sort_order,
                )
                header = self.output_table.horizontalHeader()
                header.setSortIndicator(
                    self._curation_sort_column,
                    self._curation_sort_order,
                )
                header.setSortIndicatorShown(True)
            except Exception:
                pass
        self._fit_output_table_columns()
        self._append_log(f"Found {len(rows)} sorter output folder(s).")
        self.status_label.setText(f"Found {len(rows)} sorter output folder(s).")
        if rows:
            self.output_table.selectRow(0)

    def _open_path(self, path):
        if path is None:
            return
        path = Path(path)
        if not path.exists():
            QtWidgets.QMessageBox.warning(self, "Curation", f"Path does not exist:\n{path}")
            return
        try:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Curation", f"Could not open path:\n{path}\n\n{exc}")

    def open_selected_output_folder(self):
        info = self._selected_row_info()
        if info is not None:
            self._open_path(info.get("output_folder"))

    def open_selected_summary_folder(self):
        info = self._selected_row_info()
        if info is not None:
            self._open_path(info.get("summary_folder"))

    def _summary_records_from_output_rows(self, selected_info=None):
        records = []
        start_index = 0
        selected_key = None
        if selected_info is not None:
            try:
                selected_key = str(Path(selected_info.get("output_folder")).resolve())
            except Exception:
                selected_key = str(selected_info.get("output_folder", ""))

        seen_runs = set()
        for info in list(getattr(self, "output_rows", []) or []):
            if info.get("sorting_failed") or info.get("sorting_in_progress"):
                continue
            try:
                output_key = str(Path(info.get("output_folder")).resolve())
            except Exception:
                output_key = str(info.get("output_folder", ""))
            if output_key in seen_runs:
                continue
            seen_runs.add(output_key)

            variants = []
            try:
                summary_folder = Path(info.get("summary_folder"))
                if summary_folder.exists():
                    images = _UnitSummaryImageBrowserWidget.find_summary_images(summary_folder)
                    if images:
                        variants.append({
                            "kind": "original",
                            "label": "Uncurated",
                            "summary_folder": summary_folder,
                            "images": images,
                        })
            except Exception:
                pass

            # There is only one curated summary per run.  Prefer the canonical
            # curated_unit_summary folder, then metadata, then the newest legacy
            # source-specific folder for backward compatibility.
            curated_candidates = []
            try:
                output_folder = Path(info.get("output_folder"))
                curated_candidates.append(output_folder / "curated_unit_summary")
            except Exception:
                output_folder = None
            try:
                if info.get("has_curated"):
                    cf_text = str(info.get("curated_summary_folder", "") or "").strip()
                    if cf_text:
                        curated_candidates.append(Path(cf_text))
            except Exception:
                pass
            if output_folder is not None:
                try:
                    legacy = sorted(
                        [p for p in output_folder.glob("curated_unit_summary_*") if p.is_dir()],
                        key=lambda x: x.stat().st_mtime if x.exists() else 0.0,
                        reverse=True,
                    )
                    curated_candidates.extend(legacy)
                except Exception:
                    pass

            seen_variant_folders = set()
            for folder in curated_candidates:
                try:
                    folder = Path(folder)
                    key = str(folder.resolve())
                except Exception:
                    key = str(folder)
                if key in seen_variant_folders or not folder.exists():
                    continue
                seen_variant_folders.add(key)
                images = _UnitSummaryImageBrowserWidget.find_summary_images(folder)
                if not images:
                    continue
                variants.append({
                    "kind": "curated",
                    "label": "Curated",
                    "summary_folder": folder,
                    "images": images,
                    "source": str(info.get("curated_source", "") or "curated"),
                })
                break

            if not variants:
                continue
            record = {
                "recording": str(info.get("display_recording", info.get("recording", "recording")) or "recording"),
                "configuration": str(info.get("configuration_name", "") or ""),
                "sorter": str(info.get("sorter", "") or ""),
                "run_name": str(info.get("run_name", "") or ""),
                "output_folder": Path(info.get("output_folder")),
                "variants": variants,
            }
            if selected_key is not None and output_key == selected_key:
                start_index = len(records)
            records.append(record)

        records.sort(key=lambda r: (_natural_text_key(str(r.get("recording", ""))), _natural_text_key(str(r.get("sorter", ""))), _natural_text_key(str(r.get("run_name", "")))))
        if selected_key is not None:
            for idx, record in enumerate(records):
                try:
                    key = str(Path(record.get("output_folder")).resolve())
                except Exception:
                    key = str(record.get("output_folder", ""))
                if key == selected_key:
                    start_index = idx
                    break
        return records, start_index

    def open_selected_summary_viewer(self, variant_kind=None):
        info = self._selected_row_info()
        if info is None:
            return
        if info.get("sorting_in_progress"):
            QtWidgets.QMessageBox.information(self, "Open summary", "This sorting run is still in progress. Wait until it finishes before opening the summary.")
            return
        variant_kind = str(variant_kind or "").strip().lower()
        if variant_kind == "curated" and not bool(info.get("has_curated")):
            QtWidgets.QMessageBox.information(self, "Open summary", "This sorting run has no curated output yet.")
            return
        records, start_index = self._summary_records_from_output_rows(info)
        if not records:
            QtWidgets.QMessageBox.warning(
                self,
                "Open summary",
                "No PNG unit-summary files were found for the available sorting runs.",
            )
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Unit summary viewer")
        try:
            flags = dialog.windowFlags()
            context_hint = QtCore.Qt.WindowType.WindowContextHelpButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowContextHelpButtonHint
            maximize_hint = QtCore.Qt.WindowType.WindowMaximizeButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowMaximizeButtonHint
            minimize_hint = QtCore.Qt.WindowType.WindowMinimizeButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowMinimizeButtonHint
            dialog.setWindowFlags((flags & ~context_hint) | maximize_hint | minimize_hint)
        except Exception:
            pass
        try:
            dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        except Exception:
            try:
                dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            except Exception:
                pass
        vbox = QtWidgets.QVBoxLayout(dialog)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(4)
        browser = _UnitSummaryImageBrowserWidget(records, start_index=start_index, start_variant_kind=variant_kind, parent=dialog)
        vbox.addWidget(browser, 1)
        try:
            dialog.resize(browser.suggested_dialog_size(extra_height=52))
        except Exception:
            dialog.resize(1200, 750)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close if hasattr(QtWidgets.QDialogButtonBox, "StandardButton") else QtWidgets.QDialogButtonBox.Close, dialog)
        buttons.rejected.connect(dialog.close)
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).clicked.connect(dialog.close)
        except Exception:
            buttons.accepted.connect(dialog.close)
        vbox.addWidget(buttons)
        self._summary_windows.append(dialog)
        try:
            dialog.destroyed.connect(lambda *_: self._summary_windows.remove(dialog) if dialog in self._summary_windows else None)
        except Exception:
            pass
        dialog.show()
        self._append_log(f"Opened unit summary viewer with {len(records)} recording/run summary set(s).")

    def _load_json_from_info(self, info, path_key, fallback_key=None):
        """Load a JSON snapshot from a run folder, with run_config fallback."""
        if info is None:
            return None, None
        path = info.get(path_key)
        if path is not None:
            path = Path(path)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return _pp_json_load(f), path
                except Exception as exc:
                    return {"error": f"Could not read {path}: {exc}"}, path

        if fallback_key:
            for config_key in ("run_config_path", "run_json"):
                config_path = info.get(config_key)
                if config_path is None:
                    continue
                config_path = Path(config_path)
                if not config_path.exists():
                    continue
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        payload = _pp_json_load(f)
                    if isinstance(payload, dict) and fallback_key in payload:
                        return payload.get(fallback_key), config_path
                except Exception:
                    pass
        return None, None

    def _pretty_json_text(self, value):
        try:
            return json.dumps(_json_safe(value), indent=2, sort_keys=True, ensure_ascii=False)
        except Exception:
            return str(value)

    def _short_scalar_text(self, value):
        if isinstance(value, str):
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return str(value)
        try:
            return json.dumps(_json_safe(value), ensure_ascii=False)
        except Exception:
            return str(value)

    def _add_param_tree_item(self, parent_item, key, value):
        key_text = str(key)
        if isinstance(value, dict):
            item = QtWidgets.QTreeWidgetItem([key_text, f"dict ({len(value)})"])
            parent_item.addChild(item)
            for child_key in sorted(value.keys(), key=lambda x: str(x)):
                self._add_param_tree_item(item, child_key, value.get(child_key))
            item.setExpanded(True)
            return item

        if isinstance(value, (list, tuple)):
            item = QtWidgets.QTreeWidgetItem([key_text, f"list ({len(value)})"])
            parent_item.addChild(item)
            for idx, child_value in enumerate(value):
                self._add_param_tree_item(item, f"[{idx}]", child_value)
            item.setExpanded(True)
            return item

        item = QtWidgets.QTreeWidgetItem([key_text, self._short_scalar_text(value)])
        parent_item.addChild(item)
        return item

    def _show_modeless_param_dialog(self, dialog):
        """Show a parameter dialog without blocking the main GUI.

        Keep a Python reference so several parameter windows can stay open
        at the same time for comparison.
        """
        try:
            flags = dialog.windowFlags()
            context_hint = QtCore.Qt.WindowType.WindowContextHelpButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowContextHelpButtonHint
            maximize_hint = QtCore.Qt.WindowType.WindowMaximizeButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowMaximizeButtonHint
            minimize_hint = QtCore.Qt.WindowType.WindowMinimizeButtonHint if hasattr(QtCore.Qt, "WindowType") else QtCore.Qt.WindowMinimizeButtonHint
            dialog.setWindowFlags((flags & ~context_hint) | maximize_hint | minimize_hint)
        except Exception:
            pass
        try:
            dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal if hasattr(QtCore.Qt, "WindowModality") else QtCore.Qt.NonModal)
        except Exception:
            pass
        try:
            dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        except Exception:
            try:
                dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            except Exception:
                pass

        if not hasattr(self, "_param_windows"):
            self._param_windows = []
        self._param_windows.append(dialog)
        try:
            dialog.destroyed.connect(lambda *_: self._param_windows.remove(dialog) if dialog in self._param_windows else None)
        except Exception:
            pass
        dialog.show()
        try:
            dialog.raise_()
            dialog.activateWindow()
        except Exception:
            pass

    def _show_json_dialog(self, title, value, source_path=None):
        """Show a readable parameter tree only."""
        if value is None:
            QtWidgets.QMessageBox.information(
                self,
                title,
                "No saved parameter file was found for this sorting run.\n\n"
                "This can happen for legacy runs created before run snapshots were added.",
            )
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(820, 600)
        vbox = QtWidgets.QVBoxLayout(dialog)
        vbox.setContentsMargins(12, 12, 12, 12)
        vbox.setSpacing(8)

        header = QtWidgets.QLabel(title, dialog)
        header.setStyleSheet("font-size: 15px; font-weight: 700;")
        vbox.addWidget(header)

        if source_path is not None:
            src = QtWidgets.QLabel(f"Source: {source_path}", dialog)
            src.setWordWrap(True)
            src.setStyleSheet("color: #555;")
            vbox.addWidget(src)

        tree = QtWidgets.QTreeWidget(dialog)
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Parameter", "Value"])
        tree.setAlternatingRowColors(True)
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(True)
        root = tree.invisibleRootItem()
        safe_value = _json_safe(value)
        if isinstance(safe_value, dict):
            for child_key in sorted(safe_value.keys(), key=lambda x: str(x)):
                self._add_param_tree_item(root, child_key, safe_value.get(child_key))
        elif isinstance(safe_value, list):
            for idx, child_value in enumerate(safe_value):
                self._add_param_tree_item(root, f"[{idx}]", child_value)
        else:
            self._add_param_tree_item(root, "value", safe_value)
        tree.expandToDepth(1)
        tree.resizeColumnToContents(0)
        vbox.addWidget(tree, 1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close if hasattr(QtWidgets.QDialogButtonBox, "StandardButton") else QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.close)
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).clicked.connect(dialog.close)
        except Exception:
            buttons.accepted.connect(dialog.close)
        vbox.addWidget(buttons)
        self._show_modeless_param_dialog(dialog)

    def _bool_from_saved(self, value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return bool(default)
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "checked"}

    def _pipeline_steps_from_value(self, value):
        safe_value = _json_safe(value)
        if isinstance(safe_value, dict):
            pipeline = safe_value.get("pipeline", None)
            if pipeline is None:
                pipeline = safe_value.get("enabled_pipeline", None)
            if isinstance(pipeline, list):
                return [step for step in pipeline if isinstance(step, dict)]
            if any(k in safe_value for k in ("name", "key", "params", "enabled")):
                return [safe_value]
        if isinstance(safe_value, list):
            return [step for step in safe_value if isinstance(step, dict)]
        return []

    def _add_readonly_param_grid(self, layout, params):
        params = dict(params or {})
        if not params:
            lbl = QtWidgets.QLabel("No parameter")
            lbl.setStyleSheet("color: #777;")
            layout.addWidget(lbl, 0, 0)
            return

        max_cols = 3
        for i, (param_name, value) in enumerate(params.items()):
            row = i // max_cols
            col = (i % max_cols) * 2
            name_lbl = QtWidgets.QLabel(str(param_name).replace("_", " ") + ":")
            name_lbl.setStyleSheet("font-weight: 600;")
            val_lbl = QtWidgets.QLabel(self._short_scalar_text(value))
            val_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse if hasattr(QtCore.Qt, "TextInteractionFlag") else QtCore.Qt.TextSelectableByMouse)
            val_lbl.setWordWrap(True)
            layout.addWidget(name_lbl, row, col)
            layout.addWidget(val_lbl, row, col + 1)

    def _show_preprocessing_pipeline_dialog(self, title, value, source_path=None):
        if value is None:
            QtWidgets.QMessageBox.information(
                self,
                title,
                "No saved preprocessing parameter file was found for this sorting run.\n\n"
                "This can happen for legacy runs created before run snapshots were added.",
            )
            return

        steps = self._pipeline_steps_from_value(value)
        if not steps:
            self._show_json_dialog(title, value, source_path)
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(980, 680)
        vbox = QtWidgets.QVBoxLayout(dialog)
        vbox.setContentsMargins(12, 12, 12, 12)
        vbox.setSpacing(8)

        header = QtWidgets.QLabel(title, dialog)
        header.setStyleSheet("font-size: 15px; font-weight: 700;")
        vbox.addWidget(header)

        if source_path is not None:
            src = QtWidgets.QLabel(f"Source: {source_path}", dialog)
            src.setWordWrap(True)
            src.setStyleSheet("color: #555;")
            vbox.addWidget(src)

        scroll = QtWidgets.QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        body = QtWidgets.QWidget(scroll)
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(6, 6, 6, 6)
        body_layout.setSpacing(6)

        ordered_steps = sorted(
            steps,
            key=lambda step: (
                int(float(step.get("order", step.get("display_index", 999999)) or 999999)),
                str(step.get("name", step.get("key", ""))),
            ),
        )

        for idx, step in enumerate(ordered_steps, start=1):
            enabled = self._bool_from_saved(step.get("enabled", True), default=True)
            name = str(step.get("name", step.get("key", f"Step {idx}")) or f"Step {idx}")
            title_text = name if enabled else f"{name} (disabled)"
            group = QtWidgets.QGroupBox(title_text, body)
            if not enabled:
                group.setStyleSheet("QGroupBox { color: #777; }")
            grid = QtWidgets.QGridLayout(group)
            grid.setContentsMargins(8, 8, 8, 8)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(4)
            grid.setColumnStretch(3, 1)

            enabled_box = QtWidgets.QCheckBox("Enabled", group)
            enabled_box.setChecked(enabled)
            enabled_box.setEnabled(False)
            grid.addWidget(enabled_box, 0, 0)

            order_text = str(step.get("order", ""))
            order_lbl = QtWidgets.QLabel(f"Order: {order_text}" if order_text else "Order: -", group)
            grid.addWidget(order_lbl, 0, 1)

            key_text = str(step.get("key", "") or "")
            if key_text:
                key_lbl = QtWidgets.QLabel(f"Key: {key_text}", group)
                key_lbl.setStyleSheet("color: #666;")
                grid.addWidget(key_lbl, 0, 2)

            source_bits = []
            if step.get("source"):
                source_bits.append(str(step.get("source")))
            if step.get("source_file"):
                try:
                    source_bits.append(Path(str(step.get("source_file"))).name)
                except Exception:
                    source_bits.append(str(step.get("source_file")))
            if source_bits:
                source_lbl = QtWidgets.QLabel(" | ".join(source_bits), group)
                source_lbl.setStyleSheet("color: #666;")
                source_lbl.setWordWrap(True)
                grid.addWidget(source_lbl, 1, 0, 1, 3)

            params_box = QtWidgets.QWidget(group)
            params_layout = QtWidgets.QGridLayout(params_box)
            params_layout.setContentsMargins(0, 0, 0, 0)
            params_layout.setHorizontalSpacing(10)
            params_layout.setVerticalSpacing(4)
            self._add_readonly_param_grid(params_layout, step.get("params", {}) or {})
            grid.addWidget(params_box, 2, 0, 1, 4)

            body_layout.addWidget(group)

        body_layout.addStretch(1)
        scroll.setWidget(body)
        vbox.addWidget(scroll, 1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close if hasattr(QtWidgets.QDialogButtonBox, "StandardButton") else QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.close)
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).clicked.connect(dialog.close)
        except Exception:
            buttons.accepted.connect(dialog.close)
        vbox.addWidget(buttons)
        self._show_modeless_param_dialog(dialog)

    def show_selected_preprocessing_params(self):
        info = self._selected_row_info()
        if info is None:
            return
        value, source = self._load_json_from_info(
            info,
            "preprocessing_pipeline_path",
            fallback_key="preprocessing_pipeline",
        )
        pipeline_id = str(info.get("preprocessing_pipeline_id") or "Pipeline ?")
        self._show_preprocessing_pipeline_dialog(f"Preprocessing parameters - {pipeline_id}", value, source)

    def show_selected_sorter_params(self):
        info = self._selected_row_info()
        if info is None:
            return
        value, source = self._load_json_from_info(
            info,
            "sorter_params_path",
            fallback_key="sorter_params",
        )
        pipeline_id = str(info.get("sorter_params_id") or "Pipeline ?")
        sorter_name = str(info.get("sorter") or "Sorter").strip() or "Sorter"
        self._show_json_dialog(f"{sorter_name} parameters - {pipeline_id}", value, source)

    def show_selected_run_config(self):
        info = self._selected_row_info()
        if info is None:
            return
        value, source = self._load_json_from_info(info, "run_config_path")
        if value is None:
            value, source = self._load_json_from_info(info, "run_json")
        self._show_json_dialog("Sorting run configuration", value, source)

    def delete_selected_sorting_run(self):
        infos = self._selected_row_infos()
        if not infos:
            return

        in_progress = [info for info in infos if isinstance(info, dict) and info.get("sorting_in_progress")]
        if in_progress:
            QtWidgets.QMessageBox.information(
                self,
                "Delete sorting run",
                "At least one selected sorting run is still in progress. Deselect running rows before deleting.",
            )
            return

        folders = []
        missing = []
        seen = set()
        for info in infos:
            try:
                folder = Path(info.get("output_folder"))
            except Exception:
                continue
            key = str(folder.resolve()) if folder else str(folder)
            if key in seen:
                continue
            seen.add(key)
            if folder.exists():
                folders.append((folder, info))
            else:
                missing.append(folder)

        if not folders:
            if missing:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Delete sorting run",
                    "None of the selected folders exist on disk anymore. The table will be refreshed.",
                )
                self.refresh_outputs()
            return

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Delete sorting run")
        msg.setIcon(QtWidgets.QMessageBox.Icon.Warning if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Warning)
        if len(folders) == 1:
            folder, info = folders[0]
            msg.setText(
                "Delete this sorting run from disk?\n\n"
                f"Recording: {info.get('recording', '')}\n"
                f"Sorter: {info.get('sorter', '')}\n"
                f"Run: {info.get('run_name', '')}\n\n"
                f"Folder:\n{folder}"
            )
        else:
            listed = "\n".join(f"- {folder}" for folder, _info in folders[:12])
            if len(folders) > 12:
                listed += f"\n... and {len(folders) - 12} more"
            msg.setText(f"Delete {len(folders)} selected sorting run folders from disk?\n\n{listed}")
        msg.setInformativeText("This removes the sorter output, analyzer, summaries, and saved run configuration in the selected folder(s).")
        delete_label = "Delete selected runs" if len(folders) > 1 else "Delete sorting run"
        delete_btn = msg.addButton(delete_label, QtWidgets.QMessageBox.ButtonRole.DestructiveRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.DestructiveRole)
        cancel_btn = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        if msg.clickedButton() is not delete_btn:
            return

        failures = []
        deleted = 0
        self._append_log("")
        for folder, _info in folders:
            try:
                self._append_log(f"Deleting sorting run: {folder}")
                shutil.rmtree(folder)
                deleted += 1
            except Exception as exc:
                failures.append((folder, exc))
                self._append_log(f"ERROR deleting {folder}:")
                self._append_log(traceback.format_exc())

        self.current_analyzer = None
        self.current_analyzer_folder = None
        self._append_log(f"Deleted {deleted} sorting run folder(s).")
        self.refresh_outputs()

        if failures:
            details = "\n".join(f"{folder}: {exc}" for folder, exc in failures[:8])
            if len(failures) > 8:
                details += f"\n... and {len(failures) - 8} more"
            QtWidgets.QMessageBox.warning(
                self,
                "Delete sorting run",
                f"Deleted {deleted} folder(s), but {len(failures)} deletion(s) failed.\n\n{details}",
            )

    def load_selected_analyzer(self, variant=None):
        info = self._selected_row_info()
        if info is None:
            return
        if variant is None:
            variant = getattr(self, "_pending_analyzer_variant", None) or self._original_analyzer_variant_for_row(info)
        folder = Path(variant.get("folder", info.get("analyzer_folder")))
        label = str(variant.get("label", "Original") or "Original")
        if not folder.exists():
            QtWidgets.QMessageBox.warning(self, "Curation", f"SortingAnalyzer folder does not exist:\n{folder}")
            return
        try:
            if self.current_analyzer is not None and self.current_analyzer_folder is not None and Path(self.current_analyzer_folder) == folder:
                return
        except Exception:
            pass
        self._append_log("")
        self._append_log(f"Loading {label} SortingAnalyzer: {folder}")
        self._set_busy(True, f"Curation: loading {label} SortingAnalyzer...")
        QtWidgets.QApplication.processEvents()
        try:
            try:
                from spikeinterface.core import load_sorting_analyzer
            except Exception:
                from spikeinterface.full import load_sorting_analyzer
            analyzer = _pp_external_call(load_sorting_analyzer, folder, str(folder))
            self.current_analyzer = analyzer
            self.current_analyzer_folder = folder
            sorting = getattr(analyzer, "sorting", None)
            unit_ids = []
            if sorting is not None:
                try:
                    unit_ids = list(sorting.get_unit_ids())
                except Exception:
                    unit_ids = []
            self._append_log("Loaded analyzer successfully.")
            self._append_log(f"Units: {len(unit_ids)}")
            if unit_ids:
                preview = ", ".join(str(u) for u in unit_ids[:20])
                if len(unit_ids) > 20:
                    preview += ", ..."
                self._append_log(f"Unit IDs: {preview}")
            try:
                ext_names = list(analyzer.get_loaded_extension_names())
            except Exception:
                ext_names = []
            if ext_names:
                self._append_log(f"Loaded extensions: {', '.join(map(str, ext_names))}")
            else:
                self._append_log("Loaded extensions: none reported")
            self.status_label.setText(f"{label} analyzer loaded: {len(unit_ids)} unit(s)")
        except Exception:
            self.current_analyzer = None
            self.current_analyzer_folder = None
            self._append_log("ERROR while loading SortingAnalyzer:")
            self._append_log(traceback.format_exc())
            self.status_label.setText("Could not load analyzer.")
        finally:
            self._set_busy(False, "Curation ready.")
    def open_selected_in_curation_gui(self):
        """Ask which curation interface to open for the selected run."""
        info = self._selected_row_info()
        if info is None:
            return
        if info.get("sorting_in_progress"):
            QtWidgets.QMessageBox.information(self, "Open curation", "This sorting run is still in progress. Wait until it finishes before opening curation.")
            return

        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Open curation")
        msg.setIcon(QtWidgets.QMessageBox.Icon.Question if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Question)
        msg.setText("Choose the curation interface for the selected sorting run.")
        msg.setInformativeText(
            f"Recording: {info.get('display_recording', info.get('recording', ''))}\n"
            f"Sorter: {info.get('sorter', '')}\n"
            f"Preprocessing: {info.get('preprocessing_pipeline_id', 'Pipeline ?')}\n"
            f"Sorter params: {info.get('sorter_params_id', 'Pipeline ?')}"
        )
        role = QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole
        reject_role = QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole
        si_btn = msg.addButton("SpikeInterface GUI", role)
        phy_btn = msg.addButton("Phy", role)
        cancel_btn = msg.addButton("Cancel", reject_role)
        msg.setDefaultButton(si_btn)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is si_btn:
            variant = self._choose_analyzer_variant(
                info,
                title="Open SpikeInterface GUI",
                text="Choose whether to open the original or curated SortingAnalyzer.",
            )
            if variant is None:
                return
            self._pending_analyzer_variant = variant
            try:
                self._open_selected_in_spikeinterface_gui()
            finally:
                self._pending_analyzer_variant = None
        elif clicked is phy_btn:
            self._pending_analyzer_variant = self._original_analyzer_variant_for_row(info)
            try:
                self._open_selected_in_phy()
            finally:
                self._pending_analyzer_variant = None
        else:
            return

    def _open_selected_in_spikeinterface_gui(self):
        self._append_log("")
        self._append_log("Trying to open spikeinterface_gui in curation mode...")
        self._set_busy(True, "Curation: opening SpikeInterface GUI...")
        QtWidgets.QApplication.processEvents()
        stream = _QtAppendLogStream(self._append_log)
        info_for_auto_apply = dict(self._selected_row_info() or {})
        variant_for_auto_apply = getattr(self, "_pending_analyzer_variant", None)
        auto_apply_state = {"prompted": False}
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                variant = getattr(self, "_pending_analyzer_variant", None)
                folder = None
                try:
                    folder = Path(variant.get("folder")) if variant is not None else None
                except Exception:
                    folder = None
                if self.current_analyzer is None or (folder is not None and Path(getattr(self, "current_analyzer_folder", "")) != folder):
                    self.load_selected_analyzer(variant=variant)
                analyzer = self.current_analyzer
                if analyzer is None:
                    return

                try:
                    import spikeinterface_gui
                except Exception as exc:
                    raise RuntimeError(
                        "spikeinterface_gui is not installed. Install it first, or use Phy."
                    ) from exc

                # Some spikeinterface_gui default layouts include scatter views such as
                # spikeamplitude/spikedepth. These can fail when the corresponding data
                # contain only NaN values (pyqtgraph refuses a [nan, nan] range).
                # Open a curation-safe layout first, without scatter-based views.
                # The ISI and correlogram views also depend on analyzer extensions, so
                # compute them before opening the GUI and resolve the exact view IDs from
                # the installed spikeinterface_gui version.
                def _has_analyzer_extension(sa, extension_name):
                    for method_name in ("has_extension", "is_extension"):
                        method = getattr(sa, method_name, None)
                        if callable(method):
                            try:
                                return bool(method(extension_name))
                            except Exception:
                                pass
                    try:
                        names = sa.get_loaded_extension_names()
                        return extension_name in names
                    except Exception:
                        return False

                def _ensure_analyzer_extension(extension_name):
                    if _has_analyzer_extension(analyzer, extension_name):
                        return True
                    compute = getattr(analyzer, "compute", None)
                    if not callable(compute):
                        self._append_log(f"WARNING: cannot compute {extension_name}; analyzer has no compute() method.")
                        return False
                    try:
                        self._append_log(f'Computing analyzer extension for SpikeInterface GUI: {extension_name}')
                        job_kwargs = _spikeinterface_job_kwargs(_launcher_multiprocessing_enabled(), _launcher_n_jobs())
                        try:
                            compute(extension_name, **job_kwargs)
                        except TypeError:
                            compute(extension_name)
                        return True
                    except Exception as exc:
                        self._append_log(f"WARNING: could not compute {extension_name}: {exc}")
                        return False

                _ensure_analyzer_extension("isi_histograms")
                _ensure_analyzer_extension("correlograms")

                available_views = None
                try:
                    from spikeinterface_gui.viewlist import get_all_possible_views
                    available_views = set(get_all_possible_views().keys())
                    self._append_log("Available spikeinterface_gui views: " + ", ".join(sorted(available_views)))
                except Exception as exc:
                    self._append_log(f"WARNING: could not inspect spikeinterface_gui view IDs: {exc}")

                def _resolve_view_id(candidates):
                    if available_views is None:
                        return candidates[0]
                    for candidate in candidates:
                        if candidate in available_views:
                            return candidate
                    lower_map = {str(v).lower(): v for v in available_views}
                    for candidate in candidates:
                        found = lower_map.get(str(candidate).lower())
                        if found:
                            return found
                    for view_id in sorted(available_views):
                        low = str(view_id).lower()
                        if any(str(candidate).lower() in low or low in str(candidate).lower() for candidate in candidates):
                            return view_id
                    return None

                def _keep_available(view_ids):
                    out = []
                    for view_id in view_ids:
                        if not view_id:
                            continue
                        if available_views is None or view_id in available_views:
                            out.append(view_id)
                    return out

                isi_view_id = _resolve_view_id(["isi", "isiview", "isi_histogram", "isi_histograms", "interspike", "interspike_interval"])
                correlogram_view_id = _resolve_view_id(["correlogram", "correlograms", "correlogramview", "autocorrelogram", "auto_correlogram"])
                if isi_view_id:
                    self._append_log(f"Using SpikeInterface GUI ISI view id: {isi_view_id}")
                else:
                    self._append_log("WARNING: no ISI view id found in this spikeinterface_gui installation.")
                if correlogram_view_id:
                    self._append_log(f"Using SpikeInterface GUI correlogram view id: {correlogram_view_id}")
                else:
                    self._append_log("WARNING: no correlogram view id found in this spikeinterface_gui installation.")

                safe_layout = {
                    # Keep scatter-based views out of the startup layout because they can fail
                    # when their corresponding extension contains only NaN values.
                    # Add curation-relevant non-scatter views explicitly.
                    "zone1": _keep_available(["unitlist", "curation"]),
                    "zone2": _keep_available(["spikelist"]),
                    "zone3": _keep_available(["waveform"]),
                    "zone4": _keep_available(["waveformheatmap"]),
                    "zone5": _keep_available([isi_view_id]),
                    "zone6": _keep_available([correlogram_view_id]),
                    "zone7": _keep_available(["merge"]),
                    "zone8": _keep_available(["similarity"]),
                }
                minimal_layout = {
                    "zone1": _keep_available(["unitlist", "curation"]),
                    "zone2": _keep_available([isi_view_id]),
                    "zone3": _keep_available([correlogram_view_id]),
                    "zone4": _keep_available(["waveform"]),
                    "zone5": _keep_available(["merge"]),
                    "zone6": [],
                    "zone7": [],
                    "zone8": [],
                }

                # spikeinterface-gui curation mode is enabled with curation=True.
                # The API changed a little across versions, so only pass optional
                # keyword arguments when the installed version supports them.
                import inspect

                def _supported_kwargs(func, wanted):
                    try:
                        sig = inspect.signature(func)
                        params = sig.parameters
                        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
                            return dict(wanted)
                        return {k: v for k, v in wanted.items() if k in params}
                    except Exception:
                        return dict(wanted)

                def _is_nan_range_error(exc):
                    msg = str(exc).lower()
                    tb = traceback.format_exc().lower()
                    return ("nan" in msg and "range" in msg) or "cannot set range [nan, nan]" in tb

                def _install_pyqtgraph_nan_range_guard():
                    """Prevent spikeinterface_gui scatter views from crashing on all-NaN ranges.

                    Some spikeinterface_gui versions still create or refresh scatter-based
                    views even when the startup layout tries to avoid them. If one of those
                    views has only NaN coordinates, pyqtgraph raises
                    ``Cannot set range [nan, nan]`` and the whole curation window closes.
                    The guard keeps the window open by replacing non-finite ranges with a
                    harmless [0, 1] range. It is intentionally global for this session so
                    the same protection remains active after the curation window is open.
                    """
                    try:
                        from pyqtgraph.graphicsItems.ViewBox.ViewBox import ViewBox
                    except Exception as exc:
                        self._append_log(f"WARNING: could not install pyqtgraph NaN range guard: {exc}")
                        return False

                    if getattr(ViewBox, "_nwb_gui_nan_range_guard", False):
                        return True

                    original_set_y_range = ViewBox.setYRange
                    original_set_x_range = ViewBox.setXRange

                    def _finite_pair(a, b):
                        try:
                            a = float(a)
                            b = float(b)
                        except Exception:
                            return 0.0, 1.0, True
                        bad = (not math.isfinite(a)) or (not math.isfinite(b))
                        if bad:
                            return 0.0, 1.0, True
                        if a == b:
                            pad = max(abs(a) * 0.01, 0.5)
                            return a - pad, b + pad, True
                        return a, b, False

                    def guarded_set_y_range(self_vb, min, max, padding=0.02, update=True):
                        mn, mx, changed = _finite_pair(min, max)
                        if changed:
                            padding = 0.0
                        return original_set_y_range(self_vb, mn, mx, padding=padding, update=update)

                    def guarded_set_x_range(self_vb, min, max, padding=0.02, update=True):
                        mn, mx, changed = _finite_pair(min, max)
                        if changed:
                            padding = 0.0
                        return original_set_x_range(self_vb, mn, mx, padding=padding, update=update)

                    ViewBox.setYRange = guarded_set_y_range
                    ViewBox.setXRange = guarded_set_x_range
                    ViewBox._nwb_gui_nan_range_guard = True
                    ViewBox._nwb_gui_original_setYRange = original_set_y_range
                    ViewBox._nwb_gui_original_setXRange = original_set_x_range
                    self._append_log("Installed pyqtgraph NaN range guard for SpikeInterface GUI scatter views.")
                    return True

                def _remember_window(win):
                    """Keep a reference and ask to apply curation when the Qt window closes.

                    Some spikeinterface_gui versions do not destroy their QMainWindow when
                    the user closes it; they only hide it.  The previous implementation only
                    listened to ``destroyed``, so closing the window did not always trigger the
                    apply-curation prompt.  Watch Close/Hide events and poll visibility as a
                    fallback.
                    """
                    if win is None:
                        return False
                    if isinstance(win, (list, tuple)):
                        kept = False
                        for item in win:
                            kept = _remember_window(item) or kept
                        return kept
                    if not hasattr(win, "installEventFilter"):
                        return False

                    try:
                        win.show()
                    except Exception:
                        pass
                    try:
                        if win not in self._curation_windows:
                            self._curation_windows.append(win)
                    except Exception:
                        pass

                    def _find_saved_curation_file():
                        try:
                            folder = Path(variant_for_auto_apply.get("folder")) if isinstance(variant_for_auto_apply, dict) else None
                            if folder is None:
                                return None
                            candidates = [
                                folder / "spikeinterface_gui" / "curation_data.json",
                                folder / "curation_data.json",
                            ]
                            for candidate in candidates:
                                if candidate.exists():
                                    return candidate
                        except Exception:
                            return None
                        return None

                    def _trigger_apply_prompt(reason="closed"):
                        if auto_apply_state.get("prompted"):
                            return
                        auto_apply_state["prompted"] = True
                        try:
                            if win in self._curation_windows:
                                self._curation_windows.remove(win)
                        except Exception:
                            pass
                        self._append_log(f"SpikeInterface GUI window {reason}.")
                        self._schedule_apply_prompt_after_close(
                            info_for_auto_apply,
                            "spikeinterface_gui",
                            curation_file=_find_saved_curation_file(),
                            base_variant=variant_for_auto_apply,
                        )

                    class _SpikeInterfaceCloseWatcher(QtCore.QObject):
                        def eventFilter(self, obj, event):
                            try:
                                event_type = event.type()
                                close_type = QtCore.QEvent.Type.Close if hasattr(QtCore.QEvent, "Type") else QtCore.QEvent.Close
                                if event_type == close_type:
                                    # Only the real close event should trigger the prompt immediately.
                                    # Do not trigger on Hide here: spikeinterface_gui creates and hides
                                    # many temporary top-level widgets during startup, which caused a
                                    # cascade of Apply curation dialogs.
                                    QtCore.QTimer.singleShot(250, lambda: _trigger_apply_prompt("closed"))
                            except Exception:
                                pass
                            return False

                    watcher = _SpikeInterfaceCloseWatcher(win)
                    try:
                        win.installEventFilter(watcher)
                    except Exception:
                        pass
                    try:
                        self._curation_windows.append(watcher)  # keep watcher alive
                    except Exception:
                        pass

                    def _on_window_destroyed(*_):
                        _trigger_apply_prompt("closed")

                    try:
                        win.destroyed.connect(_on_window_destroyed)
                    except Exception:
                        pass

                    visibility_state = {"ever_visible": False, "poll_count": 0}

                    def _poll_visibility():
                        if auto_apply_state.get("prompted"):
                            return
                        visibility_state["poll_count"] += 1
                        try:
                            visible = bool(win.isVisible())
                        except Exception:
                            if visibility_state.get("ever_visible"):
                                _trigger_apply_prompt("closed")
                            return

                        if visible:
                            visibility_state["ever_visible"] = True
                            QtCore.QTimer.singleShot(1000, _poll_visibility)
                            return

                        # If the watched main window was visible before and now is hidden,
                        # treat that as a close.  If it was never visible, keep waiting for
                        # startup to finish instead of opening the Apply curation dialog.
                        if visibility_state.get("ever_visible"):
                            _trigger_apply_prompt("closed")
                            return

                        if visibility_state["poll_count"] < 60:
                            QtCore.QTimer.singleShot(1000, _poll_visibility)

                    QtCore.QTimer.singleShot(1500, _poll_visibility)
                    return True

                def _remember_new_top_level_windows(before_widgets):
                    """Fallback for spikeinterface_gui versions whose run_mainwindow returns None.

                    Only watch the actual main GUI window.  spikeinterface_gui/Qt creates many
                    transient top-level widgets during startup (QMenu, QFrame, helper QWidget,
                    etc.). Watching those widgets makes their startup hide/close events look
                    like the curation window was closed and opens many Apply curation dialogs.
                    """
                    kept = False

                    def _is_likely_main_window(widget):
                        try:
                            if widget is self.window() or widget is self:
                                return False
                            cls_name = type(widget).__name__.lower()
                            title = ""
                            try:
                                title = str(widget.windowTitle() or "").lower()
                            except Exception:
                                title = ""

                            # Explicitly ignore transient Qt popups/menus/dialogs.
                            ignored_names = (
                                "qmenu", "qframe", "qtooltip", "qmessagebox", "qdialog",
                                "qcombobox", "qgraphics", "qwidgetaction", "qtoolbar",
                            )
                            if any(name in cls_name for name in ignored_names):
                                return False

                            try:
                                if isinstance(widget, QtWidgets.QMenu):
                                    return False
                                if isinstance(widget, QtWidgets.QDialog):
                                    return False
                            except Exception:
                                pass

                            try:
                                if isinstance(widget, QtWidgets.QMainWindow):
                                    return True
                            except Exception:
                                pass

                            return (
                                "mainwindow" in cls_name
                                or "spikeinterface" in title
                                or "sortinganalyzer" in title
                                or "curation" in title
                            )
                        except Exception:
                            return False

                    try:
                        after_widgets = list(QtWidgets.QApplication.topLevelWidgets())
                    except Exception:
                        return False
                    for widget in after_widgets:
                        try:
                            if widget in before_widgets:
                                continue
                            if not _is_likely_main_window(widget):
                                continue
                            title = ""
                            try:
                                title = str(widget.windowTitle())
                            except Exception:
                                title = ""
                            if _remember_window(widget):
                                kept = True
                                self._append_log(f"Watching SpikeInterface GUI main window close event: {title or type(widget).__name__}")
                        except Exception:
                            continue
                    return kept

                _install_pyqtgraph_nan_range_guard()

                run_mainwindow = getattr(spikeinterface_gui, "run_mainwindow", None)
                if callable(run_mainwindow):
                    # run_app=False is supported by some versions and lets the existing
                    # GUI Qt event loop keep the window alive. It is only passed when
                    # the installed version exposes it.
                    wanted = {
                        "mode": "desktop",
                        "curation": True,
                        "layout_dict": safe_layout,
                        "run_app": False,
                    }
                    kwargs = _supported_kwargs(run_mainwindow, wanted)
                    if "curation" not in kwargs:
                        self._append_log(
                            "WARNING: this spikeinterface_gui.run_mainwindow() does not expose a curation argument; "
                            "opening the standard GUI instead."
                        )
                    if "layout_dict" in kwargs:
                        self._append_log("Opening spikeinterface_gui with curation, ISI and correlogram views.")
                    try:
                        before_widgets = set(QtWidgets.QApplication.topLevelWidgets())
                    except Exception:
                        before_widgets = set()
                    try:
                        win = run_mainwindow(analyzer, **kwargs)
                    except TypeError:
                        # Older versions used a simpler positional-only call.
                        win = run_mainwindow(analyzer)
                    except Exception as exc:
                        if "layout_dict" in kwargs and _is_nan_range_error(exc):
                            self._append_log(
                                "spikeinterface_gui failed while opening a scatter/range view with NaN limits. "
                                "Retrying with a minimal curation layout."
                            )
                            kwargs["layout_dict"] = minimal_layout
                            try:
                                before_widgets = set(QtWidgets.QApplication.topLevelWidgets())
                            except Exception:
                                before_widgets = set()
                            win = run_mainwindow(analyzer, **kwargs)
                        else:
                            raise
                    kept = _remember_window(win)
                    kept = _remember_new_top_level_windows(before_widgets) or kept
                    if kept:
                        self._append_log("Opened spikeinterface_gui in curation mode.")
                    else:
                        self._append_log(
                            "spikeinterface_gui returned without giving a window reference. "
                            "If the window closes immediately, update spikeinterface_gui or use Phy."
                        )
                    return

                try:
                    from spikeinterface_gui.mainwindow import MainWindow
                except Exception as exc:
                    raise RuntimeError("Could not import spikeinterface_gui.mainwindow.MainWindow") from exc

                wanted = {"sorting_analyzer": analyzer, "curation": True, "layout_dict": safe_layout}
                kwargs = _supported_kwargs(MainWindow, wanted)
                try:
                    before_widgets = set(QtWidgets.QApplication.topLevelWidgets())
                except Exception:
                    before_widgets = set()
                if "sorting_analyzer" in kwargs:
                    win = MainWindow(**kwargs)
                else:
                    # Fallback for very old versions where the analyzer was positional.
                    kwargs = _supported_kwargs(MainWindow, {"curation": True, "layout_dict": safe_layout})
                    win = MainWindow(analyzer, **kwargs)
                if "curation" not in kwargs:
                    self._append_log(
                        "WARNING: this spikeinterface_gui.MainWindow does not expose a curation argument; "
                        "opening the standard GUI instead."
                    )
                kept = _remember_window(win)
                kept = _remember_new_top_level_windows(before_widgets) or kept
                if kept:
                    self._append_log("Opened spikeinterface_gui MainWindow in curation mode.")
                else:
                    self._append_log("Opened spikeinterface_gui MainWindow, but no close event could be watched.")
        except Exception:
            self._append_log("ERROR while opening spikeinterface_gui:")
            self._append_log(traceback.format_exc())
            QtWidgets.QMessageBox.warning(
                self,
                "SpikeInterface GUI",
                "Could not open spikeinterface_gui. See the curation log for details.",
            )
        finally:
            try:
                stream.flush()
            except Exception:
                pass
            self._set_busy(False, "Curation ready.")


    def _slug_text(self, value):
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9_\-]+", "_", text)
        text = text.strip("_")
        return text or "curation"

    def _load_current_or_selected_analyzer(self):
        info = self._selected_row_info()
        variant = self._original_analyzer_variant_for_row(info) if info is not None else None
        folder = None
        try:
            folder = Path(variant.get("folder")) if variant is not None else None
        except Exception:
            folder = None
        if self.current_analyzer is None or (folder is not None and Path(getattr(self, "current_analyzer_folder", "")) != folder):
            self.load_selected_analyzer(variant=variant)
        return self.current_analyzer

    def _count_sorting_units(self, sorting):
        try:
            return len(list(sorting.get_unit_ids()))
        except Exception:
            return ""

    def _get_analyzer_recording(self, analyzer):
        for attr in ("recording", "_recording"):
            try:
                rec = getattr(analyzer, attr, None)
                if rec is not None:
                    return rec
            except Exception:
                pass
        for method in ("get_recording", "get_recording_segment"):
            try:
                func = getattr(analyzer, method, None)
                if callable(func):
                    rec = func()
                    if rec is not None:
                        return rec
            except Exception:
                pass
        return None

    def _create_sorting_analyzer_compat(self, sorting, recording, folder, *, overwrite=True):
        try:
            from spikeinterface.core import create_sorting_analyzer
        except Exception:
            try:
                from spikeinterface.full import create_sorting_analyzer
            except Exception:
                create_sorting_analyzer = None
        if create_sorting_analyzer is None:
            raise RuntimeError("Could not import spikeinterface create_sorting_analyzer().")

        job_kwargs = _spikeinterface_job_kwargs(_launcher_multiprocessing_enabled(), _launcher_n_jobs())
        attempts = [
            lambda: create_sorting_analyzer(sorting=sorting, recording=recording, folder=str(folder), format="binary_folder", overwrite=overwrite, **job_kwargs),
            lambda: create_sorting_analyzer(sorting, recording, folder=str(folder), format="binary_folder", overwrite=overwrite, **job_kwargs),
            lambda: create_sorting_analyzer(sorting=sorting, recording=recording, folder=str(folder), overwrite=overwrite, **job_kwargs),
            lambda: create_sorting_analyzer(sorting, recording, folder=str(folder), overwrite=overwrite, **job_kwargs),
            lambda: create_sorting_analyzer(sorting=sorting, recording=recording, folder=str(folder), format="binary_folder", overwrite=overwrite),
            lambda: create_sorting_analyzer(sorting, recording, folder=str(folder), format="binary_folder", overwrite=overwrite),
            lambda: create_sorting_analyzer(sorting=sorting, recording=recording, folder=str(folder), overwrite=overwrite),
            lambda: create_sorting_analyzer(sorting, recording, folder=str(folder), overwrite=overwrite),
        ]
        last_exc = None
        for attempt in attempts:
            try:
                return attempt()
            except TypeError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Could not create curated SortingAnalyzer.")

    def _confirm_overwrite_folder(self, folder, title="Apply curation", *, delete_now=True):
        folder = Path(folder)
        if not folder.exists():
            return True
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setIcon(QtWidgets.QMessageBox.Icon.Warning if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Warning)
        msg.setText("A curated SortingAnalyzer folder already exists.")
        msg.setInformativeText(f"Delete and recreate it?\n\n{folder}")
        yes = msg.addButton("Delete and recreate", QtWidgets.QMessageBox.ButtonRole.DestructiveRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.DestructiveRole)
        no = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole)
        msg.setDefaultButton(no)
        msg.exec()
        if msg.clickedButton() is not yes:
            return False
        if delete_now:
            shutil.rmtree(folder)
        return True

    def _update_sorting_run_with_curation(self, info, curated_folder, source_name, source_path, unit_count, validation_path=None, unit_summary_folder=None):
        run_json = info.get("run_json")
        if run_json is None:
            return
        run_json = Path(run_json)
        payload = {}
        if run_json.exists():
            try:
                payload = _load_json_file(run_json)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        entry = {
            "source": str(source_name),
            "source_path": str(source_path) if source_path is not None else "",
            "sorting_analyzer_folder": str(curated_folder),
            "unit_count": unit_count,
            "validation_report": str(validation_path) if validation_path is not None else "",
            "unit_summary_folder": str(unit_summary_folder) if unit_summary_folder is not None else "",
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        }
        # Keep one active curated result per run.  The source is kept only as
        # metadata in the single entry, not as a separate output branch.
        payload["curations"] = [entry]
        payload["last_curation"] = entry
        payload["curated_sorting_analyzer_folder"] = str(curated_folder)
        payload["curated_unit_count"] = unit_count
        if validation_path is not None:
            payload["curated_validation_report"] = str(validation_path)
        if unit_summary_folder is not None:
            payload["curated_unit_summary_folder"] = str(unit_summary_folder)
        try:
            with open(run_json, "w", encoding="utf-8") as f:
                _pp_json_dump(_json_safe(payload), f, indent=2, ensure_ascii=False)
            _pp_compact(curated_folder)
        except Exception:
            self._append_log("WARNING: could not update sorting_run.json with curation metadata:")
            self._append_log(traceback.format_exc())

    def _analyzer_sorting(self, analyzer):
        try:
            return getattr(analyzer, "sorting", None)
        except Exception:
            return None

    def _is_sorting_analyzer_like(self, obj):
        if obj is None:
            return False
        if hasattr(obj, "get_unit_ids"):
            return False
        return self._analyzer_sorting(obj) is not None

    def _load_sorting_analyzer_from_folder(self, folder):
        try:
            from spikeinterface.core import load_sorting_analyzer
        except Exception:
            try:
                from spikeinterface.full import load_sorting_analyzer
            except Exception:
                load_sorting_analyzer = None
        if load_sorting_analyzer is None:
            return None
        try:
            return _pp_external_call(load_sorting_analyzer, folder, str(folder))
        except Exception:
            return None

    def _load_original_analyzer_for_info(self, info):
        """Load the original SortingAnalyzer for a row without depending on the selected table row."""
        if not isinstance(info, dict):
            return None
        folder = None
        try:
            folder = Path(info.get("analyzer_folder"))
        except Exception:
            folder = None
        if folder is None or not folder.exists():
            return None
        try:
            if self.current_analyzer is not None and self.current_analyzer_folder is not None and Path(self.current_analyzer_folder) == folder:
                return self.current_analyzer
        except Exception:
            pass
        analyzer = self._load_sorting_analyzer_from_folder(folder)
        if analyzer is not None:
            self.current_analyzer = analyzer
            self.current_analyzer_folder = folder
        return analyzer

    def _load_analyzer_for_info_variant(self, info, variant=None):
        """Load the analyzer selected for curation.

        When the user opens SpikeInterface GUI on the curated analyzer, the saved
        curation JSON is relative to that curated analyzer, not to the original
        sorting.  Applying it against the original sorting can fail because merged
        unit IDs already exist only in the curated analyzer.  This helper loads the
        exact analyzer variant that was opened.
        """
        if not isinstance(info, dict):
            return None, None
        if variant is None:
            analyzer = self._load_original_analyzer_for_info(info)
            folder = None
            try:
                folder = Path(info.get("analyzer_folder"))
            except Exception:
                folder = None
            return analyzer, folder

        try:
            folder = Path(variant.get("folder"))
        except Exception:
            folder = None
        if folder is None or not folder.exists():
            return None, folder
        try:
            if self.current_analyzer is not None and self.current_analyzer_folder is not None and Path(self.current_analyzer_folder) == folder:
                return self.current_analyzer, folder
        except Exception:
            pass
        analyzer = self._load_sorting_analyzer_from_folder(folder)
        if analyzer is not None:
            self.current_analyzer = analyzer
            self.current_analyzer_folder = folder
        return analyzer, folder

    def _load_unit_summary_config_for_info(self, info):
        cfg = {}
        try:
            p = Path(info.get("unit_summary_config_path"))
            if p.exists():
                cfg = _load_json_file(p)
        except Exception:
            cfg = {}
        if not isinstance(cfg, dict) or not cfg:
            try:
                payload = self._run_payload_for_row(info)
                maybe = payload.get("unit_summary_config", {}) if isinstance(payload, dict) else {}
                cfg = maybe if isinstance(maybe, dict) else {}
            except Exception:
                cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    def _load_event_frames_from_map(self, path):
        try:
            payload = _load_json_file(Path(path))
            frames = payload.get("event_frames", []) if isinstance(payload, dict) else []
            arr = np.asarray(frames, dtype=np.int64).reshape(-1)
            return arr if arr.size else None
        except Exception:
            return None

    def _load_event_intervals_from_map(self, path):
        try:
            payload = _load_json_file(Path(path))
            intervals = payload.get("event_intervals", []) if isinstance(payload, dict) else []
            arr = np.asarray(intervals, dtype=np.int64).reshape(-1, 2)
            return arr if arr.size else None
        except Exception:
            return None

    def _curated_summary_plot_events_for_info(self, info, cfg):
        output_folder = Path(info.get("output_folder"))
        out = []
        raw_plot_events = cfg.get("plot_events", []) if isinstance(cfg, dict) else []
        if not isinstance(raw_plot_events, list):
            return out
        for idx, event_cfg in enumerate(raw_plot_events, start=1):
            if not isinstance(event_cfg, dict) or not bool(event_cfg.get("enabled", True)):
                continue
            marker = str(event_cfg.get("event_marker", "") or "").strip()
            if not marker:
                continue
            display = str(event_cfg.get("display", event_cfg.get("edge", "duration")) or "duration").strip().lower()
            if display not in {"duration", "start", "stop", "center"}:
                display = "duration"
            color = str(event_cfg.get("color", "#d62728") or "#d62728").strip()
            try:
                qcolor = QtGui.QColor(color)
                color = qcolor.name() if qcolor.isValid() else "#d62728"
            except Exception:
                color = "#d62728"
            try:
                alpha = float(event_cfg.get("alpha", 0.18))
                if not math.isfinite(alpha):
                    alpha = 0.18
                alpha = max(0.0, min(1.0, alpha))
            except Exception:
                alpha = 0.18
            safe_name = _safe_folder_name(f"{idx}_{marker}_{display}")
            map_path = output_folder / f"summary_plot_event_{safe_name}_map.json"
            if display == "duration":
                intervals = self._load_event_intervals_from_map(map_path)
                if intervals is not None:
                    out.append({
                        "label": marker,
                        "event_marker": marker,
                        "display": "duration",
                        "color": color,
                        "alpha": alpha,
                        "event_intervals": intervals,
                        "map_path": str(map_path),
                    })
                    continue
                # The original generator falls back from duration to start lines if there are no spans.
                display_for_line = "start"
            else:
                display_for_line = display
            frames = self._load_event_frames_from_map(map_path)
            if frames is not None:
                out.append({
                    "label": marker,
                    "event_marker": marker,
                    "display": display_for_line,
                    "color": color,
                    "alpha": alpha,
                    "event_frames": frames,
                    "map_path": str(map_path),
                })
        return out

    def _generate_curated_unit_summary(self, curated_analyzer, info, source_name):
        if curated_analyzer is None or not isinstance(info, dict):
            return None
        cfg = self._load_unit_summary_config_for_info(info)
        if cfg and not bool(cfg.get("enabled", True)):
            self._append_log("Curated unit summary generation is disabled by this run's unit-summary config.")
            return None
        output_folder = Path(info.get("output_folder"))
        summary_folder = output_folder / "curated_unit_summary"
        if summary_folder.exists():
            shutil.rmtree(summary_folder)
        summary_folder.mkdir(parents=True, exist_ok=True)

        marker = str(cfg.get("event_marker", "") or "").strip()
        event_frames = None
        map_path = output_folder / "summary_event_alignment_map.json"
        if map_path.exists():
            # Re-use the exact alignment map created during the original unit-summary
            # generation.  This keeps curated summaries aligned to the same events as
            # the uncurated summaries.
            try:
                map_payload = _load_json_file(map_path)
                if not marker and isinstance(map_payload, dict):
                    marker = str(map_payload.get("event_marker", "") or "").strip()
            except Exception:
                pass
            event_frames = self._load_event_frames_from_map(map_path)
            if event_frames is not None:
                self._append_log(f"Curated unit summary will reuse {len(event_frames)} alignment event(s): {map_path}")
            elif marker:
                self._append_log(f"WARNING: alignment map exists but contains no usable event frames: {map_path}")
        elif marker:
            self._append_log(f"WARNING: could not find original summary alignment map, so curated summary cannot draw alignment events: {map_path}")

        plot_events = self._curated_summary_plot_events_for_info(info, cfg)
        if plot_events:
            self._append_log(f"Curated unit summary will reuse {len(plot_events)} additional plot-event overlay(s).")
        try:
            from Tools.General.plotting.plot_unit_summary import plot_sorting_summary
        except Exception as exc:
            self._append_log(f"Could not import plot_sorting_summary for curated units: {exc}")
            return str(summary_folder)

        kwargs = {
            "save_folder": str(summary_folder),
            "event_frames": event_frames,
            "pad_before_s": float(cfg.get("before_s", 0.0) if cfg.get("before_s", None) is not None else 0.0),
            "pad_after_s": float(cfg.get("after_s", 2.0) if cfg.get("after_s", None) is not None else 2.0),
            "event_label": marker or None,
        }
        if plot_events:
            kwargs["plot_events"] = plot_events
        job_kwargs = _spikeinterface_job_kwargs(_launcher_multiprocessing_enabled(), _launcher_n_jobs())
        if job_kwargs:
            kwargs["job_kwargs"] = job_kwargs
            # Keep the existing misspelled keyword because plot_unit_summary.py uses
            # it historically.  It is filtered out below if the installed version
            # does not support it.
            kwargs["acelerate"] = True

        # Filter kwargs against the installed plot_sorting_summary() signature.
        # Older local versions did not accept job_kwargs/plot_events.  The old
        # fallback regenerated the summary with *only* save_folder, which removed
        # event alignment from curated summaries.  This keeps every supported event
        # argument and removes only the unsupported keywords.
        removed_kwargs = []
        try:
            import inspect
            sig = inspect.signature(plot_sorting_summary)
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if not accepts_kwargs:
                for key in list(kwargs.keys()):
                    if key not in sig.parameters:
                        removed_kwargs.append(key)
                        kwargs.pop(key, None)
        except Exception:
            pass
        if removed_kwargs:
            self._append_log(f"plot_sorting_summary does not support these keyword(s); removed: {', '.join(removed_kwargs)}")
            if "event_frames" in removed_kwargs or "plot_events" in removed_kwargs:
                self._append_log("WARNING: the installed plot_unit_summary.py is too old to draw all curated-summary events. Replace it with V10 or newer.")

        self._append_log(f"Generating curated unit summaries: {summary_folder}")
        if job_kwargs:
            self._append_log(f"Curated summary multiprocessing enabled where supported: {job_kwargs}")
        stream = _QtAppendLogStream(self._append_log)
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            with _suppress_matplotlib_windows_for_worker():
                try:
                    plot_sorting_summary(curated_analyzer, str(info.get("sorter", "")), **kwargs)
                except TypeError as exc:
                    # Last compatibility guard: if an unexpected-keyword TypeError
                    # still gets through, remove only that keyword and retry while
                    # preserving event alignment whenever possible.
                    text = str(exc)
                    if "unexpected keyword argument" not in text:
                        raise
                    retry_kwargs = dict(kwargs)
                    changed = False
                    for key in ("job_kwargs", "n_jobs", "acelerate", "plot_events", "event_frames", "pad_before_s", "pad_after_s", "event_label"):
                        if key in retry_kwargs and key in text:
                            retry_kwargs.pop(key, None)
                            changed = True
                            self._append_log(f"Retrying curated summary without unsupported keyword: {key}")
                            break
                    if not changed:
                        # Prefer dropping multiprocessing-only arguments first.
                        for key in ("job_kwargs", "n_jobs", "acelerate"):
                            if key in retry_kwargs:
                                retry_kwargs.pop(key, None)
                                changed = True
                                self._append_log(f"Retrying curated summary without optional keyword: {key}")
                                break
                    if changed:
                        plot_sorting_summary(curated_analyzer, str(info.get("sorter", "")), **retry_kwargs)
                    else:
                        raise
        try:
            stream.flush()
        except Exception:
            pass
        self._append_log(f"Curated unit summary folder: {summary_folder}")
        return str(summary_folder)

    def _save_curated_analyzer_direct(self, curated_analyzer, curated_folder):
        """Try to save a SortingAnalyzer returned by SpikeInterface apply_curation()."""
        if not self._is_sorting_analyzer_like(curated_analyzer):
            return None
        save_as = getattr(curated_analyzer, "save_as", None)
        if not callable(save_as):
            return None
        job_kwargs = _spikeinterface_job_kwargs(_launcher_multiprocessing_enabled(), _launcher_n_jobs())
        attempts = [
            # Some SpikeInterface versions do not support overwrite= on SortingAnalyzer.save_as().
            # _confirm_overwrite_folder() already deleted the destination when the user agreed.
            lambda: save_as(folder=str(curated_folder), format="binary_folder", **job_kwargs),
            lambda: save_as(format="binary_folder", folder=str(curated_folder), **job_kwargs),
            lambda: save_as(folder=str(curated_folder), **job_kwargs),
            lambda: save_as(str(curated_folder), format="binary_folder", **job_kwargs),
            lambda: save_as(str(curated_folder), **job_kwargs),
            lambda: save_as(folder=str(curated_folder), format="binary_folder"),
            lambda: save_as(format="binary_folder", folder=str(curated_folder)),
            lambda: save_as(folder=str(curated_folder)),
            lambda: save_as(str(curated_folder), format="binary_folder"),
            lambda: save_as(str(curated_folder)),
            # Keep overwrite variants as a last fallback for versions that require/support it.
            lambda: save_as(folder=str(curated_folder), format="binary_folder", overwrite=True, **job_kwargs),
            lambda: save_as(format="binary_folder", folder=str(curated_folder), overwrite=True, **job_kwargs),
            lambda: save_as(folder=str(curated_folder), overwrite=True, **job_kwargs),
            lambda: save_as(str(curated_folder), format="binary_folder", overwrite=True, **job_kwargs),
            lambda: save_as(str(curated_folder), overwrite=True, **job_kwargs),
            lambda: save_as(folder=str(curated_folder), format="binary_folder", overwrite=True),
            lambda: save_as(format="binary_folder", folder=str(curated_folder), overwrite=True),
            lambda: save_as(folder=str(curated_folder), overwrite=True),
            lambda: save_as(str(curated_folder), format="binary_folder", overwrite=True),
            lambda: save_as(str(curated_folder), overwrite=True),
        ]
        last_exc = None
        for attempt in attempts:
            try:
                saved = attempt()
                if saved is not None and self._is_sorting_analyzer_like(saved):
                    return saved
                loaded = self._load_sorting_analyzer_from_folder(curated_folder)
                if loaded is not None:
                    return loaded
                return curated_analyzer
            except TypeError as exc:
                last_exc = exc
                continue
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            self._append_log(f"Direct curated analyzer save was not usable: {last_exc}")
        return None

    def _safe_unit_id_text_list(self, sorting_or_analyzer):
        sorting = self._analyzer_sorting(sorting_or_analyzer) if self._is_sorting_analyzer_like(sorting_or_analyzer) else sorting_or_analyzer
        if sorting is None:
            return []
        try:
            return [str(u) for u in list(sorting.get_unit_ids())]
        except Exception:
            return []

    def _extract_curation_action_summary(self, payload):
        summary = {
            "removed_units": [],
            "merge_unit_groups": [],
            "split_units": [],
            "manual_labels": {},
            "unknown_action_keys": [],
        }
        if not isinstance(payload, dict):
            return summary

        def _as_text_list(value):
            if isinstance(value, (list, tuple, set)):
                return [str(v) for v in value]
            if value is None:
                return []
            return [str(value)]

        for key in ("removed_units", "excluded_units", "deleted_units", "units_to_remove", "rejected_units", "removed"):
            value = payload.get(key)
            if isinstance(value, (list, tuple, set)):
                summary["removed_units"].extend(_as_text_list(value))

        for key in ("merge_unit_groups", "merged_units", "merge_groups", "units_to_merge", "merges"):
            value = payload.get(key)
            if isinstance(value, dict):
                value = list(value.values())
            if isinstance(value, (list, tuple)):
                for group in value:
                    if isinstance(group, dict):
                        group = group.get("unit_ids", group.get("units", group.get("merge_unit_ids", [])))
                    if isinstance(group, (list, tuple, set)):
                        group_text = _as_text_list(group)
                        if group_text:
                            summary["merge_unit_groups"].append(group_text)

        for key in ("split_units", "split_unit_ids", "unit_splits", "split_unit_groups", "splits"):
            value = payload.get(key)
            if isinstance(value, dict):
                summary["split_units"].extend(str(k) for k in value.keys())
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    if isinstance(item, dict):
                        uid = item.get("unit_id", item.get("unit", item.get("parent_unit_id", item.get("cluster_id"))))
                        if uid is not None:
                            summary["split_units"].append(str(uid))
                    else:
                        summary["split_units"].extend(_as_text_list(item))

        labels = payload.get("manual_labels") or payload.get("labels") or payload.get("unit_labels") or payload.get("labels_by_unit")
        if isinstance(labels, dict):
            summary["manual_labels"] = {str(k): str(v) for k, v in labels.items()}
        elif isinstance(labels, list):
            out = {}
            for row in labels:
                if isinstance(row, dict):
                    uid = row.get("unit_id", row.get("unit", row.get("id", row.get("cluster_id"))))
                    label = row.get("label", row.get("group", row.get("quality", None)))
                    if uid is not None and label is not None:
                        out[str(uid)] = str(label)
            summary["manual_labels"] = out

        known = {
            "removed_units", "excluded_units", "deleted_units", "units_to_remove", "rejected_units", "removed",
            "merge_unit_groups", "merged_units", "merge_groups", "units_to_merge", "merges",
            "split_units", "split_unit_ids", "unit_splits", "split_unit_groups", "splits",
            "manual_labels", "labels", "unit_labels", "labels_by_unit", "label_definitions",
        }
        action_words = ("remove", "delete", "reject", "merge", "split", "label", "accept", "noise")
        for key in payload.keys():
            key_text = str(key)
            if key_text not in known and any(word in key_text.lower() for word in action_words):
                summary["unknown_action_keys"].append(key_text)
        # Stable and unique.
        summary["removed_units"] = sorted(set(summary["removed_units"]), key=lambda x: x)
        summary["split_units"] = sorted(set(summary["split_units"]), key=lambda x: x)
        return summary

    def _validate_curated_analyzer(self, original_analyzer, curated_analyzer, curated_folder, source_name, source_path=None, curation_payload=None):
        report = {
            "version": 1,
            "source": str(source_name),
            "source_path": str(source_path) if source_path is not None else "",
            "validated_at": datetime.now().isoformat(timespec="seconds"),
            "curated_folder": str(curated_folder),
            "original_unit_ids": [],
            "curated_unit_ids": [],
            "saved_unit_ids": [],
            "action_summary": self._extract_curation_action_summary(curation_payload),
            "checks": [],
            "warnings": [],
            "status": "ok",
        }

        original_sorting = self._analyzer_sorting(original_analyzer)
        curated_sorting = self._analyzer_sorting(curated_analyzer) if self._is_sorting_analyzer_like(curated_analyzer) else curated_analyzer
        report["original_unit_ids"] = self._safe_unit_id_text_list(original_sorting)
        report["curated_unit_ids"] = self._safe_unit_id_text_list(curated_sorting)

        def _check(name, ok, detail=""):
            report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
            if not ok:
                report["warnings"].append(f"{name}: {detail}")

        _check("curated_folder_exists", Path(curated_folder).exists(), curated_folder)
        _check("curated_sorting_is_not_empty", len(report["curated_unit_ids"]) > 0, f"curated units={len(report['curated_unit_ids'])}")

        saved = self._load_sorting_analyzer_from_folder(curated_folder)
        if saved is not None:
            report["saved_unit_ids"] = self._safe_unit_id_text_list(saved)
            _check(
                "saved_folder_reloads_with_same_units",
                report["saved_unit_ids"] == report["curated_unit_ids"],
                f"returned={len(report['curated_unit_ids'])}, saved={len(report['saved_unit_ids'])}",
            )
        else:
            _check("saved_folder_reloads", False, "Could not reload curated SortingAnalyzer from disk")

        # Check that curated units have spikes. Empty units usually mean an invalid split/merge import.
        zero_spike_units = []
        if curated_sorting is not None and hasattr(curated_sorting, "get_unit_spike_train"):
            for uid in report["curated_unit_ids"]:
                try:
                    # Use the real unit id, not necessarily the string representation.
                    real_uid = None
                    for candidate in list(curated_sorting.get_unit_ids()):
                        if str(candidate) == uid:
                            real_uid = candidate
                            break
                    if real_uid is None:
                        continue
                    st = curated_sorting.get_unit_spike_train(real_uid)
                    if st is None or len(st) == 0:
                        zero_spike_units.append(uid)
                except Exception:
                    pass
        _check("no_empty_curated_units", len(zero_spike_units) == 0, ", ".join(zero_spike_units[:20]))

        actions = report["action_summary"]
        removed = set(actions.get("removed_units") or [])
        if removed and not (set(report["curated_unit_ids"]) & removed):
            _check("removed_units_absent", True, f"removed={len(removed)}")
        elif removed:
            _check("removed_units_absent", False, f"still present={sorted(set(report['curated_unit_ids']) & removed)}")

        merge_groups = actions.get("merge_unit_groups") or []
        if merge_groups:
            expected_max = len(report["original_unit_ids"]) - len(removed) - sum(max(0, len(g) - 1) for g in merge_groups)
            # Splits can increase the unit count, so only make this a warning if no split action is visible.
            if not actions.get("split_units"):
                _check(
                    "unit_count_consistent_with_removed_and_merged_units",
                    len(report["curated_unit_ids"]) == expected_max,
                    f"expected={expected_max}, got={len(report['curated_unit_ids'])}",
                )

        if actions.get("unknown_action_keys"):
            report["warnings"].append(
                "Curation file contains action-like keys that this validator does not fully understand: "
                + ", ".join(actions.get("unknown_action_keys"))
            )

        if report["warnings"]:
            report["status"] = "warning"
        validation_path = Path(curated_folder) / "curation_validation.json"
        try:
            with open(validation_path, "w", encoding="utf-8") as f:
                _pp_json_dump(_json_safe(report), f, indent=2, ensure_ascii=False)
        except Exception:
            self._append_log("WARNING: could not write curation validation report:")
            self._append_log(traceback.format_exc())
            validation_path = None

        self._append_log("Curation validation:")
        self._append_log(f"  original units: {len(report['original_unit_ids'])}")
        self._append_log(f"  curated units: {len(report['curated_unit_ids'])}")
        if actions.get("removed_units"):
            self._append_log(f"  removed units in curation file: {len(actions['removed_units'])}")
        if actions.get("merge_unit_groups"):
            self._append_log(f"  merge groups in curation file: {len(actions['merge_unit_groups'])}")
        if actions.get("split_units"):
            self._append_log(f"  split units in curation file: {len(actions['split_units'])}")
        if report["warnings"]:
            self._append_log("  validation warnings:")
            for warning in report["warnings"][:12]:
                self._append_log(f"    - {warning}")
        else:
            self._append_log("  validation passed.")
        if validation_path is not None:
            self._append_log(f"  report: {validation_path}")
        return report, validation_path

    def _write_curated_sorting_analyzer(self, sorting, info, source_name, source_path=None, curated_analyzer=None, curation_payload=None, base_analyzer=None, base_folder=None):
        original_analyzer = base_analyzer if base_analyzer is not None else self._load_original_analyzer_for_info(info)
        if original_analyzer is None:
            raise RuntimeError("Could not load the source SortingAnalyzer for this run.")
        recording = self._get_analyzer_recording(original_analyzer)
        if recording is None:
            raise RuntimeError(
                "The source SortingAnalyzer did not expose its recording. "
                "Cannot create a curated SortingAnalyzer without the recording."
            )
        output_folder = Path(info.get("output_folder"))
        curated_folder = output_folder / "SorterOutput_curated"
        try:
            base_folder = Path(base_folder) if base_folder is not None else Path(getattr(self, "current_analyzer_folder", ""))
        except Exception:
            base_folder = None

        def _same_folder(a, b):
            try:
                return Path(a).resolve() == Path(b).resolve()
            except Exception:
                try:
                    return str(Path(a)) == str(Path(b))
                except Exception:
                    return False

        replacing_the_open_curated_folder = bool(base_folder is not None and curated_folder.exists() and _same_folder(base_folder, curated_folder))
        if not self._confirm_overwrite_folder(curated_folder, delete_now=not replacing_the_open_curated_folder):
            raise RuntimeError("Curated analyzer creation cancelled.")

        write_folder = curated_folder
        if replacing_the_open_curated_folder:
            write_folder = curated_folder.parent / f"{curated_folder.name}__tmp_replace"
            if write_folder.exists():
                shutil.rmtree(write_folder)
            self._append_log("Applying curation on the existing curated analyzer; writing to a temporary folder before replacing it.")

        self._append_log(f"Writing curated SortingAnalyzer: {curated_folder}")
        QtWidgets.QApplication.processEvents()
        stream = _QtAppendLogStream(self._append_log)
        saved_directly = False
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            if self._is_sorting_analyzer_like(curated_analyzer):
                self._append_log("SpikeInterface returned a curated SortingAnalyzer. Saving that analyzer directly to preserve supported curated extensions.")
                written_analyzer = self._save_curated_analyzer_direct(curated_analyzer, write_folder)
                if written_analyzer is not None:
                    saved_directly = True
                else:
                    self._append_log("Falling back to creating a fresh SortingAnalyzer from the curated sorting.")
                    written_analyzer = self._create_sorting_analyzer_compat(
                        sorting,
                        recording,
                        write_folder,
                        overwrite=True,
                    )
            else:
                written_analyzer = self._create_sorting_analyzer_compat(
                    sorting,
                    recording,
                    write_folder,
                    overwrite=True,
                )
        stream.flush()

        if replacing_the_open_curated_folder:
            try:
                self.current_analyzer = None
                self.current_analyzer_folder = None
            except Exception:
                pass
            try:
                import gc
                gc.collect()
            except Exception:
                pass
            if curated_folder.exists():
                shutil.rmtree(curated_folder)
            shutil.move(str(write_folder), str(curated_folder))
            reloaded = self._load_sorting_analyzer_from_folder(curated_folder)
            if reloaded is not None:
                written_analyzer = reloaded

        written_sorting = self._analyzer_sorting(written_analyzer) if self._is_sorting_analyzer_like(written_analyzer) else sorting
        unit_count = self._count_sorting_units(written_sorting)
        report, validation_path = self._validate_curated_analyzer(
            original_analyzer,
            written_analyzer if self._is_sorting_analyzer_like(written_analyzer) else written_sorting,
            curated_folder,
            source_name,
            source_path=source_path,
            curation_payload=curation_payload,
        )
        unit_summary_folder = None
        try:
            unit_summary_folder = self._generate_curated_unit_summary(written_analyzer, info, source_name)
        except Exception:
            self._append_log("WARNING: could not generate curated unit summary:")
            self._append_log(traceback.format_exc())
        self._update_sorting_run_with_curation(info, curated_folder, source_name, source_path, unit_count, validation_path=validation_path, unit_summary_folder=unit_summary_folder)
        self._append_log(f"Curated analyzer written: {curated_folder}")
        self._append_log(f"Curated units: {unit_count}")
        if saved_directly:
            self._append_log("Curated analyzer was saved directly from SpikeInterface apply_curation().")
        self.status_label.setText(f"Applied curation: {unit_count} curated unit(s)")
        return written_analyzer, curated_folder

    def _read_phy_curated_sorting(self, phy_folder):
        phy_folder = Path(phy_folder)
        if not (phy_folder / "params.py").exists():
            raise RuntimeError(f"No Phy params.py found in:\n{phy_folder}")
        read_phy = None
        import_errors = []
        for module_name in ("spikeinterface.extractors", "spikeinterface.full", "spikeinterface"):
            try:
                module = __import__(module_name, fromlist=["read_phy"])
                read_phy = getattr(module, "read_phy", None)
                if callable(read_phy):
                    break
            except Exception as exc:
                import_errors.append(f"{module_name}: {exc}")
        if not callable(read_phy):
            raise RuntimeError("Could not import spikeinterface read_phy().\n" + "\n".join(import_errors))

        attempts = [
            lambda: read_phy(str(phy_folder), exclude_cluster_groups=["noise"], load_all_cluster_properties=True),
            lambda: read_phy(folder_path=str(phy_folder), exclude_cluster_groups=["noise"], load_all_cluster_properties=True),
            lambda: read_phy(str(phy_folder), exclude_cluster_groups=["noise"]),
            lambda: read_phy(folder_path=str(phy_folder), exclude_cluster_groups=["noise"]),
            lambda: read_phy(str(phy_folder)),
            lambda: read_phy(folder_path=str(phy_folder)),
        ]
        last_exc = None
        for attempt in attempts:
            try:
                sorting = attempt()
                if sorting is not None:
                    return sorting
            except TypeError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("read_phy() returned no sorting object.")

    def _apply_phy_curation_for_selected(self, info):
        phy_folder = self._phy_folder_for_info(info)
        if phy_folder is None:
            raise RuntimeError("Could not determine the Phy folder for the selected run.")
        phy_folder = Path(phy_folder)
        self._append_log(f"Reading Phy curation: {phy_folder}")
        stream = _QtAppendLogStream(self._append_log)
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            sorting = self._read_phy_curated_sorting(phy_folder)
        stream.flush()
        self._append_log(f"Phy curated sorting loaded. Units: {self._count_sorting_units(sorting)}")
        phy_payload = {"phy_folder": str(phy_folder), "exclude_cluster_groups": ["noise"]}
        return self._write_curated_sorting_analyzer(sorting, info, "phy", phy_folder, curation_payload=phy_payload)


    def _spikeinterface_gui_saved_curation_path(self, info, analyzer=None):
        """Return the curation_data.json saved by spikeinterface-gui inside the analyzer folder, if present."""
        candidate_folders = []
        if analyzer is not None:
            try:
                folder = getattr(analyzer, "folder", None)
                if folder is not None:
                    candidate_folders.append(Path(folder))
            except Exception:
                pass
        for key in ("analyzer_folder", "output_folder"):
            try:
                value = info.get(key)
                if value:
                    candidate_folders.append(Path(value))
            except Exception:
                pass

        seen = set()
        for folder in candidate_folders:
            try:
                folder = Path(folder)
                key = str(folder.resolve()) if folder.exists() else str(folder)
            except Exception:
                key = str(folder)
            if key in seen:
                continue
            seen.add(key)

            # spikeinterface-gui saves binary_folder analyzer curation here.
            json_path = folder / "spikeinterface_gui" / "curation_data.json"
            if json_path.exists():
                return json_path

            # Some older/manual workflows may have copied the file in the root.
            json_path = folder / "curation_data.json"
            if json_path.exists():
                return json_path
        return None

    def _find_curation_json_candidates(self, info):
        candidates = []
        folders = []
        for key in ("output_folder", "analyzer_folder"):
            try:
                value = info.get(key)
                if not value:
                    continue
                p = Path(value)
                if p.exists() and p.is_dir():
                    folders.append(p)
            except Exception:
                pass
        seen = set()
        saved = self._spikeinterface_gui_saved_curation_path(info)
        if saved is not None:
            try:
                seen.add(str(saved.resolve()))
            except Exception:
                seen.add(str(saved))
            candidates.append(saved)
        for folder in folders:
            for pattern in ("*curat*.json", "*Curation*.json", "curation.json", "curation_data.json"):
                try:
                    for path in folder.rglob(pattern):
                        try:
                            key = str(path.resolve())
                        except Exception:
                            key = str(path)
                        if key not in seen:
                            seen.add(key)
                            candidates.append(path)
                except Exception:
                    pass
        return sorted(candidates, key=lambda p: (0 if p.name == "curation_data.json" else 1, _natural_text_key(str(p))))

    def _choose_spikeinterface_curation_json(self, info):
        candidates = self._find_curation_json_candidates(info)
        if candidates:
            msg = QtWidgets.QMessageBox(self)
            msg.setWindowTitle("SpikeInterface GUI curation")
            msg.setIcon(QtWidgets.QMessageBox.Icon.Question if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Question)
            msg.setText("Use a detected SpikeInterface curation JSON file?")
            msg.setInformativeText("\n".join(str(p) for p in candidates[:5]) + ("\n..." if len(candidates) > 5 else ""))
            use_first = msg.addButton("Use first detected", QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole)
            choose = msg.addButton("Choose file...", QtWidgets.QMessageBox.ButtonRole.ActionRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.ActionRole)
            cancel = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole)
            msg.setDefaultButton(use_first)
            msg.exec()
            if msg.clickedButton() is use_first:
                return candidates[0]
            if msg.clickedButton() is cancel:
                return None
        start_dir = str(Path(info.get("output_folder"))) if info.get("output_folder") else ""
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Choose SpikeInterface GUI curation JSON",
            start_dir,
            "JSON files (*.json);;All files (*)",
        )
        return Path(file_path) if file_path else None

    def _import_apply_curation_function(self):
        errors = []
        for module_name in (
            "spikeinterface.curation",
            "spikeinterface.curation.curation_tools",
            "spikeinterface.full",
        ):
            try:
                module = __import__(module_name, fromlist=["apply_curation"])
                func = getattr(module, "apply_curation", None)
                if callable(func):
                    return func
            except Exception as exc:
                errors.append(f"{module_name}: {exc}")
        return None

    def _manual_apply_simple_curation_json(self, sorting, curation_payload):
        """Fallback for simple label JSONs when SpikeInterface apply_curation is unavailable."""
        unit_ids = list(sorting.get_unit_ids())
        unit_id_text_to_value = {str(u): u for u in unit_ids}
        remove_text = set()
        keep_text = None

        if isinstance(curation_payload, dict):
            for key in ("removed_units", "excluded_units", "deleted_units", "noise_units", "noise_unit_ids", "units_to_remove", "rejected_units"):
                value = curation_payload.get(key)
                if isinstance(value, (list, tuple, set)):
                    remove_text.update(str(v) for v in value)

            for key in ("kept_units", "keep_units", "selected_units", "curated_unit_ids"):
                value = curation_payload.get(key)
                if isinstance(value, (list, tuple, set)):
                    keep_text = set(str(v) for v in value)
                    break

            labels = curation_payload.get("labels") or curation_payload.get("unit_labels") or curation_payload.get("labels_by_unit")
            bad_labels = {"noise", "reject", "rejected", "bad", "mua", "artifact"}
            if isinstance(labels, dict):
                for uid, label in labels.items():
                    label_text = str(label).strip().lower()
                    if label_text in bad_labels:
                        remove_text.add(str(uid))
            elif isinstance(labels, list):
                for row in labels:
                    if not isinstance(row, dict):
                        continue
                    uid = row.get("unit_id", row.get("unit", row.get("id", row.get("cluster_id"))))
                    label = row.get("label", row.get("group", row.get("quality", "")))
                    if uid is not None and str(label).strip().lower() in bad_labels:
                        remove_text.add(str(uid))

        if keep_text is not None:
            keep_ids = [u for u in unit_ids if str(u) in keep_text]
        else:
            keep_ids = [u for u in unit_ids if str(u) not in remove_text]
        if len(keep_ids) == len(unit_ids) and not remove_text and keep_text is None:
            raise RuntimeError(
                "Could not understand this curation JSON without SpikeInterface apply_curation(). "
                "The file does not contain a simple kept/removed unit list."
            )
        if not keep_ids:
            raise RuntimeError("The curation would remove all units. Cancelled.")

        if hasattr(sorting, "select_units"):
            return sorting.select_units(keep_ids)
        if hasattr(sorting, "remove_units"):
            remove_ids = [unit_id_text_to_value[t] for t in remove_text if t in unit_id_text_to_value]
            return sorting.remove_units(remove_ids)
        raise RuntimeError("This SortingExtractor does not support select_units/remove_units.")

    def _apply_spikeinterface_json_curation_for_selected(self, info, *, prefer_saved=True, curation_file=None, base_variant=None):
        analyzer, base_folder = self._load_analyzer_for_info_variant(info, base_variant)
        if analyzer is None:
            raise RuntimeError("Could not load the selected SortingAnalyzer.")

        if curation_file is not None:
            curation_file = Path(curation_file)
        if curation_file is None and prefer_saved:
            curation_file = self._spikeinterface_gui_saved_curation_path(info, analyzer=analyzer)
            if curation_file is not None:
                self._append_log(f"Using SpikeInterface GUI curation saved in analyzer: {curation_file}")

        if curation_file is None:
            curation_file = self._choose_spikeinterface_curation_json(info)
        if curation_file is None:
            raise RuntimeError("SpikeInterface curation import cancelled.")
        curation_file = Path(curation_file)
        if not curation_file.exists():
            raise RuntimeError(f"Curation JSON file does not exist:\n{curation_file}")
        self._append_log(f"Reading SpikeInterface curation JSON: {curation_file}")
        with open(curation_file, "r", encoding="utf-8") as f:
            payload = _pp_json_load(f)

        sorting = getattr(analyzer, "sorting", None)
        if sorting is None:
            raise RuntimeError("The selected SortingAnalyzer has no sorting object.")
        apply_curation = self._import_apply_curation_function()
        curated = None
        errors = []
        if callable(apply_curation):
            attempts = [
                # Current SpikeInterface API: apply_curation(sorting_or_analyzer, curation_dict_or_model, ...)
                lambda: apply_curation(analyzer, payload),
                lambda: apply_curation(sorting, payload),
                # Older/alternate keyword APIs.
                lambda: apply_curation(sorting_or_analyzer=analyzer, curation_dict_or_model=payload),
                lambda: apply_curation(sorting_or_analyzer=sorting, curation_dict_or_model=payload),
                lambda: apply_curation(sorting=sorting, curation_dict=payload),
                lambda: apply_curation(sorting, curation_dict=payload),
                lambda: apply_curation(analyzer=analyzer, curation_dict=payload),
                lambda: apply_curation(analyzer, curation_dict=payload),
            ]
            for attempt in attempts:
                try:
                    curated = attempt()
                    break
                except TypeError as exc:
                    errors.append(str(exc))
                    continue
                except Exception as exc:
                    errors.append(str(exc))
                    continue
        if curated is None:
            self._append_log("SpikeInterface apply_curation() was not usable here. Trying a simple kept/removed-unit fallback.")
            if errors:
                self._append_log("apply_curation errors:")
                for err in errors[-3:]:
                    self._append_log(f"  - {err}")
            curated = self._manual_apply_simple_curation_json(sorting, payload)

        # Some APIs return a SortingAnalyzer, others return a SortingExtractor.
        returned_analyzer = curated if self._is_sorting_analyzer_like(curated) else None
        if returned_analyzer is not None:
            curated_sorting = self._analyzer_sorting(returned_analyzer)
            self._append_log("SpikeInterface apply_curation() returned a curated SortingAnalyzer.")
        else:
            curated_sorting = curated
        if curated_sorting is None or not hasattr(curated_sorting, "get_unit_ids"):
            raise RuntimeError("The curation did not return a valid SortingExtractor.")
        self._append_log(f"SpikeInterface curated sorting loaded. Units: {self._count_sorting_units(curated_sorting)}")
        source_name = "spikeinterface_gui"
        return self._write_curated_sorting_analyzer(
            curated_sorting,
            info,
            source_name,
            curation_file,
            curated_analyzer=returned_analyzer,
            curation_payload=payload,
            base_analyzer=analyzer,
            base_folder=base_folder,
        )

    def _confirm_apply_curation_after_close(self, info, source_name, *, curation_file=None, base_variant=None):
        if not isinstance(info, dict):
            return
        if info.get("sorting_failed") or info.get("sorting_in_progress"):
            return
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Apply curation")
        msg.setIcon(QtWidgets.QMessageBox.Icon.Question if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Question)
        msg.setText("Apply this curation to the sorting run?")
        msg.setInformativeText(
            f"Recording: {info.get('display_recording', info.get('recording', ''))}\n"
            f"Sorter: {info.get('sorter', '')}\n"
            f"Preprocessing: {info.get('preprocessing_pipeline_id', 'Pipeline ?')}\n"
            f"Sorter params: {info.get('sorter_params_id', 'Pipeline ?')}\n\n"
            "If a curated SortingAnalyzer already exists, you will be asked whether to overwrite it."
        )
        role = QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole
        reject_role = QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole
        yes_btn = msg.addButton("Apply curation", role)
        no_btn = msg.addButton("Do not apply", reject_role)
        msg.setDefaultButton(yes_btn)
        msg.exec()
        if msg.clickedButton() is not yes_btn:
            self._append_log("Curation window closed. Curation was not applied.")
            return
        self._apply_curation_for_info(info, source_name, curation_file=curation_file, base_variant=base_variant)

    def _apply_curation_for_info(self, info, source_name, *, curation_file=None, base_variant=None):
        self._append_log("")
        self._append_log("Applying curation...")
        self._set_busy(True, "Curation: applying curation...")
        QtWidgets.QApplication.processEvents()
        try:
            if str(source_name).lower() == "phy":
                self._apply_phy_curation_for_selected(info)
            else:
                self._apply_spikeinterface_json_curation_for_selected(info, prefer_saved=True, curation_file=curation_file, base_variant=base_variant)
            self._append_log("Curation applied successfully.")
            self.refresh_outputs()
        except Exception:
            self._append_log("ERROR while applying curation:")
            self._append_log(traceback.format_exc())
            QtWidgets.QMessageBox.warning(
                self,
                "Apply curation",
                "Could not apply curation. See the curation log for details.",
            )
        finally:
            self._set_busy(False, "Curation ready.")
    def _schedule_apply_prompt_after_close(self, info, source_name, *, curation_file=None, base_variant=None):
        info_copy = dict(info or {})
        curation_file_text = str(curation_file) if curation_file is not None else None
        base_variant_copy = dict(base_variant or {}) if isinstance(base_variant, dict) else None
        if isinstance(base_variant_copy, dict) and base_variant_copy.get("folder") is not None:
            base_variant_copy["folder"] = str(base_variant_copy.get("folder"))
        def _ask():
            self._confirm_apply_curation_after_close(
                info_copy,
                source_name,
                curation_file=Path(curation_file_text) if curation_file_text else None,
                base_variant=base_variant_copy,
            )
        QtCore.QTimer.singleShot(250, _ask)

    def _monitor_phy_process_for_apply_prompt(self, proc, info):
        def _poll():
            try:
                done = proc.poll() is not None
            except Exception:
                done = True
            if not done:
                QtCore.QTimer.singleShot(1500, _poll)
                return
            try:
                if proc in self._phy_processes:
                    self._phy_processes.remove(proc)
            except Exception:
                pass
            self._append_log("Phy process closed.")
            self._schedule_apply_prompt_after_close(info, "phy")
        QtCore.QTimer.singleShot(1500, _poll)


    def show_selected_export_dialog(self):
        """Open export options for selected sorter runs with per-run export/version choices."""
        selected_infos = self._selected_row_infos()
        if not selected_infos:
            return

        completed_infos = []
        skipped_status = []
        for info in selected_infos:
            if info.get("sorting_in_progress"):
                skipped_status.append((info, "running"))
            elif info.get("sorting_failed"):
                skipped_status.append((info, "failed"))
            else:
                completed_infos.append(info)

        if not completed_infos:
            QtWidgets.QMessageBox.information(
                self,
                "Export",
                "No selected row has a completed readable SortingAnalyzer to export.",
            )
            return

        # Keep only rows where at least one exportable variant exists.
        exportable_infos = []
        skipped_missing_variant = []
        for info in completed_infos:
            if (
                self._variant_for_export_kind(info, "original") is not None
                or self._variant_for_export_kind(info, "curated") is not None
            ):
                exportable_infos.append(info)
            else:
                skipped_missing_variant.append(info)

        if not exportable_infos:
            QtWidgets.QMessageBox.warning(
                self,
                "Export",
                "No readable uncurated or curated SortingAnalyzer was found for the selected completed row(s).",
            )
            return

        def _recording_key(info):
            return str(info.get("display_recording", info.get("recording", "")) or "recording").strip() or "recording"

        def _run_timestamp(info):
            text = str(info.get("run_date", "") or "").strip()
            if text:
                candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
                try:
                    return float(datetime.fromisoformat(candidate).timestamp())
                except Exception:
                    pass
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m-%d-%Y"):
                    try:
                        return float(datetime.strptime(text.split(".")[0], fmt).timestamp())
                    except Exception:
                        pass
            try:
                folder = Path(info.get("output_folder"))
                if folder.exists():
                    return float(folder.stat().st_mtime)
            except Exception:
                pass
            return 0.0

        latest_row_by_recording = {}
        for row_idx, info in enumerate(exportable_infos):
            key = _recording_key(info)
            stamp = _run_timestamp(info)
            previous = latest_row_by_recording.get(key)
            if previous is None or stamp > previous[0]:
                latest_row_by_recording[key] = (stamp, row_idx)

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Export spike sorting results")
        dialog.resize(980, 560)
        vbox = QtWidgets.QVBoxLayout(dialog)
        vbox.setContentsMargins(12, 12, 12, 12)
        vbox.setSpacing(8)

        title = QtWidgets.QLabel("Export spike sorting results", dialog)
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        vbox.addWidget(title)

        info_text = f"Rows selected: {len(selected_infos)} total, {len(exportable_infos)} exportable completed run(s)."
        if skipped_status:
            info_text += f" {len(skipped_status)} failed/running row(s) will be skipped."
        if skipped_missing_variant:
            info_text += f" {len(skipped_missing_variant)} completed row(s) had no readable variant and will be skipped."
        info_label = QtWidgets.QLabel(info_text, dialog)
        info_label.setWordWrap(True)
        vbox.addWidget(info_label)

        run_table = QtWidgets.QTableWidget(dialog)
        run_table.setColumnCount(6)
        run_table.setHorizontalHeaderLabels([
            "Export",
            "Recording",
            "Pipeline preproc",
            "Pipeline sorter",
            "Uncurated",
            "Curated",
        ])
        run_table.setRowCount(len(exportable_infos))
        run_table.setAlternatingRowColors(True)
        run_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior") else QtWidgets.QAbstractItemView.SelectRows)
        run_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection if hasattr(QtWidgets.QAbstractItemView, "SelectionMode") else QtWidgets.QAbstractItemView.SingleSelection)
        run_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers if hasattr(QtWidgets.QAbstractItemView, "EditTrigger") else QtWidgets.QAbstractItemView.NoEditTriggers)
        try:
            run_table.verticalHeader().setVisible(False)
        except Exception:
            pass
        try:
            run_table.setMinimumHeight(250)
        except Exception:
            pass

        def _center_widget(widget):
            holder = QtWidgets.QWidget(run_table)
            layout = QtWidgets.QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addStretch(1)
            layout.addWidget(widget)
            layout.addStretch(1)
            return holder

        export_radios_by_recording = {}
        row_controls = []

        def _wire_optional_exclusive_export_radio(radio, recording_key):
            """Allow zero or one exported run per recording.

            Qt radio groups normally force one checked button. Here, clicking an
            already checked export radio clears that recording completely, while
            clicking another run for the same recording keeps the mutual
            exclusion behavior.
            """
            try:
                radio.setAutoExclusive(False)
            except Exception:
                pass

            def _remember_checked_state(*_args, _radio=radio):
                try:
                    _radio._was_checked_before_press = bool(_radio.isChecked())
                except Exception:
                    pass

            def _clicked(*_args, _radio=radio, _recording_key=recording_key):
                if bool(getattr(dialog, "_updating_export_radios", False)):
                    return
                was_checked = bool(getattr(_radio, "_was_checked_before_press", False))
                try:
                    dialog._updating_export_radios = True
                    if was_checked:
                        _radio.setChecked(False)
                    elif _radio.isChecked():
                        for other in export_radios_by_recording.get(_recording_key, []):
                            if other is not _radio:
                                other.setChecked(False)
                        _radio.setChecked(True)
                finally:
                    try:
                        dialog._updating_export_radios = False
                    except Exception:
                        pass

            try:
                radio.pressed.connect(_remember_checked_state)
                radio.clicked.connect(_clicked)
            except Exception:
                pass

        for row, info in enumerate(exportable_infos):
            uncurated_variant = self._variant_for_export_kind(info, "original")
            curated_variant = self._variant_for_export_kind(info, "curated")
            recording = _recording_key(info)
            run_name = str(info.get("run_name", "") or "")
            run_date = str(info.get("run_date", "") or "")
            sorter_name = str(info.get("sorter", "") or "")
            preproc_label = str(info.get("preprocessing_pipeline_id", "") or "")
            sorter_label = str(info.get("sorter_params_id", "") or "")
            folder_text = str(info.get("output_folder", "") or "")
            tooltip = (
                f"Recording: {recording}\n"
                f"Run: {run_name}\n"
                f"Run date: {run_date}\n"
                f"Sorter: {sorter_name}\n"
                f"Folder: {folder_text}"
            )

            export_radio = QtWidgets.QRadioButton(run_table)
            export_radios_by_recording.setdefault(recording, []).append(export_radio)
            _wire_optional_exclusive_export_radio(export_radio, recording)
            should_export = latest_row_by_recording.get(recording, (None, None))[1] == row
            export_radio.setChecked(bool(should_export))
            try:
                export_radio.setToolTip(
                    "Run exported for this recording. Only one selected run per recording is exported by default. "
                    "Click the checked radio again to export no run for this recording."
                )
            except Exception:
                pass
            run_table.setCellWidget(row, 0, _center_widget(export_radio))

            for col, value in (
                (1, recording),
                (2, preproc_label),
                (3, f"{sorter_name} / {sorter_label}" if sorter_name else sorter_label),
            ):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(tooltip)
                run_table.setItem(row, col, item)

            variant_group = QtWidgets.QButtonGroup(dialog)
            variant_group.setExclusive(True)
            uncurated_radio = QtWidgets.QRadioButton(run_table)
            curated_radio = QtWidgets.QRadioButton(run_table)
            variant_group.addButton(uncurated_radio)
            variant_group.addButton(curated_radio)
            uncurated_radio.setEnabled(uncurated_variant is not None)
            curated_radio.setEnabled(curated_variant is not None)
            if curated_variant is not None:
                curated_radio.setChecked(True)
            elif uncurated_variant is not None:
                uncurated_radio.setChecked(True)
            try:
                uncurated_radio.setToolTip("Export the uncurated SortingAnalyzer for this run.")
                curated_radio.setToolTip("Export the curated SortingAnalyzer for this run.")
            except Exception:
                pass
            run_table.setCellWidget(row, 4, _center_widget(uncurated_radio))
            run_table.setCellWidget(row, 5, _center_widget(curated_radio))
            row_controls.append({
                "info": info,
                "export_radio": export_radio,
                "uncurated_radio": uncurated_radio,
                "curated_radio": curated_radio,
                "uncurated_variant": uncurated_variant,
                "curated_variant": curated_variant,
            })

        try:
            run_table.resizeColumnsToContents()
            run_table.setColumnWidth(0, 80)
            run_table.setColumnWidth(1, max(150, min(260, run_table.columnWidth(1))))
            run_table.setColumnWidth(2, max(130, min(210, run_table.columnWidth(2))))
            run_table.setColumnWidth(3, max(160, min(260, run_table.columnWidth(3))))
            run_table.setColumnWidth(4, 95)
            run_table.setColumnWidth(5, 85)
            run_table.horizontalHeader().setStretchLastSection(False)
        except Exception:
            pass
        vbox.addWidget(run_table, 1)

        note = QtWidgets.QLabel(
            "Default selection: for each recording, the newest selected run is checked for export. "
            "Only one run can be checked per recording, but clicking the checked export radio again clears that recording. "
            "Curated is selected when it exists, otherwise uncurated is selected. "
            "The same export actions are applied to every checked row.",
            dialog,
        )
        note.setWordWrap(True)
        vbox.addWidget(note)

        actions_box = QtWidgets.QGroupBox("Export actions, shared by all checked rows", dialog)
        actions_layout = QtWidgets.QVBoxLayout(actions_box)
        spike_times_cb = QtWidgets.QCheckBox("Spike times as Excel", actions_box)
        templates_cb = QtWidgets.QCheckBox("Waveform templates as Excel", actions_box)
        nwb_units_cb = QtWidgets.QCheckBox("Create Units in the respective source NWB file(s)", actions_box)
        unit_label_edit = QtWidgets.QLineEdit(actions_box)
        unit_label_edit.setText(self._sorting_project_name_for_export(exportable_infos[0] if exportable_infos else None))
        unit_label_edit.setPlaceholderText("Units label, stored in /units/sorting_project")
        unit_label_edit.setToolTip(
            "This label is written in the NWB /units sorting_project column and is used by analysis selection. "
            "By default it is the Spike_sorting project folder name."
        )
        nwb_units_row = QtWidgets.QHBoxLayout()
        nwb_units_row.addWidget(nwb_units_cb)
        nwb_units_row.addWidget(QtWidgets.QLabel("Units label", actions_box))
        nwb_units_row.addWidget(unit_label_edit, 1)
        replace_same_label_units_cb = QtWidgets.QCheckBox("Replace existing NWB units with same label", actions_box)
        replace_existing_units_cb = QtWidgets.QCheckBox("Remove all existing NWB units before writing", actions_box)
        spike_times_cb.setChecked(True)
        templates_cb.setChecked(True)
        nwb_units_cb.setChecked(True)
        replace_same_label_units_cb.setChecked(True)
        replace_existing_units_cb.setChecked(False)
        replace_same_label_units_cb.setToolTip(
            "When checked, only existing rows in /units with the same sorting_project label are removed before writing. "
            "Units with another label are kept."
        )
        replace_existing_units_cb.setToolTip("When checked, the complete existing NWB Units table is removed first, then the selected exported units are written. This overrides the same-label replacement option. When unchecked, exported units are appended unless same-label replacement is checked.")
        actions_layout.addWidget(spike_times_cb)
        actions_layout.addWidget(templates_cb)
        actions_layout.addLayout(nwb_units_row)
        actions_layout.addWidget(replace_same_label_units_cb)
        actions_layout.addWidget(replace_existing_units_cb)
        vbox.addWidget(actions_box)

        def _update_nwb_units_option_state():
            enabled = bool(nwb_units_cb.isChecked())
            unit_label_edit.setEnabled(enabled)
            replace_same_label_units_cb.setEnabled(enabled and not bool(replace_existing_units_cb.isChecked()))
            replace_existing_units_cb.setEnabled(enabled)

        def _update_replace_option_state():
            if replace_existing_units_cb.isChecked() and replace_same_label_units_cb.isChecked():
                replace_same_label_units_cb.setChecked(False)
            _update_nwb_units_option_state()

        nwb_units_cb.toggled.connect(_update_nwb_units_option_state)
        replace_existing_units_cb.toggled.connect(_update_replace_option_state)
        _update_nwb_units_option_state()

        folder_form = QtWidgets.QFormLayout()
        project_root_export_folder = self._project_root_for_excel_export(exportable_infos)
        save_excel_to_run_folder_cb = QtWidgets.QCheckBox("Save Excel in each run folder/export", dialog)
        save_excel_to_run_folder_cb.setChecked(True)
        save_excel_to_run_folder_cb.setToolTip(
            "When checked, Excel files are written in an export subfolder inside each selected run folder. "
            "When unchecked, all Excel files are written to the folder selected here."
        )
        export_folder_edit = QtWidgets.QLineEdit(str(project_root_export_folder), dialog)
        browse_btn = QtWidgets.QPushButton("Browse", dialog)
        folder_row = QtWidgets.QHBoxLayout()
        folder_row.addWidget(save_excel_to_run_folder_cb)
        folder_row.addWidget(export_folder_edit, 1)
        folder_row.addWidget(browse_btn)
        folder_form.addRow("Excel output folder", folder_row)
        vbox.addLayout(folder_form)

        def _update_export_folder_controls():
            use_run_folder = bool(save_excel_to_run_folder_cb.isChecked())
            export_folder_edit.setEnabled(not use_run_folder)
            browse_btn.setEnabled(not use_run_folder)
            if use_run_folder:
                export_folder_edit.setText("Each selected run folder/export")
            else:
                current = str(export_folder_edit.text() or "").strip()
                if not current or current in ("Each selected run folder", "Each selected run folder/export"):
                    export_folder_edit.setText(str(project_root_export_folder))

        save_excel_to_run_folder_cb.toggled.connect(_update_export_folder_controls)
        _update_export_folder_controls()

        buttons = QtWidgets.QDialogButtonBox(dialog)
        export_btn = buttons.addButton("Export", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QDialogButtonBox, "ButtonRole") else QtWidgets.QDialogButtonBox.AcceptRole)
        cancel_btn = buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel if hasattr(QtWidgets.QDialogButtonBox, "StandardButton") else QtWidgets.QDialogButtonBox.Cancel)
        vbox.addWidget(buttons)

        def _browse():
            start_folder = str(project_root_export_folder)
            current = str(export_folder_edit.text() or "").strip()
            if current and current not in ("Each selected run folder", "Each selected run folder/export"):
                start_folder = current
            folder = QtWidgets.QFileDialog.getExistingDirectory(dialog, "Choose export folder", start_folder)
            if folder:
                export_folder_edit.setText(folder)

        def _run():
            actions = [spike_times_cb.isChecked(), templates_cb.isChecked(), nwb_units_cb.isChecked()]
            if not any(actions):
                QtWidgets.QMessageBox.information(dialog, "Export", "Select at least one export action.")
                return

            targets = []
            selected_without_variant = []
            for controls in row_controls:
                if not controls["export_radio"].isChecked():
                    continue
                info = controls["info"]
                if controls["curated_radio"].isChecked():
                    variant = controls.get("curated_variant")
                else:
                    variant = controls.get("uncurated_variant")
                if variant is None:
                    selected_without_variant.append(info)
                    continue
                targets.append((info, variant))

            if not targets:
                QtWidgets.QMessageBox.warning(dialog, "Export", "Check at least one run to export.")
                return

            save_excel_to_run_folder = bool(save_excel_to_run_folder_cb.isChecked())
            out_dir = None if save_excel_to_run_folder else Path(export_folder_edit.text()).expanduser()
            units_label = str(unit_label_edit.text() or "").strip()
            if nwb_units_cb.isChecked() and not units_label:
                QtWidgets.QMessageBox.warning(dialog, "Export", "Enter a Units label before writing Units to NWB.")
                return

            dialog.close()
            self._run_export_for_selected_infos(
                targets,
                out_dir,
                export_spike_times=spike_times_cb.isChecked(),
                export_templates=templates_cb.isChecked(),
                write_units_to_nwb=nwb_units_cb.isChecked(),
                replace_existing_units=replace_existing_units_cb.isChecked(),
                replace_same_label_units=replace_same_label_units_cb.isChecked(),
                units_sorting_project_label=units_label,
                save_excel_to_run_folder=save_excel_to_run_folder,
                skipped_status=skipped_status,
                skipped_missing_variant=list(skipped_missing_variant) + selected_without_variant,
            )

        browse_btn.clicked.connect(_browse)
        export_btn.clicked.connect(_run)
        cancel_btn.clicked.connect(dialog.close)
        self._show_modeless_param_dialog(dialog)

    def _variant_for_export_kind(self, info, wanted_kind):
        wanted_kind = "curated" if str(wanted_kind).lower() == "curated" else "original"
        for variant in self._available_analyzer_variants_for_row(info):
            kind = str((variant or {}).get("kind", "original") or "original").lower()
            if kind == wanted_kind:
                return dict(variant or {})
        return None

    def _default_export_folder_for_infos(self, infos):
        try:
            if len(infos) == 1:
                return Path(infos[0].get("output_folder", "")) / "exports"
        except Exception:
            pass
        try:
            if getattr(self, "sorting_path", None) is not None:
                return Path(self.sorting_path) / "exports"
        except Exception:
            pass
        try:
            first = Path(infos[0].get("output_folder", ""))
            return first.parent / "exports"
        except Exception:
            return Path("exports")

    def _project_root_for_excel_export(self, infos=None):
        """Return the project root used as the default manual Excel export folder."""
        try:
            gui = self.sorting_info.get("GUI_instance", None)
            if gui is not None and hasattr(gui, "project_root"):
                root = Path(gui.project_root)
                if str(root).strip():
                    return root
        except Exception:
            pass
        try:
            if getattr(self, "sorting_path", None) is not None:
                path = Path(self.sorting_path).resolve()
                # Common layout: <project>/Data/Spike_sorting/<sorting_project>
                if len(path.parents) >= 3:
                    return path.parents[2]
                return path.parent
        except Exception:
            pass
        try:
            infos = list(infos or [])
            if infos:
                first = Path(infos[0].get("output_folder", "")).resolve()
                # Common layout: <project>/Data/Spike_sorting/<sorting_project>/sorter_output/...
                parts = [part.lower() for part in first.parts]
                if "data" in parts:
                    idx = parts.index("data")
                    if idx > 0:
                        return Path(*first.parts[:idx])
                if len(first.parents) >= 5:
                    return first.parents[4]
                return first.parent
        except Exception:
            pass
        return Path.cwd()

    def _excel_output_folder_for_info(self, info, output_folder=None, *, save_excel_to_run_folder=False):
        if save_excel_to_run_folder:
            try:
                run_folder = Path((info or {}).get("output_folder", "")).expanduser()
                if str(run_folder).strip():
                    return run_folder / "export"
            except Exception:
                pass
        if output_folder is None:
            return self._project_root_for_excel_export([info] if info else [])
        return Path(output_folder).expanduser()

    def _run_export_for_selected_infos(
        self,
        targets,
        output_folder,
        *,
        export_spike_times: bool,
        export_templates: bool,
        write_units_to_nwb: bool,
        replace_existing_units: bool = True,
        replace_same_label_units: bool = False,
        units_sorting_project_label: str | None = None,
        save_excel_to_run_folder: bool = False,
        skipped_status=None,
        skipped_missing_variant=None,
    ):
        targets = list(targets or [])
        skipped_status = list(skipped_status or [])
        skipped_missing_variant = list(skipped_missing_variant or [])
        if not targets:
            return
        if len(targets) == 1 and not skipped_status and not skipped_missing_variant:
            info, variant = targets[0]
            target_output_folder = self._excel_output_folder_for_info(
                info,
                output_folder,
                save_excel_to_run_folder=save_excel_to_run_folder,
            )
            self._run_export_for_selected(
                info,
                variant,
                target_output_folder,
                export_spike_times=export_spike_times,
                export_templates=export_templates,
                write_units_to_nwb=write_units_to_nwb,
                replace_existing_units=replace_existing_units,
                replace_same_label_units=replace_same_label_units,
                units_sorting_project_label=units_sorting_project_label,
            )
            return

        self._append_log("")
        self._append_log(f"Batch export: {len(targets)} selected run(s).")
        written_paths = []
        failures = []
        for i, (info, variant) in enumerate(targets, start=1):
            label = f"{info.get('display_recording', info.get('recording', ''))} / {info.get('sorter', '')} / {info.get('run_name', '')}"
            self._append_log("")
            self._append_log(f"Batch export {i}/{len(targets)}: {label}")
            try:
                target_output_folder = self._excel_output_folder_for_info(
                    info,
                    output_folder,
                    save_excel_to_run_folder=save_excel_to_run_folder,
                )
                paths = self._run_export_for_selected(
                    info,
                    variant,
                    target_output_folder,
                    export_spike_times=export_spike_times,
                    export_templates=export_templates,
                    write_units_to_nwb=write_units_to_nwb,
                    replace_existing_units=replace_existing_units,
                    replace_same_label_units=replace_same_label_units,
                    units_sorting_project_label=units_sorting_project_label,
                    show_message=False,
                    raise_on_error=True,
                )
                written_paths.extend(paths or [])
            except Exception as exc:
                failures.append((label, exc))

        skipped_lines = []
        for info, reason in skipped_status:
            skipped_lines.append(f"{info.get('display_recording', info.get('recording', ''))} / {info.get('sorter', '')} / {info.get('run_name', '')}: {reason}")
        for info in skipped_missing_variant:
            skipped_lines.append(f"{info.get('display_recording', info.get('recording', ''))} / {info.get('sorter', '')} / {info.get('run_name', '')}: selected version not available")

        summary = [f"Batch export finished: {len(targets) - len(failures)}/{len(targets)} run(s) exported."]
        if written_paths:
            summary.append(f"Files written: {len(written_paths)}")
        if skipped_lines:
            summary.append(f"Skipped: {len(skipped_lines)}")
        if failures:
            summary.append(f"Failed: {len(failures)}")
        self.status_label.setText(" ".join(summary))
        self._append_log(" ".join(summary))

        details = []
        if written_paths:
            details.append("Written files:")
            details.extend(str(p) for p in written_paths[:12])
            if len(written_paths) > 12:
                details.append(f"... and {len(written_paths) - 12} more")
        if skipped_lines:
            details.append("")
            details.append("Skipped rows:")
            details.extend(skipped_lines[:8])
            if len(skipped_lines) > 8:
                details.append(f"... and {len(skipped_lines) - 8} more")
        if failures:
            details.append("")
            details.append("Failures:")
            details.extend(f"{label}: {exc}" for label, exc in failures[:8])
            if len(failures) > 8:
                details.append(f"... and {len(failures) - 8} more")

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Export")
        if failures:
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Warning)
        else:
            box.setIcon(QtWidgets.QMessageBox.Icon.Information if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Information)
        box.setText("Batch export finished." if not failures else "Batch export finished with errors.")
        info_lines = list(summary)
        if written_paths:
            try:
                if save_excel_to_run_folder:
                    info_lines.append("Excel output: export subfolder inside each selected run folder")
                else:
                    info_lines.append(f"Excel output folder: {Path(output_folder)}")
            except Exception:
                pass
            preview_names = [Path(p).name for p in written_paths[:5]]
            if preview_names:
                info_lines.append("")
                info_lines.append("Written files:")
                info_lines.extend(f"  {name}" for name in preview_names)
            if len(written_paths) > 5:
                info_lines.append(f"  ... and {len(written_paths) - 5} more")
        if skipped_lines:
            info_lines.append("")
            info_lines.append("Some rows were skipped. See Details for the full list.")
        if failures:
            info_lines.append("")
            info_lines.append("Some rows failed. See Details for the error list.")
        if not written_paths and not failures:
            info_lines.append("")
            info_lines.append("No Excel/JSON files were written. NWB Units may have been updated directly.")
        box.setInformativeText("\n".join(info_lines))
        full_details = []
        if written_paths:
            full_details.append("Full written paths:")
            full_details.extend(str(p) for p in written_paths)
        if skipped_lines:
            full_details.append("")
            full_details.append("Skipped rows:")
            full_details.extend(skipped_lines)
        if failures:
            full_details.append("")
            full_details.append("Failures:")
            full_details.extend(f"{label}: {exc}" for label, exc in failures)
        if full_details:
            box.setDetailedText("\n".join(full_details))
        box.exec()

    def _run_export_for_selected(
        self,
        info,
        variant,
        output_folder,
        *,
        export_spike_times: bool,
        export_templates: bool,
        write_units_to_nwb: bool,
        replace_existing_units: bool = True,
        replace_same_label_units: bool = False,
        units_sorting_project_label: str | None = None,
        show_message: bool = True,
        raise_on_error: bool = False,
    ):
        """Run the selected export actions synchronously from the Curation tab."""
        info = dict(info or {})
        variant = dict(variant or {})
        output_folder = Path(output_folder)
        variant_kind = str(variant.get("kind", "original") or "original").lower()
        variant_label = "curated" if variant_kind == "curated" else "uncurated"
        safe_recording = _safe_filename(info.get("display_recording", info.get("recording", "recording")))
        safe_sorter = _safe_filename(info.get("sorter", "sorter"))
        safe_run = _safe_filename(info.get("run_name", "run"))
        prefix = f"{safe_recording}_{safe_sorter}_{safe_run}_{variant_label}"

        self._append_log("")
        self._append_log(f"Exporting {variant_label} sorting results...")
        self._set_busy(True, f"Exporting {variant_label} sorting results...")
        QtWidgets.QApplication.processEvents()

        written_paths = []
        try:
            output_folder.mkdir(parents=True, exist_ok=True)
            analyzer, analyzer_folder = self._load_analyzer_for_info_variant(info, variant)
            if analyzer is None:
                raise RuntimeError("Could not load the selected SortingAnalyzer.")
            sorting = self._analyzer_sorting(analyzer)
            if sorting is None:
                raise RuntimeError("The selected SortingAnalyzer has no sorting object.")

            spike_df = None
            spikes_by_file = None
            mapping_summary = None
            template_tables = None

            if export_spike_times or write_units_to_nwb:
                spike_df, spikes_by_file, mapping_summary = self._build_spike_export_tables(info, sorting, variant, analyzer_folder)
                if export_spike_times:
                    spike_path = output_folder / f"{prefix}_spike_times.xlsx"
                    self._write_spike_times_excel(spike_df, spike_path, info, variant, mapping_summary or {}, analyzer_folder)
                    written_paths.append(spike_path)
                    self._append_log(f"Wrote spike-time Excel: {spike_path}")

            if export_templates:
                template_tables = self._build_template_export_tables(analyzer, sorting, variant, analyzer_folder)
                template_path = output_folder / f"{prefix}_waveform_templates.xlsx"
                self._write_templates_excel(template_tables, template_path)
                written_paths.append(template_path)
                self._append_log(f"Wrote waveform-template Excel: {template_path}")

            if export_spike_times or export_templates:
                metadata_path = output_folder / f"{prefix}_export_metadata.xlsx"
                self._write_export_metadata_excel(
                    metadata_path,
                    info,
                    variant,
                    mapping_summary or {},
                    analyzer_folder,
                    spike_df=spike_df,
                    template_tables=template_tables,
                )
                written_paths.append(metadata_path)
                self._append_log(f"Wrote export metadata Excel: {metadata_path}")

            nwb_summary = []
            if write_units_to_nwb:
                self._append_log("NWB export handle fix v3 active.")
                if spikes_by_file is None:
                    spike_df, spikes_by_file, mapping_summary = self._build_spike_export_tables(info, sorting, variant, analyzer_folder)

                # The NWB write step needs exclusive read/write access.  The GUI
                # viewer/backend and SpikeInterface analyzer/recording objects may
                # keep the source NWB open read-only, which makes h5py/PyNWB fail
                # on Windows with "file is already open for read-only".  At this
                # point all export tables are already built, so the analyzer can be
                # safely released before opening source NWB files in r+ mode.
                self._release_analyzer_before_nwb_write(analyzer)
                analyzer = None
                sorting = None

                nwb_summary = self._write_units_to_source_nwb_files(
                    info,
                    spikes_by_file or {},
                    variant,
                    analyzer_folder,
                    replace_existing=replace_existing_units,
                    replace_same_label=replace_same_label_units,
                    sorting_project_label=units_sorting_project_label,
                )
                for line in nwb_summary:
                    self._append_log(line)

            if written_paths:
                self._append_log("Export files:")
                for p in written_paths:
                    self._append_log(f"  {p}")
            self.status_label.setText(f"Export finished: {variant_label}")
            if show_message:
                box = QtWidgets.QMessageBox(self)
                box.setWindowTitle("Export")
                box.setIcon(QtWidgets.QMessageBox.Icon.Information if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Information)
                box.setText("Export finished.")
                info_lines = [
                    f"Version: {variant_label}",
                    f"Files written: {len(written_paths)}",
                    f"Export folder: {output_folder}",
                ]
                if written_paths:
                    info_lines.append("")
                    info_lines.append("Written files:")
                    for p in written_paths[:5]:
                        info_lines.append(f"  {Path(p).name}")
                    if len(written_paths) > 5:
                        info_lines.append(f"  ... and {len(written_paths) - 5} more")
                else:
                    info_lines.append("")
                    info_lines.append("No Excel/JSON files were written. NWB Units may have been updated directly.")
                info_lines.append("")
                info_lines.append("Full paths are available in Details and in the log.")
                box.setInformativeText("\n".join(info_lines))
                if written_paths:
                    box.setDetailedText("\n".join(str(p) for p in written_paths))
                box.exec()
            return written_paths
        except Exception as exc:
            self._append_log("ERROR while exporting sorting results:")
            self._append_log(traceback.format_exc())
            if show_message:
                QtWidgets.QMessageBox.warning(self, "Export", f"Could not export sorting results.\n\n{exc}")
            if raise_on_error:
                raise
            return []
        finally:
            self._set_busy(False, "Curation ready.")

    def _sorting_sampling_frequency(self, sorting):
        for getter in (
            lambda s: s.get_sampling_frequency(),
            lambda s: getattr(s, "sampling_frequency"),
        ):
            try:
                fs = float(getter(sorting))
                if np.isfinite(fs) and fs > 0:
                    return fs
            except Exception:
                pass
        return None

    def _sorting_unit_ids(self, sorting):
        try:
            return list(sorting.get_unit_ids())
        except Exception:
            try:
                return list(getattr(sorting, "unit_ids", []))
            except Exception:
                return []

    def _sorting_spike_train_frames(self, sorting, unit_id):
        calls = (
            lambda: sorting.get_unit_spike_train(unit_id=unit_id, segment_index=0),
            lambda: sorting.get_unit_spike_train(unit_id=unit_id),
            lambda: sorting.get_unit_spike_train(unit_id),
        )
        last_exc = None
        for call in calls:
            try:
                arr = call()
                return np.asarray(arr, dtype=np.int64).reshape(-1)
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Could not read spike train for unit {unit_id}: {last_exc}")

    def _load_sorting_time_map_for_export(self, info, group):
        candidate_paths = []
        try:
            output_folder = Path(info.get("output_folder"))
            candidate_paths.append(output_folder / "sorting_time_map.json")
        except Exception:
            pass
        if self.sorting_path is not None:
            candidate_paths.append(Path(self.sorting_path) / "sorting_time_map.json")

        payload = None
        map_path = None
        for candidate in candidate_paths:
            try:
                candidate = Path(candidate)
                if not candidate.exists():
                    continue
                with open(candidate, "r", encoding="utf-8") as f:
                    payload = _pp_json_load(f)
                if isinstance(payload, dict):
                    map_path = candidate
                    break
            except Exception as exc:
                self._append_log(f"Could not read sorting_time_map.json at {candidate}: {exc}")
        if not isinstance(payload, dict):
            return None, None

        recordings = payload.get("recordings", {})
        if not isinstance(recordings, dict) or not recordings:
            return None, map_path

        label = str((group or {}).get("label", "") or info.get("recording", "") or "").strip()
        if label in recordings and isinstance(recordings[label], dict):
            rec = recordings[label]
            if rec.get("chunks"):
                return rec, map_path

        group_files = {self._norm_export_path(p) for p in ((group or {}).get("files", []) or []) if str(p).strip()}
        best_key = None
        best_score = 0
        for key, rec in recordings.items():
            if not isinstance(rec, dict):
                continue
            source_files = {self._norm_export_path(p) for p in (rec.get("source_files", []) or []) if str(p).strip()}
            if not source_files:
                source_files = {self._norm_export_path(c.get("file_path", "")) for c in (rec.get("chunks", []) or []) if isinstance(c, dict)}
            score = len(group_files & source_files)
            if score > best_score:
                best_key = key
                best_score = score
        if best_key is not None and best_score > 0:
            rec = recordings.get(best_key)
            if isinstance(rec, dict) and rec.get("chunks"):
                return rec, map_path

        if len(recordings) == 1:
            rec = next(iter(recordings.values()))
            if isinstance(rec, dict) and rec.get("chunks"):
                return rec, map_path
        return None, map_path

    @staticmethod
    def _norm_export_path(path):
        try:
            return str(Path(str(path))).replace("\\", "/").lower()
        except Exception:
            return str(path).replace("\\", "/").lower()

    def _electrical_series_path_for_export(self, info, group=None, time_map_recording=None):
        if isinstance(time_map_recording, dict):
            value = str(time_map_recording.get("electrical_series_path", "") or "").strip()
            if value:
                return value
            for chunk in (time_map_recording.get("chunks", []) or []):
                if isinstance(chunk, dict):
                    value = str(chunk.get("electrical_series_path", "") or "").strip()
                    if value:
                        return value
        payload = self._run_payload_for_row(info)
        candidates = []
        if isinstance(payload, dict):
            for obj in (
                payload.get("recording_recipe", {}),
                (payload.get("run_signature", {}) or {}).get("recording_recipe", {}) if isinstance(payload.get("run_signature", {}), dict) else {},
            ):
                if isinstance(obj, dict):
                    candidates.append(((obj.get("data", {}) or {}).get("path", "")))
        recipe = self._read_recipe()
        if isinstance(recipe, dict):
            candidates.append(((recipe.get("data", {}) or {}).get("path", "")))
        for value in candidates:
            value = str(value or "").strip()
            if value:
                return value
        return ""

    def _export_time_chunks_for_info(self, info, sorting_fs=None, max_frame=None):
        group = info.get("recording_group") if isinstance(info.get("recording_group"), dict) else {}
        files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
        time_map_recording, time_map_path = self._load_sorting_time_map_for_export(info, group)
        chunks = []
        mapping_mode = "none"
        gap_safe = False
        if isinstance(time_map_recording, dict) and time_map_recording.get("chunks"):
            for c in time_map_recording.get("chunks", []) or []:
                if isinstance(c, dict):
                    chunks.append(dict(c))
            mapping_mode = "sorting_time_map"
            gap_safe = True

        if not chunks:
            data_path = self._electrical_series_path_for_export(info, group, time_map_recording)
            frame_offset = 0
            for file_index, file_path in enumerate(files):
                file_chunks = []
                n_frames = 0
                if data_path:
                    try:
                        file_chunks, n_frames, _gap_count = _SortingRunWorker._time_map_chunks_from_nwb_file(
                            file_path=file_path,
                            electrical_series_path=data_path,
                            concat_frame_offset=frame_offset,
                            first_chunk_index=len(chunks),
                        )
                    except Exception as exc:
                        self._append_log(f"Could not build gap-aware export chunks for {Path(file_path).name}: {exc}")
                        try:
                            n_frames, fs, start_time = _SortingRunWorker._recording_frame_info_from_nwb(
                                file_path=file_path,
                                electrical_series_path=data_path,
                            )
                            file_chunks = [{
                                "chunk_index": int(len(chunks)),
                                "file_path": str(file_path),
                                "electrical_series_path": str(data_path),
                                "source_start_frame": 0,
                                "source_stop_frame": int(n_frames),
                                "concat_start_frame": int(frame_offset),
                                "concat_stop_frame": int(frame_offset + n_frames),
                                "source_start_time": float(start_time),
                                "source_stop_time_exclusive": float(start_time + (n_frames / fs if fs else n_frames)),
                                "source_last_time": float(start_time + ((max(0, n_frames - 1)) / fs if fs else max(0, n_frames - 1))),
                                "sampling_frequency": float(fs),
                                "n_frames": int(n_frames),
                                "has_timestamps": False,
                                "timestamp_gap_detected": False,
                            }]
                        except Exception:
                            file_chunks = []
                            n_frames = 0
                elif len(files) == 1 and sorting_fs:
                    n_frames = int(max_frame or 0) + 1
                    file_chunks = [{
                        "chunk_index": 0,
                        "file_path": str(file_path),
                        "electrical_series_path": "",
                        "source_start_frame": 0,
                        "source_stop_frame": int(n_frames),
                        "concat_start_frame": 0,
                        "concat_stop_frame": int(n_frames),
                        "source_start_time": 0.0,
                        "source_stop_time_exclusive": float(n_frames) / float(sorting_fs),
                        "source_last_time": float(max(0, n_frames - 1)) / float(sorting_fs),
                        "sampling_frequency": float(sorting_fs),
                        "n_frames": int(n_frames),
                        "has_timestamps": False,
                        "timestamp_gap_detected": False,
                    }]
                chunks.extend(file_chunks)
                if n_frames:
                    frame_offset += int(n_frames)
            if chunks:
                mapping_mode = "rebuilt_from_nwb_electrical_series" if data_path else "single_file_sorting_sampling_frequency"
                gap_safe = bool(data_path)

        def _chunk_key(c):
            return (int(c.get("concat_start_frame", 0)), int(c.get("source_start_frame", 0)))
        chunks = sorted(chunks, key=_chunk_key)
        summary = {
            "mapping_mode": mapping_mode,
            "gap_safe": bool(gap_safe),
            "sorting_time_map_path": str(time_map_path) if time_map_path is not None else "",
            "n_chunks": int(len(chunks)),
            "source_files": files,
            "chunks": [dict(c) for c in chunks],
        }
        return chunks, summary

    def _source_session_start_time_for_export(self, file_path):
        """Return the NWB session_start_time as text for export metadata."""
        file_path = str(file_path or "").strip()
        if not file_path:
            return ""
        cache = getattr(self, "_export_session_start_cache", None)
        if cache is None:
            cache = {}
            self._export_session_start_cache = cache
        key = self._norm_export_path(file_path)
        if key in cache:
            return cache[key]

        value = ""
        try:
            from pynwb import NWBHDF5IO
            with NWBHDF5IO(str(file_path), mode="r", load_namespaces=True) as io:
                nwbfile = io.read()
                raw = getattr(nwbfile, "session_start_time", None)
                value = raw.isoformat() if hasattr(raw, "isoformat") else str(raw or "")
        except Exception:
            value = ""

        cache[key] = value
        return value

    def _timestamps_for_export_chunk(self, chunk):
        """Return the full ElectricalSeries timestamps array for a chunk, if available."""
        if not isinstance(chunk, dict) or not bool(chunk.get("has_timestamps", False)):
            return None
        file_path = str(chunk.get("file_path", "") or "").strip()
        es_path = str(chunk.get("electrical_series_path", "") or "").strip()
        if not file_path or not es_path:
            return None

        cache = getattr(self, "_export_timestamp_cache", None)
        if cache is None:
            cache = {}
            self._export_timestamp_cache = cache
        key = (self._norm_export_path(file_path), es_path)
        if key in cache:
            return cache[key]

        timestamps = None
        try:
            from pynwb import NWBHDF5IO
            with NWBHDF5IO(str(file_path), mode="r", load_namespaces=True) as io:
                nwbfile = io.read()
                es = _SortingRunWorker._resolve_nwb_object_path(nwbfile, es_path)
                ts = getattr(es, "timestamps", None)
                if ts is not None:
                    timestamps = np.asarray(ts[:], dtype=float).reshape(-1)
        except Exception as exc:
            self._append_log(f"Could not read exact NWB timestamps for {Path(file_path).name}: {exc}")
            timestamps = None

        cache[key] = timestamps
        return timestamps

    @staticmethod
    def _excel_bool_text(value):
        return "yes" if bool(value) else "no"

    @staticmethod
    def _template_channel_column_name(channel_id, channel_index, used=None):
        used = used if isinstance(used, set) else set()
        raw = str(channel_id if channel_id is not None else channel_index).strip() or str(channel_index)
        raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or str(channel_index)
        name = f"channel_{raw}"
        if name in used:
            name = f"channel_{channel_index}_{raw}"
        base = name
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        return name

    def _template_nbefore(self, analyzer):
        """Best-effort lookup of the template alignment sample index."""
        candidates = []
        try:
            ext = analyzer.get_extension("templates")
            candidates.append(ext)
        except Exception:
            pass
        candidates.append(analyzer)
        for obj in candidates:
            if obj is None:
                continue
            for attr in ("nbefore", "n_before", "num_samples_before"):
                try:
                    value = getattr(obj, attr)
                    if value is not None and str(value).strip() != "":
                        return int(value)
                except Exception:
                    pass
            for method_name in ("get_params", "get_extension_params"):
                try:
                    method = getattr(obj, method_name, None)
                    if callable(method):
                        params = method()
                        if isinstance(params, dict):
                            for key in ("nbefore", "n_before", "num_samples_before"):
                                if key in params and params.get(key) is not None:
                                    return int(params.get(key))
                except Exception:
                    pass
            try:
                params = getattr(obj, "params", None)
                if isinstance(params, dict):
                    for key in ("nbefore", "n_before", "num_samples_before"):
                        if key in params and params.get(key) is not None:
                            return int(params.get(key))
            except Exception:
                pass
        return None

    def _build_spike_export_tables(self, info, sorting, variant, analyzer_folder=None):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")

        variant_kind = str((variant or {}).get("kind", "original") or "original").lower()
        variant_label = "curated" if variant_kind == "curated" else "uncurated"

        unit_ids = self._sorting_unit_ids(sorting)
        sorting_fs = self._sorting_sampling_frequency(sorting)
        max_frame = 0
        unit_frames = {}
        for uid in unit_ids:
            frames = self._sorting_spike_train_frames(sorting, uid)
            if frames.size:
                frames = np.asarray(frames, dtype=np.int64)
                max_frame = max(max_frame, int(np.nanmax(frames)))
            unit_frames[uid] = frames

        chunks, mapping_summary = self._export_time_chunks_for_info(info, sorting_fs=sorting_fs, max_frame=max_frame)
        analyzer_folder_text = str(analyzer_folder or (variant or {}).get("folder", "") or "")
        source_files = [str(p) for p in (mapping_summary.get("source_files", []) or []) if str(p).strip()]
        source_file_index_by_norm = {self._norm_export_path(p): i for i, p in enumerate(source_files)}
        rows = []
        by_file = {}

        for uid in unit_ids:
            frames = unit_frames.get(uid, np.asarray([], dtype=np.int64))
            frames = np.asarray(frames, dtype=np.int64).reshape(-1)
            if frames.size == 0:
                continue
            mapped = np.zeros(frames.shape, dtype=bool)
            for chunk in chunks:
                try:
                    concat_start = int(chunk.get("concat_start_frame", 0))
                    concat_stop = int(chunk.get("concat_stop_frame", concat_start))
                    source_start = int(chunk.get("source_start_frame", 0))
                    source_stop = int(chunk.get("source_stop_frame", source_start))
                    fs = float(chunk.get("sampling_frequency", sorting_fs or 1.0))
                    source_start_time = float(chunk.get("source_start_time", 0.0))
                    file_path = str(chunk.get("file_path", "") or "")
                    chunk_index = int(chunk.get("chunk_index", -1))
                except Exception:
                    continue
                if concat_stop <= concat_start or fs <= 0:
                    continue
                mask = (frames >= concat_start) & (frames < concat_stop)
                if not np.any(mask):
                    continue
                local_idx = np.flatnonzero(mask)
                selected_frames = frames[mask]
                source_frames = source_start + (selected_frames - concat_start)
                valid = (source_frames >= source_start) & (source_frames < source_stop)
                if not np.any(valid):
                    continue
                selected_frames = selected_frames[valid]
                source_frames = source_frames[valid]
                local_idx = local_idx[valid]

                computed_spike_times = source_start_time + ((source_frames - source_start) / fs)
                spike_times = np.asarray(computed_spike_times, dtype=float).copy()
                exact_timestamp_used = np.zeros(spike_times.shape, dtype=bool)
                timestamp_mode = "computed_from_chunk_start_and_sampling_frequency"
                timestamps = self._timestamps_for_export_chunk(chunk)
                if timestamps is not None and timestamps.size:
                    idx = np.asarray(source_frames, dtype=np.int64)
                    ok = (idx >= 0) & (idx < int(timestamps.size))
                    if np.any(ok):
                        exact_values = np.asarray(timestamps[idx[ok]], dtype=float)
                        finite = np.isfinite(exact_values)
                        if np.any(finite):
                            ok_positions = np.flatnonzero(ok)[finite]
                            spike_times[ok_positions] = exact_values[finite]
                            exact_timestamp_used[ok_positions] = True
                            timestamp_mode = "exact_nwb_timestamp_when_available"

                mapped[local_idx] = True

                file_bucket = by_file.setdefault(file_path, {})
                unit_bucket = file_bucket.setdefault(str(uid), {"unit_id": uid, "spike_times": [], "source_frames": [], "sorter_frames": []})
                unit_bucket["spike_times"].extend([float(x) for x in spike_times.tolist()])
                unit_bucket["source_frames"].extend([int(x) for x in source_frames.tolist()])
                unit_bucket["sorter_frames"].extend([int(x) for x in selected_frames.tolist()])

                norm_file = self._norm_export_path(file_path)
                source_file_index = source_file_index_by_norm.get(norm_file, "")
                source_file_name = Path(file_path).name if file_path else ""
                source_session_start_time = self._source_session_start_time_for_export(file_path) if file_path else ""
                for j, sorter_frame, source_frame, spike_time, computed_time, exact_used in zip(
                    local_idx.tolist(),
                    selected_frames.tolist(),
                    source_frames.tolist(),
                    spike_times.tolist(),
                    computed_spike_times.tolist(),
                    exact_timestamp_used.tolist(),
                ):
                    rows.append({
                        "recording": info.get("display_recording", info.get("recording", "")),
                        "sorter": info.get("sorter", ""),
                        "run_name": info.get("run_name", ""),
                        "curation_state": variant_label,
                        "unit_id": str(uid),
                        "spike_index_in_unit": int(j),
                        "spike_time_s": float(spike_time),
                        "computed_spike_time_s": float(computed_time),
                        "exact_timestamp_used": self._excel_bool_text(exact_used),
                        "sorter_frame": int(sorter_frame),
                        "source_frame": int(source_frame),
                        "source_file_index": source_file_index,
                        "source_file_name": source_file_name,
                        "source_session_start_time": source_session_start_time,
                        "nwb_file": file_path,
                        "chunk_index": int(chunk_index),
                        "mapping_status": "mapped",
                        "mapping_mode": mapping_summary.get("mapping_mode", ""),
                        "timestamp_mode": timestamp_mode,
                        "analyzer_folder": analyzer_folder_text,
                    })

            if not np.all(mapped):
                unmapped_frames = frames[~mapped]
                fallback_fs = float(sorting_fs or 1.0)
                for j, sorter_frame in zip(np.flatnonzero(~mapped).tolist(), unmapped_frames.tolist()):
                    fallback_time = float(sorter_frame) / fallback_fs
                    rows.append({
                        "recording": info.get("display_recording", info.get("recording", "")),
                        "sorter": info.get("sorter", ""),
                        "run_name": info.get("run_name", ""),
                        "curation_state": variant_label,
                        "unit_id": str(uid),
                        "spike_index_in_unit": int(j),
                        "spike_time_s": fallback_time,
                        "computed_spike_time_s": fallback_time,
                        "exact_timestamp_used": "no",
                        "sorter_frame": int(sorter_frame),
                        "source_frame": "",
                        "source_file_index": "",
                        "source_file_name": "",
                        "source_session_start_time": "",
                        "nwb_file": "",
                        "chunk_index": "",
                        "mapping_status": "unmapped",
                        "mapping_mode": "unmapped_sorter_time",
                        "timestamp_mode": "sorter_frame_divided_by_sorting_sampling_frequency",
                        "analyzer_folder": analyzer_folder_text,
                    })

        columns = [
            "recording",
            "sorter",
            "run_name",
            "curation_state",
            "unit_id",
            "spike_index_in_unit",
            "spike_time_s",
            "computed_spike_time_s",
            "exact_timestamp_used",
            "sorter_frame",
            "source_frame",
            "source_file_index",
            "source_file_name",
            "source_session_start_time",
            "nwb_file",
            "chunk_index",
            "mapping_status",
            "mapping_mode",
            "timestamp_mode",
            "analyzer_folder",
        ]
        df = pd.DataFrame(rows, columns=columns)
        if not df.empty:
            df = df.sort_values(["nwb_file", "unit_id", "spike_time_s", "sorter_frame"], kind="mergesort").reset_index(drop=True)
        mapping_summary.update({
            "curation_state": variant_label,
            "unit_count": int(len(unit_ids)),
            "spike_count": int(len(df)),
            "mapped_spike_count": int((df.get("mapping_status") == "mapped").sum()) if not df.empty else 0,
            "unmapped_spike_count": int((df.get("mapping_status") == "unmapped").sum()) if not df.empty else 0,
            "exact_timestamp_spike_count": int((df.get("exact_timestamp_used") == "yes").sum()) if not df.empty else 0,
            "sorting_sampling_frequency_hz": float(sorting_fs) if sorting_fs else "",
            "analyzer_folder": analyzer_folder_text,
        })
        return df, by_file, mapping_summary

    def _extract_templates_payload(self, analyzer):
        ext = None
        try:
            ext = analyzer.get_extension("templates")
        except Exception:
            ext = None
        candidates = []
        if ext is not None:
            for name in ("get_templates", "get_data"):
                method = getattr(ext, name, None)
                if callable(method):
                    candidates.append(method)
        for name in ("get_templates", "get_data"):
            method = getattr(analyzer, name, None)
            if callable(method):
                candidates.append(method)

        kwargs_candidates = (
            {"operator": "average"},
            {"outputs": "average"},
            {"return_scaled": True},
            {},
        )
        for method in candidates:
            for kwargs in kwargs_candidates:
                try:
                    return method(**kwargs)
                except TypeError:
                    continue
                except Exception:
                    continue
        return None

    def _analyzer_channel_ids(self, analyzer):
        for getter in (
            lambda a: a.get_channel_ids(),
            lambda a: getattr(a, "channel_ids"),
            lambda a: a.recording.get_channel_ids(),
            lambda a: getattr(a, "recording", None).get_channel_ids(),
        ):
            try:
                ids = list(getter(analyzer))
                if ids:
                    return ids
            except Exception:
                pass
        return []

    def _template_array_to_unit_arrays(self, payload, unit_ids, channel_ids):
        if payload is None:
            return {}
        if isinstance(payload, dict):
            for key in ("average", "templates", "template", "data"):
                if key in payload:
                    return self._template_array_to_unit_arrays(payload.get(key), unit_ids, channel_ids)
            out = {}
            unit_text = {str(u): u for u in unit_ids}
            for key, value in payload.items():
                arr = np.asarray(value)
                if arr.ndim >= 1 and str(key) in unit_text:
                    out[unit_text[str(key)]] = arr
            if out:
                return out
        arr = np.asarray(payload)
        if arr.size == 0:
            return {}
        if arr.ndim == 2:
            out = {}
            if len(unit_ids) == 1:
                out[unit_ids[0]] = arr
            return out
        if arr.ndim != 3:
            return {}

        n_units = len(unit_ids)
        n_channels = len(channel_ids)
        axes = list(range(3))
        unit_axis = next((ax for ax in axes if arr.shape[ax] == n_units), 0)
        remaining = [ax for ax in axes if ax != unit_axis]
        channel_axis = None
        if n_channels:
            channel_axis = next((ax for ax in remaining if arr.shape[ax] == n_channels), None)
        if channel_axis is None:
            channel_axis = remaining[-1]
        sample_axis = next(ax for ax in remaining if ax != channel_axis)
        arr = np.transpose(arr, (unit_axis, sample_axis, channel_axis))
        return {unit_ids[i]: arr[i] for i in range(min(len(unit_ids), arr.shape[0]))}

    def _orient_template_2d(self, arr, channel_ids):
        arr = np.asarray(arr)
        if arr.ndim != 2:
            return arr.reshape(-1, 1)
        n_channels = len(channel_ids)
        if n_channels and arr.shape[0] == n_channels and arr.shape[1] != n_channels:
            return arr.T
        return arr

    def _build_template_export_tables(self, analyzer, sorting, variant, analyzer_folder=None):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")

        variant_kind = str((variant or {}).get("kind", "original") or "original").lower()
        variant_label = "curated" if variant_kind == "curated" else "uncurated"

        unit_ids = self._sorting_unit_ids(sorting)
        channel_ids = self._analyzer_channel_ids(analyzer)
        payload = self._extract_templates_payload(analyzer)
        by_unit = self._template_array_to_unit_arrays(payload, unit_ids, channel_ids)
        if not by_unit:
            raise RuntimeError(
                "No waveform templates were found in the selected SortingAnalyzer. "
                "Open summary generation first, or compute/save the templates extension before exporting templates."
            )

        analyzer_folder_text = str(analyzer_folder or (variant or {}).get("folder", "") or "")
        sorting_fs = self._sorting_sampling_frequency(sorting)
        nbefore = self._template_nbefore(analyzer)

        # Excel has 16,384 columns. Keep metadata columns plus one column per channel.
        # If a future probe exceeds that limit, the export falls back to the old long format.
        max_excel_columns = 16384
        metadata_columns = 7
        force_long = bool(len(channel_ids) + metadata_columns > max_excel_columns)

        used_column_names = set()
        sample_rows = []
        long_rows = []
        channel_rows = []
        template_shapes = []

        inferred_n_channels = None
        for uid in unit_ids:
            template = by_unit.get(uid)
            if template is None and str(uid) in by_unit:
                template = by_unit[str(uid)]
            if template is None:
                continue
            template = self._orient_template_2d(np.asarray(template), channel_ids)
            if template.ndim != 2:
                continue
            inferred_n_channels = int(template.shape[1])
            break

        n_channels_for_columns = int(inferred_n_channels or len(channel_ids) or 0)
        channels = channel_ids if len(channel_ids) == n_channels_for_columns else list(range(n_channels_for_columns))
        channel_columns = [self._template_channel_column_name(channels[i], i, used_column_names) for i in range(n_channels_for_columns)]
        for i in range(n_channels_for_columns):
            channel_rows.append({
                "channel_index": int(i),
                "channel_id": str(channels[i]),
                "excel_column": channel_columns[i],
            })

        for uid in unit_ids:
            template = by_unit.get(uid)
            if template is None and str(uid) in by_unit:
                template = by_unit[str(uid)]
            if template is None:
                continue
            template = self._orient_template_2d(np.asarray(template), channel_ids)
            if template.ndim != 2:
                continue
            n_samples, n_channels = template.shape
            template_shapes.append(f"unit {uid}: {n_samples} samples x {n_channels} channels")
            local_channels = channel_ids if len(channel_ids) == n_channels else list(range(n_channels))
            local_columns = channel_columns if len(channel_columns) == n_channels else [self._template_channel_column_name(local_channels[i], i, set()) for i in range(n_channels)]
            for sample_index in range(n_samples):
                if sorting_fs:
                    zero = int(nbefore) if nbefore is not None else 0
                    sample_time_ms = ((float(sample_index) - float(zero)) / float(sorting_fs)) * 1000.0
                else:
                    sample_time_ms = ""
                if force_long:
                    for channel_index in range(n_channels):
                        long_rows.append({
                            "curation_state": variant_label,
                            "unit_id": str(uid),
                            "sample_index": int(sample_index),
                            "sample_time_ms": sample_time_ms,
                            "template_zero_sample_index": int(nbefore) if nbefore is not None else "",
                            "channel_id": str(local_channels[channel_index]),
                            "channel_index": int(channel_index),
                            "amplitude": float(template[sample_index, channel_index]),
                            "analyzer_folder": analyzer_folder_text,
                        })
                else:
                    row = {
                        "curation_state": variant_label,
                        "unit_id": str(uid),
                        "sample_index": int(sample_index),
                        "sample_time_ms": sample_time_ms,
                        "template_zero_sample_index": int(nbefore) if nbefore is not None else "",
                        "analyzer_folder": analyzer_folder_text,
                    }
                    for channel_index in range(n_channels):
                        row[local_columns[channel_index]] = float(template[sample_index, channel_index])
                    sample_rows.append(row)

        if force_long:
            templates_df = pd.DataFrame(long_rows)
            template_format = "long_rows_unit_sample_channel"
        else:
            templates_df = pd.DataFrame(sample_rows)
            ordered = [
                "curation_state",
                "unit_id",
                "sample_index",
                "sample_time_ms",
                "template_zero_sample_index",
                "analyzer_folder",
            ] + channel_columns
            if not templates_df.empty:
                templates_df = templates_df.reindex(columns=[c for c in ordered if c in templates_df.columns])
            template_format = "wide_rows_unit_sample_channels_as_columns"

        channels_df = pd.DataFrame(channel_rows, columns=["channel_index", "channel_id", "excel_column"])
        info_rows = [
            {"field": "export_type", "value": "waveform_templates"},
            {"field": "format", "value": template_format},
            {"field": "curation_state", "value": variant_label},
            {"field": "unit_count", "value": int(len(unit_ids))},
            {"field": "channel_count", "value": int(n_channels_for_columns)},
            {"field": "sorting_sampling_frequency_hz", "value": float(sorting_fs) if sorting_fs else ""},
            {"field": "template_zero_sample_index", "value": int(nbefore) if nbefore is not None else ""},
            {"field": "analyzer_folder", "value": analyzer_folder_text},
        ]
        for shape in template_shapes[:200]:
            info_rows.append({"field": "template_shape", "value": shape})
        if len(template_shapes) > 200:
            info_rows.append({"field": "template_shape", "value": f"... and {len(template_shapes) - 200} more"})
        template_info_df = pd.DataFrame(info_rows, columns=["field", "value"])

        return {
            "templates": templates_df,
            "channels": channels_df,
            "template_info": template_info_df,
        }

    def _build_template_export_dataframe(self, analyzer, sorting, variant, analyzer_folder=None):
        """Compatibility wrapper. New Excel export uses _build_template_export_tables()."""
        tables = self._build_template_export_tables(analyzer, sorting, variant, analyzer_folder)
        return tables.get("templates")

    def _make_spike_export_units_sheet(self, spike_df):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")
        if spike_df is None or spike_df.empty:
            return pd.DataFrame(columns=[
                "curation_state", "unit_id", "spike_count", "mapped_spike_count",
                "unmapped_spike_count", "first_spike_time_s", "last_spike_time_s",
                "n_source_files", "source_files",
            ])
        rows = []
        for (curation_state, unit_id), g in spike_df.groupby(["curation_state", "unit_id"], dropna=False, sort=True):
            mapped = g[g.get("mapping_status", "") == "mapped"] if "mapping_status" in g else g
            files = sorted(set(str(x) for x in g.get("nwb_file", []) if str(x).strip()))
            rows.append({
                "curation_state": curation_state,
                "unit_id": unit_id,
                "spike_count": int(len(g)),
                "mapped_spike_count": int(len(mapped)),
                "unmapped_spike_count": int(len(g) - len(mapped)),
                "first_spike_time_s": float(g["spike_time_s"].min()) if "spike_time_s" in g and len(g) else "",
                "last_spike_time_s": float(g["spike_time_s"].max()) if "spike_time_s" in g and len(g) else "",
                "n_source_files": int(len(files)),
                "source_files": "; ".join(files),
            })
        return pd.DataFrame(rows)

    def _make_spike_export_source_files_sheet(self, spike_df, mapping_summary):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")
        source_files = [str(p) for p in ((mapping_summary or {}).get("source_files", []) or []) if str(p).strip()]
        chunks = [dict(c) for c in ((mapping_summary or {}).get("chunks", []) or []) if isinstance(c, dict)]
        if not chunks:
            chunks = []
        spike_counts = {}
        if spike_df is not None and not spike_df.empty and "nwb_file" in spike_df.columns:
            try:
                spike_counts = spike_df.groupby("nwb_file").size().to_dict()
            except Exception:
                spike_counts = {}
        rows = []
        for i, path in enumerate(source_files):
            file_chunks = [c for c in chunks if self._norm_export_path(c.get("file_path", "")) == self._norm_export_path(path)]
            concat_starts = [int(c.get("concat_start_frame", 0)) for c in file_chunks if str(c.get("concat_start_frame", "")).strip() != ""]
            concat_stops = [int(c.get("concat_stop_frame", 0)) for c in file_chunks if str(c.get("concat_stop_frame", "")).strip() != ""]
            rows.append({
                "source_file_index": int(i),
                "source_file_name": Path(path).name,
                "source_session_start_time": self._source_session_start_time_for_export(path),
                "nwb_file": path,
                "spike_count": int(spike_counts.get(path, 0)),
                "n_chunks": int(len(file_chunks)),
                "concat_start_frame": min(concat_starts) if concat_starts else "",
                "concat_stop_frame": max(concat_stops) if concat_stops else "",
            })
        if not rows and spike_df is not None and not spike_df.empty and "nwb_file" in spike_df.columns:
            for i, path in enumerate(sorted(set(str(x) for x in spike_df["nwb_file"].tolist() if str(x).strip()))):
                rows.append({
                    "source_file_index": int(i),
                    "source_file_name": Path(path).name,
                    "source_session_start_time": self._source_session_start_time_for_export(path),
                    "nwb_file": path,
                    "spike_count": int(spike_counts.get(path, 0)),
                    "n_chunks": "",
                    "concat_start_frame": "",
                    "concat_stop_frame": "",
                })
        return pd.DataFrame(rows)

    def _make_export_info_sheet(self, info, variant, mapping_summary, analyzer_folder):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")

        variant_kind = str((variant or {}).get("kind", "original") or "original").lower()
        variant_label = "curated" if variant_kind == "curated" else "uncurated"

        rows = [
            {"field": "exported_at", "value": datetime.now().isoformat(timespec="seconds")},
            {"field": "recording", "value": info.get("display_recording", info.get("recording", ""))},
            {"field": "sorter", "value": info.get("sorter", "")},
            {"field": "run_name", "value": info.get("run_name", "")},
            {"field": "run_date", "value": info.get("run_date", "")},
            {"field": "curation_state", "value": variant_label},
            {"field": "preprocessing_pipeline_id", "value": info.get("preprocessing_pipeline_id", "")},
            {"field": "sorter_params_id", "value": info.get("sorter_params_id", "")},
            {"field": "analyzer_folder", "value": str(analyzer_folder or (variant or {}).get("folder", "") or "")},
        ]
        for key, value in sorted((mapping_summary or {}).items(), key=lambda kv: str(kv[0])):
            if key == "chunks":
                continue
            rows.append({"field": str(key), "value": _canonical_json_text(value) if isinstance(value, (dict, list, tuple, set)) else value})
        return pd.DataFrame(rows, columns=["field", "value"])

    @staticmethod
    def _excel_sheet_name(value, used=None, default="unit"):
        """Return an Excel-safe sheet name, keeping the unit id readable."""
        used = used if isinstance(used, set) else set()
        text = str(value if value is not None else default).strip() or str(default or "unit")
        text = re.sub(r"[\\/\?\*\[\]:]+", "_", text).strip("' ") or str(default or "unit")
        base = text[:31] or str(default or "unit")[:31] or "unit"
        name = base
        suffix = 2
        while name in used:
            suffix_text = f"_{suffix}"
            name = f"{base[:31 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        used.add(name)
        return name

    def _write_excel_sheet_split(self, writer, df, sheet_name):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to write Excel files: {exc}")
        if df is None:
            df = pd.DataFrame()
        max_rows = 1_000_000
        base = self._excel_sheet_name(sheet_name, used=set(), default="data")[:25] or "data"
        if len(df) == 0:
            df.to_excel(writer, sheet_name=base[:31], index=False)
            return
        sheet_i = 1
        for start in range(0, len(df), max_rows):
            stop = min(start + max_rows, len(df))
            suffix = "" if sheet_i == 1 else f"_{sheet_i}"
            sheet = f"{base}{suffix}"[:31]
            df.iloc[start:stop].to_excel(writer, sheet_name=sheet, index=False)
            sheet_i += 1

    def _write_multi_sheet_excel(self, tables, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with __import__("pandas").ExcelWriter(path) as writer:
            for sheet_name, df in (tables or {}).items():
                self._write_excel_sheet_split(writer, df, sheet_name)
        return path

    def _write_spike_times_excel(self, spike_df, path, info, variant, mapping_summary, analyzer_folder=None):
        """Write a compact spike-time workbook: one sheet per unit, metadata elsewhere."""
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        used_sheets = set()
        compact_columns = [
            "concatenated_recording_frame",
            "NWB_frame",
            "spike_time_s",
            "NWB_corresponding_file",
        ]

        with pd.ExcelWriter(path) as writer:
            if spike_df is None or spike_df.empty:
                pd.DataFrame(columns=compact_columns).to_excel(writer, sheet_name="empty", index=False)
                return path

            df = spike_df.copy()
            if "unit_id" not in df.columns:
                pd.DataFrame(columns=compact_columns).to_excel(writer, sheet_name="empty", index=False)
                return path

            for unit_id in sorted(df["unit_id"].dropna().unique().tolist(), key=lambda x: _natural_text_key(str(x))):
                unit_df = df[df["unit_id"].astype(str) == str(unit_id)].copy()
                if unit_df.empty:
                    continue
                compact = pd.DataFrame({
                    "concatenated_recording_frame": unit_df.get("sorter_frame", ""),
                    "NWB_frame": unit_df.get("source_frame", ""),
                    "spike_time_s": unit_df.get("spike_time_s", ""),
                    "NWB_corresponding_file": unit_df.get("nwb_file", ""),
                })
                try:
                    compact = compact.sort_values(["NWB_corresponding_file", "spike_time_s", "concatenated_recording_frame"], kind="mergesort")
                except Exception:
                    pass
                sheet = self._excel_sheet_name(unit_id, used=used_sheets, default="unit")
                compact.to_excel(writer, sheet_name=sheet, index=False)

            if not used_sheets:
                pd.DataFrame(columns=compact_columns).to_excel(writer, sheet_name="empty", index=False)
        return path

    def _write_templates_excel(self, template_tables, path):
        """Write a compact template workbook: one sheet per unit, channels as columns."""
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        templates = (template_tables or {}).get("templates")
        if templates is None:
            templates = pd.DataFrame()
        templates = templates.copy()
        used_sheets = set()

        metadata_cols = {
            "curation_state",
            "unit_id",
            "sample_index",
            "sample_time_ms",
            "template_zero_sample_index",
            "analyzer_folder",
            "channel_id",
            "channel_index",
            "amplitude",
        }
        channel_cols = [c for c in templates.columns if str(c) not in metadata_cols]

        with pd.ExcelWriter(path) as writer:
            if templates.empty or "unit_id" not in templates.columns:
                pd.DataFrame().to_excel(writer, sheet_name="empty", index=False)
                return path

            for unit_id in sorted(templates["unit_id"].dropna().unique().tolist(), key=lambda x: _natural_text_key(str(x))):
                unit_df = templates[templates["unit_id"].astype(str) == str(unit_id)].copy()
                if unit_df.empty:
                    continue

                if channel_cols:
                    try:
                        unit_df = unit_df.sort_values("sample_index", kind="mergesort") if "sample_index" in unit_df.columns else unit_df
                    except Exception:
                        pass
                    compact = unit_df[channel_cols].copy()
                elif {"sample_index", "channel_id", "amplitude"}.issubset(set(unit_df.columns)):
                    # Fallback for very large probes or older exports: reconstruct channel columns per unit.
                    compact = unit_df.pivot_table(
                        index="sample_index",
                        columns="channel_id",
                        values="amplitude",
                        aggfunc="first",
                    ).sort_index()
                    compact.columns = [self._template_channel_column_name(c, i, set()) for i, c in enumerate(compact.columns)]
                    compact = compact.reset_index(drop=True)
                else:
                    compact = pd.DataFrame()

                sheet = self._excel_sheet_name(unit_id, used=used_sheets, default="unit")
                compact.to_excel(writer, sheet_name=sheet, index=False)

            if not used_sheets:
                pd.DataFrame().to_excel(writer, sheet_name="empty", index=False)
        return path

    def _make_export_metadata_issues_sheet(self, spike_df, mapping_summary):
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")
        rows = []
        if spike_df is not None and not spike_df.empty:
            try:
                unmapped = spike_df[spike_df.get("mapping_status", "") != "mapped"]
                if len(unmapped):
                    rows.append({
                        "issue": "unmapped_spikes",
                        "count": int(len(unmapped)),
                        "details": "These spikes kept sorter-frame/sampling-frequency timing because no matching NWB time chunk was found.",
                    })
            except Exception:
                pass
            try:
                approx = spike_df[spike_df.get("exact_timestamp_used", "no") != "yes"]
                if len(approx):
                    rows.append({
                        "issue": "computed_timestamps",
                        "count": int(len(approx)),
                        "details": "These spike times were computed from chunk start time and sampling frequency rather than copied from an exact NWB timestamps vector.",
                    })
            except Exception:
                pass
            try:
                missing_file = spike_df[spike_df.get("nwb_file", "").astype(str).str.strip() == ""]
                if len(missing_file):
                    rows.append({
                        "issue": "missing_nwb_file",
                        "count": int(len(missing_file)),
                        "details": "No source NWB file could be assigned for these rows.",
                    })
            except Exception:
                pass
        chunks = [dict(c) for c in ((mapping_summary or {}).get("chunks", []) or []) if isinstance(c, dict)]
        if not chunks:
            rows.append({
                "issue": "missing_time_chunks",
                "count": 1,
                "details": "No time-chunk metadata was available. Spike-time mapping may be approximate.",
            })
        if not rows:
            rows.append({"issue": "none_detected", "count": 0, "details": "No obvious mapping metadata issue was detected."})
        return pd.DataFrame(rows, columns=["issue", "count", "details"])

    def _write_export_metadata_excel(self, path, info, variant, mapping_summary, analyzer_folder=None, *, spike_df=None, template_tables=None):
        """Write the tracking metadata removed from the compact data workbooks."""
        try:
            import pandas as pd
        except Exception as exc:
            raise RuntimeError(f"pandas is required to export Excel files: {exc}")
        chunks = [dict(c) for c in ((mapping_summary or {}).get("chunks", []) or []) if isinstance(c, dict)]
        tables = {
            "export_info": self._make_export_info_sheet(info, variant, mapping_summary or {}, analyzer_folder),
            "source_files": self._make_spike_export_source_files_sheet(spike_df, mapping_summary or {}),
            "time_chunks": pd.DataFrame(chunks),
            "units": self._make_spike_export_units_sheet(spike_df),
            "mapping_issues": self._make_export_metadata_issues_sheet(spike_df, mapping_summary or {}),
        }
        if template_tables:
            channels = (template_tables or {}).get("channels")
            template_info = (template_tables or {}).get("template_info")
            if channels is not None:
                tables["template_channels"] = channels
            if template_info is not None:
                tables["template_info"] = template_info
        return self._write_multi_sheet_excel(tables, path)

    def _write_dataframe_excel_split(self, df, path, *, base_sheet="data"):
        return self._write_multi_sheet_excel({base_sheet: df}, path)

    def _existing_export_unit_keys(self, units_table):
        keys = set()
        if units_table is None:
            return keys
        try:
            colnames = set(getattr(units_table, "colnames", []) or [])
            if not {"spike_sorting_run", "sorter_unit_id"}.issubset(colnames):
                return keys
            df = units_table.to_dataframe()
            for _, row in df.iterrows():
                keys.add((str(row.get("spike_sorting_run", "")), str(row.get("sorter_unit_id", ""))))
        except Exception:
            return set()
        return keys

    def _ensure_nwb_unit_columns(self, nwbfile):
        columns = {
            "sorter_unit_id": "Original unit id from the SpikeInterface sorting.",
            "sorting_project": "Spike-sorting project/folder name that produced this unit.",
            "spike_sorting_run": "Spike-sorting run identifier from the NWB GUI.",
            "sorter_name": "Spike sorter used to generate the units.",
            "source_analyzer_folder": "Source SortingAnalyzer folder used for this export.",
        }
        for name, description in columns.items():
            try:
                units = getattr(nwbfile, "units", None)
                if units is not None and name in (getattr(units, "colnames", []) or []):
                    continue
                nwbfile.add_unit_column(name=name, description=description)
            except ValueError:
                pass
            except Exception as exc:
                self._append_log(f"Could not add NWB Units column {name}: {exc}")

    def _sorting_project_name_for_export(self, info=None):
        """Return the Spike_sorting project name used for the NWB Units column."""
        for candidate in (
            getattr(self, "sorting_path", None),
            (self.sorting_info or {}).get("path", None) if isinstance(getattr(self, "sorting_info", None), dict) else None,
        ):
            try:
                p = Path(candidate)
                if str(p).strip() and p.name:
                    return str(p.name)
            except Exception:
                pass

        try:
            name = (self.sorting_info or {}).get("name", "") if isinstance(getattr(self, "sorting_info", None), dict) else ""
            if str(name).strip():
                return str(name).strip()
        except Exception:
            pass

        try:
            output_folder = Path((info or {}).get("output_folder", "")).resolve()
            parts_lower = [part.lower() for part in output_folder.parts]
            if "spike_sorting" in parts_lower:
                idx = parts_lower.index("spike_sorting")
                if idx + 1 < len(output_folder.parts):
                    project = output_folder.parts[idx + 1]
                    if str(project).strip():
                        return str(project)
            if "sorter_output" in parts_lower:
                idx = parts_lower.index("sorter_output")
                if idx > 0:
                    project = output_folder.parts[idx - 1]
                    if str(project).strip():
                        return str(project)
        except Exception:
            pass

        return "unknown"

    @staticmethod
    def _unit_project_path_segment(project):
        text = str(project or "").strip()
        text = re.sub(r'[\\/:*?"<>|]', "_", text).strip("._")
        return text or "unknown"

    def _nwb_unit_counts_by_sorting_project(self, nwbfile):
        units = getattr(nwbfile, "units", None)
        if units is None:
            return {}
        try:
            df = units.to_dataframe().copy()
        except Exception:
            return {}
        if df is None or df.empty:
            return {}
        if "sorting_project" not in df.columns:
            return {"": int(len(df))}
        series = df["sorting_project"].fillna("").astype(str).str.strip()
        counts = {}
        for project, count in series.value_counts(dropna=False).items():
            counts[str(project).strip()] = int(count)
        return counts

    @staticmethod
    def _nwb_unit_counts_by_sorting_project_from_rows(rows):
        counts = {}
        for payload in list(rows or []):
            metadata = dict(payload.get("metadata", {}) or {})
            project = str(metadata.get("sorting_project", "") or "").strip()
            counts[project] = int(counts.get(project, 0)) + 1
        return counts

    @staticmethod
    def _existing_nwb_unit_ids(nwbfile):
        try:
            units = getattr(nwbfile, "units", None)
            if units is None:
                return set()
            return set(units.id[:])
        except Exception:
            try:
                return set(nwbfile.units.to_dataframe().index.tolist())
            except Exception:
                return set()

    @staticmethod
    def _nwb_unit_id_for_sorter_unit(sorter_unit_id, existing_ids):
        def _next_id():
            ints = []
            for value in existing_ids:
                try:
                    ints.append(int(value))
                except Exception:
                    pass
            candidate = (max(ints) + 1) if ints else 0
            while candidate in existing_ids:
                candidate += 1
            return candidate
        try:
            candidate = int(sorter_unit_id)
            if candidate not in existing_ids:
                return candidate
        except Exception:
            pass
        return _next_id()

    @staticmethod
    def _norm_write_path(path):
        try:
            return os.path.normcase(os.path.abspath(str(Path(path))))
        except Exception:
            return os.path.normcase(os.path.abspath(str(path)))

    def _paths_refer_to_same_file_for_write(self, a, b):
        if not str(a or "").strip() or not str(b or "").strip():
            return False
        return self._norm_write_path(a) == self._norm_write_path(b)

    def _close_gui_backend_for_nwb_write(self, path):
        """Close every live Python handle targeting the NWB file.

        Windows/HDF5 refuses to reopen a file in ``r+`` while the same Python
        process still owns a read-only PyNWB or h5py handle. The workbench can
        keep such handles in the main backend, processing panels, viewers, or a
        SpikeInterface NWB recording extractor.
        """
        path = Path(path)
        closed_count = 0
        seen_ids = set()

        def _add_unique(container, obj):
            if obj is None:
                return
            oid = id(obj)
            if oid in seen_ids:
                return
            seen_ids.add(oid)
            container.append(obj)

        # Close the explicitly owned NWBBackend objects first.
        backends = []
        gui = None
        try:
            gui = self.sorting_info.get("GUI_instance", None)
        except Exception:
            gui = None

        for owner in (gui, self):
            if owner is None:
                continue
            for attr in ("backend", "nwb_backend", "NWB_backend"):
                try:
                    _add_unique(backends, getattr(owner, attr, None))
                except Exception:
                    pass

        # Walk the Qt parent/child tree because panels may own another backend.
        try:
            obj = self
            visited = set()
            while obj is not None and id(obj) not in visited:
                visited.add(id(obj))
                for attr in ("backend", "nwb_backend", "NWB_backend"):
                    try:
                        _add_unique(backends, getattr(obj, attr, None))
                    except Exception:
                        pass
                try:
                    obj = obj.parent()
                except Exception:
                    obj = None
        except Exception:
            pass

        for root in (gui, self):
            if root is None:
                continue
            try:
                for child in root.findChildren(QtCore.QObject):
                    for attr in ("backend", "nwb_backend", "NWB_backend"):
                        try:
                            _add_unique(backends, getattr(child, attr, None))
                        except Exception:
                            pass
            except Exception:
                pass

        # Include NWBBackend objects that are not reachable through the Qt tree.
        try:
            import gc
            gc.collect()
            for obj in list(gc.get_objects()):
                try:
                    cls = type(obj)
                    if (
                        str(getattr(cls, "__name__", "")) == "NWBBackend"
                        or str(getattr(cls, "__module__", "")).endswith("nwb_backend")
                    ):
                        _add_unique(backends, obj)
                except Exception:
                    continue
        except Exception:
            pass

        for backend in backends:
            try:
                backend_path = getattr(backend, "path", None)
            except Exception:
                backend_path = None

            if not self._paths_refer_to_same_file_for_write(backend_path, path):
                continue

            try:
                is_locked = (
                    bool(backend.is_locked())
                    if callable(getattr(backend, "is_locked", None))
                    else False
                )
            except Exception:
                is_locked = False

            if is_locked:
                raise RuntimeError(
                    f"Cannot write Units to {path} because an NWBBackend is "
                    "locked by another GUI operation."
                )

            try:
                backend.close()
                closed_count += 1
            except Exception as exc:
                raise RuntimeError(
                    f"Could not close the NWBBackend for {path}: {exc}"
                ) from exc

        # Close standalone PyNWB/h5py objects, including handles owned by
        # SpikeInterface NWB recording extractors. Repeat because closing a
        # NWBHDF5IO can expose/release additional h5py objects.
        try:
            import gc
            import h5py
            from pynwb import NWBHDF5IO

            for _ in range(3):
                gc.collect()
                closed_this_pass = 0

                for obj in list(gc.get_objects()):
                    try:
                        if isinstance(obj, NWBHDF5IO):
                            source = getattr(obj, "source", None)
                            if self._paths_refer_to_same_file_for_write(source, path):
                                obj.close()
                                closed_count += 1
                                closed_this_pass += 1
                                continue
                    except Exception:
                        pass

                    try:
                        if isinstance(obj, h5py.File):
                            filename = getattr(obj, "filename", None)
                            if self._paths_refer_to_same_file_for_write(filename, path):
                                if obj.id.valid:
                                    obj.close()
                                    closed_count += 1
                                    closed_this_pass += 1
                    except Exception:
                        pass

                if closed_this_pass == 0:
                    break
        except Exception:
            pass

        try:
            import gc
            gc.collect()
        except Exception:
            pass

        if closed_count:
            self._append_log(
                f"Closed {closed_count} read-only NWB handle(s) before writing: "
                f"{path.name}"
            )
        return bool(closed_count)

    def _close_known_file_handle_object(self, obj):
        """Best-effort close for h5py/PyNWB/SpikeInterface file handles."""
        if obj is None:
            return False
        closed = False

        try:
            import h5py
        except Exception:
            h5py = None
        try:
            from pynwb import NWBHDF5IO
        except Exception:
            NWBHDF5IO = None

        try:
            if h5py is not None and isinstance(obj, h5py.File):
                if obj.id.valid:
                    obj.close()
                    return True
        except Exception:
            pass

        try:
            if NWBHDF5IO is not None and isinstance(obj, NWBHDF5IO):
                obj.close()
                return True
        except Exception:
            pass

        for method_name in ("close", "close_file", "close_files", "cleanup"):
            try:
                method = getattr(obj, method_name, None)
            except Exception:
                method = None
            if not callable(method):
                continue
            try:
                method()
                closed = True
            except TypeError:
                continue
            except Exception:
                continue
        return closed

    def _release_analyzer_before_nwb_write(self, analyzer):
        """Release analyzer/recording references that can keep NWB files open."""
        candidates = []
        if analyzer is not None:
            candidates.append(analyzer)
        try:
            if getattr(self, "current_analyzer", None) is not None:
                candidates.append(self.current_analyzer)
        except Exception:
            pass

        seen = set()

        def walk(obj, depth=0):
            if obj is None or depth > 5:
                return
            oid = id(obj)
            if oid in seen:
                return
            seen.add(oid)

            # Avoid descending into large numerical/string containers.
            if isinstance(obj, (str, bytes, bytearray, int, float, bool, np.ndarray)):
                return

            self._close_known_file_handle_object(obj)

            # Common SpikeInterface/PyNWB/h5py handle attributes.
            for attr in (
                "_file", "file", "_h5_file", "h5_file", "_h5py_file", "h5py_file",
                "_io", "io", "_nwb_io", "nwb_io", "_nwbfile", "nwbfile",
                "recording", "_recording", "recording_extractor", "_recording_extractor",
                "_parent_recording", "parent_recording", "_recording_segments", "recording_segments",
                "_recordings", "recordings", "_segments", "segments",
            ):
                try:
                    child = getattr(obj, attr, None)
                except Exception:
                    child = None
                if child is not None:
                    walk(child, depth + 1)

            if isinstance(obj, dict):
                for child in list(obj.values())[:50]:
                    walk(child, depth + 1)
            elif isinstance(obj, (list, tuple, set)):
                for child in list(obj)[:50]:
                    walk(child, depth + 1)

        for candidate in candidates:
            walk(candidate)

        try:
            if analyzer is not None and getattr(self, "current_analyzer", None) is analyzer:
                self.current_analyzer = None
                self.current_analyzer_folder = None
        except Exception:
            pass

        try:
            import gc
            gc.collect()
        except Exception:
            pass

    def _prepare_nwb_file_for_write(self, path):
        """Release read-only handles and verify that ``path`` can open in r+."""
        path = Path(path)
        last_exc = None

        for attempt in range(5):
            self._close_gui_backend_for_nwb_write(path)

            try:
                import gc
                gc.collect()
            except Exception:
                pass

            try:
                import h5py
                # Probe exclusive read/write access, then close immediately.
                with h5py.File(str(path), "r+"):
                    pass
                return
            except Exception as exc:
                last_exc = exc
                if "already open" not in str(exc).lower():
                    break
                time.sleep(0.25 * (attempt + 1))

        raise RuntimeError(
            f"Could not obtain read/write access to {path} after closing "
            f"the GUI/PyNWB/h5py handles: {last_exc}"
        ) from last_exc

    def _remove_existing_nwb_units_table(self, path):
        """Remove the root NWB Units table before writing a new exported table.

        PyNWB appends cleanly in r+ mode, but replacing a DynamicTable is more
        reliable if the existing /units group is removed from the HDF5 file
        before the file is reopened with NWBHDF5IO.
        """
        path = Path(path)
        try:
            import h5py
        except Exception as exc:
            raise RuntimeError(
                "h5py is required to replace an existing NWB Units table. "
                "Install h5py or disable the replace option to append units instead."
            ) from exc

        try:
            self._prepare_nwb_file_for_write(path)
            with h5py.File(str(path), "r+") as f:
                if "units" not in f:
                    return False
                del f["units"]
                return True
        except Exception as exc:
            raise RuntimeError(
                f"Could not replace the existing NWB Units table in {path}: {exc}"
            ) from exc


    @staticmethod
    def _is_missing_nwb_unit_metadata_value(value):
        """True for metadata values that should be replaced by a typed default."""
        if value is None:
            return True
        try:
            if isinstance(value, np.ndarray):
                return value.size == 0
        except Exception:
            pass
        if isinstance(value, (list, tuple)) and len(value) == 0:
            return True
        try:
            if isinstance(value, np.generic):
                value = value.item()
        except Exception:
            pass
        try:
            if isinstance(value, float) and not np.isfinite(value):
                return True
        except Exception:
            pass
        try:
            import pandas as _pd
            missing = _pd.isna(value)
            if isinstance(missing, (bool, np.bool_)) and bool(missing):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _clean_nwb_unit_metadata_value(value):
        """Return a value that can be passed back to ``NWBFile.add_unit``."""
        try:
            if isinstance(value, np.generic):
                value = value.item()
        except Exception:
            pass
        try:
            if isinstance(value, np.ndarray):
                if value.size == 0:
                    return ""
                if value.size == 1:
                    return value.reshape(-1)[0].item() if hasattr(value.reshape(-1)[0], "item") else value.reshape(-1)[0]
                return value.tolist()
        except Exception:
            pass
        if isinstance(value, (list, tuple)) and len(value) == 0:
            return ""
        try:
            if isinstance(value, (float, np.floating)) and not np.isfinite(value):
                # Keep NaN for numeric Units columns.  Returning an empty string
                # can make HDMF infer an integer dtype and then fail with:
                # "invalid literal for int() with base 10: ''".
                return np.nan
        except Exception:
            pass
        try:
            # pandas missing scalars raise ambiguity for arrays/lists, so keep this guarded.
            import pandas as _pd
            missing = _pd.isna(value)
            if isinstance(missing, (bool, np.bool_)) and bool(missing):
                return ""
        except Exception:
            pass
        return value

    def _default_nwb_unit_metadata_value_for_column(self, rows, column_name):
        """Return a safe default for one Units metadata column.

        Rewriting /units requires every row to provide every custom column.
        Older units can be missing new columns, and new units can be missing old
        columns such as closest_channel.  Empty Python lists are not safe here
        because HDMF cannot infer their dtype, so use a scalar/string default
        unless a numeric default is clearly better.
        """
        for payload in list(rows or []):
            metadata = dict(payload.get("metadata", {}) or {})
            if column_name not in metadata:
                continue
            value = metadata.get(column_name)
            if self._is_missing_nwb_unit_metadata_value(value):
                continue
            try:
                if isinstance(value, np.generic):
                    value = value.item()
            except Exception:
                pass
            if isinstance(value, (bool, np.bool_)):
                return False
            if isinstance(value, (int, np.integer)):
                # Preserve integer-like metadata columns such as closest_channel.
                # Missing rows need an integer sentinel, not "", otherwise HDMF
                # tries to cast the empty string to int when writing.
                return -1
            if isinstance(value, (float, np.floating)):
                return np.nan
            if isinstance(value, np.ndarray):
                try:
                    return np.array([], dtype=value.dtype)
                except Exception:
                    return ""
            if isinstance(value, (list, tuple)):
                try:
                    arr = np.asarray(value)
                    if arr.dtype != object:
                        return np.array([], dtype=arr.dtype)
                except Exception:
                    pass
                return ""
            return ""
        return ""

    def _complete_nwb_unit_metadata(self, metadata, column_names, column_defaults):
        """Fill missing Units metadata columns before calling ``add_unit``."""
        metadata = dict(metadata or {})
        completed = {}
        for col in list(column_names or []):
            value = metadata.get(col, column_defaults.get(col, ""))
            if self._is_missing_nwb_unit_metadata_value(value):
                value = column_defaults.get(col, "")
            completed[col] = self._clean_nwb_unit_metadata_value(value)
        return completed

    def _delete_broken_nwb_units_group_h5(self, path):
        """Remove a malformed raw /units group that PyNWB cannot construct.

        This is a recovery path for files containing a root /units group without
        the NWB/HDMF neurodata_type metadata.  A small backup is created once
        before deleting the raw group.
        """
        path = Path(path)
        try:
            import h5py
        except Exception as exc:
            raise RuntimeError(f"h5py is required to repair broken /units in {path.name}: {exc}") from exc

        backup_path = path.with_suffix(".before_units_repair.nwb")
        try:
            if not backup_path.exists():
                import shutil
                shutil.copy2(str(path), str(backup_path))
        except Exception as exc:
            self._append_log(f"Could not create NWB repair backup for {path.name}: {exc}")

        with h5py.File(str(path), "r+") as h5:
            if "units" in h5:
                del h5["units"]

    def _read_nwb_units_for_rewrite(self, path):
        """Read the current /units table into plain Python rows for selective replacement."""
        try:
            from pynwb import NWBHDF5IO
        except Exception as exc:
            raise RuntimeError(f"pynwb is required to rewrite NWB Units: {exc}") from exc

        rows = []
        column_descriptions = {}
        path = Path(path)
        self._prepare_nwb_file_for_write(path)

        io = None
        try:
            io = NWBHDF5IO(str(path), mode="r")
            try:
                nwbfile = io.read()
            except ValueError as exc:
                if "No data_type found for builder root/units" not in str(exc):
                    raise
                try:
                    io.close()
                except Exception:
                    pass
                io = None
                self._append_log(f"Broken /units table detected in {path.name}; removing /units and retrying.")
                self._delete_broken_nwb_units_group_h5(path)
                io = NWBHDF5IO(str(path), mode="r")
                nwbfile = io.read()

            units = getattr(nwbfile, "units", None)
            if units is None:
                return rows, column_descriptions
            try:
                df = units.to_dataframe().copy()
            except Exception:
                return rows, column_descriptions
            if df is None or df.empty:
                return rows, column_descriptions

            skip_columns = {"spike_times"}
            known_descriptions = {
                "sorter_unit_id": "Original unit id from the SpikeInterface sorting.",
                "sorting_project": "Spike-sorting project/folder name that produced this unit.",
                "spike_sorting_run": "Spike-sorting run identifier from the NWB GUI.",
                "sorter_name": "Spike sorter used to generate the units.",
                "source_analyzer_folder": "Source SortingAnalyzer folder used for this export.",
            }
            for col in list(getattr(units, "colnames", []) or []):
                col = str(col)
                if col in skip_columns or col not in df.columns:
                    continue
                desc = known_descriptions.get(col, "Copied from the previous NWB Units table.")
                try:
                    col_obj = units[col]
                    desc = str(getattr(col_obj, "description", desc) or desc)
                except Exception:
                    pass
                column_descriptions[col] = desc

            for unit_id, row in df.iterrows():
                try:
                    spike_times = np.asarray(row.get("spike_times", []), dtype=float).reshape(-1)
                except Exception:
                    spike_times = np.array([], dtype=float)
                metadata = {}
                for col in column_descriptions:
                    if col in df.columns:
                        metadata[col] = self._clean_nwb_unit_metadata_value(row.get(col, ""))
                rows.append({
                    "id": self._clean_nwb_unit_metadata_value(unit_id),
                    "spike_times": spike_times[np.isfinite(spike_times)] if spike_times.size else spike_times,
                    "metadata": metadata,
                })
        finally:
            if io is not None:
                try:
                    io.close()
                except Exception:
                    pass
        return rows, column_descriptions

    def _rewrite_nwb_units_rows(self, path, rows, column_descriptions):
        """Replace /units with the provided rows, preserving other exported unit labels."""
        path = Path(path)
        try:
            from pynwb import NWBHDF5IO
        except Exception as exc:
            raise RuntimeError(f"pynwb is required to rewrite NWB Units: {exc}") from exc

        self._remove_existing_nwb_units_table(path)
        rows = list(rows or [])
        if not rows:
            return

        self._prepare_nwb_file_for_write(path)
        with NWBHDF5IO(str(path), mode="r+") as io:
            nwbfile = io.read()
            for name, description in dict(column_descriptions or {}).items():
                try:
                    if getattr(nwbfile, "units", None) is not None and name in (getattr(nwbfile.units, "colnames", []) or []):
                        continue
                    nwbfile.add_unit_column(name=name, description=str(description or "Copied from the previous NWB Units table."))
                except ValueError:
                    pass
                except Exception as exc:
                    self._append_log(f"Could not preserve NWB Units column {name}: {exc}")

            column_names = list(dict(column_descriptions or {}).keys())
            column_defaults = {
                col: self._default_nwb_unit_metadata_value_for_column(rows, col)
                for col in column_names
            }

            for payload in rows:
                metadata = self._complete_nwb_unit_metadata(
                    payload.get("metadata", {}) or {},
                    column_names,
                    column_defaults,
                )
                try:
                    nwbfile.add_unit(
                        id=payload.get("id"),
                        spike_times=np.sort(np.asarray(payload.get("spike_times", []), dtype=float)),
                        **metadata,
                    )
                except Exception as exc:
                    self._append_log(f"Could not preserve existing NWB unit {payload.get('id')}: {exc}")
            io.write(nwbfile)

    def _remove_existing_nwb_units_with_sorting_project(self, path, sorting_project):
        """Remove only rows whose sorting_project matches ``sorting_project``."""
        label = str(sorting_project or "").strip()
        if not label:
            return 0
        path = Path(path)
        rows, column_descriptions = self._read_nwb_units_for_rewrite(path)
        if not rows:
            return 0

        label_norm = label.lower()
        label_segment_norm = self._unit_project_path_segment(label).lower()
        kept = []
        removed = 0
        for payload in rows:
            metadata = dict(payload.get("metadata", {}) or {})
            project = str(metadata.get("sorting_project", "") or "").strip()
            project_norm = project.lower()
            project_segment_norm = self._unit_project_path_segment(project).lower() if project else ""
            if project_norm in {label_norm, label_segment_norm} or project_segment_norm in {label_norm, label_segment_norm}:
                removed += 1
            else:
                kept.append(payload)

        if removed:
            self._rewrite_nwb_units_rows(path, kept, column_descriptions)
        return int(removed)

    def _gui_instance_for_inventory_update(self):
        """Return the Qt GUI instance that owns nwb_inventory.xlsx, when available."""
        try:
            gui = self.sorting_info.get("GUI_instance", None)
            if gui is not None:
                return gui
        except Exception:
            pass

        try:
            obj = self
            seen = set()
            while obj is not None and id(obj) not in seen:
                seen.add(id(obj))
                if hasattr(obj, "NWB_inventory_path"):
                    return obj
                try:
                    obj = obj.parent()
                except Exception:
                    obj = None
        except Exception:
            pass
        return None

    def _inventory_path_for_units_update(self):
        """Resolve the project nwb_inventory.xlsx path used by the GUI."""
        gui = self._gui_instance_for_inventory_update()
        if gui is not None:
            try:
                inv_path = getattr(gui, "NWB_inventory_path", None)
                if inv_path:
                    return Path(inv_path)
            except Exception:
                pass

        try:
            inv_path = self.sorting_info.get("NWB_inventory_path", None)
            if inv_path:
                return Path(inv_path)
        except Exception:
            pass

        try:
            root = self.sorting_info.get("project_root", None)
            if root:
                candidate = Path(root) / "Data" / "nwb_inventory.xlsx"
                if candidate.exists():
                    return candidate
        except Exception:
            pass
        return None

    def _sync_gui_inventory_after_units_update(self, inv_df, content_df, sheets=None):
        """Refresh in-memory inventory tables so analysis sees newly exported units immediately."""
        gui = self._gui_instance_for_inventory_update()
        if gui is None:
            return

        try:
            gui.content_df = content_df.copy(deep=True)
            gui._content_df_master = content_df.copy(deep=True)
        except Exception:
            try:
                gui.content_df = content_df
                gui._content_df_master = content_df
            except Exception:
                pass

        try:
            gui.full_df = inv_df.copy(deep=True)
            gui._df_master = inv_df.copy(deep=True)
            if not getattr(gui, "current_file_filters", None):
                gui.df = inv_df.copy(deep=True)
        except Exception:
            try:
                gui.df = inv_df
                gui.full_df = inv_df
            except Exception:
                pass

        try:
            if sheets and "trial_filter_catalog" in sheets:
                gui.trial_filter_catalog_df = sheets["trial_filter_catalog"].copy(deep=True)
        except Exception:
            pass
        try:
            if sheets and "trial_filter_index" in sheets:
                gui.trial_filter_index_df = sheets["trial_filter_index"].copy(deep=True)
        except Exception:
            pass

        for panel_name in ("analysis_panel", "processing_panel"):
            try:
                panel = getattr(gui, panel_name, None)
                if panel is None:
                    continue
                panel.NWB_content_df = content_df.copy(deep=True)
                panel.NWB_inventory_df = inv_df.copy(deep=True)
            except Exception:
                pass

        try:
            filter_panel = getattr(gui, "filter_panel", None)
            if filter_panel is not None and callable(getattr(filter_panel, "rebuild_from_inventory", None)):
                filter_panel.rebuild_from_inventory()
        except Exception:
            pass

    def _update_inventory_after_units_export(self, unit_counts_by_file):
        """Update content_index after writing Units to NWB, without requiring a full rescan.

        unit_counts_by_file maps source NWB path -> either:
          - total number of units, for old callers, or
          - {sorting_project: unit_count}, for the new project-aware Units export.
        """
        if not unit_counts_by_file:
            return []

        inv_path = self._inventory_path_for_units_update()
        if inv_path is None:
            return ["Inventory was not updated: could not resolve nwb_inventory.xlsx."]
        inv_path = Path(inv_path)
        if not inv_path.exists():
            return [f"Inventory was not updated: file not found: {inv_path}"]

        try:
            import pandas as pd
        except Exception as exc:
            return [f"Inventory was not updated: pandas is required ({exc})."]

        try:
            sheets = {}
            try:
                xl = pd.ExcelFile(inv_path)
                try:
                    sheet_names = list(xl.sheet_names)
                    for sheet_name in sheet_names:
                        sheets[sheet_name] = _pp_read_excel(xl, sheet_name=sheet_name)
                finally:
                    try:
                        xl.close()
                    except Exception:
                        pass
            except Exception:
                sheets = {}

            inv_df = sheets.get("inventory")
            if inv_df is None:
                gui = self._gui_instance_for_inventory_update()
                inv_df = getattr(gui, "full_df", None) if gui is not None else None
            if inv_df is None or not isinstance(inv_df, pd.DataFrame):
                inv_df = pd.DataFrame()
            else:
                inv_df = inv_df.copy()

            content_df = sheets.get("content_index")
            if content_df is None:
                gui = self._gui_instance_for_inventory_update()
                content_df = getattr(gui, "content_df", None) if gui is not None else None
            if content_df is None or not isinstance(content_df, pd.DataFrame):
                content_df = pd.DataFrame(columns=["file_path", "nwb_path", "container", "module", "name", "neurodata_type", "sorting_project"])
            else:
                content_df = content_df.copy()

            for col in ["file_path", "nwb_path", "container", "module", "name", "neurodata_type", "sorting_project"]:
                if col not in content_df.columns:
                    content_df[col] = None
            if "file_path" not in inv_df.columns:
                inv_df["file_path"] = None

            def _norm_for_inventory(value):
                try:
                    return self._norm_write_path(value)
                except Exception:
                    try:
                        return os.path.normcase(os.path.abspath(str(value)))
                    except Exception:
                        return str(value)

            def _normalise_project_counts(value):
                if isinstance(value, dict):
                    out = {}
                    for project, count in value.items():
                        try:
                            n = int(count or 0)
                        except Exception:
                            n = 0
                        if n > 0:
                            out[str(project or "").strip()] = n
                    return out
                try:
                    n = int(value or 0)
                except Exception:
                    n = 0
                return {"": n} if n > 0 else {}

            # Remove stale Units rows for every exported file, then add one Units row
            # per sorting_project.  This lets the analysis panel offer entries such
            # as "units (Fixed)" and "units (Variable)" for the same NWB file.
            new_rows = []
            normalized_counts = {}
            for file_path, counts in unit_counts_by_file.items():
                if not str(file_path or "").strip():
                    continue
                project_counts = _normalise_project_counts(counts)
                total_units = int(sum(project_counts.values()))
                file_text = str(Path(file_path))
                normalized_counts[_norm_for_inventory(file_text)] = total_units

                path_norm = _norm_for_inventory(file_text)
                file_mask = content_df["file_path"].map(_norm_for_inventory) == path_norm
                units_mask = (
                    content_df["nwb_path"].astype(str).str.lower().str.startswith("units")
                    | content_df["container"].astype(str).str.lower().eq("units")
                    | content_df["name"].astype(str).str.lower().eq("units")
                )
                content_df = content_df.loc[~(file_mask & units_mask)].copy()

                for project, n_units in sorted(project_counts.items(), key=lambda kv: _natural_text_key(str(kv[0]))):
                    project_text = str(project or "").strip()
                    if project_text:
                        segment = self._unit_project_path_segment(project_text)
                        nwb_path = f"units/{segment}"
                        module = project_text
                    else:
                        nwb_path = "units"
                        module = ""
                    new_rows.append({
                        "file_path": file_text,
                        "nwb_path": nwb_path,
                        "container": "units",
                        "module": module,
                        "name": "units",
                        "neurodata_type": "Units",
                        "sorting_project": project_text,
                    })

            if new_rows:
                content_df = pd.concat([content_df, pd.DataFrame(new_rows)], ignore_index=True)

            content_df = content_df.drop_duplicates()
            dedup_cols = [c for c in ["file_path", "nwb_path", "container", "module", "name", "sorting_project"] if c in content_df.columns]
            if dedup_cols:
                content_df = content_df.drop_duplicates(subset=dedup_cols, keep="last")
            content_df = content_df.reset_index(drop=True)

            # Update unit counts only if the inventory already has such a column.
            # The analysis only needs content_index, so avoid changing the schema
            # unnecessarily.
            unit_count_cols = [c for c in ("number_of_units", "n_units") if c in inv_df.columns]
            if unit_count_cols:
                inv_norm = inv_df["file_path"].map(_norm_for_inventory)
                for norm_path, n_units in normalized_counts.items():
                    idx = inv_norm == norm_path
                    for col in unit_count_cols:
                        inv_df.loc[idx, col] = int(n_units)

            meta_df = sheets.get("meta")
            if meta_df is None or not isinstance(meta_df, pd.DataFrame) or meta_df.empty:
                meta_df = pd.DataFrame([{"updated_at": datetime.now(), "units_inventory_updated_at": datetime.now()}])
            else:
                meta_df = meta_df.copy()
                if "updated_at" not in meta_df.columns:
                    meta_df["updated_at"] = None
                if "units_inventory_updated_at" not in meta_df.columns:
                    meta_df["units_inventory_updated_at"] = None
                meta_df.loc[0, "updated_at"] = datetime.now()
                meta_df.loc[0, "units_inventory_updated_at"] = datetime.now()
            sheets["inventory"] = inv_df
            sheets["content_index"] = content_df
            sheets["meta"] = meta_df

            # Preserve every existing sheet while replacing inventory/content_index/meta.
            sheet_order = list(sheets.keys())
            for required in ("inventory", "content_index", "meta"):
                if required not in sheet_order:
                    sheet_order.append(required)
            with pd.ExcelWriter(inv_path, engine="openpyxl", mode="w") as writer:
                for sheet_name in sheet_order:
                    df = sheets.get(sheet_name)
                    if df is None or not isinstance(df, pd.DataFrame):
                        continue
                    df.to_excel(writer, sheet_name=sheet_name, index=False)

            self._sync_gui_inventory_after_units_update(inv_df, content_df, sheets=sheets)

            updated = len([c for c in normalized_counts.values() if int(c or 0) > 0])
            removed = len([c for c in normalized_counts.values() if int(c or 0) <= 0])
            project_rows = len(new_rows)
            details = []
            if updated:
                details.append(f"{updated} file(s) refreshed")
            if project_rows:
                details.append(f"{project_rows} project-specific Units row(s) added")
            if removed:
                details.append(f"{removed} stale Units content row(s) removed")
            if not details:
                details.append("no Units content rows changed")
            return [f"Updated NWB inventory after Units export: {inv_path} ({', '.join(details)})."]
        except Exception as exc:
            return [f"Inventory was not updated after Units export: {exc}"]

    def _write_units_to_source_nwb_files(
        self,
        info,
        spikes_by_file,
        variant,
        analyzer_folder=None,
        *,
        replace_existing=True,
        replace_same_label=False,
        sorting_project_label=None,
    ):
        if not spikes_by_file:
            return ["No source-file mapped spikes were available; no NWB Units table was written."]

        run_id = str(info.get("run_signature_hash", "") or info.get("run_name", "") or Path(info.get("output_folder", "run")).name)
        sorter_name = str(info.get("sorter", ""))
        sorting_project = str(sorting_project_label or "").strip() or self._sorting_project_name_for_export(info)
        analyzer_folder_text = str(analyzer_folder or (variant or {}).get("folder", "") or "")
        summary_lines = []
        inventory_unit_counts = {}

        column_descriptions_base = {
            "sorter_unit_id": "Original unit id from the SpikeInterface sorting.",
            "sorting_project": "Spike-sorting project/folder name that produced this unit.",
            "spike_sorting_run": "Spike-sorting run identifier from the NWB GUI.",
            "sorter_name": "Spike sorter used to generate the units.",
            "source_analyzer_folder": "Source SortingAnalyzer folder used for this export.",
        }

        for file_path, unit_map in sorted(spikes_by_file.items(), key=lambda kv: str(kv[0])):
            if not file_path or not unit_map:
                continue
            path = Path(file_path)
            if not path.exists():
                summary_lines.append(f"NWB not found, skipped: {path}")
                continue

            existing_rows, column_descriptions = self._read_nwb_units_for_rewrite(path)
            column_descriptions = dict(column_descriptions or {})
            column_descriptions.update({k: v for k, v in column_descriptions_base.items() if k not in column_descriptions})

            removed_same_label_count = 0
            removed_all_count = 0
            kept_rows = list(existing_rows or [])

            if replace_existing:
                removed_all_count = len(kept_rows)
                kept_rows = []
            elif replace_same_label:
                label_norm = sorting_project.lower()
                label_segment_norm = self._unit_project_path_segment(sorting_project).lower()
                filtered_rows = []
                for payload in kept_rows:
                    metadata = dict(payload.get("metadata", {}) or {})
                    project = str(metadata.get("sorting_project", "") or "").strip()
                    project_norm = project.lower()
                    project_segment_norm = self._unit_project_path_segment(project).lower() if project else ""
                    if project_norm in {label_norm, label_segment_norm} or project_segment_norm in {label_norm, label_segment_norm}:
                        removed_same_label_count += 1
                    else:
                        filtered_rows.append(payload)
                kept_rows = filtered_rows

            existing_ids = set()
            for payload in kept_rows:
                try:
                    existing_ids.add(payload.get("id"))
                except Exception:
                    pass

            new_rows = []
            for unit_key, payload in sorted(unit_map.items(), key=lambda kv: _natural_text_key(str(kv[0]))):
                spike_times = np.asarray(payload.get("spike_times", []), dtype=float).reshape(-1)
                spike_times = spike_times[np.isfinite(spike_times)]
                if spike_times.size == 0:
                    continue
                sorter_unit_text = str(payload.get("unit_id", unit_key))
                nwb_unit_id = self._nwb_unit_id_for_sorter_unit(sorter_unit_text, existing_ids)
                existing_ids.add(nwb_unit_id)
                new_rows.append({
                    "id": nwb_unit_id,
                    "spike_times": np.sort(spike_times),
                    "metadata": {
                        "sorter_unit_id": sorter_unit_text,
                        "sorting_project": sorting_project,
                        "spike_sorting_run": run_id,
                        "sorter_name": sorter_name,
                        "source_analyzer_folder": analyzer_folder_text,
                    },
                })

            final_rows = kept_rows + new_rows
            added = len(new_rows)

            # Do not append to an existing /units table. Some existing NWB files store
            # /units as non-chunked HDF5 datasets, and PyNWB cannot resize those.
            # Rewriting the complete table avoids "Only chunked datasets can be resized"
            # while still preserving units from other sorting_project labels.
            self._rewrite_nwb_units_rows(path, final_rows, column_descriptions)
            project_counts = self._nwb_unit_counts_by_sorting_project_from_rows(final_rows)

            if replace_existing:
                if removed_all_count:
                    action = f"rewrote table after removing {removed_all_count} existing unit(s)"
                else:
                    action = "wrote fresh table"
            elif replace_same_label:
                if removed_same_label_count:
                    action = f"rewrote table after replacing {removed_same_label_count} existing unit(s) with the same label"
                else:
                    action = "rewrote table with same-label replacement enabled"
            elif existing_rows:
                action = "rewrote table and preserved existing unit(s)"
            else:
                action = "wrote fresh table"

            summary_lines.append(f"NWB Units {action}: {path.name}, added {added} unit(s) for sorting project '{sorting_project}'.")
            inventory_unit_counts[str(path)] = project_counts if project_counts is not None else {sorting_project: int(added)}

        if inventory_unit_counts:
            summary_lines.extend(self._update_inventory_after_units_export(inventory_unit_counts))

        if not summary_lines:
            summary_lines.append("No NWB Units table was written.")
        return summary_lines

    def show_selected_curation_details(self):
        info = self._selected_row_info()
        if info is None:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Curation details / validation")
        dialog.resize(950, 720)
        vbox = QtWidgets.QVBoxLayout(dialog)
        vbox.setContentsMargins(12, 12, 12, 12)
        vbox.setSpacing(8)
        title = QtWidgets.QLabel("Curation details / validation", dialog)
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        vbox.addWidget(title)

        table = QtWidgets.QTableWidget(dialog)
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Field", "Value"])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers if hasattr(QtWidgets.QAbstractItemView, "EditTrigger") else QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior") else QtWidgets.QAbstractItemView.SelectRows)
        rows = [
            ("Recording", info.get("display_recording", info.get("recording", ""))),
            ("Sorter", info.get("sorter", "")),
            ("Run", info.get("run_name", "")),
            ("Units", info.get("unit_count", "")),
            ("Preprocessing", info.get("preprocessing_pipeline_id", "")),
            ("Sorter params", info.get("sorter_params_id", "")),
            ("Run status", info.get("run_status", "")),
            ("Curated", "Yes" if info.get("has_curated") else "No"),
            ("Curated units", info.get("curated_unit_count", "")),
            ("Curated analyzer", info.get("curated_folder", "")),
            ("Curated unit summary", info.get("curated_summary_folder", "")),
            ("Validation report", info.get("curated_validation_report", "")),
            ("Run folder", info.get("output_folder", "")),
        ]
        table.setRowCount(len(rows))
        for r, (key, value) in enumerate(rows):
            table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(key)))
            table.setItem(r, 1, QtWidgets.QTableWidgetItem(str(value)))
        try:
            table.horizontalHeader().setStretchLastSection(True)
            table.resizeColumnsToContents()
        except Exception:
            pass
        vbox.addWidget(table, 1)

        report_text = QtWidgets.QPlainTextEdit(dialog)
        report_text.setReadOnly(True)
        report_text.setMinimumHeight(260)
        validation_path = info.get("curated_validation_report", "")
        report_payload = None
        if validation_path:
            try:
                vp = Path(validation_path)
                if vp.exists():
                    report_payload = _load_json_file(vp)
            except Exception as exc:
                report_payload = {"error": str(exc)}
        if report_payload is None:
            if info.get("sorting_in_progress"):
                report_text.setPlainText("Sorting is still in progress. No curation validation report is available yet.")
            elif info.get("sorting_failed"):
                report_text.setPlainText("Sorting failed. No curation validation report is available.")
            elif not info.get("has_curated"):
                report_text.setPlainText("No curation has been applied to this run yet.")
            else:
                report_text.setPlainText("No validation report was found for the saved curated analyzer.")
        else:
            try:
                report_text.setPlainText(json.dumps(_json_safe(report_payload), indent=2, ensure_ascii=False))
            except Exception:
                report_text.setPlainText(str(report_payload))
        vbox.addWidget(report_text, 1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close if hasattr(QtWidgets.QDialogButtonBox, "StandardButton") else QtWidgets.QDialogButtonBox.Close, dialog)
        buttons.rejected.connect(dialog.close)
        try:
            buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).clicked.connect(dialog.close)
        except Exception:
            buttons.accepted.connect(dialog.close)
        vbox.addWidget(buttons)
        self._show_modeless_param_dialog(dialog)

    def apply_selected_curation(self):
        info = self._selected_row_info()
        if info is None:
            return
        if info.get("sorting_in_progress"):
            QtWidgets.QMessageBox.information(self, "Apply curation", "This sorting run is still in progress. Wait until it finishes before applying curation.")
            return
        msg = QtWidgets.QMessageBox(self)
        msg.setWindowTitle("Apply curation")
        msg.setIcon(QtWidgets.QMessageBox.Icon.Question if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Question)
        msg.setText("Choose which finished curation to import for this sorting run.")
        msg.setInformativeText(
            f"Recording: {info.get('display_recording', info.get('recording', ''))}\n"
            f"Sorter: {info.get('sorter', '')}\n"
            f"Preprocessing: {info.get('preprocessing_pipeline_id', 'Pipeline ?')}\n"
            f"Sorter params: {info.get('sorter_params_id', 'Pipeline ?')}\n\n"
            "One curated SortingAnalyzer folder will be written in the selected run folder. "
            "If one already exists, you will be asked whether to overwrite it. "
            "The original SorterOutput folder is not overwritten. "
            "For SpikeInterface GUI, the saved curation_data.json inside the analyzer folder is used automatically when available."
        )
        role = QtWidgets.QMessageBox.ButtonRole.AcceptRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.AcceptRole
        action_role = QtWidgets.QMessageBox.ButtonRole.ActionRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.ActionRole
        reject_role = QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole
        phy_btn = msg.addButton("Apply Phy curation", role)
        si_btn = msg.addButton("Apply SpikeInterface GUI saved curation", action_role)
        cancel_btn = msg.addButton("Cancel", reject_role)
        msg.setDefaultButton(phy_btn)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is cancel_btn:
            return
        self._append_log("")
        self._append_log("Applying curation...")
        self._set_busy(True, "Curation: applying curation...")
        QtWidgets.QApplication.processEvents()
        try:
            if clicked is phy_btn:
                self._apply_phy_curation_for_selected(info)
            elif clicked is si_btn:
                self._apply_spikeinterface_json_curation_for_selected(info, prefer_saved=True)
            else:
                return
            self._append_log("Curation applied successfully.")
            self.refresh_outputs()
        except Exception as exc:
            self._append_log("ERROR while applying curation:")
            self._append_log(traceback.format_exc())
            QtWidgets.QMessageBox.warning(
                self,
                "Apply curation",
                f"Could not apply curation. See the curation log for details.\n\n{exc}",
            )
        finally:
            self._set_busy(False, "Curation ready.")

    def _phy_folder_for_info(self, info):
        try:
            output_folder = Path(info.get("output_folder"))
        except Exception:
            return None
        return output_folder / "phy"

    def _ensure_phy_export_for_selected(self, analyzer, info):
        phy_folder = self._phy_folder_for_info(info)
        if phy_folder is None:
            raise RuntimeError("Could not determine the Phy output folder for the selected run.")
        params_file = phy_folder / "params.py"
        if params_file.exists():
            _pp_phy(params_file)
            self._append_log(f"Using existing Phy export: {phy_folder}")
            return phy_folder, params_file

        if phy_folder.exists() and any(phy_folder.iterdir()):
            msg = QtWidgets.QMessageBox(self)
            msg.setWindowTitle("Export to Phy")
            msg.setIcon(QtWidgets.QMessageBox.Icon.Warning if hasattr(QtWidgets.QMessageBox, "Icon") else QtWidgets.QMessageBox.Warning)
            msg.setText("A Phy folder already exists, but params.py was not found.")
            msg.setInformativeText(f"Delete and recreate this folder?\n\n{phy_folder}")
            yes = msg.addButton("Delete and recreate", QtWidgets.QMessageBox.ButtonRole.DestructiveRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.DestructiveRole)
            no = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole if hasattr(QtWidgets.QMessageBox, "ButtonRole") else QtWidgets.QMessageBox.RejectRole)
            msg.setDefaultButton(no)
            msg.exec()
            if msg.clickedButton() is not yes:
                raise RuntimeError("Phy export cancelled.")
            shutil.rmtree(phy_folder)

        self._append_log(f"Exporting selected SortingAnalyzer to Phy: {phy_folder}")
        QtWidgets.QApplication.processEvents()
        try:
            from spikeinterface.exporters import export_to_phy
        except Exception as exc:
            raise RuntimeError(
                "Could not import spikeinterface.exporters.export_to_phy. "
                "Update SpikeInterface or install the exporters module."
            ) from exc

        stream = _QtAppendLogStream(self._append_log)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                try:
                    export_to_phy(analyzer, output_folder=str(phy_folder), remove_if_exists=False, verbose=True)
                except TypeError:
                    export_to_phy(sorting_analyzer=analyzer, output_folder=str(phy_folder), remove_if_exists=False, verbose=True)
        finally:
            stream.flush()

        if not params_file.exists():
            raise RuntimeError(f"Phy export finished, but params.py was not found:\n{params_file}")
        _pp_phy(params_file)
        self._append_log("Phy export complete.")
        return phy_folder, params_file

    def _open_selected_in_phy(self):
        info = self._selected_row_info()
        if info is None:
            return
        self._set_busy(True, "Curation: preparing Phy...")
        QtWidgets.QApplication.processEvents()
        try:
            variant = getattr(self, "_pending_analyzer_variant", None) or self._original_analyzer_variant_for_row(info)
            folder = None
            try:
                folder = Path(variant.get("folder"))
            except Exception:
                folder = None
            if self.current_analyzer is None or (folder is not None and Path(getattr(self, "current_analyzer_folder", "")) != folder):
                self.load_selected_analyzer(variant=variant)
            analyzer = self.current_analyzer
            if analyzer is None:
                return

            self._append_log("")
            self._append_log("Preparing Phy curation...")
            QtWidgets.QApplication.processEvents()
            phy_folder, params_file = self._ensure_phy_export_for_selected(analyzer, info)
            phy_exe = shutil.which("phy") or shutil.which("phy.exe")
            if not phy_exe:
                raise RuntimeError(
                    "The 'phy' executable was not found in PATH. Install Phy in the current environment first.\n\n"
                    f"The Phy export is ready here:\n{phy_folder}"
                )
            self._append_log(f"Opening Phy: {phy_exe} template-gui {params_file}")
            proc = subprocess.Popen([phy_exe, "template-gui", str(params_file)], cwd=str(phy_folder))
            self._phy_processes.append(proc)
            self._append_log("Phy was launched. Curation changes will be saved in the Phy folder.")
            self._append_log("When Phy closes, NWB Workbench will ask whether to apply the curation to this run.")
            self._monitor_phy_process_for_apply_prompt(proc, dict(info))
        except Exception:
            self._append_log("ERROR while opening Phy:")
            self._append_log(traceback.format_exc())
            QtWidgets.QMessageBox.warning(
                self,
                "Phy",
                "Could not open Phy. See the curation log for details.",
            )
        finally:
            self._set_busy(False, "Curation ready.")


# DELIVERY_BUILD_ID: SSRV-20260812-01
