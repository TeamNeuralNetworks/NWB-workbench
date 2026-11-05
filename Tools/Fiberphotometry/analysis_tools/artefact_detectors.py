# -*- coding: utf-8 -*-
"""
Created on Mon May  6 11:30:20 2024

@author: PierreA.DERRIEN
"""
import pandas as pd
import numpy as np
from matplotlib import pyplot as plt
import seaborn as sb

def gradient_detector(Data : pd.DataFrame, gradient_thresh = 5.5, display = True, downscale_disp = 100):
    
    """
    Detect artifacts in the data using a gradient approach.

    Parameters:
    - Data (DataFrame): The data to analyze.
    - gradient_thresh (float): number of std above the mean toi define an artifact
    - display (boolean): allox the display of the gradient computed with the artifacts detected

    Returns:
    - artifact (dict): A dictionary containing the detected artifacts.
    """
  
    artifacts = {}
  
    for columns in Data.columns:
        if display:
            fig,axes = plt.subplots(2,1,num=  columns, sharex= True)
            fig.suptitle(columns)
        Trial = Data[columns]

        Trial_derivative =pd.Series(index = Trial.index , data =np.gradient(Trial.values.flatten(), Trial.index.to_list()) )
        Data_arti = Trial[((Trial_derivative>(Trial_derivative.mean() + Trial_derivative.std()*gradient_thresh)) | (Trial_derivative<(Trial_derivative.mean() - Trial_derivative.std()*gradient_thresh)))]
        
        if len(Data_arti.index.to_list())>2:
            artifacts[columns] = Data_arti.index.to_list()
        
            
        if display: 
            axes = axes.flatten()
            
            sb.lineplot(x=Trial_derivative.index.to_list()[::downscale_disp],y=Trial_derivative.values.flatten()[::downscale_disp],ax=axes[0],label="gradient",color="navy")
            sb.lineplot(x=Trial.index.to_list()[::downscale_disp],y=Trial.values.flatten()[::downscale_disp],ax=axes[1])
    
            sb.scatterplot(x=Data_arti.index.to_list(),y=Data_arti.values.flatten(),ax=axes[1],color="red")
            axes[0].axhline(y=Trial_derivative.mean() + Trial_derivative.std()*gradient_thresh,color="red",label="threshold")
            axes[0].axhline(y=Trial_derivative.mean() - Trial_derivative.std()*gradient_thresh,color="red")
            axes[0].legend()
            plt.legend()

    return artifacts

def windowed_gradient_detector(Data : pd.DataFrame,  window_size: int, gradient_thresh = 5.5, display = True, downscale_disp = 100):
    
    """
    Detect artifacts in the data using a sliding window approach.

    Parameters:
    - Data (DataFrame): The data to analyze.
    - window_size (int): The size of the sliding window.
    - Data (DataFrame): The data to analyze.
    - gradient_thresh (float): number of std above the mean toi define an artifact
    - display (boolean): allox the display of the gradient computed with the artifacts detected

    Returns:
    - artifact (dict): A dictionary containing the detected artifacts.
    """
  
    artifacts = {}
    for columns in Data.columns:
        if display:
            fig,axes = plt.subplots(2,1,num=  columns , sharex= True)
            fig.suptitle(columns)
        for i in range(len(Data)// window_size):
            if i == 0:
                argA = {"label":"gradient"}
                argB = {"label":"threshold"}
            else: 
                argA = {}
                argB = {}
            window = (i*window_size, (i*window_size)+window_size)
            idx_name = Data.index.name
            Trial = Data[columns].reset_index()[window[0]:window[1]]
            Trial.set_index(idx_name, inplace = True)
            Trial_derivative =pd.Series(index = Trial.index , data =np.gradient(Trial.values.flatten(), Trial.index.to_list()) )
            Data_arti = Trial[((Trial_derivative>(Trial_derivative.mean() + Trial_derivative.std()*gradient_thresh)) | (Trial_derivative<(Trial_derivative.mean() - Trial_derivative.std()*gradient_thresh)))]
            
            if len(Data_arti.index.to_list())>2:
                artifacts[columns+ ' - from '  + str(Trial.index.tolist()[0]) + " to "+ str(Trial.index.tolist()[-1])] = Data_arti.index.to_list()
            
            if display: 
                
                axes = axes.flatten()
                
                sb.lineplot(x=Trial_derivative.index.to_list()[::downscale_disp],y=Trial_derivative.values.flatten()[::downscale_disp],ax=axes[0],color="navy", **argA)
                sb.lineplot(x=Trial.index.to_list()[::downscale_disp],y=Trial.values.flatten()[::downscale_disp],ax=axes[1])
                
                x_val = Trial_derivative.index.tolist()
                
                sb.scatterplot(x=Data_arti.index.to_list(),y=Data_arti.values.flatten(),ax=axes[1],color="red")
                axes[0].plot([x_val[0], x_val[-1]],[ Trial_derivative.mean() + Trial_derivative.std()*gradient_thresh for x in range(2)],color="red",**argB)
                axes[0].plot([x_val[0], x_val[-1]], [Trial_derivative.mean() - Trial_derivative.std()*gradient_thresh for x in range(2)],color="red")
        if display:
            axes[0].legend()
            plt.legend()

    return artifacts

def artifacts_display(Data : pd.DataFrame, artefacts : dict):
    
    for key, artefacts in artefacts.items():
        if len(artefacts) > 0: 
            time_zone = key.split(" - from ")[-1].split(" to ")
            artefact_window = Data[(Data.index >= float(time_zone[0]))
                                   &
                                   (Data.index <= float(time_zone[1]))]
            fig = plt.figure()
            fig.suptitle(key)
            plt.plot(artefact_window.index, artefact_window[key.split(" - from ")[0]])
            y_val = []
            for points in artefacts:
                y_val.append(Data[Data.index == points][key.split(" - from ")[0]].tolist()[0])
            sb.scatterplot( x= points, y= y_val, color = "red")