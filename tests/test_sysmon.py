"""Tests for jetson_sysmon.py against fake /proc and /sys trees.

Fixtures are JSON (files + relative symlinks) rather than checked-in trees
because Omarchy refuses symlinks inside a plugin folder, and sysfs is mostly
symlinks. Each test materialises a fixture into a temporary root.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(os.path.dirname(HERE), "jetson_sysmon.py")

spec = importlib.util.spec_from_file_location("jetson_sysmon", HELPER)
sysmon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sysmon)


def build(name):
    with open(os.path.join(HERE, "fixtures", name + ".json"), encoding="utf-8") as handle:
        spec = json.load(handle)
    tmp = tempfile.TemporaryDirectory()
    root = tmp.name
    for path, content in spec["files"].items():
        full = root + path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as out:
            out.write(content)
    for path, target in spec["links"].items():
        full = root + path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        os.symlink(target, full)
    return tmp


def bump_stat(root, busy_add, idle_add, cpus):
    """Rewrite /proc/stat with every CPU advanced by busy/idle jiffies."""
    path = root + "/proc/stat"
    lines = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if parts and parts[0].startswith("cpu"):
                n = cpus if parts[0] == "cpu" else 1
                vals = [int(v) for v in parts[1:]]
                vals[0] += busy_add * n
                vals[3] += idle_add * n
                line = " ".join([parts[0]] + [str(v) for v in vals]) + "\n"
            lines.append(line)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)


class OrinAgx(unittest.TestCase):
    def setUp(self):
        self.tmp = build("orin-agx")
        self.root = self.tmp.name
        self.c = sysmon.Collector(root=self.root, mounts=["/"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_board_and_mode(self):
        snap = self.c.snapshot()
        self.assertEqual(snap["board"], "NVIDIA Jetson AGX Orin Developer Kit")
        self.assertEqual(snap["mode"], "MODE_30W")

    def test_gpu_from_devfreq(self):
        gpu = self.c.snapshot()["gpu"]
        self.assertEqual(gpu["name"], "17000000.gpu")
        self.assertEqual(gpu["pct"], 55.3)
        self.assertEqual(gpu["mhz"], 918)
        self.assertEqual(gpu["max_mhz"], 1300)
        self.assertEqual(gpu["min_mhz"], 306)
        self.assertEqual(gpu["memory"], "unified")

    def test_cpu_usage_and_offline_cores(self):
        self.c.sample_cpu()
        bump_stat(self.root, 25, 75, 8)
        cpu = self.c.sample_cpu()
        self.assertEqual(cpu["pct"], 25.0)
        self.assertEqual(len(cpu["cores"]), 12)
        self.assertEqual(cpu["online"], 8)
        self.assertEqual(cpu["cores"][0]["pct"], 25.0)
        self.assertEqual(cpu["cores"][0]["mhz"], 2202)
        self.assertEqual(cpu["cores"][1]["mhz"], 1114)
        self.assertFalse(cpu["cores"][11]["online"])
        self.assertIsNone(cpu["cores"][11]["pct"])
        self.assertEqual(cpu["max_mhz"], 2202)
        self.assertEqual(cpu["load"], [0.4, 0.9, 0.64])

    def test_memory_and_zram(self):
        mem = self.c.snapshot()["mem"]
        self.assertEqual(mem["total"], 64386936 * 1024)
        self.assertEqual(mem["used"], (64386936 - 53092152) * 1024)
        self.assertAlmostEqual(mem["pct"], 17.5, places=1)
        self.assertEqual(mem["swap_used"], (16096508 - 15048000) * 1024)
        self.assertEqual(mem["zram"][0]["name"], "zram0")
        self.assertEqual(mem["zram"][0]["orig"], 1073741824)
        self.assertEqual(mem["zram"][0]["compr"], 268435456)

    def test_temps_skip_enodata_and_name(self):
        temps = {t["name"]: t for t in self.c.snapshot()["temps"] if t.get("label", "Composite") == "Composite"}
        self.assertNotIn("cv0", temps)
        self.assertEqual(temps["tj"]["c"], 52.9)
        self.assertEqual(temps["cpu"]["c"], 49.0)
        self.assertEqual(temps["nvme0"]["kind"], "storage")
        self.assertEqual(temps["nvme0"]["c"], 50.9)

    def test_nvme_all_sensors_with_drive_thresholds(self):
        nvme = [(t["name"], t["label"], t["c"], t.get("warn"), t.get("crit"))
                for t in self.c.snapshot()["temps"] if t.get("kind") == "storage"]
        self.assertEqual(nvme, [
            ("nvme0", "Composite", 50.9, 83.9, 88.9),
            # No thresholds of its own: inherits the drive's Composite ones.
            ("nvme0", "Sensor 2", 70.8, 83.9, 88.9),
            # Unlabelled sensors get default labels; 65261.85 C means unset.
            ("nvme1", "Composite", 40.0, None, None),
            ("nvme1", "Sensor 2", 45.0, None, None),
        ])

    def test_gated_zone_stays_listed_without_value(self):
        self.c.snapshot()
        os.remove(self.root + "/sys/class/thermal/thermal_zone1/temp")
        temps = {t["name"]: t for t in self.c.snapshot()["temps"]}
        self.assertIn("gpu", temps)
        self.assertIsNone(temps["gpu"]["c"])
        self.assertNotIn("cv0", temps)

    def test_power_rails_sum(self):
        power = self.c.snapshot()["power"]
        names = [r["name"] for r in power["rails"]]
        self.assertEqual(names, ["VDD_GPU_SOC", "VDD_CPU_CV", "VIN_SYS_5V0"])
        self.assertFalse(power["total_is_input_rail"])
        self.assertAlmostEqual(power["total_w"], round(6.42 + 4.81 + 5.87, 1), places=1)

    def test_fan(self):
        self.assertEqual(self.c.snapshot()["fan"], {"pct": 26, "rpm": 1152})

    def test_disk_statvfs(self):
        disks = self.c.snapshot()["disks"]
        self.assertEqual(disks[0]["mount"], "/")
        self.assertGreater(disks[0]["total"], 0)
        self.assertTrue(0 <= disks[0]["pct"] <= 100)

    def test_missing_mount(self):
        c = sysmon.Collector(root=self.root, mounts=["/does-not-exist"])
        self.assertIsNone(c.snapshot()["disks"][0]["pct"])

    def test_snapshot_is_json(self):
        json.dumps(self.c.snapshot())


class XavierNx(unittest.TestCase):
    def setUp(self):
        self.tmp = build("xavier-nx")
        self.c = sysmon.Collector(root=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gpu_load_fallback_path(self):
        gpu = self.c.snapshot()["gpu"]
        self.assertEqual(gpu["pct"], 100.0)
        self.assertEqual(gpu["mhz"], 1109)

    def test_input_rail_is_total(self):
        power = self.c.snapshot()["power"]
        self.assertTrue(power["total_is_input_rail"])
        self.assertEqual(power["total_w"], 8.0)

    def test_sentinel_temps_dropped(self):
        names = [t["name"] for t in self.c.snapshot()["temps"]]
        self.assertIn("CPU-therm", names)
        self.assertNotIn("thermal-fan-est", names)
        self.assertNotIn("PMIC-Die", names)

    def test_no_mode_no_fan(self):
        snap = self.c.snapshot()
        self.assertIsNone(snap["mode"])
        self.assertIsNone(snap["fan"])


class GenericPc(unittest.TestCase):
    def setUp(self):
        self.tmp = build("generic-pc")
        self.c = sysmon.Collector(root=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_degrades_gracefully(self):
        snap = self.c.snapshot()
        self.assertIsNone(snap["board"])
        self.assertIsNone(snap["gpu"])
        self.assertIsNone(snap["power"])
        self.assertIsNone(snap["fan"])
        self.assertEqual(snap["mem"]["swap_pct"], None)
        self.assertEqual(snap["mem"]["zram"], [])
        self.assertEqual(len(snap["cpu"]["cores"]), 4)
        self.assertEqual([t["name"] for t in snap["temps"]], ["x86_pkg_temp", "acpitz"])

    def test_empty_root(self):
        with tempfile.TemporaryDirectory() as empty:
            snap = sysmon.Collector(root=empty).snapshot()
            self.assertEqual(snap["cpu"]["cores"], [])
            self.assertIsNone(snap["cpu"]["pct"])
            self.assertIsNone(snap["mem"]["pct"])


class Parsers(unittest.TestCase):
    def test_cpu_list(self):
        self.assertEqual(sysmon.parse_cpu_list("0-3,6,8-9\n"), [0, 1, 2, 3, 6, 8, 9])
        self.assertEqual(sysmon.parse_cpu_list(""), [])
        self.assertEqual(sysmon.parse_cpu_list("x,2"), [2])

    def test_usage_pct(self):
        self.assertIsNone(sysmon.usage_pct(None, (1, 2)))
        self.assertIsNone(sysmon.usage_pct((5, 10), (5, 10)))
        self.assertEqual(sysmon.usage_pct((0, 0), (50, 100)), 50.0)


class Cli(unittest.TestCase):
    def test_once_prints_one_json_line(self):
        tmp = build("orin-agx")
        try:
            out = subprocess.run(
                [sys.executable, "-I", HELPER, "--once", "--root", tmp.name, "--mounts", "/,relative,/"],
                check=True, capture_output=True, text=True, timeout=20,
            )
        finally:
            tmp.cleanup()
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        snap = json.loads(lines[0])
        self.assertEqual(snap["v"], 1)
        self.assertEqual([d["mount"] for d in snap["disks"]], ["/", "/"])
        self.assertEqual(out.stderr, "")

    def test_exits_quietly_when_reader_goes_away(self):
        tmp = build("generic-pc")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", HELPER, "--interval", "0.5", "--root", tmp.name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            proc.stdout.readline()
            proc.stdout.close()
            rc = proc.wait(timeout=10)
            err = proc.stderr.read().decode()
            proc.stderr.close()
        finally:
            tmp.cleanup()
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
