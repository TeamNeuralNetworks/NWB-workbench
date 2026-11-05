# -*- coding: utf-8 -*-
"""
Created on Sat Jul 29 18:43:38 2023

@author: pierre.LE-CABEC
"""
import numpy as np
import pandas as pd
from itertools import combinations
from scipy import stats


def hedges_g(x, y, paired=False):
    x, y = np.array(x), np.array(y)
    
    if paired:
        diff = x - y
        n = len(diff)
        d = diff.mean() / diff.std(ddof=1)
    else:
        nx, ny = len(x), len(y)
        pooled_sd = np.sqrt(((nx-1)*x.std(ddof=1)**2 + (ny-1)*y.std(ddof=1)**2) / (nx+ny-2))
        d = (x.mean() - y.mean()) / pooled_sd
        n = nx + ny
    
    # correction for small sample bias → Hedges’ g
    correction = 1 - (3 / (4*n - 9))
    return d * correction

def bootstrap_ci(x, y, func, n_boot=5000, alpha=0.05, paired=False):
    boot_stats = []
    n = len(x)
    for _ in range(n_boot):
        idx = np.random.randint(0, n, n)
        xb, yb = x[idx], y[idx] if paired else np.random.choice(y, len(y), replace=True)
        boot_stats.append(func(xb, yb, paired=paired))
    lower = np.percentile(boot_stats, 100*alpha/2)
    upper = np.percentile(boot_stats, 100*(1-alpha/2))
    return lower, upper

def format_number(number):
    if number < 0.001:
        return format(number, ".1E")
    else:
       return round(number, 3)

   
def find_position_in_boxplot(data, ax, hue_label, specific_hue, x_label, specific_x, y_label):
    
    median_positions = list(set([flier.get_xdata()[0] for flier in ax.lines if len(set(flier.get_xdata())) == 1]))
    median_positions.sort()

    x_order = ax.get_xticklabels()
    x_order_text = [text.get_text() for text in x_order]
    x_index = x_order_text.index(str(specific_x))
    
    if hue_label is None:
        final_xposition = x_index
        final_yposition = data[(data[x_label] == specific_x)][y_label].max()
    else:
        lenght_of_sublist = len(median_positions) // len(x_order_text)
        if x_index == 0:
            current_median_position_list = median_positions[:lenght_of_sublist]
        elif x_index == len(x_order_text)-1:
            current_median_position_list = median_positions[-lenght_of_sublist:]
        else:
            current_median_position_list = median_positions[(lenght_of_sublist*x_index):((lenght_of_sublist*x_index)+lenght_of_sublist+1)]
    
        current_hue_order = ax.get_legend().get_texts()
        current_hue_order = [text.get_text() for text in current_hue_order]
        hue_order_indx = current_hue_order.index(specific_hue)
        
        final_xposition = current_median_position_list[hue_order_indx]
    
        final_yposition = data[(data[x_label] == specific_x) & (data[hue_label] == specific_hue)][y_label].max()
    
    return final_xposition, final_yposition

def plot_significant_bar(first_x_pos, first_y_pos, second_x_pos, second_y_pos, stats_result, stats_func, ax, stats_disp_style, stats_disply_thres, stats_n, y_padding=None):
    y_pos = max((first_y_pos, second_y_pos))
    y_range = ax.get_ylim()[1] - ax.get_ylim()[0]
    # ax.set_ylim(None,  y_pos + y_range*0.075)
    if y_padding is None:
        y_padding = 0
        
    ax.hlines(y_pos + y_range*0.02 + y_padding, first_x_pos, second_x_pos, color='black', lw=1)
    
    ax.vlines(first_x_pos, y_pos-y_range*0.01 + y_range*0.02 + y_padding, y_pos + y_range*0.02 + y_padding, color='black', lw=1)
    ax.vlines(second_x_pos, y_pos-y_range*0.01 + y_range*0.02 + y_padding , y_pos + y_range*0.02 + y_padding, color='black', lw=1)
    
    
        
    if stats_disp_style == 'full':
        ax.text(np.mean((first_x_pos, second_x_pos)), y_pos + y_range*0.05 + y_range*y_padding, f'{stats_func}: {stats_result} (n={stats_n})', ha='center', va='center', fontsize=12)
    elif stats_disp_style == 'simple':
        ax.text(np.mean((first_x_pos, second_x_pos)), y_pos + y_range*0.05 + y_range*y_padding, stats_result, ha='center', va='center', fontsize=12)
    elif stats_disp_style == 'star':
        ax.text(np.mean((first_x_pos, second_x_pos)), y_pos + y_range*0.05 + y_range*y_padding, stats_disply_thres, ha='center', va='center', fontsize=12)

