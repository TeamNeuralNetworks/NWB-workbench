# -*- coding: utf-8 -*-
"""
Created on Wed Mar 11 14:51:29 2026

@author: Jerome.WANDHAMMER
"""
import ast
import pandas as pd
import numpy as np
import re
from pynwb import NWBHDF5IO
import xarray as xr
from Tools.General.event_alignment import (normalize_alignment, pair_segments, align_signal,
                                           validate_sampling_rates, same_marker)


# Compatibility for plotting code that receives DataArray.coords and expects
# coords.aligned_to to behave like the parent DataArray attr.
# This does not add an xarray coordinate, so unit_data.aligned_to remains the
# string stored in attrs and can still be used as a dimension name.
def _patch_xarray_coordinates_aligned_to_attr():
    try:
        from xarray.core.coordinates import DataArrayCoordinates
    except Exception:
        return

    if getattr(DataArrayCoordinates, "_nwb_gui_aligned_to_patch", False):
        return

    previous_getattr = getattr(DataArrayCoordinates, "__getattr__", None)

    def _coords_getattr(self, name):
        if name == "aligned_to":
            try:
                value = self._data.attrs.get("aligned_to", None)
            except Exception:
                value = None
            if value is not None:
                return value

        if previous_getattr is not None:
            return previous_getattr(self, name)

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    DataArrayCoordinates.__getattr__ = _coords_getattr
    DataArrayCoordinates._nwb_gui_aligned_to_patch = True


_patch_xarray_coordinates_aligned_to_attr()
from pathlib import Path

from GUI.panels.filter_panel import FilterPanel
from GUI.loading_screen import LoadingScreen
from Tools.General.nwb_backend import get_data_by_nwb_path       
from Tools.General.FilteredConditionFileManager import FilteredConditionFileManager


def event_alignment_offsets(event_alignment):
    a = normalize_alignment(event_alignment)
    return a["start_offset_s"], a["end_offset_s"]


def _event_alignment_window_text(event_alignment):
    start_offset, end_offset = event_alignment_offsets(event_alignment)
    a = normalize_alignment(event_alignment)
    return (f"start offset {start_offset} s; end {a['end_event_marker']} "
            f"{a['end_edge']} offset {end_offset} s")


def parse_scalar(x):
    if isinstance(x, str):
        x = x.strip()

        if x == "True":
            return True
        if x == "False":
            return False

        try:
            return int(x)
        except ValueError:
            try:
                return float(x)
            except ValueError:
                return x

    return x


def _empty_trials_like(trial_df):
    """Return an empty trial table while preserving its columns and dtypes."""
    return trial_df.iloc[0:0].copy()


def _parse_inventory_keywords(value):
    """Parse inventory keywords without executing arbitrary Python code."""
    if value is None:
        return []

    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            value = text

    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _timeseries_scale_and_offset(timeseries):
    conversion = getattr(timeseries, "conversion", 1.0)
    offset = getattr(timeseries, "offset", 0.0)
    conversion = 1.0 if conversion is None else float(conversion)
    offset = 0.0 if offset is None else float(offset)
    return conversion, offset


def _read_scaled_timeseries_data(timeseries, selection=None, scaled=True):
    """Read a TimeSeries selection and return values expressed in its NWB unit."""
    if selection is None:
        values = np.asarray(timeseries.data[:])
    else:
        values = np.asarray(timeseries.data[selection])

    if scaled:
        conversion, offset = _timeseries_scale_and_offset(timeseries)
        if conversion == 1.0 and offset == 0.0:
            return values
        return values * conversion + offset
    else:
        return values


def filter_trial_df(filter_dict, trial_df, identifier):
    if trial_df is None:
        raise ValueError("trial_df cannot be None")

    for filter_name, filter_values in filter_dict.items():
        if isinstance(filter_values, dict):
            if filter_name == 'trial_time':
                continue
            elif filter_name == 'trial_len':
                if "start_time" not in trial_df.columns or "stop_time" not in trial_df.columns:
                    return _empty_trials_like(trial_df)
                duration = trial_df['stop_time'] - trial_df['start_time']

                if "min" in filter_values:
                    trial_df = trial_df[duration >= filter_values['min']]
                
                if "max" in filter_values:
                    trial_df = trial_df[duration <= filter_values['max']]
                    
            else:
                if filter_name not in trial_df.columns:
                    return _empty_trials_like(trial_df)
                s = pd.to_numeric(trial_df[filter_name], errors="coerce")
                mask = s.isna()
                
                if "min" in filter_values and "max" in filter_values:
                    mask |= ((s >= filter_values["min"]) & (s <= filter_values["max"]))
                elif "min" in filter_values:
                    mask |= (s >= filter_values["min"])
                elif "max" in filter_values:
                    mask |= (s <= filter_values["max"])
                
                trial_df = trial_df[mask]
                
        elif isinstance(filter_values, list):
            if filter_name not in trial_df.columns:
                return _empty_trials_like(trial_df)

            current_series = trial_df[filter_name]
            if pd.api.types.is_bool_dtype(current_series):
                filter_values = [parse_scalar(v) for v in filter_values]
            elif pd.api.types.is_numeric_dtype(current_series):
                filter_values = [parse_scalar(v) for v in filter_values]
            elif isinstance(current_series.dtype, pd.CategoricalDtype):
                categories = current_series.cat.categories
                if pd.api.types.is_bool_dtype(categories.dtype) or pd.api.types.is_numeric_dtype(categories.dtype):
                    filter_values = [parse_scalar(v) for v in filter_values]
            else:
                inferred = pd.api.types.infer_dtype(current_series.dropna(), skipna=True)
                if inferred in {
                    "boolean",
                    "integer",
                    "floating",
                    "mixed-integer-float",
                    "decimal",
                    "complex",
                }:
                    filter_values = [parse_scalar(v) for v in filter_values]
                else:
                    # String categories must remain strings. For example, "01"
                    # and "1" may be two distinct experimental labels.
                    filter_values = list(filter_values)
                
            trial_df = trial_df[trial_df[filter_name].isin(filter_values)]
        else:
            raise ValueError
    
    filtered_trial_df = trial_df.copy()
    
    if identifier is not None:
        filtered_trial_df['trial_unique_id'] = str(identifier) + "_" + (filtered_trial_df.index + 1).astype(str)
        
    return filtered_trial_df


