from GUI.project_paths import encode as _pp_encode, decode as _pp_decode, project_root as _pp_root, active_root as _pp_active
"""Workbench-side DLC files and read-only validation; no DeepLabCut dependency."""
from GUI.project_paths import json_read as _pp_json_read, json_write as _pp_json_write
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
import h5py
import numpy as np
from GUI.dlc_storage import job_paths, roots, job_records


def sha256(path):
    value=hashlib.sha256()
    with open(path,'rb') as file:
        for block in iter(lambda:file.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def fingerprint(path):
    info=Path(path).stat()
    return [info.st_size,info.st_mtime_ns]


def text(value):
    return value.decode('utf-8') if isinstance(value,bytes) else str(value)


def timestamps(group):
    if 'timestamps' in group:
        values=np.asarray(group['timestamps'],dtype=np.float64)
    elif 'starting_time' in group and 'num_samples' in group:
        rate=float(group['starting_time'].attrs.get('rate',0))
        if not np.isfinite(rate) or rate<=0:raise ValueError('Invalid video sampling rate')
        values=float(group['starting_time'][()])+np.arange(int(group['num_samples'][()]))/rate
    else:raise ValueError('Video needs timestamps or rate and num_samples')
    if values.ndim!=1 or not len(values) or not np.all(np.isfinite(values)) or np.any(np.diff(values)<=0):
        raise ValueError('Invalid video timestamps')
    return values


def time_hash(values):
    return hashlib.sha256(np.asarray(values,dtype='<f8').tobytes()).hexdigest()


def video_source(nwb_path,object_path):
    nwb_path=Path(nwb_path).resolve()
    with h5py.File(nwb_path,'r') as file:
        group=file[object_path]
        names=[text(v) for v in group['external_file'][:]]
        starts=np.asarray(group['external_file'].attrs.get('starting_frame',[0])).reshape(-1)
        if len(names)!=1 or list(starts)!=[0]:
            raise ValueError('First DLC integration supports one external video file per ImageSeries.')
        from GUI.project_paths import resolve_video_path
        path=Path(resolve_video_path(names[0], nwb_path))
        if not path.is_file():raise FileNotFoundError('External video missing: '+str(path))
        times=timestamps(group)
    return dict(nwb=str(nwb_path),object_path=object_path,video=str(path),frames=len(times),
                timestamps_sha256=time_hash(times),video_fingerprint=fingerprint(path))


def discover_videos(paths):
    rows=[]
    for path in dict.fromkeys(map(str,paths)):
        try:
            with h5py.File(path,'r') as file:
                groups=[]
                file.visititems(lambda name,obj:groups.append(name) if isinstance(obj,h5py.Group) and 'external_file' in obj else None)
            if not groups:rows.append(dict(nwb=path,error='No external ImageSeries'))
            for group in groups:
                try:rows.append(video_source(path,group))
                except Exception as exc:rows.append(dict(nwb=path,object_path=group,error=str(exc)))
        except Exception as exc:rows.append(dict(nwb=path,error=str(exc)))
    return rows


def indexed_video_sources(paths, content_index=None, cache=None):
    """Use the in-memory content index; no frame arrays or external file stats.
    Older inventories without rows for a file use one metadata-only fallback.
    Cache invalidates when the NWB or its indexed object paths change.
    """
    cache = cache if cache is not None else {}
    key = lambda value: os.path.normcase(str(value).replace('\\','/')).rstrip('/')
    indexed = {}
    if content_index is not None:
        records = content_index.to_dict('records') if hasattr(content_index,'to_dict') else content_index
        for row in records:
            if 'file_path' not in row or 'nwb_path' not in row: continue
            entry = indexed.setdefault(key(row['file_path']), [])
            if str(row.get('neurodata_type','')).endswith('ImageSeries'):
                entry.append(str(row['nwb_path']).strip('/'))
    rows = []
    for raw_path in dict.fromkeys(map(str,paths)):
        path = str(Path(raw_path).absolute())
        try:
            groups = indexed.get(key(raw_path))
            signature = (fingerprint(path), None if groups is None else tuple(dict.fromkeys(groups)))
            saved = cache.get(key(path))
            if saved and saved[0] == signature:
                rows.extend(dict(row) for row in saved[1]);continue
            found = []
            if groups is None or groups:
                with h5py.File(path,'r') as file:
                    if groups is None:
                        groups = []
                        file.visititems(lambda name,obj:groups.append(name) if isinstance(obj,h5py.Group) and 'external_file' in obj else None)
                    for group_path in dict.fromkeys(groups):
                        try:
                            group = file[group_path]
                            if 'external_file' not in group: continue
                            names = [text(v) for v in group['external_file'][:]]
                            starts = np.asarray(group['external_file'].attrs.get('starting_frame',[0])).reshape(-1)
                            if len(names)!=1 or list(starts)!=[0]:
                                raise ValueError('Select an ImageSeries with one external video file starting at frame zero.')
                            from GUI.project_paths import resolve_video_path
                            video = Path(resolve_video_path(names[0], path))
                            frames = group['timestamps'].shape[0] if 'timestamps' in group else int(group['num_samples'][()])
                            found.append(dict(nwb=path,object_path=group_path,video=os.path.normpath(str(video)),frames=frames,indexed=True))
                        except Exception as exc:
                            found.append(dict(nwb=path,object_path=group_path,error=str(exc)))
            if not found:found=[dict(nwb=path,error='No external ImageSeries in the inventory. Refresh the NWB inventory if videos were added recently.')]
            cache[key(path)] = (signature,found)
            rows.extend(dict(row) for row in found)
        except Exception as exc:rows.append(dict(nwb=path,error=str(exc)))
    return rows


def load_prediction(result):
    if sha256(result['predictions'])!=result['predictions_sha256']:
        raise ValueError('Prediction file changed after analysis; reanalyze before importing.')
    with np.load(result['predictions'],allow_pickle=False) as file:
        values=file['values'].copy();parts=file['bodyparts'].astype(str).tolist()
    if values.shape!=(result['source']['frames'],len(parts),3) or not parts or len(set(parts))!=len(parts):
        raise ValueError('Invalid pose array shape or body-part names')
    if np.any(np.isinf(values[:,:,:2])) or not np.all(np.isfinite(values[:,:,2])) or np.any((values[:,:,2]<0)|(values[:,:,2]>1)):
        raise ValueError('Invalid pose coordinates or likelihoods')
    return values,parts


def verify_source(source):
    current=video_source(source['nwb'],source['object_path'])
    for key in ('video','frames','timestamps_sha256','video_fingerprint'):
        if current[key]!=source[key]:raise ValueError('NWB video or timing changed since analysis: '+key)
    return current


def prediction_module_name(result,run):
    suffix=hashlib.sha256(result['source']['object_path'].encode()).hexdigest()[:8]
    job=re.sub('[^a-zA-Z0-9_]','',run['job_id'])
    return 'DeepLabCut_'+job+'_'+suffix


def processed_module_name(result,run,settings):
    from Tools.General.dlc_postprocessing import settings_hash
    return prediction_module_name(result,run)+'_post_'+settings_hash(settings)


def postprocessed_cache_path(result,settings,directory):
    from Tools.General.dlc_postprocessing import settings_hash
    identity=json.dumps(dict(source=result['source'],prediction=result['predictions_sha256'],settings=settings_hash(settings)),sort_keys=True)
    return Path(directory)/(hashlib.sha256(identity.encode()).hexdigest()[:32]+'.npz')


def save_postprocessed(result,settings,directory):
    """Save an actual processed result without modifying the NWB file."""
    from Tools.General.dlc_postprocessing import normalize_settings,process_pose
    cfg=normalize_settings(settings)
    if not cfg['enabled']:raise ValueError('Enable postprocessing before applying it.')
    source=verify_source(result['source']);values,parts=load_prediction(result)
    with h5py.File(source['nwb'],'r') as file:times=timestamps(file[source['object_path']])
    xy,status,warnings=process_pose(values,parts,times,cfg)
    path=postprocessed_cache_path(result,cfg,directory);path.parent.mkdir(parents=True,exist_ok=True)
    metadata=dict(source=result['source'],predictions_sha256=result['predictions_sha256'],settings=cfg,name=str(settings.get('name','Postprocessing')),warnings=warnings)
    fd,tmp=tempfile.mkstemp(prefix='.processing_',suffix='.npz',dir=path.parent);os.close(fd)
    try:
        np.savez_compressed(tmp,coordinates=xy,status=status,timestamps=times,bodyparts=np.asarray(parts),metadata=np.asarray(json.dumps(_pp_encode(metadata,_pp_root(path)))))
        os.replace(tmp,path)
        report=path.with_suffix('.json');temporary=report.with_suffix('.tmp')
        _pp_json_write(temporary, metadata, dump_options={});os.replace(temporary,report)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return dict(path=str(path),warnings=warnings)


def postprocessed_video_keys(sources,directories):
    """Count each library video once if a saved processing manifest and array exist."""
    key=lambda source:str(source.get('nwb',''))+'|'+source.get('object_path','')
    library={key(source) for source in sources};found=set()
    for directory in roots(directories):
        for manifest in directory.glob('*.json'):
            try:
                info=_pp_json_read(manifest)
                identity=key(info['source'])
                if identity in library and info.get('predictions_sha256') and info.get('settings',{}).get('enabled') and manifest.with_suffix('.npz').is_file():found.add(identity)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
    return found


def remove_postprocessed(results,directories):
    """Delete only cached processing matching selected raw prediction results."""
    report=dict(removed=0,errors=[],failed_sources=[])
    for result in results:
        source=result['source'];source_key=str(source['nwb'])+'|'+source.get('object_path','')
        for directory in roots(directories):
            for manifest in directory.glob('*.json'):
                matched=False
                try:
                    info=_pp_json_read(manifest)
                    if info.get('source')!=source or info.get('predictions_sha256')!=result['predictions_sha256']:continue
                    matched=True
                    payload=manifest.with_suffix('.npz')
                    # Keep the manifest until the payload is deleted, allowing retries.
                    payload.unlink(missing_ok=True)
                    manifest.unlink()
                    report['removed']+=1
                except (OSError,ValueError,TypeError) as exc:
                    report['errors'].append(str(manifest)+': '+str(exc))
                    if matched:report['failed_sources'].append(source_key)
    return report


def project_export_status(jobs,sources,config,shuffle,fraction,settings,directory,nwb_cache=None):
    """Check saved arrays and NWB processing metadata, without decoding video."""
    matches,_=selected_analysis_results(jobs,[source for source in sources if source.get('object_path')],config,shuffle,fraction)
    def key(source):return os.path.normcase(os.path.abspath(source['nwb'])),source.get('object_path','')
    latest={key(result['source']):(result,run) for result,run,_ in matches}
    from Tools.General.dlc_postprocessing import normalize_settings,settings_hash
    cached={}
    for manifest in (p for root in roots(directory) for p in root.glob('*.json')):
        try:
            info=_pp_json_read(manifest);normalize_settings(info['settings'])
            if manifest.with_suffix('.npz').is_file():cached.setdefault((key(info['source']),info['predictions_sha256']),[]).append(info)
        except (OSError,ValueError,KeyError,TypeError,AttributeError):continue
    nwb_cache={} if nwb_cache is None else nwb_cache
    active=dict(settings,enabled=True);rows=[]
    for source in sources:
        row=dict(source=source,analyzed=False,postprocessed=False,current_processed=False,exported=False,model={},postprocessing_names=[],detail=source.get('error',''))
        pair=latest.get(key(source))
        if pair:
            result,run=pair;row['model']=run.get('model',{})
            row['analyzed']=Path(result['predictions']).is_file()
            if not row['analyzed']:row['detail']='Analysis completed, but the prediction file is missing.'
            records=[info for info in cached.get((key(source),result['predictions_sha256']),[]) if info['source']==result['source']]
            row['postprocessed']=bool(records)
            row['current_processed']=any(settings_hash(info['settings'])==settings_hash(active) for info in records)
            names={info.get('name') or 'Postprocessing' for info in records}
            try:
                stamp=fingerprint(source['nwb']);cache_key=source['nwb'];prior=nwb_cache.get(cache_key)
                if prior is None or prior[0]!=stamp or not isinstance(prior[1],dict):
                    with h5py.File(source['nwb'],'r') as file:
                        modules={name:group.attrs.get('description','') for name,group in file.get('processing',{}).items()}
                        modules['__tracking__']=[]
                        for candidate in sources:
                            if candidate.get('nwb')==source['nwb'] and candidate.get('object_path'):
                                modules['__tracking__'].extend(dict(item['metadata'],bodypart=item['bodypart']) for item in tracking_objects(file,candidate))
                    nwb_cache[cache_key]=(stamp,modules)
                else:modules=prior[1]
                raw=prediction_module_name(result,run);prefix=raw+'_post_'
                processed=[name for name in modules if name.startswith(prefix)]
                new_records=[info for info in modules.get('__tracking__',[]) if info.get('source',{}).get('object_path')==source['object_path'] and info.get('predictions_sha256')==result['predictions_sha256']]
                row['exported']=raw in modules or bool(processed) or bool(new_records)
                row['postprocessed']|=bool(processed)
                row['current_processed']|=processed_module_name(result,run,active) in modules
                for name in processed:
                    description=modules[name]
                    if isinstance(description,bytes):description=description.decode('utf-8',errors='replace')
                    try:details=json.loads(str(description)[str(description).index('{'):])
                    except (ValueError,TypeError):details={}
                    names.add(details.get('postprocessing_name') or 'Postprocessing')
            except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:row['detail']='Could not check NWB processing status: '+str(exc)
            row['postprocessing_names']=sorted(names)
            if row['postprocessed'] and not row['current_processed']:row['detail']+=' Saved processing uses different settings.'
            if row['exported']:row['detail']='Exported to NWB. '+row['detail']
        rows.append(row)
    return rows


def prediction_imported(result,run,postprocessing=None):
    """Whether all body parts already have tracking from this source video."""
    with np.load(result['predictions'],allow_pickle=False) as prediction:
        parts=set(prediction['bodyparts'].astype(str).tolist())
    with h5py.File(result['source']['nwb'],'r') as file:
        present={item['bodypart'] for item in tracking_objects(file,result['source'])}
    return bool(parts) and parts.issubset(present)


def project_analysis_status(jobs, sources, config, shuffle, fraction, snapshot=-1, checkpoints=(), cache=None):
    """Read completed-result manifests and model files, without opening NWB or video data."""
    norm=lambda p:os.path.normcase(os.path.abspath(p))
    key=lambda s:(norm(s['nwb']),s.get('object_path',''))
    cache={} if cache is None else cache
    hashes={}
    for path in checkpoints:
        try:
            stamp=fingerprint(path);prior=cache.get(path)
            if prior is None or prior[0]!=stamp:cache[path]=(stamp,sha256(path))
            hashes[norm(path)]=cache[path][1]
        except OSError:hashes[norm(path)]=None
    candidates={key(s):[] for s in sources}
    for path, run in job_records(jobs):
        try:
            model=run.get('model',{})
            if not usable_analysis_run(run) or norm(model.get('config',''))!=norm(config):continue
            for result in run.get('results',[]):
                if key(result['source']) in candidates:candidates[key(result['source'])].append((result,model,run))
        except (OSError,ValueError,TypeError,KeyError):continue
    rows=[]
    for source in sources:
        status='Not analyzed';detail='No completed analysis for this project video.'
        selected=None
        for result,model,run in candidates[key(source)]:
            if selected is None:selected=(result,model,run)
            status='Needs reanalysis';detail='A previous result exists for a different model, checkpoint, video or timing, or its prediction file is missing.'
            if model.get('shuffle')!=shuffle or model.get('trainingsetindex')!=fraction or model.get('snapshot_index',-1)!=snapshot:continue
            prior=result['source']
            if any(k in source and k in prior and source[k]!=prior[k] for k in ('video_fingerprint','timestamps_sha256','frames')):continue
            recorded={norm(p):v for p,v in model.get('checkpoints_sha256',{}).items()}
            if recorded and hashes and recorded!=hashes:continue
            if not Path(result['predictions']).is_file():continue
            selected=(result,model,run)
            status='Analyzed';detail='Completed predictions available for the active model and checkpoint selection.';break
        if source.get('error'):status='Unavailable';detail=source['error']
        result,model,run=selected if selected else ({},{},{})
        stamp=result.get('analyzed_at') or run.get('completed_at') or run.get('updated_at')
        rows.append(dict(source=source,status=status,detail=detail,model=model,analyzed_at=stamp,date_is_run=bool(stamp and not result.get('analyzed_at'))))
    return rows


def usable_analysis_run(run):
    """Incremental runs contain only fully validated, durably saved video results."""
    return run.get('status')=='completed' or (run.get('checkpointed') is True and
            run.get('action')=='analyze' and run.get('status') in {'running','failed','cancelled','interrupted'})


def selected_analysis_results(jobs,sources,config,shuffle,trainingsetindex):
    """Choose the newest completed result per selected NWB object and model."""
    def key(source):
        return os.path.normcase(os.path.abspath(source['nwb'])),source['object_path']
    selected={key(source) for source in sources};found={}
    for path, run in job_records(jobs):
        try:
            model=run.get('model',{})
            if (not usable_analysis_run(run) or
                os.path.normcase(os.path.abspath(model.get('config','')))!=os.path.normcase(os.path.abspath(config)) or
                model.get('shuffle')!=shuffle or model.get('trainingsetindex')!=trainingsetindex):continue
            for result in run.get('results',[]):
                identity=key(result['source'])
                if identity in selected and identity not in found:found[identity]=(result,run,str(path))
        except (OSError,ValueError,KeyError,TypeError):continue
    return [found[key(source)] for source in sources if key(source) in found],len(selected-found.keys())


def saved_postprocessing(result,directories):
    """Newest applied cache for these exact predictions; never compute processing."""
    found=[]
    for directory in roots(directories):
        for manifest in directory.glob('*.json'):
            try:
                info=_pp_json_read(manifest)
                payload=manifest.with_suffix('.npz')
                if info.get('source')==result['source'] and info.get('predictions_sha256')==result['predictions_sha256'] and payload.is_file():
                    found.append(dict(path=str(payload),metadata=info,fingerprint=fingerprint(payload),stamp=manifest.stat().st_mtime_ns))
            except (OSError,ValueError,TypeError):continue
    return max(found,key=lambda item:(item['stamp'],item['path'])) if found else None


def tracking_objects(file,source):
    """Tracking coordinates and companion objects for the same source video."""
    found=[]
    norm=lambda value:os.path.normcase(str(value).replace('\\','/'))
    for module_name,module in file.get('processing',{}).items():
        for name,series in module.items():
            if not isinstance(series,h5py.Group) or 'data' not in series:continue
            try:info=_pp_decode(json.loads(text(series.attrs.get('comments','{}'))),_pp_active())
            except (ValueError,TypeError):continue
            prior=info.get('source',{})
            if prior.get('object_path')!=source['object_path'] or norm(prior.get('video',''))!=norm(source['video']):continue
            part=info.get('bodypart')
            if not part:
                desc=text(series.attrs.get('description',''))
                for prefix in ('Body part: ','Processed body part: '):
                    if desc.startswith(prefix):part=desc[len(prefix):];break
            if part:
                companions=[key for key in module if key==name or key in (name+'_likelihood',name+'_status',name+'_interpolated')]
                found.append(dict(module=module_name,name=name,bodypart=part,companions=companions,metadata=info))
    return found


def import_prediction(result,run,postprocessing=None,*,project_name=None,existing='skip'):
    """One four-column TimeSeries per body part, using applied caches only."""
    from pynwb import NWBHDF5IO,TimeSeries
    from hdmf.backends.hdf5.h5_utils import H5DataIO
    if existing not in ('skip','replace'):raise ValueError('Unknown existing-result policy.')
    source=verify_source(result['source']);values,parts=load_prediction(result)
    path=Path(source['nwb']);before=fingerprint(path)
    with h5py.File(path,'r') as file:
        times=timestamps(file[source['object_path']]);prior=tracking_objects(file,source)
    present={item['bodypart'] for item in prior}
    chosen=[(i,part) for i,part in enumerate(parts) if existing=='replace' or part not in present]
    if not chosen:return dict(nwb=str(path),module='',backup='',warnings=[],objects=0,skipped=True)
    xy=values[:,:,:2].copy();status=np.ones(values.shape[:2],dtype=np.uint8);processing_name='None';settings=None
    if postprocessing:
        cache=Path(postprocessing['path'])
        if fingerprint(cache)!=postprocessing['fingerprint']:raise ValueError('Saved postprocessing changed since the export summary. Reopen Export.')
        with np.load(cache,allow_pickle=False) as saved:
            metadata=_pp_decode(json.loads(str(saved['metadata'].item())),_pp_root(cache))
            if metadata.get('source')!=result['source'] or metadata.get('predictions_sha256')!=result['predictions_sha256']:raise ValueError('Saved postprocessing does not match the analysis.')
            if saved['bodyparts'].astype(str).tolist()!=parts or not np.array_equal(saved['timestamps'],times):raise ValueError('Saved postprocessing body parts or timing do not match.')
            xy=saved['coordinates'].copy();codes=saved['status'].copy()
        if xy.shape!=values[:,:,:2].shape or codes.shape!=status.shape or not np.isin(codes,[0,1,2,3]).all():raise ValueError('Invalid saved postprocessing arrays.')
        # Missing coordinates stay NaN; codes describe the operation, not visibility.
        status=np.where(codes==0,1,codes).astype(np.uint8)
        processing_name=metadata.get('name','Postprocessing');settings=metadata.get('settings')
    project_name=project_name or Path(run['model']['config']).parent.name
    clean=lambda value:re.sub(r'[^a-zA-Z0-9_ -]','_',str(value)).strip() or 'unnamed'
    module_name='DeepLabCut_'+clean(project_name)+'_'+hashlib.sha256(source['object_path'].encode()).hexdigest()[:8]
    provenance=dict(schema='workbench_pose_columns_v1',model=run['model'],environment=run.get('environment',{}),job_id=run['job_id'],source=source,
                    scorer=result.get('scorer',''),project_name=project_name,predictions_sha256=result['predictions_sha256'],postprocessing_name=processing_name,
                    postprocessing=settings,imported_at=datetime.now(timezone.utc).isoformat(),columns=['x','y','confidence','postprocessing'],
                    postprocessing_codes={'1':'original','2':'interpolated','3':'fixed'})
    fd,tmp=tempfile.mkstemp(prefix='.dlc_import_',suffix='.tmp',dir=path.parent);os.close(fd)
    backup=path.with_name(path.name+'.dlc_export_'+os.path.basename(tmp)+'.bak')
    expected={}
    try:
        shutil.copy2(path,tmp)
        if existing=='replace':
            with h5py.File(tmp,'r+') as file:
                for item in prior:
                    module=file['processing'][item['module']]
                    for name in item['companions']:
                        if name in module:del module[name]
                for name in list(file.get('processing',{})):
                    if not len(file['processing'][name]):del file['processing'][name]
        with NWBHDF5IO(tmp,'a',load_namespaces=True) as io:
            nwb=io.read()
            module=nwb.processing.get(module_name)
            if module is None:module=nwb.create_processing_module(module_name,'DeepLabCut tracking. '+json.dumps(provenance))
            for i,part in chosen:
                name=clean(part)+'_'+clean(project_name)
                if name in module.data_interfaces:raise ValueError('Object name collision: '+name)
                data=np.column_stack((xy[:,i],values[:,i,2],status[:,i]))
                details=dict(provenance,bodypart=part)
                module.add(TimeSeries(name=name,data=H5DataIO(data,compression='gzip'),timestamps=times,unit='mixed',
                    description='Columns: x (pixels), y (pixels), original confidence (probability), postprocessing (1 original, 2 interpolated, 3 fixed). Missing coordinates are NaN.',comments=json.dumps(details)))
                expected[name]=data
            io.write(nwb)
        with NWBHDF5IO(tmp,'r',load_namespaces=True) as io:
            saved=io.read().processing[module_name]
            for name,data in expected.items():
                if not np.array_equal(saved[name].data[:],data,equal_nan=True) or not np.array_equal(saved[name].timestamps[:],times):raise ValueError('Export verification failed.')
        verify_source(source)
        if fingerprint(path)!=before:raise ValueError('NWB changed while preparing export.')
        with backup.open('xb') as dest,path.open('rb') as original:shutil.copyfileobj(original,dest)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return dict(nwb=str(path),module=module_name,backup=str(backup),warnings=[],objects=len(chosen),skipped=False)