def detect_collision(first_x_array, second_x_array, y_array, y_range, offset=0.065, max_iterations=100):
    y_offset = y_range * offset
    
    coordonate_list = [[min(first_x, second_x), max(first_x, second_x), y, indx] for indx, (first_x, second_x, y) in enumerate(zip(first_x_array, second_x_array, y_array))]
    coordonate_list.sort(key=lambda x: x[2])  # Sort the list based on y
    
    for indx, (first_x, second_x, y, _) in enumerate(coordonate_list):
        for collision_first_x, collision_second_x, collision_y, _ in coordonate_list[:]:  # Fix the index slicing
            if (
                ((first_x >= collision_first_x and first_x <= collision_second_x) or 
                 (second_x >= collision_first_x and second_x <= collision_second_x) or 
                (first_x >= collision_first_x and second_x <= collision_second_x) or 
                (first_x <= collision_first_x and second_x >= collision_second_x))
            and (y < (collision_y + y_offset))):
                y = collision_y + y_offset
                coordonate_list[indx][2] = y

    # Extract arrays from the NumPy array directly
    coordonate_list.sort(key=lambda x: x[3]) 
    coordonate_list = np.array(coordonate_list)  # Convert to NumPy array
    collision_free_y_array = coordonate_list[:, 2]
    return collision_free_y_array

def significant_bar_main(significant_pos_dict, stats_disp_style, y_padding=None):    
    x_lim = significant_pos_dict['ax'].get_xlim()
    y_range = significant_pos_dict['ax'].get_ylim()[1] - significant_pos_dict['ax'].get_ylim()[0]
    
    first_x = np.array(significant_pos_dict['first_x_pos'])
    second_x = np.array(significant_pos_dict['second_x_pos'])
    stats_result_list = np.array(significant_pos_dict['stats_result'])
    stats_disply_thres_list = np.array(significant_pos_dict['stats_disply_thres'])
    stats_test_name_array = np.array(significant_pos_dict['stats_func'])
    stats_n_array = np.array(significant_pos_dict['n'])
    y_array = np.array([max(first_y_value, second_y_value) for first_y_value, second_y_value in zip(significant_pos_dict['first_y_pos'], significant_pos_dict['second_y_pos'])])
    
    y_array = detect_collision(first_x, second_x, y_array, y_range)

        
    significant_pos_dict['ax'].set_ylim(None, y_array.max()*1.2)
    
    for indx, (first_x_pos, first_y_pos, second_x_pos, second_y_pos, stats_result, stats_disply_thres, stats_test_name, stats_n, ) in enumerate(zip(first_x, 
                                                                                                                                                y_array, 
                                                                                                                                                second_x, 
                                                                                                                                                y_array, 
                                                                                                                                                stats_result_list, 
                                                                                                                                                stats_disply_thres_list,
                                                                                                                                                stats_test_name_array,
                                                                                                                                                stats_n_array,
                                                                                                                                                )):
        plot_significant_bar(first_x_pos, first_y_pos, second_x_pos, second_y_pos, 
                             stats_result, stats_test_name,
                             significant_pos_dict['ax'], stats_disp_style, 
                             stats_disply_thres,
                             stats_n,
                             y_padding)
   
    significant_pos_dict['ax'].set_xlim(x_lim[0], x_lim[1])
    
def drop_outliers_IQR(df, data_column_name):

   q1=df[data_column_name].quantile(0.25)

   q3=df[data_column_name].quantile(0.75)

   IQR=q3-q1

   not_outliers = df[~((df[data_column_name]<(q1-1.5*IQR)) | (df[data_column_name]>(q3+1.5*IQR)))]


   return not_outliers

def list_of_test_pair_maker(boxplot_data, x_label, y_label, hue_label, individue_label):
    
    list_of_test_pair = []

    values_x = boxplot_data[x_label].unique()
    boxplot_data_list = [boxplot_data[boxplot_data[x_label] == current_x] for current_x in values_x]
    
    if hue_label is None:
        return list(combinations(boxplot_data_list, 2))
    
    else:
        for current_boxplot_data in boxplot_data_list:
            values_hue = current_boxplot_data[hue_label].unique()
            combination_list = list(combinations(values_hue, 2))
            for first_hue, second_hue in combination_list:
                x_data = current_boxplot_data[(current_boxplot_data[hue_label]==first_hue)]
                y_data = current_boxplot_data[(current_boxplot_data[hue_label]==second_hue)]
                list_of_test_pair.append((x_data, y_data))

        return list_of_test_pair
        
