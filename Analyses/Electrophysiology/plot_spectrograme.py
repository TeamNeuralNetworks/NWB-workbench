# -*- coding: utf-8 -*-
"""
Created on Mon Mar 16 15:00:49 2026

@author: Jerome.WANDHAMMER
"""
import matplotlib.pyplot as plt
from scipy.stats import sem
import numpy as np
from scipy.signal import welch
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks


import xarray as xr
import sys
sys.path.append(r"D:/Functionnal_Imaging_Electrophysiology")

from Tools.Electrophysiology.filtering.filtering_toolbox import notch_filter

def remove_1f_loglog_fit_1d(power, freqs, fit_range=None, eps=1e-12):
    """
    Remove 1/f component from one spectrum.

    power shape: frequencies
    freqs shape: frequencies
    """

    power = np.asarray(power, dtype=float)
    freqs = np.asarray(freqs, dtype=float)

    valid = np.isfinite(power) & np.isfinite(freqs) & (power > 0) & (freqs > 0)

    if fit_range is not None:
        fmin, fmax = fit_range
        valid &= (freqs >= fmin) & (freqs <= fmax)

    if np.sum(valid) < 3:
        return np.full_like(power, np.nan)

    log_power = np.log10(power + eps)
    log_freqs = np.log10(freqs)

    slope, intercept = np.polyfit(log_freqs[valid], log_power[valid], 1)

    background = intercept + slope * log_freqs
    corrected = log_power - background

    return corrected

def get_signal(data, method):
    if method == "concatenated":
        yield data.data.T.flatten()
    elif method == 'FFT_per_subject':
        for individue_id, mouse_signal in data.groupby('subject_id'):
            
            signal = mouse_signal.data        
            
            if len(signal) == 0:
                continue
            else:
                yield signal
               
        

def plot_fft(data_manager, data_type, condition, notch_frequency=None, min_freq=None, max_freq=None, remove_1_over_f=False, plot_condition_same_fig=False, display_peak=False, ax=None, method='concatenated', sigma_smoothing=None, title=None):
    """
    Plot the amplitude spectrum of a signal using FFT.

    Parameters
    ----------
    signal : array-like
        The input time-domain signal.
    fs : float
        Sampling frequency in Hz.
    """
    
    data = data_manager.get_data(data_type=data_type, condition=condition)
    if 'rate' in dir(data):
        rate = data.rate
    else:
        rate = 1/(np.diff(data.time).min())
        
    data = data.dropna(dim='time', how='all')
    
    lowest_freq_FFT = ((1/(data.time.max()-data.time.min()))*2).data
    
    data = data.groupby('subject_id').mean(dim=data.aligned_to)
    data = data.mean(dim='channels')
    data = (data - data.mean(dim="time")) / data.std(dim="time")
            
    condition_freqs = []
    condition_amplitude = []

    for signal in get_signal(data, method=method):
        if notch_frequency is not None and notch_frequency > 0:
            signal = notch_filter(signal, fs=rate, freq=notch_frequency)
        
        X = np.fft.rfft(signal)
        freqs = np.fft.rfftfreq(len(signal), d=1/rate)
        
        if min_freq is None:
            min_freq = lowest_freq_FFT
        
        if max_freq is not None:
            index = np.where((freqs > min_freq) & (freqs <= max_freq))
        else:
            index = np.where(freqs > min_freq)

            
        magnitude = np.abs(X)
    
        magnitude = magnitude[index]
        
        freqs = freqs[index]
        
        if remove_1_over_f:
            magnitude = remove_1f_loglog_fit_1d(magnitude, freqs)
        
        condition_freqs.append(freqs)
        condition_amplitude.append(magnitude)

    assert all((condition_freqs[0] == arr).all() for arr in condition_freqs)        

    condition_amplitude_mean = np.mean(condition_amplitude, axis=0).squeeze()
    condition_amplitude_sem = sem(condition_amplitude, axis=0).squeeze()
    
    if sigma_smoothing is not None and sigma_smoothing > 0:
        condition_amplitude_mean = gaussian_filter1d(condition_amplitude_mean, sigma=sigma_smoothing)
        condition_amplitude_sem = gaussian_filter1d(condition_amplitude_sem, sigma=sigma_smoothing)
    
    if ax is None:
        fig, ax = plt.subplots(figsize=(7,5))
        ax.set_xlabel("Frequency")
        ax.set_ylabel("Magnitude")
        if title is not None:
            fig.suptitle(title)

    ax.plot(freqs, condition_amplitude_mean, label=data.condition_name, color=data.color)
    ax.fill_between(freqs, 
                    condition_amplitude_mean-condition_amplitude_sem, 
                    condition_amplitude_mean+condition_amplitude_sem, 
                    color=data.color,
                    alpha=0.3)  
    
    
    if display_peak:
        locs, props = find_peaks(condition_amplitude_mean)
        pks_freqs = freqs[locs]
        pks_magnitude = condition_amplitude_mean[locs]
        
        ax.scatter(pks_freqs, pks_magnitude, color='red')
    
        for xi, yi in zip(pks_freqs, pks_magnitude):
            ax.text(
                xi, yi + 0.15,
                f"{xi:.2f}Hz\n({1/xi: .2f} s)",
                ha="center",
                va="bottom",
            )
        
        ax.legend()
    
