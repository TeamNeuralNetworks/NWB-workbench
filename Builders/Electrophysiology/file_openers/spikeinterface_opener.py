# -*- coding: utf-8 -*-
"""
Created on Sat Feb 28 13:56:13 2026

@author: Pierre
"""
import pandas as pd

from Builders.General.events_toolbox import generate_event_markers_dict_from_TTL, extract_trials_from_event_markers


class spikeinterface_opener:
    
    def __init__(self, filepath:str, device_info:dict, TTL_info=None, aditional_trial_metric=None, **kwargs):
        
        self.event_markers = None
        self.trials_df = None
        self.device_info = device_info
        self.trials_df_description = None
        self.data = None
        self.data_sampling_rate = None
        self.accelerometer = None
        self.accelerometer_sampling_rate = None
        self.TTL = None
        self.TTL_sampling_rate = None
        
        self.TTL_info = None
        self.aditional_trial_metric = None
        self.filepath = filepath
        self.TTL_info = TTL_info
        self.aditional_trial_metric = aditional_trial_metric
        #TODO if path is a directory find all recordings and concatenate them
        self.extract_data()
        self.convert_TTL()
    
    def open_file(self):
        """
        this class is a base class each specifc format need to build a child class with its specifc self.open_file based on recording_type
        """
        pass
    
    def extract_data(self):
        
        """ Simple file opener for the rhd obtained with the Intan recording systems
        Parameters:
        - path (str): The path to the rhb file.
        Returns:
        None
        """
        self.open_file()
        
        self.data_sampling_rate = self.data.get_sampling_frequency()
        self.data = self.convert_spikeinterface_recording_to_dataframe(self.data)
        
        if self.accelerometer is not None:
            self.accelerometer_sampling_rate = self.accelerometer.get_sampling_frequency()
            self.accelerometer = self.convert_spikeinterface_recording_to_dataframe(self.accelerometer)
        
        if self.TTL_info is not None:
            self.TTL_sampling_rate = self.TTL.get_sampling_frequency()
            self.TTL = self.convert_spikeinterface_recording_to_dataframe(self.TTL)
            
    def convert_TTL(self):
        
        if self.TTL is not None:
            self.event_markers = generate_event_markers_dict_from_TTL(self.TTL, self.TTL_info, self.TTL_sampling_rate)
            if 'trials' in self.event_markers.keys():
                self.trials_df, aditional_time_trial_metric, self.trials_df_description  = extract_trials_from_event_markers(self.event_markers, self.aditional_trial_metric)
                self.event_markers.update(aditional_time_trial_metric)
            
    
    def convert_spikeinterface_recording_to_dataframe(self, spikeinterface_recording):
        recording_df = {}
        for channel_id in spikeinterface_recording.channel_ids:
            assert channel_id not in recording_df.keys()
            recording_df[channel_id] = spikeinterface_recording.get_traces(channel_ids=[channel_id]).flatten()
        
        return pd.DataFrame(recording_df)