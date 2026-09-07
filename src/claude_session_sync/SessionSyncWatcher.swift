import AppKit
import Foundation

private final class SessionSyncWatcher: NSObject {
  private let command: [String]
  private let claudeExecutable: String
  private let statusURL: URL
  private let queue = DispatchQueue(label: "com.claude-session-sync.watcher")
  private var running = false
  private var pending = false
  private var retryDeadline: Date?
  private var lastNotice: String?

  init(command: [String], claudeExecutable: String, statusURL: URL) {
    self.command = command
    self.claudeExecutable = URL(fileURLWithPath: claudeExecutable).standardizedFileURL.path
    self.statusURL = statusURL
    super.init()
  }

  func requestAuto(afterQuit: Bool = false) {
    queue.async { [weak self] in
      if afterQuit {
        self?.retryDeadline = Date().addingTimeInterval(30)
        self?.lastNotice = nil
      }
      self?.startAutoIfNeeded()
    }
  }

  private func startAutoIfNeeded() {
    if running {
      pending = true
      return
    }
    guard let executable = command.first else { return }
    running = true
    let task = Process()
    let output = Pipe()
    task.executableURL = URL(fileURLWithPath: executable)
    task.arguments = Array(command.dropFirst()) + ["auto", "--json"]
    task.standardInput = FileHandle.nullDevice
    task.standardOutput = output
    task.standardError = FileHandle.nullDevice
    do {
      try task.run()
      // Drain while the child runs, but retain only bounded aggregate output.
      DispatchQueue.global(qos: .utility).async { [weak self] in
        var data = Data()
        while true {
          let chunk = output.fileHandleForReading.readData(ofLength: 4_096)
          if chunk.isEmpty { break }
          data.append(chunk.prefix(max(0, 65_536 - data.count)))
        }
        task.waitUntilExit()
        let captured = data
        self?.queue.async {
          self?.finishAuto(exitStatus: task.terminationStatus, output: captured)
        }
      }
    } catch {
      writeStatus(exitStatus: 127, output: Data(), launchFailed: true)
      notify("Sync needs attention. The sync tool could not start.")
      NSLog("claude-session-sync watcher could not start auto")
      running = false
      runPendingIfNeeded()
    }
  }

  private func finishAuto(exitStatus: Int32, output: Data) {
    let result = (try? JSONSerialization.jsonObject(with: output)) as? [String: Any]
    writeStatus(exitStatus: result == nil ? 1 : exitStatus, output: output, launchFailed: false)
    let reason = result?["reason"] as? String
    let progress = result?["progress"] as? String
    running = false
    if reason == "app-running" && NSWorkspace.shared.runningApplications.contains(where: {
      $0.executableURL?.standardizedFileURL.path == claudeExecutable
    }) {
      retryDeadline = nil
      runPendingIfNeeded()
      return
    }
    if reason == "busy" || (reason == "app-running" && retryDeadline != nil) {
      if retryDeadline == nil { retryDeadline = Date().addingTimeInterval(30) }
      if let deadline = retryDeadline, Date() < deadline {
        queue.asyncAfter(deadline: .now() + 1) { [weak self] in self?.startAutoIfNeeded() }
        return
      }
      writeStatus(exitStatus: 1, output: output, launchFailed: false)
      notify("Sync is still waiting. Quit Claude completely, then run sync again.")
    } else if progress == "finished" {
      notify("Sync finished. You can open Claude.")
    } else if exitStatus != 0 || progress == "needs-attention" || result == nil {
      notify("Sync needs attention. Run claude-session-sync doctor for details.")
    }
    retryDeadline = nil
    if exitStatus != 0 {
      NSLog("claude-session-sync auto failed with exit status %d", exitStatus)
    }
    runPendingIfNeeded()
  }

