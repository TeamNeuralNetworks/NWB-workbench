# -*- coding: utf-8 -*-
"""
Created on Thu Apr 11 14:45:18 2024

@author: PierreA.DERRIEN
"""

from glob import glob
import os 


def folder_crawler(path, print_folder = True):  
    folder_list = glob(path+"/*/")
    for folder in folder_list:
        if print_folder:
            print(folder)
        folder_crawler(folder, print_folder)

def copy_folder_structure(path, save_path, print_folder = True):  
    folder_list = glob(path+"/*/")
    for folder in folder_list:
        save_direc = folder.replace(path, save_path)
        if os.path.exists(save_direc)== False: 
            os.makedirs(save_direc)
        if print_folder:
            print(folder)
            print(save_direc)
        copy_folder_structure(folder, save_direc, print_folder)

def folder_crawler_with_action(path, action, action_parameter, print_folder = True):  
    folder_list = glob(path+"/*/")
    for folder in folder_list:
        if print_folder:
            print(folder)
        action(folder, **action_parameter)
        folder_crawler_with_action(folder, action, action_parameter, print_folder )


if __name__ == "__main__":
    path = r"Path\To\folder\To\scroll"
    save_path = r"Path\To\folder\To\save"
    #  exemple of just iterating on a folder to print path
    folder_crawler(path)
    # exemple of copying the folder structure of one folder to another
    copy_folder_structure(path, save_path)
    # copy the folder structure and put a log.txt in it
    def action(folder, path, save_path):
         save_direc = folder.replace(path, save_path)
         if os.path.exists(save_direc)== False: 
             os.makedirs(save_direc)
        with open(os.path.join(save_direc ,'readme.txt'), 'w') as f:
             f.write('readme')
    folder_crawler_with_action(path, action, action_parameter = {"path":path, "save_path":save_path}, print_folder = True)