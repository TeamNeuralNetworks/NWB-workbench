# -*- coding: utf-8 -*-
"""
Created on Mon Feb  5 11:05:49 2024

@author: Pierre-Alexis
"""

from doric_system_file import DoricCSV, DoricDORIC
from labview_system_file import LVM_1segments

file_CSV = "path\to\.csv"
file_DORIC = "path\to\.doric"
file_LVM = "path\to.lvm"

openerCSV = DoricCSV()
openerCSV.open_file(file_CSV)

openerDORIC = DoricDORIC()
openerDORIC.open_file(file_DORIC)

openerLVM = LVM_1segments()
openerLVM.open_file(file_LVM)


for opener in [
                openerDORIC, 
               # openerCSV, 
               # openerLVM
               ]:
    print(opener.data)
    print(opener.metaData)
    
    
