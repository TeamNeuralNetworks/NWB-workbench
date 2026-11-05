"""Project-local paths and non-overwriting deployment for the Windows bundle."""
from GUI.project_paths import json_read as _pp_json_read
from pathlib import Path
import shutil
import sys


def configure_project_paths(root):
    root = Path(root).resolve()
    # Preserve bundled application modules ahead of editable project helpers.
    for path in (root, root / "Script", root / "Script" / "Utilities"):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.append(str(path))


def _copy_runtime_tree(source, destination, project):
    """Copy independent files, using Windows' threaded copier when available."""
    import subprocess

    robocopy = shutil.which("robocopy.exe") if sys.platform == "win32" else None
    if robocopy is None:
        shutil.copytree(source, destination)
        return
    log = project / "Logs" / "copy_application.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # No mirroring, shared links, or unbounded retries. The destination is an
    # isolated staging directory; the executable is installed only on success.
    command = [robocopy, str(source), str(destination), "/E", "/MT:8",
               "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1",
               "/NFL", "/NDL", "/NP", "/UNILOG:" + str(log)]
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Robocopy uses 0 through 7 for nonfailure results, unlike most programs.
    if result.returncode < 0 or result.returncode >= 8:
        raise OSError(
            f"Application copy failed (Robocopy code {result.returncode}). "
            f"See {log}"
        )
    if not destination.is_dir():
        raise OSError(f"Application copy did not create its destination. See {log}")


def _copy_runtime(source, project, executable_name="NWB Workbench.exe"):
    """Stage the copy before installing it; never merge or overwrite runtimes."""
    import tempfile

    source, project = Path(source).resolve(), Path(project).resolve()
    if source == project:
        return project / executable_name
    if not (source / executable_name).is_file() or not (source / "_internal").is_dir():
        raise FileNotFoundError(f"Incomplete application bundle: {source}")
    names = ("_internal", executable_name)
    for name in names:
        target = project / name
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"The project already contains a runtime: {target}. Open the existing project instead.")
    stage = Path(tempfile.mkdtemp(prefix="runtime-install-", dir=project))
    installed = []
    try:
        _copy_runtime_tree(source / "_internal", stage / "_internal", project)
        shutil.copy2(source / executable_name, stage / executable_name)
        for name in names:
            (stage / name).rename(project / name)
            installed.append(name)
    except Exception:
        for name in reversed(installed):
            (project / name).rename(stage / name)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return project / executable_name


def install_bundle(root):
    """Copy the running executable's complete runtime into a new project."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    return _copy_runtime(executable.parent, root, executable.name)


def prepare_project_application(root, *, progress=None):
    """Copy a prebuilt application. Project creation never invokes a compiler."""
    import json

    root = Path(root).resolve()
    if getattr(sys, "frozen", False):
        if progress is not None:
            progress("Copying the application into the project…")
        return install_bundle(root)
    if sys.platform != "win32":
        return None
    source_root = Path(__file__).resolve().parent.parent
    bundle = source_root / "dist" / "NWB Workbench"
    report = bundle / "runtime_check.json"
    try:
        verified = _pp_json_read(report).get("ok") is True
    except (OSError, ValueError, AttributeError):
        verified = False
    if not verified or not (bundle / "NWB Workbench.exe").is_file() or not (bundle / "_internal").is_dir():
        raise FileNotFoundError(
            f"No verified reusable application bundle was found in {bundle}.\n\n"
            "Build it once from the NWB Python environment, then retry:\n"
            f'python "{source_root / "build_windows.py"}"'
        )
    if progress is not None:
        progress("Copying the prebuilt application into the project…")
    return _copy_runtime(bundle, root)


def create_project_shortcut(root, executable):
    """Create a Windows Desktop .lnk pointing to this project's executable."""
    if sys.platform != "win32":
        raise RuntimeError("Desktop .exe shortcuts require Windows.")
    if executable is None:
        raise RuntimeError("No project executable was created.")
    root = Path(root).resolve()
    executable = Path(executable).resolve()
    if executable.parent != root or not executable.is_file():
        raise FileNotFoundError("The shortcut target must be the executable inside the project.")

    import base64
    import os
    import subprocess

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        raise RuntimeError("PowerShell could not be found to create the Desktop shortcut.")
    # Pass names and paths as environment data, never as executable script text.
    project_name = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch
                           for ch in root.name).strip(" .") or "Project"
    env = os.environ.copy()
    env.update({"NWB_SHORTCUT_TARGET": str(executable),
                "NWB_SHORTCUT_PROJECT": str(root),
                "NWB_SHORTCUT_NAME": "NWB Workbench - " + project_name})
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject WScript.Shell
$desktop = $shell.SpecialFolders.Item('Desktop')
if (-not $desktop) { throw 'Windows could not locate the Desktop folder.' }
$name = $env:NWB_SHORTCUT_NAME
$path = Join-Path $desktop ($name + '.lnk')
$number = 2
while (Test-Path -LiteralPath $path) {
    $path = Join-Path $desktop ($name + ' (' + $number + ').lnk')
    $number++
}
$link = $shell.CreateShortcut($path)
$link.TargetPath = $env:NWB_SHORTCUT_TARGET
$link.Arguments = '--project "' + $env:NWB_SHORTCUT_PROJECT + '"'
$link.WorkingDirectory = $env:NWB_SHORTCUT_PROJECT
$link.IconLocation = $env:NWB_SHORTCUT_TARGET + ',0'
$link.Description = $env:NWB_SHORTCUT_NAME
$link.Save()
if (-not (Test-Path -LiteralPath $path)) { throw 'The Desktop shortcut was not saved.' }
[Console]::WriteLine($path)
'''
    command = [powershell, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
               base64.b64encode(script.encode("utf-16le")).decode("ascii")]
    try:
        result = subprocess.run(
            command, env=env, check=True, capture_output=True,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"Windows could not create the Desktop shortcut.\n{detail}") from exc
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Windows did not return a Desktop shortcut path.")
    shortcut = Path(output.splitlines()[-1])
    if not shortcut.is_file():
        raise RuntimeError(f"The Desktop shortcut could not be found: {shortcut}")
    return shortcut
