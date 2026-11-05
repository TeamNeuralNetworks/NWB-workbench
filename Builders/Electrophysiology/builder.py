# -*- coding: utf-8 -*-
"""
Created on Fri Mar  8 14:25:54 2024

@author: PierreA.DERRIEN
"""
from pynwb.ecephys import ElectricalSeries
from pynwb import TimeSeries
import numpy as np

def add_electrode(device_info, nwbfile, index=0):
    """
    Adds electrod to an NWB file based on provided channel information.

    Parameters
    ----------
    device_info : dict
        A dictionary containing channel information. Each channel should have entries for
    nwbfile : pynwb.NWBFile
        The NWB file object to which the channels and TimeSeries data will be added.

    Returns
    -------
    None
    """
    
    # Create a device and add it to the NWB file
    device = nwbfile.create_device(
            name=device_info["name"],
            description=device_info["description"],
            manufacturer=device_info["manufacturer"],
            model_name=device_info["model_name"],
            serial_number=device_info["serial_number"],
            )
    
    electrode_group = nwbfile.create_electrode_group(
                                                        name=device_info["name"],
                                                        description=device_info["description"],
                                                        device=device,
                                                        location=device_info["location"],
                                                        )
    
    index_list = []
    for channel_idx, channel_info in device_info['channel_info'].iterrows():
        nwbfile.add_electrode(
                            group=electrode_group,
                            contact_ids=channel_info['contact_ids'],
                            shank_id=channel_info['shank_id'],
                            location=device_info["location"],
                            rel_x=float(channel_info['x_position']),
                            rel_y=float(channel_info['y_position']),
                            impedance=channel_info['impedance'],
                            bad_channel=channel_info['bad_channel'],
                            )
        index_list.append(index)
        index += 1        
        
    return index_list

def add_electrode_data(device_info, data, nwbfile, starting_time=None, sampling_rate=None, timestamps=None):
    
    assert sampling_rate is not None or timestamps is not None
    
    if sampling_rate is not None and timestamps is not None:
        if len(np.unique(np.diff(timestamps)).round(6)) == 1:
            timestamps = None
            sampling_rate = float(sampling_rate)
        else:
            sampling_rate = None
            starting_time = None
            
    if timestamps is not None:
        starting_time = None
        
    electrode_table = nwbfile.create_electrode_table_region(region=device_info['electrode_table_index_list'],
                                                            description=device_info['description'])
    
    assert all([channel_id in data.columns for channel_id in device_info['channel_info']['contact_ids'].tolist()]), "Not all channel entered in Electrophysiology_signals was found in the electrophysiology file"
    data = data[device_info['channel_info']['contact_ids']] #reorder colmun order to insure that indexes match between ElectricalSeries data and electrod_table
        
    data = ElectricalSeries(
                    name="raw_signal "+ device_info["name"],
                    electrodes=electrode_table,
                    data=np.array(data),
                    conversion=device_info['conversion_factor_to_V'],
                    starting_time=starting_time,
                    rate=sampling_rate,
                    timestamps=timestamps,
                    description=f'raw recording of {device_info["name"]} electrodes',
                )
    nwbfile.add_acquisition(data)

def add_accelerometer(data, nwbfile, starting_time=None, sampling_rate=None, timestamps=None):
    """
    Add accelerometer auxiliary channels as an NWB TimeSeries.

    This function is intentionally permissive:
    - If there is no accelerometer data, it simply returns.
    - If accelerometer data exists but no timing information is available, it skips it
      instead of crashing the whole NWB conversion.
    - If timing exists, it stores the signal using either regular sampling rate or
      explicit timestamps.
    """
    if data is None:
        return

    arr = np.asarray(data)
    if arr.size == 0:
        return

    if sampling_rate is None and timestamps is None:
        print("Skipping accelerometer: no sampling rate or timestamps available")
        return

    if sampling_rate is not None and timestamps is not None:
        timestamps_arr = np.asarray(timestamps, dtype=float)
        if timestamps_arr.size > 1 and len(np.unique(np.diff(timestamps_arr).round(6))) == 1:
            # Regular timestamps: store compactly as starting_time + rate.
            if starting_time is None:
                starting_time = float(timestamps_arr[0])
            timestamps = None
            sampling_rate = float(sampling_rate)
        else:
            # Irregular timestamps: keep timestamps and do not also pass rate.
            sampling_rate = None
            starting_time = None

    if timestamps is not None:
        starting_time = None

    ts = TimeSeries(
                    name='raw_signal acceleromaters',
                    description='raw acceleromaters recordings',
                    data=arr,
                    unit="acceleration",
                    starting_time=starting_time,
                    rate=sampling_rate,
                    timestamps=timestamps,
                )
    nwbfile.add_acquisition(ts)

def add_electrophysiological_data(nwbfile, data):
    """
    Creates an NWB file with provided general information, animal information, channel data, and optional TTL signal data.

    Parameters
    ----------
    data must contain:
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

    
    if data.data is not None:
        nwbfile.add_electrode_column(name="contact_ids", description="label of electrode on the intan file")
        nwbfile.add_electrode_column(name="shank_id", description="id of the shank for current channel")
        nwbfile.add_electrode_column(name="impedance", description="impendance of the channel")
        nwbfile.add_electrode_column(name="bad_channel", description="wheter channel is bad")
        
        index = 0
        for device_name, current_device_info in data.device_info.items():
            data.device_info[device_name]['electrode_table_index_list'] = add_electrode(current_device_info, nwbfile, index=index)
            index = data.device_info[device_name]['electrode_table_index_list'][-1] + 1
        del index
        
        for device_name, current_device_info in data.device_info.items():
            if not 'data_sampling_rate' in dir(data):
                data.data_sampling_rate = None
            if not 'data_timestamps' in dir(data):
                 data.data_timestamps = None  
            if not 'data_starting_time' in dir(data):
                  data.data_starting_time = None
            add_electrode_data(device_info=current_device_info,
                                data=data.data, 
                                nwbfile=nwbfile, 
                                starting_time=data.data_starting_time, 
                                sampling_rate=data.data_sampling_rate, 
                                timestamps=data.data_timestamps)
                                
    accelerometer = getattr(data, 'accelerometer', None)
    if accelerometer is not None:
        if not 'accelerometer_sampling_rate' in dir(data):
            data.accelerometer_sampling_rate = None
        if not 'accelerometer_timestamps' in dir(data):
             data.accelerometer_timestamps = None  
        if not 'accelerometer_starting_time' in dir(data):
               data.accelerometer_starting_time = None 
               
        add_accelerometer(data=accelerometer, 
                          nwbfile=nwbfile, 
                          starting_time=data.accelerometer_starting_time,
                          sampling_rate=data.accelerometer_sampling_rate,
                          timestamps=data.accelerometer_timestamps)

    return nwbfile
