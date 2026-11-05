"""Run with the Windows Python environment that already runs NWB Workbench."""
import argparse
import ast
import importlib.util
from pathlib import Path
import subprocess
import sys
import os
import shutil
import tempfile


def update_project_runtime(distribution, project):
    """Stage a complete runtime, then replace only the exe and _internal.

    Existing runtime files are retained in a unique backup directory. On a
    handled replacement failure, restore the originals; never merge runtimes.
    """
    distribution, project = Path(distribution).resolve(), Path(project).resolve()
    names = ("_internal", "NWB Workbench.exe")
    if distribution == project:
        raise ValueError("The project must be separate from the build output.")
    if not (project / "Script").is_dir():
        raise ValueError(f"Not a project folder (missing Script): {project}")
    if not (distribution / names[0]).is_dir() or not (distribution / names[1]).is_file():
        raise ValueError("Build output must contain the exe and complete _internal folder.")
    for name in names:
        target = project / name
        if target.is_symlink():
            raise ValueError(f"Refusing to replace linked runtime path: {target}")
    stage = Path(tempfile.mkdtemp(prefix="runtime-update-", dir=project))
    backup = None
    moved, installed = [], []
    try:
        print("Preparing new runtime; existing project files remain in place.", flush=True)
        shutil.copytree(distribution / names[0], stage / names[0])
        shutil.copy2(distribution / names[1], stage / names[1])
        backup = Path(tempfile.mkdtemp(prefix="runtime-backup-", dir=project))
        # Move the old exe first: Windows normally rejects this while it runs.
        for name in reversed(names):
            if (project / name).exists():
                (project / name).rename(backup / name)
                moved.append(name)
        for name in names:
            (stage / name).rename(project / name)
            installed.append(name)
    except Exception as exc:
        try:
            for name in reversed(installed):
                (project / name).rename(stage / name)
            for name in reversed(moved):
                (backup / name).rename(project / name)
        except Exception as rollback_error:
            raise RuntimeError(
                f"Update and automatic restore failed. Close NWB Workbench. "
                f"Recovery files are in {backup} and {stage}. "
                f"Original error: {exc}; restore error: {rollback_error}"
            ) from exc
        raise RuntimeError(
            f"Runtime update failed; previous runtime restored where present. "
            f"Close NWB Workbench and retry. Staged files: {stage}. Error: {exc}"
        ) from exc
    else:
        stage.rmdir()  # Empty after the two successful moves.
        if not moved:
            backup.rmdir()
            backup = None
        print(f"Project runtime updated: {project}")
        if backup:
            print(f"Previous runtime retained in: {backup}")
    return backup




def imported_modules(folder):
    found = set()
    for path in folder.rglob("*.py"):
        if any(p in {"__pycache__", ".git"} for p in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeError) as exc:
            print(f"Cannot scan {path}: {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module)
    return found



def inspect_qt_environment():
    """Check Qt in a fresh process and locate its authoritative Windows DLLs."""
    import json
    code = r'''
import json
from pathlib import Path
import PySide6, shiboken6
from PySide6 import QtCore, QtGui, QtWidgets
if PySide6.__version__ != shiboken6.__version__:
    raise RuntimeError('PySide6 and shiboken6 versions differ: ' + PySide6.__version__ + ' / ' + shiboken6.__version__)
if PySide6.__version__.split('.')[:2] != QtCore.qVersion().split('.')[:2]:
    raise RuntimeError('The loaded Qt version does not match PySide6: ' + QtCore.qVersion())
names = ['Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Widgets.dll']
paths = [Path(PySide6.__file__).parent, Path(QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.BinariesPath))]
vendor = next((p for p in paths if all((p / name).is_file() for name in names)), None)
if vendor is None:
    raise RuntimeError('Cannot locate a complete set of PySide6 Qt DLLs: ' + str(paths))
binding_dlls = []
for directory, stem, package_path in [('PySide6', 'pyside6', Path(PySide6.__file__).parent), ('shiboken6', 'shiboken6', Path(shiboken6.__file__).parent)]:
    for name in (stem + '.abi3.dll', stem + '.dll'):
        path = next((folder / name for folder in (package_path, vendor) if (folder / name).is_file()), None)
        if path is not None:
            binding_dlls.append((str(path), directory))
            break
print(json.dumps({'pyside': PySide6.__version__, 'qt': QtCore.qVersion(), 'dlls': [str(vendor / name) for name in names], 'binding_dlls': binding_dlls}))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(
            "The build environment cannot import a consistent PySide6 runtime. "
            "Use matching PySide6 and shiboken6 packages in the NWB environment.\n"
            + (result.stderr or result.stdout)
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def verify_runtime(distribution):
    """Launch the built Qt runtime before allowing the bundle to be reused."""
    import json
    distribution = Path(distribution)
    report_path = distribution / "runtime_check.json"
    env = os.environ.copy()
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML_IMPORT_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM"):
        env.pop(key, None)
    try:
        result = subprocess.run(
            [str(distribution / "NWB Workbench.exe"), "--check-runtime", str(report_path)],
            cwd=distribution, env=env, capture_output=True, text=True,
            errors="replace", timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"The built Qt runtime did not start within 60 seconds. Build output: {distribution}") from exc
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    if result.returncode or report.get("ok") is not True:
        detail = report.get("error") or result.stderr or result.stdout or f"Exit code: {result.returncode}"
        raise RuntimeError(f"The executable failed its Qt startup check. It will not replace the reusable bundle.\n{detail}\nBuild output: {distribution}")
    return report


def publish_bundle(distribution, destination):
    """Swap the verified bundle into dist; restore the old one if the swap fails."""
    distribution, destination = Path(distribution), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError(f"Refusing to replace an unexpected bundle path: {destination}")
    backup = None
    if destination.exists():
        backup = Path(tempfile.mkdtemp(prefix=".previous-workbench-", dir=destination.parent))
        try:
            destination.rename(backup / destination.name)
        except Exception:
            backup.rmdir()
            raise
    try:
        distribution.rename(destination)
    except Exception:
        if backup is not None:
            (backup / destination.name).rename(destination)
            backup.rmdir()
        raise
    if backup is not None:
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            print(f"New bundle ready; the old bundle could not be fully removed: {backup}: {exc}", flush=True)


def create_package_shortcut(root, distribution):
    """Create/update the package launcher link after publishing a verified build."""
    root, distribution = Path(root).resolve(), Path(distribution).resolve()
    executable = distribution / "NWB Workbench.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"Cannot create launcher shortcut: {executable}")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                         "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    shortcut = root / "NWB Workbench.lnk"
    # Stage separately so a failed shortcut creation retains the previous link.
    with tempfile.TemporaryDirectory(prefix=".workbench-shortcut-", dir=root) as temporary:
        staged = Path(temporary) / shortcut.name
        environment = os.environ.copy()
        environment.update(NWB_SHORTCUT_FILE=str(staged),
                           NWB_SHORTCUT_TARGET=str(executable),
                           NWB_SHORTCUT_WORKDIR=str(root))
        # Values travel through the environment, never as PowerShell source.
        script = r'''$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:NWB_SHORTCUT_FILE)
