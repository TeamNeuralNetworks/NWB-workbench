from __future__ import annotations
from GUI.project_paths import json_dump as _pp_json_dump, json_load as _pp_json_load

import copy
import inspect
import json
import re
import shutil
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

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
from GUI.analysis_discovery import discover_analyses
from GUI.analysis_runner import (
    run_analysis,
    launch_plotting_gui,
    display_result_figures,
    close_plotting_gui,
)
from GUI.panels.preprocessed_data_manager import PreprocessedDataManager
from GUI.pipeline_store import PipelineStore, utc_now_text
from GUI.project_script_loader import discover_for_panel
from Tools.General.preprocessed_data_inventory import (
    build_preprocessed_deletion_plan,
    execute_preprocessed_deletion,
    find_stale_preprocessed_entries,
)

from Tools.General.event_alignment import normalize_alignment

DEFAULT_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


_MISSING_ALIGNMENT_VALUE = object()


def _alignment_mapping_value(mapping, canonical_key, legacy_key, *, negate_legacy=False):
    """Read one offset while accepting saved pre-offset pipeline snapshots."""
    mapping = mapping or {}
    if canonical_key in mapping:
        return mapping.get(canonical_key)
    if legacy_key not in mapping:
        return _MISSING_ALIGNMENT_VALUE

    value = mapping.get(legacy_key)
    if not negate_legacy:
        return value
    try:
        converted = -float(value)
        return 0.0 if converted == 0 else converted
    except (TypeError, ValueError):
        # Preserve invalid text so the normal numeric validation can explain it.
        return value


def _alignment_payload(event_marker, edge, start_offset, end_offset, event_path):
    """Return canonical offsets plus aliases required by older project files."""
    legacy_before = -float(start_offset)
    if legacy_before == 0:
        legacy_before = 0.0
    return {
        "event_marker": event_marker,
        "edge": edge,
        "start_offset_s": float(start_offset),
        "end_offset_s": float(end_offset),
        # Compatibility aliases. Older filter/cache modules convert the first
        # value back with ``start_offset = -pad_before_s``.
        "pad_before_s": legacy_before,
        "pad_after_s": float(end_offset),
        "event_path": event_path,
    }


def _script_metadata_from_spec(spec: Optional[dict]) -> dict:
    """Return a stable display name and absolute source path for an analysis."""
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


class _NoWheelComboBox(QtWidgets.QComboBox):
    """Combo box whose value is not changed by scrolling the mouse wheel."""

    def wheelEvent(self, event):
        # Leave the event unhandled so a parent scroll area can still use it.
        event.ignore()


def _full_inventory_df(app):
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


def _unit_project_path_segment(project: str) -> str:
    text = str(project or "").strip()
    text = re.sub(r'[\\/:*?"<>|]', "_", text).strip("._")
    return text or "unknown"


def _sorting_project_from_units_row(row) -> str:
    for key in ("sorting_project", "module"):
        try:
            value = row.get(key, "")
        except Exception:
            value = ""
        text = str(value or "").strip()
        if text and text.lower() != "nan":
            return text
    try:
        nwb_path = str(row.get("nwb_path", "") or "").strip().replace("\\", "/")
        if nwb_path.lower().startswith("units/"):
            text = nwb_path.split("/", 1)[1].strip()
            if text:
                return text
    except Exception:
        pass
    return ""


def _units_nwb_path_for_project(project: str, fallback="units") -> str:
    fallback = str(fallback or "units").strip().replace("\\", "/")
    if fallback.lower().startswith("units/"):
        return fallback
    project = str(project or "").strip()
    if project:
        return f"units/{_unit_project_path_segment(project)}"
    return "units"


def _run_date_text(value: Any) -> str:
    return str(value or "").replace("T", " ", 1)



class Analysis_data_transfer:
    def __init__(self, GUI_instance, AnalysisPanel, analysis_name: str):
        self.GUI_instance = GUI_instance
        self.backend = GUI_instance.backend

        self.NWB_inventory_path = GUI_instance.NWB_inventory_path
        self.NWB_inventory_df = _full_inventory_df(GUI_instance)
        self.NWB_content_df = _full_content_df(GUI_instance)

        self.AnalysisUI_instance = AnalysisPanel
        self.analysis_name = analysis_name

        self.selected_files: list[str] = []
        self.data_type = None
        self.data_path = None
        self.save_analysis = True
        self.save_metadata = True
        self.display_analysis_info = False
        self.analysis_save_name = ""
        self.analysis_output_dir = None
        self.condition_selection = {}
        self.debug_mode = False
        self.launch_plotting_GUI_after_analysis = False
        self.figure_per_layer = 4
        self.figure_row = 2

        self.tool_params: dict[str, Any] = {}
        self.tool_param_specs: dict[str, dict] = {}

        self.align_to_event = False
        self.event_alignment = {
            "event_marker": None,
            "edge": "start",
            "start_offset_s": 0.0,
            "end_offset_s": 0.0,
            "pad_before_s": 0.0,
            "pad_after_s": 0.0,
            "event_path": None,
        }

    def function_param_maker(self):
        return {
            "selected_files": self.selected_files,
            "analysis_name": self.analysis_name,
            "save_analysis": self.save_analysis,
            "analysis_save_name": self.analysis_save_name,
            "analysis_output_dir": self.analysis_output_dir,
            "align_to_event": self.align_to_event,
            "align_to_event_name": self.event_alignment["event_marker"],
            "event_alignment": self.event_alignment,
            "project_path": self.GUI_instance.project_root,
            "palette": {
                key: self.condition_selection[key].get("color")
                for key in self.condition_selection.keys()
                if key != "_conditions_name"
            },
        }


class _ConditionRow(QtWidgets.QFrame):
    changed = QtCore.Signal()
    set_current_requested = QtCore.Signal(object)
    select_requested = QtCore.Signal(object)
    move_up_requested = QtCore.Signal(object)
    move_down_requested = QtCore.Signal(object)
    remove_requested = QtCore.Signal(object)

    def __init__(self, idx: int, *, name="", color=None, file_path_list=None,
                 trial_filter_dict=None, file_filter_dict=None, parent=None):
        super().__init__(parent)
        self.file_path_list = list(file_path_list or [])
        self.trial_filter_dict = dict(trial_filter_dict or {})
        self.file_filter_dict = dict(file_filter_dict or {})
        self.color = color or DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
        self._selected = False
        self.setObjectName("conditionRow")
        try:
            self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        except AttributeError:  # PyQt5 compatibility
            self.setFrameShape(QtWidgets.QFrame.StyledPanel)

        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(3)
        layout.setColumnStretch(1, 1)

        self.name_lbl = QtWidgets.QLabel(f"Condition {idx + 1} name:")
        layout.addWidget(self.name_lbl, 0, 0)

        self.name_edit = QtWidgets.QLineEdit(str(name or ""))
        layout.addWidget(self.name_edit, 0, 1)

        layout.addWidget(QtWidgets.QLabel("Color:"), 0, 2)
        self.color_btn = QtWidgets.QPushButton(self.color)
        self.color_btn.setFixedWidth(92)
        self._update_color_button()
        layout.addWidget(self.color_btn, 0, 3)

        self.set_btn = QtWidgets.QPushButton("Set current filter")
        layout.addWidget(self.set_btn, 0, 4)
        self.select_btn = QtWidgets.QPushButton("Select current filter")
        layout.addWidget(self.select_btn, 0, 5)
        self.move_up_btn = QtWidgets.QPushButton("\u2191")
        self.move_up_btn.setFixedWidth(32)
        self.move_up_btn.setToolTip("Move condition up")
        layout.addWidget(self.move_up_btn, 0, 6)
        self.move_down_btn = QtWidgets.QPushButton("\u2193")
        self.move_down_btn.setFixedWidth(32)
        self.move_down_btn.setToolTip("Move condition down")
        layout.addWidget(self.move_down_btn, 0, 7)
        self.remove_btn = QtWidgets.QPushButton("Remove")
        layout.addWidget(self.remove_btn, 0, 8)

        self.info_lbl = QtWidgets.QLabel("")
        self.info_lbl.setStyleSheet("color: #555;")
        layout.addWidget(self.info_lbl, 1, 1, 1, 8)
        self._refresh_info()

        self.name_edit.textChanged.connect(self.changed.emit)
        self.color_btn.clicked.connect(self._choose_color)
        self.set_btn.clicked.connect(lambda: self.set_current_requested.emit(self))
        self.select_btn.clicked.connect(lambda: self.select_requested.emit(self))
        self.move_up_btn.clicked.connect(lambda: self.move_up_requested.emit(self))
        self.move_down_btn.clicked.connect(lambda: self.move_down_requested.emit(self))
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))
        self._update_selection_highlight()

    def set_selected(self, selected: bool):
        selected = bool(selected)
        if self._selected == selected:
            return
        self._selected = selected
        self._update_selection_highlight()

    def _update_selection_highlight(self):
        palette = self.palette()
        window_role = (
            QtGui.QPalette.ColorRole.Window
            if hasattr(QtGui.QPalette, "ColorRole")
            else QtGui.QPalette.Window
        )

        if self._selected:
            palette.setColor(window_role, QtGui.QColor("#dcecff"))
            self.setPalette(palette)
            self.setAutoFillBackground(True)
            self.setStyleSheet(
                "QFrame#conditionRow {"
                " background-color: #dcecff;"
                " border: 2px solid #2f80ed;"
                " border-radius: 5px;"
                "}"
            )
            if hasattr(self, "select_btn"):
                self.select_btn.setText("Selected")
                self.select_btn.setStyleSheet(
                    "QPushButton {"
                    " background-color: #2f80ed;"
                    " color: white;"
                    " font-weight: bold;"
                    " border: 1px solid #1f66c1;"
                    " border-radius: 3px;"
                    " padding: 4px 8px;"
                    "}"
                )
        else:
            self.setAutoFillBackground(False)
            self.setStyleSheet(
                "QFrame#conditionRow {"
                " background-color: transparent;"
                " border: 1px solid transparent;"
                " border-radius: 5px;"
                "}"
            )
            if hasattr(self, "select_btn"):
                self.select_btn.setText("Select current filter")
                self.select_btn.setStyleSheet("")

        self.update()

    def _update_color_button(self):
        self.color_btn.setText(str(self.color))
        self.color_btn.setStyleSheet(f"background-color: {self.color};")

    def _choose_color(self):
        qcol = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.color), self, "Choose condition color")
        if qcol.isValid():
            self.color = qcol.name()
            self._update_color_button()
            self.changed.emit()

    def _refresh_info(self):
        if self.file_path_list or self.trial_filter_dict or self.file_filter_dict:
            self.info_lbl.setText(
                f"Saved: {len(self.file_path_list)} file(s), "
                f"{len(self.trial_filter_dict)} trial filter(s), "
                f"{len(self.file_filter_dict)} file filter(s)"
            )
        else:
            self.info_lbl.setText("No filter saved")

    def set_current_state(self, *, files, trial_filter, file_filter):
        self.file_path_list = copy.deepcopy(list(files or []))
        self.trial_filter_dict = copy.deepcopy(dict(trial_filter or {}))
        self.file_filter_dict = copy.deepcopy(dict(file_filter or {}))
        self._refresh_info()
        self.changed.emit()

    def to_block(self):
        return {
            "color": str(self.color),
            "file_path_list": copy.deepcopy(self.file_path_list),
            "trial_filter_dict": copy.deepcopy(self.trial_filter_dict or {}),
            "file_filter_dict": copy.deepcopy(self.file_filter_dict or {}),
        }

    def condition_name(self):
        return str(self.name_edit.text()).strip()


class QtConditionSection(QtWidgets.QGroupBox):
    changed = QtCore.Signal()

    def __init__(self, *, GUI_instance, get_selected_files, parent=None):
        super().__init__("Conditions", parent)
        self.GUI_instance = GUI_instance
        self.get_selected_files = get_selected_files
        self.project_root = Path(getattr(GUI_instance, "project_root", Path.cwd()))
        self.filters_dir = self.project_root / "Filters"
        self.rows: list[_ConditionRow] = []
        self.selected_row: Optional[_ConditionRow] = None

        main = QtWidgets.QVBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(6)

        top = QtWidgets.QHBoxLayout()
        main.addLayout(top)
        top.addWidget(QtWidgets.QLabel("Conditions name:"))
        self.name_combo = _NoWheelComboBox()
        self.name_combo.setEditable(True)
        self.name_combo.setMinimumWidth(220)
        top.addWidget(self.name_combo)
        self.save_btn = QtWidgets.QPushButton("Save conditions")
        top.addWidget(self.save_btn)
        self.delete_filter_btn = QtWidgets.QPushButton("Delete filter")
        self.delete_filter_btn.setToolTip("Delete the currently selected saved filter")
        top.addWidget(self.delete_filter_btn)
        top.addStretch(1)

        self.rows_widget = QtWidgets.QWidget(self)
        self.rows_layout = QtWidgets.QVBoxLayout(self.rows_widget)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(4)
        main.addWidget(self.rows_widget)

        bottom = QtWidgets.QHBoxLayout()
        main.addLayout(bottom)
        self.add_btn = QtWidgets.QPushButton("Add condition")
        bottom.addWidget(self.add_btn)
        bottom.addStretch(1)

        self.add_btn.clicked.connect(self.add_condition)
        self.save_btn.clicked.connect(self.save_conditions_to_file)
        self.delete_filter_btn.clicked.connect(self.delete_saved_filter)
        self.name_combo.activated.connect(self._on_condition_name_selected)

        self.refresh_conditions_name_list()
        self.add_condition()

    def _ensure_filters_dir(self):
        self.filters_dir.mkdir(parents=True, exist_ok=True)
        return self.filters_dir

    def refresh_conditions_name_list(self):
        current = self.name_combo.currentText()
        self.name_combo.blockSignals(True)
        self.name_combo.clear()
        self.name_combo.addItem("")
        if self.filters_dir.exists():
            for p in sorted(self.filters_dir.glob("*.json"), key=lambda x: x.stem.lower()):
                self.name_combo.addItem(p.stem)
        self.name_combo.setCurrentText(current)
        self.name_combo.blockSignals(False)

    @staticmethod
    def _normalize_bool_strings(value):
        if isinstance(value, str):
            v = value.strip()
            if v == "True":
                return True
            if v == "False":
                return False
            return value
        if isinstance(value, dict):
            return {k: QtConditionSection._normalize_bool_strings(v) for k, v in value.items()}
        if isinstance(value, list):
            return [QtConditionSection._normalize_bool_strings(v) for v in value]
        return value

    def _reindex(self):
        for i, row in enumerate(self.rows):
            row.name_lbl.setText(f"Condition {i + 1} name:")
            row.move_up_btn.setEnabled(i > 0)
            row.move_down_btn.setEnabled(i < len(self.rows) - 1)

    def add_condition(self, *, name="", color=None, file_path_list=None, trial_filter_dict=None, file_filter_dict=None):
        row = _ConditionRow(
            len(self.rows),
            name=name,
            color=color,
            file_path_list=file_path_list,
            trial_filter_dict=trial_filter_dict,
            file_filter_dict=file_filter_dict,
            parent=self.rows_widget,
        )
        row.changed.connect(self.changed.emit)
        row.set_current_requested.connect(self._set_current_filter_for_row)
        row.select_requested.connect(self._select_filter_from_row)
        row.move_up_requested.connect(lambda current_row: self._move_row(current_row, -1))
        row.move_down_requested.connect(lambda current_row: self._move_row(current_row, 1))
        row.remove_requested.connect(self._remove_row)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        self._reindex()
        self.changed.emit()

    def _move_row(self, row, offset):
        if row not in self.rows:
            return

        current_index = self.rows.index(row)
        new_index = current_index + int(offset)
        if new_index < 0 or new_index >= len(self.rows):
            return

        self.rows.pop(current_index)
        self.rows.insert(new_index, row)
        self.rows_layout.removeWidget(row)
        self.rows_layout.insertWidget(new_index, row)
        self._reindex()
        self.changed.emit()

    def _remove_row(self, row):
        if row not in self.rows:
            return
        if row is self.selected_row:
            self._set_selected_row(None)
        if len(self.rows) <= 1:
            row.name_edit.setText("")
            row.color = DEFAULT_COLORS[0]
            row._update_color_button()
            row.file_path_list = []
            row.trial_filter_dict = {}
            row.file_filter_dict = {}
            row._refresh_info()
            self.changed.emit()
            return
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._reindex()
        self.changed.emit()

    def _set_current_filter_for_row(self, row):
        files = list(self.get_selected_files() or [])
        if not files:
            QtWidgets.QMessageBox.warning(self, "Conditions", "Please select files first.")
            return
        fp = getattr(self.GUI_instance, "filter_panel", None)
        trial_filter = fp.get_trial_filters() if fp is not None and hasattr(fp, "get_trial_filters") else None
        file_filter = fp.get_file_filters() if fp is not None and hasattr(fp, "get_file_filters") else {}
        self._activate_row(row)
        row.set_current_state(files=files, trial_filter=trial_filter or {}, file_filter=file_filter or {})

    def _select_filter_from_row(self, row):
        self._activate_row(row)
        fp = getattr(self.GUI_instance, "filter_panel", None)
        if fp is not None and hasattr(fp, "load_filter_dicts"):
            fp.load_filter_dicts(
                trial_filter_dict=dict(row.trial_filter_dict or {}),
                file_filter_dict=dict(row.file_filter_dict or {}),
                apply=True,
            )
        self._select_condition_files(row.file_path_list)
        self.changed.emit()

    @staticmethod
    def _path_key(path):
        return str(path or "").strip().replace("\\", "/").rstrip("/").casefold()

    def _select_condition_files(self, file_path_list):
        """Select a condition's saved files after its filters rebuild the tree."""
        file_panel = getattr(self.GUI_instance, "file_tree_panel", None)
        tree = getattr(file_panel, "tree", None)
        node_map = getattr(file_panel, "node_map", None)
        if tree is None or not isinstance(node_map, dict):
            return

        wanted = {
            self._path_key(path)
            for path in list(file_path_list or [])
            if self._path_key(path)
        }
        matching_items = [
            item
            for path, item in node_map.items()
            if self._path_key(path) in wanted
        ]

        signals_were_blocked = tree.blockSignals(True)
        try:
            tree.clearSelection()
            for item in matching_items:
                item.setSelected(True)
        finally:
            tree.blockSignals(signals_were_blocked)

        if matching_items:
            try:
                tree.scrollToItem(matching_items[0])
            except Exception:
                pass

        on_select = getattr(self.GUI_instance, "on_select", None)
        if callable(on_select):
            on_select()

    def _activate_row(self, row):
        """Make a condition the active selection without changing its filters."""
        self._set_selected_row(row)

    def _set_selected_row(self, selected_row):
        self.selected_row = selected_row if selected_row in self.rows else None
        for row in self.rows:
            row.set_selected(row is self.selected_row)

    def get_conditions_dict(self):
        out = {}
        cond_name = str(self.name_combo.currentText()).strip()
        if cond_name:
            out["_conditions_name"] = cond_name
        for i, row in enumerate(self.rows):
            name = row.condition_name() or f"condition_{i + 1}"
            base = name
            suffix = 2
            while name in out:
                name = f"{base}_{suffix}"
                suffix += 1
            out[name] = row.to_block()
            out[name]["trial_filter_dict"] = self._normalize_bool_strings(out[name].get("trial_filter_dict", {}))
            out[name]["file_filter_dict"] = self._normalize_bool_strings(out[name].get("file_filter_dict", {}))
        return out

    def has_valid_conditions(self):
        if not self.rows:
            return False
        for row in self.rows:
            if not row.condition_name():
                return False
            if not list(row.file_path_list or []):
                return False
        return True

    def save_conditions_to_file(self):
        name = str(self.name_combo.currentText()).strip()
        if not name:
            QtWidgets.QMessageBox.warning(self, "Save conditions", "Please enter a conditions name first.")
            return
        path = self._ensure_filters_dir() / f"{name}.json"
        if path.exists():
            answer = QtWidgets.QMessageBox.question(
                self,
                "Overwrite saved filter?",
                f'A saved filter named "{name}" already exists.\n\n'
                "Do you want to overwrite the previously saved filter?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        try:
            with open(path, "w", encoding="utf-8") as f:
                _pp_json_dump(self.get_conditions_dict(), f, indent=2, ensure_ascii=False)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Save conditions", f"Could not save conditions:\n{exc}")
            return
        self.refresh_conditions_name_list()
        self.name_combo.setCurrentText(name)
        QtWidgets.QMessageBox.information(self, "Save conditions", f"Conditions saved to:\n{path}")

    def delete_saved_filter(self):
        name = str(self.name_combo.currentText()).strip()
        if not name:
            QtWidgets.QMessageBox.warning(
                self,
                "Delete filter",
                "Please select a saved filter first.",
            )
            return

        saved_names = {
            str(self.name_combo.itemText(i)).strip()
            for i in range(self.name_combo.count())
            if str(self.name_combo.itemText(i)).strip()
        }
        if name not in saved_names:
            QtWidgets.QMessageBox.warning(
                self,
                "Delete filter",
                f'No saved filter named "{name}" exists.',
            )
            return

        path = self.filters_dir / f"{name}.json"
        if not path.is_file():
            self.refresh_conditions_name_list()
            QtWidgets.QMessageBox.warning(
                self,
                "Delete filter",
                f'The saved filter "{name}" could not be found.',
            )
            return

        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete saved filter?",
            f'Are you sure you want to delete the saved filter "{name}"?\n\n'
            "This cannot be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        try:
            path.unlink()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Delete filter",
                f"Could not delete the saved filter:\n{exc}",
            )
            return

        self.name_combo.setCurrentText("")
        self.refresh_conditions_name_list()
        self.name_combo.setCurrentIndex(0)
        self.changed.emit()
        QtWidgets.QMessageBox.information(
            self,
            "Delete filter",
            f'The saved filter "{name}" was deleted.',
        )

    def _on_condition_name_selected(self):
        name = str(self.name_combo.currentText()).strip()
        if not name:
            return
        path = self.filters_dir / f"{name}.json"
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _pp_json_load(f)
            self.load_conditions_dict(data)
            self.name_combo.setCurrentText(name)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Load conditions", f"Could not load conditions:\n{exc}")

    def load_conditions_dict(self, data: dict):
        self._set_selected_row(None)
        for row in list(self.rows):
            self.rows_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.rows = []
        self.name_combo.setCurrentText(str(data.get("_conditions_name", "") or ""))
        for name, block in data.items():
            if name == "_conditions_name":
                continue
            if not isinstance(block, dict):
                block = {}
            self.add_condition(
                name=name,
                color=str(block.get("color", "") or "") or None,
                file_path_list=list(block.get("file_path_list", []) or []),
                trial_filter_dict=dict(block.get("trial_filter_dict", {}) or {}),
                file_filter_dict=dict(block.get("file_filter_dict", {}) or {}),
            )
        if not self.rows:
            self.add_condition()
        self._reindex()
        self.changed.emit()


