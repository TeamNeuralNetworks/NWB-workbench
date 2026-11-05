# -*- coding: utf-8 -*-
"""
Created on Wed Jun  5 14:26:10 2024

@author: PierreA.DERRIEN
"""

# -*- coding: utf-8 -*-
"""
Created on Wed May 22 17:07:35 2024

@author: PierreA.DERRIEN
"""

from Functionnal_Imaging_Electrophysiology.General_tools import PySimpleGUI as sg

from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.NWB_to_Dataframe import NWB_to_Dataframe_converter
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.preprocessing_tools.photobleaching_corrector import isobestic_correction , photobleaching_correction
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.artefact_detectors import windowed_gradient_detector
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.preprocessing_tools.fiber_filters import savgol_filtering
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.custom_Zscore import zscore
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.trials_extraction import extract_trials
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.Add_events_tables import generate_event_table , generate_param_table

from pynwb import ProcessingModule, NWBHDF5IO
import traceback
import os
import gc
import time
import seaborn as sb
from matplotlib import pyplot as plt
from yaspin import yaspin
import matplotlib
import matplotlib.patches as patches
import numpy as np
from scipy import stats
import pandas as pd
import json 


import sys
import pathlib
script_directory = pathlib.Path(__file__).parent.resolve()
file_path = str(script_directory).split("\\")[:-3]
file_path[0] = file_path[0]+r"\\"
module_folder = os.path.join(*file_path)
sys.path.append(module_folder)

matplotlib.use('QtAgg')
plt.close("All")

sb.set_palette(palette='tab20c')
# =============================================================================
# Init param
# =============================================================================
detect_artifact = True
display_artifacts = True
gradient_window = 0.1 # 1 = 100% of the data / 0 = 0% of the data

TTL = []
raw_signals = []
demod_signals = []
isobestic_signals = {}
data = None
with open("Setting.json", "r") as f:
    Setting = json.load(f)
# =============================================================================
# 
# =============================================================================
sg.theme("DarkBlue")
layout = [
          [sg.T("")], [sg.Text("Choose a file: "), sg.Input(), sg.FileBrowse(key="-IN-",file_types=[("NWB Files","*.nwb")], enable_events=True), sg.Button("Load file")],
          [sg.T("Demodulated signals", pad= (5,3)),sg.T("", pad = (45,3)), sg.T("TTL signals",pad= (85,3))],
          [sg.Listbox(values=[], enable_events=True, size=(40, 10), key="-Signals List-", select_mode = "extended"),sg.Listbox(values=[], enable_events=True, size=(40, 10), key="-TTL List-") ],
          [sg.Button("Plot signal"), sg.Button("Artifact detection"), sg.Button("Analyser")]
          ]

def popup(File_name):
      layout = [
          [sg.Text("Filename"),sg.Input(File_name,key='filename'),sg.Combo(values = [".csv", ".xlsx"], key="file_extension")],
          [sg.Button("Save data")]
          ]
      window = sg.Window('POPUP', layout,  finalize=True)
      while True:
          event , values = window.Read()
          if event == "Save data": 
              if values["filename"]!= "" and values["file_extension"]!= "":
                  print(values["filename"]+values["file_extension"])
                  window.close()
                  return values["filename"]+values["file_extension"], values["file_extension"]
          
          elif event == sg.WIN_CLOSED or event=="Exit":
              break
      window.close()


