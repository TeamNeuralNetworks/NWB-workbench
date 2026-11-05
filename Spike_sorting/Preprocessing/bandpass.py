# -*- coding: utf-8 -*-
"""
Created on Fri Jun 12 11:24:50 2026

@author: bivoc
"""
import spikeinterface.preprocessing as spre

def bandpass(recording,
            freq_min=300, freq_max=6000,
            ):

    recording = spre.bandpass_filter(recording, freq_min=freq_min, freq_max=freq_max)

    return recording


PREPROCESSING = {
    "name": "Bandpass filter",
    "description": "Applay a band pass filter to the recording",
    "func": bandpass,
    "default": True,
    "params": {

        "freq_min": {
            "type": "float",
            "default": 300,
        },
        "freq_max": {
            "type": "int",
            "default": 6000,
        },
    },
}