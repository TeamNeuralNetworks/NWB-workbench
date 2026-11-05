from __future__ import annotations
from GUI.project_paths import read_excel as _pp_read_excel

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union
import gc
import traceback

from pynwb import TimeSeries


import pandas as pd  # type: ignore

from GUI.qt_compat import QtCore



@dataclass(frozen=True)
class RunnerUI:
    title: str
    initial_text: str = "Preparing..."
    width: int = 700


@dataclass(frozen=True)
class RunnerBehavior:
    continue_on_error: bool = False


class QtProcessingScreen:
    """Small adapter used by processing_runner-compatible tools.

    It sends log messages to ProcessingPanel when available, otherwise falls
    back to print(). It is safe to call from a worker thread because
    ProcessingPanel.post_log uses a Qt signal.
    """

    def __init__(self, panel=None, title: str = "Processing"):
        self.panel = panel
        self.title = title
        self.total = None

    def open(self, total=None, initial_text=""):
        self.total = total
        if self.panel is not None and hasattr(self.panel, "show_run_log"):
            self.panel.show_run_log(self.title)
        if initial_text:
            self._line(initial_text)
        if total is not None:
            self._line(f"Total files: {total}")
        self._line("")

    def _line(self, text: str):
        if self.panel is not None and hasattr(self.panel, "post_log"):
            self.panel.post_log(str(text))
        else:
            print(str(text))

    def post(self, kind: str, payload: dict | None = None):
        payload = payload or {}
        if kind == "status":
            text = payload.get("text", "")
            if text:
                self._line("")
                self._line(str(text))
        elif kind == "file":
            path = payload.get("path", "")
            if path:
                self._line(f"File: {path}")
        elif kind == "log":
            text = payload.get("text", "")
            if text:
                self._line(str(text))
        elif kind == "error":
            text = payload.get("text", "Error")
            detail = payload.get("detail", "")
            self._line("")
            self._line(f"ERROR: {text}")
            if detail:
                self._line(str(detail))
        elif kind == "done":
            text = payload.get("text", "Done")
            self._line("")
            self._line(str(text))
        elif kind == "progress":
            i = payload.get("i", None)
            if i is not None and self.total:
                self._line(f"Progress: {i}/{self.total}")


MatchFn = Callable[[str], bool]
MatchRules = Mapping[str, Callable[[Any], bool]]
MatchSpec = Union[MatchFn, MatchRules]
def _default_parent_from_transfer(transfer: Any):
    for attr in ("ProcessingUI_instance", "processing_ui", "ProcessingPanel", "panel", "root"):
        obj = getattr(transfer, attr, None)
        if obj is not None:
            return obj
    return None


def _real_qt_app_from_transfer(transfer: Any):
    """Return the real Qt app, not the Tk-compatible adapter when possible."""
    app = getattr(transfer, "qt_GUI_instance", None)
    if app is not None:
        return app

    app = getattr(transfer, "GUI_instance", None)
    # _QtAsTkParentAdapter stores the real app in _app.
    real = getattr(app, "_app", None)
    if real is not None:
        return real
    return app


