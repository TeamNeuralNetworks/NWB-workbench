"""A per-pixel transparent Qt startup logo, closed after the first GUI paint.

Qt is imported only when showing the splash, after multiprocessing's
freeze_support(). No Tk splash, color key, or binary window mask is used.
"""
from __future__ import annotations

from pathlib import Path

_splash = None
_watcher = None


def close_startup_splash():
    global _splash, _watcher
    if _watcher is not None:
        watcher, _watcher = _watcher, None
        watcher.app.removeEventFilter(watcher)
        watcher.deleteLater()
    if _splash is not None:
        splash, _splash = _splash, None
        splash.close()
        splash.deleteLater()


def show_startup_splash(app):
    """Show once on the main GUI thread, before importing the application."""
    global _splash, _watcher
    if _splash is not None:
        return _splash
    from GUI.qt_compat import QtCore, QtGui, QtWidgets

    logo = QtGui.QPixmap(str(Path(__file__).resolve().parent / "assets" /
                               "nwb_explorer_logo.png"))
    if logo.isNull():
        return None  # A missing cosmetic asset must not prevent startup.
    screen = app.screenAt(QtGui.QCursor.pos()) or app.primaryScreen()
    bounds = screen.availableGeometry()
    max_width = min(560, int(bounds.width() * 0.7))
    max_height = min(460, int(bounds.height() * 0.7))
    size = logo.size().scaled(max_width, max_height,
                             QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    ratio = screen.devicePixelRatio()
    # Keep physical pixels for high-DPI monitors and preserve partial alpha.
    logo = logo.scaled(round(size.width() * ratio), round(size.height() * ratio),
                       QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                       QtCore.Qt.TransformationMode.SmoothTransformation)
    logo.setDevicePixelRatio(ratio)

    class LogoWindow(QtWidgets.QWidget):
        def __init__(self):
            super().__init__(None, QtCore.Qt.WindowType.SplashScreen |
                             QtCore.Qt.WindowType.FramelessWindowHint)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(QtCore.Qt.WidgetAttribute.WA_QuitOnClose, False)
            self.setAutoFillBackground(False)
            self.setWindowTitle("NWB Workbench")
            self.setWindowIcon(QtGui.QIcon(logo))
            self.setFixedSize(size)
            self.move(bounds.center() - self.rect().center())

        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(self.rect(), logo)

    class FirstPaintFilter(QtCore.QObject):
        def __init__(self):
            super().__init__(app)
            self.app = app
            self.target = None
            self.pending = False

        def eventFilter(self, watched, event):
            # Startup error dialogs also release the splash. Never react to
            # the splash's own Paint event or unrelated child widgets.
            if (not self.pending and event.type() == QtCore.QEvent.Type.Paint
                    and isinstance(watched, QtWidgets.QWidget)
                    and watched is not _splash and watched.isWindow()
                    and watched.isVisible()
                    and (watched is self.target or watched.isModal())):
                self.pending = True
                self.painted_window = watched
                QtCore.QTimer.singleShot(0, self.finish)
            return False

        def finish(self):
            target = self.app.activeModalWidget() or self.painted_window
            close_startup_splash()
            try:
                if target.isVisible():
                    target.raise_()
                    target.activateWindow()
            except RuntimeError:
                pass

    _splash = LogoWindow()
    _watcher = FirstPaintFilter()
    app.installEventFilter(_watcher)
    _splash.show()
    app.processEvents()  # Paint now, before the slower application imports.
    return _splash


def finish_splash_when_visible(window):
    """Arm closure for the launcher/main window before its show()/exec()."""
    if _watcher is not None:
        _watcher.target = window
        # Also support callers attaching after a window was already shown.
        if window.isVisible():
            window.update()
