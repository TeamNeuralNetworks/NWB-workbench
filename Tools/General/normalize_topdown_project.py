"""One-off repair for the audited Top down view project. Preview by default.
Close Workbench and DLC first. Use --apply to repair/archive; originals are retained.
"""
if __name__ == '__main__':
    import sys as _portable_sys
    from pathlib import Path as _PortablePath
    _portable_sys.path.insert(0, str(_PortablePath(__file__).resolve().parents[2]))
from GUI.project_paths import json_dump as _pp_json_dump, json_read as _pp_json_read
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from datetime import datetime

POLICY = json.loads('{"project": "U:\\\\new task\\\\NWB app\\\\DeepLabCut\\\\Top down view-user-2026-10-30", "name": "Top down view-user-2026-10-30", "config_text_sha256": "c962b5a7583a7e10884c7d6e3ab0525d21fd24ab62ef50f68974d2b1342a68af", "repair_jobs": ["20261031_232116_f84c0a89", "20261031_231654_56732596", "20261031_132144_b4201734", "20261031_100023_f9391ffd"], "archive": [{"path": "labeled-data/topdown view 05121 Session 1, 22-03-2025__74841e69765bc4d0", "kind": "empty"}, {"path": "labeled-data/topdown view 05121 Session 1, 25-03-2025__d87b5fa2a83d7819_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05121 Session 1, 27-03-2025__c3177f4e2c7bdf72", "kind": "empty"}, {"path": "labeled-data/topdown view 05121 Session 1, 28-03-2025__43ff29fab3d64449", "kind": "empty"}, {"path": "labeled-data/topdown view 05121 Session 2, 21-03-2025__32bdd92b5b9a7ef9", "kind": "empty"}, {"path": "labeled-data/topdown view 05121 Session 2, 26-03-2025__585abf5ca088b8bf", "kind": "empty"}, {"path": "labeled-data/topdown view 05121 Session 2, 29-03-2025__cc8ed092c9ee053c_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05122 Session 1, 19-03-2025__5453a3a4706602ea", "kind": "empty"}, {"path": "labeled-data/topdown view 05122 Session 1, 20-03-2025__96dbeef14a0c5b7d", "kind": "empty"}, {"path": "labeled-data/topdown view 05122 Session 1, 22-03-2025__d912ee381e150d76_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05122 Session 2, 18-03-2025__82c58620e5bb99e5_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05122 Session 2, 19-03-2025__3f0049a9faa7ad4c", "kind": "empty"}, {"path": "labeled-data/topdown view 05122 Session 2, 21-03-2025__3d7281ae4ab2c859_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05122 Session 2, 28-03-2025__472025170dacac8f", "kind": "empty"}, {"path": "labeled-data/topdown view 05123 Session 1, 22-03-2025__fe73e4ad71789d64", "kind": "empty"}, {"path": "labeled-data/topdown view 05123 Session 1, 27-03-2025__780eced07e5e1c4a", "kind": "empty"}, {"path": "labeled-data/topdown view 05123 Session 2, 19-03-2025__105699fe1f4e2154_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05123 Session 2, 27-03-2025__8975d156e668fea3", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 1, 02-04-2025__1f67d537980d257e", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 1, 03-04-2025__1f20e6af74cd45e2_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05131 Session 1, 04-04-2025__873953a72e76d587_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05131 Session 1, 10-04-2025__271099b81cb4943e", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 1, 20-03-2025__deeaec5292ee08bb", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 1, 27-03-2025__5a2db1f5fab54466", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 2, 08-04-2025__128ee0da5da8f830", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 2, 10-04-2025__36ca3d6597035fef", "kind": "empty"}, {"path": "labeled-data/topdown view 05131 Session 2, 26-03-2025__fd3076a02da42504_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05131 Session 2, 31-03-2025__d1cb3f78162a0996", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 1, 05-04-2025__eaa119f2b366b923", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 1, 08-04-2025__26ad2dc25864aee4", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 1, 10-04-2025__af965a07b2e071e1", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 1, 11-04-2025__ea3372ad9db3440a_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05132 Session 1, 18-03-2025__b56563ba64c6d4d9_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05132 Session 1, 21-03-2025__14c1cdf656a4b195_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05132 Session 2, 10-04-2025__89a353756b59f3c7", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 2, 19-03-2025__ecef1a2cd889aba8", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 2, 27-03-2025__05a47245181db519", "kind": "empty"}, {"path": "labeled-data/topdown view 05132 Session 2, 28-03-2025__b149d288e82b3cb1_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05133 Session 1, 07-04-2025__6f1e14fe10e8a7c5_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05133 Session 1, 09-04-2025__cf9bcc25a84c23a1_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05133 Session 1, 10-04-2025__89356d70b0a24d51", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 1, 11-04-2025__d4d955522c22e82b", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 1, 22-03-2025__07f8f0e5b617b530_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05133 Session 1, 22-03-2025__b783e05b96ebec2a", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 1, 24-03-2025__a6b5dc89c96c5291", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 1, 29-03-2025__ef29fbf6228cce93", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 2, 09-04-2025__daaa6797f8f1126c", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 2, 19-03-2025__9f56cf8d55019f26", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 2, 20-03-2025__4d9f0bc377e591d6", "kind": "empty"}, {"path": "labeled-data/topdown view 05133 Session 2, 25-03-2025__b7b8ee0cc94b83ec_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05133 Session 2, 28-03-2025__c442761a8abda35d_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05133 Session 2, 31-03-2025__1f28bec781ea73dd_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05212 Session 1, 22-03-2025__fa92cafcc431dc0d", "kind": "empty"}, {"path": "labeled-data/topdown view 05212 Session 2, 18-03-2025__249d94f8ab437819_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05212 Session 2, 20-03-2025__f0a7d67147ae7842_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05241 Session 1, 20-03-2025__0b1b83605cc093e1", "kind": "empty"}, {"path": "labeled-data/topdown view 05241 Session 1, 21-03-2025__df557bcc1e659b71", "kind": "empty"}, {"path": "labeled-data/topdown view 05241 Session 2, 19-03-2025__7f24c2f760ea2453", "kind": "empty"}, {"path": "labeled-data/topdown view 05241 Session 2, 31-03-2025__fda6a9d3568c352e", "kind": "empty"}, {"path": "labeled-data/topdown view 05242 Session 1, 24-03-2025__0cea489152428230_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05242 Session 1, 24-03-2025__5adc150f87deddb8_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05242 Session 1, 25-03-2025__eb6a9d79fd5df3be", "kind": "empty"}, {"path": "labeled-data/topdown view 05242 Session 1, 26-03-2025__ad89c6340033b5ea_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 05242 Session 2, 19-03-2025__a33d5fa3c8fba6af", "kind": "empty"}, {"path": "labeled-data/topdown view 05242 Session 2, 28-03-2025__fa88bd87121cc1f4", "kind": "empty"}, {"path": "labeled-data/topdown view 6111 Session 1, 18-08-2025__0f46a32d95a1cfb7_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6111 Session 1, 19-08-2025__e1a0f96b4be6fa42", "kind": "empty"}, {"path": "labeled-data/topdown view 6111 Session 1, 21-08-2025__4b171bea592b2ce9", "kind": "empty"}, {"path": "labeled-data/topdown view 6112 Session 1, 14-08-2025__348a1945c9519bc3_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6112 Session 1, 18-08-2025__bf14fed51bcf0caf", "kind": "empty"}, {"path": "labeled-data/topdown view 6113 Session 1, 15-08-2025__5cec4350455654ec_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6113 Session 1, 18-08-2025__7399c84975ea6b95_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6113 Session 1, 18-08-2025__cd44d930acc7a399", "kind": "empty"}, {"path": "labeled-data/topdown view 6113 Session 1, 19-08-2025__159af3f60f4e3be9_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6113 Session 1, 19-08-2025__be8f087a036b0aaf", "kind": "empty"}, {"path": "labeled-data/topdown view 6113 Session 1, 21-08-2025__1b6d8e2f34d46ff6", "kind": "empty"}, {"path": "labeled-data/topdown view 6113 Session 1, 22-08-2025__17ac40ffba618c37", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 06-08-2025__1a974457cf844475", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 08-08-2025__3059d9dd243021fe", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 08-08-2025__8e8d3baed6a28d1a", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 09-08-2025__5e27a5a48b3ef534", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 12-08-2025__320e3bc3f3b24e0b", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 14-08-2025__a9ac44053337152b", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 15-08-2025__281739d2365c3676", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 16-08-2025__f1f721a976b01a68_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6141 Session 1, 18-08-2025__68f2fd186cb33a5f", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 20-08-2025__3f3311236fa6d9ae_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6141 Session 1, 21-08-2025__9cc80e8ed065913e", "kind": "empty"}, {"path": "labeled-data/topdown view 6141 Session 1, 22-08-2025__e8e5ad7ac1fd5000", "kind": "empty"}, {"path": "labeled-data/topdown view 6221 Session 1, 06-08-2025__cfaa11e7b53ac2fb", "kind": "empty"}, {"path": "labeled-data/topdown view 6221 Session 1, 07-08-2025__113511f479c6494d", "kind": "empty"}, {"path": "labeled-data/topdown view 6221 Session 1, 07-08-2025__83f0502deed0d25c", "kind": "empty"}, {"path": "labeled-data/topdown view 6221 Session 1, 09-08-2025__4a867a0d7707e663_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6221 Session 1, 09-08-2025__4cf9d36a6fe00c79", "kind": "empty"}, {"path": "labeled-data/topdown view 6221 Session 1, 13-08-2025__b505168df1048011", "kind": "empty"}, {"path": "labeled-data/topdown view 6222 Session 1, 08-08-2025__048d15e6cb6f42e9", "kind": "empty"}, {"path": "labeled-data/topdown view 6222 Session 1, 13-08-2025__4dcdfad2f9d0aeae_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6222 Session 1, 13-08-2025__d95b53d8f3f67853", "kind": "empty"}, {"path": "labeled-data/topdown view 6222 Session 1, 15-08-2025__d5a21bdefe1ce1d9_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6222 Session 1, 18-08-2025__01fdb2ecf3146608_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6222 Session 1, 19-08-2025__d27540f57f8e5d31_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6222 Session 1, 20-08-2025__bb5c7cc58a5d02cd", "kind": "empty"}, {"path": "labeled-data/topdown view 6222 Session 1, 25-08-2025__ece4e6883aab7fee", "kind": "empty"}, {"path": "labeled-data/topdown view 6223 Session 1, 16-08-2025__e59af32d78f7d786_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6223 Session 1, 21-08-2025__d8255d3f7260cec3", "kind": "empty"}, {"path": "labeled-data/topdown view 6223 Session 1, 22-08-2025__92d4bb1f892282d3", "kind": "empty"}, {"path": "labeled-data/topdown view 6232 Session 1, 15-08-2025__474c2c9997662ae9_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6232 Session 1, 18-08-2025__da67e77bc95a11c8", "kind": "empty"}, {"path": "labeled-data/topdown view 6232 Session 1, 19-08-2025__22be76805f2f5ca8", "kind": "empty"}, {"path": "labeled-data/topdown view 6232 Session 1, 20-08-2025__a5ef5975ec5cadac", "kind": "empty"}, {"path": "labeled-data/topdown view 6232 Session 1, 21-08-2025__0e05c9df89b0ebe5_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6233 Session 1, 16-08-2025__95ce7c3b4e71600a_labeled", "kind": "preview"}, {"path": "labeled-data/topdown view 6233 Session 1, 21-08-2025__28586a6d4bf1e7f6_labeled", "kind": "preview"}, {"path": "config.yaml.before_video_selection_1b02bcd8.bak", "kind": "backup"}, {"path": "config.yaml.before_video_selection_f646c5a5.bak", "kind": "backup"}, {"path": "config.yaml.before_video_selection_f824739d.bak", "kind": "backup"}, {"path": "workbench/migration_report.json", "kind": "report"}]}')  # Replaced with the audited, project-specific plan below.


