# -*- coding: utf-8 -*-
"""
Created on Wed Mar 20 11:51:47 2024

@author: PierreA.DERRIEN
"""

from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.builder_photometry import create_NWB
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.saver import save_NWB

from uuid import uuid4
from dateutil import tz
from datetime import datetime
session_start_time = datetime(2018, 4, 25, 2, 30, 3, tzinfo=tz.gettz("US/Pacific"))


# =============================================================================
# General info about the experiment
# =============================================================================
general_info = {
"session_description":"Mouse exploring an open field",  # required
"identifier":str(uuid4()),  # required
"session_start_time":session_start_time,  # required

"session_id":"session_1234",  # optional
"notes": "Note about the exp", #optional


"protocol": "PAD-2024-01", #optional
"lab":"Bag End Laboratory",  # optional
"institution":"University of My Institution",  # optional
"experimenter":["Baggins, Bilbo",],  # optional
"experiment_description":"I went on an adventure to reclaim vast treasures.",  # optional

"related_publications":"DOI:10.1016/j.neuron.2016.12.011",  # optional
"keywords":["Pain", "Depression"],# optional

"virus": "VTA: Chr2-Floxed",# optional
"surgery":"animal cuffed and injected by stereotaxy",# optional
"pharmacology":"Description of drugs used, including how and when they were administered. Anesthesia(s), painkiller(s), etc., plus dosage, concentration, etc.",

"source_script" :"Script file used to create this NWB file",# optional
"source_script_file_name":"Name of the source_script file",# optional
}


# =============================================================================
# Info about your animal
# =============================================================================
animal_info ={
    "subject_id":"001",  
    "description":"mouse 5",
    "sex":"M",
    "age":"P90D",
    "weight": "0.02 Kg",
    "genotype":"DAT+/-",
    "strain":"DAT-Ires-CRE",
    "species":"Mus musculus",
    "date_of_birth": datetime(2018, 4, 25, 2, 30, 3, tzinfo=tz.gettz("US/Pacific")) 
}
# ======================

channel_info = {
    "Device": {"Name": "Charlet's Team Doric", 
               "Description": "Test description",
               "Manufacturer": "DORIC"
                }, 
    "channels": {"Fiber_1":{"imaging rate": 12000,
                            "indicator": "GCamP",
                            "location": "VTA",
                            "coordinate": (3.5, 0.4, 4.5),
                            "fiber diameter": 400, 
                            "fiber NA": 0.59,
                            "signals": {"GCamP": {"excitation":480.0, 
                                                  "emission":510.0,
                                                  "data": {"raw_signal":"AIn-1 - Raw",
                                                           "demodulated_signal":"AIn-1 - Dem (AOut-1)",
                                                      }
                                                  },
                                        "rCamP": {"excitation":470.0, 
                                                  "emission":500.0,
                                                  "data":{"raw_signal":"AIn-1 - Dem (AOut-1)",
                                                          "demodulated_signal":"AIn-1 - Raw", 
                                                      }
                                                              }
                                        },
                            }
                 }
                }
TTL_info = {"Video Sync":{"data":"AOut-1",
                          "description": "Channel used to synchronize video recording, 1 TTL pulse of 100ms at start of recording"},
            }
# =============================================================================
# Opening your file
# =============================================================================
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.file_openers.doric_system_file import DoricCSV

file_CSV = r"Path\to\CSV_file"
openerCSV = DoricCSV()
openerCSV.open_file(file_CSV)


File = create_NWB(general_info, animal_info, channel_info, openerCSV.data, TTL_info)
save_NWB(File, r'Path\to\Save_file', export=False)