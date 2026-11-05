# -*- coding: utf-8 -*-
"""
Created on Mon Feb 26 13:44:16 2024

@author: PierreA.DERRIEN
"""

from sklearn.linear_model import LinearRegression
import numpy as np
from scipy.optimize import curve_fit
import pandas as pd
from matplotlib import pyplot as plt


def isobestic_correction(channel, Fiber, Iso, plot= False, return_prediction = False):
    """
    Calculate delta F over F for a given channel.

    Parameters:
    - channel (DataFrame): The channel DataFrame containing "Iso" and "Fiber" columns.
    - Fiber (str): name of the column containing the biosensor dependant signal
    - Iso (str): name of the column containing the biosenseur independant signal

    Returns:
    None
    """
    channel = channel.copy()
    X = channel[Iso].values.reshape(-1, 1)
    y = channel[Fiber].values.reshape(-1, 1)
    
    # Perform linear regression
    regression = LinearRegression().fit(X=X, y=y)
    if plot:
        plt.figure("Linear regression")
        plt.scatter(X, y)
    
    # Predict values using the regression model
    predicted_values = regression.predict(X)
    if plot:
        plt.figure("Predicted value")
        plt.plot(predicted_values)
    # Calculate delta F over F and store it in the channel DataFrame
    if return_prediction:
        channel["Predicted value"] = predicted_values
    channel["df_over_f"] = (y - predicted_values)/ channel[Iso].values.reshape(-1, 1)
    return channel

def fit_expo(x, a, b, c):
    return a * np.exp(-b * x) + c



def photobleaching_correction(channel, Fiber = None, Time = None, seed = [5 , 0.1, 1]):
    """
    Parameters
    ----------
    channel : pd.Dataframe, pd.Serie, np.array, list
        Can be a dataframe that contain the biosensor dependant signal in a columns with index as the time value
        or a pandas Serie, np.array or list that represent the dependant signal
    Fiber : str, optional
        In case the channel is a pd.Dataframe, is used to define the column wich contain the biosensor dependant signal. 
        The default is None.
    Time : pd.Dataframe, pd.Serie, np.array, list, optional
        In case the channel is not a pd.Dataframe, correspond to the time vector used. 
        The default is None.

    Returns
    -------
    ChForFit : pd.Dataframe, pd.Serie, np.array, list
        Correspond the biosensor dependant signal used for fitting.
    dFF : TYPE
        signal after photobleaching correction.
    Fit_type : str
        type of fitting used for the photobleaching correction.

    """
    
    if type(channel) == pd.DataFrame:
        assert type(Fiber) == str 
        ChForFit = channel[Fiber] # targeted filtered channel
        Time = channel.index
    else:
        assert type(channel) == list or type(channel) is pd.Series or type(channel) is np.array
        assert type(Time) == list or type(Time) is pd.Series or type(Time) is np.array
        ChForFit = channel

    

    try:
        popt, pcov = curve_fit(fit_expo, Time, ChForFit, p0 = seed)
    except RuntimeError:
        print('RuntimeError: Optimal parameters not found! Try Polynomial fit')
        Ch_coef = np.polyfit(Time, ChForFit, deg=2)  # !!ATTENTION to the degree value!!
        Ch_polyfit = np.polyval(Ch_coef, Time)
        control_arti = Ch_polyfit
        Fit_type = 'Polynomial fit'

    else:
        print('Works with mono_fit!')
        print(popt)
        control_mono = fit_expo(Time, *popt)
        control_arti = control_mono
        Fit_type = 'Monoexponential fit'
        
    dF = np.subtract(ChForFit, control_arti)
    dFF = np.divide(dF, control_arti)
    # nor_dFF = dFF * 100
    
    if type(channel) == pd.DataFrame:
        dataframe = channel.copy()
        dataframe["df_over_f"] =  dFF
        dFF = dataframe.copy()
    
    return ChForFit, dFF, (Fit_type, control_arti)

