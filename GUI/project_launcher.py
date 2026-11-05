from __future__ import annotations
from GUI.project_paths import json_read as _pp_json_read, json_write as _pp_json_write, to_excel as _pp_to_excel

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from GUI.qt_compat import QtCore, QtGui, QtWidgets, exec_app
from GUI.app import NWBApp
from GUI.startup_splash import finish_splash_when_visible


APP_NAME = "NWB Workbench"
from GUI.project_tabs import TAB_DEFINITIONS, normalize_enabled_tabs

PROJECT_METADATA_NAME = "nwb_workbench_project.json"
INVENTORY_RELATIVE_PATH = Path("Data") / "nwb_inventory.xlsx"


PROJECT_DIRECTORIES = [
    # Project data
    "Data",
    # Project filters saved or loaded by the GUI
    "Filters",
    # User/project scripts discovered by the GUI
    "Script",
    "Script/Processing",
    "Script/Analysis",
    "Script/Spike_sorting",
    "Script/Spike_sorting/Preprocessing",
    "Script/Utilities",
    # Main analysis output folder
    "Analysis",
]


SCRIPT_PACKAGE_DIRS = [
    "Script",
    "Script/Processing",
    "Script/Analysis",
    "Script/Spike_sorting",
    "Script/Spike_sorting/Preprocessing",
    "Script/Utilities",
]


README_TEXT = """# NWB Workbench project

This folder was created by the NWB Workbench project launcher.

Main folders:

- `Data`: project data root. The inventory will be created when you scan/update the data inventory.
- `Data/Spike_sorting`: spike-sorting data and outputs used by the Spike sorting tab.
- `Filters`: saved filter definitions or filter-related files.
- `Script/Processing`: project-specific processing tools.
- `Script/Analysis`: project-specific analysis tools.
- `Script/Spike_sorting/Preprocessing`: project-specific spike-sorting preprocessing functions.
- `Script/Utilities`: project-specific helper scripts.
- `Analysis`: analysis result folders written by the Analysis tab.

Use `NWB Workbench.exe` (Windows) or `launch_NWB_Workbench.py` to reopen this project directly.
"""


def _gui_autolaunch_suppressed() -> bool:
    """Return True in child processes that must not create a GUI window.

    Sorter backends may start a fresh Python process. On Windows this can
    re-import the user's launch script. The spike-sorting runner sets these
    environment variables while the sorter is launching, so child processes
    return immediately instead of opening a second NWB GUI window.
    """
    return (
        os.environ.get("NWB_GUI_SUPPRESS_AUTOLAUNCH") == "1"
        or os.environ.get("NWB_GUI_CHILD_PROCESS") == "1"
    )


def _message_box_button(name: str):
    try:
        return getattr(QtWidgets.QMessageBox.StandardButton, name)
    except AttributeError:
        return getattr(QtWidgets.QMessageBox, name)


def _message_box_buttons(*names: str):
    buttons = None
    for name in names:
        button = _message_box_button(name)
        buttons = button if buttons is None else buttons | button
    return buttons


def _repo_root_from_this_file() -> Path:
    # .../<repo>/GUI/project_launcher.py -> .../<repo>
    return Path(__file__).resolve().parent.parent


def _safe_write_text(path: Path, text: str, *, overwrite: bool = False) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _touch_init_files(project_root: Path):
    for rel in SCRIPT_PACKAGE_DIRS:
        init_file = project_root / rel / "__init__.py"
        _safe_write_text(init_file, "", overwrite=False)


def _empty_inventory_columns() -> list[str]:
    fallback = [
        "file_path",
        "file_type",
        "identifier",
        "session_description",
        "session_start_time",
        "subject_id",
        "species",
        "genotype",
        "sex",
        "has_trials",
        "number_of_trials",
    ]
    try:
        from Builders.General.generate_nwb_inventory import REQUIRED_INVENTORY_COLUMNS
        cols = list(REQUIRED_INVENTORY_COLUMNS)
        return cols or fallback
    except Exception:
        return fallback


