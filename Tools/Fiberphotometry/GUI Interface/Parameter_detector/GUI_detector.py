# -*- coding: utf-8 -*-
"""
Created on Tue Jul  9 14:12:53 2024

@author: PierreA.DERRIEN
"""

import sys
import os
import pathlib
from glob import glob
script_directory = pathlib.Path(__file__).parent.resolve()
file_path = str(script_directory).split("\\")[:-3]
file_path[0] = file_path[0]+r"\\"
module_folder = os.path.join(*file_path)
sys.path.append(module_folder)

import numpy as np
import traceback
from Functionnal_Imaging_Electrophysiology.General_tools import PySimpleGUI as sg
import pandas as pd
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.analysis_tools.parameter_detector import detect_activation, detect_inhibition
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.NWB_to_Dataframe import NWB_to_Dataframe_converter
import seaborn as sb
from matplotlib import pyplot as plt

# All the stuff inside your window.
layout = [[sg.Column([[sg.Text('Files', size=(8, 1))],
                      [sg.Input(size=(23, 1), key= "File_folder",enable_events=True), sg.FolderBrowse(key="File_browser", enable_events=True)],
                      [sg.Listbox(values=[], enable_events=True, size=(30, 17), key="File_list")],
                      [sg.Button("Load file", key = "Load_file", size=(28, 1))],
                      ]), 
           
           sg.Column([[sg.Text('Trials', size=(8, 1))],
                      [sg.Checkbox("Select All Trials", key = "All")],
                      [sg.Listbox(values=[], key= "Trial_list", enable_events=True, size=(20, 17),select_mode = "extended")],
                      [sg.Combo(values = [], size = (20,1), key= "Data_type")]
                      
                      ]),
           sg.VerticalSeparator(),
           sg.Column([[sg.Text('Detector', size=(8, 1))],
                      [sg.Combo(values=["Activation", "Inhibition", "Both"], key="Sign", size = (10,1),default_value = "Activation"), sg.Checkbox("Agregate trials", key = "Agregate_trial"), sg.Combo(values=["Mean", "Median"], key="Agregate_method", size = (10,1),default_value = "Mean") ],
                      [sg.Text("Threshold zone : lim inf"), sg.Input("", key = "ZT_min", enable_events= True,  size=(4, 1)),sg.Text("lim sup"), sg.Input("", key = "ZT_max", enable_events= True,  size=(4, 1))],
                      [sg.Text("Threshold : "), sg.Input("2", key = "Thresh", enable_events= True,  size=(4, 1)), sg.Text("in : "), sg.Radio('SD', key = "SD", group_id=1, default=True), sg.Radio('%', key="Percent", group_id=1)],
                      [sg.Text("Start research zone (in sec) : "), sg.Input("0", key = "Start", enable_events= True,  size=(4, 1))],
                      [sg.Text("Minimum time for response (in sec) :           "), sg.Input("0.05", key = "Min_rep", enable_events= True,  size=(4, 1))],
                      [sg.Text("Crossing point : "), sg.Combo(values=["Before", "After"], key="Crossing_point", size = (12,1),default_value = "After")],
                      [sg.Checkbox("Start response can be negative", key = "Neg_correction", default = True) ],
                      [sg.Button("Detect parameter", key = "Detect", size=(20, 1)), sg.Button("Show Detection", key= "Show",size=(20, 1))],
                      [sg.Button("Analyse all folder", key = "Full_folder", size=(42, 1))],
                      [sg.HorizontalSeparator()],
                      [sg.Text("Detected parameter value : ")],
                      [sg.Column([[sg.Text("Start time : "),sg.Text("" ,key= "Start time", size=(5,1))],
                                  [sg.Text("Max value : "),sg.Text("" ,key= "Max value", size=(5,1))],
                                  [sg.Text("AUC :        "),sg.Text("" ,key= "AUC",size=(5,1))]
                                  ], size=(155,90)),
                       sg.VerticalSeparator(),
                       sg.Column([[sg.Text("End time :  "),sg.Text("" ,key= "End time",size=(5,1))],
                                  [sg.Text("Max time : "),sg.Text("" ,key= "Max time",size=(5,1))],
                                  [sg.Text("Duration :  "),sg.Text("" ,key= "Duration",size=(5,1))]],size=(155,90))],

                      [sg.Input(size=(32, 1), key= "Save_file",enable_events=True), sg.FileBrowse(key="Save_browser", enable_events=True), sg.Button("Save", key = "Save")],
                      ]),
                       
          ],]
