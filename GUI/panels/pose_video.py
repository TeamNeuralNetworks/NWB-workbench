"""Read-only display of exported NWB tracking, without a DeepLabCut environment."""
import json
from pathlib import Path
from html import escape
import h5py
import numpy as np
from GUI.qt_compat import QtCore,QtGui,QtWidgets
from GUI.panels.video_panel import VideoPanel


def pose_metadata(group):
    value=group.attrs.get('comments','{}')
    if isinstance(value,bytes):value=value.decode('utf-8')
    try:info=json.loads(str(value))
    except (ValueError,TypeError):return None
    if not isinstance(info,dict) or info.get('schema')!='workbench_pose_columns_v1':return None
    if 'data' not in group or len(group['data'].shape)!=2 or group['data'].shape[1]!=4:return None
    return info


def read_pose_metadata(nwb_path,object_path):
    with h5py.File(str(nwb_path),'r') as file:
        return pose_metadata(file[object_path])


class TrackingVideoPanel(VideoPanel):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.pose_entries={};self.pose_arrays={};self._pose_frame=None
        self.pose_toolbar=QtWidgets.QWidget(self);row=QtWidgets.QHBoxLayout(self.pose_toolbar);row.setContentsMargins(0,0,0,0)
        self.bodyparts=QtWidgets.QPushButton('Body parts (0)');self.bodyparts.setEnabled(False)
        self.bodyparts_menu=QtWidgets.QMenu(self.bodyparts);self.bodyparts.setMenu(self.bodyparts_menu);row.addWidget(self.bodyparts)
        row.addWidget(QtWidgets.QLabel('Dot size'))
        self.pose_dot_size=QtWidgets.QSpinBox();self.pose_dot_size.setRange(1,40);self.pose_dot_size.setValue(6);self.pose_dot_size.setSuffix(' px');row.addWidget(self.pose_dot_size)
        self.pose_names=QtWidgets.QCheckBox('Names beside points');row.addWidget(self.pose_names)
        row.addStretch(1)
        self.layout().insertWidget(3,self.pose_toolbar);self.pose_toolbar.hide()
        self.pose_legend=QtWidgets.QLabel();self.pose_legend.setWordWrap(True);self.pose_legend.hide();self.layout().insertWidget(4,self.pose_legend)
        self.pose_dot_size.valueChanged.connect(self.redraw_tracking);self.pose_names.toggled.connect(self.redraw_tracking)

    def _file_changed(self,index):
        self.pose_entries={};self.pose_arrays={};self._pose_frame=None
        self.bodyparts_menu.clear();self.bodyparts.setText('Body parts (0)');self.bodyparts.setEnabled(False);self.pose_legend.hide();self.pose_toolbar.hide()
        super()._file_changed(index)
        if self.source is None:return
        try:
            with h5py.File(self.source['nwb_path'],'r') as file:
                for module_name,module in file.get('processing',{}).items():
                    for name,group in module.items():
                        if not isinstance(group,h5py.Group):continue
                        info=pose_metadata(group)
                        if info is None or info.get('source',{}).get('object_path')!=self.source['object_path']:continue
                        path=f'processing/{module_name}/{name}'
                        action=self.bodyparts_menu.addAction(name);action.setCheckable(True)
                        self.pose_entries[path]=dict(action=action,metadata=info,color=QtGui.QColor.fromHsv((len(self.pose_entries)*67)%360,230,220))
                        action.toggled.connect(lambda checked,path=path:self.set_bodypart(path,checked))
            self.bodyparts.setEnabled(bool(self.pose_entries))
            self.pose_toolbar.setVisible(bool(self.pose_entries))
        except Exception as exc:self.status.setText('Could not list tracking objects: '+str(exc))

    def set_bodypart(self,path,enabled=True):
        entry=self.pose_entries.get(path)
        if entry is None:raise ValueError('This body part is not associated with the displayed video.')
        action=entry['action']
        try:
            if enabled and path not in self.pose_arrays:
                with h5py.File(self.source['nwb_path'],'r') as file:
                    group=file[path];times=np.asarray(group['timestamps'])
                    if not np.array_equal(times,self.source['timestamps']):raise ValueError('Tracking timestamps differ from the associated video. Re-export this result.')
                    self.pose_arrays[path]=np.asarray(group['data'],dtype=float)
            if not enabled:self.pose_arrays.pop(path,None)
            action.blockSignals(True);action.setChecked(enabled);action.blockSignals(False)
        except Exception as exc:
            action.blockSignals(True);action.setChecked(False);action.blockSignals(False)
            self.status.setText(str(exc));return
        self.bodyparts.setText(f'Body parts ({len(self.pose_arrays)})')
        entries=[f'<span style="color:{self.pose_entries[key]["color"].name()}">●</span> {escape(self.pose_entries[key]["metadata"].get("bodypart",Path(key).name))}' for key in self.pose_arrays]
        self.pose_legend.setText(' &nbsp; '.join(entries)+' &nbsp; | ● original &nbsp; ○ interpolated &nbsp; ■ fixed')
        self.pose_legend.setVisible(bool(entries));self.redraw_tracking()

    def redraw_tracking(self,*_):
        if self._pose_frame is None:return
        self._pixmap=self._pose_frame.copy();painter=QtGui.QPainter(self._pixmap)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing);radius=self.pose_dot_size.value()/2
            for path,values in self.pose_arrays.items():
                if self.displayed>=len(values):continue
                x,y,confidence,status=values[self.displayed]
                if not np.isfinite([x,y]).all():continue
                entry=self.pose_entries[path];color=entry['color']
                painter.setPen(QtGui.QPen(color,1.5));painter.setBrush(QtCore.Qt.BrushStyle.NoBrush if status==2 else QtGui.QBrush(color))
                if status==3:painter.drawRect(QtCore.QRectF(x-radius,y-radius,2*radius,2*radius))
                else:painter.drawEllipse(QtCore.QPointF(x,y),radius,radius)
                if self.pose_names.isChecked():painter.drawText(QtCore.QPointF(x+radius+2,y-radius-2),entry['metadata'].get('bodypart',Path(path).name))
        finally:painter.end()
        self._scale()

    def _received(self,index,rgb):
        valid=self.sender() is self.decoder and self.source is not None and index==self.current
        super()._received(index,rgb)
        if valid and self._pixmap is not None:
            self._pose_frame=self._pixmap.copy();self.redraw_tracking()
