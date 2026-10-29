import AppKit
import Combine
import Darwin
import Foundation

/// Runs the ConnectOnion Studio server as a child process and hands the window a URL once it's up.
///
/// The SERVER is the sole port authority — Swift never picks a port (that would reintroduce a
/// bind/close/re-bind TOCTOU race). Swift launches it with `--free-port --port-file <path>`; the
/// server binds a free loopback port, LISTENS, then atomically writes `http://127.0.0.1:<port>` to
/// <path>. Swift reads that URL, runs the readiness probe, and only then loads `<url>/?desktop=1`.
/// Because the frontend is same-origin (relative REST + `location.host` for the WS), the port
/// propagates with no JS change and there is no hardcoded 9900 to drift.
///
/// DEV: point the shell at a local server via env vars (either works):
///   - `CO_STUDIO_PYTHON` = /abs/.venv/bin/python    (launched as `python -m co_studio …`)
///   - `CO_STUDIO_BIN`    = /abs/.venv/bin/co-studio  (the console script)
/// SHIP: a relocatable CPython is bundled at `Resources/python/bin/python3` and launched as
///       `python3 -m co_studio …` (see macos/README.md). We launch the interpreter DIRECTLY so the
///       PID we hold is the real server — SIGTERM reaches uvicorn and runs its shutdown hook.
@MainActor
final class StudioServer: ObservableObject {

    /// Startup phase, driven onto the main actor for the SwiftUI view to switch on.
    enum Phase: Equatable {
        case starting
        case running(URL)      // the ?desktop=1 URL to load
        case failed(String)    // user-facing reason; full detail is in the log file
    }

    /// One server per app: the AppDelegate reaches this from `applicationWillTerminate`, and the
    /// window's view drives `start()` — both must see the same instance so teardown is guaranteed.
    static let shared = StudioServer()

    @Published private(set) var phase: Phase = .starting

    /// Where the child's stdout+stderr are captured, so launch failures are diagnosable (Copy logs).
    private(set) var logFileURL: URL?

    private var process: Process?
    private var logHandle: FileHandle?
    private var portFileURL: URL?
    /// Bumped on every (re)launch; async work carrying a stale value is ignored after a Retry.
    private var generation = 0
    private var isStopping = false

    private static let startupTimeout: TimeInterval = 30   // port-file + readiness, total
    private static let quitGrace: TimeInterval = 0.5       // brief SIGTERM window on quit, then SIGKILL

    static var isPreview: Bool {
        Bundle.main.object(forInfoDictionaryKey: "COStudioIsolatedPreview") as? Bool == true
    }