detectors_listing = {"Activation" : [("Activation", detect_activation)],
                     "Inhibition" : [("Inhibition", detect_inhibition)],
                     "Both": [("Activation", detect_activation),("Inhibition", detect_inhibition)],
                     }
def load_file(path):
    extension = path.split("\\")[-1].split(".")[1]
    data_dict ={}
    if extension == "xlsx":
        sheets = pd.ExcelFile(path).sheet_names
        
        if "Analysis parameter" in sheets:
            for i, sheet in enumerate(sheets):
                if sheet == "Analysis parameter":
                    sheets.pop(i)
        
        
        for i, sheet in enumerate(sheets):
            if sheet != "Analysis parameter":
              data = pd.read_excel(file_dict[file_selection], 
                                    sheet_name= sheet
                                   )
              print(data.columns)
              if "resize" in data.columns:
                  data.rename(columns={"resize": "Time"}, inplace= True)
              elif "TimeStamps" in data.columns:
                  data.rename(columns={"TimeStamps": "Time"}, inplace= True)
              data.set_index("Time", inplace=True)
              data_dict[sheet] = data.copy()
            sg.one_line_progress_meter("Loading file", i+1 , len(sheets), "Loading the data")
    elif extension =="nwb":
        NWB_file = NWB_to_Dataframe_converter(path)
        NWB_file.convert()
        sheets = []
        for sheet, data in NWB_file.Events.items():
            if "Analysis parameter" not in sheet :
                if "resize" in data.columns:
                    data.rename(columns={"resize": "Time"}, inplace= True)
                elif "TimeStamps" in data.columns:
                    data.rename(columns={"TimeStamps": "Time"}, inplace= True)
                data.set_index("Time", inplace=True)
                data_dict[sheet] = data.copy()
                sheets.append(sheet)
    sg.popup("Data loaded")
    return sheets , data_dict

def show_param(analysed_data,col_to_use,parameter,points, rep_data):
    # plt.figure("Result")
    sb.lineplot(data = analysed_data, x = analysed_data.index, y= col_to_use)
    plt.axhline(y = parameter["Thresh_acti"], linestyle = "--", color = "orange")
    plt.axhline(y = parameter["Mean"], linestyle = "--", color = "grey")
    plt.axvline(x = parameter["Start_rep"], linestyle = "--", color = "green")
    plt.axvline(x = parameter["End_rep"], linestyle = "--", color = "orange")
    plt.axvline(x = parameter["Real_end"], linestyle = "--", color = "red")
    plt.scatter(x= parameter["Maximum_time"], y= parameter["Maximum_val"], marker = "v", color = "red")
    plt.scatter(points["Time"],points[col_to_use], marker = "v", color = "grey")
    if type(rep_data) != type(str()):
        plt.fill_between( x= rep_data["Time"],y1=rep_data[col_to_use],alpha = 0.5, color= "cyan")
        
def show_param_multi(analysed_data,col_to_use,parameter,points, rep_data, ax):
    # plt.figure("Result")
    sb.lineplot(data = analysed_data, x = analysed_data.index, y= col_to_use, ax= ax)
    ax.axhline(y = parameter["Thresh_acti"], linestyle = "--", color = "orange")
    ax.axhline(y = parameter["Mean"], linestyle = "--", color = "grey")
    ax.axvline(x = parameter["Start_rep"], linestyle = "--", color = "green")
    ax.axvline(x = parameter["End_rep"], linestyle = "--", color = "orange")
    ax.axvline(x = parameter["Real_end"], linestyle = "--", color = "red")
    ax.scatter(x= parameter["Maximum_time"], y= parameter["Maximum_val"], marker = "v", color = "red")
    ax.scatter(points["Time"],points[col_to_use], marker = "v", color = "grey")
    if type(rep_data) != type(str()):
        ax.fill_between( x= rep_data["Time"],y1=rep_data[col_to_use],alpha = 0.5, color= "cyan")

def update_display_param(window, parameter):
    for idx, param in [("Start time","Start_rep"),("Max value","Maximum_val"),("AUC","AUC_rep"),("End time","End_rep"),("Max time","Maximum_time")]:
        if type(parameter[param]) != type(str()):
            window[idx].update(round(parameter[param], 4))
    if type(parameter["End_rep"]) != type(str()) and type(parameter["Start_rep"])!= type(str()):
        window["Duration"].update(round(parameter["End_rep"]-parameter["Start_rep"],4))
    pass


