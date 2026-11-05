from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from types import ModuleType
from GUI.project_script_loader import load_project_script
from typing import Any, Dict

_REQUIRED_KEYS = ("name", "description", "func")


def _is_analysis_dict(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and all(k in obj for k in _REQUIRED_KEYS)
        and callable(obj.get("func"))
    )


def _extract_analysis(module: ModuleType, log: logging.Logger):
    spec = getattr(module, "ANALYSIS", None)
    if _is_analysis_dict(spec):
        return spec

    fn = getattr(module, "get_analysis", None)
    if callable(fn):
        try:
            spec2 = fn()
            if _is_analysis_dict(spec2):
                return spec2
        except Exception:
            log.exception("get_analysis() failed in %s", getattr(module, "__name__", module))

    return None


def _load_module_from_path(path: Path):
    return load_project_script(path)


def discover_analyses(
    package: str = "Analyses",
    *,
    extra_dir: str | Path | None = None,
    logger: logging.Logger | None = None,
) -> Dict[str, Dict[str, dict]]:
    """Discover analysis specs from a package and optional project scripts.

    Supported module API:
        ANALYSIS = {"name": ..., "description": ..., "func": callable, ...}
    or:
        get_analysis() -> same dict
    """
    log = logger or logging.getLogger(__name__)
    registry: Dict[str, Dict[str, dict]] = {}

    try:
        pkg = importlib.import_module(package)
    except Exception:
        pkg = None

    if pkg is not None and hasattr(pkg, "__path__"):
        for module_info in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
            mod_name = module_info.name
            try:
                module = importlib.import_module(mod_name)
            except Exception:
                log.exception("Failed importing module %s", mod_name)
                continue

            analysis = _extract_analysis(module, log)
            if analysis is None:
                continue

            parts = mod_name.split(".")
            category = parts[1] if len(parts) >= 2 else "General"
            analysis_name = str(analysis["name"])
            analysis_with_meta = dict(analysis)
            analysis_with_meta.setdefault("category", category)
            analysis_with_meta.setdefault("module", mod_name)
            registry.setdefault(category, {})[analysis_name] = analysis_with_meta

    if extra_dir is not None:
        extra_dir = Path(extra_dir)
        if extra_dir.exists():
            for py_file in sorted(extra_dir.rglob("*.py")):
                if py_file.name == "__init__.py":
                    continue
                try:
                    module = _load_module_from_path(py_file)
                except Exception:
                    log.exception("Failed loading script %s", py_file)
                    continue
                if module is None:
                    continue

                analysis = _extract_analysis(module, log)
                if analysis is None:
                    continue

                rel = py_file.relative_to(extra_dir)
                category = rel.parts[0] if len(rel.parts) >= 2 else "General"
                analysis_name = str(analysis["name"])
                analysis_with_meta = dict(analysis)
                analysis_with_meta["category"] = category
                analysis_with_meta["module"] = str(py_file)
                registry.setdefault(category, {})[analysis_name] = analysis_with_meta

    return registry
