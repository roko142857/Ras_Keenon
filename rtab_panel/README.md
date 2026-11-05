# XMAN manual-panel RTAB extension

Independent loopback service for the existing keyboard UI. It never publishes motion or initializes the robot SDK. `server.py` runs the existing finite `recording.live_session`; `cloud.py` projects matched RGB-D into RTAB camera poses. Browser WebGL displays a bounded point cloud and trajectory, with distinct live-odometry and optimized labels.

Configuration: `settings.json` (robot-specific absolute paths). Default UI duration 30 seconds; choices 15/30/60/120. Existing capture NVMe UUID, disk budget and camera/head locks remain enforced. The dedicated `parameters.txt` preserves input frame IDs with `Mem/GenerateIds=false`; other RTAB capture users are unaffected.

Stop writes the capture's native `finish-camera` file. Do not send SIGINT to live_session from the UI: a signal after camera closure can interrupt SLAM database finalization. Start stays blocked until the prior worker and its completion receipt finish.

Installed extension root: `/home/peanut/ras_keenon_ws/rtab_panel`. The existing `manual.launch.py` now reuses the service or starts it in hardware mode, always idle (no camera acquisition until Start). Standalone start: configured virtualenv Python, `PYTHONPATH=/home/peanut/ras_keenon_ws`, and `-m rtab_panel.server --config <extension>/settings.json`. Current already-running instance was started from the staging directory; same final server code. No separate OS boot service is installed.

The keyboard module adds only RTAB static assets and proxy routes. The source checkout and installed ROS Python package are separate copies on this robot; preserve each baseline. Recorded candidates/backups/deployment scripts and verification are under `runs/xman-live/20261105/rtab-keyboard-panel-v1/`. Do not copy a whole source controller over the installed controller: their pre-existing motion changes differ.

Validation: `python -m pytest tests/test_xman_rtab_panel.py -q`, `node tests/test_xman_rtab_panel_ui.mjs`, and targeted Ruff. No G1 tests. See run handoff for real static capture and UI results. Static tests do not certify room-scale loop closure or autonomous exploration.
