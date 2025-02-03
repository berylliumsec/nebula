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
    If the marker is not found, returns an empty string.
    """

    def parse(self, text: str) -> str:
        logger.debug("AfterThinkOutputParser: Parsing output text.")
        marker = "</think>"
        if marker in text:
            parsed_text = text.split(marker, 1)[1].strip()
            logger.debug(
                "AfterThinkOutputParser: Marker found; parsed text successfully."
            )
            return parsed_text
        else:
            logger.warning(
                "AfterThinkOutputParser: Marker '</think>' not found in output."
            )
            # Optionally, you could return the full text or raise an error.
            return ""


class InteractiveModel:
    def __init__(
        self, cache_dir, model_name: str = "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
    ):
        logger.info("Initializing InteractiveModel.")
        # Ensure GPU is available.
        if not torch.cuda.is_available():
            logger.error("InteractiveModel: No GPUs available.")
            raise Exception("No GPUs available")

        logger.debug(
            "InteractiveModel: GPU is available. Configuring model for 8-bit quantization."
        )
        bnb_config = BitsAndBytesConfig(load_in_8bit=True)

        logger.debug(
            "InteractiveModel: Loading tokenizer from pretrained model '%s'.",
            model_name,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            model_max_length=8192,
            low_cpu_mem_usage=True,
            cache_dir=cache_dir,
        )

        logger.debug(
            "InteractiveModel: Loading model from pretrained checkpoint '%s'.",
            model_name,
        )
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
            max_new_tokens=7000,
            repetition_penalty=1.2,
            use_fast=True,
            return_full_text=False,  # Only return the new text, not the full prompt.
        )
        self.search_results = ""

        self.search = DuckDuckGoSearchRun()

        logger.debug(
            "InteractiveModel: Wrapping pipeline with HuggingFacePipeline for LangChain."
        )
        self.llm = HuggingFacePipeline(pipeline=self.pipe)
        logger.info("InteractiveModel initialized successfully.")

    def get_template_for_mode(self, mode: str) -> str:
        logger.debug("InteractiveModel: Selecting prompt template for mode '%s'.", mode)
        if "notes" in mode:
            return """
        As a penetration testing assistant, please take detailed notes based on the context of a penetration test.
        Summarize important information, key points, and observations from the context provided. Use bullet points where appropriate.

        Given contexts:
        - Context: {context}
        - Context2: {context2}
        Question: {question}

        </think>
        """
        elif "suggestion" in mode:
            return """
        As a penetration testing assistant, provide actionable next steps with actual commands based on the context of a penetration test.
        Your suggestions should include executable terminal commands enclosed in backticks and focus on immediate next steps.

        Given contexts:
        - Context: {context}
        - Context2: {context2}
        Question: {question}

        </think>
        """
        else:  # default to "general_question"
            return """
        As a penetration testing assistant, provide a response based on your knowledge and the provided contexts.
        Ensure the response is directly relevant to the inputs, focusing particularly on elements common to both contexts.
        Revise your responses to maintain a consistent context throughout and do not alter commands returned in the contexts, but you can modify surrounding statements.
        All commands should be formatted using backticks. Eliminate any sections that diverge from or do not fit within this established context.

        Given contexts:
        - Context: {context}
        - Context2: {context2}
        Question: {question}

        </think>
        """

    def invoke(self, question: str, mode: str = "general_question", use_search=False):
        """
        Invokes the chain with the provided question and mode.
        Modes:
          - "general_question": (default) Provides a general answer.
          - "notes": Instructs the model to take detailed notes.
          - "suggestion": Instructs the model to suggest actionable next steps with commands.
        """
        logger.info(
            "InteractiveModel: Invoking chain with question: '%s' (mode: %s).",
            question[:50],
            mode,
        )
        # Optionally perform a search if enabled.
        if use_search:
            logger.debug("InteractiveModel: Performing search for question.")
            self.search_results = self.search.run(question)
            logger.debug("InteractiveModel: Search results obtained.")
        else:
            logger.debug("InteractiveModel: Search disabled; skipping search step.")

        # Build the appropriate prompt template based on the mode.
        logger.debug("InteractiveModel: Building prompt template.")
        template_str = self.get_template_for_mode(mode)
        prompt_template = ChatPromptTemplate.from_template(template_str)
        logger.debug("InteractiveModel: Prompt template built successfully.")

        # Compose the chain: first providing contexts, then formatting the prompt,
        # running the LLM, and finally parsing the output using our custom parser.
        chain = (
            {
                "context": lambda _: "",  # No additional static context provided.
                "context2": lambda x: self.search_results if use_search else "",
                "question": RunnablePassthrough(),
            }
            | prompt_template
            | self.llm
            | AfterThinkOutputParser()
        )

        logger.debug("InteractiveModel: Invoking the chain with the provided question.")
        result = chain.invoke(question)
        logger.info("InteractiveModel: Chain invocation complete.")
        return result
