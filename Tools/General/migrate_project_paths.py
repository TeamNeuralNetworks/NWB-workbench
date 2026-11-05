"""Migrate Workbench metadata to project-relative references. Preview by default.

Close Workbench and all DLC/sorting tools before applying. Scientific NWB,
video, model, HDF5 and prediction files are not rewritten. Legacy external-video
paths in NWB are resolved read-only using the project's recorded previous roots.
"""
from pathlib import Path
import argparse
import copy
from datetime import datetime
import io
import json
import os
import shutil
import sys

# Also works as python Tools/General/migrate_project_paths.py ...
PACKAGE = Path(__file__).resolve().parents[2]
if str(PACKAGE) not in sys.path: sys.path.insert(0, str(PACKAGE))
from GUI.project_paths import MARKER, encode, decode, atomic_json, _atomic_text


def excluded(path, root):
    parts = path.relative_to(root).parts
    return any(p in {'.git', '_internal', 'Script', '__pycache__', 'build', 'dist'} or
               'backup' in p.lower() or 'archive' in p.lower() or p.startswith('.portable-')
               for p in parts)


def rebase(value, old, root, key=None):
    from GUI.project_paths import MACHINE_KEYS, _path_part, _windows
    import ntpath
    if key in MACHINE_KEYS: return value
    if isinstance(value, dict):
        result={}
        for k,v in value.items():
            mapped=rebase(k,old,root)
            if mapped in result:raise ValueError('Path key collision during migration: '+str(mapped))
            result[mapped]=rebase(v,old,root,k)
        return result
    if isinstance(value, list):return [rebase(v,old,root,key) for v in value]
    if not isinstance(value,str) or not old:return value
    part,suffix=_path_part(value);module=ntpath if _windows(old) else os.path
    for anchor,dest in ((old,root),(module.dirname(old),root.parent)):
        base=module.normcase(module.normpath(anchor));candidate=module.normcase(module.normpath(part))
        if candidate==base or candidate.startswith(base+module.sep):
            return str(dest / module.relpath(part,anchor).replace('\\','/'))+suffix
    return value


def make_plan(root, previous_root=None):
    import yaml
    import openpyxl
    root=Path(root).resolve();marker=root/MARKER
    if not marker.is_file():raise ValueError(f'Not a Workbench project: {marker} is missing')
    metadata=json.loads(marker.read_text(encoding='utf-8-sig'))
    if not isinstance(metadata,dict):raise ValueError('Project metadata must be a JSON object')
    old=previous_root or metadata.get('project_root')
    if not isinstance(old,str) or old.startswith('@project/'):old=str(root)
    metadata['portable_paths_version']=1
    metadata['legacy_project_roots']=list(dict.fromkeys(metadata.get('legacy_project_roots',[])+[old,str(root)]))
    metadata['project_root']=str(root)
    metadata['inventory']=str(root/'Data'/'nwb_inventory.xlsx')
    changes=[];errors=[];warnings=[];unchanged=0
    def converted(value):return encode(rebase(value,old,root),root)
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink() or excluded(path,root) or path==marker:continue
        if path.suffix.lower() not in {'.json','.yaml','.yml','.xlsx'} and path.name!='params.py':continue
        try:
            before=path.read_bytes();suffix=path.suffix.lower()
            if path.name=='params.py':
                from GUI.project_paths import relative_phy_text
                text=before.decode('utf-8-sig');updated=relative_phy_text(text,path,root,converter=converted)
                if updated==text:unchanged+=1;continue
                after=updated.encode('utf-8')
            elif suffix=='.json':
                value=json.loads(before.decode('utf-8-sig'))
                after_value=converted(value)
                if after_value==value:unchanged+=1;continue
                after=json.dumps(after_value,indent=2,ensure_ascii=False).encode('utf-8')
            elif suffix in {'.yaml','.yml'}:
                value=yaml.safe_load(before.decode('utf-8-sig'));after_value=converted(value)
                if after_value==value:unchanged+=1;continue
                after=yaml.safe_dump(after_value,sort_keys=False,allow_unicode=True).encode('utf-8')
            else:
                # Preserve sheets, formatting and values; edit path columns only.
                workbook=openpyxl.load_workbook(io.BytesIO(before));touched=False
                for sheet in workbook:
                    columns=[c.column for c in sheet[1] if str(c.value).lower() in
                             {'file_path','nwb_path','save_path','video_path','external_file','nwb_associated_file_path'}]
                    for row in sheet.iter_rows(min_row=2):
                        for column in columns:
                            cell=row[column-1]
                            if not isinstance(cell.value,str) or cell.data_type=='f':continue
                            new=converted(cell.value)
                            if new!=cell.value:cell.value=new;touched=True
                if not touched:workbook.close();unchanged+=1;continue
                stream=io.BytesIO();workbook.save(stream);workbook.close();after=stream.getvalue()
            changes.append((path,before,after))
        except Exception as exc:errors.append({'file':str(path),'error':str(exc)})
    after=json.dumps(encode(metadata,root),indent=2,ensure_ascii=False).encode('utf-8')
    if marker.read_bytes()!=after:changes.append((marker,marker.read_bytes(),after))
    # Relative paths cannot span unrelated Windows volumes; flag remaining drives.
    for path,_,after in changes:
        if path.suffix.lower() in {'.json','.yaml','.yml'}:
            import re
            if re.search(r'[A-Za-z]:[\\/]',after.decode('utf-8',errors='replace')):
                warnings.append(str(path)+': contains absolute machine settings, historical aliases or cross-volume references; inspect if relevant.')
    return changes,errors,warnings,unchanged


def apply_plan(root,changes):
    import tempfile
    root=Path(root).resolve()
    if not changes:return None
    backup=root/'.portable-path-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    written=[]
    # Verify the preview snapshot before writing any file.
    for path,before,_ in changes:
        if path.read_bytes()!=before:raise RuntimeError(f'File changed during migration: {path}')
    backup.mkdir(parents=True)
    for path,before,_ in changes:
        copy_path=backup/path.relative_to(root);copy_path.parent.mkdir(parents=True,exist_ok=True);copy_path.write_bytes(before)
    def replace(path,data):
        fd,name=tempfile.mkstemp(prefix='.portable-',suffix='.tmp',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
    try:
        for path,before,after in changes:replace(path,after);written.append((path,before))
    except Exception:
        for path,before in reversed(written):replace(path,before)
        raise
    atomic_json(backup/'migration_report.json',{'files':[str(p.relative_to(root)) for p,_,_ in changes],
                'note':'Original bytes are retained at the same relative paths in this backup folder.'})
    return backup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',type=Path)
    parser.add_argument('--previous-root',help='Original full project path, if its drive/folder has already changed.')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    changes,errors,warnings,unchanged=make_plan(args.project,args.previous_root)
    print('APPLY' if args.apply else 'PREVIEW ONLY: no project files changed')
    print(f'{len(changes)} files to convert; {unchanged} already unchanged; {len(errors)} errors')
    for path,_,_ in changes:print('CONVERT:',path)
    for warning in warnings:print('NOTE:',warning)
    for error in errors:print('ERROR:',error['file'],error['error'])
    if errors:
        print('No migration applied. Resolve the listed errors first.');return 1
    if args.apply:
        backup=apply_plan(args.project,changes)
        print('Migration complete. Backup:',backup or 'No changes needed')
    return 0


if __name__=='__main__':raise SystemExit(main())
