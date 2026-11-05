# -*- coding: utf-8 -*-
"""
Created on Fri Mar  8 14:25:54 2024

@author: PierreA.DERRIEN
"""

import numpy as np

from pynwb import NWBHDF5IO, NWBFile, TimeSeries
from pynwb.file import Subject
from pynwb.image import ImageSeries
from pynwb.ophys import OpticalChannel


def add_channel(info, data, device, nwbfile):
    """
    Adds optical channels and corresponding TimeSeries data to an NWB file based on provided channel information.
    Old function based on imaging rate and not timestamps - Unused in the create NWB function
    Parameters
    ----------
    info : dict
        A dictionary containing channel information. Each channel should have entries for:
          raw and demodulated signals, 
          emission lambda, fiber diameter, fiber NA, 
          imaging rate, indicator, location, coordinates, 
          and (optionally) isobestic data.
    data : dict
        A dictionary or NumPy array containing the signal data.
    device : pynwb.device.Device
        The device associated with the NWB file.
    nwbfile : pynwb.NWBFile
        The NWB file object to which the channels and TimeSeries data will be added.

    Returns
    -------
    None
    """
    for channel_name, channel_info in info["channel"].items():
        
        if channel_info["raw_signal"] != None or channel_info["demod_signal"] != None:
            # Creation of an optical channel for modulated and demodulated signal
            optical_channel = OpticalChannel(name='Optical_channel for signal data of '+str(channel_name),
                                             description='An optical channel representing fiber photometry data for signal data of '+ str(channel_name),
                                             emission_lambda=float(channel_info["emission lambda"])  # replace with the actual emission wavelength
                                            )
            # Create an ImagingPlane object for the fiber photometry raw and demodulated channel signal
            imaging_plane = nwbfile.create_imaging_plane(name='imaging_plane for signal data of '+str(channel_name),
                                                         optical_channel=optical_channel,
                                                         description='Imaging plane for fiber photometry for signal data of '+str(channel_name)+ " - Optic fiber diameter: "+str(channel_info["fiber diameter"])+ " - NA: "+str(channel_info['fiber NA']),
                                                         device=device,
                                                         # excitation_lambda=float(channel_info["excitation lambda"]),  # replace with the actual excitation wavelength
                                                         excitation_lambda=0.0, 
                                                         imaging_rate=float(channel_info["imaging rate"]),  # replace with the actual imaging rate
                                                         indicator=channel_info["indicator"],  # replace with the actual indicator used
                                                         location=channel_info["location"],  # replace with the actual location
                                                         origin_coords = channel_info["coordinate"],
                                                         origin_coords_unit = "millimeters",
                                                        )
            # Assume 'data' is a NumPy array or list with your signal data, and 'timestamps' is a list of timestamps for each data point
            for signal_name in ["raw_signal", "demod_signal"]:
                if channel_info[signal_name] != None:
                    photometry_ts = TimeSeries(
                        name= signal_name +" "+ channel_name,
                        data=data[channel_info[signal_name]].tolist(),
                        unit='V',  # arbitrary units
                        rate=float(channel_info["imaging rate"]),

                        # imaging_plane=imaging_plane
                    )
                    nwbfile.add_acquisition(photometry_ts)
            
        if channel_info["raw_iso"] != None or channel_info["demod_iso"] != None:
            channel_name = channel_name.replace("Isobestic ", "")
            # Creation of an optical channel for modulated and demodulated signal
            optical_channel = OpticalChannel(name='Optical_channel for isobestic data of ' +str(channel_name),
                                             description='An optical channel representing fiber photometry data for isobestic data of '+ str(channel_name),
                                             emission_lambda=float(channel_info["isobestic emission"])  # replace with the actual emission wavelength
                                            )
            # Create an ImagingPlane object for the fiber photometry raw and demodulated channel signal
            imaging_plane = nwbfile.create_imaging_plane(name='imaging_plane for isobestic data of '+str(channel_name),
                                                         optical_channel=optical_channel,
                                                         description='Imaging plane for fiber photometry for isobestic data of ' +str(channel_name)+ " - Optic fiber diameter: "+str(channel_info["fiber diameter"])+ " - NA: "+str(channel_info['fiber NA']),
                                                         device=device,
                                                         excitation_lambda=float(channel_info["isobestic excitation"]),  # replace with the actual excitation wavelength
                                                         imaging_rate=float(channel_info["imaging rate"]),  # replace with the actual imaging rate
                                                         indicator=channel_info["indicator"],  # replace with the actual indicator used
                                                         location=channel_info["location"],  # replace with the actual location
                                                         origin_coords = channel_info["coordinate"],
                                                         origin_coords_unit = "millimeters",
                                                        )
            # Assume 'data' is a NumPy array or list with your signal data, and 'timestamps' is a list of timestamps for each data point
            for signal_name in ["raw_iso", "demod_iso"]:
                if channel_info[signal_name] != None:
                    photometry_ts = TimeSeries(
                        name= signal_name +" "+ channel_name,
                        data=data[channel_info[signal_name]].tolist(),
                        unit='V',  # arbitrary units
                        rate=float(channel_info["imaging rate"]),
                        # imaging_plane=imaging_plane
                    )
                    nwbfile.add_acquisition(photometry_ts)
    
