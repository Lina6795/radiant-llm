# A Vitual Assitant for SAM Users: Based on LLM Augmentation and AI Agents. 
import os
import matplotlib.pyplot as plt
from difflib import get_close_matches
import base64
import openai
import pandas as pd
import warnings
import logging
import fitz  # PyMuPDF
from langchain.schema import Document
from langchain_openai import OpenAIEmbeddings
from PIL import Image
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from langchain_chroma import Chroma
import tempfile                          # To create a temposral directory for chromadb when running as an executable
from langchain.chains import RetrievalQAWithSourcesChain

# Additional imports for LangChain agent setup
import webbrowser
import subprocess
import datetime as _dt
import shlex    # For SAM execution 
from tqdm import tqdm  # For SAM execution on cluster
from langchain_experimental.utilities import PythonREPL
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain.agents import create_tool_calling_agent, create_openai_tools_agent, AgentExecutor
from langchain_openai import ChatOpenAI

# Memory capabilities 
from langchain.prompts import MessagesPlaceholder
from langchain.memory import ConversationBufferMemory
from langchain.agents import initialize_agent
from langchain_community.agent_toolkits.load_tools import load_tools

from typing import  Annotated, Sequence
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# Additional imports for other functionalities
import requests
from bs4 import BeautifulSoup
import logging
import wikipedia
import json
import yaml
from langchain_google_genai.chat_models import ChatGoogleGenerativeAI
from langchain_google_genai.embeddings import GoogleGenerativeAIEmbeddings

# nougat pdf predictor
import io
from transformers import AutoProcessor, VisionEncoderDecoderModel
from transformers import StoppingCriteria, StoppingCriteriaList
from collections import defaultdict
import torch
from pathlib import Path
from collections import defaultdict
from typing import Optional, List, Dict, Any
from io import BytesIO
import json
import mimetypes              # For image Analysis tool
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# Traditional PDF Processor?
from langchain_community.document_loaders import PyPDFLoader
import fitz
import re
import time
import pytesseract

# Dash Components 
import dash
from dash import dcc, html, Input, Output, State
import dash_bootstrap_components as dbc

# WorkSataion Execution
import paramiko

# Custom Utilities
from utils.general_utilities import GeneralAlerts 



# Custom Utilities

#
warnings.filterwarnings("ignore", category=UserWarning, module='pydantic')
# -------------------------------
# Global variable for external alerts
# -------------------------------
global_external_alerts = []  # This list will be updated by your core functions outside callbacks
# global cb # This is the Chatbot class defined as a global variable 



# Load environment variables from the .env file
load_dotenv()
# Access API keys
# ======================================================================================
openai_key   = os.getenv('OPENAI_API_KEY')
gemini_key   = os.getenv('GEMINI_API_KEY')
openai_free  = os.getenv('OPENAI_API_FREE')
langchain_key = os.getenv('LANGCHAIN_API_KEY')
cse_key       = os.getenv('CUSTOM_SEARCH_ENGINE_API_KEY')   # Google Custom Search engine
cse_id        = os.getenv('CUSTOM_SEARCH_ENGINE_ID')        # Custom search ID

# Trace on LangChain
# Set environment variables
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"]    = langchain_key
os.environ["OPENAI_API_KEY"]       = openai_key

###################################################################################
###################### Start of general Function (s) ##############################
###################################################################################



#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Python REPL Tool
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

repl = PythonREPL()
@tool
def PythonREPLTool(
    code: Annotated[str, "The python code to execute to generate your chart."],
):
    """Use this to execute python code. If you want to see the output of a value,
    you should print it out with `print(...)`. This is visible to the user."""
    try:
        result = repl.run(code)
    except BaseException as e:
        return f"Failed to execute. Error: {repr(e)}"
    result_str = f"Successfully executed:\n```python\n{code}\n```\nStdout: {result}"
    return (
        result_str + "\n\nIf you have completed all tasks, respond with FINAL ANSWER."
    )

#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# URL Verification 
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

import requests
from requests.exceptions import RequestException

def URLValidation(url: str, timeout: int = 5) -> bool:
    """
    Checks if a URL is valid and reachable without downloading the entire content.

    This function sends a lightweight HEAD request, which only retrieves
    the response headers, making it much faster and more efficient
    than a full GET request for link verification.

    Args:
        url (str): The URL to check.
        timeout (int): The number of seconds to wait for a response.

    Returns:
        bool: True if the URL is valid and returns a 2xx status code, False otherwise.
    """
    try:
        # Use a HEAD request to only fetch headers, not the full page content.
        # This is more efficient for simply checking link validity.
        response = requests.head(url, timeout=timeout)
        
        # A status code in the 2xx range indicates success.
        # The 'allow_redirects=True' parameter can be added if you want to follow redirects.
        # response = requests.head(url, timeout=timeout, allow_redirects=True)
        
        return 200 <= response.status_code < 300
    except RequestException as e:
        # Catch any request-related exceptions (e.g., connection errors, timeouts).
        print(f"Error checking URL {url}: {e}")
        return False
    except Exception as e:
        # Catch any other unexpected errors.
        print(f"An unexpected error occurred: {e}")
        return False

"""
# --- Example Usage ---
# Use a known good URL
good_url = "https://www.google.com"
print(f"Checking {good_url}: {check_url_validity(good_url)}")

# Use a known bad URL (should return a 404 or a similar error)
bad_url = "https://www.google.com/nonexistentpage"
print(f"Checking {bad_url}: {check_url_validity(bad_url)}")

# Use an invalid URL that might cause a connection error
invalid_url = "https://invalid-domain-12345.com"
print(f"Checking {invalid_url}: {check_url_validity(invalid_url)}")

"""
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++