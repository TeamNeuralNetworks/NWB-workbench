# -*- coding: utf-8 -*-
"""
Created on Mon Feb  5 10:36:52 2024

@author: Pierre-Alexis
"""
from spikeinterface.extractors import read_intan
from Builders.Electrophysiology.file_openers.spikeinterface_opener import spikeinterface_opener

class Intan_opener(spikeinterface_opener):
    
    def open_file(self):
        
        self.data = read_intan(self.filepath, stream_name='RHD2000 amplifier channel')
        
        try: 
            self.accelerometer = read_intan(self.filepath, stream_name='RHD2000 auxiliary input channel')
        except:
            print(f'Unable to extract accelerometer data from {self.filepath}')
            
        if self.TTL_info is not None:
            self.TTL = read_intan(self.filepath, stream_name='USB board digital input channel')

        
