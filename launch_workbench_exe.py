"""Windows executable entry point; also usable from normal Python."""
import os
import sys


def _launch():
    import argparse
    from pathlib import Path

    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    os.environ["QT_API"] = "pyside6"
    os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
    parser = argparse.ArgumentParser(description="NWB Workbench")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--launcher", action="store_true", help="Show Open/Create project")
    parser.add_argument("--check-runtime", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_runtime is not None:
        import json
        import traceback
        try:
            import PySide6
            from GUI.qt_compat import QtCore, QtWidgets
            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
            app.processEvents()
            report = {"ok": True, "pyside_version": PySide6.__version__, "qt_version": QtCore.qVersion()}
        except Exception:
            report = {"ok": False, "error": traceback.format_exc(),
                      "dll_bootstrap": getattr(sys, "_nwb_qt_bootstrap_error", "")}
        args.check_runtime.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 0 if report["ok"] else 2
    root = args.project
    if root is None and not args.launcher and getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent
        if (candidate / "nwb_workbench_project.json").is_file():
            root = candidate
    if root is not None and not root.is_dir():
        parser.error(f"Project directory does not exist: {root}")
    # Create Qt before the scientific/GUI imports, retaining the application
    # for the entire launch. freeze_support() has already handled workers.
    from GUI.qt_compat import QtWidgets
    from GUI.startup_splash import show_startup_splash
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    show_startup_splash(app)
    from GUI.project_launcher import appLauncher
    return appLauncher(str(root.resolve()) if root is not None else None)


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    from GUI.startup_splash import close_startup_splash
    try:
        return _launch()
    finally:
        # Also remove the logo on invalid arguments, cancellation, or errors.
        close_startup_splash()


if __name__ == "__main__":
    raise SystemExit(main())
