# -*- coding: utf-8 -*-
"""
Created on Tue Feb 27 14:06:05 2024

@author: PierreA.DERRIEN
"""
import pandas  as pd
import numpy as np
from matplotlib import pyplot as plt
import seaborn as sb

def extract_trials(data, signal_channel, trigger_channel, winsize, plotting = False, plot_mean= False, compact = False, compact_value = 3):
    """
    Extracts trials from the data based on trigger events and a specified window size.

    Parameters
    ----------
    data : pandas.DataFrame
        The input data containing signal and trigger channels.
    signal_channel : str
        The column name in the data DataFrame that contains the signal values.
    trigger_channel : str
        The column name in the data DataFrame that contains the trigger events.
    winsize : tuple of int
        A tuple specifying the window size around each trigger event (pre-trigger, post-trigger) in second.
    plotting : bool, optional
        Whether to plot each extracted trial. The default is False.
    plot_mean : bool, optional
        Whether to plot the mean of all extracted trials with the standard error of the mean (SEM). The default is False.
    compact : bool, optional
        Whether to downsize the data by grouping values within a specified range. The default is False.
    compact_value : int, optional
        The number of decimal places to round the time index for downsizing. The default is 3, corresponding to downsizing to 1ms.

    Returns
    -------
    trials_df : pandas.DataFrame
        A DataFrame containing the extracted trials, with each column representing a trial.
    """   
    
    data = data.copy()  # Create a copy of the data to avoid modifying the original DataFrame
    # Identify the times at which triggers occur
    trig_time = data[data[trigger_channel] ==1].index.tolist()
    # Initialize an empty DataFrame to store the extracted trials
    trials_df = pd.DataFrame()
    n= 0

    # Add a "Time" column to the data
    data["Time"] = data.index

    if compact:
        print("Downsizing the data")
    else: 
        print("Extracting the trials without downsizing, be sure to check the alignement of the extraction")
    for trig in trig_time:
        # Extract the signal data around the trigger event within the specified window size
        trials_data = data[(data["Time"]>= trig-winsize[0]) & (data["Time"]<= trig+winsize[1])][signal_channel]

        # Create a time index relative to the trigger event
        time_list = np.linspace((-1)*winsize[0], winsize[1], len(trials_data))
        trials_data.index = time_list
        #Downsizing the data if activated
        if compact:
            hm = trials_data.reset_index()
            hm["Time"] = round(hm["index"],compact_value)
            hm = hm.groupby("Time").mean()
            hm = hm.drop("index", axis=1)
            trials_data = hm
        # Add the extracted trial to the trials DataFrame
        if n == 0:
            trials_df["Trial_"+str(n)]= trials_data
        if len(trials_df) != len(trials_data):
            print("/!\ Warning /!\ At trials {2}, length dataframe : {0} , length trials : {1}".format(len(trials_df), len(trials_data), n))
            len_df= min(len(trials_df), len(trials_data))
            trials_df = trials_df[:len_df]
            trials_data = trials_data[:len_df]
            print("Dataframe and trials resized to length : {0} => dataframe {1} - trial{2}".format(len_df, len(trials_df), len(trials_data)))
        trials_df["Trial_"+str(n)]= trials_data
        n+=1
        if plotting:
            plt.figure()
            plt.plot(trials_data)

    if plot_mean: #Display of mean of the trials
        plt.figure("Mean value of {0} trials for {1} at {2}".format(n,signal_channel, trigger_channel))
        plt.suptitle("Mean value of {0} trials for {1} at {2}".format(n,signal_channel, trigger_channel))
        mean = trials_df.mean(axis=1)
        sem = trials_df.sem(axis=1)
        sb.lineplot(x = mean.index, y= mean, color = "red", linestyle = "--")
        plt.fill_between(sem.index, y1= mean+sem, y2 =  mean-sem, alpha = 0.5)

    return trials_df