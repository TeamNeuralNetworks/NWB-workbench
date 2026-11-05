# -*- coding: utf-8 -*-
"""
Created on Thu Feb 29 17:59:20 2024

@author: PierreA.DERRIEN
"""
from pynwb.core import DynamicTable

def convert_DF_to_DynTable(df, name = "name of the table", description = "description of the table"):
    """
    Generale function to convert a pandas dataframe to a NWB dynamic table automatically

    Parameters
    ----------
    df : pd.Dataframe
        DESCRIPTION.
    name : str, optional
        Name givent to the generated table. The default is "name of the table".
    description : str, optional
        Description givent to the generated table. The default is "description of the table".

    Returns
    -------
    table : NWB DynamicTable

    """
    table = DynamicTable(name=name,
                          description=description)
    for col in df.columns:
        table.add_column(name=col, description=col)
    for i, row in df.iterrows():
        table.add_row(**row.to_dict())
        
    return table