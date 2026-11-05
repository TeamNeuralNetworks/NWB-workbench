from GUI.project_paths import to_excel as _pp_to_excel
# -*- coding: utf-8 -*-
"""
Created on Wed Apr  1 12:00:56 2026

@author: Charlie.Jaouen
"""
from GUI.project_paths import json_dump as _pp_json_dump, json_load as _pp_json_load, read_excel as _pp_read_excel
import pandas as pd
import json
import os
from pathlib import Path
import pickle
from uuid import uuid4
from datetime import datetime
from Tools.General.event_alignment import alignment_signature, normalize_alignment


def _event_alignment_offsets(event_alignment):
    """Return signed offsets from either the current or legacy field names."""
    event_alignment = event_alignment or {}
    if "start_offset_s" in event_alignment:
        start_offset = float(event_alignment["start_offset_s"])
    else:
        start_offset = -float(event_alignment["pad_before_s"])
    if "end_offset_s" in event_alignment:
        end_offset = float(event_alignment["end_offset_s"])
    else:
        end_offset = float(event_alignment["pad_after_s"])
    return start_offset, end_offset


def _cache_compatible_data_dict(data_dict):
    """Map canonical offsets to the legacy cache columns without changing the window."""
    normalized = dict(data_dict or {})
    has_canonical_offsets = (
        "start_offset_s" in normalized or "end_offset_s" in normalized
    )
    marker = normalized.get("aligned_to_event", normalized.get("event_marker"))
    alignment_enabled = marker not in (None, False, "", "False", "None")
    if has_canonical_offsets and alignment_enabled:
        start_offset, end_offset = _event_alignment_offsets(normalized)
        legacy_before = -start_offset
        normalized["pad_before_s"] = 0.0 if legacy_before == 0 else legacy_before
        normalized["pad_after_s"] = end_offset
    normalized.pop("start_offset_s", None)
    normalized.pop("end_offset_s", None)
    return normalized

