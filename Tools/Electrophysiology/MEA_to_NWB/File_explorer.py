# -*- coding: utf-8 -*-
"""
Created on Thu Feb  6 14:31:06 2025

@author: PierreA.DERRIEN
"""

from pynwb import NWBHDF5IO
import pandas as pd
import seaborn as sb
from matplotlib import pyplot as plt
import numpy as np
file = r"E:\MEA_data\Test_files\Test.nwb"
io = NWBHDF5IO(file)
nwb = io.read()

time_start = 2000
time_end = 2100

print(nwb.electrodes)
# display electrode map
plt.figure("Electrode Map")
elec_map = {}
for ind_elec, elec in enumerate(nwb.electrodes):
    elec_map[elec.index.tolist()[0]] = (int(elec["x"].tolist()[0]/200), int(elec["y"].tolist()[0]/200))
    sb.scatterplot(x=elec["x"], y= elec["y"], color = "grey")
    plt.text(x=elec["x"], y= elec["y"], s= elec.index.tolist()[0])

plt.figure("Raw data Zoom")
sr = nwb.acquisition["Raw_data"].rate
for i in range(60):
    data = (nwb.acquisition["Raw_data"].data[int(time_start*sr):int(time_end*sr),i][::50])+i
    sb.lineplot(data, color = "black")

print("This file contains {} cleaned units".format(len(nwb.units)))

select = [0,10]
for i,  unit in enumerate(nwb.units):
    if i >= select[0] and i<= select[1]:
        fig, axs = plt.subplots(8,8, sharey=True, num = "Mean Waveform {}".format(unit.index.tolist()[0]))
        fig.suptitle(f'Unit {unit.index}')
        for elec in range(60):
            
            sb.lineplot(unit['waveform_mean'].tolist()[0][:,elec], ax= axs[elec_map[elec][0],elec_map[elec][1]],legend = False, color = "black")
            y1=unit['waveform_mean'].tolist()[0][:,elec]+unit['waveform_sd'].tolist()[0][:,elec]
            y2=unit['waveform_mean'].tolist()[0][:,elec]-unit['waveform_sd'].tolist()[0][:,elec]
            axs[elec_map[elec][0],elec_map[elec][1]].fill_between([x for x in range(len(unit['waveform_mean'].tolist()[0][:,elec]))], y1, y2)
  
plt.figure("Raster plot by Units")
for i, unit in enumerate(nwb.units):
    sb.scatterplot(x = unit.spike_times.tolist()[0], y = i, marker="|", color = "black")
    
    