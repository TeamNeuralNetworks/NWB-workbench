"""Asynchronous discovery and import checks in external DLC interpreters."""
import json
import os
from pathlib import Path
import shutil
import sys
from GUI.qt_compat import QtCore

PROBE = '''import os, sys, json
if os.name == 'nt':
 import ctypes
 ctypes.windll.kernel32.SetDllDirectoryW(None)
os.environ['DLClight']='True'
os.environ['MPLBACKEND']='Agg'
import deeplabcut, torch
from deeplabcut.core.engine import Engine
assert int(deeplabcut.__version__.split('.')[0]) >= 3, 'DeepLabCut 3 required'
assert Engine.PYTORCH is not None
print('NWB_DLC_ENV='+json.dumps(dict(python=sys.executable, dlc_version=deeplabcut.__version__, cuda_available=bool(torch.cuda.is_available()))), flush=True)
'''


def candidate_pythons(extra=(), home=None):
    home = Path(home) if home is not None else Path.home()
    prefixes = []
    registry = home / '.conda' / 'environments.txt'
    try:
        prefixes.extend(Path(line.strip()) for line in registry.read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#'))
    except OSError:
        pass
    for name in ('CONDA_PREFIX', 'VIRTUAL_ENV'):
        if os.environ.get(name): prefixes.append(Path(os.environ[name]))
    roots = [home/name for name in ('anaconda3','miniconda3','miniforge3','mambaforge')]
    roots += [home/'.conda']
    if not getattr(sys, 'frozen', False): roots.append(Path(sys.prefix))
    # Explorer-launched executables do not inherit an activated Conda shell.
    env_dirs = [Path(p) for p in os.environ.get('CONDA_ENVS_PATH', '').split(os.pathsep) if p]
    for directory in env_dirs:
        try: prefixes.extend(p for p in directory.iterdir() if p.is_dir())
        except OSError: pass
    if os.name == 'nt':
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    for company in ('ContinuumAnalytics', 'PythonCore'):
                        try:
                            with winreg.OpenKey(hive, 'Software\\Python\\' + company, 0, winreg.KEY_READ | view) as key:
                                for i in range(winreg.QueryInfoKey(key)[0]):
                                    try:
                                        name = winreg.EnumKey(key, i)
                                        with winreg.OpenKey(key, name + '\\InstallPath') as install:
                                            roots.append(Path(winreg.QueryValue(install, None)))
                                    except OSError: pass
                        except OSError: pass
        except ImportError: pass
    for variable in ('CONDA_EXE', '_CONDA_EXE'):
        if os.environ.get(variable): roots.append(Path(os.environ[variable]).parent.parent)
    for variable in ('LOCALAPPDATA','PROGRAMDATA'):
        if os.environ.get(variable):
            roots.extend(Path(os.environ[variable])/name for name in ('anaconda3','miniconda3','miniforge3'))
    if os.environ.get('LOCALAPPDATA'):
        roots.extend(Path(os.environ['LOCALAPPDATA'])/'Continuum'/name for name in ('anaconda3','miniconda3'))
    for root in roots:
        prefixes.append(root)
        try: prefixes.extend(p for p in (root/'envs').iterdir() if p.is_dir())
        except OSError: pass
    paths = list(extra)
    if not getattr(sys, 'frozen', False): paths.append(sys.executable)
    for prefix in prefixes:
        paths.extend([prefix/'python.exe', prefix/'bin'/'python', prefix/'bin'/'python3'])
    result = []
    seen = set()
    for path in paths:
        path = Path(path).absolute()  # Keep venv paths: resolving symlinks loses their identity.
        key = os.path.normcase(str(path))
        if path.is_file() and key not in seen:
            seen.add(key); result.append(str(path))
    return result


def child_environment(python=None):
    env = QtCore.QProcessEnvironment.systemEnvironment()
    for key in ('QT_API','PYQTGRAPH_QT_LIB','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH',
                'PYTHONHOME','PYTHONPATH','QT_QPA_PLATFORM','DLClight','MPLBACKEND'):
        env.remove(key)
    env.insert('PYTHONUTF8','1'); env.insert('PYTHONUNBUFFERED','1')
    if getattr(sys, 'frozen', False):
        bundled = [Path(sys.executable).parent, Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))]
        def external(value):
            value = os.path.normcase(os.path.abspath(value.strip('"')))
            return not any(value == os.path.normcase(str(root)) or value.startswith(os.path.normcase(str(root)) + os.sep) for root in bundled)
        env.insert('PATH', os.pathsep.join(p for p in env.value('PATH').split(os.pathsep) if p and external(p)))
        for key in ('LD_LIBRARY_PATH', 'LIBPATH'):
            original = env.value(key + '_ORIG')
            if original: env.insert(key, original)
            else: env.remove(key)
        env.insert('PYINSTALLER_RESET_ENVIRONMENT', '1')
    if python and os.name == 'nt':
        prefix = Path(python).parent
        env.insert('PATH',os.pathsep.join(map(str,[prefix,prefix/'Library'/'bin',prefix/'Scripts',prefix/'DLLs']))+os.pathsep+env.value('PATH'))
        env.insert('CONDA_PREFIX',str(prefix))
    return env