def _paths_match(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except Exception:
        return str(a).replace("\\", "/").lower() == str(b).replace("\\", "/").lower()


def _iter_known_backends(transfer: Any):
    """Yield only real NWB backend objects that may hold HDF5 file handles.

    Important: Qt widgets also have a ``close()`` method.  A previous version
    treated any object with ``close`` as a backend and accidentally closed GUI
    panels such as the file tree, content panel, and right panel.  This helper is
    deliberately strict: it yields only objects that look like our NWBBackend, or
    a real backend stored on an object's ``.backend`` attribute.
    """
    seen: set[int] = set()

    def _looks_like_nwb_backend(be) -> bool:
        if be is None or not hasattr(be, "close"):
            return False

        cls = be.__class__
        cls_name = getattr(cls, "__name__", "").lower()
        module = getattr(cls, "__module__", "").lower()

        # The normal backend class is Tools.General.nwb_backend.NWBBackend.
        if "nwb_backend" in module or "nwbbackend" in cls_name:
            return True

        # Be tolerant of small wrappers, but avoid QWidget.close().
        if hasattr(be, "io") and (hasattr(be, "open") or hasattr(be, "read")):
            return True

        return False

    def _yield_backend(obj):
        if obj is None:
            return

        candidates = []

        # A real backend may be passed directly.
        candidates.append(obj)

        # GUI panels may store the real backend as .backend.  Only use that
        # attribute; never use the panel itself just because QWidget has close().
        try:
            be_attr = getattr(obj, "backend", None)
        except Exception:
            be_attr = None
        if be_attr is not None and be_attr is not obj:
            candidates.append(be_attr)

        for be in candidates:
            if not _looks_like_nwb_backend(be):
                continue
            ident = id(be)
            if ident in seen:
                continue
            seen.add(ident)
            yield be

    # The dedicated processing backend, if any.
    yield from _yield_backend(getattr(transfer, "backend", None))

    app = _real_qt_app_from_transfer(transfer)
    if app is not None:
        yield from _yield_backend(getattr(app, "backend", None))
        for attr in (
            "plot_panel",
            "content_panel",
            "file_tree_panel",
            "right_panel",
            "spike_sorting_panel",
            "processing_panel",
        ):
            yield from _yield_backend(getattr(app, attr, None))

    # The adapter may still expose backend-like attributes.
    adapter = getattr(transfer, "GUI_instance", None)
    if adapter is not None and adapter is not app:
        yield from _yield_backend(getattr(adapter, "backend", None))


def _force_release_nwb_read_handles(transfer: Any, target_path: Any = None, *, screen=None, log=None):
    """Close GUI read-only NWB handles before opening a file in r+.

    This is intentionally conservative. The Qt plot/browser can reopen the file
    later, but h5py cannot open a file in r+ while another read-only HDF5 handle
    to the same file is alive in the same process.
    """
    app = _real_qt_app_from_transfer(transfer)

    try:
        plot_panel = getattr(app, "plot_panel", None) if app is not None else None
        if plot_panel is not None:
            current = getattr(plot_panel, "current_nwb_path", None)
            if target_path is None or _paths_match(current, target_path):
                if hasattr(plot_panel, "release_nwb_handles"):
                    try:
                        plot_panel.release_nwb_handles()
                    except Exception:
                        pass
                if hasattr(plot_panel, "_invalidate_buffer_and_cache"):
                    try:
                        plot_panel._invalidate_buffer_and_cache()
                    except Exception:
                        pass
    except Exception:
        pass

    closed_any = False
    for backend in list(_iter_known_backends(transfer)):
        try:
            backend.close()
            closed_any = True
        except TypeError:
            try:
                backend.close(lock_id=None)
                closed_any = True
            except Exception:
                pass
        except Exception:
            pass

    gc.collect()
    if closed_any:
        try:
            time.sleep(0.05)
        except Exception:
            pass


def _make_logger(tool_name: str, base_logger: Optional[logging.Logger]) -> logging.Logger:
    if base_logger is not None:
        return base_logger.getChild(tool_name)
    return logging.getLogger(tool_name)


def _row_for_path(transfer: Any, path: str) -> Optional[dict]:
    df = getattr(transfer, "NWB_inventory_df", None)
    if df is None or pd is None:
        return None

    if not isinstance(df, pd.DataFrame):
        return None
    if "file_path" not in df.columns:
        return None

    lookup = getattr(transfer, "_inventory_lookup", None)
    if lookup is None:
        lookup = {
            str(p): row.to_dict()
            for p, row in df.set_index("file_path").iterrows()
        }
        transfer._inventory_lookup = lookup

    return lookup.get(str(path))


def build_match_from_inventory(rules: MatchRules) -> Callable[[str, Any], bool]:
    rules = dict(rules or {})

    def _match(path: str, transfer: Any) -> bool:
        row = _row_for_path(transfer, path)
        if row is None:
            return False

        for col, predicate in rules.items():
            if col not in row:
                return False

            if not bool(predicate(row.get(col))):
                return False

        return True

    return _match


def _normalize_match(match: MatchSpec, transfer: Any) -> Callable[[str], bool]:
    if isinstance(match, dict):
        inv_match = build_match_from_inventory(match)

        def _m(path: str) -> bool:
            return bool(inv_match(path, transfer))

        return _m

    def _m(path: str) -> bool:
        return bool(match(path))

    return _m


def _build_params_for_file(
    transfer: Any,
    *,
    tool_name: str,
    path: str,
    base_params: dict,
    logger: logging.Logger,
) -> dict:
    tool_defaults = dict(base_params or {})
    tool_defaults.pop("match", None)

    for k in list(tool_defaults.keys()):
        v = tool_defaults[k]
        if isinstance(v, dict) and "type" in v:
            tool_defaults.pop(k, None)

    user_vals = dict(getattr(transfer, "tool_params", {}) or {})
    user_vals.pop("match", None)

    params = dict(tool_defaults)
    params.update(user_vals)

    params["tool_name"] = tool_name
    params["file_path"] = str(path)
    params["project_root"] = transfer.GUI_instance.project_root
    params["inventory_row"] = _row_for_path(transfer, path)
    params["logger"] = logger

    return params


def get_applicable_paths(
    transfer: Any,
    *,
    tool_name: str,
    match: MatchSpec,
    logger: Optional[logging.Logger] = None,
    selected: Optional[list[Any]] = None,
) -> list[str]:
    if selected is None:
        selected = list(getattr(transfer, "selected_file_list", []) or [])

    is_applicable = _normalize_match(match, transfer)

    applicable: list[str] = []
    for fp in selected:
        s = str(fp)
        if is_applicable(s):
            applicable.append(s)

    return applicable



def _notify_processing_run_state(transfer: Any, running: bool, tool_name: str = ""):
    """Tell the Qt ProcessingPanel that a background processing job started/finished."""
    panel = _default_parent_from_transfer(transfer)
    if panel is None:
        return

    # Preferred path: emit a Qt signal. Emitting from a worker thread is safe;
    # the connected slot runs in the panel's GUI thread.
    signal = getattr(panel, "processingRunStateRequest", None)
    if signal is not None:
        try:
            signal.emit(bool(running), str(tool_name or ""))
            return
        except Exception:
            pass

    # Fallback for older panels. This is used only from the main thread path.
    try:
        if running and hasattr(panel, "processing_started"):
            panel.processing_started(str(tool_name or ""))
        elif not running and hasattr(panel, "processing_finished"):
            panel.processing_finished()
    except Exception:
        pass

def run_file_processing(
    transfer: Any,
    *,
    tool_name: str,
    process_one: Callable[..., Any],
    ui: RunnerUI,
    behavior: RunnerBehavior = RunnerBehavior(),
    logger: Optional[logging.Logger] = None,
    debug_mode: bool = False,
    tool_params: Optional[dict] = None,
    use_console_window: bool = False,
) -> bool:
    log = _make_logger(tool_name, logger)
    selected = list(getattr(transfer, "selected_file_list", []) or [])

    tool_params = dict(tool_params or {})
    user_params = dict(getattr(transfer, "tool_params", {}) or {})

    match: MatchSpec = tool_params.get("match", lambda _p: True)

    matlab_script_path = tool_params.get("matlab_script_path", None)
    if matlab_script_path is not None:
        matlab_script_path = str(matlab_script_path)

    workers = user_params.get("workers", tool_params.get("workers", 1))
    if isinstance(workers, dict):
        workers = workers.get("default", 1)
    workers = int(workers)

    applicable = get_applicable_paths(
        transfer,
        tool_name=tool_name,
        match=match,
        logger=logger,
        selected=selected,
    )

    effective_params = dict(tool_params)
    effective_params.update(user_params)

    applicable = filter_applicable_by_content_index(applicable, transfer, effective_params)

    if not applicable:
        log.info("[%s] No applicable files in selection (%d selected).", tool_name, len(selected))
        return False

    parent = _default_parent_from_transfer(transfer)

    # Qt version: log inside the Processing right panel instead of opening a Tk window.
    screen = QtProcessingScreen(parent, title=ui.title)

    screen.open(total=len(applicable), initial_text=ui.initial_text)
    _notify_processing_run_state(transfer, True, tool_name)

    # Make sure a file currently displayed by the Qt browser/plot panel is not
    # still open read-only before the worker tries to open it in r+.
    _force_release_nwb_read_handles(transfer, None, screen=screen, log=log)

    def worker():

        def _run_body():
            backend = getattr(transfer, "backend", None)
            if backend is None:
                screen.post("error", {"text": "Processing failed", "detail": "transfer.backend is missing"})
                _notify_processing_run_state(transfer, False, tool_name)
                return

            for i, path in enumerate(applicable, start=1):
                try:
                    screen.post("status", {"text": f"{ui.title} ({i}/{len(applicable)})"})
                    screen.post("file", {"path": str(path)})
                    screen.post("progress", {"i": i - 1})

                    log.info("[%s] ===== FILE %d/%d =====", tool_name, i, len(applicable))
                    log.info("[%s] Start processing: %s", tool_name, path)
                    screen.post("log", {"text": f"Start {i}/{len(applicable)}: {path}"})
                    print(f"[{tool_name}] Start {i}/{len(applicable)}: {path}")

                    log.info("[%s] Building parameters for file", tool_name)

                    params = _build_params_for_file(
                        transfer,
                        tool_name=tool_name,
                        path=str(path),
                        base_params=tool_params,
                        logger=log,
                    )

                    log.info("[%s] Parameters built. Keys: %s", tool_name, sorted(params.keys()))
                    screen.post("log", {"text": "Params built"})
                    print(f"[{tool_name}] Params built for: {path}")

                    lock_id = None
                    nwbfile = None

                    try:
                        if Path(params["file_path"]).suffix.lower() == ".nwb":
                            log.info("[%s] Opening NWB file in r+ with lock: %s", tool_name, params["file_path"])
                            screen.post("log", {"text": "Opening NWB"})
                            print(f"[{tool_name}] Opening NWB: {params['file_path']}")

                            # Release any GUI read-only handle to this exact file just
                            # before opening in r+. This is needed when the file
                            # is currently displayed in the content plot/browser.
                            _force_release_nwb_read_handles(
                                transfer, params["file_path"], screen=screen, log=log
                            )

                            try:
                                nwbfile, lock_id = backend.open(
                                    params["file_path"],
                                    "r+",
                                    lock=True,
                                    lock_owner=f"processing:{tool_name}",
                                )
                            except OSError as e:
                                if "already open for read-only" not in str(e).lower():
                                    raise
                                screen.post("log", {
                                    "text": "NWB file is open read-only in another GUI view; releasing handles and retrying..."
                                })
                                print(f"[{tool_name}] Releasing read-only NWB handles and retrying r+ open")
                                _force_release_nwb_read_handles(
                                    transfer, params["file_path"], screen=screen, log=log
                                )
                                time.sleep(0.25)
                                nwbfile, lock_id = backend.open(
                                    params["file_path"],
                                    "r+",
                                    lock=True,
                                    lock_owner=f"processing:{tool_name}",
                                )

                            backend.begin_write(params["file_path"], lock_id=lock_id)

                            params["nwbfile"] = nwbfile

                            log.info("[%s] NWB file opened successfully. lock_id=%r", tool_name, lock_id)
                            screen.post("log", {"text": "NWB opened"})
                            print(f"[{tool_name}] NWB opened")

                        log.info("[%s] Materializing selected params", tool_name)
                        params = _materialize_selected_params(params, transfer=transfer, logger=log)
                        log.info("[%s] Selected params materialized", tool_name)
                        screen.post("log", {"text": "Selected params materialized"})
                        print(f"[{tool_name}] Selected params materialized")

                        log.info("[%s] Calling process_one()", tool_name)
                        screen.post("log", {"text": "Running processing function"})
                        print(f"[{tool_name}] Running processing function")

                        returned_data = process_one(**params)

                        log.info("[%s] process_one() returned type=%s", tool_name, type(returned_data).__name__)
                        screen.post("log", {"text": f"Processing finished. Returned: {type(returned_data).__name__}"})
                        print(f"[{tool_name}] Processing finished. Returned: {type(returned_data).__name__}")

                        if returned_data is None:
                            log.info("[%s] returned_data is None for %s", tool_name, params["file_path"])
                            screen.post("log", {"text": "No output data returned for file"})
                            print(f"[{tool_name}] No output data returned for file")

                        if returned_data is not None and nwbfile is not None:
                            category = params.get("category", "General")
                            log.info("[%s] Writing output to processing module '%s'", tool_name, category)
                            screen.post("log", {"text": f"Writing result to processing/{category}"})
                            print(f"[{tool_name}] Writing result to processing/{category}")

                            if category not in nwbfile.processing:
                                log.info("[%s] Creating processing module '%s'", tool_name, category)
                                screen.post("log", {"text": f"Creating processing module: {category}"})
                                print(f"[{tool_name}] Creating processing module: {category}")
                                mod = nwbfile.create_processing_module(
                                    name=category,
                                    description=f"{category} processing results",
                                )
                            else:
                                mod = nwbfile.processing[category]
                                log.info("[%s] Using existing processing module '%s'", tool_name, category)

                            if not isinstance(returned_data, tuple):
                                returned_data = [returned_data]

                            save = False
                            for current_data in returned_data:
                                if isinstance(current_data, dict):
                                    log.info(
                                        "[%s] %s is dict. Converting to TimeSeries",
                                        tool_name,
                                        current_data["name"],
                                    )
                                    screen.post("log", {"text": "Converting dict to TimeSeries"})
                                    print(f"[{tool_name}] Converting dict to TimeSeries")
                                    current_data = TimeSeries(**current_data)

                                log.info("[%s] Adding %s to processing module", tool_name, current_data.name)
                                screen.post("log", {"text": "Adding data to NWB module"})
                                print(f"[{tool_name}] Adding data to NWB module")

                                if current_data.name in mod.keys():
                                    screen.post("log", {"text": f"{current_data.name} already in {category} of current file and could not be saved"})
                                    print(f"{current_data.name} already in {category} of current file and could not be saved")
                                    continue
                                else:
                                    mod.add(current_data)
                                    save = True

                            if save:
                                log.info("[%s] Writing NWB to disk", tool_name)
                                screen.post("log", {"text": "Writing NWB file"})
                                print(f"[{tool_name}] Writing NWB file")
                                backend.write(nwbfile, lock_id=lock_id, close_after=False)
                                log.info("[%s] NWB write complete", tool_name)
                                screen.post("log", {"text": "NWB write complete"})
                                print(f"[{tool_name}] NWB write complete")

                                log.info("[%s] Updating inventory after processing", tool_name)
                                screen.post("log", {"text": "Updating inventory"})
                                print(f"[{tool_name}] Updating inventory")
                                update_inventory_after_processing(
                                    transfer=transfer,
                                    file_path=params["file_path"],
                                    returned_data=returned_data,
                                    category=category,
                                    tool_params=tool_params,
                                    user_params=params,
                                )
                                log.info("[%s] Inventory update complete", tool_name)
                                screen.post("log", {"text": "Inventory updated"})
                                print(f"[{tool_name}] Inventory updated")

                            else:
                                log.info("[%s] file already contained all exctrated data and was not modified", tool_name)
                                screen.post("log", {"text": "file already contained all exctrated data and was not modified"})
                                print(f"[{tool_name}] file already contained all exctrated data and was not modified")

                        elif returned_data is not None and nwbfile is None:
                            log.warning(
                                "[%s] returned_data is not None but nwbfile is None for path=%s",
                                tool_name,
                                params["file_path"],
                            )
                            screen.post("log", {"text": "Warning: result returned but no NWB file was open"})
                            print(f"[{tool_name}] Warning: result returned but no NWB file was open")

                    finally:
                        if lock_id is not None:
                            try:
                                backend.end_write(lock_id=lock_id)
                            except Exception:
                                pass
                            try:
                                backend.close(lock_id=lock_id)
                            except Exception:
                                pass

                    screen.post("progress", {"i": i})
                    log.info("[%s] Done %d/%d: %s", tool_name, i, len(applicable), path)
                    screen.post("log", {"text": f"Done {i}/{len(applicable)}: {path}"})
                    print(f"[{tool_name}] Done {i}/{len(applicable)}: {path}")
                    screen.post("log", {"text": "\n"})
                    print("\n")

                except Exception as e:
                    tb = traceback.format_exc()

                    log.error("[%s] Processing failed for %s\n%s", tool_name, path, tb)

                    screen.post("log", {
                        "text": f"Error while processing {path}:\n{str(e)}"
                    })

                    screen.post("log", {
                        "text": tb
                    })

                    if not behavior.continue_on_error:
                        screen.post("error", {
                            "text": "Processing failed",
                            "detail": str(e),
                        })
                        _notify_processing_run_state(transfer, False, tool_name)
                        return
                    else:
                        continue

            log.info("[%s] All files processed successfully", tool_name)
            print(f"[{tool_name}] All files processed successfully")

            gui = getattr(transfer, "GUI_instance", None) or transfer
            def _finish_on_gui_thread():
                try:
                    if hasattr(gui, "reload_inventory"):
                        gui.reload_inventory()
                except Exception:
                    screen.post("log", {"text": traceback.format_exc()})
                screen.post("done", {"text": "Done!"})
                _notify_processing_run_state(transfer, False, tool_name)

            if QtCore is not None:
                try:
                    QtCore.QTimer.singleShot(0, _finish_on_gui_thread)
                except Exception:
                    _finish_on_gui_thread()
            else:
                _finish_on_gui_thread()

        _run_body()

    def _start_threaded_worker():
        threading.Thread(target=worker, daemon=True).start()

    try:
        if not debug_mode:
            threading.Thread(target=worker, daemon=True).start()
        else:
            worker()

    except Exception as e:
        tb = traceback.format_exc()

        log.error("[%s] Processing failed\n%s", tool_name, tb)

        screen.post("log", {
            "text": f"Error while processing {tool_name}:\n{str(e)}"
        })

        screen.post("log", {
            "text": tb
        })
        _notify_processing_run_state(transfer, False, tool_name)

    return True


def update_inventory_after_processing(
    transfer,
    file_path,
    returned_data,
    category,
    tool_params,
    user_params,
):
    import pandas as pd
    from pathlib import Path
    from datetime import datetime

    inv_df = getattr(transfer, "NWB_inventory_df", None)
    content_df = getattr(transfer, "NWB_content_df", None)
    inv_path = getattr(transfer, "NWB_inventory_path", None)

    trial_filter_index_df = getattr(transfer, "NWB_trial_filter_index_df", None)
    trial_filter_catalog_df = getattr(transfer, "NWB_trial_filter_catalog_df", None)

    if inv_df is None or content_df is None or inv_path is None:
        return

    inv_path = Path(inv_path)
    file_path = str(file_path)
    category = str(category or "").strip() or "General"

    # Safety for the Qt app: app.df can be a filtered visible table. Before
    # writing nwb_inventory.xlsx, reload the full on-disk tables and use them as
    # the base when they contain more rows than the transfer tables. This
    # prevents a filtered processing run from truncating the inventory workbook.
    try:
        if inv_path.exists():
            disk_inv_df = _pp_read_excel(inv_path, sheet_name="inventory")
            if isinstance(disk_inv_df, pd.DataFrame):
                if not isinstance(inv_df, pd.DataFrame) or len(disk_inv_df) > len(inv_df):
                    inv_df = disk_inv_df.copy()
    except Exception:
        pass
    try:
        if inv_path.exists():
            disk_content_df = _pp_read_excel(inv_path, sheet_name="content_index")
            if isinstance(disk_content_df, pd.DataFrame):
                if not isinstance(content_df, pd.DataFrame) or len(disk_content_df) > len(content_df):
                    content_df = disk_content_df.copy()
    except Exception:
        pass

    if not isinstance(returned_data, (list, tuple)):
        returned_data = [returned_data]

    saving_names = _collect_internal_saving_names(tool_params, user_params=user_params)

    new_rows = []
    added_names = []
    added_count = 0

    for i, current_data in enumerate(returned_data):
        if current_data is None:
            continue

        if isinstance(current_data, dict):
            data_name = str(current_data.get("name", "")).strip()
            neurodata_type = str(current_data.get("neurodata_type", "TimeSeries")).strip() or "TimeSeries"
        else:
            data_name = str(getattr(current_data, "name", "")).strip()
            neurodata_type = str(
                getattr(current_data, "neurodata_type", type(current_data).__name__)
            ).strip()

        if i < len(saving_names) and saving_names[i]:
            save_name = str(saving_names[i]).strip()
        elif data_name:
            save_name = data_name
        else:
            save_name = f"output_{i + 1}"

        if not save_name:
            continue

        new_rows.append({
            "file_path": file_path,
            "nwb_path": f"processing/{category}/{save_name}",
            "container": "processing",
            "module": category,
            "name": save_name,
            "neurodata_type": neurodata_type,
        })
        added_names.append(save_name)
        added_count += 1

    if not new_rows:
        return

    for col in ["file_path", "nwb_path", "container", "module", "name", "neurodata_type"]:
        if col not in content_df.columns:
            content_df[col] = None

    old_mask = (
        (content_df["file_path"].astype(str) == file_path)
        & (content_df["container"].astype(str).str.lower() == "processing")
        & (content_df["module"].astype(str) == category)
        & (content_df["name"].astype(str).isin([str(x) for x in added_names]))
    )
    content_df = content_df.loc[~old_mask].copy()

    content_df = pd.concat(
        [content_df, pd.DataFrame(new_rows)],
        ignore_index=True,
    )

    content_df = content_df.drop_duplicates()

    dedup_cols = [c for c in ["file_path", "container", "module", "name"] if c in content_df.columns]
    if dedup_cols:
        content_df = content_df.drop_duplicates(subset=dedup_cols, keep="last")

    transfer.NWB_content_df = content_df.reset_index(drop=True)

    if "file_path" in inv_df.columns:
        idx = inv_df["file_path"].astype(str) == file_path

        if "n_processing_objects" in inv_df.columns and idx.any():
            proc_mask = (
                (transfer.NWB_content_df["file_path"].astype(str) == file_path)
                & (transfer.NWB_content_df["container"].astype(str).str.lower() == "processing")
            )
            inv_df.loc[idx, "n_processing_objects"] = int(proc_mask.sum())

    transfer.NWB_inventory_df = inv_df

    # Prefer the real Qt app when present. transfer.GUI_instance may be a
    # Tk-compatible adapter used by legacy processing tools.
    gui = getattr(transfer, "qt_GUI_instance", None) or getattr(transfer, "GUI_instance", None)
    if gui is not None:
        try:
            gui.full_df = transfer.NWB_inventory_df.copy()
            gui._df_master = transfer.NWB_inventory_df.copy()
            if not getattr(gui, "current_file_filters", None):
                gui.df = transfer.NWB_inventory_df.copy()
        except Exception:
            try:
                gui.df = transfer.NWB_inventory_df
            except Exception:
                pass
        try:
            gui.content_df = transfer.NWB_content_df.copy()
            gui._content_df_master = transfer.NWB_content_df.copy()
        except Exception:
            try:
                gui.content_df = transfer.NWB_content_df
            except Exception:
                pass

        if trial_filter_index_df is not None:
            try:
                gui.trial_filter_index_df = trial_filter_index_df
            except Exception:
                pass
        if trial_filter_catalog_df is not None:
            try:
                gui.trial_filter_catalog_df = trial_filter_catalog_df
            except Exception:
                pass

    if trial_filter_index_df is None or trial_filter_catalog_df is None:
        try:
            xl = pd.ExcelFile(inv_path)

            if trial_filter_index_df is None and "trial_filter_index" in xl.sheet_names:
                trial_filter_index_df = _pp_read_excel(inv_path, sheet_name="trial_filter_index")

            if trial_filter_catalog_df is None and "trial_filter_catalog" in xl.sheet_names:
                trial_filter_catalog_df = _pp_read_excel(inv_path, sheet_name="trial_filter_catalog")
        except Exception:
            pass

    meta_df = None
    try:
        xl = pd.ExcelFile(inv_path)
        if "meta" in xl.sheet_names:
            meta_df = _pp_read_excel(inv_path, sheet_name="meta")
    except Exception:
        meta_df = None

    if meta_df is None or meta_df.empty:
        meta_df = pd.DataFrame([{"updated_at": datetime.now()}])
    else:
        if "updated_at" not in meta_df.columns:
            meta_df["updated_at"] = None
        meta_df.loc[0, "updated_at"] = datetime.now()

    with pd.ExcelWriter(inv_path, engine="openpyxl", mode="w") as writer:
        transfer.NWB_inventory_df.to_excel(writer, sheet_name="inventory", index=False)
        transfer.NWB_content_df.to_excel(writer, sheet_name="content_index", index=False)

        if trial_filter_index_df is not None:
            trial_filter_index_df.to_excel(writer, sheet_name="trial_filter_index", index=False)

        if trial_filter_catalog_df is not None:
            trial_filter_catalog_df.to_excel(writer, sheet_name="trial_filter_catalog", index=False)

        if meta_df is not None:
            meta_df.to_excel(writer, sheet_name="meta", index=False)


def _resolve_timeseries_ref(nwbfile, ref: str):
    if nwbfile is None:
        return None

    s = str(ref).strip()
    if not s:
        return None

    if ":" not in s:
        obj = nwbfile.acquisition.get(s)
        if obj is not None:
            return obj

        for _mod_name, mod in getattr(nwbfile, "processing", {}).items():
            di = getattr(mod, "data_interfaces", {})
            if s in di:
                return di.get(s)

        return None

    parts = s.split(":")
    if len(parts) < 2:
        return None

    if parts[0] == "acquisition" and len(parts) == 2:
        key = parts[1]
        return nwbfile.acquisition.get(key)

    if parts[0] == "processing":
        if len(parts) >= 3:
            mod_name = parts[1]
            key = ":".join(parts[2:])
            mod = getattr(nwbfile, "processing", {}).get(mod_name)
            if mod is None:
                return None
            return getattr(mod, "data_interfaces", {}).get(key)

        if len(parts) == 2:
            key = parts[1]
            for _mod_name, mod in getattr(nwbfile, "processing", {}).items():
                di = getattr(mod, "data_interfaces", {})
                if key in di:
                    return di.get(key)

    return None


def _materialize_selected_params(params: dict, *, transfer: Any, logger: logging.Logger) -> dict:
    specs = getattr(transfer, "tool_param_specs", None)
    if not isinstance(specs, dict) or not specs:
        return params

    nwbfile = params.get("nwbfile", None)

    for pname, ps in specs.items():
        ps = dict(ps or {})
        if ps.get("type") not in ("nwb_timeseries", "data"):
            continue
        if pname not in params:
            continue

        raw = params[pname]
        ref_str = raw if isinstance(raw, str) else None

        if isinstance(raw, str):
            obj = _resolve_timeseries_ref(nwbfile, raw)
        else:
            obj = raw

        if obj is None:
            params[pname] = None
            params[pname + "_name"] = ref_str or ""
            continue

        if ref_str is None:
            ref_str = str(getattr(obj, "name", "") or "")
        params[pname + "_name"] = ref_str

        if ps.get("load_data", False):
            params[pname] = obj.data[:]
        else:
            params[pname] = obj

    return params


def _normalize_timeseries_ref(ref: str) -> set[str]:
    if not isinstance(ref, str):
        return set()

    r = ref.strip()
    if not r:
        return set()

    out = {r}

    if ":" in r:
        out.add(r.replace(":", "/"))

    if ":" in r:
        parts = r.split(":")
        if len(parts) >= 3 and parts[0] == "processing":
            out.add("/".join(parts))
        elif len(parts) >= 2:
            out.add("/".join(parts))

    for x in list(out):
        if "/" in x:
            out.add(x.split("/")[-1])
        if ":" in x:
            out.add(x.split(":")[-1])

    return {x.strip() for x in out if x.strip()}


def _collect_internal_saving_names(tool_params: dict, user_params: dict | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    tp = dict(tool_params or {})
    up = dict(user_params or {})

    direct = up.get("internal_saving_name_list", None)
    if direct is not None:
        if isinstance(direct, (list, tuple, set)):
            vals = [str(x).strip() for x in direct if str(x).strip()]
        else:
            vals = [str(direct).strip()] if str(direct).strip() else []

        for v in vals:
            if v not in seen:
                seen.add(v)
                out.append(v)

        if out:
            return out

    for pname, spec in tp.items():
        if not (
            isinstance(spec, dict)
            and str(spec.get("type", "")).strip() == "internal_saving_name_list"
        ):
            continue

        raw = up.get(pname, spec.get("default", None))
        if raw is None:
            continue

        if isinstance(raw, (list, tuple, set)):
            vals = [str(x).strip() for x in raw if str(x).strip()]
        else:
            vals = [str(raw).strip()] if str(raw).strip() else []

        for v in vals:
            if v not in seen:
                seen.add(v)
                out.append(v)

    return out


def _skip_if_output_exists(
    applicable: list[str],
    transfer: Any,
    tool_params: dict,
    user_params: dict | None = None,
) -> list[str]:
    if pd is None or not applicable:
        return list(applicable)

    cdf = getattr(transfer, "NWB_content_df", None)
    if cdf is None or not isinstance(cdf, pd.DataFrame) or cdf.empty:
        return list(applicable)

    if "file_path" not in cdf.columns or "name" not in cdf.columns:
        return list(applicable)

    tp_user = dict(user_params or getattr(transfer, "tool_params", {}) or {})
    saving_names = _collect_internal_saving_names(tool_params, tp_user)
    if not saving_names:
        return list(applicable)

    category = tp_user.get("category", "")
    if isinstance(category, dict):
        category = category.get("default", "")
    category = str(category).strip()

    cand = set(map(str, applicable))
    cdf2 = cdf[cdf["file_path"].astype(str).isin(cand)].copy()
    if cdf2.empty:
        return list(applicable)

    if "container" in cdf2.columns:
        cdf2 = cdf2[cdf2["container"].astype(str).str.lower() == "processing"]

    if category:
        for mod_col in ("module", "category", "processing_module", "processing"):
            if mod_col in cdf2.columns:
                cdf2 = cdf2[cdf2[mod_col].astype(str).str.strip() == category]
                break

    if cdf2.empty:
        return list(applicable)

    names_by_file = (
        cdf2.groupby(cdf2["file_path"].astype(str))["name"]
        .apply(lambda s: {str(x).strip() for x in s.dropna().astype(str)})
        .to_dict()
    )

    required = set(saving_names)
    existing_files = {
        fp for fp, present in names_by_file.items()
        if required.issubset(present)
    }

    return [p for p in applicable if str(p) not in existing_files]


def filter_applicable_by_content_index(applicable: list[str], transfer: Any, tool_params: dict) -> list[str]:
    if pd is None or not applicable:
        return list(applicable)

    cdf = getattr(transfer, "NWB_content_df", None)
    if cdf is None or not isinstance(cdf, pd.DataFrame) or cdf.empty:
        return list(applicable)

    if "file_path" not in cdf.columns:
        return list(applicable)

    applicable_str = [str(p) for p in applicable]
    cdf2 = cdf[cdf["file_path"].astype(str).isin(applicable_str)].copy()

    if cdf2.empty:
        return list(applicable)

    keep = set(cdf2["file_path"].astype(str).unique())

    required_refs: list[str] = []
    allow_types: set[str] = set()

    top_req = (tool_params or {}).get("data_type_requirement", None)
    if isinstance(top_req, str) and top_req.strip():
        required_refs.append(top_req.strip())
    elif isinstance(top_req, (list, tuple, set)):
        required_refs.extend(str(x).strip() for x in top_req if str(x).strip())

    user_values = dict(getattr(transfer, "tool_params", {}) or {})

    for pname, spec in (tool_params or {}).items():
        if not (isinstance(spec, dict) and "type" in spec):
            continue

        ptype = str(spec.get("type", "")).strip()
        if ptype not in ("data", "nwb_timeseries"):
            continue

        req = spec.get("data_type_requirement", None)
        if isinstance(req, str) and req.strip():
            required_refs.append(req.strip())
        elif isinstance(req, (list, tuple, set)):
            required_refs.extend(str(x).strip() for x in req if str(x).strip())

        nts = spec.get("neurodata_type_in", None)
        if isinstance(nts, (list, tuple, set)):
            allow_types |= {str(x).strip() for x in nts if str(x).strip()}

        cur_val = user_values.get(pname, None)

        if isinstance(cur_val, str):
            cur_val = cur_val.strip()
            if cur_val and cur_val not in ("0", "1", "2", "3", "None", "none"):
                required_refs.append(cur_val)

    seen = set()
    required_refs = [x for x in required_refs if not (x in seen or seen.add(x))]

    for ref in required_refs:
        variants = _normalize_timeseries_ref(ref)
        if not variants:
            continue

        mask = pd.Series(False, index=cdf2.index)

        if "nwb_path" in cdf2.columns:
            mask = mask | cdf2["nwb_path"].astype(str).str.strip().isin(variants)

        if "name" in cdf2.columns:
            mask = mask | cdf2["name"].astype(str).str.strip().isin(variants)

        hits = set(cdf2.loc[mask, "file_path"].astype(str).unique())
        keep &= hits

        if not keep:
            break

        cdf2 = cdf2[cdf2["file_path"].astype(str).isin(keep)]

    if not required_refs and keep and allow_types and "neurodata_type" in cdf2.columns:
        ndt_hits = set(
            cdf2.loc[
                cdf2["neurodata_type"].astype(str).str.strip().isin(allow_types),
                "file_path",
            ].astype(str).unique()
        )
        keep &= ndt_hits

    filtered = [p for p in applicable if str(p) in keep] if (required_refs or allow_types) else list(applicable)

    return _skip_if_output_exists(
        filtered,
        transfer,
        tool_params,
        getattr(transfer, "tool_params", {}) or {},
    )


def make_file_tool(
    *,
    name: str,
    description: str,
    process_one: Callable[..., Any],
    ui_title: Optional[str] = None,
    ui_initial_text: str = "Preparing...",
    continue_on_error: bool = False,
    params: Optional[dict] = None,
    debug_mode: bool = False,
    use_console_window: bool = False,
) -> dict:
    tool_name = name
    ui = RunnerUI(title=ui_title or name, initial_text=ui_initial_text)
    behavior = RunnerBehavior(continue_on_error=continue_on_error)

    base_params = dict(params or {})

    match = base_params.get("match", {})

    if isinstance(match, dict) and "file_type" not in match:
        match = dict(match)
        match["file_type"] = lambda v: str(v).lower() == "nwb"

    base_params["match"] = match

    module_name = getattr(process_one, "__module__", "") or ""
    parts = module_name.split(".")
    category = parts[1] if len(parts) >= 2 else "General"

    base_params.setdefault("category", category)
    base_params.setdefault("tool_module", module_name)

    def _entrypoint(transfer: Any):
        return run_file_processing(
            transfer,
            tool_name=tool_name,
            process_one=process_one,
            ui=ui,
            behavior=behavior,
            debug_mode=debug_mode,
            tool_params=base_params,
            use_console_window=use_console_window,
        )

    return {
        "name": name,
        "description": description,
        "func": _entrypoint,
        "params": base_params,
    }