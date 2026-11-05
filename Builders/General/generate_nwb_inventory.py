# -*- coding: utf-8 -*-
"""Build an NWB-only project inventory and content/trial indices.

Folders are scanned recursively for .nwb files. New scans merge by path,
retain unselected NWB entries, and save the workbook atomically.
"""

from __future__ import annotations
from GUI.project_paths import read_excel as _pp_read_excel, to_excel as _pp_to_excel

import os
import re
import json
import tempfile
from pathlib import Path
from datetime import datetime
import math 

import numpy as np
import pandas as pd
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

from pynwb import NWBHDF5IO


# -----------------------------
# Configuration
# -----------------------------
INVENTORY_FILENAME = "nwb_inventory.xlsx"

INVENTORY_SHEET = "inventory"
CONTENT_SHEET = "content_index"
TRIAL_FILTER_INDEX_SHEET = "trial_filter_index"
TRIAL_FILTER_SHEET = "trial_filter_catalog"
META_SHEET = "meta"

REQUIRED_INVENTORY_COLUMNS = {
    "file_path",
    "file_type",  # "nwb"
    "identifier",
    "session_description",
    "session_start_time",
    "protocol",
    "keywords",
    "subject_id",
    "species",
    "genotype",
    "sex",
    "nwb_readable",
    "read_error",
    "has_trials",
    "number_of_trials",
    "median_trial_len",
    "min_trial_len",
    "max_trial_len",
    "has_bad_channel_column",
    "number_of_bad_channels",
    "bad_channels",
}

SCHEMA_VERSION = "3.0"


# NWB copies created internally while a file is being validated or replaced
# must never become dataset inputs.  New work files use a non-.nwb final
# suffix, while the name markers also exclude leftovers made by older
# versions of the application.
_INTERNAL_NWB_WORK_MARKERS = (
    "_content_edit_",
    "_content_backup_",
    "_replacement_",
    "_processing_backup_",
)


def is_internal_nwb_work_file(path: str | Path) -> bool:
    """Return True for temporary/backup NWB files owned by the application."""
    # Inventory paths can originate on Windows even when a workbook is being
    # inspected elsewhere, so recognize both slash styles explicitly.
    normalized = str(path).replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    name = (parts[-1] if parts else normalized).casefold()

    if name.endswith((".nwb.tmp", ".nwb.bak")):
        return True
    if any(part.casefold() == ".nwb_workbench_temp" for part in parts):
        return True
    if name.endswith(".before_units_repair.nwb"):
        return True

    return bool(
        name.startswith(".")
        and name.endswith(".nwb")
        and any(marker in name for marker in _INTERNAL_NWB_WORK_MARKERS)
    )


def remove_internal_nwb_work_rows(
    dataframe: pd.DataFrame,
    *,
    path_column: str = "file_path",
) -> pd.DataFrame:
    """Keep NWB inputs only, excluding internal work files."""
    if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
        return dataframe.copy() if isinstance(dataframe, pd.DataFrame) else pd.DataFrame()
    if path_column not in dataframe.columns:
        return dataframe.copy()

    valid = dataframe[path_column].map(
        lambda value: pd.notna(value) and str(value).lower().endswith(".nwb")
        and not is_internal_nwb_work_file(value)
    )
    return dataframe.loc[valid].copy().reset_index(drop=True)


# -----------------------------
# GUI helpers
# -----------------------------
def select_folder() -> str:
    root = tk.Tk()
    root.title("Select Folder")
    root.geometry("0x0+0+0")
    root.lift()
    root.attributes("-topmost", True)
    root.update()
    folder_path = filedialog.askdirectory(parent=root)
    root.destroy()
    return folder_path


def excel_safe_datetime(dt):
    if pd.isna(dt):
        return None
    try:
        if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
    except Exception:
        pass
    return dt


# -----------------------------
# Inventory file checks
# -----------------------------
def check_for_extra_inventory(root_path: str) -> None:
    root = Path(root_path)
    inventory_files = list(root.rglob(INVENTORY_FILENAME))

    main_inventory = root / INVENTORY_FILENAME
    extra_inventories = [p for p in inventory_files if p.resolve() != main_inventory.resolve()]

    if not extra_inventories:
        return

    msg = (
        f"Multiple '{INVENTORY_FILENAME}' files were found in subfolders.\n\n"
        "Only the inventory at the root folder is used by this system.\n\n"
        "Do you want to delete the extra inventory file(s)?"
    )
    response = messagebox.askyesno("Extra Inventory Detected", msg)
    if not response:
        return

    errors = []
    for file_path in extra_inventories:
        try:
            os.remove(file_path)
            print(f"Deleted: {file_path}")
        except Exception as e:
            errors.append(f"{file_path} -> {e}")

    if errors:
        messagebox.showerror(
            "Delete Errors",
            "Some files could not be deleted:\n\n" + "\n".join(errors),
        )


