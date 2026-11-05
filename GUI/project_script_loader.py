"""Load editable scripts and report discovery errors in windowed builds."""
from __future__ import annotations

import hashlib
import importlib.util
import logging
from pathlib import Path
import sys


def load_project_script(path):
    path = Path(path).resolve()
    # Two projects may use the same filename. Do not register either one under
    # a bare stem that could also shadow an installed package.
    name = "_nwb_project_script_" + hashlib.sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Python script: {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    # dataclasses and other introspection require registration during execution.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


class _DiscoveryErrors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


def discover_for_panel(discover, package, script_dir, parent):
    """Keep working scripts available while making failed imports visible."""
    script_dir = Path(script_dir).resolve()
    log = logging.getLogger(f"nwb.discovery.{package}")
    errors = _DiscoveryErrors()
    log.addHandler(errors)
    try:
        registry = discover(package, extra_dir=script_dir, logger=log)
    finally:
        log.removeHandler(errors)
    report = script_dir.parent.parent / "Logs" / f"script_discovery_{script_dir.name.lower()}.log"
    text = (
        f"Script folder: {script_dir}\n"
        f"Available entries (built-in and project): {sum(len(v) for v in registry.values())}\n\n"
        + ("\n\n".join(errors.messages) if errors.messages else "No script import errors.\n")
    )
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text, encoding="utf-8")
        report_note = f"\n\nDetails saved to:\n{report}"
    except OSError:
        report_note = "\n\nExpand Show Details to view the import errors."
    if errors.messages:
        from GUI.qt_compat import QtCore, QtWidgets

        def show_errors():
            message = QtWidgets.QMessageBox(parent)
            message.setWindowTitle("Script loading errors")
            message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            message.setText(
                f"Some {script_dir.name.lower()} scripts could not be loaded.\n"
                f"Searched folder:\n{script_dir}\n\n"
                "Scripts that loaded successfully remain available."
                + report_note
            )
            message.setInformativeText(
                "If a dependency is missing in the executable, rebuild using both "
                "projects' --project arguments so their imports are included."
            )
            message.setDetailedText(text)
            message.exec()

        QtCore.QTimer.singleShot(0, show_errors)
    return registry
