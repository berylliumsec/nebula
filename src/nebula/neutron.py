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
    A generic output parser that extracts text following a '</think>' marker.
    If the marker is not found, returns the full result.
    """

    def parse(self, text: str) -> str:
        logger.debug("AfterThinkOutputParser: Starting to parse output text.")
        logger.debug("AfterThinkOutputParser: Raw text received: %s", text)
        marker = "</think>"
        if marker in text:
            parts = text.split(marker, 1)
            parsed_text = parts[1].strip()
            logger.debug(
                "AfterThinkOutputParser: Marker found; parsed text: %s", parsed_text
            )
            return parsed_text
        else:
            logger.warning(
                "AfterThinkOutputParser: Marker '</think>' not found in output. Returning full text."
            )
            final_text = text.strip()
            logger.debug(
                "AfterThinkOutputParser: Final text after stripping: %s", final_text
            )
            return final_text


class DeepSeekOutputParser(StrOutputParser):
    """
    A DeepSeek-specific parser that returns only the text that appears after the last
    '</think>' marker in the model's output.
    """

    def parse(self, text: str) -> str:
        logger.debug(
            "DeepSeekOutputParser: Parsing output to return text after last '</think>' marker."
        )
        marker = "</think>"
        if marker in text:
            last_index = text.rfind(marker)
            cleaned = text[last_index + len(marker) :].strip()
            logger.debug(
                "DeepSeekOutputParser: Found marker; returning text after last marker: %s",
                cleaned,
            )
            return cleaned
        else:
            logger.warning(
                "DeepSeekOutputParser: Marker '</think>' not found in output. Returning full text."
            )
            final_text = text.strip()
            logger.debug(
                "DeepSeekOutputParser: Final text after stripping: %s", final_text
            )
            return final_text


class InteractiveModel:
    def __init__(
        self, cache_dir, model_name: str = "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
    ):
        self.model_name = model_name
        # For DeepSeek models we assume chain-of-thought is always output.
        self.use_think_marker = "deepseek" in model_name.lower()

        try:
            logger.info("InteractiveModel: Initializing model '%s'.", model_name)
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
                max_new_tokens=8000,
                use_fast=True,
                return_full_text=False,  # Request only new tokens (though some models ignore this)
            )
            self.search_results = ""
            self.search = DuckDuckGoSearchRun()
            logger.debug(
                "InteractiveModel: Wrapping pipeline with HuggingFacePipeline for LangChain."
            )
            self.llm = HuggingFacePipeline(pipeline=self.pipe)
            logger.info("InteractiveModel: Model initialization complete.")
        except Exception as e:
            logger.error(
                "InteractiveModel: Unable to load model '%s': %s", model_name, e
            )
            raise

    def get_template_for_mode(self, mode: str) -> str:
        # For DeepSeek models, use a simplified prompt to avoid chain-of-thought output.
        if self.use_think_marker:
            template = (
                "You are a helpful assistant. Provide only the final answer, without any internal chain-of-thought or reasoning steps.\n\n"
                "User: {question}\n\nAssistant:"
            )
        else:
            if "notes" in mode:
                template = f"""
As a penetration testing assistant, please take detailed notes based on the outputsprovided. The goal is to record pertinent information that the tester can use to formulate a final report. Your notes should include key findings, actions taken, vulnerabilities found (if any) etc. Do not record anything outside of the output of the tool provided below.

Here is the tool's output: {{question}}
"""
            elif "suggestion" in mode:
                template = f"""
As a penetration testing assistant, suggest the next steps a penetration tester should take based on the input provided. Your input will be the output of a tool. Your suggestions should include executable terminal commands enclosed in backticks that will guide the penetration tester towards discovering more vulnerabilities or exploiting vulenrabilities you may have observed.
Here is the tool's output: {{question}}
"""
            else:  # "general_question" mode: no additional messaging.
                template = f"""
As a penetration testing assistant, provide a response based on your knowledge and the provided contexts.
Ensure the response is directly relevant to the inputs, focusing on elements common to both contexts.
If a context is not available, ignore it.

Given contexts:
- Context: {{context}}
- Context2: {{context2}}
Question: {{question}}
"""
        logger.debug(
            "InteractiveModel: Built prompt template for mode '%s': %s", mode, template
        )
        return template

    def invoke(self, question: str, mode: str = "general_question", use_search=False):
        """
        Invokes the chain with the provided question and mode.
        Modes:
          - "general_question": (default) Provides a general answer.
          - "notes": Instructs the model to take detailed notes.
          - "suggestion": Instructs the model to suggest actionable next steps with commands.
        """
        logger.info(
            "InteractiveModel: Invoking chain with question (first 50 chars): '%s' (mode: %s).",
            question[:50],
            mode,
        )
        if use_search:
            logger.debug("InteractiveModel: Performing search for question.")
            self.search_results = self.search.run(question)
            logger.debug(
                "InteractiveModel: Search results obtained: %s", self.search_results
            )
        else:
            logger.debug("InteractiveModel: Search disabled; skipping search step.")

        logger.debug("InteractiveModel: Building prompt template.")
        template_str = self.get_template_for_mode(mode)
        full_prompt = template_str.format(
            context="",
            context2=(self.search_results if use_search else ""),
            question=question,
        )
        logger.debug("InteractiveModel: Full prompt constructed: %s", full_prompt)

        prompt_template = ChatPromptTemplate.from_template(template_str)
        logger.debug("InteractiveModel: Prompt template built successfully.")

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

        # For DeepSeek models, use the specialized parser to extract only the text after the last </think> marker.
        if self.use_think_marker and "deepseek" in self.model_name.lower():
            parser = DeepSeekOutputParser()
            processed_text = parser.parse(raw_output)
            logger.debug(
                "InteractiveModel: Processed text after DeepSeek parsing: %s",
                processed_text,
            )
        elif self.use_think_marker:
            parser = AfterThinkOutputParser()
            processed_text = parser.parse(raw_output)
            logger.debug(
                "InteractiveModel: Processed text after marker parsing: %s",
                processed_text,
            )
        else:
            processed_text = raw_output

        # Now ensure that only the generated text (i.e. text beyond the original prompt) is returned.
        if processed_text.startswith(full_prompt):
            generated_text = processed_text[len(full_prompt) :].strip()
            logger.debug(
                "InteractiveModel: Detected full prompt in output; sliced generated text: %s",
                generated_text,
            )
            return generated_text

        logger.info("InteractiveModel: Chain invocation complete. Returning result.")
        return processed_text
