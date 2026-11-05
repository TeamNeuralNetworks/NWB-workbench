from __future__ import annotations

import contextlib
import hashlib
import importlib
import inspect
import io
import json
import logging
import sys
import threading
import time
import traceback
import weakref
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional, Any

from Tools.General.AnalysisResult import AnalysisResult
from Tools.General.filter_data import get_filtered_data



def _emit(cb: Optional[Callable[[str], None]], text: str):
    if callable(cb):
        try:
            cb(str(text))
        except Exception:
            pass


def _emit_progress(cb: Optional[Callable[[dict], None]], **payload):
    if callable(cb):
        try:
            cb(dict(payload))
        except Exception:
            pass


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _event_alignment_offsets(event_alignment) -> tuple[float, float]:
    from Tools.General.event_alignment import normalize_alignment
    a = normalize_alignment(event_alignment)
    return a["start_offset_s"], a["end_offset_s"]


def _short_path(path: Any) -> str:
    try:
        p = Path(str(path))
        return p.name or str(path)
    except Exception:
        return str(path)


def _condition_items(condition_selection: dict) -> list[tuple[str, dict]]:
    items: list[tuple[str, dict]] = []
    if not isinstance(condition_selection, dict):
        return items
    for name, block in condition_selection.items():
        if name == "_conditions_name":
            continue
        if not isinstance(block, dict):
            block = {}
        items.append((str(name), block))
    return items


def _filter_summary(block: dict) -> str:
    trial_filters = block.get("trial_filter_dict", {}) or {}
    file_filters = block.get("file_filter_dict", {}) or {}
    parts = []
    if isinstance(file_filters, dict) and file_filters:
        parts.append(f"file filters={len(file_filters)}")
    if isinstance(trial_filters, dict) and trial_filters:
        parts.append(f"trial filters={len(trial_filters)}")
    return ", ".join(parts) if parts else "no saved filters"


def _build_extraction_tasks(transfer) -> list[dict]:
    data_names = [str(x).strip() for x in _as_list(getattr(transfer, "data_type", [])) if str(x).strip()]
    data_paths = [str(x).strip() for x in _as_list(getattr(transfer, "data_path", [])) if str(x).strip()]
    if not data_names:
        data_names = [""]
    if not data_paths:
        data_paths = [""] * len(data_names)
    if len(data_paths) < len(data_names):
        data_paths.extend([""] * (len(data_names) - len(data_paths)))

    conditions = _condition_items(getattr(transfer, "condition_selection", {}) or {})
    if not conditions:
        files = [str(x) for x in _as_list(getattr(transfer, "selected_files", [])) if str(x).strip()]
        conditions = [("selected_files", {"file_path_list": files})]

    tasks: list[dict] = []
    for condition_name, block in conditions:
        files = [str(x) for x in _as_list(block.get("file_path_list", [])) if str(x).strip()]
        if not files:
            files = [str(x) for x in _as_list(getattr(transfer, "selected_files", [])) if str(x).strip()]
        for file_path in files:
            for idx, data_name in enumerate(data_names):
                tasks.append(
                    {
                        "condition": condition_name,
                        "file_path": file_path,
                        "file_name": _short_path(file_path),
                        "data_type": data_name,
                        "data_path": data_paths[idx] if idx < len(data_paths) else "",
                        "filter_summary": _filter_summary(block),
                    }
                )
    return tasks


def _analysis_script_metadata(analysis_spec: dict) -> dict:
    """Return the exact analysis script location and a reproducibility hash."""
    analysis_spec = analysis_spec or {}
    func = analysis_spec.get("func")
    source = ""
    if callable(func):
        try:
            source = str(inspect.getsourcefile(func) or inspect.getfile(func) or "")
        except Exception:
            source = ""

    module_value = str(analysis_spec.get("module", "") or "").strip()
    if not source and module_value and (
        module_value.lower().endswith(".py")
        or "/" in module_value
        or "\\" in module_value
    ):
        source = module_value

    if source:
        try:
            source_path = Path(source).expanduser().resolve()
            script_path = str(source_path)
        except Exception:
            source_path = Path(source)
            script_path = source
        script_name = source_path.name or source
    elif module_value:
        module_leaf = module_value.rsplit(".", 1)[-1]
        script_name = module_leaf if module_leaf.lower().endswith(".py") else f"{module_leaf}.py"
        script_path = module_value
        source_path = None
    else:
        script_name = "Unknown"
        script_path = "Unknown"
        source_path = None

    script_sha256 = "Unavailable"
    if source_path is not None:
        try:
            if source_path.is_file():
                script_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except Exception:
            pass

    return {
        "script_name": script_name,
        "script_path": script_path,
        "script_sha256": script_sha256,
    }


def _pretty_log_value(value) -> str:
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _indented_lines(value, indent="  ") -> list[str]:
    return [f"{indent}{line}" for line in _pretty_log_value(value).splitlines()]


def _build_analysis_text_log(
    *,
    transfer,
    analysis_spec: dict,
    params: dict,
    started_at: str,
    finished_at: str,
    duration_s: float,
    output_dir,
    gui_log_lines: list[str],
) -> str:
    """Build a human-readable reproducibility header followed by the GUI log."""
    name = str(analysis_spec.get("name", getattr(transfer, "analysis_name", "Analysis")))
    script = _analysis_script_metadata(analysis_spec)
    condition_items = _condition_items(getattr(transfer, "condition_selection", {}) or {})
    data_names = [
        str(value).strip()
        for value in _as_list(getattr(transfer, "data_type", []))
        if str(value).strip()
    ]
    data_paths = [
        str(value).strip()
        for value in _as_list(getattr(transfer, "data_path", []))
    ]

    lines = [
        "ANALYSIS RUN",
        "============",
        f"Script used: {script['script_name']}",
        f"Script location: {script['script_path']}",
        f"Script SHA-256: {script['script_sha256']}",
        f"Analysis: {name}",
        f"Started: {started_at}",
        f"Finished: {finished_at}",
        f"Duration: {duration_s:.3f} s",
        "Status: Completed",
        f"Output folder: {output_dir}",
        "",
        "PARAMETERS",
        "----------",
    ]
    lines.extend(_pretty_log_value(dict(params or {})).splitlines())

    lines.extend(["", "CONDITIONS AND FILES", "--------------------"])
    if not condition_items:
        selected_files = [
            str(value)
            for value in _as_list(getattr(transfer, "selected_files", []))
            if str(value).strip()
        ]
        lines.append("Condition: selected_files")
        lines.append(f"  Files ({len(selected_files)}):")
        lines.extend(f"    - {path}" for path in selected_files)
    else:
        for condition_index, (condition_name, block) in enumerate(condition_items, start=1):
            files = [
                str(value)
                for value in _as_list(block.get("file_path_list", []))
                if str(value).strip()
            ]
            if not files:
                files = [
                    str(value)
                    for value in _as_list(getattr(transfer, "selected_files", []))
                    if str(value).strip()
                ]
            lines.append(f"Condition {condition_index}: {condition_name}")
            color = str(block.get("color", "") or "").strip()
            if color:
                lines.append(f"  Color: {color}")
            lines.append("  File filters:")
            lines.extend(_indented_lines(block.get("file_filter_dict", {}) or {}, "    "))
            lines.append("  Trial filters:")
            lines.extend(_indented_lines(block.get("trial_filter_dict", {}) or {}, "    "))
            lines.append(f"  Files ({len(files)}):")
            lines.extend(f"    - {path}" for path in files)
            if condition_index < len(condition_items):
                lines.append("")

    lines.extend(["", "INPUT DATA", "----------"])
    if data_names:
        for index, data_name in enumerate(data_names):
            data_path = data_paths[index] if index < len(data_paths) else ""
            lines.append(f"- Name: {data_name}")
            lines.append(f"  NWB path: {data_path or 'Not specified'}")
    else:
        lines.append("None")

    lines.extend(["", "ALIGNMENT", "---------"])
    if getattr(transfer, "align_to_event", False):
        alignment = dict(getattr(transfer, "event_alignment", {}) or {})
        start_offset, end_offset = _event_alignment_offsets(alignment)
        alignment["start_offset_s"] = start_offset
        alignment["end_offset_s"] = end_offset
        alignment.pop("pad_before_s", None)
        alignment.pop("pad_after_s", None)
        lines.extend(_pretty_log_value(alignment).splitlines())
    else:
        lines.append("Disabled")

    lines.extend(["", "GUI LOG", "-------"])
    lines.extend(str(line) for line in gui_log_lines)
    return "\n".join(lines).rstrip() + "\n"


