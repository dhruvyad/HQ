// The HQ floating panel. This is the window shell only: every pixel and every
// rule comes from the page it loads (hq/static/float.js), so this file moves a
// window and reports the screen. Using the system WebKit keeps it around a
// megabyte and keeps HQ's runtime dependencies empty.
//
// Usage: hq-float <url> <state-file> [--bench <url>]
// stdout carries only the bench's picks line; logs go to stderr.

import AppKit
import Carbon.HIToolbox
import WebKit

func log(_ message: String) {
    FileHandle.standardError.write(Data("hq-float: \(message)\n".utf8))
}

/// A borderless window refuses key status by default. The pill must stay that
/// way so it never takes the keyboard, and an open panel must accept it so the
/// reply box is typeable; `acceptsKey` is the page's call between the two.
final class FloatingPanel: NSPanel {
    var acceptsKey = false
    override var canBecomeKey: Bool { acceptsKey }
    override var canBecomeMain: Bool { false }
    // NSPanel answers Escape by re-sending it, and WebKit hands it straight back,
    // so one press becomes dozens of keydowns. The page decides what Escape does.
    override func cancelOperation(_ sender: Any?) {}
    // The drag overlay covers the whole display, menu bar included.
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}

final class PanelWebView: WKWebView {
    // WebKit only accepts first mouse on scrollbars and selections, which would
    // eat the first click on a panel that is not key.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    // Reloading would throw away unsent replies, which live in the page.
    override func willOpenMenu(_ menu: NSMenu, with event: NSEvent) {
        menu.items.removeAll { $0.identifier?.rawValue == "WKMenuItemIdentifierReload" }
        super.willOpenMenu(menu, with: event)
    }
}

/// Whether a display is showing a fullscreen app's Space. macOS keeps reporting the Dock's
/// space as reserved there, and no public API says otherwise, so ask for the Space's type.
/// This read has been stable for years, and if it ever goes away the pill just stays
/// above where the Dock would be.
enum Spaces {
    private typealias Connection = @convention(c) () -> Int32
    private typealias Displays = @convention(c) (Int32) -> Unmanaged<CFArray>?
    private static let graphics = dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", RTLD_LAZY)
    private static let connection = dlsym(graphics, "CGSMainConnectionID").map { unsafeBitCast($0, to: Connection.self) }
    private static let displays = dlsym(graphics, "CGSCopyManagedDisplaySpaces").map { unsafeBitCast($0, to: Displays.self) }
    private static let fullscreen = 4

    static func isFullscreen(on id: CGDirectDisplayID) -> Bool {
        guard let connection, let displays,
              let list = displays(connection())?.takeRetainedValue() as? [[String: Any]]
        else { return false }
        // With one set of Spaces across displays, there is a single entry named Main.
        let uuid = CGDisplayCreateUUIDFromDisplayID(id).map { CFUUIDCreateString(nil, $0.takeRetainedValue()) as String }
        let entry = list.first { ($0["Display Identifier"] as? String) == uuid } ?? list.first
        return ((entry?["Current Space"] as? [String: Any])?["type"] as? Int) == fullscreen
    }
}

struct DisplayInfo: Equatable {
    let id: CGDirectDisplayID
    let x, y, width, height: CGFloat
    let visibleX, visibleY, visibleWidth, visibleHeight: CGFloat
}

