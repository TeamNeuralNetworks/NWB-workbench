# -*- coding: utf-8 -*-
"""
Created on Mon Feb 23 12:06:47 2026

@author: pierre.le-cabec
"""
import logging
from datetime import datetime
import pandas as pd
import os
from uuid import uuid4
from alive_progress import alive_bar
from pathlib import Path  # More modern path handling

from Builders.General.NWB_builder import create_NWB, add_event_markers_data, add_trials, save_NWB

from Builders.Electrophysiology.file_openers.intan_system_file import Intan_opener
from Builders.Electrophysiology.file_openers.custom_opener import custom_opener
from Builders.Electrophysiology.builder import add_electrophysiological_data
    
def convert_gen_info(info):
    
    """ Input : pd.Dataframe
        Return: Dict
    """
    formated_info = {}
    item_list = ["session_description","session_id","notes",
                 "protocol","lab","institution","experiment_description",
                 "related_publications", "virus","surgery","pharmacology"]
    
    for item in item_list:
        try:
            current_info = info[info["General Info"]==item]["Unnamed: 1"].tolist()[0]
            formated_info[item] = None if pd.isna(current_info) else current_info
        except IndexError:
            # print(item)
            formated_info[item]= "None"
        
            
    formated_info["identifier"]=str(uuid4())
    formated_info["experimenter"] = info["Experimenters"].dropna().tolist()
    formated_info["keywords"] = []

    for keywords in info["Additional metadata"].dropna().tolist()[1:]:
        try:
            formated_info["keywords"].append(f'{keywords}:{info[info["Additional metadata"]==keywords]["Unnamed: 6"].tolist()[0]}')
        except IndexError:
            # print(item)
            formated_info["keywords"][keywords] = None
    
    formated_info["source_script"] ="Script file used to create this NWB file"# optional
    formated_info["source_script_file_name"]="Name of the source_script file"# optional

    nwb_file_name = info[info["General Info"]=="nwb_file_name"]["Unnamed: 1"].tolist()[0]
    nwb_file_name = None if pd.isna(nwb_file_name) else nwb_file_name
    
    nwb_file_path = info[info["General Info"]=="nwb_file_path"]["Unnamed: 1"].tolist()[0]
    nwb_file_path = None if pd.isna(nwb_file_path) else Path(nwb_file_path)
    
    
    return formated_info, nwb_file_name, nwb_file_path

def retrieve_animal(info):
    """ Input: pd.Series
        Return: dict
    """
    
    formated = {}
    # Change default date to indicate it's unidentified/placeholder
    session_start = datetime(1900, 1, 1, 0, 0, 0, tzinfo=tz.gettz("US/Pacific"))  # Unidentified date
    for i in info.index:
        if i == "File Directory" :
            file_direc = info[i]
        elif i == "session_start_time":
            try:
                session_start = pd.to_datetime(info[i])
                session_start = session_start.round("S")
            except ValueError:
                print(f"Invalid date format for session_start_time: {info[i]}")
                session_start = datetime(1900, 1, 1, 0, 0, 0, tzinfo=tz.gettz("US/Pacific"))  # Unidentified date
        elif i== "File name":     
            file_name = info[i]
            file_name = None if pd.isna(file_name) else file_name
        elif i == "File type":
            file_opener = info[i]
        elif i =="Animal number" or i=="Comments":
            pass
        elif i != "date_of_birth":    
            formated[i]= str(info[i])
        else:
            formated[i]= info[i]
            
    if file_name is None:
        filepath = file_direc
    else:
        filepath = os.path.join(file_direc, file_name)
    
    for key in formated.keys():
        if pd.isna(formated[key]):
            formated[key] = None
    
    return session_start, formated, filepath, file_opener
    
def convert_device(device_info):
    
    info = {}
    for row_index, row in device_info.iterrows():
        device_name = row["name"]
        assert device_name not in info.keys()
        info[device_name] = {"name": row["name"],
                            "model_name": row["model_name"],
                            "manufacturer": row["manufacturer"],
                            "serial_number": row["serial_number"],
                            "number_of_channels": row["number_of_channels"],
                            "number_of_shanks": row["number_of_shanks"],
                            "location": row["location"],
                            "description": row["description"],
                            'conversion_factor_to_V': row["conversion_factor_to_V"],
                            }
    
        for key in info[device_name].keys():
            if pd.isna(info[device_name][key]):
                info[device_name][key] = None
            
    return info


