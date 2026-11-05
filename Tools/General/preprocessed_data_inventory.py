from __future__ import annotations
from GUI.project_paths import to_excel as _pp_to_excel
from GUI.project_paths import json_load as _pp_json_load, read_excel as _pp_read_excel

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd


INVENTORY_FILENAME = "Preprocessed_filtered_data_inventory.xlsx"
DEFAULT_INVENTORY_COLUMNS = [
    "filter_name",
    "condition",
    "data_type",
    "aligned_to_event",
    "edge",
    "pad_before_s",
    "pad_after_s",
    "saved_on_disk",
    "save_path",
    "nwb_files_nb",
]


@dataclass(frozen=True)
class PreprocessedDeletionPlan:
    project_root: Path
    inventory_path: Path
    inventory_signature: tuple[int, int] | None
    requested_row_indices: tuple[int, ...]
    removed_row_indices: tuple[int, ...]
    target_paths: tuple[Path, ...]
    missing_paths: tuple[Path, ...]
    size_bytes: int


@dataclass(frozen=True)
class StalePreprocessedEntry:
    """One cache row whose saved condition content no longer matches it."""

    row_index: int
    filter_name: str
    condition: str
    data_type: str
    aligned_to_event: Any
    edge: Any
    pad_before_s: Any
    pad_after_s: Any
    save_path: Path | None
    status: str
    changed_fields: tuple[str, ...]


def preprocessed_cache_root(project_root) -> Path:
    return Path(project_root).expanduser().resolve() / "Data" / "Preprocessed_filtered_data"


def preprocessed_inventory_path(project_root) -> Path:
    return preprocessed_cache_root(project_root) / INVENTORY_FILENAME


def read_preprocessed_inventory(project_root) -> pd.DataFrame:
    path = preprocessed_inventory_path(project_root)
    if not path.exists():
        return pd.DataFrame(columns=DEFAULT_INVENTORY_COLUMNS)
    dataframe = _pp_read_excel(path)
    return dataframe.reset_index(drop=True)


def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if result is pd.NA:
        return True
    try:
        return bool(result) if not hasattr(result, "__len__") else False
    except (TypeError, ValueError):
        return False


def _text_value(value) -> str:
    if _is_missing(value):
        return ""
    return str(value).strip()


def _number_matches(left, right) -> bool:
    if _is_missing(left) and _is_missing(right):
        return True
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return _text_value(left) == _text_value(right)


def _disabled_alignment_value(value) -> bool:
    if _is_missing(value):
        return True
    if isinstance(value, bool):
        return not value
    return _text_value(value).casefold() in {"", "0", "false", "none", "nan"}


def _legacy_cache_offsets(event_alignment: dict) -> tuple[Any, Any]:
    """Translate signed offsets to the established cache-column convention."""
    event_alignment = dict(event_alignment or {})
    if "start_offset_s" in event_alignment:
        start_offset = float(event_alignment["start_offset_s"])
        pad_before = -start_offset
        if pad_before == 0:
            pad_before = 0.0
    else:
        pad_before = event_alignment.get("pad_before_s")

    if "end_offset_s" in event_alignment:
        pad_after = float(event_alignment["end_offset_s"])
    else:
        pad_after = event_alignment.get("pad_after_s")
    return pad_before, pad_after


def _alignment_row_matches(row: pd.Series, align_to_events: bool, event_alignment: dict) -> bool:
    if not align_to_events:
        return _disabled_alignment_value(row.get("aligned_to_event"))

    expected = dict(event_alignment or {})
    if _text_value(row.get("aligned_to_event")) != _text_value(expected.get("event_marker")):
        return False
    if _text_value(row.get("edge")).casefold() != _text_value(expected.get("edge")).casefold():
        return False
    expected_before, expected_after = _legacy_cache_offsets(expected)
    return _number_matches(row.get("pad_before_s"), expected_before) and _number_matches(
        row.get("pad_after_s"), expected_after
    )


def _json_compatible(value):
    if isinstance(value, dict):
        return {
            str(key): _json_compatible(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_compatible(item) for item in value), key=repr)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return _json_compatible(value.item())
        except Exception:
            pass
    if _is_missing(value):
        return None
    return value


