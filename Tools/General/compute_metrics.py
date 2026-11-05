# -*- coding: utf-8 -*-
"""
Created on Sat Jun 21 13:28:28 2025

@author: Pierre.LE-CABEC
"""

import pandas as pd
import numpy as np
from scipy.stats import bootstrap, linregress, pearsonr
from scipy.optimize import curve_fit
import xarray as xr 
from math import floor

def align_xarray_trials(xarray1, xarray2):
    xarray1_trial_session_days = [f'{mouse}_{day}_{session}_{trial}' for mouse, day, session, trial in zip(xarray1.mouse.data, xarray1.day.data, xarray1.session.data, xarray1.trials.data)]
    xarray2_trial_session_days = [f'{mouse}_{day}_{session}_{trial}' for mouse, day, session, trial in zip(xarray2.mouse.data, xarray2.day.data, xarray2.session.data, xarray2.trials.data)]
    common_trial_session_days = set(xarray1_trial_session_days).intersection(xarray2_trial_session_days)
    indices_xarray1 = [i for i, val in enumerate(xarray1_trial_session_days) if val in common_trial_session_days]
    indices_xarray2 = [i for i, val in enumerate(xarray2_trial_session_days) if val in common_trial_session_days]
    
    xarray1 = xarray1.isel(trials=indices_xarray1)
    xarray2 = xarray2.isel(trials=indices_xarray2)
    
    return xarray1, xarray2

def get_suces_rate_df(df, columns=['label'], exclude_no_response_trials=True):
    
    if not isinstance(columns, (list, tuple)):
        columns = [columns]
    
    sucess_rate_result_dict = {'sucess_rate': []}
    for current_columns in columns:
        sucess_rate_result_dict[current_columns] = []
        
    for current_columns, current_df in df.groupby(columns):
        if exclude_no_response_trials:
            current_df = current_df[current_df['response'] != 'no_response']
            
        sucess_rate_result_dict['sucess_rate'].append((len(current_df[current_df['sucess']=='sucess'])/len(current_df))*100)
        
        for columns_index, columns_value in enumerate(current_columns):
            sucess_rate_result_dict[columns[columns_index]].append(columns_value)
    
    return pd.DataFrame(sucess_rate_result_dict)

def bootstrapt_metric_df(df, metrics, columns, n_boot=1000):
    
    if not isinstance(metrics, (list, tuple)):
        metrics = [metrics]
        
    if not isinstance(columns, (list, tuple)):
        columns = [columns]
    
    bootstrat_result_dict = {}
    for current_columns in columns:
        bootstrat_result_dict[current_columns] = []
    
    for current_metrics in metrics:
        bootstrat_result_dict[current_metrics] = []
        
    for current_columns, current_df in df.groupby(columns):
        for metric in metrics:
            bootstrap_ci = bootstrap((current_df[metric],), np.mean, confidence_level=0.95, random_state=1, method='percentile', n_resamples=n_boot)
            bt = bootstrap_ci.bootstrap_distribution
            bt = [float(value) for value in bt]
            bootstrat_result_dict[metric] = bootstrat_result_dict[metric] + bt
            
        for columns_index, columns_value in enumerate(current_columns):
            bootstrat_result_dict[columns[columns_index]] = bootstrat_result_dict[columns[columns_index]] + [columns_value]*n_boot
    
    return pd.DataFrame(bootstrat_result_dict)


def gaussian(x, A, mu, sigma):
    return A * np.exp(-0.5 * ((x - mu) / sigma)**2)

def mean_normalisation(da, dim):
    mean_val = da.mean(dim=dim, keep_attrs=True)
    normalisation = da - mean_val
    normalisation.attrs = da.attrs
    return normalisation


