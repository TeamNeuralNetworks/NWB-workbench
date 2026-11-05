REBUILD NWB WORKBENCH (Windows x64, Python 3.11)

1. Extract this archive to a folder, e.g.
   U:\new task\NWB_environment_rebuild

2. Open ANACONDA PROMPT (not a Spyder console). Run:
   cd /d "U:\new task\NWB_environment_rebuild"
   conda env create -f environment-workbench.yml
   conda activate NWB_workbench
   python -m pip check
   python check_workbench_environment.py

   The check imports dependencies in separate processes and briefly opens a Qt
   window. If creation failed, keep the complete error rather than continuing
   to run the app in a partially created environment.

3. Launch the project from this new environment:
   python "U:\new task\NWB app\launch_GUI.py"

   To print its Python location:
   python -c "import sys; print(sys.executable)"

4. To use your existing Spyder installation:
   Tools > Preferences > Python interpreter > select the Python printed above.
   Restart the kernel. Spyder itself need not be installed in NWB_workbench.
   spyder-kernels 3.1.6 is included. If your Spyder requires another range, follow
   its displayed spyder-kernels install command with NWB_workbench activated.
   Use a fresh console/kernel; an old console may retain the previous Qt binding.

The environment YAML embeds the complete direct requirements list; there is no
machine-specific prefix or dependency on a separate requirements file. The TXT
list is also supplied for a Python 3.11 environment you have already created:
   python -m pip install -r requirements-workbench.txt
Do not use both installation routes in sequence; choose the YAML route for a new
Conda environment.

Included
Qt UI (PySide6 6.8.3), NWB/HDF5, Excel, video decoding, scientific processing and
plotting, SpikeInterface and its desktop viewer, PyInstaller, IPython and the
Spyder kernel. Qt libraries are installed by pip together with their matching
PySide6 components. The recipe does not install Spyder or PyQt5 into this environment.
Keep your existing DeepLabCut environment; it is independent of NWB_workbench.

The environment contains third-party dependencies; your Workbench source package
must still be present at U:\Functionnal_Imaging_Electrophysiology (or the path used
by your launcher). Creating an environment does not modify your project/NWB data
or update an existing .exe. Do not delete the old environment before verification.

Limits
MATLAB Engine requires a matching licensed MATLAB installation and is not included.
External spike sorters (Kilosort, MountainSort, WaveClus, etc.) have separate
installation requirements; SpikeInterface alone does not install all sorters.
Dependencies of your own additional scripts may also need to be added.

Validation
The list was checked against the available Workbench source imports and build
checks. Pip resolved the requested packages and their available Windows x64 /
Python 3.11 wheel candidates without a dependency conflict. This cross-platform
resolution runs on Linux: platform-specific environment markers can still differ
on Windows. It is not a completed Windows installation or an execution test of
all scientific workflows. Run pip check and the provided import/Qt check on your
machine. The Python minor version and direct package versions are pinned;
transitive packages are resolved during installation.

References
https://docs.spyder-ide.org/current/faq.html
https://spikeinterface.readthedocs.io/en/0.103.0/get_started/installation.html
https://spikeinterface-gui.readthedocs.io/en/latest/installation.html
