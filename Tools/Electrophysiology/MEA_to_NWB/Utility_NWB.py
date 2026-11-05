# -*- coding: utf-8 -*-
"""
Created on Wed Feb  5 10:31:52 2025

@author: PierreA.DERRIEN
"""

from pynwb import NWBHDF5IO, NWBFile, TimeSeries
from pynwb.file import Subject
from pynwb.ecephys import LFP, ElectricalSeries, SpikeEventSeries
import numpy as np
from alive_progress import alive_bar
def add_TTL_data(info, data, nwbfile):
    """
    Adds TTL (Transistor-Transistor Logic) signal data as TimeSeries to an NWB file.

    Parameters
    ----------
    info : dict
        A dictionary containing TTL signal information. Each TTL signal entry should have a data key pointing to the corresponding data column in the DataFrame.
    data : pandas.DataFrame
        A DataFrame containing the TTL signal data.
    nwbfile : pynwb.NWBFile
        The NWB file object to which the TTL TimeSeries data will be added.

    Returns
    -------
    None
    """
    for TTL, TTL_info in info.items():
        # Add a TimeSeries object for each TTL signal
        nwbfile.add_acquisition(TimeSeries(name= TTL+" | TTL signals",
                                   data=data[TTL_info["data"]].tolist(),
                                   description = TTL_info["data"],
                                   unit='V',  # arbitrary units
                                   timestamps = data.index.tolist()
                                   ))


def add_raw_data(info, data, device, nwbfile, sf):
    """
    Adds raw_data and corresponding TimeSeries data to an NWB file based on provided channel information.
   
    Parameters
    ----------
    info : dict
        A dictionary containing channel information. Each channel should have entries for :
        signals, emission lambda, excitation lambda, 
        imaging rate, indicator, location, coordinates, 
        and other necessary details.
    data : pandas.DataFrame
        A DataFrame containing the signal data.
    device : pynwb.device.Device
        The device informations associated with the NWB file.
    nwbfile : pynwb.NWBFile
        The NWB file object to which the channels and TimeSeries data will be added.

    Returns
    -------
    None
    """
    all_table_region = nwbfile.create_electrode_table_region(
                                                            region=list(info["channel_map"]),  # reference row indices 0 to N-1
                                                            description="all electrodes",
                                                                )
    raw_electrical_series = ElectricalSeries(
                                                name="Raw_data",
                                                description="Raw acquisition traces",
                                                data=(data.get_traces())*0.001,
                                                electrodes=all_table_region,
                                                starting_time=0.0,  # timestamp of the first sample in seconds relative to the session start time
                                                rate=sf,  # in Hz
                                                
                                                        )
    nwbfile.add_acquisition(raw_electrical_series)
    
def add_Units(info, Spikes,extracted_unit,elec_group, nwbfile):
    nwbfile.add_unit_column(name = "Xpos",
                        description = "X position of each spikes by the unit", 
                        index =True)
    nwbfile.add_unit_column(name = "Ypos",
                        description = "Y position of each spikes by the unit",
                        index =True)
    with alive_bar(len(extracted_unit)) as bar:
        for Unit_name, unit_info in extracted_unit.items():
            nwbfile.add_unit(spike_times=unit_info["Spikes"]["Times"].to_numpy() ,
                             electrodes = [int(unit_info["Main_elec"].replace("Ch",""))],
                             electrode_group = elec_group,
                             waveform_mean = np.mean(unit_info["Waveform"], axis = 0) ,
                             waveform_sd = np.std(unit_info["Waveform"], axis = 0),
                             waveforms = unit_info["Waveform"],
                             id = Unit_name,
                             Xpos = unit_info["Spikes"]["X_pos"].to_numpy(),
                             Ypos = unit_info["Spikes"]["Y_pos"].to_numpy()
                             )
            bar()

    
def set_electrode_table(nwb_file, info, device,  chip_name ="", loc = "Ex vivo slice", position = (1,1,1), ref_elec = "Not defined"):
    electrode_group = nwb_file.create_electrode_group(
        name = "MEA chip {}".format(chip_name),
        description = "Electrode group of the MEA chip used",
        device = device,
        location = loc
        
        )
    for channel in info["channel_map"]:
        nwb_file.add_electrode(
            id = channel,
            x = info["channel_positions"][channel][0],
            y = info["channel_positions"][channel][1],
            z= 0.0, #2D array
            group = electrode_group, 
            reference = ref_elec, 
            location = "Brain slice"
            )
    return electrode_group
    
def create_NWB(general_info, animal_info,info_recording, Spikes_info, device_info, raw_data, extracted_units, sf, TTL_info = {}, data_ttl = []):
    """
    Creates an NWB file with provided general information, animal information, channel data, and optional TTL signal data.

    Parameters
    ----------
    general_info : dict
        A dictionary containing general information for the NWB file, such as session_description, identifier, session_start_time, etc.
    animal_info : dict
        A dictionary containing information about the subject, such as species, genotype, sex, weight, etc.
    channel_info : dict
        A dictionary containing channel information, including details about devices, signals, optical channels, and imaging planes.
    data : pandas.DataFrame
        A DataFrame containing the signal data for the channels.
    TTL_info : dict, optional
        A dictionary containing TTL signal information, by default {}. Each TTL signal entry should have a data key pointing to the corresponding data column in the DataFrame.

    Returns
    -------
    pynwb.NWBFile
        The NWB file object with the added data.
    """
    # Create the NWB file with general information
    nwbfile = NWBFile(**general_info)
    # Add subject information to the NWB file
    nwbfile.subject= Subject(**animal_info)
    
    # Create a device and add it to the NWB file
    device = nwbfile.create_device(
        name=device_info["Name"],
        description=device_info["Description"],
        manufacturer=device_info["Manufacturer"],
        )

    # Add channel data to the NWB file  
    # add_channel(channel_info, data, device, nwbfile) #Old function based on imaging rate => assumed no errors in acquisition
    elec_group = set_electrode_table(nwbfile, info_recording, device)
    add_raw_data(info_recording, raw_data, device, nwbfile, sf)
    add_Units(info_recording, Spikes_info, extracted_units, elec_group,  nwbfile)
    # Add TTL signal data to the NWB file
    add_TTL_data(TTL_info, data_ttl, nwbfile)
    return nwbfile                                       
                          
