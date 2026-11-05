"""Project-relative persistence. Runtime objects always receive absolute paths.

Only opted-in projects are changed. The explicit @project/ prefix distinguishes
Workbench references from third-party paths relative to their own output folder.
No process cwd changes or global json/pandas monkey patches are used.
"""
from pathlib import Path
import copy
import json
import ntpath
import os
import re
import tempfile
from functools import lru_cache
from contextlib import contextmanager

@lru_cache(maxsize=32)
def _metadata(path, mtime, size):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


_ACTIVE_ROOT = None

def configure_project_context(root):
    global _ACTIVE_ROOT
    _ACTIVE_ROOT = project_root(Path(root) / '_context')


def active_root():
    return _ACTIVE_ROOT


MARKER = 'nwb_workbench_project.json'
PREFIX = '@project/'
MACHINE_KEYS = {'python', 'python_executable', 'device_environment',
                'environments', 'legacy_project_roots', 'object_path', 'data_path',
                'unit', 'units', 'description', 'label', 'title', 'name',
                'bodyparts', 'skeleton', 'subject_id', 'session_id', 'identifier'}


def project_root(path):
    try:
        candidate = Path(path).absolute()
    except (TypeError, ValueError):
        return None
    if _ACTIVE_ROOT is not None and candidate.is_relative_to(_ACTIVE_ROOT):
        return _ACTIVE_ROOT
    if candidate.name == MARKER and not candidate.exists():
        return candidate.parent
    for folder in (candidate.parent, *candidate.parents):
        marker = folder / MARKER
        if marker.is_file():
            try:
                if json.loads(marker.read_text(encoding='utf-8-sig')).get('portable_paths_version') == 1:
                    return folder
            except (OSError, ValueError, AttributeError):
                return None
            return None
    return None


def _windows(value):
    return bool(re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith('\\\\'))


def _path_part(value):
    # Video selections identify an NWB file and an internal object after '|'.
    first, separator, rest = value.partition('|')
    return first, separator + rest


def resolve_path(value, root):
    if not isinstance(value, str) or not root:
        return value
    part, suffix = _path_part(value)
    if part.startswith(PREFIX):
        relative = part[len(PREFIX):]
        if _windows(relative) or relative.startswith('/'):
            raise ValueError('Invalid project-relative reference: ' + value)
        return str((Path(root) / relative).resolve()) + suffix
    if not (_windows(part) or os.path.isabs(part)):
        return value
    # Legacy references inside unchanged NWB/NPZ files can be relocated without
    # modifying scientific data or invalidating stored prediction hashes.
    try:
        marker = Path(root) / MARKER
        stamp = marker.stat()
        metadata = _metadata(str(marker), stamp.st_mtime_ns, stamp.st_size)
        old_roots = metadata.get('legacy_project_roots', [])
    except (OSError, ValueError):
        old_roots = []
    for old in old_roots:
        module = ntpath if _windows(old) else os.path
        normalized = module.normcase(module.normpath(part))
        for anchor, destination in ((old, Path(root)), (module.dirname(old), Path(root).parent)):
            base = module.normcase(module.normpath(anchor))
            if normalized == base or normalized.startswith(base + module.sep):
                relative = module.relpath(part, anchor).replace('\\', '/')
                return str((destination / relative).resolve()) + suffix
    return value


def resolve_video_path(value, nwb_path, root=None):
    """Resolve an ImageSeries reference and collapse parent segments before I/O.

    Ordinary relative ImageSeries paths are relative to the NWB, not the project.
    Lexical normalization avoids handing Windows unnecessarily long paths with
    repeated parent segments. No filesystem access is needed for this step.
    """
    value = resolve_path(str(value), root if root is not None else active_root())
    module = ntpath if _windows(str(nwb_path)) else os.path
    value = value.replace('\\', '/')
    if not module.isabs(value):
        value = module.join(module.dirname(str(nwb_path)), value)
    return module.normpath(value)