def fit_gaussian(data, point_of_interest=2.5, window_padding=1, plot_fit=False, ax=None, color=None):
    selection = data.sel(time=slice(point_of_interest-window_padding, point_of_interest+window_padding))
    max_time  = selection.time[(selection == selection.max()).squeeze()].data[0]
    
    amp_window = data.sel(time=slice(max_time-window_padding, max_time+window_padding)).squeeze()
    
    xdata = amp_window.time.data
    ydata = amp_window.data
    
    # 3. Initial guesses
    A0 = ydata.max()
    mu0 = xdata[ydata.argmax()]
    sigma0 = (xdata.max() - xdata.min()) / 4
    
    # 4. Fit
    try:
        popt, _ = curve_fit(gaussian, xdata, ydata, p0=[A0, mu0, sigma0])
    except RuntimeError:
        return np.nan, np.nan, np.nan
    
    A_fit, mu_fit, sigma_fit = popt
    
    if plot_fit:
        # 5. Plot
        x_fit = np.linspace(xdata.min(), xdata.max(), 200)
        y_fit = gaussian(x_fit, A_fit, mu_fit, sigma_fit)
        
        ax.plot(x_fit, y_fit, color, linewidth=2,
                        )
    
    return A_fit, mu_fit, sigma_fit

def fit_linear_regression(data, window_start=0, window_stop=2.5, plot_fit=False, ax=None, color=None):
    selection = data.sel(time=slice(window_start, window_stop))
    slope, intercept, r_value, p_value, std_err = linregress(selection.time, selection.data)
    
    if plot_fit:
        fit_line = slope * selection.time + intercept
        ax.plot(selection.time, fit_line, color)
    
    return slope, intercept, r_value, p_value, std_err

