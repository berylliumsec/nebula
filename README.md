# Nebula

Welcome to Nebula.

![nebula](/images/nebula.png)

## Galaxy

- [Acknowledgement](#Acknowledgement)
- [Overview](#overview)
- [Installation](#installation)
- [Usage](#usage)
- [DocuNebula](#DocuNebula)
- [Contributing](#contributing)
- [License](#license)

## Acknowledgement

First i would like to thank the AllMighty God who is the source of all knowledge, without Him, this would not be possible.

## Overview
![nebula](/images/overview.png)
Nebula represents the forefront of technological innovation, serving as an AI-powered assistant specifically designed for the field of ethical hacking. It provides a unique capability for users to input commands using natural language processing, facilitating a seamless transition from intent to execution.

Additionally, Nebula offers a  hacking command search engine. Ethical hackers can effortlessly search for protocols, ports, or specific terms. In response, Nebula provides curated suggestions on commands that can aid in identifying potential vulnerabilities.

Currently, Nebula is integrated with and supports the following renowned ethical hacking tools:

- NMAP: A versatile tool for network discovery and security auditing.
- OWASP ZAP: A highly regarded web application security scanner.
- Crackmap: A robust network information gathering tool.

Our roadmap envisions Nebula's continuous expansion to incorporate the majority of the leading tools leveraged by ethical hackers globally. This commitment ensures that our users remain at the cutting edge of cybersecurity endeavors.

## Usage

There are two primary applications for Nebulla:

- Functioning as a dedicated search engine.
- Serving as an AI-driven assistant (currently in beta).

Within the search engine capability, ethical hackers can input port numbers or specific protocol names. In return, they will receive recommended commands to assist in the identification of potential vulnerabilities. Please refer to the GIF provided below for an illustrative example.

![nebula](/images/search.gif)


Queries can be presented naturally to the AI-driven assistant, which then translates them into specific commands. In the current beta release, it is essential for the ethical hacker to have prior familiarity with this tool to formulate valid inquiries effectively. Refer to the example provided below for a demonstration.

![nebula](/images/nmap.gif)


## DocuNebula

Upon initial access to Nebula, users are greeted with several options:

- Enter a new command (c)
- View previous results (v)
- Process previous results (currently limited to NMAP) (PR)
- Select a model (m)
- Search by keywords (s)
- Exit the application (q)

Enter a New Command: This prompt allows users to input commands using natural language. Subsequently, the system predicts and suggests a command for execution. Users have the discretion to either execute the generated command as is or modify it. After initiating the command, they can choose to await its completion or proceed with other tasks.

![Enter a New Command](/images/command.png)



View Previous Results: After a command's execution, users can review the output via this option.

![View Previous Results](/images/view_results.png)

Process Previous Results: Exclusively available for NMAP results, this feature empowers users to select prior results and receive command recommendations to evaluate the vulnerability of exposed ports. Users can chose a command to run, modify it and execute it. 

![Process Previous Results](/images/process_results.png)

![Run Suggested Command](/images/run_processed_results.png)

Select a Model: Users can choose from one of the three available natural language processing models.

![Model Selection](/images/model_selection.png)

Search by keywords: By leveraging this feature, users can input keywords—such as port numbers or protocol names—and obtain command suggestions to identify vulnerabilities related to that specific protocol.

![Search](/images/search.png)

## Contributing

Should you encounter inaccuracies in the model's responses to your natural language prompts, or face any other challenges, we kindly request that you create an issue to document the specifics. Your feedback is invaluable to our continuous improvement efforts.