# -*- coding: utf-8 -*-
"""
Created on Fri Jun 12 11:24:52 2026

@author: bivoc
"""
import spikeinterface.preprocessing as spre

def common_reference_removal(recording,
            operator='median', reference='global',
            ):
    recording = spre.common_reference(recording, reference=reference, operator=operator)

    return recording


PREPROCESSING = {
    "name": "Common reference removal",
    "description": "Remove the median signal from all channels",
    "func": common_reference_removal,
    "default": True,
    "params": {

        "operator": {
            "type": "list",
            "values": ['median', 'average'],
            "default": "median"
        },
        "reference": {
            "type": "list",
            "values": ['global', 'local'],
            "default": "global"
        },
    },
}