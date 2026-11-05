"""Project-owned Workbench output paths and result discovery."""
try:
    from project_paths import json_dump as _pp_json_dump, json_read as _pp_json_read
except ModuleNotFoundError:
    from GUI.project_paths import json_dump as _pp_json_dump, json_read as _pp_json_read
from pathlib import Path
import os


def storage_root(config):
    return Path(config).resolve().parent / 'workbench'


def roots(value):
    return [Path(value)] if isinstance(value,(str,os.PathLike)) else [Path(p) for p in value]


def job_paths(value,name='result.json'):
    # Prefer project-local records when a migrated legacy copy also exists.
    found={}
    for root in roots(value):
        for path in root.glob('*/'+name):found.setdefault(path.parent.name,path)
    return [found[key] for key in sorted(found,reverse=True)]


def extended_path(path):
    """Use extended Windows paths for file operations without changing saved paths."""
    value=os.path.abspath(os.fspath(path))
    if os.name!='nt' or value.startswith('\\\\?\\'):return value
    return '\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value


def atomic_json(path,value,create_parent=False):
    """Publish a complete JSON file, retrying transient Windows sharing failures."""
    import json
    import tempfile
    import time
    try:
        from project_paths import encode, project_root
    except ModuleNotFoundError:
        from GUI.project_paths import encode, project_root
    value=encode(value, project_root(path))
    target=Path(extended_path(path))
    if create_parent:target.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.nwb-',suffix='.tmp',dir=target.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            _pp_json_dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())
        for attempt in range(12):
            try:os.replace(name,target);break
            except OSError as exc:
                if (not isinstance(exc,PermissionError) and getattr(exc,'winerror',None) not in {5,32,33}) or attempt==11:raise
                time.sleep(min(.025*(attempt+1),.15))
    finally:
        try:os.unlink(name)
        except FileNotFoundError:pass


def job_records(value):
    """Read a rebuildable per-folder summary; only reopen changed manifests.

    File stats are deliberately checked: external workers, interrupted analyses
    and manual removals must never leave completed-result availability stale.
    """
    import json
    found = {}
    for root in roots(value):
        summary = root / 'workbench_result_index.json'
        try:
            saved = _pp_json_read(summary)
            old = saved['records'] if saved.get('version') == 1 else {}
            if not isinstance(old, dict): old = {}
        except (OSError, ValueError, TypeError, KeyError):
            old = {}
        current = {}
        for path in root.glob('*/result.json'):
            try:
                stat = path.stat()
                stamp = [stat.st_mtime_ns, stat.st_size]
                entry = old.get(path.parent.name, {})
                if not isinstance(entry, dict) or entry.get('stamp') != stamp or not isinstance(entry.get('run'), dict):
                    run = _pp_json_read(path)
                    if not isinstance(run, dict): continue
                    entry = {'stamp': stamp, 'run': run}
                if not isinstance(entry.get('run'), dict): continue
                current[path.parent.name] = entry
                found.setdefault(path.parent.name, (path, entry['run']))
            except (OSError, ValueError, TypeError):
                continue
        if current != old:
            try: atomic_json(summary, {'version': 1, 'records': current})
            except OSError: pass  # Read-only projects remain usable.
    return [found[key] for key in sorted(found, reverse=True)]
