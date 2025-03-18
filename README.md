# Nebula – AI-Powered Penetration Testing Assistant

Nebula is an advanced, AI-powered penetration testing open-source tool that revolutionizes penetration testing by integrating state-of-the-art AI models into your command-line interface. Designed for cybersecurity professionals, ethical hackers, and developers, Nebula automates vulnerability assessments and enhances security workflows with real-time insights and automated note-taking.


![Nebula AI-Powered Penetration Testing CLI Interface](/images/nebula.webp)

## Acknowledgement

**First i would like to thank the All-Mighty God who is the source of all knowledge, without Him, this would not be possible.**

## News

Introducing the Deep Application Profiler (DAP). DAP uses neural networks to analyze an executable's internal structure and intent, rather than relying on traditional virus signatures. This approach enables it to detect new, zero-day malware that conventional methods often miss. DAP also provides detailed breakdowns for rapid analyst review and is available as both a web service and an API. [Learn More Here](https://www.berylliumsec.com/dap-overview)


Introducing Nebula Pro, Nebula Pro improves on Nebula 2.0 by adding additional features such as autonomous mode, code analysis and more. [Learn More Here](https://www.berylliumsec.com/nebula-pro-overview)

## Nebula: AI-Powered Penetration Testing Platform

Nebula is a cutting-edge, AI-powered penetration testing tool designed for cybersecurity professionals and ethical hackers. It integrates advanced open-source AI models such as Meta's Llama-3.1-8B-Instruct, Mistralai's Mistral-7B-Instruct-v0.2, and DeepSeek-R1-Distill-Llama-8B—directly into the command line interface (CLI). By leveraging these state-of-the-art models, Nebula not only enhances vulnerability assessments and penetration testing workflows but also supports any tool that can be invoked from the CLI.


## Installation

**System Requirements:**

For GPU-Based Inference (huggingface):

- At least 8GB of GPU memory (tested with 12GB)
- Python 3.11 or higher

For CPU-Based Inference(Ollama)(Note that Ollama Supports GPU too):
- At least 16GB of RAM 
- Python 3.11 or higher
- [Ollama](https://ollama.com/)

### Installation Methods

#### Quick Installation (recommended)

**For Linux/macOS**:
```bash
# Clone the repository
git clone https://github.com/berylliumsec/nebula.git
cd nebula

# Run the installation script
chmod +x install_linux.sh
./install_linux.sh
```

**For Windows**:
```powershell
# Clone the repository
git clone https://github.com/berylliumsec/nebula.git
cd nebula

# Run the installation script (in PowerShell)
.\install_windows.ps1
```

#### Manual Installation

**Using pip**:
```bash
python -m pip install nebula-ai --upgrade
```

**From Source**:
```bash
# Clone the repository
git clone https://github.com/berylliumsec/nebula.git
cd nebula

# Install dependencies
pip install -r requirements.txt

# Install Nebula
pip install -e .
```

## Running Nebula

**Important:** 

**Hugging Face Local Model Based Usage** 
On your first run, you'll be prompted to select a cache directory where Nebula will download your chosen AI model. Follow these steps:

1. Create a free [Hugging Face Account](https://huggingface.co/), agree to the terms, and generate an access token.
2. Export your token to the CLI:
   ```bash
   # Linux/macOS
   export HF_TOKEN=YourTokenHere
   
   # Windows (PowerShell)
   $env:HF_TOKEN="YourTokenHere"
   ```
3. Launch Nebula and monitor the download progress on the CLI.

   ```bash
   nebula
   ```

This step only needs to be completed once. Monitor the command line interface where you invoked `nebula` from to monitor the download progress.

**Ollama Local Model Based Usage**

Install Ollama and download the three supported models (you can also download only the ones you'd be using):

```bash
 ollama pull mistral
 ollama pull deepseek-r1
 ollama pull llama3.1
 ```
### Interacting with the models. 

To interact with the models, begin your input with a `!` for example: `! write a python script to scan the ports of a remote system`

## Key Features

- **AI-Powered Internet Search via agentes:**  
  Enhance responses by integrating real-time, internet-sourced context to keep you updated on cybersecurity trends. "whats in the news on cybersecurity today"
- **AI Agents:**  
  AI Agents that execute commands on your local system based..."run an nmap scan against 192.168.1.1 without trigerring firewalls"
- **AI-Assisted Note-Taking:**  
  Automatically record and categorize security findings.

- **Real-Time AI-Driven Insights:**  
  Get immediate suggestions for discovering and exploiting vulnerabilities based on terminal tool outputs.

- **Enhanced Tool Integration:**  
  Seamlessly import data from external tools for AI-powered note-taking and advice.

- **Integrated Screenshot & Editing:**  
  Capture and annotate images directly within Nebula for streamlined documentation.

- **Manual Note-Taking & Automatic Command Logging:**  
  Maintain a detailed log of your actions and findings with both automated and manual note-taking features.


## Getting Started

For a comprehensive video guide visit [here](https://www.berylliumsec.com/nebula-pro-feature-guide) and [here](https://www.youtube.com/playlist?list=PLySxaLbLL0gpAaDQYq6g6sb1q6KwqOAr4). Please note that some features are only applicable to Nebula Pro.You can also access the help screen within Nebula or refer to the [Manual.md](/MANUAL.md) document

### Roadmap

- Support more models

### Troubleshooting

Logs are located at `/home/[your_username]/.local/share/nebula/logs`. You would most likely find the reason for the error in one of those logs

On Windows, logs are at `C:\Users\[your_username]\.local\share\nebula\logs`.

#### Testing Your Installation

To verify that your installation is working correctly, you can run the test script:

**For Linux/macOS**:
```bash
python test_installation.py
```

**For Windows**:
```powershell
python test_installation_windows.py
```

These scripts will check:
- Python version compatibility
- Required modules installation
- Existence of necessary directories
- Availability of the Nebula command

## Get More Support

- Have questions or need help? [Open an Issue](https://github.com/berylliumsec/nebula/issues) on GitHub.
- For comprehensive guides, check out our [Video Guide](https://www.berylliumsec.com/nebula-pro-feature-guide) and [User Manual](/MANUAL.md).