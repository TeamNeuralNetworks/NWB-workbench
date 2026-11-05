# Mean corner-normalized trajectory

Install the update into the main package and restart Workbench. Select **Analysis → Behaviour → Mean corner-normalized trajectory**.

1. Select NWB files or saved conditions. Tracking for the moving body parts and all four corners must already be exported in the four-column format (v146 or later).
2. Enter original DLC names separated by commas, such as `nose, tail_base`. This creates separate mean trajectories for each body part, not their centroid. No Add data selection is needed.
3. Set the associated video object, normally `acquisition/topdown_video`. Enter the DLC project name only if several complete tracking groups match that video.
4. Check corner names. Defaults are `left_top_corner`, `right_top_corner`, `right_bottom_corner`, and `left_bottom_corner`.
5. Choose the Start/End event markers, edges and offsets using the usual event alignment controls. For example, choose cue onset for both edges and offsets -1 and +2 seconds. The horizontal time origin remains the selected Start edge, not its offset. Saved trial-row filters are applied after pairing the full event sequences. Trial-time cropping filters are rejected: use alignment offsets instead.
6. Choose the averaging unit: **animal** (default), **file**, or **trial**. Animal weighting first averages trials within each subject, then averages the subject trajectories equally at each time. It requires NWB `subject_id`. File weighting gives each recording equal weight; trial weighting pools trials.
7. Enable Save analysis and run. Each body part produces a mean spatial path and mean X/time and Y/time curves. Shading is SEM across the selected averaging units. Tables contain time, mean X/Y, SEM, contributing unit counts and trial counts. Individual trial coordinates can optionally be included, but this can produce large tables.

## Normalization and sampling

For each video, each corner is estimated by the median of its usable coordinates over the complete recording. A projective transform maps top-left, top-right, bottom-right and bottom-left to (0,0), (1,0), (1,1) and (0,1). Coordinates are fractions of arena width/height, not centimeters. Y increases downwards, matching the video. Coordinates outside the arena remain outside [0,1]; they are not clipped. Incorrect/crossed or degenerate corner geometry is rejected.

Camera and arena must remain fixed within a video. This analysis does not correct camera movement or lens distortion. The plane transform is most accurate for points near the arena floor; elevated body parts can retain perspective/parallax error.

The default confidence thresholds are 0.75 for original moving detections and 0.9 for original corner detections. Finite coordinates already marked interpolated or fixed by applied postprocessing remain usable despite their low original confidence. Missing coordinates stay missing. The default corner sample requirement is 10 usable samples per corner.

The script reads irregular video timestamps directly. It normalizes positions first, then linearly resamples onto a common event-relative time grid (default 0.05 seconds). Resampling uses adjacent finite frames only, with at most 0.15 seconds between anchors by default. It does not bridge excluded detections, extrapolate before/after recorded samples, or use samples outside the selected event segment. Different-length segments contribute only where they contain data. No trial-duration normalization is performed.

Per-recording omissions and calibration matrices are recorded in the analysis log. Check the log and contributing counts before interpreting the mean. A mean spatial path is the path of the average position over time, not a route necessarily followed by any individual animal. SEM is descriptive; no hypothesis test is run.

The analysis reads NWB files without modifying them or their saved postprocessing. Its script is `Analyses/Behaviour/mean_corner_normalized_trajectory.py`. The included analysis-runner update enables its opt-in direct NWB input; existing analyses continue using their existing extraction path.
