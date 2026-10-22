import AppKit
import WebKit

final class SpiritboardApp: NSObject, NSApplicationDelegate, WKNavigationDelegate,
    WKUIDelegate, WKScriptMessageHandler {
    private let headerColor = NSColor(
        srgbRed: 17.0 / 255, green: 14.0 / 255, blue: 26.0 / 255, alpha: 1
    )
    private let boardColor = NSColor(
        srgbRed: 13.0 / 255, green: 11.0 / 255, blue: 20.0 / 255, alpha: 1
    )
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Process?
    private var origin: URL?
    private var output = Data()
    private var ready = false
    private var popupWindows = [ObjectIdentifier: NSWindow]()

    func applicationDidFinishLaunching(_ notification: Notification) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)

        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(self, name: "titlebar")
        configuration.userContentController.addUserScript(WKUserScript(
            source: """
                const reportTitlebar = () => {
                  const header = document.querySelector('.top');
                  if (header) window.webkit.messageHandlers.titlebar.postMessage(
                    header.getBoundingClientRect().bottom > 0);
                };
                addEventListener('scroll', reportTitlebar, {passive: true});
                addEventListener('load', reportTitlebar);
                reportTitlebar();
                """,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        ))
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1160, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Spiritboard"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = headerColor
        window.center()
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        startBackend()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let backend, backend.isRunning {
            backend.interrupt()
        }
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "titlebar", message.webView === webView,
              let headerVisible = message.body as? Bool else { return }
        window.backgroundColor = headerVisible ? headerColor : boardColor
    }

    private func startBackend() {
        let executable = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Helpers/spiritboard-backend")
        let process = Process()
        process.executableURL = executable
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            DispatchQueue.main.async {
                self?.receive(chunk)
            }
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                if self?.ready == false {
                    self?.showError("Spiritboard could not start its local dashboard.")
                }
            }
        }
        do {
            try process.run()
            backend = process
        } catch {
            showError("Spiritboard could not start its bundled server.")
        }
    }

    private func receive(_ chunk: Data) {
        guard !chunk.isEmpty else { return }
        output.append(chunk)
        if output.count > 16_384 {
            output.removeFirst(output.count - 8_192)
        }
        guard !ready, let message = String(data: output, encoding: .utf8) else { return }
        for line in message.split(separator: "\n", omittingEmptySubsequences: false).dropLast() {
            let prefixes = ["Spiritboard local dashboard: ", "Spiritboard is already running: "]
            guard let prefix = prefixes.first(where: { line.hasPrefix($0) }) else { continue }
            let address = String(line.dropFirst(prefix.count))
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard let url = URL(string: address), url.scheme == "http",
                  url.host == "127.0.0.1", url.port != nil,
                  url.path.isEmpty || url.path == "/",
                  url.query == nil, url.fragment == nil else { continue }
            ready = true
            origin = url
            webView.load(URLRequest(url: url))
            return
        }
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = "Check that another Spiritboard instance is not using different settings."
        alert.runModal()
        NSApplication.shared.terminate(nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.absoluteString == "about:blank",
           popupWindows[ObjectIdentifier(webView)] != nil {
            decisionHandler(.allow)
            return
        }
        if let origin, url.scheme == origin.scheme, url.host == origin.host,
           url.port == origin.port {
            decisionHandler(.allow)
        } else {
            if url.scheme == "https" || url.scheme == "http" {
                NSWorkspace.shared.open(url)
            }
            if let popup = popupWindows.removeValue(forKey: ObjectIdentifier(webView)) {
                popup.close()
            }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        let popup = WKWebView(frame: .zero, configuration: configuration)
        popup.navigationDelegate = self
        popup.uiDelegate = self
        let popupWindow = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        popupWindow.title = "Spiritboard sign in"
        popupWindow.contentView = popup
        popupWindow.center()
        popupWindow.makeKeyAndOrderFront(nil)
        popupWindows[ObjectIdentifier(popup)] = popupWindow
        return popup
    }

    func webViewDidClose(_ webView: WKWebView) {
        popupWindows.removeValue(forKey: ObjectIdentifier(webView))?.close()
    }
}

let app = NSApplication.shared
let delegate = SpiritboardApp()
app.delegate = delegate
app.run()