def add_stats_on_boxplot(boxplot_data,
                         ax_boxplot, 
                         x_label,
                         y_label, 
                         hue_label=None,
                         remove_outlier=False,
                         individu_label=None,  
                         stats_func='effect_size', 
                         paired=True,
                         stats_disp_style='full',
                         significant_only=False,
                         modify_title=True,
                         y_padding=None,
                         alternative='two-sided'):
    
    
    if alternative is None:
        alternative = 'two-sided'
    
    if stats_disp_style != 'full' and modify_title:
        current_title = ax_boxplot.get_title()
        # Concatenate the new text to the current title
        if paired:
            new_text = f'\nPaired {stats_func}'
        else:
            new_text = f'\n{stats_func}'
        if remove_outlier:
            new_text = f'{new_text} (Outlier removed)'
        updated_title = current_title + new_text
        # Set the updated title
        ax_boxplot.set_title(updated_title)
        
    boxplot_data_stats = pd.DataFrame()
    significant_pos_dict = {'first_x_pos': [],
                            'first_y_pos': [],
                            'second_x_pos': [],
                            'second_y_pos': [],
                            'stats_result': [],
                            'stats_disply_thres': [],
                            'stats_func': [],
                            'n': [],
                            'ax': ax_boxplot}

    
    if paired:
        if individu_label is not None:
            boxplot_data.sort_values(by=individu_label, inplace=True)
        else:
            print('Paired test is requested but no individual label was provided preventing a sorting to insure correct alinement of data')
    
    list_of_test_pair = list_of_test_pair_maker(boxplot_data, x_label, y_label, hue_label, individu_label)
    for x_data, y_data in list_of_test_pair:
                
        if remove_outlier:
            x_data = drop_outliers_IQR(x_data, y_label)
            y_data = drop_outliers_IQR(y_data, y_label)
        
        if paired:
            x_data = x_data[x_data[individu_label].isin(y_data[individu_label])]
            y_data = y_data[y_data[individu_label].isin(x_data[individu_label])]
            
            x_data.sort_values(by=[individu_label], inplace=True, ignore_index=True)
            y_data.sort_values(by=[individu_label], inplace=True, ignore_index=True)
            
            
            current_paired = True if paired and x_data[individu_label].tolist() == y_data[individu_label].tolist() else False
        else:
            current_paired = False
        
        if alternative != 'two-sided':
            
            if hue_label is not None:
                curent_xdatalabel = x_data[hue_label].unique()[0]
                curent_ydatalabel = y_data[hue_label].unique()[0]
            else:
                curent_xdatalabel = x_data[x_label].unique()[0]
                curent_ydatalabel = y_data[x_label].unique()[0]
            
            alternativ_match_found = False
            for curent_test in alternative:
                if curent_xdatalabel == curent_test[1] and curent_ydatalabel == curent_test[2]:
                    x_data_test = y_data[y_label]
                    y_data_test = x_data[y_label]
                elif curent_xdatalabel == curent_test[2] and curent_ydatalabel == curent_test[1]:
                    x_data_test = x_data[y_label]
                    y_data_test = y_data[y_label]
                else:
                     continue
                 
                if alternativ_match_found:
                    raise ValueError(f'Multiple match found in alternative:{curent_test}')
                else:
                    alternativ_match_found = True
                    current_alternative = curent_test[0]
                    
            if not alternativ_match_found: 
                raise ValueError(f'No match foun in alternative:{alternative}')
                              
        else:
            current_alternative = alternative
            x_data_test = x_data[y_label]
            y_data_test = y_data[y_label]
        
        n=len(x_data_test)
        
        if not current_paired and paired:
            current_stats_func = stats_func.strip('paired_').strip('paired')
        else:
            current_stats_func = stats_func 
            
        if stats_func == 'effect_size':
            stats_result = abs(hedges_g(x_data_test, y_data_test, paired=current_paired))
            ci = bootstrap_ci(np.array(x_data_test), np.array(y_data_test), hedges_g, paired=current_paired)

            if stats_result is np.nan:
                stats_disply_thres = 'Nan'
                signigicant=False
            elif stats_result > 0.8:
                stats_disply_thres = '***' 
                signigicant=True
            elif stats_result > 0.5:
                stats_disply_thres = '**'
                signigicant=True
            elif stats_result > 0.3:
                stats_disply_thres = '**'
                signigicant=True
            else:
                stats_disply_thres = 'NS'
                signigicant=False
            
            stats_result = f'{round(stats_result, 3)}\n({round(ci[0], 3)}-{round(ci[1], 3)})'
            # stats_result = f'({round(ci[0], 3)}-{round(ci[1], 3)})'
                
        elif 't_test' in stats_func :
            if current_paired:
                t, stats_result = stats.ttest_rel(x_data_test, y_data_test, alternative=current_alternative)
            else:
                t, stats_result = stats.ttest_ind(x_data_test, y_data_test, equal_var=False, alternative=current_alternative)

            if stats_result is np.nan:
                stats_disply_thres = 'Nan'
                signigicant=False
            elif stats_result > 0.05:
                stats_disply_thres = 'NS'
                signigicant=False
            elif stats_result > 0.005:
                stats_disply_thres = '*'
                signigicant=True
            elif stats_result > 0.0005:
                stats_disply_thres = '**'
                signigicant=True
            else:
                stats_disply_thres = '***'
                signigicant=True
        else:
            raise ValueError(f'Test: {stats_func} has not been implemented')
        
        if not isinstance(stats_result, str):
            processed_stats_result = format_number(stats_result)
        else:
            processed_stats_result = stats_result
        
        first_x = x_data[x_label].unique()
        if len(first_x) == 1:
            first_x = first_x[0]
        else:
            raise ValueError('Unexpected number of x_label unconter')
        second_x = y_data[x_label].unique()
        if len(second_x) == 1:
            second_x = second_x[0]
        else:
            raise ValueError('Unexpected number of x_label unconter')
        
        if hue_label is None:
            boxplot_data_stats = pd.concat((boxplot_data_stats, pd.DataFrame({x_label: [f'{first_x}VS{second_x}',], 
                                                                              stats_func: [processed_stats_result,]})))
            if significant_only and not signigicant:
                continue
            first_x_pos, first_y_pos = find_position_in_boxplot(boxplot_data, ax_boxplot, hue_label, None,  x_label, first_x, y_label)
            second_x_pos, second_y_pos = find_position_in_boxplot(boxplot_data, ax_boxplot, hue_label, None,  x_label, second_x, y_label)
            significant_pos_dict['first_x_pos'].append(first_x_pos)
            significant_pos_dict['first_y_pos'].append(first_y_pos)
            significant_pos_dict['second_x_pos'].append(second_x_pos)
            significant_pos_dict['second_y_pos'].append(second_y_pos)
            significant_pos_dict['stats_result'].append(processed_stats_result)
            significant_pos_dict['stats_func'].append(current_stats_func)
            significant_pos_dict['stats_disply_thres'].append(stats_disply_thres)
            significant_pos_dict['n'].append(n)
                
        else:
            first_hue = x_data[hue_label].unique()
            if len(first_hue) == 1:
                first_hue = first_hue[0]
            else:
                raise ValueError('Unexpected number of x_label unconter')
                
            second_hue = y_data[hue_label].unique()
            if len(second_hue) == 1:
                second_hue = second_hue[0]
            else:
                raise ValueError('Unexpected number of x_label unconter')
                
            boxplot_data_stats = pd.concat((boxplot_data_stats, pd.DataFrame({x_label: [f'{first_x}VS{second_x}',], 
                                                                              hue_label: [f'{first_hue}VS{second_hue}',], 
                                                                              stats_func: [processed_stats_result,]})))
        
            if first_x == second_x:
                if significant_only and not signigicant:
                    continue
                first_x_pos, first_y_pos = find_position_in_boxplot(boxplot_data, ax_boxplot, hue_label, first_hue,  x_label, first_x, y_label)
                second_x_pos, second_y_pos = find_position_in_boxplot(boxplot_data, ax_boxplot, hue_label, second_hue,  x_label, second_x, y_label)
                significant_pos_dict['first_x_pos'].append(first_x_pos)
                significant_pos_dict['first_y_pos'].append(first_y_pos)
                significant_pos_dict['second_x_pos'].append(second_x_pos)
                significant_pos_dict['second_y_pos'].append(second_y_pos)
                significant_pos_dict['stats_result'].append(processed_stats_result)
                significant_pos_dict['stats_func'].append(current_stats_func)
                significant_pos_dict['stats_disply_thres'].append(stats_disply_thres)
                significant_pos_dict['n'].append(n)
    
    if significant_pos_dict['stats_result']:
        significant_bar_main(significant_pos_dict, stats_disp_style, y_padding)
    return boxplot_data_stats