class FilteredConditionFileManager:
    
    def __init__(self,  
                 condition_filter,
                 data_types,
                 nwb_data_path,
                 project_path,
                 align_to_events,
                 event_marker_to_align_dict):
        
        self.project_path = Path(project_path) 
        self.save_path = self.project_path / r'Data\Preprocessed_filtered_data'
        
        self.data_types = {data_type: current_nwb_data_path for data_type, current_nwb_data_path in zip(data_types, nwb_data_path)}
        self.filter_name = condition_filter['_conditions_name']
        self.conditions = [condition for condition in condition_filter.keys() if isinstance(condition_filter[condition], dict)]
        
        self.preprocessed_inventory_df = self.get_preprocessed_inventory(project_path)
        
        self.data_df = self.initialize_data_df(condition_filter,
                                            align_to_events, 
                                            event_marker_to_align_dict)
        
        
        self.aligned_to_event = list(set(self.data_df['aligned_to_event'].tolist()))
        if len(self.aligned_to_event) == 1:
            self.aligned_to_event = self.aligned_to_event[0]
    
    def get_color(self, condition, filter_name=None):
        
        color = self.data_df[self.data_df['condition'] == condition]
        if filter_name is not None:
            color = self.data_df[self.data_df['filter_name'] == filter_name]
        
        color = color['color'].unique()
        assert len(color) == 1
        
        return color[0]
        
    def get_data_types(self):
        if isinstance(self.data_types, dict):
            return list(self.data_types.keys())
        elif isinstance(self.data_types, set):
            return list(self.data_types)
        else:
            raise ValueError
            
    def get_data_nwb_path(self, data_type=None):
        if data_type is None:
            return self.data_types
        else:
            return self.data_types[data_type]
    
    def get_conditions(self):
        return self.conditions
    
    def compare_filter_dict(self, filter_dict1, filter_dict2):
        match = True
        for key in filter_dict1.keys():
            if key != 'color':
                match = match and filter_dict1[key] == filter_dict2[key]
        return match            
    
    def check_if_precomputed_data_on_disk(self, data_dict):
        data_dict = _cache_compatible_data_dict(data_dict)
        mask = pd.Series(True, index=self.preprocessed_inventory_df.index)
        for col, val in data_dict.items():
            if col not in self.preprocessed_inventory_df.columns:
                return False, None
            mask &= self.preprocessed_inventory_df[col].isna() if val is None else self.preprocessed_inventory_df[col] == val

        saved_on_disk = mask.any()
        if saved_on_disk:
            row = self.preprocessed_inventory_df[mask]
            assert len(row) == 1
            row = row.iloc[0]
        else:
            row = None
        return saved_on_disk, row
    
    def initialize_data_df(self, condition_filter, align_to_events, event_marker_to_align_dict):
        
        data = []
        for condition in self.conditions:
            color = condition_filter[condition]['color']
            for data_name in self.get_data_types():
                
                data_dict = {'filter_name': self.filter_name, 
                             'condition': condition, 
                             'data_type': data_name, 
                             'nwb_files_nb': len(condition_filter[condition]['file_path_list']),
                             }
                if align_to_events:
                    start_offset, end_offset = _event_alignment_offsets(
                        event_marker_to_align_dict
                    )
                    data_dict['aligned_to_event'] = event_marker_to_align_dict["event_marker"]
                    data_dict['edge'] = event_marker_to_align_dict["edge"]
                    alignment = normalize_alignment(event_marker_to_align_dict)
                    data_dict['_alignment_signature'] = alignment_signature(alignment)
                    data_dict['end_event_marker'] = alignment['end_event_marker']
                    data_dict['end_edge'] = alignment['end_edge']
                    # Retain the established inventory columns so old caches
                    # remain reusable for the same scientific time window.
                    data_dict['pad_before_s'] = -start_offset
                    data_dict['pad_after_s'] = end_offset
                else:
                    data_dict['aligned_to_event'] = False
                    data_dict['edge'] = None
                    data_dict['pad_before_s'] = None
                    data_dict['pad_after_s'] = None
                
                saved_on_disk, saved_row = self.check_if_precomputed_data_on_disk(data_dict)
                data_dict['saved_on_disk'] = saved_on_disk
                data_dict['color'] = color
                
                if saved_on_disk:
                    current_saving_path = Path(saved_row['save_path'])
                    condition_filter_path = current_saving_path / 'filter.json'
                    if condition_filter_path.exists():
                        with open(condition_filter_path, "r", encoding="utf-8") as f:
                            found_condition_filter_path = _pp_json_load(f)
                        
                        assert condition_filter[condition]['file_path_list'] == found_condition_filter_path['file_path_list'], f'Condition filter for {data_name} {condition} found on disque does not matched current filter. Please delete saved data if you want to compute with a new filter of the same name'
                        if 'trial_filter_dict' in condition_filter[condition]:
                            assert condition_filter[condition]['trial_filter_dict'] == found_condition_filter_path['trial_filter_dict'], f'Condition filter for {data_name} {condition} found on disque does not matched current filter. Please delete saved data if you want to compute with a new filter of the same name'
                else:
                    current_saving_path = Path(self.save_path) / str(uuid4())
                
                # Display metadata only: never include completion time in the cache key.
                data_dict['computed_at'] = saved_row.get('computed_at', '') if saved_on_disk else ''
                data_dict['save_path'] = current_saving_path
                
                data.append(pd.Series(data_dict))
                
        return pd.concat(data, axis=1).T
    
    def save_data(self, filtered_data, data_df_index, condition_filter):
        data_df_row = self.data_df.iloc[data_df_index].copy()
        
        os.makedirs(data_df_row['save_path'], exist_ok=True)

        if isinstance(filtered_data, pd.DataFrame):
            data_path = data_df_row['save_path'] / 'data.xlsx'
            _pp_to_excel(filtered_data, data_path)
            
            condition = filtered_data['label'].unique()
            assert len(condition) == 1
            condition_filter['condition'] = condition[0]
            condition_filter['data_type'] = 'trials'
            
        else:
            data_path = data_df_row['save_path'] / 'data.pkl'
            with open(data_path, "wb") as f:
                pickle.dump(filtered_data, f)
            
            condition_filter['condition'] = filtered_data.condition_name
            condition_filter['data_type'] = filtered_data.data_type
    
        condition_filter_path = data_df_row['save_path'] / 'filter.json'
        with open(condition_filter_path, "w", encoding="utf-8") as f:
            _pp_json_dump(condition_filter, f, indent=2)
        
        # Record completion on the computing machine, including its UTC offset.
        # Reusing cached data does not call save_data and keeps the original time.
        computed_at = datetime.now().astimezone().isoformat(sep=' ', timespec='seconds')
        data_df_row['computed_at'] = computed_at
        self.data_df.at[self.data_df.index[data_df_index], 'computed_at'] = computed_at
        data_df_row['saved_on_disk'] = True
        self.preprocessed_inventory_df = pd.concat([self.preprocessed_inventory_df, data_df_row.to_frame().T], ignore_index=True)  
        _pp_to_excel(self.preprocessed_inventory_df, f'{self.save_path}/Preprocessed_filtered_data_inventory.xlsx', index=False)
            
    def get_data(self, data_type, condition):
        data_df_row = self.data_df[(self.data_df['data_type'] == data_type) & (self.data_df['condition'] == condition)]
        assert len(data_df_row) == 1
        data_df_row = data_df_row.squeeze()
        
        if data_type == 'trials':
            data_path = Path(data_df_row['save_path']) / 'data.xlsx'
            return _pp_read_excel(data_path)
        else:
            data_path = Path(data_df_row['save_path']) / 'data.pkl'
            with open(data_path, "rb") as f:
                data = pickle.load(f)
            return data
             
    def get_trials_df(self):
        trials_df_list = []
        for condition in self.conditions:
            trials_df_list.append(self.get_data('trials', condition))
        return pd.concat(trials_df_list)
    
    @classmethod
    def get_preprocessed_inventory(cls, project_path):
        
        save_path = Path(project_path) / r'Data\Preprocessed_filtered_data'
        if os.path.exists(f'{save_path}/Preprocessed_filtered_data_inventory.xlsx'):
            preprocessed_inventory_df = _pp_read_excel(f'{save_path}/Preprocessed_filtered_data_inventory.xlsx')
        else:
            preprocessed_inventory_df = pd.DataFrame({'filter_name': [], 'condition': [], 'data_type': [], 'aligned_to_event': [], 'edge': [], 'pad_before_s': [], 'pad_after_s': [], 'saved_on_disk': [], 'save_path': [], 'nwb_files_nb': []})
        return preprocessed_inventory_df
 
    @classmethod
    def from_saved_data(cls, project_path, data_info_dicts=None, preprocessed_inventory_df_indexes=None):
        
        assert data_info_dicts is not None or preprocessed_inventory_df_indexes is not None, 'Please provide either a list of data_dict or preprocessed_inventory_df_indexes'
        self = cls.__new__(cls)
        self.project_path = Path(project_path)
        self.save_path = self.project_path / r'Data\Preprocessed_filtered_data'
        
        self.preprocessed_inventory_df = self.get_preprocessed_inventory(self.project_path)
        
        if preprocessed_inventory_df_indexes is not None:
            self.data_df = pd.DataFrame(self.preprocessed_inventory_df.iloc[preprocessed_inventory_df_indexes]) #if selecting 1 row only it will be converted to a Series forcing it to stay as DataFrame
        else:
            if not isinstance(data_info_dicts, (list, tuple)):
                data_info_dicts = [data_info_dicts]
                
            self.data_df = []
            for data_dict in data_info_dicts:
                saved_on_disk, row = self.check_if_precomputed_data_on_disk(data_dict)
                if saved_on_disk:
                    self.data_df.append(row)
                else:
                    print(f'{data_dict} was not found on disk')
            self.data_df = pd.concat(self.data_df, axis=1).T
            
        self.data_types = set(self.data_df['data_type'])
        self.filter_name = list(set(self.data_df['filter_name'].tolist()))
        if len(self.filter_name) == 1:
            self.filter_name = self.filter_name[0]
            
        self.conditions = list(set(self.data_df['condition'].tolist()))

        self.aligned_to_event = list(set(self.data_df['aligned_to_event'].tolist()))
        if len(self.aligned_to_event) == 1:
            self.aligned_to_event = self.aligned_to_event[0]
        
        return self
