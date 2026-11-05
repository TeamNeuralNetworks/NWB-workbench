# -*- coding: utf-8 -*-
"""
Created on Fri Mar  8 15:18:05 2024

@author: PierreA.DERRIEN
"""

from pynwb import NWBHDF5IO

def save_NWB(NWB_file, filepath, export = False):
    if export == False:
        with NWBHDF5IO(filepath+".nwb", 'w') as io:
            io.write(NWB_file)

    else:
        with NWBHDF5IO(filepath+".nwb", mode="r") as read_io:    
            read_nwbfile = read_io.read()
            with NWBHDF5IO(filepath+".nwb", mode="w") as export_io:
                export_io.export(src_io=read_io, nwbfile=read_nwbfile)