def _create_empty_inventory(inv_path: Path, *, overwrite: bool = False) -> bool:
    if inv_path.exists() and not overwrite:
        return False

    import pandas as pd

    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_df = pd.DataFrame(columns=_empty_inventory_columns())
    content_df = pd.DataFrame(
        columns=[
            "file_path",
            "nwb_path",
            "name",
            "neurodata_type",
            "parent",
            "location",
            "shape",
            "unit",
            "sampling_rate",
            "description",
        ]
    )
    trial_filter_catalog_df = pd.DataFrame(
        columns=[
            "file_path",
            "identifier",
            "trial_column",
            "value",
            "count",
        ]
    )

    with pd.ExcelWriter(inv_path, engine="openpyxl") as writer:
        _pp_to_excel(inventory_df, writer, sheet_name="inventory", index=False)
        _pp_to_excel(content_df, writer, sheet_name="content_index", index=False)
        _pp_to_excel(trial_filter_catalog_df, writer, sheet_name="trial_filter_catalog", index=False)
    return True


def _path_literal(path) -> str:
    """Prefer a readable raw path, with safe quoting for unusual names."""
    value = str(path)
    if not value.endswith("\\"):
        for quote in ("'", '"'):
            if quote not in value and "\n" not in value and "\r" not in value:
                return "r" + quote + value + quote
    return repr(value)


def _launch_script_text(project_root: Path) -> str:
    from GUI.project_paths import encode, PREFIX
    location = encode(str(_repo_root_from_this_file()), Path(project_root).expanduser().resolve())
    if location.startswith(PREFIX):
        code_expression = "str((project_path / " + repr(location[len(PREFIX):]) + ").resolve())"
    else:
        code_expression = repr(location)
    return f'''# -*- coding: utf-8 -*-
"""Launch this NWB Workbench project."""
import sys
from pathlib import Path
project_path = Path(__file__).resolve().parent
sys.path.append({code_expression})
from GUI.project_launcher import appLauncher

appLauncher(str(project_path))
'''


def _create_launch_scripts(project_root: Path):
    # Frozen projects use their executable. Do not replace working Python
    # launchers with paths into the private bundled runtime.
    if getattr(sys, "frozen", False):
        return
    launch_script = _launch_script_text(project_root)
    _safe_write_text(
        project_root / "launch_NWB_Workbench.py",
        launch_script,
        overwrite=True,
    )


def _create_project_metadata(project_root: Path, *, enabled_tabs=None):
    metadata_path = project_root / PROJECT_METADATA_NAME
    is_new_project = not metadata_path.exists()
    if metadata_path.exists():
        try:
            data = _pp_json_read(metadata_path)
        except Exception:
            data = {}
    else:
        data = {}

    if is_new_project:
        data["portable_paths_version"] = 1
    if data.get("portable_paths_version") == 1:
        data["legacy_project_roots"] = list(dict.fromkeys(data.get("legacy_project_roots", []) + [str(project_root)]))
    data.update(
        {
            "app_name": APP_NAME,
            "project_name": project_root.name,
            "project_root": str(project_root),
            "inventory": str(project_root / INVENTORY_RELATIVE_PATH),
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "code_root": str(_repo_root_from_this_file()),
        }
    )
    if enabled_tabs is not None or "enabled_tabs" not in data:
        data["enabled_tabs"] = normalize_enabled_tabs(enabled_tabs)
    if "created" not in data:
        data["created"] = data["last_updated"]
    _pp_json_write(metadata_path, data, dump_options={'indent':2})


def create_or_update_project_structure(project_root: Path, *, create_inventory: bool = False, enabled_tabs=None) -> dict:
    """Create or update the NWB Workbench project skeleton.

    Existing user files are preserved. The launcher does not create an empty
    inventory file during first-use project creation. The inventory is created
    later by the GUI scan/update inventory action.
    """
    project_root = Path(project_root).expanduser().resolve()
    created_dirs: list[str] = []

    project_root.mkdir(parents=True, exist_ok=True)
    for rel in PROJECT_DIRECTORIES:
        p = project_root / rel
        if not p.exists():
            created_dirs.append(rel)
        p.mkdir(parents=True, exist_ok=True)

    _touch_init_files(project_root)
    _safe_write_text(project_root / "README_NWB_Workbench_project.md", README_TEXT, overwrite=False)
    _create_project_metadata(project_root, enabled_tabs=enabled_tabs)
    _create_launch_scripts(project_root)

    inventory_created = False
    if create_inventory:
        inventory_created = _create_empty_inventory(project_root / INVENTORY_RELATIVE_PATH, overwrite=False)

    return {
        "project_root": str(project_root),
        "created_dirs": created_dirs,
        "inventory_created": inventory_created,
        "inventory_path": str(project_root / INVENTORY_RELATIVE_PATH),
        "launch_script": str(project_root / "launch_NWB_Workbench.py"),
    }