def open_window(demod_signals, TTL, data_analyser, TTL_data,metadata, name, Setting =None):
    
    # analyser window
    status_pb = [('\u2B24'+' Photobleaching', 'red'), ('\u2B24'+' Photobleaching', 'green')]
    state_pb = 0
    status_zscore = [('\u2B24'+' Zscore', 'red'), ('\u2B24'+' Zscore on whole data', 'green'), ('\u2B24'+' Zscore on trial data', 'green')]
    state_zscore = 0
    status_filtering = [('\u2B24'+' Filtering', 'red'), ('\u2B24'+' Filter: All signal', 'green'), ('\u2B24'+' Filter: Trial', 'green'), ('\u2B24'+' Filter: Signal & Trial', 'green')]
    state_filtering = 0
    status_trials = [('\u2B24'+' Trials extraction', 'red'), ('\u2B24'+' Trials extraction', 'green')]
    state_trials = 0
    
    col_2_size = 25
    
    layoutAnalyser = [[sg.Column([[sg.Button("Back to Browser", size=(15,1))],[sg.Button("Save Parameter", size=(15,1))]]), sg.Text("{0}".format(name.split("/")[-1]), key="new", font=("Helvetica", 20), text_color="white")], 
                      [sg.HorizontalSeparator(color='grey')],
             # First columns
              [sg.Column(
              [[sg.Text("Signals analysable", key="new"),sg.Combo(values=[x.replace("demodulated_signal ", "") for x in demod_signals], key="-Signals List Analyzer-", size = (46,1))],
              [sg.Text("Photobleaching correction", key="new2"),sg.Combo(values=["Isobestic", "Regression"], key="-Photobleaching mode-", size = (40,1)),],
              [sg.Text("Zscore", key="new3"),sg.Radio("All", "zscore mode", key='All Zscore', default=True),sg.Radio("On trial", "zscore mode", key='Zscore on trial'),sg.Radio("None", "zscore mode", key='no zscore')], 
                  [sg.Text(" "),sg.Text("timezone (in sec) : inf:", key="new5"), sg.Input('', enable_events=True, key='-Zscore inf lim-', justification='left', size =(5,1)), 
                  sg.Text("sup:", key="new6"),sg.Input('', enable_events=True, key='-Zscore up lim-', justification='left',size =(5,1))
              ],
              [sg.Text("Filtering", key="new10")],
                [sg.Text("   ", key="new10"),sg.Checkbox( "Before photobleaching", key='PB_filter'),
                    sg.Text("window (ms):"),sg.Input("50",enable_events=True, key='-All filter window-', justification='left', size =(5,1)),
                    sg.Text("degree:"),sg.Input("3",enable_events=True, key='-All filter degree-', justification='left', size =(5,1))],
                [sg.Text("   ", key="new10"), sg.Checkbox("Before trials           ", key='Trial_filter') ,
                    sg.Text("    window (ms):"),sg.Input("50",enable_events=True, key='-trial filter window-', justification='left', size =(5,1)),
                    sg.Text("degree:"),sg.Input("3",enable_events=True, key='-trial filter degree-', justification='left', size =(5,1))],
              [sg.T("")],
              [sg.Text("Trials extraction on ", key="new4"),sg.Combo(values=TTL, key="-Event triggers-", size = (45,1)),],
                  [sg.Text(" "),sg.Text("timezone (in sec) : before:", key="new7"), sg.Input('', enable_events=True, key='-event inf lim-', justification='left', size =(5,1)), 
                  sg.Text("after:", key="new8"),sg.Input('', enable_events=True, key='-event up lim-', justification='left',size =(5,1)),],
                  [sg.Text(" "),sg.Checkbox("Is pulse train:",key='Pulse_train'),sg.Input('', enable_events=True, key='-n_pulse-', justification='left',size =(5,1)), sg.Text("pulses", key="new50"),],
              [sg.T("")],
              [sg.Button("Analyse",size = (57,1))], 
              [sg.Button("Save Corrected Data",size = (27,1)), sg.Button("Save Trials Extracted",size = (28,1))],
              [sg.Text("Decimation: ", key="NEW",font=("Courier New", 10), justification='middle center'),sg.Input('1', enable_events=True, key='Saving decimation corrected data', justification='left',size =(10,2)), sg.Text("     Decimation: ", key="NEW",font=("Courier New", 10), justification='middle center'),sg.Input('1', enable_events=True, key='Saving decimation trial data', justification='left',size =(10,2))]
              ]
              ,element_justification='left'),
              
              #Second columns
              sg.Column([
                  [sg.Text(text=status_pb[state_pb][0], text_color=status_pb[state_pb][1], size=(col_2_size, 1),
                        justification='left', font=("Courier New", 10), key='INDICATOR_pb')],
                  [sg.Text(text=status_zscore[state_zscore][0], text_color=status_zscore[state_zscore][1], size=(col_2_size, 1),
                        justification='left', font=("Courier New", 10), key='INDICATOR_zscore')],
                  [sg.Text(text=status_filtering[state_filtering][0], text_color=status_filtering[state_filtering][1], size=(col_2_size, 1),
                        justification='left', font=("Courier New", 10), key='INDICATOR_filtering')],
                  [sg.Text(text=status_trials[state_trials][0], text_color=status_trials[state_trials][1], size=(col_2_size, 1),
                      justification='left', font=("Courier New", 10), key='INDICATOR_trials')],
                  
                  [sg.Text("Plot:", key="new9")], 
                  [sg.Button("Zscore", size= (col_2_size,1))], 
                  [sg.Button("Filter signals", size= (col_2_size,1))],
                  [sg.Button("Photobleaching", size= (col_2_size,1))],
                  [sg.Button("Show Events", size= (col_2_size,1))], 
                  [sg.Button("Trials", size= (col_2_size,1))],
                  [sg.Button("Trials Mean", size= (col_2_size,1))],
                  [sg.Text("Decimation: ", key="NEW",font=("Courier New", 10), justification='middle center'),sg.Input('1000', enable_events=True, key='-decim_fact-', justification='left',size =(10,2))],
                  
                  ],element_justification='left'),
             
                
              ]]

    
    Analyser_window = sg.Window("Analyser Window", layoutAnalyser, modal=True, 
                                finalize=True
                                )
    data_analyser = data_analyser.dropna()
    # Can add auto find if regression or isobestic signal here
    Analyser_window.find_element("-Photobleaching mode-").update(value="Regression")
    # Default signals as the first in list
    Analyser_window.find_element("-Signals List Analyzer-").update(value=[x.replace("demodulated_signal ", "") for x in demod_signals][0])
    for key , value in Setting.items():
        if key == "-Event triggers-":
            if any(value in x for x in TTL):
                Analyser_window.find_element(key).update(value=value)
        else:
            Analyser_window.find_element(key).update(value=value)
    return Analyser_window
    

status_pb = [('\u2B24'+' Photobleaching', 'red'), ('\u2B24'+' Photobleaching', 'green')]
state_pb = 0
status_zscore = [('\u2B24'+' Zscore', 'red'), ('\u2B24'+' Zscore on whole data', 'green'), ('\u2B24'+' Zscore on trial data', 'green')]
state_zscore = 0
status_filtering = [('\u2B24'+' Filtering', 'red'), ('\u2B24'+' Filter: All signal', 'green'), ('\u2B24'+' Filter: Trial', 'green'), ('\u2B24'+' Filter: Signal & Trial', 'green')]
state_filtering = 0
status_trials = [('\u2B24'+' Trials extraction', 'red'), ('\u2B24'+' Trials extraction', 'green')]
state_trials = 0

list_status=[status_pb, status_zscore,status_filtering,status_trials ]
list_state=[state_pb, state_zscore,state_filtering,state_trials ]
list_indicator=['INDICATOR_pb','INDICATOR_zscore','INDICATOR_filtering','INDICATOR_trials']

