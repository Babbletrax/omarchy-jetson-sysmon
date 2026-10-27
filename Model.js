// Pure helpers for the Jetson SysMon widget: formatting, thresholds, and the
// choice of what the bar shows. No QML types here, so tests/model.test.js can
// run this file under plain Node.

var DASH = "—"

var ICONS = {
  gpu: "\u{F08AE}",   // nf-md-expansion_card
  cpu: "\u{F4BC}",    // nf-oct-cpu
  ram: "\u{EFC5}",    // nf-fa-memory
  disk: "\u{F02CA}",  // nf-md-harddisk
  temp: "\u{F2C9}",   // nf-fa-thermometer_half
  power: "\u{F140B}", // nf-md-lightning_bolt
  board: "\u{F0697}"  // nf-md-developer_board
}

var TEXT_LABELS = { gpu: "GPU", cpu: "CPU", ram: "RAM", disk: "DSK", temp: "TMP", power: "PWR" }

var ALL_METRICS = ["gpu", "cpu", "ram", "disk", "temp", "power"]
var DEFAULT_METRICS = ["gpu", "cpu", "ram", "temp"]

function isNum(v) {
  return typeof v === "number" && isFinite(v)
}

function toNum(v, fallback) {
  var n = Number(v)
  return isFinite(n) && v !== null && v !== "" && v !== undefined ? n : fallback
}

// "normal" | "warn" | "crit" | "none" (no value).
function level(value, warn, crit) {
  if (!isNum(value)) return "none"
  if (isNum(crit) && value >= crit) return "crit"
  if (isNum(warn) && value >= warn) return "warn"
  return "normal"
}

function fmtPct(v) {
  return isNum(v) ? Math.round(v) + "%" : DASH
}

function cToUnit(c, unit) {
  return unit === "F" ? c * 9 / 5 + 32 : c
}

function fmtTemp(c, unit, withUnit) {
  if (!isNum(c)) return DASH
  return Math.round(cToUnit(c, unit)) + "°" + (withUnit ? (unit === "F" ? "F" : "C") : "")
}

function fmtBytes(b) {
  if (!isNum(b)) return DASH
  var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
  var i = 0
  var v = b
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  var digits = v >= 100 || i === 0 ? 0 : 1
  return v.toFixed(digits) + " " + units[i]
}

function fmtMhz(mhz) {
  if (!isNum(mhz)) return DASH
  if (mhz >= 1000) return (mhz / 1000).toFixed(2) + " GHz"
  return Math.round(mhz) + " MHz"
}

function fmtWatts(w) {
  if (!isNum(w)) return DASH
  return (w >= 10 ? w.toFixed(1) : w.toFixed(2)) + " W"
}

// Bar-friendly watts: "14W", "6.2W".
function fmtWattsShort(w) {
  if (!isNum(w)) return DASH
  return (w >= 10 ? Math.round(w) : w.toFixed(1)) + "W"
}

// "NVIDIA Jetson AGX Orin Developer Kit" -> "Jetson AGX Orin".
function boardShort(board) {
  var s = String(board || "").replace(/\s+/g, " ").trim()
  if (!s) return "Jetson"
  s = s.replace(/^NVIDIA\s+/i, "").replace(/\s+(Developer Kit|Dev Kit|Engineering Reference.*)$/i, "")
  return s || "Jetson"
}

// Pick the temperature the bar shows. sensor: "auto" (tj, else hottest),
// "max" (hottest), or a zone name such as "cpu", "gpu", "soc0", "tj".
// Storage sensors only count when named explicitly.
function pickTemp(temps, sensor) {
  var list = temps || []
  var want = String(sensor || "auto").toLowerCase()
  var hottest = null
  var tj = null
  for (var i = 0; i < list.length; i++) {
    var t = list[i]
    if (!t || !isNum(t.c)) continue
    var name = String(t.name || "").toLowerCase()
    if (want !== "auto" && want !== "max" && name === want) return t
    if (t.kind === "storage") continue
    if (name === "tj") tj = t
    if (!hottest || t.c > hottest.c) hottest = t
  }
  if (want === "auto" && tj) return tj
  return hottest
}

