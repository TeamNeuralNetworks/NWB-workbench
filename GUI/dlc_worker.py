"""Standalone DLC subprocess entry point; executed by the chosen DLC Python.
Do not import this module into Workbench. No shell commands or Qt imports.
"""
try:
    from project_paths import json_read as _pp_json_read, json_write as _pp_json_write
except ModuleNotFoundError:
    from GUI.project_paths import json_read as _pp_json_read, json_write as _pp_json_write
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from contextlib import contextmanager
from unittest.mock import patch
import sys
import traceback
import tempfile
import uuid
from datetime import datetime, timezone

try:
    from dlc_storage import atomic_json, extended_path
except ModuleNotFoundError:
    from GUI.dlc_storage import atomic_json, extended_path


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def create_empty_project(request, aux):
    """Initialize DLC's configuration template without requiring a seed video."""
    now=datetime.now()
    root=Path(request['working_directory']).resolve()
    name,scorer=request['name'],request['experimenter']
    if not name or not scorer or any(c in name+scorer for c in '/\\:'):
        raise ValueError('Project name and experimenter must not contain path separators.')
    folder=root/(name+'-'+scorer+'-'+now.strftime('%Y-%m-%d'))
    folder.mkdir(exist_ok=False)
    try:
        for child in ('videos','labeled-data','training-datasets','dlc-models','dlc-models-pytorch'):
            (folder/child).mkdir()
        cfg,_=aux.create_config_template()
        month=('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')[now.month-1]
        cfg.update(Task=name,scorer=scorer,project_path=str(folder),date=month+str(now.day),
                   video_sets={},multianimalproject=False,bodyparts=request['bodyparts'],skeleton=[],
                   engine='pytorch',default_augmenter='default',default_net_type='resnet_50',
                   cropping=False,start=0,stop=1,numframes2pick=request.get('frames',20),
                   TrainingFraction=[.95],iteration=0,snapshotindex=-1,detector_snapshotindex=-1,
                   x1=0,x2=640,y1=277,y2=624,batch_size=8,detector_batch_size=1,
                   corner2move2=(50,50),move2corner=True,skeleton_color='black',
                   pcutoff=.6,dotsize=12,alphavalue=.7,colormap='rainbow')
        config=folder/'config.yaml'
        aux.write_config(str(config),cfg)
        saved=aux.read_config(str(config))
        if saved.get('video_sets')!={} or saved.get('bodyparts')!=request['bodyparts']:
            raise ValueError('Empty project configuration verification failed.')
        print('Empty project created. Add training videos from the Videos page.',flush=True)
        return str(config)
    except Exception:
        shutil.rmtree(folder)
        raise


def labeling_summary(cfg):
    """Count frames with at least one labeled coordinate in the active scorer's tables."""
    import pandas as pd
    root = Path(cfg['project_path']) / 'labeled-data'
    result = []
    for video in cfg.get('video_sets', {}):
        name = Path(str(video).replace('\\', '/')).stem
        folder = root / name
        extracted = sum(p.is_file() and p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}
                        for p in folder.iterdir()) if folder.is_dir() else 0
        row = dict(video=str(video), labeled_frames=0, labeled=False, extracted_frames=extracted)
        table = folder / f"CollectedData_{cfg.get('scorer', '')}.h5"
        try:
            if table.is_file():
                data = pd.read_hdf(table)
                coords = data.loc[:, data.columns.get_level_values(-1).isin(['x', 'y'])]
                row['labeled_frames'] = int(coords.notna().any(axis=1).sum())
                row['labeled'] = row['labeled_frames'] > 0
        except Exception as exc:
            row['error'] = str(exc)
        result.append(row)
    return result


def training_label_review(config, cfg, request):
    """Review fresh annotation counts before dataset creation or model training."""
    labels = labeling_summary(cfg)
    unreadable = [row['video'] for row in labels if row.get('error')]
    if unreadable:
        raise ValueError('Cannot read saved annotations for: ' + ', '.join(unreadable))
    if not any(row['labeled_frames'] for row in labels):
        raise ValueError('No selected training video has saved labels. Open labeling and save annotations before training.')
    incomplete = [row for row in labels if not row['labeled'] or row['labeled_frames'] < row['extracted_frames']]
    if not incomplete:
        return None
    signature = hashlib.sha256((digest(config) + training_annotation_digest(cfg)
                               + json.dumps(labels, sort_keys=True)).encode()).hexdigest()
    if request.get('label_confirmation') == signature:
        return None
    return dict(token=signature, total=len(labels), incomplete=incomplete)


