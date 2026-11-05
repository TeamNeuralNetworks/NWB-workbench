# -*- coding: utf-8 -*-
"""
Created on Sun Mar  1 15:02:07 2026

@author: Pierre
"""
from __future__ import annotations

from pathlib import Path
import os

import numpy as np
from pynwb.ecephys import ElectricalSeries
from pynwb import TimeSeries

from Tools.Electrophysiology.filtering.filtering_toolbox import remove_bad_channel, subsample
import importlib.util

module_name = "matlab"

if importlib.util.find_spec(module_name) is not None:
    from Tools.General.python_to_matlab_tools import get_or_start_engine, numpy_arrays_to_matlab_cell
    from Tools.General.timestamps_handler import (
        detect_sampling_rate_and_gaps_in_timestamps,
        check_resampling_consistency,
    )
    from GUI.processing_runner import make_file_tool


def clean_channels(signal, rate, timestamp, eng=None, workers=None):
    """
    Run artifact_removal_batch once and return (filtered, timestamp, eng).
    If an existing `eng` is passed, it is reused.
    """
    if workers is None:
        workers = min(signal.shape[1], os.cpu_count())
    if eng is None:
        eng, workers = get_or_start_engine(workers)

    data_cell = numpy_arrays_to_matlab_cell(signal)

    all_cleaned, all_removed = eng.artifact_removal_batch(
        data_cell, rate, workers, nargout=2
    )

    filtered_channels = np.vstack([np.asarray(c) for c in all_cleaned]).T
    all_removed = [int(x[0]) for x in all_removed]
    assert len(set(all_removed)) == 1
    all_removed = all_removed[0]

    if all_removed > 0:
        timestamp = timestamp[:-all_removed]

    assert filtered_channels.shape[0] == len(timestamp)
    return filtered_channels, timestamp, eng


def clean_and_subsample_segment(signal, timestamp, rate, max_freqs, eng=None, workers=None):
    """
    Clean one continuous segment, then subsample it, and rebuild timestamps.
    """
    filtered_signal, filtered_timestamp, eng = clean_channels(
        signal,
        rate,
        timestamp,
        eng=eng,
        workers=workers,
    )

    filtered_signal, new_rate = subsample(
        filtered_signal,
        rate,
        max_freqs=max_freqs,
        axis=0,
    )
    new_rate = float(new_rate)

    n_out = filtered_signal.shape[0]
    new_timestamp = filtered_timestamp[0] + np.arange(n_out) / new_rate

    check_resampling_consistency(filtered_timestamp, new_timestamp, rate, new_rate)

    assert filtered_signal.shape[0] == len(new_timestamp)
    return filtered_signal, new_timestamp, new_rate, eng


def denoise_ephy(
    data,
    data_name,
    saving_name,
    file_path,
    max_freqs=100,
    matlab_engine=None,
    workers=1,
    nwbfile=None,
    **kwargs,
):
    data = remove_bad_channel(data)
    if data is None:
        return None

    if nwbfile is None:
        raise ValueError("denoise_ephy requires 'nwbfile'")

    src_es = data

    if data.timestamps is not None:
        timestamp = np.asarray(data.timestamps)
        rate, gaps = detect_sampling_rate_and_gaps_in_timestamps(timestamp)
        starting_time = None
        has_explicit_timestamps = True

    elif data.rate is not None:
        rate = float(data.rate)
        starting_time = data.starting_time if data.starting_time is not None else 0.0
        timestamp = starting_time + np.arange(data.num_samples) / rate
        gaps = [(0, len(timestamp))]
        has_explicit_timestamps = False

    else:
        raise ValueError(f"{data_name} has neither timestamps nor rate")

    conversion = 1.0 if src_es.conversion is None else float(src_es.conversion)

    raw_data = src_es.data[:]
    if raw_data.ndim == 1:
        raw_data = raw_data[:, np.newaxis]

    if np.isnan(raw_data).any():
        raise ValueError("Input data contains NaNs")

    filtered_segments = []
    timestamp_segments = []
    output_rates = []

    eng = matlab_engine

    for start, stop in gaps:
        current_data = raw_data[start:stop]
        current_timestamp = timestamp[start:stop]

        current_filtered, current_timestamp_ds, current_rate, eng = clean_and_subsample_segment(
            current_data,
            current_timestamp,
            rate,
            max_freqs=max_freqs,
            eng=eng,
            workers=workers,
        )

        filtered_segments.append(current_filtered)
        timestamp_segments.append(current_timestamp_ds)
        output_rates.append(current_rate)

    filtered_data = np.concatenate(filtered_segments, axis=0)
    output_timestamp = np.concatenate(timestamp_segments, axis=0)

    assert np.allclose(output_rates, output_rates[0])
    output_rate = float(output_rates[0])

    assert filtered_data.shape[0] == len(output_timestamp)

    if has_explicit_timestamps:
        final_timestamps = output_timestamp
        final_rate = None
        final_starting_time = None
    else:
        final_timestamps = None
        final_rate = output_rate
        final_starting_time = starting_time

    if hasattr(src_es, "electrodes") and src_es.electrodes is not None:
        src_region = src_es.electrodes
        region_idx = np.asarray(src_region.data[:]).tolist()

        new_region = nwbfile.create_electrode_table_region(
            region=region_idx,
            description=getattr(src_region, "description", "electrodes"),
        )

        filtered_data = ElectricalSeries(
            name=saving_name,
            data=filtered_data,
            electrodes=new_region,
            timestamps=final_timestamps,
            starting_time=final_starting_time,
            rate=final_rate,
            conversion=conversion,
            description=(
                f"Ephys data from {data_name} filtered using MATLAB script "
                f"from Romain Goutagny lab derived from Islam et al. (2014)"
            ),
        )
    else:
        filtered_data = TimeSeries(
            name=saving_name,
            data=filtered_data,
            timestamps=final_timestamps,
            starting_time=final_starting_time,
            rate=final_rate,
            unit=getattr(src_es, "unit", "no_unit"),
            description=(
                f"Ephys data from {data_name} filtered using MATLAB script "
                f"from Romain Goutagny lab derived from Islam et al. (2014)"
            ),
        )

    return filtered_data


if importlib.util.find_spec(module_name) is not None:
    TOOL = make_file_tool(
        name="Filtering raw signal for LFP",
        description="Filter raw signal by removing bad channels and applying MATLAB artifact removal (Islam et al., 2014).",
        process_one=denoise_ephy,
        ui_title="Filtering signal...",
        ui_initial_text="Filtering signal...",
        continue_on_error=False,
        debug_mode=False,
        params={
            "data": {
                "type": "data",
                "label": "Input signal",
                "required": True,
                "neurodata_type_in": ["ElectricalSeries", "TimeSeries"],
                "load_data": False,
            },
            "saving_name": {"type": "saving_name", "label": "Output name", "default": None, "required": True},
            "max_freqs": {"type": "float", "label": "Max freq (Hz) for subsample", "default": 100.0, "min": 0.0},
            "workers": {"type": "int", "label": "Number of parallel processes", "default": 1, "min": 1},
            "matlab_script_path": Path(__file__).resolve().parent,
        },
    )