class _StreamToLog(io.TextIOBase):
    """Small stdout/stderr bridge used to show detailed filter_data prints in the Qt log."""

    def __init__(self, callback: Optional[Callable[[str], None]], prefix: str = ""):
        super().__init__()
        self.callback = callback
        self.prefix = prefix
        self._buffer = ""

    def writable(self):
        return True

    def write(self, text):
        if not text:
            return 0
        text = str(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                _emit(self.callback, f"{self.prefix}{line}")
        return len(text)

    def flush(self):
        if self._buffer.strip():
            _emit(self.callback, f"{self.prefix}{self._buffer.strip()}")
        self._buffer = ""


_MATPLOTLIB_LOCK = threading.RLock()
_FIGURE_LIFECYCLE_ATTR = "_nwb_workbench_figure_lifecycle"
_PLOTTING_TOOLBOX_INSTANCE = None
_PLOTTING_TOOLBOX_WINDOW = None


def _as_top_level_window(candidate):
    """Return the real Tk/Qt top-level window for a candidate widget."""
    if candidate is None:
        return None

    # PlottingToolboxGUI can expose a Tk Frame rather than the Toplevel/Tk that
    # contains it.  Destroying only the frame leaves the visible toolbox shell
    # alive, so always resolve the enclosing Tk window first.
    winfo_toplevel = getattr(candidate, "winfo_toplevel", None)
    if callable(winfo_toplevel):
        try:
            top_level = winfo_toplevel()
            if top_level is not None:
                return top_level
        except Exception:
            pass

    # The equivalent operation for a Qt child widget is QWidget.window().
    # Restrict this call to objects that otherwise look like Qt widgets because
    # a controller may have an unrelated method named ``window``.
    looks_like_qt_widget = any(
        callable(getattr(candidate, name, None))
        for name in ("isVisible", "showNormal", "raise_", "activateWindow", "winId")
    )
    qt_window = getattr(candidate, "window", None)
    if looks_like_qt_widget and callable(qt_window):
        try:
            top_level = qt_window()
            if top_level is not None:
                return top_level
        except Exception:
            pass

    return candidate


def _toolbox_window_candidates(toolbox):
    """Yield the real top-level windows owned by a plotting toolbox."""
    seen = set()

    # Prefer explicit window attributes over the controller itself.  Some
    # toolbox implementations inherit from Frame/QWidget while creating a
    # separate top-level window internally.
    window_attribute_names = (
        "window",
        "root",
        "top",
        "toplevel",
        "dialog",
        "main_window",
        "plot_window",
        "plotting_window",
        "tk_root",
        "master",
        "widget",
    )
    candidates = [getattr(toolbox, name, None) for name in window_attribute_names]
    candidates.append(toolbox)

    # PlottingToolboxGUI is a project utility whose internal window attribute
    # has changed names across versions.  Also inspect direct attributes that
    # look like Tk or Qt windows so the GUI can still be closed reliably when a
    # project uses a slightly different toolbox revision.
    try:
        for attribute_name, candidate in vars(toolbox).items():
            lowered_name = str(attribute_name).lower()
            if any(
                token in lowered_name
                for token in ("window", "root", "dialog", "toplevel", "top_level")
            ):
                candidates.append(candidate)
    except Exception:
        pass

    for candidate in candidates:
        candidate = _as_top_level_window(candidate)
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        yield candidate


def _window_alive(window):
    """Return True/False for a known GUI window, or None if it is unknown."""
    if window is None:
        return False

    winfo_exists = getattr(window, "winfo_exists", None)
    if callable(winfo_exists):
        try:
            return bool(winfo_exists())
        except Exception:
            return False

    # Qt wrappers raise RuntimeError after the underlying C++ object is deleted.
    if any(
        hasattr(window, name)
        for name in ("isVisible", "showNormal", "raise_", "activateWindow")
    ):
        try:
            visible = getattr(window, "isVisible", None)
            if callable(visible):
                visible()
            return True
        except (RuntimeError, ReferenceError):
            return False
        except Exception:
            return True

    return None


def _find_live_toolbox_window(toolbox):
    """Return the first live window owned by a plotting toolbox instance."""
    for candidate in _toolbox_window_candidates(toolbox):
        alive = _window_alive(candidate)
        if alive is True:
            return candidate
    # An ordinary Python controller object is not evidence that its GUI window
    # is still alive.  Returning such an object here leaves a stale singleton
    # after the real Tk/Qt window has been closed and makes later Plot GUI
    # clicks appear to do nothing.
    return None


def _clear_plotting_toolbox(toolbox=None):
    global _PLOTTING_TOOLBOX_INSTANCE, _PLOTTING_TOOLBOX_WINDOW
    if toolbox is None or _PLOTTING_TOOLBOX_INSTANCE is toolbox:
        _PLOTTING_TOOLBOX_INSTANCE = None
        _PLOTTING_TOOLBOX_WINDOW = None


def _remember_plotting_toolbox(toolbox):
    """Keep one toolbox alive and clear the reference when its window is destroyed."""
    global _PLOTTING_TOOLBOX_INSTANCE, _PLOTTING_TOOLBOX_WINDOW
    _PLOTTING_TOOLBOX_INSTANCE = toolbox
    _PLOTTING_TOOLBOX_WINDOW = _find_live_toolbox_window(toolbox)
    window = _PLOTTING_TOOLBOX_WINDOW
    if window is None:
        return

    # Tkinter/Tk-compatible windows.
    bind = getattr(window, "bind", None)
    if callable(bind):
        try:
            def _on_destroy(event, toolbox_ref=weakref.ref(toolbox), window_ref=weakref.ref(window)):
                current_toolbox = toolbox_ref()
                current_window = window_ref()
                if current_toolbox is None or current_window is None:
                    return
                if getattr(event, "widget", current_window) is current_window:
                    _clear_plotting_toolbox(current_toolbox)

            bind("<Destroy>", _on_destroy, add="+")
        except Exception:
            pass

    # Qt-compatible windows.
    destroyed = getattr(window, "destroyed", None)
    connect = getattr(destroyed, "connect", None)
    if callable(connect):
        try:
            toolbox_ref = weakref.ref(toolbox)
            connect(lambda *_args, ref=toolbox_ref: _clear_plotting_toolbox(ref()))
        except Exception:
            pass

def _raise_existing_plotting_toolbox() -> bool:
    """Raise the current Plot GUI instead of constructing another one."""
    toolbox = _PLOTTING_TOOLBOX_INSTANCE
    if toolbox is None:
        return False

    window = _PLOTTING_TOOLBOX_WINDOW or _find_live_toolbox_window(toolbox)
    if window is None or _window_alive(window) is False:
        _clear_plotting_toolbox(toolbox)
        return False

    # Restore a minimized/withdrawn window first, then bring it to the front.
    for name in ("deiconify", "showNormal", "show"):
        method = getattr(window, name, None)
        if callable(method):
            try:
                method()
                break
            except Exception:
                pass
    for name in ("lift", "raise_", "activateWindow", "focus_force", "focus_set"):
        method = getattr(window, name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
    return True


def close_plotting_gui(*, log_callback=None) -> bool:
    """Close the optional Plot GUI without closing normal figure windows.

    Analyses must never depend on the Plot GUI lifetime.  The panel calls this
    on the Qt/main thread before starting a new manual analysis or pipeline so
    the toolbox cannot retain stale figure references or a nested GUI loop.
    """
    # Do not acquire _MATPLOTLIB_LOCK here.  Older Plot GUI revisions entered
    # Tk's blocking mainloop while launch_plotting_gui still owned that lock;
    # waiting for the same lock here made both closing and the next analysis
    # hang before filtering started.
    toolbox = _PLOTTING_TOOLBOX_INSTANCE
    if toolbox is None:
        return False

    windows = []
    remembered_window = _as_top_level_window(_PLOTTING_TOOLBOX_WINDOW)
    if remembered_window is not None and _window_alive(remembered_window) is True:
        windows.append(remembered_window)
    for candidate in _toolbox_window_candidates(toolbox):
        if _window_alive(candidate) is True and all(candidate is not item for item in windows):
            windows.append(candidate)

    # Clear the singleton before closing the native window.  Its destroy
    # callback may run synchronously and must not leave a half-closed object
    # available to a new launch.
    _clear_plotting_toolbox(toolbox)

    close_attempted = False

    # The current toolbox exposes close(), which stops its Qt-to-Tk event pump
    # and destroys the exact Tk root.  Prefer that direct controller operation.
    controller_close = getattr(toolbox, "close", None)
    if callable(controller_close):
        try:
            controller_close()
            close_attempted = True
        except (RuntimeError, ReferenceError):
            close_attempted = True
        except Exception:
            pass

    for window in windows:
        if _window_alive(window) is False:
            continue
        is_tk_window = callable(getattr(window, "winfo_exists", None))

        if is_tk_window:
            withdraw = getattr(window, "withdraw", None)
            if callable(withdraw):
                try:
                    withdraw()
                except Exception:
                    pass
            method_names = ("destroy", "close")
        else:
            method_names = ("close", "hide", "deleteLater", "destroy")

        for name in method_names:
            method = getattr(window, name, None)
            if not callable(method):
                continue
            try:
                method()
                close_attempted = True
            except (RuntimeError, ReferenceError):
                close_attempted = True
                break
            except Exception:
                continue
            if is_tk_window and name == "destroy":
                break

    if close_attempted:
        _emit(log_callback, "Closed Plot GUI before starting the new run.")
        return True

    _emit(log_callback, "Could not close the existing Plot GUI automatically.")
    return False


def _sync_existing_plotting_toolbox_figures(figures) -> list:
    """Merge newly produced figures into the live singleton's result object.

    PlottingToolboxGUI versions used by existing projects commonly retain the
    constructor's result object or its figure list.  Mutating those objects in
    place lets the already-open toolbox see new analyses without creating a
    second toolbox window.
    """
    toolbox = _PLOTTING_TOOLBOX_INSTANCE
    merged = []
    seen = set()

    sources = []
    stored_view = getattr(toolbox, "_nwb_workbench_result_view", None)
    if stored_view is not None:
        sources.append(getattr(stored_view, "figures", None))
    for attr_name in ("result", "results", "analysis_result"):
        value = getattr(toolbox, attr_name, None)
        if value is not None:
            sources.append(getattr(value, "figures", None))
    sources.append(figures)

    for source in sources:
        for fig in list(source or []):
            if fig is None or id(fig) in seen or _figure_is_closed(fig):
                continue
            seen.add(id(fig))
            merged.append(fig)

    if stored_view is None:
        stored_view = SimpleNamespace(figures=list(merged))
        try:
            toolbox._nwb_workbench_result_view = stored_view
        except Exception:
            pass
    else:
        try:
            stored_view.figures[:] = merged
        except Exception:
            stored_view.figures = list(merged)

    # Keep the common public attributes synchronized without assuming one
    # particular PlottingToolboxGUI implementation.
    for attr_name in ("result", "results", "analysis_result"):
        value = getattr(toolbox, attr_name, None)
        if value is not None and hasattr(value, "figures"):
            try:
                current = getattr(value, "figures")
                if isinstance(current, list):
                    current[:] = merged
                else:
                    value.figures = list(merged)
            except Exception:
                pass
    for attr_name in ("figures", "figure_list", "fig_list"):
        if not hasattr(toolbox, attr_name):
            continue
        try:
            current = getattr(toolbox, attr_name)
            if isinstance(current, list):
                current[:] = merged
            else:
                setattr(toolbox, attr_name, list(merged))
        except Exception:
            pass

    return merged


def _set_figure_number(fig, manager, fallback: int) -> int:
    """Keep Figure.number identical to the unique manager number used by the GUI."""
    try:
        number = int(getattr(manager, "num"))
    except Exception:
        number = int(fallback)
    try:
        fig.number = number
    except Exception:
        pass
    return number


def _figure_lifecycle(fig) -> dict:
    state = getattr(fig, _FIGURE_LIFECYCLE_ATTR, None)
    if not isinstance(state, dict):
        state = {
            "registered": False,
            "closed": False,
            "canvas_id": None,
            "close_callback_id": None,
        }
        try:
            setattr(fig, _FIGURE_LIFECYCLE_ATTR, state)
        except Exception:
            pass
    return state


def _track_figure_close_event(fig):
    """Remember an explicit Matplotlib window close for the lifetime of a result."""
    state = _figure_lifecycle(fig)
    canvas = getattr(fig, "canvas", None)
    if canvas is None or not hasattr(canvas, "mpl_connect"):
        return
    canvas_id = id(canvas)
    if state.get("canvas_id") == canvas_id and state.get("close_callback_id") is not None:
        return

    fig_ref = weakref.ref(fig)

    def _closed(_event):
        closed_figure = fig_ref()
        if closed_figure is not None:
            _figure_lifecycle(closed_figure)["closed"] = True

    try:
        callback_id = canvas.mpl_connect("close_event", _closed)
    except Exception:
        return
    state["canvas_id"] = canvas_id
    state["close_callback_id"] = callback_id


def _figure_is_closed(fig) -> bool:
    """Return True after a once-displayed figure loses its GUI manager."""
    state = _figure_lifecycle(fig)
    if bool(state.get("closed", False)):
        return True
    if not bool(state.get("registered", False)):
        return False

    try:
        from matplotlib._pylab_helpers import Gcf

        for manager in list(Gcf.get_all_fig_managers()):
            managed_figure = getattr(getattr(manager, "canvas", None), "figure", None)
            if managed_figure is fig:
                return False
    except Exception:
        # If the registry cannot be inspected, do not discard the figure.
        return False

    # It had a GUI manager before and is no longer registered.  This covers
    # both a window closed manually and the plotting toolbox's Close all action,
    # including backends that do not emit close_event reliably.
    state["closed"] = True
    return True


def _available_result_figures(result, *, log_callback=None) -> list:
    figures = []
    seen = set()
    closed_count = 0
    for fig in list(getattr(result, "figures", []) or []):
        if fig is None or id(fig) in seen:
            continue
        seen.add(id(fig))
        if _figure_is_closed(fig):
            closed_count += 1
            continue
        figures.append(fig)
    if closed_count:
        _emit(
            log_callback,
            f"Keeping {closed_count} manually closed figure(s) closed.",
        )
    return figures


def _activate_last_figure(figures):
    """Make the newest figure active after registering a result batch."""
    if not figures:
        return
    target = figures[-1]
    try:
        from matplotlib._pylab_helpers import Gcf

        for manager in list(Gcf.get_all_fig_managers()):
            managed_figure = getattr(getattr(manager, "canvas", None), "figure", None)
            if managed_figure is target:
                Gcf.set_active(manager)
                return
    except Exception:
        pass


def _func_accepts_keyword(func: Any, key: str) -> bool:
    try:
        sig = inspect.signature(func)
    except Exception:
        return False
    if key in sig.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


class _WorkerMatplotlibContext:
    """Make Matplotlib figure creation safe inside the analysis worker.

    Many existing analysis scripts import ``matplotlib.pyplot`` at module import
    time and then call ``plt.subplots()`` inside the analysis function. If pyplot
    is already attached to a GUI backend, simply calling ``matplotlib.use("Agg")``
    inside the worker is too late and Matplotlib warns that a GUI is being started
    outside the main thread.

    To keep old analysis scripts unchanged, this context temporarily replaces the
    most common pyplot figure factories with off-screen Figure + FigureCanvasAgg
    factories. The analysis can still create normal Matplotlib Figure objects and
    add them to AnalysisResult. After the worker finishes, the Qt main thread can
    attach those figures to a real GUI backend and show/rearrange them.
    """

    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self._lock = _MATPLOTLIB_LOCK
        self._previous_interactive = None
        self._previous_pyplot_show = None
        self._previous_figure_show = None
        self._previous_pyplot_figure = None
        self._previous_pyplot_subplots = None
        self._previous_pyplot_gcf = None
        self._previous_pyplot_gca = None
        self._previous_pyplot_get_fignums = None
        self._plotting_toolbox_module = None
        self._previous_plotting_toolbox_gui = None
        self.created_figures: list[Any] = []
        self.preexisting_figure_ids: set[int] = set()
        self._current_figure = None

    def __enter__(self):
        self._lock.acquire()

        try:
            import matplotlib

            # Safe case: pyplot was not loaded before the worker started.
            # Then we can use a non-GUI backend for figure creation.
            if "matplotlib.pyplot" not in sys.modules:
                try:
                    matplotlib.use("Agg", force=True)
                except Exception as exc:
                    _emit(self.log_callback, f"Could not switch Matplotlib worker backend to Agg: {exc}")

            import matplotlib.pyplot as plt
            import matplotlib.figure as mpl_figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg

            # Figures already registered before this run belong to an earlier
            # analysis or to the open Plot GUI.  They must never be collected
            # into this result or have their GUI canvas replaced by the worker.
            try:
                from matplotlib._pylab_helpers import Gcf

                self.preexisting_figure_ids = {
                    id(fig)
                    for manager in list(Gcf.get_all_fig_managers())
                    for fig in [
                        getattr(getattr(manager, "canvas", None), "figure", None)
                    ]
                    if fig is not None
                }
            except Exception:
                self.preexisting_figure_ids = set()

            try:
                self._previous_interactive = plt.isinteractive()
                plt.ioff()
            except Exception:
                self._previous_interactive = None

            self._previous_pyplot_show = getattr(plt, "show", None)
            self._previous_figure_show = getattr(mpl_figure.Figure, "show", None)
            self._previous_pyplot_figure = getattr(plt, "figure", None)
            self._previous_pyplot_subplots = getattr(plt, "subplots", None)
            self._previous_pyplot_gcf = getattr(plt, "gcf", None)
            self._previous_pyplot_gca = getattr(plt, "gca", None)
            self._previous_pyplot_get_fignums = getattr(plt, "get_fignums", None)

            def _next_number(number=None):
                if number is not None:
                    try:
                        return int(number)
                    except Exception:
                        pass
                used = []
                for fig in self.created_figures:
                    try:
                        used.append(int(getattr(fig, "number", 0)))
                    except Exception:
                        pass
                n = 1
                while n in used:
                    n += 1
                return n

            def _worker_figure(*args, **kwargs):
                number = kwargs.pop("num", None)
                if args:
                    # pyplot.figure(num, ...) accepts the figure number/name as
                    # the first positional argument. For off-screen figures the
                    # value is only used as metadata.
                    number = args[0]

                kwargs.pop("clear", None)
                FigureClass = kwargs.pop("FigureClass", mpl_figure.Figure)

                # Keep only kwargs accepted by FigureClass. This avoids failures
                # if a pyplot-only keyword is passed by an old script.
                try:
                    sig = inspect.signature(FigureClass)
                    accepts_var_kw = any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    )
                    if not accepts_var_kw:
                        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
                except Exception:
                    pass

                fig = FigureClass(**kwargs)
                FigureCanvasAgg(fig)
                try:
                    fig.number = _next_number(number)
                except Exception:
                    pass
                self.created_figures.append(fig)
                self._current_figure = fig
                return fig

            def _worker_subplots(
                nrows=1,
                ncols=1,
                *,
                sharex=False,
                sharey=False,
                squeeze=True,
                width_ratios=None,
                height_ratios=None,
                subplot_kw=None,
                gridspec_kw=None,
                **fig_kw,
            ):
                fig = _worker_figure(**fig_kw)
                ax = fig.subplots(
                    nrows=nrows,
                    ncols=ncols,
                    sharex=sharex,
                    sharey=sharey,
                    squeeze=squeeze,
                    width_ratios=width_ratios,
                    height_ratios=height_ratios,
                    subplot_kw=subplot_kw,
                    gridspec_kw=gridspec_kw,
                )
                return fig, ax

            def _worker_gcf():
                if self._current_figure is None:
                    return _worker_figure()
                return self._current_figure

            def _worker_gca(*args, **kwargs):
                fig = _worker_gcf()
                if fig.axes:
                    try:
                        return fig.gca(*args, **kwargs)
                    except TypeError:
                        return fig.gca()
                return fig.add_subplot(111)

            def _worker_get_fignums():
                out = []
                for i, fig in enumerate(self.created_figures, start=1):
                    try:
                        out.append(int(getattr(fig, "number", i)))
                    except Exception:
                        out.append(i)
                return out

            def _suppressed_show(*args, **kwargs):
                _emit(self.log_callback, "Suppressed a Matplotlib show() call from the analysis worker. Figures will be displayed from the Qt main thread.")
                return None

            try:
                plt.figure = _worker_figure
                plt.subplots = _worker_subplots
                plt.gcf = _worker_gcf
                plt.gca = _worker_gca
                plt.get_fignums = _worker_get_fignums
                plt.show = _suppressed_show
            except Exception:
                pass
            try:
                mpl_figure.Figure.show = _suppressed_show
            except Exception:
                pass

        except Exception as exc:
            _emit(self.log_callback, f"Could not prepare Matplotlib worker context: {exc}")

        # Some old analysis functions can open the Tk plotting toolbox directly.
        # Suppress that in the worker and leave display to the Qt/main thread.
        try:
            import Tools.General.plotting.plotting_toolbox as plotting_toolbox

            self._plotting_toolbox_module = plotting_toolbox
            self._previous_plotting_toolbox_gui = getattr(plotting_toolbox, "PlottingToolboxGUI", None)

            def _suppressed_plotting_toolbox_gui(*args, **kwargs):
                _emit(self.log_callback, "Suppressed PlottingToolboxGUI opening from the analysis worker. Use Plot GUI to arrange figures.")
                return None

            plotting_toolbox.PlottingToolboxGUI = _suppressed_plotting_toolbox_gui
        except Exception:
            pass

        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import matplotlib.pyplot as plt
            if self._previous_pyplot_figure is not None:
                plt.figure = self._previous_pyplot_figure
            if self._previous_pyplot_subplots is not None:
                plt.subplots = self._previous_pyplot_subplots
            if self._previous_pyplot_gcf is not None:
                plt.gcf = self._previous_pyplot_gcf
            if self._previous_pyplot_gca is not None:
                plt.gca = self._previous_pyplot_gca
            if self._previous_pyplot_get_fignums is not None:
                plt.get_fignums = self._previous_pyplot_get_fignums
            if self._previous_pyplot_show is not None:
                plt.show = self._previous_pyplot_show
            if self._previous_interactive:
                plt.ion()
        except Exception:
            pass

        try:
            import matplotlib.figure as mpl_figure
            if self._previous_figure_show is not None:
                mpl_figure.Figure.show = self._previous_figure_show
        except Exception:
            pass

        try:
            if self._plotting_toolbox_module is not None and self._previous_plotting_toolbox_gui is not None:
                self._plotting_toolbox_module.PlottingToolboxGUI = self._previous_plotting_toolbox_gui
        except Exception:
            pass

        self._lock.release()
        return False


class _DebugMatplotlibContext:
    """No-op Matplotlib context used when an analysis declares debug_mode=True.

    In debug mode the analysis runs in the Qt/main thread, so we should not
    replace pyplot with off-screen Agg helpers. This keeps breakpoints and
    interactive Matplotlib behavior close to a normal script run.
    """

    def __init__(self, log_callback=None):
        self.log_callback = log_callback
        self.created_figures = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _collect_pyplot_figures_without_canvas_change(result, *, extra_figures=None, log_callback=None):
    """Collect existing pyplot figures without replacing their GUI canvas.

    This is used only in debug mode. The analysis runs on the main thread, so
    figures may already have a valid GUI manager and should not be converted
    back to Agg.
    """
    figures = list(getattr(result, "figures", []) or [])
    known_ids = {id(fig) for fig in figures}

    for fig in list(extra_figures or []):
        if fig is not None and id(fig) not in known_ids:
            figures.append(fig)
            known_ids.add(id(fig))

    try:
        from matplotlib._pylab_helpers import Gcf
        for manager in list(Gcf.get_all_fig_managers()):
            fig = getattr(getattr(manager, "canvas", None), "figure", None)
            if fig is not None and id(fig) not in known_ids:
                figures.append(fig)
                known_ids.add(id(fig))
    except Exception:
        pass

    try:
        original = getattr(result, "figures", None)
        if isinstance(original, list):
            original[:] = figures
        elif hasattr(result, "figures"):
            result.figures = figures
    except Exception:
        pass

    if figures:
        _emit(log_callback, f"Figures available for plotting: {len(figures)}")


def _attach_agg_canvas_to_figures(
    result,
    *,
    extra_figures=None,
    preexisting_figure_ids=None,
    log_callback=None,
):
    """Collect worker-created figures and detach them from GUI canvases.

    Uses Matplotlib's figure-manager registry directly instead of plt.figure(),
    because plt.figure() can create or raise GUI windows depending on the active
    backend.
    """
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib._pylab_helpers import Gcf
    except Exception:
        return

    excluded_ids = set(preexisting_figure_ids or ())
    figures = [
        fig
        for fig in list(getattr(result, "figures", []) or [])
        if fig is not None and id(fig) not in excluded_ids
    ]
    known_ids = {id(fig) for fig in figures}

    for fig in list(extra_figures or []):
        if (
            fig is not None
            and id(fig) not in excluded_ids
            and id(fig) not in known_ids
        ):
            figures.append(fig)
            known_ids.add(id(fig))

    # A script may keep a pre-imported pyplot alias that bypasses the temporary
    # worker factories.  Collect only managers created during this run.  Older
    # registered figures are intentionally excluded so an open Plot GUI and
    # previous analysis windows keep their live GUI canvases.
    try:
        for manager in list(Gcf.get_all_fig_managers()):
            fig = getattr(getattr(manager, "canvas", None), "figure", None)
            if (
                fig is not None
                and id(fig) not in excluded_ids
                and id(fig) not in known_ids
            ):
                figures.append(fig)
                known_ids.add(id(fig))
    except Exception:
        pass

    try:
        original = getattr(result, "figures", None)
        if isinstance(original, list):
            original[:] = figures
        elif hasattr(result, "figures"):
            result.figures = figures
    except Exception:
        pass

    for fig in list(getattr(result, "figures", []) or []):
        try:
            FigureCanvasAgg(fig)
        except Exception:
            pass

    if figures:
        _emit(log_callback, f"Figures available for plotting: {len(figures)}")



def _matplotlib_backend_is_gui(backend_name: str) -> bool:
    """Return whether a Matplotlib backend creates interactive windows.

    QtAgg, TkAgg, WxAgg and GTK*Agg are GUI backends even though their names end
    in ``Agg``.  Treating every ``*Agg`` backend as non-interactive caused an
    unnecessary ``switch_backend`` during display, which can destroy existing
    figure managers.
    """
    name = str(backend_name or "").strip().lower()
    if not name:
        return False
    if name.startswith("module://"):
        return any(
            marker in name
            for marker in ("qt", "tk", "wx", "gtk", "macosx", "nbagg", "ipympl", "webagg")
        )
    non_gui_backends = {
        "agg",
        "cairo",
        "pdf",
        "pgf",
        "ps",
        "svg",
        "template",
    }
    return name not in non_gui_backends


def _ensure_gui_matplotlib_backend(*, log_callback=None) -> bool:
    """Switch Matplotlib back to an interactive backend before opening the plot GUI.

    The worker can use Agg safely, but figures must be re-attached to a real
    GUI backend before the old plotting toolbox tries to access
    ``fig.canvas.manager.window``.
    """
    try:
        import matplotlib
        import matplotlib.pyplot as plt
    except Exception as exc:
        _emit(log_callback, f"Could not import Matplotlib for plotting GUI: {exc}")
        return False

    current = str(matplotlib.get_backend())
    if _matplotlib_backend_is_gui(current):
        return True

    candidates: list[str] = []

    # In the Qt GUI, a QApplication is already running, so prefer the Qt backend.
    if any(name in sys.modules for name in ("PySide6", "PyQt6", "PySide2", "PyQt5")):
        candidates.extend(["QtAgg", "Qt5Agg"])

    # Fallbacks. Duplicates are removed below while preserving order.
    candidates.extend(["QtAgg", "Qt5Agg", "TkAgg"])
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    last_error = None
    for backend in candidates:
        try:
            plt.switch_backend(backend)
            _emit(log_callback, f"Matplotlib backend for plotting GUI: {matplotlib.get_backend()}")
            return True
        except Exception as exc:
            last_error = exc

    _emit(
        log_callback,
        "Could not switch Matplotlib from non-interactive backend "
        f"{current!r} to a GUI backend. Last error: {last_error}",
    )
    return False


def _current_pyplot_backend_module():
    import matplotlib.pyplot as plt

    getter = getattr(plt, "_get_backend_mod", None)
    if callable(getter):
        mod = getter()
        if hasattr(mod, "new_figure_manager_given_figure"):
            return mod

    import matplotlib
    backend = str(matplotlib.get_backend()).lower()
    candidates = []
    if "qtagg" in backend or "qt5agg" in backend:
        candidates.extend([
            "matplotlib.backends.backend_qtagg",
            "matplotlib.backends.backend_qt5agg",
        ])
    elif "tkagg" in backend:
        candidates.append("matplotlib.backends.backend_tkagg")

    for mod_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "new_figure_manager_given_figure"):
                return mod
        except Exception:
            pass

    return None


