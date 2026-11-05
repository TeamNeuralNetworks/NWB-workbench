# -*- coding: utf-8 -*-
"""
Created on Sat Mar  7 16:11:31 2026

@author: Pierre
"""

import numpy as np
import pycwt as wavelet
from pynwb.base import TimeSeries

from GUI.processing_runner import make_file_tool
from Tools.Electrophysiology.filtering.filtering_toolbox import notch_filter
from Tools.General.timestamps_handler import (
    detect_sampling_rate_and_gaps_in_timestamps,
    check_resampling_consistency,
)


def _prepare_signal_and_time(data_obj):
    """
    Return:
        data_array: np.ndarray, shape (n_samples, n_channels)
        timestamp: np.ndarray, shape (n_samples,)
        sampling_frequency: float
        gaps: list of (start, stop)
        had_explicit_timestamps: bool
        starting_time: float or None
    """
    if data_obj.timestamps is not None:
        timestamp = np.asarray(data_obj.timestamps)
        sampling_frequency, gaps = detect_sampling_rate_and_gaps_in_timestamps(timestamp)
        starting_time = None
        had_explicit_timestamps = True

    elif data_obj.rate is not None:
        sampling_frequency = float(data_obj.rate)
        starting_time = data_obj.starting_time if data_obj.starting_time is not None else 0.0
        timestamp = starting_time + np.arange(data_obj.num_samples) / sampling_frequency
        gaps = [(0, len(timestamp))]
        had_explicit_timestamps = False

    else:
        raise ValueError("Input data must have either timestamps or rate")

    data_array = data_obj.data[:]
    if data_array.ndim == 1:
        data_array = data_array[:, np.newaxis]

    if np.isnan(data_array).any():
        raise ValueError("Input data contains NaNs")

    return data_array, timestamp, float(sampling_frequency), gaps, had_explicit_timestamps, starting_time


def _gaps_equal(gaps_a, gaps_b):
    if len(gaps_a) != len(gaps_b):
        return False
    return all((a0 == b0 and a1 == b1) for (a0, a1), (b0, b1) in zip(gaps_a, gaps_b))


def compute_coherence_segment(
    data_source,
    data_target,
    timestamp_source,
    timestamp_target,
    sampling_frequency,
    current_gap,
    low_frequency,
    high_frequency,
    delta_frequency,
    shuffle,
    notch_frequency,
):
    start, stop = current_gap

    seg_source = data_source[start:stop]
    seg_target = data_target[start:stop]
    seg_t_source = timestamp_source[start:stop]
    seg_t_target = timestamp_target[start:stop]

    if not np.allclose(seg_t_source, seg_t_target):
        raise ValueError("Source and target timestamps differ within a gap segment")

    if notch_frequency is not None and low_frequency < notch_frequency < high_frequency:
        seg_source = notch_filter(seg_source, fs=sampling_frequency, freq=notch_frequency)
        seg_target = notch_filter(seg_target, fs=sampling_frequency, freq=notch_frequency)

    source_1d = np.median(seg_source, axis=1)
    target_1d = np.median(seg_target, axis=1)

    if shuffle:
        source_1d = np.random.permutation(source_1d)
        target_1d = np.random.permutation(target_1d)

    f_ref = np.sqrt(low_frequency * high_frequency)
    dj = -np.log2(1 - delta_frequency / f_ref)

    fourier_factor = 1.033
    dt = 1.0 / sampling_frequency

    s_max = 1.0 / (low_frequency * fourier_factor)
    s_min = 1.0 / (high_frequency * fourier_factor)

    s0 = s_min
    J = int(np.log2(s_max / s0) / dj)

    magnitude, phase, coi, freqs, sig = wavelet.wct(
        source_1d,
        target_1d,
        dt=dt,
        dj=dj,
        s0=s0,
        J=J,
        wavelet="morlet",
        sig=False,
    )

    magnitude_mean = magnitude.mean(axis=0)
    phase_mean = np.angle(np.mean(np.exp(1j * phase), axis=0))

    n_out = magnitude_mean.shape[0]
    new_timestamp = seg_t_target[0] + np.arange(n_out) / sampling_frequency

    check_resampling_consistency(seg_t_target, new_timestamp, sampling_frequency, sampling_frequency)

    if magnitude_mean.shape[0] != len(new_timestamp):
        raise ValueError("Magnitude output length does not match rebuilt timestamps")
    if phase_mean.shape[0] != len(new_timestamp):
        raise ValueError("Phase output length does not match rebuilt timestamps")

    return magnitude_mean, phase_mean, new_timestamp