# Create the Window
window = sg.Window('GUI converter', layout, finalize= True)
last_folder = ""
file_dict = {}
file_selection = "None"
col_to_use = "None"
data = None
detect_param = False
trial_selection = []
# Event Loop to process "events" and get the "values" of the inputs
while True:
    event, values = window.read()
    print(values)
    try:
        if event == "File_folder":
            if last_folder != values['File_browser']:
                last_folder = values['File_browser']
                file_dict = {}
                xlsx = glob(last_folder+"\*.xlsx")
                NWB = glob(last_folder+"\*.nwb")
                for file_path in xlsx+NWB:
                    file_dict["({}) {}".format(file_path.split("\\")[-1].split(".")[1], file_path.split("\\")[-1].split(".")[0])] = file_path
                window["File_list"].update(values=["{}".format(x) for x in file_dict.keys()])
            
        elif event == "File_list":
            if len(values[event]) >0:
                file_selection = values[event][0]
            
        elif event == "Load_file":
            data = None
            window["Data_type"].update(value="", values=[])
            if file_selection != "None":
                # sheet = sheet_pop_up(file_dict[file_selection])
                trial_selection = []
                sheet , data_dict = load_file(file_dict[file_selection])
                window["Trial_list"].update([x for x in data_dict[sheet[0]].columns])
                window["Data_type"].update(value=sheet[-1], values=sheet)
                    
        elif event == "Trial_list":
            trial_selection = values[event]
            
        elif event == 'Thresh' or event == 'Start' or event == 'Min_rep' or event == "ZT_min" or event == "ZT_max":
         if len(values[event])>=1:
            if values[event][-1] not in ('0123456789.-'):
                sg.popup("Only digits allowed")
                window[event].update(values[event][:-1])
         
        elif event == "Detect":
         if  values["Percent"]:
             sg.popup("Warning : Percent threshold currently in dev - Errors in detetion might occurs")
         detect_param = False
         plt.close("Result")
         if values["Data_type"] != "":
             data = data_dict[values["Data_type"]]
         if type(data) != type(None):
            parameter_analysis = {"start_time": float(values["Start"]),
                         "thresh_acti": float(values["Thresh"]),
                         "min_time_rep": float(values["Min_rep"])
                            }
            if values["ZT_min"] != "" and values["ZT_max"] != "":
                zone_thres = [float(values["ZT_min"]),float(values["ZT_max"])]
            elif values["ZT_min"] != "" and values["ZT_max"] == "":
                zone_thres = [float(values["ZT_min"]),0]
            elif values["ZT_min"] == "" and values["ZT_max"] != "":
                zone_thres = [data.index.values.min(),float(values["ZT_max"])]
            else: 
                zone_thres = [data.index.values.min(),0]
            print(parameter_analysis)
            if values["All"]: 
                analysed_data = data.copy()
            else:   
                analysed_data = data[trial_selection].copy()
            
            if values["Agregate_trial"]:
                if values["Agregate_method"] == "Mean":
                    analysed_data["Mean"] = analysed_data.mean(axis = 1)
                    col_to_use = "Mean"
                if values["Agregate_method"] == "Median":
                    analysed_data["Median"] = analysed_data.median(axis = 1)
                    col_to_use = "Median"
            else: 
                if len(trial_selection) >1: 
                    sg.popup("If you want to analyse several trial together, check the agregate trial")
                    col_to_use = "Multi"
                elif len(trial_selection) == 0 and not values["All"]:
                    sg.popup("Select a trial or check the Select all")
                    col_to_use = "None"
                elif len(trial_selection) == 0 and values["All"]:
                    col_to_use = "None"
                else:
                    col_to_use = trial_selection[0]
                    
            if values['Sign'] == "Activation":
                if col_to_use != "None":
                    if col_to_use != "Multi":
                        plt.figure("Result")
                        points, rep_data, parameter = detect_activation(analysed_data.reset_index(), col_to_use, "Time", parameter_analysis,
                                                                       before= values["Crossing_point"] == "Before",
                                                                       neg_start_correction= not values["Neg_correction"], 
                                                                       threshold_zone = zone_thres
                                                                       )
                        detect_param = True
                        show_param(analysed_data,col_to_use,parameter,points, rep_data)
                        update_display_param(window, parameter)
                        print(parameter)
                    else:
                        fig , ax = plt.subplots(int(np.ceil((len(trial_selection)/2))),2, num = "Result")
                        for i , trials_to_use in enumerate(trial_selection):
                            points, rep_data, parameter = detect_activation(analysed_data.reset_index(), trials_to_use, "Time", parameter_analysis,
                                                                           before= values["Crossing_point"] == "Before",
                                                                           neg_start_correction= not values["Neg_correction"],
                                                                           threshold_zone = zone_thres,
                                                                           percent= values["Percent"]
                                                                           )
                            detect_param = True
                            
                            show_param_multi(analysed_data,trials_to_use,parameter,points, rep_data, ax[i//2, i%2])
                            # update_display_param(window, parameter)
                            print(parameter)
            elif values['Sign'] == "Inhibition":
                if col_to_use != "None":
                    if col_to_use != "Multi":
                        plt.figure("Result")
                        points, rep_data, parameter = detect_inhibition(analysed_data.reset_index(), col_to_use, "Time", parameter_analysis,
                                                                       before= values["Crossing_point"] == "Before",
                                                                       neg_start_correction= not values["Neg_correction"],
                                                                       threshold_zone = zone_thres,
                                                                       percent= values["Percent"]
                                                                       )
                        detect_param = True
                        show_param(analysed_data,col_to_use,parameter,points, rep_data)
                        update_display_param(window, parameter)
                        print(parameter)
                    else:
                        fig , ax = plt.subplots(int(np.ceil((len(trial_selection)/2))),2, num = "Result")
                        for i , trials_to_use in enumerate(trial_selection):
                            points, rep_data, parameter = detect_inhibition(analysed_data.reset_index(), trials_to_use, "Time", parameter_analysis,
                                                                           before= values["Crossing_point"] == "Before",
                                                                           neg_start_correction= not values["Neg_correction"],
                                                                           threshold_zone = zone_thres,
                                                                           percent= values["Percent"]
                                                                           )
                            detect_param = True
                            
                            show_param_multi(analysed_data,trials_to_use,parameter,points, rep_data, ax[i//2, i%2])
                            update_display_param(window, parameter)
                            print(parameter)
            elif values['Sign'] == "Both":
                plt.close("Result")
                if col_to_use != "Multi":
                    plt.figure("Result")
                else:
                    fig , ax = plt.subplots(int(np.ceil((len(trial_selection)/2))),2, num = "Result")
                
                for type_detector , detector in detectors_listing["Both"]:
                    
                    if col_to_use != "Multi":
                       
                        points, rep_data, parameter = detector(analysed_data.reset_index(), col_to_use, "Time", parameter_analysis,
                                                                       before= values["Crossing_point"] == "Before",
                                                                       neg_start_correction= not values["Neg_correction"],
                                                                       threshold_zone = zone_thres,
                                                                       percent= values["Percent"]
                                                                       )
                        if values["Agregate_trial"]:
                            parameter["Trial"]= "{} for {}".format(col_to_use, trial_selection)
                        else: 
                            parameter["Trial"] = col_to_use
                        print(rep_data)
                        show_param(analysed_data,col_to_use,parameter,points, rep_data)
                        # update_display_param(window, parameter)
                        print(parameter)
                    else:
                        
                        for i , trials_to_use in enumerate(trial_selection):
                            points, rep_data, parameter = detector(analysed_data.reset_index(), trials_to_use, "Time", parameter_analysis,
                                                                           before= values["Crossing_point"] == "Before",
                                                                           neg_start_correction= not values["Neg_correction"],
                                                                           threshold_zone = zone_thres,
                                                                           percent= values["Percent"]
                                                                           )
                            show_param_multi(analysed_data,trials_to_use,parameter,points, rep_data, ax[i//2, i%2])
                            # update_display_param(window, parameter)
                            print(parameter)
                            parameter["Trial"]= trials_to_use
                            parameter["Type"] = type_detector
                       
                pass
            
        if event=="Show":
          plt.close("Result")
          if  detect_param == True and col_to_use != "None":
              show_param(analysed_data,col_to_use,parameter,points, rep_data)
       
        
        if event == "Full_folder":
            if not os.path.exists(os.path.join(last_folder, "Result_Detection")):
            # Test if result folder exist 
                os.makedirs(os.path.join(last_folder, "Result_Detection"))
                # if not create a folder inside the current analysed folder
            save_folder = os.path.join(last_folder, "Result_Detection")    
            # Test is display folder exist
            if not os.path.exists(os.path.join(save_folder, "Figure")):
                os.makedirs(os.path.join(save_folder, "Figure"))
                # if not create one in the result folder
            Dataset = pd.DataFrame()
            for k , file in enumerate(file_dict.keys()):
                Errors_dict = {}
                try:
                    print(file)
                    data = pd.read_excel(file_dict[file], sheet_name=values["Data_type"])
                    data.rename(columns={"resize": "Time"}, inplace= True)
                    data.set_index("Time", inplace=True)
    
                    parameter_analysis = {"start_time": float(values["Start"]),
                                 "thresh_acti": float(values["Thresh"]),
                                 "min_time_rep": float(values["Min_rep"])
                                    }
                    if values["All"]: 
                        analysed_data = data.copy()
                    else:   
                        analysed_data = data[trial_selection].copy()
                    
                    if values["Agregate_trial"]:
                        if values["Agregate_method"] == "Mean":
                            analysed_data["Mean"] = analysed_data.mean(axis = 1)
                            col_to_use = "Mean"
                        if values["Agregate_method"] == "Median":
                            analysed_data["Median"] = analysed_data.median(axis = 1)
                            col_to_use = "Median"
                    else: 
                        if len(trial_selection) >1: 
                            # sg.popup("If you want to analyse several trial together, check the agregate trial")
                            col_to_use = "Multi"
                        elif len(trial_selection) == 0:
                            # sg.popup("Select a trial or check the Select all")
                            col_to_use = "None"
                        else:
                            col_to_use = trial_selection[0]
                    
                    if col_to_use != "None":
                        detectors_to_use = detectors_listing[values['Sign']]
                        
                        for type_detector , detector in detectors_to_use:
                            plt.close("Result")
                            if col_to_use != "Multi":
                                plt.figure("Result")
                                points, rep_data, parameter = detector(analysed_data.reset_index(), col_to_use, "Time", parameter_analysis,
                                                                               before= values["Crossing_point"] == "Before",
                                                                               neg_start_correction= not values["Neg_correction"]
                                                                               )
                                if values["Agregate_trial"]:
                                    parameter["Trial"]= "{} for {}".format(col_to_use, trial_selection)
                                else: 
                                    parameter["Trial"] = col_to_use
                                parameter["File"]= file
                                parameter["Type"] = type_detector
                                Dataset = pd.concat([Dataset, pd.DataFrame([parameter])], ignore_index = True)
                                show_param(analysed_data,col_to_use,parameter,points, rep_data)
                                # update_display_param(window, parameter)
                                print(parameter)
                            else:
                                fig , ax = plt.subplots(int(np.ceil((len(trial_selection)/2))),2, num = "Result")
                                for i , trials_to_use in enumerate(trial_selection):
                                    points, rep_data, parameter = detector(analysed_data.reset_index(), trials_to_use, "Time", parameter_analysis,
                                                                                   before= values["Crossing_point"] == "Before",
                                                                                   neg_start_correction= not values["Neg_correction"]
                                                                                   )
                                    show_param_multi(analysed_data,trials_to_use,parameter,points, rep_data, ax[i//2, i%2])
                                    # update_display_param(window, parameter)
                                    print(parameter)
                                    parameter["Trial"]= trials_to_use
                                    parameter["File"]= file
                                    parameter["Type"] = type_detector
                                    Dataset = pd.concat([Dataset, pd.DataFrame([parameter])], ignore_index = True)
                            plt.savefig(os.path.join(save_folder, "Figure", type_detector+"-"+file+".png"))
                        sg.one_line_progress_meter("Analysing folder", k+1 , len(file_dict.keys()), "Analysing")
                except Exception as e:
                    if col_to_use != "Multi":
                        Errors_dict[file+"_"+col_to_use] = e
                    else:
                        Errors_dict[file+"_"+trials_to_use] = e
                        sg.one_line_progress_meter("Analysing folder", k+1 , len(file_dict.keys()), "Analysing")
                Dataset.to_excel(os.path.join(save_folder, "Dataset.xlsx"))
                  
            
        # if user closes window or clicks cancel
        if event == sg.WIN_CLOSED or event == 'Cancel':
            break
    except Exception as e:
        sg.popup_scrolled(e)
        traceback.print_exc()
    
            
window.close()
plt.close("all")