def _figure_has_real_gui_manager(fig) -> bool:
    try:
        manager = getattr(getattr(fig, "canvas", None), "manager", None)
        return manager is not None and getattr(manager, "window", None) is not None
    except Exception:
        return False


def _register_result_figures_with_gui_backend(result, *, log_callback=None) -> bool:
    """Attach AnalysisResult figures to the current GUI backend on the main thread."""
    figures = _available_result_figures(result, log_callback=log_callback)
    if not figures:
        return False

    if not _ensure_gui_matplotlib_backend(log_callback=log_callback):
        return False

    try:
        from matplotlib._pylab_helpers import Gcf
    except Exception as exc:
        _emit(log_callback, f"Could not access Matplotlib figure registry: {exc}")
        return False

    backend_mod = _current_pyplot_backend_module()
    if backend_mod is None:
        _emit(log_callback, "Could not find a Matplotlib GUI backend module for figure registration.")
        return False

    ok_count = 0
    errors: list[str] = []
    existing_manager_by_figure = {}
    used_numbers = set()
    for existing_manager in list(Gcf.get_all_fig_managers()):
        try:
            existing_figure = getattr(
                getattr(existing_manager, "canvas", None), "figure", None
            )
            if existing_figure is not None:
                existing_manager_by_figure[id(existing_figure)] = existing_manager
            used_numbers.add(int(getattr(existing_manager, "num")))
        except Exception:
            pass

    for idx, fig in enumerate(figures, start=1):
        try:
            manager = existing_manager_by_figure.get(id(fig))
            if manager is not None and _figure_has_real_gui_manager(fig):
                _set_figure_number(fig, manager, idx)
                Gcf.set_active(manager)
                state = _figure_lifecycle(fig)
                state["registered"] = True
                state["closed"] = False
                _track_figure_close_event(fig)
                ok_count += 1
                continue

            number = getattr(fig, "number", None)
            try:
                number = int(number)
            except Exception:
                number = idx
            if number < 1:
                number = 1
            while number in used_numbers:
                number += 1
            used_numbers.add(number)

            # Remove any old Agg/non-GUI manager registration for this number.
            try:
                Gcf.destroy(number)
            except Exception:
                pass

            manager = backend_mod.new_figure_manager_given_figure(number, fig)
            _set_figure_number(fig, manager, number)
            try:
                Gcf.set_active(manager)
            except Exception:
                pass
            try:
                manager.canvas.draw_idle()
            except Exception:
                try:
                    manager.canvas.draw()
                except Exception:
                    pass

            if _figure_has_real_gui_manager(fig):
                state = _figure_lifecycle(fig)
                state["registered"] = True
                state["closed"] = False
                _track_figure_close_event(fig)
                ok_count += 1
            else:
                errors.append(f"figure {idx}: GUI manager has no window")

        except Exception as exc:
            errors.append(f"figure {idx}: {exc}")

    if errors:
        _emit(log_callback, "Some figures could not be attached to the GUI backend:")
        for line in errors[:5]:
            _emit(log_callback, f"  {line}")
        if len(errors) > 5:
            _emit(log_callback, f"  ... and {len(errors) - 5} more")

    if ok_count:
        _activate_last_figure(figures)
        _emit(log_callback, f"Figures attached to GUI backend: {ok_count}/{len(figures)}")
        return True

    return False


