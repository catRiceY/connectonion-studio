import AppKit
import SwiftUI
import WebKit
import Sparkle

/// Process-lifetime presentation state. It resets on a real app launch, but survives a closed and
/// recreated SwiftUI window so Dock reopen never replays the cold-start welcome animation.
@MainActor
private enum NativeLaunchPresentation {
    static var hasLoadedInitialPage = false
}

/// A WKWebView that fills the window and shows the studio's existing web UI.
struct WebView: NSViewRepresentable {
    let url: URL
    let appearance: StudioAppearance
    var onReady: () -> Void = {}
    var onAppearanceChange: (StudioAppearance) -> Void = { _ in }

    func makeCoordinator() -> Coordinator {
        Coordinator(onReady: onReady, onAppearanceChange: onAppearanceChange)
    }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        if StudioServer.isPreview {
            configuration.websiteDataStore = .nonPersistent()
        }
        // Same-origin loopback app; JS on. ATS permits the numeric loopback 127.0.0.1 without an
        // exception (only the "localhost" hostname would need NSAllowsLocalNetworking — see README).
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true

        // --- Sparkle bridge -------------------------------------------------------------------
        // Tell the web UI it's running inside the native app (so it shows "Relaunch to update"
        // instead of the pip/pipx command), and expose a tiny JS API to drive Sparkle.
        let skipSplash = NativeLaunchPresentation.hasLoadedInitialPage ? "true" : "false"
        let bridge = """
        window.__coStudioNative = true;
        window.__coStudioSkipSplash = \(skipSplash);
        window.__coStudio = {
          installUpdate: function () { window.webkit.messageHandlers.coStudio.postMessage({ action: 'installUpdate' }); },
          dismissUpdate: function () { window.webkit.messageHandlers.coStudio.postMessage({ action: 'dismissUpdate' }); },
          checkForUpdates: function () { window.webkit.messageHandlers.coStudio.postMessage({ action: 'checkForUpdates' }); },
          syncUpdate: function () { window.webkit.messageHandlers.coStudio.postMessage({ action: 'syncUpdate' }); },
          cancelUpdate: function () { window.webkit.messageHandlers.coStudio.postMessage({ action: 'cancelUpdate' }); },
          setAppearance: function (appearance) { window.webkit.messageHandlers.coStudio.postMessage({ action: 'setAppearance', appearance: appearance }); },
          copyText: function (text) { window.webkit.messageHandlers.coStudio.postMessage({ action: 'copyText', text: text }); }
        };
        """
        let script = WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        configuration.userContentController.addUserScript(script)
        configuration.userContentController.add(context.coordinator, name: "coStudio")

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        // Paper base (CSS --canvas) so the load-in moment shows brand color, not a white flash — matches
        // the window + spinner. Public API (macOS 12+), replaces the old private `drawsBackground` KVC.
        webView.underPageBackgroundColor = appearance.canvasNSColor
        WebUpdater.shared.webView = webView   // let Sparkle push update state into this page
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        context.coordinator.onReady = onReady   // keep the callback fresh across view updates
        context.coordinator.onAppearanceChange = onAppearanceChange
        webView.underPageBackgroundColor = appearance.canvasNSColor
        WebUpdater.shared.webView = webView
        if webView.url != url {
            webView.load(URLRequest(url: url))
        }
    }

    /// Keeps in-window navigation on our loopback origin and routes everything else to the system
    /// browser. WKWebView drops `target=_blank` / `window.open` by default, so those links (GitHub,
    /// "Powered by OpenOnion", onboarding docs) are dead without this handler.
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        /// Fired once the page finishes loading (so it has painted its paper background) — the cue to
        /// fade the paper cover, so the WKWebView's white loading document is never seen.
        var onReady: () -> Void
        var onAppearanceChange: (StudioAppearance) -> Void
        init(onReady: @escaping () -> Void,
             onAppearanceChange: @escaping (StudioAppearance) -> Void) {
            self.onReady = onReady
            self.onAppearanceChange = onAppearanceChange
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            NativeLaunchPresentation.hasLoadedInitialPage = true
            onReady()
        }

        /// JS → Swift: the web "Relaunch to update" button routes through here to Sparkle.
        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "coStudio",
                  let body = message.body as? [String: Any],
                  let action = body["action"] as? String else { return }
            switch action {
            case "installUpdate":    WebUpdater.shared.installAndRelaunch()
            case "dismissUpdate":    WebUpdater.shared.dismiss()
            case "checkForUpdates":  WebUpdater.shared.checkForUpdates()
            case "syncUpdate":       WebUpdater.shared.syncState()
            case "cancelUpdate":     WebUpdater.shared.cancelActiveUpdate()
            case "copyText":
                guard let text = body["text"] as? String else { return }
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            case "setAppearance":
                guard let raw = body["appearance"] as? String,
                      let appearance = StudioAppearance(rawValue: raw) else { return }
                onAppearanceChange(appearance)
            default:                 break
            }
        }

        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else { decisionHandler(.allow); return }
            if Coordinator.staysInApp(url) {
                decisionHandler(.allow)
            } else {
                Coordinator.openExternally(url)
                decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView,
                     createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction,
                     windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url {
                if Coordinator.isLoopback(url) {
                    webView.load(navigationAction.request)   // an internal target=_blank stays in-window
                } else {
                    Coordinator.openExternally(url)          // external → system browser
                }
            }
            return nil   // never spawn a second WKWebView window
        }

        private static func staysInApp(_ url: URL) -> Bool {
            switch url.scheme?.lowercased() {
            case "http", "https", "ws", "wss": return isLoopback(url)
            case "about", "blob", "data":      return true   // WebKit internals
            default:                           return false
            }
        }

        private static func isLoopback(_ url: URL) -> Bool {
            switch url.host {
            case "127.0.0.1", "localhost", "::1": return true
            default:                              return false
            }
        }

        private static func openExternally(_ url: URL) {
            switch url.scheme?.lowercased() {
            case "about", "blob", "data", .none: return   // nothing sensible to hand off
            default: NSWorkspace.shared.open(url)
            }
        }
    }
}

