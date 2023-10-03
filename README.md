# Nebula

Welcome to Nebula.

![nebula](/images/nebula.png)

## Galaxy

- [Acknowledgement](#Acknowledgement)
- [Why?](#Why-nebula?)
- [Overview](#overview)
- [Compatibility](#compatibility)
- [System dependencies](#system-dependencies)
- [Installation](#installation)
- [Usage](#usage)
- [DocuNebula](#DocuNebula)
- [Examples of natural language queries](#examples-of-natural-language-queries)
- [Contributing](#contributing)
- [License](#license)

## Acknowledgement

First i would like to thank the All-Mighty God who is the source of all knowledge, without Him, this would not be possible.

## Why nebula?

- Effortless Tool Usage with Natural Language: No need to remember intricate commands or switches for various tools. With Nebula, you can seamlessly communicate your intent, whether it's initiating an NMAP scan or any other task. Let Nebula translate your natural language into precise tool commands.

- Direct Command Line Access: Execute suggested commands without having to copy and paste.

**Disclaimer**: Only NMAP is currently supported for smart analysis of tool output.

- Smart Analysis of Tool Outputs: Whether it's the open ports from an NMAP scan or results from other tools, Nebula provides tailored suggestions to further investigate and identify potential vulnerabilities.

- Search for commands that help identify Vulnerabilities: Easily search and retrieve commands that aid in detecting vulnerabilities across a range of protocols. Whether you're dealing with HTTP, FTP, or SMB, Nebula guides you through.

## Overview
![nebula](/images/overview.png)
Nebula is an AI-powered assistant specifically designed for the field of ethical hacking. It provides a unique capability for users to input commands using natural language processing, facilitating a seamless transition from intent to execution.

Additionally, Nebula offers a command search engine. Ethical hackers can effortlessly search for protocols, ports, or specific terms. In response, Nebula provides curated suggestions on commands that can aid in identifying potential vulnerabilities.

Currently, Nebula is integrated with and supports the following ethical hacking tools:

- NMAP: A versatile tool for network discovery and security auditing.
- OWASP ZAP (Full Scan Only): A popular web application security scanner.
- Crackmapexec: A robust network information gathering tool.

Our roadmap envisions Nebula's continuous expansion to incorporate the majority of the tools leveraged by ethical hackers globally. This commitment ensures that our users remain at the cutting edge of cybersecurity endeavors.

## Compatibility

Nebula has been extensively tested and optimized for Linux and MacOS platforms. As of now, its functionality on Windows is not guaranteed, and it may not operate as expected.

## System dependencies

- Storage: A minimum of 50GB is required.

- Graphics Processing Unit (GPU): While not mandatory, having at least 4GB of GPU memory is recommended for optimal performance.

**Docker based distribution requirement(s)**

- [Docker](https://docs.docker.com/engine/install/)

**PYPI based distribution requirement(s)**

- [Python3](https://www.python.org/downloads/)
- libreadline-dev:

Linux (debian based):
```bash
sudo apt install -y libreadline-dev
```
MacOS:
```bash
brew install readline
```
- [NMAP](https://nmap.org/download)
- [crackmapexec](https://github.com/byt3bl33d3r/CrackMapExec/wiki/Installation)


## Installation

The easiest way to get started is to use the docker image. Please note that the ZAP model is NOT supported in the docker image. If you would like to use ZAP please install the package using `pip`.

**PRO TIP**: Regardless of if you are using the docker or pip version, always run nebula in the same folder so that it doesn't have to download the models each time you run it.

**Docker**:

Pulling the image:

``` bash
docker pull berylliumsec/nebula:latest
```
Running the image without GPU:

```bash
docker run -it berylliumsec/nebula:latest
```

To avoid downloading the models each time you run the docker container, mount a directory to store the models like so:

```bash
docker run -v "$(pwd)":/app/unified_models_no_zap -it berylliumsec/nebula:latest
```

Running the model with all GPU(s)

```bash
docker run --gpus all -v "$(pwd)":/app/unified_models_no_zap -it nebula:latest

```

```bash
docker run --gpus all -v "$(pwd)":/app/unified_models -it nebula:latest
```

**PIP**:

```
pip install nebula
```

To run nebula simply enter `nebula` in your command line interface.


## Usage

In this beta release, there are three primary applications for Nebula:

- As a dedicated search engine.
- As an AI-driven assistant (currently in beta).
- A command suggestion engine.

### As a search engine:

Within the search engine capability, ethical hackers can input port numbers or specific protocol names. In return, they will receive recommended commands to assist in the identification of potential vulnerabilities. Please refer to the GIF provided below for an illustrative example.

**Pro Tip**: For optimal results, search using protocol names or port numbers. This approach is more effective than entering a full sentence or broad query.

![nebula](/images/search.gif)


### As an AI-driven assistant
**DISCLAIMER**: The results provided by this tool may contain inaccuracies or may not be suitable for all scenarios. We highly recommend users to review and, if necessary, modify the suggested commands before executing them. Proceed with caution and always ensure you are acting within legal and ethical boundaries
Queries can be presented naturally to the AI-driven assistant, which then translates them into specific commands. 

In the current beta release, it is essential for the ethical hacker to have prior familiarity with this tool to formulate valid inquiries effectively. Refer to the example provided below for a demonstration.

**Pro Tip**: In the beta version, for optimal performance, limit your queries to a combination of up to two switches.

### As a command suggestion engine.

Nebula can process results from NMAP scans and suggest commands to run to detect vulnerabilities on services running on open ports.


## DocuNebula

Upon initial access to Nebula, users are greeted with several options:

- Enter a new command (c).
- View previous results (v) (if there are files in the results directory).
- Process previous results (currently limited to NMAP) (PR).
- Select a model (m).
- Search by keywords (s).
- Exit the application (q).

**Enter a New Command**: This prompt allows users to input commands using natural language. Subsequently, the system predicts and suggests a command for execution. Users have the discretion to either execute the generated command as is or modify it. After initiating the command, they can choose to await its completion or proceed with other tasks.

**Pro Tip**: In the beta version, for optimal performance, limit your queries to a combination of up to two switches.
![Enter a New Command](/images/command.png)

In the above screenshot, the user asks the NMAP model to perform a top 10 port-scan on 192.168.1.1.

**View Previous Results**: After a command's execution, users can review the output via this option.

![View Previous Results](/images/view_results.png)

In the above screenshot, the user reviews the output of the top 10 port-scan.

**Process Previous Results**: Currently optimized for NMAP results, Nebula enables users to select previous scan findings and obtain command suggestions to assess potential vulnerabilities of uncovered ports. Users have the flexibility to select a suggested command, modify it if needed, and execute. Support for results from other tools is planned for future releases.

![Process Previous Results](/images/process_results.png)

In the above screenshot, the user asks Nebula to process the results of the top 10 port scan,. Nebula provides suggestions on what commands to run, and the user chooses suggestion number #8 and runs the corresponding command after editing it.


![Run Suggested Command](/images/run_processed_results.png)

In the above screenshot, the user views the results of running the suggested command. In this case, the result is a list of HTTP methods supported by 192.168.1.1.

**Select a Model**: Users can choose from one of the three available natural language processing models (note that the actual name of the models/version may vary from what is seen in this screenshot).

![Model Selection](/images/model_selection.png)

**Search by keywords**: By leveraging this feature, users can input keywords—such as port numbers or protocol names—and obtain command suggestions to identify vulnerabilities related to that specific protocol.

**Pro Tip**: For optimal results, search using protocol names or port numbers. This approach is more effective than entering a full sentence or broad query.
![Search](/images/search.png)

**Results Storage**: A folder named `results` is created (if it does not already exist) in the working directory where nebula is invoked.

**Escape prompt screen without entering a prompt**: To escape the prompt screen without entering a prompt, simply hit the enter key.

## Examples of natural language queries

**NMAP**:
- do OS detection of list of hosts in file.txt
- do a Maimon scan and OS detection on 192.168.1.1
- do a ping scan on random targets in 192.168.1.0/24 and exclude host 192.168.1.9

**CRACKMAPEXEC**:

- enumerate users on  192.168.1.1 using a null session
- show disks on 192.168.1.0/24 using username nebula and password joey
- check 192.168.1.1 for unconstrained delegation using a null username and password

**ZAP**:

The ZAP model currently only supports full scan. 
- do a full scan on https://192.168.1.1
- do a full scan on https://192.168.1.1 and spider for 5 minutes
- do a full scan on https://192.168.1.1 and spider for 5 minutes and write the report to html file nebula.html

## Contributing

Should you encounter inaccuracies in the model's responses to your natural language prompts, or face any other challenges, we kindly request that you create an issue to document the specifics. Your feedback is invaluable to our continuous improvement efforts.