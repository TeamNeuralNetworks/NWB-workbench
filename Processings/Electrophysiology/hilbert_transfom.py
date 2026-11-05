# -*- coding: utf-8 -*-
"""
Created on Fri Mar  6 10:25:42 2026

@author: Pierre
"""

import numpy as np
from scipy.signal import hilbert, decimate

from pynwb.ecephys import ElectricalSeries
from pynwb.base import TimeSeries
from pynwb.misc import DecompositionSeries

from GUI.processing_runner import make_file_tool
from Tools.Electrophysiology.filtering.filtering_toolbox import notch_filter, bandpass_filter, remove_bad_channel
from Tools.General.timestamps_handler import detect_sampling_rate_and_gaps_in_timestamps, check_resampling_consistency


def extract_hilbert_transform(sig, timestamp, low_frequency, high_frequency, sample_rate, downsample_factor=20, axis=0):

    filtered_signal = bandpass_filter(
        sig,
        order=8,
        sample_rate=sample_rate,
        freq_low=low_frequency,
        freq_high=high_frequency,
        axis=axis
    )
    
    if downsample_factor > 1:
        new_fs = sample_rate / downsample_factor
    
        if new_fs <= 2 * high_frequency:
            raise ValueError(
                f"Downsampling too aggressive: new_fs={new_fs} Hz, "
                f"but high_frequency={high_frequency} Hz."
            )
    
        filtered_signal = decimate(
            filtered_signal,
            downsample_factor,
            axis=axis,
            zero_phase=True
        )
    else:
        new_fs = sample_rate
    

    hilbert_signal = hilbert(filtered_signal, axis=axis)

    n_out = hilbert_signal.shape[0]
    new_timestamp = timestamp[0] + np.arange(n_out) / new_fs

    check_resampling_consistency(timestamp, new_timestamp, sample_rate, new_fs)

    return hilbert_signal, new_fs, new_timestamp