// MARK: - Sparkle, driven by the web UI

/// Custom Sparkle `SPUUserDriver`: Sparkle does all the real work (check → download → verify →
/// install → relaunch); we suppress its native windows and instead push update state into the web UI
/// and gate the final install+relaunch on the page's "Relaunch to update" button.
@MainActor
final class WebUpdater: NSObject, SPUUserDriver {
    static let shared = WebUpdater()

    /// The live page; set by `WebView`. Used to push update state via `window.__coStudioUpdate(...)`.
    weak var webView: WKWebView?

    private var updater: SPUUpdater?
    /// The most recent "should I proceed?" reply from Sparkle — fired when the web clicks Relaunch.
    private var pendingReply: ((SPUUserUpdateChoice) -> Void)?
    private var latestVersion = ""
    /// Last status pushed to the web; replayed via syncState() when the page loads after a launch check.
    private var lastStatus = "idle"
    /// Download accounting so the web can show a real progress bar during the blocking "updating" cover.
    private var expectedLength: UInt64 = 0
    private var receivedLength: UInt64 = 0
    /// Sparkle's cancellation block for the in-flight check/download — lets the web abort a stalled
    /// download (e.g. no network) so it never hangs the blocking cover at 0%.
    private var activeCancellation: (() -> Void)?
    /// True once the user hit "Relaunch to update" — makes download → install → relaunch one action.
    private var committed = false

    private override init() { super.init() }

    /// Create + start the updater with THIS object as the user driver. Automatic background checks +
    /// downloads come from Info.plist (SUFeedURL / SUEnableAutomaticChecks / SUAutomaticallyUpdate).
    func start() {
        // Local preview bundles must never fetch or install a production update.
        guard !StudioServer.isPreview else { return }
        guard updater == nil else { return }
        let u = SPUUpdater(hostBundle: .main, applicationBundle: .main, userDriver: self, delegate: nil)
        do {
            try u.start()
            updater = u
            // The scheduler alone doesn't surface anything promptly (default interval + no first-launch
            // check), so force a background check immediately after start() — Sparkle's documented way to
            // "check on every app launch" (automatic checks are already enabled via SUEnableAutomaticChecks
            // in Info.plist; don't re-set that property here or it resets the cycle and drops this check).
            // Result is pushed to the web, or replayed via syncState() once the page registers its handler.
            u.checkForUpdatesInBackground()
        } catch {
            NSLog("[Sparkle] startUpdater failed: \(error.localizedDescription)")
        }
    }

    func checkForUpdates() { updater?.checkForUpdates() }

