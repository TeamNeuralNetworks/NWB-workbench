
# -*- coding: utf-8 -*-
"""
Created on Thu Jun  5 09:37:55 2025

@author: Pierre.LE-CABEC
"""
import numpy as np
from scipy.signal import iirfilter, sosfiltfilt, filtfilt, iirnotch, decimate

from hdmf.common import DynamicTableRegion
from pynwb.ecephys import ElectricalSeries

def bandpass_filter(signal, order=8, sample_rate=20000,freq_low=400,freq_high=2000, axis=0):
    Wn = [freq_low/(sample_rate/2),freq_high/(sample_rate/2)]
    sos_coeff = iirfilter(order ,Wn ,btype='band', ftype='butter', output='sos')
    filtered_signal = sosfiltfilt(sos_coeff, signal, axis=axis)
    
    return filtered_signal

def notch_filter(signal, fs, freq=50.0, Q=15, axis=0):
    w0 = freq / (fs / 2)  # Normalize frequency
    b, a = iirnotch(w0, Q)
    return filtfilt(b, a, signal, axis=axis)

def subsample(sig, sampling_rate, max_freqs=None, min_sampling_rate=None, axis=-1):
    
    if max_freqs is None and min_sampling_rate is None:
        raise ValueError('At least on eof max_freqs and min_sampling_rate must be given')
    
    if min_sampling_rate is None:
        min_sampling_rate =  min(4.* max_freqs, sampling_rate)
        
    #decimate
    ratio = int(sampling_rate/min_sampling_rate)
    #~ print 'ratio', ratio
    if ratio>1:
        # sig2 = tools.decimate(sig, ratio)
        sig2 = decimate(sig, ratio, n=4, zero_phase=True, axis=axis)
    else:
        sig2 = sig
        ratio=1
    
    tfr_sampling_rate = sampling_rate/ratio
    
    return sig2, tfr_sampling_rate
    
def remove_bad_channel(data):
    
    if not (hasattr(data, "electrodes") and data.electrodes is not None):
        return data

    reg = data.electrodes
    rows = np.asarray(reg.data[:], dtype=int)
    table = reg.table.to_dataframe()

    if "bad_channel" not in table.columns:
        return data

    bad_mask = table.loc[rows, "bad_channel"].eq(True).to_numpy()

    if bad_mask.all():
        return None

    if not bad_mask.any():
        return data

    good_pos = np.where(~bad_mask)[0]
    cleaned_rows = rows[good_pos]

    raw = np.asarray(data.data[:])
    if raw.ndim != 2:
        return data

    if raw.shape[1] == len(rows):
        cleaned_data = raw[:, good_pos]
    elif raw.shape[0] == len(rows):
        cleaned_data = raw[good_pos, :]
    else:
        raise ValueError(
            f"Cannot match electrode dimension for {data.name}: "
            f"data shape={raw.shape}, n_electrodes={len(rows)}"
        )

    electrodes = DynamicTableRegion(
        name="electrodes",
        data=cleaned_rows,
        description="Good electrodes for analysis",
        table=reg.table,
    )

    kwargs = dict(
        name=f"{data.name}_bad_channels_removed",
        data=cleaned_data,
        electrodes=electrodes,
        description=f"{getattr(data, 'description', '')} (bad channels removed)",
        conversion=float(getattr(data, "conversion", 1.0) or 1.0),
    )

    if getattr(data, "timestamps", None) is not None:
        kwargs["timestamps"] = np.asarray(data.timestamps[:])
    else:
        kwargs["starting_time"] = float(getattr(data, "starting_time", 0.0))
        if getattr(data, "rate", None) is not None:
            kwargs["rate"] = float(data.rate)

    if getattr(data, "comments", None) is not None:
        kwargs["comments"] = data.comments
    if getattr(data, "resolution", None) is not None:
        kwargs["resolution"] = data.resolution
    if getattr(data, "offset", None) is not None:
        kwargs["offset"] = data.offset
    if getattr(data, "filtering", None) is not None:
        kwargs["filtering"] = data.filtering

    return ElectricalSeries(**kwargs)
