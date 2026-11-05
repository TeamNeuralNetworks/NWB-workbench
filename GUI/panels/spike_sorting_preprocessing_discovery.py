# -*- coding: utf-8 -*-
"""
Spike sorting preprocessing discovery.

Scans:
1) Package default preprocessing scripts:
   <package_root>/Spike_sorting/Preprocessing

2) Project-specific preprocessing scripts:
   <project_root>/Script/Spike_sorting/Preprocessing

Accepted script formats:
    PREPROCESSING = {...}
    TOOL = {...}
    def get_preprocessing(): return {...}
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Tuple


_REQUIRED_KEYS = ("name", "description", "func")


def _is_preprocessing_dict(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and all(k in obj for k in _REQUIRED_KEYS)
        and callable(obj.get("func"))
    )


def _extract_preprocessing(module: ModuleType, log: logging.Logger):
    """
    Accepted script formats:

    1)
        PREPROCESSING = {...}

    2)
        TOOL = {...}

    3)
        def get_preprocessing():
            return {...}
    """

    prep = getattr(module, "PREPROCESSING", None)
    if _is_preprocessing_dict(prep):
        return prep

    tool = getattr(module, "TOOL", None)
    if _is_preprocessing_dict(tool):
        return tool

    fn = getattr(module, "get_preprocessing", None)
    if callable(fn):
        try:
            prep2 = fn()
            if _is_preprocessing_dict(prep2):
                return prep2
        except Exception:
            log.exception("get_preprocessing() failed in %s", module.__name__)

    return None


def _load_module_from_path(py_file: Path, log: logging.Logger, verbose: bool = False):
    """
    Load one Python file directly from disk.

    Important:
    The module is NOT registered in sys.modules.
    This avoids Spyder/IPython autoreload errors for dynamically loaded scripts.
    """

    safe_stem = py_file.stem.replace("-", "_").replace(" ", "_")
    module_name = f"spike_sorting_preprocessing_{safe_stem}"

    added_to_syspath = False
    parent_str = str(py_file.parent)

    try:
        # Allow imports from the same preprocessing folder while loading this file.
        if parent_str not in sys.path:
            sys.path.insert(0, parent_str)
            added_to_syspath = True

        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)

        # Do NOT do this:
        # sys.modules[module_name] = module

        spec.loader.exec_module(module)
        return module

    except Exception as exc:
        log.exception("Failed importing preprocessing script: %s", py_file)

        if verbose:
            print(f"[Spike preprocessing discovery] Failed importing: {py_file}")
            print(exc)

        return None

    finally:
        if added_to_syspath:
            try:
                sys.path.remove(parent_str)
            except ValueError:
                pass


def _find_package_root_from_here():
    """
    From:
        .../GUI/panels/Spike_sorting/spike_sorting_preprocessing_discovery.py

    Try to recover the package root:
        .../Functionnal_Imaging_Electrophysiology

    so we can scan:
        .../Functionnal_Imaging_Electrophysiology/Spike_sorting/Preprocessing
    """

    here = Path(__file__).resolve()

    for parent in here.parents:
        if (parent / "GUI").exists() and (parent / "Spike_sorting").exists():
            return parent

    return None


def _package_preprocessing_candidates() -> List[Path]:
    candidates: List[Path] = []

    root = _find_package_root_from_here()
    if root is not None:
        candidates.append(root / "Spike_sorting" / "Preprocessing")

    # Fallback: if Spike_sorting is importable as a package.
    try:
        import Spike_sorting  # type: ignore

        pkg_file = getattr(Spike_sorting, "__file__", None)
        if pkg_file:
            pkg_root = Path(pkg_file).resolve().parent
            candidates.append(pkg_root / "Preprocessing")
    except Exception:
        pass

    return candidates


def _project_preprocessing_candidates(project_root) -> List[Path]:
    if project_root is None:
        return []

    root = Path(project_root)

    candidates: List[Path] = []

    # Normal case:
    # <project_root>/Script/Spike_sorting/Preprocessing
    candidates.append(root / "Script" / "Spike_sorting" / "Preprocessing")

    # In case project_root is already the Script folder.
    candidates.append(root / "Spike_sorting" / "Preprocessing")

    # In case project_root is already the Preprocessing folder.
    if root.name.lower() == "preprocessing":
        candidates.append(root)

    return candidates


def _candidate_preprocessing_roots(project_root) -> List[Tuple[Path, str]]:
    """
    Returns list of:
        (path, source_scope)

    source_scope is:
        "package" or "project"

    Package defaults are scanned first.
    Project scripts are scanned second, so project scripts override package
    scripts with the same PREPROCESSING["name"].
    """

    candidates: List[Tuple[Path, str]] = []

    for path in _package_preprocessing_candidates():
        candidates.append((path, "package"))

    for path in _project_preprocessing_candidates(project_root):
        candidates.append((path, "project"))

    out: List[Tuple[Path, str]] = []
    seen = set()

    for path, source_scope in candidates:
        try:
            key = str(path.resolve()).lower()
        except Exception:
            key = str(path).lower()

        if key in seen:
            continue

        seen.add(key)
        out.append((path, source_scope))

    return out


def discover_preprocessings(
    project_root,
    *,
    logger: logging.Logger | None = None,
    verbose: bool = False,
) -> Dict[str, dict]:
    """
    Discover preprocessing scripts.

    Returns:
        {
            "Preprocessing name": preprocessing_spec_dict,
            ...
        }

    A valid preprocessing script contains:

        PREPROCESSING = {
            "name": "...",
            "description": "...",
            "func": some_function,
            "default": True or False,   # optional checkbox default
            "order": 1,                 # optional default order
            "params": {...},            # optional parameters
        }
    """

    log = logger or logging.getLogger(__name__)
    registry: Dict[str, dict] = {}

    roots = _candidate_preprocessing_roots(project_root)

    if verbose:
        print("[Spike preprocessing discovery] project_root =", project_root)
        print("[Spike preprocessing discovery] candidate folders:")
        for root, scope in roots:
            print(f"  - [{scope}]", root, "exists =", root.exists())

    discovery_order = 1

    for preprocessing_root, source_scope in roots:
        if not preprocessing_root.exists():
            continue

        if not preprocessing_root.is_dir():
            continue

        for py_file in sorted(preprocessing_root.rglob("*.py"), key=lambda p: str(p).lower()):
            if py_file.name == "__init__.py":
                continue

            if py_file.name.startswith("_"):
                continue

            if verbose:
                print(f"[Spike preprocessing discovery] checking [{source_scope}]:", py_file)

            module = _load_module_from_path(py_file, log, verbose=verbose)
            if module is None:
                continue

            prep = _extract_preprocessing(module, log)
            if prep is None:
                if verbose:
                    print("[Spike preprocessing discovery] no valid PREPROCESSING in:", py_file)
                continue

            prep = dict(prep)
            prep.setdefault("params", {})
            prep.setdefault("default", False)
            prep.setdefault("order", discovery_order)

            prep["source_file"] = str(py_file)
            prep["source_scope"] = source_scope
            prep["source_root"] = str(preprocessing_root)
            prep["discovery_order"] = discovery_order

            name = str(prep.get("name", "")).strip()
            if not name:
                continue

            # Project scripts override package scripts if the name is the same.
            registry[name] = prep
            discovery_order += 1

            if verbose:
                print(f"[Spike preprocessing discovery] found [{source_scope}]:", name)

    if verbose:
        print("[Spike preprocessing discovery] final list:", sorted(registry.keys()))

    return registry