def convert_TTL(info):
    TTL_dict = {}
    for col in info.columns[1:]:
        TTL = {"start_channel":info[info["Infos"]== "start_channel"][col].tolist()[0],
               "start_type":info[info["Infos"]== "start_type"][col].tolist()[0],
               "stop_channel":info[info["Infos"]== "stop_channel"][col].tolist()[0],
               "stop_type":info[info["Infos"]== "stop_type"][col].tolist()[0],
               "description":info[info["Infos"]== "description"][col].tolist()[0],
                   }
        
        for key in TTL.keys():
            if pd.isna(TTL[key]):
                TTL[key] = None
        
        TTL_dict[info[info["Infos"]== "TTL_name"][col].tolist()[0]] = TTL

    return TTL_dict

def convert_aditional_trial_metric(aditional_trial_metric):
    
    converted_aditional_trial_metric = {}
    for metric, metric_info in aditional_trial_metric.items():
        description = metric_info['description'].dropna()
        assert len(description) == 1
        description = description.loc[0]
        
        measurment_between_ref_and_target = metric_info['measurment_between_ref_and_target'].dropna()
        assert len(measurment_between_ref_and_target) == 1
        measurment_between_ref_and_target = measurment_between_ref_and_target.loc[0]
        
        ttl_of_reference_list = metric_info['reference'].dropna().tolist()
        if 'target' in metric_info:
            target_ttl_list = metric_info['target'].dropna().tolist()
        else:
            target_ttl_list = None
            
        if 'outcome' in metric_info:
            outcome = metric_info['outcome'].dropna().tolist()
        else:
            outcome = None
        
        converted_aditional_trial_metric[metric] = {'description': description, 
                                                    'measurment_between_ref_and_target': measurment_between_ref_and_target,
                                                    'reference': ttl_of_reference_list,
                                                    'target': target_ttl_list,
                                                    'outcome': outcome,
                                                    }
        
    return converted_aditional_trial_metric
        
def check_and_convert_channel_info_validity(channel_info, device_info):
    
    
    channel_info = channel_info.where(pd.notna(channel_info), None)
    for electrode_name, electrode_df in channel_info.groupby('name'):
        assert electrode_name in device_info.keys(), "All ectrodes must be present in the device tab"
        assert len(electrode_df) == device_info[electrode_name]['number_of_channels'], f"{device_info[electrode_name]['number_of_channels'] - len(electrode_df)} channel missing in Electrophyiology_signal for device {electrode_name} as compared to the number indicated in device tab"
        assert len(electrode_df['contact_ids'].unique()) == len(electrode_df), f"all contact_ids must be uniques ({electrode_df['contact_ids'].value_counts()[electrode_df['contact_ids'].value_counts() > 1].index.tolist()} is present multiple times)"
        if not pd.isna(device_info[electrode_name]["number_of_shanks"]):
            for shank_nb in range(1, device_info[electrode_name]["number_of_shanks"]+1):
                assert shank_nb in electrode_df['shank_id'].unique(), f"No electrode present on shank {shank_nb}"
            assert shank_nb == electrode_df['shank_id'].max(), f"More shank present in Electrophysiology_signals for electrode {electrode_name} than indicated in the Device_info tab"
        assert 'channel_info' not in device_info[electrode_name].keys()
        
        device_info[electrode_name]['channel_info'] = electrode_df
    
    return device_info

def get_opener(file_opener):

    if file_opener == "intan":
        print("Intan file opener")
        return Intan_opener
    elif file_opener == 'binary':
        raise ValueError(f'{file_opener} has not been implemented yet')
        
    elif file_opener == 'custom':
        print("Custom file opener")
        return custom_opener
    else:
        raise ValueError(f'Unsuported data_type {file_opener}')

import warnings
from datetime import datetime
from dateutil import tz

