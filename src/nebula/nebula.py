import argparse
import ast
import gc
import json
import logging
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from importlib.metadata import version
from importlib.resources import path as resource_path
from typing import List, Optional, Tuple, Union

import psutil
import pynvml
import requests
import torch
from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator
from spellchecker import SpellChecker
from termcolor import colored, cprint
from tqdm import tqdm
from transformers import GPT2LMHeadModel, GPT2Tokenizer
from transformers import logging as trans_log
from whoosh.analysis import StandardAnalyzer
from whoosh.fields import ID, TEXT, Schema
from whoosh.index import open_dir
from whoosh.qparser import MultifieldParser, OrGroup

libraries_and_functions_cli = {
    "argparse": True,
    "optparse": True,
    "getopt": True,
}

# New mapping for input prompt functions
libraries_and_functions_input = {
    "input": True,
    "raw_input": True,  # Python 2
    "prompt_toolkit.shortcuts.prompt": True,
    "PyInquirer.prompt": True,
    "click.prompt": True,
    "sys.stdin.read": True,
}


class InputAnalyzer(ast.NodeVisitor):
    def __init__(self):
        self.detected_cli_libs = set()
        self.detected_input_libs = set()
        self.options = {}
        self.has_input = False
        self.imported_libraries = set()
        self.is_python2 = False

    def visit_Import(self, node):
        for n in node.names:
            self.imported_libraries.add(n.name)
            if n.name in libraries_and_functions_cli:
                self.detected_cli_libs.add(n.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module
        self.imported_libraries.add(module)
        for n in node.names:
            full_name = f"{module}.{n.name}"
            if full_name in libraries_and_functions_cli:
                self.detected_cli_libs.add(full_name)
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            # Check for command-line arguments methods (argparse, optparse)
            if (
                node.func.attr == "add_argument"
                and "argparse" in self.detected_cli_libs
            ):
                option_name = None
                help_text = None
                if node.args and isinstance(node.args[0], ast.Str):
                    option_name = node.args[0].s
                for keyword in node.keywords:
                    if keyword.arg == "help" and isinstance(keyword.value, ast.Str):
                        help_text = keyword.value.s
                if option_name:
                    self.options[option_name] = help_text
            elif (
                node.func.attr in ["add_option", "option"]
                and "optparse" in self.detected_cli_libs
            ):
                option_name = None
                help_text = None
                if node.args and isinstance(node.args[0], ast.Str):
                    option_name = node.args[0].s
                for keyword in node.keywords:
                    if keyword.arg == "help" and isinstance(keyword.value, ast.Str):
                        help_text = keyword.value.s
                if option_name:
                    self.options[option_name] = help_text

            # Check for input functions using attribute access (e.g., module.function())
            full_name = None
            if hasattr(node.func.value, "id"):
                full_name = f"{node.func.value.id}.{node.func.attr}"
            elif hasattr(node.func.value, "attr"):
                full_name = f"{node.func.value.attr}.{node.func.attr}"
            if full_name and full_name in libraries_and_functions_input:
                self.has_input = True
                self.detected_input_libs.add(full_name)

        elif isinstance(node.func, ast.Name):
            # Check for direct input function calls (e.g., input())
            if node.func.id in libraries_and_functions_input:
                self.has_input = True
                self.detected_input_libs.add(node.func.id)
            elif node.func.id == "getopt" and "getopt" in self.detected_cli_libs:
                # Process getopt options
                if node.args and isinstance(node.args[0], ast.Str):
                    options = node.args[0].s.split()
                    for opt in options:
                        self.options[opt] = None
            elif node.func.id == "print" and not isinstance(node, ast.Call):
                # Check for Python 2 print statement
                self.is_python2 = True
        self.generic_visit(node)


trans_log.set_verbosity_error()
analyzer = StandardAnalyzer(stoplist=None)
schema = Schema(
    title=TEXT(stored=True, analyzer=analyzer),
    path=ID(stored=True),
    content=TEXT(stored=True, analyzer=analyzer),
)
logging.basicConfig(
    filename="command_errors.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
MAX_RESULTS_DEFAULT = 1e6


class ActionChoiceValidator(Validator):
    def validate(self, document):
        text = document.text.lower().strip()
        if text not in ["yes", "y", "no", "n", "always", "a"]:
            raise ValidationError(
                message="Invalid choice. Please enter 'y', 'n', or 'a'."
            )


class FunctionValidator(Validator):
    def __init__(self, validation_function):
        self.validation_function = validation_function

    def validate(self, document):
        if not self.validation_function(document.text):
            raise ValidationError(message="Invalid input. Please try again.")


class WordValidator(Validator):
    def __init__(self, accepted_words):
        self.accepted_words = accepted_words

    def validate(self, document):
        if document.text.lower() not in self.accepted_words:
            raise ValidationError(message="Please enter a valid choice.")


class InteractiveGenerator:
    # Define the IP pattern
    # This ip pattern is very generous and should change in the future
    IP_PATTERN = re.compile(
        r"\b\d{1,3}\.\d{1,3}(?:\.\d{1,3}(?:\.\d{1,3}(?:/\d{1,2})?)?)?\b"
    )

    FLAG_PATTERN = re.compile(
        r"(?<!\d{2}:\d{2}:\d{2})-\w+|(?<!\d{4}-\d{2}-\d{2})--[\w-]+"
    )  # Updated Regular expression
    URL_PATTERN_VALIDATION = r"http[s]?://(?:[a-zA-Z]|[0-9]|[-._~:/?#[\]@!$&'()*+,;=]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    CVE_PATTERN = re.compile(
        r"CVE-\d{4}-\d{4,7}", re.IGNORECASE
    )  # Regular expression for CVE pattern

    def __init__(self, results_dir=None, model_dir=None, testing_mode=None):
        self.args = self._parse_arguments()
        if results_dir is not None:
            self.args.results_dir = results_dir
        if model_dir is not None:
            self.args.model_dir = model_dir
        if testing_mode is not None:
            self.args.testing_mode = testing_mode
        self.image_name = "berylliumsec/nebula"
        self.docker_hub_api_url = (
            f"https://hub.docker.com/v2/repositories/{self.image_name}/tags/"
        )
        self.index_dir = self.return_path("indexdir")
        self.s3_url = self._determine_s3_url()
        self._ensure_model_folder_exists()
        self._validate_model_dirs()
        self.command_running = False
        self._ensure_results_directory_exists()
        self.max_truncate_length: int = 500
        self.single_model_mode = False
        self.tokenizers = {}
        self.models = {}
        self.print_star_sky()
        self.log_file_path = None
        self.services = []
        self.flag_file = None
        self.flag_descriptions = None
        self.extracted_flags = []
        self.random_name = None
        self.current_model = None
        self.current_model_name = None
        self.model_names = self.get_model_names()
        self.always_apply_action: bool = False
        self.suggestions_file = self.return_path("suggestions")
        with open(self.suggestions_file, "r") as f:
            # Each line in the file is a word you want to exclude
            self.words_to_exclude = [line.strip() for line in f]
        self.suggestions = self.get_suggestions()
        if self.is_run_as_package():
            self.check_new_pypi_version()  # Check for newer PyPI package
        if self.args.autonomous_mode is True:
            self.single_model_mode = True
            self.autonomous_mode()
        self.args.autonomous_mode = False
        if self.args.testing_mode:
            cprint("testing completed, exiting...", "green")
            exit()
        while True:  # This loop will keep asking until a valid mode is selected
            mode = self.select_mode()
            if mode == "a":
                self.current_model_name = self._load_tokenizer_and_model()
                break
            elif mode == "d":
                self.single_model_mode = True
                self.current_model_name = self._select_model()
                break
            else:
                cprint("\nInvalid Choice", "red")

    @staticmethod
    def _parse_arguments():
        parser = argparse.ArgumentParser(description="Interactive Command Generator")
        parser.add_argument(
            "--results_dir",
            type=str,
            default="./results",
            help="Directory to save command results",
        )
        parser.add_argument(
            "--model_dir",
            type=str,
            default="./unified_models",
            help="Path to the model directory",
        )
        parser.add_argument(
            "--testing_mode",
            type=bool,
            default=False,
            help="Run vulnerability scans but do not attempt any exploits",
        )
        parser.add_argument(
            "--targets_list",
            type=str,
            default="targets.txt",
            help="lists of targets for autonomous testing",
        )
        parser.add_argument(
            "--autonomous_mode",
            type=bool,
            default=False,
            help="Flag to indicate autonomous mode",
        )
        parser.add_argument(
            "--attack_mode",
            type=str,
            default="stealth",
            help="Attack approach",
        )
        parser.add_argument(
            "--nmap_vuln_scan_command",
            type=str,
            default="nmap -Pn -sV --script=vuln,exploit,vulscan/vulscan.nse",
            help="Nmap vulnerability scan command to run",
        )
        parser.add_argument(
            "--lan_or_wan_ip",
            type=str,
            help="Your lan or wan ip for metasploit tests",
        )
        parser.add_argument(
            "--exploit_db_base_location",
            type=str,
            default="/opt/exploit-database/",
            help="the base location of exploit_db files",
        )

        return parser.parse_args()

    def _extract_python_files(self, text):
        """Extract .py files from the given text."""
        return [word for word in text.split() if word.endswith(".py")]

    def _analyze_and_modify_python_file(self, file):
        """Analyze a Python file and prompt the user for input options."""
        if not os.path.exists(file):
            cprint(f"Warning: File {file} does not exist.", "red")
            return None

        try:
            with open(file, "r") as f:
                content = f.read()
                tree = ast.parse(content)
                analyzer = InputAnalyzer()
                analyzer.visit(tree)

                if not analyzer.detected_cli_libs:
                    cprint(
                        f"{file} does not use recognized input libraries, please run it manually.",
                        "red",
                    )
                    return None
                if analyzer.detected_input_libs:
                    cprint(
                        f"{file} has direct user prompt embedded in it and cannot be ran automatically, please run it manually.",
                        "red",
                    )
                    return None
                options = []
                cprint(
                    "IMPORTANT: I will attempt to guide you through the process of using this exploit...",
                    "green",
                )
                for option, description in analyzer.options.items():
                    prompt_text = f"Enter value for {option}"
                    if description:
                        prompt_text += f" ({description})"
                    prompt_text += " (or 'q' to stop): "
                    cprint(prompt_text, "yellow", end="")
                    value = input()
                    if value == "q":
                        cprint("Operation aborted by user.", "red")
                        return None
                    options.extend([option, value])

                return options

        except (FileNotFoundError, SyntaxError) as e:
            cprint(
                f"Warning: File {file} had an issue ({str(e)}). Run it manually.", "red"
            )
            return None

    def split(self, data):
        return data.split(": ", 1)

    def unique_commands_based_on_params(self, commands, ip_address):
        """
        Given a list of commands, returns all commands with unique sets of significant parameters.
        Excludes commands with nothing after the colon, commands with parameter '-p', and commands containing '-*'.
        Replaces [IP] with a provided IP address.
        """
        seen_params = set()
        unique_cmds = []

        for command in commands:
            # Extract the part of the command after the colon, if it exists
            parts = self.split(command)
            if len(parts) != 2 or not parts[1].strip():
                continue

            actual_command = parts[1].strip().rstrip(".").replace("[IP]", ip_address)

            try:
                # Split the command into tokens
                tokens = shlex.split(actual_command)
            except ValueError:
                print(f"Error splitting command: {actual_command}")
                logging.error(f"Error splitting command: {actual_command}")
                continue

            # Identify parameters in the tokens, excluding '-p'
            params = frozenset(
                token
                for token in tokens
                if (token.startswith("--") or token.startswith("-")) and token != "-p"
            )

            # Check if we've seen these parameters before
            if params not in seen_params:
                seen_params.add(params)
                unique_cmds.append(actual_command)

        return unique_cmds

    def is_simple_port_scan(self, nmap_command):
        # Ensure the command is an nmap command
        if "nmap" not in nmap_command:
            return False

        # Check if script is in the command
        if "--script" in nmap_command:
            return False

        return True

    def construct_query_for_models(self, services):
        commands = []
        url = ""
        for model_name in self.model_names:
            if model_name in ["scribe"]:
                continue
            self._load_tokenizer_and_model(model_name)

            for data in services:
                ip = data["ip"]
                for port, service in zip(data["ports"], data["services"]):
                    if (model_name == "nuclei" or model_name == "zap") and port not in [
                        "80",
                        "443",
                    ]:
                        continue
                    constructed_query = f"{service} on {ip}"
                    if model_name == "nmap":
                        constructed_query = f"run all {service} vulnerability scripts on port {port} on {ip}"
                    if port in ["80", "443"] and model_name == "nuclei":
                        url = f"https://{ip}" if port == "443" else f"http://{ip}"
                        constructed_query = f"run an automatic scan and employ only new templates on {url}"
                    if port in ["80", "443"] and model_name == "zap":
                        # to remove
                        cprint(f'{port},"red')
                        url = f"https://{ip}" if port == "443" else f"http://{ip}"
                        constructed_query = f"scan {url}"

                    elif model_name == "crackmap":
                        constructed_query = f"how can i find vulnerabilities in the {service} service using a null session on {ip}"

                    cprint(f"Constructed query: {constructed_query}", "green")
                    generated_text = self.generate_text(constructed_query.strip())
                    if model_name == "nmap":
                        cleaned_text = self.ensure_space_between_letter_and_number(
                            generated_text
                        )
                        clean_up = self.process_string(
                            cleaned_text, [ip], [url], [port]
                        )
                    else:
                        clean_up = self.process_string(
                            self.ensure_space_between_letter_and_number(generated_text),
                            [ip],
                            [url],
                        )
                    commands.append(clean_up)
        return list(set(commands))

    def autonomous_mode(self):
        timestamp = datetime.now().strftime("%I:%M:%S-%p-%Y-%m-%d").replace(" ", "-")

        def get_number_of_results(mode):
            return {"stealth": 1, "raid": 5, "war": 1e6}.get(mode, 1)

        def handle_command(command):
            if self.is_simple_port_scan(command) or command in command_history:
                cprint(f"Not running duplicate or useless command: {command}", "red")
                return
            cprint(f"Running command: {command}", "yellow")
            if not self.args.testing_mode:
                self.run_command_and_alert(command, timestamp)
            match = re.search(r"^(.*?)-oX", command)
            if match:
                command = match.group(1)
            command_history.append(command)

        if not os.path.exists(self.args.targets_list):
            logging.error("The specified targets file does not exist, exiting...")
            cprint("The specified targets file does not exist, exiting...", "red")
            return

        command_history = []
        results = []

        cprint(
            f"Running nmap vulnerability scans against {self.args.targets_list}, it may take several minutes please wait...",
            "yellow",
        )
        output_xml = f"{self.args.results_dir}/nmap_output_{timestamp}.xml"
        output_txt = f"{self.args.results_dir}/nmap_output_{timestamp}.txt"
        if self.args.testing_mode:
            result = self.run_command_and_alert(
                f"nmap -Pn --script=vuln -iL {self.args.targets_list} -oX {output_xml} -oN {output_txt}",
                timestamp,
            )
        else:
            cprint(
                f"nmap command passed in via args: {self.args.nmap_vuln_scan_command}",
                "green",
            )
            result = self.run_command_and_alert(
                f"{self.args.nmap_vuln_scan_command}  -iL {self.args.targets_list} -oX {output_xml} -oN {output_txt}",
                timestamp,
            )
        results.append(result)

        cprint(f"nmap scanning completed for {self.args.targets_list}", "green")

        processed_data = self._parse_nmap_xml(output_xml)
        number_of_results = get_number_of_results(self.args.attack_mode)
        search_results = self.search_index(
            processed_data, self.return_path("indexdir_auto"), number_of_results
        )

        for data in processed_data:
            unique_commands = self.unique_commands_based_on_params(
                search_results, data["ip"]
            )
            for comm in tqdm(unique_commands, desc="Processing commands"):
                try:
                    ip = data["ip"]

                    handle_command(self.process_string(comm, [ip]))
                except Exception as e:
                    logging.error(f"Unable to run command, error: {e}")
                    cprint("Unable to run command, error", "red")

        cprint("Consulting models", "yellow")
        commands = self.construct_query_for_models(processed_data)
        for command in commands:
            handle_command(command)

    def select_mode(self):
        style = Style.from_dict(
            {
                "prompt": "cyan",
            }
        )

        action = (
            prompt(
                "\nYou can choose to load all models at once or on demand (for computers with less RAM/No GPU(s)) \n (a) load all (d) on demand? [a/d]: ",
                style=style,
            )
            .strip()
            .lower()
        )

        return action

    def print_farewell_message(self, width=30, height=10, density=0.5):
        # Calculate the position to print the farewell message
        farewell_msg = "Until our stars align again!!"
        start_x = (width - len(farewell_msg)) // 2
        start_y = height // 2

        star_colors = [
            "cyan",
            "magenta",
            "yellow",
            "blue",
        ]  # Nebula-themed colors for stars

        for y in range(height):
            for x in range(width):
                # Check if we are at the position to print the farewell message
                if y == start_y and start_x <= x < start_x + len(farewell_msg):
                    print(
                        colored(farewell_msg[x - start_x], "green", attrs=["bold"]),
                        end="",
                    )
                    continue

                if random.random() < density:
                    chosen_star = "*" if random.random() < 0.5 else "."
                    print(colored(chosen_star, random.choice(star_colors)), end="")
                else:
                    print(" ", end="")
            print()  # Move to the next line after each row

    def print_star_sky(self, width=30, height=10, density=0.5):
        # Calculate the position to print the welcome message
        welcome_msg = "Welcome to Nebula"
        start_x = (width - len(welcome_msg)) // 2
        start_y = height // 2

        star_colors = [
            "cyan",
            "magenta",
            "yellow",
            "blue",
        ]  # Nebula-themed colors for stars

        for y in range(height):
            for x in range(width):
                # Check if we are at the position to print the welcome message
                if y == start_y and start_x <= x < start_x + len(welcome_msg):
                    print(
                        colored(welcome_msg[x - start_x], "green", attrs=["bold"]),
                        end="",
                    )
                    continue

                if random.random() < density:
                    chosen_star = "*" if random.random() < 0.5 else "."
                    print(colored(chosen_star, random.choice(star_colors)), end="")
                else:
                    print(" ", end="")
            print()  # Move to the next line after each row

    def _load_flag_descriptions(self, file_path, selected_model_name):
        """Load flag descriptions from a file and return them as a dictionary."""
        try:
            if file_path is None:
                raise ValueError("file_path cannot be None")
            with open(file_path, "r") as f:
                lines = f.readlines()
                # Store the entire line as the value in the dictionary using flag as key
                return {
                    line.split(":")[0].strip(): line.strip()
                    for line in lines
                    if ":" in line
                }
        except Exception as e:
            cprint(
                f"Flags file '{file_path}' not found, commands for '{selected_model_name}' will not contain descriptions",
                "yellow",
            )
            logging.error({e})
            return {}

    def extract_and_match_flags(self, command):
        """Extract flags from the command and match them with descriptions."""
        flags = self.FLAG_PATTERN.findall(command)

        # Using set comprehension to ensure uniqueness
        matched_descriptions = list(
            {
                self.flag_descriptions[flag]
                for flag in flags
                if flag in self.flag_descriptions
            }
        )

        self.extracted_flags.extend(matched_descriptions)
        return matched_descriptions

    @staticmethod
    def get_latest_pypi_version(package_name):
        """Return the latest version of the package on PyPI."""
        response = requests.get(f"https://pypi.org/pypi/{package_name}/json")
        if response.status_code == 200:
            return response.json()["info"]["version"]

    def check_new_pypi_version(self, package_name="nebula-ai"):
        """Check if a newer version of the package is available on PyPI."""
        installed_version = version(package_name)
        cprint(f"installed version: {installed_version}", "green")
        latest_version = self.get_latest_pypi_version(package_name)

        if latest_version and latest_version > installed_version:
            cprint(
                f"A newer version ({latest_version}) of {package_name} is available on PyPI. Please consider updating to access the latest features!",
                "yellow",
            )

    def _get_installed_docker_version(self):
        try:
            # Run docker version command and get Client Version
            result = subprocess.run(
                ["docker", "version", "--format", "{{.Client.Version}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return result.stdout.decode("utf-8").strip()
        except subprocess.CalledProcessError:
            return None

    def get_current_version(self):
        # This assumes you have a way of determining your current version
        # Perhaps an environment variable or a file in the container with the version
        # For this example, I'll assume an environment variable named "DOCKER_APP_VERSION"
        current_version = os.environ.get("DOCKER_APP_VERSION")
        if not current_version:
            raise ValueError(
                "Could not determine current version from environment variable."
            )
        return current_version

    def get_latest_version(self):
        response = requests.get(self.docker_hub_api_url)
        response.raise_for_status()
        data = response.json()
        # This assumes that the most recent version is the first in the list of tags
        latest_version = data["results"][0]["name"]
        return latest_version

    def check_for_update(self):
        current_version = self.get_current_version()
        latest_version = self.get_latest_version()

        if current_version != latest_version:
            cprint(
                f"You are running version {current_version}, but version {latest_version} is available!",
                "yellow",
            )
            print("Consider updating for the latest features and improvements.")
        else:
            cprint(f"You are running the latest version ({current_version}).", "greens")

    def return_path(self, path):
        if self.is_run_as_package():
            with resource_path("nebula", path) as correct_path:
                return str(correct_path)
        else:
            return path

    @staticmethod
    def _determine_s3_url():
        return (
            "https://nebula-models.s3.amazonaws.com/unified_models_no_zap.zip"
            if os.environ.get("IN_DOCKER")
            else "https://nebula-models.s3.amazonaws.com/unified_models.zip"
        )

    def get_s3_file_etag(self, s3_url):
        response = requests.head(s3_url)
        return response.headers.get("ETag")

    def save_local_metadata(self, file_name, etag):
        with open(file_name, "w") as f:
            json.dump({"etag": etag}, f)

    def get_local_metadata(self, file_name):
        try:
            with open(file_name, "r") as f:
                data = json.load(f)
                return data.get("etag")
        except FileNotFoundError:
            return None

    def _ensure_model_folder_exists(self):
        metadata_file = "metadata.json"
        local_etag = self.get_local_metadata(metadata_file)
        s3_etag = self.get_s3_file_etag(self.s3_url)

        if not self.folder_exists_and_not_empty(self.args.model_dir) or (
            local_etag != s3_etag
        ):
            if local_etag != s3_etag:
                user_input = self.get_input_with_default(
                    "New versions of the models are available, would you like to download them? (y/n) "
                )

                if user_input.lower() != "y":
                    return

                # Logic to remove unified_model directory if it exists
                if os.path.exists(self.args.model_dir):
                    cprint("Removing existing unified_model folder...", "yellow")
                    shutil.rmtree(self.args.model_dir)

            cprint(
                f"{self.args.model_dir} not found or is different. Downloading and unzipping...",
                "yellow",
            )
            self.download_and_unzip(self.s3_url, f"{self.args.model_dir}.zip")
            # Save new metadata
            self.save_local_metadata(metadata_file, s3_etag)
        else:
            cprint(
                f"found {self.args.model_dir}, to download new models remove {self.args.model_dir} or invoke nebula from a different directory",
                "green",
            )

    def _validate_model_dirs(self):

        while True:
            try:
                model_dirs = [
                    d
                    for d in os.listdir(self.args.model_dir)
                    if os.path.isdir(os.path.join(self.args.model_dir, d))
                ]
                if not model_dirs:
                    raise Exception(
                        "No model directories found in the specified directory."
                    )
                break
            except Exception as e:
                cprint(f"Error: {e}.", "red")
                cprint("Download the models at the next prompt", "blue")
                self._ensure_model_folder_exists()

    def _ensure_results_directory_exists(self):
        if not os.path.exists(self.args.results_dir):
            os.makedirs(self.args.results_dir)

    def is_run_as_package(self):
        # Check if the script is within a 'site-packages' directory
        if os.environ.get("IN_DOCKER"):
            self.check_for_update()
            return False
        return "site-packages" in os.path.abspath(__file__)

    def folder_exists_and_not_empty(self, folder_path):
        # Check if the folder exists
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return False
        # Check if the folder is empty
        if not os.listdir(folder_path):
            return False
        return True

    def download_and_unzip(self, url, output_name):
        try:
            # Download the file from the S3 bucket using wget with progress bar
            print("Downloading...")
            subprocess.run(
                ["wget", "--progress=bar:force:noscroll", url, "-O", output_name]
            )

            # Create the target directory if it doesn't exist
            target_dir = os.path.splitext(output_name)[0]
            os.makedirs(target_dir, exist_ok=True)

            # Unzip the downloaded file to the target directory
            print("\nUnzipping...")
            subprocess.run(["unzip", output_name, "-d", target_dir])

            os.remove(output_name)
        except subprocess.CalledProcessError as e:
            cprint(f"Error occurred: {e}", "red")
            logging.error(f"Error occurred: {e}")
        except Exception as e:
            cprint(f"Unexpected error: {e}", "red")
            logging.error(f"Unexpected error: {e}")

    def run_command_and_alert(self, text: str, timestamp=None) -> None:
        if timestamp is None:
            timestamp = (
                datetime.now().strftime("%I:%M:%S-%p-%Y-%m-%d").replace(" ", "-")
            )

        """
        A function to run a command in the background, capture its output, and print it to the screen.
        """

        def execute_command(command: Union[str, List[str]]) -> Tuple[int, str, str]:
            """
            Executes the provided command and returns the returncode, stdout, and stderr.
            """
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    text=True,
                )
                stdout, stderr = process.communicate()

                # Reset the terminal to a sane state after subprocess execution
                os.system("stty sane")
                os.system("stty echo")

                return process.returncode, stdout, stderr
            except Exception as e:
                logging.error(f"Error while executing command {command}: {e}")
                return -1, "", str(e)

        if not self.args.autonomous_mode:
            cprint(
                "\nExecuting command, you can choose the view previous command option in the main menu to view the results when command execution has been completed",
                "yellow",
            )

        if isinstance(text, list):
            command_str = " ".join(text)
        else:
            command_str = text

        # Execute the command
        returncode, stdout, stderr = execute_command(command_str)
        truncated_cmd = command_str[:10].replace(" ", "_") + (
            "..." if len(command_str) > 15 else ""
        )
        truncated_cmd = self.remove_slashes(truncated_cmd)

        file_name = f"{timestamp}_{truncated_cmd}"
        result_file_path = os.path.join(
            self.args.results_dir,
            f"result_{file_name}.txt",
        )
        try:
            ET.ElementTree(ET.fromstring(stdout))
            cprint("XML format detected, nothing to do", "green")
            return
        except Exception:

            # Conditions to decide if the results should be written to the file or not
            should_write_stderr = stderr and self.args.autonomous_mode is False
            if stdout.startswith("Starting Nmap"):
                return False
            try:
                if stderr.strip() or stdout.strip():
                    with open(result_file_path, "a") as f:
                        if stdout.strip():
                            f.write("\n" + stdout)

                            return stdout
                        else:
                            if should_write_stderr and stderr.strip():
                                cprint("\nCommand Error Output:", "red")
                                cprint(stderr, "red")

                                f.write(stderr)
                                logging.error(
                                    f"Command '{command_str}' failed with error:\n{stderr}"
                                )
                                cprint("\nhit the enter key to continue", "yellow")
                                return False

            except FileNotFoundError:
                cprint(
                    f"Error: File not found or invalid path: {result_file_path}", "red"
                )
                logging.debug(
                    f"Error: File not found or invalid path: {result_file_path}"
                )
            except Exception as e:
                # Handle or log other exceptions as required
                cprint(f"An error occurred: {e}", "red")
                logging.debug(f"An error occurred: {e}", "red")

    @staticmethod
    def ensure_space_between_letter_and_number(s: str) -> str:
        try:
            if not isinstance(s, str):
                raise ValueError("The input must be a string.")

            segments = s.split(":")

            for i, segment in enumerate(segments[:-1]):
                # If current segment ends with http or looks like a URL or IP prefix
                if segment.strip().endswith(("http", "https")) or re.match(
                    r"\s?(https?://[^\s]*|(?:(?:[0-9a-fA-F]{2}:)+)|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[0-9a-fA-F]{0,4}::[0-9a-fA-F]{0,4})",
                    segment,
                ):
                    continue
                else:
                    s = ":".join(segments[i + 1 :])
                    break


            s = re.sub(r"\.$", "", s)

            return s.strip()

        except Exception as e:
            cprint(f"Error in ensure_space_between_letter_and_number: {e}", "red")
            logging.error(f"Error in ensure_space_between_letter_and_number: {e}")
            return s

    @staticmethod
    def _extract_ip(s: str) -> Optional[str]:
        """
        Extracts the first IP address found in the given string based on a pattern.
        If no IP address is found, it returns None.
        """
        try:
            ips = InteractiveGenerator.IP_PATTERN.findall(s)
            return ips if ips else None
        except Exception as e:
            logging.error(f"Error while extracting IP from string '{s[:30]}...': {e}")
            return None

    def replace_base_location(self, path):
        """
        Replace the placeholder {base_file_location} with new_location.

        :param path: The original path with the placeholder.
        :param new_location: The string to replace the placeholder with.
        :return: The new path with the placeholder replaced.
        """
        return path.replace(
            "{base_file_location}", self.args.exploit_db_base_location.rstrip("/")
        )

    def process_string(
        self,
        s: str = "",
        replacement_ips: Optional[List[str]] = None,
        replacement_urls: Optional[List[str]] = None,
        port_arg: Optional[int] = None,
    ) -> str:

        # Handle the default values
        if replacement_ips is None:
            replacement_ips = []

        if replacement_urls is None:
            replacement_urls = []

        # Handle the default values
        if replacement_ips is None:
            replacement_ips = []

        if replacement_urls is None:
            replacement_urls = []
        """Replace the IP addresses and URLs in the given string with the respective replacements."""

        def get_local_ip() -> str:
            """Get local machine IP"""
            try:
                # This creates a new socket and connects to an external server's port.
                # We use Google's public DNS server for this example, but no data is actually sent.
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                return ip
            except Exception:
                return "127.0.0.1"  # default to loopback, if unable to determine IP

        def get_random_port(above: int = 1024, max_retries: int = 100) -> int:
            """Get random port above the specified number that's not in use."""
            for _ in range(max_retries):
                port = random.randint(above, 65535)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    try:
                        s.bind(("0.0.0.0", port))
                        return port
                    except socket.error:
                        continue
            raise ValueError("Unable to find an available port after multiple retries.")

        try:
            if not isinstance(s, str):
                raise ValueError("The input string must be a string.")
        except Exception as e:
            logging.error(f"Error in input string validation: {e}")

        # IP processing
        try:
            # Check if replacement_ips is a single IP and not a list
            if not isinstance(replacement_ips, list):
                replacement_ips = [replacement_ips]

            for ip in replacement_ips:
                if not re.match(self.IP_PATTERN, ip):
                    raise ValueError(f"One of the replacement IPs ({ip}) is not valid.")

            ip_addresses = re.findall(self.IP_PATTERN, s)
            if ip_addresses:
                for i, ip in enumerate(ip_addresses):
                    if i < len(replacement_ips):
                        s = s.replace(ip, replacement_ips[i])
        except Exception as e:
            logging.error(f"Error in IP processing: {e}")

        # URL processing
        try:
            if not isinstance(replacement_urls, list):
                replacement_urls = [replacement_urls]

            for url in replacement_urls:
                if not re.match(self.URL_PATTERN_VALIDATION, url):
                    raise ValueError(
                        f"One of the replacement URLs ({url}) is not valid."
                    )

            urls = self.extract_urls(s)
            for i, url in enumerate(urls):
                if i < len(replacement_urls):
                    s = s.replace(url, replacement_urls[i])
        except Exception as e:
            logging.error(f"Error in URL processing: {url}{e}")

        # Replace placeholders
        if replacement_ips and len(replacement_ips) > 0:
            primary_ip = replacement_ips[0]
            s = s.replace("{{ RHOSTS }}", primary_ip)
            if port_arg:
                s = s.replace("{{ RPORT }}", str(port_arg))
            s = s.replace(
                "{{ LHOST }}",
                get_local_ip()
                if not self.args.lan_or_wan_ip
                else self.args.lan_or_wan_ip,
            )
            s = s.replace("{{ LPORT }}", str(get_random_port()))
        timestamp = datetime.now().strftime("%I:%M:%S-%p-%Y-%m-%d").replace(" ", "-")
        if s.strip().startswith("nmap"):
            output_xml = f"{self.args.results_dir}/nmap_output_{timestamp}.xml"
            output_txt = f"{self.args.results_dir}/nmap_output_{timestamp}.txt"
            s += f" -oX {output_xml} -oN {output_txt}"

        if s.strip().startswith("nuclei"):
            s = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b\s*$", "", s).strip()
        if "{base_file_location}" in s:
            s = self.replace_base_location(s)
        return s

    def extract_urls(self, s: str) -> list:
        """Extract URLs from the given string.

        Args:
            s (str): The input string.

        Returns:
            list: List of URLs found in the string.
        """
        url_pattern = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
        return re.findall(url_pattern, s)

    def generate_text(self, prompt_text: str, max_length: int = 1024) -> str:
        """
        Generate text using the current model based on the provided prompt.

        Parameters:
        - prompt_text: Initial text to prompt the model for generation.
        - max_length: Maximum length for the generated text.

        Returns:
        - The generated text as a string.
        """
        try:
            if len(prompt_text) > self.current_model.config.n_ctx:
                logging.warning("Prompt too long! Truncating...")
                cprint("Prompt too long! Truncating...", "red")
                prompt_text = prompt_text[: self.current_model.config.n_ctx]

            encoding = self.current_tokenizer.encode_plus(
                prompt_text,
                return_tensors="pt",
                add_special_tokens=True,
                max_length=max_length,
                pad_to_max_length=False,
                return_attention_mask=True,
                truncation=False,
            )

            input_ids = encoding["input_ids"].to(self.device)
            attention_mask = encoding["attention_mask"].to(self.device)
            temp = 0.1
            if self.current_model == "scribe":
                temp = 0.5
            with tqdm(total=max_length, desc="Generating text", position=0) as pbar:
                output = self.current_model.generate(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    max_length=max_length,
                    num_return_sequences=1,
                    do_sample=True,
                    top_k=40,
                    top_p=0.95,
                    temperature=temp,
                    repetition_penalty=1.0,
                )
                pbar.update(len(output[0]))

            generated_text = self.current_tokenizer.decode(
                output[0], skip_special_tokens=True
            )

            cprint(f"generated text: {generated_text}", "red")
            return generated_text

        except Exception as e:
            logging.error(
                f"Error during text generation with prompt '{prompt_text[:30]}...': {e}"
            )
            return ""  # Returning an empty string as a fallback. Adjust based on your requirements.

    def start_interactive_mode(self) -> None:
        """
        Start an interactive session for the user to input prompts and receive generated text.

        In the interactive session:
        - The user is prompted to enter text.
        - The model generates text based on the user's input.
        - The generated text is displayed and handled accordingly.
        - The session can be exited by typing 'exit' or through keyboard interrupts.
        """

        while True:
            try:
                prompt = self.get_user_prompt()

                if not prompt:
                    print("Prompt is empty. Please enter a valid prompt.")
                    continue

                if prompt.lower() == "exit":
                    break

                generated_text = self.generate_and_display_text(prompt)
                self.handle_generated_text(generated_text)

            except (EOFError, KeyboardInterrupt):  # Combined the two exception types
                self.print_farewell_message()
                break
            except Exception as e:  # Handle unexpected errors
                if isinstance(prompt, bool):
                    logging.error(
                        f"Error during interactive session with prompt '{prompt}...': {e}"
                    )
                elif isinstance(prompt, list):
                    logging.error("prompt value is:%s", prompt)
                    logging.error(
                        f"Error during interactive session with prompt '{prompt[:30]}...': {e}"
                    )

    def display_command_list(self, file_extension=None) -> None:
        """
        Display a list of previously saved result filenames truncated to self.max_truncate_length.
        """
        cprint("\nPrevious Results:", "cyan")

        # Fetching filenames from the results directory
        all_files = os.listdir(self.args.results_dir)

        # If a file extension is specified, filter the files by that extension
        if file_extension:
            filenames = sorted(
                [file for file in all_files if file.endswith(file_extension)]
            )
        else:
            filenames = sorted(all_files)
        if not filenames:
            cprint("no results found", "red")
            return
        for idx, filename in enumerate(filenames):
            truncated_filename = (
                (filename[: self.max_truncate_length] + "...")
                if len(filename) > self.max_truncate_length
                else filename
            )
            cprint(f"{idx+1}. {truncated_filename}", "yellow")

    def get_user_prompt(self) -> str:
        """
        Interactively prompt the user for an action or command input.

        Returns:
            str: The user's command input.
        """
        style = Style.from_dict({"prompt": "cyan", "error": "red"})

        while True:
            try:
                if self.command_running:
                    action = (
                        prompt(
                            "\nA command is currently running. Do you want to (w) wait for it to complete, or (c) continue without waiting? [w/c]: ",
                            style=style,
                        )
                        .strip()
                        .lower()
                    )

                    if action == "w":
                        cprint("Waiting for the command to complete...", "yellow")
                        while self.command_running:
                            time.sleep(1)
                        cprint(
                            "Command completed!, you can view the result using the 'view previous results' option on the main menu",
                            "green",
                        )
                        continue

                action = (
                    prompt(
                        "\nDo you want to (c) enter a new command, (v) view previous results, (pr) process previous nmap results,\n(m) select a model, (s) to search by keywords or (q) quit? [c/v/m/s/q]: ",
                        style=style,
                    )
                    .strip()
                    .lower()
                )

                if action == "q":
                    self.print_farewell_message()
                    exit(0)
                elif action == "v":
                    return self._view_previous_results(".txt")
                elif action == "pr":
                    return self._process_previous_nmap_results(".xml")
                elif action == "c":
                    return self._input_command_without_model_selection()
                elif action == "m":
                    self._select_model()
                    continue
                elif action == "s":
                    return self.user_search_interface()
                else:
                    cprint("Invalid option. Please choose a valid option.", "red")

            except Exception as e:
                logging.error(f"Error during user prompt: {e}")
                cprint("An unexpected error occurred. Please try again.", "red")

    def contains_pytorch_model(self, directory: str) -> bool:
        """
        Check if the given directory contains a .bin file (assumed to be a PyTorch model file).
        """
        if not os.path.isdir(directory):  # Ensure it's a directory
            return False

        for filename in os.listdir(directory):
            if filename.endswith(".bin"):
                return True
        return False

    def get_model_names(self):
        self.model_names = [
            model_name
            for model_name in os.listdir(self.args.model_dir)
            if self.contains_pytorch_model(
                os.path.join(self.args.model_dir, model_name)
            )
        ]
        return self.model_names

    def _select_model(self) -> str:
        """
        Interactively allows the user to select a model from the available models.

        Returns:
            str: The name of the selected model.
        """
        selected_model_name = None

        session = PromptSession()

        while True:
            print_formatted_text(
                "Available models:", style=Style.from_dict({"": "yellow"})
            )
            for idx, model_name in enumerate(self.model_names, 1):
                print_formatted_text(
                    f"{idx}. {model_name}", style=Style.from_dict({"": "yellow"})
                )

            choice = session.prompt(
                "Select a model by entering its number: ",
                style=Style.from_dict({"": "cyan"}),
            )

            if choice.isdigit() and 1 <= int(choice) <= len(self.model_names):
                selected_model_name = self.model_names[int(choice) - 1]
                break
            else:
                print_formatted_text(
                    "Invalid choice. Please enter a valid number.",
                    style=Style.from_dict({"": "red"}),
                )

        # Handling the single model mode
        if self.single_model_mode:
            self._load_tokenizer_and_model(selected_model_name)
        else:
            self.current_model = self.models[selected_model_name]
            self.current_tokenizer = self.tokenizers[selected_model_name]

        # Handling model-specific behavior
        if selected_model_name == "nmap":
            self.flag_file = self.return_path("nmap_flags")
        elif selected_model_name == "crackmap":
            self.flag_file = self.return_path("crackmap_flags")
        elif selected_model_name == "nuclei":
            self.flag_file = self.return_path("nuclei_flags")
        elif selected_model_name == "zap":
            self.flag_file = self.return_path("zap_flags")
        elif selected_model_name == "vuln":
            self.flag_file = self.return_path(" ")
        self.flag_descriptions = self._load_flag_descriptions(
            self.flag_file, selected_model_name
        )

        Style.from_dict({"message": "bg:#ff0066 #ffff00"})

        print_formatted_text(
            f"You've selected the {selected_model_name} model!",
            style=Style.from_dict({"": "yellow"}),
        )

        return selected_model_name

    def unload_model(self):
        # 1. Explicitly delete models and tokenizers
        if hasattr(self, "current_model") and self.current_model:
            del self.current_model
            self.current_model = None

        if hasattr(self, "current_tokenizer") and self.current_tokenizer:
            del self.current_tokenizer
            self.current_tokenizer = None

        # 2. Release GPU Memory
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # 3. Clear any cached states in model's `from_pretrained` methods
        # This is specific to transformers library and can be useful for some models
        if hasattr(GPT2LMHeadModel, "clear_cache"):
            GPT2LMHeadModel.clear_cache()

        # 4. Force garbage collection
        gc.collect()

    def _load_tokenizer_and_model(self, model_name: Optional[str] = None):
        # Clear current models and tokenizers if a specific model is to be loaded
        first_loaded = None
        self.device = torch.device(
            "cuda" if torch.cuda.is_available() else "cpu"
        )  # Set the device once

        # List all available model directories or just the specified one
        model_folders = [model_name] if model_name else os.listdir(self.args.model_dir)

        # Iterating over the list of model directories
        for model_folder in model_folders:
            full_path = os.path.join(self.args.model_dir, model_folder)

            # Check if it's a directory to avoid loading from non-directory paths
            if os.path.isdir(full_path):
                try:
                    # Load tokenizer
                    if self.current_model and self.single_model_mode:
                        self.unload_model()
                    cprint(
                        f"Loading tokenizer for {model_folder}...",
                        "yellow",
                        end="",
                        flush=True,
                    )
                    self.current_tokenizer = GPT2Tokenizer.from_pretrained(full_path)
                    cprint(" Done!", "green")

                    # Load model
                    cprint(
                        f"Loading model for {model_folder}...",
                        "yellow",
                        end="",
                        flush=True,
                    )
                    self.current_model = GPT2LMHeadModel.from_pretrained(full_path)
                    self.current_model.eval()
                    self.current_model.to(self.device)
                    cprint(" Done!", "green")

                    # Add the successfully loaded model and tokenizer to their respective dictionaries
                    if not self.single_model_mode:
                        self.tokenizers[model_folder] = self.current_tokenizer
                        self.models[model_folder] = self.current_model

                    # Only set the 'first_loaded' once
                    if first_loaded is None:
                        first_loaded = model_folder

                except Exception as e:
                    cprint(
                        f"Failed to load model/tokenizer from {model_folder}: {e}",
                        "red",
                    )
                    logging.error(
                        f"Failed to load model/tokenizer from {model_folder}: {e}"
                    )

        # If not in single_model_mode or no model was specified, set the first loaded model as the active model
        if (
            not hasattr(self, "single_model_mode") or not self.single_model_mode
        ) and first_loaded:
            self.current_tokenizer = self.tokenizers[first_loaded]
            self.current_model = self.models[first_loaded]
            cprint(f"\nThe current model in use is: {first_loaded}\n", "blue")

        # Memory usage information
        try:
            process = psutil.Process(os.getpid())
            cprint(
                f"CPU memory usage: {process.memory_info().rss / (1024**2):.2f} MB",
                "green",
            )
        except Exception as e:
            cprint(f"An error occurred while fetching CPU memory info: {e}", "red")
            logging.error(f"An error occurred while fetching CPU memory info: {e}")

        try:
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            cprint(f"Used GPU memory: {info.used / (1024**2):.2f} MB", "green")

        except Exception as e:
            cprint(f"An error occurred while fetching GPU memory info: {e}", "red")
            logging.error(f"An error occurred while fetching GPU memory info: {e}")

        return first_loaded

    def _input_command_without_model_selection(self) -> str:
        """Internal method to get a command input from the user without model selection."""

        # Styling definitions
        style = Style.from_dict({"prompt": "cyan", "error": "red", "message": "green"})

        # Initialize the spell checker
        spell = SpellChecker()
        spell.word_frequency.load_words(self.words_to_exclude)
        while True:  # Keep prompting until valid input or 'q' is entered
            # Get the actual user input for the model to generate
            user_input = prompt(
                "\nEnter a prompt (or 'b' to return to the main menu): ", style=style
            ).strip()

            # If the user wants to return to the previous menu
            if user_input.lower() == "b":
                # Here, you can define what should be done to return to the previous menu.
                # For this example, we'll just return an empty string.
                return True

            # If input is empty, prompt again
            if not user_input:
                cprint("Please provide a prompt or enter 'q' to return.", "red")
                continue

            # Split the user input into words
            words = user_input.split()

            # Correct words that are not in the dictionary
            corrections_made = False  # Flag to check if any corrections were accepted
            for index, word in enumerate(words):
                # Check if the word is a number or contains digits; if yes, then continue to the next iteration
                if word.isdigit() or any(char.isdigit() for char in word):
                    continue

                # Check if word is a URL; if yes, continue to the next iteration
                if re.match(
                    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
                    word,
                ):
                    continue

                # If the word is not in the dictionary
                misspelled = spell.unknown([word])
                if misspelled:
                    # Get the most likely correct spelling for the word
                    suggestion = spell.correction(word)

                    # If no suggestions are found, continue to the next iteration
                    if not suggestion:
                        continue

                    # Display the suggestion to the user and ask for their choice
                    try:
                        choice = prompt(
                            f"Did you mean '{suggestion}' instead of '{word}'? (Y/n): ",
                            style=style,
                            validator=WordValidator(["y", "n", ""]),
                            validate_while_typing=False,
                        ).lower()
                    except Exception as e:
                        cprint(f"Error occurred: {e}", "red")
                        logging.error(f"Error occurred: {e}")

                    # If the user accepts the suggestion, replace the word in the list
                    if choice == "y" or choice == "":
                        words[index] = suggestion
                        corrections_made = True  # Update the flag

            if corrections_made:
                corrected_input = " ".join([str(word) for word in words])
            else:
                corrected_input = user_input

            if user_input.lower() in ["quit", "exit"]:
                self.print_farewell_message()
                exit(0)

            return corrected_input

    def search_index(
        self, query_list: Union[list, str], indexdir: str, max_results: int = 10
    ) -> list:
        try:
            ix = open_dir(indexdir)
        except Exception as e:
            logging.error(f"Error occurred while opening index directory: {e}")
            return []

        results_dict = {}
        searched_items = set()

        if isinstance(query_list, str):
            query_list = [{"services": [query_list]}]

        with ix.searcher() as searcher:
            # Multifield Search
            query_parser = MultifieldParser(
                ["content"], schema=ix.schema, group=OrGroup
            )

            for query in query_list:
                cves = query.get("cves", [])
                service_names = query.get("services", [])

                # Search for CVEs first if they exist
                for cve in cves:
                    if cve.lower() in searched_items:
                        continue

                    searched_items.add(cve.lower())
                    parsed_query = query_parser.parse(cve.lower())
                    cve_results = []

                    try:
                        results = searcher.search(parsed_query, limit=max_results)
                        for res in results:
                            content = res["content"]
                            lines = content.splitlines()
                            for line in lines:
                                if ":" in line:
                                    # Get the part before the colon
                                    before_colon = line.split(":")[0]
                                    if cve.lower() in before_colon.lower():
                                        cve_results.append(line.strip())
                                        if len(cve_results) >= max_results:
                                            break

                        if cve_results:
                            results_dict[cve] = cve_results

                    except Exception as e:
                        logging.error(
                            f"Error occurred while searching for CVE {cve}: {e}"
                        )

                # If services are specified, search for them
                for s in service_names:
                    if s.lower() in searched_items:
                        continue

                    searched_items.add(s.lower())
                    parsed_query = query_parser.parse(s.lower())
                    service_results = []

                    try:
                        results = searcher.search(parsed_query, limit=max_results)
                        for res in results:
                            content = res["content"]
                            lines = content.splitlines()
                            for line in lines:
                                if ":" in line:
                                    # Get the part before the colon
                                    before_colon = line.split(":")[0]
                                    if s.lower() in before_colon.lower():
                                        service_results.append(line.strip())
                                        if len(service_results) >= max_results:
                                            break

                        if service_results:
                            results_dict[f"Service {s}"] = service_results

                    except Exception as e:
                        logging.error(
                            f"Error occurred while searching for service {s}: {e}"
                        )

        formatted_results = []
        for key, values in results_dict.items():
            formatted_results.append(key + ":")
            formatted_results.extend(values)

        return formatted_results

    def _parse_nmap_xml(self, xml_file):
        if os.path.isfile(xml_file):
            tree = ET.parse(xml_file)
        else:
            tree = ET.ElementTree(ET.fromstring(xml_file))
        root = tree.getroot()

        parsed_results = []
        cve_matches = set()  # Use a set to avoid duplicate CVEs

        # Extract CVEs from the entire XML once
        for elem in root.iter():  # Search within the entire XML tree
            # Check element tag for CVE
            tag_cves = self.CVE_PATTERN.findall(elem.tag)
            cve_matches.update(tag_cves)

            # Check element text for CVE
            if elem.text:
                text_cves = self.CVE_PATTERN.findall(elem.text)
                cve_matches.update(text_cves)

            # Check all attributes for CVEs
            for attrib_name, attrib_value in elem.attrib.items():
                attrib_name_cves = self.CVE_PATTERN.findall(attrib_name)
                cve_matches.update(attrib_name_cves)

                attrib_value_cves = self.CVE_PATTERN.findall(attrib_value)
                cve_matches.update(attrib_value_cves)

        for attrib_value_cve in cve_matches:
            cprint(f"CVE(s) found: {attrib_value_cve}", "red")
        for host in root.findall("host"):
            try:
                device_name = host.find("hostnames/hostname").attrib.get(
                    "name", "Unknown"
                )
            except AttributeError:
                device_name = "Unknown"

            try:
                ip_address = host.find("address").attrib.get("addr", "Unknown")
            except AttributeError:
                ip_address = "Unknown"

            ports = []
            services = []

            for port in host.findall("ports/port"):
                try:
                    port_id = port.attrib.get("portid")
                    port_state = port.find("state").attrib.get("state")
                    service_name = port.find("service").attrib.get("name")

                    if port_state == "open" and port_id not in ports:
                        ports.append(port_id)
                        if service_name == "domain":
                            services.append("dns")
                        else:
                            services.append(service_name)
                except AttributeError:
                    continue

            parsed_results.append(
                {
                    "hostname": device_name,
                    "ip": ip_address,
                    "ports": ports,
                    "services": services,
                    "cves": list(
                        cve_matches
                    ),  # Convert the set to a list before adding
                }
            )
        timestamp = datetime.now().strftime("%I:%M:%S-%p-%Y-%m-%d").replace(" ", "-")
        cve_file_name = f"{self.args.results_dir}/CVEs-{timestamp}.txt"
        with open(cve_file_name, "w") as file:
            if isinstance(cve_matches, list):
                for match in cve_matches:
                    file.write(str(match) + "\n")
            else:
                file.write(str(cve_matches))
        return parsed_results

    def correct_cve_filename(self, filepath):
        # This regular expression is looking for the CVE pattern anywhere in the string
        match = re.search(r"(CVE)\s*(\d{4})(\d+)(\..+)", filepath, re.IGNORECASE)
        if match:
            # Reformat the CVE with the correct structure
            corrected_cve = (
                f"{match.group(1).upper()}-{match.group(2)}-{match.group(3)}"
            )
            # Replace the incorrect CVE part with the corrected one
            corrected_filepath = re.sub(
                r"CVE\s*\d{4}\d+", corrected_cve, filepath, flags=re.IGNORECASE
            )
            return corrected_filepath
        else:
            # If the filepath does not contain a CVE pattern, return it as is
            return filepath

    def colored_output(self, text):
        """Color everything before the first ':' in red and the rest in yellow."""

        # Split the text at the first ':'
        parts = re.split("(:)", text, maxsplit=1)

        # Color everything before the first ':' in red
        if len(parts) > 1:
            colored_text = (
                f"\033[91m{parts[0]}\033[0m\033[93m{parts[1]}{parts[2]}\033[0m"
            )
        else:
            colored_text = f"\033[93m{text}\033[0m"

        return colored_text

    def get_input_with_validation(
        self, prompt_text: str, valid_fn: Optional[callable] = None
    ) -> str:
        style = Style.from_dict({"prompt": "cyan", "error": "red", "message": "green"})

        validator = None
        if valid_fn:
            validator = FunctionValidator(valid_fn)

        while True:
            try:
                user_input = prompt(
                    prompt_text,
                    style=style,
                    validator=validator,
                    validate_while_typing=False,
                )
                return user_input.lower().strip()
            except ValidationError:
                continue

    def is_valid_command_choice(self, choice: str, max_choice: int) -> bool:
        if choice in ["back", "b"]:
            return True
        if choice.isdigit() and 0 < int(choice) <= max_choice:
            return True
        return False

    def is_valid_results_choice(self, choice: str) -> bool:
        return choice.isdigit() and int(choice) > 0

    def get_input_with_default(self, message, default_text=None):
        style = Style.from_dict({"prompt": "cyan", "info": "cyan"})
        history = InMemoryHistory()
        if default_text:
            user_input = prompt(
                message, default=default_text, history=history, style=style
            )
        else:
            user_input = prompt(message, history=history, style=style)
        return user_input

    def _process_previous_nmap_results(self, file_extension=None) -> bool:
        """Process the results of a previously executed nmap command."""

        # Display the command list based on the optional file extension filter
        self.display_command_list(file_extension=file_extension)

        while True:
            cmd = self.get_input_with_default(
                "Enter the number of the command you'd like to process results for (or type 'back' or 'b' to return): "
            )

            if cmd in ["back", "b"]:
                return True

            # Here, you may want to validate that `cmd` is an expected number or value.
            # If `cmd` is valid, break out of the loop.
            if self._display_and_select_results(cmd, file_extension):
                return True

            print("Invalid input. Please try again.")

    def _display_and_select_results(self, cmd, file_extension=None) -> bool:
        """Display the nmap results and prompt user for a result selection."""

        cmd_num = int(cmd)

        # Fetching filenames from the results directory based on the optional file extension filter
        all_files = os.listdir(self.args.results_dir)

        if file_extension:
            filenames = sorted(
                [file for file in all_files if file.endswith(file_extension)]
            )
        else:
            filenames = sorted(all_files)

        # Ensure the selected cmd_num is within range
        if 1 <= cmd_num <= len(filenames):
            selected_filename = filenames[cmd_num - 1]
            file_path = os.path.join(self.args.results_dir, selected_filename)
        else:
            cprint(
                f"Invalid command number: {cmd_num}. Please choose a valid command.",
                "red",
            )
            return True

        services = self._parse_nmap_xml(file_path)
        if not services:
            cprint(
                "Nothing to do here, the file you have selected does not contain valid data",
                "red",
            )
            return False
        while True:
            cmd = self.get_input_with_default(
                "Would you like to process the results? You can choose between Parsing (type 'p'), the experimental AI method (type 'ai'), or you can go back (type 'b'): "
            )

            if cmd == "p":
                number_of_results = self.get_input_with_default(
                    "Please enter the number of processed results you would like to receive. If you wish to return to the previous menu, type 'back' or 'b'. If you would like to receive all results, type 'all' or 'a': "
                )
                if number_of_results in ["back", "b"]:
                    return True

                max_results_value = (
                    int(number_of_results)
                    if number_of_results not in ["all", "a"]
                    else MAX_RESULTS_DEFAULT
                )
                results = self.search_index(services, self.index_dir, max_results_value)
                break

            elif cmd == "ai":
                results = self.construct_query_for_models(services)
                break

            elif cmd == "b":
                return True

            else:
                print("Invalid input. Please try again.")

        if cmd == "ai":
            self._display_search_results(results, ai=True)
            self._select_and_run_command(results, ai=True)
        else:
            self._display_search_results(results, ai=False)
            self._select_and_run_command(results, ai=False)
        return True

    def _display_search_results(self, results, ai):
        """Display the search results."""

        idx = 1
        for line in results:
            if ai:
                print(colored(f"{idx}. {line}", "yellow"))
                idx += 1
                continue

            is_service = line.strip().lower().startswith("service")
            is_cve = (
                line.strip().lower().startswith("cve")
                and ":" in line
                and line.split(":", 1)[1].strip()
            )

            if line.endswith(":"):
                print(colored(line, "yellow"))
            elif is_service:
                print(colored(line, "yellow"))  # Retain the yellow color for services
            elif is_cve:
                prefix, suffix = line.split(":", 1)  # Split the CVE line at the colon
                print(
                    colored(f"{idx}. {prefix}:", "red") + colored(f" {suffix}", "blue")
                )
                idx += 1
            else:
                prefix, suffix = line.split(":", 1) if ":" in line else (line, "")
                print(
                    colored(f"{idx}. {prefix}:", "white")
                    + colored(f" {suffix}", "green")
                )
                idx += 1

    def _select_and_run_command(self, results, ai):
        """Prompt user to select a search result and then run a command based on that result."""

        selection = self.get_input_with_default(
            "\nSelect a result number to modify and run the associated command or any other key to continue without running a command: "
        )

        if ai:
            selectable_results = results
        else:
            # Filtered results for selection based on the original conditions
            selectable_results = [
                line
                for line in results
                if not (
                    line.strip().lower().startswith("service")
                    or (
                        line.strip().lower().startswith("cve")
                        and ":" in line
                        and not line.split(":", 1)[1].strip()
                    )
                )
            ]

        try:
            selection = int(selection)
            if 1 <= selection <= len(selectable_results):
                selected_result = selectable_results[selection - 1]
                if not ai and ":" in selected_result:
                    content_after_colon = self.process_string(
                        selected_result.split(":", 1)[1].strip()
                    )
                    if (
                        not content_after_colon
                    ):  # If the content after the colon is empty
                        content_after_colon = selected_result
                else:
                    content_after_colon = selected_result
                if not content_after_colon.endswith(
                    (".py", ".sh")
                ) and not content_after_colon.startswith("nmap"):
                    cprint(
                        "Exploit cannot be run directly, please copy the file path and run it manually",
                        "red",
                    )
                    cprint(f"{content_after_colon}", "green")
                    return

                modified_content = self.get_input_with_default(
                    "\nEnter the modified content (or press enter to keep it unchanged): ",
                    content_after_colon,
                )

                if modified_content:
                    content_after_colon = modified_content

                self.run_command(content_after_colon)
            else:
                cprint("Invalid selection!", "red")
        except ValueError:
            pass

    def nmap_xml_to_plain_text(self, tree):
        # Parse the XML file

        root = tree.getroot()

        # Loop through each host in the XML
        for host in root.findall("host"):
            # Get IP address
            ip_address = host.find("address").get("addr")
            print(f"Host: {ip_address}")

            # Get hostnames
            hostnames = host.find("hostnames")
            for name in hostnames.findall("hostname"):
                print(f"Hostname: {name.get('name')}")

            # Get ports information
            ports = host.find("ports")
            for port in ports.findall("port"):
                port_id = port.get("portid")
                protocol = port.get("protocol")
                state = port.find("state").get("state")
                service = port.find("service").get("name")
                product = port.find("service").get("product", "")

                cprint(
                    f"Port: {port_id}/{protocol}, State: {state}, Service: {service} {product}",
                    "cyan",
                )

        print("---------")

    def _view_previous_results(self, file_extension=None) -> bool:
        """Internal method to display previous results and loop back to the main prompt."""

        # Fetch filenames from the results directory
        all_files = os.listdir(self.args.results_dir)

        # If a file extension is specified, filter the files by that extension
        if file_extension:
            filenames = sorted(
                [file for file in all_files if file.endswith(file_extension)]
            )
        else:
            filenames = sorted(all_files)

        # Check if there are no previous results
        if not filenames:
            cprint("No previous results available.", "red")
            return True  # Return to the main loop

        style = Style.from_dict({"prompt": "cyan"})

        while True:  # Keep looping until user decides to go back
            self.display_command_list(file_extension)

            cmd = prompt(
                "Enter the number of the result you'd like to view (or type 'back' or 'b' to return): ",
                style=style,
            )

            if cmd.lower().strip() in ["back", "b"]:
                return True  # Indicate that the user wants to return to the main loop

            try:
                cmd_num = int(cmd)
                if 1 <= cmd_num <= len(filenames):
                    file_path = os.path.join(
                        self.args.results_dir, filenames[cmd_num - 1]
                    )

                    with open(file_path, "r") as f:
                        result = f.read()
                        cprint(f"\nResults for command #{cmd_num}:", "cyan")
                        cprint(result, "magenta")
                else:
                    cprint(
                        f"Invalid number. Please choose between 1 and {len(filenames)}.",
                        "red",
                    )
            except ValueError:
                cprint("Please enter a valid number or type 'back'.", "red")

    def generate_and_display_text(self, prompt: str) -> str:
        """Generate and display text based on a given prompt.

        Args:
            prompt (str): The input prompt for text generation.

        Returns:
            str: The cleaned-up generated text.
        """
        try:
            generated_text = self.generate_text(prompt)
            prompt_ip = self._extract_ip(prompt)
            urls = self.extract_urls(prompt)
            first_clean_up = self.ensure_space_between_letter_and_number(generated_text)
            second_clean_up = self.process_string(first_clean_up, prompt_ip, urls)
            if self.args.autonomous_mode is False:

                try:
                    help = self.extract_and_match_flags(second_clean_up)
                    if help:
                        cprint(
                            "Verify that the flags used in the command matches your intent:",
                            "magenta",
                        )
                        for h in help:
                            cprint("\n" + h, "red")
                except Exception as e:
                    logging.error(f"unable to extract, error: {e}")

            cprint("\nGenerated Command:", "cyan")
            cprint(second_clean_up, "magenta")
            cprint("-" * 50, "blue")

            return second_clean_up
        except Exception as e:
            logging.error(f"Error during text generation and display: {e}")
            cprint(
                "An unexpected error occurred during text generation and display. Please try again or check logs.",
                "red",
            )
            return False  # This returns the original prompt as a fallback. Adjust as needed.

    def remove_slashes(self, input_str: str) -> str:
        return input_str.replace("/", "").replace("\\", "")

    def run_command(self, text: str) -> None:
        """
        A function to run a command in the background based on the generated text.
        """

        def threaded_function():
            self.run_command_and_alert(text)
            self.command_running = (
                False  # Set the flag to False once the command is done executing.
            )

        # Before starting the thread, set the command_running flag to True.
        self.command_running = True
        time.sleep(
            0.1
        )  # Introduce a short delay to give main thread some breathing room
        thread = threading.Thread(target=threaded_function)
        thread.start()

        # Inform user that command has started
        cprint(f"\nThe operation has been initiated, running {text}", "green")

        return

    def user_search_interface(self):
        """Provide a user interface for searching."""
        cprint(
            "Please ensure that you have set the base path to exploit_db_base_location using --exploit_db_base_location",
            "yellow",
        )
        while True:
            protocol_completer = WordCompleter(self.suggestions, ignore_case=True)
            history = InMemoryHistory()

            query_str = self.get_query_input(
                completer=protocol_completer, history=history
            )
            if not query_str:
                self.display_message(
                    "Please enter a query or 'b' to return to the main menu", "red"
                )
                continue
            if query_str.lower() == "b":
                break

            num_results = self.get_num_results_input(history=history)
            results = self.get_search_results(query_str, num_results)

            if not results:
                self.display_message("No results found.", "red")
                return True

            self.display_results(results, history)
        return True

    def get_suggestions(self):

        with open(self.suggestions_file, "r") as file:
            return [line.strip() for line in file if line.strip()]

    def display_message(self, message, color):
        cprint(message, color)

    def get_query_input(self, completer, history):
        return prompt(
            ANSI(
                colored(
                    "\nEnter your query, use keywords such as protocols HTTP, SSH, SMB or port numbers 443, 80 etc or 'b' to return to the main menu: ",
                    "blue",
                )
            ),
            completer=completer,
            history=history,
        )

    def get_num_results_input(self, history):
        return prompt(
            ANSI(
                colored(
                    "\nHow many results per category should i display? (Enter a number, (a) or (all) for all results, or press enter for default:",
                    "blue",
                )
            ),
            history=history,
        )

    def get_search_results(self, query_str, num_results):
        if num_results.lower() in ["a", "all"]:
            return self.search_index(
                query_str, self.index_dir, max_results=float("inf")
            )
        if not num_results:
            return self.search_index(query_str, self.index_dir)
        try:
            num = int(num_results)
            return self.search_index(query_str, self.index_dir, max_results=num)
        except ValueError:
            self.display_message("Invalid input. Returning to previous menu.", "red")
            return []

    def display_results(self, results, history):
        service_lines = [line for line in results if line.startswith("Service ")]
        other_lines = [line for line in results if not line.startswith("Service ")]

        for line in service_lines:
            print(colored(line, "green"))

        for idx, line in enumerate(other_lines, 1):
            print(f"{idx}. {self.colored_output(line)}")

        self.handle_result_selection_and_modification(other_lines, history)

    def handle_result_selection_and_modification(self, other_lines, history):
        while True:
            try:
                selection = int(
                    prompt(
                        ANSI(
                            colored(
                                "\nSelect a result number to modify or any other key to continue: ",
                                "blue",
                            )
                        ),
                        history=history,
                    )
                )

                if 1 <= selection <= len(other_lines):

                    self.modify_and_run_command(other_lines[selection - 1])
                    break
                else:
                    self.display_message("Invalid selection!", "red")
            except ValueError:
                self.display_message(
                    "Invalid input! Returning to previous menu.", "red"
                )
                break

    def modify_and_run_command(self, selected_line):
        """Modify the command based on user input and then run it."""

        # Extract content after the colon if present, or use the entire line.
        content_after_colon = (
            selected_line.split(":", 1)[1].strip()
            if ":" in selected_line
            else selected_line.strip()
        )

        # Check if the line is not a Python or shell script and does not start with "nmap".
        if not content_after_colon.endswith(
            (".py", ".sh")
        ) and not content_after_colon.startswith("nmap"):
            cprint(
                "Exploit cannot be run directly, please copy the file path and run it manually",
                "red",
            )
            return

        # Extract python files from the content, if any.
        python_files = self._extract_python_files(
            self.process_string(content_after_colon)
        )

        # Prompt the user to modify the content or keep it unchanged.
        modified_content = prompt(
            ANSI(
                colored(
                    "\nEnter the modified content or press enter to keep it unchanged: ",
                    "blue",
                )
            ),
            default=self.process_string(
                "python3 " + content_after_colon
                if python_files
                else content_after_colon
            ),
        )

        # If the user provided modified content, use it.
        if modified_content:
            content_after_colon = modified_content

        # If there are python files, analyze and run each with potential options.
        if python_files:
            for file in python_files:
                options = self._analyze_and_modify_python_file(file)
                if options:
                    cmd = ["python3"] + [file] + options
                    self.run_command(" ".join(cmd))

                else:
                    # If no options are returned, log an appropriate message and exit.
                    cprint(
                        f"Could not determine options for the file {file}.", "yellow"
                    )
                    return
        else:
            # If no python files, run the content directly.
            self.run_command(content_after_colon)

    def format_service_name(self, line):
        """Format the service name properly."""

        # Split the line into hostname and service
        parts = line.split(":", 1)
        if len(parts) != 2:
            return line  # Return the original line if it doesn't contain a service name

        hostname, service = parts

        # Format the service name based on the desired format.
        # Example: If the service name is "domain", replace it with "dns"
        if service == "domain":
            service = "dns"

        # Combine the formatted service name with the hostname
        return f"{hostname}:{service}"

    def get_modified_command(self, text: str) -> str:
        """Prompt the user to modify and return a command."""
        history = InMemoryHistory()
        style = Style.from_dict(
            {
                "prompt": "yellow",
            }
        )

        try:
            cprint(f"\nThe current command is: {text}", "cyan")

            modified_text = prompt(
                "\nModify the command as needed and press Enter: ",
                default=text,
                history=history,
                style=style,
            )

            python_files = self._extract_python_files(modified_text)
            if python_files:
                for file in python_files:
                    options = self._analyze_and_modify_python_file(file)
                    if options:
                        modified_text += " " + " ".join(options)
                        cprint(f"{modified_text}", "red")
                    else:
                        return None
                return modified_text.strip()
            else:
                return modified_text.strip()

        except Exception as e:
            logging.error(f"Error during modified command prompt: {e}")
            cprint(
                "An unexpected error occurred while modifying the command. Please try again or check logs.",
                "red",
            )
            return text  # Return the original text as a fallback

    def handle_generated_text(self, text):
        """Handle the generated text based on user's choice or predefined actions.

        Args:
            text (str): The generated text or command.
        """
        if not text:
            cprint("No text generated", "red")
            return
        try:
            # If always_apply_action is set, simply run the command
            if self.always_apply_action:
                self.run_command(text)
                return

            # Ask the user for their action choice
            if text.strip().endswith(".txt") and not text.strip().startswith("nmap"):

                return
            else:
                action_choice = self.get_action_choice()

            # If 'always' is chosen, set the flag and run the command
            if action_choice in ["always", "a"]:
                self.always_apply_action = True
                self.run_command(text)
            # If 'yes' is chosen, let the user modify the command and then run it
            elif action_choice in ["yes", "y"]:
                modified_command = self.get_modified_command(text)
                if not modified_command:
                    return
                self.run_command(modified_command if modified_command else text)
        except Exception as e:
            logging.error(f"Error handling generated text: {e}")
            cprint(
                "An error occurred while handling the generated text. Please try again or check logs.",
                "red",
            )

    def get_action_choice(self) -> str:
        style = Style.from_dict(
            {"prompt": "yellow", "options": "green", "error": "red", "message": "blue"}
        )

        if self.current_model_name != "scribe":
            action_choice_prompt = (
                "Do you want to run a command based on the generated text?"
            )
            options = "(y/n/a) (yes/no/always):"

            while True:
                try:
                    action_choice = prompt(
                        f"{action_choice_prompt} {options}",
                        style=style,
                        validator=ActionChoiceValidator(),
                        validate_while_typing=False,
                    )
                    return action_choice.lower().strip()
                except ValidationError:
                    continue
                except Exception as e:
                    logging.error(f"Error during action choice prompt: {e}")
                    print_formatted_text(
                        "An unexpected error occurred during action choice. Please try again or check logs.",
                        style="error",
                    )
        else:
            return


def main_func():
    generator = InteractiveGenerator()
    generator.start_interactive_mode()


if __name__ == "__main__":
    main_func()