    /// Preview apps never inherit the normal Agent directory, credentials or database URLs.
    private static var previewDataURL: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return support.appendingPathComponent("ConnectOnionStudioPreviews")
            .appendingPathComponent(Bundle.main.bundleIdentifier ?? "isolated-preview")
    }

    private init() {}

    // MARK: - Lifecycle

    /// Launch the server if it isn't already up. Safe to call more than once (onAppear may repeat).
    func start() {
        guard process == nil else { return }
        launch()
    }

    /// User pressed Retry after a launch failure: drop the old process and try again.
    func retry() {
        stopProcessIfRunning()   // late exit of the old process is ignored via the generation guard
        phase = .starting
        launch()
    }

    /// Blocking teardown for `applicationWillTerminate`. Send SIGTERM (uvicorn exits cleanly if it can),
    /// but only wait a brief grace before SIGKILL: the dashboard's WebSocket can hold uvicorn's graceful
    /// shutdown for its full timeout, and the server has no persistent state to flush — so waiting it out
    /// would just leave the app process (and its Dock icon) lingering seconds after the window closed.
    ///
    /// DESIGN: quit does NOT kill agents (they're independent host() servers; only a manual Stop does),
    /// so there's genuinely nothing to flush here. If you ever add kill-agents-on-quit, this short grace
    /// must grow to cover it — agents need SIGTERM + time to flush their logs (see `_lifespan` in app.py).
    func shutdownForQuit() {
        isStopping = true
        defer { closeLog(); cleanupPortFile() }
        guard let proc = process, proc.isRunning else { process = nil; return }
        proc.terminate()   // SIGTERM: a clean exit if the server can manage one within the grace
        let deadline = Date().addingTimeInterval(Self.quitGrace)
        while proc.isRunning && Date() < deadline { usleep(25_000) }   // 25ms
        if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }    // else force it, so quit is prompt
        process = nil
    }

    /// Copy the captured server log to the pasteboard (the error screen's "Copy logs").
    func copyLogs() {
        let text: String
        if let url = logFileURL, let contents = try? String(contentsOf: url, encoding: .utf8), !contents.isEmpty {
            text = contents
        } else {
            text = "(no server output was captured)"
        }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    // MARK: - Launch

    private func launch() {
        generation &+= 1
        let gen = generation
        isStopping = false

        // A fresh, unique handshake file per launch — so we never read a stale port from a prior run.
        let portFile = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("co-studio-port-\(UUID().uuidString).txt")
        try? FileManager.default.removeItem(at: portFile)
        portFileURL = portFile

        let logURL = Self.makeLogFileURL()
        logFileURL = logURL
        prepareLogFile(logURL)

        // --free-port: server picks a free loopback port and confirms it via the port-file.
        // --no-browser: we render the dashboard in WKWebView, not a browser tab.
        // NOTE: --kill-agents-on-exit is a planned flag but is NOT implemented server-side yet, so
        //   we don't pass it (argparse would reject it). Agents survive quit and are re-adopted on
        //   next launch, same as the CLI. Wire the flag through app.py's lifespan to enable it.
        let studioArgs = ["--free-port", "--port-file", portFile.path, "--no-browser"]
        let (executable, arguments) = resolveLaunch(studioArgs: studioArgs)

        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments
        if Self.isPreview {
            let directory = Self.previewDataURL
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            } catch {
                phase = .failed("Couldn't create isolated preview data directory.")
                return
            }
            // Preserve the user's real home; use explicit application paths instead of spoofing HOME.
            let allowed = Set(["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"])
            var environment = ProcessInfo.processInfo.environment.filter { allowed.contains($0.key) }
            environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            environment["STUDIO_DATA_DIR"] = directory.appendingPathComponent("studio").path
            environment["AGENT_CONFIG_PATH"] = directory.appendingPathComponent("agent-config").path
            environment["STUDIO_AUTO_AUTH"] = "0"
            environment["PYTHONNOUSERSITE"] = "1"
            proc.environment = environment
            proc.currentDirectoryURL = directory
            appendLog("Isolated preview data: \(directory.path)\n")
        }
        if let handle = logHandle {
            proc.standardOutput = handle
            proc.standardError = handle
        }
        // Called on an arbitrary thread once the child exits. Read the status here (Sendable Int32)
        // and hop to the main actor — don't capture self or the non-Sendable Process across threads.
        proc.terminationHandler = { finished in
            let code = finished.terminationStatus
            Task { @MainActor in StudioServer.shared.processDidExit(code: code, generation: gen) }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            appendLog("Failed to launch \(executable.path): \(error)\n")
            phase = .failed("Couldn't launch the studio server (\(executable.lastPathComponent)). \(error.localizedDescription)")
            return
        }

        Task { [weak self] in
            await self?.bringUp(portFile: portFile, process: proc, generation: gen)
        }
    }

    /// Resolve which binary to run and the leading args. Preference order keeps the held PID a real
    /// interpreter/server (bundled → dev interpreter → dev console-script → PATH fallback).
    private func resolveLaunch(studioArgs: [String]) -> (URL, [String]) {
        let env = ProcessInfo.processInfo.environment

        // 1) SHIP: bundled relocatable interpreter — `python3 -m co_studio …`.
        if let resources = Bundle.main.resourceURL {
            let bundledPython = resources.appendingPathComponent("python/bin/python3")
            if FileManager.default.isExecutableFile(atPath: bundledPython.path) {
                return (bundledPython, ["-m", "co_studio"] + studioArgs)
            }
        }
        // 2) DEV: an interpreter that can import co_studio (e.g. the venv python).
        if let python = env["CO_STUDIO_PYTHON"], !python.isEmpty {
            return (URL(fileURLWithPath: python), ["-m", "co_studio"] + studioArgs)
        }
        // 3) DEV: the co-studio console script by absolute path (its shebang python becomes the child).
        if let bin = env["CO_STUDIO_BIN"], !bin.isEmpty {
            return (URL(fileURLWithPath: bin), studioArgs)
        }
        // 4) Fallback: co-studio on PATH (env exec's it in place, so the PID is still the server).
        return (URL(fileURLWithPath: "/usr/bin/env"), ["co-studio"] + studioArgs)
    }

    /// Wait for the server to announce its address (port-file) and become ready, then publish the URL.
    private func bringUp(portFile: URL, process proc: Process, generation gen: Int) async {
        let deadline = Date().addingTimeInterval(Self.startupTimeout)

        // Phase 1: wait for the atomic (temp+rename) port-file write, so we only see a complete URL.
        var base: URL?
        while Date() < deadline {
            if gen != generation { return }
            if !proc.isRunning { return }   // processDidExit sets the failure message
            if let url = Self.readPortFile(portFile) { base = url; break }
            try? await Task.sleep(nanoseconds: 120_000_000)   // 0.12s
        }
        guard gen == generation else { return }
        guard let baseURL = base else {
            appendLog("Timed out waiting for port-file \(portFile.path).\n")
            phase = .failed("The studio server didn't report its address in time.")
            stopProcessIfRunning()
            return
        }

        // Phase 2: confirm it's actually serving before we load it (bound != serving yet).
        let probe = baseURL.appendingPathComponent("api/agents")
        let ready = await Self.waitUntilServing(probe, deadline: deadline)
        guard gen == generation else { return }
        if ready {
            phase = .running(Self.desktopURL(baseURL))
        } else if proc.isRunning {
            appendLog("Server at \(baseURL.absoluteString) never answered /api/agents within the timeout.\n")
            phase = .failed("The studio server started but never became ready.")
            stopProcessIfRunning()
        }
        // else: the process died; processDidExit already reported it.
    }

    private func processDidExit(code: Int32, generation gen: Int) {
        // The generation guard already means this is the current launch's process, so no identity
        // check is needed. isStopping means WE terminated it (quit/retry) — not a failure to report.
        guard gen == generation, !isStopping else { return }
        switch phase {
        case .running:
            phase = .failed("The studio server stopped unexpectedly (exit \(code)). See logs for details.")
        case .starting:
            phase = .failed("The studio server exited during startup (exit \(code)). See logs for details.")
        case .failed:
            break
        }
        process = nil
    }

    private func stopProcessIfRunning() {
        if let proc = process, proc.isRunning { proc.terminate() }
        process = nil
    }

    private func cleanupPortFile() {
        if let portFile = portFileURL { try? FileManager.default.removeItem(at: portFile) }
        portFileURL = nil
    }

    // MARK: - Handshake + readiness (pure / off-actor helpers)

    /// Read the confirmed base URL the server wrote (loopback http only); nil until it's valid.
    nonisolated private static func readPortFile(_ portFile: URL) -> URL? {
        guard let text = try? String(contentsOf: portFile, encoding: .utf8) else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let url = URL(string: trimmed),
              url.scheme == "http",
              let host = url.host,
              host == "127.0.0.1" || host == "localhost" || host == "::1"
        else { return nil }
        return url
    }

    /// `http://host:port/?desktop=1` — same origin, so REST/WS wiring is unchanged.
    nonisolated private static func desktopURL(_ base: URL) -> URL {
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false) ?? URLComponents()
        components.path = "/"
        components.queryItems = [URLQueryItem(name: "desktop", value: "1")]
        return components.url ?? base
    }

    /// Poll the API until it answers 200, so the window only loads once the server is really serving.
    nonisolated private static func waitUntilServing(_ probe: URL, deadline: Date) async -> Bool {
        var request = URLRequest(url: probe)
        request.timeoutInterval = 1
        while Date() < deadline {
            if let (_, response) = try? await URLSession.shared.data(for: request),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                return true
            }
            try? await Task.sleep(nanoseconds: 200_000_000)   // 0.2s
        }
        return false
    }

    // MARK: - Log capture

    private func prepareLogFile(_ url: URL) {
        closeLog()
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: url.path, contents: nil)   // truncate for a clean run
        logHandle = try? FileHandle(forWritingTo: url)
    }

    private func appendLog(_ message: String) {
        guard let handle = logHandle, let data = message.data(using: .utf8) else { return }
        try? handle.write(contentsOf: data)
    }

    private func closeLog() {
        try? logHandle?.close()
        logHandle = nil
    }

    // MARK: - Paths

    nonisolated private static func makeLogFileURL() -> URL {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let folder = Bundle.main.object(forInfoDictionaryKey: "COStudioIsolatedPreview") as? Bool == true
            ? "ConnectOnionStudioPreviews/\(Bundle.main.bundleIdentifier ?? "isolated-preview")"
            : "ConnectOnionStudio"
        return base.appendingPathComponent(folder).appendingPathComponent("server.log")
    }
}
