# -*- coding: utf-8 -*-
"""
Created on Sat Feb 28 11:20:47 2026

@author: Pierre
"""
from pathlib import Path
import platform
import numpy as np

from pynwb import NWBFile, NWBHDF5IO
from pynwb.file import Subject
from pynwb.epoch import TimeIntervals
from pynwb.misc import AnnotationSeries

def convert_keyword_to_dict(keywords):
    
    converted_keywords = {}
    
    for keyword in keywords:
        key = keyword.split(':')[0]
        value = keyword.split(':')[-1]
        assert key not in converted_keywords.keys(), 'keywords key must be unique'
        converted_keywords[key] = value
    
    return converted_keywords

def validate_platform_compatibility():
    """Validate platform-specific requirements"""
    system = platform.system()
    if system not in ['Windows', 'Darwin', 'Linux']:
        raise OSError(f"Unsupported operating system: {system}")
    
    if system == 'Windows':
        # Check for long path support on Windows
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
                value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
                if not value:
                    print("Warning: Windows long path support is not enabled")
        except WindowsError:
            print("Warning: Could not check Windows long path support")

def create_NWB(general_info, animal_info):
    
    # Validate platform compatibility
    validate_platform_compatibility()

    # Convert any file paths in general_info to Path objects
    if 'source_script' in general_info:
        general_info['source_script'] = str(Path(general_info['source_script']))
    if 'source_script_file_name' in general_info:
        general_info['source_script_file_name'] = str(Path(general_info['source_script_file_name']))

    # Create the NWB file with general information
    nwbfile = NWBFile(**general_info)
    # Add subject information to the NWB file
    nwbfile.subject= Subject(**animal_info)
    
    return nwbfile
    
def validate_save_path(filepath):
    """Validate and prepare the save path for both Windows and macOS."""
    # Convert to Path object
    save_path = Path(filepath)
    
    # Create parent directories if they don't exist
    save_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check platform-specific constraints
    if platform.system() == 'Windows':
        # Check Windows path length limitation
        if len(str(save_path)) > 260:
            raise OSError(f"File path exceeds Windows maximum path length: {save_path}")
    
    return save_path

def save_NWB(NWB_file, filepath, export=False):
    """
    Save NWB file with cross-platform compatibility.
    
    Parameters:
    -----------
    NWB_file : NWBFile
        The NWB file object to save
    filepath : str or Path
        The path where to save the file (without extension)
    export : bool, optional
        Whether to export the file (default: False)
    """
    try:
        # Validate and prepare save path
        save_path = validate_save_path(filepath)
        
        # Add .nwb extension
        save_path = save_path.with_suffix('.nwb')
        
        if not export:
            with NWBHDF5IO(str(save_path), 'w') as io:
                io.write(NWB_file)
        else:
            with NWBHDF5IO(str(save_path), mode="r") as read_io:    
                read_nwbfile = read_io.read()
                with NWBHDF5IO(str(save_path), mode="w") as export_io:
                    export_io.export(src_io=read_io, nwbfile=read_nwbfile)
                    
    except Exception as e:
        raise IOError(f"Failed to save NWB file: {str(e)}")
        

def add_event_markers_data(nwbfile, event_markers_dict):
    """
    Adds event_markers as AnnotationSeries if discrete event or as Timeinterval if the event last in time to an NWB file.

    Parameters
    ----------
    metadata_dict : dict
        A dictionary containing event_marker signal information (start, stop, description).
    nwbfile : pynwb.NWBFile
        The NWB file object to which the event_markers data will be added.

    Returns
    -------
    None
    """
        
    for event_markers_name, event_markers_info in event_markers_dict.items():
        if 'start' not in event_markers_info.keys() or len(event_markers_info['start']) == 0:
            continue
        
        # Add a TimeSeries object for each TTL signal
        if 'stop' in event_markers_info.keys():
            if event_markers_name == 'trials': #has it's own way to be handle in nwb file format
                continue
            if event_markers_name in nwbfile.intervals:
                print(f'\t{event_markers_name} already in NWB_file')
                continue
                
            current_epochs = TimeIntervals(
                                        name=event_markers_name,
                                        description=event_markers_info['description'],
                                        )
            
            for time_on, time_off in zip(event_markers_info['start'], event_markers_info['stop']):
                current_epochs.add_row(start_time=time_on,
                                       stop_time=time_off)
                
            nwbfile.add_time_intervals(current_epochs)          
            
        else:
            if event_markers_name in nwbfile.acquisition:
                print(f'\t{event_markers_name} already in NWB_file')
                continue
                
            if 'values' in event_markers_info.keys():
                data = event_markers_info['values']
            else:
                data = [event_markers_name]*len(event_markers_info['start'])
                
            digital_in_annotation  = AnnotationSeries(
                                                name=event_markers_name,
                                                data=data,
                                                description=event_markers_info['description'],
                                                timestamps=event_markers_info['start'],
                                            )
            nwbfile.add_acquisition(digital_in_annotation)
                
def add_trials(nwbfile, trials_df, trials_df_description=None):
    
    for column in trials_df.columns:
        if column == 'start_time' or column == 'stop_time':
            continue
        description = 'No description available'
        if trials_df_description is not None and column in trials_df_description.keys():
            description = trials_df_description[column]
        nwbfile.add_trial_column(name=column, description=description)
    
    for _, trial_row in trials_df.iterrows():
        nwbfile.add_trial(**trial_row)

def add_trials_column(nwbfile, trial_df, trial_df_description):
    
    for column_name in trial_df.columns:
        if column_name not in nwbfile.trials.colnames:
            nwbfile.add_trial_column(
                data=trial_df[column_name].tolist(),
                name=column_name,
                description=trial_df_description[column_name]
            )
        else:
            print(f'{column_name} already in nwbf')
