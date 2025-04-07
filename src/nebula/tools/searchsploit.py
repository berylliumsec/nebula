import subprocess

from langchain.tools import tool
from pydantic import BaseModel, Field


class SearchInput(BaseModel):
    query: str = Field(description="SearchSploit query, e.g. 'apache 2.4'")


@tool("searchsploit-tool", args_schema=SearchInput, return_direct=True)
def searchsploit(query: str) -> str:
    """
    Executes SearchSploit—a command-line tool that searches Exploit-DB's repository for known exploits,
    shellcodes, and proof-of-concept scripts for vulnerabilities. By supplying a query (for example,
    "apache 2.4"), the tool retrieves related exploitation information from the local Exploit-DB copy.

    Example:
        searchsploit "apache 2.4"
    """
    try:
        # Build the command; add additional flags as needed (e.g., '--json' for JSON output)
        cmd = ["searchsploit", query]
        # Execute the command and capture both standard output and standard error
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        return output
    except subprocess.CalledProcessError as e:
        return f"Error running searchsploit: {e.output}"
