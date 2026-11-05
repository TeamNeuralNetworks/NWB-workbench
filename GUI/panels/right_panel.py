from __future__ import annotations

from typing import Iterable

from GUI.qt_compat import QtWidgets


class RightPanel(QtWidgets.QWidget):
    """Right side stacked view: trials, plot, workflows, and content tools."""

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.stack = QtWidgets.QStackedWidget(self)
        outer.addWidget(self.stack)

        self.blank_page = QtWidgets.QWidget(self)
        self.stack.addWidget(self.blank_page)

        # Trials table
        self.trials_page = QtWidgets.QWidget(self)
        tr_layout = QtWidgets.QVBoxLayout(self.trials_page)
        tr_layout.setContentsMargins(0, 0, 0, 0)
        self.trials_table = QtWidgets.QTableWidget(self.trials_page)
        tr_layout.addWidget(self.trials_table)
        self.stack.addWidget(self.trials_page)

        # Plot page
        self.plot_page = QtWidgets.QWidget(self)
        plot_layout = QtWidgets.QVBoxLayout(self.plot_page)
        plot_layout.setContentsMargins(4, 4, 4, 4)
        plot_layout.setSpacing(4)
        self.control_row1 = QtWidgets.QHBoxLayout()
        self.control_row2 = QtWidgets.QHBoxLayout()
        self.control_row3 = QtWidgets.QHBoxLayout()
        plot_layout.addLayout(self.control_row1)
        plot_layout.addLayout(self.control_row2)
        plot_layout.addLayout(self.control_row3)
        self.plot_frame = QtWidgets.QWidget(self.plot_page)
        self.plot_frame_layout = QtWidgets.QVBoxLayout(self.plot_frame)
        self.plot_frame_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.addWidget(self.plot_frame, stretch=1)
        self.stack.addWidget(self.plot_page)

        # Processing placeholder/details page
        self.processing_page = QtWidgets.QWidget(self)
        proc_layout = QtWidgets.QVBoxLayout(self.processing_page)
        proc_layout.setContentsMargins(8, 8, 8, 8)
        self.processing_container = QtWidgets.QWidget(self.processing_page)
        self.processing_container_layout = QtWidgets.QVBoxLayout(self.processing_container)
        self.processing_container_layout.setContentsMargins(0, 0, 0, 0)
        proc_layout.addWidget(self.processing_container)
        self.stack.addWidget(self.processing_page)

        # Analysis placeholder/details page
        self.analysis_page = QtWidgets.QWidget(self)
        anal_layout = QtWidgets.QVBoxLayout(self.analysis_page)
        anal_layout.setContentsMargins(8, 8, 8, 8)
        self.analysis_container = QtWidgets.QWidget(self.analysis_page)
        self.analysis_container_layout = QtWidgets.QVBoxLayout(self.analysis_container)
        self.analysis_container_layout.setContentsMargins(0, 0, 0, 0)
        anal_layout.addWidget(self.analysis_container)
        self.stack.addWidget(self.analysis_page)

        # Spike sorting details page
        self.spike_sorting_page = QtWidgets.QWidget(self)
        spike_layout = QtWidgets.QVBoxLayout(self.spike_sorting_page)
        spike_layout.setContentsMargins(8, 8, 8, 8)
        self.spike_sorting_container = QtWidgets.QWidget(self.spike_sorting_page)
        self.spike_sorting_container_layout = QtWidgets.QVBoxLayout(self.spike_sorting_container)
        self.spike_sorting_container_layout.setContentsMargins(0, 0, 0, 0)
        spike_layout.addWidget(self.spike_sorting_container)
        self.stack.addWidget(self.spike_sorting_page)

        self.deeplabcut_page = QtWidgets.QWidget(self)
        self.deeplabcut_layout = QtWidgets.QVBoxLayout(self.deeplabcut_page)
        self.stack.addWidget(self.deeplabcut_page)

        # File list page
        self.filelist_page = QtWidgets.QWidget(self)
        fl_layout = QtWidgets.QVBoxLayout(self.filelist_page)
        fl_layout.setContentsMargins(6, 6, 6, 6)
        self.filelist_title = QtWidgets.QLabel("Files", self.filelist_page)
        self.filelist = QtWidgets.QListWidget(self.filelist_page)
        fl_layout.addWidget(self.filelist_title)
        fl_layout.addWidget(self.filelist, stretch=1)
        self.stack.addWidget(self.filelist_page)

        # Content object manager page. The app installs the editor widget after
        # both the Content tree and the shared NWB backend have been created.
        self.content_manager_page = QtWidgets.QWidget(self)
        content_manager_layout = QtWidgets.QVBoxLayout(self.content_manager_page)
        content_manager_layout.setContentsMargins(8, 8, 8, 8)
        self.content_manager_container = QtWidgets.QWidget(self.content_manager_page)
        self.content_manager_container_layout = QtWidgets.QVBoxLayout(self.content_manager_container)
        self.content_manager_container_layout.setContentsMargins(0, 0, 0, 0)
        content_manager_layout.addWidget(self.content_manager_container)
        self.stack.addWidget(self.content_manager_page)

        self.shared_viewer = None
        self.video_panel = None
        self.stack.setCurrentWidget(self.blank_page)

    def add_shared_object(self, nwb_path, object_path, reset=False):
        if reset and self.shared_viewer is not None:
            self.shared_viewer.shutdown()
            self.stack.removeWidget(self.shared_viewer)
            self.shared_viewer.deleteLater()
            self.shared_viewer = None
        if self.shared_viewer is None:
            from GUI.panels.shared_viewer import SharedViewer
            self.shared_viewer = SharedViewer(self)
            self.stack.addWidget(self.shared_viewer)
            QtWidgets.QApplication.instance().aboutToQuit.connect(self.shared_viewer.shutdown)
        self.stack.setCurrentWidget(self.shared_viewer)
        self.shared_viewer.add_object(nwb_path, object_path)

    def show_video_view(self, file_paths, object_path):
        if self.video_panel is None:
            from GUI.panels.pose_video import TrackingVideoPanel
            self.video_panel = TrackingVideoPanel(self)
            self.stack.addWidget(self.video_panel)
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.video_panel.shutdown)
        self.stack.setCurrentWidget(self.video_panel)
        self.video_panel.open_objects(file_paths, object_path)

    def show_pose_overlay(self,nwb_path,object_path):
        from pathlib import Path
        from GUI.panels.pose_video import read_pose_metadata
        info=read_pose_metadata(nwb_path,object_path)
        if info is None:raise ValueError('Choose an exported tracking coordinate object.')
        source_path=info['source']['object_path']
        shared=self.shared_viewer
        if shared is not None and self.stack.currentWidget() is shared and shared.nwb_path==str(Path(nwb_path).resolve()):
            if shared.video_path!=source_path:shared.add_object(nwb_path,source_path)
            shared.video.set_bodypart(object_path,True);return
        player=self.video_panel
        if player is None or player.source is None or Path(player.source['nwb_path']).resolve()!=Path(nwb_path).resolve() or player.source['object_path']!=source_path:
            self.show_video_view([nwb_path],source_path);player=self.video_panel
        else:self.stack.setCurrentWidget(player)
        if player.source is None:raise ValueError(player.status.text())
        player.set_bodypart(object_path,True)

    def closeEvent(self, event):
        if self.shared_viewer is not None:
            self.shared_viewer.shutdown()
        if self.video_panel is not None:
            self.video_panel.shutdown()
        super().closeEvent(event)

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                RightPanel.clear_layout(child_layout)

    def show_deeplabcut_view(self):
        self.stack.setCurrentWidget(self.deeplabcut_page)

    def show_trials_view(self):
        self.stack.setCurrentWidget(self.trials_page)

    def show_plot_view(self):
        self.stack.setCurrentWidget(self.plot_page)

    def show_processing_view(self):
        self.stack.setCurrentWidget(self.processing_page)

    def show_analysis_view(self):
        self.stack.setCurrentWidget(self.analysis_page)

    def show_spike_sorting_view(self):
        self.stack.setCurrentWidget(self.spike_sorting_page)

    def show_file_list_view(self):
        self.stack.setCurrentWidget(self.filelist_page)

    def show_content_manager_view(self):
        self.stack.setCurrentWidget(self.content_manager_page)

    def set_content_manager_widget(self, widget):
        self.clear_layout(self.content_manager_container_layout)
        if widget is not None:
            self.content_manager_container_layout.addWidget(widget)

    def set_file_list(self, paths: Iterable[str], title="Files containing this data"):
        self.filelist_title.setText(str(title))
        self.filelist.clear()
        for p in list(paths or []):
            self.filelist.addItem(str(p))