  private func notify(_ message: String) {
    if ProcessInfo.processInfo.environment["CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS"] == "1" {
      return
    }
    guard lastNotice != message else { return }
    lastNotice = message
    let notification = Process()
    notification.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    notification.arguments = ["-e", """
      on run argv
        display notification (item 1 of argv) with title "Claude Session Sync"
      end run
      """, message]
    notification.standardInput = FileHandle.nullDevice
    notification.standardOutput = FileHandle.nullDevice
    notification.standardError = FileHandle.nullDevice
    do {
      try notification.run()
      DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 5) {
        if notification.isRunning { notification.terminate() }
      }
    } catch { NSLog("claude-session-sync could not request a notification") }
  }

  private func runPendingIfNeeded() {
    if pending {
      pending = false
      startAutoIfNeeded()
    }
  }

  private func writeStatus(exitStatus: Int32, output: Data, launchFailed: Bool) {
    let bounded = output.prefix(2_048)
    let outputText = String(data: bounded, encoding: .utf8) ?? ""
    let document: [String: Any] = [
      "exit_status": Int(exitStatus), "launch_failed": launchFailed,
      "output": outputText, "state": exitStatus == 0 ? "ok" : "failed",
      "timestamp": ISO8601DateFormatter().string(from: Date()),
    ]
    do {
      let directory = statusURL.deletingLastPathComponent()
      try FileManager.default.createDirectory(
        at: directory, withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
      )
      try FileManager.default.setAttributes(
        [.posixPermissions: 0o700], ofItemAtPath: directory.path)
      let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
      try data.write(to: statusURL, options: [.atomic])
      try FileManager.default.setAttributes(
        [.posixPermissions: 0o600], ofItemAtPath: statusURL.path)
    } catch { NSLog("claude-session-sync watcher could not persist status") }
  }

  @objc func applicationTerminated(_ notification: Notification) {
    guard
      let application = notification.userInfo?[NSWorkspace.applicationUserInfoKey]
        as? NSRunningApplication
    else { return }
    let executableMatches = application.executableURL?.standardizedFileURL.path == claudeExecutable
    let bundleIdentifier = application.bundleIdentifier ?? ""
    let knownBundle =
      bundleIdentifier == "com.anthropic.claudefordesktop"
      || bundleIdentifier == "com.khiet.claude-personal"
      || bundleIdentifier.hasPrefix("com.claude-session-sync.")
    if executableMatches || knownBundle { requestAuto(afterQuit: true) }
  }
}

private let separatorIndex = CommandLine.arguments.firstIndex(of: "--")
guard let separator = separatorIndex else {
  fputs(
    "usage: SessionSyncWatcher --claude-executable PATH --status PATH -- COMMAND [ARG ...]\n",
    stderr)
  exit(64)
}
let watcherArguments = Array(CommandLine.arguments[1..<separator])
var claudeExecutable: String?
var statusPath: String?
var index = 0
while index < watcherArguments.count {
  guard index + 1 < watcherArguments.count else {
    fputs("SessionSyncWatcher option requires a value\n", stderr)
    exit(64)
  }
  switch watcherArguments[index] {
  case "--claude-executable": claudeExecutable = watcherArguments[index + 1]
  case "--status": statusPath = watcherArguments[index + 1]
  default:
    fputs("SessionSyncWatcher received an unknown option\n", stderr)
    exit(64)
  }
  index += 2
}
let command = Array(CommandLine.arguments.dropFirst(separator + 1))
guard let configuredExecutable = claudeExecutable,
  let configuredStatusPath = statusPath, !command.isEmpty
else {
  fputs("SessionSyncWatcher requires executable, status, and command arguments\n", stderr)
  exit(64)
}
private let watcher = SessionSyncWatcher(
  command: command, claudeExecutable: configuredExecutable,
  statusURL: URL(fileURLWithPath: configuredStatusPath)
)
NSWorkspace.shared.notificationCenter.addObserver(
  watcher, selector: #selector(SessionSyncWatcher.applicationTerminated(_:)),
  name: NSWorkspace.didTerminateApplicationNotification, object: nil
)
watcher.requestAuto()
RunLoop.main.run()