def plot_condition(data_manager, data_type, notch_frequency=None, plot_condition_same_fig=False, min_freq=None, max_freq=None, display_peak=False, sigma_smoothing=None, title=None, method='concatenated'):
    if plot_condition_same_fig:
        fig, ax = plt.subplots(figsize=(12, 8))
        ax.set_xlabel("Frequency")
        ax.set_ylabel("Magnitude")
        fig.suptitle(title)
    for condition in data_manager.get_conditions():
        if not plot_condition_same_fig:
            fig, ax = plt.subplots(figsize=(12, 8))
            ax.set_xlabel("Frequency")
            ax.set_ylabel("Magnitude")
            fig.suptitle(title)
        plot_fft(data_manager, data_type, condition, notch_frequency, min_freq=min_freq, max_freq=max_freq, ax=ax, sigma_smoothing=sigma_smoothing, method=method, display_peak=display_peak)
    return fig
    
def run_plot_spectrogram(data_manager, figure_title=None, notch_frequency=None, result=None, min_freq=None, max_freq=None, remove_1_over_f=False, plot_condition_same_fig=False, display_peak=False, sigma_smoothing=None, method='concatenated', **kwargs):
        
    if len(data_manager.get_data_types()) == 1:        
        fig = plot_condition(data_manager, data_manager.get_data_types()[0], notch_frequency, min_freq=min_freq, max_freq=max_freq, plot_condition_same_fig=plot_condition_same_fig, display_peak=display_peak, sigma_smoothing=sigma_smoothing, method=method, title=f'{figure_title}\n{data_manager.get_data_types()[0]}')
        if result is not None:
            result.add(fig)
    else:
        for current_data_type in data_manager.get_data_types():
            fig = plot_condition(data_manager, current_data_type, notch_frequency, min_freq=min_freq, max_freq=max_freq, plot_condition_same_fig=plot_condition_same_fig, display_peak=display_peak, sigma_smoothing=sigma_smoothing, method=method, title=f'{figure_title}\n{current_data_type}')
            if result is not None:
                result.add(fig)
            

ANALYSIS = {
    "name": "Plot spectrograme",
    "description": "Plot spectrograme",
    "func": run_plot_spectrogram,
    "align_to_event_marker": True,
    "debug_mode": True,
    "params": {
                # "notch_frequency": {
                #     "type": "float",
                #     "label": "notch frequency",
                #     "default": None,
                #     "min": 0.0,
                #     },
                "min_freq": {
                    "type": "float",
                    "label": "Minimum frequency (keep empty for Nyquist law)",
                    "default": None,
                    "min": 0.0,
                    },
                
                "max_freq": {
                    "type": "float",
                    "label": "Maximum frequency",
                    "default": None,
                    "min": 0.0,
                    },
                
                "method":  {
                    "type": "list",
                    "label": "Inter subject method",
                    "values": ["concatenated", "FFT_per_subject"],
                    "default": "concatenated",
                },
                
                "sigma_smoothing": {
                    "type": "float",
                   "label": "Sigma of the gaussian for smoothing of fft (0 to diseable)",
                   "default": 3
                    },
                "remove_1_over_f": {
                    "type": "bool",
                   "label": "Remove 1 over f on FFT",
                   "default": False,
                    },
                "plot_condition_same_fig": {
                    "type": "bool",
                   "label": "Plot conditions on the same fig",
                   "default": False,
                    },
                "display_peak": {
                    "type": "bool",
                   "label": "Auto detect peak",
                   "default": False,
                    },
                "figure_title": {
                    "type": "str",
                   "label": " Figure title",
                   "default": None,
                    },
               
            },
    "input_spec": {
        "accepted_data": ["ElectricalSeries", "TimeSeries"],
        "allow_multiple": True,
        "required": True,
    },
}

if __name__ == "__main__":   
    
    import sys
    sys.path.append(r"D:\Functionnal_Imaging_Electrophysiology\Tools\General")
    from FilteredConditionFileManager import FilteredConditionFileManager
      
    project_folder = r'D:\PaperFede\NWB app'
    
    data_info_dicts = [
                 {'filter_name': 'P0 + first',
                    'condition': 'Variable Delay',
                    'data_type': 'Theta_amplitude',
                    'pad_before_s': .2,
                    'pad_after_s': 2.4,
                    'aligned_to_event': 'trials',
                    'edge': 'start'}, 
                        
                 # {'filter_name': 'P0 + first',
                 #    'condition': 'Fixed Delay',
                 #    'data_type': 'Delta_amplitude',
                 #    'pad_before_s': 0.2,
                 #    'pad_after_s': 8.8,
                 #    'aligned_to_event': 'trials',
                 #    'edge': 'start'}, 
                       ]

    
    data_manager = FilteredConditionFileManager.from_saved_data(project_folder, data_info_dicts=data_info_dicts)
    
    run_plot_spectrogram(data_manager, 'test', notch_frequency=None, result=None, max_freq=4, remove_1_over_f=False, sigma_smoothing=0)