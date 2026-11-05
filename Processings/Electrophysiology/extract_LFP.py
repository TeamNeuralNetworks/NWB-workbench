# -*- coding: utf-8 -*-
"""
Created on Fri Mar  6 10:25:42 2026

@author: Pierre
"""

import numpy as np
import scipy.signal
from scipy import fftpack

from pynwb.ecephys import ElectricalSeries
from pynwb.base import TimeSeries
from pynwb.misc import DecompositionSeries

from GUI.processing_runner import make_file_tool
from Tools.Electrophysiology.filtering.filtering_toolbox import notch_filter, remove_bad_channel
from Tools.General.timestamps_handler import detect_sampling_rate_and_gaps_in_timestamps, check_resampling_consistency

######################################################
def generate_wavelet_fourier(len_wavelet, f_start, f_stop, delta_freq,
                             sampling_rate, f0, normalisation):
    """
    Compute the wavelet coefficients at all scales and their Fourier transform.
    """
    scales = f0 / np.arange(f_start, f_stop, delta_freq) * sampling_rate

    xi = np.arange(-len_wavelet / 2., len_wavelet / 2.)
    xsd = xi[:, np.newaxis] / scales

    wavelet_coefs = np.exp(1j * 2. * np.pi * f0 * xsd) * np.exp(-np.power(xsd, 2) / 2.)

    weighting_function = lambda x: x ** (-(1.0 + normalisation))
    wavelet_coefs *= weighting_function(scales[np.newaxis, :])

    wf = fftpack.fft(wavelet_coefs, axis=0)
    wf = wf.conj()

    return wf


def convolve_scalogram(sig, wf):
    """
    Convolve signal with wavelet (FFT-based).
    """
    n = wf.shape[0]
    assert sig.shape[0] <= n, f"Signal size larger than wavelet: {sig.shape[0]} > {n}"

    sigf = fftpack.fft(sig, n)
    wt_tmp = fftpack.ifft(sigf[:, np.newaxis] * wf, axis=0)
    wt = fftpack.fftshift(wt_tmp, axes=[0])

    return wt


def subsample(sig, sampling_rate, max_freqs=None, min_sampling_rate=None, axis=-1):

    if max_freqs is None and min_sampling_rate is None:
        raise ValueError("At least one of max_freqs or min_sampling_rate must be provided")

    if min_sampling_rate is None:
        min_sampling_rate = min(4. * max_freqs, sampling_rate)

    ratio = int(sampling_rate / min_sampling_rate)

    if ratio > 1:
        sig2 = scipy.signal.decimate(sig, ratio, n=4, zero_phase=True, axis=axis)
    else:
        sig2 = sig
        ratio = 1

    tfr_sampling_rate = sampling_rate / ratio

    return sig2, tfr_sampling_rate


def compute_timefreq(sig, sampling_rate, f_start, f_stop, delta_freq=1., nb_freq=None,
                     f0=2.5, normalisation=0., min_sampling_rate=None, wf=None,
                     t_start=0., zero_pad=True, joblib_memory=None):

    sampling_rate = float(sampling_rate)

    if nb_freq is not None:
        delta_freq = (f_stop - f_start) / nb_freq

    sig2, tfr_sampling_rate = subsample(sig, sampling_rate,
                                        max_freqs=f_stop,
                                        min_sampling_rate=min_sampling_rate)

    n_init = sig2.size

    if zero_pad:
        n = int(2 ** np.ceil(np.log2(n_init)))
    else:
        n = n_init

    if wf is None:
        func = generate_wavelet_fourier if joblib_memory is None else joblib_memory.cache(generate_wavelet_fourier)
        wf = func(n, f_start, f_stop, delta_freq,
                  tfr_sampling_rate, f0, normalisation)

    assert wf.shape[0] == n

    wt = convolve_scalogram(sig2, wf)
    wt = wt[:n_init, :]

    freqs = np.arange(f_start, f_stop, delta_freq)
    times = np.arange(n_init) / tfr_sampling_rate + t_start

    return wt, times, freqs, tfr_sampling_rate

######################################################


def compute_complex_map(data, timestamp, sampling_frequency, current_gaps,
                        low_frequency, high_frequency, delta_frequency,
                        f0, shuffle, notch_frequency):

    data = data[current_gaps[0]: current_gaps[1]]
    timestamp = timestamp[current_gaps[0]: current_gaps[1]]

    if notch_frequency is not None:
        if low_frequency < notch_frequency < high_frequency:
            data = notch_filter(data, fs=sampling_frequency, freq=notch_frequency)

    complex_map_list = []
    tfr_sampling_rate = None

    for channel_idx in range(data.shape[1]):

        if shuffle:
            channel_data = np.random.permutation(data[:, channel_idx])
        else:
            channel_data = data[:, channel_idx]

        complex_map, _, _, tfr_sampling_rate = compute_timefreq(
            channel_data,
            sampling_frequency,
            f_start=low_frequency,
            f_stop=high_frequency,
            delta_freq=delta_frequency,
            f0=f0
        )

        complex_map_list.append(complex_map)

    complex_map = np.stack(complex_map_list, axis=1)

    amplitude_map = np.abs(complex_map).mean(axis=2)
    phase_map = np.angle(np.mean(complex_map, axis=2))

    n_out = amplitude_map.shape[0]
    new_timestamp = timestamp[0] + np.arange(n_out) / tfr_sampling_rate

    check_resampling_consistency(timestamp, new_timestamp, sampling_frequency, tfr_sampling_rate)

    assert amplitude_map.shape[0] == len(new_timestamp)
    assert phase_map.shape[0] == len(new_timestamp)

    return amplitude_map, phase_map, new_timestamp, tfr_sampling_rate


