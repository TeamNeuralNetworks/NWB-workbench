import numpy as np
import pandas as pd

def extract_trials_from_event_markers(event_markers_dict, aditional_trial_metric=None):   
    
    aditional_time_trial_metric = {}
    trials_df_description = {'trial_nb': 'trial number'}
    if aditional_trial_metric is not None:
        for aditional_metric, aditional_metric_info in aditional_trial_metric.items():
            if aditional_metric_info['measurment_between_ref_and_target'] == 'time':
                aditional_time_trial_metric[aditional_metric] = {'start': [],
                                                                 'stop': [],
                                                                 'event_marker_type': 'epoch',
                                                                 'description': aditional_metric_info['description']}  
            trials_df_description[aditional_metric] = aditional_metric_info['description']
   
    trial_df = []
    for trial_nb, (trial_start, trial_stop) in enumerate(zip(event_markers_dict['trials']['start'], event_markers_dict['trials']['stop'])):
       
        
        current_trial = {'start_time': trial_start,
                         'stop_time': trial_stop,
                         'trial_nb': trial_nb+1, #first trial should be 1 not 0
                         }
        
        
        if aditional_trial_metric is not None:
            for aditional_metric, aditional_metric_info in aditional_trial_metric.items():
                if aditional_metric_info['measurment_between_ref_and_target'] == 'time':
                    
                    metric_list = []
                    metric_start_time_list = []
                    metric_stop_time_list = []
                    for event_marker_ref, event_marker_target in zip(aditional_metric_info['reference'], aditional_metric_info['target']):
                        
                        if event_marker_target in event_markers_dict.keys() and event_marker_ref in event_markers_dict.keys(): #if no event of that type in the session continue
                        
                            current_ref_time = event_markers_dict[event_marker_ref]['start']
                            current_ref_time = current_ref_time[(current_ref_time >= trial_start) & (current_ref_time <= trial_stop)]
                            
                            current_target_time = event_markers_dict[event_marker_target]['start']
                            current_target_time = current_target_time[(current_target_time >= trial_start) & (current_target_time <= trial_stop)]
                            
                            if len(current_ref_time) == 0 or len(current_target_time) == 0:
                                continue
                            else:
                                metric_list = metric_list + list(current_target_time - current_ref_time)
                                metric_start_time_list = metric_start_time_list + list(current_ref_time)
                                metric_stop_time_list = metric_stop_time_list + list(current_target_time)
                        
                    if len(metric_list) == 0:
                        metric_list = np.nan
                    elif len(metric_list) == 1:
                        metric_list = metric_list[0]
                    
                    current_trial[aditional_metric] = metric_list
                    aditional_time_trial_metric[aditional_metric]['start'] = aditional_time_trial_metric[aditional_metric]['start'] + metric_start_time_list
                    aditional_time_trial_metric[aditional_metric]['stop'] = aditional_time_trial_metric[aditional_metric]['stop'] + metric_stop_time_list
                    
                elif aditional_metric_info['measurment_between_ref_and_target'] == 'event':
                    outcome = None
                    else_outcome = None
                    for event_marker_ref, current_outcome in zip(aditional_metric_info['reference'], aditional_metric_info['outcome']):
                        
                        if event_marker_ref == 'else':
                            else_outcome = current_outcome
                            continue
                        
                        if event_marker_ref not in event_markers_dict.keys(): #if no event of that type in the session continue
                            continue
                        
                        current_ref_time = event_markers_dict[event_marker_ref]['start']
                        current_ref_time = current_ref_time[(current_ref_time >= trial_start) & (current_ref_time <= trial_stop)]
                        
                        if len(current_ref_time) != 0:
                            assert outcome is None, f'for measurment_between_ref_and_target event only one event marker should happen per trial for tab {aditional_metric}'
                            outcome = current_outcome
                    
                    if outcome is None: #no event happened during current trial
                        assert else_outcome is not None, f'No event during trial and else not defined for tab {aditional_metric}'
                        outcome = else_outcome
                    
                    current_trial[aditional_metric] = outcome
      
                
                elif aditional_metric_info['measurment_between_ref_and_target'] == 'condition': 
                    outcome = None
                    ref_event = None
                    target_event = None
                    ref_else_outcome = {}
                    target_else_outcome = {}
                    for event_marker_ref, event_marker_target, current_outcome in zip(aditional_metric_info['reference'], aditional_metric_info['target'], aditional_metric_info['outcome']):
                        
                        if event_marker_ref == 'else':
                            ref_else_outcome[event_marker_target] = current_outcome
                        
                        if event_marker_target == 'else':
                            target_else_outcome[event_marker_ref] = current_outcome
                        
                        if event_marker_ref == 'else' or event_marker_target == 'else': 
                            continue
                        
                        if event_marker_target in event_markers_dict.keys() and event_marker_ref in event_markers_dict.keys(): #if no event of that type in the session continue
                        
                            current_ref_time = event_markers_dict[event_marker_ref]['start']
                            current_ref_time = current_ref_time[(current_ref_time >= trial_start) & (current_ref_time <= trial_stop)]
                            
                            current_target_time = event_markers_dict[event_marker_target]['start']
                            current_target_time = current_target_time[(current_target_time >= trial_start) & (current_target_time <= trial_stop)]
                            
                            if len(current_ref_time) != 0:
                                assert ref_event is None or ref_event == event_marker_ref, f'for measurment_between_ref_and_target event only one reference_event marker should happen per trial for tab {aditional_metric}'
                                ref_event = event_marker_ref
                            
                            if len(current_target_time) != 0:
                                assert target_event is None or target_event == event_marker_target, f'for measurment_between_ref_and_target target_event only one event marker should happen per trial for tab {aditional_metric}'
                                target_event = event_marker_target
                            
                            if len(current_ref_time) != 0 and len(current_target_time) != 0:
                                assert outcome is None, f'for measurment_between_ref_and_target event only one event marker should happen per trial for tab {aditional_metric}'
                                outcome = current_outcome
                    
                    if outcome is None: #no event happened during current trial
                        assert len(ref_else_outcome) > 0 or len(target_else_outcome) > 0, f'No event during trial and else not defined for tab {aditional_metric}'
                        
                        if ref_event is None and target_event is None:
                            assert ref_else_outcome['else'] == target_else_outcome['else']
                            outcome = ref_else_outcome['else']
                        elif ref_event is None:
                            outcome = ref_else_outcome[target_event]
                        elif target_event is None:
                            outcome = target_else_outcome[ref_event]
                    
                    current_trial[aditional_metric] = outcome
                
                elif aditional_metric_info['measurment_between_ref_and_target'] == 'trial_nb_to_outcome':
                    event_marker_index = [i for i, v in enumerate(aditional_metric_info['reference']) if v == trial_nb]
                    assert len(event_marker_index) < 2, f'measurment_between_ref_and_target trial_nb_to_outcome cannot contain mulmtiple time the same trial_nb for tab {aditional_metric}'

                    if len(event_marker_index) == 1:
                        current_trial[aditional_metric] = aditional_metric_info['outcome'][event_marker_index]
                    else:
                        current_trial[aditional_metric] = None
                else:
                    raise ValueError(f"Unsuported measurment_between_ref_and_target: {aditional_metric_info['measurment_between_ref_and_target']} in {aditional_metric} tab")
                
        trial_df.append(current_trial)
        
    return pd.DataFrame(trial_df), aditional_time_trial_metric, trials_df_description