def extract_local_minimum(data, Fiber = None, Time = None, window = 10):
    """
    Extract local minimum from a channel.

    Parameters:
    - channel (DataFrame): The channel DataFrame containing "Fiber" column.
    - Fiber (str): name of the column containing the biosensor dependant signal
    - Time (pd.DataFrame, pd.Series, np.array, list): Time vector used for the channel.
    - window (int): Size of the window to consider for local minimum detection in sec.
    
    Returns:
    - local_minimums (list): List of indices where local minima are found.
    """
    if Time == None:
        data = data.reset_index()
        Time = data.columns[0]  # assuming the first column is the time vector
    
    print("Data columns:", data.columns)
    print("Using Time column:", Time)

    window = int(window)  # ensure window is an integer
    if type(data) == pd.DataFrame:
    
        local_minimums_times = []
        local_min_val = []
        # Find the local minimum in the first 100 points and add it as the initial value (Time = 0)
        first_segment = data.iloc[:100]
        if Fiber is not None:
            first_segment_vals = first_segment[Fiber]
        else:
            return "Input a dataframe with a Fiber column or a Serie"
        local_min = first_segment_vals.min()
        local_min_index = first_segment_vals.idxmin()
        print("Initial local minimum at index:", local_min_index, "with value:", local_min)
        local_minimums_times = [data[Time][local_min_index]]
        local_min_val = [local_min]

        max_Time = data[Time].max()
        num_windows = int(max_Time // window) + 1  # number of windows based
        for i in range(num_windows):
            start_time = i * window
            end_time = start_time + window
            segment = data[(data[Time] >= start_time) & (data[Time] < end_time)]
            if segment.empty:
                continue
            if Fiber is not None:
                segment = segment[Fiber]
            else:
                return "Input a dataframe with a Fiber column or a Serie"
            if segment.empty:
                continue
            # Find the local minimum in the segment
            local_min = np.percentile(segment, 10)  # using 10th percentile to avoid outliers
            local_min_index = segment.idxmin()
            local_minimums_times.append(data[Time][local_min_index])
            local_min_val.append(local_min)

           



            
            
        local_minimums = pd.DataFrame({"Time": local_minimums_times, "Value": local_min_val})
        return local_minimums
    else: 
        print("Input a dataframe or a Serie")
    
def local_min_photobleaching_correction(data, Fiber = None, Time_id = None, window = 10, seed = [5 , 0.1, 1]):
    """
    Apply photobleaching correction using local minimums.

    Parameters:
    - data (DataFrame): The channel DataFrame containing "Fiber" column.
    - Fiber (str): name of the column containing the biosensor dependant signal
    - Time (pd.DataFrame, pd.Series, np.array, list): Time vector used for the channel.
    - window (int): Size of the window to consider for local minimum detection in sec.
    
    Returns:
    - corrected_data (DataFrame): DataFrame with corrected values.
    """
    
    local_minimums = extract_local_minimum(data, Fiber=Fiber, Time=Time_id, window=window)
    


    
        
    ChForFit = local_minimums["Value"] # targeted filtered channel
    Time = local_minimums["Time"]
   

    try:
        popt, pcov = curve_fit(fit_expo, Time, ChForFit, p0 = seed)
    except RuntimeError:
        print('RuntimeError: Optimal parameters not found! Try Polynomial fit')
        Ch_coef = np.polyfit(Time, ChForFit, deg=2)  # !!ATTENTION to the degree value!!
        Ch_polyfit = np.polyval(Ch_coef, data[Time_id])
        control_arti = Ch_polyfit
        Fit_type = 'Polynomial fit'

    else:
        print('Works with mono_fit!')
        print(popt)
        control_mono = fit_expo(data[Time_id], *popt)
        control_arti = control_mono
        Fit_type = 'Monoexponential fit'

    dF = np.subtract(data[Fiber], control_arti)
    dFF = np.divide(dF, control_arti)  # gain correction using local minimum fitting
    # nor_dFF = dFF * 100
    
    if type(data) == pd.DataFrame:
        dataframe = data.copy()
        dataframe["df_over_f"] =  dFF
        dFF = dataframe.copy()
    
    return ChForFit, dFF, (Fit_type, control_arti)