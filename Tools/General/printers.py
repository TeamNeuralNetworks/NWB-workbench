# -*- coding: utf-8 -*-
"""
Created on Mon May  6 11:28:06 2024

@author: PierreA.DERRIEN
"""

def recursive_printer(element, indent = 0):
    
    indent_space = ''.join(["| " for x in range(indent)])
    for key, item in element.items():
        if type(item) == dict:
            
            print(indent_space, key, " :")
            recursive_printer(item, indent = indent +1)
            pass
        elif type(item) == list or type(item) == tuple:
            print(indent_space, key, ":")
            for i in item:
                print(indent_space+"  "+str(i))
        else:
            print(indent_space, key, " : ", item)