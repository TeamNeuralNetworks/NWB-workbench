from __future__ import annotations
from GUI.project_paths import read_excel as _pp_read_excel

import os
import posixpath
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from GUI.qt_compat import QtCore, QtWidgets, qt_checked, qt_unchecked


EDITABLE_ROOTS = {
    "acquisition",
    "analysis",
    "events",
    "intervals",
    "processing",
    "scratch",
    "stimulus",
    "trials",
    "units",
}
FIXED_NAME_PATHS = {"trials", "units"}


def _path_key(value) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.rstrip("/").lower()


def _prepare_column_for_scalar_assignment(
    dataframe: pd.DataFrame,
    column: str,
) -> None:
    """Make a column safe for values returned by a fresh NWB summary.

    Excel columns containing booleans, dates, or numbers mixed with blank cells
    can be inferred by pandas as an incompatible dtype (commonly float64).
    Inventory refreshes deliberately preserve such mixed values, so use an
    object column before assigning the newly summarized value.
    """
    if column not in dataframe.columns:
        dataframe[column] = pd.Series(None, index=dataframe.index, dtype=object)
    elif not pd.api.types.is_object_dtype(dataframe[column].dtype):
        dataframe[column] = dataframe[column].astype(object)


def _parts(object_path: str) -> list[str]:
    return [part for part in str(object_path or "").strip().strip("/").split("/") if part]


def unit_project_path_segment(project) -> str:
    """Return the inventory-safe path segment used for a sorting project."""
    text = str(project or "").strip()
    text = re.sub(r'[\\/:*?"<>|]', "_", text).strip("._")
    return text or "unknown"


def _is_unit_project_path(object_path: str) -> bool:
    parts = _parts(object_path)
    return len(parts) >= 2 and parts[0].lower() == "units"


def _unit_project_name(object_path: str, sorting_project: str = "") -> str:
    explicit = str(sorting_project or "").strip()
    if explicit:
        return explicit
    parts = _parts(object_path)
    return parts[1].strip() if len(parts) >= 2 and parts[0].lower() == "units" else ""


def canonical_object_path(object_path: str) -> str:
    """Return the real NWB path represented by a Content-tree entry.

    ``units/<sorting project>`` remains a logical path so the manager can edit
    one complete sorting-project group.  The physical NWB container is still
    the canonical ``/units`` table.
    """
    parts = _parts(object_path)
    if parts and parts[0].lower() == "trials":
        return parts[0].lower()
    if parts and parts[0].lower() == "units":
        return "/".join(["units"] + parts[1:])
    return "/".join(parts)


def physical_object_path(object_path: str) -> str:
    """Return the HDF5 path used for dependency and existence checks."""
    return "units" if _is_unit_project_path(object_path) else canonical_object_path(object_path)


def editable_object_reason(object_path: str, action: str | None = None) -> tuple[bool, str]:
    """Return whether a complete NWB container can receive ``action``."""
    parts = _parts(canonical_object_path(object_path))
    if not parts:
        return False, "No NWB object is selected."
    if parts[0] not in EDITABLE_ROOTS:
        return False, f"Objects under {parts[0]} are not supported by this manager yet."
    if action == "rename" and parts[0] == "units" and len(parts) >= 2:
        return True, ""
    if action == "rename" and parts[0] in FIXED_NAME_PATHS:
        return False, (
            f"The canonical /{parts[0]} table has a fixed NWB path and cannot be renamed. "
            "The complete table can still be deleted."
        )
    if parts[0] == "processing" and len(parts) < 3:
        return False, "Select a complete processing container or time series, not a processing module."
    if parts[0] not in {"processing", "trials", "units"} and len(parts) < 2:
        return False, "Only complete top-level NWB objects can be renamed or deleted."
    return True, ""


def renamed_object_path(object_path: str, new_name: str) -> str:
    parts = _parts(canonical_object_path(object_path))
    if not parts:
        return ""
    if parts[0].lower() == "units" and len(parts) >= 2:
        return f"units/{unit_project_path_segment(new_name)}"
    return "/".join(parts[:-1] + [str(new_name)])


def validate_new_name(new_name: str) -> str:
    name = str(new_name or "").strip()
    if not name:
        raise ValueError("The new name cannot be empty.")
    if "/" in name or ":" in name:
        raise ValueError("NWB object names cannot contain '/' or ':'.")
    if "\x00" in name:
        raise ValueError("The new name contains an invalid character.")
    return name


def validate_sorting_project_name(new_name: str) -> str:
    name = str(new_name or "").strip()
    if not name:
        raise ValueError("The sorting project name cannot be empty.")
    if "\x00" in name:
        raise ValueError("The sorting project name contains an invalid character.")
    return name


def _decode_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value or "").strip()


def _units_sorting_project_dataset(h5_file):
    units = h5_file.get("/units")
    if units is None:
        raise KeyError("The canonical /units table is missing.")
    dataset = units.get("sorting_project")
    if dataset is None or not hasattr(dataset, "shape"):
        raise KeyError(
            "The /units table has no sorting_project column, so a sorting-project group cannot be edited."
        )
    if len(dataset.shape) != 1:
        raise RuntimeError("The /units/sorting_project column is not one-dimensional.")
    return dataset


def _unit_project_row_mask(h5_file, sorting_project: str, logical_path: str = "") -> np.ndarray:
    dataset = _units_sorting_project_dataset(h5_file)
    values = [_decode_text(value) for value in dataset[...]]
    project = str(sorting_project or "").strip()
    if project:
        mask = np.asarray([value == project for value in values], dtype=bool)
    else:
        parts = _parts(logical_path)
        segment = parts[1].strip() if len(parts) >= 2 else ""
        mask = np.asarray(
            [unit_project_path_segment(value) == segment for value in values],
            dtype=bool,
        )
    if not mask.any():
        shown = project or (_parts(logical_path)[1] if len(_parts(logical_path)) >= 2 else "")
        raise KeyError(f"No units belong to sorting project '{shown}'.")
    return mask


def _write_sorting_project_name(
    h5_file,
    *,
    logical_path: str,
    sorting_project: str,
    new_name: str,
) -> int:
    dataset = _units_sorting_project_dataset(h5_file)
    mask = _unit_project_row_mask(h5_file, sorting_project, logical_path)
    decoded = [_decode_text(value) for value in dataset[...]]
    old_project = _unit_project_name(logical_path, sorting_project)
    if any(value == new_name and value != old_project for value in decoded):
        raise FileExistsError(f"A sorting project named '{new_name}' already exists in /units.")
    if dataset.dtype.kind == "S" and len(new_name.encode("utf-8")) > int(dataset.dtype.itemsize):
        raise RuntimeError(
            "The sorting_project column uses fixed-length text and the new project name is too long."
        )
    replacement = new_name.encode("utf-8") if dataset.dtype.kind == "S" else new_name
    for row_index in np.flatnonzero(mask):
        dataset[int(row_index)] = replacement
    return int(mask.sum())


def _resize_first_axis(dataset, values):
    values = np.asarray(values)
    new_shape = (int(values.shape[0]),) + tuple(dataset.shape[1:])
    try:
        dataset.resize(new_shape)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot safely resize {dataset.name} while deleting a sorting project. "
            "Delete the complete /units table instead."
        ) from exc
    if values.size:
        dataset[...] = values


def _vector_index_target(h5_file, dataset):
    import h5py

    try:
        target = dataset.attrs.get("target")
    except Exception:
        return None
    if isinstance(target, (h5py.Reference, h5py.RegionReference)) and target:
        try:
            resolved = h5_file[target]
            return resolved if isinstance(resolved, h5py.Dataset) else None
        except Exception:
            return None
    return None