def evaluation_summary(folder):
    """Read the latest per-checkpoint DLC score report, not prediction tables."""
    import csv
    files = sorted((p for p in folder.glob('*results.csv') if 'keypoint' not in p.name.lower()
                    and 'combined' not in p.name.lower()), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    path = files[0]
    try:
        with path.open(newline='', encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        if not rows:
            return None
        row = rows[-1]
        metrics = {key: value for key, value in row.items() if key and value and
                   any(term in key.lower() for term in ('rmse', 'error'))}
        return dict(file=str(path), metrics=metrics,
                    epochs=row.get('Training epochs', row.get('Training iterations', '')))
    except (OSError, ValueError, csv.Error):
        return None


def training_annotation_digest(cfg):
    import pandas as pd
    root = Path(cfg['project_path']) / 'labeled-data'
    tables = []
    for video in cfg.get('video_sets', {}):
        folder = root / Path(str(video).replace('\\', '/')).stem
        table = folder / f"CollectedData_{cfg.get('scorer', '')}.h5"
        signature = None
        if table.is_file():
            try:
                data = pd.read_hdf(table)
                signature = hashlib.sha256(pd.util.hash_pandas_object(data, index=True).values.tobytes()
                                           + repr(data.columns.tolist()).encode()).hexdigest()
            except Exception as exc:
                raise ValueError(f'Cannot read annotations for {folder.name}: {exc}') from exc
        tables.append((str(video), signature))
    value = dict(tables=tables, bodyparts=cfg.get('bodyparts'), scorer=cfg.get('scorer'),
                 fractions=cfg.get('TrainingFraction'), iteration=cfg.get('iteration', 0))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare_training_dataset(dlc, config, aux, engine, shuffle, fraction, info, force=False):
    """Prepare missing or outdated untrained datasets before starting training."""
    cfg = aux.read_config(config)
    key = f'{shuffle}:{fraction}'
    state = load_selection_state(config)
    selected = [m for m in info.get('models', [])
                if m['shuffle'] == shuffle and m['trainingsetindex'] == fraction]
    signature = training_annotation_digest(cfg)
    saved = state.get('annotation_digests', {}).get(key)
    stale = key in state.get('stale_training_sets', []) or (saved is not None and saved != signature)
    trained = any(m.get('snapshots') for m in selected)
    if trained:
        if force or stale:
            raise ValueError('Training data changed or dataset preparation was requested for an already trained model. '
                             'Choose a new shuffle in Training parameters to preserve the existing model, then click Train model.')
        print('Using the existing trained model and prepared dataset.', flush=True)
        return
    if not force and selected and saved == signature and not stale:
        print('Training dataset is up to date.', flush=True)
        return
    if not any(row.get('labeled_frames', 0) for row in labeling_summary(cfg)):
        raise ValueError('No active training video has labeled frames. Open labeling and save annotations first.')
    print('Preparing training dataset before training...', flush=True)
    result = dlc.create_training_dataset(config, Shuffles=[shuffle], engine=engine)
    if result is None:
        raise ValueError('DLC did not create a training dataset. Check the annotations and log. Training was not started.')
    state = load_selection_state(config)
    state['stale_training_sets'] = [k for k in state.get('stale_training_sets', []) if k != key]
    state.setdefault('annotation_digests', {})[key] = signature
    atomic_json(selection_state_path(config), state)


def project_info(config, aux, engine):
    cfg = aux.read_config(config)
    if cfg.get('multianimalproject', False):
        raise ValueError('This Workbench integration currently supports single-animal DLC projects.')
    root = Path(cfg['project_path'])
    if not root.is_dir():
        raise ValueError('DLC project_path does not exist. Correct it in config.yaml before continuing.')
    models = []
    shuffles = {1}
    for folder in root.glob('dlc-models-pytorch/iteration-*/*'):
        match = re.search(r'shuffle(\d+)', folder.name)
        if match:
            shuffles.add(int(match[1]))
    for index, fraction in enumerate(cfg.get('TrainingFraction', [.95])):
        for shuffle in sorted(shuffles):
            relative = aux.get_model_folder(fraction, shuffle, cfg, engine=engine)
            directory = root / relative / 'train'
            snapshots = sorted(str(p.resolve()) for p in directory.glob('*.pt'))
            if snapshots or directory.exists():
                models.append(dict(shuffle=shuffle, trainingsetindex=index, fraction=fraction,
                                   folder=str(directory), snapshots=snapshots,
                                   evaluation=evaluation_summary(root / aux.get_evaluation_folder(fraction, shuffle, cfg, engine=engine))))
    return dict(config=str(Path(config).resolve()), name=str(cfg.get('Task', root.name)),
                bodyparts=list(cfg.get('bodyparts', [])), scorer=str(cfg.get('scorer','')), models=models,
                video_sets=list(cfg.get('video_sets', {})), project_path=str(root),
                training_fractions=list(cfg.get('TrainingFraction', [.95])), labeling=labeling_summary(cfg),
                trained_models=load_selection_state(config).get('trained_models', {}),
                stale_training_sets=load_selection_state(config).get('stale_training_sets', []))


def predictions_from_csv(csv_path, output):
    import numpy as np
    import pandas as pd
    frame = pd.read_csv(csv_path, header=[0, 1, 2], index_col=0)
    if frame.columns.nlevels != 3 or len(frame.columns.get_level_values(0).unique()) != 1:
        raise ValueError('Expected a single-animal, single-scorer DLC table.')
    indices = pd.to_numeric(frame.index, errors='raise').to_numpy()
    if not np.array_equal(indices, np.arange(len(frame))):
        raise ValueError('DLC result does not contain every frame in chronological order.')
    scorer = str(frame.columns.get_level_values(0)[0])
    parts = list(dict.fromkeys(str(v) for v in frame.columns.get_level_values(1)))
    values = np.stack([frame.loc[:, [(scorer,p,c) for c in ('x','y','likelihood')]].to_numpy(dtype=float)
                       for p in parts], axis=1)
    confidence = values[:,:,2]
    if not np.all(np.isfinite(confidence)) or np.any((confidence<0)|(confidence>1)):
        raise ValueError('Invalid DLC likelihood values.')
    if np.any(np.isinf(values[:,:,:2])):
        raise ValueError('Infinite tracking coordinates.')
    np.savez_compressed(output, values=values, bodyparts=np.array(parts), scorer=np.array(scorer))
    return dict(bodyparts=parts, scorer=scorer, frames=len(frame), predictions=str(output))


def project_video_paths(sources, existing=(), staging=None, use_originals=False):
    """DLC uses filename stems as annotation directories; alias colliding copies.
    Several NWB ImageSeries can reference one physical path; add that path once.
    Analysis keeps each NWB source separately to preserve its own timestamps.
    """
    paths = []
    seen = set()
    for source in sources:
        path = Path(source['video']).resolve()
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            paths.append(str(path))
    if not paths:
        raise ValueError('No video selected. In NWB videos, list the videos and check at least one.')
    by_name = {}
    for value in list(existing) + paths:
        by_name.setdefault(Path(value).stem.casefold(), []).append(str(value))
    conflicts = [values for values in by_name.values() if len(values) > 1]
    if conflicts or use_originals:
        if staging is None:
            raise ValueError('Video filename collisions require a staging directory.')
        staging = Path(staging)
        staging.mkdir(parents=True, exist_ok=True)
        reserved = {Path(value).stem.casefold() for value in existing}
        conflict_names = {Path(values[0]).stem.casefold() for values in conflicts}
        mapping = []
        prepared = []
        for original in paths:
            source = Path(original)
            target = source
            if source.stem.casefold() in conflict_names or use_originals:
                identifier = hashlib.sha256(os.path.normcase(str(source)).encode('utf-8')).hexdigest()[:16]
                stem = source.stem[:100] + '__' + identifier
                if stem.casefold() in reserved:
                    raise ValueError('This video alias already exists in the DLC project: ' + stem)
                target = staging / (stem + source.suffix)
                if use_originals:
                    link_video(source, target)
                else:
                    with source.open('rb') as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                    shutil.copystat(source, target)
                print('DLC video alias: ' + str(source) + ' -> ' + target.name, flush=True)
            reserved.add(target.stem.casefold())
            mapping.append(dict(original=str(source), project_input=str(target)))
            prepared.append(str(target))
        atomic_json(staging / 'video_aliases.json', mapping)
        paths = prepared
    return paths


def link_video(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Video destination already exists: '+str(destination))
    try:
        destination.symlink_to(source)
    except OSError as symlink_error:
        # Hard links share the existing data without requiring symlink privilege.
        # os.link fails safely across volumes or on unsupported filesystems.
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('Video destination already exists: '+str(destination)) from symlink_error
        try:
            os.link(source, destination)
        except OSError as hardlink_error:
            raise RuntimeError(
                'Could not create a symbolic link or a hard link to the original video. '
                'Hard links require the same supported volume; network shares or exFAT may not support them. '
                'Enable Windows Developer Mode for symbolic links, or choose Copy videos. '
                'No videos were automatically copied or moved. '
                'Symbolic link: '+str(symlink_error)+'; hard link: '+str(hardlink_error)) from hardlink_error
        print('Using hard link (no video copy): '+str(destination), flush=True)
    return str(destination)


@contextmanager
def video_storage(use_originals):
    """DLC copy mode keeps project-local names; replace copies by safe links.
    This runs only in the external worker and never invokes DLC's move fallback.
    """
    if not use_originals:
        yield
        return
    def copy_as_link(src, dst, *args, **kwargs):
        if Path(dst).is_dir(): dst=Path(dst)/Path(src).name
        return link_video(src,dst)
    def no_move(*args, **kwargs):
        raise RuntimeError('Moving original videos is forbidden in use-originals mode.')
    with patch.object(shutil,'copy',copy_as_link), patch.object(shutil,'copy2',copy_as_link), patch.object(shutil,'move',no_move):
        yield


def keep_project_aliases(config, paths, aux):
    """Some DLC versions resolve symlinks in config; retain unique project keys."""
    cfg=aux.read_config(config)
    old=cfg.get('video_sets',{})
    aliases={str(Path(path).resolve()):Path(cfg['project_path'])/'videos'/Path(path).name for path in paths}
    cfg['video_sets']={str(aliases.get(str(Path(key).resolve()),Path(key))):value for key,value in old.items()}
    aux.write_config(config,cfg)


def has_labeling_images(folder):
    return folder.is_dir() and any(
        p.is_file() and p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp'}
        for p in folder.iterdir())


def ensure_labeling_frames(dlc, config, aux, frames=20):
    """Extract only active videos without images; never replace existing labels."""
    cfg = aux.read_config(config)
    root = Path(cfg.get('project_path', Path(config).resolve().parent)) / 'labeled-data'
    videos = list(cfg.get('video_sets', {}))
    if not videos:
        raise ValueError('Select at least one training video before opening labeling.')
    frames = int(frames)
    if frames < 1:
        raise ValueError('Frames per video must be at least 1.')
    missing = []
    for video in videos:
        folder = root / Path(str(video).replace('\\', '/')).stem
        if has_labeling_images(folder):
            continue
        if folder.is_dir() and any(folder.glob('CollectedData_*')):
            raise ValueError(f'{folder.name}: annotations exist but extracted images are missing. '
                             'Restore the original extracted images before opening labeling.')
        missing.append((video, folder))
    print(f'Labeling: {len(videos) - len(missing)} video(s) already have frames; '
          f'{len(missing)} video(s) need extraction.', flush=True)
    if not missing:
        return
    cfg['numframes2pick'] = frames
    aux.write_config(config, cfg)
    dlc.extract_frames(config, mode='automatic', algo='kmeans', crop=False,
                       userfeedback=False, videos_list=[video for video, _ in missing])
    failed = [folder.name for _, folder in missing if not has_labeling_images(folder)]
    if failed:
        raise ValueError('No extracted frames were created for: ' + ', '.join(failed)
                         + '. Check the extraction log and try Open labeling again.')


class LabelingVideoNavigator:
    """Save through napari-DLC and navigate extracted-frame folders in one viewer."""

    def __init__(self, viewer, config, cfg):
        self.viewer = viewer
        self.config = Path(config).resolve()
        root = Path(cfg.get('project_path', self.config.parent)).resolve() / 'labeled-data'
        names = dict.fromkeys(Path(str(p).replace('\\', '/')).stem for p in cfg.get('video_sets', {}))
        self.folders = [root / name for name in names
                        if has_labeling_images(root / name)]
        self.panel = None

    def current_folder(self):
        # The image reader provides root for both older and newer napari-DLC.
        roots = {Path(layer.metadata['root']).resolve() for layer in self.viewer.layers
                 if getattr(layer, '_type_string', '') == 'image' and layer.metadata.get('root')}
        if len(roots) != 1:
            raise ValueError('Open exactly one extracted-frame folder before navigating.')
        return roots.pop()

    def current_index(self):
        folder = self.current_folder()
        return next((i for i, path in enumerate(self.folders) if path.resolve() == folder), -1)

    def save_current(self):
        from napari.layers import Points
        folder = self.current_folder()
        points = [layer for layer in self.viewer.layers if isinstance(layer, Points)]
        if len(points) != 1:
            raise ValueError('Navigation requires exactly one keypoint layer. Select the correct labels and remove extra point layers first.')
        layer = points[0]
        root = layer.metadata.get('root')
        if root and Path(root).resolve() != folder:
            raise ValueError('The keypoint layer belongs to a different folder. Navigation stopped.')
        self.viewer.layers.selection.clear()
        self.viewer.layers.selection.add(layer)
        self.viewer.layers.selection.active = layer
        from qtpy.QtWidgets import QWidget
        widgets = self.viewer.window._qt_window.findChildren(QWidget)
        controls = next((w for w in widgets if hasattr(w, '_save_workflow')
                         and callable(getattr(w._save_workflow, 'save_layers', None))), None)
        if controls is not None:
            outcome = controls._save_workflow.save_layers(selected=True)
            if not outcome.saved:
                return False
            controls._is_saved = True
        else:
            # Earlier plugin versions expose the same napari writer interface.
            written = self.viewer.layers.save('', selected=True, plugin='napari-deeplabcut')
            if not written:
                return False
            if not any(Path(p).suffix.lower() == '.h5' and Path(p).parent.resolve() == folder
                       and Path(p).is_file() for p in written):
                raise ValueError('The label writer did not confirm an H5 file in the current video folder.')
        if not any(folder.glob('CollectedData_*.h5')):
            raise ValueError('No CollectedData H5 file was saved. The current video remains open.')
        return True

    def switch(self, index):
        if not 0 <= index < len(self.folders):
            return False
        if index == self.current_index():
            return True
        target = self.folders[index]
        # Parse before removing any layers, so missing/corrupt inputs leave the viewer intact.
        from napari_deeplabcut._reader import get_folder_parser
        reader = get_folder_parser(str(target))
        if reader is None:
            raise ValueError('Cannot read the selected extracted-frame folder: ' + str(target))
        if not self.save_current():
            return False
        old_layers = list(self.viewer.layers)
        try:
            self.viewer.layers.clear()
            self.viewer.open(str(target), plugin='napari-deeplabcut')
            from napari.layers import Points
            if not any(isinstance(layer, Points) for layer in self.viewer.layers):
                self.viewer.open(str(self.config), plugin='napari-deeplabcut')
            if self.current_folder() != target.resolve():
                raise ValueError('The new folder was not loaded correctly.')
            points = [layer for layer in self.viewer.layers if isinstance(layer, Points)]
            if len(points) != 1:
                raise ValueError('The new folder did not provide exactly one keypoint layer.')
            self.viewer.layers.selection.clear()
            self.viewer.layers.selection.add(points[0])
            self.viewer.layers.selection.active = points[0]
        except Exception:
            self.viewer.layers.clear()
            for layer in old_layers:
                self.viewer.layers.append(layer)
            raise
        print('Labeling video: ' + target.name, flush=True)
        return True

    def install(self):
        from qtpy import QtWidgets, QtCore
        self.panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.panel)
        self.counter = QtWidgets.QLabel()
        layout.addWidget(self.counter)
        self.combo = QtWidgets.QComboBox()
        for folder in self.folders:
            self.combo.addItem(folder.name)
        layout.addWidget(self.combo)
        row = QtWidgets.QHBoxLayout()
        layout.addLayout(row)
        self.previous = QtWidgets.QPushButton('Previous')
        self.next = QtWidgets.QPushButton('Save & next')
        row.addWidget(self.previous)
        row.addWidget(self.next)
        note = QtWidgets.QLabel('Labels are saved before switching in either direction. Only videos with extracted frames are listed.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.previous.clicked.connect(lambda: self.navigate(self.current_index() - 1))
        self.next.clicked.connect(lambda: self.navigate(self.current_index() + 1))
        self.combo.activated.connect(self.navigate)
        self.viewer.window.add_dock_widget(self.panel, name='Video navigation', area='right')
        self.refresh()
        # Reflect folders opened manually without taking over normal napari controls.
        self.timer = QtCore.QTimer(self.panel)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        return self.panel

    def refresh(self):
        try:
            index = self.current_index()
        except ValueError:
            index = -1
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)
        self.counter.setText(f'Video {index + 1} / {len(self.folders)}' if index >= 0
                             else f'{len(self.folders)} videos with extracted frames')
        self.previous.setEnabled(index > 0)
        self.next.setEnabled(0 <= index < len(self.folders) - 1)

    def navigate(self, index):
        try:
            self.panel.setEnabled(False)
            ok = self.switch(index)
            self.message.setText('Labels saved. Video loaded.' if ok else 'Switch cancelled. Current video kept open.')
        except Exception as exc:
            self.message.setText('Could not switch: ' + str(exc))
            print('Video navigation: ' + str(exc), flush=True)
        finally:
            self.panel.setEnabled(True)
            self.refresh()


def open_labeling(dlc, config):
    """Keep napari alive in the standalone worker until its windows close."""
    import napari
    from deeplabcut.utils import auxiliaryfunctions
    cfg = auxiliaryfunctions.read_config(config)
    viewer = napari.Viewer()
    current_viewer = viewer
    navigation = LabelingVideoNavigator(viewer, config, cfg)
    if not navigation.folders:
        viewer.close()
        raise ValueError('No active training video has extracted frames. Extract frames first.')
    viewer.window.add_plugin_dock_widget('napari-deeplabcut')
    viewer.open(str(navigation.folders[0]), plugin='napari-deeplabcut')
    from napari.layers import Points
    if not any(isinstance(layer, Points) for layer in viewer.layers):
        viewer.open(str(Path(config).resolve()), plugin='napari-deeplabcut')
    navigation.install()
    print('Labeling window open. Use Video navigation to save and switch videos; close the window to finish.', flush=True)
    napari.run()
    # Both references deliberately remain alive for the duration of napari.run.
    return viewer, current_viewer


def update_scorer(config, scorer, aux):
    scorer=str(scorer or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',scorer):
        raise ValueError('Use a scorer name of 1–64 letters, digits, underscores, dots or hyphens, starting with a letter or digit.')
    path=Path(config);before=digest(path);cfg=aux.read_config(config)
    if cfg.get('scorer')==scorer:return dict(changed=False,backup=None)
    root=Path(cfg['project_path'])
    labels=root/'labeled-data'
    if labels.exists() and any(p.is_file() for p in labels.rglob('CollectedData_*')):
        raise ValueError('This project already contains annotations. Keep its current scorer name to preserve label compatibility. Use a new project for a different scorer.')
    for child in ('training-datasets','dlc-models','dlc-models-pytorch'):
        folder=root/child
        if folder.exists() and any(p.is_file() for p in folder.rglob('*')):
            raise ValueError('This project already contains a training dataset or model. Its scorer name cannot be changed here.')
    backup=path.with_name(path.name+'.before_scorer_'+uuid.uuid4().hex[:12]+'.bak')
    fd,temporary=tempfile.mkstemp(prefix='.scorer_',suffix='.yaml',dir=path.parent);os.close(fd)
    try:
        cfg['scorer']=scorer;aux.write_config(temporary,cfg)
        if aux.read_config(temporary).get('scorer')!=scorer:raise ValueError('Scorer write verification failed.')
        if digest(path)!=before:raise ValueError('Project configuration changed. Refresh and retry.')
        with path.open('rb') as source,backup.open('xb') as target:shutil.copyfileobj(source,target)
        if digest(path)!=before or digest(backup)!=before:raise ValueError('Project changed while backing up. Refresh and retry.')
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return dict(changed=True,backup=str(backup))


def update_bodyparts(config, parts, aux):
    """Append points without renaming, removing or reordering existing labels."""
    if not isinstance(parts,list) or not parts or any(not isinstance(p,str) or not p.strip() for p in parts):
        raise ValueError('Enter at least one body-part name.')
    parts=[p.strip() for p in parts]
    if len(set(parts))!=len(parts):raise ValueError('Body-part names must be unique.')
    path=Path(config);before=digest(path)
    cfg=aux.read_config(str(path))
    old=list(cfg.get('bodyparts',[]))
    if cfg.get('multianimalproject',False):raise ValueError('Only single-animal projects are supported.')
    if parts[:len(old)]!=old:
        raise ValueError('Keep existing body-part names in the same order and append new names at the end. Renaming or removing existing labels is not supported here.')
    added=parts[len(old):]
    if any(any(c.isspace() or c in '/\\:,' for c in p) for p in added):
        raise ValueError('Use body-part names without spaces, commas or path separators (for example left_paw).')
    if not added:return dict(added=[],backup=None)
    backup=path.with_name(path.name+'.before_bodyparts_'+uuid.uuid4().hex[:12]+'.bak')
    fd,temporary=tempfile.mkstemp(prefix='.bodyparts_',suffix='.yaml',dir=path.parent);os.close(fd)
    try:
        cfg['bodyparts']=parts
        aux.write_config(temporary,cfg)
        if aux.read_config(temporary).get('bodyparts')!=parts:raise ValueError('Body-part write verification failed.')
        if digest(path)!=before:raise ValueError('Project configuration changed during the update. Refresh the project and try again.')
        with path.open('rb') as src,backup.open('xb') as dst:shutil.copyfileobj(src,dst)
        if digest(backup)!=before or digest(path)!=before:
            raise ValueError('Project configuration changed during backup. Update cancelled.')
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    print('Added body parts: '+', '.join(added)+'\nConfiguration backup: '+str(backup),flush=True)
    print('Label the new points, then recreate the training dataset and train with a new shuffle.',flush=True)
    return dict(added=added,backup=str(backup))


def selection_state_path(config):
    return Path(config).parent / 'workbench_training_selection.json'


def load_selection_state(config):
    path = selection_state_path(config)
    if not path.exists():
        return {}
    value = _pp_json_read(path)
    if not isinstance(value, dict):
        raise ValueError('Invalid Workbench training selection metadata: ' + str(path))
    return value


def selection_plain(value):
    """Normalize DLC config value objects for the Workbench JSON sidecar."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, 'model_dump'):
        return selection_plain(value.model_dump())
    if hasattr(value, 'to_dict'):
        return selection_plain(value.to_dict())
    if hasattr(value, 'items'):
        return {str(k): selection_plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [selection_plain(v) for v in value]
    raise TypeError('Unsupported video metadata type: ' + type(value).__name__)


def training_selection_plan(config, sources, aux):
    """Resolve original videos to stable project aliases, including inactive entries."""
    cfg = aux.read_config(config)
    active = cfg.get('video_sets', {})
    state = load_selection_state(config)
    archived = state.get('inactive_video_sets', {})
    known = dict(archived, **active)
    mapping = dict(state.get('video_sources', {}))
    selected, additions = {}, []
    for source in sources:
        original = str(Path(source['video']).resolve())
        if original in selected or any(v['original'] == original for v in additions):
            continue
        stem = Path(original).stem
        alias = stem[:100] + '__' + hashlib.sha256(os.path.normcase(original).encode('utf-8')).hexdigest()[:16]
        matches = [p for p in known if p == mapping.get(original) or Path(p).stem == alias]
        if not matches:
            matches = [p for p in known if Path(p).stem == stem]
        if len(matches) > 1:
            raise ValueError('Ambiguous registered video: ' + original)
        if matches:
            selected[original] = matches[0]
        else:
            # A failed older update may have created the link but restored config.yaml.
            orphan = Path(cfg['project_path']) / 'videos' / (alias + Path(original).suffix)
            if orphan.is_file():
                same = os.path.samefile(orphan, original)
                if not same and (orphan.stat().st_size != Path(original).stat().st_size or digest(orphan) != digest(original)):
                    raise ValueError('An existing project video differs from the original: ' + str(orphan))
                additions.append(dict(original=original, source=source, existing_project_video=str(orphan)))
            else:
                additions.append(dict(original=original, source=source))
    retained = set(selected.values())
    removed = [p for p in active if p not in retained]
    changed = bool(removed or additions or any(p not in active for p in retained))
    root = Path(cfg['project_path'])
    labeled = [Path(p).name for p in removed if any((root/'labeled-data'/Path(p).stem).glob('CollectedData_*.h5'))]
    artifacts = any(any(p.is_file() for p in (root/child).rglob('*'))
                    for child in ('training-datasets', 'dlc-models', 'dlc-models-pytorch'))
    return dict(config_digest=digest(config), selected=selected, additions=additions,
                removed=removed, changed=changed, labeled=labeled,
                has_training_artifacts=artifacts, needs_confirmation=changed and bool(labeled or artifacts))


def sync_training_selection(config, sources, aux, dlc, request, info):
    plan = training_selection_plan(config, sources, aux)
    if not plan['changed']:
        return None
    if plan['needs_confirmation'] and request.get('selection_confirmation') != plan['config_digest']:
        return dict(selection_review=dict(config_digest=plan['config_digest'],
                    removed_count=len(plan['removed']), added_count=len(plan['additions']),
                    labeled=plan['labeled'], has_training_artifacts=plan['has_training_artifacts']))
    if request.get('selection_confirmation') and request['selection_confirmation'] != plan['config_digest']:
        raise ValueError('The project changed after confirmation. Review the selection again.')
    cfg = aux.read_config(config)
    before = plan['config_digest']
    backup = Path(config).with_name('config.yaml.before_video_selection_' + uuid.uuid4().hex[:8] + '.bak')
    with Path(config).open('rb') as src, backup.open('xb') as dst:
        shutil.copyfileobj(src, dst)
    if digest(backup) != before or digest(config) != before:
        raise ValueError('Configuration changed during backup. Selection update cancelled.')
    state = load_selection_state(config)
    state_path = selection_state_path(config)
    old_state_bytes = state_path.read_bytes() if state_path.exists() else None
    mapping = dict(state.get('video_sources', {}))
    try:
        # Reuse archived aliases first. Never remove video or annotation files.
        known = dict(state.get('inactive_video_sets', {}), **cfg.get('video_sets', {}))
        if plan['additions']:
            originals = not request.get('copy_videos', False)
            reused = [v for v in plan['additions'] if v.get('existing_project_video')]
            fresh = [v for v in plan['additions'] if not v.get('existing_project_video')]
            assigned = []
            if reused:
                paths = [v['existing_project_video'] for v in reused]
                dlc.add_new_videos(config, paths, copy_videos=False, extract_frames=False)
                # DLC may resolve a symlink; retain its stable project alias.
                keep_project_aliases(config, paths, aux)
                assigned.extend(zip(reused, paths))
                print(f'Reusing {len(paths)} existing project video links/files.', flush=True)
            if fresh:
                paths = project_video_paths([v['source'] for v in fresh], known,
                            staging=Path(request['job_dir'])/'project_inputs', use_originals=originals)
                with video_storage(originals):
                    dlc.add_new_videos(config, paths, copy_videos=True, extract_frames=False)
                if originals:
                    keep_project_aliases(config, paths, aux)
                assigned.extend(zip(fresh, paths))
            updated = aux.read_config(config)
            for addition, input_path in assigned:
                matches = [p for p in updated.get('video_sets', {}) if Path(p).stem == Path(input_path).stem]
                if len(matches) != 1:
                    raise ValueError('Cannot identify the added video alias: ' + str(input_path))
                plan['selected'][addition['original']] = matches[0]
            known.update(updated.get('video_sets', {}))
            cfg = updated
        selected_paths = set(plan['selected'].values())
        cfg['video_sets'] = {p: known[p] for p in known if p in selected_paths}
        state['inactive_video_sets'] = {p: value for p, value in known.items() if p not in selected_paths}
        mapping.update(plan['selected'])
        state['video_sources'] = mapping
        stale = set(state.get('stale_training_sets', []))
        stale.update(f"{m['shuffle']}:{m['trainingsetindex']}" for m in info.get('models', []) if not m.get('snapshots'))
        state['stale_training_sets'] = sorted(stale)
        fd, tmp = tempfile.mkstemp(dir=Path(config).parent, suffix='.yaml', prefix='.selection-')
        os.close(fd)
        try:
            aux.write_config(tmp, cfg)
            if set(aux.read_config(tmp).get('video_sets', {})) != selected_paths:
                raise ValueError('Video selection write verification failed.')
            atomic_json(state_path, selection_plain(state))
            os.replace(tmp, config)
        finally:
            Path(tmp).unlink(missing_ok=True)
    except Exception:
        # Restore config, keeping any newly linked/copied files available for retry.
        shutil.copy2(backup, config)
        if old_state_bytes is None:
            state_path.unlink(missing_ok=True)
        else:
            state_path.write_bytes(old_state_bytes)
        raise
    print('Training selection updated. Configuration backup: ' + str(backup), flush=True)
    return None


def check_analysis_video_frames(video, expected):
    import cv2
    print('Checking full video decoding before analysis…', flush=True)
    capture = cv2.VideoCapture(str(video))
    count = 0
    try:
        if not capture.isOpened():
            raise ValueError('OpenCV could not open the video: ' + str(video))
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame is None or not frame.size:
                raise ValueError('OpenCV returned an empty frame at index ' + str(count))
            count += 1
    finally:
        capture.release()
    if count != expected:
        raise ValueError(f'OpenCV decoded {count}/{expected} expected frames. Analysis stopped before inference. '
                         'Check source-drive access and video decoding. The synchronized original was not modified.')
    print(f'Decoding check passed: {count} frames.', flush=True)


def analyze_video_safely(analyzer, config, source, destination, **options):
    """Keep DLC output local until complete frame-count validation succeeds."""
    video = Path(source['video'])
    local = Path(tempfile.mkdtemp(prefix='nwb_dlc_analysis_'))
    print('Temporary prediction output: ' + str(local), flush=True)
    try:
        check_analysis_video_frames(video, source['frames'])
        analyzer(config, [str(video)], destfolder=str(local), save_as_csv=True, **options)
        csv_files = list(local.glob('*.csv'))
        if len(csv_files) != 1:
            raise ValueError('Expected exactly one DLC CSV prediction file.')
        prediction = predictions_from_csv(csv_files[0], local/'predictions.npz')
        if prediction['frames'] != source['frames']:
            raise ValueError(f"DLC processed {prediction['frames']} frames, but NWB contains {source['frames']}. "
                             'Partial predictions rejected. Check video decoding and source-drive access; '
                             'do not replace/re-encode the synchronized original without validating every frame.')
        if [video.stat().st_size, video.stat().st_mtime_ns] != source['video_fingerprint']:
            raise ValueError('Video changed during analysis.')
        destination = Path(destination)
        Path(extended_path(destination)).mkdir(parents=True, exist_ok=True)
        for path in local.iterdir():
            if path.is_file():
                shutil.copy2(path, extended_path(destination/path.name))
        prediction.update(source=source, csv=str(destination/csv_files[0].name),
                          predictions=str(destination/'predictions.npz'),
                          predictions_sha256=digest(destination/'predictions.npz'))
    except Exception as exc:
        probes = []
        for label, path in [('source video', video), ('job folder', Path(destination).parent)]:
            try:
                path.stat()
                probes.append(label + ': accessible')
            except OSError as probe_error:
                probes.append(label + ': inaccessible (' + str(probe_error) + ')')
        raise RuntimeError(str(exc) + '\nAccess checks: ' + '; '.join(probes)
                           + '\nTemporary outputs retained at: ' + str(local)) from exc
    shutil.rmtree(local)
    return prediction


def write_failure_report(output, value):
    """Keep the original error available even when the project drive disappears."""
    try:
        if Path(output).is_file():
            try:previous=_pp_json_read(Path(output))
            except (OSError,ValueError):previous={}
            if isinstance(previous,dict) and previous.get('checkpointed'):
                value={**previous, **value}
        atomic_json(output, value)
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / ('nwb_dlc_failure_' + uuid.uuid4().hex[:12] + '.json')
        _pp_json_write(fallback, value, dump_options={'indent':2})
        print('Could not write the project error report: ' + str(exc), flush=True)
        print('Local error report: ' + str(fallback), flush=True)
    print('NWB_WORKBENCH_ERROR_JSON=' + json.dumps(value), flush=True)


def run(request):
    # Prevent the parent Workbench's Qt binding and plugin paths leaking here.
    for key in ('QT_API','PYQTGRAPH_QT_LIB','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','PYTHONHOME','PYTHONPATH'):
        os.environ.pop(key, None)
    action = request['action']
    if action == 'annotate':
        os.environ.pop('DLClight', None)
        os.environ.pop('MPLBACKEND', None)
    if action != 'annotate':
        os.environ['DLClight'] = 'True'
        os.environ['MPLBACKEND'] = 'Agg'
    import deeplabcut as dlc
    import torch
    if int(dlc.__version__.split('.')[0]) < 3:
        raise ValueError('DeepLabCut 3.x with the PyTorch engine is required.')
    from deeplabcut.utils import auxiliaryfunctions as aux
    from deeplabcut.core.engine import Engine
    engine = Engine.PYTORCH
    environment = dict(python=sys.executable, dlc_version=dlc.__version__, torch_version=torch.__version__,
                       cuda_available=bool(torch.cuda.is_available()))
    if action == 'check':
        return dict(environment=environment)
    videos = request.get('videos', [])
    config = request.get('config')
    use_originals = not request.get('copy_videos', True)
    if action == 'create':
        paths = project_video_paths(videos, staging=Path(request['job_dir']) / 'project_inputs', use_originals=use_originals) if videos else []
        destination = Path(request['working_directory'])
        if any(destination.glob(request['name'] + '-' + request['experimenter'] + '-*/config.yaml')):
            raise ValueError('A project with this name and experimenter already exists. Open it or choose a new name.')
        if paths:
            with video_storage(use_originals):
                config = str(dlc.create_new_project(request['name'], request['experimenter'], paths,
                                                   working_directory=request['working_directory'], copy_videos=True,
                                                   multianimal=False))
        else:
            config = create_empty_project(request,aux)
        if not Path(config).is_file():
            raise ValueError('DLC did not create a config.yaml. Choose a new project name.')
        if use_originals and paths: keep_project_aliases(config, paths, aux)
        cfg = aux.read_config(config)
        cfg['bodyparts'] = request['bodyparts']
        cfg['skeleton'] = []
        cfg['engine'] = 'pytorch'
        cfg['numframes2pick'] = request.get('frames', 20)
        aux.write_config(config, cfg)
    info = project_info(config, aux, engine)
    if action == 'train' and any(m['shuffle']==int(request.get('shuffle',1))
                                and m['trainingsetindex']==int(request.get('trainingsetindex',0))
                                and m.get('snapshots') for m in info['models']):
        raise ValueError('This model is already trained and locked. Create a new named model to train again.')
    if action == 'update_scorer':
        update=update_scorer(config,request.get('scorer'),aux)
        return dict(environment=environment,project=project_info(config,aux,engine),scorer_update=update)
    if action == 'update_bodyparts':
        update=update_bodyparts(config,request.get('bodyparts'),aux)
        return dict(environment=environment,project=project_info(config,aux,engine),bodyparts_update=update)
    if 'training_selection' in request:
        review = sync_training_selection(config, request['training_selection'], aux, dlc, request, info)
        if review is not None:
            return dict(environment=environment, project=info, **review)
        info = project_info(config, aux, engine)
    cfg = aux.read_config(config)
    shuffle = int(request.get('shuffle',1))
    fraction = int(request.get('trainingsetindex',0))
    if fraction < 0 or fraction >= len(cfg.get('TrainingFraction', [])):
        raise ValueError('Training fraction index is absent from this config.')
    device = request.get('device', 'cpu')
    if action=='analyze' and device=='auto':
        device='cuda:0' if torch.cuda.is_available() else 'cpu'
    if device.startswith('cuda') and not torch.cuda.is_available():
        raise ValueError('CUDA is unavailable in the selected DLC environment. Select cpu or fix the GPU installation.')
    if action=='analyze':
        environment['analysis_device']=device
        print('Analysis device: '+device,flush=True)
    if action == 'sync_videos':
        pass
    elif action == 'add_videos':
        paths = project_video_paths(videos, cfg.get('video_sets', {}), staging=Path(request['job_dir']) / 'project_inputs', use_originals=use_originals)
        with video_storage(use_originals):
            dlc.add_new_videos(config, paths, copy_videos=True, extract_frames=False)
        if use_originals: keep_project_aliases(config, paths, aux)
    elif action == 'extract':
        cfg['numframes2pick'] = int(request.get('frames',20))
        aux.write_config(config,cfg)
        dlc.extract_frames(config, mode='automatic', algo='kmeans', crop=False, userfeedback=False)
    elif action == 'annotate':
        ensure_labeling_frames(dlc, config, aux, request.get('frames', 20))
        open_labeling(dlc, config)
    elif action == 'check_labels':
        dlc.check_labels(config)
    elif action == 'dataset':
        prepare_training_dataset(dlc, config, aux, engine, shuffle, fraction, info, force=True)
    elif action == 'train':
        review = training_label_review(config, cfg, request)
        if review is not None:
            return dict(environment=environment, project=info, training_label_review=review)
        prepare_training_dataset(dlc, config, aux, engine, shuffle, fraction, info)
        dlc.train_network(config, shuffle=shuffle, trainingsetindex=fraction,
                          engine=engine, device=device, epochs=int(request.get('epochs',200)))
        trained_info = project_info(config, aux, engine)
        if any(m['shuffle']==shuffle and m['trainingsetindex']==fraction and m.get('snapshots') for m in trained_info['models']):
            state = load_selection_state(config)
            state.setdefault('trained_models', {})[f'{shuffle}:{fraction}'] = dict(
                name=str(request.get('model_name') or f'Model {shuffle}'),
                training_selection=request.get('training_selection', []),
                video_sets=list(cfg.get('video_sets', {})), labeling=labeling_summary(cfg),
                annotation_digest=training_annotation_digest(cfg),
                trained_at=datetime.now(timezone.utc).isoformat())
            atomic_json(selection_state_path(config), state)
    elif action == 'evaluate':
        dlc.evaluate_network(config, Shuffles=[shuffle], trainingsetindex=fraction, engine=engine, plotting=True,
                             snapshotindex=int(request.get('snapshot_index',-1)), device=device)
    elif action == 'analyze':
        if not videos:
            raise ValueError('Select at least one NWB video.')
        selected = [m for m in info['models'] if m['shuffle']==shuffle and m['trainingsetindex']==fraction]
        if len(selected)!=1 or not selected[0]['snapshots']:
            raise ValueError('No trained PyTorch model found for this shuffle and training fraction.')
        checkpoints = {path:digest(path) for path in selected[0]['snapshots']}
        config_hash = digest(config)
        results = []
        model=dict(config=str(config), name=request.get('model_name') or f'Model {shuffle}', config_sha256=config_hash, shuffle=shuffle,
                   trainingsetindex=fraction, snapshot_index=int(request.get('snapshot_index',-1)),
                   checkpoints_sha256=checkpoints)
        progress=dict(environment=environment, project=info, results=results, model=model,
                      job_id=request['job_id'], checkpointed=True, total_videos=len(videos), completed_videos=0)
        output=Path(request['job_dir'])/'result.json'
        def checkpoint(**updates):
            progress.update(updates)
            progress['completed_videos']=len(results)
            atomic_json(output,dict(status='running',action='analyze',updated_at=datetime.now(timezone.utc).isoformat(),**progress))
        checkpoint()
        from deeplabcut.pose_estimation_pytorch.apis.videos import analyze_videos
        for i, source in enumerate(videos):
            video = Path(source['video'])
            if [video.stat().st_size,video.stat().st_mtime_ns] != source['video_fingerprint']:
                raise ValueError('Video changed after selection: '+str(video))
            folder = Path(request.get('analysis_dir') or request['job_dir']) / ('video_%04d' % i)
            print('Analyzing video %d/%d: %s' % (i+1,len(videos),video), flush=True)
            checkpoint(current_video_index=i+1,current_video=str(video))
            prediction = analyze_video_safely(analyze_videos, config, source, folder,
                           shuffle=shuffle, trainingsetindex=fraction,
                           snapshot_index=int(request.get('snapshot_index',-1)), device=device)
            if digest(config)!=config_hash or any(digest(path)!=sha for path,sha in checkpoints.items()):
                raise ValueError('Project configuration or model weights changed during analysis. Earlier saved results were retained.')
            prediction['analyzed_at']=datetime.now(timezone.utc).isoformat()
            results.append(prediction)
            checkpoint(current_video=None,current_video_index=None)
            print(f'Saved and registered: {len(results)}/{len(videos)} completed videos.',flush=True)
        return progress
    elif action not in {'create','inspect'}:
        raise ValueError('Unknown task: '+action)
    return dict(environment=environment, project=project_info(config,aux,engine))


if __name__ == '__main__':
    request_path = Path(sys.argv[1])
    try:
        from project_paths import configure_project_context, project_root
    except ModuleNotFoundError:
        from GUI.project_paths import configure_project_context, project_root
    context_root = project_root(request_path)
    if context_root is not None: configure_project_context(context_root)
    request = _pp_json_read(request_path)
    output = request_path.parent/'result.json'
    try:
        try:
            from project_paths import external_paths
        except ModuleNotFoundError:
            from GUI.project_paths import external_paths
        from contextlib import nullcontext
        configuration = request.get('config')
        with external_paths(Path(configuration).parent) if configuration else nullcontext():
            result = run(request)
        if not configuration and result.get('project', {}).get('config'):
            try:
                from project_paths import compact_external_configs
            except ModuleNotFoundError:
                from GUI.project_paths import compact_external_configs
            compact_external_configs(Path(result['project']['config']).parent)
        atomic_json(output, dict(status='completed', action=request['action'],
                                 completed_at=datetime.now(timezone.utc).isoformat(), **result))
    except Exception as exc:
        traceback.print_exc()
        write_failure_report(output, dict(status='failed', action=request.get('action'), error=str(exc)))
        sys.exit(1)
