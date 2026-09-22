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
#
warnings.filterwarnings("ignore", category=UserWarning, module='pydantic')
# -------------------------------


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
tavily_key    = os.getenv('TAVILY_API_KEY')                 # Tavily web search

# Trace on LangChain
# Set environment variables
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"]    = langchain_key
os.environ["OPENAI_API_KEY"]       = openai_key

# Global variable for external alerts
# -------------------------------
# global_external_alerts = []  # This list will be updated by core functions outside callbacks


###################################################################################
###################### Start of Web Search Functions ##############################
###################################################################################

# @tool
def web_scraper(url_input: str, 
               alert_sink:List=None) -> str:
    """
    Scrapes the provided web pages for detailed information.

    Args:
        url_input (str): A single URL or multiple URLs separated by commas.

    Returns:
        str: A summary of the scraped content or error messages for inaccessible pages.
    """
    # import requests
    # from bs4 import BeautifulSoup
    # import dash_bootstrap_components as dbc

    # Show user-facing alert
    alert_message = f"🌐 Searching the web: '{url_input}' …"
    if alert_sink is not None:           
        GeneralAlerts (alert_sink, alert_message, color="success")

    # Normalize URL input
    if isinstance(url_input, list):
        urls = url_input
    else:
        urls = [url.strip() for url in url_input.split(",") if url.strip()]

    results = []

    def extract_table_data(soup):
        try:
            tables = soup.find_all("table")
            table_texts = []

            for table in tables:
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all(["th", "td"])
                    if len(cells) >= 2:
                        key = cells[0].get_text(" ", strip=True)

                        # Instead of simple get_text, gather from nested math/rendered spans too
                        value_parts = []
                        for child in cells[1].descendants:
                            if child.name in ["span", "script", "math", "code"]:
                                value_parts.append(child.get_text(" ", strip=True))
                            elif isinstance(child, str):
                                value_parts.append(child.strip())

                        value = " ".join(filter(None, value_parts)).strip()
                        if not value:
                            value = cells[1].get_text(" ", strip=True)

                        table_texts.append(f"{key} = {value}")
            return "\n".join(table_texts)
        except Exception as e:
            return f"Table parsing error: {str(e)}"

    def scrape_url(url: str):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            paragraphs = "\n".join(p.get_text() for p in soup.find_all("p"))
            extracted_blocks = set()

            def extract_block(tag):
                for block in soup.find_all(tag):
                    if tag == "code" and block.find_parent("pre"):
                        continue
                    content = block.get_text(strip=True)
                    if content and len(content) > 10:
                        extracted_blocks.add(content)

            for tag in ["pre", "code", "div", "math", "span"]:
                extract_block(tag)

            equations = "\n\n".join(sorted(extracted_blocks))
            tables = extract_table_data(soup)

            full_content = paragraphs
            if equations:
                full_content += "\n\n--- Extracted Code/Equations ---\n\n" + equations
            if tables:
                full_content += "\n\n--- Extracted Table Data ---\n\n" + tables

            title = soup.title.string.strip() if soup.title else "No title"
            return {"url": url, "title": title, "content": full_content}

        except requests.RequestException as e:
            return {"url": url, "error": str(e)}

    for url in urls:
        result = scrape_url(url)
        results.append(result)

    output = "\n\n".join(
        f"<Document URL='{doc.get('url')}' Title='{doc.get('title', 'No title')}'>\n{doc.get('content', 'No content')}\n</Document>"
        if "error" not in doc else
        f"<Error URL='{doc['url']}' Message='{doc['error']}'>"
        for doc in results
    )

    return output
   
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Google Search Tool
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# @tool
def google_search(query: str, 
                 num_results: int = 5,
                 alert_sink:List=None) -> str:
    """Run Google search and get top results."""
    # Check if API key and search engine ID are available
    alert_message = f"🌐 Searching Google: '{query}' …"
    # print(alert_message)
    if alert_sink is not None:           
        GeneralAlerts (alert_sink, alert_message, color="success")

    if not cse_key or not cse_id:
        return "Error: Google API key or Search Engine ID is missing."

    # Define the endpoint and parameters
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        'key': cse_key,
        'cx': cse_id,
        'q': query,
    }

    try:
        # Make the request to the Google Custom Search API
        response = requests.get(url, params=params)

        # Check if the request was successful
        if response.status_code != 200:
            return f"Error: Unable to perform search. Status code: {response.status_code}"

        # Parse the JSON response
        results = response.json().get('items', [])
        if not results:
            return "No good Google Search Result was found"

        # Collect summaries of the top results
        summaries = []
        for item in results[:num_results]:  # Limit to user-defined number of results
            title = item.get('title')
            snippet = item.get('snippet')
            link = item.get('link')
            summaries.append(f"**Title**: [{title}]({link})\n**Snippet**: {snippet}\n")

        return "\n\n".join(summaries)

    except requests.exceptions.RequestException as e:
        return f"Error: An exception occurred while performing the search: {str(e)}"