def xp(path):
    value=os.path.abspath(os.fspath(path))
    if os.name!='nt' or value.startswith('\\\\?\\'):return value
    return '\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value


def sha(path):
    h=hashlib.sha256()
    with Path(xp(path)).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def read(path):return _pp_json_read(Path(xp(path)))


def write(path,value):
    path=Path(xp(path));path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.fix-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:_pp_json_dump(value,stream,indent=2)
        for attempt in range(12):
            try:os.replace(tmp,path);break
            except PermissionError:
                if attempt==11:raise
                time.sleep(.1)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def copy(source,target):
    source=Path(xp(source));target=Path(xp(target));target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        if sha(source)!=sha(target):raise ValueError('Existing file differs; kept untouched: '+str(target))
        return
    fd,tmp=tempfile.mkstemp(prefix='.copy-',suffix='.tmp',dir=target.parent);os.close(fd)
    try:
        shutil.copy2(source,tmp)
        if sha(source)!=sha(tmp):raise ValueError('Copy verification failed: '+str(source))
        os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def link(path):
    info=path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)


def repair_job(root,identifier,apply):
    old=root.parent/'jobs'/identifier;dest=root/'workbench'/'jobs'/identifier
    request=read(old/'request.json');result=read(old/'result.json')
    owner=result.get('model',{}).get('config') or request.get('config')
    if not owner or os.path.normcase(xp(owner))!=os.path.normcase(xp(root/'config.yaml')):
        raise ValueError('Job belongs to a different project: '+identifier)
    if result.get('status')=='running':raise ValueError('Job is marked running; stop it first: '+identifier)
    analysis=root/'workbench'/'analysis'/identifier
    def rewrite(value):
        if isinstance(value,dict):return {k:rewrite(v) for k,v in value.items()}
        if isinstance(value,list):return [rewrite(v) for v in value]
        if isinstance(value,str):
            try:relative=Path(xp(value)).relative_to(Path(xp(old)))
            except ValueError:return value
            if relative.parts and relative.parts[0]=='project_inputs':return value
            # Keep stored paths readable rather than saving Windows extended prefixes.
            location=analysis if relative.parts and relative.parts[0].startswith('video_') else dest
            return str(location/relative).removeprefix('\\\\?\\')
        return value
    for item in old.rglob('*'):
        rel=item.relative_to(old)
        if 'project_inputs' in rel.parts or link(item):continue
        if not item.is_file() or item.suffix=='.tmp' or (len(rel.parts)==1 and item.suffix=='.json'):continue
        if apply:copy(item,(analysis if rel.parts[0].startswith('video_') else dest)/rel)
    updated=rewrite(result)
    for prediction in updated.get('results',[]):
        path=Path(xp(prediction['predictions']))
        if apply and (not path.is_file() or sha(path)!=prediction['predictions_sha256']):
            raise ValueError('Saved prediction verification failed: '+str(path))
    if apply:
        for name,value in (('request.json',rewrite(request)),('result.json',updated)):
            target=dest/name
            if target.exists() and read(target)!=value:raise ValueError('Existing record differs; kept untouched: '+str(target))
        write(dest/'request.json',rewrite(request));write(dest/'result.json',updated)
    return dict(job=identifier,status='repaired' if apply else 'would repair',saved_results=len(result.get('results',[])))


