from __future__ import annotations

"""
Small Qt binding compatibility layer.

The Qt rewrite is written against QtWidgets/QtCore/QtGui APIs common to
PySide6 and PyQt5. Prefer PySide6 for a new application, but fall back to
PyQt5 because many scientific environments already have it installed.
"""

import os
import sys

# Reuse the binding already loaded by an interactive host such as Spyder.
# Importing a second binding in the same process is unsafe. Frozen builds
# always use the PySide6 runtime included by build_windows.py.
QT_API = None
_frozen = bool(getattr(sys, "frozen", False))
_loaded = [name for name in ("PySide6", "PyQt5", "PyQt6", "PySide2")
           if sys.modules.get(name + ".QtCore") is not None]
if len(_loaded) > 1 or (_loaded and _loaded[0] not in ("PySide6", "PyQt5")):
    raise ImportError(
        "NWB Workbench requires PySide6 or PyQt5, but this session already loaded "
        + ", ".join(_loaded) + ". Launch the script in a fresh Python process."
    )
if _frozen and _loaded == ["PyQt5"]:
    raise ImportError("The executable requires its bundled PySide6 runtime, but PyQt5 is already loaded.")

if not _frozen and _loaded == ["PyQt5"]:
    from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore
    QT_API = "PyQt5"
else:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        QT_API = "PySide6"
    except ModuleNotFoundError as exc:
        if _frozen or exc.name != "PySide6":
            raise ImportError(
                "PySide6 or one of its dependencies could not be imported. "
                f"Original error: {exc}"
            ) from exc
        from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore
        QT_API = "PyQt5"
    except ImportError as exc:
        diagnostic = getattr(sys, "_nwb_qt_bootstrap_error", "")
        advice = ("Rebuild with the updated build_windows.py. " if _frozen else
                  "Restart the Python kernel or launch the script in a fresh Python process. ")
        raise ImportError(
            "PySide6 could not be imported. " + advice
            + f"Original error: {exc}. {diagnostic}"
        ) from exc

# Match downstream plotting libraries to the binding actually selected.
os.environ["PYQTGRAPH_QT_LIB"] = QT_API
os.environ["QT_API"] = QT_API.lower()

def _configure_windows_platform_plugins():
    """Use plugins belonging to this PySide6 installation before QApplication.

    Spyder/Conda can make QLibraryInfo point at site-packages/plugins rather
    than PySide6/plugins. Frozen builds already have their own runtime hook.
    An existing Qt application owns its plugin configuration.
    """
    if sys.platform != "win32" or _frozen or QT_API != "PySide6":
        return
    if QtCore.QCoreApplication.instance() is not None:
        return
    from pathlib import Path
    package = Path(sys.modules["PySide6"].__file__).resolve().parent
    candidates = [package / "plugins", package / "Qt" / "plugins"]
    plugins = next((p for p in candidates if (p / "platforms" / "qwindows.dll").is_file()), None)
    if plugins is None:
        raise ImportError(
            "PySide6's Windows platform plugin qwindows.dll is missing. "
            "Expected it under " + str(package / "plugins" / "platforms")
            + ". Repair PySide6 in this Python environment: " + sys.executable
        )
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugins / "platforms")
    QtCore.QCoreApplication.setLibraryPaths([str(plugins)])


_configure_windows_platform_plugins()


# Normalize signal name across bindings.
if not hasattr(QtCore, "Signal") and hasattr(QtCore, "pyqtSignal"):
    QtCore.Signal = QtCore.pyqtSignal  # type: ignore[attr-defined]


def exec_app(app: QtWidgets.QApplication) -> int:
    """Qt5 uses exec_(), Qt6 uses exec()."""
    if hasattr(app, "exec"):
        return int(app.exec())
    return int(app.exec_())


def qt_user_role() -> int:
    return int(QtCore.Qt.ItemDataRole.UserRole) if hasattr(QtCore.Qt, "ItemDataRole") else int(QtCore.Qt.UserRole)


def qt_checked():
    return QtCore.Qt.CheckState.Checked if hasattr(QtCore.Qt, "CheckState") else QtCore.Qt.Checked


def qt_unchecked():
    return QtCore.Qt.CheckState.Unchecked if hasattr(QtCore.Qt, "CheckState") else QtCore.Qt.Unchecked


def qt_horizontal():
    return QtCore.Qt.Orientation.Horizontal if hasattr(QtCore.Qt, "Orientation") else QtCore.Qt.Horizontal


def qt_vertical():
    return QtCore.Qt.Orientation.Vertical if hasattr(QtCore.Qt, "Orientation") else QtCore.Qt.Vertical


def qt_align_center():
    return QtCore.Qt.AlignmentFlag.AlignCenter if hasattr(QtCore.Qt, "AlignmentFlag") else QtCore.Qt.AlignCenter


def set_item_foreground(item, color: str):
    brush = QtGui.QBrush(QtGui.QColor(color))
    try:
        item.setForeground(0, brush)
    except Exception:
        pass
