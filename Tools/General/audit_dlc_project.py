"""Read-only audit for preparing a project-specific DeepLabCut cleanup.

Run in Spyder or Python. Enter the folder containing config.yaml when prompted,
or supply it as the first argument. The report is written outside the project.
No project, video, label, prediction or model file is modified or deleted.
"""
if __name__ == '__main__':
    import sys as _portable_sys
    from pathlib import Path as _PortablePath
    _portable_sys.path.insert(0, str(_PortablePath(__file__).resolve().parents[2]))
from GUI.project_paths import json_dump as _pp_json_dump, json_read as _pp_json_read
from pathlib import Path
import argparse
from collections import Counter
from datetime import datetime,timezone
import json
import os
import stat
import sys

IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.tif','.tiff','.bmp'}
KNOWN_FOLDERS={'videos','labeled-data','training-datasets','dlc-models','dlc-models-pytorch',
               'evaluation-results','evaluation-results-pytorch','workbench'}


def _extended(path):
    value=os.path.abspath(os.fspath(path))
    if os.name!='nt' or value.startswith('\\\\?\\'):return value
    return '\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value


def _link(path):
    info=path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)


def audit_project(folder,max_entries=12000):
    root=Path(_extended(Path(folder).expanduser()))
    if not (root/'config.yaml').is_file():
        raise ValueError('Choose the DeepLabCut project folder containing config.yaml.')
    report=dict(project=str(root),created_at=datetime.now(timezone.utc).isoformat(),
                read_only=True,folders=[],files=[],links=[],metadata={},errors=[],truncated=False)
    summaries={};stack=[root];entries=0
    while stack:
        directory=stack.pop()
        try:children=sorted(directory.iterdir(),key=lambda p:p.name.casefold())
        except OSError as exc:report['errors'].append(str(directory)+': '+str(exc));continue
        relative=str(directory.relative_to(root));summary=dict(path=relative,files=0,bytes=0,extensions={})
        counts=Counter()
        for path in children:
            entries+=1
            if entries>max_entries:
                report['truncated']=True;break
            try:
                info=path.lstat();rel=str(path.relative_to(root))
                if _link(path):
                    report['links'].append(dict(path=rel,target=str(path.resolve(strict=False)),exists=path.exists()))
                    continue  # Never traverse links/junctions into raw data or another project.
                if path.is_dir():stack.append(path);continue
                summary['files']+=1;summary['bytes']+=info.st_size;counts[path.suffix.lower() or '(none)']+=1
                # Image filenames are summarized, not read or individually listed.
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    report['files'].append(dict(path=rel,bytes=info.st_size,links=info.st_nlink))
                if path.suffix.lower() in {'.yaml','.yml'} or path.name in {'workbench_training_selection.json','workbench.json','migration_report.json'}:
                    if info.st_size<=256000:
                        report['metadata'][rel]=path.read_text(encoding='utf-8-sig',errors='replace')
                    else:report['metadata'][rel]='File too large for inline audit; see file listing.'
            except OSError as exc:report['errors'].append(str(path)+': '+str(exc))
        summary['extensions']=dict(counts);summaries[relative]=summary
        if report['truncated']:break
    report['folders']=sorted(summaries.values(),key=lambda row:row['path'])
    report['unclassified_top_level_folders']=[p.name for p in root.iterdir() if p.is_dir() and p.name not in KNOWN_FOLDERS]
    report['workbench_layout']={name:(root/'workbench'/name).is_dir() for name in ('analysis','postprocessed','jobs')}
    # Job records expose saved paths needed for migration/cleanup decisions.
    for container in (root/'workbench'/'jobs',root/'jobs'):
        if _safe_directory(container) and _safe_directory(container.parent):
            for directory in sorted(container.iterdir(),reverse=True)[:12]:
                if not _safe_directory(directory):continue
                for name in ('request.json','result.json'):
                    path=directory/name
                    try:
                        if path.is_file() and not _link(path) and path.stat().st_size<=512000:
                            report['metadata'][str(path.relative_to(root))]=_pp_json_read(path)
                    except (OSError,ValueError) as exc:report['errors'].append(str(path)+': '+str(exc))
    report['notes']=[
        'Unclassified folders are not necessarily obsolete. No deletion is recommended automatically.',
        'Model weights, prediction arrays, images and videos were not opened.',
        'Hard-link counts do not identify the other filenames; original video mappings may appear in configuration/selection metadata.',
        'Different DLC versions can legitimately create different folders. Only project-specific obsolete files should be removed.'
    ]
    return report


def _safe_directory(path):
    return path.is_dir() and not _link(path)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',nargs='?',help='Folder containing config.yaml')
    parser.add_argument('--output',help='Report filename outside the project')
    parser.add_argument('--max-entries',type=int,default=12000)
    args=parser.parse_args(argv)
    folder=args.project or input('DeepLabCut project folder (containing config.yaml): ').strip().strip('"')
    report=audit_project(folder,max_entries=args.max_entries)
    root=Path(report['project']).resolve()
    output=Path(args.output).expanduser().absolute() if args.output else root.parent/(root.name+'_audit_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.json')
    if output.resolve().is_relative_to(root):
        raise ValueError('The report must be saved outside the project folder.')
    with output.open('x',encoding='utf-8') as stream:_pp_json_dump(report,stream,indent=2)
    print('Project unchanged. Report saved to: '+str(output))
    if report['truncated']:print('The scan reached its entry limit; see the report before deciding whether a fuller scan is needed.')
    return output


if __name__=='__main__':
    try:main()
    except (OSError,ValueError) as exc:
        print('Audit could not finish: '+str(exc),file=sys.stderr);raise SystemExit(1)
