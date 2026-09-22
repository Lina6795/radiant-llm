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

# Trace on LangChain — only enable if a key is actually present
if langchain_key:
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_API_KEY"]    = langchain_key
else:
    os.environ["LANGCHAIN_TRACING_V2"] = "false"

if openai_key:
    os.environ["OPENAI_API_KEY"] = openai_key

###################################################################################
###################### Start of Data Nougat Helper Function (s) ###################
###################################################################################
#

# ========================================================================================================
# Initialize the Nougat model and processor
# ========================================================================================================
def NougatInitializer(model_name: str = "facebook/nougat-small"):
    """
    Loads the NOUGAT processor and model onto the correct device.
    
    Returns:
        processor: AutoProcessor
        model: VisionEncoderDecoderModel (on cuda or cpu)
        device: str ('cuda' or 'cpu')
    """
    print(f"\n[NOUGAT] Loading model: {model_name} ...")
    processor = AutoProcessor.from_pretrained(model_name)
    model = VisionEncoderDecoderModel.from_pretrained(model_name)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    return processor, model, device


# ========================================================================================================
# Rasterize PDF into images (pages)
# ========================================================================================================

def RasterizePaper(
    pdf: Path,
    outpath: Optional[Path] = None,
    dpi: int = 96,
    return_pil=False,
    pages=None,
) -> Optional[List[io.BytesIO]]:
    pillow_images = []
    if outpath is None:
        return_pil = True
    try:
        if isinstance(pdf, (str, Path)):
            pdf = fitz.open(pdf)
        if pages is None:
            pages = range(len(pdf))
        for i in pages:
            page_bytes: bytes = pdf[i].get_pixmap(dpi=dpi).pil_tobytes(format="PNG")
            if return_pil:
                pillow_images.append(BytesIO(page_bytes))
            else:
                with (outpath / ("%02d.png" % (i + 1))).open("wb") as f:
                    f.write(page_bytes)
    except Exception as e:
        print(f"Error rasterizing PDF: {e}")
    if return_pil:
        return pillow_images

# ========================================================================================================
# Stopping Criteria as defined by the Nougat authors
# ========================================================================================================
class RunningVarTorch:
    def __init__(self, L=15, norm=False):
        self.values = None
        self.L = L
        self.norm = norm

    def push(self, x: torch.Tensor):
        assert x.dim() == 1
        if self.values is None:
            self.values = x[:, None]
        elif self.values.shape[1] < self.L:
            self.values = torch.cat((self.values, x[:, None]), 1)
        else:
            self.values = torch.cat((self.values[:, 1:], x[:, None]), 1)

    def variance(self):
        if self.values is None:
            return
        # Use population variance (unbiased=False) so early windows with a
        # single element do not trigger degrees-of-freedom warnings.
        if self.norm:
            return torch.var(self.values, 1, unbiased=False) / self.values.shape[1]
        else:
            return torch.var(self.values, 1, unbiased=False)
# ========================================================================================================
#  Stopping Criteria Scores
# ========================================================================================================

class StoppingCriteriaScores(StoppingCriteria):
    def __init__(self, threshold: float = 0.015, window_size: int = 200):
        super().__init__()
        self.threshold = threshold
        self.vars = RunningVarTorch(norm=True)
        self.varvars = RunningVarTorch(L=window_size)
        self.stop_inds = defaultdict(int)
        self.stopped = defaultdict(bool)
        self.size = 0
        self.window_size = window_size

    @torch.no_grad()
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor):
        last_scores = scores[-1]
        self.vars.push(last_scores.max(1)[0].float().cpu())
        self.varvars.push(self.vars.variance())
        self.size += 1
        if self.size < self.window_size:
            return False

        varvar = self.varvars.variance()
        for b in range(len(last_scores)):
            if varvar[b] < self.threshold:
                if self.stop_inds[b] > 0 and not self.stopped[b]:
                    self.stopped[b] = self.stop_inds[b] >= self.size
                else:
                    self.stop_inds[b] = int(
                        min(max(self.size, 1) * 1.15 + 150 + self.window_size, 4095)
                    )
            else:
                self.stop_inds[b] = 0
                self.stopped[b] = False
        return all(self.stopped.values()) and len(self.stopped) > 0

# ==========================================================================================================
#========================================================
# JSONL PARSER
#=======================================================
def append_to_jsonl(jsonl_file: str, new_data: List[Dict]):
    """
    Safely appends new data to a JSON Lines (.jsonl) file.
    Each dictionary is written as one JSON object per line.
    If the file or directory does not exist, they are created.
    If an error occurs during writing, it is reported without
    corrupting existing data.

    Args:
        jsonl_file (str): Path to the JSONL file.
        new_data (List[Dict]): New data to append (one dict per line).
    """
    try:
        # Ensure parent directory exists
        dir_name = os.path.dirname(jsonl_file)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        # Validate input
        if not isinstance(new_data, list):
            print(f"Warning: new_data must be a list of dictionaries. Got {type(new_data)}. Skipping append.")
            return

        with open(jsonl_file, "a", encoding="utf-8") as f:
            for row in new_data:
                if not isinstance(row, dict):
                    print(f"Warning: Skipping non-dict entry in JSONL append: {type(row)}")
                    continue
                try:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                except (TypeError, ValueError) as e:
                    print(f"Warning: Failed to serialize row to JSONL. Skipping entry. Error: {e}")

    except OSError as e:
        print(f"Error: File system error while appending to JSONL file {jsonl_file}: {e}")

    except Exception as e:
        print(f"Unexpected error while appending to JSONL file {jsonl_file}: {e}")

#===============================================================================================================
# Add stable document identifiers (minimal, safe)
#===============================================================================================================
import hashlib

def make_document_id(source: str) -> str:
    """
    Generates a stable document identifier from the PDF source name.
    """
    try:
        return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    except Exception as e:
        print(f"Error generating document_id for source '{source}': {e}")
        return source  # fallback (non-fatal)
