from __future__ import annotations

import copy
import inspect
import json
import os
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import contextlib

import pandas as pd

from GUI.qt_compat import (
    QtCore,
    QtGui,
    QtWidgets,
    qt_checked,
    qt_unchecked,
    qt_user_role,
    set_item_foreground,
)
from GUI.processing_discovery import discover_tools
from GUI.processing_runner import (
    RunnerBehavior,
    RunnerUI,
    _collect_internal_saving_names,
    _processing_output_category,
    _processing_replacement_key,
    filter_applicable_by_content_index,
    get_applicable_paths,
    processing_output_conflicts,
    run_file_processing,
    _skip_if_output_exists,
)
from GUI.pipeline_store import PipelineStore, utc_now_text
from GUI.project_script_loader import discover_for_panel

try:
    from Tools.General.nwb_backend import NWBBackend
except Exception:
    NWBBackend = None


# Parameters present in tool specs but not meant to be edited by the user.
params_variable_not_be_displayed_in_GUI_list = [
    "match",
    "matlab_script_path",
    "tool_module",
    "category",
    "multiprocessing",
    "debug_mode",
]


def _script_metadata_from_spec(spec: Optional[dict]) -> dict:
    """Return a stable display name and absolute source path for a tool."""
    spec = spec or {}
    func = spec.get("func")
    source = ""
    if callable(func):
        try:
            source = str(inspect.getsourcefile(func) or inspect.getfile(func) or "")
        except Exception:
            source = ""

    module_value = str(spec.get("module", "") or "").strip()
    if not source and module_value and (
        module_value.lower().endswith(".py")
        or "/" in module_value
        or "\\" in module_value
    ):
        source = module_value

    if source:
        try:
            location = str(Path(source).expanduser().resolve())
        except Exception:
            location = source
        name = Path(source.replace("\\", "/")).name or source
        return {"script_name": name, "script_path": location}

    if module_value:
        module_leaf = module_value.rsplit(".", 1)[-1]
        name = module_leaf if module_leaf.lower().endswith(".py") else f"{module_leaf}.py"
        return {"script_name": name, "script_path": module_value}
    return {"script_name": "Unknown", "script_path": "Unknown"}


def _copy_dataframe(value):
    if isinstance(value, pd.DataFrame):
        return value.copy(deep=True)
    return value


def _full_inventory_df(app):
    """Return the unfiltered inventory dataframe used for writing to disk.

    In the Qt app, app.df is the currently visible/filtered inventory. The
    complete inventory is stored in app.full_df/_df_master. Processing tools
    must always receive the complete dataframe; otherwise saving after a
    filtered run can overwrite nwb_inventory.xlsx with only the filtered rows.
    """
    for attr in ("full_df", "_df_master", "df"):
        val = getattr(app, attr, None)
        if isinstance(val, pd.DataFrame):
            return val.copy(deep=True)
    return pd.DataFrame()


def _full_content_df(app):
    for attr in ("_content_df_master", "content_df"):
        val = getattr(app, attr, None)
        if isinstance(val, pd.DataFrame):
            return val.copy(deep=True)
    return pd.DataFrame()


def _run_date_text(value: Any) -> str:
    return str(value or "").replace("T", " ", 1)


@contextlib.contextmanager
def _suppress_success_popups():
    """Suppress end-of-run informational popups from legacy project actions.

    Some inventory-scan/update functions still call QMessageBox.information()
    or tkinter.messagebox.showinfo() when they finish. In the Qt workbench this
    is annoying and, more importantly, it can make the refresh sequence look as
    if files disappeared until the app is restarted. Errors and warnings are not
    suppressed.
    """
    qt_old_information = getattr(QtWidgets.QMessageBox, "information", None)

    def _qt_no_info(*_args, **_kwargs):
        try:
            return QtWidgets.QMessageBox.StandardButton.Ok
        except AttributeError:
            return QtWidgets.QMessageBox.Ok

    tk_messagebox = None
    tk_old_showinfo = None
    try:
        import tkinter.messagebox as tk_messagebox  # type: ignore
        tk_old_showinfo = getattr(tk_messagebox, "showinfo", None)
    except Exception:
        tk_messagebox = None
        tk_old_showinfo = None

    qt_patched = False
    tk_patched = False
    try:
        if callable(qt_old_information):
            try:
                QtWidgets.QMessageBox.information = staticmethod(_qt_no_info)
                qt_patched = True
            except Exception:
                qt_patched = False
        if tk_messagebox is not None and callable(tk_old_showinfo):
            try:
                tk_messagebox.showinfo = lambda *_args, **_kwargs: "ok"
                tk_patched = True
            except Exception:
                tk_patched = False
        yield
    finally:
        if qt_patched and callable(qt_old_information):
            try:
                QtWidgets.QMessageBox.information = qt_old_information
            except Exception:
                pass
        if tk_patched and tk_messagebox is not None and callable(tk_old_showinfo):
            try:
                tk_messagebox.showinfo = tk_old_showinfo
            except Exception:
                pass


class _QtAsTkParentAdapter:
    """Delegate app attributes but make Tk-only parent checks fail safely.

    Some existing processing functions still pass ``transfer.GUI_instance`` as a
    Tk parent to legacy helpers. Those helpers usually start with
    ``parent.winfo_exists()`` before creating Tk progress windows. The Qt main
    window has no ``winfo_exists`` method, so we provide one returning False.

    Important: app.df is the filtered/visible inventory in the Qt app. Legacy
    processing code expects parent.df to be the complete inventory, especially
    when it writes nwb_inventory.xlsx. Therefore the adapter exposes full_df as
    df and keeps writes synchronized back to the real app's full inventory.
    """

    def __init__(self, app):
        object.__setattr__(self, "_app", app)

    def __getattr__(self, name):
        app = object.__getattribute__(self, "_app")
        if name in ("df", "full_df", "_df_master"):
            return _full_inventory_df(app)
        if name in ("content_df", "_content_df_master"):
            return _full_content_df(app)
        return getattr(app, name)

    def __setattr__(self, name, value):
        app = object.__getattribute__(self, "_app")
        if name in ("df", "full_df", "_df_master") and isinstance(value, pd.DataFrame):
            full = value.copy(deep=True)
            setattr(app, "full_df", full.copy(deep=True))
            setattr(app, "_df_master", full.copy(deep=True))
            # Do not blindly replace app.df when filters are active; that is the
            # filtered view. The visible view is refreshed after the run.
            if not getattr(app, "current_file_filters", None):
                setattr(app, "df", full.copy(deep=True))
            return
        if name in ("content_df", "_content_df_master") and isinstance(value, pd.DataFrame):
            full = value.copy(deep=True)
            setattr(app, "content_df", full.copy(deep=True))
            setattr(app, "_content_df_master", full.copy(deep=True))
            return
        setattr(app, name, value)

    def winfo_exists(self):
        return False

    def winfo_toplevel(self):
        return self

    def update_idletasks(self):
        return None

    def update(self):
        return None


class Processing_data_transfer:
    def __init__(self, GUI_instance, ProcessingPanel, selected_file_list, processing_name: str):
        self.qt_GUI_instance = GUI_instance
        self.GUI_instance = _QtAsTkParentAdapter(GUI_instance)

        # Use a dedicated backend for processing. The main GUI backend can have
        # the selected file open read-only for plotting/browsing; trying to open
        # the same HDF5 file in r+ through that shared handle can fail with
        # "file is already open for read-only". The runner will still release
        # GUI read-only handles before writing, but processing itself should own
        # its own backend/lock lifecycle.
        if NWBBackend is not None:
            self.backend = NWBBackend()
        else:
            self.backend = GUI_instance.backend

        self.NWB_inventory_path = GUI_instance.NWB_inventory_path
        # Use the complete inventory/content tables, never the currently
        # filtered visible app.df. This prevents processing writes from
        # replacing nwb_inventory.xlsx with only the filtered rows.
        self.NWB_inventory_df = _full_inventory_df(GUI_instance)
        self.NWB_content_df = _full_content_df(GUI_instance)
        self.NWB_trial_filter_catalog_df = _copy_dataframe(getattr(GUI_instance, "trial_filter_catalog_df", None))

        self.ProcessingUI_instance = ProcessingPanel
        self.selected_file_list = selected_file_list
        self.processing_name = processing_name

        self.tool_params: dict[str, Any] = {}
        self.tool_param_specs: dict[str, dict] = {}
        self.replace_existing_data = False
        self.approved_replacements: set[str] = set()