def inventory_is_compatible(inventory_path: Path) -> bool:
    """
    Compatible if:
    - inventory sheet exists
    - required columns exist
    - schema_version matches (if meta sheet present)
    """
    try:
        xl = pd.ExcelFile(inventory_path)
        if INVENTORY_SHEET not in xl.sheet_names:
            return False

        df = _pp_read_excel(inventory_path, sheet_name=INVENTORY_SHEET)
        if not REQUIRED_INVENTORY_COLUMNS.issubset(set(df.columns)):
            return False

        if META_SHEET in xl.sheet_names:
            meta = _pp_read_excel(inventory_path, sheet_name=META_SHEET)
            if "schema_version" in meta.columns and len(meta) > 0:
                sv = str(meta.loc[0, "schema_version"])
                if sv != SCHEMA_VERSION:
                    return False

        return True
    except Exception:
        return False


# -----------------------------
# Trial filter helpers
# -----------------------------
def _json_safe_value(v):
    if pd.isna(v):
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (int, float, bool, str)):
        return v
    return str(v)


def _round_or_none(v, ndigits: int = 3):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return round(float(v), ndigits)

def _ceil_or_none(v, ndigits: int = 3):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    factor = 10 ** ndigits
    return round(math.ceil(float(v) * factor) / factor, ndigits)

def _floor_or_none(v, ndigits: int = 3):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    factor = 10 ** ndigits
    return round(math.floor(float(v) * factor) / factor, ndigits)


def classify_trial_series(s: pd.Series) -> dict | None:
    """
    Classify one trial column as either:
    - categorical -> multi-select in GUI
    - range -> min/max in GUI
    """
    if s is None:
        return None

    has_nan = bool(s.isna().any())
    s_non_na = s.dropna()

    if len(s_non_na) == 0:
        return None

    n_unique = int(s_non_na.nunique())

    if pd.api.types.is_bool_dtype(s_non_na):
        vals = sorted([str(v) for v in s_non_na.astype(str).unique().tolist()], key=str.lower)
        return {
            "filter_kind": "categorical",
            "dtype_detected": "bool",
            "n_unique": n_unique,
            "unique_values_json": json.dumps(vals),
            "min_value": None,
            "max_value": None,
            "has_nan": has_nan,
        }

    if pd.api.types.is_numeric_dtype(s_non_na):
        if n_unique <= 20:
            vals = [_json_safe_value(v) for v in s_non_na.unique().tolist()]

            def _sort_key(x):
                try:
                    return (0, float(x))
                except Exception:
                    return (1, str(x).lower())

            vals = sorted(vals, key=_sort_key)

            return {
                "filter_kind": "categorical",
                "dtype_detected": "int" if pd.api.types.is_integer_dtype(s_non_na) else "float",
                "n_unique": n_unique,
                "unique_values_json": json.dumps(vals),
                "min_value": None,
                "max_value": None,
                "has_nan": has_nan,
            }

        return {
            "filter_kind": "range",
            "dtype_detected": "int" if pd.api.types.is_integer_dtype(s_non_na) else "float",
            "n_unique": n_unique,
            "unique_values_json": None,
            "min_value": _floor_or_none(s_non_na.min()),
            "max_value": _ceil_or_none(s_non_na.max()),
            "has_nan": has_nan,
        }

    vals = sorted([str(v) for v in s_non_na.astype(str).unique().tolist()], key=str.lower)
    return {
        "filter_kind": "categorical",
        "dtype_detected": "string",
        "n_unique": n_unique,
        "unique_values_json": json.dumps(vals),
        "min_value": None,
        "max_value": None,
        "has_nan": has_nan,
    }