def eligible(root,item):
    path=root/item['path']
    if not path.exists():return False
    if link(path):raise ValueError('Refusing to archive a link: '+str(path))
    if item['kind']=='empty':return path.is_dir() and not any(path.iterdir())
    if item['kind']=='preview':
        original=path.with_name(path.name[:-8])
        if not original.is_dir() or not any(original.glob('CollectedData_*.h5')):return False
        files=list(path.iterdir())
        return bool(files) and all(p.is_file() and not link(p) and p.suffix.lower() in {'.png','.jpg','.jpeg','.tif','.tiff','.bmp'} for p in files)
    return path.is_file()


def recover_history(root,apply,archive):
    path=root/'workbench_training_selection.json';state=read(path)
    if state.get('trained_models',{}).get('1:0'):return 'Existing trained-model history kept.'
    candidates={}
    for directory in (root/'workbench'/'jobs',root.parent/'jobs'):
        for request in directory.glob('*/request.json'):candidates.setdefault(request.parent.name,request)
    for key in sorted(candidates,reverse=True):
        request=candidates[key]
        try:
            job=read(request);result=read(request.with_name('result.json'))
            if job.get('action')!='train' or job.get('shuffle',1)!=1 or job.get('trainingsetindex',0)!=0 or result.get('status')!='completed' or not job.get('training_selection'):continue
            if os.path.normcase(xp(job.get('config','')))!=os.path.normcase(xp(root/'config.yaml')):continue
            record=dict(name=job.get('model_name','Model 1'),training_selection=job['training_selection'],recovered_from=str(request),trained_at=result.get('completed_at',''))
            if apply:
                copy(path,archive/'metadata'/'workbench_training_selection.json')
                state.setdefault('trained_models',{})['1:0']=record;write(path,state)
            return 'Recovered training selection from '+key
        except (OSError,ValueError,KeyError,TypeError):continue
    return 'Exact historical training selection not found; kept existing metadata without inventing a list.'