def _condition_changes(current: dict, saved: dict) -> tuple[str, ...]:
    changes = []
    current_files = _json_compatible(list((current or {}).get("file_path_list", []) or []))
    saved_files = _json_compatible(list((saved or {}).get("file_path_list", []) or []))
    if current_files != saved_files:
        changes.append("selected files")

    current_has_trials = "trial_filter_dict" in (current or {})
    saved_has_trials = "trial_filter_dict" in (saved or {})
    current_trials = _json_compatible(
        dict((current or {}).get("trial_filter_dict", {}) or {})
    )
    saved_trials = _json_compatible(
        dict((saved or {}).get("trial_filter_dict", {}) or {})
    )
    if (current_has_trials and not saved_has_trials) or current_trials != saved_trials:
        changes.append("trial filters")
    return tuple(changes)


def find_stale_preprocessed_entries(
    project_root,
    *,
    condition_filter: dict,
    data_types: Iterable[str],
    align_to_events: bool,
    event_marker_to_align_dict: dict | None,
) -> list[StalePreprocessedEntry]:
    """Find cache rows whose matching current condition changed.

    Conditions are compared independently by name.  Adding a new condition,
    removing an old condition, or renaming a condition changes the condition
    set, but it does not modify the content of another condition.  Those set
    changes therefore do not make an unrelated cache row stale and do not need
    a confirmation popup.

    Candidate rows intentionally ignore ``nwb_files_nb``. A changed file list can
    change that count, and it is precisely one of the stale-cache cases this
    preflight is meant to detect before the background analysis starts.
    """
    dataframe = read_preprocessed_inventory(project_root)
    if dataframe.empty or not isinstance(condition_filter, dict):
        return []

    filter_name = _text_value(condition_filter.get("_conditions_name"))
    if not filter_name:
        return []
    requested_data = {
        _text_value(data_type)
        for data_type in list(data_types or [])
        if _text_value(data_type)
    }
    if not requested_data:
        return []

    current_conditions = {
        _text_value(condition): block
        for condition, block in condition_filter.items()
        if condition != "_conditions_name" and isinstance(block, dict)
    }
    conflicts: list[StalePreprocessedEntry] = []
    for row_index, row in dataframe.iterrows():
        condition_name = _text_value(row.get("condition"))
        data_type = _text_value(row.get("data_type"))
        if (
            _text_value(row.get("filter_name")) != filter_name
            or data_type not in requested_data
            or not _alignment_row_matches(
                row,
                bool(align_to_events),
                dict(event_marker_to_align_dict or {}),
            )
        ):
            continue

        current_block = current_conditions.get(condition_name)
        if current_block is None:
            # This saved row belongs to a condition that is not part of the
            # current launch.  It is irrelevant to the current request and can
            # remain available for another analysis using that condition set.
            continue

        cache_path, status = resolve_inventory_save_path(
            project_root,
            row.get("save_path", ""),
        )
        changes: tuple[str, ...]
        if cache_path is None:
            changes = ("cache metadata path",)
        else:
            filter_path = cache_path / "filter.json"
            try:
                with filter_path.open("r", encoding="utf-8") as stream:
                    saved_block = _pp_json_load(stream)
                if not isinstance(saved_block, dict):
                    raise ValueError("filter.json does not contain an object")
                changes = _condition_changes(current_block, saved_block)
            except Exception:
                changes = ("missing or unreadable filter metadata",)

        if changes:
            conflicts.append(
                StalePreprocessedEntry(
                    row_index=int(row_index),
                    filter_name=filter_name,
                    condition=condition_name,
                    data_type=data_type,
                    aligned_to_event=row.get("aligned_to_event"),
                    edge=row.get("edge"),
                    pad_before_s=row.get("pad_before_s"),
                    pad_after_s=row.get("pad_after_s"),
                    save_path=cache_path,
                    status=status,
                    changed_fields=changes,
                )
            )
    return conflicts