def extract_LFP(data, data_name, saving_name_amplitude, saving_name_phase,
                file_path, nwbfile,
                low_frequency, high_frequency, delta_frequency, f0,
                shuffle, notch_frequency,
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

    amplitude_map_list = []
    phase_map_list = []
    timestamps_list = []
    all_tfr_sampling_rates = []

    for current_gaps in gaps:

        amp, ph, new_timestamp, tfr_sampling_rate = compute_complex_map(
            data, timestamp, sampling_frequency, current_gaps,
            low_frequency, high_frequency, delta_frequency,
            f0, shuffle, notch_frequency
        )

        amplitude_map_list.append(amp)
        phase_map_list.append(ph)
        timestamps_list.append(new_timestamp)
        all_tfr_sampling_rates.append(tfr_sampling_rate)

    amplitude_map = np.concatenate(amplitude_map_list, axis=0)
    phase_map = np.concatenate(phase_map_list, axis=0)
    timestamp = np.concatenate(timestamps_list, axis=0)

    assert np.allclose(all_tfr_sampling_rates, all_tfr_sampling_rates[0])
    tfr_sampling_rate = all_tfr_sampling_rates[0]

    if src_es.timestamps is not None:
        tfr_sampling_rate = None
        starting_time = None
    else:
        timestamp = None

    if hasattr(src_es, "electrodes") and src_es.electrodes is not None:

        src_region = src_es.electrodes
        region_idx = np.asarray(src_region.data[:]).tolist()

        new_region = nwbfile.create_electrode_table_region(
            region=region_idx,
            description=getattr(src_region, "description", "electrodes"),
        )
        
        if saving_name_amplitude != '':
            oscillation_amplitude = ElectricalSeries(
                name=saving_name_amplitude,
                data=amplitude_map,
                electrodes=new_region,
                timestamps=timestamp,
                rate=tfr_sampling_rate,
                starting_time=starting_time,
                conversion=src_es.conversion,
                description=f"Mean amplitude across Morlet frequencies between {low_frequency}-{high_frequency} Hz "
                            f"(Δf={delta_frequency}, f0={f0}) of {data_name}"
            )

        if saving_name_phase != '':
            oscillation_phase = DecompositionSeries(
                name=saving_name_phase,
                data=phase_map[:, :, np.newaxis],
                metric="phase",
                source_timeseries=source_timeseries,
                source_channels=new_region,
                timestamps=timestamp,
                rate=tfr_sampling_rate,
                starting_time=starting_time,
                unit="radians",
                description=(
                    f"Phase of mean complex Morlet coefficient between "
                    f"{low_frequency}-{high_frequency} Hz "
                    f"(Δf={delta_frequency}, f0={f0}) of {data_name}"
                ),
            )
        
            oscillation_phase.add_band(
                band_name=f"{low_frequency}-{high_frequency}_Hz",
                band_limits=[low_frequency, high_frequency],
            )
            

    else:
        if saving_name_amplitude != '':
            oscillation_amplitude = TimeSeries(
                name=saving_name_amplitude,
                data=amplitude_map,
                timestamps=timestamp,
                rate=tfr_sampling_rate,
                starting_time=starting_time,
                unit=getattr(src_es, "unit", "no_unit"),
                description=f"Mean amplitude across Morlet frequencies between {low_frequency}-{high_frequency} Hz "
                            f"(Δf={delta_frequency}, f0={f0}) of {data_name}"
            )
        if saving_name_phase != '':
            oscillation_phase = DecompositionSeries(
                name=saving_name_phase,
                data=phase_map[:, :, np.newaxis],
                metric="phase",
                source_timeseries=source_timeseries,
                timestamps=timestamp,
                rate=tfr_sampling_rate,
                starting_time=starting_time,
                unit="radians",
                description=(
                    f"Phase of mean complex Morlet coefficient between "
                    f"{low_frequency}-{high_frequency} Hz "
                    f"(Δf={delta_frequency}, f0={f0}) of {data_name}"
                ),
            )
        
            oscillation_phase.add_band(
                band_name=f"{low_frequency}-{high_frequency}_Hz",
                band_limits=[low_frequency, high_frequency],
            )
    
    if saving_name_amplitude != '' and saving_name_phase != '':
        return oscillation_amplitude, oscillation_phase
    elif saving_name_amplitude != '':
        return oscillation_amplitude
    elif saving_name_phase != '':
        return oscillation_phase



TOOL = make_file_tool(
    name="Extracting LFP with Morlet wavelet",
    description="Extract oscillation amplitude and phase using Morlet wavelet convolution",
    process_one=extract_LFP,
    ui_title="Extracting LFP...",
    ui_initial_text="Extracting LFP...",
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
        "saving_name_amplitude": {"type": "saving_name", "label": "Output amplitude", "required": False},
        "saving_name_phase": {"type": "saving_name", "label": "Output phase", "required": False},
        "low_frequency": {"type": "float", "label": "Min frequency", "required": True},
        "high_frequency": {"type": "float", "label": "Max frequency", "required": True},
        "delta_frequency": {"type": "float", "label": "Frequency resolution", "required": True},
        "f0": {"type": "float", "label": "f0", "required": True},
        "shuffle": {"type": "bool", "label": "Shuffle", "default": False},
        "notch_frequency": {"type": "float", "label": "Notch frequency", "default": None},
    },
)