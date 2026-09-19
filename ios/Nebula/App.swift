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
    private let browser = BrowserController()
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = browser
        self.window = window
        window.makeKeyAndVisible()
        if let url = options.urlContexts.first?.url {
            DispatchQueue.main.async { self.browser.openLink(url) }
        }
    }
    func scene(_ scene: UIScene, openURLContexts contexts: Set<UIOpenURLContext>) {
        if let url = contexts.first?.url { browser.openLink(url) }
    }
}

/// Native colors for the few screens the shell draws itself (Nebula dark tokens).
enum Palette {
    static let canvas = UIColor(red: 0.047, green: 0.059, blue: 0.075, alpha: 1)
    static let surface = UIColor(red: 0.067, green: 0.082, blue: 0.102, alpha: 1)
    static let raised = UIColor(red: 0.102, green: 0.125, blue: 0.153, alpha: 1)
    static let text = UIColor(red: 0.875, green: 0.898, blue: 0.925, alpha: 1)
    static let muted = UIColor(red: 0.596, green: 0.635, blue: 0.678, alpha: 1)
    static let border = UIColor(red: 0.169, green: 0.2, blue: 0.239, alpha: 1)
    static let blue = UIColor(red: 0.416, green: 0.659, blue: 0.922, alpha: 1)
    static let orange = UIColor(red: 0.831, green: 0.604, blue: 0.357, alpha: 1)
    static let red = UIColor(red: 0.878, green: 0.431, blue: 0.447, alpha: 1)
    static let primary = UIColor(red: 0.282, green: 0.439, blue: 0.604, alpha: 1)
    static let primaryText = UIColor(red: 0.957, green: 0.973, blue: 0.988, alpha: 1)
}

/// The whole app: one edge-to-edge web view. Native UI appears only to connect,
/// to recover from a failed load, or when the page opens `nebula://settings`.
final class BrowserController: UIViewController, WKNavigationDelegate, WKUIDelegate {
    private var webView: WKWebView!
    private var server: URL?
    private let progress = UIProgressView(progressViewStyle: .bar)
    private let offline = OfflineView()
    private var observations: [NSKeyValueObservation] = []
    private var statusStyle: UIStatusBarStyle = .lightContent
    private let defaults = UserDefaults.standard

    override var preferredStatusBarStyle: UIStatusBarStyle { statusStyle }

    /// Saved servers, most recent first. Seeds from the single address older builds stored.
    private var history: [String] {
        get { defaults.stringArray(forKey: "nebula.servers") ?? [defaults.string(forKey: "nebula.server")].compactMap { $0 } }
        set { defaults.set(newValue, forKey: "nebula.servers") }
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Palette.canvas
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        // Lets the page offer shell-only rows (App connection) without any script bridge.
        config.applicationNameForUserAgent = "Mobile/15E148 NebulaShell/1.0"
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.isOpaque = false
        webView.backgroundColor = Palette.canvas
        webView.scrollView.backgroundColor = Palette.canvas
        // The page lays itself out with env(safe-area-inset-*); don't inset it twice.
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.inputAssistantItem.leadingBarButtonGroups = []
        webView.inputAssistantItem.trailingBarButtonGroups = []
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)

        progress.progressTintColor = Palette.blue
        progress.trackTintColor = .clear
        progress.isHidden = true
        progress.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(progress)

