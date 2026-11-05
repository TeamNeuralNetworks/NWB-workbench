"""Shared launcher/main-window branding, including native Windows window icons."""
from pathlib import Path
import atexit
import sys
from GUI.qt_compat import QtCore,QtGui,QtWidgets

APP_ID='nwb.inventory.explorer'
_native_icons={}


def set_process_identity():
    if sys.platform.startswith('win'):
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError,OSError):pass


def icon_paths(project_root=None):
    roots=[Path(__file__).resolve().parent/'assets']
    if getattr(sys,'_MEIPASS',None):roots.append(Path(sys._MEIPASS)/'GUI'/'assets')
    if project_root is not None:roots.append(Path(project_root)/'GUI'/'assets')
    # Prefer the multi-resolution Windows icon and retain PNG as a fallback.
    return [folder/name for folder in roots for name in ('nwb_explorer_logo.ico','nwb_explorer_logo.png','logo.ico','logo.png')]


def workbench_icon(project_root=None):
    for path in icon_paths(project_root):
        if path.is_file():
            icon=QtGui.QIcon(str(path))
            if not icon.isNull():return icon
    return QtGui.QIcon()


def _release_native_icons():
    if not sys.platform.startswith('win'):return
    try:
        import ctypes
        from ctypes import wintypes
        destroy=ctypes.windll.user32.DestroyIcon;destroy.argtypes=[wintypes.HANDLE];destroy.restype=wintypes.BOOL
        for handle in _native_icons.values():destroy(handle)
        _native_icons.clear()
    except (AttributeError,OSError):pass


atexit.register(_release_native_icons)


def _apply_native_icons(window,project_root=None):
    if not sys.platform.startswith('win'):return
    import ctypes
    from ctypes import wintypes
    path=next((p for p in icon_paths(project_root) if p.suffix.lower()=='.ico' and p.is_file()),None)
    if path is None:return
    user32=ctypes.windll.user32
    load=user32.LoadImageW
    load.argtypes=[wintypes.HINSTANCE,wintypes.LPCWSTR,wintypes.UINT,ctypes.c_int,ctypes.c_int,wintypes.UINT]
    load.restype=wintypes.HANDLE
    send=user32.SendMessageW
    send.argtypes=[wintypes.HWND,wintypes.UINT,ctypes.c_size_t,ctypes.c_ssize_t];send.restype=ctypes.c_ssize_t
    hwnd=int(window.winId())
    for kind,size in ((0,16),(1,32)):
        key=(str(path),size)
        if key not in _native_icons:
            handle=load(None,str(path),1,size,size,0x10) # IMAGE_ICON, LR_LOADFROMFILE
            if handle:_native_icons[key]=handle
        if key in _native_icons:send(hwnd,0x80,kind,_native_icons[key]) # WM_SETICON


class _WindowIconGuard(QtCore.QObject):
    def __init__(self,window,project_root=None):
        super().__init__(window);self.window=window;self.project_root=project_root;self.pending=False
    def eventFilter(self,watched,event):
        if event.type() in (QtCore.QEvent.Type.Show,QtCore.QEvent.Type.WinIdChange) and not self.pending:
            self.pending=True;QtCore.QTimer.singleShot(0,self.apply)
        return False
    def apply(self):
        self.pending=False
        try:
            if self.window.isVisible():_apply_native_icons(self.window,self.project_root)
        except (RuntimeError,AttributeError,OSError):pass


def install_window_icon(window,project_root=None):
    set_process_identity()
    icon=workbench_icon(project_root)
    if not icon.isNull():
        window.setWindowIcon(icon)
        app=QtWidgets.QApplication.instance()
        if app is not None:app.setWindowIcon(icon)
    guard=getattr(window,'_workbench_icon_guard',None)
    if guard is None:
        guard=_WindowIconGuard(window,project_root);window._workbench_icon_guard=guard;window.installEventFilter(guard)
    else:guard.project_root=project_root
    if window.isVisible():guard.apply()
    return icon
