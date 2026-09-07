import UIKit
import WebKit
import SafariServices

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication, configurationForConnecting session: UISceneSession, options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let configuration = UISceneConfiguration(name: "Nebula", sessionRole: session.role)
        configuration.delegateClass = SceneDelegate.self
        return configuration
    }
}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = UINavigationController(rootViewController: BrowserController())
        self.window = window
        window.makeKeyAndVisible()
        if let url = options.urlContexts.first?.url {
            DispatchQueue.main.async { self.openPairing(url) }
        }
    }
    func scene(_ scene: UIScene, openURLContexts contexts: Set<UIOpenURLContext>) {
        if let url = contexts.first?.url { openPairing(url) }
    }
    private func openPairing(_ url: URL) {
        let nav = window?.rootViewController as? UINavigationController
        (nav?.viewControllers.first as? BrowserController)?.openPairing(url)
    }
}

final class BrowserController: UIViewController, WKNavigationDelegate, WKUIDelegate {
    private var webView: WKWebView!
    private var server: URL?
    private let status = UILabel()
    private let progress = UIProgressView(progressViewStyle: .default)
    private var observations: [NSKeyValueObservation] = []
    private lazy var back = UIBarButtonItem(image: UIImage(systemName: "chevron.left"), style: .plain, target: self, action: #selector(goBack))

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "Nebula"
        view.backgroundColor = .systemBackground
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        status.font = .preferredFont(forTextStyle: .footnote)
        status.adjustsFontForContentSizeCategory = true
        status.numberOfLines = 0
        status.textAlignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(status)
        progress.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(progress)
        NSLayoutConstraint.activate([
            status.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            status.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 12),
            status.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -12),
            progress.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 4),
            progress.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progress.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.topAnchor.constraint(equalTo: progress.bottomAnchor),
            webView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor)
        ])
        back.accessibilityLabel = "Back"
        navigationItem.leftBarButtonItem = back
        navigationItem.rightBarButtonItem = UIBarButtonItem(image: UIImage(systemName: "ellipsis"), menu: UIMenu(children: [
            UIAction(title: "Reload", image: UIImage(systemName: "arrow.clockwise")) { [weak self] _ in self?.reloadPage() },
            UIAction(title: "Pair this app", image: UIImage(systemName: "link")) { [weak self] _ in self?.pastePairing() },
            UIAction(title: "Server", image: UIImage(systemName: "network")) { [weak self] _ in self?.editServer() }
        ]))
        navigationItem.rightBarButtonItem?.accessibilityLabel = "Connection options"
        webView.inputAssistantItem.leadingBarButtonGroups = []
        webView.inputAssistantItem.trailingBarButtonGroups = []
        observations = [
            webView.observe(\.estimatedProgress, options: [.new]) { [weak self] web, _ in
                self?.progress.progress = Float(web.estimatedProgress)
                self?.progress.isHidden = !web.isLoading
            },
            webView.observe(\.canGoBack, options: [.initial, .new]) { [weak self] web, _ in self?.back.isEnabled = web.canGoBack }
        ]
        if let saved = UserDefaults.standard.string(forKey: "nebula.server"), let url = ServerAddress.parse(saved) {
            connect(url)
        } else {
            status.text = "Choose your Nebula server to connect."
        }
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if server == nil && presentedViewController == nil { editServer() }
    }

    @objc private func editServer() {
        let alert = UIAlertController(title: "Nebula server", message: "Enter your server address. HTTP sends traffic without encryption; use it only on a trusted LAN or VPN.", preferredStyle: .alert)
        alert.addTextField { field in
            field.text = self.server?.absoluteString ?? "http://10.10.0.2:8000"
            field.placeholder = "http://server:8000"
            field.keyboardType = .URL
            field.autocapitalizationType = .none
            field.autocorrectionType = .no
            field.accessibilityLabel = "Server address"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Connect", style: .default) { [weak self, weak alert] _ in
            guard let self else { return }
            guard let url = ServerAddress.parse(alert?.textFields?.first?.text ?? "") else {
                self.status.text = "Enter an HTTP or HTTPS server address with an optional port, without a path, password, or query. Tap Server to try again."
                return
            }
            UserDefaults.standard.set(url.absoluteString, forKey: "nebula.server")
            self.connect(url)
        })
        present(alert, animated: true)
    }

    @objc private func pastePairing() {
        let alert = UIAlertController(title: "Pair this app", message: "On the Nebula host, create a pairing link under Settings → Advanced → Identity & Security → Paired devices. Paste the link here, then verify its confirmation code.", preferredStyle: .alert)
        alert.addTextField { field in
            field.placeholder = "Pairing link"
            field.autocapitalizationType = .none
            field.autocorrectionType = .no
            field.accessibilityLabel = "Pairing link"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Continue", style: .default) { [weak self, weak alert] _ in
            guard let value = alert?.textFields?.first?.text, let url = URL(string: value) else { return }
            self?.openPairing(url)
        })
        present(alert, animated: true)
    }
    func openPairing(_ input: URL) {
        loadViewIfNeeded()
        guard let server, let url = ServerAddress.pairingURL(input, server: server) else {
            status.text = "Use a valid pairing link for the selected server. Tap Server to check its address."
            return
        }
        // A fresh document is required: PairingGate consumes its fragment only on mount.
        // The one-time secret is never written to native preferences.
        webView.stopLoading()
        var document = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        document.queryItems = [URLQueryItem(name: "pairing_attempt", value: UUID().uuidString)]
        webView.load(URLRequest(url: document.url!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
    }

    private func connect(_ url: URL) {
        webView.stopLoading()
        server = url
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
    }
    @objc private func goBack() { webView.goBack() }
    @objc private func reloadPage() {
        guard let server else { editServer(); return }
        if let current = webView.url, ServerAddress.sameOrigin(current, server) {
            webView.reloadFromOrigin()
        } else { connect(server) }
    }
    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        status.text = "Connecting to \(server?.host ?? "Nebula")…"
        progress.isHidden = false
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        status.text = nil
        progress.isHidden = true
    }
    private func failed(_ error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        progress.isHidden = true
        status.text = "Couldn’t load Nebula: \(error.localizedDescription) Check your connection, then tap Reload or Server."
        UIAccessibility.post(notification: .announcement, argument: status.text)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        status.text = "The page was unloaded. Tap Reload to reconnect to Nebula."
    }
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, let server else { decisionHandler(.cancel); return }
        if ServerAddress.sameOrigin(url, server) {
            if action.targetFrame == nil { decisionHandler(.cancel); webView.load(action.request) }
            else { decisionHandler(.allow) }
            return
        }
        decisionHandler(.cancel)
        guard action.navigationType == .linkActivated, ["http", "https"].contains(url.scheme ?? ""), presentedViewController == nil else { return }
        let alert = UIAlertController(title: "Open external link?", message: url.host, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Open", style: .default) { [weak self] _ in self?.present(SFSafariViewController(url: url), animated: true) })
        present(alert, animated: true)
    }
}