def extract_coherence(
    data_source,
    data_target,
    data_source_name,
    data_target_name,
    saving_name_magnitude,
    saving_name_phase,
    file_path,
    low_frequency,
    high_frequency,
    delta_frequency,
    shuffle,
    notch_frequency,
    **kwargs,
):
    assert saving_name_magnitude != "" or saving_name_phase != "", "At least one saving name should be provided"

    (
        data_source_array,
        timestamp_source,
        sampling_frequency_source,
        gaps_source,
        source_has_timestamps,
        source_starting_time,
    ) = _prepare_signal_and_time(data_source)

    (
        data_target_array,
        timestamp_target,
        sampling_frequency_target,
        gaps_target,
        target_has_timestamps,
        target_starting_time,
    ) = _prepare_signal_and_time(data_target)

    if len(timestamp_source) != len(timestamp_target):
        raise ValueError("Source and target do not have the same number of samples")

    if not np.isclose(sampling_frequency_source, sampling_frequency_target):
        raise ValueError("Source and target sampling frequencies differ")

    if not _gaps_equal(gaps_source, gaps_target):
        raise ValueError("Source and target gap structure differs")

    if not np.allclose(timestamp_source, timestamp_target):
        raise ValueError("Source and target timestamps are not aligned")

    magnitude_list = []
    phase_list = []
    timestamps_list = []

    for current_gap in gaps_source:
        current_magnitude, current_phase, current_timestamp = compute_coherence_segment(
            data_source_array,
            data_target_array,
            timestamp_source,
            timestamp_target,
            sampling_frequency_target,
            current_gap,
            low_frequency,
            high_frequency,
            delta_frequency,
            shuffle,
            notch_frequency,
        )

        magnitude_list.append(current_magnitude)
        phase_list.append(current_phase)
        timestamps_list.append(current_timestamp)

    magnitude = np.concatenate(magnitude_list, axis=0)
    phase = np.concatenate(phase_list, axis=0)
    timestamp = np.concatenate(timestamps_list, axis=0)

    if len(magnitude) != len(timestamp):
        raise ValueError("Magnitude output length does not match timestamps")
    if len(phase) != len(timestamp):
        raise ValueError("Phase output length does not match timestamps")

    if source_has_timestamps or target_has_timestamps:
        output_timestamps = timestamp
        output_rate = None
        output_starting_time = None
    else:
        output_timestamps = None
        output_rate = sampling_frequency_target
        output_starting_time = target_starting_time if target_starting_time is not None else 0.0

    if saving_name_magnitude != "":
        coherence_magnitude = TimeSeries(
            name=saving_name_magnitude,
            data=magnitude,
            timestamps=output_timestamps,
            rate=output_rate,
            starting_time=output_starting_time,
            unit="no_unit",
            description=(
                f"Mean wavelet coherence magnitude between source {data_source_name} "
                f"and target {data_target_name} from {low_frequency} to {high_frequency} Hz "
                f"(delta frequency {delta_frequency})"
            ),
        )

    if saving_name_phase != "":
        coherence_phase = TimeSeries(
            name=saving_name_phase,
            data=phase,
            timestamps=output_timestamps,
            rate=output_rate,
            starting_time=output_starting_time,
            unit="radians",
            description=(
                f"Circular mean wavelet coherence phase between source {data_source_name} "
                f"and target {data_target_name} from {low_frequency} to {high_frequency} Hz "
                f"(delta frequency {delta_frequency})"
            ),
        )

    if saving_name_magnitude != "" and saving_name_phase != "":
        return coherence_magnitude, coherence_phase
    elif saving_name_magnitude != "":
        return coherence_magnitude
    elif saving_name_phase != "":
        return coherence_phase


TOOL = make_file_tool(
    name="Extract coherence with Morlet wavelet",
    description="Extract coherence magnitude and phase using Morlet wavelet coherence",
    process_one=extract_coherence,
    ui_title="Extracting coherence...",
    ui_initial_text="Extracting coherence...",
    continue_on_error=False,
    debug_mode=False,
    params={
        "data_source": {
            "type": "data",
            "label": "Source signal",
            "required": True,
            "neurodata_type_in": ["ElectricalSeries", "TimeSeries"],
            "load_data": False,
        },
        "data_target": {
            "type": "data",
            "label": "Target signal",
            "required": True,
            "neurodata_type_in": ["ElectricalSeries", "TimeSeries"],
            "load_data": False,
        },
        "saving_name_magnitude": {
            "type": "saving_name",
            "label": "Output name for magnitude",
            "default": None,
            "required": False,
        },
        "saving_name_phase": {
            "type": "saving_name",
            "label": "Output name for phase",
            "default": None,
            "required": False,
        },
        "low_frequency": {
            "type": "float",
            "label": "Minimum frequency",
            "default": None,
            "min": 0.0,
            "required": True,
        },
        "high_frequency": {
            "type": "float",
            "label": "Maximum frequency",
            "default": None,
            "min": 0.0,
            "required": True,
        },
        "delta_frequency": {
            "type": "float",
            "label": "Frequency resolution",
            "default": None,
            "min": 0.0,
            "required": True,
        },
        "shuffle": {
            "type": "bool",
            "label": "Shuffle",
            "default": False,
        },
        "notch_frequency": {
            "type": "float",
            "label": "Notch frequency",
            "default": None,
            "min": 0.0,
        },
    },
)