def encode(value, root, key=None):
    if not root or key in MACHINE_KEYS:
        return value
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            mapped = encode(k, root)
            if mapped in result: raise ValueError('Path key collision while saving: ' + str(mapped))
            result[mapped] = encode(v, root, k)
        return result
    if isinstance(value, list):
        return [encode(v, root, key) for v in value]
    if isinstance(value, tuple):
        return tuple(encode(v, root, key) for v in value)
    if not isinstance(value, (str, os.PathLike)):
        return value
    value = os.fspath(value)
    if value.startswith(PREFIX): return value
    part, suffix = _path_part(value)
    # Do not reinterpret NWB-internal object references as filesystem paths.
    if re.match(r'^/(acquisition|processing|intervals|general|stimulus|units)(/|$)', part):
        return value
    if not (_windows(part) or os.path.isabs(part)): return value
    module = ntpath if _windows(part) else os.path
    try:
        relative = module.relpath(part, os.fspath(root)).replace('\\', '/')
    except ValueError:  # Different Windows volume: cannot be made relative.
        return value
    return PREFIX + relative + suffix


def decode(value, root, key=None):
    if not root or key in MACHINE_KEYS: return value
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            mapped = decode(k, root)
            if mapped in result: raise ValueError('Path key collision while loading: ' + str(mapped))
            result[mapped] = decode(v, root, k)
        return result
    if isinstance(value, list): return [decode(v, root, key) for v in value]
    if isinstance(value, tuple): return tuple(decode(v, root, key) for v in value)
    return resolve_path(value, root) if isinstance(value, str) else value


def json_read(path, **_):
    path = Path(path)
    return decode(json.loads(path.read_text(encoding='utf-8-sig')), project_root(path))


def json_load(stream, **kwargs):
    return decode(json.load(stream, **kwargs), project_root(getattr(stream, 'name', None)))


def json_dump(value, stream, **kwargs):
    return json.dump(encode(value, project_root(getattr(stream, 'name', None))), stream, **kwargs)


def json_write(path, value, *, dump_options=None, **_):
    path = Path(path)
    return path.write_text(json.dumps(encode(value, project_root(path)), **(dump_options or {})), encoding='utf-8')


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.portable-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def frame_paths(frame, root, writing=False):
    if not root: return frame
    result = frame.copy()
    # Change file references only, never signal names or scientific values.
    for column in result.columns:
        if str(column).lower() in {'file_path', 'nwb_path', 'nwb_associated_file_path', 'video_path', 'external_file', 'save_path'}:
            fn = encode if writing else decode
            result[column] = result[column].map(lambda v: fn(v, root))
    return result


def read_excel(path, *args, **kwargs):
    import pandas as pd
    value = pd.read_excel(path, *args, **kwargs)
    source = getattr(path, 'io', path)
    if not isinstance(source, (str, os.PathLike)):
        source = getattr(source, 'name', source)
    root = project_root(source)
    if isinstance(value, dict): return {k: frame_paths(v, root) for k, v in value.items()}
    return frame_paths(value, root)


def to_excel(frame, writer, *args, **kwargs):
    path = getattr(writer, '_nwb_destination', None)
    if path is None:
        path = writer if isinstance(writer, (str, os.PathLike)) else getattr(getattr(writer, '_handles', None), 'handle', None)
        if not isinstance(path, (str, os.PathLike)):
            path = getattr(path, 'name', path)
    return frame_paths(frame, project_root(path), writing=True).to_excel(writer, *args, **kwargs)


