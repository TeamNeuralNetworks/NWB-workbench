# -*- coding: utf-8 -*-
"""
Created on Wed Jun 19 12:39:35 2024

@author: PierreA.DERRIEN
"""
import numpy as np 
from sklearn.metrics import auc

def detect_activation(data, data_column, time_column,parameter, before = False, neg_start_correction= False, threshold_zone = [] , percent = False):
    """
    Detects the activation point in the given data based on specified parameters.

    Parameters
    ----------
    data : pandas.DataFrame
        The input data containing time and measurement columns.
    data_column : str
        The column name in the data DataFrame that contains the measurement values.
    time_column : str
        The column name in the data DataFrame that contains the time values.
    parameter : dict
        A dictionary containing the parameters for activation detection, including:
            - "start_time": The time to start the analysis.
            - "thresh_acti": The threshold multiplier for activation detection in SD.
            - "min_time_rep": The minimum time required for a valid activation response.
    before : bool, optional
        Whether to consider the first data point before or after the threshold crossing. The default is False.
    neg_start_correction : bool, optional
        Whether to correct negative start times to zero, and correct to start of response at 0. The default is False.

    Returns
    -------
    point : pandas.DataFrame
        The data points where the threshold crossing were detected.
    rep_data : pandas.DataFrame
        The data points within the detected activation period.
    result : dict
        A dictionary containing various activation metrics, including:
            - "Maximum_val": The maximum value of the measurement during activation.
            - "Maximum_time": The time at which the maximum value occurs.
            - "Start_rep": The start time of the activation response - crossing threshold.
            - "End_rep": The end time of the activation response - crossing threshold.
            - "Real_end": The real end time of the activation period - return to Mean.
            - "AUC_rep": The area under the curve (AUC) of the activation period.
            - "Thresh_acti": The threshold value for activation detection.
            - "Mean": The mean of the measurement values before the start time.
            - "STD": The standard deviation of the measurement values before the start time.
    """
    data = data.copy()
    if len(threshold_zone)== 0:
        threshold_zone.append(data[time_column].min())
        threshold_zone.append(0)
    zone_to_thresh = (data[time_column]>=threshold_zone[0])&(data[time_column]<= threshold_zone[1])
    mean= data[zone_to_thresh][data_column].mean()
    
    
    # define threshold of response 
    std= data[zone_to_thresh][data_column].std()
    data= data[data[time_column]>=parameter["start_time"]]
    if percent: 
        threshold_acti = mean + ((parameter["thresh_acti"]/100)*mean)
    else:
        threshold_acti = mean + (parameter["thresh_acti"]*std)
    # find the biggest zone above the threshold after zero and check if its above min_time rep
    mask = np.diff(1 * (data[data_column] >  threshold_acti) != 0) 
    if before:
        point = data[np.append(mask, [False])]
    else:
        point = data[np.insert(mask, 0, [False])]
        
    if data[data_column].tolist()[0]>=  threshold_acti: 
        point = point.iloc[1:]
    if len(point)>1:
        for start_rep,end_rep in zip(point[time_column][0::2], point[time_column][1::2]):
            rep_time = end_rep-start_rep
            if rep_time > parameter["min_time_rep"] :
             if start_rep >parameter["start_time"] :
                 if end_rep > 0.2:
                     break #stop a the first couple with rep above the min time set
            start_rep,end_rep = "Unfinded", "Unfinded"
                
    elif len(point)==1:
        if point[time_column].tolist()[0] >=parameter["start_time"]:
            start_rep = point[time_column].tolist()[0]
            end_rep = data[time_column].max()
        else: 
            start_rep = "Unfinded"
    else: 
        start_rep = "Unfinded"
    
    if start_rep != "Unfinded":
        if neg_start_correction:
            if start_rep <0:
                start_rep = 0        
        real_end = data[(data[time_column]> start_rep)&(data[data_column]<mean)][time_column].tolist()
        
        if len(real_end) >0:
            real_end = real_end[0]
            
        else:
            real_end ="Unfinded"
        # Now calculate the parameter needed
        if real_end != "Unfinded":
            rep_data = data[(data[time_column] > start_rep)&(data[time_column] < real_end)]
        else: 
           
            rep_data = data[data[time_column] > start_rep]
            print(rep_data)
        
        if len(rep_data) >=1:
            Maximum = rep_data[rep_data[data_column] == rep_data[data_column].max()]
            AUC = auc(rep_data[time_column], rep_data[data_column])
            
            return point, rep_data , {"Maximum_val":Maximum[data_column].values[0],"Maximum_time": Maximum[time_column].values[0],
                            "Start_rep":start_rep, "End_rep": end_rep, "Real_end": real_end,
                            "AUC_rep": AUC, 
                            "Thresh_acti":  threshold_acti, "Mean": mean, "STD": std
                            }
        else:
            Maximum = np.nan
            AUC = np.nan
            return point, rep_data , {"Maximum_val":Maximum,"Maximum_time": Maximum,
                            "Start_rep":start_rep, "End_rep": end_rep, "Real_end": real_end,
                            "AUC_rep": AUC, 
                            "Thresh_acti":  threshold_acti,  "Mean": mean, "STD": std
                            }
    else: 
        return point, "Unfinded" , {"Maximum_val":"Unfinded","Maximum_time": "Unfinded",
                        "Start_rep":"Unfinded", "End_rep": "Unfinded", "Real_end": "Unfinded",
                        "AUC_rep": "Unfinded", 
                        "Thresh_acti":  threshold_acti,  "Mean": mean, "STD": std
                        }
    
