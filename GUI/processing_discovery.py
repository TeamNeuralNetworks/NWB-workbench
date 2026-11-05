from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from types import ModuleType
from GUI.project_script_loader import load_project_script
from typing import Any, Dict


_REQUIRED_KEYS = ("name", "description", "func")


def _is_tool_dict(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and all(k in obj for k in _REQUIRED_KEYS)
        and callable(obj.get("func"))
    )


def _extract_tool(module: ModuleType, log: logging.Logger):
    tool = getattr(module, "TOOL", None)
    if _is_tool_dict(tool):
        return tool

    fn = getattr(module, "get_tool", None)
    if callable(fn):
        try:
            tool2 = fn()
            if _is_tool_dict(tool2):
                return tool2
        except Exception:
            log.exception("get_tool() failed in %s", module.__name__)

    return None


def _load_module_from_path(path: Path):
    return load_project_script(path)


def discover_tools(
    package: str = "Processings",
    *,
    extra_dir: str | Path | None = None,
    logger: logging.Logger | None = None,
) -> Dict[str, Dict[str, dict]]:
    log = logger or logging.getLogger(__name__)
    registry: Dict[str, Dict[str, dict]] = {}

    # Built-in package discovery
    try:
        pkg = importlib.import_module(package)
    except Exception:
        log.exception("Failed to import package %s", package)
        pkg = None

    if pkg is not None and hasattr(pkg, "__path__"):
        for module_info in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
            mod_name = module_info.name

            try:
                module = importlib.import_module(mod_name)
            except Exception:
                log.exception("Failed importing module %s", mod_name)
                continue

            tool = _extract_tool(module, log)
            if tool is None:
                continue

            parts = mod_name.split(".")
            category = parts[1] if len(parts) >= 2 else "General"

            registry.setdefault(category, {})

            tool_name = tool["name"]
            tool_with_meta = dict(tool)
            tool_with_meta["category"] = category
            tool_with_meta["module"] = mod_name

            registry[category][tool_name] = tool_with_meta

    # Custom project scripts: Script/Processing/<Category>/*.py
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

                tool = _extract_tool(module, log)
                if tool is None:
                    continue

                rel = py_file.relative_to(extra_dir)
                parts = rel.parts

                if len(parts) >= 2:
                    category = parts[0]
                else:
                    category = "General"

                registry.setdefault(category, {})

                tool_name = tool["name"]
                tool_with_meta = dict(tool)
                tool_with_meta["category"] = category
                tool_with_meta["module"] = str(py_file)

                registry[category][tool_name] = tool_with_meta

    return registry