class ProcessingPanel(QtWidgets.QWidget):
    """Qt migration of the Tk Processing tab.

    Left/middle tab: available processing tools by category.
    Right panel: tool description, dynamic parameters, availability, run log.
    """

    log_message = QtCore.Signal(str)
    log_reset = QtCore.Signal(str)
    runningStateChanged = QtCore.Signal(bool)
    processingRunStateRequest = QtCore.Signal(bool, str)
    processingFileFinishedRequest = QtCore.Signal(object, str)
    processingFinishedRequest = QtCore.Signal(object, str)

    def __init__(self, parent=None, *, right_panel, GUI_instance, on_run_done=None):
        super().__init__(parent)
        self.right_panel = right_panel
        self.GUI_instance = GUI_instance
        self.on_run_done = on_run_done

        self.registry: dict[str, dict[str, dict]] = {}
        self.target_paths: list[str] = []
        self._selected_name: Optional[str] = None
        self._selected_spec: Optional[dict] = None
        self._replace_option_tool_name: Optional[str] = None

        self._item_to_ref: dict[int, tuple[str, str]] = {}
        self._ts_display_to_full_by_param: Dict[str, Dict[str, str]] = {}
        self._list_display_to_value_by_param: Dict[str, Dict[str, Any]] = {}
        self.param_widgets: dict[str, Any] = {}
        self.param_specs: dict[str, dict] = {}
        self._running = False
        self._running_tool_name = ""
        self.pipeline_store = PipelineStore(
            getattr(self.GUI_instance, "project_root", "."),
            "processing",
        )
        self._pipeline_item_to_id: dict[int, str] = {}
        self._pipeline_step_item_to_ref: dict[int, tuple[str, str]] = {}
        self._pipeline_id_to_item: dict[str, Any] = {}
        self._pipeline_step_ref_to_item: dict[tuple[str, str], Any] = {}
        self._pipeline_root_item = None
        # Expansion belongs to each pipeline independently.  Keeping the IDs
        # outside the QTreeWidget means a full tree rebuild cannot reopen all
        # pipelines or lose the user's individual expand/collapse choices.
        self._expanded_pipeline_ids: set[str] = set()
        self._active_pipeline_id: Optional[str] = None
        self._active_pipeline_step_id: Optional[str] = None
        self._editing_pipeline_step: Optional[tuple[str, str]] = None
        self._pipeline_run: Optional[dict] = None
        self._last_launch_error = ""
        self._availability_timer = QtCore.QTimer(self)
        self._availability_timer.setSingleShot(True)
        self._availability_timer.setInterval(200)
        self._availability_timer.timeout.connect(self._refresh_availability_display)

        self._build_middle_ui()
        self._build_right_ui()

        self.log_reset.connect(self._reset_log_on_gui_thread)
        self.log_message.connect(self._append_log_on_gui_thread)
        self.processingRunStateRequest.connect(self._set_processing_running)
        self.processingFileFinishedRequest.connect(self._refresh_after_processed_file)
        self.processingFinishedRequest.connect(self._finish_processing_from_runner)

        self.set_registry(self._discover_processing_tools())

    # ---------------- UI construction ----------------

    def _build_middle_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        title = QtWidgets.QLabel("Processing")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self.jobs_label = QtWidgets.QLabel("", self)
        self.jobs_label.setVisible(False)
        self.jobs_label.setWordWrap(True)
        self.jobs_label.setStyleSheet(
            "QLabel { background: #f6faf7; color: #2e7d32; "
            "border: 1px solid #cfe8d2; border-radius: 4px; padding: 3px 5px; }"
        )
        layout.addWidget(self.jobs_label)

        self.proc_tree = QtWidgets.QTreeWidget(self)
        self.proc_tree.setHeaderHidden(True)
        self.proc_tree.itemSelectionChanged.connect(self._on_select)
        self.proc_tree.itemExpanded.connect(self._on_workspace_item_expanded)
        self.proc_tree.itemCollapsed.connect(self._on_workspace_item_collapsed)
        layout.addWidget(self.proc_tree, stretch=1)

    def _build_right_ui(self):
        cont = self.right_panel.processing_container
        lay = self.right_panel.processing_container_layout
        self.right_panel.clear_layout(lay)

        self.details_widget = QtWidgets.QWidget(cont)
        main = QtWidgets.QVBoxLayout(self.details_widget)
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(8)
        lay.addWidget(self.details_widget)

        self.title_lbl = QtWidgets.QLabel("")
        self.title_lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
        main.addWidget(self.title_lbl)

        self.applies_lbl = QtWidgets.QLabel("")
        self.applies_lbl.setStyleSheet("color: #444;")
        main.addWidget(self.applies_lbl)

        self.available_lbl = QtWidgets.QLabel("Available files: 0")
        self.available_lbl.setStyleSheet("color: #444;")
        main.addWidget(self.available_lbl)

        desc_title = QtWidgets.QLabel("Description:")
        desc_title.setStyleSheet("font-weight: bold;")
        main.addWidget(desc_title)

        self.desc_lbl = QtWidgets.QLabel("")
        self.desc_lbl.setWordWrap(True)
        main.addWidget(self.desc_lbl)

        self.params_box = QtWidgets.QGroupBox("Parameters")
        params_box_layout = QtWidgets.QVBoxLayout(self.params_box)
        params_box_layout.setContentsMargins(8, 8, 8, 8)
        self.params_error_lbl = QtWidgets.QLabel("")
        self.params_error_lbl.setWordWrap(True)
        self.params_error_lbl.setStyleSheet("color: #b00020;")
        params_box_layout.addWidget(self.params_error_lbl)
        self.params_form_widget = QtWidgets.QWidget(self.params_box)
        self.params_form = QtWidgets.QFormLayout(self.params_form_widget)
        self.params_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        params_box_layout.addWidget(self.params_form_widget)
        main.addWidget(self.params_box)

        self.replace_existing_chk = QtWidgets.QCheckBox(
            "Replace existing processed data with the same name"
        )
        self.replace_existing_chk.setToolTip(
            "Recompute the output with the current parameters and replace an "
            "existing object in the same NWB processing module. A confirmation "
            "listing every affected file is shown before processing starts."
        )
        self.replace_existing_chk.stateChanged.connect(self._on_param_change)
        self.replace_existing_chk.setVisible(False)
        main.addWidget(self.replace_existing_chk)

        run_row = QtWidgets.QHBoxLayout()
        main.addLayout(run_row)

        self.run_btn = QtWidgets.QPushButton("Run")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._run_selected)
        self.run_btn.setMinimumWidth(120)
        run_row.addWidget(self.run_btn)

        self.add_pipeline_btn = QtWidgets.QPushButton("Add to pipeline")
        self.add_pipeline_btn.setEnabled(False)
        self.add_pipeline_btn.clicked.connect(self._add_current_to_pipeline)
        run_row.addWidget(self.add_pipeline_btn)

        run_row.addStretch(1)

        self.log_box = QtWidgets.QGroupBox("Processing log")
        log_layout = QtWidgets.QVBoxLayout(self.log_box)
        self.log_edit = QtWidgets.QPlainTextEdit(self.log_box)
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet("QPlainTextEdit { background: white; color: black; }")
        log_layout.addWidget(self.log_edit)
        self.log_box.setVisible(False)
        main.addWidget(self.log_box, stretch=1)

        main.addStretch(1)

        self._build_pipeline_view(cont, lay)
        self._clear_details()

    @staticmethod
    def _align_left():
        try:
            return QtCore.Qt.AlignmentFlag.AlignLeft
        except AttributeError:
            return QtCore.Qt.AlignLeft

    # ---------------- pipelines ----------------

    @staticmethod
    def _pipeline_value_text(value):
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _make_pipeline_table(self, headers):
        table = QtWidgets.QTableWidget(self.pipeline_widget)
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels([str(header) for header in headers])
        no_edit = (
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            if hasattr(QtWidgets.QAbstractItemView, "EditTrigger")
            else QtWidgets.QAbstractItemView.NoEditTriggers
        )
        select_rows = (
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior")
            else QtWidgets.QAbstractItemView.SelectRows
        )
        table.setEditTriggers(no_edit)
        table.setSelectionBehavior(select_rows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _fill_pipeline_table(self, table, rows):
        rows = list(rows or [])
        table.setRowCount(len(rows))
        for row_index, row_values in enumerate(rows):
            for column_index in range(table.columnCount()):
                value = row_values[column_index] if column_index < len(row_values) else ""
                item = QtWidgets.QTableWidgetItem(self._pipeline_value_text(value))
                item.setToolTip(item.text())
                table.setItem(row_index, column_index, item)
        table.resizeRowsToContents()
        if table.columnCount() > 1:
            table.resizeColumnToContents(0)

    @staticmethod
    def _processing_step_summary(snapshot):
        if bool((snapshot or {}).get("project_action", False)):
            source = "Project action"
        else:
            count = len((snapshot or {}).get("selected_files", []) or [])
            source = f"{count} selected file(s)"
        param_count = len((snapshot or {}).get("parameters", {}) or {})
        replacement = " | replace existing data" if bool(
            (snapshot or {}).get("replace_existing_data", False)
        ) else ""
        return f"{source} | {param_count} parameter(s){replacement}"

    def _build_pipeline_view(self, container, parent_layout):
        self._pipeline_parent_layout = parent_layout
        self._pipeline_inline_editing = False
        self._pipeline_current_error = ""
        self.pipeline_widget = QtWidgets.QWidget(container)
        parent_layout.addWidget(self.pipeline_widget)
        layout = QtWidgets.QVBoxLayout(self.pipeline_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self.pipeline_detail_stack = QtWidgets.QStackedWidget(self.pipeline_widget)
        layout.addWidget(self.pipeline_detail_stack, stretch=1)

        # Pipeline-level overview. Pipeline and step navigation is provided by
        # the middle tree, leaving the right panel free for useful information.
        self.pipeline_overview_page = QtWidgets.QWidget(self.pipeline_detail_stack)
        overview_layout = QtWidgets.QVBoxLayout(self.pipeline_overview_page)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.setSpacing(8)

        overview_header = QtWidgets.QHBoxLayout()
        overview_heading = QtWidgets.QVBoxLayout()
        self.pipeline_overview_title_lbl = QtWidgets.QLabel("Processing pipelines")
        self.pipeline_overview_title_lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
        overview_heading.addWidget(self.pipeline_overview_title_lbl)
        self.pipeline_summary_lbl = QtWidgets.QLabel("")
        self.pipeline_summary_lbl.setWordWrap(True)
        self.pipeline_summary_lbl.setStyleSheet("color: #444;")
        overview_heading.addWidget(self.pipeline_summary_lbl)
        overview_header.addLayout(overview_heading, stretch=1)

        self.new_pipeline_btn = QtWidgets.QPushButton("New pipeline")
        self.new_pipeline_btn.clicked.connect(self._new_pipeline)
        overview_header.addWidget(self.new_pipeline_btn)
        self.rename_pipeline_btn = QtWidgets.QPushButton("Rename")
        self.rename_pipeline_btn.clicked.connect(self._rename_pipeline)
        overview_header.addWidget(self.rename_pipeline_btn)
        self.delete_pipeline_btn = QtWidgets.QPushButton("Delete")
        self.delete_pipeline_btn.clicked.connect(self._delete_pipeline)
        overview_header.addWidget(self.delete_pipeline_btn)
        overview_layout.addLayout(overview_header)

        self.pipeline_report_path_lbl = QtWidgets.QLabel("")
        self.pipeline_report_path_lbl.setWordWrap(True)
        self.pipeline_report_path_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        overview_layout.addWidget(self.pipeline_report_path_lbl)

        steps_box = QtWidgets.QGroupBox("Steps overview", self.pipeline_overview_page)
        steps_layout = QtWidgets.QVBoxLayout(steps_box)
        self.pipeline_steps_overview = QtWidgets.QTreeWidget(steps_box)
        self.pipeline_steps_overview.setColumnCount(7)
        self.pipeline_steps_overview.setHeaderLabels(
            ["Run", "#", "Step", "Script", "Run date", "Status", "Destination"]
        )
        self.pipeline_steps_overview.setAlternatingRowColors(True)
        self.pipeline_steps_overview.header().setStretchLastSection(True)
        self.pipeline_steps_overview.headerItem().setToolTip(
            0, "Select the steps to include the next time this pipeline is launched."
        )
        self.pipeline_steps_overview.itemChanged.connect(
            self._on_pipeline_step_check_changed
        )
        self.pipeline_steps_overview.itemDoubleClicked.connect(
            self._on_pipeline_overview_item_double_clicked
        )
        single_selection = (
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
            if hasattr(QtWidgets.QAbstractItemView, "SelectionMode")
            else QtWidgets.QAbstractItemView.SingleSelection
        )
        select_rows = (
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior")
            else QtWidgets.QAbstractItemView.SelectRows
        )
        self.pipeline_steps_overview.setSelectionMode(single_selection)
        self.pipeline_steps_overview.setSelectionBehavior(select_rows)
        self.pipeline_steps_overview.itemSelectionChanged.connect(
            self._on_pipeline_overview_selection_changed
        )
        steps_layout.addWidget(self.pipeline_steps_overview, stretch=1)

        selection_row = QtWidgets.QHBoxLayout()
        self.pipeline_select_all_btn = QtWidgets.QPushButton("Select all", steps_box)
        self.pipeline_select_all_btn.clicked.connect(self._select_all_pipeline_steps)
        selection_row.addWidget(self.pipeline_select_all_btn)
        self.pipeline_unselect_all_btn = QtWidgets.QPushButton("Unselect all", steps_box)
        self.pipeline_unselect_all_btn.clicked.connect(self._unselect_all_pipeline_steps)
        selection_row.addWidget(self.pipeline_unselect_all_btn)
        self.pipeline_reverse_selection_btn = QtWidgets.QPushButton(
            "Reverse selection", steps_box
        )
        self.pipeline_reverse_selection_btn.clicked.connect(
            self._reverse_pipeline_step_selection
        )
        selection_row.addWidget(self.pipeline_reverse_selection_btn)
        selection_row.addStretch(1)
        self.pipeline_overview_rename_btn = QtWidgets.QPushButton("Rename", steps_box)
        self.pipeline_overview_rename_btn.clicked.connect(
            self._rename_selected_pipeline_overview_step
        )
        selection_row.addWidget(self.pipeline_overview_rename_btn)
        self.pipeline_overview_edit_btn = QtWidgets.QPushButton("Edit", steps_box)
        self.pipeline_overview_edit_btn.clicked.connect(
            self._edit_selected_pipeline_overview_step
        )
        selection_row.addWidget(self.pipeline_overview_edit_btn)
        self.pipeline_overview_move_up_btn = QtWidgets.QPushButton("Move up", steps_box)
        self.pipeline_overview_move_up_btn.clicked.connect(
            lambda: self._move_selected_pipeline_overview_step(-1)
        )
        selection_row.addWidget(self.pipeline_overview_move_up_btn)
        self.pipeline_overview_move_down_btn = QtWidgets.QPushButton("Move down", steps_box)
        self.pipeline_overview_move_down_btn.clicked.connect(
            lambda: self._move_selected_pipeline_overview_step(1)
        )
        selection_row.addWidget(self.pipeline_overview_move_down_btn)
        self.pipeline_overview_delete_btn = QtWidgets.QPushButton("Delete", steps_box)
        self.pipeline_overview_delete_btn.clicked.connect(
            self._delete_selected_pipeline_overview_step
        )
        selection_row.addWidget(self.pipeline_overview_delete_btn)
        for button in (
            self.pipeline_overview_rename_btn,
            self.pipeline_overview_edit_btn,
            self.pipeline_overview_move_up_btn,
            self.pipeline_overview_move_down_btn,
            self.pipeline_overview_delete_btn,
        ):
            button.setVisible(False)
        steps_layout.addLayout(selection_row)
        overview_layout.addWidget(steps_box, stretch=1)

        launch_row = QtWidgets.QHBoxLayout()
        self.launch_pipeline_btn = QtWidgets.QPushButton("Launch pipeline")
        self.launch_pipeline_btn.clicked.connect(self._launch_pipeline)
        launch_row.addWidget(self.launch_pipeline_btn)
        launch_row.addStretch(1)
        overview_layout.addLayout(launch_row)

        pipeline_log_box = QtWidgets.QGroupBox("Processing log", self.pipeline_overview_page)
        pipeline_log_layout = QtWidgets.QVBoxLayout(pipeline_log_box)
        self.pipeline_log_edit = QtWidgets.QPlainTextEdit(pipeline_log_box)
        self.pipeline_log_edit.setReadOnly(True)
        self.pipeline_log_edit.setMinimumHeight(180)
        self.pipeline_log_edit.setStyleSheet(
            "QPlainTextEdit { background: white; color: black; }"
        )
        pipeline_log_layout.addWidget(self.pipeline_log_edit)
        overview_layout.addWidget(pipeline_log_box, stretch=1)
        self.pipeline_detail_stack.addWidget(self.pipeline_overview_page)

        # Step-level view using the same editor as a manual Processing run.
        self.pipeline_edit_page = QtWidgets.QWidget(self.pipeline_detail_stack)
        detail_layout = QtWidgets.QVBoxLayout(self.pipeline_edit_page)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_header = QtWidgets.QHBoxLayout()
        detail_heading = QtWidgets.QVBoxLayout()
        self.pipeline_detail_title_lbl = QtWidgets.QLabel("Select a step")
        self.pipeline_detail_title_lbl.setStyleSheet("font-size: 14px; font-weight: bold;")
        detail_heading.addWidget(self.pipeline_detail_title_lbl)
        self.pipeline_detail_subtitle_lbl = QtWidgets.QLabel("")
        self.pipeline_detail_subtitle_lbl.setStyleSheet("color: #555;")
        detail_heading.addWidget(self.pipeline_detail_subtitle_lbl)
        self.pipeline_script_lbl = QtWidgets.QLabel("")
        self.pipeline_script_lbl.setWordWrap(True)
        self.pipeline_script_lbl.setStyleSheet("color: #555;")
        self.pipeline_script_lbl.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        detail_heading.addWidget(self.pipeline_script_lbl)
        detail_header.addLayout(detail_heading, stretch=1)
        self.pipeline_mode_lbl = QtWidgets.QLabel("View mode")
        self.pipeline_mode_lbl.setStyleSheet("color: #555; padding: 3px 6px; border: 1px solid #bbb; border-radius: 4px;")
        detail_header.addWidget(self.pipeline_mode_lbl)
        detail_layout.addLayout(detail_header)

        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(QtWidgets.QLabel("Step name"))
        self.pipeline_step_name_edit = QtWidgets.QLineEdit(self.pipeline_edit_page)
        name_row.addWidget(self.pipeline_step_name_edit, stretch=1)
        detail_layout.addLayout(name_row)
        self.pipeline_edit_host = QtWidgets.QWidget(self.pipeline_edit_page)
        self.pipeline_edit_host_layout = QtWidgets.QVBoxLayout(self.pipeline_edit_host)
        self.pipeline_edit_host_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.pipeline_edit_host, stretch=1)
        self.pipeline_detail_stack.addWidget(self.pipeline_edit_page)

        # The saved step is displayed with the real Processing editor.  These
        # actions therefore live outside the stacked pages and remain visible
        # in both view and edit modes.
        editor_actions = QtWidgets.QHBoxLayout()
        self.pipeline_run_result_btn = QtWidgets.QPushButton("View run result")
        self.pipeline_run_result_btn.clicked.connect(self._show_pipeline_run_result)
        editor_actions.addWidget(self.pipeline_run_result_btn)
        self.move_step_up_btn = QtWidgets.QPushButton("Move up")
        self.move_step_up_btn.clicked.connect(lambda: self._move_pipeline_step(-1))
        editor_actions.addWidget(self.move_step_up_btn)
        self.move_step_down_btn = QtWidgets.QPushButton("Move down")
        self.move_step_down_btn.clicked.connect(lambda: self._move_pipeline_step(1))
        editor_actions.addWidget(self.move_step_down_btn)
        editor_actions.addStretch(1)
        self.duplicate_step_btn = QtWidgets.QPushButton("Duplicate")
        self.duplicate_step_btn.clicked.connect(self._duplicate_pipeline_step)
        editor_actions.addWidget(self.duplicate_step_btn)
        self.remove_step_btn = QtWidgets.QPushButton("Remove")
        self.remove_step_btn.clicked.connect(self._remove_pipeline_step)
        editor_actions.addWidget(self.remove_step_btn)
        self.modify_step_btn = QtWidgets.QPushButton("Edit step")
        self.modify_step_btn.clicked.connect(self._modify_pipeline_step)
        editor_actions.addWidget(self.modify_step_btn)
        self.save_step_changes_btn = QtWidgets.QPushButton("Save changes")
        self.save_step_changes_btn.clicked.connect(self._add_current_to_pipeline)
        self.save_step_changes_btn.setVisible(False)
        editor_actions.addWidget(self.save_step_changes_btn)
        self.cancel_step_changes_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_step_changes_btn.clicked.connect(self._cancel_pipeline_step_edit)
        self.cancel_step_changes_btn.setVisible(False)
        editor_actions.addWidget(self.cancel_step_changes_btn)
        detail_layout.addLayout(editor_actions)

        self.pipeline_widget.hide()

    @staticmethod
    def _processing_pipeline_destination_text(snapshot: dict) -> str:
        snapshot = snapshot or {}
        if bool(snapshot.get("project_action", False)):
            return "Project-level action"
        files = list(snapshot.get("selected_files", []) or [])
        if not files:
            return "No selected file"
        if len(files) == 1:
            return str(files[0])
        return f"Updates {len(files)} selected NWB files in place"

    def _processing_pipeline_destination_folder(self, snapshot: dict) -> Optional[Path]:
        """Return the folder affected by a saved Processing pipeline step."""
        snapshot = snapshot or {}
        project_root = Path(getattr(self.GUI_instance, "project_root", "."))
        if bool(snapshot.get("project_action", False)):
            return project_root

        files = [str(value).strip() for value in snapshot.get("selected_files", []) or []]
        files = [value for value in files if value]
        if not files:
            return None

        paths = []
        for value in files:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = project_root / path
            paths.append(path)

        parents = [path if path.is_dir() else path.parent for path in paths]
        try:
            return Path(os.path.commonpath([str(parent) for parent in parents]))
        except (TypeError, ValueError):
            return parents[0]

    def _open_pipeline_folder(self, folder: Optional[Path], label: str):
        if folder is None:
            QtWidgets.QMessageBox.information(
                self,
                "Open folder",
                f"No {label.lower()} is defined for this step.",
            )
            return
        folder = Path(folder).expanduser()
        if not folder.exists() or not folder.is_dir():
            QtWidgets.QMessageBox.information(
                self,
                "Open folder",
                f"The {label.lower()} does not exist yet:\n{folder}",
            )
            return
        opened = QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(folder.resolve()))
        )
        if not opened:
            QtWidgets.QMessageBox.warning(
                self,
                "Open folder",
                f"Could not open the {label.lower()}:\n{folder}",
            )

    def _on_pipeline_overview_item_double_clicked(self, item, column: int):
        """Open the Script or Destination folder from the overview table."""
        if column not in (3, 6):
            return
        pipeline = self._active_pipeline()
        if pipeline is None:
            return
        step_id = str(item.data(0, qt_user_role()) or "")
        step = next(
            (
                candidate
                for candidate in pipeline.get("steps", []) or []
                if str(candidate.get("id", "")) == step_id
            ),
            None,
        )
        if step is None:
            return

        snapshot = step.get("snapshot") or {}
        if column == 3:
            script = self._processing_step_script_metadata(
                snapshot,
                step.get("last_run") or {},
            )
            script_path = str(script.get("script_path", "") or "").strip()
            if not script_path or script_path.lower() == "unknown":
                self._open_pipeline_folder(None, "Script folder")
                return
            self._open_pipeline_folder(Path(script_path).expanduser().parent, "Script folder")
            return

        self._open_pipeline_folder(
            self._processing_pipeline_destination_folder(snapshot),
            "Destination folder",
        )

    def _on_workspace_item_expanded(self, item):
        pipeline_id = self._pipeline_item_to_id.get(id(item))
        if pipeline_id is not None:
            self._expanded_pipeline_ids.add(str(pipeline_id))

    def _on_workspace_item_collapsed(self, item):
        pipeline_id = self._pipeline_item_to_id.get(id(item))
        if pipeline_id is not None:
            self._expanded_pipeline_ids.discard(str(pipeline_id))

    def _append_pipeline_tree_items(self):
        self._pipeline_item_to_id = {}
        self._pipeline_step_item_to_ref = {}
        self._pipeline_id_to_item = {}
        self._pipeline_step_ref_to_item = {}
        self._pipeline_root_item = QtWidgets.QTreeWidgetItem(["Pipelines"])
        self.proc_tree.addTopLevelItem(self._pipeline_root_item)
        self._pipeline_root_item.setExpanded(True)

        for pipeline in self.pipeline_store.pipelines():
            pipeline_id = str(pipeline.get("id", ""))
            steps = list(pipeline.get("steps", []) or [])
            pipeline_item = QtWidgets.QTreeWidgetItem([str(pipeline.get("name", "Pipeline"))])
            set_item_foreground(pipeline_item, "#000000")
            pipeline_item.setToolTip(0, f"{len(steps)} step(s)")
            self._pipeline_root_item.addChild(pipeline_item)
            self._pipeline_item_to_id[id(pipeline_item)] = pipeline_id
            self._pipeline_id_to_item[pipeline_id] = pipeline_item

            status = str((pipeline.get("last_run") or {}).get("status", ""))
            if status == "running":
                set_item_foreground(pipeline_item, "#2e7d32")
            elif status in ("completed_with_failures", "interrupted"):
                set_item_foreground(pipeline_item, "#b00020")
            elif status == "completed":
                set_item_foreground(pipeline_item, "#000000")

            for index, step in enumerate(steps, start=1):
                step_id = str(step.get("id", ""))
                step_status = self._pipeline_step_run_status(pipeline, step)
                step_item = QtWidgets.QTreeWidgetItem([
                    f"{index}. {step.get('name', 'Step')} [{self._step_status_text(step_status)}]"
                ])
                set_item_foreground(step_item, "#000000")
                step_item.setToolTip(0, self._processing_step_summary(step.get("snapshot") or {}))
                pipeline_item.addChild(step_item)
                ref = (pipeline_id, step_id)
                self._pipeline_step_item_to_ref[id(step_item)] = ref
                self._pipeline_step_ref_to_item[ref] = step_item
                if step_status == "running":
                    set_item_foreground(step_item, "#2e7d32")
                elif step_status == "failed":
                    set_item_foreground(step_item, "#b00020")
                    step_item.setBackground(0, QtGui.QBrush(QtGui.QColor("#fff1f1")))
                elif step_status == "completed":
                    set_item_foreground(step_item, "#000000")
            pipeline_item.setExpanded(pipeline_id in self._expanded_pipeline_ids)

    def _pipeline_tree_selection(self):
        items = self.proc_tree.selectedItems()
        if not items:
            return None, None, None
        item = items[0]
        if item is self._pipeline_root_item:
            return "root", None, None
        step_ref = self._pipeline_step_item_to_ref.get(id(item))
        if step_ref is not None:
            return "step", step_ref[0], step_ref[1]
        pipeline_id = self._pipeline_item_to_id.get(id(item))
        if pipeline_id is not None:
            return "pipeline", pipeline_id, None
        return None, None, None

    def _capture_pipeline_browsing_state(self):
        """Capture the view that must survive an internal pipeline-step launch."""
        pipeline_kind, pipeline_id, step_id = self._pipeline_tree_selection()
        state = {
            "view": pipeline_kind or "blank",
            "pipeline_id": pipeline_id,
            "step_id": step_id,
        }

        current_item = self.proc_tree.currentItem()
        ref = self._item_to_ref.get(id(current_item)) if current_item is not None else None
        if ref is None:
            return state

        category, name = ref
        spec = (self.registry.get(category, {}) or {}).get(name)
        if spec is None:
            return state

        param_values = {}
        for param_name in self.param_specs or {}:
            try:
                param_values[param_name] = copy.deepcopy(
                    self._resolve_param_value(param_name)
                )
            except Exception:
                pass

        state["view"] = "processing"
        state["processing"] = {
            "category": str(category),
            "name": str(name),
            "target_paths": copy.deepcopy(self.target_paths),
            "param_values": param_values,
            "log": str(self.log_edit.toPlainText()),
            "log_title": str(self.log_box.title()),
            "log_visible": not self.log_box.isHidden(),
        }
        return state

    def _select_processing_tree_ref(self, category: str, name: str):
        item = None
        root = self.proc_tree.invisibleRootItem()
        for i in range(root.childCount()):
            category_item = root.child(i)
            for j in range(category_item.childCount()):
                child = category_item.child(j)
                if self._item_to_ref.get(id(child)) == (category, name):
                    item = child
                    break
            if item is not None:
                break
        if item is None:
            return
        blocked = self.proc_tree.blockSignals(True)
        try:
            self.proc_tree.setCurrentItem(item)
            item.setSelected(True)
        finally:
            self.proc_tree.blockSignals(blocked)

    def _restore_pipeline_browsing_state(self, state):
        """Restore a user-selected view without changing the application's active tab."""
        state = dict(state or {})
        view = str(state.get("view", "blank"))

        if view == "processing":
            saved = dict(state.get("processing") or {})
            category = str(saved.get("category", ""))
            name = str(saved.get("name", ""))
            spec = (self.registry.get(category, {}) or {}).get(name)
            if spec is None:
                _category, spec = self._processing_registry_entry(name)
            if spec is None:
                return

            self._detach_pipeline_editor()
            self._active_pipeline_step_id = None
            self.target_paths = list(saved.get("target_paths", []) or [])
            self._selected_name = name
            self._selected_spec = spec
            self._show_details(name, spec)
            self._restore_processing_parameters(saved.get("param_values", {}) or {})
            self.log_edit.setPlainText(str(saved.get("log", "")))
            self.log_box.setTitle(str(saved.get("log_title", "Processing log")))
            self.log_box.setVisible(bool(saved.get("log_visible", False)))
            self.pipeline_widget.hide()
            self.details_widget.show()
            self._select_processing_tree_ref(category, name)
            self._refresh_availability_display()
            self._update_run_enabled()
            return

        if view in ("pipeline", "root"):
            pipeline_id = state.get("pipeline_id")
            if pipeline_id is None and self._pipeline_run is not None:
                pipeline_id = self._pipeline_run.get("pipeline_id")
            self._active_pipeline_id = str(pipeline_id or "") or None
            self._active_pipeline_step_id = None
            self.details_widget.hide()
            self.pipeline_widget.show()
            self.pipeline_detail_stack.setCurrentWidget(self.pipeline_overview_page)
            self._select_pipeline_tree_item(self._active_pipeline_id)
            self._refresh_pipeline_view()
            return

        if view == "step":
            pipeline_id = str(state.get("pipeline_id", "") or "")
            step_id = str(state.get("step_id", "") or "")
            pipeline = self.pipeline_store.get(pipeline_id)
            step_exists = any(
                str(step.get("id", "")) == step_id
                for step in (pipeline or {}).get("steps", []) or []
            )
            if step_exists:
                self._active_pipeline_id = pipeline_id
                self._active_pipeline_step_id = step_id
                self.pipeline_widget.show()
                self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
                self._select_pipeline_tree_item(pipeline_id, step_id)
                self._on_pipeline_step_selected(None, None)
                return

        self._detach_pipeline_editor()
        self._active_pipeline_step_id = None
        self.pipeline_widget.hide()
        self.details_widget.show()
        self._clear_details()

    def _select_pipeline_tree_item(self, pipeline_id: Optional[str], step_id: Optional[str] = None):
        item = None
        if pipeline_id and step_id:
            item = self._pipeline_step_ref_to_item.get((str(pipeline_id), str(step_id)))
        elif pipeline_id:
            item = self._pipeline_id_to_item.get(str(pipeline_id))
        elif self._pipeline_root_item is not None:
            item = self._pipeline_root_item
        if item is None:
            return
        blocked = self.proc_tree.blockSignals(True)
        try:
            self.proc_tree.setCurrentItem(item)
            item.setSelected(True)
            self.proc_tree.scrollToItem(item)
        finally:
            self.proc_tree.blockSignals(blocked)

    def _refresh_pipeline_tree_items(
        self,
        selected_pipeline_id: Optional[str] = None,
        selected_step_id: Optional[str] = None,
    ):
        """Rebuild the middle-tree pipeline branch and preserve its selection."""
        current_item = self.proc_tree.currentItem()
        current_kind, current_pipeline_id, current_step_id = self._pipeline_tree_selection()
        keep_editor_item = current_item if current_kind is None and current_item is not None else None
        if selected_pipeline_id is None:
            selected_pipeline_id = current_pipeline_id
        if selected_step_id is None and current_kind == "step":
            selected_step_id = current_step_id
        blocked = self.proc_tree.blockSignals(True)
        try:
            if self._pipeline_root_item is not None:
                index = self.proc_tree.indexOfTopLevelItem(self._pipeline_root_item)
                if index >= 0:
                    self.proc_tree.takeTopLevelItem(index)
            self._append_pipeline_tree_items()
            if keep_editor_item is not None:
                self.proc_tree.setCurrentItem(keep_editor_item)
                keep_editor_item.setSelected(True)
            elif selected_pipeline_id and selected_step_id:
                item = self._pipeline_step_ref_to_item.get(
                    (str(selected_pipeline_id), str(selected_step_id))
                )
                if item is not None:
                    self.proc_tree.setCurrentItem(item)
                    item.setSelected(True)
            elif selected_pipeline_id:
                item = self._pipeline_id_to_item.get(str(selected_pipeline_id))
                if item is not None:
                    self.proc_tree.setCurrentItem(item)
                    item.setSelected(True)
        finally:
            self.proc_tree.blockSignals(blocked)
        self._apply_running_highlight()

    def _show_pipeline_view(self, pipeline_id: Optional[str] = None):
        pipelines = self.pipeline_store.pipelines()
        valid_ids = {str(p.get("id", "")) for p in pipelines}
        if pipeline_id not in valid_ids:
            pipeline_id = self._active_pipeline_id if self._active_pipeline_id in valid_ids else None
        if pipeline_id is None and pipelines:
            pipeline_id = str(pipelines[0].get("id", ""))

        self._active_pipeline_id = pipeline_id
        self._active_pipeline_step_id = None
        self.details_widget.hide()
        self.pipeline_widget.show()
        self.pipeline_detail_stack.setCurrentWidget(self.pipeline_overview_page)
        self.show_details_view()
        self._select_pipeline_tree_item(pipeline_id)
        self._refresh_pipeline_view()

    def _show_pipeline_step(self, pipeline_id: str, step_id: str):
        pipeline = self.pipeline_store.get(pipeline_id)
        step = next(
            (
                item for item in (pipeline or {}).get("steps", []) or []
                if str(item.get("id", "")) == str(step_id)
            ),
            None,
        )
        if pipeline is None or step is None:
            self._show_pipeline_view(pipeline_id)
            return
        self._active_pipeline_id = str(pipeline_id)
        self._active_pipeline_step_id = str(step_id)
        self.pipeline_widget.show()
        self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
        self.show_details_view()
        self._select_pipeline_tree_item(pipeline_id, step_id)
        self._on_pipeline_step_selected(None, None)

    def _active_pipeline(self):
        return self.pipeline_store.get(self._active_pipeline_id)

    def _selected_pipeline_step(self):
        pipeline = self._active_pipeline()
        step_id = str(self._active_pipeline_step_id or "")
        if pipeline is None or not step_id:
            return pipeline, None, -1
        for index, step in enumerate(pipeline.get("steps", []) or []):
            if str(step.get("id", "")) == step_id:
                return pipeline, step, index
        return pipeline, None, -1

    def _running_pipeline_step(self):
        """Return the step currently executed by the pipeline runner."""
        state = self._pipeline_run
        if state is None:
            return None
        pipeline = self.pipeline_store.get(state.get("pipeline_id"))
        if pipeline is None:
            return None
        active_step_id = str(state.get("active_step_id") or "")
        if not active_step_id:
            return None
        for step in pipeline.get("steps", []) or []:
            if str(step.get("id", "")) == active_step_id:
                return step
        return None

    def _selected_step_is_running_step(self) -> bool:
        state = self._pipeline_run
        if state is None or str(state.get("pipeline_id", "")) != str(self._active_pipeline_id or ""):
            return False
        _pipeline, selected_step, _index = self._selected_pipeline_step()
        running_step = self._running_pipeline_step()
        if selected_step is None or running_step is None:
            return False
        return str(selected_step.get("id", "")) == str(running_step.get("id", ""))

    def _set_running_step_log(self, text: str):
        """Keep the live log with its pipeline step, independently of the editor widget."""
        state = self._pipeline_run
        if state is None:
            return
        log_text = str(text or "")
        state["step_log"] = log_text
        step = self._running_pipeline_step()
        if step is not None:
            last_run = dict(step.get("last_run") or {})
            last_run["log"] = log_text
            step["last_run"] = last_run

    def _pipeline_log_text(self, pipeline=None) -> str:
        """Return the retained log history, including the active run if any."""
        pipeline = pipeline if pipeline is not None else self._active_pipeline()
        if pipeline is None:
            return ""
        state = self._pipeline_run
        if (
            state is not None
            and str(state.get("pipeline_id", "")) == str(pipeline.get("id", ""))
        ):
            return str(state.get("pipeline_log", "") or "")
        return str((pipeline.get("last_run") or {}).get("log", "") or "")

    def _display_pipeline_log(self, text: str):
        text = str(text or "")
        if self.pipeline_log_edit.toPlainText() != text:
            self.pipeline_log_edit.setPlainText(text)
        scrollbar = self.pipeline_log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_pipeline_run_log(self, text: str):
        """Update the persistent combined log for the pipeline currently running."""
        state = self._pipeline_run
        if state is None:
            return
        text = str(text or "")
        state["pipeline_log"] = text
        pipeline = self.pipeline_store.get(state.get("pipeline_id"))
        if pipeline is not None:
            last_run = dict(pipeline.get("last_run") or {})
            last_run["log"] = text
            pipeline["last_run"] = last_run
        if str(state.get("pipeline_id", "")) == str(self._active_pipeline_id or ""):
            self._display_pipeline_log(text)

    def _append_pipeline_run_log(self, text: str):
        """Append output without clearing anything from earlier steps or runs."""
        state = self._pipeline_run
        if state is None:
            return
        message = str(text or "")
        if not message:
            return
        existing = str(state.get("pipeline_log", "") or "")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        self._set_pipeline_run_log(existing + separator + message)

    @staticmethod
    def _step_status_text(status: str) -> str:
        return {
            "pending": "Pending",
            "running": "Running",
            "completed": "Completed",
            "failed": "Failed",
            "skipped": "Skipped",
            "not_run": "Not run",
        }.get(str(status or ""), "Not run")

    @staticmethod
    def _pipeline_step_run_status(pipeline: Optional[dict], step: dict) -> str:
        """Return the latest status without changing unchecked steps.

        Older pipeline runs stored ``"skipped"`` in ``step_statuses`` for an
        unchecked step.  Ignore that transient value and display the status of
        the step's last actual execution instead.
        """
        step_id = str((step or {}).get("id", ""))
        latest_statuses = dict(
            (((pipeline or {}).get("last_run") or {}).get("step_statuses") or {})
        )
        status = str(latest_statuses.get(step_id, "") or "")
        if status and status != "skipped":
            return status
        return str(((step or {}).get("last_run") or {}).get("status", "") or "")

    def _set_pipeline_step_run_status(self, step_id: str, status: str):
        """Persist a current-run status without replacing the step result."""
        state = self._pipeline_run
        if state is None:
            return
        step_id = str(step_id or "")
        if not step_id:
            return
        statuses = dict(state.get("step_statuses") or {})
        statuses[step_id] = str(status or "")
        state["step_statuses"] = statuses
        pipeline = self.pipeline_store.get(state.get("pipeline_id"))
        if pipeline is not None:
            last_run = dict(pipeline.get("last_run") or {})
            last_run["step_statuses"] = dict(statuses)
            pipeline["last_run"] = last_run

    @staticmethod
    def _pipeline_step_enabled(step: dict) -> bool:
        """Older saved pipelines did not have this field, so they remain fully selected."""
        return bool((step or {}).get("enabled", True))

    def _pipeline_overview_summary_text(self, pipeline: dict) -> str:
        steps = list((pipeline or {}).get("steps", []) or [])
        selected_step_count = sum(
            1 for step in steps if self._pipeline_step_enabled(step)
        )
        last_run = (pipeline or {}).get("last_run") or {}
        status = str(last_run.get("status", "not_run") or "not_run").replace("_", " ")
        text = (
            f"{len(steps)} step(s) | {selected_step_count} selected to run | "
            f"Last status: {status}"
        )
        last_run_date = str(last_run.get("started_at", "") or "")
        if last_run_date:
            text += f" | Last run: {_run_date_text(last_run_date)}"
        failed_steps = int(last_run.get("failed_steps", 0) or 0)
        if failed_steps:
            text += f" | Failed steps: {failed_steps}"
        return text

    def _update_pipeline_selection_controls(self, pipeline=None):
        """Update selection actions without rebuilding the checkbox tree."""
        pipeline = pipeline if pipeline is not None else self._active_pipeline()
        steps = list((pipeline or {}).get("steps", []) or [])
        selected_step_count = sum(
            1 for step in steps if self._pipeline_step_enabled(step)
        )
        editable = bool(
            pipeline is not None
            and steps
            and self._pipeline_run is None
            and not self._running
            and not self._pipeline_inline_editing
        )
        self.pipeline_select_all_btn.setEnabled(editable)
        self.pipeline_unselect_all_btn.setEnabled(editable)
        self.pipeline_reverse_selection_btn.setEnabled(editable)
        self.launch_pipeline_btn.setEnabled(editable and selected_step_count > 0)
        if pipeline is not None:
            self.pipeline_summary_lbl.setText(
                self._pipeline_overview_summary_text(pipeline)
            )
        self._update_pipeline_overview_step_actions()

    def _selected_pipeline_overview_step(self):
        """Return the single row-selected overview step without changing views."""
        selected_items = list(self.pipeline_steps_overview.selectedItems())
        pipeline = self._active_pipeline()
        if pipeline is None or len(selected_items) != 1:
            return pipeline, None, -1

        step_id = str(selected_items[0].data(0, qt_user_role()) or "")
        if not step_id:
            return pipeline, None, -1
        for index, step in enumerate(pipeline.get("steps", []) or []):
            if str(step.get("id", "")) == step_id:
                return pipeline, step, index
        return pipeline, None, -1

    def _update_pipeline_overview_step_actions(self):
        pipeline, step, index = self._selected_pipeline_overview_step()
        has_step = step is not None
        step_count = len((pipeline or {}).get("steps", []) or [])
        editable = bool(
            has_step
            and self._pipeline_run is None
            and not self._running
            and not self._pipeline_inline_editing
        )

        for button in (
            self.pipeline_overview_rename_btn,
            self.pipeline_overview_edit_btn,
            self.pipeline_overview_move_up_btn,
            self.pipeline_overview_move_down_btn,
            self.pipeline_overview_delete_btn,
        ):
            button.setVisible(has_step)
        self.pipeline_overview_rename_btn.setEnabled(editable)
        self.pipeline_overview_edit_btn.setEnabled(editable)
        self.pipeline_overview_move_up_btn.setEnabled(editable and index > 0)
        self.pipeline_overview_move_down_btn.setEnabled(
            editable and 0 <= index < step_count - 1
        )
        self.pipeline_overview_delete_btn.setEnabled(editable)

    def _on_pipeline_overview_selection_changed(self):
        self._update_pipeline_overview_step_actions()

    def _select_pipeline_overview_row(self, step_id: str):
        """Restore a row selection after moving a step in the overview."""
        step_id = str(step_id or "")
        self.pipeline_steps_overview.clearSelection()
        if not step_id:
            return
        for index in range(self.pipeline_steps_overview.topLevelItemCount()):
            row = self.pipeline_steps_overview.topLevelItem(index)
            if str(row.data(0, qt_user_role()) or "") != step_id:
                continue
            self.pipeline_steps_overview.setCurrentItem(row)
            row.setSelected(True)
            self.pipeline_steps_overview.scrollToItem(row)
            return

    def _rename_selected_pipeline_overview_step(self):
        pipeline, step, _index = self._selected_pipeline_overview_step()
        if (
            pipeline is None
            or step is None
            or self._pipeline_run is not None
            or self._running
        ):
            return
        step_id = str(step.get("id", ""))
        name, accepted = QtWidgets.QInputDialog.getText(
            self,
            "Rename processing step",
            "Step name",
            text=str(step.get("name", "Step")),
        )
        if not accepted:
            return
        try:
            self.pipeline_store.rename_step(
                str(pipeline.get("id", "")),
                step_id,
                str(name),
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
            return
        self._active_pipeline_step_id = None
        self._refresh_pipeline_view()
        self._select_pipeline_overview_row(step_id)

    def _edit_selected_pipeline_overview_step(self):
        pipeline, step, _index = self._selected_pipeline_overview_step()
        if (
            pipeline is None
            or step is None
            or self._pipeline_run is not None
            or self._running
        ):
            return
        pipeline_id = str(pipeline.get("id", ""))
        step_id = str(step.get("id", ""))
        self._show_pipeline_step(pipeline_id, step_id)
        self._modify_pipeline_step()

    def _delete_selected_pipeline_overview_step(self):
        pipeline, step, _index = self._selected_pipeline_overview_step()
        if (
            pipeline is None
            or step is None
            or self._pipeline_run is not None
            or self._running
        ):
            return
        self._active_pipeline_step_id = str(step.get("id", ""))
        self._remove_pipeline_step()

    def _move_selected_pipeline_overview_step(self, offset: int):
        pipeline, step, _index = self._selected_pipeline_overview_step()
        if (
            pipeline is None
            or step is None
            or self._pipeline_run is not None
            or self._running
        ):
            return
        pipeline_id = str(pipeline.get("id", ""))
        step_id = str(step.get("id", ""))
        if self.pipeline_store.move_step(pipeline_id, step_id, offset):
            self._active_pipeline_step_id = None
            self._refresh_pipeline_view()
            self._select_pipeline_overview_row(step_id)

    def _set_pipeline_step_selection(self, mode: str):
        if self._pipeline_run is not None or self._running or self._pipeline_inline_editing:
            return
        pipeline = self._active_pipeline()
        if pipeline is None:
            return
        steps = list(pipeline.get("steps", []) or [])
        if not steps:
            return

        enabled_by_id = {}
        changed = False
        for step in steps:
            current = self._pipeline_step_enabled(step)
            if mode == "all":
                enabled = True
            elif mode == "none":
                enabled = False
            elif mode == "reverse":
                enabled = not current
            else:
                return
            step_id = str(step.get("id", ""))
            enabled_by_id[step_id] = enabled
            if current != enabled or "enabled" not in step:
                step["enabled"] = enabled
                changed = True

        was_blocked = self.pipeline_steps_overview.blockSignals(True)
        try:
            for index in range(self.pipeline_steps_overview.topLevelItemCount()):
                row = self.pipeline_steps_overview.topLevelItem(index)
                step_id = str(row.data(0, qt_user_role()) or "")
                if step_id not in enabled_by_id:
                    continue
                row.setCheckState(
                    0,
                    qt_checked() if enabled_by_id[step_id] else qt_unchecked(),
                )
        finally:
            self.pipeline_steps_overview.blockSignals(was_blocked)

        if changed:
            pipeline["updated_at"] = utc_now_text()
            self.pipeline_store.save()
        self._update_pipeline_selection_controls(pipeline)

    def _select_all_pipeline_steps(self):
        self._set_pipeline_step_selection("all")

    def _unselect_all_pipeline_steps(self):
        self._set_pipeline_step_selection("none")

    def _reverse_pipeline_step_selection(self):
        self._set_pipeline_step_selection("reverse")

    def _on_pipeline_step_check_changed(self, item, column: int):
        if column != 0 or self._pipeline_run is not None or self._running:
            return
        pipeline = self._active_pipeline()
        if pipeline is None:
            return
        step_id = str(item.data(0, qt_user_role()) or "")
        if not step_id:
            return
        enabled = item.checkState(0) == qt_checked()
        for step in pipeline.get("steps", []) or []:
            if str(step.get("id", "")) != step_id:
                continue
            if self._pipeline_step_enabled(step) == enabled and "enabled" in step:
                return
            step["enabled"] = enabled
            pipeline["updated_at"] = utc_now_text()
            self.pipeline_store.save()
            # Do not rebuild the tree from inside itemChanged. Clearing the
            # QTreeWidget here destroys ``item`` while Qt is still dispatching
            # its signal and can terminate the Python process in PySide.
            self._update_pipeline_selection_controls(pipeline)
            return

    def _refresh_pipeline_view(self, selected_step_id: Optional[str] = None):
        pipeline = self._active_pipeline()
        if selected_step_id is not None:
            self._active_pipeline_step_id = str(selected_step_id)
        has_pipeline = pipeline is not None
        running = self._pipeline_run is not None or self._running
        editing = bool(self._pipeline_inline_editing)
        self.new_pipeline_btn.setEnabled(not running and not editing)
        self.rename_pipeline_btn.setEnabled(has_pipeline and not running and not editing)
        self.delete_pipeline_btn.setEnabled(has_pipeline and not running and not editing)
        self.pipeline_steps_overview.blockSignals(True)
        self.pipeline_steps_overview.clear()
        if pipeline is None:
            self.pipeline_overview_title_lbl.setText("Processing pipelines")
            self.pipeline_summary_lbl.setText("No processing pipeline has been created yet.")
            report_directory = self.pipeline_store.directory / "reports" / "processing"
            self.pipeline_report_path_lbl.setText(f"Reports will be saved in: {report_directory}")
            self._display_pipeline_log("")
            step_count = 0
            selected_step_count = 0
        else:
            steps = list(pipeline.get("steps", []) or [])
            last_run = pipeline.get("last_run") or {}
            report_path = str(last_run.get("report_path", "") or "")
            self.pipeline_overview_title_lbl.setText(str(pipeline.get("name", "Pipeline")))
            self.pipeline_summary_lbl.setText(
                self._pipeline_overview_summary_text(pipeline)
            )
            if report_path:
                try:
                    report_path = str(Path(getattr(self.GUI_instance, "project_root", ".")) / report_path)
                except Exception:
                    pass
                self.pipeline_report_path_lbl.setText(f"Last report: {report_path}")
            else:
                report_directory = self.pipeline_store.directory / "reports" / "processing"
                self.pipeline_report_path_lbl.setText(f"Report destination: {report_directory}")

            self._display_pipeline_log(self._pipeline_log_text(pipeline))

            for index, step in enumerate(steps, start=1):
                step_run = step.get("last_run") or {}
                step_status = self._pipeline_step_run_status(pipeline, step)
                script = self._processing_step_script_metadata(
                    step.get("snapshot") or {},
                    step_run,
                )
                script_name = str(script.get("script_name", "Unknown") or "Unknown")
                script_path = str(script.get("script_path", "Unknown") or "Unknown")
                run_date = _run_date_text(step_run.get("started_at", ""))
                row = QtWidgets.QTreeWidgetItem([
                    "",
                    str(index),
                    str(step.get("name", "Step")),
                    script_name,
                    run_date,
                    self._step_status_text(step_status),
                    self._processing_pipeline_destination_text(step.get("snapshot") or {}),
                ])
                row.setData(0, qt_user_role(), str(step.get("id", "")))
                row.setCheckState(
                    0,
                    qt_checked() if self._pipeline_step_enabled(step) else qt_unchecked(),
                )
                row.setToolTip(2, self._processing_step_summary(step.get("snapshot") or {}))
                row.setToolTip(3, f"{script_path}\nDouble-click to open the script folder.")
                destination_folder = self._processing_pipeline_destination_folder(
                    step.get("snapshot") or {}
                )
                if destination_folder is not None:
                    row.setToolTip(
                        6,
                        f"{destination_folder}\nDouble-click to open the destination folder.",
                    )
                if step_status == "running":
                    row.setForeground(5, QtGui.QBrush(QtGui.QColor("#2e7d32")))
                elif step_status == "failed":
                    row.setForeground(5, QtGui.QBrush(QtGui.QColor("#b00020")))
                    for column in range(7):
                        row.setBackground(column, QtGui.QBrush(QtGui.QColor("#fff1f1")))
                elif step_status == "completed":
                    row.setForeground(5, QtGui.QBrush(QtGui.QColor("#000000")))
                elif step_status == "skipped":
                    row.setForeground(5, QtGui.QBrush(QtGui.QColor("#777777")))
                self.pipeline_steps_overview.addTopLevelItem(row)
            for column in (0, 1, 2, 3, 4, 5):
                self.pipeline_steps_overview.resizeColumnToContents(column)
            step_count = len(steps)

        self.pipeline_steps_overview.blockSignals(False)
        self.pipeline_steps_overview.setEnabled(has_pipeline and not running and not editing)
        self._update_pipeline_selection_controls(pipeline)
        self._refresh_pipeline_tree_items(
            selected_pipeline_id=self._active_pipeline_id,
            selected_step_id=self._active_pipeline_step_id,
        )

        # Status updates also run while the user is browsing elsewhere. Only
        # alter the right-side pipeline page when that page is actually open.
        if not self.pipeline_widget.isHidden():
            _pipeline, selected_step, _index = self._selected_pipeline_step()
            if selected_step is not None:
                self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
                self._on_pipeline_step_selected(None, None)
            else:
                self._active_pipeline_step_id = None
                self.details_widget.hide()
                self.pipeline_detail_stack.setCurrentWidget(self.pipeline_overview_page)

    def _on_pipeline_step_selected(self, current, _previous):
        pipeline, step, index = self._selected_pipeline_step()
        running = self._pipeline_run is not None or self._running
        has_step = step is not None
        step_count = len((pipeline or {}).get("steps", []) or [])
        editing = bool(self._pipeline_inline_editing)
        self.modify_step_btn.setEnabled(has_step and not running and not editing)
        self.duplicate_step_btn.setEnabled(has_step and not running and not editing)
        self.remove_step_btn.setEnabled(has_step and not running and not editing)
        self.move_step_up_btn.setEnabled(has_step and index > 0 and not running and not editing)
        self.move_step_down_btn.setEnabled(has_step and 0 <= index < step_count - 1 and not running and not editing)
        self.pipeline_run_result_btn.setEnabled(has_step)
        if step is None:
            self._clear_pipeline_step_details()
            return

        last_run = step.get("last_run") or {}
        self.pipeline_detail_title_lbl.setText(f"{index + 1}. {step.get('name', 'Step')}")
        latest_status = self._pipeline_step_run_status(pipeline, step)
        status_text = self._step_status_text(latest_status)
        timestamp = last_run.get("finished_at") or last_run.get("started_at") or ""
        self.pipeline_detail_subtitle_lbl.setText(
            status_text + (f" | {_run_date_text(timestamp)}" if timestamp else "")
        )
        script = self._processing_step_script_metadata(
            step.get("snapshot") or {},
            last_run,
        )
        self.pipeline_script_lbl.setText(
            f"Script: {script.get('script_name', 'Unknown')}\n"
            f"Location: {script.get('script_path', 'Unknown')}"
        )
        self._pipeline_current_error = str(last_run.get("error", "") or "")
        try:
            self._attach_pipeline_editor()
            self.pipeline_step_name_edit.setText(str(step.get("name", "Step")))
            self._restore_processing_snapshot(
                copy.deepcopy(step.get("snapshot") or {}),
                show_editor=False,
            )
            self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
            self.details_widget.show()
            self._set_pipeline_editor_editable(False)
            self.log_box.setTitle(f"Processing log: {step.get('name', 'Step')}")
            self.log_box.setVisible(True)
            self.log_edit.setPlainText(str(last_run.get("log", "") or ""))
        except Exception as exc:
            self.details_widget.hide()
            self.pipeline_mode_lbl.setText("Unavailable")
            self.pipeline_detail_subtitle_lbl.setText(f"{status_text} | {exc}")

    def _clear_pipeline_step_details(self):
        self.pipeline_detail_title_lbl.setText("Select a step")
        self.pipeline_detail_subtitle_lbl.clear()
        self.pipeline_script_lbl.clear()
        self._pipeline_current_error = ""
        self.pipeline_run_result_btn.setEnabled(False)
        self.pipeline_step_name_edit.clear()
        if self.details_widget.parent() is self.pipeline_edit_host:
            self.details_widget.hide()

    def _attach_pipeline_editor(self):
        """Place the normal Processing editor inside the selected pipeline step."""
        if self.details_widget.parent() is self.pipeline_edit_host:
            return
        self._pipeline_parent_layout.removeWidget(self.details_widget)
        self.details_widget.setParent(self.pipeline_edit_host)
        self.pipeline_edit_host_layout.addWidget(self.details_widget)

    def _detach_pipeline_editor(self):
        """Return the normal editor to the right panel when leaving Pipelines."""
        if self.details_widget.parent() is self.pipeline_edit_host:
            self.pipeline_edit_host_layout.removeWidget(self.details_widget)
            self.details_widget.setParent(self.right_panel.processing_container)
            self._pipeline_parent_layout.insertWidget(0, self.details_widget)
        self._pipeline_inline_editing = False
        self.params_box.setEnabled(True)
        self.pipeline_step_name_edit.setReadOnly(False)

    def _set_pipeline_editor_editable(self, editable: bool):
        editable = bool(editable)
        self._pipeline_inline_editing = editable
        self.pipeline_mode_lbl.setText("Editing" if editable else "Saved configuration")
        self.pipeline_step_name_edit.setReadOnly(not editable)
        self.params_box.setEnabled(editable)
        self.replace_existing_chk.setEnabled(
            editable and self._tool_supports_replacement(self._selected_spec)
        )
        self._sync_pipeline_edit_buttons()

    def _show_pipeline_run_result(self):
        _pipeline, step, _index = self._selected_pipeline_step()
        if step is None:
            return
        last_run = step.get("last_run") or {}
        status = self._step_status_text(last_run.get("status", ""))
        script = self._processing_step_script_metadata(
            step.get("snapshot") or {},
            last_run,
        )

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"Processing result: {step.get('name', 'Step')}")
        dialog.resize(850, 600)
        layout = QtWidgets.QVBoxLayout(dialog)
        summary = QtWidgets.QLabel(
            f"Status: {status}\n"
            f"Started: {last_run.get('started_at', '') or ''}\n"
            f"Finished: {last_run.get('finished_at', '') or ''}\n"
            f"Script: {script.get('script_name', 'Unknown')}\n"
            f"Location: {script.get('script_path', 'Unknown')}"
        )
        summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)

        tabs = QtWidgets.QTabWidget(dialog)
        log_edit = QtWidgets.QPlainTextEdit(tabs)
        log_edit.setReadOnly(True)
        log_edit.setPlainText(str(last_run.get("log", "") or "No log stored for this step."))
        tabs.addTab(log_edit, "Log")
        error_edit = QtWidgets.QPlainTextEdit(tabs)
        error_edit.setReadOnly(True)
        error_edit.setPlainText(str(last_run.get("error", "") or "No error stored for this step."))
        tabs.addTab(error_edit, "Full error")
        layout.addWidget(tabs, stretch=1)

        close_button = (
            QtWidgets.QDialogButtonBox.StandardButton.Close
            if hasattr(QtWidgets.QDialogButtonBox, "StandardButton")
            else QtWidgets.QDialogButtonBox.Close
        )
        buttons = QtWidgets.QDialogButtonBox(close_button, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_pipeline_error(self):
        if not self._pipeline_current_error:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Full processing error")
        dialog.resize(850, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        error_edit = QtWidgets.QPlainTextEdit(dialog)
        error_edit.setReadOnly(True)
        error_edit.setPlainText(self._pipeline_current_error)
        error_edit.setStyleSheet("QPlainTextEdit { background: white; color: black; }")
        layout.addWidget(error_edit)
        close_button = (
            QtWidgets.QDialogButtonBox.StandardButton.Close
            if hasattr(QtWidgets.QDialogButtonBox, "StandardButton")
            else QtWidgets.QDialogButtonBox.Close
        )
        buttons = QtWidgets.QDialogButtonBox(close_button, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _prompt_new_pipeline(self):
        name, accepted = QtWidgets.QInputDialog.getText(self, "New processing pipeline", "Pipeline name")
        if not accepted:
            return None
        try:
            return self.pipeline_store.create(str(name))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
            return None

    def _new_pipeline(self):
        if self._pipeline_run is not None or self._running:
            return
        pipeline = self._prompt_new_pipeline()
        if pipeline is not None:
            self._active_pipeline_id = str(pipeline.get("id", ""))
            self.refresh_list()
            self._show_pipeline_view(self._active_pipeline_id)

    def _rename_pipeline(self):
        pipeline = self._active_pipeline()
        if pipeline is None or self._pipeline_run is not None or self._running:
            return
        name, accepted = QtWidgets.QInputDialog.getText(
            self,
            "Rename processing pipeline",
            "Pipeline name",
            text=str(pipeline.get("name", "")),
        )
        if not accepted:
            return
        try:
            self.pipeline_store.rename(str(pipeline.get("id", "")), str(name))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
            return
        self.refresh_list()
        self._show_pipeline_view(str(pipeline.get("id", "")))

    def _delete_pipeline(self):
        pipeline = self._active_pipeline()
        if pipeline is None or self._pipeline_run is not None or self._running:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete processing pipeline",
            f'Delete pipeline "{pipeline.get("name", "Pipeline")}"?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.pipeline_store.delete(str(pipeline.get("id", "")))
        self._active_pipeline_id = None
        self.refresh_list()
        self._show_pipeline_view()

    def _choose_pipeline_for_new_step(self):
        pipelines = self.pipeline_store.pipelines()
        if not pipelines:
            return self._prompt_new_pipeline()
        create_label = "<Create a new pipeline>"
        choices = [str(p.get("name", "Pipeline")) for p in pipelines] + [create_label]
        choice, accepted = QtWidgets.QInputDialog.getItem(
            self,
            "Add processing to pipeline",
            "Pipeline",
            choices,
            0,
            False,
        )
        if not accepted:
            return None
        if str(choice) == create_label:
            return self._prompt_new_pipeline()
        return self.pipeline_store.find_by_name(str(choice))

    def _processing_registry_entry(self, name: str):
        for category, tools in self.registry.items():
            if name in (tools or {}):
                return str(category), (tools or {})[name]
        return None, None

    def _processing_step_script_metadata(self, snapshot: dict, last_run: Optional[dict] = None) -> dict:
        """Resolve script metadata, preferring the exact values stored at run time."""
        for payload in (last_run or {}, (snapshot or {}).get("script", {}) or {}):
            name = str(payload.get("script_name", payload.get("name", "")) or "").strip()
            path = str(payload.get("script_path", payload.get("path", "")) or "").strip()
            if name or path:
                return {
                    "script_name": name or Path(path.replace("\\", "/")).name or "Unknown",
                    "script_path": path or "Unknown",
                }

        snapshot = snapshot or {}
        name = str(snapshot.get("processing_name", ""))
        category = str(snapshot.get("category", ""))
        spec = (self.registry.get(category, {}) or {}).get(name)
        if spec is None:
            _category, spec = self._processing_registry_entry(name)
        return _script_metadata_from_spec(spec)

    def _capture_processing_snapshot(self):
        name, spec = self._get_selected_tool_spec()
        if not spec or not name:
            name, spec = self._selected_name, self._selected_spec
        if not spec or not name:
            raise ValueError("No processing is selected.")
        params = self._collect_and_validate_params()
        if params is None:
            raise ValueError(self.params_error_lbl.text() or "The processing parameters are invalid.")
        project_action = self._is_project_action(spec)
        if not project_action and not self.target_paths:
            raise ValueError("No files are selected.")
        applicable = [] if project_action else self._compute_applicable_paths_for_tool(name, spec, params)
        if not project_action and not applicable:
            raise ValueError("No selected file is applicable to this processing.")
        category, _ = self._processing_registry_entry(name)
        script = _script_metadata_from_spec(spec)
        return {
            "category": category or "",
            "processing_name": str(name),
            "script": copy.deepcopy(script),
            "selected_files": list(self.target_paths),
            "applicable_files_at_save": list(applicable),
            "parameters": copy.deepcopy(params),
            "project_action": bool(project_action),
            "replace_existing_data": bool(
                self.replace_existing_chk.isVisible()
                and self.replace_existing_chk.isChecked()
            ),
        }

    def _add_current_to_pipeline(self):
        if self._pipeline_run is not None or self._running:
            return
        try:
            snapshot = self._capture_processing_snapshot()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
            return
        name = str(snapshot.get("processing_name", "Processing"))
        if self._editing_pipeline_step is not None:
            pipeline_id, step_id = self._editing_pipeline_step
            edited_name = str(self.pipeline_step_name_edit.text()).strip()
            if not edited_name:
                QtWidgets.QMessageBox.warning(self, "Processing pipeline", "The step name cannot be empty.")
                return
            try:
                self.pipeline_store.update_step(pipeline_id, step_id, edited_name, snapshot)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
                return
            self._editing_pipeline_step = None
            self._sync_pipeline_edit_buttons()
            self._active_pipeline_id = pipeline_id
            self._leave_pipeline_inline_editor()
            self._refresh_pipeline_tree_items()
            self._show_pipeline_view(pipeline_id)
            self._refresh_pipeline_view(selected_step_id=step_id)
            return
        else:
            pipeline = self._choose_pipeline_for_new_step()
            if pipeline is None:
                return
            try:
                self.pipeline_store.add_step(str(pipeline.get("id", "")), name, snapshot)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
                return
            self._active_pipeline_id = str(pipeline.get("id", ""))
        self._refresh_pipeline_tree_items()
        # Adding a step must leave the processing editor open. Rebuilding the
        # pipeline branch can otherwise make Qt select that branch and invoke
        # _show_pipeline_view through itemSelectionChanged.
        self.pipeline_widget.hide()
        self.details_widget.show()
        self.show_details_view()

    def _restore_processing_parameters(self, values: dict):
        for pname, value in (values or {}).items():
            widget = self.param_widgets.get(pname)
            if widget is None:
                continue
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
                continue
            if isinstance(widget, QtWidgets.QComboBox):
                display_maps = (
                    self._list_display_to_value_by_param.get(pname, {}),
                    self._ts_display_to_full_by_param.get(pname, {}),
                )
                restored = False
                for display_map in display_maps:
                    for display, real in display_map.items():
                        try:
                            matches = bool(real == value)
                        except Exception:
                            matches = False
                        if matches:
                            widget.setCurrentText(str(display))
                            restored = True
                            break
                    if restored:
                        break
                if not restored:
                    index = widget.findText(str(value))
                    if index >= 0:
                        widget.setCurrentIndex(index)
                continue
            widget.setText("" if value is None else str(value))

    def _restore_processing_snapshot(self, snapshot: dict, *, show_editor: bool):
        name = str(snapshot.get("processing_name", ""))
        category = str(snapshot.get("category", ""))
        spec = (self.registry.get(category, {}) or {}).get(name)
        if spec is None:
            _, spec = self._processing_registry_entry(name)
        if spec is None:
            raise ValueError(f'Processing "{name}" is no longer available.')
        self.target_paths = list(snapshot.get("selected_files", []) or [])
        self._selected_name = name
        self._selected_spec = spec
        self._show_details(name, spec)
        self._restore_processing_parameters(snapshot.get("parameters", {}) or {})
        self.replace_existing_chk.setChecked(
            bool(snapshot.get("replace_existing_data", False))
            and self.replace_existing_chk.isVisible()
        )
        self._refresh_availability_display()
        self._update_run_enabled()
        if show_editor:
            self.pipeline_widget.hide()
            self.details_widget.show()
            self.show_details_view()
        elif (
            self.details_widget.parent() is self.pipeline_edit_host
            and self._active_pipeline_step_id
        ):
            self.pipeline_widget.show()
            self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
            self.details_widget.show()
            self._set_pipeline_editor_editable(self._pipeline_inline_editing)

    def _sync_pipeline_edit_buttons(self):
        editing = self._editing_pipeline_step is not None
        in_pipeline = self.details_widget.parent() is self.pipeline_edit_host
        self.add_pipeline_btn.setText("Add to pipeline")
        self.add_pipeline_btn.setVisible(not in_pipeline)
        self.run_btn.setVisible(not in_pipeline)
        self.modify_step_btn.setVisible(not editing)
        self.save_step_changes_btn.setVisible(in_pipeline and editing)
        self.cancel_step_changes_btn.setVisible(in_pipeline and editing)

    def _enter_pipeline_inline_editor(self, step):
        self._attach_pipeline_editor()
        self.pipeline_step_name_edit.setText(str(step.get("name", "Step")))
        self.details_widget.show()
        self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
        self.proc_tree.setEnabled(False)
        self.new_pipeline_btn.setEnabled(False)
        self.rename_pipeline_btn.setEnabled(False)
        self.delete_pipeline_btn.setEnabled(False)
        self.launch_pipeline_btn.setEnabled(False)
        self.move_step_up_btn.setEnabled(False)
        self.move_step_down_btn.setEnabled(False)
        self.duplicate_step_btn.setEnabled(False)
        self.remove_step_btn.setEnabled(False)
        self.modify_step_btn.setEnabled(False)
        self.pipeline_run_result_btn.setEnabled(False)
        self._set_pipeline_editor_editable(True)

    def _leave_pipeline_inline_editor(self):
        self._set_pipeline_editor_editable(False)
        self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
        self.proc_tree.setEnabled(True)

    def _modify_pipeline_step(self):
        pipeline, step, _index = self._selected_pipeline_step()
        if pipeline is None or step is None or self._pipeline_run is not None or self._running:
            return
        self._editing_pipeline_step = (str(pipeline.get("id", "")), str(step.get("id", "")))
        self._sync_pipeline_edit_buttons()
        try:
            self._restore_processing_snapshot(copy.deepcopy(step.get("snapshot") or {}), show_editor=False)
            self._enter_pipeline_inline_editor(step)
        except Exception as exc:
            self._editing_pipeline_step = None
            self._leave_pipeline_inline_editor()
            self._sync_pipeline_edit_buttons()
            self._show_pipeline_view(str(pipeline.get("id", "")))
            self._refresh_pipeline_view(selected_step_id=str(step.get("id", "")))
            QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))

    def _cancel_pipeline_step_edit(self):
        pipeline_id = self._editing_pipeline_step[0] if self._editing_pipeline_step else self._active_pipeline_id
        step_id = self._editing_pipeline_step[1] if self._editing_pipeline_step else None
        self._editing_pipeline_step = None
        self._leave_pipeline_inline_editor()
        self._sync_pipeline_edit_buttons()
        self._show_pipeline_view(pipeline_id)
        self._refresh_pipeline_view(selected_step_id=step_id)

    def _duplicate_pipeline_step(self):
        pipeline, step, _index = self._selected_pipeline_step()
        if pipeline is None or step is None or self._pipeline_run is not None or self._running:
            return
        copy_name = f"{step.get('name', 'Step')} copy"
        try:
            new_step = self.pipeline_store.add_step(
                str(pipeline.get("id", "")),
                copy_name,
                copy.deepcopy(step.get("snapshot") or {}),
            )
            new_step["enabled"] = self._pipeline_step_enabled(step)
            self.pipeline_store.save()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Processing pipeline", str(exc))
            return
        self._refresh_pipeline_tree_items()
        self._refresh_pipeline_view(selected_step_id=str(new_step.get("id", "")))

    def _remove_pipeline_step(self):
        pipeline, step, _index = self._selected_pipeline_step()
        if pipeline is None or step is None or self._pipeline_run is not None or self._running:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Remove pipeline step",
            f'Remove "{step.get("name", "Step")}" from this pipeline?',
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.pipeline_store.remove_step(str(pipeline.get("id", "")), str(step.get("id", "")))
        self.refresh_list()
        self._show_pipeline_view(str(pipeline.get("id", "")))

    def _move_pipeline_step(self, offset: int):
        pipeline, step, _index = self._selected_pipeline_step()
        if pipeline is None or step is None or self._pipeline_run is not None or self._running:
            return
        step_id = str(step.get("id", ""))
        if self.pipeline_store.move_step(str(pipeline.get("id", "")), step_id, offset):
            self._refresh_pipeline_view(selected_step_id=step_id)

    def _launch_pipeline(self):
        pipeline = self._active_pipeline()
        if pipeline is None or not (pipeline.get("steps", []) or []) or self._running or self._pipeline_run is not None:
            return
        steps = list(pipeline.get("steps", []) or [])
        step_indices = [
            index
            for index, step in enumerate(steps)
            if self._pipeline_step_enabled(step)
        ]
        if not step_indices:
            QtWidgets.QMessageBox.warning(
                self,
                "Processing pipeline",
                "Select at least one pipeline step to run.",
            )
            return
        approved_replacements = self._pipeline_replacement_preflight(
            pipeline,
            steps,
            step_indices,
        )
        if approved_replacements is None:
            return
        skipped_steps = len(steps) - len(step_indices)
        pipeline_id = str(pipeline.get("id", ""))
        self._active_pipeline_id = pipeline_id
        self._active_pipeline_step_id = None
        self.pipeline_detail_stack.setCurrentWidget(self.pipeline_overview_page)
        self.details_widget.hide()
        self._select_pipeline_tree_item(pipeline_id)
        started_at = utc_now_text()
        run_header = (
            f"Processing pipeline: {pipeline.get('name', 'Pipeline')}\n"
            f"Started: {started_at}\n"
            f"Selected steps: {len(step_indices)}/{len(steps)}"
        )
        previous_log = str((pipeline.get("last_run") or {}).get("log", "") or "").rstrip()
        if previous_log:
            initial_log = (
                previous_log
                + "\n\n"
                + ("=" * 72)
                + "\nNEW PROCESSING PIPELINE RUN\n"
                + run_header
            )
        else:
            initial_log = run_header
        # Only selected steps belong to this launch.  Omitting unchecked steps
        # leaves their previously displayed status and execution metadata intact.
        step_statuses = {
            str(step.get("id", "")): "pending"
            for step in steps
            if self._pipeline_step_enabled(step) and str(step.get("id", ""))
        }
        pipeline["last_run"] = {
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "failed_steps": 0,
            "run_steps": len(step_indices),
            "skipped_steps": skipped_steps,
            "report_path": "",
            "log": initial_log,
            "step_statuses": dict(step_statuses),
        }
        self._pipeline_run = {
            "pipeline_id": pipeline_id,
            "index": 0,
            "step_indices": step_indices,
            "active_step_id": None,
            "step_log": "",
            "pipeline_log": initial_log,
            "step_statuses": dict(step_statuses),
            "approved_replacements": set(approved_replacements),
            "started_at": started_at,
            "failed_steps": 0,
            "report_lines": [
                f"Processing pipeline: {pipeline.get('name', 'Pipeline')}",
                f"Started: {started_at}",
                f"Selected steps: {len(step_indices)}/{len(steps)}",
                f"Skipped steps: {skipped_steps}",
                "",
            ],
        }
        self.pipeline_store.save()
        self._refresh_pipeline_view()
        self._run_next_pipeline_step()

    def _run_next_pipeline_step(self):
        state = self._pipeline_run
        if state is None:
            return
        pipeline = self.pipeline_store.get(state.get("pipeline_id"))
        if pipeline is None:
            self._pipeline_run = None
            return
        steps = pipeline.get("steps", []) or []
        step_indices = list(state.get("step_indices", []) or [])
        index = int(state.get("index", 0))
        if index >= len(step_indices):
            self._finish_pipeline_run()
            return
        step_index = int(step_indices[index])
        if step_index < 0 or step_index >= len(steps):
            self._finish_pipeline_run()
            return
        step = steps[step_index]
        state["active_step_id"] = str(step.get("id", ""))
        state["step_log"] = ""
        self._set_pipeline_step_run_status(step.get("id", ""), "running")
        now = utc_now_text()
        script = self._processing_step_script_metadata(step.get("snapshot") or {})
        step["last_run"] = {
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "script_name": script.get("script_name", "Unknown"),
            "script_path": script.get("script_path", "Unknown"),
            "error": "",
            "log": "",
        }
        self._append_pipeline_run_log(
            f"\n\nPipeline step {step_index + 1} "
            f"({index + 1}/{len(step_indices)} selected): {step.get('name', 'Step')}\n"
            f"Started: {now}\n"
            f"Script: {script.get('script_name', 'Unknown')}\n"
            f"Location: {script.get('script_path', 'Unknown')}"
        )
        self.pipeline_store.save()
        state["report_lines"].extend([
            f"PIPELINE STEP {step_index + 1} "
            f"({index + 1}/{len(step_indices)} selected): {step.get('name', 'Step')}",
            f"Started: {now}",
            f"Script: {script.get('script_name', 'Unknown')}",
            f"Location: {script.get('script_path', 'Unknown')}",
        ])
        self.jobs_label.setText(
            f"â— Pipeline {pipeline.get('name', 'Pipeline')}: "
            f"running {index + 1}/{len(step_indices)} selected steps"
        )
        self.jobs_label.setVisible(True)
        self._refresh_pipeline_view()
        browsing_state = self._capture_pipeline_browsing_state()
        try:
            self._active_pipeline_id = str(pipeline.get("id", ""))
            self._pipeline_restoring_step = True
            self._restore_processing_snapshot(copy.deepcopy(step.get("snapshot") or {}), show_editor=False)
            self._run_selected(_from_pipeline=True)
        except Exception:
            self._complete_pipeline_step(False, traceback.format_exc(), "")
        finally:
            self._pipeline_restoring_step = False
            self._restore_pipeline_browsing_state(browsing_state)

    def _complete_pipeline_step(self, success: bool, error: str = "", log_text: str = ""):
        state = self._pipeline_run
        if state is None:
            return
        pipeline = self.pipeline_store.get(state.get("pipeline_id"))
        if pipeline is None:
            self._pipeline_run = None
            return
        steps = pipeline.get("steps", []) or []
        step_indices = list(state.get("step_indices", []) or [])
        index = int(state.get("index", 0))
        if index >= len(step_indices):
            return
        step_index = int(step_indices[index])
        if step_index < 0 or step_index >= len(steps):
            return
        step = steps[step_index]
        captured_log = str(state.get("step_log", "")) if "step_log" in state else str(log_text or "")
        finished_at = utc_now_text()
        running_step = dict(step.get("last_run") or {})
        step["last_run"] = {
            "status": "completed" if success else "failed",
            "started_at": running_step.get("started_at"),
            "finished_at": finished_at,
            "script_name": running_step.get("script_name", "Unknown"),
            "script_path": running_step.get("script_path", "Unknown"),
            "error": str(error or ""),
            "log": captured_log,
        }
        if not success:
            state["failed_steps"] = int(state.get("failed_steps", 0)) + 1
        self._set_pipeline_step_run_status(
            step.get("id", ""),
            "completed" if success else "failed",
        )
        state["report_lines"].append(f"Status: {'COMPLETED' if success else 'FAILED'}")
        state["report_lines"].append(f"Finished: {finished_at}")
        if error:
            state["report_lines"].extend(["Full error:", str(error)])
        if captured_log:
            state["report_lines"].extend(["Step log:", captured_log])
        state["report_lines"].append("")
        completion_lines = [
            f"Status: {'COMPLETED' if success else 'FAILED'}",
            f"Finished: {finished_at}",
        ]
        if error:
            completion_lines.extend(["Full error:", str(error)])
        self._append_pipeline_run_log("\n".join(completion_lines))
        state["active_step_id"] = None
        state["index"] = index + 1
        self.pipeline_store.save()
        self._refresh_pipeline_view()
        if not success:
            self.jobs_label.setText(
                f"Pipeline {pipeline.get('name', 'Pipeline')}: "
                f"selected step {index + 1}/{len(step_indices)} failed; continuing"
            )
            self.jobs_label.setStyleSheet(
                "QLabel { background: #fff1f1; color: #b00020; border: 1px solid #efb5b5; "
                "border-radius: 4px; padding: 3px 5px; }"
            )
            self.jobs_label.setVisible(True)
        QtCore.QTimer.singleShot(0, self._run_next_pipeline_step)

    def _finish_pipeline_run(self):
        state = self._pipeline_run
        if state is None:
            return
        pipeline = self.pipeline_store.get(state.get("pipeline_id"))
        if pipeline is None:
            self._pipeline_run = None
            return
        finished_at = utc_now_text()
        failed_steps = int(state.get("failed_steps", 0))
        run_steps = len(state.get("step_indices", []) or [])
        skipped_steps = max(0, len(pipeline.get("steps", []) or []) - run_steps)
        status = "completed_with_failures" if failed_steps else "completed"
        state["report_lines"].extend([
            f"Finished: {finished_at}",
            f"Final status: {status.replace('_', ' ')}",
            f"Run steps: {run_steps}",
            f"Skipped steps: {skipped_steps}",
            f"Failed steps: {failed_steps}",
        ])
        self._append_pipeline_run_log(
            f"\n\nPipeline finished: {finished_at}\n"
            f"Final status: {status.replace('_', ' ')}\n"
            f"Run steps: {run_steps}\n"
            f"Skipped steps: {skipped_steps}\n"
            f"Failed steps: {failed_steps}"
        )
        try:
            report_path = self.pipeline_store.write_report(
                pipeline,
                state.get("report_lines", []),
                state.get("started_at", ""),
            )
            report_text = self.pipeline_store.project_relative_path(report_path)
        except Exception:
            report_text = ""
        pipeline["last_run"] = {
            "status": status,
            "started_at": state.get("started_at"),
            "finished_at": finished_at,
            "failed_steps": failed_steps,
            "run_steps": run_steps,
            "skipped_steps": skipped_steps,
            "report_path": report_text,
            "log": str(state.get("pipeline_log", "") or ""),
            "step_statuses": dict(state.get("step_statuses") or {}),
        }
        pipeline["updated_at"] = finished_at
        pipeline_id = str(pipeline.get("id", ""))
        self._pipeline_run = None
        self.pipeline_store.save()
        if failed_steps:
            self.jobs_label.setText(
                f"Pipeline {pipeline.get('name', 'Pipeline')} finished with {failed_steps} failed step(s)."
            )
            self.jobs_label.setStyleSheet(
                "QLabel { background: #fff1f1; color: #b00020; border: 1px solid #efb5b5; "
                "border-radius: 4px; padding: 3px 5px; }"
            )
        else:
            self.jobs_label.setText(f"Pipeline {pipeline.get('name', 'Pipeline')} completed.")
            self.jobs_label.setStyleSheet(
                "QLabel { background: #f6faf7; color: #2e7d32; border: 1px solid #cfe8d2; "
                "border-radius: 4px; padding: 3px 5px; }"
            )
        self.jobs_label.setVisible(True)
        self._refresh_pipeline_tree_items()
        self._refresh_pipeline_view()
        self._update_run_enabled()

    # ---------------- discovery ----------------

    def _discover_processing_tools(self) -> dict:
        project_root = Path(getattr(self.GUI_instance, "project_root", "."))
        processing_script_dir = project_root / "Script" / "Processing"
        return discover_for_panel(discover_tools, "Processings", processing_script_dir, self)

    def _patch_tool_function_for_qt_runner(self, func):
        """Make existing processing wrappers use the Qt-compatible runner.

        Many Processing scripts import run_file_processing from GUI.processing_runner
        at module import time. Patching function globals lets the same scripts run
        with the Qt log panel instead of opening Tk windows.
        """
        try:
            g = getattr(func, "__globals__", None)
            if not isinstance(g, dict):
                return
            g["run_file_processing"] = run_file_processing
            g["RunnerUI"] = RunnerUI
            g["RunnerBehavior"] = RunnerBehavior
            g["get_applicable_paths"] = get_applicable_paths
            g["filter_applicable_by_content_index"] = filter_applicable_by_content_index
            g["_skip_if_output_exists"] = _skip_if_output_exists
        except Exception:
            pass

    # ---------------- public API ----------------

    def set_registry(self, registry: dict):
        self.registry = registry or {}
        for tools in self.registry.values():
            for spec in (tools or {}).values():
                fn = spec.get("func") if isinstance(spec, dict) else None
                if callable(fn):
                    self._patch_tool_function_for_qt_runner(fn)
        self.refresh_list()

    def set_target_paths(self, paths: list[str]):
        """Update the file selection without navigating away from the open view.

        File and filter clicks are selection changes, not navigation actions.
        Rebuilding ``proc_tree`` here used to destroy the selected pipeline-step
        item and could strand the inline editor after its files were changed.
        Keep the tree and its selection intact, refresh only the enabled state
        of ordinary processing tools, and rebuild the currently visible editor
        in place so file-dependent parameter choices stay current.
        """
        keep_params = {}
        for param_name in self.param_specs or {}:
            try:
                keep_params[param_name] = copy.deepcopy(
                    self._resolve_param_value(param_name, for_validation=False)
                )
            except Exception:
                pass
        keep_replace_existing = bool(
            self.replace_existing_chk.isVisible()
            and self.replace_existing_chk.isChecked()
        )
        was_pipeline_editable = bool(self._pipeline_inline_editing)

        self.target_paths = list(
            dict.fromkeys(str(path) for path in (paths or []) if str(path).strip())
        )
        self._refresh_tool_enabled_states()

        editor_is_open = bool(
            self.pipeline_widget.isHidden()
            or self.pipeline_detail_stack.currentWidget() is self.pipeline_edit_page
        )
        if editor_is_open and self._selected_name and self._selected_spec:
            self._show_details(self._selected_name, self._selected_spec)
            self._restore_processing_parameters(keep_params)
            self.replace_existing_chk.setChecked(
                keep_replace_existing and self.replace_existing_chk.isVisible()
            )
            if self.details_widget.parent() is self.pipeline_edit_host:
                self._set_pipeline_editor_editable(was_pipeline_editable)
            self._refresh_availability_display()
            self._update_run_enabled()

    def _refresh_tool_enabled_states(self):
        """Refresh ordinary tool availability without rebuilding the tree."""
        items_by_id = {}
        signals_were_blocked = self.proc_tree.blockSignals(True)
        try:
            iterator = QtWidgets.QTreeWidgetItemIterator(self.proc_tree)
            while iterator.value():
                item = iterator.value()
                items_by_id[id(item)] = item
                iterator += 1
            for item_id, (category, name) in list(self._item_to_ref.items()):
                spec = (self.registry.get(category, {}) or {}).get(name)
                item = items_by_id.get(item_id)
                if spec is None or item is None:
                    continue
                enabled = self._is_enabled(spec)
                item.setDisabled(not enabled)
                if not enabled:
                    try:
                        set_item_foreground(item, "#888888")
                    except Exception:
                        pass
        finally:
            self.proc_tree.blockSignals(signals_were_blocked)
        self._apply_running_highlight()

    def show_details_view(self):
        try:
            self.right_panel.show_processing_view()
        except Exception:
            pass

    def hide_details_view(self):
        pass

    def refresh_list(self):
        signals_were_blocked = self.proc_tree.blockSignals(True)
        try:
            self.proc_tree.clear()
            self._item_to_ref.clear()

            for category in sorted(self.registry.keys(), key=str.lower):
                cat_item = QtWidgets.QTreeWidgetItem([str(category)])
                self.proc_tree.addTopLevelItem(cat_item)
                cat_item.setExpanded(True)

                tools = self.registry.get(category, {}) or {}
                for name in sorted(tools.keys(), key=str.lower):
                    spec = tools[name]
                    item = QtWidgets.QTreeWidgetItem([str(name)])
                    cat_item.addChild(item)
                    self._item_to_ref[id(item)] = (category, name)
                    if not self._is_enabled(spec):
                        item.setDisabled(True)
                        try:
                            set_item_foreground(item, "#888888")
                        except Exception:
                            pass
            self._append_pipeline_tree_items()
        finally:
            self.proc_tree.blockSignals(signals_were_blocked)
        self._apply_running_highlight()

    def is_running(self) -> bool:
        return bool(self._running)

    def processing_started(self, tool_name: str | None = None):
        self._set_processing_running(True, tool_name)

    def processing_finished(self):
        self._set_processing_running(False)

    def _refresh_after_processed_file(self, transfer=None, tool_name: str | None = None):
        """Reload inventory after one successful file while keeping the run active."""
        try:
            if hasattr(self.GUI_instance, "reload_inventory"):
                self.GUI_instance.reload_inventory()
                return
        except Exception:
            try:
                self.post_log("Could not reload inventory after processed file:")
                self.post_log(traceback.format_exc())
            except Exception:
                pass

        if transfer is not None:
            try:
                self._sync_full_inventory_after_processing(transfer)
            except Exception:
                try:
                    self.post_log("Could not sync inventory after processed file:")
                    self.post_log(traceback.format_exc())
                except Exception:
                    pass

    def _finish_processing_from_runner(self, transfer=None, tool_name: str | None = None):
        """Finalize a background processing run on the GUI thread."""
        name = str(tool_name or self._running_tool_name or self._selected_name or "Processing").strip()
        completed_paths = list(self.target_paths)
        if self._pipeline_run is not None:
            running_step = self._running_pipeline_step()
            running_snapshot = (running_step or {}).get("snapshot", {}) or {}
            completed_paths = list(
                running_snapshot.get("selected_files", completed_paths) or []
            )
        step_log = self.log_edit.toPlainText()

        # Reload from disk first.  The runner writes nwb_inventory.xlsx after
        # conversion/processing, so this is the safest source of truth and fixes
        # the file tree immediately after the job finishes.
        reloaded = False
        try:
            if hasattr(self.GUI_instance, "reload_inventory"):
                self.GUI_instance.reload_inventory()
                reloaded = True
        except Exception:
            try:
                self.post_log("Could not reload inventory from disk after processing:")
                self.post_log(traceback.format_exc())
            except Exception:
                pass

        if not reloaded and transfer is not None:
            try:
                self._sync_full_inventory_after_processing(transfer)
            except Exception:
                try:
                    self.post_log("Could not sync inventory after processing:")
                    self.post_log(traceback.format_exc())
                except Exception:
                    pass

        self._set_processing_running(False, name)

        try:
            if self._pipeline_run is None and self._selected_name and self._selected_spec:
                self._show_details(self._selected_name, self._selected_spec)
        except Exception:
            pass

        if callable(self.on_run_done):
            try:
                self.on_run_done(name, completed_paths)
            except Exception:
                pass

        if self._pipeline_run is not None:
            report = getattr(transfer, "processing_run_report", None) if transfer is not None else None
            report = report if isinstance(report, dict) else {}
            success = bool(report.get("success", True))
            error_blocks = []
            for failure in report.get("errors", []) or []:
                if not isinstance(failure, dict):
                    error_blocks.append(str(failure))
                    continue
                path = str(failure.get("file_path", "") or "")
                heading = f"File: {path}\n" if path else ""
                error_blocks.append(heading + str(failure.get("traceback") or failure.get("error") or "Processing failed"))
            self._complete_pipeline_step(success, "\n\n".join(error_blocks), step_log)

    def _set_processing_running(self, running: bool, tool_name: str | None = None):
        running = bool(running)
        name = str(tool_name or self._running_tool_name or self._selected_name or "Processing").strip() or "Processing"
        changed = (self._running != running)
        self._running = running
        self._running_tool_name = name if running else ""
        try:
            self.replace_existing_chk.setEnabled(
                not running and self._tool_supports_replacement(self._selected_spec)
            )
        except Exception:
            pass

        try:
            if running:
                self.jobs_label.setStyleSheet(
                    "QLabel { background: #f6faf7; color: #2e7d32; "
                    "border: 1px solid #cfe8d2; border-radius: 4px; padding: 3px 5px; }"
                )
                self.jobs_label.setText(f"â— {name} is running")
                self.jobs_label.setVisible(True)
            elif self._pipeline_run is None:
                self.jobs_label.setVisible(False)
        except Exception:
            pass

        self._apply_running_highlight()
        self._update_run_enabled()

        if changed:
            try:
                self.runningStateChanged.emit(running)
            except Exception:
                pass

    def _apply_running_highlight(self):
        clear_bg = QtGui.QBrush()
        clear_fg = QtGui.QBrush()
        running_bg = QtGui.QBrush(QtGui.QColor("#eef8f0"))
        running_fg = QtGui.QBrush(QtGui.QColor("#2e7d32"))
        running_name = str(self._running_tool_name or "") if self._running else ""

        it = QtWidgets.QTreeWidgetItemIterator(self.proc_tree)
        while it.value():
            item = it.value()
            if (
                item is self._pipeline_root_item
                or id(item) in self._pipeline_item_to_id
                or id(item) in self._pipeline_step_item_to_ref
            ):
                it += 1
                continue
            ref = self._item_to_ref.get(id(item))
            item_name = ref[1] if ref else ""
            if running_name and item_name == running_name:
                item.setBackground(0, running_bg)
                item.setForeground(0, running_fg)
                item.setToolTip(0, f"{running_name} is running")
            else:
                item.setBackground(0, clear_bg)
                item.setForeground(0, clear_fg)
                item.setToolTip(0, "")
                if item.isDisabled():
                    try:
                        set_item_foreground(item, "#888888")
                    except Exception:
                        pass
            it += 1

    # ---------------- selection/details ----------------

    def _is_enabled(self, spec: dict) -> bool:
        fn = (spec or {}).get("enabled_if", None)
        if callable(fn):
            try:
                return bool(fn(self.target_paths))
            except Exception:
                return False
        return True

    def _is_project_action(self, spec: Optional[dict]) -> bool:
        return bool((spec or {}).get("project_action", False))

    def _tool_supports_replacement(self, spec: Optional[dict]) -> bool:
        if self._is_project_action(spec):
            return False
        raw_params = (spec or {}).get("params", {}) or {}
        if not isinstance(raw_params, dict):
            return False
        return any(
            isinstance(entry, dict)
            and str(entry.get("type", "")).strip()
            in ("saving_name", "internal_saving_name_list")
            for entry in raw_params.values()
        )

    def _get_selected_tool_spec(self) -> Tuple[Optional[str], Optional[dict]]:
        items = self.proc_tree.selectedItems()
        if not items:
            return None, None
        item = items[0]
        ref = self._item_to_ref.get(id(item))
        if not ref:
            return None, None
        category, name = ref
        spec = (self.registry.get(category, {}) or {}).get(name)
        return name, spec

    def _on_select(self):
        pipeline_kind, pipeline_id, step_id = self._pipeline_tree_selection()
        if pipeline_kind in ("root", "pipeline"):
            self._show_pipeline_view(pipeline_id)
            return
        if pipeline_kind == "step":
            self._show_pipeline_step(str(pipeline_id), str(step_id))
            return
        self._detach_pipeline_editor()
        self._active_pipeline_step_id = None
        self.pipeline_widget.hide()
        self.details_widget.show()
        name, spec = self._get_selected_tool_spec()
        self.show_details_view()
        if not spec:
            self._selected_name = None
            self._selected_spec = None
            self._clear_details()
            return
        self._selected_name = name
        self._selected_spec = spec
        self._show_details(name or "", spec)

    def _clear_details(self):
        self.title_lbl.setText("")
        self.applies_lbl.setText("")
        self.available_lbl.setText("Available files: 0")
        self.desc_lbl.setText("")
        self.params_error_lbl.setText("")
        self._clear_form()
        self.param_widgets = {}
        self.param_specs = {}
        self._ts_display_to_full_by_param = {}
        self._list_display_to_value_by_param = {}
        self._replace_option_tool_name = None
        self.replace_existing_chk.setChecked(False)
        self.replace_existing_chk.setVisible(False)
        self.run_btn.setEnabled(False)
        self.add_pipeline_btn.setEnabled(False)

    def _show_details(self, name: str, spec: dict):
        restoring_pipeline_step = bool(getattr(self, "_pipeline_restoring_step", False))
        if not restoring_pipeline_step and self.details_widget.parent() is not self.pipeline_edit_host:
            self.pipeline_widget.hide()
        if not restoring_pipeline_step:
            self.details_widget.show()
        self.title_lbl.setText(str(spec.get("name", name)))
        self.applies_lbl.setText("Applies to current project" if self._is_project_action(spec) else "Applies to selected files that match tool criteria")
        self.desc_lbl.setText(str(spec.get("description", "") or ""))
        self._build_params_ui(spec)
        supports_replacement = self._tool_supports_replacement(spec)
        if self._replace_option_tool_name != str(name):
            self.replace_existing_chk.setChecked(False)
        self._replace_option_tool_name = str(name)
        self.replace_existing_chk.setVisible(supports_replacement)
        self.replace_existing_chk.setEnabled(supports_replacement and not self._running)
        if not supports_replacement:
            self.replace_existing_chk.setChecked(False)
        self._refresh_availability_display()
        self._update_run_enabled()
        self._sync_pipeline_edit_buttons()

    # ---------------- NWB choices helpers ----------------

    def _content_df_for_selection(self):
        cdf = getattr(self.GUI_instance, "content_df", None)
        if cdf is None or not isinstance(cdf, pd.DataFrame) or cdf.empty:
            return None
        if "file_path" not in cdf.columns:
            return None
        selected_nwb = {str(p) for p in (self.target_paths or []) if str(p).lower().endswith(".nwb")}
        if not selected_nwb:
            return None
        out = cdf[cdf["file_path"].astype(str).isin(selected_nwb)].copy()
        return None if out.empty else out

    @staticmethod
    def _row_to_timeseries_ref(row) -> Optional[str]:
        container = str(row.get("container", "")).strip()
        module = str(row.get("module", "")).strip()
        name = str(row.get("name", "")).strip()
        nwb_path = str(row.get("nwb_path", "")).strip()

        if nwb_path:
            s = nwb_path.replace("\\", "/").strip("/")
            if s.startswith("acquisition/"):
                key = s[len("acquisition/"):].strip("/")
                if key:
                    return f"acquisition:{key}"
            if s.startswith("processing/"):
                parts = [x for x in s.split("/") if x]
                if len(parts) >= 3:
                    mod = parts[1].strip()
                    key = "/".join(parts[2:]).strip()
                    if mod and key:
                        return f"processing:{mod}:{key}"

        if container == "acquisition" and name:
            return f"acquisition:{name}"
        if container == "processing" and module and name:
            return f"processing:{module}:{name}"
        return None

    def _list_data_refs_for_selection(self, ps: Optional[dict] = None) -> list[str]:
        cdf = self._content_df_for_selection()
        if cdf is None or cdf.empty:
            return []
        allow_types = None
        if ps:
            nts = ps.get("neurodata_type_in", None)
            if nts is not None:
                allow_types = {str(x).strip() for x in nts if str(x).strip()}
        cdf2 = cdf.copy()
        if allow_types is not None and "neurodata_type" in cdf2.columns:
            cdf2 = cdf2[cdf2["neurodata_type"].astype(str).isin(allow_types)]
        out = []
        for _, row in cdf2.iterrows():
            ref = self._row_to_timeseries_ref(row)
            if ref:
                out.append(ref)
        return sorted(set(out))

    def _list_processing_modules_for_selection(self) -> list[str]:
        cdf = self._content_df_for_selection()
        if cdf is None or cdf.empty:
            return []
        if "container" not in cdf.columns or "module" not in cdf.columns:
            return []
        cdf2 = cdf[cdf["container"].astype(str).str.strip() == "processing"]
        mods = [str(x).strip() for x in cdf2.get("module", pd.Series(dtype=str)).dropna().astype(str).tolist() if str(x).strip()]
        return sorted(set(mods))

    @staticmethod
    def _make_timeseries_display_map(full_refs: list[str]) -> Dict[str, str]:
        parsed: List[tuple[str, str, str, str]] = []
        for full in full_refs:
            s = str(full)
            parts = s.split(":")
            if len(parts) >= 2 and parts[0] == "acquisition":
                parsed.append((s, "acquisition", "", ":".join(parts[1:])))
            elif len(parts) >= 3 and parts[0] == "processing":
                parsed.append((s, "processing", parts[1], ":".join(parts[2:])))
            else:
                parsed.append((s, "unknown", "", s))
        first_labels = []
        for _full, container, module, key in parsed:
            if container == "acquisition":
                first_labels.append(key)
            elif container == "processing":
                first_labels.append(f"{key} ({module})")
            else:
                first_labels.append(key)
        counts: Dict[str, int] = {}
        for lab in first_labels:
            counts[lab] = counts.get(lab, 0) + 1
        mapping: Dict[str, str] = {}
        for (full, *_), lab in zip(parsed, first_labels):
            mapping[lab if counts.get(lab, 0) == 1 else full] = full
        return mapping

    @staticmethod
    def _make_list_display_map(values: list[Any]) -> Dict[str, Any]:
        base_labels = [str(v) for v in values]
        counts: Dict[str, int] = {}
        for lab in base_labels:
            counts[lab] = counts.get(lab, 0) + 1
        display_map: Dict[str, Any] = {}
        used_labels: Dict[str, int] = {}
        for v in values:
            base = str(v)
            if counts[base] == 1:
                label = base
            else:
                label = f"{base} ({type(v).__name__})"
                if label in display_map:
                    used_labels[label] = used_labels.get(label, 1) + 1
                    label = f"{label} [{used_labels[label]}]"
                else:
                    used_labels[label] = 1
            display_map[label] = v
        return display_map

    # ---------------- params UI ----------------

    def _clear_form(self):
        while self.params_form.count():
            item = self.params_form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _normalize_param_entry(self, pname: str, entry: Any) -> Tuple[dict, Any, Optional[str]]:
        if isinstance(entry, dict) and "type" in entry:
            spec = dict(entry)
            return spec, spec.get("default", None), spec.get("type", None)
        return {}, entry, None

    def _build_params_ui(self, spec: dict):
        self._clear_form()
        self.params_error_lbl.setText("")
        self.param_widgets = {}
        self.param_specs = {}
        self._ts_display_to_full_by_param = {}
        self._list_display_to_value_by_param = {}

        raw = (spec.get("params") or {}) if isinstance(spec, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        filtered_items = [(k, v) for k, v in raw.items() if k not in params_variable_not_be_displayed_in_GUI_list]
        if not filtered_items:
            self.params_form.addRow(QtWidgets.QLabel("No parameters."))
            return

        for pname, entry in filtered_items:
            spec_dict, default_value, ptype = self._normalize_param_entry(pname, entry)
            if spec_dict:
                self.param_specs[pname] = spec_dict
            label = str(spec_dict.get("label", pname) if spec_dict else pname)
            required = bool(spec_dict.get("required", False)) if spec_dict else False
            if required:
                label += " *"

            # choice/list
            if ptype in ("choice", "list"):
                values = list(spec_dict.get("choices", []) if ptype == "choice" else spec_dict.get("values", []) or [])
                display_map = self._make_list_display_map(values)
                choices = list(display_map.keys())
                self._list_display_to_value_by_param[pname] = display_map
                default_value = spec_dict.get("default", values[0] if values else "")
                default_display = next((disp for disp, real in display_map.items() if real == default_value), choices[0] if choices else "")
                cb = QtWidgets.QComboBox()
                cb.addItems([str(c) for c in choices])
                if default_display:
                    cb.setCurrentText(str(default_display))
                cb.currentTextChanged.connect(self._on_param_change)
                self.param_widgets[pname] = cb
                self.params_form.addRow(label, cb)
                continue

            # nwb timeseries/data
            if ptype in ("nwb_timeseries", "data"):
                full_refs = [str(x) for x in self._list_data_refs_for_selection(spec_dict) if str(x).strip()]
                display_map = self._make_timeseries_display_map(full_refs)
                choices = sorted(display_map.keys())
                self._ts_display_to_full_by_param[pname] = display_map
                raw_default = str(spec_dict.get("default", "") or "").strip()
                resolved_full = raw_default if raw_default in full_refs else (full_refs[0] if full_refs else "")
                default_display = next((disp for disp, full in display_map.items() if str(full) == resolved_full), choices[0] if choices else "")
                cb = QtWidgets.QComboBox()
                cb.addItems([str(c) for c in choices])
                if default_display:
                    cb.setCurrentText(str(default_display))
                cb.currentTextChanged.connect(self._on_param_change)
                self.param_widgets[pname] = cb
                self.params_form.addRow(label, cb)
                continue

            # processing module
            if ptype == "nwb_processing_module":
                choices = self._list_processing_modules_for_selection()
                allow_new = bool(spec_dict.get("allow_new", True))
                sentinel = "processing:<new...>"
                if allow_new:
                    choices = list(choices) + [sentinel]
                default = str(spec_dict.get("default", choices[0] if choices else sentinel))
                cb = QtWidgets.QComboBox()
                cb.addItems([str(x) for x in choices])
                cb.setCurrentText(default if default in choices else (choices[0] if choices else ""))
                new_edit = QtWidgets.QLineEdit(str(spec_dict.get("new_default", "")))
                new_edit.setPlaceholderText(str(spec_dict.get("new_label", "New module")))
                row_widget = QtWidgets.QWidget()
                row_layout = QtWidgets.QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(cb, stretch=1)
                row_layout.addWidget(new_edit, stretch=1)

                def _toggle_new(*_):
                    new_edit.setVisible(cb.currentText() == sentinel)
                    self._on_param_change()

                cb.currentTextChanged.connect(_toggle_new)
                new_edit.textChanged.connect(self._on_param_change)
                self.param_widgets[pname] = (cb, new_edit)
                self.params_form.addRow(label, row_widget)
                _toggle_new()
                continue

            # bool
            if ptype == "bool":
                chk = QtWidgets.QCheckBox(label)
                chk.setChecked(bool(spec_dict.get("default", False)))
                chk.stateChanged.connect(self._on_param_change)
                self.param_widgets[pname] = chk
                self.params_form.addRow("", chk)
                continue

            # default entry
            edit = QtWidgets.QLineEdit("" if default_value is None else str(default_value))
            edit.textChanged.connect(self._on_param_change)
            self.param_widgets[pname] = edit
            self.params_form.addRow(label, edit)

    # ---------------- availability/params ----------------

    def _get_widget_raw_value(self, pname: str):
        w = self.param_widgets.get(pname)
        if isinstance(w, QtWidgets.QLineEdit):
            return w.text()
        if isinstance(w, QtWidgets.QCheckBox):
            return w.isChecked()
        if isinstance(w, QtWidgets.QComboBox):
            return w.currentText()
        if isinstance(w, tuple) and len(w) == 2:
            return w[0].currentText()
        return ""

    def _effective_tool_params_for_availability(self, spec: dict) -> dict:
        base = spec.get("params", {}) or {}
        if not isinstance(base, dict):
            return {}
        out: dict[str, Any] = {}
        dtr = base.get("data_type_requirement", None)
        if isinstance(dtr, str) and dtr.strip():
            out["data_type_requirement"] = dtr.strip()
        elif isinstance(dtr, (list, tuple, set)):
            vals = [str(x).strip() for x in dtr if str(x).strip()]
            if vals:
                out["data_type_requirement"] = vals
        for pname, ps in (self.param_specs or {}).items():
            if not isinstance(ps, dict):
                continue
            ptype = str(ps.get("type", "")).strip()
            if ptype not in ("data", "nwb_timeseries"):
                continue
            entry = dict(ps)
            cur = str(self._resolve_param_value(pname, for_validation=False)).strip()
            if cur and cur != "2":
                entry["default"] = cur
            else:
                entry.pop("default", None)
            out[pname] = entry
        return out

    def _compute_applicable_paths_for_tool(
        self,
        name,
        spec,
        validated_params,
        *,
        replace_existing: Optional[bool] = None,
    ):
        spec_params = spec.get("params", {}) or {}
        match = spec_params.get("match", None) if isinstance(spec_params, dict) else None
        transfer = Processing_data_transfer(self.GUI_instance, self, self.target_paths, name)
        if replace_existing is None:
            replace_existing = bool(
                self.replace_existing_chk.isVisible()
                and self.replace_existing_chk.isChecked()
            )
        transfer.replace_existing_data = bool(replace_existing)
        applicable = get_applicable_paths(
            transfer,
            tool_name=name,
            match=match if match is not None else (lambda _p: True),
        )
        safe_user_params: dict[str, Any] = {}
        for key in ("category", "internal_saving_name_list"):
            if key in validated_params:
                safe_user_params[key] = validated_params[key]
            elif isinstance(spec_params, dict) and key in spec_params:
                entry = spec_params.get(key)
                safe_user_params[key] = entry.get("default", "") if isinstance(entry, dict) else entry
        for pname, ps in (self.param_specs or {}).items():
            if not isinstance(ps, dict):
                continue
            if str(ps.get("type", "")).strip() not in ("data", "nwb_timeseries"):
                continue
            raw = str(self._resolve_param_value(pname, for_validation=False)).strip()
            if raw:
                safe_user_params[pname] = raw
        transfer.tool_params = safe_user_params
        tool_params = self._effective_tool_params_for_availability(spec)
        applicable = filter_applicable_by_content_index(applicable, transfer, tool_params)
        if not transfer.replace_existing_data:
            applicable = _skip_if_output_exists(
                applicable, transfer, tool_params, safe_user_params
            )
        return list(applicable)

    def _refresh_availability_display(self):
        name = self._selected_name
        spec = self._selected_spec
        if not name or not spec:
            self.available_lbl.setText("Available files: 0")
            self.run_btn.setEnabled(False)
            return
        if self._running:
            self.available_lbl.setText(f"{self._running_tool_name or 'Processing'} is running")
            self.run_btn.setEnabled(False)
            return
        if self._is_project_action(spec):
            self.available_lbl.setText("Available files: Project action")
            self.run_btn.setEnabled(callable(spec.get("func", None)))
            return
        validated = self._collect_and_validate_params()
        if validated is None:
            validated = {}
            self.run_btn.setEnabled(False)
        n_avail = len(self._compute_applicable_paths_for_tool(name, spec, validated))
        self.available_lbl.setText(f"Available files: {n_avail}")
        self.run_btn.setEnabled(n_avail > 0 and callable(spec.get("func", None)))

    def _resolve_param_value(self, pname: str, *, for_validation: bool = True):
        ps = (self.param_specs or {}).get(pname, None)
        ptype = ps.get("type", "str") if isinstance(ps, dict) else "str"
        raw = self._get_widget_raw_value(pname)

        if ptype in ("data", "nwb_timeseries"):
            return self._ts_display_to_full_by_param.get(pname, {}).get(str(raw), "")
        if ptype in ("choice", "list"):
            return self._list_display_to_value_by_param.get(pname, {}).get(str(raw), raw)
        if ptype == "nwb_processing_module":
            w = self.param_widgets.get(pname)
            choice = str(raw).strip()
            sentinel = "processing:<new...>"
            if choice == sentinel and isinstance(w, tuple):
                new_name = w[1].text().strip()
                return f"processing:{new_name}" if new_name else ""
            return choice if choice.startswith("processing:") else f"processing:{choice}" if choice else ""
        return raw

    def _collect_and_validate_params(self) -> Optional[dict]:
        params: dict[str, Any] = {}
        errors: list[str] = []
        for pname, ps in (self.param_specs or {}).items():
            ptype = ps.get("type", "str") if isinstance(ps, dict) else "str"
            required = bool(ps.get("required", False)) if isinstance(ps, dict) else False
            raw = self._resolve_param_value(pname)
            if required and str(raw).strip() == "":
                errors.append(f"{pname} is required")
                continue
            try:
                if ptype == "int":
                    val = None if str(raw).strip() == "" else int(raw)
                elif ptype == "float":
                    val = None if str(raw).strip() == "" else float(raw)
                elif ptype == "bool":
                    val = bool(raw)
                else:
                    val = raw
                params[pname] = val
                if ptype == "saving_name":
                    params.setdefault("internal_saving_name_list", [])
                    if isinstance(val, (list, tuple, set)):
                        params["internal_saving_name_list"].extend([str(x).strip() for x in val if str(x).strip()])
                    elif str(val).strip():
                        params["internal_saving_name_list"].append(str(val).strip())
                if isinstance(ps, dict) and ptype in ("int", "float") and val is not None:
                    v = float(val)
                    if "min" in ps and v < float(ps["min"]):
                        errors.append(f"{pname} must be >= {ps['min']}")
                    if "max" in ps and v > float(ps["max"]):
                        errors.append(f"{pname} must be <= {ps['max']}")
            except Exception as exc:
                errors.append(f"{pname}: {exc}")
        # Include plain default params that are hidden but needed by runner/tool.
        spec = self._selected_spec or {}
        raw_base = spec.get("params", {}) or {}
        if isinstance(raw_base, dict):
            for k, entry in raw_base.items():
                if k in params or k in params_variable_not_be_displayed_in_GUI_list:
                    continue
                if isinstance(entry, dict) and "type" in entry:
                    continue
                params[k] = entry
        if errors:
            self.params_error_lbl.setText("; ".join(errors))
            return None
        self.params_error_lbl.setText("")
        return params

    def _on_param_change(self):
        self._availability_timer.start()
        self._update_run_enabled()

    def _update_run_enabled(self):
        name = self._selected_name
        spec = self._selected_spec
        if not name or not spec:
            self.run_btn.setEnabled(False)
            self.add_pipeline_btn.setEnabled(False)
            return
        if self._running or self._pipeline_run is not None:
            self.run_btn.setEnabled(False)
            self.add_pipeline_btn.setEnabled(False)
            return
        fn_ok = callable(spec.get("func", None))
        params = self._collect_and_validate_params()
        params_ok = params is not None
        if self._is_project_action(spec):
            enabled = fn_ok and params_ok
            self.run_btn.setEnabled(enabled)
            self.add_pipeline_btn.setEnabled(enabled)
            return
        n_avail = len(self._compute_applicable_paths_for_tool(name, spec, params or {})) if params_ok else 0
        enabled = fn_ok and params_ok and n_avail > 0
        self.run_btn.setEnabled(enabled)
        self.add_pipeline_btn.setEnabled(enabled)

    def _sync_full_inventory_after_processing(self, transfer):
        """Synchronize full inventory tables back to the app without losing filters."""
        app = self.GUI_instance
        inv_df = getattr(transfer, "NWB_inventory_df", None)
        content_df = getattr(transfer, "NWB_content_df", None)

        if isinstance(inv_df, pd.DataFrame):
            full = inv_df.copy(deep=True)
            app.full_df = full.copy(deep=True)
            app._df_master = full.copy(deep=True)

        if isinstance(content_df, pd.DataFrame):
            full_content = content_df.copy(deep=True)
            app.content_df = full_content.copy(deep=True)
            app._content_df_master = full_content.copy(deep=True)

        # Reapply active file filters to the new full inventory. If no filters
        # are active, show the complete table.
        try:
            if getattr(app, "current_file_filters", None):
                app.filter_panel._apply_filters()
            elif isinstance(getattr(app, "full_df", None), pd.DataFrame):
                app.df = app.full_df.copy(deep=True)
                app.file_tree_panel.df = app.df
                app.file_tree_panel.build_tree()
        except Exception:
            pass

    # ---------------- run/log ----------------

    def show_run_log(self, title: str = "Processing"):
        self.log_reset.emit(str(title))

    def post_log(self, text: str):
        self.log_message.emit(str(text))

    def _reset_log_on_gui_thread(self, title: str):
        if self._pipeline_run is None:
            self.show_details_view()
        title_text = str(title or "Processing log")
        if self._pipeline_run is not None:
            existing = str(self._pipeline_run.get("step_log", "") or "")
            captured = title_text
            if existing.strip() and existing.strip() != title_text.strip():
                captured += "\n\n" + existing
            self._set_running_step_log(captured)
            self._append_pipeline_run_log(title_text)
            if self._selected_step_is_running_step():
                step = self._running_pipeline_step()
                step_name = str((step or {}).get("name", title_text))
                self.log_box.setTitle(f"Processing log: {step_name}")
                self.log_box.setVisible(True)
                self.log_edit.setPlainText(captured)
            return
        self.log_box.setTitle(title_text)
        self.log_box.setVisible(True)
        self.log_edit.clear()

    def _append_log_on_gui_thread(self, text: str):
        message = str(text)
        if self._pipeline_run is not None:
            existing = str(self._pipeline_run.get("step_log", "") or "")
            captured = existing + ("\n" if existing else "") + message
            self._set_running_step_log(captured)
            self._append_pipeline_run_log(message)
            if not self._selected_step_is_running_step():
                return
            self.log_box.setVisible(True)
            self.log_edit.setPlainText(captured)
        else:
            self.log_box.setVisible(True)
            self.log_edit.appendPlainText(message)
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _replacement_approval_keys(conflicts: list[dict]) -> set[str]:
        keys: set[str] = set()
        for conflict in conflicts or []:
            file_path = conflict.get("file_path", "")
            category = conflict.get("category", "")
            for name in conflict.get("names", []) or []:
                keys.add(_processing_replacement_key(file_path, category, name))
        return keys

    def _confirm_processing_replacements(
        self,
        conflicts: list[dict],
        *,
        pipeline_name: str = "",
    ) -> bool:
        if not conflicts:
            return True

        details = []
        for conflict in conflicts:
            file_path = str(conflict.get("file_path", "") or "")
            category = str(conflict.get("category", "") or "")
            steps = [str(x) for x in (conflict.get("steps", []) or []) if str(x)]
            details.append(file_path)
            if steps:
                details.append("  Pipeline step(s): " + ", ".join(steps))
            for name in conflict.get("names", []) or []:
                details.append(f"  processing/{category}/{name}")
            details.append("")

        unique_files = {
            _processing_replacement_key(conflict.get("file_path", ""), "", "")
            for conflict in conflicts
        }
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(
            "Confirm processing pipeline replacement"
            if pipeline_name
            else "Confirm processed-data replacement"
        )
        dialog.resize(760, 430)
        layout = QtWidgets.QVBoxLayout(dialog)
        if pipeline_name:
            summary_text = (
                f'Pipeline "{pipeline_name}" will replace processed data in '
                f"{len(unique_files)} file(s)."
            )
        else:
            summary_text = (
                f"Processed data will be replaced in {len(unique_files)} file(s)."
            )
        summary = QtWidgets.QLabel(
            summary_text
            + "\n\nEach affected NWB file will be exported to a temporary "
            "file, validated, and then replaced."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        affected_edit = QtWidgets.QPlainTextEdit(dialog)
        affected_edit.setReadOnly(True)
        affected_edit.setPlainText("\n".join(details).rstrip())
        affected_edit.setStyleSheet(
            "QPlainTextEdit { background: white; color: black; }"
        )
        layout.addWidget(affected_edit, stretch=1)

        standard_buttons = (
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons = QtWidgets.QDialogButtonBox(standard_buttons, parent=dialog)
        replace_button = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        replace_button.setText("Replace data")
        cancel_button = buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        cancel_button.setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted

    def _processing_conflicts_for_run(self, spec, params, applicable):
        transfer = Processing_data_transfer(
            self.GUI_instance,
            self,
            list(applicable or []),
            str((spec or {}).get("name", self._selected_name or "Processing")),
        )
        transfer.tool_params = dict(params or {})
        return processing_output_conflicts(
            list(applicable or []),
            transfer,
            (spec or {}).get("params", {}) or {},
            params,
        )

    def _applicable_paths_for_snapshot(self, snapshot: dict, spec: dict) -> list[str]:
        name = str((snapshot or {}).get("processing_name", "Processing"))
        params = dict((snapshot or {}).get("parameters", {}) or {})
        selected = list((snapshot or {}).get("selected_files", []) or [])
        replace_existing = bool((snapshot or {}).get("replace_existing_data", False))
        transfer = Processing_data_transfer(
            self.GUI_instance, self, selected, name
        )
        transfer.tool_params = params
        transfer.replace_existing_data = replace_existing
        spec_params = (spec or {}).get("params", {}) or {}
        match = spec_params.get("match", None)
        applicable = get_applicable_paths(
            transfer,
            tool_name=name,
            match=match if match is not None else (lambda _path: True),
            selected=selected,
        )
        return list(
            filter_applicable_by_content_index(applicable, transfer, spec_params)
        )

    def _pipeline_replacement_preflight(
        self,
        pipeline: dict,
        steps: list[dict],
        step_indices: list[int],
    ) -> Optional[set[str]]:
        """Confirm every current or intra-pipeline replacement before launch."""
        planned_outputs: set[str] = set()
        conflicts_by_file: dict[tuple[str, str], dict] = {}

        for step_index in step_indices:
            if step_index < 0 or step_index >= len(steps):
                continue
            step = steps[step_index]
            snapshot = dict(step.get("snapshot") or {})
            if bool(snapshot.get("project_action", False)):
                continue
            name = str(snapshot.get("processing_name", ""))
            category_name = str(snapshot.get("category", ""))
            spec = (self.registry.get(category_name, {}) or {}).get(name)
            if spec is None:
                _unused_category, spec = self._processing_registry_entry(name)
            if spec is None:
                continue

            params = dict(snapshot.get("parameters", {}) or {})
            applicable = self._applicable_paths_for_snapshot(snapshot, spec)
            spec_params = (spec or {}).get("params", {}) or {}
            output_names = _collect_internal_saving_names(spec_params, params)
            output_category = _processing_output_category(spec_params, params)
            replace_existing = bool(snapshot.get("replace_existing_data", False))

            current_conflict_keys: set[str] = set()
            if replace_existing:
                transfer = Processing_data_transfer(
                    self.GUI_instance, self, applicable, name
                )
                transfer.tool_params = params
                for conflict in processing_output_conflicts(
                    applicable, transfer, spec_params, params
                ):
                    for output_name in conflict.get("names", []) or []:
                        current_conflict_keys.add(
                            _processing_replacement_key(
                                conflict.get("file_path", ""),
                                conflict.get("category", output_category),
                                output_name,
                            )
                        )

            step_label = f"{step_index + 1}. {step.get('name', name or 'Step')}"
            for file_path in applicable:
                for output_name in output_names:
                    key = _processing_replacement_key(
                        file_path, output_category, output_name
                    )
                    is_conflict = replace_existing and (
                        key in current_conflict_keys or key in planned_outputs
                    )
                    if is_conflict:
                        group_key = (str(file_path), output_category)
                        entry = conflicts_by_file.setdefault(
                            group_key,
                            {
                                "file_path": str(file_path),
                                "category": output_category,
                                "names": [],
                                "steps": [],
                            },
                        )
                        if output_name not in entry["names"]:
                            entry["names"].append(output_name)
                        if step_label not in entry["steps"]:
                            entry["steps"].append(step_label)
                    planned_outputs.add(key)

        conflicts = list(conflicts_by_file.values())
        if conflicts and not self._confirm_processing_replacements(
            conflicts,
            pipeline_name=str(pipeline.get("name", "Pipeline")),
        ):
            return None
        return self._replacement_approval_keys(conflicts)

    def _run_selected(self, _from_pipeline: bool = False):
        if self._running or (self._pipeline_run is not None and not _from_pipeline):
            return False
        if _from_pipeline:
            name, spec = self._selected_name, self._selected_spec
        else:
            name, spec = self._get_selected_tool_spec()
            if not spec or not name:
                name, spec = self._selected_name, self._selected_spec
        if not spec or not name:
            message = "No processing is selected."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            return False
        fn = spec.get("func", None)
        if not callable(fn):
            message = f"No function attached for '{name}'."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            else:
                QtWidgets.QMessageBox.critical(self, "Processing", message)
            return False
        is_project_action = self._is_project_action(spec)
        if not is_project_action and not self.target_paths:
            message = "No files selected."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            else:
                QtWidgets.QMessageBox.information(self, "Processing", message)
            return False
        if not self._is_enabled(spec):
            message = f"'{name}' is disabled for the saved selection."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            else:
                QtWidgets.QMessageBox.information(self, "Processing", message)
            return False
        params = self._collect_and_validate_params()
        if params is None:
            message = self.params_error_lbl.text() or "The processing parameters are invalid."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            return False

        # A pipeline run must use the value saved with the step. Reading the
        # live checkbox here is unreliable because the right-side editor can be
        # rebuilt while the pipeline restores a step or the user browses other
        # files. Manual runs still use the visible checkbox.
        pipeline_snapshot = {}
        if _from_pipeline:
            running_step = self._running_pipeline_step()
            pipeline_snapshot = dict((running_step or {}).get("snapshot") or {})
        replace_existing = bool(
            pipeline_snapshot.get("replace_existing_data", False)
            if _from_pipeline
            else (
                not is_project_action
                and self.replace_existing_chk.isVisible()
                and self.replace_existing_chk.isChecked()
            )
        )

        if is_project_action:
            applicable = []
        else:
            applicable = self._compute_applicable_paths_for_tool(
                name,
                spec,
                params,
                replace_existing=replace_existing,
            )
            if not applicable:
                message = "No applicable files remain for the saved selection."
                if _from_pipeline:
                    self._complete_pipeline_step(False, message, "")
                else:
                    QtWidgets.QMessageBox.information(self, "Processing", message)
                return False
        approved_replacements: set[str] = set()
        if replace_existing:
            if _from_pipeline:
                approved_replacements = {
                    str(value)
                    for value in (
                        (self._pipeline_run or {}).get(
                            "approved_replacements", set()
                        )
                        or set()
                    )
                }
            else:
                conflicts = self._processing_conflicts_for_run(
                    spec, params, applicable
                )
                if conflicts and not self._confirm_processing_replacements(conflicts):
                    return False
                approved_replacements = self._replacement_approval_keys(conflicts)
        transfer = Processing_data_transfer(self.GUI_instance, self, applicable, name)
        transfer.tool_params = params
        transfer.tool_param_specs = dict(self.param_specs or {})
        transfer.debug_mode = bool(spec.get("debug_mode", False))
        transfer.replace_existing_data = replace_existing
        transfer.approved_replacements = approved_replacements
        self.show_run_log(str(spec.get("name", name)))
        self.post_log(f"Launching processing: {name}")
        if getattr(transfer, "debug_mode", False):
            self.post_log("Debug mode enabled: running processing on the GUI thread.")
        if applicable:
            self.post_log(f"Applicable files: {len(applicable)}")
        self._set_processing_running(True, name)
        self.run_btn.setEnabled(False)
        started = None
        try:
            if is_project_action:
                with _suppress_success_popups():
                    started = fn(transfer)
            else:
                started = fn(transfer)
        except Exception:
            full_error = traceback.format_exc()
            self.post_log("ERROR while launching processing:")
            self.post_log(full_error)
            self._set_processing_running(False)
            self.run_btn.setEnabled(True)
            if _from_pipeline:
                self._complete_pipeline_step(False, full_error, self.log_edit.toPlainText())
            return False

        if started is False or is_project_action:
            self._set_processing_running(False)

        if is_project_action:
            # Project-wide inventory scans write nwb_inventory.xlsx on disk.
            # Do not sync the pre-run transfer tables back to the app here,
            # because they can still be empty and would make the file tree look
            # empty until the user restarts. Reload the freshly written workbook
            # instead.
            try:
                if hasattr(self.GUI_instance, "reload_inventory"):
                    self.GUI_instance.reload_inventory(select_processing_if_empty=False)
                    self.post_log("Inventory reloaded from disk.")
            except TypeError:
                try:
                    self.GUI_instance.reload_inventory()
                    self.post_log("Inventory reloaded from disk.")
                except Exception:
                    self.post_log("Could not reload inventory after project action:")
                    self.post_log(traceback.format_exc())
            except Exception:
                self.post_log("Could not reload inventory after project action:")
                self.post_log(traceback.format_exc())
        else:
            # Background tools return immediately.  Do not sync the pre-run
            # transfer tables here; the runner emits processingFinishedRequest
            # when the worker actually ends, and the GUI reloads the inventory
            # from disk at that point.
            if started is False:
                self._sync_full_inventory_after_processing(transfer)
                if callable(self.on_run_done):
                    try:
                        self.on_run_done(name, self.target_paths)
                    except Exception:
                        pass

        if is_project_action and callable(self.on_run_done):
            try:
                self.on_run_done(name, self.target_paths)
            except Exception:
                pass

        if _from_pipeline and is_project_action:
            self._complete_pipeline_step(True, "", self.log_edit.toPlainText())
        elif _from_pipeline and started is False:
            self._complete_pipeline_step(True, "", self.log_edit.toPlainText())
        return True