def add_channel_data(info, data, device, nwbfile):
    """
    Adds optical channels and corresponding TimeSeries data to an NWB file based on provided channel information.

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
    for channel_name, channel_info in info["channels"].items():
        list_optic_channel= []
        list_indicator = str()
        for signal, signal_info in channel_info["signals"].items():
            optical_channel = OpticalChannel(name='Optical_channel for signal data of '+str(signal) + " from " + str(channel_name),
                                             description='An optical channel representing fiber photometry data for signal data of '+str(signal) + " from " + str(channel_name),
                                             emission_lambda=float(signal_info["emission"])  # replace with the actual emission wavelength
                                            )
            list_optic_channel.append(optical_channel)
            list_indicator = list_indicator + str(signal) + " - excitation lambda: "+ str(signal_info["excitation"])+" | "
            for signal_name , signal_col in signal_info["data"].items():
                if type(signal_col) != type(np.NaN): 
                    photometry_ts = TimeSeries(name= str(signal_name)+" "+str(signal) +" "+ str(channel_name),
                                               data=data[signal_col].tolist(),
                                               unit='V',  # arbitrary units
                                               # rate=float(channel_info["imaging rate"]),
                                               timestamps = data.index.tolist()
                                               )
                    nwbfile.add_acquisition(photometry_ts)
        imaging_plane = nwbfile.create_imaging_plane(name='imaging_plane for '+str(channel_name),
                                                     optical_channel=list_optic_channel,
                                                     description='Imaging plane for fiber photometry for '+str(channel_name) +' of '+str(channel_name)+" - Optic fiber diameter: "+str(channel_info["fiber diameter"])+ " - NA: "+str(channel_info['fiber NA']),
                                                     device=device,
                                                     excitation_lambda=0.0,  # replace with the actual excitation wavelength
                                                     imaging_rate=float(channel_info["imaging rate"]),  # replace with the actual imaging rate
                                                     indicator=list_indicator,  # replace with the actual indicator used
                                                     location=channel_info["location"],  # replace with the actual location
                                                     origin_coords = channel_info["coordinate"],
                                                     origin_coords_unit = "millimeters",
                                                    )
       
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
    



def create_NWB(general_info, animal_info, channel_info, data, TTL_info = {}):
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
        name=channel_info["Device"]["Name"],
        description=channel_info["Device"]["Description"],
        manufacturer=channel_info["Device"]["Manufacturer"],
        )

    # Add channel data to the NWB file  
    # add_channel(channel_info, data, device, nwbfile) #Old function based on imaging rate => assumed no errors in acquisition
    add_channel_data(channel_info, data, device, nwbfile)
    
    # Add TTL signal data to the NWB file
    add_TTL_data(TTL_info, data, nwbfile)
    return nwbfile                                       
                                                