def _show_registered_figures(figures) -> int:
    """Show only the supplied GUI-registered figures without reopening closed ones."""
    try:
        import matplotlib.pyplot as plt
        from matplotlib._pylab_helpers import Gcf
    except Exception:
        return 0

    figure_ids = {id(fig) for fig in list(figures or []) if fig is not None}
    shown = 0
    for manager in list(Gcf.get_all_fig_managers()):
        try:
            fig = getattr(getattr(manager, "canvas", None), "figure", None)
            if fig is None or id(fig) not in figure_ids:
                continue
            try:
                manager.show()
            except Exception:
                pass
            try:
                manager.canvas.draw_idle()
            except Exception:
                try:
                    manager.canvas.draw()
                except Exception:
                    pass
            shown += 1
        except Exception:
            pass

    try:
        plt.show(block=False)
    except Exception:
        pass
    _activate_last_figure(figures)
    return shown

def _make_filter_progress_callback(log_callback, progress_callback, total_hint: int):
    def _callback(*args, **kwargs):
        payload = {}
        if args and isinstance(args[0], dict):
            payload.update(args[0])
        elif args:
            # Common callback forms: (i, total, text) or (text,)
            if len(args) >= 1:
                payload["value"] = args[0]
            if len(args) >= 2:
                payload["total"] = args[1]
            if len(args) >= 3:
                payload["text"] = args[2]
        payload.update(kwargs)

        text = str(payload.get("text") or payload.get("message") or payload.get("status") or "").strip()
        value = payload.get("value", payload.get("i", payload.get("current", None)))
        total = payload.get("total", payload.get("maximum", total_hint))
        _emit_progress(progress_callback, text=text or "Filtering/alignment", value=value, total=total)

    return _callback


