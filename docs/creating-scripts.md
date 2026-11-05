# Create processing and analysis scripts

## Choose the correct extension

Use Processing to derive data and store a new NWB object for later use. Use Analysis to consume selected, filtered data and produce figures and numerical results. The examples below are functional teaching templates. Test processing on a disposable copy of a recording before using original data.

| Extension | Entry declaration | Function receives | Function produces |
|---|---|---|---|
| Processing | TOOL = make_file_tool(...) | Selected data and runner keyword arguments | NWB object or list/tuple of objects |
| Analysis | ANALYSIS dictionary | data_manager, result, and parameter keywords | Figures/tables registered with result |

## Install a project script

Create an Examples folder beneath Script/Processing or Script/Analysis in the project. Copy the corresponding example into it. Restart Workbench and reopen the project so discovery imports the script. Select the Examples category and its uniquely named tool. If loading fails, inspect the project's Logs directory for script-discovery diagnostics.

Project scripts are discovered from the project folders; the examples shipped under docs/examples are not active until copied there. No application source edit is required. Built-in modules live under Processings or Analyses, but project folders are preferable for project-specific work. Avoid two tools with the same name in the same category.

Do not run data processing, open windows, or write files at module import time. Imports and declarations are evaluated during discovery. Put executable work inside the callable. Place optional standalone experiments behind an if __name__ == "__main__" guard.

## Processing example: subtract a channel mean

Save this as Script/Processing/Examples/demean_signal.py. Select a TimeSeries or ElectricalSeries in Processing and provide a new output name. The function removes each channel's mean over the full recording. It returns a generic TimeSeries with the same timestamps or rate and starting time.

The runner opens each eligible file, materializes selected input parameters, calls the function, stores returned objects in processing/Examples, and updates indexed output information. Let the runner own opening, writing, and closing the source NWB file. Do not add the returned object to a module yourself as well.

```python
"""Copy to <project>/Script/Processing/Examples/demean_signal.py."""
import numpy as np
from pynwb import TimeSeries
from GUI.processing_runner import make_file_tool


def demean_signal(data, saving_name, **kwargs):
    """Subtract each channel's temporal mean; preserve sample timing."""
    if data is None:
        raise ValueError("Select an input signal.")
    if not saving_name or "/" in saving_name:
        raise ValueError("Enter a nonempty output name without '/'.")
    values = np.asarray(data.data[:], dtype=float)
    if values.ndim not in (1, 2) or values.shape[0] == 0:
        raise ValueError("Expected samples or samples-by-channels.")
    # NWB conversion and offset map stored values to physical units.
    values = values * float(data.conversion) + float(data.offset)
    channel_conversion = getattr(data, "channel_conversion", None)
    if channel_conversion is not None:
        raise ValueError("This introductory template does not handle channel_conversion.")
    values = values - np.nanmean(values, axis=0, keepdims=True)
    if data.timestamps is not None:
        timing = {"timestamps": np.asarray(data.timestamps[:])}
    elif data.rate is not None:
        timing = {"rate": float(data.rate),
                  "starting_time": float(data.starting_time)}
    else:
        raise ValueError("Input has no usable timing information.")
    return TimeSeries(
        name=saving_name, data=values, unit=data.unit,
        description="Temporal mean subtracted from " + data.name,
        **timing,
    )


TOOL = make_file_tool(
    name="Example: demean signal",
    description="Subtract each channel's temporal mean; return a new TimeSeries.",
    process_one=demean_signal,
    continue_on_error=False,
    debug_mode=False,
    params={
        "category": "Examples",
        "data": {
            "type": "data", "label": "Input signal", "required": True,
            "neurodata_type_in": ["TimeSeries", "ElectricalSeries"],
            "load_data": False,
        },
        "saving_name": {
            "type": "saving_name", "label": "Output name",
            "default": "demeaned_signal", "required": True,
        },
    },
)
```

## Understand the processing contract

Parameter keys must match your function arguments. A data parameter with load_data=False supplies the NWB object, so its timing, units, and metadata remain available. With load_data=True, the runner supplies its data array instead. It also supplies a companion name argument, such as data_name.