def _delete_unit_project_rows(
    h5_file,
    *,
    logical_path: str,
    sorting_project: str,
) -> tuple[int, bool]:
    """Delete every Units-table row for one project, preserving other projects."""
    import h5py

    units = h5_file.get("/units")
    if units is None:
        raise KeyError("The canonical /units table is missing.")
    id_dataset = units.get("id")
    if id_dataset is None or not getattr(id_dataset, "shape", None):
        raise RuntimeError("The /units table has no valid id column.")
    row_count = int(id_dataset.shape[0])
    remove_mask = _unit_project_row_mask(h5_file, sorting_project, logical_path)
    if int(remove_mask.shape[0]) != row_count:
        raise RuntimeError("The sorting_project and id columns have different row counts.")
    removed_count = int(remove_mask.sum())
    keep_rows = np.flatnonzero(~remove_mask)
    if len(keep_rows) == 0:
        del h5_file["units"]
        return removed_count, True

    direct_datasets = {
        obj.name: obj
        for obj in units.values()
        if isinstance(obj, h5py.Dataset) and getattr(obj, "shape", ())
    }
    processed: set[str] = set()

    # Ragged columns consist of a row-length VectorIndex and a target vector.
    # Rebuild both before ordinary row-aligned columns are shortened.
    for index_dataset in list(direct_datasets.values()):
        if int(index_dataset.shape[0]) != row_count:
            continue
        target_dataset = _vector_index_target(h5_file, index_dataset)
        if target_dataset is None or not getattr(target_dataset, "shape", ()):
            continue
        old_ends = np.asarray(index_dataset[...], dtype=np.int64)
        old_starts = np.concatenate(([0], old_ends[:-1]))
        segments = [target_dataset[int(old_starts[row]):int(old_ends[row])] for row in keep_rows]
        if segments:
            kept_target = np.concatenate(segments, axis=0)
        else:
            kept_target = np.empty((0,) + tuple(target_dataset.shape[1:]), dtype=target_dataset.dtype)
        lengths = np.asarray([int(old_ends[row] - old_starts[row]) for row in keep_rows], dtype=np.int64)
        new_ends = np.cumsum(lengths, dtype=index_dataset.dtype)
        _resize_first_axis(target_dataset, kept_target)
        _resize_first_axis(index_dataset, new_ends)
        processed.update({target_dataset.name, index_dataset.name})

    for dataset in list(direct_datasets.values()):
        if dataset.name in processed or int(dataset.shape[0]) != row_count:
            continue
        _resize_first_axis(dataset, dataset[keep_rows])
        processed.add(dataset.name)

    return removed_count, False


