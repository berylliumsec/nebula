import argparse
import html
import json
import logging
import os
import shutil
import socket
import subprocess
from importlib.metadata import version
from typing import List, Set, Tuple
from zipfile import ZipFile

import requests
import torch
import transformers
from prompt_toolkit import prompt
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from transformers import BertForTokenClassification, BertTokenizerFast

transformers.logging.set_verbosity_error()
# Configure basic logging
# This will set the log level to ERROR, meaning only error and critical messages will be logged
# You can specify a filename to write the logs to a file; otherwise, it will log to stderr
log_file_path = os.path.join(os.path.expanduser("~"), "eclipse.log")

# Configure basic logging
# Get the user's home directory from the HOME environment variable
home_directory = os.getenv("HOME")  # This returns None if 'HOME' is not set

if home_directory:
    log_file_path = os.path.join(home_directory, "eclipse.log")
else:
    # Fallback mechanism or throw an error
    log_file_path = (
        "eclipse.log"  # Default to current directory, or handle error as needed
    )

# Configure basic logging
logging.basicConfig(
    filename=log_file_path,
    level=logging.ERROR,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


# Test logging at different levels

s3_url = "https://nebula-models.s3.amazonaws.com/ner_model_bert.zip"  # Update this with your actual S3 URL

# Define the label mappings
label_to_id = {
    "O": -100,
    "NETWORK_INFORMATION": 1,
    "BENIGN": 2,
    "SECURITY_CREDENTIALS": 3,
    "PERSONAL_DATA": 4,
}
id_to_label = {id: label for label, id in label_to_id.items()}

DEFAULT_MODEL_PATH = "./ner_model_bert"


class ModelManager:
    instance = None

    class __ModelManager:
        def __init__(self, model_path, device):
            self.device = torch.device(
                "cuda" if torch.cuda.is_available() and device == "cuda" else "cpu"
            )

            self.model = BertForTokenClassification.from_pretrained(model_path)
            self.model.config.id2label = id_to_label
            self.model.config.label2id = label_to_id
            self.model.to(self.device)
            self.model.eval()
            self.tokenizer = BertTokenizerFast.from_pretrained(model_path)

    @staticmethod
    def get_instance(model_path=DEFAULT_MODEL_PATH, device="cpu"):
        if ModelManager.instance is None:
            ModelManager.instance = ModelManager.__ModelManager(model_path, device)
        return ModelManager.instance


def get_s3_file_etag(s3_url):
    if not is_internet_available():
        logging.error(
            "No internet connection available. Skipping version check.", "red"
        )
        return None
    response = requests.head(s3_url)
    return response.headers.get("ETag")


def get_local_metadata(file_name):
    try:
        with open(file_name, "r") as f:
            data = json.load(f)
            return data.get("etag")
    except FileNotFoundError:
        return None


def is_run_as_package():
    # Check if the script is within a 'site-packages' directory
    return "site-packages" in os.path.abspath(__file__)


def get_latest_pypi_version(package_name):
    try:
        response = requests.get(f"https://pypi.org/pypi/{package_name}/json")
        if response.status_code == 200:
            return response.json()["info"]["version"]
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to get latest version information: {e}", "red")
    return None


def check_new_pypi_version(package_name="eclipse-ai"):
    """Check if a newer version of the package is available on PyPI."""
    if not is_internet_available():
        logging.error(
            "No internet connection available. Skipping version check.", "red"
        )
        return

    try:
        installed_version = version(package_name)
    except Exception as e:
        logging.error(
            f"Error retrieving installed version of {package_name}: {e}", "red"
        )
        return

    logging.info(f"Installed version: {installed_version}", "green")

    try:
        latest_version = get_latest_pypi_version(package_name)
        if latest_version is None:
            logging.error(
                f"Error retrieving latest version of {package_name} from PyPI.", "red"
            )
            return

        if latest_version > installed_version:
            logging.info(
                f"A newer version ({latest_version}) of {package_name} is available on PyPI. Please consider updating to access the latest features!",
                "yellow",
            )
    except Exception as e:
        logging.error(
            f"An error occurred while checking for the latest version: {e}", "red"
        )


def get_input_with_default(message, default_text=None):
    style = Style.from_dict({"prompt": "cyan", "info": "cyan"})
    history = InMemoryHistory()
    if default_text:
        user_input = prompt(message, default=default_text, history=history, style=style)
    else:
        user_input = prompt(message, history=history, style=style)
    return user_input


def folder_exists_and_not_empty(folder_path):
    # Check if the folder exists
    if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
        return False
    # Check if the folder is empty
    if not os.listdir(folder_path):
        return False
    return True


def download_and_unzip(url, output_name):
    try:
        # Download the file from the S3 bucket using wget with progress bar
        logging.info("Downloading...")
        subprocess.run(
            ["wget", "--progress=bar:force:noscroll", url, "-O", output_name],
            check=True,
        )

        # Define the target directory based on the intended structure
        target_dir = os.path.splitext(output_name)[0]  # Removes '.zip' from output_name

        # Extract the ZIP file
        logging.info("\nUnzipping...")
        with ZipFile(output_name, "r") as zip_ref:
            # Here we will extract in a temp directory to inspect the structure
            temp_dir = "temp_extract_dir"
            zip_ref.extractall(temp_dir)

            # Check if there is an unwanted nested structure
            extracted_dirs = os.listdir(temp_dir)
            if len(extracted_dirs) == 1 and os.path.isdir(
                os.path.join(temp_dir, extracted_dirs[0])
            ):
                nested_dir = os.path.join(temp_dir, extracted_dirs[0])
                # Move content up if there is exactly one directory inside
                if os.path.basename(nested_dir) == "ner_model_bert":
                    shutil.move(nested_dir, target_dir)
                else:
                    shutil.move(nested_dir, os.path.join(target_dir, "ner_model_bert"))
            else:
                # No nested structure, so just move all to the target directory
                os.makedirs(target_dir, exist_ok=True)
                for item in extracted_dirs:
                    shutil.move(
                        os.path.join(temp_dir, item), os.path.join(target_dir, item)
                    )

            # Cleanup temp directory
            shutil.rmtree(temp_dir)

        # Remove the ZIP file to clean up
        os.remove(output_name)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error occurred during download: {e}", "red")
        logging.error(f"Error occurred during download: {e}")
    except Exception as e:
        logging.error(f"Unexpected error: {e}", "red")
        logging.error(f"Unexpected error: {e}")


def save_local_metadata(file_name, etag):
    with open(file_name, "w") as f:
        json.dump({"etag": etag}, f)


def ensure_model_folder_exists(model_directory, auto_update=True):
    metadata_file = os.path.join(model_directory, "metadata.json")
    local_etag = get_local_metadata(metadata_file)
    s3_etag = get_s3_file_etag(s3_url)
    if s3_etag is None:
        return  # Exit if there's no internet connection or other issues with S3

    # Check if the model directory exists and has the same etag (metadata)
    if folder_exists_and_not_empty(model_directory) and local_etag == s3_etag:
        logging.info(f"Model directory {model_directory} is up-to-date.", "green")
        return  # No need to update anything as local version matches S3 version

    if not auto_update:
        # If folder doesn't exist, is empty, or etag doesn't match, prompt for download.
        user_input = get_input_with_default(
            "New versions of the models are available, would you like to download them? (y/n) ",
            default_text="y",  # Automatically opt for download if not specified otherwise
        )

        if user_input.lower() != "y":
            return  # Exit if user chooses not to update
    else:
        logging.info(
            "Auto-update is enabled. Downloading new version if necessary...", "yellow"
        )

    # Proceed with the removal of the existing model directory and the download of the new version
    if os.path.exists(model_directory):
        logging.info("Removing existing model folder...", "yellow")
        shutil.rmtree(model_directory)

    logging.info(
        f"{model_directory} not found or is outdated. Downloading and unzipping...",
        "yellow",
    )
    download_and_unzip(s3_url, f"{model_directory}.zip")
    # Save new metadata
    save_local_metadata(metadata_file, s3_etag)


def is_internet_available(host="8.8.8.8", port=53, timeout=3):
    """Check if there is an internet connection."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except Exception:
        return False


def recognize_entities_bert(
    prompt_text: str,
    model: BertForTokenClassification,
    tokenizer: BertTokenizerFast,
    device: torch.device,
) -> Tuple[Set[str], List[str], List[float], float]:
    """
    Recognize entities using BERT model and return unique labels detected along with all labels, their confidence scores, and average confidence score.
    """
    try:
        tokenized_inputs = tokenizer(
            prompt_text,
            truncation=True,
            padding=True,
            max_length=512,
            return_tensors="pt",
        )
        tokenized_inputs = tokenized_inputs.to(device)

        with torch.no_grad():
            outputs = model(**tokenized_inputs)

        logits = outputs.logits
        softmax = torch.nn.functional.softmax(logits, dim=-1)
        confidence_scores, predictions = torch.max(softmax, dim=2)
        average_confidence = (
            confidence_scores.mean().item()
        )  # Calculate average confidence

        predictions_labels = [
            id_to_label.get(pred.item(), "O") for pred in predictions[0]
        ]
        confidence_list = (
            confidence_scores.squeeze().tolist()
        )  # Convert to list for easier processing

        detected_labels = {label for label in predictions_labels if label != "O"}
        return detected_labels, predictions_labels, confidence_list, average_confidence
    except Exception as e:
        logging.error(f"An error occurred in recognize_entities_bert: {e}")
        # Return empty sets and lists if an error occurs
        return set(), [], [], 0.0


def process_text(
    input_text: str,
    model_path: str,
    device: str,
    line_by_line: bool = False,
    confidence_threshold: float = 0.80,
):
    # Ensure the model folder exists and is up to date
    ensure_model_folder_exists(model_path)
    # Get the singleton instance of the model manager for the specified model path and device
    model_manager = ModelManager.get_instance(model_path, device)

    # Define an inner function for processing a single line
    def process_single(input_line):
        try:
            # Process the single line using the shared model and tokenizer
            return process_single_line(input_line, model_manager, device)
        except Exception as e:
            logging.error(f"An error occurred while processing single line: {e}")
            return input_line, "Error", [0], False

    # Define an inner generator function for processing line by line
    def process_multiple(input_lines):
        for line in input_lines.split("\n"):
            try:
                # Yield results from processing each line individually
                yield process_single_line(line, model_manager, device)
            except Exception as e:
                logging.error(
                    f"An error occurred while processing line: {line}, Error: {e}"
                )
                yield line, "Error", [0], False

    # Process the input text based on the line_by_line flag
    if line_by_line:
        return process_multiple(input_text)  # This returns a generator
    else:
        return process_single(input_text)  # This returns a single tuple


def process_single_line(
    line: str, model_manager, device, confidence_threshold: float = 0.80
):
    # Rest of the function remains unchanged

    # Access the tokenizer and model from the model manager
    tokenizer = model_manager.tokenizer
    model = model_manager.model
    device = model_manager.device  # Use device from the model manager

    # Tokenize the input line
    tokenized_inputs = tokenizer(
        line, truncation=True, padding=True, max_length=512, return_tensors="pt"
    )
    tokenized_inputs = tokenized_inputs.to(device)

    # Perform model inference
    with torch.no_grad():
        outputs = model(**tokenized_inputs)

    # Process the model outputs
    logits = outputs.logits
    softmax = torch.nn.functional.softmax(logits, dim=-1)
    confidence_scores, predictions = torch.max(softmax, dim=2)
    average_confidence = confidence_scores.mean().item()

    # Convert model predictions to labels
    predictions_labels = [
        model_manager.model.config.id2label.get(pred.item(), "O")
        for pred in predictions[0]
    ]
    detected_labels = [label for label in predictions_labels if label != "O"]

    # Find the most frequent label among detected labels, if any
    if detected_labels:
        highest_avg_label = max(set(detected_labels), key=detected_labels.count)
        highest_avg_conf = (
            confidence_scores[0][
                predictions[0] == model_manager.model.config.label2id[highest_avg_label]
            ]
            .mean()
            .item()
        )
    else:
        highest_avg_label = "None"  # Use 'None' if no entity detected
        highest_avg_conf = 0.0

    # Return the processed line information
    return (
        line,
        highest_avg_label,
        highest_avg_conf,
        average_confidence > confidence_threshold,
    )


def main():
    parser = argparse.ArgumentParser(description="Entity recognition using BERT.")
    parser.add_argument(
        "-p", "--prompt", type=str, help="Direct text prompt for recognizing entities."
    )
    parser.add_argument(
        "-f", "--file", type=str, help="Path to a text file to read prompts from."
    )
    parser.add_argument(
        "-m",
        "--model_path",
        type=str,
        default=DEFAULT_MODEL_PATH,
        help="Path to the pretrained BERT model.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="output.html",
        help="Path to the output HTML file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode to display label and confidence for every line.",
    )
    parser.add_argument(
        "-d",
        "--delimiter",
        type=str,
        default="\n",
        help="Delimiter to separate text inputs, defaults to newline.",
    )
    parser.add_argument(
        "-g",
        "--use_gpu",
        action="store_true",
        help="Enable GPU usage for model inference.",
    )

    parser.add_argument(
        "--line_by_line",
        action="store_true",
        help="Process text line by line and yield results incrementally.",
    )
    parser.add_argument(
        "-c",
        "--confidence_threshold",
        type=float,
        default=0.90,
        help="Confidence threshold for considering predictions as high confidence.",
    )

    args = parser.parse_args()

    # Early exit if only displaying help
    if not any([args.prompt, args.file]):
        parser.print_help()
        return

    # Now we ensure the model folder exists if needed
    if args.prompt or args.file:
        ensure_model_folder_exists(args.model_path, auto_update=True)

    # Determine whether to use the GPU or not based on the user's command line input
    device = "cuda" if args.use_gpu and torch.cuda.is_available() else "cpu"
    if args.prompt:
        if args.line_by_line:
            # If line-by-line mode is enabled, iterate over generator
            for (
                processed_text,
                highest_avg_label,
                highest_avg_confidence,
                is_high_confidence,
            ) in process_text(
                args.prompt, args.model_path, device, True, args.confidence_threshold
            ):
                print(f"Processed Text: {processed_text}")
                print(f"Highest Average Label: {highest_avg_label}")
                print(f"Highest Average Confidence: {highest_avg_confidence}")
                print(f"Is High Confidence: {is_high_confidence}")
        else:
            # Process the entire text as a single block

            (
                processed_text,
                highest_avg_label,
                highest_avg_confidence,
                is_high_confidence,
            ) = process_text(args.prompt, args.model_path, device, False)
            print(f"Processed Text: {processed_text}")
            print(f"Highest Average Label: {highest_avg_label}")
            print(f"Highest Average Confidence: {highest_avg_confidence}")
            print(f"Is High Confidence: {is_high_confidence}")
    elif args.file:
        # Adapt file processing as needed, similar to the prompt handling

        process_file(
            args.file,
            args.model_path,
            device,
            args.output,
            args.debug,
            args.delimiter,
        )


def process_file(file_path, model_path, device, output_path, debug, delimiter):
    try:
        results = []
        with open(file_path, "r") as file:
            file_content = file.read()
            lines = (
                file_content.split(delimiter)
                if delimiter != "\n"
                else file_content.splitlines()
            )
            for line in lines:
                line = line.strip()
                if line:  # Avoid processing empty lines
                    # Assume process_text is a function that processes the text and returns a tuple
                    # containing the processed line, highest average label, highest average confidence,
                    # and a boolean indicating if the confidence is high.
                    results.append(process_text(line, model_path, device))

        with open(output_path, "w") as html_file:
            html_file.write(
                "<html><head><title>Processed Output</title></head><body>\n"
            )
            for result in results:
                line, highest_avg_label, highest_avg_conf, high_conf = result
                debug_info = ""
                if debug:
                    debug_info = f" <small>(Highest Avg. Label: {highest_avg_label}, Highest Avg. Conf.: {highest_avg_conf:.2f})</small>"

                # Escape the line to convert any HTML special characters to their equivalent entities
                colored_line = html.escape(line)
                if high_conf:
                    colored_line = f"<span style='color: red;'>{colored_line}</span>"
                html_file.write(f"{colored_line}{debug_info}<br>\n")
            html_file.write("</body></html>")
        logging.info(f"Output written to {output_path}")
    except FileNotFoundError:
        logging.info(f"The file {file_path} was not found.")


if __name__ == "__main__":
    main()
