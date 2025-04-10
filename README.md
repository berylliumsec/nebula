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

For CPU-Based Inference(Ollama)(Note that Ollama Supports GPU too):
- At least 16GB of RAM 
- Python 3.11 or higher
- [Ollama](https://ollama.com/)

**Installation Command:**
```bash
python -m pip install nebula-ai --upgrade
```


## Running Nebula

**Important:** 


**Ollama Local Model Based Usage**

Install Ollama and download the three supported models (you can also download only the ones you'd be using):

```bash
 ollama pull mistral
 ollama pull deepseek-r1
 ollama pull llama3.1
 ```

 Run nebula

 ```
 nebula
 ```

**Using docker**

First allow local connections to your X server:

```bash
xhost +local:docker
```

```bash
docker run --rm -it   -e DISPLAY=$DISPLAY   -v /home/YOUR_HOST_NAME/.local/share/nebula/logs:/root/.local/share/nebula/logs -v YOUR_ENGAGEMENT_FOLDER_ON_HOST_MACHINE:/engagements -v /tmp/.X11-unix:/tmp/.X11-unix   berylliumsec/nebula:latest
```
### Interacting with the models. 

To interact with the models, begin your input with a `!` for example: `! write a python script to scan the ports of a remote system`

## Key Features

- **AI-Powered Internet Search via agentes:**  
  Enhance responses by integrating real-time, internet-sourced context to keep you updated on cybersecurity trends. "whats in the news on cybersecurity today"
  
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
- **Status feed:**  
  This panel displays your most recent penetration testing activities, it refreshes every five minutes


## Getting Started

For a comprehensive video guide visit [here](https://www.berylliumsec.com/nebula-pro-feature-guide) and [here](https://www.youtube.com/playlist?list=PLySxaLbLL0gpAaDQYq6g6sb1q6KwqOAr4). Please note that some features are only applicable to Nebula Pro.You can also access the help screen within Nebula or refer to the [Manual.md](/MANUAL.md) document

### Roadmap

- Create custom models that are more useful for penetration testing

### Troubleshooting

Logs are located at `/home/[your_username]/.local/share/nebula/logs`. You would most likely find the reason for the error in one of those logs

## Get More Support

- Have questions or need help? [Open an Issue](https://github.com/berylliumsec/nebula/issues) on GitHub.
- For comprehensive guides, check out our [Video Guide](https://www.berylliumsec.com/nebula-pro-feature-guide) and [User Manual](/MANUAL.md).

