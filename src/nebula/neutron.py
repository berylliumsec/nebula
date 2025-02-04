import torch
import transformers
from langchain_community.llms.huggingface_pipeline import HuggingFacePipeline
from langchain_community.tools import DuckDuckGoSearchRun
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from transformers import (AutoModelForCausalLM, AutoTokenizer,
                          BitsAndBytesConfig, pipeline)

from . import constants
from .log_config import setup_logging

# Suppress verbose warnings from transformers.
transformers.logging.set_verbosity_error()

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/neutron.log")


class AfterThinkOutputParser(StrOutputParser):
    """
    A custom output parser that extracts only the text after the marker </think>.
    If the marker is not found, returns the full result.
    Logs the raw text and the parsed text.
    """

    def parse(self, text: str) -> str:
        logger.debug("AfterThinkOutputParser: Starting to parse output text.")
        logger.debug("AfterThinkOutputParser: Raw text received: %s", text)
        marker = "</think>"
        if marker in text:
            # Log the splitting operation details.
            parts = text.split(marker, 1)
            logger.debug("AfterThinkOutputParser: Split text into %d part(s).", len(parts))
            parsed_text = parts[1].strip()
            logger.debug("AfterThinkOutputParser: Marker found; parsed text: %s", parsed_text)
            return parsed_text
        else:
            logger.warning("AfterThinkOutputParser: Marker '</think>' not found in output. Returning full text.")
            final_text = text.strip()
            logger.debug("AfterThinkOutputParser: Final text after stripping: %s", final_text)
            return final_text


class InteractiveModel:
    def __init__(
        self, cache_dir, model_name: str = "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
    ):
        # Save the model name and set a flag to indicate if we should use the think marker.
        self.model_name = model_name
        self.use_think_marker = "deepseek" in model_name.lower()

        try:
            logger.info("InteractiveModel: Initializing model '%s'.", model_name)
            # Ensure GPU is available.
            if not torch.cuda.is_available():
                logger.error("InteractiveModel: No GPUs available.")
                raise Exception("No GPUs available")

            logger.debug("InteractiveModel: GPU is available. Configuring model for 8-bit quantization.")
            bnb_config = BitsAndBytesConfig(load_in_8bit=True)

            logger.debug("InteractiveModel: Loading tokenizer from pretrained model '%s'.", model_name)
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                model_max_length=8192,
                low_cpu_mem_usage=True,
                cache_dir=cache_dir,
            )

            logger.debug("InteractiveModel: Loading model from pretrained checkpoint '%s'.", model_name)
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name,
                low_cpu_mem_usage=True,
                quantization_config=bnb_config,
                device_map="auto",
                cache_dir=cache_dir,
            )

            logger.debug("InteractiveModel: Setting up text-generation pipeline.")
            self.pipe = pipeline(
                "text-generation",
                model=self.model,
                tokenizer=self.tokenizer,
                max_new_tokens=8000,
                use_fast=True,
                return_full_text=False,  # Only return the new text, not the full prompt.
            )
            self.search_results = ""

            self.search = DuckDuckGoSearchRun()

            logger.debug("InteractiveModel: Wrapping pipeline with HuggingFacePipeline for LangChain.")
            self.llm = HuggingFacePipeline(pipeline=self.pipe)
            logger.info("InteractiveModel: Model initialization complete.")
        except Exception as e:
            logger.error("InteractiveModel: Unable to load model '%s': %s", model_name, e)
            raise

    def get_template_for_mode(self, mode: str) -> str:
        # For "notes" and "suggestion", include the think marker if the model is deepseek.
        think_marker = "\n</think>" if self.use_think_marker else ""

        if "notes" in mode:
            template = f"""
    As a penetration testing assistant, please take detailed notes based on the context of a penetration test.
    Summarize important information, key points, and observations from the provided contexts.
    If a context is not available, ignore it.
    Use bullet points where appropriate.

    Given contexts:
    - Context: {{context}}
    - Context2: {{context2}}
    Question: {{question}}{think_marker}
    """
        elif "suggestion" in mode:
            template = f"""
                    As a penetration testing assistant, provide actionable next steps with actual commands based on the context of a penetration test.
                    Your suggestions should include executable terminal commands enclosed in backticks and focus on immediate next steps.
                    If a context is not available, ignore it.

                    Given contexts:
                    - Context: {{context}}
                    - Context2: {{context2}}
                    Question: {{question}}{think_marker}
                    """
        else:  # "general_question" mode: no additional messaging or think marker.
            template = f"""
                    As a penetration testing assistant, provide a response based on your knowledge and the provided contexts.
                    Ensure the response is directly relevant to the inputs, focusing on elements common to both contexts.
                    If a context is not available, ignore it.

                    Given contexts:
                    - Context: {{context}}
                    - Context2: {{context2}}
                    Question: {{question}}
                    """
        logger.debug("InteractiveModel: Built prompt template for mode '%s': %s", mode, template)
        return template

    def invoke(self, question: str, mode: str = "general_question", use_search=False):
        """
        Invokes the chain with the provided question and mode.
        Modes:
          - "general_question": (default) Provides a general answer.
          - "notes": Instructs the model to take detailed notes.
          - "suggestion": Instructs the model to suggest actionable next steps with commands.
        Logs the raw output and, if applicable, the final output after processing the think marker.
        """
        logger.info("InteractiveModel: Invoking chain with question (first 50 chars): '%s' (mode: %s).",
                    question[:50], mode)
        # Optionally perform a search if enabled.
        if use_search:
            logger.debug("InteractiveModel: Performing search for question.")
            self.search_results = self.search.run(question)
            logger.debug("InteractiveModel: Search results obtained: %s", self.search_results)
        else:
            logger.debug("InteractiveModel: Search disabled; skipping search step.")

        # Build the appropriate prompt template based on the mode.
        logger.debug("InteractiveModel: Building prompt template.")
        template_str = self.get_template_for_mode(mode)
        prompt_template = ChatPromptTemplate.from_template(template_str)
        logger.debug("InteractiveModel: Prompt template built successfully.")

        # Compose the chain up to the language model.
        chain = (
            {
                "context": lambda _: "",  # No additional static context provided.
                "context2": lambda x: self.search_results if use_search else "",
                "question": RunnablePassthrough(),
            }
            | prompt_template
            | self.llm
        )

        logger.debug("InteractiveModel: Invoking the chain to get raw output.")
        raw_output = chain.invoke(question)
        logger.info("InteractiveModel: Raw output from chain: %s", raw_output)

        if self.use_think_marker:
            logger.debug("InteractiveModel: Using think marker. Applying AfterThinkOutputParser.")
            parser = AfterThinkOutputParser()
            final_output = parser.parse(raw_output)
            logger.info("InteractiveModel: Final output after applying think marker: %s", final_output)
            result = final_output
        else:
            logger.debug("InteractiveModel: Think marker not in use. Returning raw output.")
            result = raw_output

        logger.info("InteractiveModel: Chain invocation complete. Returning result.")
        return result