def inventory_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return int(stat.st_mtime_ns), int(stat.st_size)


def format_file_size(size_bytes: int | float | None) -> str:
    if size_bytes is None:
        return ""
    try:
        value = max(0.0, float(size_bytes))
    except (TypeError, ValueError):
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def folder_size_bytes(path: Path, stop_requested=None) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return int(path.lstat().st_size)
        except OSError:
            return 0

    total = 0
    for current_root, directory_names, file_names in os.walk(path, followlinks=False):
        if callable(stop_requested) and stop_requested():
            break
        current = Path(current_root)
        # Do not follow directory symlinks while calculating cache size.
        directory_names[:] = [
            name for name in directory_names if not (current / name).is_symlink()
        ]
        for file_name in file_names:
            if callable(stop_requested) and stop_requested():
                return total
            file_path = current / file_name
            try:
                total += int(file_path.lstat().st_size)
            except OSError:
                continue
    return total


def _clean_excel_path(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def resolve_inventory_save_path(project_root, value) -> tuple[Path | None, str]:
    """Resolve one inventory path and classify whether it is safe to manage."""
    cache_root = preprocessed_cache_root(project_root)
    text = _clean_excel_path(value)
    if not text:
        return None, "Missing save path"

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = Path(project_root).expanduser().resolve() / candidate

    # If a project was moved, the saved absolute path may be stale while the
    # UUID folder is still present under the current project's cache root.
    if not candidate.exists() and candidate.name:
        relocated = cache_root / candidate.name
        if relocated.exists():
            candidate = relocated

    try:
        root_resolved = cache_root.resolve()
        candidate_resolved = candidate.resolve(strict=False)
    except OSError:
        return None, "Invalid save path"

    if candidate_resolved == root_resolved or candidate_resolved.parent != root_resolved:
        return None, "Outside cache folder"
    if candidate.exists() and candidate.is_symlink():
        return None, "Symbolic links are not managed"
    if not candidate.exists():
        return candidate_resolved, "Missing on disk"
    return candidate_resolved, "Available"


def inventory_rows_with_storage(project_root, *, calculate_sizes: bool = True) -> pd.DataFrame:
    dataframe = read_preprocessed_inventory(project_root)
    rows = dataframe.copy(deep=True)
    resolved_paths = []
    statuses = []
    sizes = []
    for _, row in dataframe.iterrows():
        path, status = resolve_inventory_save_path(project_root, row.get("save_path", ""))
        resolved_paths.append(str(path) if path is not None else "")
        statuses.append(status)
        if calculate_sizes and path is not None and status == "Available":
            sizes.append(folder_size_bytes(path))
        else:
            sizes.append(None)
    rows.insert(0, "_inventory_row", list(dataframe.index))
    rows["_resolved_path"] = resolved_paths
    rows["_status"] = statuses
    rows["_size_bytes"] = sizes
    return rows


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _validate_managed_target(cache_root: Path, target: Path) -> Path:
    root_resolved = cache_root.resolve()
    target_resolved = Path(target).resolve(strict=False)
    if target_resolved == root_resolved or target_resolved.parent != root_resolved:
        raise ValueError(f"Refusing to delete a path outside the cache folder:\n{target}")
    if target_resolved.exists() and target_resolved.is_symlink():
        raise ValueError(f"Refusing to delete a symbolic link:\n{target}")
    return target_resolved


def build_preprocessed_deletion_plan(
    project_root,
    requested_row_indices: Iterable[int],
) -> PreprocessedDeletionPlan:
    project_root = Path(project_root).expanduser().resolve()
    inventory_path = preprocessed_inventory_path(project_root)
    dataframe = read_preprocessed_inventory(project_root)
    requested = tuple(sorted({int(index) for index in requested_row_indices}))
    if not requested:
        raise ValueError("No preprocessed data are selected.")

    invalid = [index for index in requested if index not in dataframe.index]
    if invalid:
        raise ValueError("The preprocessing inventory changed. Refresh the manager and try again.")

    cache_root = preprocessed_cache_root(project_root)
    path_by_index: dict[int, Path] = {}
    key_by_index: dict[int, str] = {}
    for index, row in dataframe.iterrows():
        path, status = resolve_inventory_save_path(project_root, row.get("save_path", ""))
        if path is None:
            if int(index) in requested:
                raise ValueError(
                    f"Row {int(index) + 1} cannot be deleted safely: {status}."
                )
            continue
        path = _validate_managed_target(cache_root, path)
        path_by_index[int(index)] = path
        key_by_index[int(index)] = _path_key(path)

    selected_keys = {key_by_index[index] for index in requested}
    # If stale duplicate rows reference the same cache folder, remove all of
    # those rows together so the remaining inventory never points to a folder
    # that was just deleted.
    removed_indices = tuple(
        int(index)
        for index in dataframe.index
        if key_by_index.get(int(index)) in selected_keys
    )
    targets_by_key = {
        key: path_by_index[index]
        for index, key in key_by_index.items()
        if key in selected_keys
    }
    targets = tuple(sorted(targets_by_key.values(), key=lambda path: str(path).lower()))
    missing = tuple(path for path in targets if not path.exists())
    size_bytes = sum(folder_size_bytes(path) for path in targets if path.exists())

    return PreprocessedDeletionPlan(
        project_root=project_root,
        inventory_path=inventory_path,
        inventory_signature=inventory_signature(inventory_path),
        requested_row_indices=requested,
        removed_row_indices=removed_indices,
        target_paths=targets,
        missing_paths=missing,
        size_bytes=size_bytes,
    )


def _write_inventory_atomic(dataframe: pd.DataFrame, inventory_path: Path) -> None:
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = inventory_path.with_name(
        f".{inventory_path.stem}.{uuid4().hex}.tmp.xlsx"
    )
    try:
        _pp_to_excel(dataframe, temporary_path, index=False)
        os.replace(temporary_path, inventory_path)
    finally:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass


def execute_preprocessed_deletion(plan: PreprocessedDeletionPlan) -> dict:
    if inventory_signature(plan.inventory_path) != plan.inventory_signature:
        raise RuntimeError(
            "The preprocessing inventory changed after the confirmation. "
            "Refresh the manager and try again."
        )

    dataframe = read_preprocessed_inventory(plan.project_root)
    cache_root = preprocessed_cache_root(plan.project_root)
    for target in plan.target_paths:
        _validate_managed_target(cache_root, target)

    cache_root.mkdir(parents=True, exist_ok=True)
    staging_root = cache_root / f".delete_staging_{uuid4().hex}"
    moved: list[tuple[Path, Path]] = []
    try:
        for target in plan.target_paths:
            if not target.exists():
                continue
            staging_root.mkdir(parents=True, exist_ok=True)
            staged_target = staging_root / target.name
            shutil.move(str(target), str(staged_target))
            moved.append((target, staged_target))

        remaining = dataframe.drop(index=list(plan.removed_row_indices)).reset_index(drop=True)
        _write_inventory_atomic(remaining, plan.inventory_path)
    except Exception:
        rollback_errors = []
        for original, staged in reversed(moved):
            try:
                if staged.exists() and not original.exists():
                    shutil.move(str(staged), str(original))
            except Exception as exc:
                rollback_errors.append(f"{original}: {exc}")
        try:
            if staging_root.exists() and not any(staging_root.iterdir()):
                staging_root.rmdir()
        except OSError:
            pass
        if rollback_errors:
            raise RuntimeError(
                "Deletion failed and some cache folders could not be restored:\n"
                + "\n".join(rollback_errors)
            )
        raise

    cleanup_warning = ""
    if staging_root.exists():
        try:
            shutil.rmtree(staging_root)
        except Exception as exc:
            cleanup_warning = (
                "The inventory was updated, but some deleted data could not be "
                f"removed from the temporary staging folder:\n{staging_root}\n\n{exc}"
            )

    return {
        "removed_rows": len(plan.removed_row_indices),
        "removed_folders": len(moved),
        "missing_folders": len(plan.missing_paths),
        "size_bytes": int(plan.size_bytes),
        "warning": cleanup_warning,
    }