        offline.isHidden = true
        offline.translatesAutoresizingMaskIntoConstraints = false
        offline.onRetry = { [weak self] in self?.reloadPage() }
        offline.onAlternate = { [weak self] url in self?.connect(url) }
        offline.onChangeServer = { [weak self] in self?.showConnect(firstRun: false) }
        view.addSubview(offline)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progress.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            progress.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progress.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            offline.topAnchor.constraint(equalTo: view.topAnchor),
            offline.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            offline.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            offline.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        observations = [
            webView.observe(\.estimatedProgress, options: [.new]) { [weak self] web, _ in
                self?.progress.setProgress(Float(web.estimatedProgress), animated: true)
            },
            webView.observe(\.themeColor, options: [.initial, .new]) { [weak self] web, _ in
                self?.applyTheme(web.themeColor)
            },
        ]
        if let saved = history.first.flatMap(ServerAddress.parse) { connect(saved) }
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if server == nil && presentedViewController == nil { showConnect(firstRun: true) }
    }

    /// Matches the status bar and overscroll background to the page's theme-color.
    private func applyTheme(_ color: UIColor?) {
        guard let color else { return }
        view.backgroundColor = color
        webView.backgroundColor = color
        webView.scrollView.backgroundColor = color
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        guard color.getRed(&red, green: &green, blue: &blue, alpha: &alpha) else { return }
        statusStyle = 0.299 * red + 0.587 * green + 0.114 * blue > 0.6 ? .darkContent : .lightContent
        setNeedsStatusBarAppearanceUpdate()
    }

    func showConnect(firstRun: Bool) {
        loadViewIfNeeded()
        guard presentedViewController == nil else { return }
        let connect = ConnectController(current: server, saved: ServerHistory.alternates(to: nil, in: history), firstRun: firstRun)
        connect.onConnect = { [weak self] url in self?.connect(url) }
        connect.onPair = { [weak self] url in self?.openLink(url) }
        connect.onReload = { [weak self] in self?.reloadPage() }
        if firstRun {
            connect.modalPresentationStyle = .fullScreen
            connect.isModalInPresentation = true
        } else if let sheet = connect.sheetPresentationController {
            sheet.detents = [.large()]
            sheet.prefersGrabberVisible = true
        }
        present(connect, animated: !firstRun)
    }

    /// Handles `nebula://pair?url=…`, `nebula://settings`, and pasted pairing links.
    func openLink(_ input: URL) {
        loadViewIfNeeded()
        if ServerAddress.isSettingsLink(input) {
            showConnect(firstRun: server == nil)
            return
        }
        guard let server, let url = ServerAddress.pairingURL(input, server: server) else {
            let alert = UIAlertController(title: "Pairing link doesn’t match", message: "Use a pairing link created by the Nebula server this app is connected to. Check the server address, then try again.", preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .cancel))
            (presentedViewController ?? self).present(alert, animated: true)
            return
        }
        // A fresh document is required: PairingGate consumes its fragment only on mount.
        // The one-time secret is never written to native preferences.
        webView.stopLoading()
        offline.isHidden = true
        var document = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        document.queryItems = [URLQueryItem(name: "pairing_attempt", value: UUID().uuidString)]
        webView.load(URLRequest(url: document.url!, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
    }

    private func connect(_ url: URL) {
        webView.stopLoading()
        server = url
        offline.isHidden = true
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
    }

    private func reloadPage() {
        guard let server else { showConnect(firstRun: true); return }
        offline.isHidden = true
        if let current = webView.url, ServerAddress.sameOrigin(current, server) {
            webView.reloadFromOrigin()
        } else { connect(server) }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        progress.setProgress(0.05, animated: false)
        progress.isHidden = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        progress.isHidden = true
        offline.isHidden = true
        guard let server else { return }
        history = ServerHistory.remember(server, in: history)
        defaults.set(server.absoluteString, forKey: "nebula.server")
        defaults.set(Date(), forKey: "nebula.lastConnected")
    }

    private func failed(_ error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        progress.isHidden = true
        offline.show(
            server: server,
            error: error.localizedDescription,
            lastConnected: defaults.object(forKey: "nebula.lastConnected") as? Date,
            alternate: ServerHistory.alternates(to: server, in: history).first
        )
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { reloadPage() }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "nebula" {
            decisionHandler(.cancel)
            openLink(url)
            return
        }
        guard let server else { decisionHandler(.cancel); return }
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

// MARK: - Shared native building blocks

private func label(_ text: String, size: CGFloat, weight: UIFont.Weight = .regular, color: UIColor = Palette.text, style: UIFont.TextStyle = .body) -> UILabel {
    let label = UILabel()
    label.text = text
    label.font = UIFontMetrics(forTextStyle: style).scaledFont(for: .systemFont(ofSize: size, weight: weight))
    label.adjustsFontForContentSizeCategory = true
    label.textColor = color
    label.numberOfLines = 0
    return label
}

private func filledButton(_ title: String, primary: Bool) -> UIButton {
    var config = primary ? UIButton.Configuration.filled() : UIButton.Configuration.gray()
    config.title = title
    config.baseBackgroundColor = primary ? Palette.primary : Palette.surface
    config.baseForegroundColor = primary ? Palette.primaryText : Palette.text
    config.cornerStyle = .fixed
    config.background.cornerRadius = 14
    if !primary {
        config.background.strokeColor = Palette.border
        config.background.strokeWidth = 1
    }
    config.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { attributes in
        var next = attributes
        next.font = UIFontMetrics(forTextStyle: .headline).scaledFont(for: .systemFont(ofSize: 16, weight: primary ? .semibold : .medium))
        return next
    }
    let button = UIButton(configuration: config)
    button.heightAnchor.constraint(greaterThanOrEqualToConstant: 52).isActive = true
    return button
}

private func linkButton(_ title: String, symbol: String? = nil) -> UIButton {
    var config = UIButton.Configuration.plain()
    config.title = title
    config.baseForegroundColor = Palette.blue
    if let symbol { config.image = UIImage(systemName: symbol); config.imagePadding = 6 }
    config.preferredSymbolConfigurationForImage = UIImage.SymbolConfiguration(pointSize: 14, weight: .medium)
    let button = UIButton(configuration: config)
    button.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
    return button
}

// MARK: - Connect (first launch and nebula://settings)

final class ConnectController: UIViewController, UITextFieldDelegate {
    var onConnect: ((URL) -> Void)?
    var onPair: ((URL) -> Void)?
    var onReload: (() -> Void)?
    private let current: URL?
    private let saved: [URL]
    private let firstRun: Bool
    private let field = UITextField()
    private let fieldBox = UIView()
    private let notice = UIStackView()
    private let problem = label("", size: 13, color: Palette.red, style: .footnote)

    init(current: URL?, saved: [URL], firstRun: Bool) {
        self.current = current
        self.saved = saved
        self.firstRun = firstRun
        super.init(nibName: nil, bundle: nil)
        overrideUserInterfaceStyle = .dark
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Palette.canvas

        let mark = UIView()
        mark.backgroundColor = Palette.primary
        mark.layer.cornerRadius = 20
        mark.layer.cornerCurve = .continuous
        let ring = UIView()
        ring.layer.borderColor = Palette.primaryText.cgColor
        ring.layer.borderWidth = 3
        ring.layer.cornerRadius = 17
        ring.translatesAutoresizingMaskIntoConstraints = false
        mark.addSubview(ring)
        mark.translatesAutoresizingMaskIntoConstraints = false
        mark.isAccessibilityElement = false
        NSLayoutConstraint.activate([
            mark.widthAnchor.constraint(equalToConstant: 76), mark.heightAnchor.constraint(equalToConstant: 76),
            ring.widthAnchor.constraint(equalToConstant: 34), ring.heightAnchor.constraint(equalToConstant: 34),
            ring.centerXAnchor.constraint(equalTo: mark.centerXAnchor), ring.centerYAnchor.constraint(equalTo: mark.centerYAnchor),
        ])

        let title = label(firstRun ? "Connect to Nebula" : "Nebula connection", size: 26, weight: .semibold, style: .title1)
        title.textAlignment = .center
        title.accessibilityTraits = .header
        let subtitle = label("Only the server address is saved on this phone. Chats and sessions stay on your Core.", size: 15, color: Palette.muted, style: .subheadline)
        subtitle.textAlignment = .center
        let hero = UIStackView(arrangedSubviews: [mark, title, subtitle])
        hero.axis = .vertical
        hero.alignment = .center
        hero.spacing = 14
        hero.setCustomSpacing(18, after: mark)

        let caption = label("Server address", size: 13, weight: .medium, color: Palette.muted, style: .footnote)
        field.text = current?.absoluteString ?? saved.first?.absoluteString ?? "http://10.10.0.2:8000"
        field.placeholder = "http://server:8000"
        field.font = UIFontMetrics(forTextStyle: .body).scaledFont(for: .monospacedSystemFont(ofSize: 16, weight: .regular))
        field.adjustsFontForContentSizeCategory = true
        field.textColor = Palette.text
        field.keyboardType = .URL
        field.returnKeyType = .go
        field.autocapitalizationType = .none
        field.autocorrectionType = .no
        field.clearButtonMode = .whileEditing
        field.accessibilityLabel = "Server address"
        field.delegate = self
        field.addTarget(self, action: #selector(fieldChanged), for: .editingChanged)
        field.translatesAutoresizingMaskIntoConstraints = false
        fieldBox.backgroundColor = Palette.surface
        fieldBox.layer.cornerRadius = 14
        fieldBox.layer.cornerCurve = .continuous
        fieldBox.layer.borderWidth = 1
        fieldBox.layer.borderColor = Palette.border.cgColor
        fieldBox.addSubview(field)
        NSLayoutConstraint.activate([
            fieldBox.heightAnchor.constraint(greaterThanOrEqualToConstant: 52),
            field.leadingAnchor.constraint(equalTo: fieldBox.leadingAnchor, constant: 16),
            field.trailingAnchor.constraint(equalTo: fieldBox.trailingAnchor, constant: -10),
            field.topAnchor.constraint(equalTo: fieldBox.topAnchor, constant: 4),
            field.bottomAnchor.constraint(equalTo: fieldBox.bottomAnchor, constant: -4),
        ])

        let chips = UIStackView()
        chips.axis = .horizontal
        chips.spacing = 8
        chips.alignment = .leading
        for url in saved.prefix(ServerHistory.limit) {
            var config = UIButton.Configuration.gray()
            config.title = ServerAddress.label(url)
            config.baseBackgroundColor = Palette.raised
            config.baseForegroundColor = Palette.text
            config.cornerStyle = .capsule
            config.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { attributes in
                var next = attributes
                next.font = UIFontMetrics(forTextStyle: .footnote).scaledFont(for: .systemFont(ofSize: 13, weight: .medium))
                return next
            }
            let chip = UIButton(configuration: config, primaryAction: UIAction { [weak self] _ in
                self?.field.text = url.absoluteString
                self?.fieldChanged()
            })
            chip.accessibilityLabel = "Use \(ServerAddress.label(url))"
            chip.heightAnchor.constraint(greaterThanOrEqualToConstant: 36).isActive = true
            chips.addArrangedSubview(chip)
        }
        let chipScroll = UIScrollView()
        chipScroll.showsHorizontalScrollIndicator = false
        chipScroll.addSubview(chips)
        chips.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            chips.leadingAnchor.constraint(equalTo: chipScroll.contentLayoutGuide.leadingAnchor),
            chips.trailingAnchor.constraint(equalTo: chipScroll.contentLayoutGuide.trailingAnchor),
            chips.topAnchor.constraint(equalTo: chipScroll.contentLayoutGuide.topAnchor),
            chips.bottomAnchor.constraint(equalTo: chipScroll.contentLayoutGuide.bottomAnchor),
            chips.heightAnchor.constraint(equalTo: chipScroll.frameLayoutGuide.heightAnchor),
        ])
        chipScroll.isHidden = saved.isEmpty

        let shield = UIImageView(image: UIImage(systemName: "lock.open"))
        shield.tintColor = Palette.orange
        shield.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        shield.setContentHuggingPriority(.required, for: .horizontal)
        notice.addArrangedSubview(shield)
        notice.addArrangedSubview(label("HTTP isn’t encrypted — use it only on your LAN or VPN.", size: 13, color: Palette.muted, style: .footnote))
        notice.spacing = 8
        notice.alignment = .center
        problem.isHidden = true

        let form = UIStackView(arrangedSubviews: [caption, fieldBox, chipScroll, notice, problem])
        form.axis = .vertical
        form.spacing = 10

        let connect = filledButton(firstRun ? "Connect" : "Connect", primary: true)
        connect.addAction(UIAction { [weak self] _ in self?.submit() }, for: .primaryActionTriggered)
        let pair = linkButton("I have a pairing link", symbol: "link")
        pair.addAction(UIAction { [weak self] _ in self?.pastePairing() }, for: .primaryActionTriggered)
        let actions = UIStackView(arrangedSubviews: [connect, pair])
        actions.axis = .vertical
        actions.spacing = 6
        if !firstRun {
            let reload = filledButton("Reload page", primary: false)
            reload.addAction(UIAction { [weak self] _ in
                self?.dismiss(animated: true) { self?.onReload?() }
            }, for: .primaryActionTriggered)
            actions.insertArrangedSubview(reload, at: 1)
            actions.setCustomSpacing(10, after: connect)
        }

        let content = UIStackView(arrangedSubviews: [hero, form])
        content.axis = .vertical
        content.spacing = 28
        let scroll = UIScrollView()
        scroll.alwaysBounceVertical = true
        scroll.keyboardDismissMode = .interactive
        scroll.addSubview(content)
        [scroll, content, actions].forEach { $0.translatesAutoresizingMaskIntoConstraints = false }
        view.addSubview(scroll)
        view.addSubview(actions)
        let margins = view.layoutMarginsGuide
        view.directionalLayoutMargins = NSDirectionalEdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: actions.topAnchor, constant: -12),
            content.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor, constant: firstRun ? 56 : 32),
            content.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor, constant: -12),
            content.leadingAnchor.constraint(equalTo: margins.leadingAnchor),
            content.trailingAnchor.constraint(equalTo: margins.trailingAnchor),
            actions.leadingAnchor.constraint(equalTo: margins.leadingAnchor),
            actions.trailingAnchor.constraint(equalTo: margins.trailingAnchor),
            actions.bottomAnchor.constraint(equalTo: view.keyboardLayoutGuide.topAnchor, constant: -8),
        ])
        if !firstRun {
            let close = UIButton(type: .close, primaryAction: UIAction { [weak self] _ in self?.dismiss(animated: true) })
            close.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(close)
            NSLayoutConstraint.activate([
                close.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
                close.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
                close.widthAnchor.constraint(equalToConstant: 44), close.heightAnchor.constraint(equalToConstant: 44),
            ])
        }
        fieldChanged()
    }

    func textFieldDidBeginEditing(_ textField: UITextField) { fieldBox.layer.borderColor = Palette.blue.cgColor }
    func textFieldDidEndEditing(_ textField: UITextField) { fieldBox.layer.borderColor = Palette.border.cgColor }
    func textFieldShouldReturn(_ textField: UITextField) -> Bool { submit(); return false }

    @objc private func fieldChanged() {
        problem.isHidden = true
        let text = field.text?.trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        notice.isHidden = text.hasPrefix("https://")
    }

    private func submit() {
        guard let url = ServerAddress.parse(field.text ?? "") else {
            problem.text = "Enter an HTTP or HTTPS address with an optional port, like http://10.10.0.2:8000 — no path, password or query."
            problem.isHidden = false
            UIAccessibility.post(notification: .announcement, argument: problem.text)
            return
        }
        field.resignFirstResponder()
        dismiss(animated: true) { [onConnect] in onConnect?(url) }
    }

    private func pastePairing() {
        guard let server = ServerAddress.parse(field.text ?? "") else { submit(); return }
        let alert = UIAlertController(title: "Pair this app", message: "On the Nebula host, open Settings → Paired devices and create a pairing link. Paste it here, then confirm its code.", preferredStyle: .alert)
        alert.addTextField { field in
            field.placeholder = "Pairing link"
            field.text = UIPasteboard.general.hasURLs ? UIPasteboard.general.url?.absoluteString : nil
            field.autocapitalizationType = .none
            field.autocorrectionType = .no
            field.accessibilityLabel = "Pairing link"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Continue", style: .default) { [weak self, weak alert] _ in
            guard let self, let value = alert?.textFields?.first?.text, let link = URL(string: value.trimmingCharacters(in: .whitespaces)) else { return }
            guard ServerAddress.pairingURL(link, server: server) != nil else {
                self.problem.text = "That pairing link belongs to a different server or is incomplete. Check the address above, then paste the link again."
                self.problem.isHidden = false
                return
            }
            self.dismiss(animated: true) { [onConnect = self.onConnect, onPair = self.onPair] in
                onConnect?(server)
                onPair?(link)
            }
        })
        present(alert, animated: true)
    }
}

