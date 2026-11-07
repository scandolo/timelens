// timelens.app: a tiny launcher so macOS shows "timelens" (not "python3.13") in Privacy & Security.
//
// macOS credits screen access to the app that started a process, so this app starts the timelens tracker as a
// child and waits for it. It only ever runs timelens.py (next to the app) with timelens's own Python, never an
// arbitrary command, so other programs can't borrow its screen-recording permission.
import Foundation

let bundleDir = Bundle.main.bundleURL.deletingLastPathComponent()
let script = bundleDir.appendingPathComponent("timelens.py").path
let data = ProcessInfo.processInfo.environment["TIMELENS_DATA"]
    ?? NSHomeDirectory() + "/Library/Application Support/timelens"
let python = data + "/venv/bin/python"
let allowed: Set<String> = ["run", "sample", "process"]   // the commands that need the screen

let args = Array(CommandLine.arguments.dropFirst())
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

do { try child.run() } catch {
    FileHandle.standardError.write("couldn't start timelens: \(error)\n".data(using: .utf8)!)
    exit(70)
}
child.waitUntilExit()
exit(child.terminationStatus)