The runner injects file_path, project_root, inventory_row, logger, tool_name, and, for NWB processing, nwbfile. Other configuration values may also be passed. Accept **kwargs unless you explicitly accept every keyword passed by the runner. Parameter declaration dictionaries configure the GUI; they are not the values received by the function.

Use a saving_name parameter for output names so the interface can participate in output-conflict handling. Keep the returned object's name consistent with that value. Returning several NWB objects is supported, but each needs a distinct name. Returning None means there is no object for the runner to attach; it is not a general-purpose successful-result format for arbitrary data.

The example explicitly sets category=Examples, making its NWB destination predictable. Do not use file paths as object names. The GUI replacement option governs existing outputs; check the log after a repeated run.

This example loads the entire signal into memory, accepts only one- or two-dimensional arrays with time first, and rejects per-channel conversion factors. NaNs are ignored when calculating the mean; all-NaN channels remain NaN. It returns a generic TimeSeries and therefore does not retain an ElectricalSeries electrode-table link. Adapt these choices deliberately for a production electrophysiology operation. Demeaning is not a substitute for filtering or artifact rejection.

## Analysis example: a mean trace by condition

Save this as Script/Analysis/Examples/mean_trace.py. Define and save your conditions in Analysis, choose this analysis, add a compatible signal, and configure event alignment. The application extracts and filters data before invoking the function. This example expects an xarray array with a time dimension.

```python
"""Copy to <project>/Script/Analysis/Examples/mean_trace.py."""
import pandas as pd
import matplotlib.pyplot as plt


def mean_trace(data_manager, result, title="Mean trace", **kwargs):
    """Illustrative pooled mean, not an animal-level statistical estimator."""
    for data_type in data_manager.get_data_types():
        fig, ax = plt.subplots()
        tables = []
        for condition in data_manager.get_conditions():
            data = data_manager.get_data(data_type, condition)
            if "time" not in getattr(data, "dims", ()):
                raise ValueError("This example requires an xarray array with a time dimension.")
            reduce_dims = [dim for dim in data.dims if dim != "time"]
            trace = data.mean(dim=reduce_dims, skipna=True) if reduce_dims else data
            if trace.size == 0:
                raise ValueError("The selected condition has no samples.")
            ax.plot(trace.time.values, trace.values, label=str(condition))
            tables.append(pd.DataFrame({
                "condition": str(condition), "time_s": trace.time.values,
                "mean_value": trace.values,
            }))
        if not tables:
            plt.close(fig)
            raise ValueError("No conditions were provided.")
        ax.set(title=f"{title}: {data_type}", xlabel="Time (s)",
               ylabel="Mean signal (input units)")
        ax.legend()
        fig.tight_layout()
        result.add(fig, data=pd.concat(tables, ignore_index=True))
        result.add_log(f"Mean trace created for {data_type}")


ANALYSIS = {
    "name": "Example: mean trace",
    "description": "Plot a pooled mean over all dimensions except time.",
    "func": mean_trace,
    "align_to_event_marker": True,
    "debug_mode": False,
    "params": {
        "title": {"type": "str", "label": "Figure title", "default": "Mean trace"},
    },
    "input_spec": {
        "accepted_data": ["TimeSeries", "ElectricalSeries"],
        "allow_multiple": True, "required": True,
    },
}
```

## Understand the analysis contract

The runner calls func(data_manager=data_manager, result=result, **analysis_kwargs). It does not use the function's return value as the saved analysis result. Register figures explicitly with result.add(fig, data=table). The optional table is attached to that figure; calling result.add(None, data=table) does not register data in the current implementation. Use result.add_log for useful diagnostic context.

The data manager provides get_data_types(), get_conditions(), and get_data(data_type, condition). The latter loads the prepared data for that pairing. Signal data can contain time, channels, trial/alignment, and subject-related dimensions or coordinates. Inspect data.dims, data.sizes, and data.coords rather than assuming a fixed axis order. Not every data type returns an xarray signal, which is why the example validates the input.

