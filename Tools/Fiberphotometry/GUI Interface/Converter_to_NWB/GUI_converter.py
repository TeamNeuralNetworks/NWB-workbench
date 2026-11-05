# -*- coding: utf-8 -*-
"""
Created on Wed Apr  3 13:24:13 2024

@author: PierreA.DERRIEN
"""

import sys
import os
import pathlib
import traceback
#  Get the file directory to find all the parent submmodule whereever the user put this script
script_directory = pathlib.Path(__file__).parent.resolve()
file_path = str(script_directory).split("\\")[:-3]
file_path[0] = file_path[0]+r"\\"
module_folder = os.path.join(*file_path)
sys.path.append(module_folder)


from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.convert_from_excel import convert_excel_to_nwb
from Functionnal_Imaging_Electrophysiology.General_tools import PySimpleGUI as sg

# All the stuff inside your window.
layout = [[sg.Text('Enter the Excel file to setup the conversion tool')],
          [sg.Text('File', size=(8, 1)), sg.Input(), sg.FileBrowse()],
          [sg.Submit(), sg.Cancel()]] 

# Create the Window
window = sg.Window('GUI converter', layout)

# Event Loop to process "events" and get the "values" of the inputs
while True:
    event, values = window.read()
    
    if event == "Submit":
        try:
            print("File used to conversion: {0}".format(values[0].split("/")[-1]))
            path_list = values[0].split("/")[:-1]
            path_list[0] = path_list[0]+r"\\"
            direc_path = os.path.join(*path_list)
            print(os.path.join(direc_path, "Files"))
            # print(Path(os.path.join(direc_path, "Files")).is_python dir())
            if os.path.exists(os.path.join(direc_path, "Files")) == False: 
                print("No files directorie, making one")
                os.mkdir(os.path.join(direc_path, "Files"))
            # pathlib.Path.exists(os.path.join(direc_path, "Files"))
            else: 
                print("Files directories already existing")
            convert_excel_to_nwb(values[0], os.path.join(direc_path, "Files"))
        except Exception as e:
            sg.popup_scrolled("Error : "+str(e))
            traceback.print_exc()
        
    # if user closes window or clicks cancel
    if event == sg.WIN_CLOSED or event == 'Cancel':
        break
window.close()


