import AppKit
import Foundation
import CryptoKit
import Darwin

private enum RestartCheck {
  case ready(Set<pid_t>)
  case retryable
  case blocked
}

private struct RestartReceipt: Codable {
  let accountHash: String
  let phase: String
  var candidateHash: String?
  var preflightAttempts: Int?
  var retryNotBefore: Double?

  private enum CodingKeys: String, CodingKey {
    case accountHash = "account_hash"
    case phase
    case candidateHash = "candidate_hash"
    case preflightAttempts = "preflight_attempts"
    case retryNotBefore = "retry_not_before"
  }
}

// Chats sync while Claude runs. When new chats land in the signed-in account's
// folder, Claude shows them only after a restart, so the menu offers one.
// Pins and groups live in Claude's own database, which it locks while open, and
// Claude reloads each account's groups from its servers at sign-in. So when
// pin and group sync is on, an account switch quits Claude once, copies the
// organization from the account just left, and reopens Claude.
private final class SessionSyncWatcher: NSObject {
  private let command: [String]
  private let claudeExecutable: String
  private let statusURL: URL
  private let queue = DispatchQueue(label: "com.claude-session-sync.watcher")
  private var running = false
  private var pending = false
  private var retryDeadline: Date?
  private let accountURL: URL?
  private let sessionsURL: URL?
  private let profile: String?
  private let restartURL: URL
  private let restartRequestURL: URL
  private var accountHash: String?
  private var candidateHash: String?
  private var candidateSince: Date?
  private var accountTimer: DispatchSourceTimer?
  private var restartRequested = false
  private var restartAttempt: UUID?
  private var quittingPIDs = Set<pid_t>()
  private var restartPhase = "ready"
  private var restartSuggested = 0
  private var restartPending = false
  private var pendingRestartMode: String?
  private var restartMode: String?
  private let restartOnSwitch: Bool
  private var lastFailureKey: String?
  private var folderSignature: String?
  private var lastFolderCheck = Date.distantPast
  private var lastFallbackRun = Date()
  private var statusItem: NSStatusItem?
  private var noticePanel: NSPanel?
  private var noticeGeneration = 0
  private var lastMessage = ""

  init(command: [String], claudeExecutable: String, statusURL: URL,
       accountURL: URL?, profile: String?, restartOnSwitch: Bool) {
    self.command = command
    self.restartOnSwitch = restartOnSwitch && profile != nil
    self.claudeExecutable = URL(fileURLWithPath: claudeExecutable).standardizedFileURL.path
    self.statusURL = statusURL
    self.accountURL = accountURL
    self.sessionsURL = accountURL?.deletingLastPathComponent().appendingPathComponent("claude-code-sessions")
    self.profile = profile
    let directory = statusURL.deletingLastPathComponent()
    self.restartURL = directory.appendingPathComponent("account-restart.json")
    self.restartRequestURL = directory.appendingPathComponent("restart-request")
    super.init()
    if accountURL != nil {
      do {
        if FileManager.default.fileExists(atPath: restartURL.path) {
          let saved = try JSONDecoder().decode(RestartReceipt.self, from: privateData(restartURL))
          let hash = saved.accountHash
          let phase = saved.phase
          guard hash.count == 64, hash.allSatisfy({ $0.isHexDigit }),
            ["ready", "finished", "checking", "quitting", "syncing", "needs-attention"].contains(phase)
          else { throw CocoaError(.fileReadCorruptFile) }
          accountHash = hash
          if phase == "checking" {
            // Older helpers quit Claude after an account check. Validate the
            // receipt, then drop the pending check: switches never quit Claude.
            guard let candidate = saved.candidateHash, candidate.count == 64,
              candidate.allSatisfy({ $0.isHexDigit }), candidate != hash,
              let attempts = saved.preflightAttempts, (1...3).contains(attempts),
              let retryAt = saved.retryNotBefore, retryAt.isFinite, retryAt >= 0
            else { throw CocoaError(.fileReadCorruptFile) }
            restartPhase = "ready"
          } else if !["ready", "finished"].contains(phase) {
            restartPhase = "needs-attention"
          }
        } else if let hash = readAccountHash() {
          accountHash = hash
          try saveRestartState()
        }
      } catch {
        restartPhase = "needs-attention"
        // An unreadable receipt must never become permission to quit.
      }
      folderSignature = currentFolderSignature()
      let timer = DispatchSource.makeTimerSource(queue: queue)
      timer.schedule(deadline: .now() + 1, repeating: 1)
      timer.setEventHandler { [weak self] in self?.tick() }
      timer.resume()
      accountTimer = timer
    }
    showStatus(restartPhase == "needs-attention" ? "Sync needs attention" :
      accountURL != nil ? "Sync is on. Chats sync while Claude is open." : "Sync ready. Quit Claude to sync.", banner: true)
  }

