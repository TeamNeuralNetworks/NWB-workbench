from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List

import pandas as pd

from GUI.qt_compat import QtCore, QtWidgets, qt_checked, qt_unchecked


class FilterPanel(QtWidgets.QWidget):
    """Qt port of the Tkinter FilterPanel.

    The goal here is to keep the same filtering *rules* as the Tk GUI:
    - file filters are categorical/list based, not numeric min/max sliders
    - list-like metadata columns are split into flag/key-value filters
    - high-cardinality columns are ignored
    - the compact filter dictionaries use the same keys as the Tk version
    """

    EXCLUDED_COLUMNS = {
        "file_path",
        "nwb_path",
        "identifier",
        "session_description",
        "nwb_readable",
        "has_trials",
        "file_type",
        "number_of_trials",
        "max_trial_len",
        "min_trial_len",
        "median_trial_len",
        "read_error",
    }

    EXCLUDED_TRIAL_COLUMNS = {
        "start_time",
        "stop_time",
        "trial_time",
    }

    PREFERRED_CATEGORICAL_COLUMNS = {
        "subject_id",
        "species",
        "genotype",
        "sex",
        "protocol",
    }

    BAD_CHANNEL_FILTER_COLUMNS = {
        "bad_channels",
        "bad_channel",
        "number_of_bad_channels",
        "has_bad_channel_column",
    }

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        self.filter_specs: Dict[str, Dict[str, Any]] = {}
        self.trial_filter_specs: Dict[str, Dict[str, Any]] = {}
        self.trial_filter_section = None
        self.trial_filter_container = None
        self._trial_filters_visible = False

        # Allow a compact filter column; the scroll area keeps controls accessible.
        self.setMinimumWidth(180)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Filters")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)
        header.addStretch(1)

        reset_btn = QtWidgets.QPushButton("Reset")
        # Qt clicked(bool) passes False when unchecked; wrap it so reset really applies.
        reset_btn.clicked.connect(lambda _checked=False: self.reset_filters(apply=True))
        header.addWidget(reset_btn)
        outer.addLayout(header)

        self.scroll = QtWidgets.QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.body = QtWidgets.QWidget(self.scroll)
        self.body_layout = QtWidgets.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, stretch=1)

        self.rebuild_from_inventory()

    # ------------------------------------------------------------------
    # sorting helpers
    # ------------------------------------------------------------------

    def _natural_sort_key(self, value):
        s = str(value).strip()
        parts = re.split(r"(\d+)", s)
        return [int(p) if p.isdigit() else p.lower() for p in parts]

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        self.trial_filter_container = None
        while self.body_layout.count() > 0:
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.body_layout.addStretch(1)

    def _insert_before_stretch(self, widget):
        self.body_layout.insertWidget(max(0, self.body_layout.count() - 1), widget)

    def _add_section_label(self, text: str):
        lbl = QtWidgets.QLabel(text)
        font = lbl.font()
        font.setBold(True)
        lbl.setFont(font)
        self._insert_before_stretch(lbl)
        return lbl

    # ------------------------------------------------------------------
    # generic widgets
    # ------------------------------------------------------------------

    def _add_multi_filter(self, store, key_name, label, values: List[str], parent_layout=None):
        if parent_layout is None:
            parent_layout = self.body_layout

        frame = QtWidgets.QGroupBox(str(label))
        layout = QtWidgets.QHBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)

        summary = QtWidgets.QLabel("")
        summary.setMinimumWidth(40)
        summary.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        summary.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        btn = QtWidgets.QPushButton("Select...")

        layout.addWidget(summary, stretch=1)
        layout.addWidget(btn)

        store[key_name] = {
            "type": "multi",
            "selected": [],
            "summary": summary,
            "values": list(values),
        }

        btn.clicked.connect(lambda _=False, k=key_name, st=store: self._open_multi_dialog(k, st))

        if parent_layout is self.body_layout:
            self._insert_before_stretch(frame)
        else:
            parent_layout.addWidget(frame)

        self._update_multi_summary(store, key_name)

    def _add_range_filter(self, store, key_name, label, min_value=None, max_value=None, parent_layout=None):
        if parent_layout is None:
            parent_layout = self.body_layout

        frame = QtWidgets.QGroupBox(str(label))
        layout = QtWidgets.QHBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)

        default_min = "" if min_value is None else str(min_value)
        default_max = "" if max_value is None else str(max_value)

        min_edit = QtWidgets.QLineEdit(default_min)
        max_edit = QtWidgets.QLineEdit(default_max)
        min_edit.setMaximumWidth(80)
        max_edit.setMaximumWidth(80)

        layout.addWidget(QtWidgets.QLabel("min"))
        layout.addWidget(min_edit)
        layout.addWidget(QtWidgets.QLabel("max"))
        layout.addWidget(max_edit)
        layout.addStretch(1)

        store[key_name] = {
            "type": "range",
            "min": min_edit,
            "max": max_edit,
            "default_min": default_min,
            "default_max": default_max,
        }

        min_edit.editingFinished.connect(self._apply_filters)
        max_edit.editingFinished.connect(self._apply_filters)

        if parent_layout is self.body_layout:
            self._insert_before_stretch(frame)
        else:
            parent_layout.addWidget(frame)

    def _open_multi_dialog(self, key_name, store):
        spec = store[key_name]
        selected_now = set(spec.get("selected", []))
        values = list(spec.get("values", []))

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Select {key_name}")
        dlg.resize(360, 460)
        layout = QtWidgets.QVBoxLayout(dlg)

        title = QtWidgets.QLabel(f"Select values for {key_name}")
        layout.addWidget(title)

        listw = QtWidgets.QListWidget(dlg)
        try:
            checkable_flag = QtCore.Qt.ItemFlag.ItemIsUserCheckable
        except AttributeError:
            checkable_flag = QtCore.Qt.ItemIsUserCheckable

        for v in values:
            item = QtWidgets.QListWidgetItem(str(v))
            item.setFlags(item.flags() | checkable_flag)
            item.setCheckState(qt_checked() if v in selected_now else qt_unchecked())
            listw.addItem(item)

        layout.addWidget(listw, stretch=1)

        row = QtWidgets.QHBoxLayout()
        all_btn = QtWidgets.QPushButton("All")
        clear_btn = QtWidgets.QPushButton("Clear")
        ok_btn = QtWidgets.QPushButton("OK")
        cancel_btn = QtWidgets.QPushButton("Cancel")
        row.addWidget(all_btn)
        row.addWidget(clear_btn)
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addWidget(ok_btn)
        layout.addLayout(row)

        all_btn.clicked.connect(lambda: [listw.item(i).setCheckState(qt_checked()) for i in range(listw.count())])
        clear_btn.clicked.connect(lambda: [listw.item(i).setCheckState(qt_unchecked()) for i in range(listw.count())])
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn.clicked.connect(dlg.accept)

        if dlg.exec():
            spec["selected"] = [
                values[i]
                for i in range(listw.count())
                if listw.item(i).checkState() == qt_checked()
            ]
            self._update_multi_summary(store, key_name)
            self._apply_filters()

    def _update_multi_summary(self, store, key_name):
        spec = store[key_name]
        selected = spec.get("selected", [])
        label = spec.get("summary")
        if label is None:
            return

        if not selected:
            label.setText("")
        elif len(selected) <= 3:
            label.setText(", ".join(str(v) for v in selected))
        else:
            label.setText(f"{len(selected)} selected")

    # ------------------------------------------------------------------
    # list-like metadata helpers copied from Tk version
    # ------------------------------------------------------------------

    def _parse_list_cell(self, v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return []

        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip() != ""]

        if isinstance(v, (tuple, set)):
            return [str(x).strip() for x in v if str(x).strip() != ""]

        if isinstance(v, str):
            txt = v.strip()
            if txt == "":
                return []

            if txt.startswith("[") and txt.endswith("]"):
                try:
                    parsed = ast.literal_eval(txt)
                    if isinstance(parsed, (list, tuple, set)):
                        return [str(x).strip() for x in parsed if str(x).strip() != ""]
                except Exception:
                    pass

            return [txt]

        return [str(v).strip()]

    def _is_list_like_column(self, series: pd.Series) -> bool:
        non_na = series.dropna()
        if non_na.empty:
            return False

        for v in non_na.head(20):
            if isinstance(v, (list, tuple, set)):
                return True
            if isinstance(v, str):
                txt = v.strip()
                if txt.startswith("[") and txt.endswith("]"):
                    try:
                        parsed = ast.literal_eval(txt)
                        if isinstance(parsed, (list, tuple, set)):
                            return True
                    except Exception:
                        pass
        return False

    def _collect_list_filters(self, series: pd.Series):
        flag_items = set()
        keyed_items = {}

        for v in series.dropna():
            items = self._parse_list_cell(v)
            for item in items:
                if ":" in item:
                    key, value = item.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        keyed_items.setdefault(key, set()).add(value)
                else:
                    item = item.strip()
                    if item:
                        flag_items.add(item)

        return flag_items, keyed_items

    # ------------------------------------------------------------------
    # file-filter generation copied from Tk version
    # ------------------------------------------------------------------

    def _normalize_for_filtering(self, series: pd.Series) -> pd.Series:
        def _norm(v):
            try:
                if pd.isna(v):
                    return None
            except Exception:
                pass

            if isinstance(v, list):
                return " | ".join(map(str, v))
            if isinstance(v, tuple):
                return " | ".join(map(str, v))
            if isinstance(v, set):
                return " | ".join(sorted(map(str, v)))
            if isinstance(v, dict):
                return str(v)
            return v

        return series.map(_norm)

    @classmethod
    def _is_bad_channel_filter_column(cls, col) -> bool:
        name = str(col or "").strip().lower()
        return name in cls.BAD_CHANNEL_FILTER_COLUMNS or "bad_channel" in name

    def _get_filterable_columns(self, df: pd.DataFrame):
        cols = []

        for col in df.columns:
            if col in self.EXCLUDED_COLUMNS:
                continue
            if self._is_bad_channel_filter_column(col):
                continue

            s_raw = df[col]

            if self._is_list_like_column(s_raw):
                cols.append(col)
                continue

            s = self._normalize_for_filtering(s_raw).dropna()
            if s.empty:
                continue

            try:
                nunique = s.nunique()
            except Exception:
                continue

            nrows = len(s)

            if col in self.PREFERRED_CATEGORICAL_COLUMNS:
                cols.append(col)
                continue

            if pd.api.types.is_bool_dtype(s_raw):
                cols.append(col)
                continue

            if pd.api.types.is_numeric_dtype(s_raw):
                if nunique <= 50:
                    cols.append(col)
                continue

            if nunique <= min(50, max(5, int(0.5 * nrows))):
                cols.append(col)

        return sorted(cols, key=self._natural_sort_key)

    def rebuild_from_inventory(self):
        self.filter_specs.clear()
        self.trial_filter_specs.clear()
        self.trial_filter_section = None
        self._clear_body()

        full_df = getattr(self.app, "full_df", None)
        if full_df is None or not isinstance(full_df, pd.DataFrame) or full_df.empty:
            return

        self._add_section_label("File filters")

        for col in self._get_filterable_columns(full_df):
            series = full_df[col]

            if self._is_list_like_column(series):
                flag_items, keyed_items = self._collect_list_filters(series)

                for item_name in sorted(flag_items, key=self._natural_sort_key):
                    key_name = f"{col}__flag__{item_name}"
                    self._add_multi_filter(self.filter_specs, key_name, item_name, ["True", "False"])
                    self.filter_specs[key_name]["type"] = "list_flag"
                    self.filter_specs[key_name]["column"] = col
                    self.filter_specs[key_name]["item"] = item_name

                for key_label, values in sorted(keyed_items.items(), key=lambda x: self._natural_sort_key(x[0])):
                    key_name = f"{col}__kv__{key_label}"
                    sorted_vals = sorted([str(v) for v in values], key=self._natural_sort_key)
                    self._add_multi_filter(self.filter_specs, key_name, key_label, sorted_vals)
                    self.filter_specs[key_name]["type"] = "list_value"
                    self.filter_specs[key_name]["column"] = col
                    self.filter_specs[key_name]["key"] = key_label

                continue

            safe_series = self._normalize_for_filtering(series).dropna()
            values = sorted(safe_series.astype(str).unique().tolist(), key=self._natural_sort_key)

            self._add_multi_filter(self.filter_specs, col, col, values)
            self.filter_specs[col]["column"] = col

        # Build trial filter specs too, so get_trial_filters() keeps the same API,
        # but keep the whole section hidden unless the Analysis tab is active.
        trial_catalog_df = getattr(self.app, "trial_filter_catalog_df", None)
        if isinstance(trial_catalog_df, pd.DataFrame) and not trial_catalog_df.empty:
            self.trial_filter_container = QtWidgets.QWidget(self.body)
            trial_layout = QtWidgets.QVBoxLayout(self.trial_filter_container)
            trial_layout.setContentsMargins(0, 8, 0, 0)
            trial_layout.setSpacing(6)

            sep = QtWidgets.QFrame(self.trial_filter_container)
            sep.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            sep.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
            trial_layout.addWidget(sep)

            lbl = QtWidgets.QLabel("Trial filters", self.trial_filter_container)
            font = lbl.font()
            font.setBold(True)
            lbl.setFont(font)
            trial_layout.addWidget(lbl)

            self._insert_before_stretch(self.trial_filter_container)

            trial_catalog_df = trial_catalog_df[
                ~trial_catalog_df["trial_column"].isin(self.EXCLUDED_TRIAL_COLUMNS)
            ]
            trial_catalog_df = trial_catalog_df.sort_values(
                "trial_column",
                key=lambda s: s.map(self._natural_sort_key),
            )

            categorical_df = trial_catalog_df[
                trial_catalog_df["filter_kind"].astype(str) == "categorical"
            ]
            for _, row in categorical_df.iterrows():
                col = str(row["trial_column"])
                values = []
                raw = row.get("unique_values_json")
                if pd.notna(raw):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, list):
                            values = [str(v) for v in parsed]
                    except Exception:
                        pass
                values = sorted(values, key=self._natural_sort_key)
                self._add_multi_filter(self.trial_filter_specs, col, col, values, parent_layout=trial_layout)

            range_df = trial_catalog_df[
                trial_catalog_df["filter_kind"].astype(str) == "range"
            ]
            for _, row in range_df.iterrows():
                col = str(row["trial_column"])
                self._add_range_filter(self.trial_filter_specs, col, col, parent_layout=trial_layout)

            self.trial_filter_container.setVisible(self._trial_filters_visible)

    def set_trial_filters_visible(self, visible: bool):
        """Show trial filters only when the Analysis tab is active, like the Tk GUI."""
        self._trial_filters_visible = bool(visible)
        if self.trial_filter_container is not None:
            self.trial_filter_container.setVisible(self._trial_filters_visible)

    # ------------------------------------------------------------------
    # filter state IO
    # ------------------------------------------------------------------

    def load_filter_dicts(self, trial_filter_dict=None, file_filter_dict=None, apply=True):
        self.reset_filters(apply=False)

        trial_filter_dict = trial_filter_dict or {}
        file_filter_dict = file_filter_dict or {}

        for key, spec in self.filter_specs.items():
            val = file_filter_dict.get(key, None)

            if val is None and key.startswith("keywords__kv__"):
                short_key = key.replace("keywords__kv__", "")
                val = file_filter_dict.get(short_key, None)

            if val is None:
                continue

            if spec["type"] in ["multi", "list_flag", "list_value"]:
                spec["selected"] = list(val)
                self._update_multi_summary(self.filter_specs, key)
            elif spec["type"] == "range":
                spec["min"].setText(str(val.get("min", "")))
                spec["max"].setText(str(val.get("max", "")))

        for key, spec in self.trial_filter_specs.items():
            if key not in trial_filter_dict:
                continue
            val = trial_filter_dict[key]

            if spec["type"] == "multi":
                spec["selected"] = list(val)
                self._update_multi_summary(self.trial_filter_specs, key)
            elif spec["type"] == "range":
                spec["min"].setText(str(val.get("min", "")))
                spec["max"].setText(str(val.get("max", "")))

        if apply:
            self._apply_filters()

    # ------------------------------------------------------------------
    # filtering
    # ------------------------------------------------------------------

    def reset_filters(self, apply=True):
        for store in (self.filter_specs, self.trial_filter_specs):
            for key, spec in store.items():
                if spec["type"] in {"multi", "list_flag", "list_value"}:
                    spec["selected"] = []
                    self._update_multi_summary(store, key)
                elif spec["type"] == "range":
                    spec["min"].setText("")
                    spec["max"].setText("")

        if apply:
            self._apply_filters()

    def _apply_filters(self, *_):
        full_df = getattr(self.app, "full_df", None)
        if full_df is None or full_df.empty:
            return

        new_trial_filters = self.get_trial_filters()
        new_file_filters = self.get_file_filters()
        previous_file_filters = getattr(self.app, "current_file_filters", {}) or {}

        # Trial filters do not change which files are displayed. Preserve the
        # current tree selection when only the trial-filter state changed.
        # A change to any file filter still rebuilds the tree with no selection,
        # matching the existing behaviour.
        preserve_file_selection = previous_file_filters == new_file_filters
        selected_tree_paths = []
        file_tree_panel = getattr(self.app, "file_tree_panel", None)
        if preserve_file_selection and file_tree_panel is not None:
            try:
                selected_tree_paths = list(file_tree_panel.selected_paths())
            except Exception:
                selected_tree_paths = []

        df = full_df.copy()

        for _, spec in self.filter_specs.items():
            col = spec.get("column")
            if not col or col not in df.columns:
                continue

            if spec["type"] == "multi":
                selected = spec.get("selected", [])
                if selected:
                    safe_col = self._normalize_for_filtering(df[col]).astype(str)
                    df = df[safe_col.isin(selected)]

            elif spec["type"] == "list_flag":
                selected = spec.get("selected", [])
                item_name = spec["item"]

                if selected:
                    masks = []
                    for wanted in selected:
                        want_present = wanted == "True"
                        mask = df[col].map(lambda v: item_name in self._parse_list_cell(v))
                        masks.append(mask if want_present else ~mask)

                    if masks:
                        combined = masks[0]
                        for m in masks[1:]:
                            combined = combined | m
                        df = df[combined]

            elif spec["type"] == "list_value":
                selected_values = spec.get("selected", [])
                key_name = spec["key"]

                if selected_values:
                    targets = {f"{key_name}:{val}" for val in selected_values}
                    mask = df[col].map(lambda v: any(t in self._parse_list_cell(v) for t in targets))
                    df = df[mask]

        self.app.current_trial_filters = new_trial_filters
        self.app.current_file_filters = new_file_filters
        self.app.apply_filtered_inventory(df.copy())

        if preserve_file_selection and selected_tree_paths and file_tree_panel is not None:
            tree = getattr(file_tree_panel, "tree", None)
            if tree is not None:
                matching_items = []
                for path in selected_tree_paths:
                    try:
                        item = file_tree_panel.find_item_by_path(path)
                    except Exception:
                        item = None
                    if item is not None:
                        matching_items.append(item)

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

                    on_select = getattr(self.app, "on_select", None)
                    if callable(on_select):
                        on_select()

    def get_file_filters(self):
        out = {}

        for _, spec in self.filter_specs.items():
            col = spec.get("column")
            if not col:
                continue

            if spec["type"] == "multi":
                selected = list(spec.get("selected", []) or [])
                if selected:
                    out[col] = selected

            elif spec["type"] == "list_flag":
                selected = list(spec.get("selected", []) or [])
                item_name = spec.get("item")

                if selected:
                    out.setdefault(item_name, []).extend(selected)

            elif spec["type"] == "list_value":
                selected = list(spec.get("selected", []) or [])
                key_name = spec.get("key")

                if selected:
                    out.setdefault(key_name, []).extend(selected)

            elif spec["type"] == "range":
                vmin = str(spec["min"].text()).strip()
                vmax = str(spec["max"].text()).strip()

                if vmin != "" or vmax != "":
                    out[col] = {
                        "min": vmin if vmin != "" else None,
                        "max": vmax if vmax != "" else None,
                    }

        return out

    def get_trial_filters(self):
        out = {}

        for col, spec in self.trial_filter_specs.items():
            if spec["type"] == "multi":
                selected = spec.get("selected", [])
                if selected:
                    out[col] = selected

            elif spec["type"] == "range":
                min_txt = spec["min"].text().strip()
                max_txt = spec["max"].text().strip()

                block = {}
                if min_txt != "":
                    try:
                        block["min"] = float(min_txt)
                    except ValueError:
                        pass
                if max_txt != "":
                    try:
                        block["max"] = float(max_txt)
                    except ValueError:
                        pass

                if block:
                    out[col] = block

        if len(out) == 0:
            out = None

        return out
