# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('D:/Functionnal_Imaging_Electrophysiology/GUI/assets', 'GUI/assets'), ('D:/Functionnal_Imaging_Electrophysiology/Spike_sorting/Preprocessing', 'Spike_sorting/Preprocessing')]
binaries = []
hiddenimports = ['Tools.General.plotting.add_stats_on_boxplot', 'Analyses', 'Analyses.Behaviour', 'Analyses.Electrophysiology', 'Analyses.Electrophysiology.plot_spectrograme', 'Analyses.General', 'Builders', 'Builders.Electrophysiology', 'Builders.Electrophysiology.builder', 'Builders.Electrophysiology.convert_excel_to_nwb', 'Builders.Electrophysiology.file_openers', 'Builders.Electrophysiology.file_openers.custom_opener', 'Builders.Electrophysiology.file_openers.intan_system_file', 'Builders.Electrophysiology.file_openers.spikeinterface_opener', 'Builders.Fiberphotometry', 'Builders.Fiberphotometry.Add_events_tables', 'Builders.Fiberphotometry.Example', 'Builders.Fiberphotometry.Example_convert_from_Excel', 'Builders.Fiberphotometry.NWB_to_Dataframe', 'Builders.Fiberphotometry.Utils', 'Builders.Fiberphotometry.builder', 'Builders.Fiberphotometry.convert_from_excel', 'Builders.Fiberphotometry.file_openers', 'Builders.Fiberphotometry.file_openers.doric_system_file', 'Builders.Fiberphotometry.file_openers.labview_system_file', 'Builders.General', 'Builders.General.NWB_builder', 'Builders.General.events_toolbox', 'Builders.General.generate_nwb_inventory', 'Builders.General.scan_update_data_inventory', 'GUI', 'GUI.analysis_discovery', 'GUI.analysis_runner', 'GUI.app', 'GUI.events', 'GUI.frozen_project', 'GUI.loading_screen', 'GUI.panels', 'GUI.panels.analysis_panel', 'GUI.panels.content_object_manager', 'GUI.panels.content_panel', 'GUI.panels.file_tree_panel', 'GUI.panels.filter_panel', 'GUI.panels.new_spike_sorting_dialog', 'GUI.panels.plot_panel', 'GUI.panels.preprocessed_data_manager', 'GUI.panels.processing_panel', 'GUI.panels.right_panel', 'GUI.panels.spike_sorting_panel', 'GUI.panels.spike_sorting_preprocessing_discovery', 'GUI.panels.spike_sorting_right_views', 'GUI.pipeline_store', 'GUI.processing', 'GUI.processing_discovery', 'GUI.processing_runner', 'GUI.project_launcher', 'GUI.project_script_loader', 'GUI.qt_compat', 'GUI.startup_splash', 'GUI.view_state', 'Processings', 'Processings.Electrophysiology', 'Processings.Electrophysiology.denoise_extracellulaire_electrophy_for_LFP', 'Processings.Electrophysiology.extract_LFP', 'Processings.Electrophysiology.extract_coherence', 'Processings.Electrophysiology.hilbert_transfom', 'Processings.General', 'Spike_sorting', 'Spike_sorting.Preprocessing.bandpass', 'Spike_sorting.Preprocessing.common_reference_removal', 'Tools', 'Tools.Electrophysiology', 'Tools.Electrophysiology.MEA_to_NWB', 'Tools.Electrophysiology.MEA_to_NWB.File_explorer', 'Tools.Electrophysiology.MEA_to_NWB.NWB_builder', 'Tools.Electrophysiology.MEA_to_NWB.Utility_NWB', 'Tools.Electrophysiology.burst_detection', 'Tools.Electrophysiology.burst_detection.logISI', 'Tools.Electrophysiology.burst_detection.logISI.Test_reimplementation', 'Tools.Electrophysiology.burst_detection.utility', 'Tools.Electrophysiology.burst_detection.utility.Realigner', 'Tools.Electrophysiology.filtering', 'Tools.Electrophysiology.filtering.filtering_toolbox', 'Tools.Fiberphotometry', 'Tools.Fiberphotometry.Analysis_exemple', 'Tools.Fiberphotometry.Analysis_exemple.Simple_analysis', 'Tools.Fiberphotometry.NWB_builders', 'Tools.Fiberphotometry.NWB_builders.Add_events_tables', 'Tools.Fiberphotometry.NWB_builders.Example', 'Tools.Fiberphotometry.NWB_builders.Example_convert_from_Excel', 'Tools.Fiberphotometry.NWB_builders.NWB_to_Dataframe', 'Tools.Fiberphotometry.NWB_builders.Utils', 'Tools.Fiberphotometry.NWB_builders.builder_photometry', 'Tools.Fiberphotometry.NWB_builders.convert_from_excel', 'Tools.Fiberphotometry.NWB_builders.saver', 'Tools.Fiberphotometry.analysis_tools', 'Tools.Fiberphotometry.analysis_tools.artefact_detectors', 'Tools.Fiberphotometry.analysis_tools.custom_Zscore', 'Tools.Fiberphotometry.analysis_tools.parameter_detector', 'Tools.Fiberphotometry.analysis_tools.trials_extraction', 'Tools.Fiberphotometry.file_openers', 'Tools.Fiberphotometry.file_openers.doric_system_file', 'Tools.Fiberphotometry.file_openers.labview_system_file', 'Tools.Fiberphotometry.file_openers.test_openers', 'Tools.Fiberphotometry.preprocessing_tools', 'Tools.Fiberphotometry.preprocessing_tools.fiber_filters', 'Tools.Fiberphotometry.preprocessing_tools.photobleaching_corrector', 'Tools.Fiberphotometry.preprocessing_tools.signals_demodulators', 'Tools.General', 'Tools.General.AnalysisResult', 'Tools.General.FilteredConditionFileManager', 'Tools.General.PySimpleGUI', 'Tools.General.PySimpleGUI.PySimpleGUI', 'Tools.General.compute_metrics', 'Tools.General.filter_data', 'Tools.General.folder_crawlers', 'Tools.General.nwb_backend', 'Tools.General.plotting', 'Tools.General.plotting.add_stats_on_boxplot', 'Tools.General.plotting.plot_unit_summary', 'Tools.General.plotting.plotting_toolbox', 'Tools.General.preprocessed_data_inventory', 'Tools.General.printers', 'Tools.General.python_to_matlab_tools', 'Tools.General.timestamps_handler']
hiddenimports += collect_submodules('Tools')
tmp_ret = collect_all('astropy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tensorpac')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pynwb')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('hdmf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('spikeinterface')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('probeinterface')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/Functionnal_Imaging_Electrophysiology/launch_workbench_exe.py'],
    pathex=['D:/Functionnal_Imaging_Electrophysiology'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NWB Workbench',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/Functionnal_Imaging_Electrophysiology/GUI/assets/nwb_explorer_logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NWB Workbench',
)
