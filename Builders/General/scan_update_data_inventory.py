from __future__ import annotations

from pathlib import Path
import sys

from Builders.General.generate_nwb_inventory import scan_directory_gui
from GUI.qt_compat import QtCore, QtGui, QtWidgets


def _computer_theme():
    """Read OS preferences independently of Workbench's forced light palette."""
    dark = False
    hints = QtWidgets.QApplication.styleHints()
    if hasattr(hints, "colorScheme") and hasattr(QtCore.Qt, "ColorScheme"):
        dark = hints.colorScheme() == QtCore.Qt.ColorScheme.Dark
    system_colors = {}
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                dark = not bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
        except (ImportError, OSError):
            pass
        try:
            import ctypes
            from ctypes import wintypes

            class HighContrast(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                            ("lpszDefaultScheme", wintypes.LPWSTR)]

            user32 = ctypes.windll.user32
            get_color = user32.GetSysColor
            get_color.argtypes = [ctypes.c_int]
            get_color.restype = wintypes.DWORD
            def color(index):
                value = int(get_color(index))
                return "#{:02x}{:02x}{:02x}".format(value & 255, (value >> 8) & 255, (value >> 16) & 255)
            system_colors["Highlight"] = color(13)
            system_colors["HighlightedText"] = color(14)
            contrast = HighContrast()
            contrast.cbSize = ctypes.sizeof(contrast)
            query = user32.SystemParametersInfoW
            query.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
            query.restype = wintypes.BOOL
            if query(0x0042, contrast.cbSize, ctypes.byref(contrast), 0) and contrast.dwFlags & 1:
                for name, index in {"Window":15, "WindowText":18, "Base":5,
                                    "Text":8, "Button":15, "ButtonText":18,
                                    "Mid":6, "DisabledText":17}.items():
                    system_colors[name] = color(index)
                dark = QtGui.QColor(system_colors["Window"]).lightness() < 128
        except (AttributeError, OSError):
            pass
    return dark, system_colors


def _browser_colors(dark, system_colors):
    if dark:
        colors = {"Window":"#202020", "WindowText":"#f0f0f0", "Base":"#191919",
                  "Text":"#f0f0f0", "Button":"#2d2d2d", "ButtonText":"#f0f0f0",
                  "Mid":"#505050", "Highlight":"#0078d4", "HighlightedText":"#ffffff",
                  "DisabledText":"#929292"}
    else:
        colors = {"Window":"#f9f9f9", "WindowText":"#202020", "Base":"#ffffff",
                  "Text":"#202020", "Button":"#f3f3f3", "ButtonText":"#202020",
                  "Mid":"#c8c8c8", "Highlight":"#0078d4", "HighlightedText":"#ffffff",
                  "DisabledText":"#777777"}
    colors.update(system_colors)
    return colors


