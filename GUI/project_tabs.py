"""Project-specific workflow tabs and their lazy-loaded panel definitions.

Add future implemented tabs here. A panel accepts right_panel and GUI_instance;
it may expose set_target_paths, is_running, runningStateChanged or job_manager.
Its right_view is a method on RightPanel. Registering a checkbox alone does not
implement a new workflow or its right-hand view.
"""
from GUI.project_paths import json_read as _pp_json_read
import importlib
import json
from pathlib import Path

PROJECT_METADATA_NAME = 'nwb_workbench_project.json'
TAB_DEFINITIONS = (
    dict(id='processing', title='Processing', module='GUI.panels.processing_panel',
         class_name='ProcessingPanel', attribute='processing_panel', right_view='show_processing_view', default=True),
    dict(id='deeplabcut', title='DeepLabCut', module='GUI.panels.deeplabcut_panel',
         class_name='DeepLabCutPanel', attribute='deeplabcut_panel', right_view='show_deeplabcut_view', default=False),
    dict(id='spike_sorting', title='Spike sorting', module='GUI.panels.spike_sorting_panel',
         class_name='SpikeSortingPanel', attribute='spike_sorting_panel', right_view='show_spike_sorting_view', default=True),
    dict(id='analysis', title='Analysis', module='GUI.panels.analysis_panel',
         class_name='AnalysisPanel', attribute='analysis_panel', right_view='show_analysis_view', default=True),
)


def default_enabled_tabs():
    return ['content'] + [tab['id'] for tab in TAB_DEFINITIONS if tab.get('default', True)]


def normalize_enabled_tabs(tabs):
    if tabs is None:
        return default_enabled_tabs()
    if not isinstance(tabs, (list, tuple)) or not all(isinstance(t, str) for t in tabs):
        raise ValueError('enabled_tabs must be a list of tab identifiers.')
    # Retain unknown IDs for compatibility with other application versions.
    return list(dict.fromkeys(['content'] + [t.strip() for t in tabs if t.strip()]))


def read_enabled_tabs(project_root):
    path = Path(project_root) / PROJECT_METADATA_NAME
    if not path.exists():
        return default_enabled_tabs()
    metadata = _pp_json_read(path)
    return normalize_enabled_tabs(metadata.get('enabled_tabs'))


def create_panel(definition, *, right_panel, GUI_instance):
    module = importlib.import_module(definition['module'])
    factory = getattr(module, definition['class_name'])
    return factory(right_panel=right_panel, GUI_instance=GUI_instance)
