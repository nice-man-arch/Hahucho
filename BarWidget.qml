import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
    id: root
    moduleName: "tenzin.animechy"

    visible: true
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
    readonly property string setupScript: Qt.resolvedUrl("animechy-setup.sh").toString().replace(/^file:\/\//, "")
    property bool backendReady: false
    property bool installing: false
    property string backendError: ""

    function ensureBackend() {
        if (setupProc.running) return;
        root.installing = true;
        root.backendError = "";
        setupProc.setupOutput = "";
        setupProc.command = ["bash", root.setupScript];
        setupProc.running = true;
        installNotifyTimer.restart();
    }

    function injectPanel() {
        var target = panelLoader.item;
        if (!target) return;
        if ("bar" in target) target.bar = root.bar;
        if ("settings" in target) target.settings = root.settings;
        if ("anchorItem" in target) target.anchorItem = button;
        if ("hostWidget" in target) target.hostWidget = root;
    }

    function togglePanel() {
        if (panelLoader.status === Loader.Error) {
            notify("Hakuchō — Panel Load Error", "Failed to load Panel.qml — check logs", "critical");
            return;
        }
        if (!panelLoader.item) {
            notify("Hakuchō — Panel Not Ready", "Loader status=" + panelLoader.status, "critical");
            return;
        }
        if (!root.backendReady) {
            root.ensureBackend();
            // Still try to open panel even while installing, so user sees UI
        }
        if (panelLoader.item && panelLoader.item.toggle) {
            panelLoader.item.toggle();
        } else if (panelLoader.item && panelLoader.item.openFromHotkey) {
            // fallback
            if (root.opened) panelLoader.item.close();
            else panelLoader.item.openFromHotkey();
        } else {
            notify("Hakuchō — No toggle", "Panel item has no toggle/open", "critical");
        }
    }

    function open() {
        if (panelLoader.item && panelLoader.item.openFromHotkey)
            panelLoader.item.openFromHotkey();
    }
    function close() {
        if (panelLoader.item && panelLoader.item.close)
            panelLoader.item.close();
    }
    function closeForPopoutSwitch() {
        if (panelLoader.item && panelLoader.item.closeForPopoutSwitch)
            panelLoader.item.closeForPopoutSwitch();
    }

    function notify(title, body, urgency) {
        var u = urgency || "normal";
        var t = title || "Hakuchō";
        var b = body || "";
        notifyProc.command = ["notify-send", "-a", "Hakuchō", "-u", u, "-i", "video-display", t, b];
        notifyProc.running = true;
    }

    onBarChanged: injectPanel()
    onSettingsChanged: injectPanel()

    Process {
        id: setupProc
        property string setupOutput: ""
        property string errorOutput: ""
        stdout: SplitParser {
            onRead: function(data) { setupProc.setupOutput += data + "\n" }
        }
        stderr: SplitParser {
            onRead: function(data) { setupProc.errorOutput += data + "\n" }
        }
        onExited: function(exitCode) {
            installNotifyTimer.stop();
            root.installing = false;
            root.backendReady = exitCode === 0;
            if (!root.backendReady) {
                root.backendError = setupProc.errorOutput.trim() || "Backend setup failed";
                notify("Hakuchō — Backend setup failed", root.backendError, "critical");
                return;
            }
            root.backendError = "";
            var out = setupProc.setupOutput;
            var isFresh = out.indexOf("already running") === -1 && (out.indexOf("ready") !== -1);
            if (isFresh) {
                notify("Hakuchō — Ready", "Local anime backend is ready", "normal");
            }
            if (panelLoader.item) {
                if (typeof panelLoader.item.loadAdminStatus === "function") panelLoader.item.loadAdminStatus();
                if (typeof panelLoader.item.loadSettings === "function") panelLoader.item.loadSettings();
                if (typeof panelLoader.item.refreshCurrent === "function") panelLoader.item.refreshCurrent();
            }
        }
    }

    Process {
        id: notifyProc
    }

    Timer {
        id: healthTimer
        interval: 6000
        repeat: true
        running: true
        onTriggered: {
            if (setupProc.running) return;
            var xhr = new XMLHttpRequest();
            xhr.open("GET", "http://127.0.0.1:8765/health");
            xhr.timeout = 2500;
            xhr.onreadystatechange = function() {
                if (xhr.readyState === XMLHttpRequest.DONE) {
                    if (xhr.status === 200) {
                        root.backendReady = true;
                        root.backendError = "";
                    } else {
                        root.backendReady = false;
                        root.ensureBackend();
                    }
                }
            };
            xhr.onerror = function() {
                root.backendReady = false;
                root.ensureBackend();
            };
            xhr.send();
        }
    }

    Timer {
        id: installNotifyTimer
        interval: 600
        repeat: false
        onTriggered: {
            if (root.installing && !root.backendReady) {
                notify("Hakuchō", "Setting up backend…", "normal");
            }
        }
    }

    Loader {
        id: panelLoader
        active: true
        source: Qt.resolvedUrl("Panel.qml")
        visible: false
        onStatusChanged: {
            if (status === Loader.Error) {
                console.warn("Hakuchō Panel failed to load:", source, "error:", panelLoader.sourceComponent ? "" : "component null");
                root.notify("Hakuchō — Loader Error", "Panel.qml failed to load (status Error)", "critical");
            } else if (status === Loader.Ready) {
                console.log("Hakuchō Panel loaded OK");
            }
        }
        onLoaded: {
            console.log("Hakuchō Panel onLoaded");
            root.injectPanel();
            Qt.callLater(root.injectPanel);
        }
    }

    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        // ▶ play triangle — distinct from OmaMovie's  film, universally rendered
        text: "ア"
        slotSize: Style.bar.statusSlot
        tooltipText: root.installing ? "Hakuchō • installing backend …" :
                     (root.backendReady ? "Hakuchō • search & watch anime in mpv (sub/dub)" :
                      (root.backendError || "Hakuchō • backend not ready; click to retry"))
        onPressed: root.togglePanel()
    }

    Component.onCompleted: {
        root.ensureBackend();
    }
}
