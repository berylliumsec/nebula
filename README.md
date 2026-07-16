# Nebula – Security Testing Workbench

Nebula 3 is the primary application in this repository. Install the native
desktop package, open **Nebula**, and start in the Workbench; terminal use does
not require a model provider. Nebula keeps human terminal work inside a
verified Docker or Podman runtime and never falls back to a host shell.

## Install and launch Nebula 3

Install a signed macOS DMG/Homebrew cask or Linux DEB/AppImage from the
[Nebula releases](https://github.com/BerylliumSec/nebula/releases). Docker or
Podman must be installed separately for terminal and automation features.

Launch **Nebula** from the operating-system application menu or run:

```console
nebula
```

The `nebula` command opens the desktop application. Administration and
diagnostics are deliberately separate:

```console
nebula-core doctor --json
nebula-core migrate
```

For a source checkout, see [the Nebula 3 guide](docs/NEBULA3.md). Existing
Nebula 2 data can be imported without changing its source; see
[Migrating from Nebula 2](docs/MIGRATING-2-TO-3.md). The
[automation runtime guide](docs/AUTOMATION-RUNTIME.md) documents the isolated command boundary.
The [local diagnostics guide](docs/NEBULA3_DIAGNOSTICS.md) documents structured
feature logs, privacy controls, error references, and support bundles.

Nebula 3 is currently versioned separately from the maintenance-only Nebula 2
Python distribution. The `nebula3` source command remains as a compatibility
alias for existing development workflows.

Nebula is designed for cybersecurity professionals, ethical hackers, and
developers. It combines a terminal-first security workspace with optional AI
assistance, immutable evidence, reviewed execution, and reporting.

## Nebula 2 maintenance distribution

Nebula 2.0.0 is now available. Because earlier releases were published as beta versions, pip may have kept some existing installations on an older version even when `--upgrade` was used. Users running Python 3.11 through 3.13 should upgrade with:

```bash
python -m pip install --upgrade nebula-ai
```

Verify the installed version with:

```bash
python -m pip show nebula-ai
```

The output should show `Version: 2.0.0`. Python 3.14 is not currently supported; use Python 3.13 or earlier when installing Nebula 2.0.0.


![Nebula AI-Powered Penetration Testing CLI Interface](/images/Nebula.png)

## Acknowledgement

**First i would like to thank the All-Mighty God who is the source of all knowledge, without Him, this would not be possible.**

## News

Introducing the Deep Application Profiler (DAP). DAP uses neural networks to analyze an executable's internal structure and intent, rather than relying on traditional virus signatures. This approach enables it to detect new, zero-day malware that conventional methods often miss. DAP also provides detailed breakdowns for rapid analyst review and is available as both a web service and an API. [Learn More Here](https://berylliumsec.com/malware-analysis)


## Nebula: AI-Powered Penetration Testing Platform

Nebula is a cutting-edge, AI-powered penetration testing tool designed for cybersecurity professionals and ethical hackers. It integrates both hosted models available through the OpenAI API and open-source models such as Meta's Llama-3.1-8B-Instruct, Mistral AI's Mistral-7B-Instruct-v0.2, and DeepSeek-R1-Distill-Llama-8B directly into the command line interface (CLI). By leveraging these state-of-the-art models, Nebula not only enhances vulnerability assessments and penetration testing workflows but also supports any tool that can be invoked from the CLI.


## Nebula 2 installation

**System Requirements:**

For CPU-Based Inference(Ollama)(Note that Ollama Supports GPU too):
- At least 16GB of RAM 
- Python 3.11 – 3.13
- [Ollama](https://ollama.com/)

**Installation Command:**
```bash
python -m pip install nebula-ai --upgrade
```


## Running Nebula 2

**Important:** 


**Ollama Local Model Based Usage**

[Install Ollama](https://ollama.com/download/mac) and download your preferred models for example

```bash
 ollama pull mistral
```
Then select **Ollama** as the AI provider and enter the model's exact name as it
appears in Ollama in the engagement settings.

**OpenAI Models Usage**

To use OpenAI models, add your API keys to your env like so

```bash
export OPENAI_API_KEY="sk-blah-blaj"
```

Then select **OpenAI** as the AI provider and enter the OpenAI model's exact name
in the engagement settings. Provider selection is explicit: the presence of
`OPENAI_API_KEY` does not change an engagement from Ollama to OpenAI. For
headless compatibility, set `NEBULA_AI_PROVIDER=openai` or
`NEBULA_AI_PROVIDER=ollama`.

### Legacy-agent safety

The Nebula 2.x AI assistant does not receive a host shell tool by default.
Commands shown in AI responses are suggestions for the operator to review and
run in the human-controlled terminal. The old direct-shell behavior is available
only as an unsafe compatibility option by setting
`NEBULA_UNSAFE_MODEL_SHELL=1` (or `ALLOW_UNSAFE_MODEL_SHELL` in an engagement's
`config.json`). This grants the model unsandboxed command execution and is not
recommended.


Run the maintenance application installed from PyPI:

```
nebula
```

**Nebula 3 headless deployment**

The legacy root/X11 container is not a Nebula 3 release path. Run the non-root,
loopback-published Core profile with an explicit API token:

```bash
export NEBULA_V3_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f compose.v3.yaml up --build
```

No Docker/Podman socket is mounted. The service therefore remains analysis-only
until a separately administered worker with enforced egress is configured.
### Interacting with the models. 

To interact with the models, begin your input with a `!` or use the AI/Terminal button to switch between modes. For example: `! write a python script to scan the ports of a remote system` the "!" is not needed if you use the context button

## Key Features

- **Optional AI-Powered Internet Search:**
  When `use_internet_search` is enabled for an engagement, the assistant may use
  DuckDuckGo search. It is disabled otherwise; retrieved content is untrusted and
  should be reviewed before acting on it.
  
- **AI-Assisted Note-Taking:**
  Draft report-style notes from selected terminal output for analyst review.

- **Real-Time AI-Driven Insights:**  
  Get immediate suggestions for discovering and exploiting vulnerabilities based on terminal tool outputs.

- **Enhanced Tool Integration:**  
  Seamlessly import data from external tools for AI-powered note-taking and advice.

- **Integrated Screenshot & Editing:**  
  Capture and annotate images directly within Nebula for streamlined documentation.

- **Manual Note-Taking & Automatic Command Logging:**  
  Maintain a detailed log of your actions and findings with both automated and manual note-taking features.
  
- **Status feed:**  
  This panel displays your most recent penetration testing activities, it refreshes every five minutes


### Roadmap

- Create custom models that are more useful for penetration testing

### Troubleshooting

Logs are located at `/home/[your_username]/.local/share/nebula/logs`. You would most likely find the reason for the error in one of those logs

## Get More Support

- Have questions or need help? [Open an Issue](https://github.com/berylliumsec/nebula/issues) on GitHub.
