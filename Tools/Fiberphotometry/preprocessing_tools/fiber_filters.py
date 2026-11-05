# -*- coding: utf-8 -*-
"""
Created on Mon Feb 26 15:00:18 2024

@author: PierreA.DERRIEN
"""

from scipy.signal import savgol_filter
import pandas as pd
def savgol_filtering(channel, win_size, Fiber = "",  polyorder = 3):
    if type(win_size)== float:
        win_size = int(win_size)
    
    if (win_size%2) ==0:
        print(" winsize is pair")
        win_size +=1
    
    if type(channel) == pd.DataFrame:
        ch_to_filter = channel[Fiber]
    else: 
        ch_to_filter = channel
    sg_filter = savgol_filter(ch_to_filter, window_length=win_size, polyorder=polyorder)
    
    if type(channel)== pd.DataFrame:
        channel = channel.copy()
        channel[Fiber+"_filter"] = sg_filter
        return channel
    else: 
        return sg_filter