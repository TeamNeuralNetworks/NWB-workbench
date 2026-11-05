# -*- coding: utf-8 -*-
"""
Created on Mon May  6 15:41:41 2024

@author: PierreA.DERRIEN
"""

from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.NWB_to_Dataframe import NWB_to_Dataframe_converter
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.preprocessing_tools.photobleaching_corrector import isobestic_correction , photobleaching_correction
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.artefact_detectors import windowed_gradient_detector
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.preprocessing_tools.fiber_filters import savgol_filtering
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.custom_Zscore import zscore
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.trials_extraction import extract_trials

import gc
import time
import seaborn as sb
from matplotlib import pyplot as plt
from yaspin import yaspin
import matplotlib
import numpy as np

# =============================================================================
# Setup Parameters
# =============================================================================
file = "path/to/file"

analysis_window = [0, 800]

detect_artifact = True
display_artifacts = True
gradient_window = 0.1 # 1 = 100% of the data / 0 = 0% of the data
correct_artifact = "Interpolate" 
#methods: "Drop" = suppress the data point
#         "Interpolate" = find the interpolation between the last good data point and first new good data point


filter_before_df_over_f = False #Activate the savgol filter
display_filter = True
filter_window = 0.001

display_photobleaching = True

overall_zscore = True #Compute a Zscore on whole data
zscore_window = [0, 300] #Time window where to calculate Mean and SD for the ZScore
display_overall_zscore = True

TTL_to_analyse = "Female Interaction" #Name of the TTL channel used to extract event
event_window = [10,10] #Time, in sec, before and after the event to extract signal
display_mean_trials = True 

start_script = time.time()
# =============================================================================
# Preprocessing
# =============================================================================
# Retrieve your data and metaData from the NWB file
NWB_converter = NWB_to_Dataframe_converter(file)
NWB_converter.convert()

# from the metaData extract the TTL, raw and demodulated signals names
TTL = []
raw_signals = []
demod_signals = []
isobestic_signals = {}
for signal in NWB_converter.get_metaData()["Acquisition"].keys():
    if "| TTL signals" in signal:
        TTL.append(signal.split(" | ")[0])
    elif "demodulated_signal" in signal:
        demod_signals.append(signal)
    elif "raw_signal" in signal:
        raw_signals.append(signal)
    elif "isobestic"in signal:
        isobestic_signals[signal.replace("isobestic","raw_signal")] = signal
        
# Collect the artifacts timestamps in a dictionnary using a gradient based approach
if detect_artifact:
    start_time = time.time()
    data = NWB_converter.get_data()[demod_signals].dropna()
    artifacts= windowed_gradient_detector(data, int(len(data)*gradient_window), display = display_artifacts)
    print("Artifact detection time: %s seconds" % (time.time() - start_time))
    
    if correct_artifact:
        print("Artifact correction methods : {0}".format(correct_artifact))     
        if correct_artifact == "Drop": 
            for channel, artefact_times in artifacts.items():
                channel = channel.split(" - ")[0]
                for point in artefact_times:
                    data.at[point, channel] = np.nan
        elif correct_artifact == "Interpolate":     
            for channel, artefact_times in artifacts.items():
                channel = channel.split(" - ")[0]
                for point in artefact_times:
                    if point != data.index[0]: #if the first point is defined as artifact ignored to allows interpolation
                        data.at[point, channel] = np.nan
            print("Number of artifact before correction")
            print(data.isna().sum())
            data.interpolate(method= "linear", inplace = True, axis = 0)
            print("Number of artifact post correction")
            print(data.isna().sum())
        print("Artifact correction time: %s seconds" % (time.time() - start_time))
            # pass
    all_data = data   
    del data
    gc.collect()       
    
    
