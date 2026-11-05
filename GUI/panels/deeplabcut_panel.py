"""Optional DLC workflow. DeepLabCut itself runs only in an external Python."""
from GUI.project_paths import json_read as _pp_json_read
from pathlib import Path
import json
import os
import sys
import uuid
import shutil
import random
import time
from html import escape
from datetime import datetime
import numpy as np
from GUI.qt_compat import QtCore, QtGui, QtWidgets
from GUI.dlc_environments import EnvironmentDetector, child_environment, start_external
from GUI.dlc_storage import storage_root,job_paths,job_records,atomic_json
from GUI.panels.video_panel import VideoPanel
from Tools.General.dlc_io import discover_videos, indexed_video_sources, video_source, load_prediction, verify_source, import_prediction, saved_postprocessing, selected_analysis_results, save_postprocessed, project_export_status, project_analysis_status, usable_analysis_run, remove_postprocessed, postprocessed_video_keys


def save_json(path,value):
    atomic_json(path,value,create_parent=True)


def managed_project_folder(root, config, declared_path=None):
    """Only offer disk deletion for a dedicated project immediately under DeepLabCut."""
    root=Path(root).resolve();config=Path(config)
    folder=config.parent
    if folder.is_symlink() or folder.resolve().parent!=root or folder.name in {'jobs','videos','labeled-data'}:
        raise ValueError('Folder deletion is available only for dedicated projects directly inside this Workbench DeepLabCut folder.')
    if config.name!='config.yaml' or not config.is_file():
        raise ValueError('The project config.yaml is missing. You can still remove the project from the list.')
    if declared_path and Path(declared_path).resolve()!=folder.resolve():
        raise ValueError('The registered project path differs from the configuration folder. Remove from the list only.')
    return folder.resolve()


class ProcessingModeComboBox(QtWidgets.QComboBox):
    def wheelEvent(self,event):
        # Let the enclosing table handle scrolling without changing this mode.
        event.ignore()


class PostprocessingSpinBox(QtWidgets.QSpinBox):
    def wheelEvent(self,event):
        event.ignore()


class PostprocessingDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    def wheelEvent(self,event):
        event.ignore()


class WorkThread(QtCore.QThread):
    done=QtCore.Signal(object)
    failed=QtCore.Signal(str)
    def __init__(self,fn,parent=None):super().__init__(parent);self.fn=fn
    def run(self):
        try:self.done.emit(self.fn())
        except Exception as exc:self.failed.emit(str(exc))


from Tools.General.dlc_postprocessing import normalize_settings,process_pose


class PosePreview(VideoPanel):
    def __init__(self,parent=None):
        self.pose=None;self.parts=[];self.cutoff=.9;self._raw_frame_pixmap=None;self.processed_xy=None;self.processed_status=None
        super().__init__(parent)
        self.title.hide();self.file_choice.hide()
        row=QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Dot size'))
        self.dot_size=QtWidgets.QSpinBox();self.dot_size.setRange(2,40);self.dot_size.setValue(8);self.dot_size.setSuffix(' px')
        self.dot_size.setToolTip('Dot diameter in original video pixels. Dots scale with video zoom.')
        row.addWidget(self.dot_size)
        self.point_labels=QtWidgets.QCheckBox('Labels beside points');self.point_labels.setChecked(False);row.addWidget(self.point_labels)
        self.legend_toggle=QtWidgets.QCheckBox('Show labels');self.legend_toggle.setChecked(True);row.addWidget(self.legend_toggle)
        self.legend_toggle.setToolTip('Show tracking dots, optional point names and the color legend.')
        self.processed_toggle=QtWidgets.QCheckBox('Processed');self.processed_toggle.setChecked(True);self.processed_toggle.setEnabled(False);row.addWidget(self.processed_toggle)
        row.addWidget(QtWidgets.QLabel('Minimum confidence'))
        self.confidence=QtWidgets.QDoubleSpinBox();self.confidence.setRange(0,1);self.confidence.setDecimals(2);self.confidence.setSingleStep(.05);self.confidence.setValue(self.cutoff)
        self.confidence.setFixedWidth(self.confidence.sizeHint().width())
        self.confidence.setToolTip('Hide points below this confidence in the preview. Raw predictions are unchanged.')
        row.addWidget(self.confidence)
        row.addStretch(1)
        # Reuse one toolbar row for overlay and zoom controls.
        zoom_item=self.layout().takeAt(2)
        zoom_row=zoom_item.layout()
        while zoom_row.count():
            item=zoom_row.takeAt(0)
            if item.widget():row.addWidget(item.widget())
        self.layout().insertLayout(2,row)
        self.layout().setContentsMargins(0,0,0,0);self.layout().setSpacing(4)
        self.legend=QtWidgets.QLabel();self.legend.setWordWrap(True)
        self.layout().insertWidget(3,self.legend)
        self.dot_size.valueChanged.connect(self.redraw_pose)
        self.point_labels.toggled.connect(self.redraw_pose)
        self.legend_toggle.toggled.connect(self.update_legend)
        self.legend_toggle.toggled.connect(self.redraw_pose)
        self.confidence.valueChanged.connect(self.set_confidence)
        self.processed_toggle.toggled.connect(self.redraw_pose)
        self.processed_toggle.toggled.connect(self.update_legend)
        self.update_legend()

    def set_confidence(self,value):
        self.cutoff=value
        self.redraw_pose()

    @staticmethod
    def part_color(index):
        return QtGui.QColor.fromHsv((index*67)%360,230,255)

    def update_legend(self,*_):
        entries=[f'<span style="color:{self.part_color(i).name()}">●</span> {escape(str(part))}' for i,part in enumerate(self.parts)]
        self.legend.setText(' &nbsp; &nbsp; '.join(entries))
        if self.processed_toggle.isChecked() and self.processed_xy is not None:
            self.legend.setText(self.legend.text()+' &nbsp; | &nbsp; ○ interpolated &nbsp; ■ fixed &nbsp; ● original')
        self.legend.setVisible(self.legend_toggle.isChecked() and bool(self.parts))

    def redraw_pose(self,*_):
        if self._raw_frame_pixmap is None or self._pixmap is None:return
        self._pixmap=self._raw_frame_pixmap.copy()
        processed=self.processed_toggle.isChecked() and self.processed_xy is not None
        self.confidence.setEnabled(not processed)
        index=self.displayed
        if self.legend_toggle.isChecked() and self.pose is not None and index<len(self.pose):
            painter=QtGui.QPainter(self._pixmap)
            try:
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                radius=self.dot_size.value()/2
                for i,(x,y,score) in enumerate(self.pose[index]):
                    status=1
                    if processed:
                        x,y=self.processed_xy[index,i];status=int(self.processed_status[index,i])
                        if not status or not np.all(np.isfinite([x,y])):continue
                    elif not np.all(np.isfinite([x,y,score])) or score<self.cutoff:continue
                    color=self.part_color(i)
                    painter.setPen(QtGui.QPen(color,1));painter.setBrush(QtCore.Qt.BrushStyle.NoBrush if status==2 else color)
                    if status==3:painter.drawRect(QtCore.QRectF(float(x)-radius,float(y)-radius,2*radius,2*radius))
                    else:painter.drawEllipse(QtCore.QPointF(float(x),float(y)),radius,radius)
                    if self.point_labels.isChecked():
                        painter.drawText(QtCore.QPointF(float(x)+radius+2,float(y)-radius-2),str(self.parts[i]))
            finally:painter.end()
        self._scale()

    def _received(self,index,rgb):
        valid=self.sender() is self.decoder and index==self.current
        super()._received(index,rgb)
        if valid and self._pixmap is not None:
            self._raw_frame_pixmap=self._pixmap.copy()
            self.redraw_pose()