def _call_get_filtered_data_with_supported_options(base_kwargs: dict, *, log_callback, progress_callback, total_hint: int):
    """Call get_filtered_data while enabling any optional logging/progress hooks it exposes.

    The Tools version has changed over time. This keeps the Qt GUI compatible with old
    and new signatures by only passing optional arguments that are explicitly accepted.
    """
    kwargs = dict(base_kwargs)
    try:
        sig = inspect.signature(get_filtered_data)
        params = sig.parameters
    except Exception:
        params = {}

    progress_cb = _make_filter_progress_callback(log_callback, progress_callback, total_hint)

    optional_values = {
        "display_info": True,
        "verbose": True,
        "verbose_filtering": True,
        "log_callback": log_callback,
        "logger_callback": log_callback,
        "progress_callback": progress_cb,
        "status_callback": progress_cb,
    }
    for key, value in optional_values.items():
        if key in params and key not in kwargs:
            kwargs[key] = value

    stdout_bridge = _StreamToLog(log_callback)
    stderr_bridge = _StreamToLog(log_callback, prefix="STDERR: ")
    with contextlib.redirect_stdout(stdout_bridge), contextlib.redirect_stderr(stderr_bridge):
        try:
            return get_filtered_data(**kwargs)
        finally:
            stdout_bridge.flush()
            stderr_bridge.flush()


