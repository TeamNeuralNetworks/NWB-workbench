# -*- coding: utf-8 -*-
"""
Created on Mon Feb  5 11:59:05 2024

@author: Pierre-Alexis
"""
import numpy as np

def bandpower(data, sf, band, window_sec=None, relative=False):
    """Compute the average power of the signal x in a specific frequency band.

    Parameters
    ----------
    data : 1d-array
        Input signal in the time-domain.
    sf : float
        Sampling frequency of the data.
    band : list
        Lower and upper frequencies of the band of interest.
    window_sec : float
        Length of each window in seconds.
        If None, window_sec = (1 / min(band)) * 2
    relative : boolean
        If True, return the relative power (= divided by the total power of the signal).
        If False (default), return the absolute power.

    Return
    ------
    bp : float
        Absolute or relative band power.
    """
    from scipy.signal import welch
    from scipy.integrate import simps
    band = np.asarray(band)
    low, high = band

    # Define window length
    if window_sec is not None:
        nperseg = window_sec * sf
    else:
        nperseg = (2 / low) * sf

    # Compute the modified periodogram (Welch)
    freqs, psd = welch(data, sf, nperseg=nperseg)

    # Frequency resolution
    freq_res = freqs[1] - freqs[0]

    # Find closest indices of band in frequency vector
    idx_band = np.logical_and(freqs >= low, freqs <= high)

    # Integral approximation of the spectrum using Simpson's rule.
    bp = simps(psd[idx_band], dx=freq_res)

    if relative:
        bp /= simps(psd, dx=freq_res)
    return bp

def demodulator(data , fs, ctrl_bw, green_bw):
    demodSig= {"Ctrl":[],
               "Green":[],
               "Red":[]}
    # demodulation
    k=1
    N_samples = int(0.05*fs) #windows size for demod => final sampling 0.05ms <=> 20Hz
    for l in range(1,int((len(data)/N_samples-1))):
        # sub vector of 50ms
        ySig_sub= data.iloc[k:k+N_samples].tolist()
        # extract power of harmonic from subvector
        # change to +/- 10 Hz around your modulation frequency
        demodSig["Ctrl"].append(bandpower(ySig_sub, fs, ctrl_bw, window_sec = 0.5))
        demodSig["Green"].append(bandpower(ySig_sub, fs, green_bw, window_sec = 0.5))
        
        k= k + N_samples;
        
    return demodSig