def get_filtered_index(
    filter_dict,
    trial_df,
    rate=None,
    starting_time=None,
    data_len=None,
    timestamp=None,
):
    if timestamp is None and (rate is None or starting_time is None or data_len is None):
        raise ValueError("get_filtered_index requires either timestamp, or rate + starting_time + data_len")

    trial_df = trial_df.reset_index(drop=True)
    filtered_trial_df = filter_trial_df(filter_dict, trial_df, identifier=None)

    if timestamp is None:
        timestamp = float(starting_time) + np.arange(int(data_len), dtype=float) / float(rate)
    else:
        timestamp = np.asarray(timestamp, dtype=float)

    index_to_keep = np.zeros(len(timestamp), dtype=bool)

    for row_idx, row in trial_df.iterrows():
        
        if row_idx in filtered_trial_df.index:
            assert row.equals(filtered_trial_df.loc[row_idx])
            current_start = float(row["start_time"])
            start_test = timestamp >= current_start
            
            current_stop = float(row["stop_time"])
            stop_test = timestamp <= current_stop
            
            if row_idx > 0:
                previous_stop = float(trial_df.loc[row_idx-1, "stop_time"])
                if previous_stop < current_start:
                    start_test = timestamp > previous_stop
                    
            
            if row_idx < len(trial_df)-1:
                next_start = float(trial_df.loc[row_idx+1, "start_time"])
                if next_start > current_stop:
                    stop_test = timestamp < next_start
    
            mask = start_test & stop_test
            index_to_keep |= mask
        
    filtered_timestamp = timestamp[index_to_keep]
    return index_to_keep, filtered_timestamp

    
def _is_units_table(obj):
    return (
        obj is not None
        and hasattr(obj, "to_dataframe")
        and hasattr(obj, "colnames")
        and "spike_times" in list(obj.colnames)
    )


def _unit_project_path_segment(project):
    text = str(project or "").strip()
    text = re.sub(r'[\\/:*?"<>|]', "_", text).strip("._")
    return text or "unknown"


def _units_selection_project_from_path(nwb_data_path):
    s = str(nwb_data_path or "").strip().strip("/").replace("\\", "/")
    if not s:
        return ""
    if "?" in s:
        base, query = s.split("?", 1)
        if base.strip().lower() == "units":
            for part in query.split("&"):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                if key.strip().lower() == "sorting_project":
                    return value.strip()
    if s.lower().startswith("units/"):
        return s.split("/", 1)[1].strip()
    return ""


def _base_data_name_for_selection(data_type, nwb_data_path):
    s = str(nwb_data_path or "").strip().replace("\\", "/").lower()
    if s == "units" or s.startswith("units/") or s.startswith("units?"):
        return "units"
    return str(data_type or "")


def _filter_units_dataframe_by_sorting_project(units_df, sorting_project):
    project = str(sorting_project or "").strip()
    if not project:
        return units_df
    if "sorting_project" not in units_df.columns:
        return units_df.iloc[0:0].copy()

    target_values = {project.lower(), _unit_project_path_segment(project).lower()}
    series = units_df["sorting_project"].fillna("").astype(str).str.strip()
    keep = series.str.lower().isin(target_values) | series.map(lambda v: _unit_project_path_segment(v).lower()).isin(target_values)
    return units_df.loc[keep].copy()


def _extract_units_dataframe(units, sorting_project=None):
    df = units.to_dataframe().copy()
    df.reset_index(inplace=True)

    if "id" not in df.columns:
        if hasattr(units, "id"):
            try:
                df["id"] = np.asarray(units.id[:])
            except Exception:
                df["id"] = np.arange(len(df))
        else:
            df["id"] = np.arange(len(df))

    if "spike_times" not in df.columns:
        raise ValueError("Units table does not contain a 'spike_times' column")

    df = _filter_units_dataframe_by_sorting_project(df, sorting_project)
    return df


def _safe_unit_coord_text(value, default="unknown"):
    text = str(value if value is not None else "").strip()
    if text.lower() in {"", "nan", "none", "nat"}:
        text = str(default)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return text or str(default)


def _units_xarray_unit_ids(units_df, file_identifier=None):
    """Build globally unique unit labels for xarray.

    NWB unit row ids and sorter unit ids can repeat across files or across
    sorting projects.  xarray aligns coordinates by value during concat, so
    repeated unit_id values make unrelated units collide and can create
    conflicts in per-unit coordinates such as sorting_project or closest_channel.
    """
    labels = []
    used = set()
    file_part = _safe_unit_coord_text(file_identifier, default="file")

    for row_i, row in units_df.iterrows():
        try:
            raw_id = row.get("id", row_i)
        except Exception:
            raw_id = row_i
        try:
            sorter_id = row.get("sorter_unit_id", raw_id)
        except Exception:
            sorter_id = raw_id
        try:
            project = row.get("sorting_project", "")
        except Exception:
            project = ""

        raw_part = _safe_unit_coord_text(raw_id, default=row_i)
        sorter_part = _safe_unit_coord_text(sorter_id, default=raw_part)
        project_part = _safe_unit_coord_text(project, default="all")

        label = f"{file_part}_{project_part}_unit_{sorter_part}"
        if label in used:
            label = f"{label}_nwb_{raw_part}"
        base = label
        suffix = 2
        while label in used:
            label = f"{base}_{suffix}"
            suffix += 1
        used.add(label)
        labels.append(label)

    return np.asarray(labels, dtype=object)


def _filter_spike_times_by_trials(spike_times, trial_df):
    spike_times = np.asarray(spike_times, dtype=float)

    if trial_df is None:
        return spike_times
    if len(trial_df) == 0:
        return spike_times[:0]

    keep_mask = np.zeros(len(spike_times), dtype=bool)

    for _, row in trial_df.iterrows():
        start = float(row["start_time"])
        stop = float(row["stop_time"])
        keep_mask |= (spike_times > start) & (spike_times < stop)

    return spike_times[keep_mask]