// The metric list from settings: a non-empty array of known keys, else the
// defaults. Order follows ALL_METRICS so the bar reads the same everywhere.
function metricsFrom(setting) {
  var chosen = []
  if (setting && typeof setting.length === "number" && typeof setting !== "string") {
    for (var i = 0; i < setting.length; i++) chosen.push(String(setting[i]).toLowerCase())
  } else if (typeof setting === "string" && setting.trim() !== "") {
    chosen = setting.toLowerCase().split(/[\s,]+/)
  }
  var out = []
  for (var j = 0; j < ALL_METRICS.length; j++) {
    if (chosen.indexOf(ALL_METRICS[j]) !== -1) out.push(ALL_METRICS[j])
  }
  return out.length > 0 ? out : DEFAULT_METRICS.slice()
}

function mountsFrom(setting) {
  var raw = setting && typeof setting.length === "number" && typeof setting !== "string"
    ? Array.prototype.slice.call(setting) : String(setting || "/").split(/[\s,]+/)
  var out = []
  for (var i = 0; i < raw.length; i++) {
    var m = String(raw[i]).trim()
    if (m.charAt(0) === "/" && m.indexOf("..") === -1 && out.indexOf(m) === -1) out.push(m)
  }
  return out.length > 0 ? out : ["/"]
}

// Thresholds with sane defaults and ordering (warn <= crit).
function thresholds(settings) {
  var s = settings || {}
  function pair(wKey, cKey, wDef, cDef) {
    var w = toNum(s[wKey], wDef)
    var c = toNum(s[cKey], cDef)
    if (c < w) c = w
    return { warn: w, crit: c }
  }
  return {
    load: pair("loadWarn", "loadCrit", 75, 90),
    temp: pair("tempWarn", "tempCrit", 70, 85)
  }
}

// One entry per bar metric: { key, icon, label, text, level }.
function barSegments(data, settings) {
  var s = settings || {}
  var th = thresholds(s)
  var unit = s.tempUnit === "F" ? "F" : "C"
  var metrics = metricsFrom(s.metrics)
  var d = data || {}
  var out = []
  for (var i = 0; i < metrics.length; i++) {
    var key = metrics[i]
    var value = null
    var text = DASH
    var lvl = "none"
    if (key === "gpu") {
      if (!d.gpu) continue
      value = d.gpu.pct
      text = fmtPct(value)
      lvl = level(value, th.load.warn, th.load.crit)
    } else if (key === "cpu") {
      value = d.cpu ? d.cpu.pct : null
      text = fmtPct(value)
      lvl = level(value, th.load.warn, th.load.crit)
    } else if (key === "ram") {
      value = d.mem ? d.mem.pct : null
      text = fmtPct(value)
      lvl = level(value, th.load.warn, th.load.crit)
    } else if (key === "disk") {
      var disk = d.disks && d.disks.length > 0 ? d.disks[0] : null
      value = disk ? disk.pct : null
      text = fmtPct(value)
      lvl = level(value, th.load.warn, th.load.crit)
    } else if (key === "temp") {
      var t = pickTemp(d.temps, s.tempSensor)
      value = t ? t.c : null
      text = fmtTemp(value, unit, false)
      lvl = tempLevel(t, th)
    } else if (key === "power") {
      if (!d.power) continue
      value = d.power.total_w
      text = fmtWattsShort(value)
      lvl = isNum(value) ? "normal" : "none"
    }
    out.push({ key: key, icon: ICONS[key], label: TEXT_LABELS[key], text: text, level: lvl })
  }
  return out
}

// Level for one temperature entry. A sensor that carries its own limits
// (NVMe temp*_max / temp*_crit) is judged by them; the rest use the settings.
function tempLevel(t, th) {
  if (!t) return "none"
  var limits = th && th.temp ? th.temp : { warn: 70, crit: 85 }
  var warn = isNum(t.warn) ? t.warn : limits.warn
  var crit = isNum(t.crit) ? t.crit : limits.crit
  if (isNum(t.warn) && !isNum(t.crit)) crit = Math.max(warn, limits.crit)
  if (isNum(t.crit) && !isNum(t.warn)) warn = Math.min(crit, limits.warn)
  return level(t.c, warn, crit)
}

