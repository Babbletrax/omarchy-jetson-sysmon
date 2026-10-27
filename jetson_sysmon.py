#!/usr/bin/env python3
"""Jetson system snapshot collector for the Jetson SysMon Omarchy plugin.

Prints one compact JSON object per line on stdout every --interval seconds.
Reads /proc and /sys only: no root, no tegrastats, no nvidia-smi, no
third-party modules. Anything this board does not expose is left out (null or
an empty list) so the widget can show a dash instead of a made-up number.

--root points every /proc and /sys read at another directory, so the tests can
run the collector against a fake Jetson tree on any machine.
"""

import argparse
import glob
import json
import os
import re
import sys
import time

SCHEMA = 1

# devfreq / platform device names used by Jetson integrated GPUs:
# gm20b (Nano/TX1), gp10b (TX2), gv11b (Xavier), ga10b (Orin), gb10b (Thor),
# and the generic "gpu" node names (17000000.gpu, 57000000.gpu, gpu.0).
GPU_NAME_RE = re.compile(r"(^|[.\-_/])(gpu|gm20b|gp10b|gv11b|ga10b|gb10b)($|[.\-_/0-9])", re.I)

# Fallback locations for the GPU load file when no devfreq node is found.
GPU_LOAD_GLOBS = (
    "/sys/devices/platform/bus@0/*.gpu/load",
    "/sys/devices/platform/*.gpu/load",
    "/sys/devices/platform/gpu.0/load",
    "/sys/devices/gpu.0/load",
    "/sys/devices/platform/bus@0/*.ga10b/load",
    "/sys/devices/*.gv11b/load",
    "/sys/devices/platform/*.gv11b/load",
    "/sys/devices/*.gp10b/load",
    "/sys/devices/*.gm20b/load",
)

# Zones that are not real measurements: Xavier's PMIC-Die reads a fixed
# 100 C placeholder and thermal-fan-est is a fan-control estimate.
SKIP_ZONES = ("PMIC-Die", "thermal-fan-est")

POWER_HWMON_NAMES = ("ina3221", "ina238", "ina219", "ina226", "ina2xx")
TOTAL_RAIL_NAMES = ("VDD_IN", "POM_5V_IN", "VIN")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        # ENODATA (switched-off cv* zones), EACCES, ENOENT, EINVAL: all "absent".
        return None


def read_int(path):
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip().split()[0])
    except (ValueError, IndexError):
        return None


def natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def parse_cpu_list(text):
    """Parse a sysfs CPU list such as '0-3,6,8-11' into a sorted list."""
    cpus = set()
    for part in (text or "").strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                cpus.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                cpus.add(int(part))
            except ValueError:
                continue
    return sorted(cpus)


def parse_proc_stat(text):
    """Return {"cpu": (busy, total), "cpu0": (busy, total), ...}."""
    out = {}
    for line in (text or "").splitlines():
        if not line.startswith("cpu"):
            continue
        fields = line.split()
        try:
            values = [int(v) for v in fields[1:]]
        except ValueError:
            continue
        if len(values) < 4:
            continue
        # user nice system idle iowait irq softirq steal [guest guest_nice]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values[:8])
        out[fields[0]] = (total - idle, total)
    return out


def usage_pct(prev, cur):
    if not prev or not cur:
        return None
    busy = cur[0] - prev[0]
    total = cur[1] - prev[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * busy / total)), 1)


def parse_meminfo(text):
    out = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                out[key.strip()] = int(parts[0]) * 1024
            except ValueError:
                continue
    return out


def pct(used, total):
    if not total:
        return None
    return round(100.0 * used / total, 1)