def convert_excel_to_nwb(file, warning =  False, logger = None, saving_folder=None):
    """
    

    Parameters
    ----------
    file : TYPE
        DESCRIPTION.
    save_direc : TYPE
        DESCRIPTION.

    Returns
    -------
    None.

    """
    logging.basicConfig(level= logging.INFO)
    log = logging.getLogger('alive_progress')
    
    file = Path(file)

    if warning == False:
        warnings.filterwarnings("ignore")
    print("\n Start script")
    general_info = pd.read_excel(file, sheet_name="General_info")
    animals_info = pd.read_excel(file, sheet_name="Animals_info", skiprows=1)
    channel_info = pd.read_excel(file, sheet_name="Electrophyisiology_signals", skiprows=0)
    device_info = pd.read_excel(file, sheet_name="Device_info")
    TTL_info = pd.read_excel(file, sheet_name="TTL_info").dropna(axis=1, how='all')
    aditional_trial_metric = {aditional_name: pd.read_excel(file, 
                                                            sheet_name=aditional_name, 
                                                            skiprows=[125, 126]).dropna(axis=0, how='all').dropna(axis=1, how='all') 
                              for aditional_name in pd.ExcelFile(file).sheet_names if aditional_name not in ["General_info",
                                                                                                             "Animals_info",
                                                                                                             "TTL_info",
                                                                                                             "Electrophyisiology_signals",
                                                                                                             "Device_info"
                                                                                                             ]}
    if len(aditional_trial_metric) == 0:
        aditional_trial_metric = None
        
    print("\n All info loaded from excel")
    general_info, nwb_file_name, nwb_file_path = convert_gen_info(general_info)
    TTL_info = convert_TTL(TTL_info)
    device_info = convert_device(device_info)
    aditional_trial_metric = convert_aditional_trial_metric(aditional_trial_metric)
    
    device_info = check_and_convert_channel_info_validity(channel_info, device_info)
    del channel_info
    
    print("\n Start file conversion to NWB")
    # with MoonSpinner('Processing…') as bar:
    with alive_bar(len(animals_info)) as bar:
        
        if len(animals_info) > 1 or animals_info['Animal number'].max() > 1:
            raise ValueError('Multi animal not implemented yet for electrophysiological data')
            
        for index, row in animals_info.iterrows():
            session_start, anim, filepath, file_opener = retrieve_animal(row)
            print(session_start)
    # =============================================================================
    #         auto opener in function of extension
    # =============================================================================
            general_info["session_start_time"] = session_start
            logger.info(f"File used for conversion: {filepath}")
            opener = get_opener(file_opener,
                                )
            opener = opener(filepath=filepath, 
                                device_info=device_info, 
                                TTL_info=TTL_info,
                                general_info=general_info, 
                                animal_info=anim, 
                                aditional_trial_metric=aditional_trial_metric)
        
            print("File openned")
            File = create_NWB(general_info, anim)
            
            add_electrophysiological_data(File, opener)
            
            if opener.event_markers is not None:
                add_event_markers_data(File, opener.event_markers)
                
            if opener.trials_df is not None:
                add_trials(File, opener.trials_df, opener.trials_df_description)
            
            if nwb_file_name is None:
                # Format the session start time
                session_start_str = str(session_start).replace(" ", "_").replace(":", "-")
                
                # Build the save path
                nwb_file_name = [
                    str(row['subject_id']),
                    session_start_str
                ]
                
                if pd.notna(row['Animal number']) and row['Animal number'] != '' and row['Animal number'] is not None:
                    nwb_file_name.append(str(row['Animal number']))
                
                if pd.notna(row['Comments']) and row['Comments'] != '' and row['Comments'] is not None:
                    nwb_file_name.append(str(row['Comments']).replace(" ", "_"))
                
                nwb_file_name = "_".join(nwb_file_name)
                
            if nwb_file_path is None:
                nwb_file_path = file.parent
            
            if saving_folder is not None:
                saving_folder = Path(saving_folder)
                save_path = saving_folder.joinpath(nwb_file_name)
            else:
                # Use Path for building save paths
                save_path = nwb_file_path.joinpath(nwb_file_name)
            i = 2
            while save_path.exists():
                save_path = nwb_file_path.joinpath(
                    f"{Path(nwb_file_name).stem}_{i}{Path(nwb_file_name).suffix}"
                )
                i += 1
            print(f"File saving to: {save_path}")
            save_NWB(File, save_path)   
            logger.info(f"File saved at: {save_path}")
            
      
        bar()
        # break
    print("\ End file conversion to NWB")
    logger.info("End file conversion to NWB")
    
    return save_path

