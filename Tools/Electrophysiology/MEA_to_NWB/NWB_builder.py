# -*- coding: utf-8 -*-
"""
Created on Wed Feb  5 10:35:39 2025

@author: PierreA.DERRIEN
"""
from glob import glob
import pandas as pd
import numpy as np
from spikeinterface.extractors import read_mcsh5
from alive_progress import alive_bar
from phylib.utils import Bunch
from phylib.io.model import load_model
from phylib.io.array import SpikeSelector
import os
from Functionnal_Imaging_Electrophysiology.electrophysiology.MEA_to_NWB.Utility_NWB import create_NWB
from Functionnal_Imaging_Electrophysiology.fiberphotometry_tools.NWB_builders.saver import save_NWB

def extract_waveform_data(waveform_dict, cluster_id):
    """
    Extract waveform data for a specific cluster from the waveform dictionary.

    Parameters:
    - waveform_dict: dict
        Dictionary containing waveform data functions.
    - cluster_id: int
        The cluster ID for which to extract waveform data.

    Returns:
    - dict
        Extracted waveform data including data array, channel IDs, labels, positions, masks, and alpha transparency.
    """
    if 'waveforms' not in waveform_dict:
        raise ValueError("The waveform dictionary must contain the key 'waveforms'.")
    print("Run")
    waveform_function = waveform_dict['waveforms']
    bunch = waveform_function(cluster_id)
    
    data = {
        'data': bunch.data,
        'channel_ids': bunch.channel_ids,
        'channel_labels': bunch.get('channel_labels', []),
        'channel_positions': bunch.channel_positions,
        'masks': bunch.masks,
        'alpha': bunch.alpha
    }
    
    return data


def get_waveforms_for_cluster(model, cluster_id, n_spikes_waveforms=1000):
    spike_ids = model.selector(n_spikes_waveforms, [cluster_id], subset_chunks=True)
    channel_ids = model.get_best_channels(cluster_id)
    channel_labels = model._get_channel_labels(channel_ids)
    data = model.get_waveforms(spike_ids, channel_ids)
    if data is not None:
        data = data - np.median(data, axis=1)[:, np.newaxis, :]
    data = model.raw_data_filter.apply(data, axis=1) if data is not None else data
    return Bunch(
        data=data,
        channel_ids=channel_ids,
        channel_labels=channel_labels,
        channel_positions=model.channel_positions[channel_ids],
    )

def get_mean_waveforms_for_cluster(model, cluster_id, n_spikes_waveforms=1000):
    bunch = get_waveforms_for_cluster(model, cluster_id, n_spikes_waveforms)
    if bunch.data is not None:
        bunch.data = bunch.data.mean(axis=0)[np.newaxis, ...]
    bunch.alpha = 1.0
    return bunch

def generate_waveform_dict(model):
    # Ensure the selector is set up
    model.selector = SpikeSelector(
        get_spikes_per_cluster=model.spikes_per_cluster,
        spike_times=model.spike_samples,
        chunk_bounds=model.traces.chunk_bounds if model.traces else [0.0, model.spike_samples[-1] + 1],
        n_chunks_kept=model.n_chunks_kept
    )
    waveform_functions = {
        'waveforms': lambda cluster_id: get_waveforms_for_cluster(model, cluster_id),
        'mean_waveforms': lambda cluster_id: get_mean_waveforms_for_cluster(model, cluster_id),
    }
    return waveform_functions



def extract_unit(Spikes, info, waveform_dict, model):
    
    Units = {}
    with alive_bar(len(Spikes["Cluster"].unique())) as bar:
        for Unit in Spikes["Cluster"].unique():
            Units[Unit] = {}
            Units[Unit]["Spikes"] = Spikes[Spikes["Cluster"]== Unit]
            Units[Unit]["Main_elec"] = "Ch"+str(info["cluster_info"][info["cluster_info"]["cluster_id"]== Unit]["ch"].tolist()[0])
            # Units[Unit]["Waveform"] = extractWF(Spikes[Spikes["Cluster"]== Unit]["Times"], raw_data, Units[Unit]["Main_elec"] )
            # Units[Unit]["Waveform"] =extract_waveform_data(waveform_dict, cluster_id = [Unit])
            Units[Unit]["Waveform"] = model.get_waveforms(model.get_cluster_spikes(Unit))
            # break
            bar()
    return Units



def process_MEA_data(folder, raw_data, general_info, device_info, TTL = {}):
    files= glob(folder+("/*"))
    info = {}
    for file in files:
        name = file.split("\\")[-1].split(".")[0]
        extension = file.split("\\")[-1].split(".")[1]
        if extension == "npy":
            info[name] = np.load(file, allow_pickle=True)
        elif extension == "tsv":
            info[name] = pd.read_csv(file,sep='\t')
        elif extension == "py":   
            # Usage
            model = load_model(file)# Your model instance here
            model.spikes_per_cluster =1000
            model.n_chunks_kept = 20
            waveform_dict = generate_waveform_dict(model)
    
    raw_data =  read_mcsh5(raw_data)
    sample_rate = raw_data.get_sampling_frequency()
    cluster_to_del = info["cluster_info"][info["cluster_info"]["Type"] == "N"]["cluster_id"].tolist()
    
    Spikes = pd.DataFrame()
    Spikes["Times"] = info["spike_times"]/sample_rate
    Spikes["Cluster"] = info["spike_clusters"]
    Spikes["X_pos"] = pd.DataFrame(info["spike_positions"])[0]
    Spikes["Y_pos"] = pd.DataFrame(info["spike_positions"])[1]
    # Spikes["Position"] = info["spike_positions"]
    
    for del_clust in cluster_to_del:
        Spikes = Spikes[Spikes["Cluster"]!=del_clust]
        
    animal_info = {}
    Extracted_units = extract_unit(Spikes, info, waveform_dict, model) 
    return create_NWB(general_info, animal_info, info, Spikes, device_info, raw_data, Extracted_units,  sample_rate, TTL)



if __name__ == "__main__":
    from uuid import uuid4
    from dateutil import tz
    from datetime import datetime
    session_start_time = datetime(2018, 4, 25, 2, 30, 3, tzinfo=tz.gettz("US/Pacific"))

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
    
    
    device_info = {"Name": "MEA multichannel",
                   "Description": "MEA system of A.Charlet",
                   "Manufacturer": "multichannel"}
    
    folder_sorted_data = glob(r"path\to\sorted\folder\from\kilosort4")  #this folder contain should contains a lot of .npy and .tsv file
    raw_path = glob(r"path\to\raw_data\.h5")  
    NWB = process_MEA_data(folder_sorted_data, raw_path, general_info , device_info, TTL = {})
    save_NWB(NWB, r'path\to\save\file', export=False)

    
    

