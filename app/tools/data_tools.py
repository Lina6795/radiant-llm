# A Vitual Assitant for SAM Users: Based on LLM Augmentation and AI Agents. 
import os
import matplotlib.pyplot as plt
from difflib import get_close_matches
import warnings

# Additional imports for LangChain agent setup
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
from urllib.parse import urlparse, unquote
from cgi import parse_header


# Custom Tools

# Custom Utilities
from utils.general_utilities import GeneralAlerts 
from utils.general_utilities import get_osti_pdf_link

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

# Trace on LangChain
# Set environment variables
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_API_KEY"]    = langchain_key
os.environ["OPENAI_API_KEY"]       = openai_key

###################################################################################
###################### Start of Data Analysis Function (s) #######################
###################################################################################

# @tool
def csv_excel_reader(
    working_directory: str,
    file_name: str = None,
    include_all_rows: bool = False,       # ← only dump every row when explicitly requested
    alert_sink: Optional[List] = None
) -> Dict[str, Any]:
    """
    Reads a CSV or Excel file and summarizes its contents.

    Parameters:
        working_directory (str): Path to the directory containing files.
        file_name (str, optional): Name of the CSV/Excel file. If omitted, picks the first one found.
        include_all_rows (bool): If True (depending on user query), returns ALL rows under 'all_rows' 
        — this could be huge, so only do it when explicitly requested bu the user.
        alert_sink (list, optional): A place to send alerts instead of printing.

    Returns:
        dict: A summary of the file contents, possibly including every row.
    """
    try:
        # 1) locate file
        if not file_name:
            files = [f for f in os.listdir(working_directory) if f.endswith(('.csv', '.xlsx', '.xls'))]
            if not files:
                return {"error": "No CSV or Excel files found in the specified directory."}
            file_name = files[0]

        file_path = os.path.join(working_directory, file_name)

        if alert_sink is not None:
            GeneralAlerts(alert_sink, f"Reading '{file_name}'...", color="info")

        # 2) read into DataFrame
        if file_name.lower().endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_name.lower().endswith(('.xlsx', '.xls')):
            df = pd.read_excel(file_path)
        else:
            return {"error": "Unsupported file type. Only CSV and Excel files are supported."}

        # 3) capture df.info() into a string
        buf = io.StringIO()
        df.info(buf=buf)
        info_str = buf.getvalue()

        # 4) build summary
        summary: Dict[str, Any] = {
            'file_name': file_name,
            'columns': df.columns.tolist(),
            'head': df.head().to_dict(),
            'description': df.describe(include='all').to_dict(),
            'info': info_str
        }

        # 5) optionally include all rows (this could be huge!)
        if include_all_rows:
            summary['all_rows'] = df.to_dict(orient='records')

        return summary

    except FileNotFoundError:
        return {"error": f"The file '{file_name}' was not found in '{working_directory}'."}
    except pd.errors.EmptyDataError:
        return {"error": f"The file '{file_name}' is empty."}
    except pd.errors.ParserError:
        return {"error": f"There was an error parsing the file '{file_name}'."}
    except Exception as e:
        return {"error": f"An unexpected error occurred: {e}"}
    
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Unified Text File Reader Tool
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