def generate_event_markers_dict_from_TTL(TTL, info, sampling_rate):
    """
    Extract event or epoch timestamps from TTL digital input channels.

    For each entry in `info`, rising (0→1) or falling (1→0) edges are detected
    in the specified digital channel and converted to timestamps (in seconds).

    Parameters
    ----------
    info : dict
        Dictionary describing TTL channels with:
            - description
            - start_channel
            - start_type ('rise' or 'fall')
            - stop_channel (optional)
            - stop_type ('rise' or 'fall', if stop_channel is provided)

    TTL : pandas dataFrame
        each column beeing a TTL channel whith it's name as the column name'

    Returns
    -------
    dict
        Dictionary with detected timestamps:
            - 'start' (always present)
            - 'stop' (if defined → epoch)
            - 'description'
    """
    
    event_markers = {}
    for digital_in_name, digital_in_dict in info.items():
        
        try:
            digital_in_trace = np.array(TTL[info[digital_in_name]['start_channel']])
        except:
            print(f'Unable to extract TTL start {digital_in_name}')
            continue
                
        if (digital_in_trace == 0).all():
            print(f'\tDigital input for {digital_in_name} is empty')
            continue
        
        assert digital_in_name not in event_markers.keys()
        event_markers[digital_in_name] = {'description': digital_in_dict['description']}
        
        if info[digital_in_name]['start_type'] == 'rise':
            timestamps_on = (np.where((digital_in_trace[:-1] == 0) & (digital_in_trace[1:] == 1))[0] + 1)/sampling_rate
        elif info[digital_in_name]['start_type'] == 'fall':
            timestamps_on = (np.where((digital_in_trace[:-1] == 1) & (digital_in_trace[1:] == 0))[0] + 1)/sampling_rate
        else:
            raise ValueError(f'start_type should only be rise or fall in TTL_info tab, not {info[digital_in_name]["start_type"]}')
            
        event_markers[digital_in_name]['start'] = timestamps_on
        
        if info[digital_in_name]['stop_channel'] is not None:
            
            if info[digital_in_name]['start_channel'] != info[digital_in_name]['stop_channel']:
                try:
                    digital_in_trace = np.array(TTL[info[digital_in_name]['stop_channel']])
                except:
                    print(f'Unable to extract TTL {digital_in_name}')
                    continue
            
            if info[digital_in_name]['stop_type'] == 'rise':
                timestamps_off = (np.where((digital_in_trace[:-1] == 0) & (digital_in_trace[1:] == 1))[0] + 1)/sampling_rate
            elif info[digital_in_name]['stop_type'] == 'fall':
                timestamps_off = (np.where((digital_in_trace[:-1] == 1) & (digital_in_trace[1:] == 0))[0] + 1)/sampling_rate
            else:
                raise ValueError(f'start_type should only be rise or fall in TTL_info tab, not {info[digital_in_name]["stop_type"]}')
                
            if digital_in_name == 'trials':                   
                if len(timestamps_on) == len(timestamps_off)+1 and timestamps_on[-1] > timestamps_off[-1]: #recording stop before trial_on had time to go back to 0
                    timestamps_off = np.append(timestamps_off, len(digital_in_trace)/sampling_rate) #add time of last point of recording
            
            assert len(timestamps_on) == len(timestamps_off), f'Unequall amout of start and stop TTL for {digital_in_name}'

            event_markers[digital_in_name]['stop'] = timestamps_off
            
        else:
            if digital_in_name == 'trials': #Assume that begining of a new trial correspond to end of previous one and end of recording correspond to end of last trial
                print('No stop was define trial TTL.\nAssuming that begining of a new trial correspond to end of previous one and end of recording correspond to end of last trial')    
                timestamps_off = np.delete(timestamps_on, 0)
                timestamps_off = np.append(timestamps_off, len(digital_in_trace)/sampling_rate)
                assert len(timestamps_on) == len(timestamps_off), f'Unequall amout of start and stop TTL for {digital_in_name}'
                event_markers[digital_in_name]['stop'] = timestamps_off
                
    return event_markers


"""
Created on Sat Feb 28 12:53:57 2026

@author: Pierre
"""

