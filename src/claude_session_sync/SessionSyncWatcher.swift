import AppKit
import Foundation

private final class SessionSyncWatcher: NSObject {
  private let command: [String]
  private let claudeExecutable: String
  private let statusURL: URL
  private let queue = DispatchQueue(label: "com.claude-session-sync.watcher")
  private var running = false
  private var pending = false

  init(command: [String], claudeExecutable: String, statusURL: URL) {
    self.command = command
    self.claudeExecutable = URL(fileURLWithPath: claudeExecutable).standardizedFileURL.path
    self.statusURL = statusURL
    super.init()
  }

  func requestAuto() {
    queue.async { [weak self] in self?.startAutoIfNeeded() }
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
    task.arguments = Array(command.dropFirst()) + ["auto"]
    task.standardInput = FileHandle.nullDevice
    task.standardOutput = output
    task.standardError = FileHandle.nullDevice
    task.terminationHandler = { [weak self] completed in
      let data = output.fileHandleForReading.readDataToEndOfFile()
      self?.queue.async {
        self?.finishAuto(exitStatus: completed.terminationStatus, output: data)
      }
    }
    do { try task.run() } catch {
      writeStatus(exitStatus: 127, output: Data(), launchFailed: true)
      NSLog("claude-session-sync watcher could not start auto")
      running = false
      runPendingIfNeeded()
    }
  }

  private func finishAuto(exitStatus: Int32, output: Data) {
    writeStatus(exitStatus: exitStatus, output: output, launchFailed: false)
    if exitStatus != 0 {
      NSLog("claude-session-sync auto failed with exit status %d", exitStatus)
    }
    running = false
    runPendingIfNeeded()
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
    if executableMatches || knownBundle { requestAuto() }
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