# @tool
def text_file_reader(file_path: str, alert_sink: Optional[List] = None) -> str:
    """
    Reads and returns the contents of a file. Supports:
    - Plain text files (.txt, .py, .m, .inp, .i, .md, .toml, .cfg, .ini, .rst, etc.)
    - JSON files (.json)
    - YAML files (.yaml, .yml)

    Parameters:
    file_path (str): The full path to the file.
    alert_sink (list, optional): A place to send user-facing alerts.

    Returns:
    str: File contents or an error message.
    """
    file_name = os.path.basename(file_path)

    if not os.path.isfile(file_path):
        message = f"[Error] File not found: '{file_name}'"
        if alert_sink is not None:
            GeneralAlerts(alert_sink, message, color="warning")
        return "[Error] The specified path does not point to a valid file."

    ext = os.path.splitext(file_path.lower())[1]

    try:
        if ext == '.json':
            if alert_sink is not None:
                GeneralAlerts(alert_sink, f"Parsing JSON: '{file_name}'", color="info")
            with open(file_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
            return json.dumps(data, indent=2)

        elif ext in ('.yaml', '.yml'):
            if alert_sink is not None:
                GeneralAlerts(alert_sink, f"Parsing YAML: '{file_name}'", color="info")
            with open(file_path, 'r', encoding='utf-8') as file:
                data = yaml.safe_load(file)
            return yaml.dump(data, default_flow_style=False)

        else:
            if alert_sink is not None:
                GeneralAlerts(alert_sink, f"Reading text file: '{file_name}'", color="info")
            with open(file_path, 'r', encoding='utf-8') as file:
                return file.read()

    except FileNotFoundError:
        message = f"[Error] File not found: '{file_name}'"
        if alert_sink is not None:
            GeneralAlerts(alert_sink, message, color="warning")
        return "[Error] File not found."
    except json.JSONDecodeError as e:
        message = f"[Error] Failed to parse JSON '{file_name}': {e}"
        if alert_sink is not None:
            GeneralAlerts(alert_sink, message, color="warning")
        return f"[Error] Failed to parse JSON: {e}"
    except yaml.YAMLError as e:
        message = f"[Error] Failed to parse YAML '{file_name}': {e}"
        if alert_sink is not None:
            GeneralAlerts(alert_sink, message, color="warning")
        return f"[Error] Failed to parse YAML: {e}"
    except Exception as e:
        message = f"[Error] Unexpected error reading '{file_name}': {e}"
        if alert_sink is not None:
            GeneralAlerts(alert_sink, message, color="warning")
        return f"[Error] An unexpected error occurred: {e}"
    
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Tool to download any file from a web url 
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
def file_downloader(url_input: str | list[str],
                    working_directory: str,
                    alert_sink: Optional[List] = None) -> str:
    """
    Downloads files (images, PDFs, etc.) from the provided URLs and saves them to
    the working directory. Automatically fixes OSTI links to point at the real PDF.
    """
    from utils.general_utilities import get_osti_pdf_link
    
    Path(working_directory).mkdir(parents=True, exist_ok=True)

    # unify to a list
    urls = url_input if isinstance(url_input, list) else [u.strip() for u in url_input.split(",")]

    results = []
    for url in urls:
        original_url = url
        # 1) If it's an OSTI link, try to extract an OSTI ID
        if "osti.gov" in url:
            m = re.search(r"/(\d+)(?:$|[^0-9])", url)
            if m:
                osti_id = m.group(1)
                # 2) Ask the OSTI API for the real PDF link
                pdf_link = get_osti_pdf_link(osti_id)
                if pdf_link:
                    url = pdf_link
                    message = f"downloading from osti.gov via {url}..."
                    if alert_sink is not None:
                        GeneralAlerts (alert_sink, message, color="success")

        try:
            # 3) Fetch the URL (following redirects)
            resp = requests.get(url, stream=True, timeout=20)
            resp.raise_for_status()

            # 4) Determine filename:
            #   a) from Content-Disposition header if present
            cd = resp.headers.get("Content-Disposition")
            if cd:
                _, params = parse_header(cd)
                fname = params.get("filename")
            else:
                # b) fallback: use last path segment
                path = urlparse(url).path
                fname = os.path.basename(path)

            # 5) Ensure .pdf extension if PDF content-type
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" in content_type and not fname.lower().endswith(".pdf"):
                fname += ".pdf"

            # 6) Save
            file_path = os.path.join(working_directory, unquote(fname))
            with open(file_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)

            # results.append(f"Downloaded: {file_path}")
            results.append(("Downloaded", os.path.basename(file_path), url))

        except Exception as e:
            results.append(f"Failed to download {original_url} → {url}: {e}")

    # return "\n".join(results)
    return results[0] if results else "No valid URLs provided"

# -------------------------------------------------------------------
# Core CSVDataFinder: two small helpers + main logic
# -------------------------------------------------------------------
import os
import io
from typing import List, Optional, Any, Dict
import pandas as pd
import dash_bootstrap_components as dbc
from langchain_experimental.agents.agent_toolkits import create_pandas_dataframe_agent

def CSVDataFinder(working_directory: str,
                  file_name: Optional[str],
                  query: str,
                  cb,  # REQUIRED: must include .llm_model
                  alert_sink: Optional[List] = None
                ) -> str:
    """
    Q&A over a CSV/Excel in a directory using cb.llm_model (MANDATORY).
    
    - If file_name is None, auto-picks the only .csv/.xls/.xlsx in working_directory.
    - Else reads working_directory/file_name (you may omit the extension).
    - Returns a plain string (agent answer or error).
    """

    # -------------------------------------------------------------------
    # Helper #1: Locate & Read the file into a DataFrame
    # -------------------------------------------------------------------
    def read_df(path: str) -> pd.DataFrame:
        """
        Reads the CSV/Excel in a directory and converts to a DataFrame.
        """
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".csv":
                df = pd.read_csv(path)
            elif ext in (".xls", ".xlsx"):
                df = pd.read_excel(path)
            else:
                raise ValueError(f"Unsupported extension {ext!r}")

            # -------------------------------------------------------------------
            message = f"📚 Loaded '{os.path.basename(path)}' with {len(df)} rows"
            if alert_sink is not None:
                alert_sink.append(
                    dbc.Alert(message, color="success", dismissable=True)
                )
            # -------------------------------------------------------------------
            return df

        except Exception as e:
            # -------------------------------------------------------------------
            message = f"Error reading file '{os.path.basename(path)}': {e}"
            if alert_sink is not None:
                alert_sink.append(
                    dbc.Alert(message, color="danger", dismissable=True)
                )
            # -------------------------------------------------------------------
            raise

    # -------------------------------------------------------------------
    # Helper #2: Run the Pandas DataFrame Agent
    # -------------------------------------------------------------------
    def run_agent(df: pd.DataFrame, question: str, llm_model) -> str:
        """
        Initializes the <<create_pandas_dataframe_agent>> and submits a query.
        """
        try:
            agent = create_pandas_dataframe_agent(
                llm_model,
                df,
                verbose=True,
                allow_dangerous_code=True
            )
            result = agent.invoke(question)
            if isinstance(result, dict) and "output" in result:
                return result["output"]
            return result if isinstance(result, str) else repr(result)

        except Exception as e:
            message = f"Agent query error: {e}"
            if alert_sink is not None:
                alert_sink.append(
                    dbc.Alert(message, color="danger", dismissable=True)
                )
            return message

    # -------------------------------------------------------------------
    # Main Logic
    # -------------------------------------------------------------------
    try:
        # 0) Validate cb.llm_model
        if not hasattr(cb, "llm_model") or cb.llm_model is None:
            return "Error: cb.llm_model is missing. Please provide cb with an initialized LLM."

        # 1) Pick or auto-detect file
        files = [f for f in os.listdir(working_directory)
                 if f.lower().endswith((".csv", ".xls", ".xlsx"))]

        if not file_name:
            if not files:
                return "Error: no CSV/Excel files found."
            if len(files) > 1:
                return f"Error: multiple files found {files!r}, please specify one."
            file_name = files[0]
            # -------------------------------------------------------------------
            message = f"📚 Detected a file: {file_name}"
            if alert_sink is not None:
                alert_sink.append(
                    dbc.Alert(message, color="info", dismissable=True)
                )
            # -------------------------------------------------------------------

        base, ext = os.path.splitext(file_name)
        if not ext:
            for e in (".csv", ".xlsx", ".xls"):
                if base + e in files:
                    file_name = base + e
                    break

        path = os.path.join(working_directory, file_name)
        if not os.path.exists(path):
            return f"Error: file not found: {path!r}"

        # -------------------------------------------------------------------
        message = "🔍Now querying the database..."
        if alert_sink is not None:
            alert_sink.append(
                dbc.Alert(message, color="info", dismissable=True)
            )
        # -------------------------------------------------------------------

        # 2) Read DataFrame
        df = read_df(path)

        # 3) Query using agent
        return run_agent(df, query, cb.llm_model)

    except Exception as e:
        return f"CSVDataFinder error: {e}"
