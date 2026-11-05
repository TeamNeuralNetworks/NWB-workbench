# -*- coding: utf-8 -*-
"""
Created on Mon Apr 29 14:44:17 2024

@author: PierreA.DERRIEN
"""
from pathlib import Path
import pynwb
from pynwb import NWBHDF5IO
import pandas as pd
import numpy as np
import time
from General_tools.printers import recursive_printer
import sys
import platform

class NWB_to_Dataframe_converter():
    
    def __init__(self, path):
        """
        Initialize the converter with the path to the NWB file.
        """
        try:
            self.file_path = Path(path).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as e:
            raise FileNotFoundError(f"File not found or inaccessible: {path}")
        self.loaded = False
        
        # Platform-specific checks
        if platform.system() == 'Darwin':  # macOS
            if not self.file_path.is_file():
                raise FileNotFoundError(f"File not accessible on macOS: {self.file_path}")
        elif platform.system() == 'Windows':  # Windows
            if len(str(self.file_path)) > 260:  # Windows PATH length limitation
                raise OSError("File path exceeds Windows maximum path length")
        
    def load_NWB(self):
        """
        Load the NWB file and extract acquisition data, timestamps, and events.
        """
        start_time = time.time()
        # Convert to string representation for NWBHDF5IO
        with NWBHDF5IO(str(self.file_path), mode="r") as io2:
            self.file_data = io2.read()
            self.acqui_data = {}
            self.timestmps_data = {}
            self.Events = {}
            for acqui_name in self.file_data.acquisition: 
                self.acqui_data[acqui_name] = np.array(self.file_data.acquisition[acqui_name].data)
                self.timestmps_data[acqui_name] = np.array(self.file_data.acquisition[acqui_name].timestamps)
            for processing in self.file_data.processing:
                if processing == "Events":
                    for Events_table in self.file_data.processing["Events"].data_interfaces:
                        self.Events[Events_table] = self.file_data.processing["Events"].data_interfaces[Events_table].to_dataframe()
            self.loaded = True
        print("Loading time: %s seconds" % (time.time() - start_time))
            
    def convert(self):
        """
        Convert the NWB file to a DataFrame and retrieve metadata and signals.
        """
        start_time = time.time()
        self.retrieve_metaData()
        self.retrieve_signals()
        print("Conversion time: %s seconds" % (time.time() - start_time))
        
    def retrieve_metaData(self):
        """
        Retrieve metadata from the NWB file.
        """
        if self.loaded == False:
            print("NWb not loaded before - automatic loading used")
            self.load_NWB()
        assert type(self.file_data) == pynwb.file.NWBFile , " file_data is not a NWB file"
        
        device_dic = {}
        for device in self.file_data.devices:
            device_dic[device] = {"name": device,
                                  "manufacturer": self.file_data.devices[device].manufacturer,
                                  "description": self.file_data.devices[device].description}
            
        imaging_plane_dic = {}
        for imaging_plane in self.file_data.imaging_planes:
            y = self.file_data.imaging_planes[imaging_plane].fields
            x= self.file_data.imaging_planes[imaging_plane].optical_channel
            optic_ch = {}
            for k in x:
                optic_ch[k.name] = k.fields
                
            y["optical_channel"] = optic_ch
            y['device'] =y['device'].name
            imaging_plane_dic[imaging_plane] = y
            
            
        Subject_infos = {"Age": self.file_data.subject.age,
                         "Age_reference": self.file_data.subject.age__reference,
                         "Date_of_birth": self.file_data.subject.date_of_birth,
                         "Description": self.file_data.subject.description, 
                         "Genotype": self.file_data.subject.genotype, 
                         "Sex": self.file_data.subject.sex, 
                         "Species": self.file_data.subject.species, 
                         "Strain": self.file_data.subject.strain,
                         "Subject_id": self.file_data.subject.subject_id,
                         "Weight": self.file_data.subject.weight
                         }
        
        self.metaData = {"Devices": device_dic,
                         "Subject": Subject_infos, 
                         "Imaging_planes": imaging_plane_dic,
                         "Experiment_description": self.file_data.experiment_description,
                         "Experimenter": self.file_data.experimenter,
                         "Creation date": self.file_data.file_create_date,
                         "Identifier": self.file_data.identifier,
                         "Institution": self.file_data.institution,
                         "Lab": self.file_data.lab,
                         "Notes": self.file_data.notes,
                         "Pharmacology": self.file_data.pharmacology,
                         "Protocol": self.file_data.protocol,
                         "Related_publications": self.file_data.related_publications,
                         "Session_description": self.file_data.session_description,
                         "Session_id": self.file_data.session_id,
                         "Session_start_time": self.file_data.session_start_time,
                         "Source_script": self.file_data.source_script,
                         "Surgery": self.file_data.surgery, 
                         "Timestamps_reference_time": self.file_data.timestamps_reference_time,
                         "Virus": self.file_data.virus,
                        }
        
    def retrieve_signals(self):
        """
        Retrieve signals from the NWB file and convert them to a DataFrame.
        """
        try :
            if self.loaded == False:
                print("NWb not loaded before - automatic loading used")
                self.load_NWB()
            self.converted_data = pd.DataFrame()
            acqui_metaData = {}
            for acqui_name in self.file_data.acquisition: 
                acquisition = self.file_data.acquisition[acqui_name]
                acqui_metaData[acqui_name] = acquisition.fields
                data = pd.Series(self.acqui_data[acqui_name])
                data.index = self.timestmps_data[acqui_name]
                data.index.name = "Timestamps"
                del acqui_metaData[acqui_name]['data']
                del acqui_metaData[acqui_name]['timestamps']
                self.converted_data[acqui_name] = data
            try :
                self.metaData["Acquisition"] = acqui_metaData      
            except: 
                self.retrieve_metaData()
                self.metaData["Acquisition"] = acqui_metaData   
                
        except Exception as e: 
            print(e)
        
    def get_data(self): 
        """
        Get the converted data as a DataFrame.
        """
        try: 
            assert type(self.converted_data) == pd.DataFrame
        except Exception as e: 
            print(e)
            self.retrieve_signals()
        return self.converted_data
    
    def get_metaData(self): 
        """
        Get the metadata as a dictionary.
        """
        try: 
            assert type(self.metaData) == dict
        except Exception as e: 
            print(e)
            self.retrieve_metaData()
        return self.metaData
    
    def get_unaligned_data(self):
        """
        Get the unaligned data and timestamps.
        """
        return {"data": self.acqui_data, "timestamps": self.timestmps_data}
        
    def print_metaData(self):
        """
        Print the metadata using the recursive printer.
        """
        recursive_printer(self.metaData)
        
if __name__ == "__main__":
    # Example of cross-platform path handling
    path = Path("Path/to/File")  # Forward slashes work on both platforms
    converter = NWB_to_Dataframe_converter(path=path)
    converter.convert()
    data = converter.get_data()
    converter.print_metaData()

