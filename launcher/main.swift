// timelens.app: a tiny launcher so macOS shows "timelens" (not "python3.13") in Privacy & Security.
//
// macOS credits screen access to the app that started a process, so this app starts the timelens tracker as a
// child and waits for it. It only ever runs timelens.py (next to the app) with timelens's own Python, never an
// arbitrary command, so other programs can't borrow its screen-recording permission.
//
// It also posts timelens's notifications (`notify`), so they carry its name and icon. A click on one goes to the
// running timelens app, which is the tracker (`run`), so that one listens for clicks and opens the file the
// notification points at. If timelens isn't running, macOS launches the app with no arguments to handle it.
import AppKit
import UserNotifications

let bundleDir = Bundle.main.bundleURL.deletingLastPathComponent()
let script = bundleDir.appendingPathComponent("timelens.py").path
let data = ProcessInfo.processInfo.environment["TIMELENS_DATA"]
    ?? NSHomeDirectory() + "/Library/Application Support/timelens"
let python = data + "/venv/bin/python"
let allowed: Set<String> = ["run", "sample", "process"]   // the commands that need the screen

let args = Array(CommandLine.arguments.dropFirst())

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write("\(message)\n".data(using: .utf8)!)
    exit(code)
}

class Clicks: NSObject, UNUserNotificationCenterDelegate {
    var quitAfter = false   // only when the app was launched just to handle a click
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let path = response.notification.request.content.userInfo["open"] as? String {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
        done()
        if quitAfter { exit(0) }
    }
}

let center = UNUserNotificationCenter.current()
let clicks = Clicks()   // the center only holds its delegate weakly

if args.isEmpty {   // a notification was clicked (or the app was opened from Finder: then nothing arrives)
    center.delegate = clicks   // must be set before the app finishes launching, or the click is lost
    clicks.quitAfter = true
    DispatchQueue.main.asyncAfter(deadline: .now() + 10) { exit(0) }
    NSApplication.shared.run()   // a normal app launch (no Dock icon: LSUIElement), which is what the click is handed to
}

if args.first == "notify" {   // notify --title T --body B [--open PATH]: post one notification and quit, no Python
    var opts: [String: String] = [:], rest = args.dropFirst()
    while let flag = rest.popFirst(), let value = rest.popFirst() { opts[flag] = value }
    let content = UNMutableNotificationContent()
    content.title = opts["--title"] ?? "timelens"
    content.body = opts["--body"] ?? ""
    if let path = opts["--open"] { content.userInfo = ["open": path] }
    // the first time, macOS asks you to allow notifications and this waits for your answer (the question
    // disappears when this process quits, so give it a few minutes)
    center.requestAuthorization(options: [.alert]) { granted, error in
        guard granted else {
            fail("notifications are off for timelens: turn them on in System Settings > Notifications (\(error.map { "\($0)" } ?? "denied"))", 77)
        }
        center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)) { error in
            if let error { fail("couldn't post the notification: \(error)", 70) }
            exit(0)
        }
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 300) { fail("notifications for timelens are still waiting for your OK", 75) }
    dispatchMain()
}

guard let command = args.first, allowed.contains(command) else {
    FileHandle.standardError.write("timelens.app only starts the tracker: timelens run|sample|process\n".data(using: .utf8)!)
    exit(64)
}
guard FileManager.default.isExecutableFile(atPath: python), FileManager.default.fileExists(atPath: script) else {
    FileHandle.standardError.write("timelens isn't set up yet: run `timelens setup` first\n".data(using: .utf8)!)
    exit(69)
}

let child = Process()
child.executableURL = URL(fileURLWithPath: python)
child.arguments = [script] + args

// pass stop signals (launchd sends SIGTERM) on to the tracker
var sources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .global())
    source.setEventHandler { kill(child.processIdentifier, sig) }
    source.resume()
    sources.append(source)
}

child.terminationHandler = { exit($0.terminationStatus) }
do { try child.run() } catch {
    FileHandle.standardError.write("couldn't start timelens: \(error)\n".data(using: .utf8)!)
    exit(70)
}
// while the tracker runs, this is the running timelens app: clicks on its notifications arrive here
center.delegate = clicks
NSApplication.shared.run()
