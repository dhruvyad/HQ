// HQ Links: where macOS sends an hq://i/<id> link an agent wrote in a terminal chat.
//
// It runs `hq open` with the link, which shows the item in the pill, or the dashboard when the
// pill isn't running. The app is background-only, so it never takes focus from the terminal the
// link was clicked in, and it quits once it has nothing left to open. hq/opener.py builds the
// app around this file and names the command to run in its Info.plist.

import AppKit

func log(_ message: String) {
    FileHandle.standardError.write(Data("hq-links: \(message)\n".utf8))
}

final class Opener: NSObject, NSApplicationDelegate {
    private var running = 0
    private var idle: DispatchWorkItem?

    func applicationDidFinishLaunching(_: Notification) {
        quitWhenIdle()
    }

    func application(_: NSApplication, open urls: [URL]) {
        for url in urls { open(url) }
    }

    private func open(_ url: URL) {
        guard url.scheme?.lowercased() == "hq" else { return }
        guard let command = Bundle.main.object(forInfoDictionaryKey: "HQCommand") as? [String],
              let program = command.first
        else { return log("no HQCommand in Info.plist") }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: program)
        // The link goes in as one argument, never through a shell; `hq open` checks it is an item.
        process.arguments = Array(command.dropFirst()) + ["open", url.absoluteString]
        var environment = ProcessInfo.processInfo.environment
        if let home = Bundle.main.object(forInfoDictionaryKey: "HQHome") as? String {
            environment["HQ_HOME"] = home
        }
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.terminationHandler = { [weak self] done in
            if done.terminationStatus != 0 { log("hq open exited with \(done.terminationStatus) for \(url.absoluteString)") }
            DispatchQueue.main.async {
                self?.running -= 1
                self?.quitWhenIdle()
            }
        }
        do {
            try process.run()
            running += 1
            idle?.cancel()
        } catch {
            log("could not run \(program): \(error.localizedDescription)")
        }
    }

    // A few links clicked in a row reach this same process, so it lingers briefly before quitting.
    private func quitWhenIdle() {
        idle?.cancel()
        guard running == 0 else { return }
        let work = DispatchWorkItem { NSApp.terminate(nil) }
        idle = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }
}

let app = NSApplication.shared
let opener = Opener()
app.delegate = opener
app.run()
