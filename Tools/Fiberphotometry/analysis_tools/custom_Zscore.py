# -*- coding: utf-8 -*-
"""
Created on Fri Apr 12 12:40:09 2024

@author: PierreA.DERRIEN
"""

import pandas as pd


def zscore(data, data_column, time_column, window):
    data = data.copy()
    mean= data[(data[time_column] > window[0])&(data[time_column] < window[1])][data_column].mean()
    std= data[(data[time_column] > window[0])&(data[time_column] < window[1])][data_column].std()
    data["Zscore"] = (data[data_column]-mean)/std
    
    return data , mean , std

def zscorewOoutlier(data, data_column, time_column, window, outlier_threshold=95):
    data = data.copy()
    upthreshold_value = pd.Series(data[data_column]).quantile(outlier_threshold/100)
    bottomthreshold_value = pd.Series(data[data_column]).quantile((100 - outlier_threshold)/100)
    meandata = data[(data[data_column] > bottomthreshold_value)&(data[data_column] < upthreshold_value)][data_column]
    mean= meandata.mean()
    std= meandata.std()
    zscores = (data[data_column]-mean)/std
    data["Zscore"] = zscores
    return data , mean , std


def zscore_trials(data:pd.DataFrame ,psth_window:list):
    data = data.copy()
    data["Time(s)"] = data.index
    all_zscores = pd.DataFrame()
    for trial in data.columns:
        if trial == "Time(s)":
            continue
        zscored_trial, mean, std = zscore(data, trial, "Time(s)", psth_window)
        all_zscores[trial] = zscored_trial["Zscore"]
    
    return all_zscores