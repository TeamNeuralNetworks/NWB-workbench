"""Run from Anaconda Prompt after activating NWB_workbench."""
import importlib.util
import os
import subprocess
import sys

MODULES = ['numpy','pandas','scipy','h5py','pynwb','hdmf','openpyxl','xarray',
           'matplotlib','pyqtgraph','av','PIL','sklearn','seaborn','statsmodels',
           'astropy','tensorpac','pycwt','spectral_connectivity','spikeinterface',
           'probeinterface','spikeinterface_gui','neo','phylib','lvm_read',
           'PyInstaller','spyder_kernels']

def main():
    print('Python:', sys.executable)
    print('Version:', sys.version)
    failed = []
    for module in MODULES:
        try:
            result = subprocess.run([sys.executable,'-c','import '+module],
                                    capture_output=True,text=True,timeout=90)
            if result.returncode:
                failed.append(module)
                print('FAIL:',module,'\n',result.stderr[-2000:])
            else:print('OK:',module)
        except Exception as exc:
            failed.append(module);print('FAIL:',module,exc)
    other_qt = [m for m in ['PyQt5','PyQt6','PySide2'] if importlib.util.find_spec(m)]
    if other_qt:
        failed.append('multiple Qt bindings');print('Additional Qt bindings:',other_qt)
    env = os.environ.copy()
    for key in ['QT_API','PYQTGRAPH_QT_LIB','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','QT_QPA_PLATFORM']:
        env.pop(key,None)
    code = '''from PySide6 import QtCore,QtWidgets
app=QtWidgets.QApplication([])
w=QtWidgets.QLabel('NWB Workbench: Qt works');w.resize(350,100);w.show()
QtCore.QTimer.singleShot(1000,app.quit)
print('Qt:',QtCore.qVersion(),flush=True)
app.exec()
'''
    try:
        result=subprocess.run([sys.executable,'-c',code],env=env,timeout=30)
        if result.returncode:failed.append('Qt window')
    except Exception as exc:failed.append('Qt window');print('FAIL: Qt window',exc)
    print('FAILED: '+', '.join(failed) if failed else 'Environment imports and Qt window: OK')
    return bool(failed)

if __name__=='__main__':sys.exit(main())
