# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-27

### Added

- Bar widget showing Jetson GPU load, CPU usage, RAM usage and temperature
  (disk usage and module power optional), colour-coded against configurable
  warning and critical thresholds using the active theme's colours.
- Detail panel: board and power mode, GPU load and clock (with a note that
  Jetson memory is unified), CPU usage, frequency, load average and per-core
  bars (offline cores dimmed), RAM and zram swap, disk usage per mount, every
  readable thermal zone plus NVMe, INA3221 power rails and fan.
- `jetson_sysmon.py`: a stdlib-only helper that stays running and prints one
  JSON line per interval from `/proc` and `/sys`. No root, no `tegrastats`,
  no `nvidia-smi`. Detects GPU, thermal, power and fan paths at runtime and
  degrades to a dash on boards that lack them.
- Settings: bar metrics, icon or text labels, refresh interval, disk mount
  points, bar temperature sensor, Celsius or Fahrenheit, thresholds.
- Tests against fake sysfs trees (AGX Orin, Xavier NX, a generic PC), Node
  tests for the formatting model, and CI for GitHub Actions and GitLab
  (qmllint, ShellCheck, shfmt, gitleaks, private-info scan).