class _ProjectCreationThread(QtCore.QThread):
    stage_changed = QtCore.Signal(str)

    def __init__(self, root, parent=None, *, build_exe=True, create_shortcut=False, enabled_tabs=None):
        super().__init__(parent)
        self.root = Path(root)
        self.enabled_tabs = list(enabled_tabs) if enabled_tabs is not None else None
        self.build_exe = bool(build_exe)
        self.create_shortcut = bool(build_exe and create_shortcut)
        self.error = None
        self.warning = None

    def run(self):
        try:
            from GUI.frozen_project import prepare_project_application
            self.stage_changed.emit("Creating project folders…")
            create_or_update_project_structure(self.root, create_inventory=False, enabled_tabs=self.enabled_tabs)
            if self.build_exe:
                executable = prepare_project_application(self.root, progress=self.stage_changed.emit)
                if self.create_shortcut:
                    try:
                        from GUI.frozen_project import create_project_shortcut
                        self.stage_changed.emit("Creating Desktop shortcut…")
                        create_project_shortcut(self.root, executable)
                    except Exception as exc:
                        self.warning = f"The project was created, but its Desktop shortcut could not be created.\n\n{exc}"
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__


def _workbench_icon():
    from GUI.application_icon import workbench_icon
    return workbench_icon()


class ProjectNameDialog(QtWidgets.QDialog):
    """Collect the new project's name and creation options in one window."""

    def __init__(self, parent=None, *, sanitize_name):
        super().__init__(parent)
        self.setWindowTitle("Create project")
        self.setMinimumWidth(280)
        self._sanitize_name = sanitize_name
        self.project_name = ""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        label = QtWidgets.QLabel("Project name:")
        self.name_edit = QtWidgets.QLineEdit()
        label.setBuddy(self.name_edit)
        layout.addWidget(label)
        layout.addWidget(self.name_edit)

        options = QtWidgets.QHBoxLayout()
        self.build_exe_chk = QtWidgets.QCheckBox("Copy .exe")
        self.build_exe_chk.setChecked(True)
        self.build_exe_chk.setToolTip("Copy the prebuilt Windows application into the new project (no compilation)")
        self.create_shortcut_chk = QtWidgets.QCheckBox("Create shortcut")
        self.create_shortcut_chk.setChecked(True)
        self.create_shortcut_chk.setToolTip("Create a Desktop shortcut named NWB Workbench - <project name>")
        options.addWidget(self.build_exe_chk)
        options.addWidget(self.create_shortcut_chk)
        options.addStretch(1)
        layout.addLayout(options)
        self.build_exe_chk.toggled.connect(self._update_options)

        tabs_box = QtWidgets.QGroupBox("Project tabs")
        tabs_layout = QtWidgets.QVBoxLayout(tabs_box)
        core = QtWidgets.QCheckBox("Content (required)")
        core.setChecked(True)
        core.setEnabled(False)
        tabs_layout.addWidget(core)
        self.tab_checkboxes = {}
        for definition in TAB_DEFINITIONS:
            checkbox = QtWidgets.QCheckBox(definition["title"])
            checkbox.setChecked(bool(definition.get("default", True)))
            tabs_layout.addWidget(checkbox)
            self.tab_checkboxes[definition["id"]] = checkbox
        layout.addWidget(tabs_box)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.setCenterButtons(True)
        self.create_btn = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        self.create_btn.setText("Create project")
        self.create_btn.setDefault(True)
        self.create_btn.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_edit.textChanged.connect(self._update_create_button)

    def selected_tabs(self):
        return ["content"] + [key for key, checkbox in self.tab_checkboxes.items() if checkbox.isChecked()]

    def _update_options(self, build_exe):
        if not build_exe:
            self.create_shortcut_chk.setChecked(False)
        self.create_shortcut_chk.setEnabled(build_exe)

    def _update_create_button(self, *_args):
        self.create_btn.setEnabled(bool(self._sanitize_name(self.name_edit.text())))

    def accept(self):
        name = self._sanitize_name(self.name_edit.text())
        if not name:
            self.name_edit.setFocus()
            return
        self.project_name = name
        super().accept()