class DeepLabCutPanel(QtWidgets.QWidget):
    LIBRARY,LABELING,ANALYSIS,POSTPROCESSING,REVIEW,EXPORT=range(6)
    STAGE_NAMES=('Video library','Model creation','Analyze videos','Postprocessing','Review','Export')
    runningStateChanged=QtCore.Signal()
    exportProgress=QtCore.Signal(object)
    def __init__(self,parent=None,*,right_panel,GUI_instance):
        super().__init__(parent)
        self.setMinimumWidth(300)
        self.gui=GUI_instance;self.right_panel=right_panel
        self.root=Path(GUI_instance.project_root)/'DeepLabCut'
        self.root.mkdir(parents=True,exist_ok=True)
        self.settings_path=self.root/'workbench.json'
        self.settings=_pp_json_read(self.settings_path) if self.settings_path.exists() else {}
        self.projects=list(self.settings.get('projects',[]));self.targets=[];self.sources=[];self.video_cache={}
        self.process=None;self.worker=None;self.job=None;self.cancelled=False;self.preview_result=None;self.loaded_run=None
        self.buttons=[];self.locked_widgets=[];self.importing=False
        self.exportProgress.connect(self.export_progress)
        self._pp_video_rows=[];self._export_rows=[];self._export_dirty=True;self._export_nwb_cache={};self._postprocessed_video_keys=None;self._exported_video_count=None
        self._analysis_rows={};self._analysis_dirty=True;self._analysis_checkpoint_cache={}
        self.postprocessing_settings=dict(self.settings.get("postprocessing",{}));self._pp_loading=False
        self.video_ui=dict(self.settings.get("video_ui",{}));self._active_project=None;self.action_buttons={};self._restoring=False;self._active_model_key=None;self._switching_model=False
        layout=QtWidgets.QVBoxLayout(self)
        title=QtWidgets.QLabel('DeepLabCut');title.setStyleSheet('font-size:16px; font-weight:bold');layout.addWidget(title)
        self.validated_python = ''
        status_row=QtWidgets.QHBoxLayout();layout.addLayout(status_row)
        self.environment_dot=QtWidgets.QLabel('●');status_row.addWidget(self.environment_dot)
        self.environment_status=QtWidgets.QLabel();self.environment_status.setMinimumWidth(0)
        self.environment_status.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred)
        status_row.addWidget(self.environment_status,1)
        settings_button=QtWidgets.QToolButton();settings_button.setText('⚙')
        settings_button.setToolTip('Configure the DeepLabCut environment')
        settings_button.clicked.connect(self.show_environment_settings);status_row.addWidget(settings_button)
        self.environment_dialog=QtWidgets.QDialog(self)
        self.environment_dialog.setWindowTitle('DeepLabCut environment');self.environment_dialog.resize(560,360)
        env_layout=QtWidgets.QVBoxLayout(self.environment_dialog)
        env_layout.addWidget(QtWidgets.QLabel('Detected environments'))
        self.environments=QtWidgets.QComboBox();env_layout.addWidget(self.environments)
        self.environments.currentIndexChanged.connect(self.environment_selected)
        env_layout.addWidget(QtWidgets.QLabel('Environment Python'))
        self.python=QtWidgets.QLineEdit(self.settings.get('python',''));env_layout.addWidget(self.python)
        self.python.textChanged.connect(lambda *_:self.update_environment_status())
        self.python.editingFinished.connect(self.persist)
        env_buttons=QtWidgets.QHBoxLayout();env_layout.addLayout(env_buttons)
        self.button(env_buttons,'Detect',self.detect_environments)
        self.button(env_buttons,'Browse…',self.choose_python)
        self.button(env_buttons,'Test',lambda:self.launch('check'))
        self.environment=QtWidgets.QLabel('DLC 3 / PyTorch • one animal per video')
        self.environment.setWordWrap(True);env_layout.addWidget(self.environment)
        self.environment_log=QtWidgets.QPlainTextEdit();self.environment_log.setReadOnly(True)
        self.environment_log.setMaximumBlockCount(500);env_layout.addWidget(self.environment_log,1)
        footer=QtWidgets.QHBoxLayout();env_layout.addLayout(footer)
        self.environment_cancel=QtWidgets.QPushButton('Cancel detection / test')
        self.environment_cancel.clicked.connect(self.cancel_job);self.environment_cancel.setEnabled(False)
        footer.addWidget(self.environment_cancel)
        close=QtWidgets.QPushButton('Close');close.clicked.connect(self.environment_dialog.close);footer.addWidget(close)
        layout.addWidget(QtWidgets.QLabel('Project'))
        self.project=QtWidgets.QComboBox();layout.addWidget(self.project)
        project_actions=QtWidgets.QHBoxLayout();layout.addLayout(project_actions)
        self.button(project_actions,'New',lambda:self.show_project_settings(True))
        self.button(project_actions,'Open',self.open_config)
        self.project_delete_button=self.button(project_actions,'Delete',self.delete_project)
        self.button(layout,'Refresh projects / models',self.inspect_project)
        self.stages=QtWidgets.QListWidget();self.stages.addItems(self.STAGE_NAMES)
        layout.addWidget(self.stages,1)
        self.selection=QtWidgets.QLabel('No NWB files selected: showing all project videos.');self.selection.setWordWrap(True);layout.addWidget(self.selection)
        self.locked_widgets.extend([self.python,self.project,self.environments])
        self.right=QtWidgets.QWidget();outer=QtWidgets.QVBoxLayout(self.right)
        self.workflow_header=QtWidgets.QWidget();header_layout=QtWidgets.QVBoxLayout(self.workflow_header);header_layout.setContentsMargins(0,0,0,0);outer.addWidget(self.workflow_header)
        self.heading=QtWidgets.QLabel('DeepLabCut');self.heading.setWordWrap(True);header_layout.addWidget(self.heading)
        modelrow=QtWidgets.QHBoxLayout();header_layout.addLayout(modelrow)
        modelrow.addWidget(QtWidgets.QLabel('Model'))
        self.models=QtWidgets.QComboBox();self.models.setPlaceholderText('No model yet');self.models.setMinimumWidth(80);modelrow.addWidget(self.models,1)
        self.advanced_toggle=QtWidgets.QToolButton();self.advanced_toggle.setText('Advanced settings');self.advanced_toggle.setCheckable(True);modelrow.addWidget(self.advanced_toggle)
        self.advanced=QtWidgets.QWidget();advanced_layout=QtWidgets.QHBoxLayout(self.advanced)
        self.shuffle=self.spin(advanced_layout,'Shuffle',1,999,1)
        self.fraction=self.spin(advanced_layout,'Fraction index',0,99,0)
        self.snapshot=self.spin(advanced_layout,'Checkpoint index',-1,99999,-1)
        self.snapshot.setToolTip('-1 uses the latest checkpoint. Other indices follow DeepLabCut ordering.')
        header_layout.addWidget(self.advanced);self.advanced.hide();self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.models.currentIndexChanged.connect(self.model_selected)
        self.next_step=QtWidgets.QLabel();self.next_step.setWordWrap(True);header_layout.addWidget(self.next_step)
        self.inline_error=QtWidgets.QLabel();self.inline_error.setWordWrap(True);self.inline_error.setStyleSheet('color:#b42318');self.inline_error.hide();outer.addWidget(self.inline_error)
        self.pages=QtWidgets.QStackedWidget();outer.addWidget(self.pages,1)
        self.build_pages()
        navigation=QtWidgets.QHBoxLayout();outer.addLayout(navigation)
        self.previous_stage_button=QtWidgets.QPushButton('Previous section');self.previous_stage_button.clicked.connect(lambda:self.stages.setCurrentRow(max(0,self.stages.currentRow()-1)));navigation.addWidget(self.previous_stage_button)
        navigation.addStretch(1)
        self.next_stage_button=QtWidgets.QPushButton();self.next_stage_button.clicked.connect(lambda:self.stages.setCurrentRow(min(self.EXPORT,self.stages.currentRow()+1)));navigation.addWidget(self.next_stage_button)
        jobrow=QtWidgets.QHBoxLayout();outer.addLayout(jobrow)
        self.job_status=QtWidgets.QLabel('Idle');jobrow.addWidget(self.job_status,1)
        self.log_toggle=QtWidgets.QToolButton();self.log_toggle.setText('Show log');self.log_toggle.setCheckable(True);jobrow.addWidget(self.log_toggle)
        self.cancel=QtWidgets.QPushButton('Cancel task');self.cancel.clicked.connect(self.cancel_job);self.cancel.setEnabled(False);jobrow.addWidget(self.cancel)
        self.log=QtWidgets.QPlainTextEdit();self.log.setReadOnly(True);self.log.setMaximumBlockCount(3000);self.log.setMaximumHeight(160);outer.addWidget(self.log)
        self.log_toggle.toggled.connect(self.log.setVisible)
        self.log_toggle.toggled.connect(lambda visible:self.log_toggle.setText('Hide log' if visible else 'Show log'))
        self.log_toggle.setChecked(True)
        self.job_clock=QtCore.QElapsedTimer();self.job_timer=QtCore.QTimer(self);self.job_timer.setInterval(1000);self.job_timer.timeout.connect(self.update_job_status)
        self._last_job_status='Idle'
        right_panel.deeplabcut_layout.addWidget(self.right)
        self.project.currentIndexChanged.connect(self.project_changed)
        self.refresh_projects();self.stages.setCurrentRow(0)
        # Initialize the selected stage without opening its right-side view.
        self.stages.currentRowChanged.connect(self.show_stage)
        self.locked_widgets.extend([self.models,self.shuffle,self.fraction,self.snapshot])
        self.shuffle.valueChanged.connect(self.update_workflow);self.fraction.valueChanged.connect(self.update_workflow)
        self.shuffle.valueChanged.connect(self.history_selected);self.fraction.valueChanged.connect(self.history_selected)
        self.shuffle.valueChanged.connect(self.invalidate_export);self.fraction.valueChanged.connect(self.invalidate_export)
        self.snapshot.valueChanged.connect(self.invalidate_export)
        for field in (self.shuffle,self.fraction,self.snapshot):field.valueChanged.connect(self.schedule_analysis_status)
        self.update_navigation();self.set_busy()
        self.detector=EnvironmentDetector(self)
        self.detector.progress.connect(self.log.appendPlainText)
        self.detector.progress.connect(self.environment_log.appendPlainText)
        self.detector.completed.connect(self.environments_detected)
        self.populate_environments(self.settings.get('environments',[]),self.python.text())
        self.update_environment_status()
        if not Path(self.python.text()).is_file() or not self.detected_environments:
            QtCore.QTimer.singleShot(0,lambda:self.safe(self.detect_environments))

    def button(self,layout,label,fn):
        button=QtWidgets.QPushButton(label);button.clicked.connect(lambda:self.safe(fn));layout.addWidget(button);self.buttons.append(button);return button
    def spin(self,layout,label,low,high,value):
        layout.addWidget(QtWidgets.QLabel(label));widget=QtWidgets.QSpinBox();widget.setRange(low,high);widget.setValue(value);layout.addWidget(widget);return widget
    def page(self,title):
        page=QtWidgets.QWidget();layout=QtWidgets.QVBoxLayout(page);label=QtWidgets.QLabel(title);label.setWordWrap(True);layout.addWidget(label);self.pages.addWidget(page);return layout
    def safe(self,fn):
        try:fn()
        except Exception as exc:self.error(str(exc))
    def error(self,message):
        self.log.appendPlainText('ERROR: '+message)
        self.inline_error.setText(message+'  Open the log for details.');self.inline_error.show()
        self._last_job_status='Task failed';self.update_job_status()
    def action_button(self,layout,label,action,fn):
        button=self.button(layout,label,fn);self.action_buttons[action]=button;return button

    def build_pages(self):
        self.project_dialog=QtWidgets.QDialog(self);self.project_dialog.setWindowTitle('DeepLabCut project settings');self.project_dialog.resize(540,360)
        form=QtWidgets.QVBoxLayout(self.project_dialog)
        self.name=QtWidgets.QLineEdit('Operant_behavior')
        self.bodyparts=QtWidgets.QLineEdit('nose, left_ear, right_ear, tail_base')
        for label,widget in [('Project name',self.name),('Body parts, separated by commas',self.bodyparts)]:
            form.addWidget(QtWidgets.QLabel(label));form.addWidget(widget);self.locked_widgets.append(widget)
        self.create_button=self.button(form,'Create project',self.create_project)
        self.new_project_hint=QtWidgets.QLabel('Create the project, then add videos to its saved library.');self.new_project_hint.setWordWrap(True);form.addWidget(self.new_project_hint)
        self.save_bodyparts_button=self.button(form,'Save body parts',self.save_bodyparts)
        self.bodyparts_hint=QtWidgets.QLabel('For existing projects, append new points and keep existing names and order. After labeling new points, recreate the training dataset and train with a new shuffle.')
        self.bodyparts_hint.setWordWrap(True);form.addWidget(self.bodyparts_hint)
        self.project_folder_button=self.button(form,'Open project folder',self.open_project_folder)
        self.delete_project_button=self.button(form,'Delete project…',self.delete_project)
        self.project_close_button=self.button(form,'Close',self.project_dialog.close)
        video=self.page('Add the videos you want to use in this project. This list is saved automatically.')
        self.library_page=video.parentWidget()
        self.storage_options=QtWidgets.QWidget();storage_layout=QtWidgets.QVBoxLayout(self.storage_options);storage_layout.setContentsMargins(0,0,0,0)
        storage_row=QtWidgets.QHBoxLayout();storage_layout.addLayout(storage_row)
        storage_row.addWidget(QtWidgets.QLabel('Training video storage'))
        self.video_storage=QtWidgets.QComboBox()
        self.video_storage.addItem('Use originals (links, no copies)',False)
        self.video_storage.addItem('Copy videos into the project',True)
        self.video_storage.setCurrentIndex(1 if self.settings.get('copy_videos',False) else 0)
        storage_row.addWidget(self.video_storage,1);self.locked_widgets.append(self.video_storage)
        self.video_storage.currentIndexChanged.connect(lambda *_:self.persist())
        storage_note=QtWidgets.QLabel('The library stores references. This link/copy choice applies when registering training videos with DLC. Originals must remain accessible when using links.')
        storage_note.setWordWrap(True);storage_layout.addWidget(storage_note)
        self.library_host=QtWidgets.QWidget();self.library_host_layout=QtWidgets.QVBoxLayout(self.library_host);self.library_host_layout.setContentsMargins(0,0,0,0);video.addWidget(self.library_host,1)
        self.library_browser=QtWidgets.QWidget();browser=QtWidgets.QVBoxLayout(self.library_browser);browser.setContentsMargins(0,0,0,0);self.library_host_layout.addWidget(self.library_browser)
        self.library_actions=QtWidgets.QWidget();row=QtWidgets.QHBoxLayout(self.library_actions);row.setContentsMargins(0,0,0,0);browser.addWidget(self.library_actions)
        self.add_library_button=self.button(row,'Add videos',self.scan)
        self.add_library_button.setToolTip('Choose videos from the NWB files currently selected in the file list.')
        self.remove_library_button=self.button(row,'Remove selected',self.remove_library_videos)
        self.refresh_library_button=self.button(row,'Refresh library metadata',self.refresh_library)
        self.role_controls=QtWidgets.QWidget();row=QtWidgets.QHBoxLayout(self.role_controls);row.setContentsMargins(0,0,0,0);browser.addWidget(self.role_controls)
        self.select_shown_button=self.button(row,'Select all',lambda:self.check_sources(True));self.clear_shown_button=self.button(row,'Clear',lambda:self.check_sources(False))
        self.select_shown_button.setToolTip('Select all shown videos for this section. Hidden selections are kept.')
        self.clear_shown_button.setToolTip('Clear the selection among shown videos. Hidden selections are kept.')

        self.selection_purpose=QtWidgets.QComboBox();self.selection_purpose.addItem('Training',0);self.selection_purpose.addItem('Analysis',1);self.selection_purpose.addItem('Test',5);row.addWidget(self.selection_purpose);self.selection_purpose.hide()
        self.locked_widgets.append(self.selection_purpose)
        self.random_controls=QtWidgets.QWidget();random_row=QtWidgets.QHBoxLayout(self.random_controls);random_row.setContentsMargins(0,0,0,0);browser.addWidget(self.random_controls)
        self.random_label=QtWidgets.QLabel('Random subset');random_row.addWidget(self.random_label)
        self.random_percentage=QtWidgets.QSpinBox();self.random_percentage.setRange(1,100)
        self.random_percentage.setSingleStep(10);self.random_percentage.setSuffix(' %');self.random_percentage.setValue(10)
        random_row.addWidget(self.random_percentage);self.locked_widgets.append(self.random_percentage)
        self.random_select_button=self.button(random_row,'Select randomly',self.select_random_sources)
        self.random_select_button.setToolTip('Replace the chosen role among shown valid videos. Training and Test exclude each other; Test also selects Analysis. Hidden rows are kept.')
        self.test_remaining_button=self.button(random_row,'Use remaining as Test',self.select_test_complement)
        self.test_remaining_button.setToolTip('Mark shown videos without the Training role as Test and Analysis. Keep Training selections and hidden rows. Existing models may already have seen these videos.')
        random_row.addStretch(1)
        # One compact selection row; training parameters open separately.
        role_row=self.role_controls.layout()
        while random_row.count():
            item=random_row.takeAt(0)
            if item.widget():role_row.addWidget(item.widget())
        role_row.addStretch(1)
        self.label_parameters_button=self.button(role_row,'Parameters',self.show_label_parameters)
        self.random_controls.hide()
        self.library_filters=QtWidgets.QWidget();filter_row=QtWidgets.QHBoxLayout(self.library_filters);filter_row.setContentsMargins(0,0,0,0);browser.addWidget(self.library_filters)
        filter_row.addWidget(QtWidgets.QLabel('NWB object name'))
        self.object_name_filter=QtWidgets.QComboBox();self.object_name_filter.addItem('All objects','')
        filter_row.addWidget(self.object_name_filter,1)
        self.object_search=QtWidgets.QLineEdit();self.object_search.setPlaceholderText('Search video, NWB file or object…')
        self.object_search.setClearButtonEnabled(True);filter_row.addWidget(self.object_search,1)
        self.button(filter_row,'Show all',self.clear_video_filters)
        self.select_library_button=self.button(filter_row,'Select all',self.select_library_rows)
        self.select_library_button.setToolTip('Highlight all currently shown videos. Name filters and the Files selection are respected.')
        self.video_filter_count=QtWidgets.QLabel();browser.addWidget(self.video_filter_count)
        self.locked_widgets.extend([self.object_name_filter,self.object_search])
        self.table=QtWidgets.QTableWidget(0,8);self.table.setHorizontalHeaderLabels(['Training','Analysis','NWB / ImageSeries','Video','Status','Test','Model used','Analysis date'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setMinimumSectionSize(30)
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Expanding)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._fitting_video_columns=False;self._manual_video_columns=False;self._video_header_pressed=False
        self._video_fit_timer=QtCore.QTimer(self);self._video_fit_timer.setSingleShot(True);self._video_fit_timer.timeout.connect(self.fit_video_columns)
        self._video_header_viewport=self.table.horizontalHeader().viewport();self._video_header_viewport.installEventFilter(self)
        self._video_viewport=self.table.viewport();self._video_viewport.installEventFilter(self)
        self.table.horizontalHeader().sectionResized.connect(self.video_column_resized)
        self.table.horizontalHeader().moveSection(5,1)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers);browser.addWidget(self.table,1);self.locked_widgets.append(self.table)
        self.object_name_filter.currentIndexChanged.connect(self.filter_video_rows)
        self.object_search.textChanged.connect(self.filter_video_rows)
        self.table.itemChanged.connect(self.video_choices_changed)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.library_context_menu)
        self.filter_video_rows()
        self.library_role_note=QtWidgets.QLabel('Test videos are excluded from future training and selected for analysis. Existing models may already have seen them. DLC’s internal frame split is unchanged.');self.library_role_note.setWordWrap(True);browser.addWidget(self.library_role_note)
        annotate=self.page('Choose training videos, label their frames, then train the model. Missing frames and training datasets are prepared automatically.')
        model_row=QtWidgets.QHBoxLayout();annotate.addLayout(model_row);model_row.addWidget(QtWidgets.QLabel('Model'))
        self.creation_models=QtWidgets.QComboBox();model_row.addWidget(self.creation_models,1);self.locked_widgets.append(self.creation_models)
        self.button(model_row,'New model',self.new_named_model)
        self.rename_model_button=self.button(model_row,'Rename',self.rename_named_model)
        self.creation_models.currentIndexChanged.connect(self.creation_model_selected)
        self.model_lock_note=QtWidgets.QLabel();self.model_lock_note.setWordWrap(True);annotate.addWidget(self.model_lock_note)
        self.labeling_host=QtWidgets.QWidget();self.labeling_host_layout=QtWidgets.QVBoxLayout(self.labeling_host);self.labeling_host_layout.setContentsMargins(0,0,0,0);annotate.addWidget(self.labeling_host,1)
        self.training_summary=QtWidgets.QLabel();annotate.addWidget(self.training_summary)
        actions=QtWidgets.QHBoxLayout();annotate.addLayout(actions)
        self.action_button(actions,'Open labeling','annotate',lambda:self.launch('annotate',frames=self.frames.value()))
        self.action_button(actions,'Train model','train',lambda:self.launch('train',epochs=self.epochs.value()))
        self.training_parameters_button=self.button(actions,'Training parameters',self.show_training_parameters)
        self.action_button(actions,'Evaluate model','evaluate',lambda:self.launch('evaluate'))
        self.training_status=QtWidgets.QLabel();self.training_status.setWordWrap(True);annotate.addWidget(self.training_status)
        self.label_parameters_dialog=QtWidgets.QDialog(self);self.label_parameters_dialog.setWindowTitle('Labeling parameters');self.label_parameters_dialog.resize(600,320)
        options=QtWidgets.QVBoxLayout(self.label_parameters_dialog)
        parts_row=QtWidgets.QHBoxLayout();options.addLayout(parts_row);parts_row.addWidget(QtWidgets.QLabel('Body parts'))
        self.label_bodyparts=QtWidgets.QLineEdit();self.label_bodyparts.setToolTip('Comma-separated names. Existing labels retain their body-part names and order.');parts_row.addWidget(self.label_bodyparts,1)
        self.label_bodyparts_save=self.button(parts_row,'Save',self.save_label_bodyparts);self.locked_widgets.append(self.label_bodyparts)
        self.parts_summary=QtWidgets.QLabel(self.label_parameters_dialog);self.parts_summary.hide()
        scorer_row=QtWidgets.QHBoxLayout();options.addLayout(scorer_row);scorer_row.addWidget(QtWidgets.QLabel('Scorer name'))
        self.scorer=QtWidgets.QLineEdit('user');scorer_row.addWidget(self.scorer,1);self.locked_widgets.append(self.scorer)
        self.save_scorer_button=self.button(scorer_row,'Save',self.save_scorer)
        self.scorer.setToolTip('Identifies saved annotations. Set before labeling; existing scorer protections are preserved.')
        row=QtWidgets.QHBoxLayout();options.addLayout(row);self.frames=self.spin(row,'Frames per video',1,500,20);self.locked_widgets.append(self.frames)
        self.frames.setToolTip('Number of frames to extract when a training video has no extracted frames. Existing frames and labels are kept.')
        self.frames.valueChanged.connect(lambda *_:self.label_frame_count_changed())
        options.addWidget(self.storage_options)
        self.button(options,'Open labeled frames folder',lambda:self.open_project_folder('labeled-data'))
        close=QtWidgets.QPushButton('Close');close.clicked.connect(self.label_parameters_dialog.close);options.addWidget(close)
        self.training_parameters_dialog=QtWidgets.QDialog(self);self.training_parameters_dialog.setWindowTitle('Training parameters');self.training_parameters_dialog.resize(460,200)
        parameters=QtWidgets.QVBoxLayout(self.training_parameters_dialog)
        row=QtWidgets.QHBoxLayout();parameters.addLayout(row)
        self.device=QtWidgets.QComboBox();self.device.addItem('cpu');self.device.activated.connect(self.training_device_selected);row.addWidget(QtWidgets.QLabel('Device'));row.addWidget(self.device)
        self.epochs=self.spin(row,'Epochs',1,100000,200);self.locked_widgets.extend([self.device,self.epochs])
        note=QtWidgets.QLabel('Epochs is the requested training limit. DLC may stop earlier. The frame split uses the configured fraction index. Dataset preparation happens automatically when needed. Create a new named model to train another selection. Each model has a separate shuffle.');note.setWordWrap(True);parameters.addWidget(note)
        close=QtWidgets.QPushButton('Close');close.clicked.connect(self.training_parameters_dialog.close);parameters.addWidget(close)
        self.device.currentIndexChanged.connect(self.update_workflow);self.epochs.valueChanged.connect(self.update_workflow)
        self.training_identity=QtWidgets.QLabel();parameters.insertWidget(1,self.training_identity)
        self.excluded_videos_button=self.button(parameters,'Excluded training videos…',self.edit_test_videos)
        analysis=self.page('Select videos, analyze them, then review predictions. Status refers to the active model. Files selection and name filters limit the list.')
        self.analysis_models=QtWidgets.QComboBox();self.analysis_models.setMinimumWidth(100)
        self.analysis_models.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Preferred);self.locked_widgets.append(self.analysis_models)
        self.analysis_models.currentIndexChanged.connect(self.analysis_model_selected)
        self.analysis_summary=QtWidgets.QLabel();analysis.addWidget(self.analysis_summary)
        analysis_tools=QtWidgets.QHBoxLayout();self.analysis_selection_tools=analysis_tools;analysis.addLayout(analysis_tools)
        self.analysis_status_filter=QtWidgets.QComboBox();self.analysis_status_filter.addItems(['All','Not analyzed','Analyzed','Needs reanalysis','Analyzing']);analysis_tools.addWidget(self.analysis_status_filter)
        self.button(analysis_tools,'Select not analyzed',self.select_pending_analysis)
        analysis_tools.addWidget(self.select_shown_button);analysis_tools.addWidget(self.clear_shown_button)
        self.button(analysis_tools,'Refresh status',self.refresh_analysis_status);analysis_tools.addStretch()
        self.analysis_device=QtWidgets.QComboBox();self.analysis_device.addItem('CPU','cpu');self._analysis_device_capabilities={};self._analysis_device_probe=None
        self.analysis_device.setCurrentIndex(max(0,self.analysis_device.findData(self.settings.get('analysis_device','cpu'))))
        self.analysis_device.setToolTip('Analysis only. GPU is selected by default when CUDA is available in the selected DeepLabCut environment.')
        self.locked_widgets.append(self.analysis_device)
        self.analysis_device.currentIndexChanged.connect(lambda *_:self.persist())
        self.analysis_status_filter.currentIndexChanged.connect(self.filter_video_rows)
        self.analysis_host=QtWidgets.QWidget();self.analysis_host_layout=QtWidgets.QVBoxLayout(self.analysis_host);self.analysis_host_layout.setContentsMargins(0,0,0,0);analysis.addWidget(self.analysis_host,1)
        self.evaluation_status=QtWidgets.QLabel();self.evaluation_status.setWordWrap(True);annotate.addWidget(self.evaluation_status)
        analyze_actions=QtWidgets.QHBoxLayout();analysis.addLayout(analyze_actions)
        self.analyze_button=self.action_button(analyze_actions,'Analyze selected videos','analyze',lambda:self.launch('analyze',videos=self.checked_sources('analysis')))
        analyze_actions.addWidget(QtWidgets.QLabel('Model'));analyze_actions.addWidget(self.analysis_models,1)
        analyze_actions.addWidget(QtWidgets.QLabel('Device'));analyze_actions.addWidget(self.analysis_device)
        results=self.page('')
        results.itemAt(0).widget().hide()
        results.setContentsMargins(0,0,0,0);results.setSpacing(4)
        toolbar=QtWidgets.QHBoxLayout();results.addLayout(toolbar)
        toolbar.addWidget(QtWidgets.QLabel('Video'))
        self.results=QtWidgets.QComboBox();self.results.setMinimumWidth(100)
        review_list=QtWidgets.QListView();review_list.setUniformItemSizes(True)
        self.results.setView(review_list);self.results.setMaxVisibleItems(12)
        review_list.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.results.setStyleSheet('QComboBox { combobox-popup: 0; }')
        self.results.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Fixed)
        toolbar.addWidget(self.results,1)
        self.review_filter=ProcessingModeComboBox();self.review_filter.addItems(['All analyzed','Postprocessed','Unprocessed']);toolbar.addWidget(self.review_filter)
        self.review_filter.currentIndexChanged.connect(self.review_filter_changed)
        self.review_previous_button=self.button(toolbar,'Previous video',lambda:self.step_review_video(-1))
        self.review_next_button=self.button(toolbar,'Next video',lambda:self.step_review_video(1))
        self.review_settings_toggle=QtWidgets.QToolButton();self.review_settings_toggle.setText('Settings')
        self.review_settings_toggle.setCheckable(True);self.review_settings_toggle.setToolTip('Show model and analysis run settings');toolbar.addWidget(self.review_settings_toggle)
        self.review_settings=QtWidgets.QWidget();settings_layout=QtWidgets.QFormLayout(self.review_settings)
        settings_layout.setContentsMargins(0,0,0,0)
        self.history=QtWidgets.QComboBox();self.history.currentIndexChanged.connect(self.history_selected)
        settings_layout.addRow('Analysis run',self.history)
        results.addWidget(self.review_settings);self.review_settings.hide()
        self.review_settings_toggle.toggled.connect(self.update_review_layout)
        self.preview_player=PosePreview();results.addWidget(self.preview_player,1)
        self.cutoff=self.preview_player.confidence
        self.locked_widgets.extend([self.history,self.results,self.review_filter])
        self.results.currentIndexChanged.connect(self.result_selected)
        self.build_postprocessing_page()
        post=self.pages.widget(self.pages.count()-1);self.pages.removeWidget(post);self.pages.insertWidget(self.POSTPROCESSING,post)
        self.build_export_page()
        self.configure_library_browser(self.LIBRARY)
        self.workflow_header.hide()

    def build_export_page(self):
        layout=self.page('All videos in this DLC project, independent of Files selection and name filters. Status uses the newest analysis for the selected model. Saved processing with different settings is identified below.')
        self.export_summary=QtWidgets.QLabel('Open Export to check result availability.');self.export_summary.setStyleSheet('font-weight:600');layout.addWidget(self.export_summary)
        row=QtWidgets.QHBoxLayout();layout.addLayout(row)
        self.button(row,'Select unexported',self.select_unexported_exports)
        self.button(row,'Select analyzed',lambda:self.select_export_rows(True))
        self.button(row,'Select postprocessed',self.select_postprocessed_exports)
        row=QtWidgets.QHBoxLayout();layout.addLayout(row)
        self.button(row,'Reverse selection',self.reverse_export_selection)
        self.button(row,'Clear selection',lambda:self.select_export_rows(False))
        self.export_table=QtWidgets.QTableWidget(0,8);self.export_table.setHorizontalHeaderLabels(['Analyzed','Postprocessed','Export','NWB / ImageSeries','Video','Exported','Analysis model','Postprocessing name'])
        self.export_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        header=self.export_table.horizontalHeader();header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(30);header.setStretchLastSection(False)
        for position,col in enumerate((2,5,0,6,1,7,3,4)):header.moveSection(header.visualIndex(col),position)
        self.export_table.setMinimumWidth(0);self.export_table.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,QtWidgets.QSizePolicy.Policy.Expanding)
        self.export_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.export_table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._fitting_export_columns=False;self._manual_export_columns=False;self._export_header_pressed=False
        self._export_fit_timer=QtCore.QTimer(self);self._export_fit_timer.setSingleShot(True);self._export_fit_timer.timeout.connect(self.fit_export_columns)
        self._export_viewport=self.export_table.viewport();self._export_viewport.installEventFilter(self)
        self._export_header_viewport=header.viewport();self._export_header_viewport.installEventFilter(self)
        header.sectionResized.connect(self.export_column_resized)
        layout.addWidget(self.export_table,1);self.locked_widgets.append(self.export_table)
        self.export_table.itemChanged.connect(self.export_selection_changed)
        self.export_selection_summary=QtWidgets.QLabel();layout.addWidget(self.export_selection_summary)
        self.export_button=self.button(layout,'Export selected videos to NWB (0)',self.export_selected)
        self.export_button.setToolTip('Review the export summary, then save one object per body part using applied postprocessing or original coordinates. Choose whether to skip or replace existing tracking from the same video.')

    def invalidate_export(self,*_):
        self._analysis_dirty=True;self._analysis_rows={};self._postprocessed_video_keys=None;self._exported_video_count=None
        self._export_dirty=True;self._export_rows=[]
        self.refresh_postprocessing_videos([])
        if hasattr(self,'export_table'):
            self.export_table.setRowCount(0);self.export_button.setText('Export selected videos to NWB (0)');self.export_summary.setText('Open Export or refresh status to check project videos.')
            self.export_button.setEnabled(False);self.export_selection_summary.setText('Refresh status to select videos for export.')
        self.schedule_analysis_status()
        if self.pages.currentIndex()==self.EXPORT and not self.is_running():QtCore.QTimer.singleShot(0,lambda:self.safe(self.refresh_export) if not self.is_running() else None)

    def schedule_analysis_status(self,*_):
        if self._analysis_dirty:
            QtCore.QTimer.singleShot(0,lambda:self.safe(self.refresh_analysis_status) if self._analysis_dirty and not self.is_running() else None)

    def refresh_analysis_status(self):
        if self.is_running():return
        config=self.project.currentData()
        if not config:return
        shuffle,fraction,snapshot=self.shuffle.value(),self.fraction.value(),self.snapshot.value()
        info=next((p for p in self.projects if p['config']==config),{})
        checkpoints=[p for m in info.get('models',[]) if m['shuffle']==shuffle and m['trainingsetindex']==fraction for p in m.get('snapshots',[])]
        sources=[dict(s) for s in self.sources];self._analysis_dirty=False
        self.analysis_summary.setText('Checking saved analysis results…')
        jobs=self.job_roots();directories=self.processed_roots();settings=self.postprocessing_config()
        def work():
            rows=project_analysis_status(jobs,sources,config,shuffle,fraction,snapshot,checkpoints,self._analysis_checkpoint_cache)
            exports=project_export_status(jobs,sources,config,shuffle,fraction,settings,directories,self._export_nwb_cache)
            return rows,postprocessed_video_keys(sources,directories),exports
        self.start_thread(work,lambda value:self.analysis_status_loaded(value[0],value[1],value[2]))

    def analysis_status_loaded(self,rows,processed_keys=None,export_rows=None):
        if processed_keys is not None:self._postprocessed_video_keys=set(processed_keys)
        if export_rows is not None:self.export_status_loaded(export_rows)
        self._analysis_rows={self.source_key(r['source']):r for r in rows};self._analysis_dirty=False
        self.table.blockSignals(True)
        try:
            if self.pages.currentIndex()==self.ANALYSIS:
                for i,s in enumerate(self.sources):
                    row=self._analysis_rows.get(self.source_key(s),{})
                    self.render_analysis_row(i,row)
        finally:self.table.blockSignals(False)
        self.filter_video_rows()

    def render_analysis_row(self,index,row):
        model=row.get('model') or {}
        key=f"{model.get('shuffle')}:{model.get('trainingsetindex')}"
        name=self.model_records().get(key,{}).get('name') or model.get('name')
        if not name and model:name=f"Model {model.get('shuffle','?')}"
        stamp=row.get('analyzed_at') or ''
        display=stamp
        if stamp:
            try:display=datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:pass
        values={4:row.get('status','Not analyzed'),6:name or '',7:display}
        for col,value in values.items():
            item=self.table.item(index,col)
            if item is None:item=QtWidgets.QTableWidgetItem();self.table.setItem(index,col,item)
            item.setText(value)
            if col==4:item.setToolTip(row.get('detail',''))
            elif col==6:item.setToolTip(f"{name} · Shuffle {model.get('shuffle')} · Fraction index {model.get('trainingsetindex')}" if model else '')
            else:item.setToolTip(('Run timestamp, exact per-video completion time was not recorded by this older version. ' if row.get('date_is_run') else 'Video analysis completed at: ')+stamp if stamp else 'No recorded completion date.')

    def select_pending_analysis(self):
        if self._analysis_dirty:
            raise ValueError('Refresh analysis status before selecting videos that need analysis.')
        self.table.blockSignals(True)
        try:
            for row,source in enumerate(self.sources):
                if not self.table.isRowHidden(row) and 'error' not in source:
                    status=self._analysis_rows.get(self.source_key(source),{}).get('status')
                    self.set_role_checked(row,1,status in ('Not analyzed','Needs reanalysis'))
        finally:self.table.blockSignals(False)
        self.video_choices_changed()

    def edit_test_videos(self):
        if self.model_is_locked():raise ValueError('This model is trained and its training selection is locked. Create a new model to change it.')
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('Videos excluded from training');dialog.resize(720,450);layout=QtWidgets.QVBoxLayout(dialog)
        note=QtWidgets.QLabel('Checked videos are excluded from future training. Existing models retain what they learned. These exclusions do not evaluate accuracy or select videos for analysis.');note.setWordWrap(True);layout.addWidget(note)
        table=QtWidgets.QTableWidget(len(self.sources),2);table.setHorizontalHeaderLabels(['Exclude','Video / NWB']);layout.addWidget(table)
        table.horizontalHeader().setSectionResizeMode(1,QtWidgets.QHeaderView.ResizeMode.Stretch)
        for i,source in enumerate(self.sources):
            check=QtWidgets.QTableWidgetItem();check.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled|QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(self.table.item(i,5).checkState());table.setItem(i,0,check)
            item=QtWidgets.QTableWidgetItem(Path(source.get('video','')).name+' · '+Path(source['nwb']).name);item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled);table.setItem(i,1,item)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save|QtWidgets.QDialogButtonBox.StandardButton.Cancel);layout.addWidget(buttons);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
        if dialog.exec()==QtWidgets.QDialog.DialogCode.Accepted:
            self.table.blockSignals(True)
            try:
                for row in range(len(self.sources)):
                    excluded=table.item(row,0).checkState()==QtCore.Qt.CheckState.Checked
                    self.table.item(row,5).setCheckState(table.item(row,0).checkState())
                    if excluded:self.table.item(row,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
            finally:self.table.blockSignals(False)
            self.video_choices_changed()

    def refresh_export(self):
        if self.is_running():return
        config=self.project.currentData()
        if not config:raise ValueError('Select a DLC project first.')
        sources=[dict(source) for source in self.sources];settings=self.postprocessing_config()
        shuffle=self.shuffle.value();fraction=self.fraction.value()
        self._export_dirty=False;self.export_summary.setText(f'Checking {len(sources)} project videos…')
        self.start_thread(lambda:project_export_status(self.job_roots(),sources,config,shuffle,fraction,settings,self.processed_roots(),self._export_nwb_cache),self.export_status_loaded)

    def export_status_loaded(self,rows):
        self._manual_export_columns=False;self._export_fit_timer.start(0)
        self._export_rows=rows;self.refresh_postprocessing_videos(rows);self._export_dirty=False;self._exported_video_count=sum(row.get('exported',False) for row in rows)
        state=self.video_ui.get(self._active_project or '__new__',{})
        chosen=set(state.get('export',[self.source_key(row['source']) for row in rows if row['analyzed']]))
        self.export_table.blockSignals(True)
        try:
            self.export_table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                source=row['source'];processed='Yes' if row['current_processed'] else 'Yes (other settings)' if row['postprocessed'] else 'No'
                model=row.get('model') or {};record=self.model_records().get(f"{model.get('shuffle')}:{model.get('trainingsetindex')}",{})
                model_name=record.get('name') or model.get('name') or (f"Model {model.get('shuffle')}" if model else '')
                values={0:'Yes' if row['analyzed'] else 'No',1:processed,3:Path(source['nwb']).name+'\n'+source.get('object_path',''),4:Path(source.get('video','')).name,5:'Yes' if row.get('exported',False) else 'No',6:model_name,7:', '.join(row.get('postprocessing_names',[]))}
                for col,value in values.items():
                    item=QtWidgets.QTableWidgetItem(value);item.setToolTip(source['nwb']+'\n'+source.get('video','')+'\n'+row['detail']);self.export_table.setItem(i,col,item)
                check=QtWidgets.QTableWidgetItem();check.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled|QtCore.Qt.ItemFlag.ItemIsUserCheckable if row['analyzed'] else QtCore.Qt.ItemFlag.NoItemFlags)
                check.setCheckState(QtCore.Qt.CheckState.Checked if row['analyzed'] and self.source_key(source) in chosen else QtCore.Qt.CheckState.Unchecked);self.export_table.setItem(i,2,check)
        finally:self.export_table.blockSignals(False)
        self.export_summary.setText(f"Total videos: {len(rows)}   |   Analyzed: {sum(row['analyzed'] for row in rows)}   |   Postprocessed: {sum(row['postprocessed'] for row in rows)}   |   Exported: {sum(row.get('exported',False) for row in rows)}")
        self.export_selection_changed()
        if self.pages.currentIndex()==self.REVIEW:self.history_selected()

    def export_checked_sources(self):
        if not hasattr(self,'export_table'):return []
        return [row['source'] for i,row in enumerate(self._export_rows) if row['analyzed'] and self.export_table.item(i,2) is not None and self.export_table.item(i,2).checkState()==QtCore.Qt.CheckState.Checked]

    def export_selection_changed(self,*_):
        chosen=self.export_checked_sources()
        self.export_button.setText(f'Export selected videos to NWB ({len(chosen)})')
        self.video_ui.setdefault(self._active_project or '__new__',{})['export']=[self.source_key(source) for source in chosen]
        self.export_selection_summary.setText(f'{len(chosen)} video(s) selected for export. Saved postprocessing is exported when available; otherwise original coordinates are exported.')
        self.persist();self.update_workflow()

    def select_export_rows(self,checked):
        self.export_table.blockSignals(True)
        try:
            for i,row in enumerate(self._export_rows):self.export_table.item(i,2).setCheckState(QtCore.Qt.CheckState.Checked if checked and row['analyzed'] else QtCore.Qt.CheckState.Unchecked)
        finally:self.export_table.blockSignals(False)
        self.export_selection_changed()

    def select_postprocessed_exports(self):
        self.export_table.blockSignals(True)
        try:
            for index,row in enumerate(self._export_rows):
                checked=row['analyzed'] and row['postprocessed']
                self.export_table.item(index,2).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        finally:self.export_table.blockSignals(False)
        self.export_selection_changed()

    def select_unexported_exports(self):
        self.export_table.blockSignals(True)
        try:
            for index,row in enumerate(self._export_rows):
                checked=row['analyzed'] and not row.get('exported',False)
                self.export_table.item(index,2).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        finally:self.export_table.blockSignals(False)
        self.export_selection_changed()

    def reverse_export_selection(self):
        self.export_table.blockSignals(True)
        try:
            for index,row in enumerate(self._export_rows):
                item=self.export_table.item(index,2)
                checked=row['analyzed'] and item.checkState()!=QtCore.Qt.CheckState.Checked
                item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        finally:self.export_table.blockSignals(False)
        self.export_selection_changed()

    def unprocessed_postprocessing_sources(self):
        return [row['source'] for row in self._pp_video_rows if not row['postprocessed']]

    def postprocess_all_unprocessed(self):
        sources=self.unprocessed_postprocessing_sources()
        if not sources:raise ValueError('No analyzed videos are available without postprocessing.')
        self.postprocess_selected(sources)

    def open_postprocessing_selection(self):
        self.pp_selection_dialog.open()

    def remove_all_postprocessing(self):
        sources=[row['source'] for row in self._pp_video_rows if row['postprocessed']]
        if not sources:raise ValueError('No postprocessed videos are available.')
        self.remove_selected_postprocessing(sources)

    def postprocess_selected(self,sources=None):
        settings=self.postprocessing_config()
        if not settings['name'].strip():raise ValueError('Enter a postprocessing name.')
        sources=self.checked_postprocessing_sources() if sources is None else sources
        config=self.project.currentData();shuffle=self.shuffle.value();fraction=self.fraction.value()
        all_sources=[dict(source) for source in self.sources];directories=self.processed_roots()
        def work():
            matches,missing=selected_analysis_results(self.job_roots(),sources,config,shuffle,fraction)
            report=dict(processed=0,missing=missing,errors=[],warnings=[],sources=[])
            for result,run,path in matches:
                try:
                    saved=save_postprocessed(result,settings,self.project_storage('postprocessed'));report['processed']+=1;report['sources'].append(result['source'])
                    report['warnings'].extend(Path(result['source']['nwb']).name+': '+warning for warning in saved['warnings'])
                except Exception as exc:report['errors'].append(Path(result['source']['nwb']).name+': '+str(exc))
            report['processed_keys']=postprocessed_video_keys(all_sources,directories)
            return report
        def done(report):
            self._postprocessed_video_keys=report['processed_keys']
            removed=self.postprocessing_removed_sources()
            removed.difference_update(self.postprocessing_source_key(source) for source in report['sources'])
            self.save_removed_postprocessing(removed);self.apply_postprocessing_preview()
            message=f"Postprocessing: {report['processed']} saved, {report['missing']} without analysis, {len(report['errors'])} failed."
            self.pp_status.setText(message);self.log.appendPlainText(message)
            for error in report['warnings']+report['errors']:self.log.appendPlainText(error)
            self._export_dirty=True
        self.start_thread(work,done)

    def postprocessing_source_key(self,source):
        return self.model_key()+'|'+self.source_key(source)

    def postprocessing_removed_sources(self):
        return set(self.video_ui.get(self._active_project or '__new__',{}).get('postprocessing_removed',[]))

    def save_removed_postprocessing(self,removed):
        self.video_ui.setdefault(self._active_project or '__new__',{})['postprocessing_removed']=sorted(removed)
        self._export_dirty=True;self.persist();self.update_workflow()

    def postprocessing_for_source(self,source):
        settings=self.postprocessing_config()
        if self.postprocessing_source_key(source) in self.postprocessing_removed_sources():settings['enabled']=False
        return settings

    def remove_selected_postprocessing(self,sources=None):
        sources=self.checked_postprocessing_sources() if sources is None else sources
        parent=self.pp_selection_dialog if self.pp_selection_dialog.isVisible() else self
        message=f'Are you sure you want to remove postprocessing from {len(sources)} video(s)?'
        buttons=QtWidgets.QMessageBox.StandardButton
        if QtWidgets.QMessageBox.question(parent,'Remove postprocessing',message,buttons.Yes|buttons.No,buttons.No)!=buttons.Yes:return
        config=self.project.currentData()
        shuffle,fraction=self.shuffle.value(),self.fraction.value()
        directories=self.processed_roots();all_sources=[dict(source) for source in self.sources]
        def work():
            matches,missing=selected_analysis_results(self.job_roots(),sources,config,shuffle,fraction)
            report=remove_postprocessed([result for result,run,path in matches],directories)
            report['processed_keys']=postprocessed_video_keys(all_sources,directories)
            return report
        def done(report):
            self._postprocessed_video_keys=report['processed_keys']
            failed=set(report['failed_sources'])
            removed=self.postprocessing_removed_sources()
            removed.update(self.postprocessing_source_key(source) for source in sources if self.source_key(source) not in failed)
            self.save_removed_postprocessing(removed);self.apply_postprocessing_preview()
            message=f"Removed postprocessing for {len(sources)-len(failed)} selected video(s); {report['removed']} saved result(s) deleted. Raw analyses and existing NWB exports are unchanged."
            self.pp_status.setText(message);self.log.appendPlainText(message)
            for error in report['errors']:self.log.appendPlainText(error)
        self.start_thread(work,done)

    def checked_postprocessing_sources(self,required=True):
        selected={index.row() for index in self.pp_video_table.selectionModel().selectedRows()}
        sources=[row['source'] for index,row in enumerate(self._pp_video_rows) if index in selected]
        if required and not sources:raise ValueError('Select videos in the Postprocessing video list first.')
        return sources

    def postprocessing_selection_changed(self,*_):
        state=self.video_ui.setdefault(self._active_project or '__new__',{})
        state.setdefault('postprocessing_selection',{})[self.model_key()]=[self.source_key(source) for source in self.checked_postprocessing_sources(False)]
        self.persist();self.update_workflow()

    def refresh_postprocessing_videos(self,rows):
        if not hasattr(self,'pp_video_table'):return
        selected=set(self.video_ui.get(self._active_project or '__new__',{}).get('postprocessing_selection',{}).get(self.model_key(),[]))
        self._pp_video_rows=[row for row in rows if row['analyzed']]
        self.pp_video_table.blockSignals(True)
        try:
            self.pp_video_table.clearSelection()
            self.pp_video_table.setRowCount(len(self._pp_video_rows))
            for index,row in enumerate(self._pp_video_rows):
                source=row['source']
                model=row.get('model') or {};record=self.model_records().get(f"{model.get('shuffle')}:{model.get('trainingsetindex')}",{})
                values=[Path(source.get('video','')).name,record.get('name') or model.get('name') or f"Model {model.get('shuffle','?')}",'Postprocessed' if row['postprocessed'] else 'Unprocessed',', '.join(row.get('postprocessing_names',[]))]
                for col,value in enumerate(values):
                    item=QtWidgets.QTableWidgetItem(value);item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled|QtCore.Qt.ItemFlag.ItemIsSelectable)
                    item.setToolTip(source['nwb']+'\n'+source.get('object_path','')+'\n'+value);self.pp_video_table.setItem(index,col,item)
            self.select_postprocessing_rows([index for index,row in enumerate(self._pp_video_rows) if self.source_key(row['source']) in selected])
        finally:self.pp_video_table.blockSignals(False)
        self.update_workflow()

    def select_postprocessing_rows(self,rows):
        selection=QtCore.QItemSelection();model=self.pp_video_table.model()
        for row in rows:selection.select(model.index(row,0),model.index(row,self.pp_video_table.columnCount()-1))
        flags=QtCore.QItemSelectionModel.SelectionFlag
        self.pp_video_table.selectionModel().select(selection,flags.ClearAndSelect|flags.Rows)

    def select_postprocessing_videos(self,mode):
        current={index.row() for index in self.pp_video_table.selectionModel().selectedRows()}
        self.pp_video_table.blockSignals(True)
        try:
            self.select_postprocessing_rows([index for index,row in enumerate(self._pp_video_rows) if mode=='all' or (mode=='unprocessed' and not row['postprocessed']) or (mode=='reverse' and index not in current)])
        finally:self.pp_video_table.blockSignals(False)
        self.postprocessing_selection_changed()

    def build_postprocessing_page(self):
        layout=self.page('Configure postprocessing, then apply it to all unprocessed analyzed videos or choose a custom selection.')
        name_row=QtWidgets.QHBoxLayout();name_row.addWidget(QtWidgets.QLabel('Postprocessing name'))
        self.pp_name=QtWidgets.QLineEdit('Postprocessing');name_row.addWidget(self.pp_name,1);layout.insertLayout(0,name_row)
        self.pp_name.textChanged.connect(self.postprocessing_changed)
        self.pp_selection_dialog=QtWidgets.QDialog(self);self.pp_selection_dialog.setWindowTitle('Custom postprocessing video selection')
        self.pp_selection_dialog.resize(950,550)
        selection_layout=QtWidgets.QVBoxLayout(self.pp_selection_dialog)
        selection_note=QtWidgets.QLabel('Click a row to select a video. Use Ctrl-click or Shift-click to select multiple videos. This selection is independent of Analysis and Export.')
        selection_note.setWordWrap(True);selection_layout.addWidget(selection_note)
        selection_tools=QtWidgets.QHBoxLayout();selection_layout.addLayout(selection_tools)
        for text,mode in [('Select unprocessed','unprocessed'),('Select all','all'),('Reverse selection','reverse'),('Clear','clear')]:
            self.button(selection_tools,text,lambda mode=mode:self.select_postprocessing_videos(mode))
        self.pp_video_table=QtWidgets.QTableWidget(0,4);self.pp_video_table.setHorizontalHeaderLabels(['Video','Analysis model','Status','Postprocessing name'])
        self.pp_video_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pp_video_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.pp_video_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.pp_video_table.horizontalHeader().setSectionResizeMode(0,QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.pp_video_table.setMinimumHeight(100);selection_layout.addWidget(self.pp_video_table,1)
        self.pp_video_table.itemSelectionChanged.connect(self.postprocessing_selection_changed);self.locked_widgets.append(self.pp_video_table)
        form=QtWidgets.QFormLayout();layout.addLayout(form)
        self.pp_confidence=PostprocessingDoubleSpinBox();self.pp_confidence.setRange(0,1);self.pp_confidence.setSingleStep(.05);self.pp_confidence.setValue(.75)
        self.pp_anchor=PostprocessingDoubleSpinBox();self.pp_anchor.setRange(0,1);self.pp_anchor.setSingleStep(.05);self.pp_anchor.setValue(.9)
        self.pp_gap=PostprocessingDoubleSpinBox();self.pp_gap.setRange(.001,60);self.pp_gap.setDecimals(3);self.pp_gap.setValue(.2);self.pp_gap.setSuffix(' s')
        self.pp_gap.setToolTip('Maximum elapsed time between the two confident measured frames bracketing a gap. Uses synchronized NWB timestamps.')
        self.pp_estimator=ProcessingModeComboBox();self.pp_estimator.addItems(['Median','Mean'])
        self.pp_min_samples=PostprocessingSpinBox();self.pp_min_samples.setRange(1,10000000);self.pp_min_samples.setValue(10)
        form.addRow('Minimum confidence',self.pp_confidence);form.addRow('Surrounding-point confidence',self.pp_anchor)
        form.addRow('Maximum gap (between anchors)',self.pp_gap);form.addRow('Fixed-point estimator',self.pp_estimator);form.addRow('Minimum confident fixed-point samples',self.pp_min_samples)
        self.pp_table=QtWidgets.QTableWidget(0,2);self.pp_table.setHorizontalHeaderLabels(['Body part','Processing mode'])
        self.pp_table.horizontalHeader().setStretchLastSection(True);layout.addWidget(self.pp_table,1)
        note=QtWidgets.QLabel('Moving: discard uncertain detections and interpolate short interior gaps only. Fixed: one median/mean position per video from confident detections. Unprocessed: preserve original coordinates. Names containing "corner" initially use Fixed; other points use Moving. No camera movement correction is applied.')
        note.setWordWrap(True);layout.addWidget(note)
        actions=QtWidgets.QHBoxLayout();layout.addLayout(actions)
        self.pp_apply_all_button=self.button(actions,'Apply to all unprocessed videos (0)',self.postprocess_all_unprocessed)
        self.pp_remove_all_button=self.button(actions,'Remove postprocessing from all videos (0)',self.remove_all_postprocessing)
        self.pp_remove_all_button.setToolTip('Remove postprocessing from all postprocessed project videos for the active model. Raw analyses and existing NWB exports are preserved.')
        self.pp_custom_button=self.button(actions,'Custom video selection',self.open_postprocessing_selection)
        custom_actions=QtWidgets.QHBoxLayout();selection_layout.addLayout(custom_actions)
        self.pp_apply_button=self.button(custom_actions,'Apply to selected videos',self.postprocess_selected)
        self.pp_remove_button=self.button(custom_actions,'Remove postprocessing from selected',self.remove_selected_postprocessing)
        self.pp_remove_button.setToolTip('Delete postprocessing for selected videos. Raw analyses and existing NWB exports are preserved.')
        close_button=QtWidgets.QPushButton('Close');close_button.clicked.connect(self.pp_selection_dialog.accept);custom_actions.addWidget(close_button)
        self.pp_status=QtWidgets.QLabel();self.pp_status.setWordWrap(True);layout.addWidget(self.pp_status)
        self.pp_controls=[self.pp_name,self.pp_confidence,self.pp_anchor,self.pp_gap,self.pp_estimator,self.pp_min_samples,self.pp_table]
        self.locked_widgets.extend(self.pp_controls)
        for widget in [self.pp_confidence,self.pp_anchor,self.pp_gap,self.pp_estimator,self.pp_min_samples]:
            signal=widget.toggled if isinstance(widget,QtWidgets.QCheckBox) else widget.currentIndexChanged if isinstance(widget,QtWidgets.QComboBox) else widget.valueChanged
            signal.connect(self.postprocessing_changed)

    def postprocessing_config(self):
        modes=dict(self.postprocessing_settings.get(self.project.currentData() or '__new__',{}).get('modes',{}))
        for row in range(self.pp_table.rowCount()):modes[self.pp_table.item(row,0).text()]=self.pp_table.cellWidget(row,1).currentData()
        settings=normalize_settings(dict(enabled=True,confidence=self.pp_confidence.value(),anchor_confidence=self.pp_anchor.value(),
                                       max_gap_seconds=self.pp_gap.value(),fixed_estimator=self.pp_estimator.currentText().lower(),
                                       fixed_min_samples=self.pp_min_samples.value(),modes=modes))
        settings['name']=self.pp_name.text().strip()
        return settings

    def load_postprocessing(self,parts=None):
        if not hasattr(self,'pp_table'):return
        config=self.project.currentData() or '__new__'
        cfg=normalize_settings(self.postprocessing_settings.get(config))
        if parts is None:
            info=next((p for p in self.projects if p['config']==config),{})
            parts=list(dict.fromkeys(list(info.get('bodyparts',[]))+list(cfg['modes'])))
        self._pp_loading=True
        try:
            self.pp_name.setText(self.postprocessing_settings.get(config,{}).get('name','Postprocessing'));self.pp_confidence.setValue(cfg['confidence']);self.pp_anchor.setValue(cfg['anchor_confidence'])
            self.pp_gap.setValue(cfg['max_gap_seconds']);self.pp_estimator.setCurrentIndex(0 if cfg['fixed_estimator']=='median' else 1);self.pp_min_samples.setValue(cfg['fixed_min_samples'])
            self.pp_table.setRowCount(0)
            for part in parts:
                row=self.pp_table.rowCount();self.pp_table.insertRow(row)
                item=QtWidgets.QTableWidgetItem(str(part));item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable);self.pp_table.setItem(row,0,item)
                combo=ProcessingModeComboBox()
                for text,key in [('Moving point','moving'),('Fixed point','fixed'),('Unprocessed','raw')]:combo.addItem(text,key)
                combo.setCurrentIndex(combo.findData(cfg['modes'].get(part,'fixed' if 'corner' in part.lower() else 'moving')))
                combo.currentIndexChanged.connect(self.postprocessing_changed);self.pp_table.setCellWidget(row,1,combo)
        finally:self._pp_loading=False
        self.postprocessing_settings[config]=self.postprocessing_config()

    def postprocessing_changed(self,*_):
        if self._pp_loading:return
        # Keep the endpoint threshold at least as strict as the missing-point threshold.
        if self.pp_anchor.value()<self.pp_confidence.value():
            self.pp_anchor.blockSignals(True);self.pp_anchor.setValue(self.pp_confidence.value());self.pp_anchor.blockSignals(False)
        self.postprocessing_settings[self.project.currentData() or '__new__']=self.postprocessing_config()
        self._export_dirty=True
        self.persist();self.apply_postprocessing_preview();self.update_workflow()

    def apply_postprocessing_preview(self):
        player=self.preview_player;cfg=self.postprocessing_for_source(self.preview_result['source']) if self.preview_result else self.postprocessing_config()
        player.processed_xy=None;player.processed_status=None;player.processed_toggle.setEnabled(cfg['enabled'])
        if player.pose is not None and player.source is not None and cfg['enabled']:
            xy,status,warnings=process_pose(player.pose,player.parts,player.source['timestamps'],cfg)
            player.processed_xy=xy;player.processed_status=status
            counts=f'{int((status==2).sum())} interpolated points; {int((status==3).sum())} fixed estimates; {int((status==0).sum())} missing points.'
            self.pp_status.setText(counts+(' '+ ' '.join(warnings) if warnings else '')+' Review: hollow circles = interpolated; squares = fixed; filled circles = original.')
        else:self.pp_status.setText('Postprocessing was removed for this video.' if not cfg['enabled'] else 'Select an analyzed video in Review to preview these settings.')
        player.confidence.setEnabled(not (player.processed_toggle.isChecked() and player.processed_xy is not None))
        player.update_legend()
        player.redraw_pose()

    def set_target_paths(self,paths):
        self.targets=[str(p) for p in paths if str(p).lower().endswith('.nwb')]
        self._target_keys={self.nwb_path_key(path) for path in self.targets}
        self.selection.setText(f'{len(self.targets)} NWB file(s) selected. The video list follows this selection.' if self.targets else 'No NWB files selected: showing all project videos.')
        self.filter_video_rows()
    def is_running(self):return self.process is not None or self.worker is not None or bool(getattr(getattr(self,"detector",None),"running",False))
    def set_busy(self):
        busy=self.is_running()
        for widget in self.buttons+self.locked_widgets:widget.setEnabled(not busy)
        self.cancel.setEnabled(self.process is not None or bool(getattr(getattr(self,"detector",None),"running",False)))
        self.export_button.setEnabled(not busy and bool(self.project.currentData()) and bool(self.export_checked_sources()))
        if hasattr(self,'environment_cancel'):
            self.environment_cancel.setEnabled(bool(getattr(getattr(self,'detector',None),'running',False)) or (self.process is not None and self.job.get('action')=='check'))
        self.save_bodyparts_button.setEnabled(not busy and bool(self.project.currentData()))
        self.update_environment_status()
        self.update_workflow();self.update_job_status()
        self.runningStateChanged.emit()
    def persist(self):
        self.remember_video_ui()
        save_json(self.settings_path,dict(python=self.python.text().strip(),projects=self.projects,current=self.project.currentData(),copy_videos=bool(self.video_storage.currentData()),environments=getattr(self,"detected_environments",self.settings.get("environments",[])),analysis_device=self.analysis_device.currentData() if hasattr(self,'analysis_device') else self.settings.get('analysis_device','cpu'),video_ui=self.video_ui,postprocessing=self.postprocessing_settings))
    def refresh_projects(self,select=None):
        select=select or self.settings.get('current')
        self.project.blockSignals(True);self.project.clear()
        for info in self.projects:self.project.addItem(info.get('name',Path(info['config']).parent.name),info['config'])
        if select:
            index=self.project.findData(select)
            if index>=0:self.project.setCurrentIndex(index)
        self.project.blockSignals(False);self.project_changed()
    def project_changed(self,*_):
        self.remember_video_ui();self._refreshing_project=True
        cfg=self.project.currentData();self._active_project=cfg;info=next((p for p in self.projects if p['config']==cfg),{})
        self.heading.setText(info.get('name','Create or open a project'));self.heading.setToolTip(str(cfg or ''))
        self.bodyparts.setText(', '.join(info.get('bodyparts', ['nose','left_ear','right_ear','tail_base'])))
        self.scorer.setText(info.get('scorer','') if cfg else 'user')
        self.label_bodyparts.setText(self.bodyparts.text())
        self.scorer.setPlaceholderText('Refresh projects / models to load the current scorer' if cfg and 'scorer' not in info else 'user')
        self.save_bodyparts_button.setEnabled(bool(cfg) and not self.is_running())
        self.models.blockSignals(True);self.models.clear()
        for model in info.get('models',[]):
            self.models.addItem('Shuffle %s • fraction %s • %s checkpoint(s)'%(model['shuffle'],model['fraction'],len(model['snapshots'])),model)
        trained_indices=[i for i in range(self.models.count()) if self.models.itemData(i).get('snapshots')]
        if trained_indices:self.models.setCurrentIndex(trained_indices[0])
        self.models.blockSignals(False);self.model_selected(self.models.currentIndex())
        cached=self.video_ui.get(cfg or '__new__',{}).get('sources',[])
        self.scanned(cached,remember=False)
        self.load_postprocessing()
        self.review_filter.blockSignals(True);self.review_filter.setCurrentText(self.video_ui.get(cfg or '__new__',{}).get('review_filter','All analyzed'));self.review_filter.blockSignals(False)
        self.invalidate_export()
        self.refresh_history()
        self._refreshing_project=False;self.refresh_named_models();self.update_workflow();self.persist()
    def project_storage(self,child,config=None):
        config=config or self.project.currentData()
        return storage_root(config)/child if config else self.root/'_setup_jobs'/child

    def job_roots(self):
        return [self.project_storage('jobs'),self.root/'jobs',self.root/'_setup_jobs']

    def processed_roots(self):
        return [self.project_storage('postprocessed'),self.root/'postprocessed']

    def model_key(self):
        return f'{self.shuffle.value()}:{self.fraction.value()}'

    def model_records(self):
        return self.video_ui.setdefault(self._active_project or '__new__',{}).setdefault('model_records',{})

    def current_model_record(self):
        if not hasattr(self,'shuffle'):return {}
        return self.model_records().get(self.model_key(),{})

    def model_is_locked(self):
        if not hasattr(self,'shuffle'):return False
        info=next((p for p in self.projects if p['config']==self._active_project),{})
        return any(m['shuffle']==self.shuffle.value() and m['trainingsetindex']==self.fraction.value() and m.get('snapshots') for m in info.get('models',[])) or bool(self.current_model_record().get('trained'))

    def refresh_named_models(self):
        if not hasattr(self,'creation_models') or self._switching_model:return
        info=next((p for p in self.projects if p['config']==self._active_project),{})
        state=self.video_ui.setdefault(self._active_project or '__new__',{});records=self.model_records()
        for model in info.get('models',[]):
            key=f"{model['shuffle']}:{model['trainingsetindex']}"
            record=records.setdefault(key,dict(name=f"Model {model['shuffle']}",training=[],testing=[]))
            if model.get('snapshots'):
                record['trained']=True
                history=info.get('trained_models',{}).get(key,{})
                sources=history.get('training_selection')
                if not sources and not record.get('history_checked'):
                    for path in job_paths(self.job_roots(),'request.json'):
                        try:
                            job=_pp_json_read(path);result=_pp_json_read(path.with_name('result.json'))
                            if job.get('action')=='train' and result.get('status')=='completed' and self.nwb_path_key(job.get('config',''))==self.nwb_path_key(self._active_project or '') and f"{job.get('shuffle',1)}:{job.get('trainingsetindex',0)}"==key:
                                sources=job.get('training_selection');record['name']=record.get('name_override') or job.get('model_name') or record['name'];break
                        except (OSError,ValueError,TypeError):continue
                    record['history_checked']=True
                if sources:
                    record.update(training=[self.source_key(s) for s in sources],training_sources=sources,selection_known=True)
                    record['name']=record.get('name_override') or history.get('name') or record['name']
                    if history.get('labeling') is not None:record['labeling']=history['labeling']
                elif 'selection_known' not in record:
                    # Saved draft selections from this update remain authoritative.
                    record['selection_known']=bool(record.get('training'))
        key=self.model_key()
        records.setdefault(key,dict(name=f'Model {self.shuffle.value()}',training=list(state.get('training',[])),testing=list(state.get('testing',[]))))
        for combo in (self.creation_models,self.analysis_models):
            previous=combo.blockSignals(True)
            try:
                combo.clear()
                for ident,record in sorted(records.items(),key=lambda pair:tuple(int(n) for n in pair[0].split(':'))):
                    combo.addItem(record['name']+(' · Trained / locked' if record.get('trained') else ' · Draft'),ident)
                combo.setCurrentIndex(combo.findData(key))
            finally:combo.blockSignals(previous)
        self.select_named_model(key,remember=False)
        for i in range(self.models.count()):
            model=self.models.itemData(i);record=records.get(f"{model['shuffle']}:{model['trainingsetindex']}",{})
            self.models.setItemText(i,record.get('name','Model')+f" · Shuffle {model['shuffle']} · Fraction {model['fraction']}")

    def select_named_model(self,key,remember=True):
        if self._switching_model:return
        if remember:self.remember_video_ui()
        record=self.model_records()[key];state=self.video_ui[self._active_project or '__new__']
        self._switching_model=True;previous=self.table.blockSignals(True)
        try:
            self._active_model_key=key
            shuffle,fraction=map(int,key.split(':'));self.shuffle.setValue(shuffle);self.fraction.setValue(fraction)
            state['training']=list(record.get('training',[]));state['testing']=list(record.get('testing',[]))
            for row,source in enumerate(self.sources):
                self.table.item(row,0).setCheckState(QtCore.Qt.CheckState.Checked if self.source_key(source) in state['training'] else QtCore.Qt.CheckState.Unchecked)
                self.table.item(row,5).setCheckState(QtCore.Qt.CheckState.Checked if self.source_key(source) in state['testing'] else QtCore.Qt.CheckState.Unchecked)
            self.epochs.setValue(record.get('epochs',200));self.frames.setValue(record.get('frames',20));self.update_training_device()
        finally:self.table.blockSignals(previous);self._switching_model=False
        self.update_workflow();self.filter_video_rows();self.persist()

    def creation_model_selected(self,*_):
        key=self.creation_models.currentData()
        if key and not self._switching_model:self.select_named_model(key)

    def analysis_model_selected(self,*_):
        key=self.analysis_models.currentData()
        if key and not self._switching_model:self.select_named_model(key)

    def rename_named_model(self):
        if not self.project.currentData():raise ValueError('Create or open a project first.')
        if self.is_running():return
        record=self.current_model_record()
        name,ok=QtWidgets.QInputDialog.getText(self,'Rename model','Model name',text=record.get('name',''))
        if not ok:return
        name=name.strip()
        if not name:raise ValueError('Enter a model name.')
        if any(key!=self.model_key() and r['name'].casefold()==name.casefold() for key,r in self.model_records().items()):
            raise ValueError('A model already uses this name.')
        record.update(name=name,name_override=name)
        self.refresh_named_models();self.persist()

    def new_named_model(self):
        if not self.project.currentData():raise ValueError('Create or open a project first.')
        name,ok=QtWidgets.QInputDialog.getText(self,'New model','Model name')
        if not ok:return
        name=name.strip()
        if not name:raise ValueError('Enter a model name.')
        records=self.model_records()
        if any(r['name'].casefold()==name.casefold() for r in records.values()):raise ValueError('A model already uses this name.')
        self.remember_video_ui()
        shuffle=max([int(k.split(':')[0]) for k in records]+[0])+1
        if shuffle>999:raise ValueError('No unused shuffle is available.')
        key=f'{shuffle}:0';records[key]=dict(name=name,training=[],testing=[],epochs=200,device=self.device.currentText(),frames=self.frames.value(),selection_known=True)
        self.select_named_model(key,remember=False);self.refresh_named_models()

    def apply_model_lock(self):
        if not hasattr(self,'creation_models'):return
        locked=self.model_is_locked();record=self.current_model_record();busy=self.is_running()
        self.model_lock_note.setText(('Trained model: training videos and Train model are locked. Create a new model to train a different selection.' + (' Historical training-video selection is unavailable for this older model.' if not record.get('selection_known') else '')) if locked else 'Draft model: select training videos, label frames, then train. Training locks this model and its video selection.')
        self.training_identity.setText('Model: '+record.get('name','Model')+f' · Shuffle {self.shuffle.value()}')
        self.excluded_videos_button.setEnabled(not busy and not locked)
        missing=set(record.get('training',[]))-{self.source_key(s) for s in self.sources}
        if locked and missing:self.model_lock_note.setText(self.model_lock_note.text()+f' {len(missing)} saved training video(s) are outside the library.')
        for widget in (self.select_shown_button,self.clear_shown_button,self.random_percentage,self.random_select_button):
            widget.setEnabled(not busy and not (locked and self.pages.currentIndex()==self.LABELING))
        previous=self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                item=self.table.item(row,0)
                if item:
                    flags=item.flags()
                    item.setFlags(flags & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable if locked or busy else flags | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            for row,source in enumerate(self.sources):
                item=self.table.item(row,1)
                if item and 'error' not in source:
                    flags=item.flags();item.setFlags(flags & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable if busy else flags | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        finally:self.table.blockSignals(previous)

    def model_selected(self,index):
        if not self._restoring and not getattr(self,'_refreshing_project',False):self.remember_video_ui()
        model=self.models.itemData(index)
        if model:self.shuffle.setValue(model['shuffle']);self.fraction.setValue(model['trainingsetindex'])
        if hasattr(self,'creation_models') and not self._restoring and not getattr(self,'_refreshing_project',False):self.refresh_named_models()
        self.update_workflow()
    def update_review_layout(self,*_):
        review=self.pages.currentIndex()==self.REVIEW
        expanded=self.review_settings_toggle.isChecked()
        self.workflow_header.setVisible(self.pages.currentIndex() not in (self.LIBRARY,self.LABELING,self.ANALYSIS,self.POSTPROCESSING,self.EXPORT) and (not review or expanded))
        self.review_settings.setVisible(expanded)

    def update_navigation(self):
        if not hasattr(self,'next_stage_button'):return
        index=max(0,self.stages.currentRow())
        self.previous_stage_button.setEnabled(index>0)
        self.next_stage_button.setVisible(index<self.EXPORT)
        if index<self.EXPORT:self.next_stage_button.setText('Next: '+self.STAGE_NAMES[index+1].replace('&','&&'))

    def eventFilter(self,watched,event):
        kind=event.type()
        if watched is getattr(self,'_video_header_viewport',None):
            if kind in (QtCore.QEvent.Type.MouseButtonPress,QtCore.QEvent.Type.MouseButtonDblClick):
                self._video_header_pressed=event.button()==QtCore.Qt.MouseButton.LeftButton
            elif kind==QtCore.QEvent.Type.MouseButtonRelease:self._video_header_pressed=False
        if watched is getattr(self,'_export_header_viewport',None):
            if kind in (QtCore.QEvent.Type.MouseButtonPress,QtCore.QEvent.Type.MouseButtonDblClick):self._export_header_pressed=event.button()==QtCore.Qt.MouseButton.LeftButton
            elif kind==QtCore.QEvent.Type.MouseButtonRelease:self._export_header_pressed=False
        if watched is getattr(self,'_export_viewport',None) and kind in (QtCore.QEvent.Type.Resize,QtCore.QEvent.Type.Show):self._export_fit_timer.start(0)
        if watched is getattr(self,'_video_viewport',None) and kind in (QtCore.QEvent.Type.Resize,QtCore.QEvent.Type.Show):
            self.schedule_video_column_fit()
        return super().eventFilter(watched,event)

    def schedule_video_column_fit(self):
        if hasattr(self,'_video_fit_timer'):self._video_fit_timer.start(0)

    def video_column_resized(self,*_):
        # Splitter drags and section changes also resize headers while the mouse
        # is held. Only a drag in this header counts as a manual width override.
        if not self._fitting_video_columns and self._video_header_pressed:self._manual_video_columns=True

    def video_column_sizes(self,table=None,checkbox_columns=(0,1,5)):
        table=self.table if table is None else table
        header=table.horizontalHeader();header_metrics=header.fontMetrics()
        minima={};desired={}
        visible_rows=[row for row in range(table.rowCount()) if not table.isRowHidden(row)]
        for col in range(table.columnCount()):
            if table.isColumnHidden(col):continue
            title=table.horizontalHeaderItem(col).text()
            floor=max(30,header_metrics.horizontalAdvance(title)+20)
            minima[col]=floor;desired[col]=floor
            if col in checkbox_columns:continue
            for row in visible_rows:
                item=table.item(row,col)
                if item:
                    metrics=QtGui.QFontMetrics(item.font())
                    width=max((metrics.horizontalAdvance(line) for line in item.text().splitlines()),default=0)+20
                    desired[col]=max(desired[col],width)
        return minima,desired

    def fit_video_columns(self):
        if self._manual_video_columns or self._fitting_video_columns:return
        widths=self.content_fitted_widths(self.table,(0,1,5))
        self._fitting_video_columns=True
        try:
            for col,width in widths.items():self.table.setColumnWidth(col,width)
        finally:self._fitting_video_columns=False

    def content_fitted_widths(self,table,checkbox_columns):
        minima,widths=self.video_column_sizes(table,checkbox_columns)
        if not widths:return {}
        available=max(0,table.viewport().width()-1)
        # Cap every column at its longest shown cell/header. If necessary,
        # shrink columns in proportion to their room above header width.
        deficit=max(0,sum(widths.values())-available)
        room=sum(widths[col]-minima[col] for col in widths)
        if deficit and room:
            reduction=min(deficit,room);remaining=reduction
            flexible=[col for col in widths if widths[col]>minima[col]]
            for col in flexible:
                amount=min(widths[col]-minima[col],remaining if col==flexible[-1] else reduction*(widths[col]-minima[col])//room)
                widths[col]-=amount;remaining-=amount
            if remaining:
                for col in reversed(flexible):
                    amount=min(remaining,widths[col]-minima[col]);widths[col]-=amount;remaining-=amount
                    if not remaining:break
        return widths

    def export_column_resized(self,col,old,new):
        if self._fitting_export_columns or not self._export_header_pressed:return
        self._manual_export_columns=True
        _,caps=self.video_column_sizes(self.export_table,(2,))
        if col in caps and new>caps[col]:
            self._fitting_export_columns=True
            try:self.export_table.setColumnWidth(col,caps[col])
            finally:self._fitting_export_columns=False

    def fit_export_columns(self):
        if self._manual_export_columns or self._fitting_export_columns:return
        widths=self.content_fitted_widths(self.export_table,(2,))
        self._fitting_export_columns=True
        try:
            for col,width in widths.items():self.export_table.setColumnWidth(col,width)
        finally:self._fitting_export_columns=False

    def configure_library_browser(self,index):
        self._manual_video_columns=False
        library=index==self.LIBRARY;training=index==self.LABELING
        self.role_controls.setVisible(training);self.random_controls.hide()
        destination=self.analysis_selection_tools if index==self.ANALYSIS else self.role_controls.layout()
        for position,button in enumerate((self.select_shown_button,self.clear_shown_button),2 if index==self.ANALYSIS else 0):
            destination.insertWidget(position,button)
        self.label_parameters_button.setVisible(training)
        self.library_filters.show()
        self.library_actions.setVisible(library)
        self.select_library_button.setVisible(library)
        self.refresh_library_button.hide()
        self.library_role_note.hide()
        self.test_remaining_button.setVisible(index==self.ANALYSIS)
        self.table.setColumnHidden(0,not training);self.table.setColumnHidden(5,index!=self.ANALYSIS)
        self.table.setColumnHidden(1,index!=self.ANALYSIS)
        self.table.setColumnHidden(4,library or training)
        self.table.setColumnHidden(6,index!=self.ANALYSIS);self.table.setColumnHidden(7,index!=self.ANALYSIS)
        self.video_filter_count.setVisible(not training)
        self.table.verticalHeader().setVisible(not library)
        self.selection_purpose.setCurrentIndex(self.selection_purpose.findData(0 if training else 1))
        self.random_select_button.setText('Select random training videos' if training else 'Select random analysis videos')
        for widget in (self.random_label,self.random_percentage,self.random_select_button,self.test_remaining_button):widget.setVisible(training and widget is not self.test_remaining_button)
        self.table.setColumnHidden(5,True)
        self.table.horizontalHeaderItem(1).setText('Select')
        self.select_shown_button.setText('Select all shown')
        previous=self.table.blockSignals(True)
        try:
            for row,source in enumerate(self.sources):
                result=self._analysis_rows.get(self.source_key(source)) if index==self.ANALYSIS else None
                item=self.table.item(row,4)
                if index==self.ANALYSIS:self.render_analysis_row(row,result or dict(status='Checking…' if self._analysis_dirty else 'Not analyzed'))
                elif item:item.setText(source.get('error',str(source.get('frames',0))+' frames'))
        finally:self.table.blockSignals(previous)
        self.filter_video_rows()
        self.schedule_video_column_fit()

    def select_library_rows(self):
        selection=QtCore.QItemSelection()
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                selection.select(self.table.model().index(row,0),self.table.model().index(row,self.table.columnCount()-1))
        self.table.selectionModel().select(selection,QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect | QtCore.QItemSelectionModel.SelectionFlag.Rows)

    def show_stage(self,index):
        if index>=0:
            if index in (self.LIBRARY,self.LABELING,self.ANALYSIS):
                host={self.LIBRARY:self.library_host_layout,self.LABELING:self.labeling_host_layout,self.ANALYSIS:self.analysis_host_layout}[index]
                if self.library_browser.parentWidget() is not host.parentWidget():host.addWidget(self.library_browser)
            if index==self.LABELING:self.refresh_named_models()
            self.pages.setCurrentIndex(index)
            if index in (self.LABELING,self.ANALYSIS):self.ensure_analysis_device()
            if index in (self.LIBRARY,self.LABELING,self.ANALYSIS):self.configure_library_browser(index)
            self.update_review_layout();self.update_navigation();self.update_workflow();self.right_panel.show_deeplabcut_view()
            if index==self.REVIEW:self.refresh_history()
            if index==self.ANALYSIS and self._analysis_dirty and not self.is_running():self.safe(self.refresh_analysis_status)
            if index in (self.POSTPROCESSING,self.EXPORT) and self._export_dirty and not self.is_running():self.safe(self.refresh_export)
    def show_project_settings(self,new=False):
        self.create_button.setVisible(bool(new));self.save_bodyparts_button.setVisible(not new)
        self.delete_project_button.setVisible(not new)
        self.bodyparts_hint.setVisible(not new);self.project_folder_button.setVisible(not new)
        self.new_project_hint.hide();self.project_close_button.setVisible(not new)
        self.project_close_button.setText('Close')
        self.name.setReadOnly(not new)
        self.project_dialog.setWindowTitle('New DeepLabCut project' if new else 'Project settings')
        self.project_dialog.layout().activate()
        self.project_dialog.resize(540,self.project_dialog.sizeHint().height())
        self.project_dialog.show();self.project_dialog.raise_();self.project_dialog.activateWindow()

    def delete_project(self):
        if self.is_running():raise ValueError('Wait for the current task to finish before deleting a project.')
        config=self.project.currentData()
        if not config:raise ValueError('Select a project first.')
        info=next(p for p in self.projects if p['config']==config)
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('Delete DeepLabCut project')
        layout=QtWidgets.QVBoxLayout(dialog)
        label=QtWidgets.QLabel('Remove "'+info.get('name',Path(config).parent.name)+'" from the project list?');label.setWordWrap(True);layout.addWidget(label)
        path_label=QtWidgets.QLabel(str(Path(config).parent));path_label.setWordWrap(True);path_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse);layout.addWidget(path_label)
        erase=QtWidgets.QCheckBox('Also permanently delete the project folder, labels and models');layout.addWidget(erase)
        try:
            managed_project_folder(self.root,config,info.get('project_path'))
            note='With this option off, all project files are kept. With it on, the entire project folder is deleted, including copied videos. Linked original videos outside the folder are kept. NWB files and Workbench job history are kept.'
        except ValueError as exc:
            erase.setEnabled(False);note=str(exc)+' Removing the registration keeps all files.'
        hint=QtWidgets.QLabel(note);hint.setWordWrap(True);layout.addWidget(hint)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel);layout.addWidget(buttons)
        confirm=buttons.addButton('Remove from list',QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        erase.toggled.connect(lambda checked:confirm.setText('Permanently delete' if checked else 'Remove from list'))
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
        execute=getattr(dialog,'exec',None) or dialog.exec_
        if execute()!=QtWidgets.QDialog.DialogCode.Accepted:return
        self.project_dialog.hide();self.clear_preview()
        if erase.isChecked():
            # Validate again inside the worker before touching disk.
            def remove_folder():
                folder=managed_project_folder(self.root,config,info.get('project_path'))
                shutil.rmtree(folder)
                return True
            self.start_thread(remove_folder,lambda _:self.forget_project(config,True))
        else:self.forget_project(config,False)

    def forget_project(self,config,deleted=False):
        self.remember_video_ui()
        self.projects=[p for p in self.projects if p['config']!=config]
        self.video_ui.pop(config,None)
        next_config=self.projects[0]['config'] if self.projects else None
        self.settings['current']=next_config
        self._restoring=True
        self.refresh_projects(next_config)
        self._restoring=False
        self.persist();self.update_workflow()
        message='Project folder deleted.' if deleted else 'Project removed from the list. Files kept.'
        self.log.appendPlainText(message);self._last_job_status=message;self.update_job_status()

    def update_workflow(self,*_):
        if not hasattr(self,'parts_summary'):return
        cfg=self.project.currentData();info=next((p for p in self.projects if p['config']==cfg),{})
        chosen=[m for m in info.get('models',[]) if m['shuffle']==self.shuffle.value() and m['trainingsetindex']==self.fraction.value()]
        dataset=bool(chosen);trained=any(m.get('snapshots') for m in chosen);busy=self.is_running()
        self.update_training_summary(info,chosen,busy)
        counts=getattr(self,'_selection_counts',[0,0]);registered=len(info.get('video_sets',[]))
        for action,button in self.action_buttons.items():
            reason=''
            if not cfg:reason='Create or open a project first.'
            elif action=='sync_videos' and not self.video_ui.get(self._active_project or '__new__',{}).get('catalog_initialized',bool(self.sources)):reason='Add videos to the project library first.'
            elif action=='train' and self.model_is_locked():reason='This model is trained and locked. Create a new model to train again.'
            elif action=='train' and not self.selected_training_sources() and not registered:reason='Select training videos and label their frames first.'
            elif action=='train' and info.get('labeling') and not any(row.get('labeled_frames',0) for row in info['labeling']) and len(self.selected_training_sources())==registered:reason='Save labels before training. Refresh projects / models after labeling outside Workbench.'
            elif action in {'train','evaluate','analyze'} and self._analysis_device_probe is not None:reason='Checking CUDA availability in the DeepLabCut environment.'
            elif action in {'evaluate','analyze'} and not trained:reason='Train or select a model with a saved checkpoint first.'
            elif action=='add_videos' and not counts[0]:reason='Select Training videos in the project library.'
            elif action=='analyze' and not counts[1]:reason='Select Analysis videos from the project library in Analyze videos.'
            elif action in {'extract','annotate','check_labels'} and not registered and not self.selected_training_sources():reason='Add training videos to the project first.'
            button.setEnabled(not busy and not reason);button.setToolTip(reason)
        self.analysis_device.setEnabled(not busy and self._analysis_device_probe is None)
        self.device.setEnabled(not busy and self._analysis_device_probe is None)
        self.apply_model_lock()
        self.table.setEnabled(not busy or bool(self.process is not None and self.job and self.job.get('action')=='analyze' and self.pages.currentIndex()==self.ANALYSIS))
        # Keep scrolling and row navigation available while export writes files.
        self.export_table.setEnabled(not busy or self.importing)
        if getattr(self,'_export_selection_locked',None)!=busy:
            self._export_selection_locked=busy
            previous=self.export_table.blockSignals(True)
            try:
                for row in range(self.export_table.rowCount()):
                    item=self.export_table.item(row,2)
                    if item is None:continue
                    flags=item.flags()
                    if busy:flags &= ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    elif self._export_rows[row]['analyzed']:flags |= QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    item.setFlags(flags)
            finally:self.export_table.blockSignals(previous)
        self.training_summary.setText(f"{len(self.current_model_record().get('training',[]))} video(s) in the saved training selection" if self.model_is_locked() else f'{len(self.selected_training_sources())} training video(s) · {self.frames.value()} frames per new video')
        self.label_bodyparts_save.setEnabled(not busy and bool(cfg))
        parts=info.get('bodyparts',[]);self.parts_summary.setText('Body parts: '+(', '.join(parts) if parts else 'set these in project settings'))
        self.export_button.setEnabled(not busy and bool(cfg) and bool(self.export_checked_sources()))
        self.pp_apply_button.setEnabled(not busy and bool(cfg) and bool(self.checked_postprocessing_sources(False)) and bool(self.pp_name.text().strip()))
        available=len(self.unprocessed_postprocessing_sources())
        self.pp_apply_all_button.setText(f'Apply to all unprocessed videos ({available})')
        self.pp_apply_all_button.setEnabled(not busy and bool(cfg) and bool(available) and bool(self.pp_name.text().strip()))
        processed=sum(bool(row['postprocessed']) for row in self._pp_video_rows)
        self.pp_remove_all_button.setText(f'Remove postprocessing from all videos ({processed})')
        self.pp_remove_all_button.setEnabled(not busy and bool(cfg) and bool(processed))
        self.pp_custom_button.setEnabled(not busy and bool(cfg))
        self.pp_remove_button.setEnabled(not busy and bool(cfg) and bool(self.checked_postprocessing_sources(False)))
        self.pp_apply_button.setText(f'Apply to selected videos ({len(self.checked_postprocessing_sources(False))})')
        self.save_scorer_button.setEnabled(not busy and bool(cfg))
        self.project_delete_button.setEnabled(not busy and bool(cfg))
        self.delete_project_button.setEnabled(not busy and bool(cfg))
        self.create_button.setEnabled(not busy)
        self.create_button.setToolTip('Create an empty project; select training videos afterward.')
        self.add_library_button.setEnabled(not busy and bool(cfg))
        self.remove_library_button.setEnabled(not busy and bool(self.sources))
        self.refresh_library_button.setEnabled(not busy and bool(self.sources))
        analyzed=sum(self._analysis_rows.get(self.source_key(source),{}).get('status')=='Analyzed' for source in self.sources)
        checked=all(self.source_key(source) in self._analysis_rows for source in self.sources)
        analysis_status=(f'{analyzed} videos analyzed'+(' · Refresh needed' if self._analysis_dirty else '') if checked else 'Checking saved analyses…')
        statuses=[f'{len(self.sources)} videos',f'{counts[0]} training videos · '+('Model trained' if trained else 'Ready to label / train'),analysis_status,
                  (f'{len(self._postprocessed_video_keys)} videos postprocessed' if self._postprocessed_video_keys is not None else '0 videos postprocessed' if not self.sources else 'Checking saved postprocessing…'),'',(f'{self._exported_video_count} videos exported' if self._exported_video_count is not None else 'Checking saved exports…' if self.sources else '0 videos exported')]
        stage_height=self.stages.fontMetrics().lineSpacing()*2+8
        for i,title in enumerate(self.STAGE_NAMES):
            self.stages.item(i).setText(f'{title}\n{statuses[i]}' if statuses[i] else title)
            self.stages.item(i).setSizeHint(QtCore.QSize(0,stage_height))
        descriptions=(
            'Add videos to the saved project library.',
            'Label training frames, then train the model. Dataset preparation is automatic.',
            'Select Analysis videos for the selected NWB files and generate predictions. Name filters do not clear checked selections.',
            'Optionally configure moving and fixed points. These settings apply to the selected videos during preview and export.',
            'Compare raw and processed predictions for individual videos.',
            'All project videos and their saved result status. Select videos to export to NWB.')
        self.next_step.setText(descriptions[max(0,self.stages.currentRow())])

    def show_training_parameters(self):
        self.training_parameters_dialog.show();self.training_parameters_dialog.raise_();self.training_parameters_dialog.activateWindow()

    def update_training_summary(self,info,chosen,busy):
        if not hasattr(self,'evaluation_status'):return
        snapshots=[p for m in chosen for p in m.get('snapshots',[])]
        labels=info.get('labeling')
        if self.model_is_locked() and self.current_model_record().get('labeling') is not None:labels=self.current_model_record()['labeling']
        labeled=sum(bool(row.get('labeled')) for row in labels or [])
        partial=sum(bool(row.get('labeled')) and row.get('labeled_frames',0)<row.get('extracted_frames',0) for row in labels or [])
        label_text=(f'{labeled}/{len(labels)} registered videos have labels · {sum(row.get("labeled_frames",0) for row in labels)} labeled frames' if labels is not None else 'Status unknown; refresh projects / models')
        if partial:label_text+=f' · {partial} partially labeled video(s)'
        stale=f'{self.shuffle.value()}:{self.fraction.value()}' in info.get('stale_training_sets',[])
        training=('Trained; '+str(len(snapshots))+' checkpoint(s)' if snapshots else 'Ready to train; dataset prepared automatically' if labeled else 'Label frames before training' if labels is not None else 'Save labels before training')
        if stale:training+=' · Selection changed; use a new shuffle for an existing trained model'
        evaluated=any(m.get('evaluation') for m in chosen)
        evaluation='Completed; saved report available in Analyze videos' if evaluated else 'Ready to evaluate' if snapshots else 'Train a model before evaluation'
        self.training_status.setText(f'Labels: {label_text}\nTraining: {training}\nEvaluation: {evaluation}')
        self.training_status.setToolTip('Counts describe registered training videos. Train model prepares or updates an untrained dataset when needed. Use a new shuffle to train changed labels without replacing a trained model.')
        evaluation=next((m.get('evaluation') for m in chosen if m.get('evaluation')),None)
        self.evaluation_status.setText(('Saved evaluation: '+(' · '.join(f'{k}: {v}' for k,v in evaluation.get('metrics',{}).items()) or 'open evaluation files for details')) if evaluation else 'No saved evaluation report for this model.')
        if hasattr(self,'analysis_models'):
            for combo in (self.creation_models,self.analysis_models):
                previous=combo.blockSignals(True)
                try:combo.setCurrentIndex(combo.findData(self.model_key()))
                finally:combo.blockSignals(previous)
            checkpoint='DLC default' if self.snapshot.value()==-1 else str(self.snapshot.value())
            self.analysis_models.setToolTip(f'Checkpoint: {checkpoint}. Configure checkpoint selection in Advanced settings.' if snapshots else 'Train this model in Model creation before analyzing videos.')
            self.rename_model_button.setEnabled(not busy and bool(self.project.currentData()))

    def update_live_analysis(self):
        if self.process is None or not self.job or self.job.get('action')!='analyze':return
        path=Path(self.job['job_dir'])/'result.json'
        try:
            stat=path.stat();stamp=(str(path),stat.st_size,stat.st_mtime_ns)
            if stamp==getattr(self,'_live_analysis_stamp',None):return
            run=_pp_json_read(path)
            if not run.get('checkpointed') or not usable_analysis_run(run):return
            model=run.get('model',{})
            if self.nwb_path_key(model.get('config',''))!=self.nwb_path_key(self.project.currentData() or '') or model.get('shuffle')!=self.shuffle.value() or model.get('trainingsetindex')!=self.fraction.value() or model.get('snapshot_index',-1)!=self.snapshot.value():return
            self._live_analysis_stamp=stamp
        except (OSError,ValueError,TypeError,AttributeError):return
        for result in run.get('results',[]):
            source=result.get('source',{})
            if source.get('nwb') and Path(result.get('predictions','')).is_file():
                self._analysis_rows[self.source_key(source)]=dict(source=source,status='Analyzed',detail='Completed and saved during the current analysis run.',model=model,analyzed_at=result.get('analyzed_at'),date_is_run=False)
        self._analysis_live_summary=f"{len(run.get('results',[]))}/{run.get('total_videos',len(self.job.get('videos',[])))} saved"
        index=run.get('current_video_index');videos=self.job.get('videos',[])
        if isinstance(index,int) and 1<=index<=len(videos):
            source=videos[index-1]
            self._analysis_rows[self.source_key(source)]=dict(source=source,status='Analyzing',detail=f'Analyzing video {index}/{len(videos)}. Completed videos are saved individually.',model=model)
        if self.pages.currentIndex()==self.ANALYSIS:
            previous=self.table.blockSignals(True)
            try:
                for row,source in enumerate(self.sources):
                    status=self._analysis_rows.get(self.source_key(source))
                    if status:
                        self.render_analysis_row(row,status)
            finally:self.table.blockSignals(previous)
            self.filter_video_rows()
        self._export_dirty=True

    def update_job_status(self):
        if not hasattr(self,'job_status'):return
        if self.is_running():
            self.update_live_analysis()
            if not self.job_clock.isValid():self.job_clock.start();self.job_timer.start()
            elapsed=self.job_clock.elapsed()//1000
            action=self.job.get('action','Working') if self.process and self.job else 'Checking environment' if getattr(getattr(self,'detector',None),'running',False) else 'Preparing data'
            if action=='analyze' and getattr(self,'_analysis_live_summary',''):action+=' · '+self._analysis_live_summary
            if self.importing:action='Exporting · '+getattr(self,'_export_live_summary','Starting')
            self.job_status.setText(f'{action.replace("_"," ").capitalize()} | {elapsed//60}:{elapsed%60:02d}')
        else:
            self.job_timer.stop();self.job_clock.invalidate();self.job_status.setText(self._last_job_status)

    def show_environment_settings(self):
        self.environment_dialog.show();self.environment_dialog.raise_();self.environment_dialog.activateWindow()

    def training_device_selected(self,*_):
        if self._switching_model:return
        record=self.current_model_record()
        record.update(device=self.device.currentText(),device_manual=True,device_environment=self.nwb_path_key(self.python.text().strip()))
        self.persist()

    def update_training_device(self):
        if not hasattr(self,'device') or not hasattr(self,'_analysis_device_capabilities'):return
        key=self.nwb_path_key(self.python.text().strip())
        available=self._analysis_device_capabilities.get(key,False)
        record=self.current_model_record()
        manual=record.get('device_manual') and record.get('device_environment')==key
        choice=record.get('device','cpu') if manual else 'cuda:0' if available else 'cpu'
        if choice=='cuda:0' and not available:choice='cpu'
        previous=self.device.blockSignals(True)
        try:
            self.device.clear();self.device.addItem('cpu')
            if available:self.device.addItem('cuda:0')
            self.device.setCurrentText(choice)
        finally:self.device.blockSignals(previous)
        self.device.setToolTip('Training and evaluation. GPU is selected by default when CUDA is available. You can choose CPU manually.')

    def update_analysis_device(self,cuda_available,prefer=False):
        if not hasattr(self,'analysis_device'):return
        key=self.nwb_path_key(self.python.text().strip())
        self._analysis_device_capabilities[key]=cuda_available
        self._analysis_device_env_key=key
        current=self.analysis_device.currentData()
        self.analysis_device.blockSignals(True);self.analysis_device.clear()
        if cuda_available:self.analysis_device.addItem('GPU (CUDA:0)','cuda:0')
        self.analysis_device.addItem('CPU','cpu')
        choice='cuda:0' if cuda_available and (prefer or current!='cpu') else 'cpu'
        self.analysis_device.setCurrentIndex(self.analysis_device.findData(choice));self.analysis_device.blockSignals(False)
        self.analysis_device.setToolTip('Analysis only. CUDA is available in this DeepLabCut environment.' if cuda_available else 'Analysis only. CUDA is unavailable in this DeepLabCut environment; using CPU.')
        self.update_training_device();self.persist();self.update_workflow()

    def ensure_analysis_device(self):
        if not hasattr(self,'analysis_device') or self.process is not None:return
        python=self.python.text().strip();key=self.nwb_path_key(python)
        if not Path(python).is_file():return
        if key in self._analysis_device_capabilities:
            if key!=getattr(self,'_analysis_device_env_key',None):self.update_analysis_device(self._analysis_device_capabilities[key],prefer=True)
            return
        if self._analysis_device_probe is not None:return
        probe=QtCore.QProcess(self);self._analysis_device_probe=probe
        probe.setProcessEnvironment(child_environment(python));probe.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        self.analysis_device.setToolTip('Checking CUDA availability in the selected DeepLabCut environment…')
        def done(code,*_):
            if self._analysis_device_probe is not probe:return
            output=bytes(probe.readAllStandardOutput()).decode('utf-8',errors='replace')
            self._analysis_device_probe=None;probe.deleteLater()
            if self.nwb_path_key(self.python.text().strip())==key:
                available=code==0 and 'NWB_CUDA=1' in output
                self.update_analysis_device(available,prefer=True)
                if code!=0:self.log.appendPlainText('Could not confirm CUDA availability; CPU selected. Use the environment Test button to check details.')
            else:self.ensure_analysis_device()
            self.update_workflow()
        probe.finished.connect(done)
        probe.errorOccurred.connect(lambda error:done(-1) if error==QtCore.QProcess.ProcessError.FailedToStart else None)
        script="import os\nif os.name=='nt':\n import ctypes\n ctypes.windll.kernel32.SetDllDirectoryW(None)\nimport torch\nprint('NWB_CUDA='+str(int(torch.cuda.is_available())),flush=True)"
        start_external(probe,python,['-u','-c',script])
        QtCore.QTimer.singleShot(30000,lambda:probe.kill() if self._analysis_device_probe is probe else None)
        self.update_workflow()

    def update_environment_status(self):
        if not hasattr(self,'python'):return
        path=self.python.text().strip()
        known=any(v['python']==path for v in getattr(self,'detected_environments',[]))
        valid=bool(path and Path(path).is_file() and (known or path==self.validated_python))
        detecting=bool(getattr(getattr(self,'detector',None),'running',False))
        testing=self.process is not None and self.job.get('action')=='check'
        if detecting or testing:
            label='Checking DeepLabCut environment…';color='#b77900'
        elif valid:
            prefix=Path(path).parent
            if prefix.name.lower() in {'bin','scripts'}:prefix=prefix.parent
            label='DeepLabCut environment detected: '+prefix.name;color='#218739'
        else:
            label='DeepLabCut environment needs checking' if path else 'DeepLabCut environment not detected';color='#c53030'
        self.environment_dot.setStyleSheet('color:'+color+';')
        self.environment_status.setText(label)
        self.environment_status.setToolTip(label+('\n'+path if path else '')+'\nClick ⚙ for details.')
        if hasattr(self,'pages') and self.pages.currentIndex() in (self.LABELING,self.ANALYSIS):self.ensure_analysis_device()

    def populate_environments(self,values,selected=''):
        self.detected_environments=list(values)
        self.environments.blockSignals(True);self.environments.clear()
        for item in values:
            path=Path(item['python']);prefix=path.parent.parent if path.parent.name in {'bin','Scripts'} else path.parent
            label=prefix.name+' • DLC '+str(item.get('dlc_version',''))
            if item.get('cuda_available'):label+=' • CUDA'
            self.environments.addItem(label,str(path))
            self.environments.setItemData(self.environments.count()-1,str(path),QtCore.Qt.ItemDataRole.ToolTipRole)
        if selected and self.environments.findData(selected)<0:
            self.environments.addItem('Saved selection • '+Path(selected).parent.name,selected)
        index=self.environments.findData(selected)
        if index>=0:self.environments.setCurrentIndex(index)
        self.environments.blockSignals(False)
        self.update_environment_status()

    def environment_selected(self,index):
        path=self.environments.itemData(index)
        if path and hasattr(self,'python'):
            self.python.setText(path);self.persist()

    def detect_environments(self):
        if self.is_running():return
        self.validated_python=''
        self.environment_log.clear()
        self.log.appendPlainText('Detecting DeepLabCut environments…')
        self.detector.start([self.python.text()] if self.python.text().strip() else [])
        self.set_busy()

    def environments_detected(self,result):
        values=result['environments'];previous=self.python.text().strip()
        selected=previous if any(v['python']==previous for v in values) else values[0]['python'] if values else previous
        self.populate_environments(values,selected)
        if values:
            self.python.setText(selected);self.persist()
            self.environment.setText(str(len(values))+' DLC environment(s) available. Select one from the list if needed.')
        elif result.get('cancelled'):
            self.environment.setText('Detection cancelled. Browse… is still available.')
        else:
            self.environment.setText('No usable DLC 3/PyTorch environment found. Use Browse… for a custom installation.')
        for item in result['failures']:
            self.log.appendPlainText(item['python']+' : '+item['error'])
            self.environment_log.appendPlainText(item['python']+' : '+item['error'])
        self.set_busy()

    def choose_python(self):
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,'DeepLabCut environment Python',self.python.text(),'Python (python.exe python python3);;All files (*)')
        if path:
            self.python.setText(path);self.populate_environments(getattr(self,'detected_environments',[]),path);self.persist()
    def open_config(self):
        path,_=QtWidgets.QFileDialog.getOpenFileName(self,'DeepLabCut project',str(self.root),'Configuration (config.yaml *.yaml)')
        if path:self.launch('inspect',config=path)
    def inspect_project(self):self.launch('inspect')
    def open_project_folder(self,child=''):
        if not self.project.currentData():raise ValueError('Select a project.')
        path=Path(self.project.currentData()).parent/child
        if not path.exists():raise ValueError('Folder not found: '+str(path))
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
    @staticmethod
    def source_key(source):return str(source.get('nwb',''))+'|'+str(source.get('object_path',''))

    @staticmethod
    def nwb_path_key(path):return os.path.normcase(os.path.abspath(str(path)))

    def source_file_selected(self,source):
        return not self.targets or self.nwb_path_key(source['nwb']) in getattr(self,'_target_keys',set())

    def remember_video_ui(self):
        if self._restoring or self._switching_model or getattr(self,'_refreshing_project',False) or not hasattr(self,'table'):return
        key=self._active_project or '__new__'
        state=self.video_ui.setdefault(key,{'training':[],'analysis':[]})
        for col,purpose in ((0,'training'),(1,'analysis'),(5,'testing')):
            selected=set(state.get(purpose,[]))
            for i,source in enumerate(self.sources):
                item=self.table.item(i,col)
                if item is None:continue
                ident=self.source_key(source)
                if item.checkState()==QtCore.Qt.CheckState.Checked:selected.add(ident)
                else:selected.discard(ident)
            state[purpose]=sorted(selected)
        if self._active_model_key and not self._switching_model:
            record=state.setdefault('model_records',{}).setdefault(self._active_model_key,dict(name='Model '+self._active_model_key.split(':')[0]))
            if not self.model_is_locked():
                record.update(training=list(state.get('training',[])),testing=list(state.get('testing',[])),epochs=self.epochs.value(),device=self.device.currentText(),frames=self.frames.value())
        if hasattr(self,'frames'):state['label_frames']=self.frames.value()
        state.update(catalog_initialized=bool(self.sources) or state.get('catalog_initialized',False),object_name=self.object_name_filter.currentData() or '',search=self.object_search.text(),sources=self.sources)
        if hasattr(self,'shuffle'):state.update(shuffle=self.shuffle.value(),fraction=self.fraction.value(),snapshot=self.snapshot.value())

    def restore_video_ui(self):
        if not hasattr(self,'table'):return
        self._restoring=True;self.table.blockSignals(True)
        try:
            state=self.video_ui.get(self._active_project or '__new__',{})
            if hasattr(self,'shuffle'):
                shuffle,fraction=state.get('shuffle',1),state.get('fraction',0)
                for i in range(self.models.count()):
                    model=self.models.itemData(i)
                    if model and model['shuffle']==shuffle and model['trainingsetindex']==fraction:self.models.setCurrentIndex(i);break
                self.shuffle.setValue(shuffle);self.fraction.setValue(fraction);self.snapshot.setValue(state.get('snapshot',-1))
            if hasattr(self,'frames'):self.frames.setValue(state.get('label_frames',20))
            name=state.get('object_name','')
            if name and self.object_name_filter.findData(name)<0:self.object_name_filter.addItem(name,name)
            self.object_name_filter.setCurrentIndex(max(0,self.object_name_filter.findData(name)))
            self.object_search.setText(state.get('search',''))
            for i,source in enumerate(self.sources):
                for col,purpose in ((0,'training'),(1,'analysis'),(5,'testing')):
                    item=self.table.item(i,col)
                    if item:item.setCheckState(QtCore.Qt.CheckState.Checked if self.source_key(source) in state.get(purpose,[]) else QtCore.Qt.CheckState.Unchecked)
            for row in range(len(self.sources)):
                if self.table.item(row,5).checkState()==QtCore.Qt.CheckState.Checked:self.table.item(row,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        finally:self.table.blockSignals(False);self._restoring=False
        self.filter_video_rows()

    def set_role_checked(self,row,col,checked):
        if col==0 and self.model_is_locked():return
        self.table.item(row,col).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        if checked and col==0:self.table.item(row,5).setCheckState(QtCore.Qt.CheckState.Unchecked)
        if checked and col==5:
            self.table.item(row,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.table.item(row,1).setCheckState(QtCore.Qt.CheckState.Checked)

    def video_choices_changed(self,item=None):
        if self._restoring:return
        if item is not None and item.column() in (0,5) and item.checkState()==QtCore.Qt.CheckState.Checked:
            self.table.blockSignals(True)
            try:self.set_role_checked(item.row(),item.column(),True)
            finally:self.table.blockSignals(False)
        self.update_video_filter_count();self.persist()

    def open_evaluation_folder(self):
        cfg=self.project.currentData()
        if not cfg:raise ValueError('Select a project first.')
        info=next((p for p in self.projects if p['config']==cfg),{})
        root=Path(info.get('project_path',Path(cfg).parent))
        for child in ('evaluation-results-pytorch','evaluation-results'):
            path=root/child
            if path.is_dir():QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)));return
        raise ValueError('No evaluation folder yet. Evaluate a trained model first.')

    def checked_sources_optional(self,purpose='analysis'):
        col={'training':0,'analysis':1,'testing':5}[purpose]
        return [source for i,source in enumerate(self.sources) if 'error' not in source and (purpose!='analysis' or self.source_file_selected(source)) and self.table.item(i,col) is not None and self.table.item(i,col).checkState()==QtCore.Qt.CheckState.Checked]

    def checked_sources(self,purpose='training'):
        sources=self.checked_sources_optional(purpose)
        if not sources:raise ValueError('Select at least one video in the '+purpose+' column of the project library.')
        return sources

    def selected_training_sources(self):
        return [source for i,source in enumerate(self.sources) if 'error' not in source and self.table.item(i,0) is not None and self.table.item(i,0).checkState()==QtCore.Qt.CheckState.Checked]

    def check_sources(self,checked):
        col=self.selection_purpose.currentData()
        self.table.blockSignals(True)
        for i,source in enumerate(self.sources):
            if 'error' not in source and not self.table.isRowHidden(i):self.set_role_checked(i,col,checked)
        self.table.blockSignals(False);self.video_choices_changed()

    def select_random_sources(self):
        col=self.selection_purpose.currentData()
        rows=[i for i,source in enumerate(self.sources)
              if 'error' not in source and not self.table.isRowHidden(i)]
        if not rows:return
        # Nearest whole video, with at least one for a nonempty list.
        count=max(1,min(len(rows),(len(rows)*self.random_percentage.value()+50)//100))
        selected=set(random.sample(rows,count))
        previous=self.table.blockSignals(True)
        try:
            for i in rows:
                self.set_role_checked(i,col,i in selected)
        finally:self.table.blockSignals(previous)
        self.video_choices_changed()

    def select_test_complement(self):
        self.table.blockSignals(True)
        try:
            for row,source in enumerate(self.sources):
                if 'error' not in source and not self.table.isRowHidden(row):
                    self.set_role_checked(row,5,self.table.item(row,0).checkState()!=QtCore.Qt.CheckState.Checked)
        finally:self.table.blockSignals(False)
        self.video_choices_changed()

    def clear_video_filters(self):
        if hasattr(self,'analysis_status_filter'):self.analysis_status_filter.setCurrentIndex(0)
        self.object_name_filter.setCurrentIndex(0);self.object_search.clear()
        tree=getattr(getattr(self.gui,'file_tree_panel',None),'tree',None)
        if tree is not None:tree.clearSelection()
        self.gui.selected_file_list=[]
        self.set_target_paths([])

    def filter_video_rows(self,*_):
        name=self.object_name_filter.currentData() or '';query=self.object_search.text().strip().casefold()
        self.table.setUpdatesEnabled(False)
        for i,source in enumerate(self.sources):
            object_path=source.get('object_path','').strip('/')
            text=' '.join(str(source.get(k,'')) for k in ('nwb','object_path','video')).casefold()
            locked_scope=(self.pages.currentIndex()!=self.LABELING or not self.model_is_locked() or self.source_key(source) in self.current_model_record().get('training',[]))
            match_status=(self.pages.currentIndex()!=self.ANALYSIS or not hasattr(self,'analysis_status_filter') or self.analysis_status_filter.currentText()=='All' or self._analysis_rows.get(self.source_key(source),{}).get('status')==self.analysis_status_filter.currentText())
            self.table.setRowHidden(i,not (locked_scope and match_status and self.source_file_selected(source) and (not name or object_path.rsplit('/',1)[-1]==name) and (not query or query in text)))
        self.table.setUpdatesEnabled(True);self.update_video_filter_count()
        if not self._restoring and not self._switching_model and hasattr(self,'video_storage'):self.persist()

    def update_video_filter_count(self,*_):
        counts=[0,0];shown=sum(not self.table.isRowHidden(i) for i in range(len(self.sources)));testing=0;hidden_selected=0
        for i,source in enumerate(self.sources):
            if 'error' in source:continue
            for col in range(2):
                if col==1 and not self.source_file_selected(source):continue
                item=self.table.item(i,col)
                if item and item.checkState()==QtCore.Qt.CheckState.Checked:
                    counts[col]+=1
                    if col==1 and self.table.isRowHidden(i):hidden_selected+=1
            item=self.table.item(i,5)
            if item and item.checkState()==QtCore.Qt.CheckState.Checked:testing+=1
        if self.pages.currentIndex()==self.LIBRARY:self.video_filter_count.setText(f'{shown} / {len(self.sources)} imported videos shown')
        elif self.pages.currentIndex()==self.LABELING:self.video_filter_count.setText(f'{shown} / {len(self.sources)} videos shown | {counts[0]} training | {testing} test')
        else:self.video_filter_count.setText(f'{shown} / {len(self.sources)} videos shown | {counts[1]} selected for analysis ({hidden_selected} hidden)')
        if hasattr(self,'analyze_button'):self.analyze_button.setText(f'Analyze selected videos ({counts[1]})')
        if hasattr(self,'analysis_summary'):
            scoped=[self._analysis_rows.get(self.source_key(s),{}) for s in self.sources if self.source_file_selected(s)]
            analyzed=sum(r.get('status')=='Analyzed' for r in scoped);pending=sum(r.get('status') in ('Not analyzed','Needs reanalysis','Analyzing') for r in scoped)
            self.analysis_summary.setText(f'{len(scoped)} videos · {analyzed} analyzed · {pending} remaining' + (' · Status needs refresh' if self._analysis_dirty else ''))
        self._selection_counts=counts;self._test_count=testing
        self.schedule_video_column_fit()
        self.update_workflow()

    def scan(self):
        if not self.project.currentData():raise ValueError('Create or open a DLC project before adding library videos.')
        if not self.targets:raise ValueError('Select NWB files in the file list.')
        paths=list(self.targets)
        index=getattr(self.gui,'_content_df_master',getattr(self.gui,'content_df',None))
        if index is not None:index=index.copy(deep=True)
        self.start_thread(lambda:indexed_video_sources(paths,index,self.video_cache),self.add_scan_results)

    def choose_library_additions(self,candidates):
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('Add videos to project library');dialog.resize(900,560)
        layout=QtWidgets.QVBoxLayout(dialog)
        note=QtWidgets.QLabel('Choose videos to add. Existing library entries and role selections are kept. Add uses checked, shown entries only.');note.setWordWrap(True);layout.addWidget(note)
        row=QtWidgets.QHBoxLayout();layout.addLayout(row)
        names=QtWidgets.QComboBox();names.addItem('All NWB objects','')
        for name in sorted({s['object_path'].strip('/').rsplit('/',1)[-1] for s in candidates}):names.addItem(name,name)
        row.addWidget(names)
        search=QtWidgets.QLineEdit();search.setPlaceholderText('Search NWB file, video or object');search.setClearButtonEnabled(True);row.addWidget(search,1)
        listing=QtWidgets.QListWidget();layout.addWidget(listing,1)
        for source in candidates:
            item=QtWidgets.QListWidgetItem(Path(source['nwb']).name+' | '+source['object_path']+' | '+Path(source['video']).name)
            item.setToolTip(source['nwb']+'\n'+source['video']);item.setFlags(item.flags()|QtCore.Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(QtCore.Qt.CheckState.Checked);listing.addItem(item)
        def filter_candidates(*_):
            name=names.currentData();query=search.text().casefold().strip()
            for i,source in enumerate(candidates):
                text=' '.join(str(source.get(k,'')) for k in ('nwb','video','object_path')).casefold()
                listing.item(i).setHidden(bool(name and source['object_path'].strip('/').rsplit('/',1)[-1]!=name) or bool(query and query not in text))
        names.currentIndexChanged.connect(filter_candidates);search.textChanged.connect(filter_candidates)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('Add shown checked videos')
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted:return []
        return [source for i,source in enumerate(candidates) if not listing.item(i).isHidden() and listing.item(i).checkState()==QtCore.Qt.CheckState.Checked]

    def add_scan_results(self,sources):
        known={self.source_key(source) for source in self.sources};candidates=[]
        for source in sources:
            if 'error' in source:self.log.appendPlainText('Not added: '+source['nwb']+': '+source['error']);continue
            key=self.source_key(source)
            if key not in known:candidates.append(source);known.add(key)
        if not candidates:
            self.log.appendPlainText('No new videos to add. Existing library entries were kept.');return
        selected=self.choose_library_additions(candidates)
        if not selected:return
        self.remember_video_ui()
        self.scanned(self.sources+selected,remember=False)
        self.log.appendPlainText(f'Added {len(selected)} video(s) to the project library. Total: {len(self.sources)}.')

    def remove_library_videos(self):
        if self.is_running():raise ValueError('Wait for the current task before changing the library.')
        rows=sorted({index.row() for index in self.table.selectionModel().selectedRows() if not self.table.isRowHidden(index.row())})
        if not rows:raise ValueError('Highlight library rows to remove. Role checkboxes select training or analysis, not removal.')
        info=next((p for p in self.projects if p['config']==self.project.currentData()),{})
        message=f'Remove {len(rows)} video(s) from this project library and all its role selections? Original videos, NWB data, extracted frames, labels and models will stay on disk.'
        if info.get('video_sets') or info.get('models'):
            message+='\n\nSome entries may already have labels or have contributed to a trained model. Removing them cannot undo what an existing model learned. Apply the training selection to update future labeling and training; the existing labeled/trained-video confirmation still applies.'
        if QtWidgets.QMessageBox.question(self,'Remove library videos',message,QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No,QtWidgets.QMessageBox.StandardButton.No)!=QtWidgets.QMessageBox.StandardButton.Yes:return
        self.remember_video_ui();removed={self.source_key(self.sources[i]) for i in rows}
        state=self.video_ui.setdefault(self._active_project or '__new__',{})
        for role in ('training','analysis','testing'):state[role]=[key for key in state.get(role,[]) if key not in removed]
        state['catalog_initialized']=True
        self.scanned([source for i,source in enumerate(self.sources) if i not in rows],remember=False)
        self.history_selected();self.log.appendPlainText(f'Removed {len(rows)} library entries. Files and existing models were kept.')

    def refresh_library(self):
        if not self.sources:raise ValueError('The project library is empty. Add videos first.')
        original=[dict(source) for source in self.sources]
        paths=list(dict.fromkeys(source['nwb'] for source in original))
        index=getattr(self.gui,'_content_df_master',getattr(self.gui,'content_df',None))
        if index is not None:index=index.copy(deep=True)
        def work():
            # Bypass the scan cache; refresh only entries already in this library.
            updated=indexed_video_sources(paths,index,{})
            mapping={self.source_key(source):source for source in updated if source.get('object_path')}
            result=[]
            for source in original:
                candidate=mapping.get(self.source_key(source))
                result.append(candidate if candidate is not None else dict(source,error='Library video object unavailable. Check the file and refresh its NWB inventory.'))
            return result
        self.start_thread(work,lambda sources:self.scanned(sources,remember=False))

    def library_context_menu(self,position):
        menu=QtWidgets.QMenu(self.table)
        refresh=menu.addAction('Refresh video metadata');refresh.setEnabled(bool(self.sources) and not self.is_running());refresh.triggered.connect(lambda:self.safe(self.refresh_library))
        remove=menu.addAction('Remove selected videos from library');remove.setEnabled(bool(self.table.selectionModel().selectedRows()) and not self.is_running());remove.triggered.connect(lambda:self.safe(self.remove_library_videos))
        menu.exec(self.table.viewport().mapToGlobal(position))
    def scanned(self,sources,remember=True):
        self._analysis_dirty=True;self._analysis_rows={};self._postprocessed_video_keys=None;self._exported_video_count=None
        if remember:self.remember_video_ui()
        self._restoring=True
        self.table.setUpdatesEnabled(False);self.table.blockSignals(True)
        self.sources=sources;self.table.setRowCount(len(sources))
        self._export_dirty=True
        for i,source in enumerate(sources):
            for col in (0,1,5):
                item=QtWidgets.QTableWidgetItem();item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable | (QtCore.Qt.ItemFlag.ItemIsUserCheckable if 'error' not in source else QtCore.Qt.ItemFlag.NoItemFlags))
                item.setCheckState(QtCore.Qt.CheckState.Unchecked);self.table.setItem(i,col,item)
            values=[Path(source['nwb']).name+'\n'+source.get('object_path',''),Path(source['video']).name if source.get('video') else '',source.get('error',str(source.get('frames',0))+' frames')]
            for col in (6,7):self.table.setItem(i,col,QtWidgets.QTableWidgetItem())
            for j,value in enumerate(values,2):
                item=QtWidgets.QTableWidgetItem(value);item.setToolTip(source['nwb']+'\n'+source.get('object_path','') if j==2 else source.get('video','') if j==3 else value);self.table.setItem(i,j,item)
        self.object_name_filter.blockSignals(True);self.object_name_filter.clear();self.object_name_filter.addItem('All objects','')
        names=sorted({v.get('object_path','').strip('/').rsplit('/',1)[-1] for v in sources if v.get('object_path')})
        for name in names:self.object_name_filter.addItem(name,name)
        self.object_name_filter.blockSignals(False);self.table.blockSignals(False);self.table.setUpdatesEnabled(True)
        self._restoring=False;self.restore_video_ui();self.refresh_postprocessing_videos([]);self.persist()
        if remember:self.stages.setCurrentRow(0)
    def show_label_parameters(self):
        self.label_bodyparts.setText(self.bodyparts.text())
        self.label_parameters_dialog.show();self.label_parameters_dialog.raise_();self.label_parameters_dialog.activateWindow()

    def save_label_bodyparts(self):
        self.bodyparts.setText(self.label_bodyparts.text());self.save_bodyparts()

    def label_frame_count_changed(self):
        if not self._restoring:self.persist()
        self.update_workflow()

    def save_scorer(self):
        scorer=self.scorer.text().strip()
        if not scorer:raise ValueError('Enter a scorer name. Refresh the project if its current name is unknown.')
        self.launch('update_scorer',scorer=scorer)

    def save_bodyparts(self):
        config=self.project.currentData()
        if not config:raise ValueError('Select an existing DLC project first.')
        parts=[p.strip() for p in self.bodyparts.text().split(',')]
        if not all(parts) or len(set(parts))!=len(parts):raise ValueError('Enter unique body-part names separated by commas.')
        self.project_dialog.hide();self.launch('update_bodyparts',bodyparts=parts)

    def create_project(self):
        parts=[p.strip() for p in self.bodyparts.text().split(',') if p.strip()]
        if not parts or len(parts)!=len(set(parts)):raise ValueError('Enter unique body-part names.')
        name=self.name.text().strip();experimenter='user'
        if not name or not experimenter or any(c in name+experimenter for c in '/\\:'):raise ValueError('Project name and experimenter are required, without path separators.')
        self.project_dialog.hide()
        self.launch('create',name=name,experimenter=experimenter,bodyparts=parts,working_directory=str(self.root),videos=[],frames=self.frames.value())

    def launch(self,action,**extra):
        if self.is_running():raise ValueError('A task is already running.')
        self.inline_error.hide()
        python=Path(self.python.text().strip())
        if not python.is_file():raise ValueError('Select the Python executable of the DeepLabCut environment.')
        library_ready=self.video_ui.get(self._active_project or '__new__',{}).get('catalog_initialized',bool(self.sources))
        if action in {'train','evaluate'}:self.ensure_analysis_device()
        if action in {'train','evaluate','analyze'} and self._analysis_device_probe is not None:raise ValueError('CUDA detection is still running. Wait for the device selection to update.')
        if action=='train' and self.model_is_locked():raise ValueError('This model is trained and locked. Create a new model to train again.')
        if action in {'train','analyze'}:extra.setdefault('model_name',self.current_model_record().get('name','Model'))
        if action=='sync_videos' and not library_ready:raise ValueError('Add videos to the project library first.')
        prepared=extra.pop('_sources_prepared',False)
        if action in {'sync_videos','annotate','extract','check_labels','dataset','train'} and library_ready and 'training_selection' not in extra:
            extra['training_selection']=self.selected_training_sources()
        if action in {'annotate','extract','check_labels','dataset','train'} and extra.get('training_selection')==[]:
            raise ValueError('Select at least one Training video in the project library.')
        fields=[key for key in ('videos','training_selection') if extra.get(key)]
        if fields and not prepared:
            options=dict(extra)
            self.log.appendPlainText('Checking selected videos and NWB timestamps…')
            def prepare():
                return {key:[video_source(source['nwb'],source['object_path']) for source in options[key]] for key in fields}
            def ready(values):
                # Continue after the QThread has fully stopped.
                options.update(values);options['_sources_prepared']=True
                self._pending_video_launch=(action,options)
            self.start_thread(prepare,ready);return
        if action in {'create','add_videos','sync_videos','annotate','extract','check_labels','dataset','train'}:extra.setdefault('copy_videos',bool(self.video_storage.currentData()))
        config=extra.pop('config',self.project.currentData())
        if action not in {'check','create'} and not config:raise ValueError('Create or open a DLC project.')
        identifier=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
        directory=(self.project_storage('jobs',config) if config and action not in {'check','create'} else self.root/'_setup_jobs')/identifier;directory.mkdir(parents=True)
        request=dict(action=action,job_id=identifier,job_dir=str(directory.resolve()),config=config,
                     shuffle=self.shuffle.value(),trainingsetindex=self.fraction.value(),snapshot_index=self.snapshot.value(),device=self.analysis_device.currentData() if action=='analyze' else self.device.currentText(),**extra)
        if action=='analyze':request['analysis_dir']=str((self.project_storage('analysis',config)/identifier).resolve())
        if not prepared:
            for source in request.get('videos',[])+request.get('training_selection',[]):verify_source(source)
        request_path=directory/'request.json';save_json(request_path,request)
        worker_path=Path(__file__).resolve().parents[1]/'dlc_worker.py'
        if not worker_path.is_file():raise FileNotFoundError('dlc_worker.py is missing from the package or executable.')
        self.job=request;self._live_analysis_stamp=None;self._analysis_live_summary='';self.cancelled=False;self.persist();self.log.clear();self.log.appendPlainText('Starting: '+action+'\n')
        process=QtCore.QProcess(self);self.process=process
        environment=child_environment(str(python))
        process.setProcessEnvironment(environment);process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        process.setWorkingDirectory(str(directory));process.readyReadStandardOutput.connect(self.read_log)
        process.finished.connect(self.finished)
        process.errorOccurred.connect(self.process_error)
        self.set_busy()
        frozen_windows = os.name == 'nt' and getattr(sys, 'frozen', False)
        if frozen_windows:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            process.start(str(python),['-u',str(worker_path),str(request_path.resolve())])
            if frozen_windows: process.waitForStarted(5000)
        finally:
            if frozen_windows: ctypes.windll.kernel32.SetDllDirectoryW(str(getattr(sys, '_MEIPASS', Path(sys.executable).parent)))
    def confirm_incomplete_training(self,review):
        warning=QtWidgets.QMessageBox(self)
        warning.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        warning.setWindowTitle('Incomplete training labels')
        warning.setText(f"{len(review['incomplete'])} of {review['total']} selected training videos have no labels or still contain unlabeled extracted frames.")
        warning.setInformativeText('Only frames with saved annotations can contribute labels to training. Finish labeling for better coverage, or continue with the available labels. Continue training?')
        warning.setDetailedText('\n'.join(f"{Path(str(row['video']).replace(chr(92), '/')).name}: {row['labeled_frames']} labeled / {row['extracted_frames']} extracted frames" for row in review['incomplete']))
        warning.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        warning.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        return warning.exec()==QtWidgets.QMessageBox.StandardButton.Yes

    def read_log(self):
        if self.process is None:return
        output=bytes(self.process.readAllStandardOutput()).decode('utf-8',errors='replace')
        self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End);self.log.insertPlainText(output)
        if self.job.get('action')=='check':self.environment_log.insertPlainText(output)
        try:
            with (Path(self.job['job_dir'])/'stdout.log').open('a',encoding='utf-8') as file:file.write(output)
        except OSError:pass
        if 'Saved and registered:' in output:self.update_live_analysis()
    def process_error(self,error):
        if error==QtCore.QProcess.ProcessError.FailedToStart:
            self.log.appendPlainText('Could not start the selected Python.');self.finished(-1,0)
    def finished(self,exit_code,exit_status):
        if self.process is None:return
        self.read_log();process=self.process;self.process=None;process.deleteLater()
        path=Path(self.job['job_dir'])/'result.json'
        try:
            result=_pp_json_read(path) if path.exists() else dict(status='failed',error='Process ended without a result.')
        except (OSError, ValueError) as exc:
            result=dict(status='failed',error='Could not read result: '+str(exc))
        if result.get('status')=='failed' and not path.exists():
            marker='NWB_WORKBENCH_ERROR_JSON='
            for line in reversed(self.log.toPlainText().splitlines()):
                if line.startswith(marker):
                    try:result=json.loads(line[len(marker):])
                    except ValueError:pass
                    break
        if self.cancelled:
            result.update(status='cancelled',action=self.job['action'])
            result.pop('error',None)
            try:save_json(path,result)
            except OSError as exc:self.log.appendPlainText('Could not save job status: '+str(exc))
        elif exit_code!=0:
            result['status']='failed'
            try:save_json(path,result)
            except OSError as exc:self.log.appendPlainText('Could not save job status: '+str(exc))
        if result.get('status')=='running':
            result.update(status='interrupted',error='Analysis ended before the batch completed. Saved videos are retained.')
            try:save_json(path,result)
            except OSError as exc:self.log.appendPlainText(str(exc))
        self.log.appendPlainText('\nStatus: '+result['status'])
        self._last_job_status={'completed':'Completed','failed':'Failed; open log for details','cancelled':'Cancelled'}.get(result['status'],result['status'])
        if result['status']=='completed':
            self.validated_python=self.python.text().strip()
            env=result.get('environment',{})
            if 'cuda_available' in env:self.update_analysis_device(bool(env['cuda_available']),prefer=self.job.get('action')=='check')
            self.environment.setText(f"DLC {env.get('dlc_version','')} • CUDA: {env.get('cuda_available',False)}")
            info=result.get('project')
            if info:
                self.projects=[p for p in self.projects if p['config']!=info['config']]+[info];self.refresh_projects(info['config'])
            scorer_update=result.get('scorer_update')
            if scorer_update is not None:
                self.log.appendPlainText('Scorer name saved.' if scorer_update['changed'] else 'Scorer name unchanged.')
                if scorer_update.get('backup'):self.log.appendPlainText('Configuration backup: '+scorer_update['backup'])
            update=result.get('bodyparts_update')
            if update is not None:
                self.log.appendPlainText('Body parts saved. '+('Added: '+', '.join(update['added']) if update['added'] else 'No changes.'))
                if update.get('backup'):self.log.appendPlainText('Configuration backup: '+update['backup'])
            review=result.get('selection_review')
            if review is not None:
                lines=[f"This changes the active training video selection ({review['removed_count']} excluded, {review['added_count']} new)."]
                if review.get('labeled'):
                    lines.append(f"{len(review['labeled'])} excluded video(s) already have labels.")
                if review.get('has_training_artifacts'):
                    lines.append('Training datasets or models already exist and may include these videos. Existing models keep what they learned. Recreate the training dataset before further training; use a new shuffle to keep an existing trained model.')
                lines.append('Original videos, extracted frames, saved labels and existing models will be kept. Excluded videos will disappear from labeling navigation and future datasets. Continue?')
                answer=QtWidgets.QMessageBox.question(self,'Change training videos','\n\n'.join(lines),QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,QtWidgets.QMessageBox.StandardButton.No)
                if answer==QtWidgets.QMessageBox.StandardButton.Yes:
                    options={k:v for k,v in self.job.items() if k not in {'action','job_id','job_dir','shuffle','trainingsetindex','snapshot_index','device'}}
                    options.update(selection_confirmation=review['config_digest'],_sources_prepared=True)
                    action=self.job['action']
                    QtCore.QTimer.singleShot(0,lambda:self.safe(lambda:self.launch(action,**options)))
                else:self.log.appendPlainText('Video selection update cancelled. Project configuration unchanged.')
            label_review=result.get('training_label_review')
            if label_review is not None:
                if self.confirm_incomplete_training(label_review):
                    options={k:v for k,v in self.job.items() if k not in {'action','job_id','job_dir','shuffle','trainingsetindex','snapshot_index','device'}}
                    options.pop('selection_confirmation',None)
                    options.update(label_confirmation=label_review['token'],_sources_prepared=True)
                    QtCore.QTimer.singleShot(0,lambda:self.safe(lambda:self.launch('train',**options)))
                else:
                    self.log.appendPlainText('Training cancelled. Finish labeling, then click Train model again.')
                    self._last_job_status='Training cancelled: incomplete labels'

            self.refresh_history()
            if result.get('results'):self.stages.setCurrentRow(self.POSTPROCESSING)
        elif result.get('error'):
            self.log.appendPlainText(result['error'])
            self.inline_error.setText(result['error']);self.inline_error.show()
            if self.job.get('action')=='check':
                self.validated_python=''
                self.detected_environments=[v for v in self.detected_environments if v['python']!=self.python.text().strip()]
                self.environment.setText('Environment test failed.')
                self.environment_log.appendPlainText(result['error']);self.persist()
        if result.get('results') and result['status']!='completed':
            self.refresh_history()
            message=f"{len(result['results'])}/{result.get('total_videos',len(self.job.get('videos',[])))} completed videos saved; available in Review and Export."
            self.log.appendPlainText(message);self._last_job_status+= ' · '+message
        self.set_busy();self.schedule_analysis_status()
    def cancel_job(self):
        if self.detector.running:
            self.detector.cancel();return
        if self.process is None:return
        self.cancelled=True;process=self.process;process.terminate()
        QtCore.QTimer.singleShot(2500,lambda:process.kill() if self.process is process else None)
    def start_thread(self,fn,done):
        if self.is_running():raise ValueError('A task is already running.')
        thread=WorkThread(fn,self);self.worker=thread
        thread.done.connect(lambda value:self.safe(lambda:done(value)))
        thread.failed.connect(self.error)
        thread.finished.connect(self.thread_finished);self.set_busy();thread.start()
    def thread_finished(self):
        thread=self.worker;self.worker=None
        if thread:thread.deleteLater()
        if self.importing:
            self.importing=False;self.gui._nwb_object_edit_active_paths=set()
            self.gui.file_tree_panel.setEnabled(True);self.gui.content_panel.setEnabled(True)
        self.set_busy()
        prepared=getattr(self,'_pending_export_confirmation',None)
        if prepared is not None:
            self._pending_export_confirmation=None
            self.safe(lambda:self.confirm_export(prepared));return
        self.schedule_analysis_status()
        if self.pages.currentIndex() in (self.POSTPROCESSING,self.EXPORT) and self._export_dirty:QtCore.QTimer.singleShot(0,lambda:self.safe(self.refresh_export) if not self.is_running() else None)
        pending=getattr(self,'_pending_video_launch',None)
        if pending:
            self._pending_video_launch=None
            self.safe(lambda:self.launch(pending[0],**pending[1]))
    def refresh_history(self):
        self._export_dirty=True;self._analysis_dirty=True
        if not hasattr(self,'history') or self.pages.currentIndex()!=self.REVIEW:return
        self.history.blockSignals(True);self.history.clear()
        self.history.addItem('Latest results for all project videos','__latest__')
        for path, run in job_records(self.job_roots()):
            try:
                config=self.project.currentData()
                if not config or os.path.normcase(os.path.abspath(run.get('model',{}).get('config','')))!=os.path.normcase(os.path.abspath(config)):continue
                if usable_analysis_run(run) and run.get('results'):
                    try:label=datetime.strptime(path.parent.name[:15],'%Y%m%d_%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
                    except ValueError:label=path.parent.name
                    self.history.addItem(f"{label} • {len(run['results'])} video(s)"+(f" · {run.get('status')}" if run.get('status')!='completed' else ''),str(path))
                    self.history.setItemData(self.history.count()-1,path.parent.name,QtCore.Qt.ItemDataRole.ToolTipRole)
            except (OSError,ValueError):continue
        self.history.blockSignals(False)
        if self.pages.currentIndex()==self.REVIEW:self.history_selected()
        self._export_dirty=True;self._analysis_dirty=True
    def review_filter_changed(self,*_):
        self.video_ui.setdefault(self._active_project or '__new__',{})['review_filter']=self.review_filter.currentText()
        self.history_selected();self.persist()

    def step_review_video(self,direction):
        index=self.results.currentIndex()+direction
        if 0<=index<self.results.count():self.results.setCurrentIndex(index)

    def history_selected(self,*_):
        self.clear_preview();self.results.blockSignals(True)
        state=self.video_ui.get(self._active_project or '__new__',{})
        preferred=state.get('review_video',{}).get(self.model_key())
        try:
            self.results.clear();self.loaded_run=None
            path=self.history.currentData()
            if path:
                if path=='__latest__':
                    matches,_=selected_analysis_results(self.job_roots(),self.sources,self.project.currentData() or '',self.shuffle.value(),self.fraction.value())
                    self.loaded_run=dict(results=[result for result,run,run_path in matches])
                else:self.loaded_run=_pp_json_read(Path(path))
                library={self.source_key(source) for source in self.sources}
                processed={self.source_key(row['source']) for row in self._export_rows if row['postprocessed']}
                mode=self.review_filter.currentText()
                for i,result in enumerate(self.loaded_run['results']):
                    identity=self.source_key(result['source'])
                    if identity not in library or not Path(result.get('predictions','')).is_file():continue
                    if mode=='Postprocessed' and identity not in processed:continue
                    if mode=='Unprocessed' and identity in processed:continue
                    self.results.addItem(Path(result['source']['nwb']).name+' • '+result['source']['object_path'],i)
                    self.results.setItemData(self.results.count()-1,result['source']['nwb']+'\n'+result['source']['video'],QtCore.Qt.ItemDataRole.ToolTipRole)
                    if identity==preferred:self.results.setCurrentIndex(self.results.count()-1)
        finally:self.results.blockSignals(False)
        self.result_selected()

    def result_selected(self,*_):
        index=self.results.currentIndex();self.review_previous_button.setEnabled(index>0);self.review_next_button.setEnabled(0<=index<self.results.count()-1)
        if self.loaded_run is not None and index>=0:
            source=self.loaded_run['results'][self.results.currentData()]['source']
            self.video_ui.setdefault(self._active_project or '__new__',{}).setdefault('review_video',{})[self.model_key()]=self.source_key(source)
            self.persist()
        self._preview_generation=getattr(self,'_preview_generation',0)+1
        generation=self._preview_generation
        self.clear_preview()
        if self.loaded_run is None or self.results.currentIndex()<0 or self.pages.currentIndex()!=self.REVIEW:return
        def load_selected():
            if generation==self._preview_generation and self.pages.currentIndex()==self.REVIEW:
                self.safe(self.preview)
        QtCore.QTimer.singleShot(0,load_selected)

    def clear_preview(self,*_):
        self.preview_result=None
        if hasattr(self,'preview_player'):
            self.preview_player.shutdown();self.preview_player.processed_xy=None;self.preview_player.processed_status=None;self.preview_player.processed_toggle.setEnabled(False);self.preview_player.confidence.setEnabled(True);self.preview_player.pose=None;self.preview_player.parts=[];self.preview_player.update_legend();self.preview_player.image.clear();self.preview_player._pixmap=None;self.preview_player._raw_frame_pixmap=None
    def preview(self):
        if self.loaded_run is None or self.results.currentIndex()<0:raise ValueError('Select an analysis and a result.')
        result=self.loaded_run['results'][self.results.currentData()];verify_source(result['source'])
        values,parts=load_prediction(result);self.preview_player.pose=values;self.preview_player.parts=parts;self.preview_player.cutoff=self.cutoff.value()
        self.preview_player.update_legend()
        source=result['source'];self.preview_player.open_objects([source['nwb']],source['object_path'])
        if self.preview_player.source is None:raise ValueError(self.preview_player.status.text())
        self.preview_result=result;self.load_postprocessing(parts);self.apply_postprocessing_preview();self.set_busy()
    def change_cutoff(self,value):
        self.preview_player.cutoff=value
        if self.preview_result is not None:self.preview_player.redraw_pose()
    def export_selected(self):
        if self.is_running():raise ValueError('A task is already running.')
        config=self.project.currentData()
        if not config:raise ValueError('Select a project first.')
        sources=self.export_checked_sources()
        if not sources:raise ValueError('Check at least one analyzed video in Export.')
        shuffle,fraction=self.shuffle.value(),self.fraction.value()
        directories=self.processed_roots();removed=self.postprocessing_removed_sources();jobs=self.job_roots()
        project_name=self.project.currentText();records=dict(self.model_records())
        def prepare():
            matches,missing=selected_analysis_results(jobs,sources,config,shuffle,fraction)
            plan=[]
            for result,run,path in matches:
                processing=None if self.model_key_for_run(run)+'|'+self.source_key(result['source']) in removed else saved_postprocessing(result,directories)
                model=run.get('model',{});record=records.get(f"{model.get('shuffle')}:{model.get('trainingsetindex')}",{})
                plan.append(dict(result=result,run=run,path=path,processing=processing,model_name=record.get('name') or model.get('name') or f"Model {model.get('shuffle','?')}"))
            return dict(plan=plan,missing=missing,project_name=project_name)
        self.start_thread(prepare,self.export_plan_prepared)

    def export_plan_prepared(self,prepared):
        self._pending_export_confirmation=prepared

    @staticmethod
    def model_key_for_run(run):
        model=run.get('model',{})
        return f"{model.get('shuffle')}:{model.get('trainingsetindex')}"

    def confirm_export(self,prepared):
        plan=prepared['plan']
        if not plan:raise ValueError('No completed analysis is available for the selected videos.')
        dialog=QtWidgets.QDialog(self);dialog.setWindowTitle('Export tracking to NWB');dialog.resize(950,500)
        layout=QtWidgets.QVBoxLayout(dialog)
        processed=sum(item['processing'] is not None for item in plan)
        summary=QtWidgets.QLabel(f"{len(plan)} videos: {processed} with saved postprocessing, {len(plan)-processed} with original coordinates. {prepared['missing']} without analysis.\nOne object per body part: X, Y, original confidence, postprocessing status. No postprocessing will be computed.")
        summary.setWordWrap(True);layout.addWidget(summary)
        table=QtWidgets.QTableWidget(len(plan),4);table.setHorizontalHeaderLabels(['Video / NWB object','NWB file','Model','Postprocessing'])
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for row,item in enumerate(plan):
            source=item['result']['source'];processing=item['processing']
            values=[Path(source['video']).name+'\n'+source['object_path'],Path(source['nwb']).name,item['model_name'],processing['metadata'].get('name','Postprocessing') if processing else 'None (original)']
            for col,value in enumerate(values):table.setItem(row,col,QtWidgets.QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch);layout.addWidget(table,1)
        policy=QtWidgets.QComboBox();policy.addItem('Skip body parts already present from the same video','skip');policy.addItem('Replace all existing body parts from the same video','replace')
        layout.addWidget(QtWidgets.QLabel('Existing tracking objects'));layout.addWidget(policy)
        note=QtWidgets.QLabel('Replacement includes tracking from older exports and other DLC projects for the same source video. Other videos are preserved. A backup of each modified NWB file is kept.')
        note.setWordWrap(True);layout.addWidget(note)
        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok|QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('Export')
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec()!=QtWidgets.QDialog.DialogCode.Accepted:return
        self.run_export_plan(prepared,policy.currentData())

    def run_export_plan(self,prepared,policy):
        plan=prepared['plan'];paths=list(dict.fromkeys(item['result']['source']['nwb'] for item in plan))
        self.preview_player.shutdown()
        if not self.gui.prepare_nwb_object_edit(paths):return
        self.gui.file_tree_panel.setEnabled(False);self.gui.content_panel.setEnabled(False);self.importing=True
        self._export_live_summary=f'0/{len(plan)} videos completed'
        self.log.appendPlainText(f"Export started: {len(plan)} videos; project: {prepared['project_name']}; existing objects: {policy}; {prepared['missing']} without analysis.")
        inventory=Path(self.gui.NWB_inventory_path)
        def work():
            report=dict(saved=[],skipped=prepared['missing'],errors=[],warnings=[])
            for index,item in enumerate(plan,1):
                result,run,run_path=item['result'],item['run'],item['path']
                label=Path(result['source']['nwb']).name+' / '+result['source']['object_path']
                processing=item['processing'];processing_name=processing['metadata'].get('name','Postprocessing') if processing else 'None (original coordinates)'
                started=time.monotonic()
                event=dict(source=result['source'],index=index,total=len(plan))
                self.exportProgress.emit(dict(event,state='started',message=f"[{index}/{len(plan)}] Exporting {Path(result['source']['video']).name}\n  NWB: {result['source']['nwb']} / {result['source']['object_path']}\n  Model: {item['model_name']}; postprocessing: {processing_name}"))
                try:
                    saved=import_prediction(result,run,item['processing'],project_name=prepared['project_name'],existing=policy)
                    if saved.get('skipped'):
                        report['skipped']+=1
                        self.exportProgress.emit(dict(event,state='skipped',message=f'[{index}/{len(plan)}] Skipped: {label}; all body parts already present.'))
                        continue
                    report['saved'].append(saved)
                    self.exportProgress.emit(dict(event,state='saved',message=f"[{index}/{len(plan)}] Exported: {label}; {saved.get('objects',0)} body-part objects; {time.monotonic()-started:.1f} s.\n  Module: {saved['module']}\n  Backup: {saved['backup']}"))
                    try:
                        with (Path(run_path).parent/'imports.jsonl').open('a',encoding='utf-8') as file:file.write(json.dumps(saved)+'\n')
                    except OSError as exc:
                        message=label+': imported, but could not record job history: '+str(exc)
                        report['warnings'].append(message);self.exportProgress.emit(dict(event,state='warning',message='Warning: '+message))
                except Exception as exc:
                    report['errors'].append(label+': '+str(exc))
                    self.exportProgress.emit(dict(event,state='failed',message=f'[{index}/{len(plan)}] Failed: {label}; {exc}'))
            changed=list(dict.fromkeys(item['nwb'] for item in report['saved']))
            if changed and inventory.exists():
                self.exportProgress.emit(dict(state='inventory',message=f'Refreshing inventory for {len(changed)} NWB file(s)…'))
                try:
                    from GUI.panels.content_object_manager import _write_refreshed_inventory
                    _write_refreshed_inventory(inventory,[Path(path) for path in changed])
                except Exception as exc:report['warnings'].append('NWB saved; inventory refresh failed: '+str(exc))
            return report
        self.start_thread(work,self.exported)

    def export_progress(self,event):
        self.log.appendPlainText(event['message'])
        state=event['state']
        if state in ('started','saved','skipped','failed'):
            completed=event['index']-(state=='started')
            self._export_live_summary=f"{completed}/{event['total']} videos completed"
            key=self.source_key(event['source'])
            self.export_table.blockSignals(True)
            try:
                for index,row in enumerate(self._export_rows):
                    if self.source_key(row['source'])!=key:continue
                    if state in ('saved','skipped'):row['exported']=True
                    cell=self.export_table.item(index,5)
                    if cell is not None:
                        cell.setText({'started':'Exporting…','saved':'Yes','skipped':'Yes (already present)','failed':'Failed'}[state])
                        cell.setToolTip(event['message'])
                    break
            finally:self.export_table.blockSignals(False)
            self._exported_video_count=sum(row.get('exported',False) for row in self._export_rows)
            self.export_summary.setText(f"Total videos: {len(self._export_rows)}   |   Analyzed: {sum(row['analyzed'] for row in self._export_rows)}   |   Postprocessed: {sum(row['postprocessed'] for row in self._export_rows)}   |   Exported: {self._exported_video_count}   |   {self._export_live_summary}")
            self.update_workflow()
        elif state=='inventory':self._export_live_summary='Refreshing inventory'
        self.update_job_status()

    def exported(self,report):
        summary=f"Export finished: {len(report['saved'])} imported, {report['skipped']} skipped (no analysis or already imported), {len(report['errors'])} failed."
        self.log.appendPlainText(summary)
        for message in report['warnings']+report['errors']:self.log.appendPlainText(message)
        self._last_job_status=summary;self.update_job_status()
        if report['saved'] and not any('inventory refresh failed' in message for message in report['warnings']):
            self.gui._on_nwb_object_edit_finished(dict(action='DLC export',files=list(dict.fromkeys(item['nwb'] for item in report['saved']))))
        self.preview_result=None;self._export_dirty=True;self._analysis_dirty=True;self._exported_video_count=sum(row.get('exported',False) for row in self._export_rows)
    def shutdown(self):
        probe=getattr(self,'_analysis_device_probe',None)
        if probe is not None:
            probe.blockSignals(True);probe.kill();probe.waitForFinished(1000);probe.deleteLater();self._analysis_device_probe=None
        self.persist();self.preview_player.shutdown()
