from __future__ import annotations
from GUI.project_paths import json_dump as _pp_json_dump

import ast
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from GUI.qt_compat import QtCore, QtGui, QtWidgets, qt_checked, qt_unchecked, qt_user_role

try:
    import h5py  # type: ignore
except Exception:  # pragma: no cover
    h5py = None  # type: ignore

try:
    from pynwb import NWBHDF5IO  # type: ignore
except Exception:  # pragma: no cover
    NWBHDF5IO = None  # type: ignore


class NewSpikeSortingDialog(QtWidgets.QDialog):
    """Qt version of the Tk NewSpikeSortingDialog.

    It writes the same sorting_recipe.json and preprocessing_pipeline.json files
    as the Tk dialog, so the backend recipe mechanism stays unchanged.
    """

    def __init__(
        self,
        parent=None,
        *,
        files=None,
        subject_lookup: Optional[Callable[[str], str]] = None,
        on_confirm: Optional[Callable[[dict], None]] = None,
        project_path=None,
        GUI_instance=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("New spike sorting")
        self.resize(900, 600)
        self.setMinimumSize(720, 480)

        self.files = list(dict.fromkeys(str(p) for p in (files or []) if p))
        self.subject_lookup = subject_lookup or (lambda _p: "")
        self.on_confirm = on_confirm
        self.project_path = Path(project_path) if project_path is not None else None
        self.GUI_instance = GUI_instance

        self._data_payload_by_label = {}
        self._available_data_choices = []
        self._grouping_field_specs_by_key = {}
        self.selected_grouping_keys = []
        self._grouping_default_initialized = False
        self._session_start_cache = {}
        self._confirm_busy = False

        self._build_ui()
        self._refresh_data_choices()

    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(QtWidgets.QLabel("Sorting name:"))
        self.name_edit = QtWidgets.QLineEdit(self)
        name_row.addWidget(self.name_edit, stretch=1)
        outer.addLayout(name_row)

        self.tabs = QtWidgets.QTabWidget(self)
        outer.addWidget(self.tabs, stretch=1)

        files_tab = QtWidgets.QWidget(self.tabs)
        self.tabs.addTab(files_tab, "Files")
        self._build_files_tab(files_tab)

        opts = QtWidgets.QGroupBox("Options", self)
        opts_layout = QtWidgets.QVBoxLayout(opts)
        opts_layout.setSpacing(6)

        concat_row = QtWidgets.QHBoxLayout()
        concat_row.setSpacing(6)
        self.concatenate_check = QtWidgets.QCheckBox("Concatenate recordings by file filter field(s)", opts)
        self.grouping_fields_btn = QtWidgets.QPushButton("Grouping fields...", opts)
        self.grouping_fields_btn.setMaximumWidth(160)
        self.grouping_summary_lbl = QtWidgets.QLabel("", opts)
        self.grouping_summary_lbl.setWordWrap(True)
        self.grouping_summary_lbl.setStyleSheet("color: #555;")
        concat_row.addWidget(self.concatenate_check)
        concat_row.addWidget(self.grouping_fields_btn)
        concat_row.addWidget(self.grouping_summary_lbl, stretch=1)
        opts_layout.addLayout(concat_row)

        self.remove_bad_channels_check = QtWidgets.QCheckBox("Remove bad channels", opts)
        opts_layout.addWidget(self.remove_bad_channels_check)
        outer.addWidget(opts)

        self.concatenate_check.toggled.connect(self._on_concatenate_toggled)
        self.grouping_fields_btn.clicked.connect(self._open_grouping_fields_dialog)
        self._refresh_grouping_controls()

        bottom_row = QtWidgets.QHBoxLayout()
        bottom_row.setSpacing(8)

        self.confirm_status_lbl = QtWidgets.QLabel("", self)
        self.confirm_status_lbl.setStyleSheet("color: #555;")
        self.confirm_status_lbl.setMinimumWidth(170)
        self.confirm_status_lbl.setVisible(False)
        bottom_row.addWidget(self.confirm_status_lbl)

        self.confirm_progress = QtWidgets.QProgressBar(self)
        self.confirm_progress.setRange(0, 0)  # indeterminate while the recipe is being prepared
        self.confirm_progress.setFixedWidth(150)
        self.confirm_progress.setTextVisible(False)
        self.confirm_progress.setVisible(False)
        bottom_row.addWidget(self.confirm_progress)
        bottom_row.addStretch(1)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        try:
            self.confirm_btn = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
            self.confirm_btn.setText("Confirm")
        except Exception:
            self.confirm_btn = None
        try:
            self.cancel_btn = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        except Exception:
            self.cancel_btn = None
        self.buttons.accepted.connect(self._confirm)
        self.buttons.rejected.connect(self.reject)
        bottom_row.addWidget(self.buttons)
        outer.addLayout(bottom_row)

        self.name_edit.setFocus()

    def _set_confirm_busy(self, busy: bool, text: str = ""):
        self._confirm_busy = bool(busy)
        try:
            self.confirm_progress.setVisible(bool(busy))
        except Exception:
            pass
        try:
            self.confirm_status_lbl.setText(str(text or ""))
            self.confirm_status_lbl.setVisible(bool(busy) and bool(text))
        except Exception:
            pass
        try:
            if self.confirm_btn is not None:
                self.confirm_btn.setEnabled(not bool(busy))
        except Exception:
            pass
        try:
            if self.cancel_btn is not None:
                self.cancel_btn.setEnabled(not bool(busy))
        except Exception:
            pass
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

    def _set_confirm_status(self, text: str):
        try:
            self.confirm_status_lbl.setText(str(text or ""))
            self.confirm_status_lbl.setVisible(bool(text))
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

    def _build_files_tab(self, tab):
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        note = QtWidgets.QLabel("Files selected in the main GUI will be used for this spike sorting.")
        layout.addWidget(note)

        self.files_table = QtWidgets.QTableWidget(tab)
        self.files_table.setColumnCount(3)
        self.files_table.setHorizontalHeaderLabels(["File", "Subject", "Data present"])
        self.files_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.files_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.files_table, stretch=1)

        data_row = QtWidgets.QHBoxLayout()
        data_row.addWidget(QtWidgets.QLabel("Data for sorting:"))
        self.data_combo = QtWidgets.QComboBox(tab)
        self.data_combo.currentIndexChanged.connect(self._on_data_change)
        data_row.addWidget(self.data_combo, stretch=1)
        layout.addLayout(data_row)

        self.data_warning_lbl = QtWidgets.QLabel("")
        self.data_warning_lbl.setWordWrap(True)
        self.data_warning_lbl.setStyleSheet("color: #b00020;")
        layout.addWidget(self.data_warning_lbl)

    def update_files(self, files):
        self.files = list(dict.fromkeys(str(p) for p in (files or []) if p))
        self._refresh_data_choices()
        self._refresh_grouping_controls()

    # ---------- data discovery copied from Tk dialog behavior ----------

    @staticmethod
    def _normalize_path_key(path):
        return str(path).strip().replace("\\", "/")

    def _get_selected_nwb_files(self):
        out = []
        seen = set()
        for p in self.files:
            s = str(p).strip()
            if not s or not s.lower().endswith(".nwb"):
                continue
            key = self._normalize_path_key(s)
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    def _get_content_df_for_selected_files(self):
        gui = self.GUI_instance
        if gui is None:
            return None
        cdf = getattr(gui, "content_df", None)
        if cdf is None or getattr(cdf, "empty", True):
            return None
        needed = {"file_path", "nwb_path", "name", "neurodata_type"}
        if not needed.issubset(set(cdf.columns)):
            return None
        selected_files = self._get_selected_nwb_files()
        if not selected_files:
            return cdf.iloc[0:0].copy()
        selected_keys = {self._normalize_path_key(p) for p in selected_files if str(p).strip()}
        file_keys = cdf["file_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        cdf2 = cdf[file_keys.isin(selected_keys)].copy()
        if cdf2.empty:
            return cdf2
        cdf2["file_path"] = cdf2["file_path"].astype(str).str.strip()
        cdf2["file_key"] = cdf2["file_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        cdf2["nwb_path"] = cdf2["nwb_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
        cdf2["name"] = cdf2["name"].astype(str).str.strip()
        cdf2["neurodata_type"] = cdf2["neurodata_type"].astype(str).str.strip()
        if "container" not in cdf2.columns:
            cdf2["container"] = ""
        if "module" not in cdf2.columns:
            cdf2["module"] = ""
        cdf2["container"] = cdf2["container"].astype(str).str.strip()
        cdf2["module"] = cdf2["module"].astype(str).str.strip()
        return cdf2

    @staticmethod
    def _make_data_key(row):
        return (
            str(row.get("name", "")).strip(),
            str(row.get("nwb_path", "")).strip().replace("\\", "/"),
            str(row.get("neurodata_type", "")).strip(),
            str(row.get("container", "")).strip(),
            str(row.get("module", "")).strip(),
        )

    @staticmethod
    def _make_data_label(row):
        name = str(row.get("name", "")).strip()
        container = str(row.get("container", "")).strip()
        module = str(row.get("module", "")).strip()
        if not name:
            return ""
        if container == "processing" and module:
            return f"{name} ({module})"
        return name

    def _list_available_sorting_data(self):
        cdf = self._get_content_df_for_selected_files()
        if cdf is None or cdf.empty:
            return []
        selected_files = self._get_selected_nwb_files()
        if not selected_files:
            return []
        selected_file_keys = {self._normalize_path_key(p) for p in selected_files}
        allowed_types = {"ElectricalSeries", "TimeSeries"}
        rows = cdf[cdf["neurodata_type"].astype(str).isin(allowed_types)].copy()
        if rows.empty:
            return []
        by_key = {}
        for _, row in rows.iterrows():
            key = self._make_data_key(row)
            label = self._make_data_label(row)
            if not label:
                continue
            file_key = str(row.get("file_key", "")).strip()
            if not file_key:
                continue
            if key not in by_key:
                by_key[key] = {
                    "label": label,
                    "name": str(row.get("name", "")).strip(),
                    "type": str(row.get("neurodata_type", "")).strip(),
                    "nwb_path": str(row.get("nwb_path", "")).strip(),
                    "container": str(row.get("container", "")).strip(),
                    "module": str(row.get("module", "")).strip(),
                    "present_file_keys": set(),
                    "selected_n_files": len(selected_file_keys),
                }
            by_key[key]["present_file_keys"].add(file_key)
        out = list(by_key.values())
        for item in out:
            item["present_file_keys"] = set(item["present_file_keys"])
            item["present_n_files"] = len(item["present_file_keys"])
        out.sort(key=lambda d: (str(d.get("label", "")).lower(), str(d.get("nwb_path", "")).lower()))
        return out

    def _refresh_data_choices(self):
        previous = self.data_combo.currentText().strip() if hasattr(self, "data_combo") else ""
        data_items = self._list_available_sorting_data()
        self._data_payload_by_label = {}
        choices = []
        base_counts = {}
        for item in data_items:
            base = str(item.get("label", "")).strip()
            if base:
                base_counts[base] = base_counts.get(base, 0) + 1
        for item in data_items:
            label = str(item.get("label", "")).strip()
            if not label:
                continue
            present_n = int(item.get("present_n_files", 0))
            selected_n = int(item.get("selected_n_files", 0))
            if base_counts.get(label, 0) > 1:
                display_label = f"{label} [{item.get('nwb_path', '')}] ({present_n}/{selected_n} files)"
            else:
                display_label = f"{label} ({present_n}/{selected_n} files)"
            self._data_payload_by_label[display_label] = item
            choices.append(display_label)
        self._available_data_choices = choices
        self.data_combo.blockSignals(True)
        self.data_combo.clear()
        self.data_combo.addItems(choices)
        if previous in choices:
            self.data_combo.setCurrentText(previous)
        elif choices:
            self.data_combo.setCurrentIndex(0)
        self.data_combo.blockSignals(False)
        self._refresh_files_table()

    def _data_present_in_file(self, path, data_payload):
        file_key = self._normalize_path_key(path)
        present_file_keys = data_payload.get("present_file_keys", set())
        return file_key in present_file_keys

    def _on_data_change(self, *_):
        self._refresh_files_table()

    def _refresh_files_table(self):
        selected_data_label = self.data_combo.currentText().strip()
        selected_data_payload = self._data_payload_by_label.get(selected_data_label)

        self.files_table.setRowCount(0)
        if not self.files:
            self.files_table.setRowCount(1)
            self.files_table.setItem(0, 0, QtWidgets.QTableWidgetItem("No file selected in the main file tree."))
            self.files_table.setItem(0, 1, QtWidgets.QTableWidgetItem(""))
            self.files_table.setItem(0, 2, QtWidgets.QTableWidgetItem(""))
            self.data_warning_lbl.setText("")
            return

        missing_count = 0
        self.files_table.setRowCount(len(self.files))
        for r, path in enumerate(self.files):
            subject = self.subject_lookup(path)
            if selected_data_payload:
                present = self._data_present_in_file(path, selected_data_payload)
                data_present_text = "True" if present else "False"
                if not present:
                    missing_count += 1
            else:
                present = True
                data_present_text = ""
            values = [path, subject, data_present_text]
            for c, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                if selected_data_payload and not present:
                    item.setBackground(QtGui.QColor("#ffe5e5"))
                    item.setForeground(QtGui.QColor("#b00020"))
                self.files_table.setItem(r, c, item)
        self.files_table.resizeColumnsToContents()
        if selected_data_payload and missing_count > 0:
            self.data_warning_lbl.setText(
                f"Selected data is missing in {missing_count} selected file(s). Rows marked False will not have this data."
            )
        else:
            self.data_warning_lbl.setText("")

    # ---------- grouping fields for concatenation ----------

    @staticmethod
    def _safe_text(value):
        try:
            if value is None:
                return ""
            # pandas/numpy NaN without importing pandas here
            if value != value:
                return ""
        except Exception:
            pass
        text = str(value).strip()
        if text.lower() in ("nan", "none", "nat"):
            return ""
        return text


    @staticmethod
    def _natural_sort_key(value):
        text = str(value or "")
        parts = re.split(r"(\d+)", text.lower())
        key = []
        for part in parts:
            if part.isdigit():
                try:
                    key.append((0, int(part)))
                except Exception:
                    key.append((1, part))
            else:
                key.append((1, part))
        return key

    @staticmethod
    def _bool_from_inventory_value(value):
        text = NewSpikeSortingDialog._safe_text(value).lower()
        if text in ("", "nan", "none", "nat"):
            return False
        if text in ("1", "true", "yes", "y", "bad"):
            return True
        if text in ("0", "false", "no", "n", "good"):
            return False
        try:
            return bool(int(float(text)))
        except Exception:
            return False

    def _inventory_df(self):
        gui = self.GUI_instance
        if gui is None:
            return None
        for attr in ("full_df", "df", "NWB_inventory_df"):
            df = getattr(gui, attr, None)
            if df is not None and not getattr(df, "empty", True) and hasattr(df, "columns"):
                return df
        return None

    def _inventory_row_for_path(self, path):
        df = self._inventory_df()
        if df is None or "file_path" not in getattr(df, "columns", []):
            return None
        try:
            key = self._normalize_path_key(path)
            series = df["file_path"].astype(str).str.strip().str.replace("\\", "/", regex=False)
            hit = df[series == key]
            if hit.empty:
                # Last fallback: exact string comparison for paths that already use a platform style.
                hit = df[df["file_path"].astype(str) == str(path)]
            if hit.empty:
                return None
            return hit.iloc[0]
        except Exception:
            return None

    def _parse_list_cell(self, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [self._safe_text(v) for v in value if self._safe_text(v)]
        text = self._safe_text(value)
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
                if isinstance(parsed, (list, tuple, set)):
                    return [self._safe_text(v) for v in parsed if self._safe_text(v)]
            except Exception:
                pass
        return [text]

    def _add_grouping_field_spec(self, specs, seen, *, key, label, kind, column=None, item=None, key_name=None):
        key = str(key or "").strip()
        label = str(label or key).strip()
        if not key or key in seen:
            return
        seen.add(key)
        specs.append({
            "key": key,
            "label": label,
            "kind": str(kind or "column"),
            "column": str(column or ""),
            "item": str(item or ""),
            "key_name": str(key_name or ""),
        })

    def _available_grouping_fields(self):
        """Return file-filter-style metadata fields that can define concatenation groups.

        The list is based first on the GUI File filters.  If the filter panel is not
        available yet, it falls back to the inventory columns.  A date-only grouping
        option is added for common date/session columns so recordings from the same
        day can be concatenated even when their exact session_start_time differs.
        """
        specs = []
        seen = set()
        gui = self.GUI_instance
        filter_panel = getattr(gui, "filter_panel", None) if gui is not None else None
        filter_specs = getattr(filter_panel, "filter_specs", None)

        if isinstance(filter_specs, dict) and filter_specs:
            for spec_key, raw_spec in filter_specs.items():
                if not isinstance(raw_spec, dict):
                    continue
                stype = str(raw_spec.get("type", "multi") or "multi")
                col = str(raw_spec.get("column", "") or "")
                if not col:
                    continue
                if stype in ("multi", "range"):
                    self._add_grouping_field_spec(
                        specs, seen,
                        key=f"col::{col}",
                        label=col,
                        kind="column",
                        column=col,
                    )
                elif stype == "list_flag":
                    item = str(raw_spec.get("item", spec_key) or spec_key)
                    self._add_grouping_field_spec(
                        specs, seen,
                        key=f"list_flag::{col}::{item}",
                        label=item,
                        kind="list_flag",
                        column=col,
                        item=item,
                    )
                elif stype == "list_value":
                    key_name = str(raw_spec.get("key", spec_key) or spec_key)
                    self._add_grouping_field_spec(
                        specs, seen,
                        key=f"list_value::{col}::{key_name}",
                        label=key_name,
                        kind="list_value",
                        column=col,
                        key_name=key_name,
                    )

        df = self._inventory_df()
        if df is not None and hasattr(df, "columns"):
            # Fallback/enrichment: include preferred common grouping columns even if
            # they were hidden by the File filter high-cardinality rules.
            preferred = [
                "subject_id", "subject", "animal", "mouse_id",
                "day", "date", "session_date", "recording_date", "run_date",
                "protocol", "session_protocol", "task", "condition", "genotype", "sex",
            ]
            for col in preferred:
                if col in df.columns:
                    self._add_grouping_field_spec(
                        specs, seen,
                        key=f"col::{col}",
                        label=col,
                        kind="column",
                        column=col,
                    )

            for col in ("session_start_time", "recording_start_time", "datetime", "date_time"):
                if col in df.columns:
                    self._add_grouping_field_spec(
                        specs, seen,
                        key=f"date::{col}",
                        label=f"{col} (date only)",
                        kind="date",
                        column=col,
                    )

            # If the filter panel has not provided specs, ask it for the same
            # filterable columns or fall back to low-cardinality columns.
            if not specs:
                cols = []
                get_cols = getattr(filter_panel, "_get_filterable_columns", None)
                if callable(get_cols):
                    try:
                        cols = list(get_cols(df))
                    except Exception:
                        cols = []
                if not cols:
                    for col in df.columns:
                        if col == "file_path":
                            continue
                        try:
                            n_unique = int(df[col].dropna().astype(str).nunique())
                            n_rows = max(1, int(len(df[col].dropna())))
                        except Exception:
                            continue
                        if n_unique <= min(80, max(8, int(0.6 * n_rows))):
                            cols.append(col)
                for col in cols:
                    self._add_grouping_field_spec(
                        specs, seen,
                        key=f"col::{col}",
                        label=str(col),
                        kind="column",
                        column=str(col),
                    )

        specs.sort(key=lambda d: (str(d.get("label", "")).lower(), str(d.get("key", "")).lower()))
        return specs

    def _refresh_grouping_controls(self):
        specs = self._available_grouping_fields()
        self._grouping_field_specs_by_key = {str(s.get("key")): s for s in specs}

        # Preserve old behavior: when the user checks concatenation without opening
        # the selector, subject_id is selected by default if available.
        if not self._grouping_default_initialized and not self.selected_grouping_keys:
            for key in ("col::subject_id", "col::subject", "col::animal", "col::mouse_id"):
                if key in self._grouping_field_specs_by_key:
                    self.selected_grouping_keys = [key]
                    break
            self._grouping_default_initialized = True

        self.selected_grouping_keys = [
            key for key in self.selected_grouping_keys
            if key in self._grouping_field_specs_by_key
        ]
        if hasattr(self, "grouping_fields_btn"):
            self.grouping_fields_btn.setEnabled(bool(self.concatenate_check.isChecked()) and bool(specs))
        self._update_grouping_summary()

    def _on_concatenate_toggled(self, _checked=False):
        self._refresh_grouping_controls()

    def _update_grouping_summary(self):
        if not hasattr(self, "grouping_summary_lbl"):
            return
        if not bool(self.concatenate_check.isChecked()):
            self.grouping_summary_lbl.setText("Off: one recording group per NWB file")
            return
        if not self._grouping_field_specs_by_key:
            self.grouping_summary_lbl.setText("No file-filter metadata field is available")
            return
        labels = [
            self._grouping_field_specs_by_key.get(key, {}).get("label", key)
            for key in self.selected_grouping_keys
            if key in self._grouping_field_specs_by_key
        ]
        if not labels:
            self.grouping_summary_lbl.setText("Select at least one grouping field")
        else:
            self.grouping_summary_lbl.setText("Group by: " + " + ".join(str(x) for x in labels))

    def _open_grouping_fields_dialog(self):
        specs = self._available_grouping_fields()
        self._grouping_field_specs_by_key = {str(s.get("key")): s for s in specs}
        if not specs:
            QtWidgets.QMessageBox.information(
                self,
                "Grouping fields",
                "No file-filter metadata field is available for grouping.",
            )
            self._update_grouping_summary()
            return

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Concatenation grouping fields")
        dlg.resize(420, 520)
        layout = QtWidgets.QVBoxLayout(dlg)
        label = QtWidgets.QLabel(
            "Select one or more file-filter fields. Recordings with identical values "
            "for all selected fields will be concatenated together."
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        listw = QtWidgets.QListWidget(dlg)
        try:
            checkable_flag = QtCore.Qt.ItemFlag.ItemIsUserCheckable
        except AttributeError:
            checkable_flag = QtCore.Qt.ItemIsUserCheckable
        selected_now = set(self.selected_grouping_keys)
        for spec in specs:
            item = QtWidgets.QListWidgetItem(str(spec.get("label", spec.get("key", ""))))
            item.setData(qt_user_role(), str(spec.get("key", "")))
            item.setFlags(item.flags() | checkable_flag)
            item.setCheckState(qt_checked() if str(spec.get("key", "")) in selected_now else qt_unchecked())
            tooltip = str(spec.get("column", ""))
            kind = str(spec.get("kind", ""))
            if kind and tooltip:
                tooltip = f"{kind}: {tooltip}"
            if tooltip:
                item.setToolTip(tooltip)
            listw.addItem(item)
        layout.addWidget(listw, stretch=1)

        row = QtWidgets.QHBoxLayout()
        clear_btn = QtWidgets.QPushButton("Clear")
        ok_btn = QtWidgets.QPushButton("OK")
        cancel_btn = QtWidgets.QPushButton("Cancel")
        row.addWidget(clear_btn)
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addWidget(ok_btn)
        layout.addLayout(row)

        def _set_checked_by_keys(keys):
            keys = set(keys)
            for i in range(listw.count()):
                item = listw.item(i)
                key = str(item.data(qt_user_role()) or "")
                item.setCheckState(qt_checked() if key in keys else qt_unchecked())

        clear_btn.clicked.connect(lambda: _set_checked_by_keys([]))
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn.clicked.connect(dlg.accept)

        if dlg.exec():
            selected = []
            for i in range(listw.count()):
                item = listw.item(i)
                if item.checkState() == qt_checked():
                    key = str(item.data(qt_user_role()) or "")
                    if key:
                        selected.append(key)
            self.selected_grouping_keys = selected
            self._refresh_grouping_controls()

    def _selected_grouping_specs(self):
        self._refresh_grouping_controls()
        return [
            dict(self._grouping_field_specs_by_key[key])
            for key in self.selected_grouping_keys
            if key in self._grouping_field_specs_by_key
        ]

    @staticmethod
    def _date_only_text(value):
        text = NewSpikeSortingDialog._safe_text(value)
        if not text:
            return ""
        # Common ISO/pandas string forms: 2026-06-18 10:30:00, 2026-06-18T10:30:00
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
        if m:
            a, b, c = m.groups()
            if len(c) == 2:
                c = "20" + c
            return f"{c}-{int(b):02d}-{int(a):02d}"
        try:
            date_attr = getattr(value, "date", None)
            if callable(date_attr):
                return str(date_attr())
        except Exception:
            pass
        return text.split()[0]

    def _grouping_value_for_spec(self, path, spec):
        row = self._inventory_row_for_path(path)
        kind = str(spec.get("kind", "column"))
        col = str(spec.get("column", ""))
        raw = None
        if row is not None and col:
            try:
                raw = row.get(col, None)
            except Exception:
                try:
                    raw = row[col]
                except Exception:
                    raw = None

        if kind == "date":
            value = self._date_only_text(raw)
        elif kind == "list_flag":
            item = str(spec.get("item", "") or "")
            items = set(self._parse_list_cell(raw))
            value = "True" if item and item in items else "False"
        elif kind == "list_value":
            key_name = str(spec.get("key_name", "") or "")
            values = []
            for item in self._parse_list_cell(raw):
                if ":" not in item:
                    continue
                k, v = item.split(":", 1)
                if k.strip() == key_name and v.strip():
                    values.append(v.strip())
            value = " | ".join(sorted(dict.fromkeys(values)))
        else:
            value = self._safe_text(raw)

        if not value and col in ("subject_id", "subject", "animal", "mouse_id"):
            value = self._safe_text(self.subject_lookup(path))
        return self._safe_text(value)

    def _label_for_group_values(self, group_values):
        parts = []
        for label, value in group_values:
            label = self._safe_text(label)
            value = self._safe_text(value)
            if label and value:
                parts.append(f"{label}_{value}")
            elif value:
                parts.append(value)
        return " | ".join(parts) or "recording"


    # ---------- NWB temporal ordering ----------

    @staticmethod
    def _decode_hdf5_value(value):
        try:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            if hasattr(value, "shape") and getattr(value, "shape", None) == ():
                value = value[()]
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
            return value
        except Exception:
            return value

    @staticmethod
    def _coerce_datetime_for_sort(value):
        """Return (missing_flag, timestamp, text) for stable oldest-to-newest sorting."""
        text = NewSpikeSortingDialog._safe_text(value)
        if not text:
            return (1, float("inf"), "")

        try:
            if hasattr(value, "to_pydatetime"):
                value = value.to_pydatetime()
        except Exception:
            pass

        try:
            if hasattr(value, "timestamp"):
                return (0, float(value.timestamp()), text)
        except Exception:
            pass

        candidate = text.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(candidate)
            return (0, float(dt.timestamp()), text)
        except Exception:
            pass

        # Common date-only fallback.  It still gives a deterministic older-to-newer order.
        try:
            d = datetime.strptime(candidate.split()[0], "%Y-%m-%d")
            return (0, float(d.timestamp()), text)
        except Exception:
            pass
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                d = datetime.strptime(candidate.split()[0], fmt)
                return (0, float(d.timestamp()), text)
            except Exception:
                pass

        # If parsing fails, keep files with readable but unparsable dates before files
        # with no date, then use the text and file path as deterministic fallbacks.
        return (0, float("inf"), text)

    def _read_nwb_session_start_time(self, path):
        """Read the NWB session_start_time without loading the full file when possible."""
        # Fast path: NWB stores session_start_time as a root HDF5 attribute in normal files.
        if h5py is not None:
            try:
                with h5py.File(str(path), "r") as f:
                    value = f.attrs.get("session_start_time", None)
                    value = self._decode_hdf5_value(value)
                    if self._safe_text(value):
                        return value

                    # Conservative fallback for non-standard files.
                    if "general" in f and "session_start_time" in f["general"]:
                        value = self._decode_hdf5_value(f["general/session_start_time"][()])
                        if self._safe_text(value):
                            return value
            except Exception:
                pass

        # Slower fallback, useful if a file uses an unusual storage layout.
        if NWBHDF5IO is not None:
            try:
                with NWBHDF5IO(str(path), "r", load_namespaces=True) as io:
                    nwbfile = io.read()
                    value = getattr(nwbfile, "session_start_time", None)
                    if self._safe_text(value):
                        return value
            except Exception:
                pass

        return None

    def _session_start_for_path(self, path):
        key = self._normalize_path_key(path)
        if key in self._session_start_cache:
            return self._session_start_cache[key]

        value = None

        # Prefer the inventory.  It is already loaded in the GUI and avoids opening
        # hundreds of NWB files when confirming a concatenated spike sorting.
        row = self._inventory_row_for_path(path)
        if row is not None:
            for col in (
                "session_start_time",
                "recording_start_time",
                "datetime",
                "date_time",
                "run_date",
                "date",
            ):
                try:
                    raw = row.get(col, None) if hasattr(row, "get") else None
                except Exception:
                    raw = None
                if self._safe_text(raw):
                    value = raw
                    break

        # Compatibility fallback for older inventories or files not present in the
        # inventory.  This should normally not run after rescanning with the updated
        # inventory generator.
        if not self._safe_text(value):
            value = self._read_nwb_session_start_time(path)

        self._session_start_cache[key] = value
        return value

    def _sort_files_by_nwb_start_time(self, files):
        files = [str(p) for p in files if str(p).strip()]
        keyed = []
        total = len(files)
        for i, path in enumerate(files, start=1):
            if total > 1 and getattr(self, "_confirm_busy", False):
                self._set_confirm_status(f"Reading start times from inventory ({i}/{total})...")
            keyed.append((
                path,
                (
                    *self._coerce_datetime_for_sort(self._session_start_for_path(path)),
                    self._normalize_path_key(path),
                ),
            ))
        return [path for path, _key in sorted(keyed, key=lambda item: item[1])]

    # ---------- recipe creation ----------

    @staticmethod
    def _clean_name(name: str) -> str:
        name = str(name).strip()
        name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
        name = re.sub(r"\s+", " ", name)
        return name

    def _make_recording_groups(self, selected_files):
        selected_files = self._sort_files_by_nwb_start_time(selected_files)
        concatenate_by_fields = bool(self.concatenate_check.isChecked())
        grouping_specs = self._selected_grouping_specs() if concatenate_by_fields else []
        groups = []
        used_labels = {}

        def _unique_label(base):
            base = str(base or "recording").strip() or "recording"
            base = re.sub(r"[\\/:*?\"<>|]+", "_", base)
            base = re.sub(r"\s+", " ", base).strip()
            if base not in used_labels:
                used_labels[base] = 1
                return base
            used_labels[base] += 1
            return f"{base}_{used_labels[base]}"

        def _subject_for_group(files):
            subjects = []
            for fp in files:
                subject = self._safe_text(self.subject_lookup(fp))
                if subject and subject not in subjects:
                    subjects.append(subject)
            return subjects[0] if len(subjects) == 1 else ""

        if concatenate_by_fields:
            if not grouping_specs:
                raise RuntimeError("Concatenation is enabled, but no grouping field is selected.")

            grouped = {}
            order = []
            for path in selected_files:
                key_parts = []
                display_pairs = []
                raw_values_by_label = {}
                missing_value = False
                for spec in grouping_specs:
                    label = str(spec.get("label", spec.get("key", ""))).strip()
                    value = self._grouping_value_for_spec(path, spec)
                    if not value:
                        # Missing grouping metadata must not concatenate unrelated files.
                        value = f"missing_{Path(path).stem}"
                        missing_value = True
                    key_parts.append(value)
                    display_pairs.append((label, value))
                    raw_values_by_label[label] = value

                key = tuple(key_parts)
                if key not in grouped:
                    grouped[key] = {
                        "files": [],
                        "display_pairs": display_pairs,
                        "values": raw_values_by_label,
                        "missing_grouping_value": missing_value,
                    }
                    order.append(key)
                grouped[key]["files"].append(path)

            for key in order:
                block = grouped[key]
                files = self._sort_files_by_nwb_start_time(block.get("files", []) or [])
                group_values = dict(block.get("values", {}) or {})
                subject = _subject_for_group(files)
                # Prefer the explicit subject_id grouping value when present.
                for spec in grouping_specs:
                    col = str(spec.get("column", ""))
                    label = str(spec.get("label", ""))
                    if col in ("subject_id", "subject", "animal", "mouse_id") or label in ("subject_id", "subject", "animal", "mouse_id"):
                        subject = self._safe_text(group_values.get(label, subject)) or subject
                        break
                groups.append({
                    "label": _unique_label(self._label_for_group_values(block.get("display_pairs", []))),
                    "subject": subject,
                    "subject_id": subject,
                    "files": files,
                    "concatenated": len(files) > 1,
                    "concatenate_by_fields": True,
                    "group_by_fields": [dict(s) for s in grouping_specs],
                    "group_by_values": group_values,
                    "group_key": list(key),
                    "missing_grouping_value": bool(block.get("missing_grouping_value", False)),
                })
        else:
            for path in selected_files:
                subject = str(self.subject_lookup(path) or "").strip()
                groups.append({
                    "label": _unique_label(Path(path).stem),
                    "subject": subject,
                    "subject_id": subject,
                    "files": [path],
                    "concatenated": False,
                    "concatenate_by_fields": False,
                    "group_by_fields": [],
                    "group_by_values": {},
                })
        return groups

    @staticmethod
    def _decode_bad_channel_hdf5_value(value):
        try:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="ignore")
            if hasattr(value, "decode") and not isinstance(value, str):
                return value.decode("utf-8", errors="ignore")
        except Exception:
            pass
        return value

    @classmethod
    def _clean_bad_channel_text(cls, value):
        try:
            value = cls._decode_bad_channel_hdf5_value(value)
            if value is None:
                return ""
            text = str(value).strip()
            if text.lower() in {"", "nan", "none", "nat"}:
                return ""
            return text
        except Exception:
            return ""

    @classmethod
    def _bad_channel_bool(cls, value):
        try:
            value = cls._decode_bad_channel_hdf5_value(value)
            if isinstance(value, (bool, int, float)):
                return bool(value)
            return str(value).strip().lower() in {"1", "true", "yes", "y", "bad"}
        except Exception:
            return False

    @staticmethod
    def _h5_read_array(obj):
        try:
            arr = obj[()]
        except Exception:
            try:
                arr = obj[:]
            except Exception:
                return []
        try:
            if hasattr(arr, "tolist"):
                arr = arr.tolist()
        except Exception:
            pass
        if isinstance(arr, (bytes, str)):
            return [arr]
        try:
            return list(arr)
        except Exception:
            return [arr]

    @classmethod
    def _h5_electrode_column(cls, electrodes_group, name):
        try:
            if name not in electrodes_group:
                return []
            obj = electrodes_group[name]
            if hasattr(obj, "keys") and "data" in obj:
                return cls._h5_read_array(obj["data"])
            return cls._h5_read_array(obj)
        except Exception:
            return []

    @classmethod
    def _bad_channel_metadata_from_nwb_fast(cls, nwb_file_path):
        """Read bad-channel metadata without loading the full NWB file.

        This is used only when confirming a new spike sorting recipe.  It avoids
        opening the NWB through PyNWB on the GUI thread unless the lightweight
        HDF5 read is not sufficient.
        """
        meta = {
            "path": str(nwb_file_path),
            "has_electrodes": False,
            "has_bad_channel_column": False,
            "bad_labels": [],
        }

        if h5py is not None:
            try:
                with h5py.File(str(nwb_file_path), "r") as f:
                    electrodes = None
                    for key in (
                        "general/extracellular_ephys/electrodes",
                        "/general/extracellular_ephys/electrodes",
                    ):
                        try:
                            if key in f:
                                electrodes = f[key]
                                break
                        except Exception:
                            pass
                    if electrodes is None:
                        return meta

                    meta["has_electrodes"] = True
                    bad_values = cls._h5_electrode_column(electrodes, "bad_channel")
                    if not bad_values:
                        return meta
                    meta["has_bad_channel_column"] = True

                    labels = []
                    for col in (
                        "contact_ids",
                        "channel_name",
                        "channel_id",
                        "label",
                        "electrode_label",
                        "group_name",
                        "id",
                    ):
                        values = cls._h5_electrode_column(electrodes, col)
                        cleaned = [cls._clean_bad_channel_text(v) for v in values]
                        cleaned = [v for v in cleaned if v]
                        if len(cleaned) >= len(bad_values):
                            labels = cleaned[:len(bad_values)]
                            break
                    if not labels:
                        labels = [str(i) for i in range(len(bad_values))]

                    bad_labels = []
                    for idx, value in enumerate(bad_values):
                        if cls._bad_channel_bool(value):
                            lab = labels[idx] if idx < len(labels) else str(idx)
                            if lab not in bad_labels:
                                bad_labels.append(lab)
                    meta["bad_labels"] = bad_labels
                    return meta
            except Exception:
                pass

        if NWBHDF5IO is not None:
            try:
                with NWBHDF5IO(str(nwb_file_path), mode="r") as io:
                    nwb = io.read()
                    if not hasattr(nwb, "electrodes") or nwb.electrodes is None:
                        return meta
                    electrodes = nwb.electrodes.to_dataframe()
                if electrodes is None or getattr(electrodes, "empty", True):
                    return meta
                meta["has_electrodes"] = True
                if "bad_channel" not in electrodes.columns:
                    return meta
                meta["has_bad_channel_column"] = True
                bad = electrodes["bad_channel"].fillna(False)
                if getattr(bad, "dtype", None) == object:
                    bad = bad.astype(str).str.strip().str.lower().isin(("1", "true", "yes", "y", "bad"))
                else:
                    bad = bad.astype(bool)

                label_col = None
                for col in (
                    "contact_ids",
                    "channel_name",
                    "channel_id",
                    "label",
                    "electrode_label",
                    "group_name",
                ):
                    if col in electrodes.columns:
                        label_col = col
                        break
                bad_labels = []
                for idx, is_bad in bad.items():
                    if not bool(is_bad):
                        continue
                    if label_col is not None:
                        label = cls._clean_bad_channel_text(electrodes.loc[idx, label_col])
                    else:
                        label = cls._clean_bad_channel_text(idx)
                    if not label:
                        label = str(len(bad_labels))
                    if label not in bad_labels:
                        bad_labels.append(label)
                meta["bad_labels"] = bad_labels
            except Exception:
                pass
        return meta

    def _bad_channel_metadata_from_inventory(self, nwb_file_path):
        row = self._inventory_row_for_path(nwb_file_path)
        if row is None:
            return None

        has_column_raw = None
        bad_raw = None
        n_bad_raw = None
        try:
            has_column_raw = row.get("has_bad_channel_column", None)
            bad_raw = row.get("bad_channels", None)
            n_bad_raw = row.get("number_of_bad_channels", None)
        except Exception:
            return None

        has_inventory_fields = (
            "has_bad_channel_column" in getattr(row, "index", [])
            or "bad_channels" in getattr(row, "index", [])
            or "number_of_bad_channels" in getattr(row, "index", [])
        )
        if not has_inventory_fields:
            return None

        labels = self._parse_list_cell(bad_raw)
        has_bad_column = self._bool_from_inventory_value(has_column_raw)
        if labels and not has_bad_column:
            # Older hand-edited inventories may only contain the list.
            has_bad_column = True

        try:
            n_bad = int(float(str(n_bad_raw))) if self._safe_text(n_bad_raw) else len(labels)
        except Exception:
            n_bad = len(labels)

        return {
            "has_electrodes": True,
            "has_bad_channel_column": bool(has_bad_column),
            "number_of_bad_channels": int(n_bad),
            "bad_labels": labels,
            "source": "inventory",
        }

    def _bad_channel_mismatch_policy_for_recipe(self, groups):
        """Ask once, at recipe creation, how bad-channel mismatches should be handled."""
        if not bool(self.remove_bad_channels_check.isChecked()):
            return "per_file"

        mismatched = []
        detail_lines = []
        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        except Exception:
            pass
        try:
            groups_list = list(groups or [])
            total_groups = len(groups_list)
            for group_index, group in enumerate(groups_list, start=1):
                if total_groups > 1 and getattr(self, "_confirm_busy", False):
                    self._set_confirm_status(f"Checking bad channels ({group_index}/{total_groups})...")
                files = [str(p) for p in (group.get("files", []) or []) if str(p).strip()]
                if len(files) <= 1:
                    continue

                per_file = []
                for file_path in files:
                    meta = self._bad_channel_metadata_from_inventory(file_path)
                    if meta is None:
                        meta = self._bad_channel_metadata_from_nwb_fast(file_path)
                    bad_labels = [str(x) for x in (meta.get("bad_labels", []) or [])]
                    has_bad_column = bool(meta.get("has_bad_channel_column", False))
                    key = tuple(sorted(set(bad_labels), key=self._natural_sort_key))
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
        finally:
            try:
                QtWidgets.QApplication.restoreOverrideCursor()
            except Exception:
                pass

        if not mismatched:
            return "per_file"

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Different bad channels across files")
        box.setText("Some concatenated NWB files do not have the same bad channels.")
        box.setInformativeText(
            "Choose one channel rule for this spike sorting. This choice will be saved in the recipe and will not be asked again when running the sorter.\n\n"
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
        box.addButton(
            "Cancel",
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

    def _make_sorting_recipe(self, config: dict):
        ordered_files = self._sort_files_by_nwb_start_time(config.get("files", []))
        groups = self._make_recording_groups(ordered_files)
        return {
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "name": config.get("name"),
            "mode": "recipe_from_nwb",
            "files": ordered_files,
            # Backward-compatible key: old code used only subject_id grouping.
            "concatenate_per_subject": bool(config.get("concatenate_per_subject", False)),
            "concatenate_by_fields": bool(config.get("concatenate_by_fields", False)),
            "concatenate_grouping_fields": list(config.get("concatenate_grouping_fields", []) or []),
            "remove_bad_channels": bool(config.get("remove_bad_channels", False)),
            "bad_channel_mismatch_policy": str(config.get("bad_channel_mismatch_policy", "per_file") or "per_file"),
            "data": {
                "label": config.get("data_label"),
                "name": config.get("data_name"),
                "path": config.get("data_path"),
                "type": config.get("data_type"),
                "container": config.get("data_container"),
                "module": config.get("data_module"),
            },
            "recording_groups": groups,
        }

    def _write_sorting_recipe(self, sorting_folder: Path, recipe: dict):
        sorting_folder.mkdir(parents=True, exist_ok=False)
        recipe_path = sorting_folder / "sorting_recipe.json"
        with open(recipe_path, "w", encoding="utf-8") as f:
            _pp_json_dump(recipe, f, indent=2, ensure_ascii=False)
        preprocessing_path = sorting_folder / "preprocessing_pipeline.json"
        if not preprocessing_path.exists():
            with open(preprocessing_path, "w", encoding="utf-8") as f:
                _pp_json_dump({
                    "version": 1,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "pipeline": [],
                    "enabled_pipeline": [],
                }, f, indent=2, ensure_ascii=False)
        return recipe_path

    def _confirm(self):
        if self._confirm_busy:
            return
        sorting_name = self._clean_name(self.name_edit.text())
        if not sorting_name:
            QtWidgets.QMessageBox.warning(self, "Missing sorting name", "Please enter a name for this spike sorting.")
            return
        if not self.files:
            QtWidgets.QMessageBox.warning(self, "No file selected", "Please select at least one file in the main GUI.")
            return
        selected_data_label = self.data_combo.currentText().strip()
        selected_data_payload = self._data_payload_by_label.get(selected_data_label)
        if not selected_data_payload:
            QtWidgets.QMessageBox.warning(self, "No data selected", "Please select the data to use for spike sorting.")
            return
        selected_files = [p for p in self.files if self._data_present_in_file(p, selected_data_payload)]
        if not selected_files:
            QtWidgets.QMessageBox.warning(self, "No usable file", "The selected data is not present in any selected file.")
            return
        if self.project_path is None:
            QtWidgets.QMessageBox.critical(self, "Missing project path", "Could not find the project path.")
            return

        concatenate_by_fields = bool(self.concatenate_check.isChecked())
        grouping_specs = self._selected_grouping_specs() if concatenate_by_fields else []
        if concatenate_by_fields and not grouping_specs:
            QtWidgets.QMessageBox.warning(
                self,
                "Missing grouping field",
                "Please select at least one file-filter field for concatenation grouping.",
            )
            return
        grouping_columns = [str(s.get("column", "")) for s in grouping_specs]

        config = {
            "name": sorting_name,
            "files": selected_files,
            "concatenate_per_subject": bool(
                concatenate_by_fields
                and len(grouping_specs) == 1
                and grouping_columns[0] in ("subject_id", "subject", "animal", "mouse_id")
            ),
            "concatenate_by_fields": concatenate_by_fields,
            "concatenate_grouping_fields": [dict(s) for s in grouping_specs],
            "remove_bad_channels": bool(self.remove_bad_channels_check.isChecked()),
            "data_label": selected_data_label,
            "data_name": selected_data_payload.get("name"),
            "data_path": selected_data_payload.get("nwb_path"),
            "data_type": selected_data_payload.get("type"),
            "data_container": selected_data_payload.get("container"),
            "data_module": selected_data_payload.get("module"),
        }

        sorting_folder = self.project_path / "Data" / "Spike_sorting" / sorting_name
        if sorting_folder.exists():
            QtWidgets.QMessageBox.warning(
                self,
                "Sorting name already exist",
                "Sorting name already exist\nPlease enter an other name for this spike sorting.",
            )
            return
        try:
            self._set_confirm_busy(True, "Preparing recipe...")
            recipe = self._make_sorting_recipe(config)

            self._set_confirm_status("Checking bad channels from inventory...")
            bad_channel_policy = self._bad_channel_mismatch_policy_for_recipe(recipe.get("recording_groups", []))
            if bad_channel_policy is None:
                self._set_confirm_busy(False)
                return
            config["bad_channel_mismatch_policy"] = bad_channel_policy
            recipe["bad_channel_mismatch_policy"] = bad_channel_policy

            self._set_confirm_status("Writing recipe...")
            recipe_path = self._write_sorting_recipe(sorting_folder, recipe)
            config["sorting_folder"] = str(sorting_folder)
            config["recipe_path"] = str(recipe_path)
            config["recording_groups"] = recipe.get("recording_groups", [])

            self._set_confirm_status("Opening spike sorting...")
            if callable(self.on_confirm):
                self.on_confirm(config)
            self.accept()
        except Exception as exc:
            try:
                if sorting_folder.exists():
                    import shutil
                    shutil.rmtree(sorting_folder)
            except Exception:
                pass
            self._set_confirm_busy(False)
            QtWidgets.QMessageBox.critical(self, "Create spike sorting", str(exc))
