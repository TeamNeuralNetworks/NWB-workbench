# Portable Workbench projects

Keep the relative arrangement of the project, NWB files and raw videos:

    new task/
      NWB app/
      NWB_data/
      raw_data/

The drive letter and the location of `new task` can change. Project folders can
also be renamed. Workbench stores references such as
`@project/../NWB_data/recording.nwb` and resolves them against the project folder
at runtime. The `@project/` prefix distinguishes these references from ordinary
relative paths used by third-party libraries. Do not add this prefix manually in
file selectors.

New projects created with this version use portable references automatically.
Existing projects continue to work as before until migrated.

## Installation and existing projects

1. Close Workbench and external labeling, training, analysis and curation tools.
2. Copy the updated package files over your current main package. Keep its folder
   name `Functionnal_Imaging_Electrophysiology`. Install migration dependencies if
   missing: `python -m pip install PyYAML openpyxl`.
3. If you use the executable, rebuild and update it before migrating settings:

       conda activate nwb_workbench
       cd /d "U:\Functionnal_Imaging_Electrophysiology"
       python build_windows.py --project "U:\new task\NWB app" --update-project

4. Preview one existing project:

       python Tools\General\migrate_project_paths.py "U:\new task\NWB app"

5. If the preview reports no errors, apply the conversion:

       python Tools\General\migrate_project_paths.py "U:\new task\NWB app" --apply

The migration backs up every modified file, preserving original bytes, under
`.portable-path-backups/<timestamp>/` inside the project. If a replacement fails,
it restores already modified files. Corrupt/unsupported records block migration;
they are reported, never silently replaced with empty settings. The preview does
not change project files. Repeating the migration at the same location has no
further effect.

If the drive has ALREADY changed and the recorded project location is incorrect,
supply its original full project path explicitly, for example:

    python Tools\General\migrate_project_paths.py "D:\new task\NWB app" --previous-root "U:\new task\NWB app" --apply

Do not move the folders while Workbench, DeepLabCut or a sorting/curation process
is running. Open the updated executable inside the moved project folder. Desktop
shortcuts belong to each computer and may need updating.

## What is covered

- Project configuration, file/content/trial inventories and prepared-data
  inventory paths.
- Saved analysis conditions, selected files and processing/analysis pipelines.
- DeepLabCut registration, source libraries, role selections, model records,
  requests/results and postprocessing sidecars. Old compressed postprocessing
  metadata is resolved when read, without rewriting prediction arrays.
- Spike-sorting recipes, configuration, summaries, time maps, analyzer JSON
  configurations and curation records. Phy's literal `dat_path` in `params.py`
  uses a normal path relative to its own Phy folder.
- New Python project launchers derive their project folder from `__file__` and
  use a relative source-package path when both are on the same volume.

Scientific NWB, video, trained checkpoint, label HDF5 and prediction files are
not rewritten by migration. Previous project locations are retained as historical
aliases in project metadata so legacy external video paths inside NWBs and source
references inside compressed postprocessing metadata can be resolved read-only.
This means those historical alias entries themselves remain absolute; active
Workbench file references use the project-relative format.

DeepLabCut and SpikeInterface read their own configuration formats. Workbench
materializes project references to absolute paths around those external calls and
encodes the configuration again afterward. Use Workbench to launch these tools
for migrated projects. A tagged configuration is not intended to be passed
unchanged to a separate manual `deeplabcut` or `spikeinterface` session.

## Boundaries

Python/DeepLabCut environment executables remain local to the computer. Paths on
another Windows volume cannot be represented relative to this project; the
migration retains them and flags files containing remaining absolute references.
The same folder arrangement must be kept for referenced data outside the project.

User-written scripts and arbitrary pickle/third-party binary files are not
rewritten. In particular, change hard-coded drive letters in custom scripts to
use the provided `project_path` / `project_root` context. Unchanged NWB HDF5
external links to other HDF5 files are not migrated; the read-only compatibility
resolver specifically covers the application's external ImageSeries video paths.

Existing generated Python launchers can be refreshed by opening the project with
the updated Python package at its new location. Custom launch scripts are kept.

## For script authors

Use absolute runtime paths from Workbench as usual. When persisting your own
project metadata, use `GUI.project_paths.json_read`, `json_write`, `json_load`
or `json_dump`, or `encode(value, project_root)` and `decode(value, project_root)`.
These helpers only activate for projects with `portable_paths_version = 1`.
They do not monkeypatch standard Python, pandas or third-party libraries.
Do not encode input arrays or manually attach encoded paths to an NWB object.

## Validation

The supplied update was checked with synthetic projects, actual NWB export,
real SpikeInterface binary recording/analyzer loading, and an offscreen Qt GUI.
Tests cover moving the workspace, renaming the project, metadata and inventory
resolution, model/prediction integrity, postprocessing reuse, export status,
standalone DLC helper imports, migration rollback, idempotence, native Phy paths,
and creation of new portable projects. Full DeepLabCut training, external Phy
GUI interaction and a Windows executable build were not run in this environment.

## Relative video path correction

The player and both DLC source lookup routes now collapse parent-directory
segments in NWB-relative video references before accessing the filesystem.
This avoids unnecessarily long Windows paths. Installing this correction
requires updating the source package and rebuilding the executable; an already
migrated project does not need another migration. NWB files are unchanged.