def _build_units_dataarray(units, filtered_trial_df=None, metadata=None, attrs=None, sorting_project=None, identifier=None):
    units_df = _extract_units_dataframe(units, sorting_project=sorting_project)
    if units_df.empty:
        return None

    filtered_spikes = []
    for _, row in units_df.iterrows():
        st = _filter_spike_times_by_trials(row["spike_times"], filtered_trial_df)
        filtered_spikes.append(np.asarray(st, dtype=float))

    unit_labels = _units_xarray_unit_ids(units_df, file_identifier=identifier)
    spike_array = np.empty(len(filtered_spikes), dtype=object)
    spike_array[:] = filtered_spikes

    out = xr.DataArray(
        spike_array,
        dims=("unit_id",),
        coords={
            "unit_id": unit_labels,
            "nwb_unit_id": ("unit_id", units_df["id"].to_numpy()),
        },
        attrs=attrs or {},
    )

    for col in units_df.columns:
        if col in ("spike_times",):
            continue
        try:
            values = units_df[col].to_numpy()
            if len(values) == len(units_df):
                out = out.assign_coords({col: ("unit_id", values)})
        except Exception:
            pass

    if metadata is not None:
        for key, value in metadata.items():
            out = out.assign_coords({key: value})

    return out


def _align_units_to_event_marker(
    units,
    event_marker,
    event_marker_to_align_dict,
    identifier,
    filtered_trial_df=None,
    save_metadata=True,
    end_event_marker=None,
    all_trials=None,
):
    if filtered_trial_df is not None and len(filtered_trial_df) == 0:
        return None

    units_df = _extract_units_dataframe(units, sorting_project=event_marker_to_align_dict.get("sorting_project"))
    if units_df.empty:
        return None

    a = normalize_alignment(event_marker_to_align_dict)
    if end_event_marker is None and same_marker(a):
        end_event_marker = event_marker
    segments, a = pair_segments(event_marker, end_event_marker, a,
                               trials=all_trials, selected_trials=filtered_trial_df)
    if segments.empty:
        return None
    event_marker_to_align_dict = a
    window_start_offset, window_stop_offset = event_alignment_offsets(a)
    additional_metadata_df = segments
    kept_event_idx = list(range(len(segments)))
    aligned = np.empty((len(units_df), len(segments)), dtype=object)
    for ev_idx, row in enumerate(segments.itertuples()):
        for unit_pos, (_, unit) in enumerate(units_df.iterrows()):
            st = np.asarray(unit["spike_times"], dtype=float)
            aligned[unit_pos, ev_idx] = st[(st >= row.segment_start_time) &
                                          (st < row.segment_stop_time)] - row.reference_time

    if len(kept_event_idx) == 0:
        return None

    aligned = aligned[:, kept_event_idx]
    kept_event_numbers = [f'{identifier}_{int(n)}' for n in segments.event_number] 

    unit_labels = _units_xarray_unit_ids(units_df, file_identifier=identifier)
    coords = {
        "unit_id": unit_labels,
        "nwb_unit_id": ("unit_id", units_df["id"].to_numpy()),
        event_marker_to_align_dict["event_marker"]: kept_event_numbers,
    }

    for col in units_df.columns:
        if col in ("spike_times",):
            continue
        try:
            values = units_df[col].to_numpy()
            if len(values) == len(units_df):
                coords[col] = ("unit_id", values)
        except Exception:
            pass

    for col in ("reference_time", "end_event_time", "segment_start_time", "segment_stop_time",
                "segment_duration_s", "end_event_relative_s", "segment_stop_relative_s"):
        coords[col] = (a["event_marker"], segments[col].to_numpy())
    if save_metadata and additional_metadata_df is not None:
        kept_meta = additional_metadata_df.iloc[kept_event_idx].reset_index(drop=True)
        for col in kept_meta.columns:
            coords[col] = (
                event_marker_to_align_dict["event_marker"],
                kept_meta[col].to_numpy(),
            )

    return xr.DataArray(
        aligned,
        dims=("unit_id", event_marker_to_align_dict["event_marker"]),
        coords=coords,
        attrs={
            "data_type": "units",
            "end_event_marker": a["end_event_marker"],
            "end_edge": a["end_edge"],
            "alignment_version": 2,
            "representation": "spike_times_relative" if event_marker is not None else "spike_times",
            'aligned_to': event_marker_to_align_dict["event_marker"],
            'alignent_edge': event_marker_to_align_dict["edge"],
            'start_offset_s': window_start_offset,
            'end_offset_s': window_stop_offset,
            # Legacy attribute aliases already stored signed window limits.
            'pad_before_s': window_start_offset,
            'pad_after_s': window_stop_offset
        },
    )

def _valid(value):
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return True


def _add_if_valid(d, key, value):
    if _valid(value):
        if value != 'nan':
            d[key] = value

def extract_nwb_metadata_for_xarray(nwbfile):
    meta = {}

    # session level
    _add_if_valid(meta, "protocol", getattr(nwbfile, "protocol", None))
    # _add_if_valid(meta, "session_description", getattr(nwbfile, "session_description", None))

    # subject
    subject = getattr(nwbfile, "subject", None)
    if subject is not None:
        _add_if_valid(meta, "subject_id", getattr(subject, "subject_id", None))
        # _add_if_valid(meta, "age", getattr(subject, "age", None))
        # _add_if_valid(meta, "subject_description", getattr(subject, "description", None))
        # _add_if_valid(meta, "genotype", getattr(subject, "genotype", None))
        _add_if_valid(meta, "sex", getattr(subject, "sex", None))
        # _add_if_valid(meta, "species", getattr(subject, "species", None))
        # _add_if_valid(meta, "strain", getattr(subject, "strain", None))
        # _add_if_valid(meta, "weight", getattr(subject, "weight", None))
    
    
    # keywords
    keywords = getattr(nwbfile, "keywords", None)
    if keywords is not None:
        for kw in keywords:
            if kw is None:
                continue
    
            kw = str(kw).strip()
            if not kw:
                continue
    
            if ":" in kw:
                key, value = kw.split(":", 1)
                key = key.strip()
                value = value.strip()
    
                if key:
                    _add_if_valid(meta, key, value if value != "" else True)
            else:
                _add_if_valid(meta, kw, True)
                
    return meta

