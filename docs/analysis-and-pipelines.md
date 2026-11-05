# Analysis and pipelines

## Build analysis conditions

An analysis condition combines a file selection and filters with a name and display color. Select the recordings and filters that define the first group, then create a condition and click Set current filter. This stores the current selection for that condition. Select current filter restores the condition's selection for inspection.

Use Add condition for additional groups. Give each group an explicit name, such as a task condition or experimental cohort. Check the saved file count for each group. Use Conditions name and Save conditions to retain the definition for later use. Delete filter removes the saved definition selected in that interface, not the underlying recordings.

A condition is a data-selection rule, not a guarantee of independent observations. If several sessions belong to the same animal, account for that structure in the analysis and statistical interpretation.

## Configure an analysis

Select an analysis in the Analysis tab and read its description and availability information. Use Add data to specify required data sources. Review the parameter fields and validation messages. Available inputs depend on the analysis and the indexed objects in the selected recordings.

Event alignment has Start and End columns, each with an Event marker, Edge, and signed Offset (s). For example, Start = cue/start/-0.5 s and End = reward/start/+1 s extracts from 0.5 s before the cue until 1 s after the reward.

Distinct markers are paired using the first end event at or after the start, before the next start. When the start belongs to a trial, the end must also fall within that trial. Pairing uses the complete event sequence before trial selection. Missing boundaries or a calculated end at/before the start cause that segment to be skipped. Offsets are applied after matching and may create overlapping windows. Selecting the same marker uses the same occurrence, including the start and stop of the same interval; selecting the same edge on both sides reproduces a fixed window.

Time zero is the selected Start edge, before its offset. Segments share a relative-time axis and shorter segments are padded with NaN at the end. Requested data outside the recording are also NaN. Signal values are copied without resampling or interpolation. Alignment uses the nearest sample bin, with at most half a sample of timing uncertainty; `sample_grid_offset_s` records the source timestamp offset from that grid. Different sampling rates within a combined signal produce an explicit error. Irregular timestamps also produce an error rather than implicit resampling.

Each segment retains its actual boundaries, duration, end-event time and recorded sample count. Spike trains retain their variable-length timestamp arrays and use the same event pairing and window boundaries. Continuous windows and spike windows are half-open: start included, end excluded.

Use NaN-aware averages and inspect the number of contributing segments at later times. Perform filtering or spectral calculations on valid data, not on artificial NaN padding. Existing saved alignment settings remain loadable. Cached extractions now include both boundary definitions and an alignment version, so older extractions are recomputed when running with this implementation. Previously saved results are not deleted.

Check the actual events and resulting intervals on a known recording before interpreting a comparison. Identically named events need not carry the same experimental meaning in every dataset.

Choose Save analysis and a Folder name when results should be retained. Outputs are organized under the project Analysis directory, with exact contents determined by the analysis. Click Run, follow Analysis log, and check completion and errors for the requested inputs.

## Plot and retain results

Use Auto launch plotting GUI when you want the plotting interface to open after execution, or open Plot GUI manually. Number of figures per layer and Number of rows organize displayed figures. The plotting interface and analysis saving are distinct operations: confirm that the numerical outputs and metadata you need were actually saved.

Record the condition definitions, input objects, alignment, parameters, and analysis revision alongside a final result. A figure alone is insufficient to reproduce an analysis.

## Create a named pipeline

Processing and Analysis maintain separate pipeline collections. Configure a tool or analysis, then use Add to pipeline. Use New pipeline where needed and choose a descriptive name. A useful name identifies the intended workflow rather than only its creation date.

Review the saved steps before launching. Move up and Move down change their order. Edit step opens editable settings; Save changes commits them. Duplicate is useful for a parameter variant. Remove or Delete acts on the selected step or pipeline according to the current view.

A saved step captures settings for reuse. Changing the current GUI controls does not substitute for editing and saving the existing step. Check its stored targets and filters, particularly after moving files or changing the inventory.

## Run and review a pipeline

1. Select the pipeline and inspect its ordered steps.
2. Select the steps to execute using their checkboxes or selection controls.
3. Confirm that prerequisites are available before downstream steps.
4. Click Launch pipeline.
5. Review step status, logs, and the run report.
6. Use View run result for a selected completed analysis step where available.

Do not interpret the end of a pipeline as proof that every step succeeded. Review failed and skipped steps and any continuation setting exposed by the workflow. If an upstream output is absent, a downstream step may be unable to run or may encounter an older output. Verify the result actually used.

Pipeline definitions are stored in Pipelines/processing_pipelines.json and Pipelines/analysis_pipelines.json. Keep these with the project when archiving or sharing a workflow. These definitions do not package external source data or install new dependencies.

[Documentation overview](README.md)