###Building Window
window = sg.Window('My File Browser', layout, size=(600,300),finalize=True)
selection = None
file_loaded =False
choice = None
Zscore_all_done = False
Trials_extraction_done = False
analyser_active = False
analyse_done = False
loaded_file = ""
while True:
    win, event, values = sg.read_all_windows()
    if win == window:
        try:
                if event == "Load file":
                  if values["-IN-"] != "":
                    
                    data_folder =""
                    i =0
                    for fold in values['-IN-'].split("/")[:-1]:
                        if i ==0:
                            data_folder = fold+"/"
                        else:
                            data_folder = os.path.join(data_folder, fold)
                        i+=1
                    name = values['-IN-'].split("/")[-1].split(".")[0]
                    print(values["-IN-"])
                    NWB_converter = NWB_to_Dataframe_converter(values["-IN-"])
                    NWB_converter.load_NWB()
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
                        elif "isobestic"in signal or "Isobestic" in signal :
                            signal_name = signal.replace("isobestic ","")
                            signal_name = signal_name.replace("Isobestic ","")
                            signal_name = signal_name.replace("demodulated_iso ","")
                            isobestic_signals[signal_name] = signal
                    list_signals = []
                    for signal in [x.replace("demodulated_signal ", "") for x in demod_signals]:
                        if signal in isobestic_signals.keys():
                            list_signals.append(signal+ ' - w/ISO')
                        else: 
                            list_signals.append(signal)
                    window["-Signals List-"].update(list_signals)
                    window["-TTL List-"].update(TTL)
                    data = NWB_converter.get_data()[demod_signals +[i for i in isobestic_signals.values()]]
                    TTL_data = NWB_converter.get_data()[[x+" | TTL signals" for x in TTL]]
                    file_loaded = True
                    loaded_file = values["-IN-"]
                   
                elif event == "-Signals List-":
                    selection = values[event]
                elif event == "Artifact detection":
                    start_time = time.time()
                    artifacts= windowed_gradient_detector(data, int(len(data)*gradient_window), display = display_artifacts)
                    print("Artifact detection time: %s seconds" % (time.time() - start_time))
                elif event == "Plot signal":
                    data = NWB_converter.get_data().dropna()    
                    # amelio : check to get last figure position and create it as the same place if possible
                    plt.close("Plot unprocessed demodulated signals")
                    plt.figure("Plot unprocessed demodulated signals")
                    if selection != None:
                        if len(selection)==1:
                            sb.lineplot(x= data.index[::1000], y= data["demodulated_signal "+selection[0].replace(" - w/ISO","")][::1000], label = "demodulated_signal "+selection[0])
                        else:
                            for signal in selection:
                                sb.lineplot(x= data.index[::1000], y= data["demodulated_signal "+signal.replace(" - w/ISO","")][::1000], label = "demodulated_signal "+signal) 
                    else:
                        for signal in demod_signals:
                           sb.lineplot(x= data.index[::1000], y= data[signal.replace(" - w/ISO","")][::1000], label = signal)      
                elif event == "Analyser":
                    if values["-IN-"]!= "":
                        if loaded_file != values["-IN-"]:
                            file_loaded = False
                        if file_loaded == False:
                            data_folder =""
                            i =0
                            for fold in values['-IN-'].split("/")[:-1]:
                                if i ==0:
                                    data_folder = fold+"/"
                                else:
                                    data_folder = os.path.join(data_folder, fold)
                                i+=1
                            name = values['-IN-'].split("/")[-1].split(".")[0]
                            NWB_converter = NWB_to_Dataframe_converter(values["-IN-"])
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
                                elif "isobestic"in signal or "Isobestic" in signal :
                                    signal_name = signal.replace("isobestic ","")
                                    signal_name = signal_name.replace("Isobestic ","")
                                    signal_name = signal_name.replace("demodulated_iso ","")
                                    isobestic_signals[signal_name] = signal
                            list_signals = []
                            for signal in [x.replace("demodulated_signal ", "") for x in demod_signals]:
                                if signal in isobestic_signals.keys():
                                    list_signals.append(signal+ ' - w/ISO')
                                else: 
                                    list_signals.append(signal)
                            window["-Signals List-"].update(list_signals)
                            window["-TTL List-"].update(TTL)
                            data = NWB_converter.get_data()[demod_signals +[i for i in isobestic_signals.values()]]
                            TTL_data = NWB_converter.get_data()[[x+" | TTL signals" for x in TTL]]
                            file_loaded = True
                            loaded_file = values["-IN-"]
                        data_analyser = data.dropna()  
                        metadata = NWB_converter.get_metaData()
                        Analyser_window= open_window(demod_signals, TTL, data, TTL_data, NWB_converter.get_metaData(), name=values["-IN-"], Setting=Setting)
                        analyser_active = True
                        window.Hide()
                    else:
                        sg.popup_auto_close("Select a file to analyse", title="No file to analyse", auto_close_duration = 3)
                elif event == sg.WIN_CLOSED or event=="Exit":
                    window.close()
                    if analyser_active:
                        Analyser_window.close()
                    break
                        
        except Exception as e:
            sg.popup("Error encountered in File Browser: {0}".format(e), title = "Error in File Browser")
            plt.close("All")
            traceback.print_exc()
    if analyser_active:
        if win == Analyser_window:
            try:
                    # Handling text input are numbers
                    if event == '-Zscore inf lim-' or event == '-Zscore up lim-' or event == '-event inf lim-' or event == '-event up lim-':
                     if len(values[event])>=1:
                        if values[event][-1] not in ('0123456789.-'):
                            sg.popup("Only digits allowed")
                            Analyser_window[event].update(values[event][:-1])
                    
                    
                    
                    elif event == '-n_pulse-' or event == '-trial filter degree-' or event == '-trial filter Analyser_window-' or event == '-All filter degree-' or  event == '-All filter Analyser_window-' or event == '-decim_fact-' or event =="Saving decimation corrected data":
                     if len(values[event])>=1:
                         if values[event][-1] not in ('0123456789'):
                            sg.popup(event)
                            Analyser_window[event].update(values[event][:-1])
                    
                    elif event == "Exit" or event == sg.WIN_CLOSED or event == "Back to Browser":
                        analyser_active = False
                        window.UnHide()
                        Analyser_window.close()
            # =============================================================================
            #         Analysis
            # =============================================================================
                    elif event == "Analyse":
                        state_filtering = 0
                        analyse_done = False
                        sampling_fz = metadata["Imaging_planes"]['imaging_plane for '+values["-Signals List Analyzer-"].split(" ")[1]]['imaging_rate']
                        filtered_demod =""
                        if type(data_analyser) == type(None): 
                            sg.popup("No file loaded - closing the Analyser")
                            break
                    #  Savgol filter before photobleaching correction
                        if values['PB_filter']:
                            print("Savgol filter before photobleaching correction")
                            filter_Analyser_window = int((0.001*int(values['-All filter window-']))*sampling_fz)
                            print("Start filtering with a Analyser_window of {0} points - that represent {1} seconds".format(filter_Analyser_window, data_analyser.index[int(filter_Analyser_window)] - data_analyser.index[0]))
                            start_time = time.time()
                            with yaspin(text = "Filtering") as spinner:
                                filtered_signals = savgol_filtering(data_analyser, Fiber = "demodulated_signal "+values["-Signals List Analyzer-"], win_size = filter_Analyser_window, polyorder=int(values['-All filter degree-']))
                                spinner.write(" Filtering time: {0} min {1} seconds".format(((time.time() - start_time)//60) , (time.time() - start_time)%60))
                                filtered_demod = "_filter"
                            data_analyser = filtered_signals
                            state_filtering += 1
                    # Photobleaching correction handling
                        state_pb = 0
                        if values["-Photobleaching mode-"] == "Regression":
                            print("Using Regression correction on {0}".format(signal))
                            ChForFit, pb_data , Fit = photobleaching_correction(data_analyser, Fiber = "demodulated_signal "+values["-Signals List Analyzer-"]+filtered_demod)
                            text_pb = "Photobleaching correction done by Regression"
                            state_pb = 1
                        elif values["-Photobleaching mode-"] == "Isobestic":
                            print("Using Isobestic correction on {0}".format(signal))
                            pb_data = isobestic_correction(data_analyser, "demodulated_signal "+values["-Signals List Analyzer-"]+filtered_demod, isobestic_signals[values["-Signals List Analyzer-"]], return_prediction= True)
                            text_pb = "Photobleaching correction done by Isobestic correction"
                            state_pb = 1
                        else:
                            pb_data = data_analyser
                            pb_data["df_over_f"]= data_analyser["demodulated_signal "+values["-Signals List Analyzer-"]+filtered_demod]
                            text_pb  = "No Photobleaching correction"
                        sg.popup_notify(text_pb, title="Photobleaching done", display_duration_in_ms = 3000, fade_in_duration= 500)
                    # Zscore handling
                        Zscore_all_done = False
                        state_zscore = 0
                        if values['All Zscore']:
                            print("Zscore done on whole data")
                            if "-" in values['-Zscore inf lim-'] :
                               values['-Zscore inf lim-'] = sg.popup_get_text('You define a negative timing on inf limit to do the Zscore on whole data')
                               Analyser_window.find_element('-Zscore inf lim-').update(value=values['-Zscore inf lim-'])
                            if "-" in values['-Zscore up lim-']:
                               values['-Zscore up lim-'] = sg.popup_get_text('You define a negative timing on up limit to do the Zscore on whole data')
                               Analyser_window.find_element('-Zscore up lim-').update(value=values['-Zscore up lim-'])
                            if float(values['-Zscore up lim-']) <= float(values['-Zscore inf lim-']):
                                up = values['-Zscore inf lim-']
                                values['-Zscore inf lim-'] = values['-Zscore up lim-']
                                values['-Zscore up lim-'] = up
                                Analyser_window.find_element('-Zscore inf lim-').update(value=values['-Zscore inf lim-'])
                                Analyser_window.find_element('-Zscore up lim-').update(value=values['-Zscore up lim-'])
                                sg.popup_notify("limit inf is after limit up, inverting the two point to create a valide timerange", title="Inverting the timezone for Zscore", display_duration_in_ms = 3000, fade_in_duration= 500)
                            # No time zone set
                            if values['-Zscore inf lim-'] == "" and values['-Zscore up lim-'] == "":
                                zscore_data = stats.zscore(pb_data["df_over_f"])
                                Zscore_all_done = True
                                sg.popup_notify("Zscore done on all data", title="Zscore done on all data", display_duration_in_ms = 3000, fade_in_duration= 500)
                                state_zscore = 1
                            # Only start point set
                            elif values['-Zscore inf lim-'] != "" and values['-Zscore up lim-'] == "":
                                zscore_data , mean , std = zscore(pb_data.reset_index(),"df_over_f", "Timestamps",window = [float(values['-Zscore inf lim-']),pb_data.index.tolist()[-1]])
                                zscore_data.set_index("Timestamps", inplace = True)
                                zscore_data = zscore_data["Zscore"]
                                Zscore_all_done = True
                                sg.popup_notify("Zscore done on all data between {0} and {1}".format(float(values['-Zscore inf lim-']),pb_data.index.tolist()[-1]), title="Zscore done on all data", display_duration_in_ms = 3000, fade_in_duration= 500)
                                state_zscore = 1
                                pass
                            # Only end point set
                            elif values['-Zscore inf lim-'] == "" and values['-Zscore up lim-'] != "":
                                zscore_data , mean , std = zscore(pb_data.reset_index(),"df_over_f", "Timestamps", window = [pb_data.index.tolist()[0],float(values['-Zscore up lim-'])])
                                zscore_data.set_index("Timestamps", inplace = True)
                                zscore_data = zscore_data["Zscore"]
                                Zscore_all_done = True
                                sg.popup_notify("Zscore done on all data between {0} and {1}".format(pb_data.index.tolist()[0],float(values['-Zscore up lim-'])), title="Zscore done on all data", display_duration_in_ms = 3000, fade_in_duration= 500)
                                state_zscore = 1
                                pass
                            # Both start and end point set
                            elif values['-Zscore inf lim-'] != "" and values['-Zscore up lim-'] != "":
                                zscore_data , mean , std = zscore(pb_data.reset_index(),"df_over_f", "Timestamps", window = [float(values['-Zscore inf lim-']),float(values['-Zscore up lim-'])])
                                zscore_data.set_index("Timestamps", inplace = True)
                                zscore_data = zscore_data["Zscore"]
                                Zscore_all_done = True
                                sg.popup_notify("Zscore done on all data between {0} and {1}".format(float(values['-Zscore inf lim-']),float(values['-Zscore up lim-'])), title="Zscore done on all data", display_duration_in_ms = 3000, fade_in_duration= 500)
                                state_zscore = 1
                                pass
                    
                    
                    
                       
                    # Trials extraction 
                        Trials_extraction_done = False
                        state_trials = 0
                        if values["-Event triggers-"]!="":
                            print("Extracting trials based on {0}".format(values["-Event triggers-"]))
                            Events = TTL_data[values["-Event triggers-"]+" | TTL signals"].diff()
                            # Need gui to define how to analyse the TTL 
                            if values["Pulse_train"]:
                                if values['-n_pulse-']=="":
                                    values['-n_pulse-']= sg.popup_get_text('You forget to define the pulse number in our pulse train')
                                start_id  = Events[Events==1].index.tolist()[::int(values['-n_pulse-'])]
                                end_id  = Events[Events==-1].index.tolist()[int(values['-n_pulse-'])-1:][::int(values['-n_pulse-'])]
                                Events.values[:] = 0
                                for s,e in zip(start_id,end_id):
                                    Events[s]=1
                                    Events[e]=-1
                            Trials_data = pd.DataFrame()
                            if Zscore_all_done:
                                Trials_data["Zscore"] = zscore_data
                                analysed_col = "Zscore"
                            else:
                                Trials_data["df_over_f"] = pb_data["df_over_f"]
                                analysed_col = "df_over_f"
                                
                            if values["-event inf lim-"] !="" and values["-event up lim-"]!="":
                                Trials_data["Events"]= Events[(Events.index >= min(Trials_data.index))&(Events.index <= max(Trials_data.index))]
                                Trials_extracted = extract_trials(data= Trials_data, signal_channel=analysed_col, trigger_channel="Events", winsize = [int(values["-event inf lim-"]), int(values["-event up lim-"])],compact = True, compact_value= 3)
                                Trials_extraction_done = True
                                sg.popup_notify("Trials extraction done", title="Trial extraction done", display_duration_in_ms = 3000, fade_in_duration= 500)
                                if values['Trial_filter']:
                                    print("Savgol filter before trial extraction")
                                    Filtered_trials = pd.DataFrame()
                                    filter_Analyser_window = int((0.001*int(values['-trial filter window-']))*sampling_fz)
                                    for col in Trials_extracted.columns:
                                        Filtered_trials[col] = savgol_filtering(Trials_extracted, Fiber = col, win_size = filter_Analyser_window, polyorder=int(values['-trial filter degree-']))[col+"_filter"]
                                    state_filtering += 2  
                                
                                if values['Zscore on trial']:
                                    if values['Trial_filter']:
                                        data_to_use = Filtered_trials
                                    else:
                                        data_to_use = Trials_extracted
                                    Zscore_trials = pd.DataFrame()
                                    for col in data_to_use.columns:
                                        # Make zscore on trials here
                                        if values['-Zscore inf lim-'] == "" and values['-Zscore up lim-'] == "":
                                            zscore_trial = stats.zscore(data_to_use[col])
            
                                        # Only start point set
                                        elif values['-Zscore inf lim-'] != "" and values['-Zscore up lim-'] == "":
                                            zscore_trial , mean , std = zscore(data_to_use.reset_index(),col, data_to_use.index.name, window = [float(values['-Zscore inf lim-']),data_to_use.index.tolist()[-1]])
                                            zscore_trial.set_index(data_to_use.index.name, inplace = True)
                                            zscore_trial = zscore_trial["Zscore"]
                                            
                                        # Only end point set
                                        elif values['-Zscore inf lim-'] == "" and values['-Zscore up lim-'] != "":
                                            zscore_trial , mean , std = zscore(data_to_use.reset_index(),col, data_to_use.index.name,window = [data_to_use.index.tolist()[0],float(values['-Zscore up lim-'])])
                                            zscore_trial.set_index(data_to_use.index.name, inplace = True)
                                            zscore_trial = zscore_trial["Zscore"]
                                           
                                        # Both start and end point set
                                        elif values['-Zscore inf lim-'] != "" and values['-Zscore up lim-'] != "":
                                            zscore_trial , mean , std = zscore(data_to_use.reset_index(),col, data_to_use.index.name, window = [float(values['-Zscore inf lim-']),float(values['-Zscore up lim-'])])
                                            zscore_trial.set_index(data_to_use.index.name, inplace = True)
                                            zscore_trial = zscore_trial["Zscore"]
                                            
                                        Zscore_trials[col] = zscore_trial
                                    state_zscore = 2
                                    sg.popup_notify("Zscore done on all trial between {0} and {1}".format(float(values['-Zscore inf lim-']),float(values['-Zscore up lim-'])), title="Zscore done on all data", display_duration_in_ms = 5000, fade_in_duration= 1000)
                                state_trials = 1
                                Trials_extraction_done = True
                            else: 
                                sg.popup_auto_close("Set up inferior and superior limit of the Analyser_window", title="Trial Analyser_window not set", auto_close_duration = 3)
                        analyse_done = True
                        sg.popup_notify("End Analysis", display_duration_in_ms = 3000, fade_in_duration= 500)
                        
                    #_______________________________________________________________________
                    # Display of signals parts
                    elif event == "Filter signals":
                        if values['PB_filter']:
                            fig = plt.figure("Display Filtering of {0}".format(values["-Signals List Analyzer-"]))
                            plt.suptitle("Display Filtering of {0}".format(values["-Signals List Analyzer-"]))
                            sb.lineplot(x=filtered_signals.index[::int(values['-decim_fact-'])], y=filtered_signals["demodulated_signal "+values["-Signals List Analyzer-"]][::int(values['-decim_fact-'])], label='Signal')
                            sb.lineplot(x=filtered_signals.index[::int(values['-decim_fact-'])], y=filtered_signals["demodulated_signal "+values["-Signals List Analyzer-"]+"_filter"][::int(values['-decim_fact-'])], label='Filtered signal')
                        elif values['Trial_filter']:
                            n_plot = len(Filtered_trials.columns)
                            fig, ax = plt.subplots(int(np.ceil(n_plot/2)), 2, sharey= True, sharex= True)
                            for i in range(n_plot):
                                col = Filtered_trials.columns[i]
                                r = i//2
                                c = i%2
                                sb.lineplot(data = Trials_extracted, x= Trials_extracted.index, y= col, ax = ax[r,c], color = sb.color_palette('tab20c')[i])
                                sb.lineplot(data = Filtered_trials, x= Filtered_trials.index, y= col, ax = ax[r,c], color = sb.color_palette('tab20c')[i], linestyle = "--")
                                # display zscore zone a faire
                        else:
                            sg.popup("No Filtering on whole data", title= "Filtering")
                    elif event == "Photobleaching":
                        if values["-Photobleaching mode-"] == "Regression":
                            fig = plt.figure("Display Filtering of {0}".format(values["-Signals List Analyzer-"]), figsize = (15,10))
                            plt.gca().title.set_text("Display photobleaching of {0}".format(values["-Signals List Analyzer-"]))
                            ax = plt.gca()
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y=pb_data["demodulated_signal "+values["-Signals List Analyzer-"]][::int(values['-decim_fact-'])], label='Signal',legend = False,)
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y = Fit[1][::int(values['-decim_fact-'])], label = "Fit: {0}".format(Fit[0]),legend = False)
                            ax2 = plt.twinx()
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y=pb_data["df_over_f"][::int(values['-decim_fact-'])], label='df/f', color = "orange", legend= False, ax=ax2)
                            lines_labels = [ax.get_legend_handles_labels() for ax in fig.axes]
                            lines, labels = [sum(lol, []) for lol in zip(*lines_labels)]
                            ax.legend(lines, labels, loc = 'upper right')
                        else: 
                            fig = plt.figure("Display Filtering of {0}".format(values["-Signals List Analyzer-"]), figsize = (15,10))
                            plt.gca().title.set_text("Display photobleaching of {0}".format(values["-Signals List Analyzer-"]))
                            ax = plt.gca()
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y=pb_data["demodulated_signal "+values["-Signals List Analyzer-"]][::int(values['-decim_fact-'])], label='Signal',legend = False,)
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y=pb_data["Predicted value"][::int(values['-decim_fact-'])], label='Predicted',legend = False, alpha = 0.75, color="yellow")
                            ax2 = plt.twinx()
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y=pb_data["df_over_f"][::int(values['-decim_fact-'])], label='df/f', color = "orange", legend= False, ax=ax2)
                            lines_labels = [ax.get_legend_handles_labels() for ax in fig.axes]
                            lines, labels = [sum(lol, []) for lol in zip(*lines_labels)]
                            ax.legend(lines, labels, loc = 'upper right')
                    elif event == "Zscore":
                        if Zscore_all_done:
                            fig = plt.figure("Display Zscore of {0}".format(values["-Signals List Analyzer-"]), figsize = (15,10))
                            ax = plt.gca()
                            plt.gca().title.set_text("Display Zscore of {0}".format(values["-Signals List Analyzer-"]))
                            sb.lineplot(x=pb_data.index[::int(values['-decim_fact-'])], y=pb_data["df_over_f"][::int(values['-decim_fact-'])], label='df/f', color = "blue", legend= False)
                            ax.axhline(0, linestyle ="--", color = "dodgerblue")
                            ax2 = plt.twinx()
                            ax2.set_ylabel('Zscore')
                            sb.lineplot(x=zscore_data.index[::int(values['-decim_fact-'])], y=zscore_data[::int(values['-decim_fact-'])], label='Zscore', linestyle= "--", color = "orange", ax=ax2, legend= False)
                            ax2.axhline(0, linestyle ="--", color = "gold")
                            lines_labels = [ax.get_legend_handles_labels() for ax in fig.axes]
                            lines, labels = [sum(lol, []) for lol in zip(*lines_labels)]
                            ax.legend(lines, labels, loc = 'upper right')
                            ymin, ymax = ax2.get_ylim()
                            if values['-Zscore inf lim-'] != "" and values['-Zscore up lim-'] == "":
                                
                                rect = patches.Rectangle((float(values['-Zscore inf lim-']), ymin), pb_data.index.tolist()[-1]-float(values['-Zscore inf lim-']), ymax, linewidth=1, edgecolor='r', facecolor='pink')
                                ax2.add_patch(rect)
                            elif values['-Zscore inf lim-'] == "" and values['-Zscore up lim-'] != "":
                                rect = patches.Rectangle((pb_data.index.tolist()[0], ymin), float(values['-Zscore up lim-'])-pb_data.index.tolist()[0], ymax, linewidth=1, edgecolor='r', facecolor='pink')
                                ax2.add_patch(rect)
                            elif values['-Zscore inf lim-'] != "" and values['-Zscore up lim-'] != "":
                                rect = patches.Rectangle((float(values['-Zscore inf lim-']), ymin), float(values['-Zscore up lim-'])-float(values['-Zscore inf lim-']), ymax, linewidth=1, edgecolor='r', facecolor='pink')
                                ax2.add_patch(rect)
                        elif values['Trial_filter']:
                            n_plot = len(Zscore_trials.columns)
                            fig, ax = plt.subplots(n_plot//2, 2, sharey= True, sharex= True)
                            for i in range(n_plot):
                                col = Filtered_trials.columns[i]
                                r = i//2
                                c = i%2
                                sb.lineplot(data = Trials_extracted, x= Trials_extracted.index, y= col, ax = ax[r,c], color = sb.color_palette('tab20c')[i])
                                ax2 = ax[r,c].twinx()
                                sb.lineplot(data =  Zscore_trials, x=  Zscore_trials.index, y= col, ax = ax2, color = sb.color_palette('tab20c')[i], linestyle = "--")
                        
                        
                        else:
                            sg.popup_auto_close("Zscore not don \n reanalyse the data with the option activated to display", title="Zscore not done on all data", auto_close_duration = 3)
                  
                    elif event == "Show Events":
                        if Zscore_all_done:
                            data_to_disp = pd.DataFrame()
                            data_to_disp["Zscore"] =zscore_data
                            data_type = "Zscore"
                            label_text ="Zscore"
                        else:
                            data_to_disp =pb_data
                            data_type = "df_over_f"
                            label_text = "df/f"
                        fig = plt.figure("Display Events of {0} on {1}".format(values["-Event triggers-"], label_text), figsize = (15,10))
                        ax = plt.gca()
                        plt.gca().title.set_text("Display Events of {0} on {1}".format(values["-Event triggers-"], label_text))
                        sb.lineplot(x=data_to_disp.index[::int(values['-decim_fact-'])], y=data_to_disp[data_type][::int(values['-decim_fact-'])],legend= False)
                        
                        lines_labels = [ax.get_legend_handles_labels() for ax in fig.axes]
                        lines, labels = [sum(lol, []) for lol in zip(*lines_labels)]
                        ax.legend(lines, labels, loc = 'upper right')            
                        ymin, ymax = ax.get_ylim()
                        # sb.lineplot(x=TTL_data.index[::int(values['-decim_fact-'])], y=Events[::int(values['-decim_fact-'])])
                        for ttl_point in Events[Events == 1].index:
                            plt.axvline(x= ttl_point, color = "red", linestyle = "--")
                        for ttl_point in Events[Events == -1].index:
                            plt.axvline(x= ttl_point, color = "pink", linestyle = "--")
                        for start,end in zip(Events[Events == 1].index, Events[Events == -1].index):
                            rect = patches.Rectangle((start, ymin), end-start, ymax, linewidth=1, edgecolor='r', facecolor='pink')
                            plt.gca().add_patch(rect)
                            
                    elif event == "Trials":
                        if Trials_extraction_done:
                            if Zscore_all_done:
                                data_type = "Zscore"
                            elif values['Zscore on trial']:
                                data_type = "Zscore"
                            else:
                                data_type = "df/f"
                            if values['Zscore on trial']:
                                 data_to_disp = Zscore_trials
                            elif values['Trial_filter']:
                                data_to_disp = Filtered_trials
                            else:
                                data_to_disp = Trials_extracted
                            
                            plt.figure("Display Trials of {0} on {1}".format(values["-Event triggers-"], data_type), figsize = (15,10))
                            plt.gca().title.set_text("Display Trials of {0} on {1}".format(values["-Event triggers-"], data_type))
                            plt.axvline(x= 0, color = "red", linestyle = "--")
                            for col in data_to_disp.columns:
                                sb.lineplot(data= data_to_disp, x=data_to_disp.index, y= col, label= col)
                            plt.ylabel(data_type)
                    elif event == "Trials Mean":
                        if Trials_extraction_done:
                            if Zscore_all_done:
                                data_type = "Zscore"
                            elif state_zscore == 2:
                                data_type = "Zscore"
                            else:
                                data_type = "df/f"
                            if  state_zscore == 2:
                                 data_to_disp = Zscore_trials
                            elif state_filtering >= 2:
                                data_to_disp = Filtered_trials
                            else:
                                data_to_disp = Trials_extracted
                            
                            plt.figure("Display Trials of {0} on {1}".format(values["-Event triggers-"], data_type), figsize = (15,10))
                            plt.gca().title.set_text("Display Trials of {0} on {1}".format(values["-Event triggers-"], data_type))        
                            mean= data_to_disp.mean(axis = 1)
                            sem = data_to_disp.sem(axis = 1)
                            sb.lineplot(x = mean.index, y= mean, color = "red", linestyle = "--")
                            plt.fill_between(sem.index, y1= mean+sem, y2 =  mean-sem, alpha = 0.5)
                            plt.axvline(x= 0, color = "red", linestyle = "--")
                            plt.ylabel(data_type)
                            
# =============================================================================
#Saving the data
# =============================================================================
                    elif event == "Save Corrected Data":
                        if analyse_done:
                          response = sg.popup_yes_no("Saving the corrected signal to the NWB ?")
                          if response == "Yes":
                              
                              pass
                              
                          elif response == "No":
                              
                              # popup_animated(
                              start_name = name+"_Signals-"+values["-Signals List Analyzer-"]
                              
                              save_filename , extension = popup(start_name)
                              analysed_data_folder = os.path.join(data_folder, "Analysed_data")
                              popup_size = (50, 5)
                              if os.path.exists(analysed_data_folder):
                                  sg.popup_notify("Analysed data folder already in : \n {0}".format(data_folder))
                              else:
                                  sg.popup_notify("Creating a analysed data folder in : \n {0}".format(data_folder))
                                  os.makedirs(analysed_data_folder)
                              corrected_data_folder = os.path.join(analysed_data_folder, "Corrected_data")
                              if os.path.exists(corrected_data_folder):
                                  sg.popup_notify("Corrected data folder already in : \n {0}".format(analysed_data_folder))
                              else:
                                  sg.popup_notify("Creating a corrected data folder in : \n {0}".format(analysed_data_folder))
                                  os.makedirs(corrected_data_folder)
                                  
                              # Need to define how to automatically reformat the analysed data
                              # Need to add a sheet with the metadata of the analysis
                              data_to_save = pd.DataFrame()
                              data_to_save["Raw_signal"] = pb_data["demodulated_signal "+values["-Signals List Analyzer-"]]
                              if filtered_demod == "_filter":
                                  data_to_save["Filtered_signal"] = pb_data["demodulated_signal "+values["-Signals List Analyzer-"]+"_filter"]
                              data_to_save["df_over_f"] = pb_data["df_over_f"]
                              if Zscore_all_done:
                                  data_to_save["Zscore"] = zscore_data
                              data_to_save = data_to_save[::int(values["Saving decimation corrected data"])]   
                              
                              print(analysed_data_folder)
                              print(corrected_data_folder)
                              if extension == ".csv":
                                  data_to_save.to_csv(os.path.join(corrected_data_folder,"Data_"+save_filename))
                                  pd.Series(values).to_csv(os.path.join(corrected_data_folder,"AnalysisParameter_"+save_filename))
                                  # pd.Series(metadata).to_csv(os.path.join(corrected_data_folder,"MetaData_"+save_filename))
                                  sg.popup("File Saved")
                              elif extension == ".xlsx":
                                  max_size = 1000000
                                  if int(np.ceil(len(data_to_save)/max_size)) <=3:
                                      writer = pd.ExcelWriter(os.path.join(corrected_data_folder,save_filename),engine="openpyxl")
                                      if len(data_to_save)<=max_size:
                                          data_to_save.to_excel(writer, sheet_name = "Data")
                                      else:
                                          for i in range(int(np.ceil(len(data_to_save)/max_size))):
                                              data_to_save.iloc[i*max_size:(i+1)*max_size].to_excel(writer, sheet_name = "Data_part"+str(i))
                                              print(i)
                                      pd.Series(values).to_excel(writer, sheet_name = "Analysis parameter")
                                      # pd.Series(metadata).to_excel(writer, sheet_name = "MetaData")
                                      # writer.save()
                                      writer.close()
                                      sg.popup("File Saved")
                                  else:
                                     sg.popup_scrolled("Data are too long to be saved as Excel sheet ! \n Decimate your data if you don't need as much of temporal resolution \n or Use CSV or NWB file format if you need to conserve your temporal resolution")
                        else:
                            sg.popup("Analyse your data before saving them !")

                    elif event == "Save Trials Extracted":
                        if Trials_extraction_done:
                          response = sg.popup_yes_no("Saving the trial data to the NWB ?")
                          analysed_data_folder = os.path.join(data_folder, "Analysed_data")
                            
                          if os.path.exists(analysed_data_folder):
                                sg.popup_notify("Analysed data folder already in : \n {0}".format(data_folder))
                          else:
                                sg.popup_notify("Creating a analysed data folder in : \n {0}".format(data_folder))
                                os.makedirs(analysed_data_folder)
                                
                          if response == "Yes":
                              trials_data_to_save = {"df_over_f":Trials_extracted}
                              if values['Trial_filter']:
                                  trials_data_to_save["Filter"] = Filtered_trials
                              if values['Zscore on trial']:
                                  trials_data_to_save["Zscore"] = Zscore_trials
                              with NWBHDF5IO(NWB_converter.file_path, "r") as io:
                                    export_filename = os.path.join(analysed_data_folder, NWB_converter.file_path.split("/")[-1].split(".nwb")[0]+"_Analysed.nwb")
                                    read_nwbfile = io.read()
                                    try:
                                        read_nwbfile.create_processing_module(name="Events", 
                                                                  description = "Event extracted around {}".format(values["-Event triggers-"]))
                                    except:
                                        print("Processing module already exist")
                                    for data_type, trial_data_to_save in trials_data_to_save.items(): 
                                          trial_data_to_save =  trial_data_to_save.reset_index()
                                          trial_data_to_save.rename(columns={"resize": "TimeStamps"}, inplace= True)
                                          Event_table = generate_event_table(info =  {"Event_trigger": values["-Event triggers-"], "Data_type": data_type}, data= trial_data_to_save)
                                          try: 
                                              read_nwbfile.processing["Events"].add(Event_table) 
                                          except:
                                              print("Event table {} already exist".format(Event_table.name))
                                              read_nwbfile.processing["Events"].data_interfaces.pop(Event_table.name)
                                              read_nwbfile.processing["Events"].add(Event_table)
                                    Param_table =generate_param_table({"Event_trigger": values["-Event triggers-"], "Data_type": "Analysis parameter"}, data = values)
                                    try:
                                          read_nwbfile.processing["Events"].add(Param_table)
                                    except:
                                          print("Event table {} already exist".format(Param_table.name))
                                          read_nwbfile.processing["Events"].data_interfaces.pop(Param_table.name)
                                          read_nwbfile.processing["Events"].add(Param_table)
                                    with NWBHDF5IO(export_filename, mode="w") as export_io:
                                        export_io.export(src_io=io, nwbfile=read_nwbfile)        
                                    sg.popup("Trials Saved to NWB")
                                
                          elif response == "No":
                            start_name = name+"_Trials-"+values["-Signals List Analyzer-"]
                            
                            save_filename , extension = popup(start_name)
                            analysed_data_folder = os.path.join(data_folder, "Analysed_data")
                            popup_size = (50, 5)
                            if os.path.exists(analysed_data_folder):
                                sg.popup_notify("Analysed data folder already in : \n {0}".format(data_folder))
                            else:
                                sg.popup_notify("Creating a analysed data folder in : \n {0}".format(data_folder))
                                os.makedirs(analysed_data_folder)
                            corrected_data_folder = os.path.join(analysed_data_folder, "Trials_data")
                            if os.path.exists(corrected_data_folder):
                                sg.popup_notify("Corrected data folder already in : \n {0}".format(analysed_data_folder))
                            else:
                                sg.popup_notify("Creating a corrected data folder in : \n {0}".format(analysed_data_folder))
                                os.makedirs(corrected_data_folder)
                            if extension == ".csv":
                                pass
                                
                            elif extension == ".xlsx":
                                writer = pd.ExcelWriter(os.path.join(corrected_data_folder,save_filename),engine="openpyxl")
                                Trials_extracted.reset_index().rename(columns={"resize": "TimeStamps"}).to_excel(writer, sheet_name = "df_over_f")
                                if values['Trial_filter']:
                                    Filtered_trials.reset_index().rename(columns={"resize": "TimeStamps"}).to_excel(writer, sheet_name = "Filter")
                                if values['Zscore on trial']:
                                    Zscore_trials.reset_index().rename(columns={"resize": "TimeStamps"}).to_excel(writer, sheet_name = "Zscore")
                                pd.Series(values).to_excel(writer, sheet_name = "Analysis parameter")
                                writer.close()
                                sg.popup("File Saved")
                        else:
                            sg.popup("Extract your trials before saving them !")
                    elif event =="Save Parameter":
                        with open("Setting.json", "w") as outfile: 
                            Setting = values
                            del Setting["-Signals List Analyzer-"]
                            json.dump(Setting, outfile)
                            sg.popup_notify("Parameter saved to Setting.json", title="Parameter saved", display_duration_in_ms = 3000, fade_in_duration= 500)
                    if analyser_active:
                        list_state = [state_pb, state_zscore,state_filtering,state_trials]
                        # Status bar update
                        for status , state, indicator in zip(list_status, list_state, list_indicator):
                            # print(status)
                            # print(state)
                            value, text_color = status[state]
                            Analyser_window[indicator].update(value=value, text_color=text_color)
                            
                  
            except Exception as e:
                sg.popup("Error encountered in Analyser: {0}".format(e), title = "Error in Analyser")
                plt.close("All")
                Analyser_window.close()
                analyser_active = False
                window.UnHide()
                traceback.print_exc()
window.close()
if analyser_active:
    Analyser_window.close()