def _absolute_h5_path(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return "/"
    return "/" + text.strip("/")


def _is_path_within(path: str, parent: str) -> bool:
    path = _absolute_h5_path(path)
    parent = _absolute_h5_path(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _object_token(h5_object) -> tuple[str, int]:
    import h5py

    return ("address", int(h5py.h5o.get_info(h5_object.id).addr))


def _iter_h5_objects(h5_file):
    """Yield each hard-linked object once while preserving its first path."""
    import h5py

    stack = [("/", h5_file)]
    visited_groups = set()
    while stack:
        path, group = stack.pop()
        token = _object_token(group)
        if token in visited_groups:
            continue
        visited_groups.add(token)
        yield path, group
        for name in list(group.keys()):
            link = group.get(name, getlink=True)
            if not isinstance(link, h5py.HardLink):
                continue
            try:
                child = group[name]
            except Exception:
                continue
            child_path = posixpath.join(path, str(name))
            if isinstance(child, h5py.Group):
                stack.append((child_path, child))
            else:
                yield child_path, child


def _iter_h5_links(h5_file):
    """Yield every link, including aliases that ``visititems`` would omit."""
    import h5py

    stack = [("/", h5_file)]
    traversed_groups = set()
    while stack:
        group_path, group = stack.pop()
        group_token = _object_token(group)
        if group_token in traversed_groups:
            continue
        traversed_groups.add(group_token)
        for name in list(group.keys()):
            link_path = posixpath.join(group_path, str(name))
            link = group.get(name, getlink=True)
            yield link_path, group, str(name), link
            if isinstance(link, h5py.HardLink):
                try:
                    child = group[name]
                except Exception:
                    continue
                if isinstance(child, h5py.Group):
                    stack.append((link_path, child))


def _target_tokens(h5_file, target_paths: Iterable[str]) -> set[tuple[str, int]]:
    tokens = set()
    for target_path in target_paths:
        absolute = _absolute_h5_path(target_path)
        if absolute.strip("/") not in h5_file:
            continue
        target = h5_file[absolute]
        tokens.add(_object_token(target))
        # Dependencies between NWB containers point to the container itself
        # (for example ``source_timeseries`` or a DynamicTable ``table``
        # link). Do not treat every descendant as owned by the target: linked
        # source containers also appear below their consumer in HDF5 and would
        # otherwise reverse the dependency direction.
    return tokens


def _dtype_has_reference(dtype) -> bool:
    import h5py

    if h5py.check_dtype(ref=dtype) is not None:
        return True
    if dtype.names:
        return any(_dtype_has_reference(dtype.fields[name][0]) for name in dtype.names)
    if dtype.subdtype:
        return _dtype_has_reference(dtype.subdtype[0])
    return False


def _dtype_has_string(dtype) -> bool:
    import h5py

    if dtype.kind in {"S", "U"} or h5py.check_dtype(vlen=dtype) in {str, bytes}:
        return True
    if dtype.names:
        return any(_dtype_has_string(dtype.fields[name][0]) for name in dtype.names)
    if dtype.subdtype:
        return _dtype_has_string(dtype.subdtype[0])
    return False


def _iter_array_values(value):
    if isinstance(value, np.ndarray):
        for item in value.flat:
            yield from _iter_array_values(item)
    elif isinstance(value, np.void) and value.dtype.names:
        for name in value.dtype.names:
            yield from _iter_array_values(value[name])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_array_values(item)
    else:
        yield value


def _iter_dataset_chunks(dataset, max_values: int = 16384):
    if dataset.shape == ():
        yield (), dataset[()]
        return
    if not dataset.shape or int(np.prod(dataset.shape, dtype=np.int64)) == 0:
        return
    values_per_row = max(1, int(np.prod(dataset.shape[1:], dtype=np.int64)))
    step = max(1, int(max_values // values_per_row))
    for start in range(0, int(dataset.shape[0]), step):
        selection = (slice(start, min(int(dataset.shape[0]), start + step)),) + tuple(
            slice(None) for _ in dataset.shape[1:]
        )
        yield selection, dataset[selection]


def _reference_hits_target(h5_file, value, tokens: set[tuple[str, int]]) -> bool:
    import h5py

    for item in _iter_array_values(value):
        if not isinstance(item, (h5py.Reference, h5py.RegionReference)) or not item:
            continue
        try:
            if _object_token(h5_file[item]) in tokens:
                return True
        except Exception:
            continue
    return False


def _path_text_matches(value, target_paths: Iterable[str]) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            return False
    if not isinstance(value, str):
        return False
    text = value.strip().replace("\\", "/")
    for target_path in target_paths:
        absolute = _absolute_h5_path(target_path)
        relative = absolute.strip("/")
        if text == absolute or text.startswith(absolute + "/"):
            return True
        if text == relative or text.startswith(relative + "/"):
            return True
    return False


def _value_has_path(value, target_paths: Iterable[str]) -> bool:
    return any(_path_text_matches(item, target_paths) for item in _iter_array_values(value))


def _managed_owner_path(reference_location: str) -> str | None:
    """Map a low-level reference to the complete data object that owns it."""
    location = str(reference_location).split("@", 1)[0]
    parts = _parts(location)
    if not parts:
        return None
    root = parts[0]
    if root == "processing" and len(parts) >= 3:
        return "/" + "/".join(parts[:3])
    if root in {"acquisition", "analysis", "events", "intervals", "scratch", "stimulus"} and len(parts) >= 2:
        return "/" + "/".join(parts[:2])
    if root in FIXED_NAME_PATHS:
        return "/" + root
    return None


def _resolved_soft_path(parent_path: str, link_path: str) -> str:
    if str(link_path).startswith("/"):
        return _absolute_h5_path(link_path)
    return _absolute_h5_path(posixpath.normpath(posixpath.join(parent_path, str(link_path))))


def _scan_incoming_references(h5_file, target_paths: Iterable[str]) -> list[dict]:
    import h5py

    target_paths = [_absolute_h5_path(path) for path in target_paths]
    tokens = _target_tokens(h5_file, target_paths)
    records: list[dict] = []
    seen = set()

    def _add(location: str, kind: str):
        if any(_is_path_within(location.split("@", 1)[0], target) for target in target_paths):
            return
        owner = _managed_owner_path(location)
        key = (str(location), str(kind), str(owner or ""))
        if key not in seen:
            seen.add(key)
            records.append({"location": str(location), "kind": str(kind), "owner_path": owner})

    for link_location, parent, name, link in _iter_h5_links(h5_file):
        if isinstance(link, h5py.HardLink):
            try:
                if _object_token(parent[name]) in tokens:
                    _add(link_location, "NWB object link")
            except Exception:
                pass
        elif isinstance(link, h5py.SoftLink):
            resolved = _resolved_soft_path(posixpath.dirname(link_location), link.path)
            if any(_is_path_within(resolved, target) for target in target_paths):
                _add(link_location, "soft path link")
        elif isinstance(link, h5py.ExternalLink):
            same_file = str(link.filename or "") in {"", ".", Path(h5_file.filename).name, str(h5_file.filename)}
            if same_file and any(_is_path_within(link.path, target) for target in target_paths):
                _add(link_location, "external path link")

    for object_path, h5_object in _iter_h5_objects(h5_file):
        for attr_name in list(h5_object.attrs.keys()):
            try:
                value = h5_object.attrs[attr_name]
            except Exception:
                continue
            location = f"{object_path}@{attr_name}"
            if _reference_hits_target(h5_file, value, tokens):
                _add(location, "object or region reference")
            if _value_has_path(value, target_paths):
                _add(location, "stored NWB path")

        if not isinstance(h5_object, h5py.Dataset):
            continue
        check_refs = _dtype_has_reference(h5_object.dtype)
        check_strings = _dtype_has_string(h5_object.dtype)
        if not check_refs and not check_strings:
            continue
        for _selection, values in _iter_dataset_chunks(h5_object):
            if check_refs and _reference_hits_target(h5_file, values, tokens):
                _add(object_path, "object or region reference")
                check_refs = False
            if check_strings and _value_has_path(values, target_paths):
                _add(object_path, "stored NWB path")
                check_strings = False
            if not check_refs and not check_strings:
                break
    return records


def _dependency_summary(records: Iterable[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for record in records:
        owner = record.get("owner_path")
        if not owner:
            continue
        entry = grouped.setdefault(owner, {"object_path": owner, "reference_count": 0, "kinds": set()})
        entry["reference_count"] += 1
        entry["kinds"].add(str(record.get("kind", "reference")))
    return [
        {
            "object_path": key,
            "reference_count": int(value["reference_count"]),
            "kinds": sorted(value["kinds"]),
        }
        for key, value in sorted(grouped.items())
    ]


def inspect_nwb_object_dependencies(
    file_paths: Iterable[str],
    object_path: str,
    *,
    action: str,
    new_name: str = "",
    sorting_project: str = "",
) -> list[dict]:
    """Inspect all incoming dependencies before showing a destructive popup."""
    import h5py

    action = str(action).strip().lower()
    if action not in {"rename", "delete"}:
        raise ValueError(f"Unsupported action: {action}")
    canonical_path = canonical_object_path(object_path)
    unit_project_edit = _is_unit_project_path(canonical_path)
    selected_project = _unit_project_name(canonical_path, sorting_project)
    supported, reason = editable_object_reason(canonical_path, action)
    if not supported:
        raise ValueError(reason)
    if action == "rename":
        new_name = (
            validate_sorting_project_name(new_name)
            if unit_project_edit
            else validate_new_name(new_name)
        )

    reports = []
    for value in dict.fromkeys(str(path) for path in file_paths):
        source_path = Path(value)
        if not source_path.exists():
            raise FileNotFoundError(f"NWB file does not exist: {source_path}")
        with h5py.File(str(source_path), mode="r") as h5_file:
            target = _absolute_h5_path(physical_object_path(canonical_path))
            if target.strip("/") not in h5_file:
                raise KeyError(f"Object not found in {source_path}: {canonical_path}")
            if unit_project_edit:
                project_mask = _unit_project_row_mask(h5_file, selected_project, canonical_path)
                if action == "rename":
                    values = [_decode_text(value) for value in _units_sorting_project_dataset(h5_file)[...]]
                    current_values = {
                        values[index]
                        for index in np.flatnonzero(~project_mask)
                    }
                    if new_name in current_values:
                        raise FileExistsError(
                            f"Cannot rename sorting project '{selected_project}' in {source_path}: "
                            f"'{new_name}' already exists."
                        )
            elif action == "rename":
                destination = _absolute_h5_path(renamed_object_path(canonical_path, new_name))
                if destination.strip("/") in h5_file:
                    raise FileExistsError(
                        f"Cannot rename {canonical_path} in {source_path}: {destination} already exists."
                    )

            scan_paths = [target]
            if unit_project_edit:
                scan_paths.append(_absolute_h5_path(canonical_path))
            delete_paths = [target]
            all_records: list[dict] = []
            if action == "delete":
                while True:
                    records = _scan_incoming_references(h5_file, scan_paths + delete_paths[1:])
                    new_owners = []
                    for record in records:
                        owner = record.get("owner_path")
                        if owner and not any(_is_path_within(owner, path) for path in delete_paths):
                            new_owners.append(owner)
                    all_records.extend(records)
                    if not new_owners:
                        break
                    delete_paths.extend(sorted(set(new_owners)))
            else:
                all_records = _scan_incoming_references(h5_file, scan_paths)

            protected = [record for record in all_records if not record.get("owner_path")]
            dependencies = _dependency_summary(all_records)
            reported_delete_paths = delete_paths
            if unit_project_edit:
                reported_delete_paths = [
                    path for path in delete_paths if not _is_path_within(path, target)
                ]
            reports.append(
                {
                    "file_path": str(source_path),
                    "object_path": canonical_path,
                    "physical_object_path": physical_object_path(canonical_path),
                    "sorting_project": selected_project,
                    "dependencies": dependencies,
                    "protected_dependencies": protected,
                    "delete_paths": sorted(set(path.strip("/") for path in reported_delete_paths)),
                    "reference_count": len(all_records),
                    "baseline_broken_links": _find_broken_links(h5_file),
                }
            )
    return reports


def _replace_path_text(value, old_path: str, new_path: str):
    was_bytes = isinstance(value, bytes)
    if was_bytes:
        try:
            text = value.decode("utf-8")
        except Exception:
            return value, False
    elif isinstance(value, str):
        text = value
    else:
        return value, False

    slash_style = "\\" if "\\" in text and "/" not in text else "/"
    normalized = text.replace("\\", "/")
    old_absolute = _absolute_h5_path(old_path)
    new_absolute = _absolute_h5_path(new_path)
    old_relative = old_absolute.strip("/")
    new_relative = new_absolute.strip("/")
    if normalized == old_absolute or normalized.startswith(old_absolute + "/"):
        updated = new_absolute + normalized[len(old_absolute):]
    elif normalized == old_relative or normalized.startswith(old_relative + "/"):
        updated = new_relative + normalized[len(old_relative):]
    else:
        return value, False
    if slash_style == "\\":
        updated = updated.replace("/", "\\")
    if was_bytes:
        updated = updated.encode("utf-8")
    return updated, True


def _replace_paths_in_value(value, old_path: str, new_path: str):
    if isinstance(value, np.ndarray):
        copied = value.copy()
        changed = False
        for index in np.ndindex(copied.shape):
            updated, item_changed = _replace_paths_in_value(copied[index], old_path, new_path)
            if item_changed:
                copied[index] = updated
                changed = True
        return copied, changed
    if isinstance(value, np.void) and value.dtype.names:
        copied = value.copy()
        changed = False
        for name in value.dtype.names:
            updated, item_changed = _replace_paths_in_value(copied[name], old_path, new_path)
            if item_changed:
                copied[name] = updated
                changed = True
        return copied, changed
    if isinstance(value, list):
        output = []
        changed = False
        for item in value:
            updated, item_changed = _replace_paths_in_value(item, old_path, new_path)
            output.append(updated)
            changed = changed or item_changed
        return output, changed
    if isinstance(value, tuple):
        output, changed = _replace_paths_in_value(list(value), old_path, new_path)
        return tuple(output), changed
    return _replace_path_text(value, old_path, new_path)


def _rewrite_soft_links(h5_file, old_path: str, new_path: str) -> int:
    import h5py

    updates = []
    for link_location, parent, name, link in _iter_h5_links(h5_file):
        if isinstance(link, h5py.SoftLink):
            resolved = _resolved_soft_path(posixpath.dirname(link_location), link.path)
            updated, changed = _replace_path_text(resolved, old_path, new_path)
            if changed:
                updates.append((parent, name, h5py.SoftLink(str(updated))))
        elif isinstance(link, h5py.ExternalLink):
            same_file = str(link.filename or "") in {"", ".", Path(h5_file.filename).name, str(h5_file.filename)}
            updated, changed = _replace_path_text(link.path, old_path, new_path)
            if same_file and changed:
                updates.append((parent, name, h5py.ExternalLink(link.filename, str(updated))))
    for parent, name, replacement in updates:
        del parent[name]
        parent[name] = replacement
    return len(updates)


def _rewrite_stored_paths(h5_file, old_path: str, new_path: str) -> int:
    import h5py

    update_count = 0
    for _object_path, h5_object in _iter_h5_objects(h5_file):
        for attr_name in list(h5_object.attrs.keys()):
            value = h5_object.attrs[attr_name]
            updated, changed = _replace_paths_in_value(value, old_path, new_path)
            if changed:
                h5_object.attrs[attr_name] = updated
                update_count += 1

        if not isinstance(h5_object, h5py.Dataset) or not _dtype_has_string(h5_object.dtype):
            continue
        for selection, values in _iter_dataset_chunks(h5_object):
            updated, changed = _replace_paths_in_value(values, old_path, new_path)
            if not changed:
                continue
            if h5_object.dtype.kind == "S":
                maximum = int(h5_object.dtype.itemsize)
                for item in _iter_array_values(updated):
                    if isinstance(item, bytes) and len(item) > maximum:
                        raise RuntimeError(
                            f"Cannot update a fixed-length stored path in {h5_object.name}; "
                            "the new name is longer than the available field."
                        )
            h5_object[selection] = updated
            update_count += 1
    return update_count


def _remove_empty_processing_modules(h5_file):
    processing = h5_file.get("/processing")
    if processing is None:
        return
    for module_name in list(processing.keys()):
        try:
            module = processing[module_name]
            if len(module.keys()) == 0:
                del processing[module_name]
        except Exception:
            continue


def _reduced_delete_paths(paths: Iterable[str]) -> list[str]:
    ordered = sorted({_absolute_h5_path(path) for path in paths}, key=lambda value: (value.count("/"), value))
    reduced = []
    for path in ordered:
        if not any(_is_path_within(path, parent) for parent in reduced):
            reduced.append(path)
    return reduced


def _find_broken_links(h5_file) -> list[str]:
    import h5py

    broken = []
    for link_location, parent, name, link in _iter_h5_links(h5_file):
        if isinstance(link, h5py.HardLink):
            continue
        try:
            parent[name]
        except Exception:
            broken.append(link_location)
    return sorted(set(broken))


def _validate_nwb_file(
    export_path: Path,
    *,
    expected_path: str = "",
    removed_paths: Iterable[str] = (),
    allowed_broken_links: Iterable[str] = (),
):
    import h5py
    from pynwb import NWBHDF5IO

    with h5py.File(str(export_path), mode="r") as h5_file:
        if expected_path and _absolute_h5_path(expected_path).strip("/") not in h5_file:
            raise RuntimeError(f"Validation failed: {expected_path} is missing from the edited file.")
        for removed_path in removed_paths:
            if _absolute_h5_path(removed_path).strip("/") in h5_file:
                raise RuntimeError(f"Validation failed: {removed_path} is still present in the edited file.")
        allowed = set(str(path) for path in allowed_broken_links)
        broken_links = [path for path in _find_broken_links(h5_file) if path not in allowed]
        if broken_links:
            preview = "\n".join(broken_links[:20])
            more = "" if len(broken_links) <= 20 else f"\n... and {len(broken_links) - 20} more"
            raise RuntimeError(f"The edit created or retained broken HDF5 links:\n{preview}{more}")

    with NWBHDF5IO(str(export_path), mode="r", load_namespaces=True) as check_io:
        check_io.read()

    try:
        import pynwb

        try:
            validation_result = pynwb.validate(path=str(export_path))
        except TypeError:
            validation_result = pynwb.validate(paths=[str(export_path)])
        if isinstance(validation_result, tuple):
            validation_result = validation_result[0]
        errors = list(validation_result or [])
        if errors:
            preview = "\n".join(str(error) for error in errors[:10])
            more = "" if len(errors) <= 10 else f"\n... and {len(errors) - 10} more error(s)"
            raise RuntimeError(f"NWB schema validation failed:\n{preview}{more}")
    except (AttributeError, ImportError):
        pass


def _export_edited_file(
    source_path: Path,
    object_path: str,
    action: str,
    new_name: str = "",
    sorting_project: str = "",
    dependency_report: dict | None = None,
) -> tuple[Path, dict]:
    import h5py

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{source_path.stem}_content_edit_",
        # Keep the work file beside the source so installation remains an
        # atomic same-filesystem replace, but do not give it a final .nwb
        # suffix that an inventory scan could discover.
        suffix=f"{source_path.suffix or '.nwb'}.tmp",
        dir=str(source_path.parent),
    )
    os.close(fd)
    temporary_path = Path(temporary_name)

    try:
        shutil.copy2(source_path, temporary_path)
        object_path = canonical_object_path(object_path)
        unit_project_edit = _is_unit_project_path(object_path)
        selected_project = _unit_project_name(object_path, sorting_project)
        edit_details = {
            "updated_links": 0,
            "updated_stored_paths": 0,
            "deleted_paths": [],
            "renamed_unit_rows": 0,
            "deleted_unit_rows": 0,
        }

        if action == "rename" and unit_project_edit:
            new_path = renamed_object_path(object_path, new_name)
            with h5py.File(str(temporary_path), mode="r+") as h5_file:
                edit_details["renamed_unit_rows"] = _write_sorting_project_name(
                    h5_file,
                    logical_path=object_path,
                    sorting_project=selected_project,
                    new_name=new_name,
                )
                edit_details["updated_stored_paths"] = _rewrite_stored_paths(
                    h5_file,
                    object_path,
                    new_path,
                )
            _validate_nwb_file(
                temporary_path,
                expected_path="units",
                allowed_broken_links=(dependency_report or {}).get("baseline_broken_links", []),
            )
        elif action == "rename":
            new_path = renamed_object_path(object_path, new_name)
            with h5py.File(str(temporary_path), mode="r+") as h5_file:
                old_h5_path = _absolute_h5_path(object_path).strip("/")
                new_h5_path = _absolute_h5_path(new_path).strip("/")
                if old_h5_path not in h5_file:
                    raise KeyError(f"Object not found in exported file: {object_path}")
                if new_h5_path in h5_file:
                    raise FileExistsError(f"An object named '{new_name}' already exists in this location.")
                h5_file.move(old_h5_path, new_h5_path)
                edit_details["updated_links"] = _rewrite_soft_links(h5_file, object_path, new_path)
                edit_details["updated_stored_paths"] = _rewrite_stored_paths(h5_file, object_path, new_path)
            _validate_nwb_file(
                temporary_path,
                expected_path=new_path,
                removed_paths=[object_path],
                allowed_broken_links=(dependency_report or {}).get("baseline_broken_links", []),
            )
        else:
            report = dependency_report or {}
            default_delete_paths = [] if unit_project_edit else [object_path]
            delete_paths = _reduced_delete_paths(report.get("delete_paths", default_delete_paths))
            whole_units_removed = False
            with h5py.File(str(temporary_path), mode="r+") as h5_file:
                for delete_path in sorted(delete_paths, key=lambda value: value.count("/"), reverse=True):
                    key = delete_path.strip("/")
                    if key in h5_file:
                        del h5_file[key]
                if unit_project_edit:
                    edit_details["deleted_unit_rows"], whole_units_removed = _delete_unit_project_rows(
                        h5_file,
                        logical_path=object_path,
                        sorting_project=selected_project,
                    )
                _remove_empty_processing_modules(h5_file)
            removed_paths = [path.strip("/") for path in delete_paths]
            if whole_units_removed:
                removed_paths.append("units")
            edit_details["deleted_paths"] = removed_paths
            _validate_nwb_file(
                temporary_path,
                expected_path="" if whole_units_removed else ("units" if unit_project_edit else ""),
                removed_paths=removed_paths,
                allowed_broken_links=report.get("baseline_broken_links", []),
            )

        return temporary_path, edit_details
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _dependency_plan_key(report: dict) -> tuple:
    dependencies = tuple(sorted(str(item.get("object_path", "")) for item in report.get("dependencies", [])))
    protected = tuple(
        sorted((str(item.get("location", "")), str(item.get("kind", ""))) for item in report.get("protected_dependencies", []))
    )
    delete_paths = tuple(sorted(str(path).strip("/") for path in report.get("delete_paths", [])))
    broken_links = tuple(sorted(str(path) for path in report.get("baseline_broken_links", [])))
    return (
        str(report.get("object_path", "")),
        str(report.get("sorting_project", "")),
        dependencies,
        protected,
        delete_paths,
        broken_links,
    )


def _allocate_backup_path(source_path: Path) -> Path:
    fd, backup_name = tempfile.mkstemp(
        prefix=f".{source_path.stem}_content_backup_",
        suffix=f"{source_path.suffix or '.nwb'}.bak",
        dir=str(source_path.parent),
    )
    os.close(fd)
    backup_path = Path(backup_name)
    backup_path.unlink()
    return backup_path


def _replace_with_backup(source_path: Path, edited_path: Path) -> Path:
    backup_path = _allocate_backup_path(source_path)
    os.replace(source_path, backup_path)
    try:
        os.replace(edited_path, source_path)
    except Exception:
        os.replace(backup_path, source_path)
        raise
    return backup_path


def _write_refreshed_inventory(inventory_path: Path, modified_paths: Iterable[Path]):
    from Builders.General.generate_nwb_inventory import (
        build_trial_filter_catalog,
        remove_internal_nwb_work_rows,
        summarize_nwb,
    )

    sheets = _pp_read_excel(inventory_path, sheet_name=None) if inventory_path.exists() else {}
    inventory_df = sheets.get("inventory", pd.DataFrame()).copy()
    content_df = sheets.get("content_index", pd.DataFrame()).copy()
    trial_index_df = sheets.get("trial_filter_index", pd.DataFrame()).copy()

    # Remove legacy work-file rows that may have been captured by a scan made
    # with an older application version.  This protects both the displayed
    # summary and every downstream analysis file list.
    inventory_df = remove_internal_nwb_work_rows(inventory_df)
    content_df = remove_internal_nwb_work_rows(content_df)
    trial_index_df = remove_internal_nwb_work_rows(trial_index_df)

    for source_path in modified_paths:
        inventory_row, content_rows, trial_rows = summarize_nwb(source_path)
        if not bool(inventory_row.get("nwb_readable", False)):
            raise RuntimeError(
                f"The edited file could not be summarized: {source_path}\n"
                f"{inventory_row.get('read_error', 'Unknown read error')}"
            )

        source_key = _path_key(source_path)
        if "file_path" not in inventory_df.columns:
            inventory_df["file_path"] = None
        inventory_mask = inventory_df["file_path"].map(_path_key) == source_key
        if inventory_mask.any():
            for column, value in inventory_row.items():
                _prepare_column_for_scalar_assignment(inventory_df, column)
                for row_index in inventory_df.index[inventory_mask]:
                    inventory_df.at[row_index, column] = value
        else:
            inventory_df = pd.concat([inventory_df, pd.DataFrame([inventory_row])], ignore_index=True)

        if "file_path" not in content_df.columns:
            content_df["file_path"] = None
        content_df = content_df.loc[content_df["file_path"].map(_path_key) != source_key].copy()
        if content_rows:
            content_df = pd.concat([content_df, pd.DataFrame(content_rows)], ignore_index=True)

        if "file_path" not in trial_index_df.columns:
            trial_index_df["file_path"] = None
        trial_index_df = trial_index_df.loc[
            trial_index_df["file_path"].map(_path_key) != source_key
        ].copy()
        if trial_rows:
            trial_index_df = pd.concat([trial_index_df, pd.DataFrame(trial_rows)], ignore_index=True)

    content_df = content_df.drop_duplicates().reset_index(drop=True)
    trial_index_df = trial_index_df.drop_duplicates().reset_index(drop=True)
    sheets["inventory"] = inventory_df.reset_index(drop=True)
    sheets["content_index"] = content_df
    sheets["trial_filter_index"] = trial_index_df
    sheets["trial_filter_catalog"] = build_trial_filter_catalog(trial_index_df)

    meta_df = sheets.get("meta", pd.DataFrame()).copy()
    if meta_df.empty:
        meta_df = pd.DataFrame([{"updated_at": datetime.now(), "schema_version": "2.5"}])
    else:
        _prepare_column_for_scalar_assignment(meta_df, "updated_at")
        meta_df.loc[0, "updated_at"] = datetime.now()
    sheets["meta"] = meta_df

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{inventory_path.stem}_content_edit_",
        suffix=inventory_path.suffix or ".xlsx",
        dir=str(inventory_path.parent),
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with pd.ExcelWriter(temporary_path, engine="openpyxl", mode="w") as writer:
            preferred = [
                "inventory",
                "content_index",
                "trial_filter_index",
                "trial_filter_catalog",
                "meta",
            ]
            for sheet_name in preferred:
                if sheet_name in sheets:
                    sheets[sheet_name].to_excel(writer, sheet_name=sheet_name, index=False)
            for sheet_name, dataframe in sheets.items():
                if sheet_name not in preferred:
                    dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
        os.replace(temporary_path, inventory_path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def edit_nwb_objects_batch(
    *,
    file_paths: Iterable[str],
    object_path: str,
    action: str,
    inventory_path: str | Path,
    new_name: str = "",
    sorting_project: str = "",
    dependency_reports: Iterable[dict] | None = None,
    progress: Callable[[int, str, str], None] | None = None,
) -> dict:
    """Edit a set of NWB files as one rollback-capable batch."""
    action = str(action).strip().lower()
    if action not in {"rename", "delete"}:
        raise ValueError(f"Unsupported action: {action}")
    object_path = canonical_object_path(object_path)
    unit_project_edit = _is_unit_project_path(object_path)
    selected_project = _unit_project_name(object_path, sorting_project)
    supported, reason = editable_object_reason(object_path, action)
    if not supported:
        raise ValueError(reason)
    if action == "rename":
        new_name = (
            validate_sorting_project_name(new_name)
            if unit_project_edit
            else validate_new_name(new_name)
        )
        current_name = selected_project if unit_project_edit else _parts(object_path)[-1]
        if new_name == current_name:
            raise ValueError("The new name is identical to the current name.")

    paths = [Path(str(path)) for path in dict.fromkeys(str(path) for path in file_paths)]
    if not paths:
        raise ValueError("No NWB files were selected.")

    fresh_reports = inspect_nwb_object_dependencies(
        [str(path) for path in paths],
        object_path,
        action=action,
        new_name=new_name,
        sorting_project=selected_project,
    )
    expected_by_path = {
        _path_key(report.get("file_path")): report for report in list(dependency_reports or [])
    }
    for report in fresh_reports:
        expected = expected_by_path.get(_path_key(report.get("file_path")))
        if expected is not None and _dependency_plan_key(expected) != _dependency_plan_key(report):
            raise RuntimeError(
                f"Dependencies changed after confirmation for {report.get('file_path')}. "
                "No file was modified; inspect the object again and retry."
            )
        if action == "delete" and report.get("protected_dependencies"):
            locations = "\n".join(
                f"  {item.get('location')} ({item.get('kind')})"
                for item in report.get("protected_dependencies", [])[:20]
            )
            raise RuntimeError(
                "The target has dependencies in protected NWB metadata that cannot be removed safely:\n"
                + locations
            )
    report_by_path = {_path_key(report.get("file_path")): report for report in fresh_reports}

    backups: dict[Path, Path] = {}
    edited_temporaries: set[Path] = set()
    per_file_details: list[dict] = []
    try:
        total = len(paths)
        for index, source_path in enumerate(paths, start=1):
            if progress is not None:
                progress(index - 1, str(source_path), "Copying and editing temporary NWB file")
            report = report_by_path[_path_key(source_path)]
            edited_path, edit_details = _export_edited_file(
                source_path,
                object_path,
                action,
                new_name,
                selected_project,
                dependency_report=report,
            )
            edited_temporaries.add(edited_path)
            per_file_details.append(
                {
                    "file_path": str(source_path),
                    "dependencies": list(report.get("dependencies", [])),
                    **edit_details,
                }
            )
            if progress is not None:
                progress(index - 1, str(source_path), "Installing validated NWB file")
            backup_path = _replace_with_backup(source_path, edited_path)
            edited_temporaries.discard(edited_path)
            backups[source_path] = backup_path
            if progress is not None:
                progress(index, str(source_path), "Done")

        if progress is not None:
            progress(total, "", "Updating NWB inventory")
        _write_refreshed_inventory(Path(inventory_path), paths)

        backup_warnings = []
        for backup_path in backups.values():
            try:
                backup_path.unlink(missing_ok=True)
            except Exception as exc:
                backup_warnings.append(f"Could not remove backup {backup_path}: {exc}")

        return {
            "action": action,
            "object_path": object_path,
            "new_object_path": renamed_object_path(object_path, new_name) if action == "rename" else "",
            "sorting_project": new_name if action == "rename" and unit_project_edit else selected_project,
            "files": [str(path) for path in paths],
            "details": per_file_details,
            "warnings": backup_warnings,
        }
    except Exception:
        rollback_errors = []
        for source_path, backup_path in reversed(list(backups.items())):
            try:
                if backup_path.exists():
                    os.replace(backup_path, source_path)
            except Exception as rollback_exc:
                rollback_errors.append(f"{source_path}: {rollback_exc}")
        for temporary_path in edited_temporaries:
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass
        if rollback_errors:
            raise RuntimeError(
                "The edit failed and at least one original file could not be restored:\n"
                + "\n".join(rollback_errors)
            )
        raise


_qt_slot = getattr(QtCore, "Slot", getattr(QtCore, "pyqtSlot", None))


class _ObjectEditWorker(QtCore.QObject):
    progress = QtCore.Signal(int, str, str)
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, request: dict):
        super().__init__()
        self.request = dict(request)

    @_qt_slot()
    def run(self):
        try:
            result = edit_nwb_objects_batch(
                **self.request,
                progress=lambda value, path, status: self.progress.emit(value, path, status),
            )
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class _DependencyInspectWorker(QtCore.QObject):
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()

    def __init__(self, request: dict):
        super().__init__()
        self.request = dict(request)

    @_qt_slot()
    def run(self):
        try:
            reports = inspect_nwb_object_dependencies(**self.request)
            self.succeeded.emit(reports)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class ContentObjectManager(QtWidgets.QWidget):
    """Right-panel editor for one indexed NWB object across selected files."""

    operation_finished = QtCore.Signal(object)
    plot_requested = QtCore.Signal(str, str)
    runningStateChanged = QtCore.Signal(bool, str)

    def __init__(self, *, GUI_instance, parent=None):
        super().__init__(parent)
        self.GUI_instance = GUI_instance
        self.object_path = ""
        self.sorting_project = ""
        self.neurodata_type = ""
        self.plot_capable = False
        self._thread = None
        self._worker = None
        self._busy = False
        self._active_action = ""
        self._pending_paths: list[str] = []
        self._pending_new_name = ""
        self._dependency_reports: list[dict] | None = None
        self._dependency_error = ""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title_label = QtWidgets.QLabel("NWB object manager", self)
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        layout.addWidget(self.title_label)

        self.path_label = QtWidgets.QLabel("No object selected", self)
        selectable_text = (
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            if hasattr(QtCore.Qt, "TextInteractionFlag")
            else QtCore.Qt.TextSelectableByMouse
        )
        self.path_label.setTextInteractionFlags(selectable_text)
        layout.addWidget(self.path_label)

        self.info_label = QtWidgets.QLabel(
            "Select the files on which the action should be applied. Each file is exported, checked, and replaced safely.",
            self,
        )
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        selection_row = QtWidgets.QHBoxLayout()
        self.select_all_button = QtWidgets.QPushButton("Select all", self)
        self.unselect_all_button = QtWidgets.QPushButton("Unselect all", self)
        self.invert_button = QtWidgets.QPushButton("Invert selection", self)
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.unselect_all_button)
        selection_row.addWidget(self.invert_button)
        selection_row.addStretch(1)
        layout.addLayout(selection_row)

        self.file_table = QtWidgets.QTableWidget(self)
        self.file_table.setColumnCount(3)
        self.file_table.setHorizontalHeaderLabels(["Use", "NWB file", "Status"])
        self.file_table.verticalHeader().setVisible(False)
        select_rows = (
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
            if hasattr(QtWidgets.QAbstractItemView, "SelectionBehavior")
            else QtWidgets.QAbstractItemView.SelectRows
        )
        no_edit = (
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
            if hasattr(QtWidgets.QAbstractItemView, "EditTrigger")
            else QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.file_table.setSelectionBehavior(select_rows)
        self.file_table.setEditTriggers(no_edit)
        header = self.file_table.horizontalHeader()
        header.setStretchLastSection(False)
        resize_to_contents = (
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            if hasattr(QtWidgets.QHeaderView, "ResizeMode")
            else QtWidgets.QHeaderView.ResizeToContents
        )
        stretch = (
            QtWidgets.QHeaderView.ResizeMode.Stretch
            if hasattr(QtWidgets.QHeaderView, "ResizeMode")
            else QtWidgets.QHeaderView.Stretch
        )
        header.setSectionResizeMode(0, resize_to_contents)
        header.setSectionResizeMode(1, stretch)
        header.setSectionResizeMode(2, resize_to_contents)
        layout.addWidget(self.file_table, stretch=1)

        rename_row = QtWidgets.QHBoxLayout()
        self.rename_label = QtWidgets.QLabel("New name:", self)
        rename_row.addWidget(self.rename_label)
        self.new_name_edit = QtWidgets.QLineEdit(self)
        self.new_name_edit.setPlaceholderText("Enter the new NWB object name")
        self.rename_button = QtWidgets.QPushButton("Rename selected", self)
        rename_row.addWidget(self.new_name_edit, stretch=1)
        rename_row.addWidget(self.rename_button)
        layout.addLayout(rename_row)

        action_row = QtWidgets.QHBoxLayout()
        self.plot_button = QtWidgets.QPushButton("Plot selected file", self)
        self.delete_button = QtWidgets.QPushButton("Delete selected", self)
        action_row.addWidget(self.plot_button)
        action_row.addStretch(1)
        action_row.addWidget(self.delete_button)
        layout.addLayout(action_row)

        self.progress_bar = QtWidgets.QProgressBar(self)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = QtWidgets.QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.select_all_button.clicked.connect(lambda _checked=False: self._set_all_checked(True))
        self.unselect_all_button.clicked.connect(lambda _checked=False: self._set_all_checked(False))
        self.invert_button.clicked.connect(lambda _checked=False: self._invert_checked())
        self.rename_button.clicked.connect(lambda _checked=False: self._start_action("rename"))
        self.delete_button.clicked.connect(lambda _checked=False: self._start_action("delete"))
        self.plot_button.clicked.connect(lambda _checked=False: self._request_plot())
        self.file_table.itemChanged.connect(lambda *_args: self._update_action_state())
        self.new_name_edit.textChanged.connect(lambda *_args: self._update_action_state())

        self._update_action_state()

    def set_object(
        self,
        *,
        object_path: str,
        file_paths: Iterable[str],
        neurodata_type: str = "",
        plot_capable: bool = False,
        sorting_project: str = "",
    ):
        if self.is_busy():
            return False
        self.object_path = canonical_object_path(object_path)
        self.sorting_project = _unit_project_name(self.object_path, sorting_project)
        self.neurodata_type = str(neurodata_type or "")
        self.plot_capable = bool(plot_capable)
        paths = list(dict.fromkeys(str(path) for path in file_paths if str(path).strip()))
        self.title_label.setText(self.neurodata_type or "NWB object manager")
        if _is_unit_project_path(self.object_path):
            self.path_label.setText(f"/units | sorting project: {self.sorting_project}")
            self.rename_label.setText("New sorting project name:")
            self.new_name_edit.setPlaceholderText("Enter the new sorting project name")
        else:
            self.path_label.setText(self.object_path or "No object selected")
            self.rename_label.setText("New name:")
            self.new_name_edit.setPlaceholderText("Enter the new NWB object name")
        self.status_label.clear()
        current_name = (
            self.sorting_project
            if _is_unit_project_path(self.object_path)
            else (_parts(self.object_path)[-1] if _parts(self.object_path) else "")
        )
        self.new_name_edit.setText(current_name)

        self.file_table.blockSignals(True)
        self.file_table.setRowCount(len(paths))
        try:
            checkable = QtCore.Qt.ItemFlag.ItemIsUserCheckable
            enabled = QtCore.Qt.ItemFlag.ItemIsEnabled
        except AttributeError:
            checkable = QtCore.Qt.ItemIsUserCheckable
            enabled = QtCore.Qt.ItemIsEnabled
        for row, path in enumerate(paths):
            use_item = QtWidgets.QTableWidgetItem("")
            use_item.setFlags(enabled | checkable)
            use_item.setCheckState(qt_checked())
            path_item = QtWidgets.QTableWidgetItem(path)
            status_item = QtWidgets.QTableWidgetItem("Ready")
            self.file_table.setItem(row, 0, use_item)
            self.file_table.setItem(row, 1, path_item)
            self.file_table.setItem(row, 2, status_item)
        self.file_table.blockSignals(False)

        rename_supported, rename_reason = editable_object_reason(self.object_path, "rename")
        delete_supported, delete_reason = editable_object_reason(self.object_path, "delete")
        if rename_supported and delete_supported:
            if _is_unit_project_path(self.object_path):
                self.info_label.setText(
                    "Rename changes the sorting_project value for every unit in this project. Delete removes "
                    "every unit belonging to this project while preserving other sorting projects. Dependencies "
                    "are inspected before either action."
                )
            else:
                self.info_label.setText(
                    "Select the files on which the action should be applied. Each complete NWB file is exported, "
                    "its dependencies are inspected, and it is replaced only after validation succeeds."
                )
        elif delete_supported:
            self.info_label.setText(rename_reason)
        else:
            self.info_label.setText(delete_reason or rename_reason)
        self.progress_bar.setRange(0, max(1, len(paths)))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self._update_action_state()
        return True

    def selected_paths(self) -> list[str]:
        selected = []
        for row in range(self.file_table.rowCount()):
            item = self.file_table.item(row, 0)
            path_item = self.file_table.item(row, 1)
            if item is not None and path_item is not None and item.checkState() == qt_checked():
                selected.append(path_item.text())
        return selected

    def is_busy(self) -> bool:
        return bool(self._busy)

    def active_paths(self) -> list[str]:
        return list(self._pending_paths) if self.is_busy() else []

    def _set_all_checked(self, checked: bool):
        state = qt_checked() if checked else qt_unchecked()
        for row in range(self.file_table.rowCount()):
            item = self.file_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _invert_checked(self):
        for row in range(self.file_table.rowCount()):
            item = self.file_table.item(row, 0)
            if item is not None:
                item.setCheckState(
                    qt_unchecked() if item.checkState() == qt_checked() else qt_checked()
                )

    def _update_action_state(self):
        rename_supported, _rename_reason = editable_object_reason(self.object_path, "rename")
        delete_supported, _delete_reason = editable_object_reason(self.object_path, "delete")
        selected_count = len(self.selected_paths())
        busy = self.is_busy()
        self.select_all_button.setEnabled(not busy and self.file_table.rowCount() > 0)
        self.unselect_all_button.setEnabled(not busy and self.file_table.rowCount() > 0)
        self.invert_button.setEnabled(not busy and self.file_table.rowCount() > 0)
        self.file_table.setEnabled(not busy)
        self.new_name_edit.setEnabled(not busy and rename_supported)
        self.rename_button.setEnabled(
            not busy
            and rename_supported
            and selected_count > 0
            and bool(self.new_name_edit.text().strip())
            and self.new_name_edit.text().strip()
            != (
                self.sorting_project
                if _is_unit_project_path(self.object_path)
                else (_parts(self.object_path)[-1] if _parts(self.object_path) else "")
            )
        )
        self.delete_button.setEnabled(not busy and delete_supported and selected_count > 0)
        self.plot_button.setEnabled(not busy and self.plot_capable and selected_count == 1)

    def _request_plot(self):
        paths = self.selected_paths()
        if len(paths) == 1:
            self.plot_requested.emit(paths[0], self.object_path)

    def _confirmation_text(
        self,
        action: str,
        paths: list[str],
        new_name: str = "",
        dependency_reports: Iterable[dict] = (),
    ) -> str:
        verb = "rename" if action == "rename" else "permanently delete"
        target = self.object_path
        if _is_unit_project_path(self.object_path):
            target = f"/units sorting project '{self.sorting_project}'"
        if action == "rename":
            if _is_unit_project_path(self.object_path):
                target += f"\n\nNew sorting project name:\n{new_name}"
            else:
                target += f"\n\nNew path:\n{renamed_object_path(self.object_path, new_name)}"
        shown = paths[:12]
        files_text = "\n".join(f"  {path}" for path in shown)
        if len(paths) > len(shown):
            files_text += f"\n  ... and {len(paths) - len(shown)} more file(s)"
        dependency_lines = []
        for report in dependency_reports:
            dependencies = list(report.get("dependencies", []))
            if not dependencies:
                continue
            dependency_lines.append(f"\n{Path(str(report.get('file_path', ''))).name}:")
            for dependency in dependencies[:20]:
                kinds = ", ".join(dependency.get("kinds", []))
                dependency_lines.append(
                    f"  {str(dependency.get('object_path', '')).strip('/')}"
                    + (f" [{kinds}]" if kinds else "")
                )
            if len(dependencies) > 20:
                dependency_lines.append(f"  ... and {len(dependencies) - 20} more dependent object(s)")

        dependency_text = ""
        if dependency_lines and action == "delete":
            dependency_text = (
                "\n\nDependent data that must also be deleted:"
                + "\n".join(dependency_lines)
                + "\n\nThese dependent data will need to be reprocessed."
            )
        elif dependency_lines:
            dependency_text = (
                "\n\nDependencies detected:"
                + "\n".join(dependency_lines)
                + "\n\nNWB object links will remain connected and stored paths will be updated automatically."
            )
        return (
            f"This will {verb} the following NWB object:\n\n{target}\n\n"
            f"Selected files ({len(paths)}):\n{files_text}{dependency_text}\n\nContinue?"
        )

    def _confirm_action(
        self,
        action: str,
        paths: list[str],
        new_name: str,
        dependency_reports: list[dict],
    ) -> bool:
        yes_button = (
            QtWidgets.QMessageBox.StandardButton.Yes
            if hasattr(QtWidgets.QMessageBox, "StandardButton")
            else QtWidgets.QMessageBox.Yes
        )
        cancel_button = (
            QtWidgets.QMessageBox.StandardButton.Cancel
            if hasattr(QtWidgets.QMessageBox, "StandardButton")
            else QtWidgets.QMessageBox.Cancel
        )
        text = self._confirmation_text(action, paths, new_name, dependency_reports)
        has_dependencies = any(report.get("dependencies") for report in dependency_reports)
        if action != "delete" or not has_dependencies:
            answer = QtWidgets.QMessageBox.question(
                self,
                "Confirm NWB object edit",
                text,
                yes_button | cancel_button,
                cancel_button,
            )
            return answer == yes_button

        message = QtWidgets.QMessageBox(self)
        warning_icon = (
            QtWidgets.QMessageBox.Icon.Warning
            if hasattr(QtWidgets.QMessageBox, "Icon")
            else QtWidgets.QMessageBox.Warning
        )
        destructive_role = (
            QtWidgets.QMessageBox.ButtonRole.DestructiveRole
            if hasattr(QtWidgets.QMessageBox, "ButtonRole")
            else QtWidgets.QMessageBox.DestructiveRole
        )
        reject_role = (
            QtWidgets.QMessageBox.ButtonRole.RejectRole
            if hasattr(QtWidgets.QMessageBox, "ButtonRole")
            else QtWidgets.QMessageBox.RejectRole
        )
        message.setIcon(warning_icon)
        message.setWindowTitle("Delete NWB object and dependencies")
        message.setText(text)
        delete_button = message.addButton("Delete target and dependent data", destructive_role)
        cancel = message.addButton("Cancel", reject_role)
        message.setDefaultButton(cancel)
        if hasattr(message, "exec"):
            message.exec()
        else:
            message.exec_()
        return message.clickedButton() is delete_button

    def _start_action(self, action: str):
        paths = self.selected_paths()
        if not paths:
            QtWidgets.QMessageBox.warning(self, "NWB object manager", "Select at least one NWB file.")
            return
        new_name = ""
        if action == "rename":
            try:
                new_name = (
                    validate_sorting_project_name(self.new_name_edit.text())
                    if _is_unit_project_path(self.object_path)
                    else validate_new_name(self.new_name_edit.text())
                )
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "Rename NWB object", str(exc))
                return
        self._active_action = action
        self._pending_paths = list(paths)
        self._pending_new_name = new_name
        self._dependency_reports = None
        self._dependency_error = ""
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Checking NWB dependencies...")
        for row in range(self.file_table.rowCount()):
            path_item = self.file_table.item(row, 1)
            status_item = self.file_table.item(row, 2)
            if (
                path_item is not None
                and status_item is not None
                and _path_key(path_item.text()) in {_path_key(path) for path in paths}
            ):
                status_item.setText("Checking dependencies")
        self._set_busy(True, self._running_message(checking=True))

        request = {
            "file_paths": paths,
            "object_path": self.object_path,
            "action": action,
            "new_name": new_name,
            "sorting_project": self.sorting_project,
        }
        thread = QtCore.QThread(self)
        worker = _DependencyInspectWorker(request)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_dependencies_inspected)
        worker.failed.connect(self._on_dependency_inspection_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._dependency_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _running_message(self, *, checking: bool = False) -> str:
        action = "rename" if self._active_action == "rename" else "deletion"
        if checking:
            return f"NWB object {action}: dependency check is running"
        return f"NWB object {action} is running"

    def _set_busy(self, busy: bool, message: str = ""):
        changed = bool(self._busy) != bool(busy)
        self._busy = bool(busy)
        if not busy:
            self._thread = None
            self._worker = None
        self._update_action_state()
        if changed or (busy and message):
            self.runningStateChanged.emit(bool(busy), str(message or ""))

    def _on_dependencies_inspected(self, reports):
        self._dependency_reports = list(reports or [])

    def _on_dependency_inspection_failed(self, message: str):
        self._dependency_error = str(message)

    def _dependency_thread_finished(self):
        self._thread = None
        self._worker = None
        if self._dependency_error:
            message = self._dependency_error
            self._dependency_error = ""
            self.status_label.setText("Dependency check failed. No file was modified.")
            self.progress_bar.setRange(0, max(1, len(self._pending_paths)))
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(False)
            self._set_busy(False)
            self._clear_pending_operation()
            QtWidgets.QMessageBox.critical(self, "NWB dependency check failed", message)
            return

        dependency_reports = list(self._dependency_reports or [])
        action = self._active_action
        paths = list(self._pending_paths)
        new_name = self._pending_new_name
        protected = [
            (report, item)
            for report in dependency_reports
            for item in report.get("protected_dependencies", [])
        ]
        if action == "delete" and protected:
            lines = []
            for report, item in protected[:30]:
                lines.append(
                    f"{Path(str(report.get('file_path', ''))).name}: "
                    f"{item.get('location')} ({item.get('kind')})"
                )
            if len(protected) > 30:
                lines.append(f"... and {len(protected) - 30} more")
            self.status_label.setText("Deletion cancelled because protected dependencies were found.")
            self.progress_bar.setVisible(False)
            self._set_busy(False)
            self._clear_pending_operation()
            QtWidgets.QMessageBox.critical(
                self,
                "Cannot safely delete NWB object",
                "The target is referenced by NWB metadata that cannot be removed as an independent data object:\n\n"
                + "\n".join(lines)
                + "\n\nNo file was modified.",
            )
            return

        if not self._confirm_action(action, paths, new_name, dependency_reports):
            self.status_label.setText("Edit cancelled.")
            self.progress_bar.setVisible(False)
            self._set_busy(False)
            self._clear_pending_operation()
            return

        prepare = getattr(self.GUI_instance, "prepare_nwb_object_edit", None)
        if callable(prepare) and not prepare(paths):
            self.status_label.setText("Edit could not start.")
            self.progress_bar.setVisible(False)
            self._set_busy(False)
            self._clear_pending_operation()
            return

        request = {
            "file_paths": paths,
            "object_path": self.object_path,
            "action": action,
            "inventory_path": self.GUI_instance.NWB_inventory_path,
            "new_name": new_name,
            "sorting_project": self.sorting_project,
            "dependency_reports": dependency_reports,
        }
        self.progress_bar.setRange(0, len(paths))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Preparing NWB files...")
        self._set_busy(True, self._running_message())
        thread = QtCore.QThread(self)
        worker = _ObjectEditWorker(request)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._edit_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _clear_pending_operation(self):
        self._pending_paths = []
        self._pending_new_name = ""
        self._dependency_reports = None
        self._dependency_error = ""
        self._active_action = ""

    def _on_progress(self, value: int, path: str, status: str):
        self.progress_bar.setValue(int(value))
        if path:
            path_key = _path_key(path)
            for row in range(self.file_table.rowCount()):
                path_item = self.file_table.item(row, 1)
                if path_item is not None and _path_key(path_item.text()) == path_key:
                    status_item = self.file_table.item(row, 2)
                    if status_item is not None:
                        status_item.setText(str(status))
                    break
        self.status_label.setText(str(status))

    def _on_succeeded(self, result: dict):
        files = list(result.get("files", []))
        self.progress_bar.setValue(len(files))
        dependent_objects = {
            str(dependency.get("object_path", ""))
            for detail in result.get("details", [])
            for dependency in detail.get("dependencies", [])
            if str(dependency.get("object_path", ""))
        }
        dependency_note = (
            f" {len(dependent_objects)} dependent object(s) were also deleted and must be reprocessed."
            if result.get("action") == "delete" and dependent_objects
            else ""
        )
        renamed_unit_rows = sum(
            int(detail.get("renamed_unit_rows", 0) or 0)
            for detail in result.get("details", [])
        )
        deleted_unit_rows = sum(
            int(detail.get("deleted_unit_rows", 0) or 0)
            for detail in result.get("details", [])
        )
        unit_note = ""
        if renamed_unit_rows:
            unit_note = f" Updated {renamed_unit_rows} unit row(s)."
        elif deleted_unit_rows:
            unit_note = f" Deleted {deleted_unit_rows} unit row(s)."
        self.status_label.setText(
            f"{result.get('action', 'Edit').capitalize()} completed for {len(files)} file(s)."
            + unit_note
            + dependency_note
        )
        new_object_path = str(result.get("new_object_path", "") or "")
        if new_object_path:
            self.object_path = new_object_path
            if _is_unit_project_path(new_object_path):
                self.sorting_project = str(result.get("sorting_project", "") or "")
                self.path_label.setText(f"/units | sorting project: {self.sorting_project}")
                self.new_name_edit.setText(self.sorting_project)
            else:
                self.path_label.setText(new_object_path)
                new_parts = _parts(new_object_path)
                self.new_name_edit.setText(new_parts[-1] if new_parts else "")
        warnings = list(result.get("warnings", []))
        if warnings:
            QtWidgets.QMessageBox.warning(self, "NWB object manager", "\n".join(warnings))
        self.operation_finished.emit(result)

    def _on_failed(self, message: str):
        self.status_label.setText("The edit failed. Original files were restored.")
        selected_keys = {_path_key(path) for path in self.selected_paths()}
        for row in range(self.file_table.rowCount()):
            path_item = self.file_table.item(row, 1)
            status_item = self.file_table.item(row, 2)
            if (
                path_item is not None
                and status_item is not None
                and _path_key(path_item.text()) in selected_keys
            ):
                status_item.setText("Unchanged / restored")
        QtWidgets.QMessageBox.critical(self, "NWB object edit failed", str(message))

    def _edit_thread_finished(self):
        self._set_busy(False)
        self._clear_pending_operation()