class AnalysisPanel(QtWidgets.QWidget):
    log_message = QtCore.Signal(str)
    log_reset = QtCore.Signal(str)
    analysis_finished = QtCore.Signal(object)
    analysis_failed = QtCore.Signal(str)
    progress_update = QtCore.Signal(object)
    runningStateChanged = QtCore.Signal(bool)

    def __init__(self, parent=None, *, right_panel, GUI_instance, on_run_done=None):
        super().__init__(parent)
        self.right_panel = right_panel
        self.GUI_instance = GUI_instance
        self.on_run_done = on_run_done

        self.registry: dict[str, dict[str, dict]] = {}
        self.target_paths: list[str] = []
        self.selected_files: list[str] = []
        self.available_files: list[str] = []
        self._selected_name: Optional[str] = None
        self._selected_spec: Optional[dict] = None
        self._item_to_ref: dict[int, tuple[str, str]] = {}

        self._data_payload_by_label: dict[str, dict] = {}
        self._event_marker_payload_by_label: dict[str, dict] = {}
        self._available_data_choices: list[str] = []
        self._data_row_combos: list[QtWidgets.QComboBox] = []
        self._allow_multiple_data = False

        self.param_widgets: dict[str, Any] = {}
        self.param_specs: dict[str, dict] = {}
        self._ts_display_to_full_by_param: Dict[str, Dict[str, str]] = {}
        self._list_display_to_value_by_param: Dict[str, Dict[str, Any]] = {}

        self._running = False
        self._running_analysis_name = ""
        self._last_result = None
        self.pipeline_store = PipelineStore(
            getattr(self.GUI_instance, "project_root", "."),
            "analysis",
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
        self._pipeline_results: dict[str, Any] = {}

        self._availability_timer = QtCore.QTimer(self)
        self._availability_timer.setSingleShot(True)
        self._availability_timer.setInterval(200)
        self._availability_timer.timeout.connect(self._refresh_availability_display)

        self._build_middle_ui()
        self._build_right_ui()

        self.log_reset.connect(self._reset_log_on_gui_thread)
        self.log_message.connect(self._append_log_on_gui_thread)
        self.analysis_finished.connect(self._on_analysis_finished)
        self.analysis_failed.connect(self._on_analysis_failed)
        self.progress_update.connect(self._update_progress_on_gui_thread)

        self.set_registry(self._discover_analyses())

    # ---------------- UI ----------------

    def _build_middle_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header_row = QtWidgets.QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel("Analysis")
        title.setStyleSheet("font-weight: bold;")
        header_row.addWidget(title)
        header_row.addStretch(1)
        self.preprocessed_data_btn = QtWidgets.QPushButton(
            "Preprocessed data manager",
            self,
        )
        self.preprocessed_data_btn.setToolTip(
            "Open the preprocessed-data manager in a separate window."
        )
        self.preprocessed_data_btn.clicked.connect(
            self._show_preprocessed_data_manager
        )
        header_row.addWidget(self.preprocessed_data_btn)
        layout.addLayout(header_row)

        self.jobs_label = QtWidgets.QLabel("", self)
        self.jobs_label.setVisible(False)
        self.jobs_label.setWordWrap(True)
        self.jobs_label.setStyleSheet(
            "QLabel { background: #f6faf7; color: #2e7d32; "
            "border: 1px solid #cfe8d2; border-radius: 4px; padding: 3px 5px; }"
        )
        layout.addWidget(self.jobs_label)

        self.analysis_tree = QtWidgets.QTreeWidget(self)
        self.analysis_tree.setHeaderHidden(True)
        self.analysis_tree.itemSelectionChanged.connect(self._on_select)
        self.analysis_tree.itemExpanded.connect(self._on_workspace_item_expanded)
        self.analysis_tree.itemCollapsed.connect(self._on_workspace_item_collapsed)
        layout.addWidget(self.analysis_tree, stretch=1)

    def _build_right_ui(self):
        cont = self.right_panel.analysis_container
        lay = self.right_panel.analysis_container_layout
        self.right_panel.clear_layout(lay)

        self.details_widget = QtWidgets.QWidget(cont)
        lay.addWidget(self.details_widget)
        outer = QtWidgets.QVBoxLayout(self.details_widget)
        outer.setContentsMargins(0, 0, 0, 0)

        self.scroll = QtWidgets.QScrollArea(self.details_widget)
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll, stretch=1)

        self.body = QtWidgets.QWidget(self.scroll)
        self.scroll.setWidget(self.body)
        main = QtWidgets.QVBoxLayout(self.body)
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(8)

        self.title_lbl = QtWidgets.QLabel("")
        self.title_lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
        main.addWidget(self.title_lbl)

        self.applies_lbl = QtWidgets.QLabel("Applies to selected files that match analysis criteria")
        self.applies_lbl.setStyleSheet("color: #444;")
        main.addWidget(self.applies_lbl)

        self.available_lbl = QtWidgets.QLabel("No analysis selected")
        self.available_lbl.setStyleSheet("color: #444;")
        main.addWidget(self.available_lbl)

        desc_title = QtWidgets.QLabel("Description:")
        desc_title.setStyleSheet("font-weight: bold;")
        main.addWidget(desc_title)
        self.desc_lbl = QtWidgets.QLabel("")
        self.desc_lbl.setWordWrap(True)
        main.addWidget(self.desc_lbl)

        self.condition_section = QtConditionSection(
            GUI_instance=self.GUI_instance,
            get_selected_files=self._get_selected_files,
            parent=self.body,
        )
        self.condition_section.changed.connect(self.refresh_from_filters)
        main.addWidget(self.condition_section)

        self.data_box = QtWidgets.QGroupBox("Input data")
        self.data_layout = QtWidgets.QVBoxLayout(self.data_box)
        self.data_rows_layout = QtWidgets.QVBoxLayout()
        self.data_layout.addLayout(self.data_rows_layout)
        self.add_data_btn = QtWidgets.QPushButton("Add data")
        self.add_data_btn.clicked.connect(lambda: self._add_data_row())
        self.data_layout.addWidget(self.add_data_btn, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)
        main.addWidget(self.data_box)

        self.event_box = QtWidgets.QGroupBox("Event alignment")
        event_layout = QtWidgets.QGridLayout(self.event_box)
        event_layout.addWidget(QtWidgets.QLabel("Start"), 0, 1)
        event_layout.addWidget(QtWidgets.QLabel("End"), 0, 2)
        self.event_marker_combo = _NoWheelComboBox()
        self.end_event_marker_combo = _NoWheelComboBox()
        self.event_edge_combo = _NoWheelComboBox()
        self.end_event_edge_combo = _NoWheelComboBox()
        self.start_offset_edit = QtWidgets.QLineEdit("")
        self.end_offset_edit = QtWidgets.QLineEdit("")
        for row, (label, left, right) in enumerate([
            ("Event marker", self.event_marker_combo, self.end_event_marker_combo),
            ("Edge", self.event_edge_combo, self.end_event_edge_combo),
            ("Offset (s)", self.start_offset_edit, self.end_offset_edit),
        ], 1):
            event_layout.addWidget(QtWidgets.QLabel(label), row, 0)
            event_layout.addWidget(left, row, 1)
            event_layout.addWidget(right, row, 2)
        event_layout.setColumnStretch(1, 1)
        event_layout.setColumnStretch(2, 1)
        for combo in (self.event_marker_combo, self.end_event_marker_combo):
            combo.setMinimumWidth(0)
            combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(6)
            combo.currentTextChanged.connect(self._on_event_marker_change)
        for combo in (self.event_edge_combo, self.end_event_edge_combo):
            combo.addItems(["start", "stop"])
            combo.currentTextChanged.connect(self._queue_availability_refresh)
        self.start_offset_edit.setPlaceholderText("e.g. -0.5")
        self.end_offset_edit.setPlaceholderText("e.g. 1")
        for edit in (self.start_offset_edit, self.end_offset_edit):
            edit.setToolTip("Signed offset from this column's event edge, in seconds.")
            edit.textChanged.connect(self._queue_availability_refresh)
        main.addWidget(self.event_box)

        self.params_box = QtWidgets.QGroupBox("Parameters")
        self.params_layout = QtWidgets.QVBoxLayout(self.params_box)
        main.addWidget(self.params_box)

        self.error_lbl = QtWidgets.QLabel("")
        self.error_lbl.setStyleSheet("color: #b00020;")
        self.error_lbl.setWordWrap(True)
        main.addWidget(self.error_lbl)

        run_row = QtWidgets.QHBoxLayout()
        main.addLayout(run_row)
        self.run_btn = QtWidgets.QPushButton("Run")
        self.run_btn.clicked.connect(self._run_button_clicked)
        run_row.addWidget(self.run_btn)
        self.add_pipeline_btn = QtWidgets.QPushButton("Add to pipeline")
        self.add_pipeline_btn.clicked.connect(self._add_current_to_pipeline)
        run_row.addWidget(self.add_pipeline_btn)
        run_row.addStretch(1)

        save_row = QtWidgets.QHBoxLayout()
        main.addLayout(save_row)
        self.save_analysis_chk = QtWidgets.QCheckBox("Save analysis")
        self.save_analysis_chk.setChecked(False)
        self.save_analysis_chk.stateChanged.connect(self._queue_availability_refresh)
        save_row.addWidget(self.save_analysis_chk)
        save_row.addWidget(QtWidgets.QLabel("Folder name"))
        self.analysis_name_edit = QtWidgets.QLineEdit("")
        self.analysis_name_edit.textChanged.connect(self._queue_availability_refresh)
        save_row.addWidget(self.analysis_name_edit)

        self.analysis_plot_options_widget = QtWidgets.QWidget(self.details_widget)
        plot_row = QtWidgets.QHBoxLayout(self.analysis_plot_options_widget)
        plot_row.setContentsMargins(0, 0, 0, 0)
        main.addWidget(self.analysis_plot_options_widget)
        plot_row.addWidget(QtWidgets.QLabel("Number of figures per layer"))
        self.figure_per_layer_spin = QtWidgets.QSpinBox()
        self.figure_per_layer_spin.setRange(1, 200)
        self.figure_per_layer_spin.setValue(4)
        plot_row.addWidget(self.figure_per_layer_spin)
        plot_row.addWidget(QtWidgets.QLabel("Number of rows"))
        self.figure_row_spin = QtWidgets.QSpinBox()
        self.figure_row_spin.setRange(1, 50)
        self.figure_row_spin.setValue(2)
        plot_row.addWidget(self.figure_row_spin)
        self.auto_plot_chk = QtWidgets.QCheckBox("Auto launch plotting GUI")
        plot_row.addWidget(self.auto_plot_chk)
        self.plot_btn = QtWidgets.QPushButton("Plot GUI")
        self.plot_btn.clicked.connect(self._open_plotting_gui)
        plot_row.addWidget(self.plot_btn)
        plot_row.addStretch(1)

        log_title = QtWidgets.QLabel("Analysis log")
        log_title.setStyleSheet("font-weight: bold;")
        main.addWidget(log_title)

        self.progress_status_lbl = QtWidgets.QLabel("Idle")
        self.progress_status_lbl.setStyleSheet("color: #444;")
        self.progress_status_lbl.setWordWrap(True)
        main.addWidget(self.progress_status_lbl)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(180)
        self.log_edit.setStyleSheet("QPlainTextEdit { background: white; color: black; }")
        main.addWidget(self.log_edit)

        main.addStretch(1)
        self._build_pipeline_view(cont, lay)

        # Keep the project-level cache manager outside the shared right panel.
        # QDialog.show() is non-modal, so the user can continue browsing and
        # editing the main GUI while this window remains open.
        self.preprocessed_data_window = QtWidgets.QDialog(self.window())
        self.preprocessed_data_window.setWindowTitle("Preprocessed data manager")
        self.preprocessed_data_window.setModal(False)
        self.preprocessed_data_window.setMinimumSize(760, 420)
        self.preprocessed_data_window.resize(1100, 650)
        preprocessed_window_layout = QtWidgets.QVBoxLayout(
            self.preprocessed_data_window
        )
        preprocessed_window_layout.setContentsMargins(8, 8, 8, 8)
        self.preprocessed_data_manager = PreprocessedDataManager(
            project_root=getattr(self.GUI_instance, "project_root", Path.cwd()),
            is_busy=lambda: bool(self._running or self._pipeline_run is not None),
            parent=self.preprocessed_data_window,
        )
        preprocessed_window_layout.addWidget(self.preprocessed_data_manager)
        self.runningStateChanged.connect(
            lambda _running: self.preprocessed_data_manager.refresh_busy_state()
        )
        self._clear_details()

    # ---------------- pipelines ----------------

    @staticmethod
    def _align_left():
        try:
            return QtCore.Qt.AlignmentFlag.AlignLeft
        except AttributeError:
            return QtCore.Qt.AlignLeft

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
    def _analysis_step_summary(snapshot):
        snapshot = snapshot or {}
        data_names = []
        for entry in snapshot.get("input_data", []) or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("analysis_data_name", entry.get("name", entry.get("label", ""))) or "")
            if name and name not in data_names:
                data_names.append(name)
        data_text = ", ".join(data_names) if data_names else "No input data"
        conditions = snapshot.get("conditions", {}) or {}
        condition_count = len([name for name in conditions if name != "_conditions_name"])
        file_count = len(snapshot.get("selected_files", []) or [])
        return f"{data_text} | {condition_count} condition(s) | {file_count} file(s)"

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

        # Pipeline-level overview. Navigation between pipelines and steps lives
        # exclusively in the middle tree, so the right panel is no longer
        # duplicated by another combo box and another step list.
        self.pipeline_overview_page = QtWidgets.QWidget(self.pipeline_detail_stack)
        overview_layout = QtWidgets.QVBoxLayout(self.pipeline_overview_page)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.setSpacing(8)

        overview_header = QtWidgets.QHBoxLayout()
        overview_heading = QtWidgets.QVBoxLayout()
        self.pipeline_overview_title_lbl = QtWidgets.QLabel("Analysis pipelines")
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
            ["Run", "#", "Step", "Script", "Run date", "Status", "Output"]
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
        self.launch_pipeline_btn.clicked.connect(self._launch_pipeline_button_clicked)
        launch_row.addWidget(self.launch_pipeline_btn)
        launch_row.addStretch(1)
        overview_layout.addLayout(launch_row)

        pipeline_plot_box = QtWidgets.QGroupBox("Plot layout", self.pipeline_overview_page)
        pipeline_plot_layout = QtWidgets.QGridLayout(pipeline_plot_box)
        pipeline_plot_layout.addWidget(QtWidgets.QLabel("Figures per layout"), 0, 0)
        self.pipeline_figure_per_layer_spin = QtWidgets.QSpinBox(pipeline_plot_box)
        self.pipeline_figure_per_layer_spin.setRange(1, 200)
        self.pipeline_figure_per_layer_spin.setValue(4)
        self.pipeline_figure_per_layer_spin.valueChanged.connect(
            self._save_pipeline_plot_options
        )
        pipeline_plot_layout.addWidget(self.pipeline_figure_per_layer_spin, 0, 1)
        pipeline_plot_layout.addWidget(QtWidgets.QLabel("Number of rows"), 0, 2)
        self.pipeline_figure_row_spin = QtWidgets.QSpinBox(pipeline_plot_box)
        self.pipeline_figure_row_spin.setRange(1, 50)
        self.pipeline_figure_row_spin.setValue(2)
        self.pipeline_figure_row_spin.valueChanged.connect(
            self._save_pipeline_plot_options
        )
        pipeline_plot_layout.addWidget(self.pipeline_figure_row_spin, 0, 3)
        pipeline_plot_layout.setColumnStretch(4, 1)

        self.pipeline_auto_plot_chk = QtWidgets.QCheckBox(
            "Auto launch Plot GUI after pipeline", pipeline_plot_box
        )
        self.pipeline_auto_plot_chk.toggled.connect(self._save_pipeline_plot_options)
        pipeline_plot_layout.addWidget(self.pipeline_auto_plot_chk, 1, 0, 1, 4)
        self.pipeline_plot_btn = QtWidgets.QPushButton("Plot GUI")
        self.pipeline_plot_btn.clicked.connect(self._open_pipeline_plotting_gui)
        pipeline_plot_layout.addWidget(self.pipeline_plot_btn, 2, 0)
        overview_layout.addWidget(pipeline_plot_box)

        pipeline_log_box = QtWidgets.QGroupBox("Analysis log", self.pipeline_overview_page)
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

        # Step-level view. It embeds the normal Analysis editor, matching the
        # manual configuration view shown elsewhere in the application.
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

        # The selected step uses the real Analysis editor.  Keep pipeline
        # actions outside the stacked pages so they remain available while the
        # saved configuration is being viewed or edited.
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
    def _spec_requires_event_alignment(spec: Optional[dict]) -> bool:
        raw = (spec or {}).get("align_to_event_marker", False)
        if isinstance(raw, dict):
            return bool(raw.get("required", True))
        return bool(raw)

    @staticmethod
    def _cache_data_names(input_data: list[dict]) -> list[str]:
        names = []
        seen = set()
        for payload in input_data or []:
            name = str(
                payload.get("analysis_data_name", payload.get("name", ""))
            ).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def _cache_request_from_snapshot(self, snapshot: dict, context: str) -> Optional[dict]:
        snapshot = snapshot or {}
        analysis_name = str(snapshot.get("analysis_name", "") or "")
        category = str(snapshot.get("category", "") or "")
        spec = (self.registry.get(category, {}) or {}).get(analysis_name)
        if spec is None:
            _category, spec = self._analysis_registry_entry(analysis_name)
        if spec is None:
            return None
        return {
            "context": str(context or analysis_name or "Analysis"),
            "condition_filter": copy.deepcopy(snapshot.get("conditions", {}) or {}),
            "data_types": self._cache_data_names(snapshot.get("input_data", []) or []),
            "align_to_events": self._spec_requires_event_alignment(spec),
            "event_alignment": copy.deepcopy(snapshot.get("event_alignment", {}) or {}),
        }

    def _confirm_stale_preprocessed_data(self, requests: list[dict]) -> bool:
        """Warn and remove cache entries whose saved condition has changed."""
        project_root = getattr(self.GUI_instance, "project_root", Path.cwd())
        conflicts_by_row = {}
        try:
            for request in requests or []:
                entries = find_stale_preprocessed_entries(
                    project_root,
                    condition_filter=request.get("condition_filter", {}) or {},
                    data_types=request.get("data_types", []) or [],
                    align_to_events=bool(request.get("align_to_events", False)),
                    event_marker_to_align_dict=request.get("event_alignment", {}) or {},
                )
                for entry in entries:
                    record = conflicts_by_row.setdefault(
                        int(entry.row_index),
                        {"entry": entry, "contexts": []},
                    )
                    context = str(request.get("context", "Analysis") or "Analysis")
                    if context not in record["contexts"]:
                        record["contexts"].append(context)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Preprocessed data check failed",
                "The preprocessed-data inventory could not be checked. "
                f"The analysis was not launched.\n\n{exc}",
            )
            return False

        if not conflicts_by_row:
            return True

        details = []
        for record in conflicts_by_row.values():
            entry = record["entry"]
            contexts = ", ".join(record["contexts"])
            alignment_value = str(entry.aligned_to_event).strip()
            if alignment_value.casefold() in {
                "", "0", "false", "none", "nan", "<na>"
            }:
                alignment = "disabled"
            else:
                try:
                    start_offset = -float(entry.pad_before_s)
                    if start_offset == 0:
                        start_offset = 0.0
                except (TypeError, ValueError):
                    start_offset = entry.pad_before_s
                alignment = (
                    f"{entry.aligned_to_event} {entry.edge}, window "
                    f"[{start_offset}, {entry.pad_after_s}] s"
                )
            details.extend(
                [
                    f"Analysis/step: {contexts}",
                    f"Condition set: {entry.filter_name}",
                    f"Condition: {entry.condition}",
                    f"Data: {entry.data_type}",
                    f"Alignment: {alignment}",
                    f"Changed: {', '.join(entry.changed_fields)}",
                    f"Cache folder: {entry.save_path or 'unavailable'}",
                    "",
                ]
            )

        message = QtWidgets.QMessageBox(self)
        message.setWindowTitle("Condition changed since preprocessing")
        message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        message.setText(
            f"{len(conflicts_by_row)} preprocessed data entr"
            f"{'y was' if len(conflicts_by_row) == 1 else 'ies were'} created "
            "with an older version of the selected condition."
        )
        message.setInformativeText(
            "These cached data cannot be reused safely. Delete the stale data "
            "and continue with the analysis, or cancel the launch."
        )
        message.setDetailedText("\n".join(details).rstrip())
        delete_button = message.addButton(
            "Delete stale data and continue",
            QtWidgets.QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = message.addButton(
            "Cancel",
            QtWidgets.QMessageBox.ButtonRole.RejectRole,
        )
        message.setDefaultButton(cancel_button)
        message.setEscapeButton(cancel_button)
        message.exec()
        if message.clickedButton() is not delete_button:
            return False

        try:
            plan = build_preprocessed_deletion_plan(
                project_root,
                conflicts_by_row.keys(),
            )
            result = execute_preprocessed_deletion(plan)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Could not delete stale preprocessed data",
                f"The analysis was not launched.\n\n{exc}",
            )
            return False

        try:
            self.preprocessed_data_manager.refresh()
        except Exception:
            pass
        warning = str((result or {}).get("warning", "") or "")
        if warning:
            QtWidgets.QMessageBox.warning(self, "Preprocessed data", warning)
        return True

    def _analysis_pipeline_output_text(self, snapshot: dict, pipeline: Optional[dict] = None) -> str:
        output_path = self._analysis_pipeline_output_path(snapshot, pipeline)
        if output_path is None:
            options = (snapshot or {}).get("analysis_options", {}) or {}
            if not bool(options.get("save_analysis", False)):
                return "Not saved"
            return "Save folder not defined"
        return str(output_path)

    def _analysis_pipeline_output_path(
        self,
        snapshot: dict,
        pipeline: Optional[dict] = None,
    ) -> Optional[Path]:
        options = (snapshot or {}).get("analysis_options", {}) or {}
        if not bool(options.get("save_analysis", False)):
            return None
        folder_name = str(options.get("analysis_save_name", "") or "").strip()
        if not folder_name:
            return None
        try:
            return self._get_pipeline_analysis_output_root(pipeline) / folder_name
        except Exception:
            return Path(folder_name)

    def _pipeline_output_folder_conflicts(
        self,
        pipeline: dict,
        steps: list,
        step_indices: list,
    ) -> Optional[list]:
        """Return existing output folders for the selected, saved analysis steps."""
        conflicts_by_path = {}
        for step_index in step_indices:
            if step_index < 0 or step_index >= len(steps):
                continue
            step = steps[step_index]
            snapshot = step.get("snapshot") or {}
            output_path = self._analysis_pipeline_output_path(snapshot, pipeline)
            if output_path is None or not output_path.exists():
                continue
            if not output_path.is_dir():
                QtWidgets.QMessageBox.critical(
                    self,
                    "Analysis pipeline",
                    "An analysis output path already exists but is not a folder:\n\n"
                    f"{output_path}\n\nThe pipeline was not launched.",
                )
                return None
            try:
                path_key = str(output_path.resolve(strict=False)).casefold()
            except Exception:
                path_key = str(output_path).casefold()
            conflict = conflicts_by_path.setdefault(
                path_key,
                {
                    "path": output_path,
                    "steps": [],
                    "choice": "",
                },
            )
            step_name = str(step.get("name", "Analysis step") or "Analysis step")
            analysis_name = str(snapshot.get("analysis_name", "Analysis") or "Analysis")
            label = f"{step_name} (analysis: {analysis_name})"
            if label not in conflict["steps"]:
                conflict["steps"].append(label)
        return list(conflicts_by_path.values())

    def _prepare_pipeline_output_folders(self, pipeline: dict, steps: list, step_indices: list):
        """Resolve every existing output folder before a pipeline run starts."""
        conflicts = self._pipeline_output_folder_conflicts(pipeline, steps, step_indices)
        if conflicts is None:
            return None
        if not conflicts:
            return []

        choice_for_remaining = ""
        total = len(conflicts)
        for conflict_index, conflict in enumerate(conflicts):
            if choice_for_remaining:
                conflict["choice"] = choice_for_remaining
                continue

            step_labels = "\n".join(f"  • {label}" for label in conflict.get("steps", []))
            msg = QtWidgets.QMessageBox(self)
            msg.setWindowTitle("Existing analysis output folder")
            msg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            msg.setText(
                f"An output folder already exists for:\n\n{step_labels}\n\n"
                f"Folder {conflict_index + 1} of {total}:\n{conflict['path']}"
            )
            msg.setInformativeText(
                "Add: keep the existing folder and add this run's output.\n"
                "Delete: remove the existing folder before the pipeline starts.\n"
                "Cancel: do not launch the pipeline."
            )
            add_btn = msg.addButton("Add", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            delete_btn = msg.addButton("Delete", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(add_btn)
            msg.setEscapeButton(cancel_btn)

            apply_to_all_chk = None
            if total > 1 and conflict_index < total - 1:
                apply_to_all_chk = QtWidgets.QCheckBox(
                    "Apply this choice to all remaining existing folders",
                    msg,
                )
                msg.setCheckBox(apply_to_all_chk)

            msg.exec()
            clicked = msg.clickedButton()
            if clicked is cancel_btn or clicked not in (add_btn, delete_btn):
                return None
            choice = "delete" if clicked is delete_btn else "add"
            conflict["choice"] = choice
            if apply_to_all_chk is not None and apply_to_all_chk.isChecked():
                choice_for_remaining = choice

        # No destructive action is performed until every required choice has
        # been collected, so Cancel always leaves all folders untouched.
        for conflict in conflicts:
            if conflict.get("choice") != "delete":
                continue
            output_path = Path(conflict["path"])
            try:
                shutil.rmtree(output_path)
            except Exception as exc:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Analysis pipeline",
                    f"Could not delete the analysis output folder:\n\n{output_path}\n\n{exc}\n\n"
                    "The pipeline was not launched.",
                )
                return None
        return conflicts

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
        """Open the Script or Output folder from the overview table."""
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
            script = self._analysis_step_script_metadata(
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
            self._analysis_pipeline_output_path(snapshot, pipeline),
            "Output folder",
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
        self.analysis_tree.addTopLevelItem(self._pipeline_root_item)
        self._pipeline_root_item.setExpanded(True)

        for pipeline in self.pipeline_store.pipelines():
            pipeline_id = str(pipeline.get("id", ""))
            steps = list(pipeline.get("steps", []) or [])
            pipeline_item = QtWidgets.QTreeWidgetItem([str(pipeline.get("name", "Pipeline"))])
            pipeline_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#000000")))
            pipeline_item.setToolTip(0, f"{len(steps)} step(s)")
            self._pipeline_root_item.addChild(pipeline_item)
            self._pipeline_item_to_id[id(pipeline_item)] = pipeline_id
            self._pipeline_id_to_item[pipeline_id] = pipeline_item

            status = str((pipeline.get("last_run") or {}).get("status", ""))
            if status == "running":
                pipeline_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#2e7d32")))
            elif status in ("completed_with_failures", "interrupted"):
                pipeline_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#b00020")))
            elif status == "completed":
                pipeline_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#000000")))

            for index, step in enumerate(steps, start=1):
                step_id = str(step.get("id", ""))
                step_status = self._pipeline_step_run_status(pipeline, step)
                step_item = QtWidgets.QTreeWidgetItem([
                    f"{index}. {step.get('name', 'Step')} [{self._step_status_text(step_status)}]"
                ])
                step_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#000000")))
                step_item.setToolTip(0, self._analysis_step_summary(step.get("snapshot") or {}))
                pipeline_item.addChild(step_item)
                ref = (pipeline_id, step_id)
                self._pipeline_step_item_to_ref[id(step_item)] = ref
                self._pipeline_step_ref_to_item[ref] = step_item
                if step_status == "running":
                    step_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#2e7d32")))
                elif step_status == "failed":
                    step_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#b00020")))
                    step_item.setBackground(0, QtGui.QBrush(QtGui.QColor("#fff1f1")))
                elif step_status == "completed":
                    step_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#000000")))
            pipeline_item.setExpanded(pipeline_id in self._expanded_pipeline_ids)

    def _pipeline_tree_selection(self):
        items = self.analysis_tree.selectedItems()
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

        current_item = self.analysis_tree.currentItem()
        ref = self._item_to_ref.get(id(current_item)) if current_item is not None else None
        if ref is None:
            return state

        category, name = ref
        spec = (self.registry.get(category, {}) or {}).get(name)
        if spec is None:
            return state

        state["view"] = "analysis"
        state["analysis"] = {
            "category": str(category),
            "name": str(name),
            "target_paths": copy.deepcopy(self.target_paths),
            "selected_files": copy.deepcopy(self.selected_files),
            "conditions": copy.deepcopy(self.condition_section.get_conditions_dict()),
            "data_values": copy.deepcopy(self._get_selected_data_values()),
            "event_values": copy.deepcopy(self._capture_event_alignment_values()),
            "param_values": copy.deepcopy(self._capture_param_values()),
            "save_analysis": bool(self.save_analysis_chk.isChecked()),
            "analysis_save_name": str(self.analysis_name_edit.text()),
            "auto_plot": bool(self.auto_plot_chk.isChecked()),
            "figures_per_layout": int(self.figure_per_layer_spin.value()),
            "number_of_rows": int(self.figure_row_spin.value()),
            "log": str(self.log_edit.toPlainText()),
        }
        return state

    def _select_analysis_tree_ref(self, category: str, name: str):
        item = None
        root = self.analysis_tree.invisibleRootItem()
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
        blocked = self.analysis_tree.blockSignals(True)
        try:
            self.analysis_tree.setCurrentItem(item)
            item.setSelected(True)
        finally:
            self.analysis_tree.blockSignals(blocked)

    def _restore_pipeline_browsing_state(self, state):
        """Restore a user-selected view without changing the application's active tab."""
        state = dict(state or {})
        view = str(state.get("view", "blank"))

        if view == "preprocessed_data":
            self._show_preprocessed_data_manager()
            return

        if view == "analysis":
            saved = dict(state.get("analysis") or {})
            category = str(saved.get("category", ""))
            name = str(saved.get("name", ""))
            spec = (self.registry.get(category, {}) or {}).get(name)
            if spec is None:
                _category, spec = self._analysis_registry_entry(name)
            if spec is None:
                return

            self._detach_pipeline_editor()
            self._active_pipeline_step_id = None
            self.target_paths = list(saved.get("target_paths", []) or [])
            self.selected_files = list(saved.get("selected_files", []) or [])
            self.available_files = list(self.selected_files)
            self._selected_name = name
            self._selected_spec = spec

            blocked = self.condition_section.blockSignals(True)
            combo_blocked = self.condition_section.name_combo.blockSignals(True)
            try:
                self.condition_section.load_conditions_dict(
                    copy.deepcopy(saved.get("conditions", {}) or {})
                )
            finally:
                self.condition_section.name_combo.blockSignals(combo_blocked)
                self.condition_section.blockSignals(blocked)

            self._show_details(
                name,
                spec,
                keep_param_values=copy.deepcopy(saved.get("param_values", {}) or {}),
                keep_data_values=copy.deepcopy(saved.get("data_values", []) or []),
                keep_event_values=copy.deepcopy(saved.get("event_values", {}) or {}),
            )
            self.save_analysis_chk.setChecked(bool(saved.get("save_analysis", False)))
            self.analysis_name_edit.setText(str(saved.get("analysis_save_name", "")))
            self.auto_plot_chk.setChecked(bool(saved.get("auto_plot", False)))
            self.figure_per_layer_spin.setValue(int(saved.get("figures_per_layout", 4)))
            self.figure_row_spin.setValue(int(saved.get("number_of_rows", 2)))
            self.log_edit.setPlainText(str(saved.get("log", "")))
            self.pipeline_widget.hide()
            self.details_widget.show()
            self._select_analysis_tree_ref(category, name)
            self._refresh_availability_display()
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
        blocked = self.analysis_tree.blockSignals(True)
        try:
            self.analysis_tree.setCurrentItem(item)
            item.setSelected(True)
            self.analysis_tree.scrollToItem(item)
        finally:
            self.analysis_tree.blockSignals(blocked)

    def _refresh_pipeline_tree_items(
        self,
        selected_pipeline_id: Optional[str] = None,
        selected_step_id: Optional[str] = None,
    ):
        """Rebuild the middle-tree pipeline branch and preserve its selection."""
        current_item = self.analysis_tree.currentItem()
        current_kind, current_pipeline_id, current_step_id = self._pipeline_tree_selection()
        keep_editor_item = current_item if current_kind is None and current_item is not None else None
        if selected_pipeline_id is None:
            selected_pipeline_id = current_pipeline_id
        if selected_step_id is None and current_kind == "step":
            selected_step_id = current_step_id
        blocked = self.analysis_tree.blockSignals(True)
        try:
            if self._pipeline_root_item is not None:
                index = self.analysis_tree.indexOfTopLevelItem(self._pipeline_root_item)
                if index >= 0:
                    self.analysis_tree.takeTopLevelItem(index)
            self._append_pipeline_tree_items()
            if keep_editor_item is not None:
                self.analysis_tree.setCurrentItem(keep_editor_item)
                keep_editor_item.setSelected(True)
            elif selected_pipeline_id and selected_step_id:
                item = self._pipeline_step_ref_to_item.get(
                    (str(selected_pipeline_id), str(selected_step_id))
                )
                if item is not None:
                    self.analysis_tree.setCurrentItem(item)
                    item.setSelected(True)
            elif selected_pipeline_id:
                item = self._pipeline_id_to_item.get(str(selected_pipeline_id))
                if item is not None:
                    self.analysis_tree.setCurrentItem(item)
                    item.setSelected(True)
        finally:
            self.analysis_tree.blockSignals(blocked)
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
        try:
            self.right_panel.show_analysis_view()
        except Exception:
            pass
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
        try:
            self.right_panel.show_analysis_view()
        except Exception:
            pass
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
        last_started_at = str(last_run.get("started_at", "") or "")
        if last_started_at:
            text += f" | Last run: {_run_date_text(last_started_at)}"
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
        self.launch_pipeline_btn.setEnabled(
            editable and selected_step_count > 0
        )
        self.launch_pipeline_btn.setToolTip("")
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
        """Restore a row selection after renaming or moving an overview step."""
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
            "Rename analysis step",
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
            QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
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
            self.pipeline_overview_title_lbl.setText("Analysis pipelines")
            self.pipeline_summary_lbl.setText("No analysis pipeline has been created yet.")
            report_directory = self.pipeline_store.directory / "reports" / "analysis"
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
                report_directory = self.pipeline_store.directory / "reports" / "analysis"
                self.pipeline_report_path_lbl.setText(f"Report destination: {report_directory}")

            self._display_pipeline_log(self._pipeline_log_text(pipeline))

            for index, step in enumerate(steps, start=1):
                snapshot = step.get("snapshot") or {}
                step_run = step.get("last_run") or {}
                step_status = self._pipeline_step_run_status(pipeline, step)
                script = self._analysis_step_script_metadata(snapshot, step_run)
                script_name = str(script.get("script_name", "Unknown") or "Unknown")
                script_path = str(script.get("script_path", "Unknown") or "Unknown")
                row = QtWidgets.QTreeWidgetItem([
                    "",
                    str(index),
                    str(step.get("name", "Step")),
                    script_name,
                    _run_date_text(step_run.get("started_at", "")),
                    self._step_status_text(step_status),
                    self._analysis_pipeline_output_text(
                        snapshot,
                        pipeline,
                    ),
                ])
                row.setData(0, qt_user_role(), str(step.get("id", "")))
                row.setCheckState(
                    0,
                    qt_checked() if self._pipeline_step_enabled(step) else qt_unchecked(),
                )
                row.setToolTip(2, self._analysis_step_summary(step.get("snapshot") or {}))
                row.setToolTip(3, f"{script_path}\nDouble-click to open the script folder.")
                output_path = self._analysis_pipeline_output_path(snapshot, pipeline)
                if output_path is not None:
                    row.setToolTip(
                        6,
                        f"{output_path}\nDouble-click to open the output folder.",
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
        self._load_pipeline_plot_options(pipeline)
        self._update_pipeline_selection_controls(pipeline)
        plot_controls_enabled = has_pipeline and not running and not editing
        self.pipeline_plot_btn.setEnabled(plot_controls_enabled)
        self.pipeline_figure_per_layer_spin.setEnabled(plot_controls_enabled)
        self.pipeline_figure_row_spin.setEnabled(plot_controls_enabled)
        self.pipeline_auto_plot_chk.setEnabled(plot_controls_enabled)
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
        script = self._analysis_step_script_metadata(
            step.get("snapshot") or {},
            last_run,
        )
        script_name = str(script.get("script_name", "Unknown") or "Unknown")
        self.pipeline_detail_subtitle_lbl.setText(
            status_text
            + (f" | {script_name}" if script_name else "")
            + (f" | {_run_date_text(timestamp)}" if timestamp else "")
        )
        self.pipeline_script_lbl.setText(
            f"Script: {script_name}\n"
            f"Location: {script.get('script_path', 'Unknown')}"
        )
        self._pipeline_current_error = str(last_run.get("error", "") or "")
        try:
            self._attach_pipeline_editor()
            self.pipeline_step_name_edit.setText(str(step.get("name", "Step")))
            self._restore_analysis_snapshot(
                copy.deepcopy(step.get("snapshot") or {}),
                show_editor=False,
            )
            self.pipeline_detail_stack.setCurrentWidget(self.pipeline_edit_page)
            self.details_widget.show()
            self._set_pipeline_editor_editable(False)
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
        """Place the normal Analysis editor inside the selected pipeline step."""
        if self.details_widget.parent() is not self.pipeline_edit_host:
            self._pipeline_parent_layout.removeWidget(self.details_widget)
            self.details_widget.setParent(self.pipeline_edit_host)
            self.pipeline_edit_host_layout.addWidget(self.details_widget)
        # Plot layout and automatic Plot GUI opening are pipeline-level
        # choices. Do not show duplicate controls in an individual step.
        self.analysis_plot_options_widget.hide()

    def _detach_pipeline_editor(self):
        """Return the normal editor to the right panel when leaving Pipelines."""
        if self.details_widget.parent() is self.pipeline_edit_host:
            self.pipeline_edit_host_layout.removeWidget(self.details_widget)
            self.details_widget.setParent(self.right_panel.analysis_container)
            self._pipeline_parent_layout.insertWidget(0, self.details_widget)
        self.analysis_plot_options_widget.show()
        self._pipeline_inline_editing = False
        self.pipeline_step_name_edit.setReadOnly(False)
        for widget in (
            self.condition_section,
            self.data_box,
            self.event_box,
            self.params_box,
            self.save_analysis_chk,
            self.analysis_name_edit,
        ):
            widget.setEnabled(True)

    def _set_pipeline_editor_editable(self, editable: bool):
        editable = bool(editable)
        self._pipeline_inline_editing = editable
        self.pipeline_mode_lbl.setText("Editing" if editable else "Saved configuration")
        self.pipeline_step_name_edit.setReadOnly(not editable)
        for widget in (
            self.condition_section,
            self.data_box,
            self.event_box,
            self.params_box,
            self.save_analysis_chk,
            self.analysis_name_edit,
        ):
            widget.setEnabled(editable)
        self.analysis_plot_options_widget.hide()
        self._sync_pipeline_edit_buttons()

    def _show_pipeline_run_result(self):
        _pipeline, step, _index = self._selected_pipeline_step()
        if step is None:
            return
        last_run = step.get("last_run") or {}
        status = self._step_status_text(last_run.get("status", ""))
        script = self._analysis_step_script_metadata(
            step.get("snapshot") or {},
            last_run,
        )
        script_name = str(script.get("script_name", "Unknown") or "Unknown")

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"Analysis result: {step.get('name', 'Step')}")
        dialog.resize(850, 600)
        layout = QtWidgets.QVBoxLayout(dialog)
        summary = QtWidgets.QLabel(
            f"Status: {status}\n"
            f"Script: {script_name}\n"
            f"Location: {script.get('script_path', 'Unknown')}\n"
            f"Started: {last_run.get('started_at', '') or ''}\n"
            f"Finished: {last_run.get('finished_at', '') or ''}"
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

    def _load_pipeline_plot_options(self, pipeline):
        """Display the saved layout used for the pipeline-level Plot GUI."""
        options = (pipeline or {}).get("plot_options", {}) or {}
        try:
            figures_per_layout = int(options.get("figures_per_layout", 4))
        except (TypeError, ValueError):
            figures_per_layout = 4
        try:
            number_of_rows = int(options.get("number_of_rows", 2))
        except (TypeError, ValueError):
            number_of_rows = 2
        auto_launch = bool(options.get("auto_launch", False))

        widgets = (
            self.pipeline_figure_per_layer_spin,
            self.pipeline_figure_row_spin,
            self.pipeline_auto_plot_chk,
        )
        previous_states = [widget.blockSignals(True) for widget in widgets]
        try:
            self.pipeline_figure_per_layer_spin.setValue(figures_per_layout)
            self.pipeline_figure_row_spin.setValue(number_of_rows)
            self.pipeline_auto_plot_chk.setChecked(auto_launch)
        finally:
            for widget, previous_state in zip(widgets, previous_states):
                widget.blockSignals(previous_state)

    def _save_pipeline_plot_options(self, *_args):
        """Persist Plot GUI layout controls with the selected pipeline."""
        pipeline = self._active_pipeline()
        if pipeline is None or self._pipeline_run is not None or self._running:
            return
        options = {
            "figures_per_layout": int(self.pipeline_figure_per_layer_spin.value()),
            "number_of_rows": int(self.pipeline_figure_row_spin.value()),
            "auto_launch": bool(self.pipeline_auto_plot_chk.isChecked()),
        }
        if dict(pipeline.get("plot_options", {}) or {}) == options:
            return
        pipeline["plot_options"] = options
        pipeline["updated_at"] = utc_now_text()
        try:
            self.pipeline_store.save()
        except Exception as exc:
            self.log_message.emit(f"Could not save pipeline plot options: {exc}")

    def _pipeline_result(self, pipeline=None):
        """Return the figures produced by the supplied pipeline's current run."""
        pipeline = pipeline or self._active_pipeline()
        figures = []
        seen = set()
        for step in (pipeline or {}).get("steps", []) or []:
            result = self._pipeline_results.get(str(step.get("id", "")))
            for figure in list(getattr(result, "figures", None) or []):
                if id(figure) in seen:
                    continue
                seen.add(id(figure))
                figures.append(figure)
        return SimpleNamespace(figures=figures)

    def _display_completed_figures(
        self,
        result,
        *,
        context="analysis",
        open_plot_gui=False,
        figures_per_layout=4,
        number_of_rows=2,
    ):
        """Show normal figure windows, then optionally open the arrangement GUI."""
        figures = list(getattr(result, "figures", None) or [])
        if not figures:
            return

        try:
            displayed = display_result_figures(
                result,
                log_callback=self.log_message.emit,
            )
        except Exception as exc:
            displayed = False
            self.log_message.emit(f"Could not display {context} figures automatically: {exc}")

        if displayed:
            self.log_message.emit(
                f"{context.capitalize()} figures displayed automatically. "
                "Click Plot GUI only to arrange or edit them."
            )
        else:
            self.log_message.emit(
                f"Could not open the {context} figures as normal Matplotlib windows."
            )

        if open_plot_gui:
            try:
                launch_plotting_gui(
                    result,
                    nb_fig_layer=int(figures_per_layout),
                    nb_of_row=int(number_of_rows),
                    log_callback=self.log_message.emit,
                )
            except Exception as exc:
                self.log_message.emit(f"Could not open plotting GUI: {exc}")

    def _open_pipeline_plotting_gui(self):
        """Open figures produced by every completed step of the active run."""
        result = self._pipeline_result()
        figures = list(getattr(result, "figures", None) or [])

        if figures:
            try:
                launch_plotting_gui(
                    result,
                    nb_fig_layer=int(self.pipeline_figure_per_layer_spin.value()),
                    nb_of_row=int(self.pipeline_figure_row_spin.value()),
                    log_callback=self.log_message.emit,
                )
            except Exception as exc:
                self.log_message.emit(f"Could not open plotting GUI: {exc}")
            return
        self.log_message.emit("No figures are available from the current pipeline run")

    def _show_pipeline_error(self):
        if not self._pipeline_current_error:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Full analysis error")
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
        name, accepted = QtWidgets.QInputDialog.getText(self, "New analysis pipeline", "Pipeline name")
        if not accepted:
            return None
        try:
            return self.pipeline_store.create(str(name))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
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
            "Rename analysis pipeline",
            "Pipeline name",
            text=str(pipeline.get("name", "")),
        )
        if not accepted:
            return
        try:
            self.pipeline_store.rename(str(pipeline.get("id", "")), str(name))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
            return
        self.refresh_list()
        self._show_pipeline_view(str(pipeline.get("id", "")))

    def _delete_pipeline(self):
        pipeline = self._active_pipeline()
        if pipeline is None or self._pipeline_run is not None or self._running:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete analysis pipeline",
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
            "Add analysis to pipeline",
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

    def _analysis_registry_entry(self, name: str):
        for category, analyses in self.registry.items():
            if name in (analyses or {}):
                return str(category), (analyses or {})[name]
        return None, None

    def _analysis_step_script_metadata(self, snapshot: Optional[dict], last_run: Optional[dict] = None) -> dict:
        """Resolve script metadata, preferring the exact values stored at run time."""
        snapshot = snapshot or {}
        last_run = last_run or {}
        saved_name = str(last_run.get("script_name", "") or snapshot.get("script_name", "") or "").strip()
        saved_path = str(last_run.get("script_path", "") or snapshot.get("script_path", "") or "").strip()
        if saved_name or saved_path:
            return {
                "script_name": saved_name or Path(saved_path.replace("\\", "/")).name or "Unknown",
                "script_path": saved_path or "Unknown",
            }
        analysis_name = str(snapshot.get("analysis_name", "") or "")
        category = str(snapshot.get("category", "") or "")
        spec = (self.registry.get(category, {}) or {}).get(analysis_name)
        if spec is None:
            _category, spec = self._analysis_registry_entry(analysis_name)
        return _script_metadata_from_spec(spec)

    def _capture_analysis_snapshot(self):
        name, spec = self._get_selected_analysis_spec()
        if not spec or not name:
            name, spec = self._selected_name, self._selected_spec
        if not spec or not name:
            raise ValueError("No analysis is selected.")
        params = self._collect_and_validate_params()
        if params is None:
            raise ValueError(self.error_lbl.text() or "The analysis parameters are invalid.")
        event_alignment = self._collect_and_validate_event_alignment()
        if event_alignment is None:
            raise ValueError(self.error_lbl.text() or "The event alignment is invalid.")
        input_data = self._collect_and_validate_input_data()
        if input_data is None:
            raise ValueError(self.error_lbl.text() or "The input data selection is invalid.")
        if not self.condition_section.has_valid_conditions():
            raise ValueError("Each condition needs a name and at least one file.")
        category, _ = self._analysis_registry_entry(name)
        script = _script_metadata_from_spec(spec)
        return {
            "category": category or "",
            "analysis_name": str(name),
            "script_name": script.get("script_name", "Unknown"),
            "script_path": script.get("script_path", "Unknown"),
            "selected_files": list(self.selected_files),
            "conditions": copy.deepcopy(self.condition_section.get_conditions_dict()),
            "input_data": copy.deepcopy(input_data),
            "event_alignment": copy.deepcopy(event_alignment),
            "parameters": copy.deepcopy(params),
            "parameter_widget_values": copy.deepcopy(self._capture_param_values()),
            "analysis_options": {
                "save_analysis": bool(self.save_analysis_chk.isChecked()),
                "analysis_save_name": self._sanitize_folder_name(self.analysis_name_edit.text()),
            },
        }

    def _add_current_to_pipeline(self):
        if self._pipeline_run is not None or self._running:
            return
        try:
            snapshot = self._capture_analysis_snapshot()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
            return
        name = str(snapshot.get("analysis_name", "Analysis"))
        if self._editing_pipeline_step is not None:
            pipeline_id, step_id = self._editing_pipeline_step
            edited_name = str(self.pipeline_step_name_edit.text()).strip()
            if not edited_name:
                QtWidgets.QMessageBox.warning(self, "Analysis pipeline", "The step name cannot be empty.")
                return
            try:
                self.pipeline_store.update_step(pipeline_id, step_id, edited_name, snapshot)
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
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
                QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
                return
            self._active_pipeline_id = str(pipeline.get("id", ""))
        self._refresh_pipeline_tree_items()
        # Adding a step must leave the analysis editor open. Rebuilding the
        # pipeline branch can otherwise make Qt select that branch and invoke
        # _show_pipeline_view through itemSelectionChanged.
        self.pipeline_widget.hide()
        self.details_widget.show()
        try:
            self.right_panel.show_analysis_view()
        except Exception:
            pass

    def _saved_input_labels(self, saved_payloads: list[dict]) -> list[str]:
        labels = []
        for saved in saved_payloads or []:
            saved_path = str(saved.get("nwb_path", ""))
            saved_name = str(saved.get("analysis_data_name", saved.get("name", "")))
            saved_label = str(saved.get("label", ""))
            found = None
            for display, current in self._data_payload_by_label.items():
                same_path = str(current.get("nwb_path", "")) == saved_path
                current_name = str(current.get("analysis_data_name", current.get("name", "")))
                if same_path and current_name == saved_name:
                    found = display
                    break
                if saved_label and str(current.get("label", "")) == saved_label:
                    found = display
            if found and found not in labels:
                labels.append(found)
        return labels

    def _restore_analysis_snapshot(self, snapshot: dict, *, show_editor: bool):
        name = str(snapshot.get("analysis_name", ""))
        category = str(snapshot.get("category", ""))
        spec = (self.registry.get(category, {}) or {}).get(name)
        if spec is None:
            _, spec = self._analysis_registry_entry(name)
        if spec is None:
            raise ValueError(f'Analysis "{name}" is no longer available.')

        self.target_paths = list(snapshot.get("selected_files", []) or [])
        self.selected_files = list(self.target_paths)
        self.available_files = list(self.target_paths)
        self._selected_name = name
        self._selected_spec = spec

        blocked = self.condition_section.blockSignals(True)
        combo_blocked = self.condition_section.name_combo.blockSignals(True)
        try:
            self.condition_section.load_conditions_dict(copy.deepcopy(snapshot.get("conditions", {}) or {}))
        finally:
            self.condition_section.name_combo.blockSignals(combo_blocked)
            self.condition_section.blockSignals(blocked)

        self._show_details(
            name,
            spec,
            keep_param_values=copy.deepcopy(snapshot.get("parameter_widget_values", {}) or {}),
            keep_data_values=[],
            keep_event_values=copy.deepcopy(snapshot.get("event_alignment", {}) or {}),
        )
        files = self._get_files_from_conditions()
        self._set_data_choices_from_items(self._list_available_data(spec, files=files))
        if self._should_show_data_panel(spec):
            labels = self._saved_input_labels(snapshot.get("input_data", []) or [])
            self._rebuild_data_rows(keep_values=labels)
        self._rebuild_event_alignment_ui(keep_values=snapshot.get("event_alignment", {}) or {})
        self._build_params_ui(
            spec,
            keep_values=copy.deepcopy(snapshot.get("parameter_widget_values", {}) or {}),
        )

        options = snapshot.get("analysis_options", {}) or {}
        self.save_analysis_chk.setChecked(bool(options.get("save_analysis", False)))
        self.analysis_name_edit.setText(str(options.get("analysis_save_name", "") or self._get_default_analysis_save_name()))
        # Ignore plotting values in legacy step snapshots. The pipeline
        # overview is the single owner of layout and automatic opening.
        pipeline_plot_options = (self._active_pipeline() or {}).get("plot_options", {}) or {}
        self.auto_plot_chk.setChecked(False)
        self.figure_per_layer_spin.setValue(int(pipeline_plot_options.get("figures_per_layout", 4)))
        self.figure_row_spin.setValue(int(pipeline_plot_options.get("number_of_rows", 2)))
        self._refresh_availability_display()

        if show_editor:
            self.pipeline_widget.hide()
            self.details_widget.show()
            try:
                self.right_panel.show_analysis_view()
            except Exception:
                pass
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
        self.analysis_tree.setEnabled(False)
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
        self.analysis_tree.setEnabled(True)

    def _modify_pipeline_step(self):
        pipeline, step, _index = self._selected_pipeline_step()
        if pipeline is None or step is None or self._pipeline_run is not None or self._running:
            return
        self._editing_pipeline_step = (str(pipeline.get("id", "")), str(step.get("id", "")))
        self._sync_pipeline_edit_buttons()
        try:
            self._restore_analysis_snapshot(copy.deepcopy(step.get("snapshot") or {}), show_editor=False)
            self._enter_pipeline_inline_editor(step)
        except Exception as exc:
            self._editing_pipeline_step = None
            self._leave_pipeline_inline_editor()
            self._sync_pipeline_edit_buttons()
            self._show_pipeline_view(str(pipeline.get("id", "")))
            self._refresh_pipeline_view(selected_step_id=str(step.get("id", "")))
            QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))

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
            QtWidgets.QMessageBox.warning(self, "Analysis pipeline", str(exc))
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

    def _close_plotting_gui_before_launch(self, *_args):
        """Close the Plot GUI as the first action of either launch-button click."""
        close_plotting_gui(log_callback=self.log_message.emit)

    def _run_button_clicked(self, *_args):
        """Close the Plot GUI first, then launch one manual analysis."""
        self._close_plotting_gui_before_launch()
        return self._run_selected()

    def _launch_pipeline_button_clicked(self, *_args):
        """Close the Plot GUI first, then launch the selected pipeline."""
        self._close_plotting_gui_before_launch()
        return self._launch_pipeline()

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
                "Analysis pipeline",
                "Select at least one pipeline step to run.",
            )
            return

        cache_requests = []
        for selected_position, step_index in enumerate(step_indices, start=1):
            step = steps[step_index]
            step_name = str(step.get("name", "Analysis step") or "Analysis step")
            request = self._cache_request_from_snapshot(
                step.get("snapshot", {}) or {},
                f"Pipeline step {selected_position}: {step_name}",
            )
            if request is not None:
                cache_requests.append(request)
        if not self._confirm_stale_preprocessed_data(cache_requests):
            return

        skipped_steps = len(steps) - len(step_indices)
        saved_steps = [
            step
            for step in steps
            if self._pipeline_step_enabled(step)
            and bool(
                (((step.get("snapshot") or {}).get("analysis_options", {}) or {}).get(
                    "save_analysis", False
                ))
            )
        ]
        pipeline_output_root = None
        output_folder_conflicts = []
        if saved_steps:
            output_folder_conflicts = self._prepare_pipeline_output_folders(
                pipeline,
                steps,
                step_indices,
            )
            if output_folder_conflicts is None:
                return
            pipeline_output_root = self._get_pipeline_analysis_output_root(pipeline)
            try:
                pipeline_output_root.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                QtWidgets.QMessageBox.critical(
                    self,
                    "Analysis pipeline",
                    f"Could not create the pipeline output folder:\n{pipeline_output_root}\n\n{exc}",
                )
                return

        pipeline_id = str(pipeline.get("id", ""))
        self._active_pipeline_id = pipeline_id
        self._active_pipeline_step_id = None
        self.pipeline_detail_stack.setCurrentWidget(self.pipeline_overview_page)
        self.details_widget.hide()
        self._select_pipeline_tree_item(pipeline_id)
        self._pipeline_results = {}
        started_at = utc_now_text()
        run_header = (
            f"Analysis pipeline: {pipeline.get('name', 'Pipeline')}\n"
            f"Started: {started_at}\n"
            f"Selected steps: {len(step_indices)}/{len(steps)}"
        )
        report_lines = [
            f"Analysis pipeline: {pipeline.get('name', 'Pipeline')}",
            f"Started: {started_at}",
            f"Selected steps: {len(step_indices)}/{len(steps)}",
            f"Skipped steps: {skipped_steps}",
        ]
        if pipeline_output_root is not None:
            run_header += f"\nAnalysis output folder: {pipeline_output_root}"
            report_lines.append(f"Analysis output folder: {pipeline_output_root}")
        if output_folder_conflicts:
            report_lines.append("Existing output folder choices:")
            for conflict in output_folder_conflicts:
                action = "Add to existing folder"
                if conflict.get("choice") == "delete":
                    action = "Delete existing folder and recreate it"
                report_lines.append(f"  {action}: {conflict.get('path', '')}")
        report_lines.append("")
        previous_log = str((pipeline.get("last_run") or {}).get("log", "") or "").rstrip()
        if previous_log:
            initial_log = (
                previous_log
                + "\n\n"
                + ("=" * 72)
                + "\nNEW ANALYSIS PIPELINE RUN\n"
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
            "output_root": str(pipeline_output_root) if pipeline_output_root is not None else "",
            "output_folder_choices": [
                {
                    "path": str(conflict.get("path", "") or ""),
                    "choice": str(conflict.get("choice", "") or ""),
                }
                for conflict in output_folder_conflicts
            ],
            "started_at": started_at,
            "failed_steps": 0,
            "report_lines": report_lines,
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
        script = self._analysis_step_script_metadata(step.get("snapshot") or {})
        script_name = str(script.get("script_name", "Unknown") or "Unknown")
        script_path = str(script.get("script_path", "Unknown") or "Unknown")
        step["last_run"] = {
            "status": "running",
            "script_name": script_name,
            "script_path": script_path,
            "started_at": now,
            "finished_at": None,
            "error": "",
            "log": "",
        }
        self._append_pipeline_run_log(
            f"\n\nPipeline step {step_index + 1} "
            f"({index + 1}/{len(step_indices)} selected): {step.get('name', 'Step')}\n"
            f"Script: {script_name}\n"
            f"Location: {script_path}\n"
            f"Started: {now}"
        )
        self.pipeline_store.save()
        state["report_lines"].extend([
            f"PIPELINE STEP {step_index + 1} "
            f"({index + 1}/{len(step_indices)} selected): {step.get('name', 'Step')}",
            f"Script: {script_name}",
            f"Location: {script_path}",
            f"Started: {now}",
        ])
        self.jobs_label.setText(
            f"● Pipeline {pipeline.get('name', 'Pipeline')}: "
            f"running {index + 1}/{len(step_indices)} selected steps"
        )
        self.jobs_label.setStyleSheet(
            "QLabel { background: #f6faf7; color: #2e7d32; border: 1px solid #cfe8d2; "
            "border-radius: 4px; padding: 3px 5px; }"
        )
        self.jobs_label.setVisible(True)
        self._refresh_pipeline_view()
        browsing_state = self._capture_pipeline_browsing_state()
        try:
            self._active_pipeline_id = str(pipeline.get("id", ""))
            self._pipeline_restoring_step = True
            self._restore_analysis_snapshot(copy.deepcopy(step.get("snapshot") or {}), show_editor=False)
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
        previous_run = step.get("last_run") or {}
        step["last_run"] = {
            "status": "completed" if success else "failed",
            "script_name": previous_run.get("script_name", ""),
            "script_path": previous_run.get("script_path", ""),
            "started_at": previous_run.get("started_at"),
            "finished_at": finished_at,
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
        auto_launch_plot_gui = bool(
            ((pipeline.get("plot_options", {}) or {}).get("auto_launch", False))
        )
        figures_per_layout = int(
            ((pipeline.get("plot_options", {}) or {}).get("figures_per_layout", 4))
        )
        number_of_rows = int(
            ((pipeline.get("plot_options", {}) or {}).get("number_of_rows", 2))
        )
        pipeline_result = self._pipeline_result(pipeline)
        self._pipeline_run = None
        self.preprocessed_data_manager.refresh_busy_state()
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
        self._refresh_availability_display()
        if getattr(pipeline_result, "figures", None):
            QtCore.QTimer.singleShot(
                0,
                lambda result=pipeline_result,
                       auto_launch=auto_launch_plot_gui,
                       figures_per_layout=figures_per_layout,
                       number_of_rows=number_of_rows: self._display_completed_figures(
                    result,
                    context="pipeline",
                    open_plot_gui=auto_launch,
                    figures_per_layout=figures_per_layout,
                    number_of_rows=number_of_rows,
                ),
            )

    # ---------------- discovery/list ----------------

    def _discover_analyses(self):
        project_root = Path(getattr(self.GUI_instance, "project_root", Path.cwd()))
        script_dir = project_root / "Script" / "Analysis"
        return discover_for_panel(discover_analyses, "Analyses", script_dir, self)

    def set_registry(self, registry: dict):
        self.registry = registry or {}
        self.refresh_list()

    def refresh_list(self):
        signals_were_blocked = self.analysis_tree.blockSignals(True)
        try:
            self.analysis_tree.clear()
            self._item_to_ref = {}
            for category in sorted(self.registry.keys(), key=str.lower):
                cat_item = QtWidgets.QTreeWidgetItem([str(category)])
                self.analysis_tree.addTopLevelItem(cat_item)
                cat_item.setExpanded(True)
                analyses = self.registry.get(category, {}) or {}
                for name in sorted(analyses.keys(), key=str.lower):
                    spec = analyses[name]
                    item = QtWidgets.QTreeWidgetItem([str(name)])
                    if not self._is_enabled(spec):
                        try:
                            set_item_foreground(item, "#888888")
                        except Exception:
                            pass
                    cat_item.addChild(item)
                    self._item_to_ref[id(item)] = (str(category), str(name))
            self._append_pipeline_tree_items()
        finally:
            self.analysis_tree.blockSignals(signals_were_blocked)
        self._apply_running_highlight()

    def is_running(self) -> bool:
        return bool(self._running)

    def _set_running(self, running: bool, analysis_name: str | None = None):
        running = bool(running)
        name = str(analysis_name or self._running_analysis_name or self._selected_name or "Analysis").strip() or "Analysis"
        changed = (self._running != running)
        self._running = running
        self._running_analysis_name = name if running else ""

        try:
            if running:
                self.jobs_label.setStyleSheet(
                    "QLabel { background: #f6faf7; color: #2e7d32; "
                    "border: 1px solid #cfe8d2; border-radius: 4px; padding: 3px 5px; }"
                )
                self.jobs_label.setText(f"● {name} is running")
                self.jobs_label.setVisible(True)
            elif self._pipeline_run is None:
                self.jobs_label.setVisible(False)
        except Exception:
            pass

        self._apply_running_highlight()
        self._refresh_availability_display()

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
        running_name = str(self._running_analysis_name or "") if self._running else ""

        it = QtWidgets.QTreeWidgetItemIterator(self.analysis_tree)
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
            it += 1

    def _is_enabled(self, spec: dict) -> bool:
        fn = spec.get("enabled_if", None)
        if callable(fn):
            try:
                return bool(fn(self.target_paths))
            except Exception:
                return False
        return True

    def _get_selected_analysis_spec(self) -> Tuple[Optional[str], Optional[dict]]:
        items = self.analysis_tree.selectedItems()
        if not items:
            return None, None
        item = items[0]
        ref = self._item_to_ref.get(id(item))
        if not ref:
            return None, None
        category, name = ref
        return name, (self.registry.get(category, {}) or {}).get(name)

    def _on_select(self):
        current_item = self.analysis_tree.currentItem()

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
        name, spec = self._get_selected_analysis_spec()
        if not spec:
            self._selected_name = None
            self._selected_spec = None
            self._clear_details()
            return
        self._selected_name = name
        self._selected_spec = spec
        self._show_details(name or "", spec)
        self._apply_running_highlight()

    def _show_preprocessed_data_manager(self):
        self.preprocessed_data_manager.refresh_busy_state()
        if self.preprocessed_data_window.isMinimized():
            self.preprocessed_data_window.showNormal()
        else:
            self.preprocessed_data_window.show()
        self.preprocessed_data_window.raise_()
        self.preprocessed_data_window.activateWindow()
        QtCore.QTimer.singleShot(0, self.preprocessed_data_manager.refresh)

    # ---------------- target/selection ----------------

    def set_target_paths(self, paths: list[str]):
        scroll_value = self._details_scroll_value()
        keep_params = self._capture_param_values()
        keep_data = self._get_selected_data_values()
        keep_event = self._capture_event_alignment_values()
        was_pipeline_editable = bool(self._pipeline_inline_editing)
        self.target_paths = list(
            dict.fromkeys(str(path) for path in (paths or []) if str(path).strip())
        )
        self.selected_files = list(self.target_paths)
        self.available_files = list(self.target_paths)
        self._refresh_analysis_enabled_states()

        # A file/filter click updates inputs but must not turn a pipeline
        # overview into an analysis editor or destroy the selected step. Reuse
        # the existing editor only when an editor page is already open.
        editor_is_open = bool(
            self.pipeline_widget.isHidden()
            or self.pipeline_detail_stack.currentWidget() is self.pipeline_edit_page
        )
        if editor_is_open and self._selected_name and self._selected_spec:
            self._show_details(
                self._selected_name,
                self._selected_spec,
                keep_param_values=keep_params,
                keep_data_values=keep_data,
                keep_event_values=keep_event,
                scroll_value=scroll_value,
            )
            if self.details_widget.parent() is self.pipeline_edit_host:
                self._set_pipeline_editor_editable(was_pipeline_editable)

    def _refresh_analysis_enabled_states(self):
        """Update analysis availability without rebuilding its navigation tree."""
        self._apply_running_highlight()
        running_name = str(self._running_analysis_name or "") if self._running else ""
        iterator = QtWidgets.QTreeWidgetItemIterator(self.analysis_tree)
        while iterator.value():
            item = iterator.value()
            ref = self._item_to_ref.get(id(item))
            if ref is not None:
                category, name = ref
                spec = (self.registry.get(category, {}) or {}).get(name)
                if spec is not None and name != running_name:
                    if self._is_enabled(spec):
                        item.setForeground(0, QtGui.QBrush())
                    else:
                        try:
                            set_item_foreground(item, "#888888")
                        except Exception:
                            pass
            iterator += 1

    def _get_selected_files(self):
        return list(self.selected_files or self.target_paths or [])

    def _get_selected_nwb_paths_for_inputs(self) -> list[str]:
        out = []
        seen = set()
        for p in self._get_selected_files():
            s = str(p).strip()
            if not s or not s.lower().endswith(".nwb"):
                continue
            key = s.replace("\\", "/")
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    def _get_files_from_conditions(self):
        out = []
        seen = set()
        for row in self.condition_section.rows:
            for p in list(row.file_path_list or []):
                s = str(p).strip()
                if not s or not s.lower().endswith(".nwb"):
                    continue
                key = s.replace("\\", "/")
                if key in seen:
                    continue
                seen.add(key)
                out.append(s)
        if not out:
            return self._get_selected_nwb_paths_for_inputs()
        return out

    # ---------------- details / choices ----------------

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            child = item.layout()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif child is not None:
                self._clear_layout(child)

    def _clear_details(self):
        self.title_lbl.setText("")
        self.desc_lbl.setText("")
        self.available_lbl.setText("No analysis selected")
        self.error_lbl.setText("")
        self.data_box.hide()
        self.event_box.hide()
        self.params_box.hide()
        self.run_btn.setEnabled(False)
        self.add_pipeline_btn.setEnabled(False)
        self.plot_btn.setEnabled(False)
        self._reset_progress_on_gui_thread("Idle")
        self._clear_data_rows()
        self._clear_layout(self.params_layout)

    def _details_scroll_value(self) -> int:
        """Return the current position of the Analysis details scroll area."""
        try:
            return int(self.scroll.verticalScrollBar().value())
        except Exception:
            return 0

    def _restore_details_scroll(self, value: int):
        """Restore a details position after Qt has recalculated the layout."""
        try:
            saved_value = max(0, int(value))
        except Exception:
            saved_value = 0

        def restore():
            try:
                self.scroll.verticalScrollBar().setValue(saved_value)
            except Exception:
                pass

        # Set it now and once more after pending layout changes. Rebuilding the
        # input/event/parameter widgets can otherwise make QScrollArea reset to
        # the beginning on the next event-loop pass.
        restore()
        QtCore.QTimer.singleShot(0, restore)

    def _show_details(
        self,
        name: str,
        spec: dict,
        keep_param_values=None,
        keep_data_values=None,
        keep_event_values=None,
        scroll_value=None,
    ):
        restoring_pipeline_step = bool(getattr(self, "_pipeline_restoring_step", False))
        if not restoring_pipeline_step and self.details_widget.parent() is not self.pipeline_edit_host:
            self.pipeline_widget.hide()
        if not restoring_pipeline_step:
            self.details_widget.show()
        self.title_lbl.setText(str(spec.get("name", name)))
        self.desc_lbl.setText(str(spec.get("description", "") or ""))
        if not self.analysis_name_edit.text().strip():
            self.analysis_name_edit.setText(self._get_default_analysis_save_name())

        input_spec = dict(spec.get("input_spec", {}) or {})
        self._allow_multiple_data = bool(input_spec.get("allow_multiple", False))

        data_items = self._list_available_data(spec)
        self._set_data_choices_from_items(data_items)
        if self._should_show_data_panel(spec):
            self._rebuild_data_rows(keep_values=keep_data_values)
            self.data_box.show()
        else:
            self._clear_data_rows()
            self.data_box.hide()

        self._rebuild_event_alignment_ui(keep_values=keep_event_values)
        self._build_params_ui(spec, keep_values=keep_param_values)
        self._refresh_availability_display()
        if scroll_value is None:
            self.scroll.verticalScrollBar().setValue(0)
        else:
            self._restore_details_scroll(scroll_value)
        self._sync_pipeline_edit_buttons()

    def _sanitize_folder_name(self, value: str) -> str:
        value = str(value or "").strip()
        value = re.sub(r'[\\/:*?"<>|]', "_", value)
        value = re.sub(r"\s+", "_", value)
        value = value.strip("._")
        return value

    def _get_default_analysis_save_name(self) -> str:
        return self._sanitize_folder_name(self._selected_name or "analysis")

    def _get_analysis_output_root(self) -> Path:
        return Path(self.GUI_instance.project_root) / "Analysis"

    def _get_pipeline_analysis_output_root(self, pipeline: Optional[dict] = None) -> Path:
        pipeline = pipeline if pipeline is not None else self._active_pipeline()
        pipeline_name = self._sanitize_folder_name((pipeline or {}).get("name", ""))
        return self._get_analysis_output_root() / (pipeline_name or "pipeline")

    def _should_show_data_panel(self, spec: dict) -> bool:
        input_spec = dict(spec.get("input_spec", {}) or {})
        accepted_data = [str(x).strip() for x in (input_spec.get("accepted_data", []) or []) if str(x).strip()]
        accepted_names = [str(x).strip() for x in (input_spec.get("accepted_names", []) or []) if str(x).strip()]
        return bool(accepted_data or accepted_names)

    def _get_filtered_content_df_for_selected_files(self, files=None):
        cdf = getattr(self.GUI_instance, "content_df", None)
        if cdf is None or getattr(cdf, "empty", True):
            return None
        needed = {"file_path", "nwb_path", "name", "neurodata_type"}
        if not needed.issubset(set(cdf.columns)):
            return None
        selected_nwb = self._get_selected_nwb_paths_for_inputs() if files is None else list(files)
        selected_nwb = [str(p).strip() for p in selected_nwb if str(p).strip()]
        if not selected_nwb:
            return cdf.iloc[0:0].copy()
        selected_keys = {p.replace("\\", "/") for p in selected_nwb}
        file_keys = cdf["file_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        cdf2 = cdf[file_keys.isin(selected_keys)].copy()
        if cdf2.empty:
            return cdf2
        cdf2["file_path"] = cdf2["file_path"].astype(str)
        cdf2["nwb_path"] = cdf2["nwb_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        cdf2["name"] = cdf2["name"].astype(str).str.strip()
        cdf2["neurodata_type"] = cdf2["neurodata_type"].astype(str).str.strip()
        if "container" not in cdf2.columns:
            cdf2["container"] = ""
        if "module" not in cdf2.columns:
            cdf2["module"] = ""
        if "sorting_project" not in cdf2.columns:
            cdf2["sorting_project"] = ""
        cdf2["container"] = cdf2["container"].astype(str).str.strip()
        cdf2["module"] = cdf2["module"].astype(str).str.strip()
        cdf2["sorting_project"] = cdf2["sorting_project"].astype(str).replace("nan", "").str.strip()
        return cdf2

    def _list_available_data(self, spec, files=None):
        accepted = spec.get("input_spec", {}).get("accepted_data", [])
        accepted = {str(x).strip() for x in (accepted or []) if str(x).strip()}
        accepted_lower = {x.lower() for x in accepted}
        accepted_names = spec.get("input_spec", {}).get("accepted_names", None)
        if accepted_names:
            accepted_names = {str(x).strip() for x in accepted_names}

        out = []
        if "trials" in accepted_lower:
            out.append({"label": "trials", "type": "trials", "nwb_path": "trials", "name": "trials", "container": "table", "module": "", "analysis_data_name": "trials"})

        cdf = self._get_filtered_content_df_for_selected_files(files)
        if cdf is None or cdf.empty:
            if "units" in accepted_lower:
                out.append({"label": "units", "type": "units", "nwb_path": "units", "name": "units", "container": "units", "module": "", "sorting_project": "", "analysis_data_name": "units"})
            return out

        rows = cdf.copy()
        if accepted:
            neuro = rows["neurodata_type"].astype(str).str.strip()
            neuro_lower = neuro.str.lower()
            name_lower = rows["name"].astype(str).str.strip().str.lower()
            container_lower = rows.get("container", "").astype(str).str.strip().str.lower()
            nwb_path_lower = rows["nwb_path"].astype(str).str.strip().str.lower()
            rows = rows[
                neuro.isin(accepted)
                | neuro_lower.isin(accepted_lower)
                | name_lower.isin(accepted_lower)
                | (("units" in accepted_lower) & ((container_lower == "units") | nwb_path_lower.str.startswith("units")))
                | (("trials" in accepted_lower) & ((container_lower == "trials") | nwb_path_lower.eq("trials")))
            ]
        if accepted_names:
            rows = rows[rows["name"].astype(str).isin(accepted_names)]
        if rows.empty:
            if "units" in accepted_lower:
                out.append({"label": "units", "type": "units", "nwb_path": "units", "name": "units", "container": "units", "module": "", "sorting_project": "", "analysis_data_name": "units"})
            return out

        units_rows_seen = False
        for _, row in rows.iterrows():
            name = str(row.get("name", "")).strip()
            dtype = str(row.get("neurodata_type", "")).strip()
            nwb_path = str(row.get("nwb_path", "")).strip().replace("\\", "/")
            container = str(row.get("container", "")).strip()
            module = str(row.get("module", "")).strip()
            if not name:
                continue

            is_units = (
                name.lower() == "units"
                or container.lower() == "units"
                or nwb_path.lower().startswith("units")
                or dtype.lower() == "units"
            )
            if is_units:
                units_rows_seen = True
                sorting_project = _sorting_project_from_units_row(row)
                if sorting_project:
                    label = f"units ({sorting_project})"
                    nwb_path = _units_nwb_path_for_project(sorting_project, nwb_path)
                    analysis_data_name = label
                    module = sorting_project
                else:
                    label = "units"
                    nwb_path = "units"
                    analysis_data_name = "units"
                out.append({
                    "label": label,
                    "type": dtype or "Units",
                    "nwb_path": nwb_path,
                    "name": "units",
                    "container": "units",
                    "module": module,
                    "sorting_project": sorting_project,
                    "analysis_data_name": analysis_data_name,
                })
                continue

            label = f"{name} ({module})" if container == "processing" and module else name
            out.append({"label": label, "type": dtype, "nwb_path": nwb_path, "name": name, "container": container, "module": module, "analysis_data_name": name})

        if "units" in accepted_lower:
            if units_rows_seen:
                out.append({
                    "label": "units (all)",
                    "type": "Units",
                    "nwb_path": "units",
                    "name": "units",
                    "container": "units",
                    "module": "",
                    "sorting_project": "",
                    "analysis_data_name": "units (all)",
                })
            else:
                out.append({"label": "units", "type": "units", "nwb_path": "units", "name": "units", "container": "units", "module": "", "sorting_project": "", "analysis_data_name": "units"})

        seen = set()
        clean = []
        for item in out:
            key = (item["label"], item["nwb_path"])
            if key in seen:
                continue
            seen.add(key)
            clean.append(item)
        return sorted(clean, key=lambda d: (d["label"].lower(), d["nwb_path"].lower()))

    def _set_data_choices_from_items(self, data_items):
        self._data_payload_by_label = {}
        choices = []
        for item in data_items:
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            display_label = label
            if display_label in self._data_payload_by_label:
                display_label = f"{label} [{item.get('nwb_path', '')}]"
            self._data_payload_by_label[display_label] = item
            choices.append(display_label)
        self._available_data_choices = choices

    def _clear_data_rows(self):
        self._clear_layout(self.data_rows_layout)
        self._data_row_combos = []

    def _rebuild_data_rows(self, keep_values=None):
        keep_values = list(keep_values or []) or [""]
        self._clear_data_rows()
        self.add_data_btn.setEnabled(bool(self._available_data_choices) and self._allow_multiple_data)
        for val in keep_values:
            self._add_data_row(initial_value=val, trigger_refresh=False)
        self._refresh_data_row_buttons()
        self._queue_availability_refresh()

    def _add_data_row(self, initial_value="", trigger_refresh=True):
        if not self._allow_multiple_data and len(self._data_row_combos) >= 1:
            return
        row = QtWidgets.QWidget(self.data_box)
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        combo = _NoWheelComboBox(row)
        combo.addItem("")
        combo.addItems(list(self._available_data_choices or []))
        if initial_value in self._available_data_choices:
            combo.setCurrentText(initial_value)
        lay.addWidget(combo, stretch=1)
        btn = QtWidgets.QPushButton("Remove", row)
        lay.addWidget(btn)
        self.data_rows_layout.addWidget(row)
        self._data_row_combos.append(combo)
        combo.currentTextChanged.connect(self._queue_availability_refresh)
        btn.clicked.connect(lambda: self._remove_data_row(row, combo))
        self._refresh_data_row_buttons()
        if trigger_refresh:
            self._queue_availability_refresh()

    def _remove_data_row(self, row, combo):
        if len(self._data_row_combos) <= 1:
            return
        if combo in self._data_row_combos:
            self._data_row_combos.remove(combo)
        self.data_rows_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self._refresh_data_row_buttons()
        self._queue_availability_refresh()

    def _refresh_data_row_buttons(self):
        # Button is second widget in each row; disable when row is mandatory.
        for combo in self._data_row_combos:
            row = combo.parentWidget()
            btn = row.layout().itemAt(1).widget() if row and row.layout() and row.layout().count() > 1 else None
            if btn is not None:
                btn.setEnabled(self._allow_multiple_data and len(self._data_row_combos) > 1)

    def _get_selected_data_values(self):
        out = []
        seen = set()
        for cb in self._data_row_combos:
            val = str(cb.currentText()).strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
        return out

    def _get_event_alignment_spec(self):
        spec = self._selected_spec or {}
        raw = spec.get("align_to_event_marker", False)
        if raw is True:
            return {"required": True}
        if raw in (False, None):
            return {"required": False}
        if isinstance(raw, dict):
            out = dict(raw)
            out["required"] = bool(out.get("required", True))
            return out
        return {"required": bool(raw)}

    def _analysis_requires_event_alignment(self):
        return bool(self._get_event_alignment_spec().get("required", False))

    def _compute_available_event_markers(self, files=None):
        if files is None:
            files = self._get_files_from_conditions()
        cdf = self._get_filtered_content_df_for_selected_files(files)
        markers = []
        if files:
            # trials if all/any selected files have trials in inventory/content.
            add_trials = False
            inv = _full_inventory_df(self.GUI_instance)
            if isinstance(inv, pd.DataFrame) and not inv.empty and "file_path" in inv.columns:
                keys = {str(p).replace("\\", "/") for p in files}
                fkeys = inv["file_path"].astype(str).str.replace("\\", "/", regex=False)
                inv2 = inv[fkeys.isin(keys)]
                if "number_of_trials" in inv2.columns:
                    try:
                        add_trials = bool((pd.to_numeric(inv2["number_of_trials"], errors="coerce").fillna(0) > 0).any())
                    except Exception:
                        pass
            if cdf is not None and not cdf.empty:
                low_name = cdf["name"].astype(str).str.strip().str.lower()
                low_path = cdf["nwb_path"].astype(str).str.strip().str.lower()
                if bool(((low_name == "trials") | (low_path == "trials")).any()):
                    add_trials = True
            if add_trials:
                markers.append({"label": "trials", "event_path": "trials", "type": "trials", "kind": "interval"})

        if cdf is None or cdf.empty:
            return markers
        event_types = {"TimeIntervals", "IntervalSeries", "AnnotationSeries", "BehavioralEvents", "BehavioralEpochs", "EventDetection"}
        cdf_evt = cdf[cdf["neurodata_type"].isin(event_types)].copy()
        if cdf_evt.empty:
            return markers
        # The content frame is already restricted to the analysis files.
        # Missing markers are handled per file during extraction, not by hiding
        # otherwise usable choices from the Start/End controls.
        available = {str(name).strip() for name in cdf_evt["name"] if str(name).strip()}
        kind_map = {
            "TimeIntervals": "interval",
            "IntervalSeries": "interval",
            "AnnotationSeries": "point",
            "BehavioralEvents": "behavior_point",
            "BehavioralEpochs": "behavior_interval",
            "EventDetection": "detection",
        }
        for label in sorted(available):
            rows = cdf_evt[cdf_evt["name"].astype(str).str.strip() == str(label)]
            if rows.empty:
                continue
            row = rows.iloc[0]
            ndt = str(row.get("neurodata_type", "")).strip()
            markers.append({"label": label, "event_path": row.get("nwb_path", ""), "type": ndt, "kind": kind_map.get(ndt, "")})
        # unique
        seen = set()
        out = []
        for m in markers:
            lab = str(m.get("label", ""))
            if lab in seen:
                continue
            seen.add(lab)
            out.append(m)
        return out

    def _capture_event_alignment_values(self):
        """Capture editable alignment fields before availability is rebuilt."""
        return {
            "event_marker": str(self.event_marker_combo.currentText()),
            "edge": str(self.event_edge_combo.currentText()),
            "end_event_marker": str(self.end_event_marker_combo.currentText()),
            "end_edge": str(self.end_event_edge_combo.currentText()),
            "start_offset_s": str(self.start_offset_edit.text()),
            "end_offset_s": str(self.end_offset_edit.text()),
        }

    def _rebuild_event_alignment_ui(self, keep_values=None):
        if not self._analysis_requires_event_alignment():
            self.event_box.hide()
            return
        keep_values = dict(keep_values or {})
        markers = self._compute_available_event_markers(self._get_files_from_conditions())
        labels = [m["label"] for m in markers]
        self._event_marker_payload_by_label = {m["label"]: m for m in markers}
        current = str(keep_values.get("event_marker", self.event_marker_combo.currentText()))
        self.event_marker_combo.blockSignals(True)
        self.event_marker_combo.clear()
        self.event_marker_combo.addItems(labels)
        event_spec = self._get_event_alignment_spec()
        default_marker = str(event_spec.get("event_marker", "") or "").strip()
        if current in labels:
            self.event_marker_combo.setCurrentText(current)
        elif default_marker in labels:
            self.event_marker_combo.setCurrentText(default_marker)
        elif labels:
            self.event_marker_combo.setCurrentIndex(0)
        edge = str(
            keep_values.get("edge", event_spec.get("edge", "start")) or "start"
        ).lower()
        if edge in ("start", "stop"):
            self.event_edge_combo.setCurrentText(edge)

        start_value = _alignment_mapping_value(
            keep_values,
            "start_offset_s",
            "pad_before_s",
            negate_legacy=True,
        )
        if start_value is _MISSING_ALIGNMENT_VALUE:
            start_value = _alignment_mapping_value(
                event_spec,
                "start_offset_s",
                "pad_before_s",
                negate_legacy=True,
            )
        if start_value is not _MISSING_ALIGNMENT_VALUE:
            self.start_offset_edit.setText(str(start_value))

        end_value = _alignment_mapping_value(
            keep_values,
            "end_offset_s",
            "pad_after_s",
        )
        if end_value is _MISSING_ALIGNMENT_VALUE:
            end_value = _alignment_mapping_value(
                event_spec,
                "end_offset_s",
                "pad_after_s",
            )
        if end_value is not _MISSING_ALIGNMENT_VALUE:
            self.end_offset_edit.setText(str(end_value))
        self.end_event_marker_combo.blockSignals(True)
        self.end_event_marker_combo.clear()
        self.end_event_marker_combo.addItems(labels)
        # Legacy snapshots have one reference: migrate both boundaries to it.
        end_marker = str(keep_values.get("end_event_marker", "") or
                         event_spec.get("end_event_marker", "") or self.event_marker_combo.currentText())
        if end_marker in labels:
            self.end_event_marker_combo.setCurrentText(end_marker)
        end_edge = str(keep_values.get("end_edge", event_spec.get("end_edge", edge)))
        self.end_event_edge_combo.setCurrentText(end_edge)
        self.end_event_marker_combo.blockSignals(False)
        self.event_marker_combo.blockSignals(False)
        self._on_event_marker_change()
        # Hide section when all required fields are fixed by the analysis spec.
        offsets_fixed = (
            ("start_offset_s" in event_spec or "pad_before_s" in event_spec)
            and ("end_offset_s" in event_spec or "pad_after_s" in event_spec)
        )
        if "event_marker" in event_spec and "edge" in event_spec and offsets_fixed:
            self.event_box.hide()
        else:
            self.event_box.show()

    def _on_event_marker_change(self, *_):
        for marker_combo, edge_combo in (
            (self.event_marker_combo, self.event_edge_combo),
            (self.end_event_marker_combo, self.end_event_edge_combo),
        ):
            payload = self._event_marker_payload_by_label.get(str(marker_combo.currentText()).strip(), {})
            is_interval = str(payload.get("kind", "")).lower() in ("interval", "behavior_interval")
            edge_combo.setEnabled(is_interval)
            if not is_interval:
                edge_combo.setCurrentText("start")
        self._queue_availability_refresh()

    def _find_payload_by_name(self, data_name: str, files=None):
        data_name = str(data_name).strip()
        if not data_name:
            return None
        if data_name in ("trials", "units"):
            return {"label": data_name, "type": data_name, "nwb_path": data_name, "name": data_name, "container": data_name, "module": "", "analysis_data_name": data_name}
        if data_name.lower() in ("units (all)", "all units"):
            return {"label": "units (all)", "type": "Units", "nwb_path": "units", "name": "units", "container": "units", "module": "", "sorting_project": "", "analysis_data_name": "units (all)"}
        cdf = self._get_filtered_content_df_for_selected_files(files)
        if cdf is None or cdf.empty:
            return None
        rows = cdf.copy()
        rows["name"] = rows["name"].astype(str).str.strip()
        rows["container"] = rows.get("container", "").astype(str).str.strip()
        rows["module"] = rows.get("module", "").astype(str).str.strip()
        rows["nwb_path"] = rows["nwb_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        parts = [p.strip() for p in data_name.replace("\\", "/").split("/") if p.strip()]
        if len(parts) == 2:
            container, name = parts
            rows = rows[(rows["container"] == container) & (rows["name"] == name)]
        elif len(parts) >= 3 and parts[0] == "processing":
            _, module, name = parts[0], parts[1], "/".join(parts[2:])
            rows = rows[(rows["container"] == "processing") & (rows["module"] == module) & (rows["name"] == name)]
        else:
            rows = rows[rows["name"] == data_name]
        if rows.empty:
            return None
        row = rows.iloc[0]
        name = str(row.get("name", "")).strip()
        module = str(row.get("module", "")).strip()
        container = str(row.get("container", "")).strip()
        nwb_path = str(row.get("nwb_path", "")).strip().replace("\\", "/")
        dtype = str(row.get("neurodata_type", "")).strip()
        is_units = name.lower() == "units" or container.lower() == "units" or nwb_path.lower().startswith("units") or dtype.lower() == "units"
        if is_units:
            sorting_project = _sorting_project_from_units_row(row)
            if sorting_project:
                label = f"units ({sorting_project})"
                nwb_path = _units_nwb_path_for_project(sorting_project, nwb_path)
                analysis_data_name = label
                module = sorting_project
            else:
                label = "units"
                nwb_path = "units"
                analysis_data_name = "units"
            return {"label": label, "type": dtype or "Units", "nwb_path": nwb_path, "name": "units", "container": "units", "module": module, "sorting_project": sorting_project, "analysis_data_name": analysis_data_name}
        label = f"{name} ({module})" if container == "processing" and module else name
        return {"label": label, "type": dtype, "nwb_path": nwb_path, "name": name, "container": container, "module": module, "analysis_data_name": name}

    def _collect_and_validate_input_data(self):
        spec = self._selected_spec or {}
        input_spec = dict(spec.get("input_spec", {}) or {})
        required = bool(input_spec.get("required", False))
        mandatory = [str(x).strip() for x in (input_spec.get("mandatory_data", []) or []) if str(x).strip()]
        selected = []
        seen = set()
        for label in self._get_selected_data_values():
            payload = self._data_payload_by_label.get(label)
            if payload is None:
                continue
            key = str(payload.get("nwb_path", "")) or str(payload.get("label", ""))
            if key in seen:
                continue
            seen.add(key)
            selected.append(payload)
        for mandatory_name in mandatory:
            payload = self._data_payload_by_label.get(mandatory_name)
            if payload is None:
                for item in self._data_payload_by_label.values():
                    if str(item.get("name", "")).strip() == mandatory_name:
                        payload = item
                        break
            if payload is None:
                payload = self._find_payload_by_name(mandatory_name, files=self._get_files_from_conditions())
            if payload is None:
                self.error_lbl.setText(f"mandatory data not found: {mandatory_name}")
                return None
            key = str(payload.get("nwb_path", "")) or str(payload.get("label", ""))
            if key not in seen:
                seen.add(key)
                selected.append(payload)
        if required and not selected:
            self.error_lbl.setText("input data is required")
            return None
        return selected

    def _collect_and_validate_event_alignment(self):
        event_spec = self._get_event_alignment_spec()
        if not event_spec.get("required", False):
            return _alignment_payload(None, "start", 0.0, 0.0, None)
        event_marker = str(event_spec.get("event_marker", "") or "").strip() or str(self.event_marker_combo.currentText()).strip()
        if not event_marker:
            self.error_lbl.setText("event marker is required")
            return None
        payload = self._event_marker_payload_by_label.get(event_marker, {})
        edge = str(event_spec.get("edge", "") or "").strip().lower()
        if edge not in ("start", "stop"):
            edge = str(self.event_edge_combo.currentText()).strip().lower() or "start"
        try:
            start_value = _alignment_mapping_value(
                event_spec,
                "start_offset_s",
                "pad_before_s",
                negate_legacy=True,
            )
            if start_value is _MISSING_ALIGNMENT_VALUE:
                start_value = self.start_offset_edit.text()
            end_value = _alignment_mapping_value(
                event_spec,
                "end_offset_s",
                "pad_after_s",
            )
            if end_value is _MISSING_ALIGNMENT_VALUE:
                end_value = self.end_offset_edit.text()
            start_offset = float(start_value)
            end_offset = float(end_value)
        except (TypeError, ValueError):
            self.error_lbl.setText("start and end offsets must be numeric")
            return None
        # Old fixed specs retain their one-reference meaning; new specs may fix
        # each boundary independently.
        end_marker = str(event_spec.get("end_event_marker", "") or
                         (event_spec.get("event_marker", "") if "event_marker" in event_spec else "") or
                         self.end_event_marker_combo.currentText()).strip()
        end_edge = str(event_spec.get("end_edge", "") or
                       (event_spec.get("edge", "") if "event_marker" in event_spec else "") or
                       self.end_event_edge_combo.currentText()).strip()
        end_payload = self._event_marker_payload_by_label.get(end_marker, {})
        if not payload.get("event_path") or not end_payload.get("event_path"):
            self.error_lbl.setText("Both start and end event markers are required")
            return None
        result = _alignment_payload(event_marker, edge, start_offset, end_offset, payload.get("event_path"))
        result.update(end_event_marker=end_marker, end_edge=end_edge,
                      end_event_path=end_payload.get("event_path"), alignment_version=2)
        try:
            return normalize_alignment(result)
        except ValueError as exc:
            self.error_lbl.setText(str(exc))
            return None

    # ---------------- params ----------------

    def _normalize_param_entry(self, pname, entry):
        if isinstance(entry, dict) and "type" in entry:
            spec = dict(entry)
            return spec, spec.get("default", None), spec.get("type", None)
        return {}, entry, None

    @staticmethod
    def _row_to_timeseries_ref(row) -> Optional[str]:
        container = str(row.get("container", "")).strip()
        module = str(row.get("module", "")).strip()
        name = str(row.get("name", "")).strip()
        nwb_path = str(row.get("nwb_path", "")).strip().replace("\\", "/").strip("/")
        if nwb_path.startswith("acquisition/"):
            key = nwb_path[len("acquisition/"):].strip("/")
            if key:
                return f"acquisition:{key}"
        if nwb_path.startswith("processing/"):
            parts = [x for x in nwb_path.split("/") if x]
            if len(parts) >= 3:
                return f"processing:{parts[1]}:{'/'.join(parts[2:])}"
        if container == "acquisition" and name:
            return f"acquisition:{name}"
        if container == "processing" and module and name:
            return f"processing:{module}:{name}"
        return None

    def _content_df_for_selection(self):
        cdf = getattr(self.GUI_instance, "content_df", None)
        if cdf is None or not isinstance(cdf, pd.DataFrame) or cdf.empty or "file_path" not in cdf.columns:
            return None
        selected = {str(p) for p in self._get_selected_files() if str(p).lower().endswith(".nwb")}
        if not selected:
            return None
        out = cdf[cdf["file_path"].astype(str).isin(selected)].copy()
        return None if out.empty else out

    def _list_data_refs_for_selection(self, ps=None):
        cdf = self._content_df_for_selection()
        if cdf is None or cdf.empty:
            return []
        allow_types = None
        if ps and ps.get("neurodata_type_in", None) is not None:
            allow_types = {str(x).strip() for x in ps.get("neurodata_type_in", []) if str(x).strip()}
        cdf2 = cdf.copy()
        if allow_types is not None and "neurodata_type" in cdf2.columns:
            cdf2 = cdf2[cdf2["neurodata_type"].astype(str).isin(allow_types)]
        out = []
        for _, row in cdf2.iterrows():
            ref = self._row_to_timeseries_ref(row)
            if ref:
                out.append(ref)
        return sorted(set(out))

    @staticmethod
    def _make_list_display_map(values):
        base = [str(v) for v in values]
        counts = {b: base.count(b) for b in base}
        display = {}
        used = {}
        for v in values:
            b = str(v)
            lab = b if counts[b] == 1 else f"{b} ({type(v).__name__})"
            if lab in display:
                used[lab] = used.get(lab, 1) + 1
                lab = f"{lab} [{used[lab]}]"
            display[lab] = v
        return display

    @staticmethod
    def _make_timeseries_display_map(full_refs):
        mapping = {}
        labels = []
        parsed = []
        for full in full_refs:
            parts = str(full).split(":")
            if len(parts) >= 2 and parts[0] == "acquisition":
                label = ":".join(parts[1:])
            elif len(parts) >= 3 and parts[0] == "processing":
                label = f"{':'.join(parts[2:])} ({parts[1]})"
            else:
                label = str(full)
            labels.append(label)
            parsed.append((full, label))
        counts = {x: labels.count(x) for x in labels}
        for full, label in parsed:
            mapping[label if counts[label] == 1 else str(full)] = str(full)
        return mapping

    def _capture_param_values(self):
        """Return the current parameter values in a form that survives a UI rebuild."""
        values = {}
        for pname, widget in self.param_widgets.items():
            ps = self.param_specs.get(pname, {})
            ptype = str(ps.get("type", "str"))

            if isinstance(widget, QtWidgets.QCheckBox):
                values[pname] = {"kind": "bool", "value": widget.isChecked()}
                continue

            if isinstance(widget, QtWidgets.QComboBox):
                display_value = widget.currentText()
                if ptype in ("choice", "list"):
                    display_map = self._list_display_to_value_by_param.get(pname, {})
                    if display_value in display_map:
                        values[pname] = {
                            "kind": "mapped",
                            "value": copy.deepcopy(display_map[display_value]),
                        }
                    else:
                        values[pname] = {"kind": "display", "value": display_value}
                elif ptype in ("nwb_timeseries", "data"):
                    display_map = self._ts_display_to_full_by_param.get(pname, {})
                    values[pname] = {
                        "kind": "mapped",
                        "value": display_map.get(display_value, display_value),
                    }
                else:
                    values[pname] = {"kind": "display", "value": display_value}
                continue

            values[pname] = {"kind": "text", "value": widget.text()}
        return values

    @staticmethod
    def _set_combo_to_mapped_value(combo, display_map, wanted_value):
        for display_value, real_value in display_map.items():
            try:
                matches = bool(real_value == wanted_value)
            except Exception:
                matches = False
            if matches:
                combo.setCurrentText(display_value)
                return True
        return False

    def _restore_param_widget_value(self, pname, widget, saved_value):
        if not isinstance(saved_value, dict) or "value" not in saved_value:
            return

        kind = saved_value.get("kind")
        value = saved_value.get("value")
        ptype = str(self.param_specs.get(pname, {}).get("type", "str"))

        if isinstance(widget, QtWidgets.QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QtWidgets.QComboBox):
            if kind == "mapped" and ptype in ("choice", "list"):
                display_map = self._list_display_to_value_by_param.get(pname, {})
                self._set_combo_to_mapped_value(widget, display_map, value)
            elif kind == "mapped" and ptype in ("nwb_timeseries", "data"):
                display_map = self._ts_display_to_full_by_param.get(pname, {})
                self._set_combo_to_mapped_value(widget, display_map, value)
            elif str(value) in [widget.itemText(i) for i in range(widget.count())]:
                widget.setCurrentText(str(value))
        else:
            widget.setText("" if value is None else str(value))

    def _build_params_ui(self, spec: dict, keep_values=None):
        self._clear_layout(self.params_layout)
        self.param_widgets = {}
        self.param_specs = {}
        self._ts_display_to_full_by_param = {}
        self._list_display_to_value_by_param = {}
        raw = spec.get("params", {}) if isinstance(spec, dict) else {}
        if not isinstance(raw, dict) or not raw:
            self.params_box.hide()
            return
        for pname, entry in raw.items():
            ps, default_value, ptype = self._normalize_param_entry(pname, entry)
            if not ps:
                ps = {"type": "str", "default": default_value}
                ptype = "str"
            ptype = str(ptype or ps.get("type", "str") or "str")
            ps["type"] = ptype
            self.param_specs[pname] = ps
            label = str(ps.get("label", pname))
            required = bool(ps.get("required", False))

            row = QtWidgets.QWidget(self.params_box)
            lay = QtWidgets.QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)

            if ptype == "bool":
                chk = QtWidgets.QCheckBox(label)
                chk.setChecked(bool(ps.get("default", False)))
                self.param_widgets[pname] = chk
                if pname in (keep_values or {}):
                    self._restore_param_widget_value(pname, chk, keep_values[pname])
                chk.stateChanged.connect(self._queue_availability_refresh)
                lay.addWidget(chk)
                lay.addStretch(1)
                self.params_layout.addWidget(row)
                continue

            lab = QtWidgets.QLabel(label + (" *" if required else ""))
            lab.setMinimumWidth(150)
            lay.addWidget(lab)

            if ptype in ("choice", "list"):
                values = list(ps.get("choices", []) or []) if ptype == "choice" else list(ps.get("values", []) or [])
                display_map = self._make_list_display_map(values)
                self._list_display_to_value_by_param[pname] = display_map
                cb = _NoWheelComboBox()
                cb.addItems(list(display_map.keys()))
                default = ps.get("default", values[0] if values else "")
                for disp, real in display_map.items():
                    if real == default:
                        cb.setCurrentText(disp)
                        break
                self.param_widgets[pname] = cb
                if pname in (keep_values or {}):
                    self._restore_param_widget_value(pname, cb, keep_values[pname])
                cb.currentTextChanged.connect(self._queue_availability_refresh)
                lay.addWidget(cb, stretch=1)

            elif ptype in ("nwb_timeseries", "data"):
                refs = [str(x) for x in self._list_data_refs_for_selection(ps) if str(x).strip()]
                display_map = self._make_timeseries_display_map(refs)
                self._ts_display_to_full_by_param[pname] = display_map
                cb = _NoWheelComboBox()
                cb.addItems(list(display_map.keys()))
                raw_default = str(ps.get("default", "") or "").strip()
                if raw_default in refs:
                    for disp, full in display_map.items():
                        if full == raw_default:
                            cb.setCurrentText(disp)
                            break
                self.param_widgets[pname] = cb
                if pname in (keep_values or {}):
                    self._restore_param_widget_value(pname, cb, keep_values[pname])
                cb.currentTextChanged.connect(self._queue_availability_refresh)
                lay.addWidget(cb, stretch=1)

            else:
                ent = QtWidgets.QLineEdit("" if default_value is None else str(default_value))
                self.param_widgets[pname] = ent
                if pname in (keep_values or {}):
                    self._restore_param_widget_value(pname, ent, keep_values[pname])
                ent.textChanged.connect(self._queue_availability_refresh)
                lay.addWidget(ent, stretch=1)

            self.params_layout.addWidget(row)
        self.params_box.show()

    def _collect_and_validate_params(self):
        params = {}
        errors = []
        for pname, widget in self.param_widgets.items():
            ps = self.param_specs.get(pname, {})
            ptype = str(ps.get("type", "str"))
            required = bool(ps.get("required", False))
            if isinstance(widget, QtWidgets.QCheckBox):
                raw = widget.isChecked()
            elif isinstance(widget, QtWidgets.QComboBox):
                raw = widget.currentText()
            else:
                raw = widget.text()
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
                elif ptype in ("choice", "list"):
                    display_map = self._list_display_to_value_by_param.get(pname, {})
                    if str(raw) not in display_map:
                        errors.append(f"{pname} must be one of the allowed values")
                        continue
                    val = display_map[str(raw)]
                elif ptype in ("nwb_timeseries", "data"):
                    display_map = self._ts_display_to_full_by_param.get(pname, {})
                    val = display_map.get(str(raw), "")
                else:
                    val = raw
                if ptype in ("int", "float") and val is not None:
                    vf = float(val)
                    if "min" in ps and vf < float(ps["min"]):
                        errors.append(f"{pname} must be >= {ps['min']}")
                    if "max" in ps and vf > float(ps["max"]):
                        errors.append(f"{pname} must be <= {ps['max']}")
                params[pname] = val
            except Exception:
                errors.append(f"invalid value for {pname}")
        if errors:
            self.error_lbl.setText("; ".join(errors))
            return None
        return params

    # ---------------- availability/run ----------------

    def refresh_from_filters(self):
        spec = self._selected_spec
        if not spec:
            return
        scroll_value = self._details_scroll_value()
        keep_params = self._capture_param_values()
        keep_event = self._capture_event_alignment_values()
        files = self._get_files_from_conditions()
        keep = self._get_selected_data_values()
        self._set_data_choices_from_items(self._list_available_data(spec, files=files))
        if self._should_show_data_panel(spec):
            self._rebuild_data_rows(keep_values=keep)
        self._rebuild_event_alignment_ui(keep_values=keep_event)
        self._build_params_ui(spec, keep_values=keep_params)
        self._refresh_availability_display()
        self._restore_details_scroll(scroll_value)

    def _queue_availability_refresh(self, *_):
        self._availability_timer.start()

    def _refresh_availability_display(self):
        spec = self._selected_spec
        if not spec:
            self.run_btn.setEnabled(False)
            self.add_pipeline_btn.setEnabled(False)
            self.available_lbl.setText("No analysis selected")
            return
        errors = []
        if self._running:
            errors.append("analysis is already running")
        if self._pipeline_run is not None:
            errors.append("an analysis pipeline is running")
        if not callable(spec.get("func", None)):
            errors.append("analysis function is missing")
        params = self._collect_and_validate_params()
        if params is None:
            errors.append(self.error_lbl.text() or "invalid parameters")
        input_data = self._collect_and_validate_input_data()
        if input_data is None:
            errors.append(self.error_lbl.text() or "invalid input data")
        if self._analysis_requires_event_alignment():
            event_alignment = self._collect_and_validate_event_alignment()
            if event_alignment is None:
                errors.append(self.error_lbl.text() or "invalid event alignment")
        if self.save_analysis_chk.isChecked() and not self._sanitize_folder_name(self.analysis_name_edit.text()):
            errors.append("analysis folder name is required")
        can_run = not errors
        self.run_btn.setEnabled(can_run)
        self.add_pipeline_btn.setEnabled(can_run)
        self.run_btn.setToolTip("")
        if can_run:
            self.available_lbl.setText("Ready")
            self.error_lbl.setText("")
        else:
            # Avoid repeating long validation messages endlessly.
            clean = []
            for e in errors:
                if e and e not in clean:
                    clean.append(e)
            self.available_lbl.setText("Cannot run: " + "; ".join(clean))

    def _run_selected(self, _from_pipeline: bool = False):
        if self._running or (self._pipeline_run is not None and not _from_pipeline):
            return False
        if _from_pipeline:
            name = self._selected_name
            spec = self._selected_spec
        else:
            name, spec = self._get_selected_analysis_spec()
            if not spec or not name:
                name = self._selected_name
                spec = self._selected_spec
        if not spec or not name:
            message = "No analysis selected."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            else:
                QtWidgets.QMessageBox.critical(self, "Analysis", message)
            return False
        if not callable(spec.get("func", None)):
            message = f"No function attached for '{name}'."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            else:
                QtWidgets.QMessageBox.critical(self, "Analysis", message)
            return False
        params = self._collect_and_validate_params()
        if params is None:
            if _from_pipeline:
                self._complete_pipeline_step(False, self.error_lbl.text() or "Invalid analysis parameters.", "")
            return False
        event_alignment = self._collect_and_validate_event_alignment()
        if event_alignment is None:
            if _from_pipeline:
                self._complete_pipeline_step(False, self.error_lbl.text() or "Invalid event alignment.", "")
            return False
        input_data = self._collect_and_validate_input_data()
        if input_data is None:
            if _from_pipeline:
                self._complete_pipeline_step(False, self.error_lbl.text() or "Invalid input data.", "")
            return False
        if not self.condition_section.has_valid_conditions():
            message = "No valid conditions defined. Each condition needs a name and at least one file."
            if _from_pipeline:
                self._complete_pipeline_step(False, message, "")
            else:
                QtWidgets.QMessageBox.information(self, "Analysis", message)
            return False

        condition_selection = self.condition_section.get_conditions_dict()
        data_names = []
        data_paths = []
        seen = set()
        for payload in input_data:
            data_name = str(
                payload.get("analysis_data_name", payload.get("name", ""))
            ).strip()
            path = str(payload.get("nwb_path", "")).strip()
            key = (data_name, path)
            if data_name and key not in seen:
                seen.add(key)
                data_names.append(data_name)
                data_paths.append(path)

        if not _from_pipeline:
            cache_request = {
                "context": str(name),
                "condition_filter": condition_selection,
                "data_types": data_names,
                "align_to_events": self._spec_requires_event_alignment(spec),
                "event_alignment": event_alignment,
            }
            if not self._confirm_stale_preprocessed_data([cache_request]):
                return False

        transfer = Analysis_data_transfer(self.GUI_instance, self, name)
        transfer.tool_params = dict(params)
        transfer.tool_param_specs = dict(self.param_specs or {})
        transfer.selected_files = list(self.selected_files)
        transfer.save_analysis = bool(self.save_analysis_chk.isChecked())
        transfer.save_metadata = True
        transfer.display_analysis_info = True
        transfer.analysis_save_name = self._sanitize_folder_name(self.analysis_name_edit.text())
        if _from_pipeline:
            pipeline = self.pipeline_store.get((self._pipeline_run or {}).get("pipeline_id"))
            plot_options = (pipeline or {}).get("plot_options", {}) or {}
            transfer.launch_plotting_GUI_after_analysis = False
            transfer.figure_per_layer = int(plot_options.get("figures_per_layout", 4))
            transfer.figure_row = int(plot_options.get("number_of_rows", 2))
        else:
            transfer.launch_plotting_GUI_after_analysis = bool(self.auto_plot_chk.isChecked())
            transfer.figure_per_layer = int(self.figure_per_layer_spin.value())
            transfer.figure_row = int(self.figure_row_spin.value())
        transfer.debug_mode = bool(spec.get("debug_mode", False))

        if transfer.save_analysis:
            if _from_pipeline:
                pipeline = self.pipeline_store.get((self._pipeline_run or {}).get("pipeline_id"))
                pipeline_output_root = str((self._pipeline_run or {}).get("output_root", "") or "")
                if pipeline_output_root:
                    output_root = Path(pipeline_output_root)
                else:
                    output_root = self._get_pipeline_analysis_output_root(pipeline)
            else:
                output_root = self._get_analysis_output_root()
            output_dir = output_root / transfer.analysis_save_name
            if output_dir.exists():
                if _from_pipeline:
                    self.log_message.emit(f"Output folder already exists; continuing in: {output_dir}")
                else:
                    msg = QtWidgets.QMessageBox(self)
                    msg.setWindowTitle("Analysis")
                    msg.setText(f"The folder already exists:\n\n{output_dir}\n\nWhat do you want to do?")
                    continue_btn = msg.addButton("Continue", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
                    delete_btn = msg.addButton("Delete", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
                    cancel_btn = msg.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
                    msg.exec()
                    clicked = msg.clickedButton()
                    if clicked is cancel_btn:
                        return False
                    if clicked is delete_btn:
                        try:
                            shutil.rmtree(output_dir)
                        except Exception as exc:
                            QtWidgets.QMessageBox.critical(self, "Analysis", f"Could not delete folder:\n{exc}")
                            return False
                    elif clicked is continue_btn:
                        pass
            transfer.analysis_output_dir = output_dir

        transfer.data_type = data_names
        transfer.data_path = data_paths
        transfer.align_to_event = self._analysis_requires_event_alignment()
        transfer.event_alignment = event_alignment
        transfer.condition_selection = condition_selection

        self._set_running(True, name)
        self.run_btn.setEnabled(False)
        self.log_reset.emit(f"Launching analysis: {name}")
        if not _from_pipeline:
            self.right_panel.show_analysis_view()

        run_analysis(
            transfer=transfer,
            analysis_spec=spec,
            params=params,
            ui=self.GUI_instance,
            log_callback=self.log_message.emit,
            progress_callback=self.progress_update.emit,
            done_callback=self.analysis_finished.emit,
            error_callback=self.analysis_failed.emit,
            debug_mode=bool(getattr(transfer, "debug_mode", False)),
        )
        return True


    def _reset_progress_on_gui_thread(self, text="Idle"):
        try:
            self.progress_status_lbl.setText(str(text or "Idle"))
        except Exception:
            pass

    def _update_progress_on_gui_thread(self, payload):
        if not isinstance(payload, dict):
            payload = {"text": str(payload)}

        if payload.get("reset"):
            self._reset_progress_on_gui_thread(payload.get("text", "Starting"))
            return

        text = str(payload.get("text") or payload.get("status") or "")
        if text:
            try:
                self.progress_status_lbl.setText(text)
            except Exception:
                pass

    def _reset_log_on_gui_thread(self, text):
        title_text = str(text or "")
        if self._pipeline_run is not None:
            existing = str(self._pipeline_run.get("step_log", "") or "")
            captured = title_text
            if existing.strip() and existing.strip() != title_text.strip():
                captured += "\n\n" + existing
            self._set_running_step_log(captured)
            self._append_pipeline_run_log(title_text)
            if self._selected_step_is_running_step():
                self.log_edit.setPlainText(captured)
            return
        self.log_edit.setPlainText(title_text)
        if title_text:
            self.log_edit.appendPlainText("")

    def _append_log_on_gui_thread(self, text):
        message = str(text)
        if self._pipeline_run is not None:
            existing = str(self._pipeline_run.get("step_log", "") or "")
            captured = existing + ("\n" if existing else "") + message
            self._set_running_step_log(captured)
            self._append_pipeline_run_log(message)
            if not self._selected_step_is_running_step():
                return
            self.log_edit.setPlainText(captured)
        else:
            self.log_edit.appendPlainText(message)
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def _on_analysis_finished(self, result):
        step_log = self.log_edit.toPlainText()
        pipeline_running = self._pipeline_run is not None
        completed_name = self._selected_name
        completed_files = list(self.selected_files)
        if pipeline_running:
            running_step = self._running_pipeline_step()
            running_snapshot = (running_step or {}).get("snapshot", {}) or {}
            completed_name = str(running_snapshot.get("analysis_name", completed_name or "Analysis"))
            completed_files = list(running_snapshot.get("selected_files", completed_files) or [])
            active_step_id = str(self._pipeline_run.get("active_step_id") or "")
            if active_step_id:
                self._pipeline_results[active_step_id] = result
        self._set_running(False)
        has_figures = bool(getattr(result, "figures", None))
        if not pipeline_running:
            self._last_result = result
            self.plot_btn.setEnabled(has_figures)
        self._refresh_availability_display()

        if has_figures and not pipeline_running:
            auto_launch = bool(self.auto_plot_chk.isChecked())
            figures_per_layout = int(self.figure_per_layer_spin.value())
            number_of_rows = int(self.figure_row_spin.value())
            # Let the analysis-finished signal return before creating GUI
            # backend managers.  This keeps all window creation in a clean Qt
            # event-loop turn and makes display independent from the Plot GUI.
            QtCore.QTimer.singleShot(
                0,
                lambda completed_result=result,
                       auto_launch=auto_launch,
                       figures_per_layout=figures_per_layout,
                       number_of_rows=number_of_rows: self._display_completed_figures(
                    completed_result,
                    context="analysis",
                    open_plot_gui=auto_launch,
                    figures_per_layout=figures_per_layout,
                    number_of_rows=number_of_rows,
                ),
            )
        elif has_figures:
            self.log_message.emit(
                "Figures stored for this pipeline step. All pipeline figures "
                "will be displayed automatically when the pipeline finishes."
            )

        if callable(self.on_run_done):
            try:
                self.on_run_done(completed_name, completed_files)
            except Exception:
                pass

        if self.preprocessed_data_window.isVisible():
            QtCore.QTimer.singleShot(0, self.preprocessed_data_manager.refresh)

        if pipeline_running and self._pipeline_run is not None:
            self._complete_pipeline_step(True, "", step_log)

    def _on_analysis_failed(self, _tb):
        step_log = self.log_edit.toPlainText()
        self._set_running(False)
        self._refresh_availability_display()
        if self._pipeline_run is not None:
            self._complete_pipeline_step(False, str(_tb or "Analysis failed."), step_log)

    def _open_plotting_gui(self):
        if self._last_result is not None and getattr(self._last_result, "figures", None):
            try:
                launch_plotting_gui(
                    self._last_result,
                    nb_fig_layer=int(self.figure_per_layer_spin.value()),
                    nb_of_row=int(self.figure_row_spin.value()),
                    log_callback=self.log_message.emit,
                )
            except Exception as exc:
                self.log_message.emit(f"Could not open plotting GUI: {exc}")
            return

        try:
            import matplotlib.pyplot as plt
            figs = plt.get_fignums()
        except Exception:
            figs = []
        if not figs:
            self.log_message.emit("No figures available")
            return

        # Fallback for analyses that left figures only in pyplot. This path is
        # intentionally used only when no AnalysisResult figures exist.
        try:
            from matplotlib._pylab_helpers import Gcf
            figures = [
                getattr(getattr(manager, "canvas", None), "figure", None)
                for manager in list(Gcf.get_all_fig_managers())
            ]
            figures = [figure for figure in figures if figure is not None]
            launch_plotting_gui(
                SimpleNamespace(figures=figures),
                nb_fig_layer=int(self.figure_per_layer_spin.value()),
                nb_of_row=int(self.figure_row_spin.value()),
                log_callback=self.log_message.emit,
            )
        except Exception as exc:
            self.log_message.emit(f"Could not open plotting GUI: {exc}")


# DELIVERY_BUILD_ID: AP-20260812-01