def get_filtered_data(
    condition_filter,
    data_types,
    nwb_data_path,
    content_df,
    metadata_df=None,
    align_to_events=False,
    event_marker_to_align_dict=None,
    loading_screen=True,
    parent=None,
    save_metadata=True,
    save=False,
    project_path=None,
    results=None,
    log_callback=None,
    progress_callback=None,
    status_callback=None,
):
    def _safe_callback(callback, *args, **kwargs):
        if not callable(callback):
            return
        try:
            callback(*args, **kwargs)
        except Exception:
            pass

    def _short_file_name(path):
        try:
            return Path(str(path)).name or str(path)
        except Exception:
            return str(path)

    def _format_log_extra(extra):
        pieces = []
        for key in ("condition_name", "data_type", "file_path", "current", "total", "remaining"):
            if key not in extra:
                continue
            value = extra.get(key)
            if value is None:
                continue
            if key == "file_path":
                value = _short_file_name(value)
                pieces.append(f"file={value}")
            elif key == "condition_name":
                pieces.append(f"condition={value}")
            else:
                pieces.append(f"{key}={value}")
        return " | " + " | ".join(pieces) if pieces else ""

    def _log(message, level="INFO", **extra):
        # Keep the structured information in AnalysisResult, but keep the GUI log readable.
        show_context = bool(extra.pop("show_context", False)) or str(level).upper() != "INFO"
        if results is not None:
            results.add_log(message, level=level, **extra)
        if callable(log_callback):
            prefix = "" if str(level).upper() == "INFO" else f"{level}: "
            context = _format_log_extra(extra) if show_context else ""
            _safe_callback(log_callback, prefix + str(message) + context)

    def _progress(value=None, total=None, text=None, done=False, indeterminate=False, **extra):
        payload = dict(extra)
        if value is not None:
            payload["value"] = int(value)
        if total is not None:
            payload["total"] = int(total)
        if text is not None:
            payload["text"] = str(text)
        if done:
            payload["done"] = True
        if indeterminate:
            payload["indeterminate"] = True
        if callable(progress_callback):
            _safe_callback(progress_callback, payload)
        elif callable(status_callback):
            _safe_callback(status_callback, payload)
    
    
    excluded = []
    if metadata_df is not None:
        excluded = list(getattr(FilterPanel, "EXCLUDED_COLUMNS", set()) or set())
        excluded = [current_excluded for current_excluded in excluded if current_excluded in metadata_df.columns]
     
    data_manager = FilteredConditionFileManager(
                                            condition_filter,
                                            data_types,
                                            nwb_data_path,
                                            project_path,
                                            align_to_events,
                                            event_marker_to_align_dict)
    
    total_file_to_processed = int(data_manager.data_df['nwb_files_nb'].sum())    
    total_nb_of_file_processed = 0
    _progress(value=0, total=total_file_to_processed, text="Searching cache...")
    
    if loading_screen:
        loading = LoadingScreen(parent=parent, title="Filtering and extracting data from NWB files")
        loading.open(total=total_file_to_processed, initial_text="Searching cache...")
        loading.post("status", {"text": "Searching cache..."})
        loading.refresh()
    else:
        loading = None
        
    for data_condition_row_index, data_condition_row in data_manager.data_df.iterrows():
        
        nb_of_file_processed = 0
        current_data_type = data_condition_row['data_type']
        current_nwb_data_path = data_manager.get_data_nwb_path(current_data_type)
        current_base_data_type = _base_data_name_for_selection(current_data_type, current_nwb_data_path)
        current_sorting_project = _units_selection_project_from_path(current_nwb_data_path)
        current_condition_filter_name = data_condition_row['condition']
        trial_filter_dict = condition_filter[current_condition_filter_name]['trial_filter_dict']
        has_trial_filter = trial_filter_dict is not None and len(trial_filter_dict) > 0
        
        filtered_data = None
        current_row_file_total = int(data_condition_row['nwb_files_nb'])
        current_text = f"Extracting {current_data_type} | condition: {current_condition_filter_name} | files: {current_row_file_total}"
        _log("")
        _log(current_text, condition_name=current_condition_filter_name, data_type=current_data_type)
        _progress(value=total_nb_of_file_processed, total=total_file_to_processed, text=current_text)
        
        if loading is not None:
            loading.post("log", {"text": current_text})
            loading.refresh()
        
        if data_condition_row['saved_on_disk']:
            total_nb_of_file_processed += current_row_file_total
            status_text = f"  cached: {current_row_file_total} file(s) already on disk"
            _log(status_text, condition_name=current_condition_filter_name, data_type=current_data_type)
            _progress(value=total_nb_of_file_processed, total=total_file_to_processed, text=status_text)
            if loading is not None:
                loading.post("status", {"text": status_text})
                loading.post("log", {"text": status_text + "\n"})
                loading.post("progress", {"i": total_nb_of_file_processed})
                loading.refresh()
            continue
        
        else:
            
            for nwb_file_path in condition_filter[current_condition_filter_name]['file_path_list']:
                
                nb_of_file_processed += 1
                total_nb_of_file_processed += 1
                remaining_file_nb = max(0, total_file_to_processed - total_nb_of_file_processed)
                current_file_name = _short_file_name(nwb_file_path)
                status_text = f"  [{total_nb_of_file_processed}/{total_file_to_processed}] {current_file_name} ({remaining_file_nb} remaining)"
                _log(status_text)
                _progress(
                    value=total_nb_of_file_processed,
                    total=total_file_to_processed,
                    text=status_text,
                    file_path=str(nwb_file_path),
                    condition_name=current_condition_filter_name,
                    data_type=current_data_type,
                    current=total_nb_of_file_processed,
                    remaining=remaining_file_nb,
                )
                
                if loading is not None:
                    loading.post("status", {"text": f"Processing file {total_nb_of_file_processed}/{total_file_to_processed}"})
                    loading.post("file", {"path": nwb_file_path})
                    loading.post("log", {"text": f"  {nb_of_file_processed}/{current_row_file_total} {nwb_file_path}"})
                    loading.post("progress", {"i": total_nb_of_file_processed})
                    loading.refresh()
                
                current_metadata = None
                if metadata_df is not None and 'file_path' in metadata_df.columns:
                    metadata_rows = metadata_df[metadata_df['file_path'].astype(str) == str(nwb_file_path)]
                    if not metadata_rows.empty:
                        current_metadata = metadata_rows.iloc[0].copy()
                current_nwb_file_content = content_df[content_df['file_path'] == nwb_file_path]
                
                content_names = current_nwb_file_content['name'].astype(str).str.strip().tolist() if 'name' in current_nwb_file_content else []
                content_paths = current_nwb_file_content['nwb_path'].astype(str).str.strip().str.replace("\\", "/", regex=False).tolist() if 'nwb_path' in current_nwb_file_content else []
                has_requested_data = current_base_data_type in content_names
                if not has_requested_data and str(current_nwb_data_path or "").strip():
                    requested_path = str(current_nwb_data_path).strip().replace("\\", "/")
                    has_requested_data = requested_path in content_paths
                if not has_requested_data and current_base_data_type == "units":
                    has_requested_data = any(str(p).lower().startswith("units") for p in content_paths) or "units" in [str(n).lower() for n in content_names]

                if not has_requested_data:
                    if loading_screen:
                        loading.post("log", {"text": f"  {nb_of_file_processed}/{data_condition_row['nwb_files_nb']} nwb file does not contain {current_data_type}\n"})
                        loading.refresh()
                    
                    _log(
                        f"nwb file does not contain {current_data_type}",
                        level="WARNING",
                        file_path=str(nwb_file_path),
                        condition_name=current_condition_filter_name,
                        data_type=current_data_type,
                    )
                    continue
                
                with NWBHDF5IO(nwb_file_path, mode='r') as io:
                    nwbfile = io.read()
    
                    if nwbfile.trials is not None:
                        current_trials = nwbfile.trials.to_dataframe()
                    else:
                        current_trials = None
                        filtered_trial = None
    
                    if current_metadata is not None:
                        for current_excluded in excluded:
                            if current_excluded in current_metadata.index:
                                current_metadata = current_metadata.drop(current_excluded)
                        if 'keywords' in current_metadata:
                            for keyword in _parse_inventory_keywords(current_metadata['keywords']):
                                if isinstance(keyword, str):
                                    if ':' in keyword:
                                        value = keyword.split(":", 1)[1]
                                        keyword = keyword.split(':')[0]
                                        current_metadata[keyword] = value
                                else:
                                    current_metadata[str(keyword)] = True
                            del current_metadata['keywords']
    
                    if align_to_events:
                        assert event_marker_to_align_dict is not None, 'If align_to_events, event_marker_to_align_dict is required'
                        event_marker_to_align_dict = normalize_alignment(event_marker_to_align_dict)
                        event_marker = get_data_by_nwb_path(nwbfile, event_marker_to_align_dict['event_path'])
                        end_event_marker = get_data_by_nwb_path(nwbfile, event_marker_to_align_dict['end_event_path'])
                        missing = []
                        if event_marker is None:
                            missing.append(
                                f"Start '{event_marker_to_align_dict['event_marker']}' "
                                f"({event_marker_to_align_dict['event_path']})"
                            )
                        if end_event_marker is None:
                            missing.append(
                                f"End '{event_marker_to_align_dict['end_event_marker']}' "
                                f"({event_marker_to_align_dict['end_event_path']})"
                            )
                        if missing:
                            message = "Skipping file: missing alignment marker(s): " + "; ".join(missing)
                            _log(
                                message, level="WARNING", file_path=str(nwb_file_path),
                                condition_name=current_condition_filter_name,
                                data_type=current_data_type,
                            )
                            if loading is not None:
                                loading.post("log", {"text": f"{current_file_name}: {message}"})
                                loading.refresh()
                            continue

    

                    ######################################################################
                    ########################### Handle trial df ##########################
                    if current_trials is not None:
                        if has_trial_filter:
                            filtered_trial = filter_trial_df(trial_filter_dict, current_trials, nwbfile.identifier)
                        else:
                            filtered_trial = current_trials.copy()

                        _log(
                            f"    trials kept: {len(filtered_trial)}/{len(current_trials)}",
                            file_path=str(nwb_file_path),
                            condition_name=current_condition_filter_name,
                            data_type=current_data_type,
                        )

                        if len(filtered_trial) == 0:
                            if has_trial_filter or current_data_type == 'trials':
                                if loading_screen:
                                    loading.post("log", {"text": f"  {nb_of_file_processed}/{data_condition_row['nwb_files_nb']} Filtering resulted in an empty session for filter:{current_condition_filter_name}\n"})
                                    loading.refresh()
                                
                                _log(
                                    "Filtering resulted in an empty session",
                                    level="WARNING",
                                    file_path=str(nwb_file_path),
                                    condition_name=current_condition_filter_name,
                                    data_type=current_data_type,
                                )
                                continue
                        else:
                            filtered_trial['label'] = [current_condition_filter_name] * len(filtered_trial)
                            

                            if current_metadata is not None:
                                for key in current_metadata.index:
                                    filtered_trial[key] = current_metadata[key]

                            if current_data_type == 'trials':

                                if filtered_data is None:
                                    filtered_data = filtered_trial.reset_index(drop=True)
                                else:
                                    missing_metadata = [
                                        columns_name for columns_name in filtered_data.columns
                                        if columns_name not in filtered_trial.columns
                                    ]
                                    if len(missing_metadata) > 0:
                                        for key in missing_metadata:
                                            filtered_trial[key] = False

                                    missing_metadata = [
                                        columns_name for columns_name in filtered_trial.columns
                                        if columns_name not in filtered_data.columns
                                    ]
                                    if len(missing_metadata) > 0:
                                        for key in missing_metadata:
                                            filtered_data[key] = False

                                    filtered_data = pd.concat(
                                        [filtered_data, filtered_trial],
                                        axis=0,
                                        ignore_index=True,
                                        sort=False,
                                    )
                                    del filtered_trial
                                
                                    
                    if current_data_type != 'trials':
                        
                        if filtered_data is None:
                            filtered_data = []
                        
                        current_data = get_data_by_nwb_path(nwbfile, current_nwb_data_path)
                        
                        ######################################################################
                        ############################# Handle Units ###########################
                        if _is_units_table(current_data):

                            if current_trials is not None:
                                if has_trial_filter:
                                    filtered_trial_for_units = filter_trial_df(trial_filter_dict, current_trials, nwbfile.identifier)
                                    if len(filtered_trial_for_units) == 0:
                                        if loading_screen:
                                            loading.post("log", {
                                                "text": f"  [{nb_of_file_processed}/{data_condition_row['nwb_files_nb']}] Filtering resulted in an empty session for filter:{current_condition_filter_name}"
                                            })
                                            loading.refresh()
                                        _log(
                                            "Filtering resulted in an empty session",
                                            level="WARNING",
                                            file_path=str(nwb_file_path),
                                            condition_name=current_condition_filter_name,
                                            data_type=current_data_type,
                                        )
                                        continue
                                elif align_to_events:
                                    filtered_trial_for_units = None
                                else:
                                    filtered_trial_for_units = current_trials.copy()
                            else:
                                filtered_trial_for_units = None
                                if has_trial_filter:
                                    _log(
                                        "Trial filter requested but file has no trials table",
                                        level="WARNING",
                                        file_path=str(nwb_file_path),
                                        condition_name=current_condition_filter_name,
                                        data_type=current_data_type,
                                    )
                                    continue
                        
                            attrs = {
                                "data_type": current_data_type,
                                "condition_name": current_condition_filter_name,
                                "color": condition_filter[current_condition_filter_name]["color"],
                            }
                        
                            if save_metadata:
                                xarray_nwb_metadata = extract_nwb_metadata_for_xarray(nwbfile)
                            else:
                                xarray_nwb_metadata = {}
                        
                            if align_to_events:
                                _log(
                                    f"    aligning units: {event_marker_to_align_dict['event_marker']} {event_marker_to_align_dict['edge']}, window {_event_alignment_window_text(event_marker_to_align_dict)}",
                                    file_path=str(nwb_file_path),
                                    condition_name=current_condition_filter_name,
                                    data_type=current_data_type,
                                )
                                current_filtered_data = _align_units_to_event_marker(
                                    units=current_data,
                                    event_marker=event_marker,
                                    event_marker_to_align_dict={**event_marker_to_align_dict, "sorting_project": current_sorting_project},
                                    filtered_trial_df=filtered_trial_for_units,
                                    save_metadata=save_metadata,
                                    identifier=nwbfile.identifier,
                                    end_event_marker=end_event_marker,
                                    all_trials=current_trials,
                                )
                                if current_filtered_data is None:
                                    if loading_screen:
                                        loading.post("log", {
                                            "text": f"  [{nb_of_file_processed}/{data_condition_row['nwb_files_nb']}] Filtering resulted in an empty session for filter:{current_condition_filter_name}"
                                        })
                                        loading.refresh()
                                    _log(
                                        "Filtering resulted in an empty session",
                                        level="WARNING",
                                        file_path=str(nwb_file_path),
                                        condition_name=current_condition_filter_name,
                                        data_type=current_data_type,
                                    )
                                    continue
                        
                                if save_metadata:
                                    event_dim = event_marker_to_align_dict["event_marker"]
                                    event_n = current_filtered_data.sizes[event_dim]
                                    _log(
                                        f"    aligned events kept: {event_n}",
                                        file_path=str(nwb_file_path),
                                        condition_name=current_condition_filter_name,
                                        data_type=current_data_type,
                                    )
                                    coords = {}
                                    for key, value in xarray_nwb_metadata.items():
                                        coords[key] = (event_dim, [value] * event_n)
                                    current_filtered_data = current_filtered_data.assign_coords(coords)
                        
                            else:
                                current_filtered_data = _build_units_dataarray(
                                    units=current_data,
                                    filtered_trial_df=filtered_trial_for_units,
                                    attrs=attrs,
                                    sorting_project=current_sorting_project,
                                    identifier=nwbfile.identifier,
                                )
                                if current_filtered_data is None:
                                    _log(
                                        f"No units matched sorting project '{current_sorting_project}'",
                                        level="WARNING",
                                        file_path=str(nwb_file_path),
                                        condition_name=current_condition_filter_name,
                                        data_type=current_data_type,
                                    )
                                    continue
                                current_filtered_data = current_filtered_data.expand_dims(file=[nwbfile.identifier])
                        
                                if save_metadata:
                                    coords = {"file_path": ("file", [str(nwb_file_path)])}
                                    for key, value in xarray_nwb_metadata.items():
                                        coords[key] = ("file", [value])
                                    current_filtered_data = current_filtered_data.assign_coords(coords)
                        
                            current_filtered_data.attrs.update(attrs)
                        
                        
                            filtered_data.append(current_filtered_data)
                            del current_filtered_data
                            continue
                        
                        ######################################################################
                        ###################### Handle Time series's like data ##################
                        else:
                            rate = current_data.rate

                            filtered_timestamp = current_data.timestamps
                            if filtered_timestamp is None:
                                  filtered_timestamp = current_data.starting_time + np.arange(current_data.num_samples, dtype=float) / rate
                            else:
                                filtered_timestamp = filtered_timestamp[:]
                            
                            if has_trial_filter:
                                if current_trials is None:
                                    _log(
                                        "Trial filter requested but file has no trials table",
                                        level="WARNING",
                                        file_path=str(nwb_file_path),
                                        condition_name=current_condition_filter_name,
                                        data_type=current_data_type,
                                    )
                                    continue

                                if align_to_events:
                                    # Keep the full recording for cutting aligned windows.
                                    # The trial filter is used later to choose which events are kept,
                                    # but padding before/after the event must remain available.
                                    current_filtered_data = _read_scaled_timeseries_data(current_data)
                                else:
                                    index_to_keep, filtered_timestamp = get_filtered_index(
                                        trial_filter_dict,
                                        current_trials,
                                        timestamp=filtered_timestamp,
                                        rate=rate
                                    )
                                    
                                    current_filtered_data = _read_scaled_timeseries_data(current_data, index_to_keep)
                                    
                                    if len(current_filtered_data) == 0:
                                        if loading_screen:
                                            loading.post("log", {"text": f"  {nb_of_file_processed}/{data_condition_row['nwb_files_nb']} Filtering resulted in an empty session for filter:{current_condition_filter_name}\n"})
                                            loading.refresh()
                                        _log(
                                            "Filtering resulted in an empty session",
                                            level="WARNING",
                                            file_path=str(nwb_file_path),
                                            condition_name=current_condition_filter_name,
                                            data_type=current_data_type,
                                         )
                                        continue
                                
                            else:
                                current_filtered_data = _read_scaled_timeseries_data(current_data)
                            
                            if len(current_data.data.shape) == 2 and 'electrodes' in dir(current_data):
                                electrodes_table = current_data.electrodes.table
                                electrode_indices = current_data.electrodes.data[:]
                                channels = electrodes_table['contact_ids'].data[electrode_indices]

                                dims = ('time', 'channels')
                                coords = {
                                    'time': filtered_timestamp,
                                    'channels': channels,
                                }
                            else:
                                dims = ['time']
                                coords = {'time': filtered_timestamp}

                                if len(current_data.data.shape) > 1:
                                    for axis_nb in range(1, len(current_data.data.shape)):
                                        dims.append(f'axis_{axis_nb}')
                                        coords[f'axis_{axis_nb}'] = list(range(current_data.data.shape[axis_nb]))
                            
                            attrs={
                                'data_type': current_data_type,
                                'condition_name': current_condition_filter_name,
                                'color': condition_filter[current_condition_filter_name]['color']
                            }
                            source_conversion, source_offset = _timeseries_scale_and_offset(current_data)
                            attrs['conversion'] = 1.0
                            attrs['offset'] = 0.0
                            attrs['source_conversion'] = source_conversion
                            attrs['source_offset'] = source_offset
                            attrs['conversion_applied'] = True
                            if rate is not None:
                                attrs['rate'] = rate
                            
                            if 'unit' in dir(current_data):
                                attrs['unit'] = current_data.unit
                            
                            current_filtered_data = xr.DataArray(
                                current_filtered_data,
                                dims=dims,
                                coords=coords,
                                attrs=attrs)
                            
                            if save_metadata:
                                xarray_nwb_metadata = extract_nwb_metadata_for_xarray(nwbfile)
                            
                            if align_to_events:
                                _log(
                                    f"    aligning data: {event_marker_to_align_dict['event_marker']} {event_marker_to_align_dict['edge']}, window {_event_alignment_window_text(event_marker_to_align_dict)}",
                                    file_path=str(nwb_file_path),
                                    condition_name=current_condition_filter_name,
                                    data_type=current_data_type,
                                )
                                current_filtered_data = cut_and_align_to_event_marker(
                                    event_marker=event_marker,
                                    data=current_filtered_data,
                                    end_event_marker=end_event_marker,
                                    all_trials=current_trials,
                                    event_marker_to_align_dict=event_marker_to_align_dict,
                                    save_metadata=save_metadata,
                                    file_identifier=nwbfile.identifier,
                                    trials_df=(
                                        filtered_trial
                                        if (
                                            event_marker_to_align_dict["event_marker"] == "trials"
                                            or (trial_filter_dict is not None and len(trial_filter_dict) > 0)
                                        )
                                        else None
                                    )
                                )
                                if current_filtered_data is None:
                                    if loading_screen:
                                        loading.post("log", {"text": f"  {nb_of_file_processed}/{data_condition_row['nwb_files_nb']} Filtering resulted in an empty session for filter:{current_condition_filter_name}"})
                                        loading.refresh()
                                    _log(
                                        "Filtering resulted in an empty session",
                                        level="WARNING",
                                        file_path=str(nwb_file_path),
                                        condition_name=current_condition_filter_name,
                                        data_type=current_data_type,
                                     )
                                    continue
                                
                                event_dim = event_marker_to_align_dict["event_marker"]
                                event_n = current_filtered_data.sizes[event_dim]
                                _log(
                                    f"    aligned events kept: {event_n}",
                                    file_path=str(nwb_file_path),
                                    condition_name=current_condition_filter_name,
                                    data_type=current_data_type,
                                )

                                if save_metadata:
                                    coords = {}
                                    for key, value in xarray_nwb_metadata.items():
                                        coords[key] = (event_dim, [value] * event_n)

                            else:
                                current_filtered_data = current_filtered_data.expand_dims(file=[nwbfile.identifier])
                                if save_metadata:
                                    coords = {"file_path": ("file", [str(nwb_file_path)])}
                                    for key, value in xarray_nwb_metadata.items():
                                        coords[key] = ("file", [value])
                            
                            if save_metadata:
                                current_filtered_data = current_filtered_data.assign_coords(coords)
                            
                            filtered_data.append(current_filtered_data)

                            del current_filtered_data, filtered_timestamp

                    if current_data_type != 'trials':
                        del current_data
            
            ######################################################################
            ######################## Concatenate xarrays #########################
            if current_data_type != 'trials':
                
                _log(
                    f"Packaging {current_data_type} | condition: {current_condition_filter_name}",
                    condition_name=current_condition_filter_name,
                    data_type=current_data_type,
                )
                _progress(value=total_nb_of_file_processed, total=total_file_to_processed, text=f"Packaging {current_data_type} for {current_condition_filter_name}")
                if loading_screen:
                    loading.post("log", {"text": "Packging data to xarray"})
                    loading.refresh()

                if filtered_data is None:
                    _log(
                        f"No data to package for {current_data_type} | condition: {current_condition_filter_name}",
                        level="WARNING",
                        condition_name=current_condition_filter_name,
                        data_type=current_data_type,
                    )
                    continue

                if isinstance(filtered_data, (xr.DataArray, xr.Dataset)):
                    continue
                if not isinstance(filtered_data, list):
                    raise TypeError(
                        f"Expected list, DataArray, or Dataset for "
                        f"{current_data_type}/{current_condition_filter_name}, got {type(filtered_data)}"
                    )
                if len(filtered_data) == 0:
                    continue
    
                if not all(isinstance(v, (xr.DataArray, xr.Dataset)) for v in filtered_data):
                    bad_types = [type(v) for v in filtered_data if not isinstance(v, (xr.DataArray, xr.Dataset))]
                    raise TypeError(
                        f"Cannot concat {current_data_type}/{current_condition_filter_name}: found non-xarray elements {bad_types}"
                    )
        
                dim = event_marker_to_align_dict["event_marker"] if align_to_events else "file"
        
                if align_to_events:
                    filtered_data = validate_sampling_rates(filtered_data)
                concat_compat = "override" if current_base_data_type == "units" else "no_conflicts"
                filtered_data = xr.concat(
                                        filtered_data,
                                        dim=dim,
                                        join="outer",
                                        coords="minimal",
                                        compat=concat_compat,
                                        )
            
            ######################################################################
            ######################### save filtered_data #########################
            if filtered_data is None or (
                isinstance(filtered_data, pd.DataFrame) and filtered_data.empty
            ):
                _log(
                    f"No data to save for {current_data_type} | condition: {current_condition_filter_name}",
                    level="WARNING",
                    condition_name=current_condition_filter_name,
                    data_type=current_data_type,
                )
                continue

            if loading_screen:
                loading.post("log", {"text": f"Saving {current_data_type} for {current_condition_filter_name} to disk\n"})
                loading.refresh()
            _log(f"Saving {current_data_type} | condition: {current_condition_filter_name}", condition_name=current_condition_filter_name, data_type=current_data_type)
            _progress(value=total_nb_of_file_processed, total=total_file_to_processed, text=f"Saving {current_data_type} for {current_condition_filter_name}")
            data_manager.save_data(filtered_data, data_condition_row_index, condition_filter[current_condition_filter_name])
        

    if loading_screen:
        loading.post("done", {"text": "All data has been extracted.\nThe window can be closed."})
        loading.refresh()
        
    _log("Filtering finished successfully")
    _progress(value=total_file_to_processed, total=total_file_to_processed, text="Filtering finished successfully", done=True)

    return data_manager