@contextmanager
def external_paths(folder):
    """Temporarily materialize third-party configuration files for a library.

    Existing ordinary relative references are preserved. Only @project/ values
    are expanded. The mutated configurations are encoded again on exit.
    """
    folder = Path(folder)
    root = project_root(folder / '_context')
    if not root:
        yield
        return
    import yaml
    paths = list(folder.rglob('*.json')) + list(folder.rglob('*.yaml')) + list(folder.rglob('*.yml'))
    changed = []
    try:
        for path in paths:
            if any(part in {'workbench', 'jobs', '.git', '__pycache__'} for part in path.relative_to(folder).parts): continue
            if path.is_symlink(): continue
            try:
                text = path.read_text(encoding='utf-8-sig')
                original = json.loads(text) if path.suffix == '.json' else yaml.safe_load(text)
                value = decode(original, root)
                if value == original: continue
                if path.suffix == '.json': atomic_json(path, value)
                else: _atomic_text(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
                changed.append(path)
            except (ValueError, OSError, yaml.YAMLError) as exc:
                raise RuntimeError(f'Cannot resolve external configuration {path}: {exc}') from exc
        yield
    finally:
        # Include new configurations generated during the action.
        paths = set(changed) | set(folder.rglob('*.json')) | set(folder.rglob('*.yaml')) | set(folder.rglob('*.yml'))
        for path in paths:
            if any(part in {'workbench', 'jobs', '.git', '__pycache__'} for part in path.relative_to(folder).parts): continue
            if path.is_symlink(): continue
            try:
                text = path.read_text(encoding='utf-8-sig')
                value = json.loads(text) if path.suffix == '.json' else yaml.safe_load(text)
                encoded = encode(value, root)
                if encoded == value: continue
                if path.suffix == '.json': atomic_json(path, encoded)
                else: _atomic_text(path, yaml.safe_dump(encoded, sort_keys=False, allow_unicode=True))
            except (ValueError, OSError, yaml.YAMLError) as exc:
                # Report rather than silently discard a third-party update.
                print(f'Portable path warning: {path}: {exc}', flush=True)


def external_call(function, folder, *args, **kwargs):
    with external_paths(folder): return function(*args, **kwargs)


def compact_external_configs(folder):
    """Encode third-party config paths after an operation has finished."""
    folder = Path(folder)
    root = project_root(folder / '_context')
    if not root or not folder.exists(): return
    import yaml
    for pattern in ('*.json', '*.yaml', '*.yml'):
        for path in folder.rglob(pattern):
            if path.is_symlink() or any('backup' in p.lower() or 'archive' in p.lower() for p in path.relative_to(folder).parts): continue
            try:
                raw = path.read_text(encoding='utf-8-sig')
                value = json.loads(raw) if path.suffix == '.json' else yaml.safe_load(raw)
                relative = encode(decode(value, root), root)
                if relative == value: continue
                if path.suffix == '.json': atomic_json(path, relative)
                else: _atomic_text(path, yaml.safe_dump(relative, sort_keys=False, allow_unicode=True))
            except (ValueError, OSError, yaml.YAMLError) as exc:
                print(f'Portable path warning: {path}: {exc}', flush=True)


def _atomic_text(path, text):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.portable-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def relative_phy_text(text, path, root=None, converter=None):
    """Use Phy's native params.py-relative dat_path, without executing Python."""
    import ast
    root = root or project_root(path)
    if not root: return text
    tree=ast.parse(text);lines=text.splitlines(keepends=True);offsets=[0]
    for line in lines: offsets.append(offsets[-1]+len(line))
    replacements=[]
    for node in tree.body:
        if not isinstance(node,ast.Assign) or not any(isinstance(t,ast.Name) and t.id=='dat_path' for t in node.targets): continue
        try:value=ast.literal_eval(node.value)
        except (ValueError,TypeError):continue
        if not isinstance(value,str):continue
        absolute=resolve_path(converter(value) if converter else value,root)
        if not (_windows(absolute) or os.path.isabs(absolute)):continue
        module=ntpath if _windows(absolute) else os.path
        try:relative=module.relpath(absolute,os.fspath(Path(path).parent)).replace('\\','/')
        except ValueError:continue
        a=offsets[node.value.lineno-1]+len(lines[node.value.lineno-1].encode()[:node.value.col_offset].decode())
        b=offsets[node.value.end_lineno-1]+len(lines[node.value.end_lineno-1].encode()[:node.value.end_col_offset].decode())
        replacements.append((a,b,repr(relative)))
    for a,b,value in sorted(replacements,reverse=True):text=text[:a]+value+text[b:]
    return text


def portable_phy(path):
    path=Path(path)
    if not path.is_file():return
    original=path.read_text(encoding='utf-8-sig');updated=relative_phy_text(original,path)
    if updated!=original:_atomic_text(path,updated)
