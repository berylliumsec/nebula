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
- [Links to videos](#links-to-videos)
- [Contributing](#contributing)


## Acknowledgement

First i would like to thank the All-Mighty God who is the source of all knowledge, without Him, this would not be possible.

**DISCLAIMER**

**Please do not use this tool in a production environment, we are still experimenting and it is currently only suitable for testing environments**

At the moment, to utilize the tools provided in this repository effectively, users are expected to possess a proficient understanding of nmap, nuclei, zap, and crackmap. 

In future versions, we'll focus on enhancing free natural language capabilities, and we're dedicated to making this vision a reality. 

For now, the models within operate on nuanced natural language patterns. For optimal interactions, users are advised to consult the guidance provided in this README, which offers insights into crafting effective prompts. 

Please be advised that this repository is currently in its beta phase; as such, occasional discrepancies or bugs might be encountered. We are diligently working towards refining and resolving all known issues.


## Why nebula?

The youtube video below provide a quick example of how Nebula can speed up the process of identifying vulnerabilities

[Nebula Usage Youtube Video](https://www.youtube.com/watch?v=FF8LEyRmqHk)

- Enhanced Vulnerability Identification and Exploitation: Nebula will execute a series of scripts to detect potential vulnerabilities. Leveraging AI-driven algorithms, it will subsequently try to exploit identified vulnerabilities.

- Effortless Tool Usage with Natural Language: No need to remember intricate commands or switches for various tools. With Nebula, you can seamlessly communicate your intent, whether it's initiating an NMAP scan or any other task. Let Nebula translate your natural language into precise tool commands.

- Direct Command Line Access: Execute suggested commands without having to copy and paste.

- Monitor the IP addresses and ports you've engaged with during a penetration test using the Nebula-Watcher tool to ensure complete coverage. Visit [nebula-watcher](https://github.com/berylliumsec/nebula_watcher) for more information.


**Disclaimer**: Only NMAP is currently supported for smart analysis of tool output.

- Smart Analysis of Tool Outputs: Whether it's the open ports from an NMAP scan or results from other tools, Nebula provides tailored suggestions to further investigate and identify potential vulnerabilities.

- Search for commands that help identify Vulnerabilities: Easily search and retrieve commands that aid in detecting vulnerabilities across a range of services. Whether you're dealing with HTTP, FTP, or SMB, Nebula guides you through.


## Overview

![nebula](/images/overview.png)
Nebula is an AI-powered assistant specifically designed for the field of ethical hacking. It provides a unique capability for users to input commands using natural language processing, facilitating a seamless transition from intent to execution.

Additionally, Nebula offers a command search engine. Ethical hackers can effortlessly search for services, ports, or specific terms. In response, Nebula provides curated suggestions on commands that can aid in identifying potential vulnerabilities.

Currently, Nebula is integrated with and supports the following ethical hacking tools:

- NMAP: A versatile tool for network discovery and security auditing.
- OWASP ZAP (Full Scan Only): A popular web application security scanner.
- Crackmapexec: A robust network information gathering tool.
- Nuclei: A tool is used to send requests across targets based on a template, leading to zero false positives and providing fast scanning on a large number of hosts.

Our roadmap envisions Nebula's continuous expansion to incorporate the majority of the tools leveraged by ethical hackers globally. This commitment ensures that our users remain at the cutting edge of cybersecurity endeavors.

## Compatibility

Non-Docker versions of Nebula have been extensively tested and optimized for Linux platforms. As of now, its functionality on Windows or macOS is not guaranteed, and it may not operate as expected.

## System dependencies

- Storage: A minimum of 50GB is required.

- RAM: A minimum of 16GB RAM memory is required

- Graphics Processing Unit (GPU): While not mandatory, having at least 8GB of GPU memory is recommended for optimal performance.

**Docker based distribution requirement(s)**

- [Docker](https://docs.docker.com/engine/install/)

**PYPI based distribution requirement(s)**

- [Python3](https://www.python.org/downloads/)
- libreadline-dev:

Linux (debian based):
```bash
sudo apt install -y libreadline-dev
```
- wget:

Linux (debian based):
```bash
sudo apt install -y wget
```
- [Docker](https://docs.docker.com/engine/install/)
- [NMAP](https://nmap.org/download)
- [crackmapexec](https://github.com/byt3bl33d3r/CrackMapExec/wiki/Installation)
- [Nuclei](https://docs.nuclei.sh/getting-started/install)


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
docker run --rm -it berylliumsec/nebula:latest
```

To avoid downloading the models each time you run the docker container, mount a directory to store the models like so:

```bash
docker run --rm -v "$(pwd)":/app/unified_models_no_zap -it berylliumsec/nebula:latest
```

Running the model with all GPU(s)

```bash
docker run --rm --gpus all -v "$(pwd)":/app/unified_models_no_zap -it nebula:latest

```

```bash
docker run --rm --gpus all -v "$(pwd)":/app/unified_models -it nebula:latest
```


**PIP**:

```
pip install nebula-ai
```

To run nebula simply run this command:

```bash 
nebula
``` 

For performing operations that require elevated privileges, consider installing via sudo

```bash
sudo pip install nebula-ai
```

Then run:

```bash
sudo nebula
```


**OPTIONAL nebula-watcher installation**

**PIP**:

To install nebula-watcher:

```bash
pip3 install nebula-watcher
```

**Docker**

Pulling the image:

``` bash
docker pull berylliumsec/nebula_watcher:latest
```
Running the docker image :


```bash
docker run --network host -v directory_that_contains_nmap_results/nmap_plain_text_or_xml:/app/results -v where/you/want/the/diagram:/app/output  berylliumsec/nebula_watcher:latest
```

To change the diagram name from the default:

```bash
docker run --network host -v directory_that_contains_nmap_results/nmap_plain_text_or_xml:/app/results -v where/you/want/the/diagram:/app/output  berylliumsec/nebula_watcher:latest python3 nebula_watcher.py --diagram_name /app/your_diagram_name
```

## Upgrading

To maintain optimal performance and benefit from the latest improvements, we regularly release updates and enhanced versions of our models. Prior to upgrading, please delete the unified_models directory to ensure the latest models are downloaded seamlessly.

PIP:

```bash
pip install nebula-ai --upgrade
```

```bash
pip3 install nebula-watcher --upgrade
```

Docker:

``` bash
docker pull berylliumsec/nebula:latest
```

``` bash
docker pull berylliumsec/nebula_watcher:latest
```


## Usage.

In this beta release, there are three primary applications for Nebula:

- As an auto-exploitation engine.
- As a dedicated search engine.
- As an AI-driven assistant (currently in beta).
- A command suggestion engine.

### As an auto-exploitation Engine

Using the [autonomous mode](#autonomous-mode-experimental), ethical hackers can supply a list of targets in a file named targets.txt. Nebula will run an NMAP vulnerability scan and then attempt
to exploit the vulnerabilities using a combination of scripts and AI.

### As a search engine:

Within the search engine capability, ethical hackers can input port numbers or specific service names. In return, they will receive recommended commands to assist in the identification of potential vulnerabilities.

**Pro Tip**: For optimal results, search using service names or port numbers. This approach is more effective than entering a full sentence or broad query.

### As an AI-driven assistant.
**DISCLAIMER**: The results provided by this tool may contain inaccuracies or may not be suitable for all scenarios. We highly recommend users to review and, if necessary, modify the suggested commands before executing them. Proceed with caution and always ensure you are acting within legal and ethical boundaries
Queries can be presented naturally to the AI-driven assistant, which then translates them into specific commands. 

In the current beta release, it is essential for the ethical hacker to have prior familiarity with this tool to formulate valid inquiries effectively. Refer to the example provided below for a demonstration.

**Pro Tip**: **In the beta version, for optimal performance, limit your queries to a combination of up to two switches. Also, queries should be formulated as commands rather than questions, see the [natural language examples](#examples-of-natural-language-queries) section for more information**

### As a command suggestion engine.

Nebula can process results from NMAP scans (plain text or XML format) and suggest commands to run to detect vulnerabilities on services running on open ports.

### As an ethical hacking coverage tool

Using the optional [Nebula-Watcher](https://github.com/berylliumsec/nebula_watcher), ethical hackers can automatically monitor the IP addresses and ports that they have engaged with during a penetration test to ensure maximum coverage . 

## DocuNebula.

### Options

To view Nebula's options run

```
nebula -h
```

```bash
usage: nebula.py [-h] [--results_dir RESULTS_DIR] [--model_dir MODEL_DIR] [--testing_mode TESTING_MODE] [--targets_list TARGETS_LIST]
                 [--autonomous_mode AUTONOMOUS_MODE] [--attack_mode ATTACK_MODE]

Interactive Command Generator

options:
  -h, --help            show this help message and exit
  --results_dir RESULTS_DIR
                        Directory to save command results
  --model_dir MODEL_DIR
                        Path to the model directory
  --testing_mode TESTING_MODE
                        Run vulnerability scans but do not attempt any exploits
  --targets_list TARGETS_LIST
                        lists of targets for autonomous testing
  --autonomous_mode AUTONOMOUS_MODE
                        Flag to indicate autonomous mode
  --attack_mode ATTACK_MODE
                        Attack approach
```
### Autonomous Mode (Experimental).

Nebula can be run in autonomous mode or manual mode.

To activate the autonomous mode, run:

```bash
nebula --autonomous_mode True
```

Nebula will run an initial vulnerability scan, parse the results, attempt to discover more vulnerabilities. Your targets should be placed in a plain text file
titled `targets.txt`. This is also customizable by using the `--targets_list` arg:

If Nebula recognizes any CVEs, it will try to exploit them. By default, the commands are limited to 1 commands per service/port. You can set the number of commands like this:

For up to 5 commands per service with open ports:

```bash
nebula --autonomous_mode True --attack_mode raid
```

To run every possible command per service with open ports:

```bash
nebula --autonomous_mode True --attack_mode war
```

If you want to do a dry-run of autonomous mode so that it does not attempt to exploit any vulnerabilities, set testing mode to `True`:

**Note that testing mode will still perform a vulnerability scan, but it will not attempt to discover more vulnerabilities or attempt to exploit them**

```bash
nebula --autonomous_mode True --testing_mode True
```

For bruteforce/password spraying attacks `(if available or recommended by AI)`, provide these files `usernames.txt` and `passwords.txt` in the directory where you run nebula from.

Depending on how many IP addresses you provide, you may have several files to review. After Nebula is done in autonomous mode, it will drop into manual mode where you can view the results. 

**Note that results will only be written to a file if it is not empty.**

### Manual mode.

Upon initial access to Nebula, users are greeted with several options:

- Enter a new command (c).
- View previous results (v) (if there are files in the results directory).
- Process previous results (currently limited to NMAP) (PR).
- Select a model (m).
- Search by keywords (s).
- Exit the application (q).

**Enter a New Command**: This prompt allows users to input commands using natural language. Subsequently, the system predicts and suggests a command for execution. Users have the discretion to either execute the generated command as is or modify it. After initiating the command, they can choose to await its completion or proceed with other tasks. Queries should be formulated as commands rather than questions, see the [natural language examples](#examples-of-natural-language-queries) section for more information.

**Pro Tip**: In the beta version, for optimal performance, limit your queries to a combination of up to two switches.

![Enter a New Command](/images/command.png)

In the above screenshot, the user asks the NMAP model to perform a top 10 port-scan on 192.168.1.1.

**View Previous Results**: After a command's execution, users can review the output via this option.

![View Previous Results](/images/view_results.png)

In the above screenshot, the user reviews the output of the top 10 port-scan.

**Process Previous Results**: Currently optimized for NMAP results (in plain text or XML format), Nebula enables users to select previous scan findings and obtain command suggestions to assess potential vulnerabilities of uncovered ports. Users have the flexibility to select a suggested command, modify it if needed, and execute. Support for results from other tools is planned for future releases.

![Process Previous Results](/images/process_results.png)

In the above screenshot, the user asks Nebula to process the results of the top 10 port scan, Nebula provides suggestions on what commands to run, and the user chooses suggestion number #8 and runs the corresponding command after editing it.


![Run Suggested Command](/images/run_processed_results.png)

In the above screenshot, the user views the results of running the suggested command. In this case, the result is a list of HTTP methods supported by 192.168.1.1.

**Select a Model**: Users can choose from one of the three available natural language processing models (note that the actual name of the models/version may vary from what is seen in this screenshot).

![Model Selection](/images/model_selection.png)

**Search by keywords**: By leveraging this feature, users can input keywords—such as port numbers or service names—and obtain command suggestions to identify vulnerabilities related to that specific service.

**Pro Tip**: For optimal results, search using service names or port numbers. This approach is more effective than entering a full sentence or broad query.

![Search](/images/search.png)

**Results Storage**: A folder named `results` is created (if it does not already exist) in the working directory where nebula is invoked.

**Escape prompt screen without entering a prompt**: To escape the prompt screen without entering a prompt, simply hit the enter key.

## Examples of natural language queries

**Although the models are trained to be able to construct commands from natural languages, there are some nuances to get the right results**:
**General rules**

- Use numbers instead of the word equivalent, for example use `10` instead of ten.
- Use all caps for abbreviations, for example use `SMB` instead of smb.
- Keep your commands as short as possible.

**NMAP**:

**Note that for nmap commands, -oX and -oN are automatically appended. The plain text version is for you to be able to easily read while the xml version is for processing, please do not remove them**

- Always end the command with the IP addresses and always refer to IP addresses as hosts regardless of whether its a subnet or not. For example:

`do a top 10 scan on host 192.168.1.1`


- If you want to do a vulnerability scan using a script, be sure to mention the "script" keyword as there are many ways NMAP can detect vulnerabilities that do not involve the scripting engine. For example:

`discover vulnerabilities using a script on host 192.168.1.1`

- Always place ports before the host, so if you want to discover vulnerabilities on port 80 on host 192.168.1.1 do:

`discover vulnerabilities on port 80 on host 192.168.1.1`

More examples:

- do OS detection on list of hosts in file.txt
- discover vulnerabilities on port 80 on host 192.168.1.1
- do a ping scan on random targets in host 192.168.1.0/24 and exclude host 192.168.1.9
- do service detection on host 192.168.1.1

**CRACKMAPEXEC**:

For crackmap, always include a username and a password in your prompt or indicate that you would like to use a null session:

- enumerate users on host 192.168.1.1 using a null session
- show disks on host 192.168.1.0/24 using username nebula and password joey
- check 192.168.1.1 for unconstrained delegation using a null username and password

**ZAP**:

The ZAP model currently only supports "full scan". Be sure to use the terms full scan in your commands.

- do a full scan on https://192.168.1.1
- do a full scan on https://192.168.1.1 and spider for 5 minutes
- do a full scan on https://192.168.1.1 and spider for 5 minutes and write the report to html file nebula.html

**Nuclei**

- do an automatic scan using only new templates on https://yourtarget.com
- use only templates from author "joe" on https://yourtarget.com

## Links to videos:

[Model Usage Video](https://youtu.be/Rz3DzuvX6bI)

[Search Usage Video](https://youtu.be/j-Ot3UxEwSY)

## Contributing

Should you encounter inaccuracies in the model's responses to your natural language prompts, or face any other challenges, we kindly request that you create an issue to document the specifics. Your feedback is invaluable to our continuous improvement efforts.