    /// Web clicked "Relaunch to update".
    func installAndRelaunch() {
        committed = true
        if let reply = pendingReply {
            reply(.install)
            pendingReply = nil
        } else if let u = updater, !u.sessionInProgress {
            // No live reply — a previously dismissed/finished update. Re-check, but ONLY when no session
            // is in progress: firing while a prior (e.g. failed) session is still tearing down silently
            // no-ops and hangs the cover at 0%. `committed` stays set so showUpdateFound goes straight in.
            u.checkForUpdatesInBackground()
        }
    }

    /// Web dismissed the update.
    func dismiss() {
        committed = false
        pendingReply?(.dismiss)
        pendingReply = nil
    }

    // MARK: push state → web

    private func push(_ status: String, version: String? = nil, progress: Double? = nil) {
        lastStatus = status
        var payload: [String: Any] = ["status": status, "version": version ?? latestVersion]
        if let progress = progress { payload["progress"] = progress }
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return }
        webView?.evaluateJavaScript("window.__coStudioUpdate && window.__coStudioUpdate(\(json))", completionHandler: nil)
    }

    /// Re-push the last status — the web calls this on load so an update found before the page was ready
    /// (the launch race) still reaches the banner.
    func syncState() { push(lastStatus) }

    // MARK: SPUUserDriver

    func show(_ request: SPUUpdatePermissionRequest, reply: @escaping (SUUpdatePermissionResponse) -> Void) {
        reply(SUUpdatePermissionResponse(automaticUpdateChecks: true, sendSystemProfile: false))
    }

    func showUserInitiatedUpdateCheck(cancellation: @escaping () -> Void) {
        activeCancellation = cancellation
        push("checking")
    }

    /// Web asks to abort a stalled check/download (its stall watchdog fired). Cancelling makes Sparkle
    /// tear the session down (→ dismissUpdateInstallation → "idle"), so the web can show a clean error.
    func cancelActiveUpdate() {
        activeCancellation?()
        activeCancellation = nil
    }

    func showUpdateFound(with appcastItem: SUAppcastItem, state: SPUUserUpdateState, reply: @escaping (SPUUserUpdateChoice) -> Void) {
        latestVersion = appcastItem.displayVersionString
        if committed {
            reply(.install)   // already committed (a retry after a failed download) — go straight to it
            return
        }
        pendingReply = reply
        // If Sparkle already downloaded/extracted it (e.g. resuming after a relaunch), the web can offer
        // "Relaunch to update" straight away; otherwise the click downloads first, then relaunches.
        push(state.stage == .downloaded || state.stage == .installing ? "readyToRelaunch" : "available")
    }

    func showUpdateReleaseNotes(with downloadData: SPUDownloadData) {}

    func showUpdateReleaseNotesFailedToDownloadWithError(_ error: Error) {}

    func showUpdateNotFoundWithError(_ error: Error) async { committed = false; push("none") }

    func showUpdaterError(_ error: Error) async { committed = false; push("error") }

    func showDownloadInitiated(cancellation: @escaping () -> Void) {
        activeCancellation = cancellation
        expectedLength = 0; receivedLength = 0
        push("downloading")
    }

    func showDownloadDidReceiveExpectedContentLength(_ expectedContentLength: UInt64) {
        expectedLength = expectedContentLength; receivedLength = 0
    }

    func showDownloadDidReceiveData(ofLength length: UInt64) {
        receivedLength += length
        guard expectedLength > 0 else { return }
        push("downloading", progress: Double(receivedLength) / Double(expectedLength))
    }

    func showDownloadDidStartExtractingUpdate() { push("installing") }

    func showExtractionReceivedProgress(_ progress: Double) { push("installing") }   // feed the web's stall watchdog during extraction

    func showReady(toInstallAndRelaunch reply: @escaping (SPUUserUpdateChoice) -> Void) {
        if committed {
            reply(.install)   // user already chose to relaunch — finish without a second click
        } else {
            pendingReply = reply
            push("readyToRelaunch")
        }
    }

    func showInstallingUpdate(withApplicationTerminated applicationTerminated: Bool, retryTerminatingApplication: @escaping () -> Void) {
        push("installing")
    }

    func showUpdateInstalledAndRelaunched(_ relaunched: Bool) async {}

    func dismissUpdateInstallation() {
        committed = false
        pendingReply = nil
        activeCancellation = nil
        expectedLength = 0; receivedLength = 0
        push("idle")
    }
}
