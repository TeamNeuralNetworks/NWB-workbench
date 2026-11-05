# -*- coding: utf-8 -*-
"""
Created on Thu Mar 21 14:16:28 2024

@author: PierreA.DERRIEN
"""

from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.convert_from_excel import convert_excel_to_nwb
file = r"path\to\Setup_NWB.xlsx"
save_direc = r"Path\to\Save\Folder"
convert_excel_to_nwb(file, save_direc)