def detect_inhibition(data, data_column, time_column,parameter, before = False, neg_start_correction= False,  threshold_zone = [], percent = False):
    """
    Detects the inhibtion point in the given data based on specified parameters.

    Parameters
    ----------
    data : pandas.DataFrame
        The input data containing time and measurement columns.
    data_column : str
        The column name in the data DataFrame that contains the measurement values.
    time_column : str
        The column name in the data DataFrame that contains the time values.
    parameter : dict
        A dictionary containing the parameters for inhibition detection, including:
            - "start_time": The time to start the analysis.
            - "thresh_acti": The threshold multiplier for activation detection in SD.
            - "min_time_rep": The minimum time required for a valid activation response.
    before : bool, optional
        Whether to consider the first data point before or after the threshold crossing. The default is False.
    neg_start_correction : bool, optional
        Whether to correct negative start times to zero, and correct to start of response at 0. The default is False.

    Returns
    -------
    point : pandas.DataFrame
        The data points where the threshold crossing were detected.
    rep_data : pandas.DataFrame
        The data points within the detected inhibition period.
    result : dict
        A dictionary containing various activation metrics, including:
            - "Maximum_val": The maximum value of the measurement during inhibition.
            - "Maximum_time": The time at which the maximum value occurs.
            - "Start_rep": The start time of the inhibition response - crossing threshold.
            - "End_rep": The end time of the inhibition response - crossing threshold.
            - "Real_end": The real end time of the inhibition period - return to Mean.
            - "AUC_rep": The area under the curve (AUC) of the inhibition period.
            - "Thresh_acti": The threshold value for inhibition detection.
            - "Mean": The mean of the measurement values before the start time.
            - "STD": The standard deviation of the measurement values before the start time.
    """
    
    data = data.copy()
    #Just needing to invert the data as inhibition is the same as activation in the opposite direction
    data[data_column] = data[data_column]*(-1)
    point , rep_data, parameter =   detect_activation(data, data_column, time_column,parameter, before = False, neg_start_correction= False,  threshold_zone = threshold_zone, percent = percent)
    parameter["Maximum_val"] = (-1)* parameter["Maximum_val"]
    parameter["AUC_rep"] = (-1)* parameter["AUC_rep"]
    parameter["Thresh_acti"] = (-1)* parameter["Thresh_acti"]      
         
    if type(point) != type(str()):
        point.set_index("Time", inplace = True)
        point = (point*(-1)).reset_index()
    if type(rep_data) != type(str()):
        rep_data.set_index("Time", inplace = True)
        rep_data = (rep_data*(-1)).reset_index()
    
    return point , rep_data, parameter