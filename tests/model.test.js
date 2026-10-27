// Tests for Model.js under plain Node (no QML runtime needed).
"use strict"
const fs = require("fs")
const path = require("path")
const vm = require("vm")
const assert = require("assert/strict")

const src = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8")
const M = {}
vm.createContext(M)
vm.runInContext(src, M, { filename: "Model.js" })

let failed = 0
function test(name, fn) {
  try { fn(); console.log("ok - " + name) } catch (e) { failed++; console.log("not ok - " + name + "\n  " + e.message) }
}

const snap = {
  v: 1,
  board: "NVIDIA Jetson AGX Orin Developer Kit",
  mode: "MAXN",
  cpu: { pct: 12.4, cores: [], online: 12 },
  gpu: { pct: 80.2, mhz: 1300 },
  mem: { pct: 95.5, used: 1, total: 2 },
  disks: [{ mount: "/", pct: 35.4 }],
  temps: [{ name: "cpu", c: 49.2 }, { name: "tj", c: 72.0 }, { name: "gpu", c: 75.1 }, { name: "nvme0", c: 90, kind: "storage" }],
  power: { total_w: 14.14, rails: [] },
  fan: { pct: 26, rpm: 1152 }
}

test("level thresholds", () => {
  assert.equal(M.level(null, 75, 90), "none")
  assert.equal(M.level(10, 75, 90), "normal")
  assert.equal(M.level(75, 75, 90), "warn")
  assert.equal(M.level(95, 75, 90), "crit")
})

test("formatters", () => {
  assert.equal(M.fmtPct(12.6), "13%")
  assert.equal(M.fmtPct(undefined), "—")
  assert.equal(M.fmtTemp(49.6, "C", false), "50°")
  assert.equal(M.fmtTemp(100, "F", true), "212°F")
  assert.equal(M.fmtBytes(65932222464), "61.4 GiB")
  assert.equal(M.fmtBytes(512), "512 B")
  assert.equal(M.fmtMhz(1300), "1.30 GHz")
  assert.equal(M.fmtMhz(306), "306 MHz")
  assert.equal(M.fmtWatts(14.14), "14.1 W")
  assert.equal(M.fmtWatts(5.27), "5.27 W")
  assert.equal(M.fmtWattsShort(5.27), "5.3W")
})

test("boardShort", () => {
  assert.equal(M.boardShort("NVIDIA Jetson AGX Orin Developer Kit"), "Jetson AGX Orin")
  assert.equal(M.boardShort("NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super"), "Jetson Orin Nano")
  assert.equal(M.boardShort(null), "Jetson")
})

test("pickTemp prefers tj, ignores storage unless named", () => {
  assert.equal(M.pickTemp(snap.temps, "auto").name, "tj")
  assert.equal(M.pickTemp(snap.temps, "max").name, "gpu")
  assert.equal(M.pickTemp(snap.temps, "nvme0").name, "nvme0")
  assert.equal(M.pickTemp([{ name: "x86_pkg_temp", c: 60 }, { name: "acpitz", c: 30 }], "auto").name, "x86_pkg_temp")
  assert.equal(M.pickTemp([], "auto"), null)
  assert.equal(M.pickTemp(snap.temps, "missing").name, "gpu")
})

test("metricsFrom keeps canonical order and falls back", () => {
  assert.deepEqual(Array.from(M.metricsFrom(["temp", "gpu"])), ["gpu", "temp"])
  assert.deepEqual(Array.from(M.metricsFrom("cpu, power")), ["cpu", "power"])
  assert.deepEqual(Array.from(M.metricsFrom([])), ["gpu", "cpu", "ram", "temp"])
  assert.deepEqual(Array.from(M.metricsFrom(["bogus"])), ["gpu", "cpu", "ram", "temp"])
})

test("mountsFrom rejects relative and traversal paths", () => {
  assert.deepEqual(Array.from(M.mountsFrom("/, /home ,data,/../etc,/home")), ["/", "/home"])
  assert.deepEqual(Array.from(M.mountsFrom("")), ["/"])
})

