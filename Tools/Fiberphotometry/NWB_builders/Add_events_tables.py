# -*- coding: utf-8 -*-
"""
Created on Tue Jul 16 09:50:16 2024

@author: PierreA.DERRIEN
"""
import pynwb
from pynwb import ProcessingModule
import pandas as pd

def generate_event_table(info: dict, data: pd.DataFrame or pd.Series):
    Event_table = pynwb.base.DynamicTable(name = "{} for events - {}".format(info["Data_type"], info["Event_trigger"]), 
                                          description = "Dynamic Table of events extracted on the {} signal around {}".format(info["Data_type"], info["Event_trigger"])
                                          )
    for event in data.columns:
        Event_table.add_column(name = event, description = "{} extracted on {}".format(event, info["Event_trigger"]))
    for idx, row in data.iterrows():
        Event_table.add_row(row.to_dict())
    
    return Event_table

def generate_param_table(info: dict, data: dict):
    Param_table = pynwb.base.DynamicTable(name = "{} for events - {}".format(info["Data_type"], info["Event_trigger"]), 
                                          description = "Dynamic Table of events extracted on the {} signal around {}".format(info["Data_type"], info["Event_trigger"])
                                          )
    for event in data:
        Param_table.add_column(name = event, description = "{} extracted on {}".format(event, info["Event_trigger"]))
   
    Param_table.add_row(data)
    return Param_table

