# A Vitual Assitant for SAM Users: Based on LLM Augmentation and AI Agents. 
import os
import google.generativeai as genai
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

# Custom Tools


# Custom Utilities
from utils.general_utilities import GeneralAlerts 


#
warnings.filterwarnings("ignore", category=UserWarning, module='pydantic')

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
###################### Start of Image Analysis Function (s) #######################
###################################################################################


#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# Image Analysis Tool 
#++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# @tool
def image_analysis(query: Optional[str],
                   image_directory: str, 
                   chatbot = None,
                   alert_sink: Optional[List] = None) -> Dict[str, List[Dict[str, Any]]]:
    
    """
    Scans a directory, sends any *new* images to GPT-4o for description,
    update a Chroma vector-store + JSON log, and return the descriptions
    that match an optional query.
    """   

    # ========================== Utilities ========================
    # ************** Image description with Gemini ****************
    def describe_with_gemini(image_path: str, prompt: str = "Describe this image in detail.") -> str:
        
        # Instatiate your model
        gemini_vision_llm = chatbot.gemini_vision_llm 

        image = Image.open(image_path).convert("RGB")
        response = gemini_vision_llm.generate_content([prompt, image])

        return response.text
    
    # ************** Image description with GPT ****************
    def describe_with_openai(image_path: str) -> str:

        gpt_vision_model = chatbot.gpt_vision_llm
        gpt_vision_client = chatbot.gpt_vision_client

        def data_url(path: str) -> str:
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:{mime};base64,{b64}"

        try:
            response = gpt_vision_client.chat.completions.create(
                model=gpt_vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image in detail."},
                        {"type": "image_url",
                        "image_url": {"url": data_url(image_path), "detail": "low"}}
                    ]
                }]
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"ERROR: {e}"

# ==========================End of Utilities ========================
    
    # ─── Path: Initialize the paths  here ───────────────────────
    processed_file   = os.path.join(image_directory, "processed_images.txt")
    output_file      = os.path.join(image_directory, "image_descriptions.json")
    vector_store_dir = os.path.join(image_directory, "image_vector_store")

    # ─── Helper: build data-URL with correct MIME type ───────────────────────
    def data_url(path: str) -> str:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:{mime};base64,{b64}"

    # ─── Logging for Images Detected and Processed ───────────────────────────
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger(__name__)

    # ─── Models: Get all the Models from the Chatbot (cb) class ──────────────
    embedding_model = chatbot.embedding_model
    # print(f"\nLLM Model: {llm}\n")

    # -----------------------------------------------------------------------
    # Main Loop 
    # -----------------------------------------------------------------------
    # Find images

    img_files = [f for f in os.listdir(image_directory)
                 if f.lower().endswith(("png", "jpg", "jpeg", "gif", "bmp", "tif", "TIF"))]

    already   = set(open(processed_file).read().splitlines()) if os.path.exists(processed_file) else set()
    new_files = [f for f in img_files if f not in already]

    # Status messages (only these two)
    if new_files:
        log.info(f"Processing {len(new_files)} new image(s) …")
        log_message = f"Processing {len(new_files)} new image(s) …"
        if alert_sink is not None:           
            GeneralAlerts (alert_sink, log_message, color="success")


        # global_external_alerts.append(dbc.Alert(f"Processing {len(new_files)} new image(s) …", color="success",
        #                                  style={ "whiteSpace": "normal", "wordBreak": "break-word", 
        #                                         "overflowWrap": "break-word", "maxWidth": "200px" }))
    else:
        log.info("No new images detected. Querying vector store only...")
        log_message = f"No new images detected. Now querying the image vector store..."
        if alert_sink is not None:           
            GeneralAlerts (alert_sink, log_message, color="success")

    # Describe NEW images
    new_docs: List[Document] = []
    for fname in new_files:
        path = os.path.join(image_directory, fname)
        try:
            # NEW Multi-LLM logic
            llm_type = chatbot.llm_type

            if llm_type not in ["gpt", "gemini"]:
                error_message= "Unsupported LLM for image analysis."
                if alert_sink is not None:           
                    GeneralAlerts (alert_sink, error_message, color="danger")
                # global_external_alerts.append(dbc.Alert(error_message, color="danger",
                #                          style={ "whiteSpace": "normal", "wordBreak": "break-word", 
                #                                 "overflowWrap": "break-word", "maxWidth": "200px" }))

            if llm_type == "gemini":
                desc = describe_with_gemini(path)
            elif llm_type == "gpt":
                desc = describe_with_openai(path)
            else:
                desc = "Unsupported LLM type for image analysis."

        except Exception as e:
            desc = f"ERROR: {e}"

        new_docs.append(
            Document(page_content=desc,
                     metadata={"source": fname, "title": desc.split(".")[0][:100]})
        )

    if new_docs:
        # Update / create JSON log
        data: Dict[str, Dict[str, str]] = {}
        if os.path.exists(output_file):
            with open(output_file, "r", encoding="utf-8") as jf:
                data = json.load(jf)
        for d in new_docs:
            data[d.metadata["source"]] = {"title": d.metadata["title"],
                                          "description": d.page_content}
        with open(output_file, "w", encoding="utf-8") as jf:
            json.dump(data, jf, ensure_ascii=False, indent=2)

        # Mark processed
        with open(processed_file, "a", encoding="utf-8") as pf:
            pf.writelines(f"{f}\n" for f in new_files)

        # Add docs to vector store
        store_new = Chroma(persist_directory=vector_store_dir,
                           embedding_function=embedding_model)
        store_new.add_documents(new_docs)
        if hasattr(store_new, "persist"):   # backward compatibility
            store_new.persist()

    # Retrieve matching docs
    store = Chroma(persist_directory=vector_store_dir,
                   embedding_function=embedding_model)

    if query:
        docs = store.as_retriever().get_relevant_documents(query)
    else:
        raw = store._collection.get(include=["documents", "metadatas"])
        docs = [Document(page_content=raw["documents"][i],
                         metadata=raw["metadatas"][i] or {})
                for i in range(len(raw["documents"]))]

    return {
        "descriptions": [
            {"file":  d.metadata.get("source", "unknown"),
             "title": d.metadata.get("title", ""),
             "text":  d.page_content}
            for d in docs
        ]
    }

# ====================================== End of Tool ==========================================