$link.TargetPath = $env:NWB_SHORTCUT_TARGET
$link.Arguments = ''
$link.WorkingDirectory = $env:NWB_SHORTCUT_WORKDIR
$link.IconLocation = $env:NWB_SHORTCUT_TARGET + ',0'
$link.Description = 'Open NWB Workbench to create or open a project'
$link.WindowStyle = 1
$link.Save()
'''
        subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                       env=environment, check=True, capture_output=True, timeout=30)
        if not staged.is_file():
            raise RuntimeError("Windows did not create the Workbench shortcut.")
        os.replace(staged, shortcut)
    print(f"Package launcher shortcut ready: {shortcut}", flush=True)
    return shortcut


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, action="append", default=[],
                        help="Project whose Script imports will be scanned; repeat for multiple projects")
    parser.add_argument("--update-project", action="store_true",
                        help="After building, replace the runtime in every --project folder")
    parser.add_argument("--include-package", action="append", default=[],
                        help="Collect an installed Python package, including dynamic imports; repeat as needed")
    args = parser.parse_args()
    if args.update_project and not args.project:
        parser.error("--update-project requires at least one --project")
    if sys.platform != "win32":
        parser.error("Build this Windows executable on Windows.")
    os.environ["QT_API"] = "pyside6"
    os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
    root = Path(__file__).resolve().parent
    entry_point = root / "launch_workbench_exe.py"
    if not entry_point.is_file():
        parser.error(
            f"Missing executable entry-point script: {entry_point}. "
            "Restore launch_workbench_exe.py beside build_windows.py, then retry."
        )
    scripts = [project.resolve() / "Script" for project in args.project]
    for script in scripts:
        if not script.is_dir():
            parser.error(f"Missing project Script directory: {script}")
    for dependency in ("PyInstaller", "PySide6", "pynwb", "numpy", "pandas", "PIL", "astropy", "tensorpac", "av", "yaml", "openpyxl"):
        if importlib.util.find_spec(dependency) is None:
            install_name = {"PIL": "Pillow", "yaml": "PyYAML"}.get(dependency, dependency)
            parser.error(f"Missing build dependency: {install_name}. Install with python -m pip install {install_name}")
    icon = root / "GUI" / "assets" / "nwb_explorer_logo.png"
    if not icon.is_file():
        parser.error(f"Missing NWB Workbench logo: {icon}")
    executable_icon = icon.with_suffix(".ico")
    if not executable_icon.is_file():
        executable_icon = icon
    helper = root / "Tools" / "General" / "plotting" / "add_stats_on_boxplot.py"
    if not helper.is_file():
        parser.error(f"Missing application source file: {helper}")
    runtime_hook = root / "pyi_rth_workbench_qt.py"
    if not runtime_hook.is_file():
        parser.error(f"Missing Qt runtime hook: {runtime_hook}")
    qt_info = inspect_qt_environment()
    print(f"Building with PySide6 {qt_info['pyside']} / Qt {qt_info['qt']}", flush=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx",
        "--runtime-hook", str(runtime_hook),
        "--hidden-import", "PySide6.QtCore", "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtWidgets", "--collect-all", "shiboken6",
        "--onedir", "--windowed", "--name", "NWB Workbench",
        "--icon", str(executable_icon),
        "--paths", str(root),
        "--add-data", f"{root / 'GUI' / 'assets'};GUI/assets",
        "--add-data", f"{root / 'GUI' / 'dlc_worker.py'};GUI",
        "--add-data", f"{root / 'GUI' / 'project_paths.py'};GUI",
        "--add-data", f"{root / 'GUI' / 'dlc_storage.py'};GUI",
        ]
    command += ["--add-data", f"{root / 'GUI' / 'dlc_storage.py'};GUI"]
    for dll in qt_info["dlls"]:
        command += ["--add-binary", f"{dll};PySide6"]
    for dll, directory in qt_info.get("binding_dlls", []):
        command += ["--add-binary", f"{dll};{directory}"]
    command += ["--add-data", f"{root / 'Spike_sorting' / 'Preprocessing'};Spike_sorting/Preprocessing"]
    command += ["--collect-submodules", "Tools",
                "--hidden-import", "Tools.General.plotting.add_stats_on_boxplot",
                "--collect-all", "astropy", "--collect-all", "tensorpac", "--collect-all", "av"]
    for package in args.include_package:
        if not all(part.isidentifier() for part in package.split(".")):
            parser.error(f"Use a Python import name for --include-package: {package}")
        if package.split(".")[0] in {"PyQt5", "PyQt6", "PySide2"}:
            parser.error("This build uses PySide6; other Qt bindings cannot be included.")
        if importlib.util.find_spec(package) is None:
            parser.error(f"Package is not installed in the build environment: {package}")
        command += ["--collect-all", package]
    # Keep only one Qt binding, even when Spyder installed PyQt5 alongside it.
    for package in ("PyQt5", "PyQt6", "PySide2"):
        command += ["--exclude-module", package]
    for package in ("pynwb", "hdmf", "spikeinterface", "probeinterface"):
        if importlib.util.find_spec(package):
            command += ["--collect-all", package]
    # Include imports from external scripts without bundling the scripts themselves.
    modules = set().union(*(imported_modules(script) for script in scripts))
    application_roots = {"GUI", "Tools", "Builders", "Processings", "Analyses", "Spike_sorting"}
    for folder in sorted(application_roots):
        directory = root / folder
        if directory.is_dir():
            for path in directory.rglob("*.py"):
                # The DLC worker is raw data executed by a separate Python.
                if path == root / "GUI" / "dlc_worker.py":
                    continue
                parts = path.relative_to(root).with_suffix("").parts
                if all(part.isidentifier() for part in parts):
                    name = ".".join(parts)
                    if name.endswith(".__init__"):
                        name = name[:-9]
                    modules.add(name)
    external_roots = {p.stem for script in scripts for p in script.rglob("*.py")}
    external_roots.update(p.name for script in scripts for p in script.iterdir() if p.is_dir())
    for module in sorted(modules):
        top = module.split(".")[0]
        if (top in external_roots and top not in application_roots) or top in {"PyQt5", "PyQt6", "PySide2"}:
            continue
        if (root / top).exists() or importlib.util.find_spec(top) is not None:
            command += ["--hidden-import", module]
        else:
            print(f"WARNING: unavailable dependency {module}; scripts using it may fail.")
    work_parent = root / "build"
    work_parent.mkdir(exist_ok=True)
    build_directory = Path(tempfile.mkdtemp(prefix="workbench-", dir=work_parent))
    command += ["--distpath", str(build_directory / "dist"),
                "--workpath", str(build_directory / "work"),
                "--specpath", str(build_directory)]
    command.append(str(entry_point))
    print(f"Build work files: {build_directory}", flush=True)
    # Failed builds retain diagnostics and leave the previous reusable bundle intact.
    subprocess.run(command, cwd=root, check=True)
    distribution = build_directory / "dist" / "NWB Workbench"
    verify_runtime(distribution)
    destination = root / "dist" / "NWB Workbench"
    publish_bundle(distribution, destination)
    try:
        create_package_shortcut(root, destination)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"WARNING: build published, but the package shortcut could not be created: {exc}", flush=True)
    try:
        shutil.rmtree(build_directory)
        if not any(work_parent.iterdir()):
            work_parent.rmdir()
    except OSError as exc:
        print(f"Build succeeded; temporary files could not be fully removed: {build_directory}: {exc}", flush=True)
    print(f"Verified reusable bundle ready: {destination}. Keep this folder for new projects.", flush=True)
    if args.update_project:
        for project in dict.fromkeys(p.resolve() for p in args.project):
            update_project_runtime(destination, project)



if __name__ == "__main__":
    main()