def normalize(root,apply=False):
    root=Path(xp(root))
    if root.name!=POLICY['name']:raise ValueError('This script is only for '+POLICY['name'])
    config=root/'config.yaml'
    if hashlib.sha256(config.read_text(encoding='utf-8').encode()).hexdigest()!=POLICY['config_text_sha256']:
        raise ValueError('config.yaml changed since the audit. No cleanup performed; send an updated audit.')
    for path in (root/'workbench'/'jobs').glob('*/result.json'):
        if read(path).get('status')=='running':raise ValueError('A run is marked running: '+str(path.parent)+'. Stop the job before cleanup.')
    # Preserve a manifest of all trained weights and source annotation tables.
    protected=[config]+list(root.glob('dlc-models-pytorch/**/*.pt'))+list(root.glob('labeled-data/*/CollectedData_*'))
    before={str(p.relative_to(root)):(p.stat().st_size,p.stat().st_mtime_ns) for p in protected}
    archive=root.parent/(root.name+'_cleanup_archive_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    report=dict(project=str(root),apply=apply,archive=str(archive) if apply else None,jobs=[],archived=[],kept=[],errors=[])
    for identifier in POLICY['repair_jobs']:
        try:report['jobs'].append(repair_job(root,identifier,apply))
        except (OSError,ValueError,KeyError) as exc:report['errors'].append(str(exc))
    for item in POLICY['archive']:
        if item['path']=='workbench/migration_report.json' and report['errors']:continue
        if not eligible(root,item):report['kept'].append(item['path']);continue
        source=root/item['path'];target=archive/item['path']
        if apply:
            target.parent.mkdir(parents=True,exist_ok=True)
            os.rename(source,target)
        report['archived'].append(item['path'])
    report['training_history']=recover_history(root,apply,archive)
    after={str(p.relative_to(root)):(p.stat().st_size,p.stat().st_mtime_ns) for p in protected}
    if before!=after:raise ValueError('A protected file changed during cleanup; stop other applications and inspect the archive.')
    report['protected_files_unchanged']=len(protected)
    if apply:write(archive/'cleanup_report.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',default=POLICY['project']);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    report=normalize(args.project,args.apply)
    print(('APPLIED' if args.apply else 'PREVIEW ONLY')+f": {len(report['archived'])} archive candidates, {len(report['jobs'])} job repairs, {len(report['errors'])} issue(s).")
    print(report['training_history'])
    for error in report['errors']:print('ISSUE: '+error)
    print('Archive/report: '+report['archive'] if args.apply else 'Use --apply after closing Workbench/DLC. Nothing has changed.')


if __name__=='__main__':main()