The input_spec fields restrict the selectable NWB types, whether multiple data inputs are allowed, and whether input is required. align_to_event_marker exposes alignment controls. Parameters such as title are passed by keyword to your function. Choose Save analysis in the GUI to retain outputs and use Plot GUI to inspect the registered figures.

### Independent event boundaries

Use `align_to_event_marker=True` to expose both Start and End controls. For a fixed alignment specification, `event_marker`, `event_path`, and `edge` identify the Start reference. `end_event_marker`, `end_event_path`, and `end_edge` identify the End reference. Offsets are `start_offset_s` and `end_offset_s`. Legacy specifications with a single marker/edge still use that reference for both boundaries. A fixed old marker/edge remains fixed for both unless end fields are supplied explicitly.

Extracted continuous data remain xarray objects with a shared time axis. Short segments contain trailing NaNs. The alignment dimension keeps the Start marker's name for compatibility. Coordinates include `reference_time`, `end_event_time`, `segment_start_time`, `segment_stop_time`, `segment_duration_s`, `end_event_relative_s`, `segment_stop_relative_s`, and `valid_sample_count`. The count describes copied source samples, which may themselves contain original NaNs. `sample_grid_offset_s` records sub-sample timing relative to the shared grid. No values are interpolated. Spike-time arrays retain their object representation and boundary coordinates, without artificial spike padding.

Do not infer a variable segment's duration from `end_offset_s` or its legacy alias `pad_after_s`: these are offsets relative to its End event. Use the per-segment boundary coordinates. Use NaN-aware reductions where appropriate; do not feed NaN padding directly into filters or FFTs. Report contributing segment counts when averaging variable durations. The existing `examples/mean_trace.py` uses `skipna=True` for its mean.

Do not call plt.show(), start a second QApplication, or open a blocking dialog from the analysis function. The runner controls worker execution and figure presentation. debug_mode=True is available for troubleshooting, but can run work on the GUI thread and block interaction; use False for normal execution.

The example averages every dimension except time. This pools channels and observations and does not implement animal-level weighting, paired comparisons, confidence intervals, or hypothesis tests. Missing values can change the contributing observations across time. Adapt aggregation to the experimental unit before using it for scientific inference. The plotted table retains condition and time but is a summary, not the complete extracted dataset.

## Add parameters

Use matching declaration keys and function arguments. Common GUI types include str, int, float, bool, and list. A list selector uses values and default; numeric fields can use min and max. required marks mandatory input. Keep labels explicit about units. Validate scientific constraints inside the function as well, such as a positive duration or a lower cutoff below an upper cutoff. Raise ValueError with a useful message when an input is unsuitable.

For processing, data and saving_name have special selection/output behavior. For analysis, select input objects through input_spec and Add data rather than copying the processing data-parameter pattern into ANALYSIS.

## Test and distribute

1. Import the script without opening the GUI and confirm that import itself performs no analysis or file writes.
2. Call its computational function on a small synthetic input with a known expected output.
3. Install it in a temporary project and verify that it appears in the right category.
4. Run it on one disposable recording; check output name, type, dimensions, timing, units, and numerical values.
5. Try missing data and invalid parameters. Confirm that failures are understandable in the log.
6. Repeat processing with replacement disabled and enabled. Verify that unrelated NWB objects remain intact.
7. Test several files or conditions, then add the configured operation to a pipeline and inspect its saved settings.
8. Test saved analysis tables and figures by reopening them, not only by looking at the live plot.
9. If using an executable, include new dependencies in a rebuild and test the script in that executable.

The supplied examples were exercised locally with synthetic TimeSeries inputs and an xarray-backed test manager. This checks their numerical core and result registration. It does not substitute for a Windows GUI run, NWB write/replacement integration testing, or validation of your scientific method.

Project scripts remain editable, but their dependencies must be available in the Python environment or executable bundle. See Building the Windows application for --project and --include-package. Keep the script version with saved conditions and pipeline definitions so a result can be reproduced.