#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Tavily Search Tool (primary web search backend)
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
def tavily_search(query: str,
                 num_results: int = 5,
                 alert_sink: List = None) -> str:
    """Run a Tavily web search and get top results."""
    alert_message = f"🌐 Searching the web: '{query}' …"
    if alert_sink is not None:
        GeneralAlerts(alert_sink, alert_message, color="success")

    if not tavily_key:
        return "Error: web search API key is missing."

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": tavily_key,
                "query": query,
                "max_results": num_results,
                "include_raw_content": False,
                "chunks_per_source": 1,
                "search_depth": "basic",
                "include_answer": "basic",
            },
            timeout=20,
        )

        if response.status_code != 200:
            return f"Error: Unable to perform search. Status code: {response.status_code}"

        payload = response.json()
        results = payload.get("results", [])
        if not results:
            return "No good web search result was found"

        summaries = []
        answer = payload.get("answer")
        if answer:
            summaries.append(f"**Answer**: {answer[:800]}")

        for item in results[:num_results]:
            title = item.get("title")
            snippet = (item.get("content") or "")[:800]  # defense-in-depth cap on top of API-level limits
            link = item.get("url")
            summaries.append(f"**Title**: [{title}]({link})\n**Snippet**: {snippet}\n")

        return "\n\n".join(summaries)

    except requests.exceptions.RequestException as e:
        return f"Error: An exception occurred while performing the search: {str(e)}"


#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Web Search Orchestrator — Tavily primary, silent one-shot fallback to Google Custom Search
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
def web_search(query: str,
              num_results: int = 5,
              alert_sink: List = None) -> str:
    """Resilient web search: tries Tavily first, falls back to Google Custom Search
    only on a real failure (never on a legitimate empty result)."""
    result = tavily_search(query, num_results=num_results, alert_sink=alert_sink)
    if not result.startswith("Error:"):
        return result
    return google_search(query, num_results=num_results, alert_sink=alert_sink)


#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Wikipedia Search
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# @tool
def wikipedia_search(query: str, 
                    num_results: int = 10,
                    alert_sink:List=None) -> str:
    """Run Wikipedia search and get page summaries."""

    alert_message = f"🌐 Searching Wikipedia: '{query}' …"
    # print(alert_message)
    if alert_sink is not None:           
        GeneralAlerts (alert_sink, alert_message, color="success")

    try:
        # Search for Wikipedia pages based on query
        page_titles = wikipedia.search(query)
        if not page_titles:
            return "No good Wikipedia Search Result was found"

        summaries = []
        for page_title in page_titles[:num_results]:  # Limit to user-defined number of results
            try:
                wiki_page = wikipedia.page(title=page_title, auto_suggest=False)
                summaries.append(f"**Page**: {page_title}\n**Summary**: {wiki_page.summary[:1000]}...")  # Limit summary to 500 chars
            except wikipedia.exceptions.PageError:
                summaries.append(f"PageError: Could not find the page {page_title}.")
            except wikipedia.exceptions.DisambiguationError:
                summaries.append(f"DisambiguationError: Multiple meanings found for {page_title}.")

        return "\n\n".join(summaries)

    except wikipedia.exceptions.WikipediaException as e:
        return f"Error: An exception occurred while searching Wikipedia: {str(e)}"
    except Exception as e:
        # The upstream wikipedia package can occasionally bubble up raw JSON
        # parsing/network errors; return a tool error string so the agent can recover.
        return f"Error: Unexpected failure while searching Wikipedia: {str(e)}"
    
# ====================================== End of Tool ==========================================