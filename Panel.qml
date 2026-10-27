import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Jetson SysMon: GPU, CPU, RAM, disk and temperature in the bar, with a
// click-open detail panel. Data comes from jetson_sysmon.py, a small stdlib
// Python helper that stays running and prints one JSON line per interval, so
// the shell never forks a process per tick.
Panel {
  id: root
  moduleName: "babbletrax.jetson-sysmon"
  ipcTarget: "babbletrax.jetson-sysmon"

  // Latest snapshot from the helper (see README "Data sources").
  property var stats: null
  property bool helperFailed: false
  property string helperError: ""
  property int restartCount: 0
  property bool restartPending: false
  property color warnColor: Qt.tint(Color.foreground, Qt.rgba(1, 0.75, 0.2, 0.75))

  readonly property var segments: Model.barSegments(stats, settings)
  readonly property var th: Model.thresholds(settings)
  readonly property string tempUnit: setting("tempUnit", "C") === "F" ? "F" : "C"
  readonly property bool textLabels: setting("labelStyle", "icons") === "text"
  readonly property var helperArgs: Model.helperArgs(settings)
  readonly property string helperPath: decodeURIComponent(Qt.resolvedUrl("jetson_sysmon.py").toString().replace(/^file:\/\//, ""))
  readonly property var helperCommand: ["python3", "-I", helperPath].concat(helperArgs)
  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function levelColor(lvl, base) {
    if (lvl === "crit") return root.urgentColor
    if (lvl === "warn") return root.warnColor
    return base
  }

  function loadLevel(v) { return Model.level(v, root.th.load.warn, root.th.load.crit) }

  function refresh() {
    // Restart the helper: re-detects sensors and the power mode immediately.
    root.restartCount = 0
    restartHelper()
  }

  function restartHelper() {
    if (helper.running) {
      // onExited picks the restart up once the old helper is gone.
      root.restartPending = true
      helper.running = false
    } else {
      restartTimer.interval = 50
      restartTimer.restart()
    }
  }

  onHelperCommandChanged: if (helper.running) restartHelper()

  // The bar injects settings just after creation; starting a beat later
  // avoids launching the helper once with defaults and again with settings.
  Component.onCompleted: {
    restartTimer.interval = 250
    restartTimer.restart()
  }

  // ---------------------------------------------------------------- data --
  Process {
    id: helper
    command: root.helperCommand
    running: false

    stdout: SplitParser {
      onRead: function(line) {
        var snap = Model.parseLine(line)
        if (!snap) return
        root.stats = snap
        root.helperFailed = false
        root.restartCount = 0
      }
    }

    stderr: SplitParser {
      onRead: function(line) {
        if (line.length === 0) return
        root.helperError = line.slice(0, 300)
        console.warn("[jetson-sysmon] " + root.helperError)
      }
    }

    onExited: function(code, status) {
      if (root.restartPending) {
        root.restartPending = false
        restartTimer.interval = 50
        restartTimer.restart()
        return
      }
      // Back off 2 s, 4 s, ... up to a minute, so a broken python3 never
      // turns into a fork loop.
      root.helperFailed = true
      root.restartCount += 1
      restartTimer.interval = Math.min(60000, 2000 * Math.pow(2, Math.min(5, root.restartCount - 1)))
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: if (!helper.running) helper.running = true
  }

  // Theme warning colour (yellow) from the active theme's colors.toml; the
  // shell's Color singleton only carries foreground/accent/urgent/muted.
  FileView {
    id: themeColors
    path: Color.currentThemePath + "/colors.toml"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var hex = Model.warnColorFromToml(text())
      root.warnColor = hex !== "" ? hex : Qt.tint(Color.foreground, Qt.rgba(1, 0.75, 0.2, 0.75))
    }
  }

  Connections {
    target: Color
    function onForegroundChanged() { themeColors.reload() }
    function onUrgentChanged() { themeColors.reload() }
  }

  // ----------------------------------------------------------------- bar --
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property real openPanelIndicatorWidth: button.vertical ? 0 : barRow.implicitWidth

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    tooltipText: ""
    fixedWidth: vertical ? -1 : barRow.implicitWidth + scaledHorizontalMargin * 2
    fixedHeight: vertical ? barColumn.implicitHeight + scaledVerticalPadding * 2 : -1

    onPressed: function(b) {
      if (b === Qt.RightButton) {
        if (root.bar) root.bar.run("omarchy-launch-or-focus-tui btop")
      } else if (b === Qt.MiddleButton) {
        root.refresh()
      } else {
        root.toggle()
      }
    }

    Row {
      id: barRow
      visible: !button.vertical
      anchors.centerIn: parent
      spacing: Style.space(10)

      Repeater {
        model: root.segments

        Row {
          required property var modelData
          spacing: Style.space(root.textLabels ? 4 : 5)
          anchors.verticalCenter: parent ? parent.verticalCenter : undefined

          Text {
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            text: root.textLabels ? modelData.label : modelData.icon
            color: root.levelColor(modelData.level, button.foreground)
            opacity: root.textLabels ? 0.7 : 1
            font.family: button.fontFamily
            font.pixelSize: root.textLabels ? Style.font.caption : button.fontSize
            font.bold: root.textLabels
            renderType: Text.NativeRendering
          }

          Text {
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            text: modelData.text
            color: root.levelColor(modelData.level, button.foreground)
            font.family: button.fontFamily
            font.pixelSize: button.fontSize
            renderType: Text.NativeRendering
            // Fixed width per metric so the bar doesn't jitter as digits change.
            width: Math.max(implicitWidth, digitMetrics.advanceWidth * (modelData.key === "power" ? 4 : 3) + Style.space(2))
            horizontalAlignment: Text.AlignLeft
          }
        }
      }
    }

    TextMetrics {
      id: digitMetrics
      font.family: button.fontFamily
      font.pixelSize: button.fontSize
      text: "0"
    }

    Column {
      id: barColumn
      visible: button.vertical
      anchors.centerIn: parent
      spacing: Style.space(4)

      Repeater {
        model: root.segments

        Column {
          required property var modelData
          spacing: 0

          Text {
            textFormat: Text.PlainText
            anchors.horizontalCenter: parent.horizontalCenter
            text: modelData.icon
            color: root.levelColor(modelData.level, button.foreground)
            font.family: button.fontFamily
            font.pixelSize: button.fontSize
          }

          Text {
            textFormat: Text.PlainText
            anchors.horizontalCenter: parent.horizontalCenter
            text: modelData.text.replace("%", "")
            color: root.levelColor(modelData.level, button.foreground)
            font.family: button.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  // --------------------------------------------------------------- panel --
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.refresh()

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        // ---------- Hero: board · mode/power/fan · junction temp ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroTemp.implicitHeight)

          readonly property var heroTempValue: Model.pickTemp(root.stats ? root.stats.temps : [], root.setting("tempSensor", "auto"))

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: Model.ICONS.board
            color: root.fg
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroTemp.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: root.stats ? Model.boardShort(root.stats.board) : "Jetson"
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: (root.helperFailed && !root.stats
                ? "Waiting for sensors"
                : (Model.heroStatus(root.stats) || "System monitor")).toUpperCase()
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Text {
            id: heroTemp
            textFormat: Text.PlainText
            text: Model.fmtTemp(parent.heroTempValue ? parent.heroTempValue.c : null, root.tempUnit, false)
            color: root.levelColor(Model.tempLevel(parent.heroTempValue, root.th), root.fg)
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        // ---------- GPU ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(8)

          SectionTitle { text: "GPU" + (root.stats && root.stats.gpu ? "" : " · NOT DETECTED") }

          Meter {
            visible: !!(root.stats && root.stats.gpu)
            label: "Load"
            value: root.stats && root.stats.gpu ? root.stats.gpu.pct : null
            valueText: Model.fmtPct(value)
            detail: root.stats && root.stats.gpu
              ? Model.fmtMhz(root.stats.gpu.mhz) + " of " + Model.fmtMhz(root.stats.gpu.max_mhz)
              : ""
            lvl: root.loadLevel(value)
          }

          InfoNote {
            text: root.stats && root.stats.gpu
              ? "Unified memory: the GPU shares system RAM, so there is no separate VRAM figure."
              : "No Jetson GPU load node was found (see README, Troubleshooting)."
          }
        }

        // ---------- CPU ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(8)

          SectionTitle {
            text: "CPU" + (root.stats && root.stats.cpu
              ? " · " + root.stats.cpu.online + "/" + root.stats.cpu.cores.length + " CORES ONLINE"
              : "")
          }

          Meter {
            label: "Usage"
            value: root.stats && root.stats.cpu ? root.stats.cpu.pct : null
            valueText: Model.fmtPct(value)
            detail: root.stats && root.stats.cpu
              ? Model.fmtMhz(root.stats.cpu.mhz) + " avg"
                + (root.stats.cpu.load ? " · load " + root.stats.cpu.load.map(function(v) { return v.toFixed(2) }).join(" ") : "")
              : ""
            lvl: root.loadLevel(value)
          }

          // Per-core columns.
          Row {
            id: coreRow
            width: parent.width
            spacing: Style.space(4)
            readonly property var cores: root.stats && root.stats.cpu ? root.stats.cpu.cores : []
            readonly property real cellWidth: cores.length > 0 ? (width - spacing * (cores.length - 1)) / cores.length : 0

            Repeater {
              model: coreRow.cores

              Column {
                required property var modelData
                width: coreRow.cellWidth
                spacing: Style.space(3)
                opacity: modelData.online ? 1 : 0.35

                Item {
                  width: parent.width
                  height: Style.space(30)

                  Rectangle {
                    anchors.fill: parent
                    radius: Style.space(2)
                    color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.10)
                  }

                  Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    radius: Style.space(2)
                    height: Math.max(modelData.online ? 2 : 0, parent.height * Model.clamp01((modelData.pct || 0) / 100))
                    color: root.levelColor(root.loadLevel(modelData.pct), root.fg)
                    Behavior on height { NumberAnimation { duration: 250; easing.type: Easing.OutCubic } }
                  }
                }

                Text {
                  textFormat: Text.PlainText
                  anchors.horizontalCenter: parent.horizontalCenter
                  text: String(modelData.id)
                  color: root.fg
                  opacity: 0.6
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }

        // ---------- Memory ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(8)

          SectionTitle { text: "MEMORY" }

          Meter {
            label: "RAM"
            value: root.stats && root.stats.mem ? root.stats.mem.pct : null
            valueText: Model.fmtPct(value)
            detail: root.stats && root.stats.mem
              ? Model.fmtBytes(root.stats.mem.used) + " of " + Model.fmtBytes(root.stats.mem.total)
                + " · " + Model.fmtBytes(root.stats.mem.cached) + " cache"
              : ""
            lvl: root.loadLevel(value)
          }

          Meter {
            readonly property var zram: root.stats && root.stats.mem && root.stats.mem.zram.length > 0 ? root.stats.mem.zram[0] : null
            visible: !!(root.stats && root.stats.mem && root.stats.mem.swap_total > 0)
            label: zram ? "Swap (zram)" : "Swap"
            value: root.stats && root.stats.mem ? root.stats.mem.swap_pct : null
            valueText: Model.fmtPct(value)
            detail: root.stats && root.stats.mem
              ? Model.fmtBytes(root.stats.mem.swap_used) + " of " + Model.fmtBytes(root.stats.mem.swap_total)
                + (zram && Model.zramRatio(zram) !== "" && zram.orig > 1048576 ? " · " + Model.zramRatio(zram) + " compressed" : "")
              : ""
            lvl: root.loadLevel(value)
          }
        }

        // ---------- Storage ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(8)

          SectionTitle { text: "STORAGE" }

          Repeater {
            model: root.stats ? root.stats.disks : []

            Meter {
              required property var modelData
              label: modelData.mount
              value: modelData.pct
              valueText: Model.fmtPct(value)
              detail: modelData.total !== null
                ? Model.fmtBytes(modelData.used) + " of " + Model.fmtBytes(modelData.total)
                : "not mounted"
              lvl: root.loadLevel(value)
            }
          }
        }

        // ---------- Thermals ----------
        PanelSeparator { foreground: root.fg }

        Column {
          width: parent.width
          spacing: Style.space(8)

          SectionTitle { text: "TEMPERATURES" }

          Grid {
            id: tempGrid
            width: parent.width
            columns: 2
            columnSpacing: Style.space(20)
            rowSpacing: Style.spacing.labelGap

            Repeater {
              model: Model.sortedTemps(root.stats ? root.stats.temps : [])

              InfoPair {
                required property var modelData
                width: (tempGrid.width - tempGrid.columnSpacing) / 2
                label: Model.tempLabelFor(modelData)
                value: Model.fmtTemp(modelData.c, root.tempUnit, true)
                valueColor: root.levelColor(Model.tempLevel(modelData, root.th), root.fg)
              }
            }
          }
        }

        // ---------- Power ----------
        PanelSeparator {
          foreground: root.fg
          visible: !!(root.stats && (root.stats.power || root.stats.fan))
        }

        Column {
          width: parent.width
          spacing: Style.space(8)
          visible: !!(root.stats && (root.stats.power || root.stats.fan))

          SectionTitle {
            text: "POWER" + (root.stats && root.stats.power
              ? " · " + Model.fmtWatts(root.stats.power.total_w) + (root.stats.power.total_is_input_rail ? " INPUT" : " MONITORED RAILS")
              : "")
          }

          Grid {
            id: railGrid
            width: parent.width
            columns: 2
            columnSpacing: Style.space(20)
            rowSpacing: Style.spacing.labelGap

            Repeater {
              model: root.stats && root.stats.power ? root.stats.power.rails : []

              InfoPair {
                required property var modelData
                width: (railGrid.width - railGrid.columnSpacing) / 2
                label: Model.railLabel(modelData.name)
                value: Model.fmtWatts(modelData.w)
              }
            }

            InfoPair {
              visible: !!(root.stats && root.stats.fan)
              width: (railGrid.width - railGrid.columnSpacing) / 2
              label: "Fan"
              value: root.stats && root.stats.fan
                ? [root.stats.fan.pct !== undefined ? root.stats.fan.pct + "%" : "",
                   root.stats.fan.rpm !== undefined ? root.stats.fan.rpm + " rpm" : ""].filter(function(s) { return s !== "" }).join(" · ")
                : ""
            }
          }
        }

        InfoNote {
          visible: root.helperFailed && root.helperError !== ""
          text: "Helper error: " + root.helperError
        }
      }
    }
  }

  // ---------------------------------------------------------- components --
  component SectionTitle: PanelSectionHeader {
    foreground: root.fg
    fontFamily: root.fontFamily
  }

  component InfoNote: Text {
    width: parent ? parent.width : 0
    textFormat: Text.PlainText
    wrapMode: Text.WordWrap
    color: root.fg
    opacity: 0.55
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
  }

  component Meter: Column {
    id: meter
    property string label: ""
    property var value: null
    property string valueText: ""
    property string detail: ""
    property string lvl: "none"

    width: parent ? parent.width : 0
    spacing: Style.space(4)

    Item {
      width: parent.width
      implicitHeight: Math.max(meterLabel.implicitHeight, meterValue.implicitHeight)

      Text {
        id: meterLabel
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        text: meter.label
        color: root.fg
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        textFormat: Text.PlainText
        anchors.left: meterLabel.right
        anchors.leftMargin: Style.space(8)
        anchors.right: meterValue.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        text: meter.detail
        color: root.fg
        opacity: 0.55
        elide: Text.ElideRight
        horizontalAlignment: Text.AlignRight
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        id: meterValue
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        text: meter.valueText
        color: root.levelColor(meter.lvl, root.fg)
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }
    }

    Item {
      width: parent.width
      implicitHeight: Style.space(6)

      Rectangle {
        id: track
        anchors.fill: parent
        radius: height / 2
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
      }

      Rectangle {
        anchors.left: track.left
        anchors.verticalCenter: track.verticalCenter
        height: track.height
        radius: track.radius
        visible: typeof meter.value === "number"
        width: Math.max(track.height, track.width * Model.clamp01((meter.value || 0) / 100))
        color: root.levelColor(meter.lvl, root.fg)
        Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: 200 } }
      }
    }
  }

  component InfoPair: Item {
    property string label: ""
    property string value: ""
    property color valueColor: root.fg

    implicitHeight: Math.max(pairLabel.implicitHeight, pairValue.implicitHeight)

    Text {
      id: pairLabel
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.right: pairValue.left
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      text: parent.label
      color: root.fg
      opacity: 0.6
      elide: Text.ElideRight
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      id: pairValue
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: parent.value
      color: parent.valueColor
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }
}
