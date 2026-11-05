# -*- coding: utf-8 -*-
"""
Created on Mon Feb  5 10:36:52 2024

@author: Pierre-Alexis
"""
import numpy as np
import pandas as pd
import time
import h5py

class DoricCSV ():
    
    def open_file(self, path:str):
        
        """ Simple file opener for the CSV obtained with the DORIC recording systems
        Parameters:
        - path (str): The path to the CSV file.
        Returns:
        None
        """
        
        csv_data = pd.read_csv(path, skiprows=1)
        csv_data.set_index("Time(s)", inplace=True)
        self.data = csv_data
        self.metaData =  pd.DataFrame({"Name":csv_data.columns})



class DoricDORIC ():
    
    def open_file(self ,path:str):
         """
         Open and preprocess an HDF5 file.
    
         Parameters:
         - path (str): The path to the HDF5 file.
         - config_path (str): The path to the HDF5 file.
         Returns:
         None
         """
         # Initialize
         # init()
         self.start = time.time()
    
         # Open the HDF5 file
         self.path = path
         with h5py.File(path, 'r', rdcc_nbytes=2048*3 ,driver='core',swmr=False) as f :
             self.file = f
             self.raw_data = self._explore_hdf5(file=self.file,data= pd.DataFrame(columns=["Values", "MetaData"]),parent='')
            
             # Preprocess the data
             self.data, self.metaData = self._preprocess_data(self.raw_data)
    
    def _explore_hdf5(self,file, parent:str='', data= None, resolution_divider:int =1)-> pd.DataFrame:
        """
        Recursively explores an HDF5 file, extracts datasets and their attributes, and stores them in a DataFrame.
    
        Args:
            file (h5py.File or h5py.Group): HDF5 file or group object.
            parent (str, optional): Parent path in the HDF5 file. Defaults to ''.
            data (pd.DataFrame, optional): DataFrame to store extracted data. Defaults to an empty DataFrame.
            resolution_divider (int, optional): Resolution divider for extracting data values. Defaults to 10000.
    
        Returns:
            pd.DataFrame: DataFrame containing extracted datasets and attributes.
    
        """
        for key in file.keys():
            
            item = file[key]
            path = f"{parent}/{key}" if parent else key
    
            if isinstance(item, h5py.Group):
                # Recursively explore HDF5 groups
                self._explore_hdf5(item, parent=path, data=data)
            elif isinstance(item, h5py.Dataset):
                pass
                # Extract dataset and its attributes and add them to the DataFrame
                
                keys = list(item.attrs)
                values = list(item.attrs.values())
                
                data.loc[" ".join(path.split("/")[-2:])] = [np.asarray(item),{keys[i]: values[i] for i in range(len(keys)) if keys}]
                del(keys)
                del(values)
            else:
                # Handle unknown objects
                print(f"Unknown object: {path}")
        
        return data


    def _preprocess_data(self,df: pd.DataFrame, resize: bool = False, method: str = 'mean') -> pd.DataFrame:
        """
        Preprocesses the input DataFrame by rearranging columns, merging data, and sorting index.
    
        Args:
            df (pd.DataFrame): Input DataFrame with columns 'Values', 'MetaData', 'Channel', and 'Time'.
    
        Returns:
            pd.DataFrame: Processed DataFrame with sorted index and rearranged columns.
    
        Raises:
            Exception: If DataFrame columns are not ['Values', 'MetaData'].
    
        """
        expected_columns = ["Values", "MetaData"]
    
        # Checking if DataFrame columns match the expected columns
        if list(df.columns) != expected_columns:
            raise Exception(f"DataFrame columns aren't: {expected_columns}")
    
        # Extracting 'Channel' and 'Time' from the index
        df["Channel"] = [index.split(" ")[0] for index in df.index]
        df["Time"] = ["Time" in index for index in df.index]
        
        # Grouping the DataFrame by 'Time'
        groups = df.groupby(by="Time").groups
        Times= groups[True]
        Values= groups[False]
    
        # Creating new DataFrames based on 'Time' groups
        Times = df.loc[Times].rename({"Values": "Times"}, axis=1)[["Times", "Channel"]]
        Values= df.loc[Values]
        
        # Merging the two DataFrames based on 'Channel' and dropping 'Time'
        df = Times.merge(Values, on="Channel").drop(["Time","Channel"], axis=1)
     
        Meta_Data = pd.DataFrame.from_records(df["MetaData"].values)
        Meta_Data.set_index("Username",inplace=True)
       
        min_NaN = max([lst[0] for lst in  df["Times"].values])
        max_NaN = min([lst[-1] for lst in  df["Times"].values])
        
        final_df = pd.DataFrame(dtype=float)
        
        dup = 0
        for _, row in df.iterrows():
            
            serie = pd.Series(index=row["Times"].astype(float),data = row["Values"].astype(float))
            serie = serie[~serie.index.duplicated()]
            serie = serie[(serie.index >=min_NaN) & (max_NaN >= serie.index )]
            if str(row["MetaData"]["Username"]) in final_df.columns:
                final_df[ str(row["MetaData"]["Username"])+"_"+str(dup)] = serie
                dup +=1
            else:    
                final_df[ str(row["MetaData"]["Username"])] = serie
            
        
        return final_df.sort_index(),Meta_Data
         
             

    
