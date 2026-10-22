import AppKit
import WebKit

final class SpiritboardApp: NSObject, NSApplicationDelegate, NSMenuItemValidation,
    WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private static let dataDirectoryKey = "dataDirectory"
    private let headerColor = NSColor(
        srgbRed: 17.0 / 255, green: 14.0 / 255, blue: 26.0 / 255, alpha: 1
    )
    private let boardColor = NSColor(
        srgbRed: 13.0 / 255, green: 11.0 / 255, blue: 20.0 / 255, alpha: 1
    )
    private var window: NSWindow!
    private var webView: WKWebView!
    private let spinner = NSProgressIndicator()
    private var backend: Process?
    private var launch = 0
    private var origin: URL?
    private var output = Data()
    private var ready = false
    private var popupWindows = [ObjectIdentifier: NSWindow]()

    // A folder chosen by the user; nil means the normal default data directory.
    private var dataDirectory: String? {
        get { UserDefaults.standard.string(forKey: Self.dataDirectoryKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.dataDirectoryKey) }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        installMenu()

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
                addEventListener('resize', reportTitlebar, {passive: true});
                addEventListener('load', reportTitlebar);
                reportTitlebar();
                """,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        ))
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.underPageBackgroundColor = headerColor
        webView.autoresizingMask = [.width, .height]

        let content = NSView()
        webView.frame = content.bounds
        content.addSubview(webView)
        spinner.style = .spinning
        spinner.isDisplayedWhenStopped = false
        spinner.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(spinner)
        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: content.centerYAnchor),
        ])

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = headerColor
        window.minSize = NSSize(width: 640, height: 420)
        window.contentView = content
        if !window.setFrameUsingName("Spiritboard") {
            window.center()
        }
        window.setFrameAutosaveName("Spiritboard")
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        connect()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
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
        let color = headerVisible ? headerColor : boardColor
        window.backgroundColor = color
        webView.underPageBackgroundColor = color
    }

    private func installMenu() {
        let main = NSMenu()
        func item(_ title: String, _ action: Selector?, _ key: String = "",
                  _ modifiers: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            return item
        }
        func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.submenu = menu
            main.addItem(item)
            return menu
        }
        _ = submenu("Spiritboard", [
            item("About Spiritboard",
                 #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            item("Hide Spiritboard", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h",
                 [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            item("Quit Spiritboard", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = submenu("File", [
            item("Choose Data Folder…", #selector(chooseDataDirectory(_:)), "o"),
            item("Use Default Data Folder", #selector(useDefaultDataDirectory(_:))),
            .separator(),
            item("Close Window", #selector(NSWindow.performClose(_:)), "w"),
        ])
        _ = submenu("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = submenu("View", [
            item("Reload", #selector(reloadDashboard(_:)), "r"),
            .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f",
                 [.command, .control]),
        ])
        let windows = submenu("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:))),
            .separator(),
            item("Bring All to Front", #selector(NSApplication.arrangeInFront(_:))),
        ])
        NSApplication.shared.mainMenu = main
        NSApplication.shared.windowsMenu = windows
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        switch menuItem.action {
        case #selector(useDefaultDataDirectory(_:)):
            menuItem.state = dataDirectory == nil ? .on : .off
        case #selector(chooseDataDirectory(_:)):
            menuItem.state = dataDirectory == nil ? .off : .on
        case #selector(reloadDashboard(_:)):
            return ready
        default:
            break
        }
        return window.attachedSheet == nil
    }

    @objc private func chooseDataDirectory(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        // Custom --data-dir folders are often hidden, such as a project's .spiritboard.
        panel.showsHiddenFiles = true
        panel.prompt = "Show Dashboard"
        panel.message = "Choose the --data-dir folder of a Spiritboard dashboard that is running."
        if let dataDirectory {
            panel.directoryURL = URL(fileURLWithPath: dataDirectory)
        }
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            guard response == .OK, let path = panel.url?.path else {
                // Cancelling after a failure must not leave an empty window.
                if !self.ready { self.connect() }
                return
            }
            self.dataDirectory = path
            self.connect()
        }
    }

    @objc private func useDefaultDataDirectory(_ sender: Any?) {
        guard dataDirectory != nil else { return }
        dataDirectory = nil
        connect()
    }

    @objc private func reloadDashboard(_ sender: Any?) {
        webView.reload()
    }

    private func connect() {
        launch += 1
        let current = launch
        output = Data()
        ready = false
        origin = nil
        webView.stopLoading()
        webView.isHidden = true
        spinner.startAnimation(nil)
        let folder = dataDirectory.map { URL(fileURLWithPath: $0).lastPathComponent }
        window.title = folder.map { "Spiritboard — \($0)" } ?? "Spiritboard"
        guard let old = backend, old.isRunning else {
            startBackend(current)
            return
        }
        backend = nil
        // Our own server may own the folder chosen next, so let it release the lock first.
        old.interrupt()
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(5)
            while old.isRunning, Date() < deadline { usleep(50_000) }
            if old.isRunning { old.terminate() }
            old.waitUntilExit()
            DispatchQueue.main.async { [weak self] in
                guard let self, current == self.launch else { return }
                self.startBackend(current)
            }
        }
    }

    private func startBackend(_ current: Int) {
        let process = Process()
        process.executableURL = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Helpers/spiritboard-backend")
        if let dataDirectory {
            process.arguments = ["--data-dir", dataDirectory]
        }
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
                guard let self, current == self.launch else { return }
                self.receive(chunk)
            }
        }
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                guard let self, current == self.launch, !self.ready else { return }
                self.showFailure()
            }
        }
        do {
            try process.run()
            backend = process
        } catch {
            showFailure()
        }
    }

    private func receive(_ chunk: Data) {
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

    private func showFailure(_ detail: String? = nil) {
        spinner.stopAnimation(nil)
        webView.isHidden = true
        ready = false
        let reason = detail ?? String(data: output, encoding: .utf8)?
            .split(separator: "\n").last.map {
                String($0).replacingOccurrences(of: "Spiritboard could not connect: ", with: "")
            }
        let alert = NSAlert()
        let current = launch
        let custom = dataDirectory != nil
        alert.addButton(withTitle: "Try Again")
        alert.addButton(withTitle: "Choose Data Folder…")
        if let dataDirectory {
            let folder = (dataDirectory as NSString).abbreviatingWithTildeInPath
            alert.messageText = "Spiritboard can't show the dashboard for this data folder."
            alert.informativeText = [
                reason, "Folder: \(folder)",
                "Start it with spiritboard serve --data-dir and its usual options, then try again.",
            ].compactMap { $0 }.joined(separator: "\n\n")
            alert.addButton(withTitle: "Use Default Data Folder")
        } else {
            alert.messageText = "Spiritboard could not start its local dashboard."
            alert.informativeText = reason ?? ""
        }
        alert.addButton(withTitle: "Quit")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, current == self.launch else { return }
            switch response {
            case .alertFirstButtonReturn:
                self.connect()
            case .alertSecondButtonReturn:
                DispatchQueue.main.async { self.chooseDataDirectory(nil) }
            case .alertThirdButtonReturn where custom:
                self.useDefaultDataDirectory(nil)
            default:
                NSApplication.shared.terminate(nil)
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView === self.webView, ready, let origin, webView.url?.host == origin.host,
              webView.url?.port == origin.port else { return }
        spinner.stopAnimation(nil)
        webView.isHidden = false
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        // A replaced or cancelled load is not a dashboard failure.
        guard webView === self.webView, ready,
              (error as NSError).code != NSURLErrorCancelled else { return }
        launch += 1
        showFailure("The dashboard stopped responding.")
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