def summarize_trials_table(file_path: str, trials_df: pd.DataFrame) -> list[dict]:
    rows = []

    for col in trials_df.columns:
        info = classify_trial_series(trials_df[col])
        if info is None:
            continue

        rows.append({
            "file_path": str(file_path),
            "trial_column": str(col),
            **info,
        })

    if "start_time" in trials_df.columns and "stop_time" in trials_df.columns:
        start = pd.to_numeric(trials_df["start_time"], errors="coerce")
        stop = pd.to_numeric(trials_df["stop_time"], errors="coerce")

        # Synthetic range filter: trial_len = stop_time - start_time
        trial_len = stop - start
        valid_trial_len = trial_len.dropna()

        if len(valid_trial_len):
            rows.append({
                "file_path": str(file_path),
                "trial_column": "trial_len",
                "filter_kind": "range",
                "dtype_detected": "float",
                "n_unique": int(valid_trial_len.nunique()),
                "unique_values_json": None,
                "min_value": _floor_or_none(valid_trial_len.min()),
                "max_value": _ceil_or_none(valid_trial_len.max()),
                "has_nan": bool(trial_len.isna().any()),
            })

        # Synthetic range filter: trial_time
        # min = previous stop_time - current start_time
        # max = next start_time - current start_time
        prev_stop = stop.shift(1)
        next_start = start.shift(-1)

        trial_time_min = prev_stop - start
        trial_time_max = next_start - start

        valid_min = trial_time_min.dropna()
        valid_max = trial_time_max.dropna()
        valid_all = pd.concat([valid_min, valid_max], axis=0).dropna()

        if len(valid_min) or len(valid_max):
            rows.append({
                "file_path": str(file_path),
                "trial_column": "trial_time",
                "filter_kind": "range",
                "dtype_detected": "float",
                "n_unique": int(valid_all.nunique()) if len(valid_all) else 0,
                "unique_values_json": None,
                "min_value": _floor_or_none(valid_min.min()) if len(valid_min) else None,
                "max_value": _ceil_or_none(valid_max.max()) if len(valid_max) else None,
                "has_nan": bool(trial_time_min.isna().any() or trial_time_max.isna().any()),
            })

    return rows