def align_to_ref_timebase(aligned_data, time_maps, ref_time, accepted_rounding_decimals):
    rounded_ref_time = np.round(np.asarray(ref_time, dtype=float), accepted_rounding_decimals)

    new_aligned_data = []

    for data_idx, arr in enumerate(aligned_data):
        local_time = np.round(np.asarray(time_maps[data_idx], dtype=float), accepted_rounding_decimals)
        arr = np.asarray(arr)

        # fast path: already on ref_time
        if len(local_time) == len(rounded_ref_time) and np.array_equal(local_time, rounded_ref_time):
            new_aligned_data.append(arr)
            continue

        # if padding is needed, allow NaN
        if np.issubdtype(arr.dtype, np.floating):
            out_dtype = np.float32 if arr.dtype == np.float64 else arr.dtype
        else:
            out_dtype = np.float32

        new_shape = (len(rounded_ref_time),) + arr.shape[1:]
        padded = np.full(new_shape, np.nan, dtype=out_dtype)

        if len(rounded_ref_time) == 0 or len(local_time) == 0:
            new_aligned_data.append(padded)
            continue

        # Rounded high-rate time bases are not exactly uniform. For example,
        # 30 kHz timestamps rounded to microseconds alternate between 33 and
        # 34 microsecond steps. Match each sample to the nearest actual
        # reference value instead of estimating an index from one median dt.
        right_idx = np.searchsorted(rounded_ref_time, local_time, side="left")
        left_idx = np.clip(right_idx - 1, 0, len(rounded_ref_time) - 1)
        right_idx = np.clip(right_idx, 0, len(rounded_ref_time) - 1)

        left_distance = np.abs(local_time - rounded_ref_time[left_idx])
        right_distance = np.abs(local_time - rounded_ref_time[right_idx])
        use_right = right_distance < left_distance
        insert_idx = np.where(use_right, right_idx, left_idx)
        distance = np.minimum(left_distance, right_distance)

        if len(rounded_ref_time) == 1:
            tolerance = np.full(len(local_time), 0.5 * 10 ** (-accepted_rounding_decimals))
        else:
            gaps = np.diff(rounded_ref_time)
            left_gap = np.empty(len(rounded_ref_time), dtype=float)
            right_gap = np.empty(len(rounded_ref_time), dtype=float)
            left_gap[0] = gaps[0]
            left_gap[1:] = gaps
            right_gap[:-1] = gaps
            right_gap[-1] = gaps[-1]
            tolerance = 0.5 * np.minimum(left_gap[insert_idx], right_gap[insert_idx])
            tolerance += 0.5 * 10 ** (-accepted_rounding_decimals)

        valid = np.isfinite(local_time) & (distance <= tolerance)
        padded[insert_idx[valid]] = arr[valid].astype(out_dtype, copy=False)

        new_aligned_data.append(padded)

    return new_aligned_data, rounded_ref_time

def cut_and_align_to_event_marker(
    event_marker, data, event_marker_to_align_dict, file_identifier,
    accepted_rounding_decimals=6, save_metadata=True, trials_df=None,
    end_event_marker=None, all_trials=None,
):
    a = normalize_alignment(event_marker_to_align_dict)
    if end_event_marker is None and same_marker(a):
        end_event_marker = event_marker
    segments, a = pair_segments(event_marker, end_event_marker, a,
                               trials=all_trials, selected_trials=trials_df)
    return align_signal(data, segments, a, file_identifier, save_metadata)
