import argparse
import gc
import importlib.resources as resources
import logging
import os
import random
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple, Union

import psutil
import pynvml
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

trans_log.set_verbosity_error()
analyzer = StandardAnalyzer(stoplist=None)
schema = Schema(
    title=TEXT(stored=True, analyzer=analyzer),
    path=ID(stored=True),
    content=TEXT(stored=True, analyzer=analyzer),
)
logging.basicConfig(filename="command_errors.log", level=logging.ERROR)
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
    IP_PATTERN = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?\b")
    FLAG_PATTERN = re.compile(r"(-\w+|--[\w-]+)")  # Updated Regular expression
    URL_PATTERN_VALIDATION = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    CVE_PATTERN = re.compile(r'CVE-\d{4}-\d{4,7}')  # Regular expression for CVE pattern

    def __init__(self):
        self.args = self._parse_arguments()
        self.index_dir = self.return_path("indexdir")
        self.s3_url = self._determine_s3_url()
        self.command_history = self._get_command_history()
        self._ensure_model_folder_exists()
        self._validate_model_dirs()
        self.command_running = False
        self._ensure_results_directory_exists()
        self.max_truncate_length: int = 500
        self.single_model_mode = False
        self.tokenizers = {}
        self.models = {}
        self.print_star_sky()
        self.random_name = None
        self.current_model = None
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

        self.log_file_path = None
        self.always_apply_action: bool = False
        self.print_lock = threading.Lock()
        self.services = []
        self.flag_file = self.return_path(self.current_model_name + "_flags")
        self.flag_descriptions = self._load_flag_descriptions(self.flag_file)
        self.extracted_flags = []

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
        return parser.parse_args()

    def _load_flag_descriptions(self, file_path):
        """Load flag descriptions from a file and return them as a dictionary."""
        with open(file_path, "r") as f:
            lines = f.readlines()
            # Store the entire line as the value in the dictionary using flag as key
            return {
                line.split(":")[0].strip(): line.strip()
                for line in lines
                if ":" in line
            }

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

    def return_path(self, path):
        if self.is_run_as_package():
            with resources.path("nebula", path) as correct_path:
                return str(correct_path)
        return path

    @staticmethod
    def _determine_s3_url():
        return (
            "https://nebula-models.s3.amazonaws.com/unified_models_no_zap.zip"
            if os.environ.get("IN_DOCKER")
            else "https://nebula-models.s3.amazonaws.com/unified_models.zip"
        )

    def _get_command_history(self):
        if os.path.exists(self.args.results_dir):
            # Sort the files by their name to ensure a consistent order
            return sorted(
                [
                    (file, os.path.join(self.args.results_dir, file))
                    for file in os.listdir(self.args.results_dir)
                ],
                key=lambda x: x[0],
            )  # Sorting by the filename
        return []

    def _ensure_model_folder_exists(self):
        if not self.folder_exists_and_not_empty(self.args.model_dir):
            cprint(
                f"{self.args.model_dir} not found or is empty. Downloading and unzipping...",
                "yellow",
            )
            self.download_and_unzip(self.s3_url, f"{self.args.model_dir}.zip")
        else:
            cprint(
                f"found {self.args.model_dir}, to download new models remove {self.args.model_dir} or invoke nebula from a different directory",
                "green",
            )

    def _validate_model_dirs(self):
        model_dirs = [
            d
            for d in os.listdir(self.args.model_dir)
            if os.path.isdir(os.path.join(self.args.model_dir, d))
        ]
        if not model_dirs:
            raise ValueError("No model directories found in the specified directory.")

    def _ensure_results_directory_exists(self):
        if not os.path.exists(self.args.results_dir):
            os.makedirs(self.args.results_dir)

    def is_run_as_package(self):
        # Check if the script is within a 'site-packages' directory
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
            print(f"Error occurred: {e}")
        except Exception as e:
            print(f"Unexpected error: {e}")

    def run_command_and_alert(self, text: str) -> None:
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

                return process.returncode, stdout, stderr
            except Exception as e:
                logging.error(f"Error while executing command {command}: {e}")
                return -1, "", str(e)

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

        truncated_cmd = command_str[:15].replace(" ", "_") + (
            "..." if len(command_str) > 15 else ""
        )
        result_file_path = os.path.join(
            self.args.results_dir,
            f"result_{truncated_cmd}_{len(self.command_history) + 1}.txt",
        )

        with open(result_file_path, "w") as f:
            if returncode == 0:
                f.write(stdout)
            else:
                if stderr:
                    cprint("\nCommand Error Output:", "red")
                    cprint(stderr, "red")
                    f.write(stderr)
                    logging.error(
                        f"Command '{command_str}' failed with error:\n{stderr}"
                    )
                    cprint("\nhit the enter key to continue", "yellow")
        # Update command history
        self.command_history.append((text, result_file_path))

    @staticmethod
    def ensure_space_between_letter_and_number(s: str) -> str:
        try:
            if not isinstance(s, str):
                raise ValueError("The input must be a string.")

            # Regex operations
            # Match everything up to the first colon that's not immediately followed by a MAC address or an IP address
            s = re.sub(
                r"^.*?:\s(?!(?:[0-9a-fA-F]{2}:)+)(?!\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})",
                "",
                s,
            )

            s = re.sub(r"-(?=\d)", "", s)
            s = re.sub(r"(?<=[a-zA-Z])(?=\d)", " ", s)
            s = re.sub(r"(?<=\d)(?=[a-zA-Z])", " ", s)
            s = re.sub(r"\.$", "", s)

            return s.strip()

        except Exception as e:
            print(f"Error in ensure_space_between_letter_and_number: {e}")
            return s

    @staticmethod
    def _extract_ip(s: str) -> Optional[str]:
        """
        Extracts the first IP address found in the given string based on a pattern.
        If no IP address is found, it returns None.
        """
        try:
            ips = InteractiveGenerator.IP_PATTERN.findall(s)
            return ips[0] if ips else None
        except Exception as e:
            logging.error(f"Error while extracting IP from string '{s[:30]}...': {e}")
            return None

    def process_string(
        self, s: str, replacement_ips: list, replacement_urls: list
    ) -> str:
        """Replace the IP addresses and URLs in the given string with the respective replacements."""

        try:
            if not isinstance(s, str):
                raise ValueError("The input string must be a string.")
        except Exception as e:
            logging.error(f"Error in input string validation: {e}")

        try:
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

        # Validate URLs in replacement_urls list
        try:
            for url in replacement_urls:
                if not re.match(self.URL_PATTERN_VALIDATION, url):
                    raise ValueError(
                        f"One of the replacement URLs ({url}) is not valid."
                    )
        except Exception as e:
            logging.error(f"Error in URL validation: {e}")

        # Extract and replace URLs
        try:
            if replacement_urls:
                urls = self.extract_urls(s)
                for i, url in enumerate(urls):
                    if i < len(replacement_urls):
                        s = s.replace(url, replacement_urls[i])
        except Exception as e:
            logging.error(f"Error in URL processing: {e}")

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

    def generate_text(self, prompt_text: str, max_length: int = 300000) -> str:
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
                print("Prompt too long! Truncating...")
                prompt_text = prompt_text[: self.current_model.config.n_ctx]

            encoding = self.current_tokenizer.encode_plus(
                prompt_text,
                return_tensors="pt",
                add_special_tokens=True,
                max_length=max_length,
                pad_to_max_length=False,
                return_attention_mask=True,
                truncation=True,
            )

            input_ids = encoding["input_ids"].to(self.device)
            attention_mask = encoding["attention_mask"].to(self.device)

            with tqdm(total=max_length, desc="Generating text", position=0) as pbar:
                output = self.current_model.generate(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    max_length=max_length,
                    num_return_sequences=1,
                    do_sample=True,
                    top_k=40,
                    top_p=0.95,
                    temperature=0.1,
                    repetition_penalty=1.0,
                )
                pbar.update(len(output[0]))

            generated_text = self.current_tokenizer.decode(
                output[0], skip_special_tokens=True
            )

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

                if prompt.lower() == "view_results":
                    still_viewing = self._view_previous_results()
                    if still_viewing:
                        continue
                    else:
                        print("Returning to the main prompt.")
                        continue

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

    def display_command_list(self) -> None:
        """
        Display a list of previously saved result filenames truncated to self.max_truncate_length.
        """
        cprint("\nPrevious Results:", "cyan")

        # Fetching filenames from the results directory
        filenames = sorted(os.listdir(self.args.results_dir))

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
                    return self._view_previous_results()
                elif action == "pr":
                    return self._process_previous_nmap_results()
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

    def _select_model(self) -> str:
        """
        Interactively allows the user to select a model from the available models.

        Returns:
            str: The name of the selected model.
        """
        # Displaying available models
        model_names = [
            model_name
            for model_name in os.listdir(self.args.model_dir)
            if self.contains_pytorch_model(
                os.path.join(self.args.model_dir, model_name)
            )
        ]

        selected_model_name = None

        session = PromptSession()

        while True:
            print_formatted_text(
                "Available models:", style=Style.from_dict({"": "yellow"})
            )
            for idx, model_name in enumerate(model_names, 1):
                print_formatted_text(
                    f"{idx}. {model_name}", style=Style.from_dict({"": "yellow"})
                )

            choice = session.prompt(
                "Select a model by entering its number: ",
                style=Style.from_dict({"": "cyan"}),
            )

            if choice.isdigit() and 1 <= int(choice) <= len(model_names):
                selected_model_name = model_names[int(choice) - 1]
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

        self.flag_descriptions = self._load_flag_descriptions(self.flag_file)

        Style.from_dict({"message": "bg:#ff0066 #ffff00"})

        print_formatted_text(
            f"You've selected the {selected_model_name} model!",
            style=Style.from_dict({"": "yellow"}),
        )

        return selected_model_name

    def unload_model(self):

        self.current_model = None
        self.current_tokenizer = None

        # 2. Release GPU Memory
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # 3. Garbage Collection
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
                    tokenizer = GPT2Tokenizer.from_pretrained(full_path)
                    cprint(" Done!", "green")

                    # Load model
                    cprint(
                        f"Loading model for {model_folder}...",
                        "yellow",
                        end="",
                        flush=True,
                    )
                    model = GPT2LMHeadModel.from_pretrained(full_path)
                    model.eval()
                    model.to(self.device)
                    if self.single_model_mode:
                        self.current_model = model
                        self.current_tokenizer = tokenizer
                    cprint(" Done!", "green")

                    # Add the successfully loaded model and tokenizer to their respective dictionaries
                    if not self.single_model_mode:
                        self.tokenizers[model_folder] = tokenizer
                        self.models[model_folder] = model

                    # Only set the 'first_loaded' once
                    if first_loaded is None:
                        first_loaded = model_folder

                except Exception as e:
                    cprint(
                        f"Failed to load model/tokenizer from {model_folder}: {e}",
                        "red",
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

        try:
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            cprint(f"Used GPU memory: {info.used / (1024**2):.2f} MB", "green")

        except Exception:
            cprint("An error occurred while fetching GPU memory info.", "red")

        return first_loaded

    def _input_command_without_model_selection(self) -> str:
        """Internal method to get a command input from the user without model selection."""

        # Styling definitions
        style = Style.from_dict({"prompt": "cyan", "error": "red", "message": "green"})

        # Initialize the spell checker
        spell = SpellChecker()

        # Get the actual user input for the model to generate
        user_input = prompt("\nEnter a prompt: ", style=style).strip()

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
                    print(f"Error occurred: {e}")

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
        tree = ET.parse(xml_file)
        root = tree.getroot()

        parsed_results = []
        

        for host in root.findall("host"):
            try:
                device_name = host.find("hostnames/hostname").attrib.get("name", "Unknown")
            except AttributeError:
                device_name = "Unknown"

            try:
                ip_address = host.find("address").attrib.get("addr", "Unknown")
            except AttributeError:
                ip_address = "Unknown"

            ports = set()
            services = set()

            for port in host.findall("ports/port"):
                try:
                    port_id = port.attrib.get("portid")
                    port_state = port.find("state").attrib.get("state")
                    service_name = port.find("service").attrib.get("name")

                    if port_state == "open":
                        ports.add(port_id)
                        if service_name == "domain":
                            services.add("dns")
                        else:
                            services.add(service_name)
                except AttributeError:
                    continue  # If there's an error with a port, skip it and continue to the next one

            # Extract CVEs from host xml_file 
            cve_matches = []
            for elem in host.iter():  # Search within the host to associate CVE with the IP
                try:
                    if elem.text:
                        cve_found = self.CVE_PATTERN.findall(elem.text)
                        if cve_found:
                            cve_matches.extend(cve_found)
                except AttributeError:
                    continue  # If there's an error with an element, skip it and continue to the next one

            parsed_results.append(
                {
                    "hostname": device_name,
                    "ip": ip_address,
                    "ports": list(ports),
                    "services": list(services),
                    "cves": cve_matches,
                }
            )

        return parsed_results

    def parse_nmap(self, file_path):

        extension = os.path.splitext(file_path)[1]
        if extension == ".xml":
            return self._parse_nmap_xml(file_path)
        else:
            return self.parse_nmap_text(file_path)

    def parse_nmap_text(self, file_path):
        try:
            with open(file_path, "r") as f:
                data = f.read()
                if not data:  # Check if the file content is empty
                    raise ValueError("The file is empty.")
        except FileNotFoundError:
            print("Error: File not found.")
            return False
        except ValueError as e:  # Handle the exception for an empty file
            print(f"Error: {e}")
            return False
        try:
            if not data or not isinstance(data, str):
                raise ValueError("The provided data is empty or not of type string.")

            hosts = re.split(r"Nmap scan report for ", data)
            if len(hosts) <= 1:
                raise ValueError("No valid Nmap reports found in the provided data.")

            parsed_results = []

            for host in hosts[1:]:
                match = re.search(r"([a-zA-Z0-9.-]+)?\s?(\([\d\.]+\))?", host)
                if not match:
                    continue

                ports = set()

                device_name = match.group(1) or "Unknown"
                ip_address = (
                    match.group(2).replace("(", "").replace(")", "")
                    if match.group(2)
                    else "Unknown"
                )

                ports = set()
                services = set()

                # Extract port, protocol, state, and service details
                service_matches = re.findall(
                    r"(\d+)/(tcp|udp)\s+(\w+)\s+([\w-]+)", host
                )
                for match in service_matches:
                    port, _, state, service = match
                    if state == "open":
                        ports.add(port)
                        if service == "domain":  # Replace "domain" service with "dns"
                            services.add("dns")
                        else:
                            services.add(service)

                # Extract CVEs from host data
                cve_matches = re.findall(
                    r"cve-\d{4}-\d+", host, re.IGNORECASE
                )  # Updated regex for CVE
                parsed_results.append(
                    {
                        "hostname": device_name,
                        "ip": ip_address,
                        "ports": list(ports),
                        "services": list(services),
                        "cves": cve_matches,  # Add CVEs to the result
                    }
                )

            print("DONE")
            return parsed_results

        except Exception as e:
            print(f"Error occurred while parsing: {e}")
            return []

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

    def _process_previous_nmap_results(self) -> bool:
        """Process the results of a previously executed nmap command."""

        self.display_command_list()

        cmd = self.get_input_with_default(
            "Enter the number of the command you'd like to process results for (or type 'back' or 'b' to return): "
        )

        if cmd in ["back", "b"]:
            return True

        number_of_results = self.get_input_with_default(
            "Enter the number of the processed results you would like to receive (or type 'back' or 'b' to return, 'all' or 'a' for all results): "
        )

        if number_of_results in ["back", "b"]:
            return True

        return self._display_and_select_results(cmd, number_of_results)

    def _display_and_select_results(self, cmd, number_of_results) -> bool:
        """Display the nmap results and prompt user for a result selection."""

        cmd_num = int(cmd)

        _, file_path = self.command_history[cmd_num - 1]

        services = self.parse_nmap(file_path)
        max_results_value = (
            int(number_of_results)
            if number_of_results not in ["all", "a"]
            else MAX_RESULTS_DEFAULT
        )
        results = self.search_index(services, self.index_dir, max_results_value)

        if not results:
            print("No results found.")
            return True

        self._display_search_results(results)
        self._select_and_run_command(results)

        return True

    def _display_search_results(self, results):
        """Display the search results."""

        idx = 1
        for line in results:
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

    def _select_and_run_command(self, results):
        """Prompt user to select a search result and then run a command based on that result."""

        selection = self.get_input_with_default(
            "\nSelect a result number to modify and run the associated command or any other key to continue without running a command: "
        )

        # Filtered results for selection
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
                content_after_colon = (
                    selectable_results[selection - 1].split(":", 1)[1].strip()
                    if ":" in selectable_results[selection - 1]
                    else selectable_results[selection - 1]
                )

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

    def _view_previous_results(self) -> bool:
        """Internal method to display previous results and loop back to the main prompt."""

        # Fetch filenames from the results directory
        filenames = sorted(os.listdir(self.args.results_dir))

        # Check if there are no previous results
        if not filenames:
            cprint("No previous results available.", "red")
            return True  # Return to the main loop

        self.display_command_list()
        style = Style.from_dict({"prompt": "cyan"})

        cmd = prompt(
            "Enter the number of the result you'd like to view (or type 'back' or 'b' to return): ",
            style=style,
        )

        if cmd.lower().strip() in ["back", "b"]:
            return True  # Indicate that the user wants to return to the main loop

        try:
            cmd_num = int(cmd)
            if 0 < cmd_num <= len(filenames):
                file_path = os.path.join(self.args.results_dir, filenames[cmd_num - 1])
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

        return True  # Indicate the user is still in the results view mode

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
            try:
                help = self.extract_and_match_flags(second_clean_up)
                if help:
                    cprint(
                        "Verify that the flags used in the command matches your intent:",
                        "magenta",
                    )
                    for h in help:
                        cprint("\n" + h, "red")
            except:
                logging.error("unable to extract")

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
            # Depending on your application's need, you might want to return an empty string or the original prompt.
            return prompt  # This returns the original prompt as a fallback. Adjust as needed.

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
        thread = threading.Thread(target=threaded_function)
        thread.start()

        # Inform user that command has started
        print("\nThe operation has been initiated.")

        return self.get_user_prompt()

    def user_search_interface(self):
        """Provide a user interface for searching."""
        suggestions_file = self.return_path("suggestions")
        with open(suggestions_file, "r") as file:
            suggestions = [line.strip() for line in file if line.strip()]

        # Use the suggestions in the WordCompleter
        protocol_completer = WordCompleter(suggestions, ignore_case=True)
        history = InMemoryHistory()

        while True:
            query_str = prompt(
                ANSI(
                    colored(
                        "\nEnter your query, use keywords such as protocols HTTP, SSH, SMB or port numbers 443, 80 etc (or 'q' to to return to the main menu): ",
                        "blue",
                    )
                ),
                completer=protocol_completer,
                history=history,
            )

            if query_str.lower() == "q":
                break

            num_results = prompt(
                ANSI(
                    colored(
                        "\nHow many results per category should i display? (Enter a number, (a) or (all) for all results, or press enter for default): ",
                        "blue",
                    )
                ),
                history=history,
            )

            # Handle result input
            if num_results.lower() == "a" or num_results.lower() == "all":
                results = self.search_index(
                    query_str, self.index_dir, max_results=float("inf")
                )
            elif not num_results:
                results = self.search_index(query_str, self.index_dir)
            else:
                try:
                    num = int(num_results)
                    results = self.search_index(
                        query_str, self.index_dir, max_results=num
                    )
                except ValueError:
                    print_formatted_text(
                        ANSI(
                            colored("Invalid input. Returning to previous menu.", "red")
                        )
                    )
                    return

            random.shuffle(results)

            if results:
                service_lines = [
                    line for line in results if line.startswith("Service ")
                ]
                other_lines = [
                    line for line in results if not line.startswith("Service ")
                ]

                # First, display the service lines without an index
                for line in service_lines:
                    print(colored(line, "green"))

                # Next, display the other lines with an index
                idx = 1  # Use this variable to manually control the index
                for line in other_lines:
                    colored_line = self.colored_output(line)
                    print(f"{idx}. {colored_line}")
                    idx += 1

                while True:  # Loop for result selection and modification
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
                                validate_while_typing=True,
                            )
                        )
                        if 1 <= selection <= len(other_lines):
                            content_after_colon = (
                                other_lines[selection - 1].split(":", 1)[1].strip()
                                if ":" in other_lines[selection - 1]
                                else other_lines[selection - 1]
                            )
                            modified_content = prompt(
                                ANSI(
                                    colored(
                                        "\nEnter the modified content (or press enter to keep it unchanged): ",
                                        "blue",
                                    )
                                ),
                                default=content_after_colon,
                                history=history,
                            )

                            # Use the modification if it exists
                            if modified_content:
                                content_after_colon = modified_content

                            self.run_command(content_after_colon)
                            break
                        else:
                            cprint("Invalid selection!", "red")
                    except ValueError:
                        # User did not enter a valid number
                        cprint("Invalid input! Returning to previous menu.", "red")
                        break  # Break out of the loop and return to previous menu

            else:
                cprint("No results found.", "red")
                return

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

    def notify_command_status(self):
        """
        Notify user about command status.
        """
        if self.command_running:
            cprint(
                "\nThe operation has been initiated. You'll be notified once it's complete.",
                "cyan",
            )
        else:
            cprint(
                "\nCommand completed!, you can view the result using the 'view previous results' option on the main menu"
            ), "green"
            self.command_history = self._get_command_history()
            with self.print_lock:
                self.get_user_prompt()

    def get_modified_command(self, text: str) -> str:
        """Prompt the user to modify and return a command.

        Args:
            text (str): The proposed command.

        Returns:
            str: The modified command.
        """
        history = InMemoryHistory()
        style = Style.from_dict(
            {
                "prompt": "yellow",
            }
        )
        try:
            print_formatted_text(
                ANSI(colored("\nThe current command is:", "cyan")), end=""
            )
            print_formatted_text(ANSI(colored(text, "magenta")))

            # Prompt the user to modify the command with a default value
            modified_text = prompt(
                "\nModify the command as needed and press Enter: ",
                default=text,
                history=history,
                style=style,
            )

            return modified_text.strip()

        except Exception as e:
            logging.error(f"Error during modified command prompt: {e}")
            print_formatted_text(
                ANSI(
                    colored(
                        "An unexpected error occurred while modifying the command. Please try again or check logs.",
                        "red",
                    )
                )
            )
            return text  # Return the original text as a fallback

    def handle_generated_text(self, text):
        """Handle the generated text based on user's choice or predefined actions.

        Args:
            text (str): The generated text or command.
        """
        try:
            # If always_apply_action is set, simply run the command
            if self.always_apply_action:
                self.run_command(text)
                return

            # Ask the user for their action choice
            action_choice = self.get_action_choice()

            # If 'always' is chosen, set the flag and run the command
            if action_choice in ["always", "a"]:
                self.always_apply_action = True
                self.run_command(text)
            # If 'yes' is chosen, let the user modify the command and then run it
            elif action_choice in ["yes", "y"]:
                modified_command = self.get_modified_command(text)
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


def main_func():
    generator = InteractiveGenerator()
    generator.start_interactive_mode()


if __name__ == "__main__":
    main_func()