# all_data = NWB_converter.get_data().dropna()    
#Generate the figure subplots
if display_photobleaching:
    fig_pb , ax_pb = plt.subplots((max(1, len(demod_signals)//2)), abs((len(demod_signals)%2)-1)+1)
    fig_pb.canvas.manager.set_window_title('Display photobleaching') 
if display_overall_zscore:
    fig_ozs , ax_ozs = plt.subplots((max(1, len(demod_signals)//2)), abs((len(demod_signals)%2)-1)+1)    
    fig_ozs.canvas.manager.set_window_title('Display Zscore')

i= 0
for signal in demod_signals:
    print("-------------------------------------------------------------------")
    print(" Start Analysis of {0}".format(signal))
    print("-------------------------------------------------------------------")
    start_time = time.time()
    filtered_demod = ""
    #  extract a subset of data to analyse based on the timestamps
    data = all_data[(all_data.index >= analysis_window[0])&(all_data.index <= analysis_window[1])]
    
    # Filtering the data before photobleaching correction
    if filter_before_df_over_f:
        #  Using a savgol filter to reduced noise and extract the general tendency in the signals
        print("Start filtering with a window of {0} points - that represent {1} seconds".format(int(len(data)*filter_window), data.index[int(len(data)*filter_window)] - data.index[0]))
        with yaspin(text = "Filtering") as spinner:
            filtered_signals = savgol_filtering(data, Fiber = signal, win_size = int(len(data)*filter_window))
            spinner.write(" Filtering time: {0} min {1} seconds".format(((time.time() - start_time)//60) , (time.time() - start_time)%60))
            filtered_demod = "_filter"
        if display_filter:
            fig = plt.figure("Display Filtering of {0}".format(signal))
            plt.suptitle("Display Filtering of {0}".format(signal))
            sb.lineplot(x=filtered_signals.index, y=filtered_signals[signal], label='Signal')
            sb.lineplot(x=filtered_signals.index, y=filtered_signals[signal+"_filter"], label='Filtered signal')
        data = filtered_signals
        
        
    # Correction of photobleaching - Autoselection based on 
    pb_time = time.time()
    if signal in isobestic_signals.keys():
        print("Using Isobestic correction on {0}".format(signal))
        pb_data = isobestic_correction(data, signal+filtered_demod, isobestic_signals[signal])
        
    else: 
        
        print("Using Regression correction on {0}".format(signal))
        ChForFit, pb_data , Fit = photobleaching_correction(data, Fiber = signal+filtered_demod)
    print("Photobleaching correction time: %s seconds" % (time.time() - pb_time))
    
    # Display of the signal and corrected signal
    if display_photobleaching:
        label = None
        # fig = plt.figure("Display photobleaching of {0}".format(signal))
        if len(demod_signals)==2:
            ax = ax_pb[i]
        elif len(demod_signals)>2:
            ax = ax_pb[i//2, i%1]
        else:
            ax = ax_pb
        ax.title.set_text("Display photobleaching of {0}".format(signal))
        sb.lineplot(x=pb_data.index, y=pb_data[signal], label='Signal', legend= False, ax=ax)
        if i == 0 :
            label = "Fit: {0}".format(Fit[0])
        sb.lineplot(x=pb_data.index, y = Fit[1], label = label , legend= False, ax=ax)
        ax2 = ax.twinx()
        if i == 0:
            label='df/f'
        sb.lineplot(x=pb_data.index, y=pb_data["df_over_f"], label=label, color = "orange", legend= False, ax=ax2)
        fig_pb.legend(loc = "upper right")
    
    # Zscore of the overall signal
    if overall_zscore:
        zscore_data, mean_data, std_data = zscore(pb_data.reset_index(), "df_over_f", "Timestamps", zscore_window)        
        if display_overall_zscore:
            if len(demod_signals)==2:
                ax = ax_ozs[i]
            elif len(demod_signals)>2:
                ax = ax_ozs[i//2, i%1]
            else:
                ax = ax_ozs
            ax.title.set_text("Display Zscore of {0}".format(signal))
            sb.lineplot(x=pb_data.index, y=pb_data["df_over_f"], label='df/f', color = "orange", legend= False, ax=ax)
            ax2 = ax.twinx()
            sb.lineplot(data = zscore_data, x="Timestamps", y=zscore_data["Zscore"], label="Zscore", color = "red", legend= False, ax=ax2)
    
    # Extraction of event based on a TTL signal 
    list_event_time = zscore_data[zscore_data[TTL_to_analyse+" | TTL signals"].diff()==1]["Timestamps"].tolist()
    zscore_data["Event"] = zscore_data[TTL_to_analyse+" | TTL signals"].diff()
    trials_event = extract_trials(zscore_data.set_index("Timestamps"), "Zscore", "Event", event_window, plot_mean = display_mean_trials, compact = True, compact_value = 3)

    i+=1
    # break



print("Total execution time: {0} min {1} seconds".format((time.time() - start_script)//60 , (time.time() - start_script)%60))