// MARK: - Offline recovery

final class OfflineView: UIView {
    var onRetry: (() -> Void)?
    var onAlternate: ((URL) -> Void)?
    var onChangeServer: (() -> Void)?
    private let body = label("", size: 15, color: Palette.muted, style: .subheadline)
    private let detail = label("", size: 12, color: Palette.muted, style: .caption1)
    private let lastSeen = label("", size: 12, color: Palette.muted, style: .caption1)
    private let alternate = filledButton("", primary: false)
    private var alternateURL: URL?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = Palette.canvas
        overrideUserInterfaceStyle = .dark

        let icon = UIImageView(image: UIImage(systemName: "wifi.slash"))
        icon.tintColor = Palette.muted
        icon.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 26, weight: .regular)
        icon.contentMode = .center
        let badge = UIView()
        badge.backgroundColor = Palette.surface
        badge.layer.cornerRadius = 36
        badge.layer.borderWidth = 1
        badge.layer.borderColor = Palette.border.cgColor
        badge.addSubview(icon)
        [icon, badge].forEach { $0.translatesAutoresizingMaskIntoConstraints = false }
        NSLayoutConstraint.activate([
            badge.widthAnchor.constraint(equalToConstant: 72), badge.heightAnchor.constraint(equalToConstant: 72),
            icon.centerXAnchor.constraint(equalTo: badge.centerXAnchor), icon.centerYAnchor.constraint(equalTo: badge.centerYAnchor),
        ])
        let title = label("Can’t reach Nebula", size: 24, weight: .semibold, style: .title2)
        title.accessibilityTraits = .header
        [title, body, detail, lastSeen].forEach { $0.textAlignment = .center }
        let message = UIStackView(arrangedSubviews: [badge, title, body, lastSeen, detail])
        message.axis = .vertical
        message.alignment = .center
        message.spacing = 12
        message.setCustomSpacing(18, after: badge)

        let retry = filledButton("Try again", primary: true)
        retry.addAction(UIAction { [weak self] _ in self?.onRetry?() }, for: .primaryActionTriggered)
        alternate.addAction(UIAction { [weak self] _ in
            if let url = self?.alternateURL { self?.onAlternate?(url) }
        }, for: .primaryActionTriggered)
        let change = linkButton("Change server")
        change.addAction(UIAction { [weak self] _ in self?.onChangeServer?() }, for: .primaryActionTriggered)
        let actions = UIStackView(arrangedSubviews: [retry, alternate, change])
        actions.axis = .vertical
        actions.spacing = 10

        [message, actions].forEach {
            $0.translatesAutoresizingMaskIntoConstraints = false
            addSubview($0)
        }
        let margins = layoutMarginsGuide
        directionalLayoutMargins = NSDirectionalEdgeInsets(top: 0, leading: 24, bottom: 0, trailing: 24)
        NSLayoutConstraint.activate([
            message.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -60),
            message.leadingAnchor.constraint(equalTo: margins.leadingAnchor),
            message.trailingAnchor.constraint(equalTo: margins.trailingAnchor),
            actions.leadingAnchor.constraint(equalTo: margins.leadingAnchor),
            actions.trailingAnchor.constraint(equalTo: margins.trailingAnchor),
            actions.bottomAnchor.constraint(equalTo: safeAreaLayoutGuide.bottomAnchor, constant: -12),
            actions.topAnchor.constraint(greaterThanOrEqualTo: message.bottomAnchor, constant: 24),
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    func show(server: URL?, error: String, lastConnected: Date?, alternate url: URL?) {
        let host = server.map(ServerAddress.label) ?? "The server"
        body.text = "\(host) didn’t answer. Check that this phone is on your home network or VPN, then try again."
        detail.text = error
        if let lastConnected {
            let formatter = RelativeDateTimeFormatter()
            formatter.unitsStyle = .full
            lastSeen.text = "Last connected \(formatter.localizedString(for: lastConnected, relativeTo: Date()))"
            lastSeen.isHidden = false
        } else { lastSeen.isHidden = true }
        alternateURL = url
        alternate.configuration?.title = url.map { "Try \(ServerAddress.label($0))" }
        alternate.isHidden = url == nil
        isHidden = false
        UIAccessibility.post(notification: .screenChanged, argument: self)
    }
}
