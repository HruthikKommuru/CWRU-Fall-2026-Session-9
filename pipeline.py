"""Load → Split → Store → Ask: a Gemini and Pinecone handbook lab."""

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Any

from docx import Document
from docx.table import Table
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain.embeddings import init_embeddings
from langchain_core.documents import Document as Piece
from langchain_core.embeddings import Embeddings
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnablePassthrough
from langchain_pinecone import PineconeVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter
from pinecone import Pinecone, ServerlessSpec

EMBEDDING_MODEL = "google_genai:gemini-embedding-001"
CHAT_MODEL = "google_genai:gemini-3.7-flash"
INDEX_NAME = "trailmix-handbook"
HANDBOOK_PATH = Path(__file__).parent / "data" / "support_handbook.docx"
K = 3
SYSTEM_MESSAGE = (
    "You are a TrailMix support assistant. Answer only from the handbook excerpts below. "
    "After each claim, cite the section in square brackets exactly as shown, "
    "e.g. [Policies > Returns]. If the excerpts do not answer the question, say: "
    "'The handbook doesn't cover this — route to General Support.'"
)
logger = logging.getLogger(__name__)


def docx_to_markdown(path: str | Path) -> str:
    """Convert body paragraphs and tables to markdown in reading order."""
    document = Document(path)
    blocks = []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            # Preserve each table's position, with one readable line per row.
            blocks.append("\n".join(" | ".join(c.text for c in r.cells) for r in item.rows))
        elif item.text.strip():
            # Word styles become the headings that will supply citations.
            prefix = {"Heading 1": "# ", "Heading 2": "## "}.get(item.style.name, "")
            blocks.append(prefix + item.text.strip())
    return "\n\n".join(blocks)


def split_handbook(path: str | Path) -> list[Piece]:
    """Split markdown into pieces with section and topic metadata."""
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[("#", "section"), ("##", "topic")])
    return splitter.split_text(docx_to_markdown(path))


def citation_label(piece: Piece) -> str:
    """Build a citation from heading metadata, including front matter."""
    section = piece.metadata.get("section", "Front matter")
    topic = piece.metadata.get("topic")
    return f"[{section} > {topic}]" if topic else f"[{section}]"


def format_docs(pieces: list[Piece]) -> str:
    """Prefix every retrieved passage with its exact citation label."""
    return "\n\n".join(f"{citation_label(p)}\n{p.page_content}" for p in pieces)


def get_or_create_index(pc: Pinecone, emb: Embeddings) -> Any:
    """Create a matching serverless index if missing and wait until ready."""
    dimension = len(emb.embed_query("test"))
    logger.info("Embedding dimension: %d", dimension)
    if not pc.has_index(INDEX_NAME):
        pc.create_index(name=INDEX_NAME, dimension=dimension, metric="cosine",
                        spec=ServerlessSpec(cloud="aws", region="us-east-1"))
    deadline = time.monotonic() + 120
    while not (description := pc.describe_index(INDEX_NAME)).status["ready"]:
        if time.monotonic() >= deadline:
            raise TimeoutError("Index is not ready; wait and retry.")
        time.sleep(1)
    if description.dimension != dimension or description.metric != "cosine":
        raise ValueError("Index mismatch; delete and recreate the lab index, then ingest again.")
    return pc.Index(host=description.host)


def ingest(pc: Pinecone, emb: Embeddings) -> PineconeVectorStore:
    """Load and upsert pieces with stable IDs, then allow search visibility."""
    pieces = split_handbook(HANDBOOK_PATH)
    store = PineconeVectorStore(index=get_or_create_index(pc, emb), embedding=emb)
    store.add_documents(pieces, ids=[f"piece-{i}" for i in range(len(pieces))])
    time.sleep(5)  # Pinecone writes take a moment to become searchable.
    logger.info("Stored %d handbook pieces.", len(pieces))
    return store


def build_chain(store: PineconeVectorStore) -> Runnable:
    """Connect retrieval, cited excerpts, a Gemini prompt and text output."""
    retriever = store.as_retriever(search_kwargs={"k": K})
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_MESSAGE),
        ("human", "Handbook excerpts:\n{excerpts}\n\nQuestion: {question}"),
    ])
    model = init_chat_model(CHAT_MODEL)
    return (
        {"excerpts": retriever | format_docs, "question": RunnablePassthrough()}
        | prompt | model | StrOutputParser()
    )


def main() -> int:
    """Run ingestion, a question, or both with friendly failure messages."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingest", action="store_true", help="Load and store the handbook")
    parser.add_argument("question", nargs="?", help="A question about the handbook")
    args = parser.parse_args()
    if not args.ingest and not args.question:
        parser.error("Use --ingest, a quoted question, or both.")
    load_dotenv(Path(__file__).parent / ".env")
    missing = [key for key in ("GOOGLE_API_KEY", "PINECONE_API_KEY") if not os.getenv(key)]
    if missing:
        logger.error("Missing %s. Copy .env.example to .env and add your keys.", ", ".join(missing))
        return 1
    try:
        emb = init_embeddings(EMBEDDING_MODEL)
        pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        if args.ingest:
            store = ingest(pc, emb)
        elif pc.has_index(INDEX_NAME):
            store = PineconeVectorStore(index=pc.Index(INDEX_NAME), embedding=emb)
        else:
            logger.error("Index missing. Run python pipeline.py --ingest first.")
            return 1
        if args.question:
            logger.info("%s", build_chain(store).invoke(args.question))
    except Exception as error:
        # Avoid printing exception payloads, which can contain credentials.
        logger.error("Request failed (%s). Check keys and model access (%s, %s); "
                     "for 429/rate limits, wait and retry. Check index dimensions and readiness.",
                     type(error).__name__, EMBEDDING_MODEL, CHAT_MODEL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