// Thermal zones for the panel: storage last, then by name; a drive's
// Composite sensor before its other sensors.
function sortedTemps(temps) {
  var list = (temps || []).filter(function(t) { return t && t.name })
  return list.sort(function(a, b) {
    var sa = a.kind === "storage" ? 1 : 0
    var sb = b.kind === "storage" ? 1 : 0
    if (sa !== sb) return sa - sb
    var byName = String(a.name).localeCompare(String(b.name), undefined, { numeric: true })
    if (byName !== 0) return byName
    var ca = a.label === "Composite" ? 0 : 1
    var cb = b.label === "Composite" ? 0 : 1
    if (ca !== cb) return ca - cb
    return String(a.label || "").localeCompare(String(b.label || ""), undefined, { numeric: true })
  })
}

// "NVMe 0" for a drive's Composite temperature, "NVMe 0 Sensor 2" for its
// other sensors, the zone label otherwise.
function tempLabelFor(t) {
  if (!t) return ""
  var base = tempLabel(t.name)
  if (t.label && t.label !== "Composite") return base + " " + t.label
  return base
}

function tempLabel(name) {
  var n = String(name || "")
  var map = { tj: "Junction (Tj)", cpu: "CPU", gpu: "GPU", cv0: "CV0", cv1: "CV1", cv2: "CV2" }
  if (map[n]) return map[n]
  if (/^soc\d*$/.test(n)) return "SoC " + n.slice(3)
  if (/^nvme\d*/.test(n)) return "NVMe" + (n.length > 4 ? " " + n.slice(4) : "")
  return n.charAt(0).toUpperCase() + n.slice(1)
}

// Readable rail names: "VDD_GPU_SOC" -> "GPU + SoC".
function railLabel(name) {
  var map = {
    VDD_GPU_SOC: "GPU + SoC",
    VDD_CPU_CV: "CPU + CV",
    VIN_SYS_5V0: "System 5V",
    VDDQ_VDD2_1V8AO: "Memory (DDR)",
    VDD_IN: "Module input",
    VDD_CPU_GPU_CV: "CPU + GPU + CV",
    VDD_SOC: "SoC",
    POM_5V_IN: "Module input",
    POM_5V_GPU: "GPU",
    POM_5V_CPU: "CPU"
  }
  return map[name] || String(name || "")
}

function zramRatio(z) {
  if (!z || !isNum(z.orig) || !isNum(z.compr) || z.compr <= 0 || z.orig <= 0) return ""
  return (z.orig / z.compr).toFixed(1) + "×"
}

// Short status line for the panel hero: "MAXN · 14.1 W · FAN 26%".
function heroStatus(data) {
  var d = data || {}
  var parts = []
  if (d.mode) parts.push(String(d.mode))
  if (d.power && isNum(d.power.total_w)) parts.push(fmtWatts(d.power.total_w))
  if (d.fan && isNum(d.fan.pct)) parts.push("fan " + d.fan.pct + "%")
  else if (d.fan && isNum(d.fan.rpm)) parts.push("fan " + d.fan.rpm + " rpm")
  return parts.join(" · ")
}

function clamp01(v) {
  return isNum(v) ? Math.max(0, Math.min(1, v)) : 0
}

// Parse one helper line; returns null for anything that is not a v1 snapshot.
function parseLine(line) {
  var text = String(line || "").trim()
  if (text.charAt(0) !== "{") return null
  try {
    var obj = JSON.parse(text)
    return obj && obj.v === 1 ? obj : null
  } catch (e) {
    return null
  }
}

// Helper argv for the given settings (interval clamped to 1..30 s).
function helperArgs(settings) {
  var s = settings || {}
  var interval = Math.max(1, Math.min(30, Math.round(toNum(s.intervalSec, 2))))
  return ["--interval", String(interval), "--mounts", mountsFrom(s.mounts).join(",")]
}

// Theme warning colour: colors.toml `yellow`, else `color3`, else "".
function warnColorFromToml(raw) {
  var lines = String(raw || "").split("\n")
  var color3 = ""
  for (var i = 0; i < lines.length; i++) {
    var m = lines[i].match(/^\s*([A-Za-z0-9_-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/)
    if (!m) continue
    if (m[1] === "yellow") return m[2]
    if (m[1] === "color3") color3 = m[2]
  }
  return color3
}