def sliding_pearson(da1, da2, window_size=10, step_size=5):
    """
    Compute sliding Pearson correlation between two xarray DataArrays
    with dimensions (trials, time, freqs).
    
    Parameters:
        da1, da2: xarray.DataArray
        window_size: int, number of time points in each window
        step_size: int, number of time points to slide
    
    Returns:
        xarray.DataArray of shape (trials, windows)
    """
    if len(da1.shape) == 2:
        n_trials, n_time = da1.shape
        
    elif len(da1.shape) == 3:
        n_trials, n_time, n_freqs = da1.shape
        
    n_windows = (n_time - window_size) // step_size + 1
    result = np.full((n_trials, n_windows), np.nan)

    for trial in range(n_trials):
        for w in range(n_windows):
            t0 = w * step_size
            t1 = t0 + window_size
            
            if len(da1.shape) == 3:
                x = da1[trial, t0:t1, :].values.flatten()
                y = da2[trial, t0:t1, :].values.flatten()
            elif len(da1.shape) == 2:
                x = da1[trial, t0:t1].values.flatten()
                y = da2[trial, t0:t1].values.flatten()
                
            mask = np.isfinite(x) & np.isfinite(y)
            if np.sum(mask) >= 10:
                r, _ = pearsonr(x[mask], y[mask])
                result[trial, w] = r

    time_coords = da1.coords['time'].values
    time_windows = [time_coords[i * step_size + window_size // 2] for i in range(n_windows)]

    return xr.DataArray(
        result,
        dims=('trials', 'time'),
        coords={'trials': da1.coords['trials'], 'time': time_windows}
    )


# def compute_instantanious_sucess_rate(metadata_df, nb_of_unintended_trial=3, nb_of_trial_for_instantanious_sucess=4):

#     def split_by_block(df, nb_of_trial):
        
#         df = df.sort_values("trial_nb")
#         df = df.reset_index(drop=True)
#         count = np.diff(df['trial_nb'].sort_values())
#         index = np.where(count >= nb_of_trial)[0]
#         if len(index) > 0:
#             previous_indx = 0
#             df_list = []
#             for ind in index:
#                 df_list.append(df.loc[previous_indx:ind].reset_index(drop=True))
#                 previous_indx = ind+1      
#             df_list.append(df.loc[previous_indx:len(df)].reset_index(drop=True))
#         else:
#             df_list = [df]
            
#         return df_list
    
#     def compute_sliding_sucess(df, nb_of_trial):
#         df = df.sort_values("trial_nb")
#         results = df['sucess'].astype(int)
#         if len(results) > nb_of_trial:
#             sucess_list = []
#             for i, current_sucess in enumerate(results):
#                 if i <= len(results)-nb_of_trial:
#                     sucess = []
#                     for current_i in range(nb_of_trial):
#                         sucess.append(results[i+current_i])
                    
#                     sucess_list.append((sum(sucess) / nb_of_trial)*100)
#                 else:
#                     sucess = []
#                     for current_i in range(nb_of_trial):
#                         sucess.append(results[i-current_i])
#                     sucess_list.append((sum(sucess) / nb_of_trial)*100)
#         elif len(results) == nb_of_trial:
#             sucess_list = [(sum(results) / nb_of_trial)*100]*nb_of_trial
#         else:
#             return None
#         df['instantanious_sucess'] = sucess_list
#         return df
    
#     # def compute_block_sucess(df, nb_of_trial=3):
#     #     df = df.sort_values("trial_nb")
#     #     results = df['sucess'].astype(int)
#     #     if len(results) > nb_of_trial:
#     #         sucess_list = []
#     #         current_ind = 0
#     #         for i in range(floor(len(results)/nb_of_trial)):
#     #             sucess_list.append(sum(results[current_ind: current_ind+nb_of_trial]) / nb_of_trial)
#     #             current_ind = current_ind+nb_of_trial
#     #     elif len(results) == nb_of_trial:
#     #         sucess_list = [sum(results) / nb_of_trial]
#     #     else:
#     #         sucess_list = []
        
#     #     return sucess_list
    
#     final_metadata_df = pd.DataFrame()
#     for (mouse, day, session), side_df in metadata_df.groupby(['mouse', 'day', 'session']):
        
#         df_list = split_by_block(side_df, nb_of_trial=nb_of_unintended_trial)
#         for curent_df in df_list:
            
#             curent_df = compute_sliding_sucess(curent_df, nb_of_trial=nb_of_trial_for_instantanious_sucess)
#             final_metadata_df = pd.concat([final_metadata_df, curent_df])
#     return final_metadata_df

def compute_instantanious_sucess_rate(
                                      metadata_df,
                                      nb_of_unintended_trial=2,
                                      nb_of_trial_per_block=4,
                                      ):

    def split_by_block(df, nb_of_unintended_trial):
        df = df.sort_values("trial_nb").reset_index(drop=True)
        gaps = np.diff(df['trial_nb'].dropna().to_numpy())
        split_pos = np.where(gaps > (nb_of_unintended_trial+1))[0]
        blocks, start = [], 0
        for pos in split_pos:
            blocks.append(df.iloc[start:pos+1].reset_index(drop=True))
            start = pos + 1
        blocks.append(df.iloc[start:].reset_index(drop=True))
        return blocks, gaps

    def compute_sliding_sucess(df, nb_of_trial):
        df = df.sort_values("trial_nb").reset_index(drop=True)
        s = df['sucess'].astype(int)
        n = len(s)
        if n < nb_of_trial:
            return None
        rates = []
        for i in range(n):
            if i <= n - nb_of_trial:
                window = s.iloc[i:i+nb_of_trial]
            else:
                window = s.iloc[i-nb_of_trial+1:i+1]
            rates.append(window.mean() * 100.0)
        df['instantanious_sucess'] = rates
        return df

    final = []
    gaps_array = {'mouse': [], 'gaps': []}
    len_block = {'mouse': [], 'len_block': []}
    for (mouse, day, session), side_df in metadata_df.groupby(['mouse', 'day', 'session']):
        tag = 0
        blocks, gaps = split_by_block(side_df, nb_of_unintended_trial)
        for gap in gaps:
            len_block['mouse'].append(mouse)
            len_block['len_block'].append(gap)
            
        for cur in blocks:
            len_block['mouse'].append(mouse)
            len_block['len_block'].append(len(cur))
                
            if len(cur) < nb_of_trial_per_block:
                continue
            # cur = compute_sliding_sucess(cur, nb_of_trial_for_instantanious_sucess)
            block_sucess = (len(cur[cur['sucess']])/len(cur))*100
            cur['instantanious_sucess'] = [block_sucess]*len(cur)
            cur['block_tag'] = [f'{tag}_{mouse}_{day}_{session}']*len(cur)
            tag += 1
            if cur is not None:
                final.append(cur)
                
    return pd.concat(final, ignore_index=True) if final else pd.DataFrame(), pd.DataFrame(gaps_array), pd.DataFrame(len_block)