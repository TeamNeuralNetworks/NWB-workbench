# -*- coding: utf-8 -*-
"""
Created on Mon Feb  5 11:13:17 2024

@author: Pierre-Alexis
"""
import lvm_read
import pandas as pd

class LVM_1segments ():
    "Open LVM file that have data in only one sgements an get the metadata from the channel"
    def open_file(self, path:str):
        
        """ Simple file opener for the CSV obtained with the DORIC recording systems
        Parameters:
        - path (str): The path to the CSV file.
        Returns:
        None
        """
        
        # this read the file first segment and put it in a dataframe with columns
        file = lvm_read.read(path, read_from_pickle=False)
        self.data = pd.DataFrame(data=file[0]["data"], columns=file[0]["Channel names"][:-1])
        metaData = dict()
        for key , info in file.items():
            if type(key) != int:
                metaData[key] = info
            else:
                for nkey, ninfo in info.items():
                    if nkey != "data":
                        metaData[nkey+"_Seg:"+str(key)] = ninfo
        
       
        self.metaData = metaData