def display_result_figures(result, *, log_callback=None):
    """Display AnalysisResult figures immediately from the Qt/main thread.

    This does not open the plotting toolbox. It only creates normal Matplotlib
    windows for the figures produced during the analysis. The user can still
    press Plot GUI afterward to spatially arrange/reorder them.
    """
    if result is None or not getattr(result, "figures", None):
        return False

    with _MATPLOTLIB_LOCK:
        figures = _available_result_figures(result, log_callback=log_callback)
        if not figures:
            return False
        if not _register_result_figures_with_gui_backend(result, log_callback=log_callback):
            return False

        shown = _show_registered_figures(figures)

        if shown:
            _emit(log_callback, f"Figures displayed: {shown}")
            return True
        return False


def launch_plotting_gui(result, nb_fig_layer=4, nb_of_row=2, log_callback=None):
    """Open the plotting toolbox from the GUI thread only.

    Worker-created figures are stored with an Agg canvas so the analysis can run
    safely outside the Qt event loop. Before opening the old plotting toolbox,
    the figures are re-attached to a real GUI backend from the main thread.
    """
    with _MATPLOTLIB_LOCK:
        if result is None or not getattr(result, "figures", None):
            return False

        figures = _available_result_figures(result, log_callback=log_callback)
        if not figures:
            _emit(log_callback, "No open figures available")
            return False
        result_view = SimpleNamespace(figures=figures)
        attached = _register_result_figures_with_gui_backend(
            result_view,
            log_callback=log_callback,
        )
        _activate_last_figure(figures)

        # Register the new result first.  The old implementation returned here
        # before doing that, so a second analysis was silently ignored whenever
        # the Plot GUI was already open.
        if _raise_existing_plotting_toolbox():
            merged = _sync_existing_plotting_toolbox_figures(figures)
            _show_registered_figures(figures)
            _activate_last_figure(merged)
            _emit(
                log_callback,
                "Plot GUI already open; updated it with the available figures "
                "and brought it to the front.",
            )
            return True

        from Tools.General.plotting.plotting_toolbox import PlottingToolboxGUI

        try:
            toolbox = PlottingToolboxGUI(
                result=result_view,
                nb_fig_layer=nb_fig_layer,
                nb_of_row=nb_of_row,
            )
            try:
                toolbox._nwb_workbench_result_view = result_view
            except Exception:
                pass
            _remember_plotting_toolbox(toolbox)
            _activate_last_figure(figures)
            # Keep a reference when the toolbox is a non-blocking object.  Some
            # versions otherwise let it be collected immediately after return.
            try:
                result._nwb_workbench_plotting_toolbox = toolbox
            except Exception:
                pass
            return True
        except AttributeError as exc:
            # Common failure after Agg worker rendering: fig.canvas.manager.window
            # does not exist. When possible, fall back to normal Matplotlib windows
            # instead of losing the figures entirely.
            if "window" not in str(exc):
                raise
            _emit(log_callback, f"Plotting toolbox could not use the figure manager: {exc}")
            if attached:
                try:
                    import matplotlib.pyplot as plt
                    plt.show(block=False)
                    _emit(log_callback, "Opened figures with Matplotlib fallback windows.")
                    return True
                except Exception:
                    pass
            raise

