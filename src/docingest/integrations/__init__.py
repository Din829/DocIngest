# -*- coding: utf-8 -*-
"""
Optional downstream-framework integrations.

Each submodule adapts DocIngest output (chunks.jsonl) to one external
framework and imports that framework's dependency only when the submodule
itself is imported. The core ``docingest run`` pipeline never imports anything
here — these stay fully opt-in.

Available:
  - ``langchain`` — DocIngestLoader maps chunks.jsonl → LangChain Document,
        bridging DocIngest to any LangChain vector store / retriever
        (Azure AI Search, Bedrock Knowledge Bases, Pinecone, ...).
        Install:  pip install 'docingest[langchain]'

This package's ``__init__`` deliberately imports no submodule, so
``import docingest.integrations`` never drags in an optional dependency.
"""
