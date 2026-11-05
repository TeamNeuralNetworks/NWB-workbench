# -*- coding: utf-8 -*-
"""
Created on Mon Feb  5 10:36:52 2024

@author: Pierre-Alexis
"""
import numpy as np
import os
import pandas as pd

from Builders.Electrophysiology.file_openers.intan_system_file import Intan_opener
from Builders.General.NWB_builder import convert_keyword_to_dict
from Builders.General.events_toolbox import generate_event_markers_dict_from_TTL, extract_trials_from_event_markers

class custom_opener(Intan_opener):
    """
    Custom session opener for Pierre Le Cabec dataset (2025).

    This class loads and synchronizes:
        - Behavioral metrics from Excel
        - Electrophysiology signals from Intan (.rhd)
        - TTL digital inputs

    It builds a unified `event_markers` dictionary containing all detected
    behavioral and electrophysiological events aligned in time.

    Main attributes created:
        - self.data : spikeinterface RecordingExtractor (amplifier channels)
        - self.accelerometer : auxiliary channel recording (if present)
        - self.event_markers : dict of structured event timestamps
    """
    def __init__(self, filepath:str, device_info:dict, TTL_info:dict, general_info:dict, animal_info:dict, aditional_trial_metric=None, **kwargs):
        
        # filepath = "D" + filepath[1:]
        
        self.event_markers = None
        self.trials_df = None
        self.trials_df_description = None
        self.device_info = device_info

        self.data = None
        self.data_sampling_rate = None
        
        self.accelerometer = None
        self.accelerometer_sampling_rate = None
        
        self.TTL = None
        self.TTL_sampling_rate = None
        
        
        self.TTL_info = TTL_info
        self.aditional_trial_metric = aditional_trial_metric
                
        session_path_dict = self.file_path_finder(filepath)
        session_df = pd.read_excel(session_path_dict['behaviour_exel_path'])
        event_markers_from_excel = self.extract_behaviour_metric(session_df)
        
        if self.has_ephy(session_path_dict):
            self.filepath = session_path_dict['ephy_file_path']
            self.extract_data()
            
            if self.TTL is not None:
                self.fixe_TTL()
                
                self.event_markers = generate_event_markers_dict_from_TTL(self.TTL, self.TTL_info, self.TTL_sampling_rate)
                keywords = convert_keyword_to_dict(general_info['keywords'])
        
                if keywords['group'] == '5':
                    self.fix_right_light_cue_bug()
                    
                if 'trials' in self.event_markers.keys():
                    self.trials_df, aditional_time_trial_metric, self.trials_df_description = extract_trials_from_event_markers(self.event_markers, self.aditional_trial_metric)
                    self.event_markers.update(aditional_time_trial_metric)

            self.compare_metric_df_to_event_marker(event_markers_from_excel)
            self.add_variation_type_to_trials_df()
            self.add_stim_type_to_trials_df()
        else:
            self.event_markers = event_markers_from_excel
            self.trials_df = self.create_trial_df_from_behavior(session_df)
          
    def file_path_finder(self, session_path):
        """
        Identify behavioral and electrophysiology files inside a session folder.
    
        Expected structure:
            session_folder/
                Behaviour*.xlsx
                subfolder/
                    *.rhd
    
        Populates:
            self.session_path_dict:
                {
                    'session_path': ...,
                    'behaviour_exel_path': ...,
                    'ephy_file_path': ...
                }
    
        Raises
        ------
        ValueError
            If:
                - Multiple behavioral files are found
                - Multiple .rhd files are found
                - Unsupported folder structure is detected
                - No behavioral Excel file is found
        """
        
        session_path_dict = {'session_path': session_path}
        
        file_list = os.listdir(session_path)
        
        for file in file_list:
            
            if 'Behaviour' in file and file.split('.')[-1] == 'xlsx':
                if 'behaviour_exel_path' in session_path_dict.keys():
                    raise ValueError(f'Multiple behaviour_exel_path found for {session_path}')
                session_path_dict['behaviour_exel_path'] = fr'{session_path}/{file}'
            
            elif file == 'cameras':
                continue #video data not yet suported 
            
            elif file == 'sensor_probe':
                continue
            
            elif os.path.isdir(f'{session_path}/{file}'):
                ephy_file_path = os.listdir(fr'{session_path}/{file}')
                ephy_file_path = [file for file in ephy_file_path if file.split('.')[-1] == 'rhd']
                
                if len(ephy_file_path) == 0:
                    raise ValueError(f'Unsuported folder found {file}')
                elif len(ephy_file_path) > 1:
                    raise ValueError(f'Multiple ephy file found in {file}')
                else:
                    if 'ephy_file_path' in session_path_dict.keys():
                        raise ValueError(f'Multiple ephy_file_path found in {file}')
                        
                    session_path_dict['ephy_file_path'] = fr'{session_path}/{file}/{ephy_file_path[0]}'
                
        
        if 'behaviour_exel_path' not in session_path_dict.keys():
            raise ValueError('No Behaviour excel found')
        
        if 'ephy_file_path' not in session_path_dict.keys():
            print('No ephy file found')
        
        return session_path_dict

                
    def has_ephy(self, session_path_dict):
        """
        Check whether electrophysiology data is available.
    
        Returns
        -------
        bool
            True if an Intan .rhd file was detected.
        """
        
        if 'ephy_file_path' in session_path_dict.keys():
            return True
        else:
            return False
    
    def compare_metric_df_to_event_marker(self, event_markers_from_excel, excel_to_ephy_acceptable_time_diff=0.01):
        """
        Compare behavioral Excel timestamps with TTL-derived timestamps.
    
        The function aligns events relative to trial start times and checks that
        the maximum timing difference between Excel and electrophysiology does
        not exceed a defined tolerance.
    
        Parameters
        ----------
        event_markers_from_excel : dict
            Event markers extracted from behavioral Excel.
        excel_to_ephy_acceptable_time_diff : float
            Maximum allowed time difference (seconds).
            Default = 0.01 (10 ms)
    
        Raises
        ------
        AssertionError
            If maximum time difference exceeds tolerance.
    
        Notes
        -----
        This ensures temporal alignment between behavioral logs and digital input recordings.
        """
        
        ephy_trial_start_time = self.event_markers['trials']['start'] #ephy start before excel so we need to put the time of the start of the first trial to 0 in the ephy
        ephy_trial_stop_time = self.event_markers['trials']['stop'] #ephy start before excel so we need to put the time of the start of the first trial to 0 in the ephy
        
        excel_trial_start_time = event_markers_from_excel['trials']['start'] #ephy start before excel so we need to put the time of the start of the first trial to 0 in the ephy
        excel_trial_stop_time = event_markers_from_excel['trials']['stop'] #ephy start before excel so we need to put the time of the start of the first trial to 0 in the ephy
        for additional_marker_name in self.event_markers.keys():
            if additional_marker_name in event_markers_from_excel.keys() and additional_marker_name in self.event_markers.keys():
                if len(self.event_markers[additional_marker_name]['start']) < len(ephy_trial_start_time):
                    ephy_zero_time = [np.where((current_time >= ephy_trial_start_time) & (current_time <= ephy_trial_stop_time))[0] for current_time in self.event_markers[additional_marker_name]['start']]
                    ephy_zero_time = ephy_trial_start_time[ephy_zero_time]
                    ephy_zero_time = ephy_zero_time.flatten()
                    
                    excel_zero_time = [np.where((current_time >= excel_trial_start_time) & (current_time <= excel_trial_stop_time))[0] for current_time in event_markers_from_excel[additional_marker_name]['start']]
                    excel_zero_time = excel_trial_start_time[excel_zero_time]
                    excel_zero_time = excel_zero_time.flatten()
                else:
                    ephy_zero_time = ephy_trial_start_time
                    excel_zero_time = excel_trial_start_time
                
                diff = (self.event_markers[additional_marker_name]['start'] - ephy_zero_time) - (event_markers_from_excel[additional_marker_name]['start'] - excel_zero_time)

                assert abs(diff.max()) < excel_to_ephy_acceptable_time_diff, 'Maximum time diff between ephy and excel reached'
        print(f'Data from digital in and excel are matching within set time tolerance of {excel_to_ephy_acceptable_time_diff}s')

    def extract_behaviour_metric(self, session_df):
        """
        Extract behavioral event markers from Excel session dataframe.
    
        Returns
        -------
        dict
            Dictionary structured as:
    
            {
                event_name: {
                    'description': str,
                    'start': np.ndarray,
                    'stop': np.ndarray (if epoch),
                }
            }
    
        Extracted Events
        ----------------
            - trials (epoch)
            - reward (event)
            - nose_poke_right (event)
            - nose_poke_left (event)
            - right_light_cue (epoch)
            - left_light_cue (epoch)
            - warning_signal (epoch)
            - audio_cue (epoch)
    
        Notes
        -----
        - Events are reconstructed trials by trials.
        - Timing is accumulated using trials duration + ITI.
        - Audio cue duration is artificially set to 0.5 seconds as the end of the cue was not loged in the excel.
        """
        
        event_markers_from_excel = {'trials': {'description': 'Trial is active',
                                              'start': [],
                                              'stop': []}, 
                                    'reward': {'description': 'Reward is delivered (stepper motor on)',
                                                                          'start': []}, 
                                    'nose_poke_right': {'description': 'Nose poke to the right side of the operant wall by the animal',
                                                                          'start': []}, 
                                    'nose_poke_left': {'description': 'Nose poke to the left side of the operant wall by the animal',
                                                                          'start': []}, 
                                    'right_light_cue': {'description': 'Right light cue is on',
                                                                          'start': [],
                                                                          'stop': []}, 
                                    'left_light_cue': {'description': 'Left light cue is on',
                                                                          'start': [],
                                                                          'stop': []}, 
                                    'warning_signal': {'description': 'Warning signal is on (shut off of the main light)',
                                                                        'start': [],
                                                                        'stop': []}, 
                                    'audio_cue': {'description': 'Audio cue played when a response',
                                                                'start': [],
                                                                'stop': []}, 
                                    }
        
        
        def extract_event_time_from_df(df, code, name, start_stop, trial_start_time):
            event_time = df[df["code"].isin(code)]['trial_time'].tolist()
            assert len(event_time) < 2, f'More than 1 code per trial {code}, {event_time}'
            
            if len(event_time) == 0:
                return None
            else:
                event_time = event_time[0]
                event_markers_from_excel[name][start_stop].append(event_time+trial_start_time)
                return event_time
        
        trial_start_time = 0
        for trial_nb, trial_df in session_df.groupby(["trial"]):
            
            extract_event_time_from_df(trial_df, [1], 'trials', 'start', trial_start_time)
            
            extract_event_time_from_df(trial_df, [5, 9], 'reward', 'start', trial_start_time)
            
            extract_event_time_from_df(trial_df, [5, 6], 'nose_poke_right', 'start', trial_start_time)
            
            extract_event_time_from_df(trial_df, [9, 10], 'nose_poke_left', 'start', trial_start_time)
            
            extract_event_time_from_df(trial_df, [21], 'right_light_cue', 'start', trial_start_time)
            extract_event_time_from_df(trial_df, [22], 'right_light_cue', 'stop', trial_start_time)

            extract_event_time_from_df(trial_df, [23], 'left_light_cue', 'start', trial_start_time)
            extract_event_time_from_df(trial_df, [24], 'left_light_cue', 'stop', trial_start_time)
            
            extract_event_time_from_df(trial_df, [1], 'warning_signal', 'start', trial_start_time)
            extract_event_time_from_df(trial_df, [16], 'warning_signal', 'stop', trial_start_time)
            
            audio_time = extract_event_time_from_df(trial_df, [19, 25], 'audio_cue', 'start', trial_start_time)
            if audio_time is not None:
                event_markers_from_excel['audio_cue']['stop'].append(audio_time+0.5)
            
            trial_start_time += extract_event_time_from_df(trial_df, [0], 'trials', 'stop', trial_start_time)
            iti_duration = trial_df['itiDuration'].unique()
            assert len(iti_duration) == 1
            iti_duration = iti_duration[0]
            trial_start_time += iti_duration
       
        key_to_remove = []
        for key in event_markers_from_excel.keys(): # convert list to array and remove empty event markers
            if len(event_markers_from_excel[key]['start']) == 0:
                key_to_remove.append(key)
            else:
                event_markers_from_excel[key]['start'] = np.array(event_markers_from_excel[key]['start'])
                if 'stop' in event_markers_from_excel[key].keys():
                    event_markers_from_excel[key]['stop'] = np.array(event_markers_from_excel[key]['stop'])
        
        if key_to_remove:
            for key in key_to_remove:
                del event_markers_from_excel[key]
        
        return event_markers_from_excel
    
                
    def compute_trial_metrics(self, trial_df, 
                              answered_good_code=[5, 9], 
                              answered_bad_code=[10, 6], 
                              cue_on_code=[23, 21], 
                              code_right=[5, 6], 
                              code_left=[9, 10], 
                              no_answer_code=14):
        
        cue_side_code_to_str_converter_dict = {1: 'right',
                                                2: 'left'}
        
        stim_code_to_str_converter_dict = {0: 'No_stim',
                                           2: 'Theta_Burst',
                                           3: '40Hz'}
        
        start_time = trial_df[trial_df['code'] == 1]['session_time']
        assert len(start_time) == 1
        start_time = start_time.iloc[0]
        
        stop_time = trial_df[trial_df['code'] == 0]['session_time']
        assert len(stop_time) == 1
        stop_time = stop_time.iloc[0]
        
        cue_side = trial_df['cue_order'].unique()
        assert len(cue_side) == 1
        cue_side = cue_side[0]
        cue_side = cue_side_code_to_str_converter_dict[cue_side]
        
        stim = trial_df['stim'].unique()
        assert len(stim) == 1
        stim = stim[0]
        stim = stim_code_to_str_converter_dict[stim]
            
        cue_time = trial_df[trial_df['code'].isin(cue_on_code)]['trial_time']
        assert len(cue_time) == 1
        cue_time = cue_time.iloc[0]
        
        FP_duration = round(cue_time, 3)
        
        if FP_duration.round(1) == 2.5:
            variation_type = "no_variation"
        elif FP_duration.round(1) == 3.3:
            variation_type = "long"
        elif FP_duration.round(1) == 1.7:
            variation_type = "short"
        else:
            raise ValueError

                   
        if no_answer_code in trial_df['code'].values:
            return np.nan, cue_side, 'no_response', FP_duration, variation_type, 'no_response', no_answer_code, stim, start_time, stop_time
        
        else:
            answer_row = trial_df[trial_df['code'].isin(answered_good_code+answered_bad_code)]
            response = 'right' if answer_row['code'].isin(code_right).all() else 'left'
            code = answer_row['code'].iloc[0]
            sucess = 'sucess' if answer_row['code'].isin(answered_good_code).all() else 'failure'
            assert len(answer_row) == 1
            answer_time = float(answer_row['trial_time'].iloc[0]) 
            
            cue_start_rows = trial_df[trial_df['code'].isin(cue_on_code)]
            assert len(cue_start_rows) == 1
            response_window_start_time = float(cue_start_rows['trial_time'].iloc[0])
            
            reaction_time = round(answer_time - response_window_start_time, 3)
            
            return reaction_time, cue_side, sucess, FP_duration, variation_type, response, code, stim , start_time, stop_time


    def create_trial_df_from_behavior(self, session_df):
        
        metrics_df = pd.DataFrame()
        for trial_nb, trial_df in session_df.groupby(["trial"]):
                
            reaction_time, cue_side, sucess, FP_duration, variation_type, response, code, stim, start_time, stop_time = self.compute_trial_metrics(trial_df)
            
            if variation_type is not None:
                metrics_df = pd.concat([metrics_df, pd.DataFrame([{
                                                                    'start_time': start_time,
                                                                    'stop_time': stop_time,
                                                                    'trial_nb': trial_nb[0],
                                                                    'FP_duration': FP_duration,
                                                                    'cue_side': cue_side,
                                                                    'reaction_time': reaction_time, 
                                                                    'sucess': sucess,
                                                                    'response': response,
                                                                    'variation_type': variation_type,
                                                                    'stim': False if stim == 'no_stim' else True,
                                                                    'stim_type': stim,
                                                                    }])])
       
        return metrics_df
    
    def fixe_TTL(self):
        """sometimes the very first TTL value is on a high wich cuases issue when detecting begining and end of events. 
        In this task no TTL shoiulkd be high when session start so i manually set the first TTL value to low for all digital in.
        
        sometimes the session stop before the TTL for trial has time to go low. Because trial is an epoch it must =has a begining and an end wich trigger an error.
        to fix this I manually set the last data point of the digital in corresponing the trials end to 0.
        """
        self.TTL.iloc[0, :] = 0
        
        trial_stop_channel = self.TTL_info['trials']['stop_channel']
        self.TTL.loc[-1, trial_stop_channel] = 0
        
        
    def fix_right_light_cue_bug(self):
        """
        Fixing bug from arduino code where right_light_cue and audio cue share the same digital_in.
        Fix hardware bug affecting right_light_cue TTL channel.
    
        Problem
        -------
        In some sessions, right_light_cue and audio_cue share the same digital input,
        causing ambiguous TTL detection.
    
        Solution
        --------
        - Use behavioral context (nose poke response, left cue presence)
          to disambiguate TTL pulses.
        - Reconstruct separate right_light_cue and audio_cue events.
        - Artificially set cue duration to 0.5 seconds.
    
        Raises
        ------
        AssertionError
            If reconstructed event counts do not match expected trial structure.
    
        Notes
        -----
        This correction is dataset-specific and based on empirical timing offsets.
        """
        
        right_light_cue_start_time_list = []
        right_light_cue_stop_time_list = []
        
        audio_cue_start_time_list = []
        audio_cue_stop_time_list = []
        
        response_list = []
        for trial_indx, (trial_start_time, trial_stop_time) in enumerate(zip(self.event_markers['trials']['start'],
                                                                             self.event_markers['trials']['stop'])):
            response_time = None
            for side in ['right', 'left']:
                if f'nose_poke_{side}' in self.event_markers.keys():
                    current_response = self.event_markers[f'nose_poke_{side}']['start']
                    current_response = current_response[(current_response >= trial_start_time) & (current_response <= trial_stop_time)]
                    
                    assert len(current_response) < 2
                    if len(current_response) == 1:
                        assert response_time is None
                        response_time = current_response[0]
                        response_list.append(response_time)
            
            left_digital_start = self.event_markers ['left_light_cue']['start']
            left_digital_start = left_digital_start[(left_digital_start >= trial_start_time) & (left_digital_start <= trial_stop_time)]
            assert len(left_digital_start) < 2
                
            buged_digital_start = self.event_markers ['right_light_cue']['start']
            buged_digital_start = buged_digital_start[(buged_digital_start >= trial_start_time) & (buged_digital_start <= trial_stop_time)]
            
            if len(buged_digital_start) != 0:
                
                if len(left_digital_start) != 0:
                    audio_cue_start_time_list.append(float(buged_digital_start[0]))
                    audio_cue_stop_time_list.append(float(buged_digital_start[0])+0.5)#TODO This this very artificial --> correct base probleme would be better
                else:
                    right_light_cue_start_time_list.append(float(buged_digital_start[0]))
                    right_light_cue_stop_time_list.append(float(buged_digital_start[0])+0.5) #TODO This this very artificial --> correct base probleme would be better
                    if len(buged_digital_start) == 2:
                        audio_cue_start_time_list.append(float(buged_digital_start[1]))
                        audio_cue_stop_time_list.append(float(buged_digital_start[1])+0.5)#TODO This this very artificial --> correct base probleme would be better

                    elif response_time is not None:
                        audio_cue_start_time_list.append(response_time-0.00055)#Empiricly found
                        audio_cue_stop_time_list.append((response_time-0.00055)+0.5)#TODO This this very artificial --> correct base probleme would be better
                        
        assert len(right_light_cue_start_time_list) == len(self.event_markers['left_light_cue']['start'])
        assert len(audio_cue_start_time_list) == len(response_list)
        
        assert len(audio_cue_start_time_list) == len(audio_cue_stop_time_list)
        assert len(right_light_cue_start_time_list) == len(right_light_cue_stop_time_list)
        
        self.event_markers['right_light_cue'] = {'start': np.array(right_light_cue_start_time_list), 
                                                    'stop': np.array(right_light_cue_stop_time_list),
                                                    'description': 'Right light cue is on'}
        
        self.event_markers['audio_cue'] = {'start': np.array(audio_cue_start_time_list), 
                                            'stop': np.array(audio_cue_stop_time_list),
                                            'description': 'Audio cue played when a response'}
    
    def add_variation_type_to_trials_df(self):
        
        mapping = {
            2.5: "no_variation",
            3.3: "long",
            1.7: "short",
        }
        
        self.trials_df["variation_type"] = self.trials_df["FP_duration"].round(1).map(mapping)
        self.trials_df_description["variation_type"] = 'FP variation for current trial (no variation correspond to 2.5s FP, short variation correspond to 1.666s FP and long variation correspond to 3.333s FP)'
    
    def add_stim_type_to_trials_df(self):
        
        
        if 'opto_pulse' not in self.event_markers.keys():
            stim_type_list = ['no_stim']*len(self.trials_df)
            stim = [False]*len(self.trials_df)
        else:
            stim_type_list = []
            stim = []
            for trial_indx, trial_row in self.trials_df.iterrows():
                current_opto_pulse_start = self.event_markers['opto_pulse']['start'][(self.event_markers['opto_pulse']['start'] >= trial_row['start_time']) & (self.event_markers['opto_pulse']['start'] <= trial_row['stop_time'])]
                current_opto_pulse_stop = self.event_markers['opto_pulse']['stop'][(self.event_markers['opto_pulse']['stop'] >= trial_row['start_time']) & (self.event_markers['opto_pulse']['stop'] <= trial_row['stop_time'])]
                assert len(current_opto_pulse_start) == len(current_opto_pulse_stop), 'Each opto pulse should start and end within a trial'
                
                if len(current_opto_pulse_start) == 0:
                    stim.append(False)
                    stim_type_list.append('no_stim')
                else:
                    assert len(current_opto_pulse_start) == 30
                    delay_between_pulse = np.unique(np.diff(current_opto_pulse_start).round(3))
                    if (delay_between_pulse == 0.025).all():
                       stim_type_list.append('40Hz')
                       stim.append(True)
                    else:
                      if len(delay_between_pulse) == 2 and 0.01 in delay_between_pulse and 0.119 in delay_between_pulse:
                          stim_type_list.append('Theta_Burst') 
                          stim.append(True)
                      else:
                          raise ValueError('Unrecognise Opto sitm ptrotocol from Opto_pulse TTL')
          
            assert len(stim_type_list) == len(self.trials_df)
            assert len(stim) == len(self.trials_df)
            
            
        self.trials_df['stim'] = stim
        self.trials_df_description["stim"] = 'whether and optogenetic stimulation of the right lateral cerebellar nucleus was perfrom at the end of the FP in the current trial'
        
        self.trials_df['stim_type'] = stim_type_list
        self.trials_df_description["stim_type"] = 'The type of optogenetic sitmulation delivered during the current trial'
        