  private func privateData(_ url: URL) throws -> Data {
    let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true,
      let size = values.fileSize, size <= 1_048_576
    else { throw CocoaError(.fileReadCorruptFile) }
    return try Data(contentsOf: url)
  }

  private func readAccountHash() -> String? {
    guard let url = accountURL, let data = try? privateData(url),
      let document = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let value = document["lastKnownAccountUuid"] as? String,
      let uuid = UUID(uuidString: value)
    else { return nil }
    // Never retain or log the surrounding config, which can contain credentials.
    return SHA256.hash(data: Data(uuid.uuidString.utf8)).map { String(format: "%02x", $0) }.joined()
  }

  private func saveRestartState() throws {
    guard let hash = accountHash else { return }
    let directory = restartURL.deletingLastPathComponent()
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
      attributes: [.posixPermissions: 0o700])
    guard (try directory.resourceValues(forKeys: [.isSymbolicLinkKey])).isSymbolicLink != true
    else { throw CocoaError(.fileWriteNoPermission) }
    if FileManager.default.fileExists(atPath: restartURL.path) { _ = try privateData(restartURL) }
    let receipt = RestartReceipt(accountHash: hash, phase: restartPhase)
    let data = try JSONEncoder().encode(receipt)
    try data.write(to: restartURL, options: [.atomic])
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: restartURL.path)
    let file = try FileHandle(forWritingTo: restartURL)
    defer { file.closeFile() }
    try file.synchronize()
    let descriptor = open(directory.path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else { throw CocoaError(.fileWriteUnknown) }
    defer { close(descriptor) }
    guard fsync(descriptor) == 0 else { throw CocoaError(.fileWriteUnknown) }
  }

  private func restartPIDs() -> RestartCheck {
    guard let executable = command.first, let profile = profile else { return .blocked }
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = Array(command.dropFirst()) + ["restart-check", profile]
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do {
      try process.run()
      // Allow the CLI's bounded process probe plus interpreter startup time.
      DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 10) {
        if process.isRunning { process.terminate() }
      }
      let data = pipe.fileHandleForReading.readData(ofLength: 4_097)
      process.waitUntilExit()
      if process.terminationReason == .uncaughtSignal && process.terminationStatus == SIGTERM {
        return .retryable
      }
      guard data.count <= 4_096,
        let document = try JSONSerialization.jsonObject(with: data) as? [String: Any]
      else { return .blocked }
      if process.terminationStatus != 0,
        document["error_type"] as? String == "process-timeout",
        document["reason"] as? String == "process-inspection-timeout" {
        return .retryable
      }
      guard process.terminationStatus == 0,
        document["state"] as? String == "ready", let pids = document["pids"] as? [Int],
        pids.allSatisfy({ $0 > 0 && $0 <= Int(Int32.max) }) else { return .blocked }
      return .ready(Set(pids.map { pid_t($0) }))
    } catch { return .blocked }
  }

  private func tick() {
    if restartRequested && restartPhase == "quitting" {
      let attempt = restartAttempt
      // Quit notifications can omit the executable URL. Also observe actual exit.
      DispatchQueue.main.async { [weak self] in
        guard let self = self else { return }
        let stillRunning = NSWorkspace.shared.runningApplications.contains {
          $0.executableURL?.standardizedFileURL.path == self.claudeExecutable
        }
        self.queue.async {
          if !stillRunning && self.restartRequested && self.restartPhase == "quitting" && self.restartAttempt == attempt {
            self.startAutoIfNeeded()
          }
        }
      }
      return
    }
    if FileManager.default.fileExists(atPath: restartRequestURL.path) {
      let request = (try? privateData(restartRequestURL)).flatMap { String(data: $0.prefix(64), encoding: .utf8) } ?? ""
      try? FileManager.default.removeItem(at: restartRequestURL)
      requestRestart(mode: request.contains("adopt-current-sidebar") ? "adopt-current-sidebar" : nil)
      return
    }
    checkFolders()
    checkAccount()
  }

  private func currentFolderSignature() -> String? {
    guard let root = sessionsURL else { return nil }
    let manager = FileManager.default
    let keys: [URLResourceKey] = [.contentModificationDateKey, .isDirectoryKey, .isSymbolicLinkKey]
    guard let accounts = try? manager.contentsOfDirectory(at: root, includingPropertiesForKeys: keys,
      options: [.skipsHiddenFiles]) else { return "" }
    var parts: [String] = []
    for account in accounts.sorted(by: { $0.path < $1.path }) {
      guard let folders = try? manager.contentsOfDirectory(at: account, includingPropertiesForKeys: keys,
        options: [.skipsHiddenFiles]) else { continue }
      for folder in folders.sorted(by: { $0.path < $1.path }) {
        let values = try? folder.resourceValues(forKeys: Set(keys))
        guard values?.isDirectory == true, values?.isSymbolicLink != true else { continue }
        let stamp = values?.contentModificationDate?.timeIntervalSince1970 ?? 0
        parts.append("\(account.lastPathComponent)/\(folder.lastPathComponent):\(stamp)")
      }
    }
    return parts.joined(separator: "|")
  }

  // Claude saves a chat by renaming a temp file into place, which changes its
  // folder's time. A five-minute pass catches anything else.
  private func checkFolders() {
    let now = Date()
    guard sessionsURL != nil, now.timeIntervalSince(lastFolderCheck) >= 5 else { return }
    lastFolderCheck = now
    let signature = currentFolderSignature()
    let changed = signature != folderSignature
    folderSignature = signature
    if changed || now.timeIntervalSince(lastFallbackRun) >= 300 {
      lastFallbackRun = now
      startAutoIfNeeded()
    }
  }

  private func checkAccount() {
    guard !restartRequested else { return }
    guard let hash = readAccountHash() else {
      candidateHash = nil
      candidateSince = nil
      return
    }
    guard let previous = accountHash else {
      accountHash = hash
      do { try saveRestartState() } catch { restartFailed("Could not save account-switch status.") }
      return
    }
    guard hash != previous else {
      candidateHash = nil
      candidateSince = nil
      return
    }
    if candidateHash != hash {
      candidateHash = hash
      candidateSince = Date()
      return
    }
    guard let since = candidateSince, Date().timeIntervalSince(since) >= 3 else { return }
    accountHash = hash
    candidateHash = nil
    candidateSince = nil
    restartSuggested = 0
    if restartPhase != "needs-attention" { restartPhase = "ready" }
    do { try saveRestartState() } catch {
      restartFailed("Could not save account-switch status.")
      return
    }
    if restartOnSwitch {
      // Pins and groups can only be carried over with Claude closed.
      showStatus("Account changed. Restarting Claude once to bring your pins and groups.", banner: true)
      requestRestart(mode: "after-account-switch")
      return
    }
    // Chats were kept up to date while Claude was open, so a sync is enough.
    showStatus("Account changed. Syncing chats.")
    startAutoIfNeeded()
  }

  @objc private func restartClicked(_ sender: Any?) {
    queue.async { [weak self] in self?.requestRestart(mode: nil) }
  }

  // A request made while a sync runs waits for it instead of being dropped.
  private func requestRestart(mode: String?) {
    guard !restartRequested, profile != nil else { return }
    if running {
      restartPending = true
      if mode != nil { pendingRestartMode = mode }
      showStatus("Restart queued. It starts when the current sync finishes.")
      return
    }
    beginRestart(mode: mode)
  }

  // Quit Claude once, sync everything, and reopen it. Only on request.
  // A busy moment is retried a few times instead of dropping the request.
  private func beginRestart(attempt: Int = 1, mode: String? = nil) {
    guard !restartRequested, profile != nil else { return }
    if running {
      restartPending = true
      if mode != nil { pendingRestartMode = mode }
      return
    }
    DispatchQueue.main.async { [weak self] in
      guard let self = self else { return }
      let applications = NSWorkspace.shared.runningApplications.filter {
        $0.executableURL?.standardizedFileURL.path == self.claudeExecutable
      }
      self.queue.async {
        guard !self.restartRequested else { return }
        if self.running {
          self.restartPending = true
          if mode != nil { self.pendingRestartMode = mode }
          return
        }
        guard !applications.isEmpty else {
          if attempt < 3 {
            self.queue.asyncAfter(deadline: .now() + 1) { [weak self] in
              self?.beginRestart(attempt: attempt + 1, mode: mode)
            }
            return
          }
          self.noteRestartSkipped("claude-not-open")
          self.showStatus("Claude is not open. It will show every chat when it starts.", banner: true)
          return
        }
        let approvedPIDs: Set<pid_t>
        switch self.restartPIDs() {
        case .ready(let pids):
          approvedPIDs = pids
        case .retryable:
          if attempt < 3 {
            self.queue.asyncAfter(deadline: .now() + 2) { [weak self] in
              self?.beginRestart(attempt: attempt + 1, mode: mode)
            }
            return
          }
          self.noteRestartSkipped("process-check-busy")
          self.showStatus("Mac is busy. Try the restart again in a moment.", banner: true)
          return
        case .blocked:
          self.restartFailed("Could not confirm the default Claude profile. Claude was left open.")
          return
        }
        let targets = applications.filter { approvedPIDs.contains($0.processIdentifier) }
        guard !targets.isEmpty else {
          self.restartFailed("Could not confirm the default Claude profile. Claude was left open.")
          return
        }
        self.restartPhase = "quitting"
        do { try self.saveRestartState() } catch {
          self.restartFailed("Could not save the restart guard. Claude was left open.")
          return
        }
        self.restartRequested = true
        self.restartMode = mode
        let attempt = UUID()
        self.restartAttempt = attempt
        self.quittingPIDs = Set(targets.map { $0.processIdentifier })
        self.writeStatus(exitStatus: 0, output: Data("{\"progress\":\"quitting-Claude\"}".utf8), launchFailed: false)
        self.notify("Restarting Claude to load new chats.")
        // Request a normal quit only for the configured executable, never force-kill.
        DispatchQueue.main.async {
          for application in targets { _ = application.terminate() }
        }
        self.queue.asyncAfter(deadline: .now() + 30) {
          if self.restartRequested && self.restartPhase == "quitting" && self.restartAttempt == attempt {
            self.restartFailed("Claude did not close. Finish any quit prompt, then try again.")
          }
        }
      }
    }
  }

  private func noteRestartSkipped(_ reason: String) {
    let data = (try? JSONSerialization.data(withJSONObject: ["progress": "finished", "restart_skipped": reason])) ?? Data()
    writeStatus(exitStatus: 0, output: data, launchFailed: false)
  }

  private func restartFailed(_ message: String, reason: String = "restart-failed") {
    restartRequested = false
    restartPhase = "needs-attention"
    try? saveRestartState()
    let data = (try? JSONSerialization.data(withJSONObject: ["progress": "needs-attention", "reason": reason])) ?? Data()
    writeStatus(exitStatus: 1, output: data, launchFailed: false)
    notify("Sync needs attention. " + message)
  }

  func requestAuto(afterQuit: Bool = false) {
    queue.async { [weak self] in
      if afterQuit {
        self?.retryDeadline = Date().addingTimeInterval(30)
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
    let restarting = restartRequested && profile != nil
    task.executableURL = URL(fileURLWithPath: executable)
    if restarting, let profile = profile {
      restartPhase = "syncing"
      do { try saveRestartState() } catch {
        running = false
        restartFailed("Could not save sync status. Claude has not been reopened.")
        return
      }
      var arguments = ["switch", profile, "--wait-for-exit", "30", "--json"]
      if let mode = restartMode { arguments.append("--" + mode) }
      task.arguments = Array(command.dropFirst()) + arguments
      writeStatus(exitStatus: 0, output: Data("{\"progress\":\"syncing\"}".utf8), launchFailed: false)
      showStatus("Syncing, then reopening Claude")
    } else {
      task.arguments = Array(command.dropFirst()) + ["auto", "--json"]
    }
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
          self?.finishAuto(exitStatus: task.terminationStatus, output: captured, restarting: restarting)
        }
      }
    } catch {
      writeStatus(exitStatus: 127, output: Data(), launchFailed: true)
      notify("Sync needs attention. The sync tool could not start.")
      NSLog("claude-session-sync watcher could not start auto")
      running = false
      if restarting { restartFailed("The sync tool could not start. Claude has not been reopened.") }
      runPendingIfNeeded()
    }
  }

  private func finishAuto(exitStatus: Int32, output: Data, restarting: Bool) {
    let result = (try? JSONSerialization.jsonObject(with: output)) as? [String: Any]
    let reason = result?["reason"] as? String
    let progress = result?["progress"] as? String
    let suggested = result?["restart_suggested"] as? Int ?? 0
    let hadSuggestion = restartSuggested > 0
    running = false
    if restarting {
      restartRequested = false
      restartMode = nil
    }
    if restarting && (exitStatus != 0 || progress != "finished") {
      restartFailed("Sync needs attention. Claude was not automatically reopened.")
      runPendingIfNeeded()
      return
    }
    if exitStatus == 0 && progress == "finished" {
      restartSuggested = restarting ? 0 : max(0, suggested)
      restartPhase = "finished"
      if let hash = readAccountHash() { accountHash = hash }
      do { try saveRestartState() } catch {
        restartFailed("Sync finished, but its account-switch receipt could not be saved.")
        return
      }
    }
    writeStatus(exitStatus: result == nil ? 1 : exitStatus, output: output, launchFailed: false)
    if reason == "app-running" && NSWorkspace.shared.runningApplications.contains(where: {
      $0.executableURL?.standardizedFileURL.path == claudeExecutable
    }) {
      retryDeadline = nil
      showStatus(restartPhase == "needs-attention" ? "Sync needs attention" : "Sync ready, Claude open")
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
      reportFailure("Sync is still waiting. Run claude-session-sync status for details.", key: "waiting")
    } else if exitStatus == 0 && progress == "finished" {
      lastFailureKey = nil
      if restarting {
        notify("Sync finished. Claude reopened.")
      } else if restartSuggested > 0 {
        let chats = restartSuggested == 1 ? "1 new chat" : "\(restartSuggested) new chats"
        showStatus("\(chats) will show after Claude restarts.", banner: !hadSuggestion)
      } else {
        showStatus("Sync finished")
      }
    } else if exitStatus != 0 || progress == "needs-attention" || result == nil {
      reportFailure("Sync needs attention. Run claude-session-sync doctor for details.",
        key: "\(exitStatus)|\(reason ?? "")|\(result?["error_type"] as? String ?? "")")
    }
    retryDeadline = nil
    if exitStatus != 0 {
      NSLog("claude-session-sync auto failed with exit status %d", exitStatus)
    }
    if restartPending && !restarting {
      // The restart syncs everything itself, so a queued sync is not needed.
      restartPending = false
      pending = false
      let mode = pendingRestartMode
      pendingRestartMode = nil
      beginRestart(mode: mode)
      return
    }
    runPendingIfNeeded()
  }

  // Syncs run every few seconds; the same failure shows its banner only once.
  private func reportFailure(_ message: String, key: String) {
    let repeated = key == lastFailureKey
    lastFailureKey = key
    showStatus(message, banner: !repeated)
  }

  private func notify(_ message: String) {
    showStatus(message, banner: true)
  }

  private func showStatus(_ message: String, banner: Bool = false) {
    if ProcessInfo.processInfo.environment["CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS"] == "1" {
      return
    }
    let offerRestart = restartSuggested > 0 && profile != nil
    if message == lastMessage && !banner { return }
    lastMessage = message
    DispatchQueue.main.async { [weak self] in
      guard let self = self else { return }
      if self.statusItem == nil {
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
      }
      self.statusItem?.button?.title = message.contains("attention") ? "Sync !" :
        offerRestart ? "Sync ↻" : message.contains("finished") ? "Sync ✓" : "Sync"
      self.statusItem?.button?.toolTip = message
      let menu = NSMenu()
      let item = NSMenuItem(title: message, action: nil, keyEquivalent: "")
      menu.addItem(item)
      if offerRestart {
        let restart = NSMenuItem(title: "Restart Claude now", action: #selector(SessionSyncWatcher.restartClicked(_:)),
          keyEquivalent: "")
        restart.target = self
        menu.addItem(restart)
      }
      self.statusItem?.menu = menu
      guard banner else { return }
      self.noticeGeneration += 1
      let generation = self.noticeGeneration
      self.noticePanel?.close()
      let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 370, height: 92),
        styleMask: [.titled, .nonactivatingPanel], backing: .buffered, defer: false)
      panel.title = "Claude Session Sync"
      panel.level = .floating
      panel.hidesOnDeactivate = false
      panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
      let label = NSTextField(wrappingLabelWithString: offerRestart ? message + " Use the Sync menu to restart." : message)
      label.frame = NSRect(x: 18, y: 12, width: 334, height: 60)
      label.font = .systemFont(ofSize: 14)
      panel.contentView?.addSubview(label)
      if let screen = NSScreen.main {
        panel.setFrameTopLeftPoint(NSPoint(x: screen.visibleFrame.maxX - 390, y: screen.visibleFrame.maxY - 18))
      }
      self.noticePanel = panel
      panel.orderFrontRegardless()
      DispatchQueue.main.asyncAfter(deadline: .now() + 10) {
        if self.noticeGeneration == generation { self.noticePanel?.orderOut(nil) }
      }
    }
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
      "restart_phase": restartPhase,
      "restart_suggested": restartSuggested,
      "automatic_restart": false,
      "live_sync": accountURL != nil,
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
    let pid = application.processIdentifier
    queue.async { [weak self] in
      guard let self = self else { return }
      if executableMatches || knownBundle || self.quittingPIDs.contains(pid) {
        self.quittingPIDs.remove(pid)
        // Claude reads every folder again at start, so a restart suggestion is spent.
        if !self.restartRequested { self.restartSuggested = 0 }
        self.requestAuto(afterQuit: true)
      }
    }
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
var accountPath: String?
var profile: String?
var restartOnSwitch = false
var index = 0
while index < watcherArguments.count {
  guard index + 1 < watcherArguments.count else {
    fputs("SessionSyncWatcher option requires a value\n", stderr)
    exit(64)
  }
  switch watcherArguments[index] {
  case "--claude-executable": claudeExecutable = watcherArguments[index + 1]
  case "--status": statusPath = watcherArguments[index + 1]
  case "--account-file": accountPath = watcherArguments[index + 1]
  case "--profile": profile = watcherArguments[index + 1]
  case "--restart-on-switch": restartOnSwitch = watcherArguments[index + 1] == "1"
  default:
    fputs("SessionSyncWatcher received an unknown option\n", stderr)
    exit(64)
  }
  index += 2
}
let command = Array(CommandLine.arguments.dropFirst(separator + 1))
guard let configuredExecutable = claudeExecutable,
  let configuredStatusPath = statusPath, !command.isEmpty,
  (accountPath == nil) == (profile == nil)
else {
  fputs("SessionSyncWatcher requires executable, status, and command arguments\n", stderr)
  exit(64)
}
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
private let watcher = SessionSyncWatcher(
  command: command, claudeExecutable: configuredExecutable,
  statusURL: URL(fileURLWithPath: configuredStatusPath),
  accountURL: accountPath.map { URL(fileURLWithPath: $0) }, profile: profile,
  restartOnSwitch: restartOnSwitch
)
NSWorkspace.shared.notificationCenter.addObserver(
  watcher, selector: #selector(SessionSyncWatcher.applicationTerminated(_:)),
  name: NSWorkspace.didTerminateApplicationNotification, object: nil
)
watcher.requestAuto()
application.run()