def run_analysis(
    *,
    transfer,
    analysis_spec,
    params,
    ui,
    logger=None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    done_callback: Optional[Callable[[object], None]] = None,
    error_callback: Optional[Callable[[str], None]] = None,
    debug_mode: bool = False,
):
    """Run an analysis for the Qt GUI.

    This intentionally avoids Tk loading screens. All status goes through
    log_callback/progress_callback, which should be Qt signal emitters from the panel.
    """
    log = logger or logging.getLogger(str(analysis_spec.get("name", "Analysis")))
    func = analysis_spec.get("func")

    def worker():
        result = AnalysisResult()
        started_clock = time.perf_counter()
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        output_dir = None
        try:
            name = str(analysis_spec.get("name", transfer.analysis_name))
            gui_log_lines = [f"Launching analysis: {name}", ""]

            def run_log_callback(text):
                gui_log_lines.append(str(text))
                _emit(log_callback, text)

            _emit_progress(progress_callback, reset=True, text="Starting analysis")

            backend = getattr(transfer, "backend", None)
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass

            result.add_log("Starting analysis", analysis_name=name)

            tasks = _build_extraction_tasks(transfer)
            total_tasks = max(1, len(tasks))
            unique_files = sorted({t["file_path"] for t in tasks})
            unique_conditions = sorted({t["condition"] for t in tasks})
            unique_data = sorted({t["data_type"] for t in tasks if t["data_type"]})

            _emit(run_log_callback, "Filtering/alignment setup")
            _emit(run_log_callback, f"  Conditions: {len(unique_conditions)}")
            _emit(run_log_callback, f"  Files: {len(unique_files)}")
            _emit(run_log_callback, f"  Data: {', '.join(unique_data) if unique_data else 'none'}")
            _emit(run_log_callback, f"  Extraction steps: {len(tasks)}")

            if getattr(transfer, "align_to_event", False):
                ev = getattr(transfer, "event_alignment", {}) or {}
                start_offset, end_offset = _event_alignment_offsets(ev)
                _emit(
                    run_log_callback,
                    "  Alignment: "
                    f"{ev.get('event_marker')} {ev.get('edge')}, "
                    f"offset {start_offset} s -> "
                    f"{ev.get('end_event_marker', ev.get('event_marker'))} "
                    f"{ev.get('end_edge', ev.get('edge'))}, offset {end_offset} s",
                )
            else:
                _emit(run_log_callback, "  Alignment: disabled")

            _emit_progress(
                progress_callback,
                total=total_tasks,
                value=0,
                text=f"Filtering/alignment: 0/{total_tasks} files",
            )
            _emit(run_log_callback, "")
            _emit(run_log_callback, "Filtering selected data and applying alignment")

            base_kwargs = dict(
                condition_filter=transfer.condition_selection,
                data_types=transfer.data_type,
                nwb_data_path=transfer.data_path,
                align_to_events=transfer.align_to_event,
                event_marker_to_align_dict=transfer.event_alignment,
                content_df=transfer.GUI_instance._content_df_master.copy(deep=True),
                metadata_df=transfer.GUI_instance._df_master.copy(deep=True),
                save=True,
                save_metadata=transfer.save_metadata,
                project_path=transfer.GUI_instance.project_root,
                loading_screen=False,
                parent=None,
                results=result,
            )

            matplotlib_context_cls = _DebugMatplotlibContext if debug_mode else _WorkerMatplotlibContext
            if debug_mode:
                _emit(run_log_callback, "Debug mode enabled: running analysis on the GUI thread.")

            # Filtering does not create figures and must never wait for a
            # plotting lock or GUI event loop.  Keep it entirely outside the
            # Matplotlib worker context.
            if analysis_spec.get('input_mode') == 'raw_nwb':
                # Opt-in readers handle their own alignment, including irregular
                # video timestamps, instead of the regular-signal extractor.
                import copy
                data_manager = SimpleNamespace(
                    condition_selection=copy.deepcopy(transfer.condition_selection),
                    selected_files=list(getattr(transfer,'selected_files',[]) or []),
                )
                _emit(run_log_callback,'Direct read-only NWB input: this analysis handles event alignment.')
            else:
                data_manager = _call_get_filtered_data_with_supported_options(
                    base_kwargs,
                    log_callback=run_log_callback,
                    progress_callback=progress_callback,
                    total_hint=total_tasks,
                )

            preparation_message = "Read-only NWB inputs prepared" if analysis_spec.get("input_mode") == "raw_nwb" else "Filtering/alignment finished"
            result.add_log(preparation_message)
            _emit_progress(progress_callback, total=total_tasks, value=total_tasks, text=preparation_message, done=True)
            _emit(run_log_callback, preparation_message)

            analysis_kwargs = dict(transfer.function_param_maker())
            analysis_kwargs.update(dict(params or {}))

            # Old analysis functions sometimes know how to open the plotting GUI
            # by themselves. In normal threaded mode this must not happen in
            # the worker. In debug mode we still disable the automatic toolbox
            # request so the GUI panel remains the owner of arranging figures.
            if _func_accepts_keyword(func, "launch_plotting_GUI_after_analysis"):
                analysis_kwargs["launch_plotting_GUI_after_analysis"] = False

            _emit_progress(progress_callback, indeterminate=True, text="Running analysis function")
            _emit(run_log_callback, "Running analysis function...")

            with matplotlib_context_cls(log_callback=run_log_callback) as mpl_context:
                stdout_bridge = _StreamToLog(run_log_callback)
                stderr_bridge = _StreamToLog(run_log_callback, prefix="STDERR: ")
                with contextlib.redirect_stdout(stdout_bridge), contextlib.redirect_stderr(stderr_bridge):
                    try:
                        func(data_manager=data_manager, result=result, **analysis_kwargs)
                    finally:
                        stdout_bridge.flush()
                        stderr_bridge.flush()

                if debug_mode:
                    _collect_pyplot_figures_without_canvas_change(
                        result,
                        extra_figures=getattr(mpl_context, "created_figures", []),
                        log_callback=run_log_callback,
                    )
                else:
                    _attach_agg_canvas_to_figures(
                        result,
                        extra_figures=getattr(mpl_context, "created_figures", []),
                        preexisting_figure_ids=getattr(
                            mpl_context,
                            "preexisting_figure_ids",
                            set(),
                        ),
                        log_callback=run_log_callback,
                    )

            result.add_log(
                "Analysis function finished",
                parameter_keys=sorted(list((params or {}).keys())),
            )
            _emit(run_log_callback, "Analysis function finished")

            if transfer.save_analysis:
                output_dir = transfer.analysis_output_dir or (
                    Path(transfer.GUI_instance.project_root) / "Analysis" / transfer.analysis_name
                )
                _emit_progress(progress_callback, indeterminate=True, text="Saving analysis result")
                _emit(run_log_callback, f"Saving analysis result to: {output_dir}")
                saved = result.save(output_dir, save_logs=False)
                result.add_log("Analysis result saved", output_dir=str(output_dir), saved=saved)
                _emit(run_log_callback, "Analysis result saved")

            if getattr(result, "figures", None):
                _emit(run_log_callback, f"Figures created: {len(result.figures)}")

            _emit_progress(progress_callback, total=1, value=1, text="Done", done=True)
            _emit(run_log_callback, "Done!")

            if transfer.save_analysis:
                finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                text_log = _build_analysis_text_log(
                    transfer=transfer,
                    analysis_spec=analysis_spec,
                    params=dict(params or {}),
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_s=time.perf_counter() - started_clock,
                    output_dir=output_dir,
                    gui_log_lines=gui_log_lines,
                )
                result.save_logs(output_dir, text_log=text_log)

            if callable(done_callback):
                done_callback(result)

        except Exception as exc:
            tb = traceback.format_exc()
            try:
                result.add_log("Analysis failed", level="ERROR", error=str(exc))
            except Exception:
                pass
            log.exception("Analysis failed")
            _emit_progress(progress_callback, total=1, value=0, text="Analysis failed")
            _emit(log_callback, "Analysis failed:")
            _emit(log_callback, tb)
            if callable(error_callback):
                error_callback(tb)

    if debug_mode:
        worker()
    else:
        threading.Thread(target=worker, daemon=True).start()
    return True