class Collector:
    def __init__(self, root="", mounts=("/",)):
        self.root = root.rstrip("/")
        self.mounts = [m for m in mounts if m] or ["/"]
        self.prev_stat = {}
        self.cpu_ids = []
        self.cpu_max_khz = {}
        self.gpu = None
        self.zones = []
        self.seen_zones = set()
        self.power_hwmon = []
        self.power_channels = []
        self.nvme_hwmon = []
        self.fan = {}
        self.board = None
        self.power_mode = None
        self.detect()

    # -- paths -----------------------------------------------------------
    def p(self, path):
        return self.root + path

    def unroot(self, path):
        if self.root and path.startswith(self.root):
            return path[len(self.root):] or "/"
        return path

    def glob(self, pattern):
        # Natural order, so hwmon10 sorts after hwmon3.
        return sorted(glob.glob(self.p(pattern)), key=natural_key)

    def realpath(self, path):
        return os.path.realpath(path)

    # -- detection (once at start) ----------------------------------------
    def detect(self):
        self.board = self.detect_board()
        self.cpu_ids = parse_cpu_list(read_text(self.p("/sys/devices/system/cpu/possible")))
        if not self.cpu_ids:
            stat = parse_proc_stat(read_text(self.p("/proc/stat")))
            self.cpu_ids = sorted(int(k[3:]) for k in stat if k != "cpu" and k[3:].isdigit())
        self.cpu_max_khz = {
            cpu: read_int(self.p("/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq" % cpu))
            for cpu in self.cpu_ids
        }
        self.gpu = self.detect_gpu()
        self.zones = self.detect_zones()
        self.power_hwmon, self.nvme_hwmon, self.fan = self.detect_hwmon()
        self.power_channels = self.detect_power_channels()
        self.power_mode = self.detect_power_mode()

    def detect_board(self):
        model = read_text(self.p("/proc/device-tree/model"))
        if model:
            return model.replace("\x00", "").strip() or None
        return None

    def detect_gpu(self):
        for node in self.glob("/sys/class/devfreq/*"):
            name = os.path.basename(node)
            device = self.realpath(os.path.join(node, "device"))
            if not (GPU_NAME_RE.search(name) or GPU_NAME_RE.search(os.path.basename(device))):
                continue
            load = os.path.join(device, "load")
            return {
                "name": name,
                "load": load if os.path.exists(load) else self.find_gpu_load(),
                "devfreq": node,
            }
        load = self.find_gpu_load()
        if load:
            return {"name": os.path.basename(os.path.dirname(load)), "load": load, "devfreq": None}
        return None

    def find_gpu_load(self):
        for pattern in GPU_LOAD_GLOBS:
            for path in self.glob(pattern):
                if read_int(path) is not None:
                    return path
        return None

    def detect_zones(self):
        zones = []
        for zone in self.glob("/sys/class/thermal/thermal_zone*"):
            kind = (read_text(os.path.join(zone, "type")) or "").strip()
            if not kind or kind in SKIP_ZONES:
                continue
            name = re.sub(r"[-_]thermal$", "", kind)
            zones.append((name, os.path.join(zone, "temp")))
        return zones

    def detect_hwmon(self):
        power, nvme, fan = [], [], {}
        for hw in self.glob("/sys/class/hwmon/hwmon*"):
            name = (read_text(os.path.join(hw, "name")) or "").strip()
            if name in POWER_HWMON_NAMES:
                power.append(hw)
            elif name == "nvme":
                nvme.extend(self.detect_nvme_sensors(hw))
            elif name == "pwmfan" and os.path.exists(os.path.join(hw, "pwm1")):
                fan["pwm"] = os.path.join(hw, "pwm1")
            elif name == "pwm_tach" and os.path.exists(os.path.join(hw, "rpm")):
                fan["rpm"] = os.path.join(hw, "rpm")
            elif os.path.exists(os.path.join(hw, "fan1_input")) and "rpm" not in fan:
                fan["rpm"] = os.path.join(hw, "fan1_input")
        return power, nvme, fan

    def detect_nvme_sensors(self, hw):
        """Every labelled temperature of one NVMe drive.

        temp1 is the spec's Composite temperature; temp2..temp9 are the
        drive's own sensors (vendor-defined, often the controller). Warning and
        critical thresholds come from temp*_max / temp*_crit; a sensor without
        its own falls back to the drive's Composite thresholds.
        """
        device = os.path.basename(self.realpath(os.path.join(hw, "device")))
        drive = device if device.startswith("nvme") else "nvme"

        def threshold(path):
            milli = read_int(path)
            # Unset thresholds read 0, negative, or 65261.85 C (0xFFFF K).
            # Half-up to 0.1 C (83850 -> 83.9), unlike round()'s half-even.
            return (milli + 50) // 100 / 10.0 if milli is not None and 0 < milli < 150000 else None

        sensors = []
        for input_path in sorted(glob.glob(os.path.join(hw, "temp*_input")),
                                 key=lambda p: int(re.search(r"temp(\d+)_input$", p).group(1))):
            index = re.search(r"temp(\d+)_input$", input_path).group(1)
            label = (read_text(os.path.join(hw, "temp%s_label" % index)) or "").strip()
            if not label:
                label = "Composite" if index == "1" else "Sensor %s" % index
            sensors.append({
                "name": drive,
                "label": label,
                "path": input_path,
                "warn": threshold(os.path.join(hw, "temp%s_max" % index)),
                "crit": threshold(os.path.join(hw, "temp%s_crit" % index)),
            })
        composite = next((x for x in sensors if x["label"] == "Composite"), None)
        for sensor in sensors:
            if composite and sensor["warn"] is None and sensor["crit"] is None:
                sensor["warn"], sensor["crit"] = composite["warn"], composite["crit"]
        return sensors

    def detect_power_channels(self):
        channels = []
        for hw in self.power_hwmon:
            for label_path in sorted(glob.glob(os.path.join(hw, "in*_label"))):
                chan = re.search(r"in(\d+)_label$", label_path).group(1)
                label = (read_text(label_path) or "").strip()
                if not label or label.lower().startswith("sum of"):
                    continue
                # ina238-style chips report power directly (uW); INA3221 has
                # bus voltage (mV) and current (mA) per channel.
                power = os.path.join(hw, "power%s_input" % chan)
                channels.append((
                    label,
                    power if os.path.exists(power) else None,
                    os.path.join(hw, "in%s_input" % chan),
                    os.path.join(hw, "curr%s_input" % chan),
                ))
        return channels

    def detect_power_mode(self):
        status = read_text(self.p("/var/lib/nvpmodel/status")) or ""
        match = re.search(r"pmode:\s*(\d+)", status)
        if not match:
            return None
        mode_id = int(match.group(1))
        conf = (read_text(self.p("/var/lib/nvpmodel/conf_file_path")) or "").strip() or "/etc/nvpmodel.conf"
        text = read_text(self.p(conf)) or read_text(self.p("/etc/nvpmodel.conf")) or ""
        for found in re.finditer(r"<\s*POWER_MODEL\s+ID=(\d+)\s+NAME=([^\s>]+)\s*>", text):
            if int(found.group(1)) == mode_id:
                return found.group(2)
        return "mode %d" % mode_id

    # -- sampling -----------------------------------------------------------
    def sample_cpu(self):
        stat = parse_proc_stat(read_text(self.p("/proc/stat")))
        online = set(parse_cpu_list(read_text(self.p("/sys/devices/system/cpu/online"))))
        cores = []
        freqs = []
        for cpu in self.cpu_ids:
            key = "cpu%d" % cpu
            base = "/sys/devices/system/cpu/cpu%d/cpufreq/" % cpu
            is_online = (cpu in online) if online else key in stat
            mhz = read_int(self.p(base + "scaling_cur_freq")) if is_online else None
            max_khz = self.cpu_max_khz.get(cpu)
            if mhz is not None:
                mhz = round(mhz / 1000)
                freqs.append(mhz)
            cores.append({
                "id": cpu,
                "online": bool(is_online),
                "pct": usage_pct(self.prev_stat.get(key), stat.get(key)) if is_online else None,
                "mhz": mhz,
                "max_mhz": round(max_khz / 1000) if max_khz else None,
            })
        result = {
            "pct": usage_pct(self.prev_stat.get("cpu"), stat.get("cpu")),
            "cores": cores,
            "online": sum(1 for c in cores if c["online"]),
            "mhz": round(sum(freqs) / len(freqs)) if freqs else None,
            "max_mhz": max([c["max_mhz"] or 0 for c in cores] or [0]) or None,
        }
        load = (read_text(self.p("/proc/loadavg")) or "").split()
        result["load"] = [float(v) for v in load[:3]] if len(load) >= 3 else None
        self.prev_stat = stat
        return result

    def sample_gpu(self):
        if not self.gpu:
            return None
        out = {"name": self.gpu["name"], "pct": None, "mhz": None, "max_mhz": None, "min_mhz": None,
               "memory": "unified"}
        if self.gpu.get("load"):
            raw = read_int(self.gpu["load"])
            if raw is not None:
                # nvgpu reports load in tenths of a percent (0-1000).
                out["pct"] = round(max(0.0, min(100.0, raw / 10.0)), 1)
        node = self.gpu.get("devfreq")
        if node:
            for key, name in (("mhz", "cur_freq"), ("max_mhz", "max_freq"), ("min_mhz", "min_freq")):
                hz = read_int(os.path.join(node, name))
                if hz is not None:
                    out[key] = round(hz / 1e6)
        return out

    def sample_memory(self):
        info = parse_meminfo(read_text(self.p("/proc/meminfo")))
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - avail)
        swap_total = info.get("SwapTotal", 0)
        swap_used = max(0, swap_total - info.get("SwapFree", 0))
        zram = []
        for dev in self.glob("/sys/block/zram*"):
            stat = (read_text(os.path.join(dev, "mm_stat")) or "").split()
            size = read_int(os.path.join(dev, "disksize"))
            if not size:
                continue
            entry = {"name": os.path.basename(dev), "size": size, "orig": None, "compr": None, "used": None}
            if len(stat) >= 3:
                entry["orig"], entry["compr"], entry["used"] = int(stat[0]), int(stat[1]), int(stat[2])
            zram.append(entry)
        return {
            "total": total,
            "used": used,
            "available": avail,
            "pct": pct(used, total),
            "cached": info.get("Cached", 0) + info.get("Buffers", 0),
            "swap_total": swap_total,
            "swap_used": swap_used,
            "swap_pct": pct(swap_used, swap_total),
            "zram": zram,
        }

    def sample_disks(self):
        disks = []
        for mount in self.mounts:
            try:
                st = os.statvfs(self.p(mount) if self.root else mount)
            except OSError:
                disks.append({"mount": mount, "total": None, "used": None, "pct": None})
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - st.f_bfree * st.f_frsize
            # df semantics: percentage of the space available to users.
            denom = used + free
            disks.append({
                "mount": mount,
                "total": total,
                "used": used,
                "pct": round(100.0 * used / denom, 1) if denom else None,
            })
        return disks

    def sample_temps(self):
        temps = []
        for name, path in self.zones:
            milli = read_int(path)
            # Disabled zones read ENODATA or a -40 C / -256 C sentinel. A zone
            # that has worked before is power-gated rather than absent (Orin's
            # gpu-thermal reads ENODATA while the GPU is idle and gated), so it
            # stays listed with no value; never-seen zones (cv0-2) stay hidden.
            if milli is None or milli <= -40000 or milli > 200000:
                if name in self.seen_zones:
                    temps.append({"name": name, "c": None})
                continue
            self.seen_zones.add(name)
            temps.append({"name": name, "c": round(milli / 1000.0, 1)})
        for sensor in self.nvme_hwmon:
            milli = read_int(sensor["path"])
            if milli is not None and 0 < milli < 200000:
                entry = {"name": sensor["name"], "label": sensor["label"],
                         "c": round(milli / 1000.0, 1), "kind": "storage"}
                if sensor["warn"] is not None:
                    entry["warn"] = sensor["warn"]
                if sensor["crit"] is not None:
                    entry["crit"] = sensor["crit"]
                temps.append(entry)
        return temps

    def sample_power(self):
        rails = []
        for label, power_path, volt_path, curr_path in self.power_channels:
            watts = None
            if power_path:
                uw = read_int(power_path)
                watts = uw / 1e6 if uw is not None else None
            else:
                mv = read_int(volt_path)
                ma = read_int(curr_path)
                if mv is not None and ma is not None:
                    watts = mv * ma / 1e6
            if watts is not None:
                rails.append({"name": label, "w": round(watts, 2)})
        if not rails:
            return None
        total_rail = next((r for r in rails if r["name"].upper() in TOTAL_RAIL_NAMES), None)
        total = total_rail["w"] if total_rail else sum(r["w"] for r in rails)
        return {"total_w": round(total, 1), "total_is_input_rail": bool(total_rail), "rails": rails}

    def sample_fan(self):
        if not self.fan:
            return None
        out = {}
        pwm = read_int(self.fan["pwm"]) if "pwm" in self.fan else None
        if pwm is not None:
            out["pct"] = round(100.0 * pwm / 255.0)
        rpm = read_int(self.fan["rpm"]) if "rpm" in self.fan else None
        if rpm is not None:
            out["rpm"] = rpm
        return out or None

    def snapshot(self):
        return {
            "v": SCHEMA,
            "t": round(time.time(), 1),
            "board": self.board,
            "mode": self.power_mode,
            "cpu": self.sample_cpu(),
            "gpu": self.sample_gpu(),
            "mem": self.sample_memory(),
            "disks": self.sample_disks(),
            "temps": self.sample_temps(),
            "power": self.sample_power(),
            "fan": self.sample_fan(),
        }


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between snapshots (default 2)")
    parser.add_argument("--mounts", default="/", help="comma-separated mount points for disk usage")
    parser.add_argument("--root", default="", help="prefix for every /proc and /sys path (tests)")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--redetect", type=float, default=300.0,
                        help="seconds between re-detecting sensors and power mode (default 300)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    interval = max(0.5, min(60.0, args.interval))
    mounts = [m.strip() for m in args.mounts.split(",") if m.strip().startswith("/")]
    collector = Collector(root=args.root, mounts=mounts or ["/"])

    # Prime the /proc/stat counters so the first line already has CPU usage.
    collector.sample_cpu()
    time.sleep(0.05 if args.once else min(0.5, interval))

    out = sys.stdout
    parent = os.getppid()
    last_detect = time.monotonic()
    while True:
        try:
            out.write(json.dumps(collector.snapshot(), separators=(",", ":")) + "\n")
            out.flush()
        except BrokenPipeError:
            # The shell went away (plugin reload, logout): stop quietly and
            # keep Python from complaining while it flushes stdout at exit.
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            return 0
        if args.once:
            return 0
        # An orphaned helper (parent died without closing our pipe) exits too.
        if os.getppid() != parent:
            return 0
        if time.monotonic() - last_detect > args.redetect:
            collector.detect()
            last_detect = time.monotonic()
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
