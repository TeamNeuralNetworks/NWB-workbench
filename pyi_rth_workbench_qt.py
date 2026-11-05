"""Prefer the bundle's own Qt DLLs before PyInstaller imports its Qt hook."""
import os
import sys
from pathlib import Path


def configure_qt_dlls():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes

    base = Path(sys._MEIPASS)
    candidates = [base / "PySide6", base / "PySide6" / "Qt" / "bin"]
    vendor = next((p for p in candidates if (p / "Qt6Core.dll").is_file()), None)
    if vendor is None:
        sys._nwb_qt_bootstrap_error = "The bundle contains no canonical PySide6/Qt6Core.dll."
        return
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML_IMPORT_PATH", "QML2_IMPORT_PATH"):
        os.environ.pop(key, None)
    os.environ["QT_API"] = "pyside6"
    os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
    os.environ["PATH"] = str(vendor) + os.pathsep + os.environ.get("PATH", "")
    # Retain handles for the lifetime of the process. Preload exact DLL paths
    # so a different Qt6Core from Conda/PATH cannot win later imports.
    handles = []
    try:
        if hasattr(os, "add_dll_directory"):
            for directory in (vendor, base / "PySide6", base / "shiboken6"):
                if directory.is_dir():
                    handles.append(os.add_dll_directory(str(directory)))
        required = [vendor / name for name in ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll")]
        for path in required:
            if not path.is_file():
                raise FileNotFoundError(f"Missing bundled Qt DLL: {path}")
        handles.append(ctypes.WinDLL(str(required[0])))
        for directory, stem in ((base / "shiboken6", "shiboken6"), (base / "PySide6", "pyside6")):
            for name in (stem + ".abi3.dll", stem + ".dll"):
                path = directory / name
                if path.is_file():
                    handles.append(ctypes.WinDLL(str(path)))
                    break
        for path in required[1:]:
            handles.append(ctypes.WinDLL(str(path)))
    except OSError as exc:
        sys._nwb_qt_bootstrap_error = f"Failed to preload bundled Qt DLLs from {vendor}: {exc}"
    sys._nwb_qt_dll_handles = handles


configure_qt_dlls()