class EnvironmentDetector(QtCore.QObject):
    progress = QtCore.Signal(str)
    completed = QtCore.Signal(object)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.running = False
        self.process = None
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timeout)

    def start(self,extra=()):
        if self.running: return
        self.running = True; self.results = []; self.failures = []; self.cancelled = False
        self.candidates = candidate_pythons(extra)
        conda = os.environ.get('CONDA_EXE') or shutil.which('conda')
        if conda and Path(conda).suffix.lower() not in {'.bat','.cmd'}:
            self.phase = 'conda'
            self.progress.emit('Searching Conda environments…')
            self.execute(str(conda),['env','list','--json'],None,10000)
        else:
            self.next_candidate()

    def execute(self,executable,args,python,timeout):
        self.output = bytearray(); self.timed_out = False
        process = QtCore.QProcess(self); self.process = process
        process.setProcessEnvironment(child_environment(python))
        process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        process.started.connect(lambda:process.kill() if process is self.process and (self.cancelled or self.timed_out) else None)
        process.readyReadStandardOutput.connect(lambda:self.read(process))
        process.finished.connect(lambda code,status:self.finished(process,code))
        process.errorOccurred.connect(lambda error:self.failed_start(process,error))
        frozen = os.name == 'nt' and getattr(sys,'frozen',False)
        if frozen:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:
            self.timer.start(timeout); process.start(executable,args)
            if frozen: process.waitForStarted(5000)
        finally:
            if frozen: ctypes.windll.kernel32.SetDllDirectoryW(str(getattr(sys,'_MEIPASS',Path(sys.executable).parent)))

    def read(self,process):
        if process is self.process: self.output.extend(bytes(process.readAllStandardOutput()))

    def failed_start(self,process,error):
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.finished(process,-1)

    def timeout(self):
        if self.process is not None:
            self.timed_out = True; self.process.kill()

    def cancel(self):
        self.cancelled = True
        if self.process is not None: self.process.kill()

    def finished(self,process,code):
        if process is not self.process: return
        self.timer.stop(); self.read(process); self.process = None; process.deleteLater()
        output = self.output.decode('utf-8',errors='replace')
        if self.phase == 'conda' and code == 0:
            try:
                data = json.loads(output)
                extras = []
                for prefix in data.get('envs',[]):
                    extras.extend([Path(prefix)/'python.exe',Path(prefix)/'bin'/'python'])
                self.candidates = candidate_pythons(self.candidates+extras)
            except (ValueError,TypeError): pass
        elif self.phase == 'probe':
            found = None
            if code == 0:
                for line in output.splitlines():
                    if line.startswith('NWB_DLC_ENV='):
                        try: found = json.loads(line.split('=',1)[1])
                        except ValueError: pass
            if found:
                # Retain interpreter path, particularly for virtual environments.
                found['python'] = self.current
                self.results.append(found)
                self.progress.emit('Found: '+self.current)
            else:
                reason = 'Timed out (60 s)' if self.timed_out else output.strip()[-1200:] or 'Python unavailable'
                self.failures.append(dict(python=self.current,error=reason))
        self.next_candidate()

    def next_candidate(self):
        if self.cancelled or not self.candidates:
            self.running = False
            self.completed.emit(dict(environments=self.results,failures=self.failures,cancelled=self.cancelled))
            return
        self.current = self.candidates.pop(0); self.phase = 'probe'
        self.progress.emit('Test DLC : '+self.current)
        self.execute(self.current,['-u','-c',PROBE],self.current,60000)


def start_external(process, executable, arguments):
    """Start a foreign interpreter without inheriting frozen Windows DLL search."""
    frozen = os.name == 'nt' and getattr(sys, 'frozen', False)
    if frozen:
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        process.start(str(executable), arguments)
        if frozen: process.waitForStarted(5000)
    finally:
        if frozen:
            ctypes.windll.kernel32.SetDllDirectoryW(str(getattr(sys, '_MEIPASS', Path(sys.executable).parent)))