class ProjectLauncher(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        from GUI.application_icon import install_window_icon
        install_window_icon(self)
        self.setMinimumWidth(360)
        self.project_root = None
        self._creation_thread = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title_row = QtWidgets.QHBoxLayout()
        icon = self.windowIcon()
        if not icon.isNull():
            logo = QtWidgets.QLabel()
            logo.setPixmap(icon.pixmap(40, 40))
            logo.setFixedSize(40, 40)
            title_row.addWidget(logo)
        title = QtWidgets.QLabel(APP_NAME)
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        button_row = QtWidgets.QHBoxLayout()
        button_row.setSpacing(10)
        layout.addLayout(button_row)

        self.open_btn = QtWidgets.QPushButton("Open project")
        self.create_btn = QtWidgets.QPushButton("Create project")
        button_row.addWidget(self.open_btn)
        button_row.addWidget(self.create_btn)

        self.open_btn.clicked.connect(self.open_project_browser)
        self.create_btn.clicked.connect(self.create_project_browser)

        self.creation_status = QtWidgets.QLabel()
        self.creation_status.setWordWrap(True)
        self.creation_status.hide()
        layout.addWidget(self.creation_status)
        self.creation_progress = QtWidgets.QProgressBar()
        self.creation_progress.setRange(0, 0)
        self.creation_progress.setTextVisible(False)
        self.creation_progress.hide()
        layout.addWidget(self.creation_progress)

    def reject(self):
        # Escape must not destroy a running worker or interrupt a runtime copy.
        if self._creation_thread is None:
            super().reject()

    def closeEvent(self, event):
        if self._creation_thread is not None:
            event.ignore()
        else:
            super().closeEvent(event)

    def _start_project_creation(self, root, *, build_exe=True, create_shortcut=False, enabled_tabs=None):
        if self._creation_thread is not None:
            return
        self.open_btn.setEnabled(False)
        self.create_btn.setEnabled(False)
        self.creation_status.setText("Creating project…")
        self.creation_status.show()
        self.creation_progress.show()
        worker = _ProjectCreationThread(
            root, self,
            build_exe=build_exe,
            create_shortcut=create_shortcut,
            enabled_tabs=enabled_tabs,
        )
        self._creation_thread = worker
        worker.stage_changed.connect(self.creation_status.setText)
        worker.finished.connect(self._project_creation_finished)
        worker.start()

    def _project_creation_finished(self):
        worker = self._creation_thread
        if worker is None:
            return
        root, error = worker.root, worker.error
        warning = worker.warning
        self._creation_thread = None
        worker.deleteLater()
        self.creation_progress.hide()
        self.creation_status.hide()
        self.open_btn.setEnabled(True)
        self.create_btn.setEnabled(True)
        if error is not None:
            QtWidgets.QMessageBox.critical(self, "Project creation failed", error)
            return
        if warning:
            QtWidgets.QMessageBox.warning(self, "Desktop shortcut", warning)
        self._open_project(root)

    def _open_project(self, root: Path):
        from GUI.frozen_project import configure_project_paths
        configure_project_paths(root)
        self.project_root = Path(root)
        self.accept()

    def open_project_browser(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Select an NWB Workbench project folder")
        if not folder:
            return

        root = Path(folder).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            QtWidgets.QMessageBox.critical(self, "Invalid folder", "Please select a valid project folder.")
            return

        try:
            create_or_update_project_structure(root, create_inventory=False)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Could not open project", str(exc))
            return

        self._open_project(root)

    @staticmethod
    def _sanitize_project_name(name: str) -> str:
        name = str(name or "").strip()
        forbidden = '<>:"/\\|?*'
        cleaned = "".join("_" if ch in forbidden else ch for ch in name)
        cleaned = " ".join(cleaned.split())
        cleaned = cleaned.strip(" ._")
        return cleaned

    def create_project_browser(self):
        parent_folder = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select where to create the project",
        )
        if not parent_folder:
            return

        dialog = ProjectNameDialog(self, sanitize_name=self._sanitize_project_name)
        result = dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()
        if result != QtWidgets.QDialog.Accepted:
            return
        root = Path(parent_folder).expanduser().resolve() / dialog.project_name
        self._start_project_creation(
            root,
            build_exe=dialog.build_exe_chk.isChecked(),
            create_shortcut=dialog.create_shortcut_chk.isChecked(),
            enabled_tabs=dialog.selected_tabs(),
        )



def _configure_spike_sorting_compute(*, multiprocessing=True, n_jobs=None):
    """Store appLauncher spike-sorting compute defaults for child panels.

    Parameters
    ----------
    multiprocessing : bool, default True
        Enables SpikeInterface multiprocessing where supported.
    n_jobs : int or None, default None
        If None, spike-sorting code uses all CPU cores except one, with one
        worker as the minimum. Explicit values are clamped to at least one.
    """
    os.environ["NWB_SPIKE_SORTING_MULTIPROCESSING"] = "1" if bool(multiprocessing) else "0"

    if n_jobs is None:
        os.environ.pop("NWB_SPIKE_SORTING_N_JOBS", None)
        return

    try:
        value = max(1, int(n_jobs))
    except Exception:
        value = 1
    os.environ["NWB_SPIKE_SORTING_N_JOBS"] = str(value)

def _apply_light_theme(app):
    """Keep the Workbench palette readable regardless of the OS color scheme."""
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme") and hasattr(QtCore.Qt, "ColorScheme"):
        hints.setColorScheme(QtCore.Qt.ColorScheme.Light)

    # Fusion uses the explicit palette consistently, including on dark Windows.
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    roles = QtGui.QPalette.ColorRole
    groups = QtGui.QPalette.ColorGroup
    colors = {
        "Window": "#ffffff",
        "WindowText": "#202020",
        "Base": "#ffffff",
        "AlternateBase": "#f2f5f8",
        "Text": "#202020",
        "Button": "#f0f0f0",
        "ButtonText": "#202020",
        "ToolTipBase": "#fffbe6",
        "ToolTipText": "#202020",
        "BrightText": "#b00020",
        # A pale selection also keeps explicitly colored tree/table text legible.
        "Highlight": "#cce4ff",
        "HighlightedText": "#202020",
        "Link": "#005ab5",
        "LinkVisited": "#663399",
        "Light": "#ffffff",
        "Midlight": "#f5f5f5",
        "Mid": "#b8b8b8",
        "Dark": "#808080",
        "Shadow": "#606060",
        "PlaceholderText": "#666666",
        "Accent": "#005ab5",
    }
    for group in (groups.Active, groups.Inactive, groups.Disabled):
        for name, color in colors.items():
            role = getattr(roles, name, None)
            if role is not None:
                palette.setColor(group, role, QtGui.QColor(color))
    for name in ("WindowText", "Text", "ButtonText", "PlaceholderText"):
        role = getattr(roles, name, None)
        if role is not None:
            palette.setColor(groups.Disabled, role, QtGui.QColor("#666666"))
    app.setPalette(palette)


def appLauncher(project_root_path=None, *, multiprocessing=True, n_jobs=None):
    if _gui_autolaunch_suppressed():
        return 0

    _configure_spike_sorting_compute(multiprocessing=multiprocessing, n_jobs=n_jobs)

    from GUI.application_icon import set_process_identity
    set_process_identity()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    _apply_light_theme(app)
    icon = _workbench_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    try:
        app.setApplicationName(APP_NAME)
    except Exception:
        pass

    if project_root_path is not None and os.path.exists(str(project_root_path)):
        root = Path(project_root_path)
        from GUI.frozen_project import configure_project_paths
        configure_project_paths(root)
        try:
            create_or_update_project_structure(root, create_inventory=False)
        except Exception:
            pass
        win = NWBApp(project_root=root)
        finish_splash_when_visible(win)
        win.show()
        return exec_app(app)

    launcher = ProjectLauncher()
    finish_splash_when_visible(launcher)
    try:
        accepted = QtWidgets.QDialog.DialogCode.Accepted
    except AttributeError:
        accepted = QtWidgets.QDialog.Accepted
    if launcher.exec() == accepted and launcher.project_root is not None:
        win = NWBApp(project_root=launcher.project_root)
        finish_splash_when_visible(win)
        win.show()
        return exec_app(app)
    return 0


if __name__ == "__main__":
    if _gui_autolaunch_suppressed():
        sys.exit(0)
    sys.exit(appLauncher(sys.argv[1] if len(sys.argv) > 1 else None))