test("thresholds keep crit >= warn", () => {
  const t = M.thresholds({ tempWarn: 90, tempCrit: 80, loadWarn: "50" })
  assert.equal(t.temp.warn, 90)
  assert.equal(t.temp.crit, 90)
  assert.equal(t.load.warn, 50)
  assert.equal(t.load.crit, 90)
})

test("barSegments default set and levels", () => {
  const segs = M.barSegments(snap, {})
  assert.deepEqual(Array.from(segs).map(s => s.key), ["gpu", "cpu", "ram", "temp"])
  assert.deepEqual(Array.from(segs).map(s => s.text), ["80%", "12%", "96%", "72°"])
  assert.deepEqual(Array.from(segs).map(s => s.level), ["warn", "normal", "crit", "warn"])
})

test("barSegments hides GPU/power when absent and shows dashes when no data", () => {
  const segs = M.barSegments({ cpu: { pct: 1 }, temps: [] }, { metrics: ["gpu", "cpu", "temp", "power", "disk"] })
  assert.deepEqual(Array.from(segs).map(s => s.key), ["cpu", "disk", "temp"])
  assert.equal(segs[1].text, "—")
  assert.equal(M.barSegments(null, {}).length, 3)
})

test("barSegments power and fahrenheit", () => {
  const segs = M.barSegments(snap, { metrics: ["power", "temp"], tempUnit: "F" })
  assert.equal(segs[0].text, "162°")
  assert.equal(segs[1].text, "14W")
})

test("heroStatus", () => {
  assert.equal(M.heroStatus(snap), "MAXN · 14.1 W · fan 26%")
  assert.equal(M.heroStatus({}), "")
})

test("labels", () => {
  assert.equal(M.tempLabel("tj"), "Junction (Tj)")
  assert.equal(M.tempLabel("soc1"), "SoC 1")
  assert.equal(M.tempLabel("nvme0"), "NVMe 0")
  assert.equal(M.railLabel("VDD_GPU_SOC"), "GPU + SoC")
  assert.equal(M.railLabel("SOMETHING"), "SOMETHING")
  assert.equal(M.zramRatio({ orig: 1000, compr: 250 }), "4.0×")
  assert.equal(M.zramRatio({ orig: 0, compr: 0 }), "")
})

test("sortedTemps puts storage last and keeps gated zones", () => {
  const names = Array.from(M.sortedTemps(snap.temps.concat([{ name: "soc0", c: null }]))).map(t => t.name)
  assert.deepEqual(names, ["cpu", "gpu", "soc0", "tj", "nvme0"])
  assert.equal(M.fmtTemp(null, "C", true), "\u2014")
})

test("parseLine", () => {
  assert.equal(M.parseLine('{"v":1,"cpu":{}}').v, 1)
  assert.equal(M.parseLine('{"v":2}'), null)
  assert.equal(M.parseLine("Traceback"), null)
  assert.equal(M.parseLine("{broken"), null)
})

test("helperArgs clamps interval", () => {
  assert.deepEqual(Array.from(M.helperArgs({ intervalSec: 0, mounts: "/,/home" })), ["--interval", "1", "--mounts", "/,/home"])
  assert.deepEqual(Array.from(M.helperArgs({})), ["--interval", "2", "--mounts", "/"])
  assert.deepEqual(Array.from(M.helperArgs({ intervalSec: 999 })), ["--interval", "30", "--mounts", "/"])
})

test("warnColorFromToml", () => {
  assert.equal(M.warnColorFromToml('red = "#f7768e"\nyellow = "#e0af68"\n'), "#e0af68")
  assert.equal(M.warnColorFromToml('color3 = "#aabbcc"\n'), "#aabbcc")
  assert.equal(M.warnColorFromToml(""), "")
})

test("icons are single code points", () => {
  for (const k of Object.keys(M.ICONS)) assert.equal([...M.ICONS[k]].length, 1, k)
})

if (failed) { console.log(failed + " failed"); process.exit(1) }
console.log("all model tests passed")
