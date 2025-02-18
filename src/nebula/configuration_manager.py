import json
import os

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from . import constants
from .log_config import setup_logging

AVAILABLE_TOOLS = [
    "0trace",
    "acccheck",
    "ace-voip",
    "aesedb",
    "aircrack-ng",
    "airgeddon",
    "altdns",
    "amap",
    "amass",
    "apache-users",
    "arachni",
    "arp-scan",
    "asleap",
    "assetfinder",
    "autopsy",
    "bbqsql",
    "beef-xss",
    "bettercap",
    "binwalk",
    "blindelephant",
    "bloodhound",
    "blue-hydra",
    "bluesnarfer",
    "braa",
    "bruteshark",
    "bulk-extractor",
    "bully",
    "burpsuite",
    "cewl",
    "changeme",
    "cherrytree",
    "chisel",
    "chntpw",
    "ciphey",
    "cisco-auditing-tool",
    "cisco-global-exploiter",
    "cisco-ocs",
    "cisco-torch",
    "cloudbrute",
    "cloud-enum",
    "cmospwd",
    "cmseek",
    "commix",
    "cookie-cadger",
    "copy-router-config",
    "crackle",
    "crackmapexec",
    "creddump",
    "creepy",
    "crowbar",
    "crunch",
    "cryptcat",
    "cuckoo",
    "cupid-wpa",
    "cutycapt",
    "cyberchef",
    "cymothoa",
    "davtest",
    "dbd",
    "dbpwaudit",
    "dc3dd",
    "dff",
    "dhcpig",
    "dirb",
    "dirbuster",
    "dirsearch",
    "dmitry",
    "dns2tcp",
    "dnscat2",
    "dnschef",
    "dnsenum",
    "dnsgen",
    "dnsmap",
    "dnsrecon",
    "dnstracer",
    "dnswalk",
    "dnsx",
    "donut-shellcode",
    "doona",
    "dotdotpwn",
    "dradis",
    "drozer",
    "dscan",
    "dsniff",
    "dumpsterdiver",
    "dvwa",
    "eaphammer",
    "eapmd5pass",
    "edb-debugger",
    "enum4linux",
    "enum4linux-ng",
    "enumiax",
    "ettercap",
    "evilginx2",
    "evil-ssdp",
    "evil-winrm",
    "exiflooter",
    "exploitdb",
    "extundelete",
    "eyewitness",
    "fcrackzip",
    "fern-wifi-cracker",
    "feroxbuster",
    "fierce",
    "fiked",
    "fimap",
    "finalrecon",
    "findmyhash",
    "firewalk",
    "firmware-mod-kit",
    "flake",
    "foremost",
    "fragroute",
    "fragrouter",
    "freeipmi",
    "freeradius-wpe",
    "freerdp",
    "fruitywifi",
    "ftpsmap",
    "fuzzdb",
    "galleta",
    "gobuster",
    "godoh",
    "gophish",
    "gpp-decrypt",
    "gps3",
    "grabber",
    "guymager",
    "h8mail",
    "hackrf",
    "hakrawler",
    "hamster-sidejack",
    "hashcat",
    "hashcat-utils",
    "hashid",
    "hash-identifier",
    "havoc",
    "hcxdumptool",
    "hcxtools",
    "heartleech",
    "hexinject",
    "hostapd-mana",
    "hostapd-wpe",
    "hosthunter",
    "hping3",
    "htshells",
    "http-parser",
    "httprint",
    "httprobe",
    "http-tunnel",
    "httpx-toolkit",
    "hydra",
    "hypercorn",
    "hyperion",
    "iaxflood",
    "ibombshell",
    "idb",
    "ident-user-enum",
    "ike-scan",
    "impacket",
    "infoga",
    "inguma",
    "inssider",
    "instrusive",
    "inviteflood",
    "ipv6-toolkit",
    "irpas",
    "ismtp",
    "isr-evilgrade",
    "ivre",
    "jad",
    "jadx",
    "javasnoop",
    "jboss-autopwn",
    "jd-gui",
    "john",
    "joomscan",
    "jsql",
    "juice-shop",
    "kali-nethunter",
    "kerberoast",
    "killerbee",
    "king-phisher",
    "kismet",
    "koadic",
    "lapdumper",
    "laudanum",
    "legion",
    "lbd",
    "leviathan",
    "lft",
    "libnfc",
    "lief",
    "linenum",
    "linux-exploit-suggester",
    "lynis",
    "macchanger",
    "maltego",
    "mana-toolkit",
    "manuf",
    "maryam",
    "maskprocessor",
    "masscan",
    "massdns",
    "mdk3",
    "mdk4",
    "medusa",
    "merlin",
    "metagoofil",
    "metasploit-framework",
    "mfoc",
    "mfterm",
    "mimikatz",
    "miranda",
    "mitmf",
    "mitmproxy",
    "nbtscan-unixwiz",
    "ncdu",
    "ncrack",
    "ndiff",
    "netcat",
    "netdiscover",
    "netrecon",
    "netsniff-ng",
    "netstat",
    "netwox",
    "nikto",
    "nipper-ng",
    "nishang",
    "nmap",
    "nmapsi4",
    "ntopng",
    "oclhashcat",
    "odat",
    "ohwurm",
    "ollydbg",
    "openvas",
    "oscanner",
    "osrframework",
    "o-saft",
    "owasp-mantra-ff",
    "owasp-zap",
    "p0f",
    "pack",
    "padbuster",
    "paros",
    "parsero",
    "patator",
    "pdfid",
    "pdf-parser",
    "pdgmail",
    "peepdf",
    "perl-cisco-copyconfig",
    "phishery",
    "photon",
    "php-sploit",
    "phpstress",
    "pidgin",
    "pipal",
    "pixiewps",
    "plecost",
    "polenum",
    "powerfuzzer",
    "powershell-empire",
    "powersploit",
    "prelude",
    "proxmark3",
    "proxychains",
    "proxystrike",
    "ptunnel",
    "pwnat",
    "pwncat",
    "pwnie-express",
    "pyew",
    "pyrit",
    "qsslcaudit",
    "radare2",
    "rainbowcrack",
    "rcracki-mt",
    "rdesktop",
    "reaver",
    "recon-ng",
    "redfang",
    "regripper",
    "responder",
    "rfcat",
    "rfidiot",
    "ridenum",
    "rizin",
    "ropper",
    "routersploit",
    "rsmangler",
    "rtl-sdr",
    "rtlsdr-scanner",
    "rtpbreak",
    "rtpflood",
    "rtpinsertsound",
    "rtpmixsound",
    "sakis3g",
    "samba",
    "samdump2",
    "sandi",
    "sasquatch",
    "sbd",
    "scapy",
    "sctpscan",
    "seclists",
    "setoolkit (Social-Engineer Toolkit)",
    "sfuzz",
    "shodan",
    "sidguesser",
    "siege",
    "silenttrinity",
    "simscan",
    "siparmyknife",
    "sipvicious",
    "skipfish",
    "sleuthkit",
    "slowhttptest",
    "smali",
    "smbclient",
    "smbmap",
    "smtp-user-enum",
    "sn0int",
    "snarf",
    "snmpcheck",
    "snort",
    "socat",
    "sparta",
    "sphinxbase",
    "spiderfoot",
    "spike",
    "spooftooph",
    "sqlmap",
    "sqlninja",
    "sqlsus",
    "sqsh",
    "ssdeep",
    "sslcaudit",
    "sslscan",
    "sslsplit",
    "sslstrip",
    "sslyze",
    "stunnel",
    "subfinder",
    "sublist3r",
    "sudomy",
    "sunxi-tools",
    "suricata",
    "svmap",
    "svwar",
    "svn",
    "swaks",
    "sysstat",
    "t50",
    "tcpdump",
    "tcpflow",
    "tcpick",
    "tcpreplay",
    "tcpview",
    "tcpxtract",
    "telnet",
    "termineter",
    "thc-aireplay",
    "thc-ipv6",
    "thc-pptp-bruter",
    "theharvester",
    "tinfoilhat",
    "tkiptun-ng",
    "tmux",
    "tnscmd10g",
    "tor",
    "traceroute",
    "ucspi-tcp",
    "udisks2",
    "ufw",
    "unicornscan",
    "unix-privesc-check",
    "unrar",
    "upx-ucl",
    "urlcrazy",
    "valgrind",
    "veil-evasion",
    "veracrypt",
    "vinetto",
    "vlan",
    "vmfs-tools",
    "voiphopper",
    "volatility",
    "w3af",
    "wafw00f",
    "wapiti",
    "wash",
    "watobo",
    "webacoo",
    "webshag",
    "webscarab",
    "webshells",
    "weevely",
    "wfuzz",
    "whatweb",
    "whois",
    "wireshark",
    "wireless-tools",
    "wizznic",
    "wpscan",
    "x11-apps",
    "xprobe",
    "xspy",
    "xsser",
    "xsspy",
    "xxd",
    "yara",
    "yersinia",
    "zabbix",
    "zaproxy",
    "zenmap",
    "zip",
    "zlib1g",
    "zmap",
    "zonetransfer",
    "zykeys",
]