def build_trial_filter_catalog(trial_index_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build one global catalog row per trial column for the GUI.
    """
    empty_cols = [
        "trial_column",
        "filter_kind",
        "dtype_detected",
        "unique_values_json",
        "global_min",
        "global_max",
        "n_files_present",
    ]

    if trial_index_df is None or trial_index_df.empty:
        return pd.DataFrame(columns=empty_cols)

    rows = []

    for col_name, g in trial_index_df.groupby("trial_column", dropna=False):
        col_name = str(col_name)

        kinds = set(g["filter_kind"].dropna().astype(str))
        dtype_values = g["dtype_detected"].dropna().astype(str)
        dtype_detected = dtype_values.iloc[0] if len(dtype_values) else "string"

        if "range" in kinds:
            mins = pd.to_numeric(g["min_value"], errors="coerce").dropna()
            maxs = pd.to_numeric(g["max_value"], errors="coerce").dropna()

            rows.append({
                "trial_column": col_name,
                "filter_kind": "range",
                "dtype_detected": dtype_detected,
                "unique_values_json": None,
                "global_min": _floor_or_none(mins.min()) if len(mins) else None,
                "global_max": _ceil_or_none(maxs.max()) if len(maxs) else None,
                "n_files_present": int(g["file_path"].nunique()),
            })
        else:
            values = set()
            for raw in g["unique_values_json"].dropna():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        for v in parsed:
                            values.add(_json_safe_value(v))
                except Exception:
                    pass

            def _sort_key(v):
                try:
                    return (0, float(v))
                except Exception:
                    return (1, str(v).lower())

            rows.append({
                "trial_column": col_name,
                "filter_kind": "categorical",
                "dtype_detected": dtype_detected,
                "unique_values_json": json.dumps(sorted(values, key=_sort_key)),
                "global_min": None,
                "global_max": None,
                "n_files_present": int(g["file_path"].nunique()),
            })

    return pd.DataFrame(rows, columns=empty_cols)


# -----------------------------
# NWB summarization + content index
# -----------------------------
def _type_name(obj) -> str:
    try:
        return obj.__class__.__name__
    except Exception:
        return "Unknown"


def _unit_project_path_segment(project) -> str:
    text = str(project or "").strip()
    text = re.sub(r'[\\/:*?"<>|]', "_", text).strip("._")
    return text or "unknown"


def _unit_sorting_project_counts(units) -> dict[str, int]:
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
    counts: dict[str, int] = {}
    for project, count in series.value_counts(dropna=False).items():
        counts[str(project or "").strip()] = int(count)
    return counts


def _append_recursive_items(
    content_rows: list[dict],
    obj,
    base_path: str,
    file_path: str,
    *,
    container: str = "",
    module: str = "",
    depth: int = 0,
    max_depth: int = 3,
    visited: set[int] | None = None,
):
    if visited is None:
        visited = set()

    if obj is None or depth > max_depth:
        return

    obj_id = id(obj)
    if obj_id in visited:
        return
    visited.add(obj_id)

    try:
        items = obj.items()
    except Exception:
        return

    for key, child in items:
        child_path = f"{base_path}/{key}"
        content_rows.append(
            {
                "file_path": file_path,
                "nwb_path": child_path,
                "container": container,
                "module": module,
                "name": str(key),
                "neurodata_type": _type_name(child),
            }
        )
        _append_recursive_items(
            content_rows,
            child,
            child_path,
            file_path,
            container=container,
            module=module,
            depth=depth + 1,
            max_depth=max_depth,
            visited=visited,
        )

def _append_container_items(
    content_rows: list[dict],
    container_obj,
    base_path: str,
    file_path: str,
    *,
    container: str = "",
    module: str = "",
):
    if container_obj is None:
        return

    try:
        items = container_obj.items()
    except Exception:
        items = None

    if items is not None:
        for key, obj in items:
            content_rows.append(
                {
                    "file_path": file_path,
                    "nwb_path": f"{base_path}/{key}",
                    "container": container,
                    "module": module,
                    "name": str(key),
                    "neurodata_type": _type_name(obj),
                }
            )
        return

    try:
        for key in container_obj.keys():
            try:
                obj = container_obj[key]
            except Exception:
                obj = None
            content_rows.append(
                {
                    "file_path": file_path,
                    "nwb_path": f"{base_path}/{key}",
                    "container": container,
                    "module": module,
                    "name": str(key),
                    "neurodata_type": _type_name(obj),
                }
            )
    except Exception:
        return




def _json_list_cell(values) -> str:
    cleaned = []
    for value in values or []:
        text = "" if value is None else str(value).strip()
        if not text or text.lower() in ("nan", "none", "nat"):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned)


def _bool_from_cell(value) -> bool:
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer, float, np.floating)):
        try:
            return bool(int(value))
        except Exception:
            return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "bad"}


def _summarize_bad_channels_from_electrodes(nwb) -> dict:
    """Return bad-channel metadata for the inventory row.

    The GUI can then compare bad-channel lists without opening every NWB file
    when creating a concatenated spike-sorting recipe.
    """
    out = {
        "has_bad_channel_column": False,
        "number_of_bad_channels": 0,
        "bad_channels": json.dumps([]),
    }

    electrodes = getattr(nwb, "electrodes", None)
    if electrodes is None:
        return out

    try:
        df = electrodes.to_dataframe()
    except Exception:
        return out

    if df is None or getattr(df, "empty", True):
        return out

    if "bad_channel" not in df.columns:
        return out

    out["has_bad_channel_column"] = True

    bad = df["bad_channel"].fillna(False)
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
        if col in df.columns:
            label_col = col
            break

    labels = []
    for idx, is_bad in bad.items():
        if not bool(is_bad):
            continue
        if label_col is not None:
            label = df.loc[idx, label_col]
        else:
            label = idx
        text = "" if label is None else str(label).strip()
        if not text or text.lower() in ("nan", "none", "nat"):
            text = str(idx)
        if text not in labels:
            labels.append(text)

    out["number_of_bad_channels"] = int(len(labels))
    out["bad_channels"] = _json_list_cell(labels)
    return out

def summarize_nwb(nwb_path: Path) -> tuple[dict, list[dict], list[dict]]:
    """
    Returns:
      - inventory row dict
      - list of content index rows
      - list of trial filter index rows
    """
    inv = {
        "file_path": str(nwb_path),
        "file_type": "nwb",
        "identifier": None,
        "session_description": None,
        "session_start_time": None,
        "protocol": None,
        "keywords": None,
        "subject_id": None,
        "species": None,
        "genotype": None,
        "sex": None,
        "nwb_readable": False,
        "read_error": None,
        "has_trials": False,
        "number_of_trials": None,
        "median_trial_len": None,
        "min_trial_len": None,
        "max_trial_len": None,
        "has_bad_channel_column": None,
        "number_of_bad_channels": None,
        "bad_channels": None,
    }

    content_rows: list[dict] = []
    trial_rows: list[dict] = []

    try:
        with NWBHDF5IO(str(nwb_path), "r", load_namespaces=True) as io:
            nwb = io.read()
            inv["nwb_readable"] = True

            inv["identifier"] = getattr(nwb, "identifier", None)
            inv["session_description"] = getattr(nwb, "session_description", None)
            inv["session_start_time"] = excel_safe_datetime(getattr(nwb, "session_start_time", None))

            subj = getattr(nwb, "subject", None)
            if subj is not None:
                inv["subject_id"] = getattr(subj, "subject_id", None)
                inv["species"] = getattr(subj, "species", None)
                inv["genotype"] = getattr(subj, "genotype", None)
                inv["sex"] = getattr(subj, "sex", None)

            inv["protocol"] = getattr(nwb, "protocol", None)

            keywords = getattr(nwb, "keywords", None)
            try:
                keywords = None if keywords is None else list(keywords[:])
            except Exception:
                try:
                    keywords = None if keywords is None else list(keywords)
                except Exception:
                    keywords = None
            inv["keywords"] = keywords

            bad_channel_info = _summarize_bad_channels_from_electrodes(nwb)
            inv.update(bad_channel_info)

            fp = str(nwb_path)

            _append_container_items(
                content_rows,
                getattr(nwb, "acquisition", None),
                "acquisition",
                fp,
                container="acquisition",
                module="",
            )
            
            _append_container_items(
                content_rows,
                getattr(nwb, "stimulus", None),
                "stimulus",
                fp,
                container="stimulus",
                module="",
            )

            _append_container_items(
                content_rows,
                getattr(nwb, "events", None),
                "events",
                fp,
                container="events",
                module="",
            )
            
            intervals = getattr(nwb, "intervals", None)
            if intervals is not None:
                try:
                    for key, obj in intervals.items():
                        if str(key) == "trials":
                            continue
                        content_rows.append(
                            {
                                "file_path": fp,
                                "nwb_path": f"intervals/{key}",
                                "container": "intervals",
                                "module": "",
                                "name": str(key),
                                "neurodata_type": _type_name(obj),
                            }
                        )
                except Exception:
                    pass
            
            processing = getattr(nwb, "processing", None)
            if processing is not None:
                try:
                    for mod_name, mod in processing.items():
                        data_intf = getattr(mod, "data_interfaces", None)
                        if data_intf is None:
                            continue
                        _append_recursive_items(
                            content_rows,
                            data_intf,
                            f"processing/{mod_name}",
                            fp,
                            container="processing",
                            module=str(mod_name),
                            max_depth=4,
                        )
                except Exception:
                    pass
            
            if getattr(nwb, "units", None) is not None:
                project_counts = _unit_sorting_project_counts(nwb.units)
                if not project_counts:
                    project_counts = {"": 0}
                for sorting_project, _unit_count in sorted(project_counts.items(), key=lambda kv: str(kv[0]).lower()):
                    sorting_project = str(sorting_project or "").strip()
                    content_rows.append(
                        {
                            "file_path": fp,
                            "nwb_path": f"units/{_unit_project_path_segment(sorting_project)}" if sorting_project else "units",
                            "container": "units",
                            "module": sorting_project,
                            "name": "units",
                            "neurodata_type": _type_name(nwb.units),
                            "sorting_project": sorting_project,
                        }
                    )
            
            if getattr(nwb, "trials", None) is not None:
                content_rows.append(
                    {
                        "file_path": fp,
                        "nwb_path": "trials",
                        "container": "trials",
                        "module": "",
                        "name": "trials",
                        "neurodata_type": _type_name(nwb.trials),
                    }
                )

                try:
                    trials_df = nwb.trials.to_dataframe()
                    trial_rows = summarize_trials_table(fp, trials_df)

                    inv["has_trials"] = True
                    inv["number_of_trials"] = int(len(trials_df))

                    if "start_time" in trials_df.columns and "stop_time" in trials_df.columns:
                        start = pd.to_numeric(trials_df["start_time"], errors="coerce")
                        stop = pd.to_numeric(trials_df["stop_time"], errors="coerce")
                        trial_len = (stop - start).dropna()
                        if len(trial_len):
                            inv["median_trial_len"] = float(trial_len.median())
                            inv["min_trial_len"] = float(trial_len.min())
                            inv["max_trial_len"] = float(trial_len.max())

                except Exception as e:
                    print(f"Could not summarize trials for {nwb_path}: {e}")
                    inv["has_trials"] = True

    except Exception as e:
        inv["read_error"] = str(e)
        print(f"Error reading {nwb_path}: {e}")

    return inv, content_rows, trial_rows


# -----------------------------
# Main scanner
# -----------------------------
def inventory_path_key(path):
    """Normalize aliases and Windows case for duplicate detection."""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def collect_scan_inputs(paths):
    """Collect unique NWB files, recursively expanding chosen folders."""
    found = {}
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Selected input does not exist: {path}")
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() == ".nwb" and not is_internal_nwb_work_file(candidate):
                found.setdefault(inventory_path_key(candidate), candidate)
    files = sorted(found.values(), key=lambda p: str(p).lower())
    return files


def merge_scanned_rows(previous, scanned, refreshed_paths):
    """Replace all indexed rows for refreshed files and retain unrelated files."""
    previous = remove_internal_nwb_work_rows(previous)
    keys = {inventory_path_key(p) for p in refreshed_paths}
    if "file_path" in previous.columns:
        previous = previous.loc[~previous["file_path"].map(
            lambda p: inventory_path_key(p) if pd.notna(p) else None
        ).isin(keys)]
    # Selected inputs are already unique by normalized path, and their old
    # rows were removed above. Do not hash whole rows: NWB metadata can contain
    # lists (e.g. keywords or bad channels), dictionaries, or arrays.
    return pd.concat([previous, scanned], ignore_index=True)


def normalize_inventory_tables(sheets):
    """Migrate stored tables to the NWB-only inventory schema.

    Legacy column names occur only here so older workbooks can be cleaned.
    This does not open or interpret any non-NWB input file.
    """
    cleaned = {}
    for name, dataframe in sheets.items():
        frame = (remove_internal_nwb_work_rows(dataframe)
                 if name in (INVENTORY_SHEET, CONTENT_SHEET, TRIAL_FILTER_INDEX_SHEET)
                 else dataframe.copy())
        if name == INVENTORY_SHEET:
            frame = frame.drop(columns=["NWB_file_converted", "NWB_associated_file_path"], errors="ignore")
        if name == INVENTORY_SHEET and "file_type" in frame.columns:
            frame["file_type"] = "nwb"
        cleaned[name] = frame
    if TRIAL_FILTER_INDEX_SHEET in cleaned:
        cleaned[TRIAL_FILTER_SHEET] = build_trial_filter_catalog(cleaned[TRIAL_FILTER_INDEX_SHEET])
    if META_SHEET in cleaned:
        meta = cleaned[META_SHEET].copy()
        if not meta.empty:
            meta["schema_version"] = SCHEMA_VERSION
        cleaned[META_SHEET] = meta
    return cleaned


def migrate_inventory_to_nwb_only(path):
    """Clean existing project inventories once, preserving unrelated sheets."""
    path = Path(path)
    if not path.exists():
        return
    original = _pp_read_excel(path, sheet_name=None)
    cleaned = normalize_inventory_tables(original)
    if any(name not in original or not frame.equals(original[name])
           for name, frame in cleaned.items()):
        write_inventory_atomic(path, cleaned)


def write_inventory_atomic(path, sheets):
    """Keep the previous workbook intact if writing or replacement fails."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".inventory_", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    try:
        with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
            writer._nwb_destination = path
            for name, dataframe in sheets.items():
                _pp_to_excel(dataframe, writer, sheet_name=name, index=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scan_directory_gui(
    root_path: str | None = None,
    save: bool = True,
    output_inventory_path=None,
    parent=None,
    force_rebuild: bool = False,
    selected_paths=None,
    append: bool = True,
):
    
    """Scan folders or selected files, merging by path by default.

    force_rebuild refreshes selected inputs; append=False explicitly replaces
    the inventory. Existing files are never deleted before a successful write.
    """
    if root_path is None and selected_paths is None:
        root_path = select_folder()
        if not root_path:
            raise RuntimeError("No folder selected")

    inputs = list(selected_paths) if selected_paths is not None else [root_path]
    if not inputs:
        raise ValueError("No files or folders selected")
    root = Path(root_path) if root_path else Path(inputs[0])
    if not root.is_dir():
        root = root.parent
    inventory_path = Path(output_inventory_path) if output_inventory_path is not None else root / INVENTORY_FILENAME
    nwb_files = collect_scan_inputs(inputs)
    existing = {}
    if append and inventory_path.exists():
        # Do not silently discard an unreadable existing workbook.
        existing = normalize_inventory_tables(_pp_read_excel(inventory_path, sheet_name=None))
        if INVENTORY_SHEET not in existing:
            raise ValueError(f"Existing inventory has no '{INVENTORY_SHEET}' sheet: {inventory_path}")

    gui = None
    
    try:
        if parent is not None and getattr(parent, "winfo_exists", lambda: False)():
            gui = tk.Toplevel(parent)
        else:
            gui = tk.Tk()
    
        gui.title("Scanning NWB Files")
        gui.geometry("720x240")
        gui.lift()
        gui.attributes("-topmost", True)
        gui.update()

        label = tk.Label(gui, text="Preparing scan...", wraplength=690)
        label.pack(pady=10)

        progress = ttk.Progressbar(gui, orient="horizontal", length=650, mode="determinate")
        progress.pack(pady=10)

        file_label = tk.Label(gui, text="", wraplength=690, fg="blue")
        file_label.pack(pady=5)

        gui.update()

        total_files = len(nwb_files)
        total_files = max(total_files, 1)

        inventory_rows: list[dict] = []
        content_rows: list[dict] = []
        trial_rows: list[dict] = []

        processed = 0

        for path in nwb_files:
            label.config(text=f"Scanning NWB: {path.name}")
            file_label.config(text=str(path))
            gui.update()

            inv_row, cont, tr = summarize_nwb(path)
            inventory_rows.append(inv_row)
            content_rows.extend(cont)
            trial_rows.extend(tr)

            processed += 1
            progress["value"] = (processed / total_files) * 100
            gui.update()

        inv_df = pd.DataFrame(inventory_rows)

        for col in REQUIRED_INVENTORY_COLUMNS:
            if col not in inv_df.columns:
                inv_df[col] = None

        numeric_cols = ["number_of_units", "number_of_bad_channels", "number_of_trials", "median_trial_len", "min_trial_len", "max_trial_len"]
        for col in numeric_cols:
            if col in inv_df.columns:
                inv_df[col] = pd.to_numeric(inv_df[col], errors="coerce")

        content_df = pd.DataFrame(
            content_rows,
            columns=["file_path", "nwb_path", "container", "module", "name", "neurodata_type"],
        ).drop_duplicates()

        trial_index_df = pd.DataFrame(trial_rows).drop_duplicates()
        refreshed_paths = [row["file_path"] for row in inventory_rows]
        inv_df = merge_scanned_rows(existing.get(INVENTORY_SHEET), inv_df, refreshed_paths)
        content_df = merge_scanned_rows(existing.get(CONTENT_SHEET), content_df, refreshed_paths)
        trial_index_df = merge_scanned_rows(existing.get(TRIAL_FILTER_INDEX_SHEET), trial_index_df, refreshed_paths)
        trial_filter_catalog_df = build_trial_filter_catalog(trial_index_df)

        save_path = str(inventory_path)
        if save:
            sheets = dict(existing)
            sheets.update({
                INVENTORY_SHEET: inv_df,
                CONTENT_SHEET: content_df,
                TRIAL_FILTER_INDEX_SHEET: trial_index_df,
                TRIAL_FILTER_SHEET: trial_filter_catalog_df,
                META_SHEET: pd.DataFrame([{"schema_version": SCHEMA_VERSION, "created_at": datetime.now()}]),
            })
            write_inventory_atomic(inventory_path, sheets)

        label.config(text="Scan complete!")
        file_label.config(text="")
        progress["value"] = 100
        gui.update()

        gui.update_idletasks()
        gui.update()
        gui.destroy()

        return inv_df, content_df, save_path

    finally:
        try:
            if gui is not None and gui.winfo_exists():
                gui.destroy()
        except Exception:
            pass


# -----------------------------
# CLI entry
# -----------------------------
if __name__ == "__main__":
    root_directory = None
    inv_df, content_df, save_path = scan_directory_gui(root_directory, save=True)
    print("Inventory saved to:", save_path)
    print("Inventory shape:", inv_df.shape)
    print("Content index shape:", content_df.shape)