def extract_using_hilbert_transform(data, data_name, saving_name_amplitude, saving_name_phase, file_path, nwbfile,
                                    low_frequency, high_frequency, shuffle, notch_frequency, downsample_factor,
                                    **kwargs):
    
    assert saving_name_amplitude != '' or saving_name_phase != '', 'At leat one saving name should be provided'
    
    source_timeseries = data
    data = remove_bad_channel(data)
    src_es = data

    if data.timestamps is not None:
        timestamp = np.asarray(data.timestamps)
        sampling_frequency, gaps = detect_sampling_rate_and_gaps_in_timestamps(timestamp)
        starting_time = None

    elif data.rate is not None:
        sampling_frequency = float(data.rate)
        starting_time = data.starting_time if data.starting_time is not None else 0
        timestamp = starting_time + np.arange(data.num_samples) / sampling_frequency
        gaps = [(0, len(timestamp))]

    data = data.data[:]
    if data.ndim == 1:
        data = data[:, np.newaxis]

    assert not np.isnan(data).any(), "Input data contains NaNs"

    if notch_frequency is not None:
        if low_frequency < notch_frequency < high_frequency:
            data = notch_filter(
                data,
                fs=sampling_frequency,
                freq=notch_frequency
            )

    if shuffle:
        idx = np.random.permutation(data.shape[0])
        data = data[idx]

    hilbert_signal_list = []
    timestamps_list = []
    all_final_sampling_rates = []

    for current_gaps in gaps:  # handle regularly sampled data with gaps
        hilbert_signal, final_sampling_rate, current_timestamp = extract_hilbert_transform(
            data[current_gaps[0]: current_gaps[1]],
            timestamp=timestamp[current_gaps[0]: current_gaps[1]],
            sample_rate=sampling_frequency,
            low_frequency=low_frequency,
            high_frequency=high_frequency,
            downsample_factor=downsample_factor
        )
        timestamps_list.append(current_timestamp)
        hilbert_signal_list.append(hilbert_signal)
        all_final_sampling_rates.append(final_sampling_rate)

    hilbert_signal = np.concatenate(hilbert_signal_list, axis=0)
    timestamp = np.concatenate(timestamps_list, axis=0)

    assert np.allclose(all_final_sampling_rates, all_final_sampling_rates[0])
    final_sampling_rate = all_final_sampling_rates[0]

    assert len(timestamp) == hilbert_signal.shape[0]

    if src_es.timestamps is not None:
        final_sampling_rate = None
        starting_time = None
    elif src_es.rate is not None:
        timestamp = None

    if hasattr(src_es, "electrodes") and src_es.electrodes is not None:

        src_region = src_es.electrodes
        region_idx = np.asarray(src_region.data[:]).tolist()
        new_region = nwbfile.create_electrode_table_region(
            region=region_idx,
            description=getattr(src_region, "description", "electrodes"),
        )
        
        if saving_name_amplitude != '':
            hilbert_amplitude = ElectricalSeries(
                name=saving_name_amplitude,
                data=np.abs(hilbert_signal),
                electrodes=new_region,
                timestamps=timestamp,
                rate=final_sampling_rate,
                starting_time=starting_time,
                conversion=src_es.conversion,
                description=f"Amplitude of the Hilbert transform from {low_frequency} to {high_frequency} Hz of {data_name}"
            )
        
        if saving_name_phase != '':
            hilbert_phase = DecompositionSeries(
               name=saving_name_phase,
               data=np.angle(hilbert_signal)[:, :, np.newaxis],
               metric="phase",
               source_timeseries=source_timeseries,
               source_channels=new_region,
               timestamps=timestamp,
               rate=final_sampling_rate,
               starting_time=starting_time,
               unit="radians",
               description=f"Phase of the Hilbert transform from {low_frequency} to {high_frequency} Hz of {data_name}"
               )
            hilbert_phase.add_band(
                   band_name=f"{low_frequency}-{high_frequency}_Hz",
                   band_limits=[low_frequency, high_frequency],
               )
    else:
        if saving_name_amplitude != '':
            hilbert_amplitude = TimeSeries(
                name=saving_name_amplitude,
                data=np.abs(hilbert_signal),
                timestamps=timestamp,
                rate=final_sampling_rate,
                starting_time=starting_time,
                unit=getattr(src_es, "unit", "no_unit"),
                description=f"Amplitude of the Hilbert transform from {low_frequency} to {high_frequency} Hz of {data_name}"
            )
            
        if saving_name_phase != '':
            hilbert_phase = DecompositionSeries(
                name=saving_name_phase,
                data=np.angle(hilbert_signal)[:, :, np.newaxis],
                metric="phase",
                source_timeseries=source_timeseries,
                timestamps=timestamp,
                rate=final_sampling_rate,
                starting_time=starting_time,
                unit="radians",
                description=f"Phase of the Hilbert transform from {low_frequency} to {high_frequency} Hz of {data_name}"
            )
            hilbert_phase.add_band(
                band_name=f"{low_frequency}-{high_frequency}_Hz",
                band_limits=[low_frequency, high_frequency],
            )
         
    if saving_name_amplitude != '' and saving_name_phase != '':
        return hilbert_amplitude, hilbert_phase
    elif saving_name_amplitude != '':
        return hilbert_amplitude
    elif saving_name_phase != '':
        return hilbert_phase


TOOL = make_file_tool(
    name="Hilbert transform",
    description="Extract oscillation amplitude and phase using the Hilbert transform",
    process_one=extract_using_hilbert_transform,
    ui_title="Hilbert transform...",
    ui_initial_text="Hilbert transform...",
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
        "saving_name_amplitude": {"type": "saving_name", "label": "Output name for amplitude", "default": None, "required": False},
        "saving_name_phase": {"type": "saving_name", "label": "Output name for phase", "default": None, "required": False},
        "downsample_factor": {"type": "int", "label": "Downsample factor", "default": 20, "min": 0.0, "required": True},
        "low_frequency": {"type": "float", "label": "Minimum frequency", "default": None, "min": 0.0, "required": True},
        "high_frequency": {"type": "float", "label": "Maximum frequency", "default": None, "min": 0.0, "required": True},
        "shuffle": {"type": "bool", "label": "Shuffle", "default": False},
        "notch_frequency": {"type": "float", "label": "Notch frequency", "default": None, "min": 0.0},
    },
)