logger = setup_logging(
    log_file=constants.SYSTEM_LOGS_DIR + "/configuration_manager.log"
)


class ErrorDialog(QDialog):
    def __init__(self, message, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Error")
        self.setPalette(self.dark_palette())
        self.init_ui(message)

    def dark_palette(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ToolTipBase, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.ToolTipText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        palette.setColor(QPalette.ColorRole.ButtonText, Qt.GlobalColor.white)
        palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
        palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        return palette

    def init_ui(self, message):
        layout = QVBoxLayout()
        label = QLabel(message)
        button = QPushButton("OK")
        button.clicked.connect(self.accept)
        layout.addWidget(label)
        layout.addWidget(button)
        self.setLayout(layout)


class ConfigManager(QObject):
    def __init__(self, initialengagement_folder=None):
        super().__init__()

        self.engagement_folder = initialengagement_folder
        try:
            self.CONFIG_FILE_PATH = os.path.join(self.engagement_folder, "config.json")
        except Exception as e:
            logger.debug(
                f"Could not load config file path, but this is expected for the first initialization in nebula.py, {e}"
            )
        self.CONFIG = self.load_config()
        self.AVAILABLE_TOOLS = AVAILABLE_TOOLS
        try:
            if self.engagement_folder:
                self.update_paths()
                logger.debug("Updated paths")
            else:
                logger.debug("Did not locate engagement folders")
        except Exception as e:
            logger.debug(f"An Error occurred: {e}")

    def setengagement_folder(self, newengagement_folder):
        self.engagement_folder = newengagement_folder
        self.update_paths()
        logger.debug(f"Home directory changed to: {self.engagement_folder}")

    def update_paths(self):
        self.load_config()
        logger.debug(f"config before it is loaded into update_paths {self.CONFIG}")

        self.LOG_DIRECTORY = os.path.join(self.engagement_folder, "command_output")
        self.SUGGESTIONS_NOTES_DIRECTORY = os.path.join(
            self.engagement_folder, "suggestions_notes"
        )
        self.PRIVACY_DIR = os.path.join(self.engagement_folder, "privacy")
        self.HISTORY_FILE = os.path.join(self.engagement_folder, "history.txt")
        self.SCREENSHOTS_DIR = os.path.join(self.engagement_folder, "screenshots")
        self.AUTONOMOUS_DIRECTORY = os.path.join(self.engagement_folder, "autonomous")

        self.CERTIFICATES_DIRECTORY = os.path.join(
            self.engagement_folder, "certificates"
        )
        try:
            for directory in [
                constants.NEBULA_DIR,
                self.SCREENSHOTS_DIR,
                constants.SYSTEM_LOGS_DIR,
                self.LOG_DIRECTORY,
                self.SUGGESTIONS_NOTES_DIRECTORY,
                self.PRIVACY_DIR,
                self.AUTONOMOUS_DIRECTORY,
                self.CERTIFICATES_DIRECTORY,
            ]:
                self.create_directory(directory)

            self.AVAILABLE_TOOLS.sort()
            updated_config = {
                "CONFIG_FILE_PATH": os.path.join(self.engagement_folder, "config.json"),
                "LOG_DIRECTORY": self.LOG_DIRECTORY,
                "SUGGESTIONS_NOTES_DIRECTORY": self.SUGGESTIONS_NOTES_DIRECTORY,
                "PRIVACY_FILE": os.path.join(self.PRIVACY_DIR, "privacy.txt"),
                "HISTORY_FILE": self.HISTORY_FILE,
                "SCREENSHOTS_DIR": self.SCREENSHOTS_DIR,
                "ENGAGEMENT_FOLDER": self.engagement_folder,
                "SELECTED_TOOLS": self.safe_get_selected_tools(self.CONFIG),
                "AUTONOMOUS_DIRECTORY": self.AUTONOMOUS_DIRECTORY,
                "CERTIFICATES_DIRECTORY": self.CERTIFICATES_DIRECTORY,
            }
            if "AVAILABLE_TOOLS" not in self.CONFIG:
                updated_config["AVAILABLE_TOOLS"] = self.AVAILABLE_TOOLS

            # Update config with model, cache directory, and internet search preference
            # from engagement_details.json if it exists.
            engagement_details_path = os.path.join(
                self.engagement_folder, "engagement_details.json"
            )
            if os.path.exists(engagement_details_path):
                with open(engagement_details_path, "r") as f:
                    details = json.load(f)
                updated_config["MODEL"] = details.get(
                    "model", "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
                )
                updated_config["CACHE_DIR"] = details.get(
                    "cache_dir",
                    os.getenv(
                        "TRANSFORMERS_CACHE",
                        os.path.join(
                            os.path.expanduser("~"),
                            ".cache",
                            "huggingface",
                            "transformers",
                        ),
                    ),
                )
                updated_config["USE_INTERNET_SEARCH"] = details.get(
                    "use_internet_search", False
                )

            self.CONFIG.update(updated_config)
            self.save_config(self.CONFIG_FILE_PATH, self.CONFIG)
        except Exception as e:
            logger.error(f"An error occurred during configuration: {e}")

    def safe_get_selected_tools(self, config):
        try:
            if isinstance(config, dict) and "SELECTED_TOOLS" in config:
                selected_tools = config.get("SELECTED_TOOLS", [])
                logger.debug(f"loaded selected tools is {selected_tools}")
                return selected_tools if isinstance(selected_tools, list) else []
            else:
                return []
        except Exception as e:
            logger.debug(f"Error retrieving 'SELECTED_TOOLS': {e}")
            return []

    def create_directory(self, directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            logger.error(f"An error occurred while creating the directory: {e}")

    def load_config(self):
        try:
            with open(self.CONFIG_FILE_PATH, "r") as file:
                self.CONFIG = json.load(file)
                return self.CONFIG
        except Exception as e:
            logger.debug(f"unable to load config: {e}")
            self.CONFIG = {}
            return self.CONFIG

    def save_config(self, config_path, config):
        try:
            with open(config_path, "w") as file:
                json.dump(config, file, indent=4)
                logger.debug(f"Configuration saved to {config_path}")
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")