final class Shell: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler,
                   WKNavigationDelegate, WKUIDelegate {
    private let url: URL
    private let stateURL: URL
    private let benchURL: URL?

    private var panel: FloatingPanel!
    private var web: PanelWebView!
    private var benchPanel: FloatingPanel?
    private var benchWeb: PanelWebView?

    private var dock: String?
    private var snoozedUntil: Double?
    private var settings: [String: Any]?
    private var placed = false
    private var dragging = false
    private var current: NSScreen?
    private var lastDisplay: DisplayInfo?
    private var feel: [String: Any] = [:]
    private var finished = Set<ObjectIdentifier>()

    private var ticker: Timer?
    private var watchdog: DispatchWorkItem?
    private var reveal: DispatchWorkItem?
    private var burstStart: TimeInterval = 0
    private var burstLast: TimeInterval = -.infinity

    private var activity: NSObjectProtocol?
    private var signals: [DispatchSourceSignal] = []

    // The window is larger than what it shows, so the page says which part catches the mouse.
    private var hitRect: NSRect?
    private var hitTimer: Timer?
    private var pointerInside = false

    private var hotKeyRefs: [EventHotKeyRef] = []
    private var hotKeyActions: [UInt32: String] = [:]
    private var hotKeyHandler: EventHandlerRef?
    private var keyMonitor: Any?

    init(url: URL, stateURL: URL, benchURL: URL?) {
        self.url = url
        self.stateURL = stateURL
        self.benchURL = benchURL
    }

    func applicationDidFinishLaunching(_: Notification) {
        installEditMenu()
        readState()
        installSignals()
        installHotKeyHandler()
        // The panel is always on screen, so a nap would freeze its count.
        activity = ProcessInfo.processInfo.beginActivity(
            options: .userInitiatedAllowingIdleSystemSleep,
            reason: "HQ floating panel stays current"
        )

        web = makeWebView()
        panel = FloatingPanel(
            contentRect: NSRect(x: 0, y: 0, width: 120, height: 36),
            // .nonactivatingPanel is the important one: clicking the pill must
            // not pull the user out of the app they are working in.
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.contentView = web
        panel.isOpaque = false
        panel.backgroundColor = .clear
        // Screen-saver level, as OpenFlow's status bar uses and Wispr Flow's does on a
        // live Mac: status-bar level loses to anything an app raises above it.
        panel.level = .screenSaver
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = false
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.animationBehavior = .none

        NotificationCenter.default.addObserver(
            self, selector: #selector(panelResigned),
            name: NSWindow.didResignKeyNotification, object: panel
        )
        // Switching Spaces changes the free area at once, so do not wait for the next tick.
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(spaceChanged),
            name: NSWorkspace.activeSpaceDidChangeNotification, object: nil
        )
        NotificationCenter.default.addObserver(
            self, selector: #selector(menuOpened),
            name: NSMenu.didBeginTrackingNotification, object: nil
        )
        NotificationCenter.default.addObserver(
            self, selector: #selector(menuClosed),
            name: NSMenu.didEndTrackingNotification, object: nil
        )
        NotificationCenter.default.addObserver(
            self, selector: #selector(screensChanged),
            name: NSApplication.didChangeScreenParametersNotification, object: nil
        )

        web.load(URLRequest(url: url))
        if let benchURL { openBench(benchURL) }
        scheduleTicker()
    }

    private func makeWebView() -> PanelWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "hq")
        config.setValue(false, forKey: "drawsBackground")
        let view = PanelWebView(frame: NSRect(x: 0, y: 0, width: 120, height: 36), configuration: config)
        view.navigationDelegate = self
        // A WKWebView with no UI delegate silently drops target="_blank" links.
        view.uiDelegate = self
        view.setValue(false, forKey: "drawsBackground")
        view.underPageBackgroundColor = .clear
        if #available(macOS 13.3, *), ProcessInfo.processInfo.environment["HQ_FLOAT_DEBUG"] != nil {
            view.isInspectable = true
        }
        return view
    }

    private func openBench(_ target: URL) {
        let size = NSSize(width: 440, height: 760)
        let area = NSScreen.main?.visibleFrame ?? NSRect(origin: .zero, size: size)
        let window = FloatingPanel(
            contentRect: NSRect(
                x: area.maxX - size.width - 24,
                y: area.maxY - min(size.height, area.height) - 24,
                width: size.width,
                height: min(size.height, area.height)
            ),
            styleMask: [.titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        window.acceptsKey = true
        window.title = "HQ floating window lab"
        window.level = .floating
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false
        window.delegate = self
        let view = makeWebView()
        window.contentView = view
        benchWeb = view
        benchPanel = window
        view.load(URLRequest(url: target))
        window.orderFrontRegardless()
    }

    /// ⌘V, ⌘C, ⌘X, ⌘A and ⌘Z are menu key equivalents: AppKit delivers them to
    /// the web view only by finding them in the main menu, and an accessory app
    /// has none. This one is never drawn; it gives those shortcuts somewhere to land.
    private func installEditMenu() {
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let item = NSMenuItem()
        item.submenu = edit
        let main = NSMenu()
        main.addItem(item)
        NSApp.mainMenu = main
    }

    private func installSignals() {
        for (number, action) in [(SIGTERM, #selector(quit)), (SIGUSR1, #selector(wake))] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in self?.perform(action) }
            source.resume()
            signals.append(source)
        }
    }

    // MARK: State

    private func readState() {
        guard let data = try? Data(contentsOf: stateURL),
              let state = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return }
        dock = state["dock"] as? String
        settings = state["settings"] as? [String: Any]
        if let until = (state["snoozedUntil"] as? NSNumber)?.doubleValue, until > Date().timeIntervalSince1970 {
            snoozedUntil = until
        }
    }

    private func writeState(dock: String?, snoozedUntil: Double?) -> Bool {
        var state: [String: Any] = [:]
        if let dock { state["dock"] = dock }
        if let snoozedUntil { state["snoozedUntil"] = snoozedUntil }
        if let settings { state["settings"] = settings }
        do {
            let data = try JSONSerialization.data(withJSONObject: state)
            try data.write(to: stateURL, options: .atomic)
            return true
        } catch {
            log("could not save \(stateURL.path): \(error.localizedDescription)")
            return false
        }
    }

    // MARK: Messages from the pages

    func userContentController(_: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame,
              let body = message.body as? [String: Any],
              let type = body["type"] as? String
        else { return }
        if message.webView === benchWeb {
            bench(type, body)
        } else if message.webView === web {
            page(type, body)
        }
    }

    private func page(_ type: String, _ body: [String: Any]) {
        switch type {
        case "ready":
            send(["type": "start", "dock": dock ?? NSNull(), "settings": settings ?? NSNull()])
            lastDisplay = nil
            tick()
            for (key, value) in feel { send(["type": "feel", "key": key, "value": value]) }
        case "place":
            place(body)
        case "drag":
            setDragging(body["on"] as? Bool ?? false)
        case "save":
            save(body["dock"] as? String)
        case "settings":
            guard let next = body["settings"] as? [String: Any], JSONSerialization.isValidJSONObject(next) else { return }
            settings = next
            _ = writeState(dock: dock, snoozedUntil: snoozedUntil)
        case "hotkeys":
            registerHotKeys(body["hotkeys"] as? [[String: Any]] ?? [])
        case "recordKey":
            if let action = body["action"] as? String { recordKey(for: action) }
        case "openDashboard":
            openDashboard()
        case "menu":
            showMenu()
        default:
            log("unknown page message \(type)")
        }
    }

    private func bench(_ type: String, _ body: [String: Any]) {
        switch type {
        case "feel":
            guard let key = body["key"] as? String, let value = body["value"] else { return }
            feel[key] = value
            send(["type": "feel", "key": key, "value": value])
        case "notice":
            if let event = body["event"] as? [String: Any] { send(["type": "notice", "event": event]) }
        case "picks":
            guard let text = body["text"] as? String,
                  let line = try? JSONSerialization.data(withJSONObject: ["picks": text])
            else { return }
            FileHandle.standardOutput.write(line + Data("\n".utf8))
            NSApp.terminate(nil)
        default:
            log("unknown bench message \(type)")
        }
    }

    private func send(_ message: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(message),
              let data = try? JSONSerialization.data(withJSONObject: message),
              let json = String(data: data, encoding: .utf8)
        else { return }
        web.evaluateJavaScript("window.hqFloat?.receive(\(json))", completionHandler: nil)
    }

    private func save(_ next: String?) {
        guard let next, !next.isEmpty, next.count <= 64 else {
            send(["type": "saved", "dock": next ?? NSNull(), "ok": false])
            return
        }
        // Only a write that landed changes the dock the next launch restores.
        let ok = writeState(dock: next, snoozedUntil: snoozedUntil)
        if ok { dock = next }
        send(["type": "saved", "dock": next, "ok": ok])
    }

    // MARK: Placement

    private func place(_ body: [String: Any]) {
        guard let x = number(body["x"]), let y = number(body["y"]),
              let width = number(body["width"]), let height = number(body["height"]),
              width > 0, height > 0, width <= 32768, height <= 32768,
              abs(x) <= 1_000_000, abs(y) <= 1_000_000,
              let f = activeScreen()?.frame
        else {
            log("rejected a place request")
            return
        }
        panel.hasShadow = body["shadow"] as? Bool ?? false
        panel.setFrame(NSRect(x: f.minX + x, y: f.maxY - y - height, width: width, height: height), display: true)
        setHitRect(body["hit"] as? [String: Any])
        refreshShadow()
        if !placed {
            placed = true
            if snoozedUntil == nil { panel.orderFrontRegardless() }
        }
        setKey(body["key"] as? Bool ?? false)
    }

    private func setHitRect(_ hit: [String: Any]?) {
        guard let hit, let x = number(hit["x"]), let y = number(hit["y"]),
              let width = number(hit["width"]), let height = number(hit["height"])
        else {
            hitRect = nil
            panel.ignoresMouseEvents = false
            return
        }
        hitRect = NSRect(x: x, y: y, width: width, height: height)
        checkPointer()
        guard hitTimer == nil else { return }
        let timer = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in self?.checkPointer() }
        RunLoop.main.add(timer, forMode: .common)
        hitTimer = timer
    }

    // macOS has no shaped hit area for a transparent window, so ask where the cursor is
    // and let clicks through everywhere else, as OpenFlow's status window does. This also
    // reports hover: WebKit gives a panel that is not key neither :hover nor mouse moves.
    private func checkPointer() {
        guard let hitRect, !dragging else {
            panel.ignoresMouseEvents = false
            return
        }
        // A press that started inside stays ours, such as selecting text past the edge.
        guard NSEvent.pressedMouseButtons == 0 else { return }
        let mouse = NSEvent.mouseLocation, frame = panel.frame
        let inside = hitRect.contains(NSPoint(x: mouse.x - frame.minX, y: frame.maxY - mouse.y))
        panel.ignoresMouseEvents = !inside
        guard inside != pointerInside else { return }
        pointerInside = inside
        send(["type": "hover", "on": inside])
    }

    private func number(_ value: Any?) -> CGFloat? {
        guard let number = (value as? NSNumber)?.doubleValue, number.isFinite else { return nil }
        return CGFloat(number)
    }

    // The native shadow comes from the window's alpha, which WebKit repaints a
    // moment after the frame changes.
    private func refreshShadow() {
        guard panel.hasShadow else { return }
        panel.invalidateShadow()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { [weak self] in
            self?.panel.invalidateShadow()
        }
    }

    private func setKey(_ wanted: Bool) {
        guard wanted != panel.acceptsKey else { return }
        panel.acceptsKey = wanted
        if wanted {
            panel.makeKey()
        } else if panel.isKeyWindow {
            releaseKey()
        }
    }

    // resignKey() is a notification, not a command. Deactivating hands the keyboard
    // back to the frontmost app's window without touching our window order;
    // orderOut would too, but flips the page's visibility on every collapse, and
    // the page reconnects its live stream each time that happens.
    private func releaseKey() {
        NSApp.deactivate()
    }

    // MARK: Following the screen

    private func activeScreen() -> NSScreen? {
        if let current, NSScreen.screens.contains(current) { return current }
        current = screenUnderCursor()
        return current
    }

    private func screenUnderCursor() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
    }

    private func describe(_ screen: NSScreen) -> DisplayInfo? {
        guard let primary = NSScreen.screens.first else { return nil }
        let f = screen.frame, v = screen.visibleFrame
        let id = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID ?? 0
        let top = f.maxY - v.maxY
        // A fullscreen app has no Dock, so its space is free. The top stays: the camera
        // housing is still there, and the menu bar still drops down over it.
        let dockless = Spaces.isFullscreen(on: id)
        return DisplayInfo(
            id: id,
            x: f.minX, y: primary.frame.maxY - f.maxY, width: f.width, height: f.height,
            visibleX: dockless ? 0 : v.minX - f.minX, visibleY: top,
            visibleWidth: dockless ? f.width : v.width, visibleHeight: dockless ? f.height - top : v.height
        )
    }

    private func tick() {
        if let until = snoozedUntil, Date().timeIntervalSince1970 >= until { wake() }
        // The pill and a drag go where the cursor is; an open panel stays put.
        let following = dragging || !panel.acceptsKey
        guard let screen = following ? screenUnderCursor() : activeScreen(),
              let info = describe(screen)
        else { return }
        current = screen
        guard info != lastDisplay else { return }
        let changed = lastDisplay.map { $0.id != info.id } ?? false
        if lastDisplay != nil, !changed, !dragging, !panel.acceptsKey { hideDuringTransition() }
        lastDisplay = info
        send([
            "type": "display",
            "x": info.x, "y": info.y, "width": info.width, "height": info.height,
            "visible": [
                "x": info.visibleX, "y": info.visibleY,
                "width": info.visibleWidth, "height": info.visibleHeight,
            ],
            "changed": changed,
        ])
    }

    /// The free area changing under an idle pill means the Dock or a Space is
    /// moving. Hide through the burst rather than let the pill visibly jump:
    /// reveal after 600ms of quiet, at most 2s after the burst began.
    private func hideDuringTransition() {
        let now = ProcessInfo.processInfo.systemUptime
        if now - burstLast > 0.6 { burstStart = now }
        burstLast = now
        let remaining = burstStart + 2 - now
        guard remaining > 0 else { return }
        reveal?.cancel()
        panel.alphaValue = 0
        let work = DispatchWorkItem { [weak self] in
            self?.panel.alphaValue = 1
            self?.reveal = nil
        }
        reveal = work
        DispatchQueue.main.asyncAfter(deadline: .now() + min(0.6, remaining), execute: work)
    }

    private func scheduleTicker() {
        ticker?.invalidate()
        let timer = Timer(timeInterval: dragging ? 0.05 : 0.4, repeats: true) { [weak self] _ in self?.tick() }
        // .common keeps it running during menu and mouse tracking.
        RunLoop.main.add(timer, forMode: .common)
        ticker = timer
    }

    private func setDragging(_ on: Bool) {
        guard on != dragging else { return }
        dragging = on
        watchdog?.cancel()
        watchdog = nil
        if on {
            // A drag interrupts any pending reveal and must stay visible.
            reveal?.cancel()
            reveal = nil
            panel.alphaValue = 1
            let work = DispatchWorkItem { [weak self] in self?.cancelDrag() }
            watchdog = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 30, execute: work)
        }
        scheduleTicker()
    }

    private func cancelDrag() {
        guard dragging else { return }
        setDragging(false)
        send(["type": "cancelDrag"])
    }

    @objc private func spaceChanged() {
        tick()
    }

    @objc private func screensChanged() {
        cancelDrag()
        tick()
    }

    // Menus and dropdowns are fixed at popUpMenu level, under the panel, so it steps
    // just below them for as long as one is open.
    @objc private func menuOpened() {
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.popUpMenu.rawValue - 1)
    }

    @objc private func menuClosed() {
        panel.level = .screenSaver
    }

    @objc private func panelResigned() {
        send(["type": "resigned"])
    }

    // MARK: Shortcuts

    // Carbon hot keys need no Accessibility permission and work from any app, which an
    // NSEvent global monitor cannot promise: it watches keys but cannot consume them.
    private func installHotKeyHandler() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let me = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            var id = EventHotKeyID()
            let status = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                           nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            if status == noErr, let context {
                Unmanaged<Shell>.fromOpaque(context).takeUnretainedValue().hotKeyPressed(id.id)
            }
            return noErr
        }, 1, &spec, me, &hotKeyHandler)
    }

    private func hotKeyPressed(_ id: UInt32) {
        if let action = hotKeyActions[id] { send(["type": "hotkey", "action": action]) }
    }

    private func unregisterHotKeys() {
        hotKeyRefs.forEach { UnregisterEventHotKey($0) }
        hotKeyRefs = []
        hotKeyActions = [:]
    }

    private func registerHotKeys(_ entries: [[String: Any]]) {
        unregisterHotKeys()
        var failed: [String] = []
        for (index, entry) in entries.enumerated() {
            guard let action = entry["action"] as? String,
                  let keyCode = (entry["keyCode"] as? NSNumber)?.uint32Value,
                  let modifiers = (entry["modifiers"] as? NSNumber)?.uint32Value
            else { continue }
            let id = EventHotKeyID(signature: OSType(0x4851464C), id: UInt32(index + 1)) // "HQFL"
            var ref: EventHotKeyRef?
            let status = RegisterEventHotKey(keyCode, modifiers, id, GetApplicationEventTarget(), 0, &ref)
            if status == noErr, let ref {
                hotKeyRefs.append(ref)
                hotKeyActions[id.id] = action
            } else {
                failed.append(action)
            }
        }
        send(["type": "hotkeysApplied", "failed": failed])
    }

    // While recording, the existing shortcuts stand down so pressing one can record it.
    private func recordKey(for action: String) {
        unregisterHotKeys()
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            if Int(event.keyCode) == kVK_Escape {
                self.finishRecording(["type": "keyRecorded", "action": action])
            } else if [kVK_Delete, kVK_ForwardDelete].contains(Int(event.keyCode)) {
                self.finishRecording(["type": "keyRecorded", "action": action, "cleared": true])
            } else if let key = self.describeKey(event) {
                self.finishRecording(["type": "keyRecorded", "action": action, "key": key])
            }
            return nil
        }
    }

    private func finishRecording(_ message: [String: Any]) {
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
        send(message)
    }

    /// A global shortcut needs Command, Control or Option, or it would swallow plain typing.
    private func describeKey(_ event: NSEvent) -> [String: Any]? {
        let flags = event.modifierFlags
        guard !flags.intersection([.command, .control, .option]).isEmpty else { return nil }
        var carbon = 0, label = ""
        if flags.contains(.control) { carbon |= controlKey; label += "⌃" }
        if flags.contains(.option) { carbon |= optionKey; label += "⌥" }
        if flags.contains(.shift) { carbon |= shiftKey; label += "⇧" }
        if flags.contains(.command) { carbon |= cmdKey; label += "⌘" }
        let named = [kVK_Space: "Space", kVK_Return: "↩", kVK_Tab: "⇥", kVK_LeftArrow: "←", kVK_RightArrow: "→",
                     kVK_UpArrow: "↑", kVK_DownArrow: "↓"]
        label += named[Int(event.keyCode)] ?? (event.charactersIgnoringModifiers ?? "").uppercased()
        return ["keyCode": Int(event.keyCode), "modifiers": carbon, "label": label]
    }

    // MARK: Menu and snooze

    private func showMenu() {
        let menu = NSMenu()
        let items: [(String, Selector)] = [
            ("Open dashboard", #selector(openDashboard)),
            ("Customize pill…", #selector(customize)),
            ("Hide for an hour", #selector(hideForAnHour)),
        ]
        for (title, action) in items {
            let item = menu.addItem(withTitle: title, action: action, keyEquivalent: "")
            item.target = self
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(quit), keyEquivalent: "").target = self
        menu.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil)
    }

    @objc private func customize() {
        send(["type": "customize"])
    }

    @objc private func openDashboard() {
        guard var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }
        parts.path = "/"
        parts.query = nil
        parts.fragment = nil
        if let target = parts.url { NSWorkspace.shared.open(target) }
    }

    @objc private func hideForAnHour() {
        let until = Date().timeIntervalSince1970 + 3600
        if writeState(dock: dock, snoozedUntil: until) { snoozedUntil = until }
        panel.orderOut(nil)
    }

    @objc private func wake() {
        if snoozedUntil != nil {
            snoozedUntil = nil
            _ = writeState(dock: dock, snoozedUntil: nil)
        }
        if placed { panel.orderFrontRegardless() }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    func windowWillClose(_ notification: Notification) {
        if (notification.object as? NSWindow) === benchPanel { NSApp.terminate(nil) }
    }

    // MARK: Navigation

    private func sameOrigin(_ target: URL) -> Bool {
        target.scheme == url.scheme && target.host == url.host && target.port == url.port
    }

    /// The last gate before a URL an agent wrote is handed to the system.
    private func openExternally(_ target: URL) {
        guard target.user == nil, target.password == nil else { return }
        let scheme = target.scheme?.lowercased()
        guard scheme == "https" || (scheme == "http" && sameOrigin(target)) else { return }
        NSWorkspace.shared.open(target)
    }

    func webView(
        _: WKWebView,
        decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let target = action.request.url, !sameOrigin(target), target.scheme != "about" else {
            decisionHandler(.allow)
            return
        }
        openExternally(target)
        decisionHandler(.cancel)
    }

    /// target="_blank" asks for a new window; hand it to the browser instead.
    func webView(
        _: WKWebView,
        createWebViewWith _: WKWebViewConfiguration,
        for action: WKNavigationAction,
        windowFeatures _: WKWindowFeatures
    ) -> WKWebView? {
        if let target = action.request.url { openExternally(target) }
        return nil
    }

    func webView(_ view: WKWebView, didFinish _: WKNavigation!) {
        finished.insert(ObjectIdentifier(view))
    }

    func webView(_ view: WKWebView, didFail _: WKNavigation!, withError error: Error) {
        retry(view, error)
    }

    func webView(_ view: WKWebView, didFailProvisionalNavigation _: WKNavigation!, withError error: Error) {
        retry(view, error)
    }

    private func address(of view: WKWebView) -> URL? {
        view === benchWeb ? benchURL : url
    }

    // Only a page that never loaded: reloading a live one would lose unsent replies.
    private func retry(_ view: WKWebView, _ error: Error) {
        guard !finished.contains(ObjectIdentifier(view)),
              (error as NSError).code != NSURLErrorCancelled
        else { return }
        log("load failed, retrying: \(error.localizedDescription)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self, weak view] in
            guard let self, let view, let target = self.address(of: view) else { return }
            view.load(URLRequest(url: target))
        }
    }

    func webViewWebContentProcessDidTerminate(_ view: WKWebView) {
        finished.remove(ObjectIdentifier(view))
        log("web content process ended, reloading")
        if let target = address(of: view) { view.load(URLRequest(url: target)) }
    }
}

let arguments = Array(CommandLine.arguments.dropFirst())
var benchTarget: URL?
if let flag = arguments.firstIndex(of: "--bench"), arguments.indices.contains(flag + 1) {
    benchTarget = URL(string: arguments[flag + 1])
}
guard arguments.count >= 2, let target = URL(string: arguments[0]) else {
    FileHandle.standardError.write(Data("usage: hq-float <url> <state-file> [--bench <url>]\n".utf8))
    exit(64)
}

let app = NSApplication.shared
// .accessory is the programmatic LSUIElement: no Dock icon, no menu bar, never
// in the app switcher.
app.setActivationPolicy(.accessory)
let shell = Shell(url: target, stateURL: URL(fileURLWithPath: arguments[1]), benchURL: benchTarget)
app.delegate = shell
app.run()