class NwbInputDialog(QtWidgets.QFileDialog):
    """Standard Qt browser with acceptance of both folders and NWB files."""

    def __init__(self, initial_directory, parent=None):
        super().__init__(parent, "Add NWB files or folders", str(initial_directory))
        # Native dialogs cannot mix file and folder selection. Keep Qt's
        # standard browser, including its drive sidebar and location controls.
        self.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
        self.setFileMode(QtWidgets.QFileDialog.ExistingFiles)
        self.setNameFilter("NWB files (*.nwb *.NWB)")
        self.setLabelText(QtWidgets.QFileDialog.Accept, "Add")
        self.setViewMode(QtWidgets.QFileDialog.Detail)
        self.input_paths = []
        for view in self.findChildren(QtWidgets.QAbstractItemView):
            if view.objectName() in ("listView", "treeView"):
                view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
                view.selectionModel().selectionChanged.connect(self._label_add_button)
        self.currentChanged.connect(self._label_add_button)
        self.directoryEntered.connect(self._label_add_button)
        self._label_add_button()
        self._theme_signature = None
        self._explorer_style = None
        self._theme_timer = QtCore.QTimer(self)
        self._theme_timer.setInterval(1500)
        self._theme_timer.timeout.connect(self._refresh_computer_theme)
        self._apply_explorer_appearance()

    def _apply_explorer_appearance(self):
        """Keep Explorer layout and use the computer theme locally."""
        self.resize(980, 620)
        self.setMinimumSize(720, 440)
        self.setSizeGripEnabled(True)
        self._refresh_computer_theme()

        # Standard Windows locations plus This PC (file:) for disk navigation.
        urls = [QtCore.QUrl("file:")]
        locations = (
            QtCore.QStandardPaths.DesktopLocation,
            QtCore.QStandardPaths.DocumentsLocation,
            QtCore.QStandardPaths.DownloadLocation,
            QtCore.QStandardPaths.HomeLocation,
        )
        for location in locations:
            path = QtCore.QStandardPaths.writableLocation(location)
            if path and Path(path).is_dir():
                url = QtCore.QUrl.fromLocalFile(path)
                if url not in urls:
                    urls.append(url)
        self.setSidebarUrls(urls)
        splitter = self.findChild(QtWidgets.QSplitter, "splitter")
        if splitter is not None:
            splitter.setSizes([185, 740])

        tree = self.findChild(QtWidgets.QTreeView, "treeView")
        if tree is not None:
            tree.setAlternatingRowColors(False)
            tree.setRootIsDecorated(False)
            tree.setUniformRowHeights(True)
            tree.setIconSize(QtCore.QSize(20, 20))
            tree.setSortingEnabled(True)
            tree.sortByColumn(0, QtCore.Qt.AscendingOrder)
            header = tree.header()
            header.setStretchLastSection(False)
            # QFileSystemModel: Name, Size, Type, Date Modified.
            # Explorer's usual order: Name, Date Modified, Type, Size.
            if header.count() >= 4:
                for position, column in enumerate((0, 3, 2, 1)):
                    header.moveSection(header.visualIndex(column), position)
                header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
                for column in (1, 2, 3):
                    header.setSectionResizeMode(column, QtWidgets.QHeaderView.Interactive)
                tree.setColumnWidth(3, 165)
                tree.setColumnWidth(2, 125)
                tree.setColumnWidth(1, 90)
        self.setLabelText(QtWidgets.QFileDialog.LookIn, "Look in:")
        self.setLabelText(QtWidgets.QFileDialog.FileName, "File name:")
        self.setLabelText(QtWidgets.QFileDialog.FileType, "File type:")

    def _refresh_computer_theme(self):
        dark, system_colors = _computer_theme()
        colors = _browser_colors(dark, system_colors)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.GeneralFont)
        signature = (dark, tuple(sorted(colors.items())), font.toString())
        if signature == self._theme_signature:
            return
        self._theme_signature = signature
        self.setFont(font)
        available = {name.lower(): name for name in QtWidgets.QStyleFactory.keys()}
        # Fusion honours dark palettes consistently; Windows styles draw some
        # controls using light native resources even with an explicit palette.
        candidates = (("fusion",) if dark else
                      ("windows11", "windowsvista", "windows", "fusion"))
        old_style = self._explorer_style
        for candidate in candidates:
            if candidate in available:
                self._explorer_style = QtWidgets.QStyleFactory.create(available[candidate])
                break
        if self._explorer_style is not None:
            self._explorer_style.setParent(self)
            self.setStyle(self._explorer_style)
            for widget in self.findChildren(QtWidgets.QWidget):
                widget.setStyle(self._explorer_style)
        if old_style is not None and old_style is not self._explorer_style:
            old_style.deleteLater()
        palette = QtGui.QPalette()
        roles = QtGui.QPalette
        for name, value in colors.items():
            role = getattr(roles, name, None)
            if role is not None:
                palette.setColor(role, QtGui.QColor(value))
        palette.setColor(roles.AlternateBase, QtGui.QColor(colors["Base"]))
        palette.setColor(roles.ToolTipBase, QtGui.QColor(colors["Button"]))
        palette.setColor(roles.ToolTipText, QtGui.QColor(colors["Text"]))
        for role in (roles.WindowText, roles.Text, roles.ButtonText):
            palette.setColor(roles.Disabled, role, QtGui.QColor(colors["DisabledText"]))
        self.setPalette(palette)
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.setPalette(palette)
        # Every control uses the same local palette; no application-wide theme
        # setting is changed, including Workbench's forced white theme.
        stylesheet = """
            QFileDialog { background: palette(window); color: palette(window-text); }
            QFileDialog QTreeView, QFileDialog QListView {
                background: palette(base); color: palette(text);
                border: 1px solid palette(mid); outline: 0;
            }
            QFileDialog QTreeView::item, QFileDialog QListView::item { min-height: 25px; }
            QFileDialog QTreeView::item:selected, QFileDialog QListView::item:selected {
                background: palette(highlight); color: palette(highlighted-text);
            }
            QFileDialog QHeaderView::section {
                background: palette(window); color: palette(window-text);
                border: none; border-right: 1px solid palette(mid);
                border-bottom: 1px solid palette(mid);
                padding: 7px 8px; font-weight: normal;
            }
            QFileDialog QListView#sidebar {
                background: palette(window); border: none;
                border-right: 1px solid palette(mid);
            }
            QFileDialog QListView#sidebar::item { min-height: 32px; }
            QFileDialog QToolButton { padding: 5px; }
            QFileDialog QLineEdit, QFileDialog QComboBox {
                background: palette(base); color: palette(text);
                border: 1px solid palette(mid); min-height: 27px;
            }
            QFileDialog QPushButton { min-width: 80px; min-height: 27px; }
        """
        # Resolve locally: Qt stylesheet palette() can otherwise resolve the
        # application palette (forced light) during initial widget polishing.
        for token, name in {
            "window":"Window", "window-text":"WindowText", "base":"Base",
            "text":"Text", "button":"Button", "button-text":"ButtonText",
            "mid":"Mid", "highlight":"Highlight", "highlighted-text":"HighlightedText",
        }.items():
            stylesheet = stylesheet.replace("palette(" + token + ")", colors[name])
        self.setStyleSheet(stylesheet)
        self.setPalette(palette)
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.setPalette(palette)
        self._match_title_bar(dark)

    def _match_title_bar(self, dark):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            setter.restype = ctypes.c_long
            enabled = wintypes.BOOL(bool(dark))
            setter(wintypes.HWND(int(self.winId())), 20,
                   ctypes.byref(enabled), ctypes.sizeof(enabled))
        except (AttributeError, OSError):
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_computer_theme()
        dark = bool(self._theme_signature[0])
        self._match_title_bar(dark)
        self._theme_timer.start()

    def hideEvent(self, event):
        self._theme_timer.stop()
        super().hideEvent(event)

    def _label_add_button(self, *args):
        buttons = self.findChild(QtWidgets.QDialogButtonBox)
        if buttons is not None:
            button = buttons.button(QtWidgets.QDialogButtonBox.Open)
            if button is not None:
                button.setText("Add")

    def accept(self):
        # QFileDialog's normal accept() enters a selected directory. Here the
        # Add button accepts it for scanning; double-click still navigates.
        paths = self.selectedFiles()
        selected = []
        seen = set()
        for value in paths:
            path = Path(value).expanduser()
            if not (path.is_dir() or (path.is_file() and path.suffix.lower() == ".nwb")):
                QtWidgets.QMessageBox.warning(
                    self, "Selection", "Select existing NWB files or folders."
                )
                return
            key = str(path.resolve())
            if key not in seen:
                selected.append(key)
                seen.add(key)
        if not selected:
            return
        self.input_paths = selected
        # Bypass QFileDialog's directory-navigation and file-only validation.
        QtWidgets.QDialog.accept(self)


def select_scan_inputs(initial_directory, parent=None):
    dialog = NwbInputDialog(initial_directory, parent)
    result = dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()
    return dialog.input_paths if result == QtWidgets.QDialog.Accepted else []


def run_scan_update_inventory(gui):
    """Open the project import browser directly from the Files panel."""
    parent = gui if isinstance(gui, QtWidgets.QWidget) else None
    project_root = Path(getattr(gui, "project_root", Path(".")))
    selected_paths = select_scan_inputs(project_root, parent)
    if not selected_paths:
        return False

    scan_directory_gui(
        selected_paths=selected_paths,
        save=True,
        output_inventory_path=gui.NWB_inventory_path,
        parent=gui,
        force_rebuild=True,
        append=True,
    )
    reload_fn = getattr(gui, "reload_inventory", None)
    if callable(reload_fn):
        reload_fn()
    QtWidgets.QMessageBox.information(parent, "Inventory", "Selected data added or refreshed. Existing project files were kept.")
    return True
