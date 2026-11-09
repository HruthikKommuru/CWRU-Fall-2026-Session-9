"""Load → Split → Store → Ask: a Gemini and Pinecone handbook lab."""  # Module overview, also used by CLI help.

import argparse  # Parse --ingest and the optional question argument.
import logging  # Report progress and errors without print().
import os  # Read API keys from environment variables.
import time  # Pause while Pinecone starts or exposes new records.
from pathlib import Path  # Build file paths without OS-specific separators.
from typing import Any  # Annotate the SDK index object returned by our helper.

from docx import Document  # Open a Word document with python-docx.
from docx.table import Table  # Identify table objects while walking the document.
from dotenv import load_dotenv  # Load key-value pairs from the local .env file.
from langchain.chat_models import init_chat_model  # Create a chat client from a provider:model string.
from langchain.embeddings import init_embeddings  # Create an embedding client from a provider:model string.
from langchain_core.documents import Document as Piece  # Name text-and-metadata records separately from Word documents.
from langchain_core.embeddings import Embeddings  # Describe the embedding interface in type hints.
from langchain_core.output_parsers import StrOutputParser  # Extract plain text from the chat response.
from langchain_core.prompts import ChatPromptTemplate  # Build system and human messages with placeholders.
from langchain_core.runnables import Runnable, RunnablePassthrough  # Type the chain and forward the question unchanged.
from langchain_pinecone import PineconeVectorStore  # Connect LangChain documents to a Pinecone index.
from langchain_text_splitters import MarkdownHeaderTextSplitter  # Split markdown and retain headings as metadata.
from pinecone import Pinecone, ServerlessSpec  # Use the Pinecone client and serverless index settings.

EMBEDDING_MODEL = "google_genai:gemini-embedding-001"  # Choose the Gemini embedding provider and model.
CHAT_MODEL = "google_genai:gemini-3.7-flash"  # Choose the Gemini answer provider and model.
INDEX_NAME = "trailmix-handbook"  # Name the Pinecone index used throughout the lab.
HANDBOOK_PATH = Path(__file__).parent / "data" / "support_handbook.docx"  # Resolve data beside this script, regardless of terminal folder.
K = 3  # Retrieve three passages for each question.
SYSTEM_MESSAGE = (  # Combine the following strings into one system instruction.
    "You are a TrailMix support assistant. Answer only from the handbook excerpts below. "  # Limit answers to the handbook excerpts provided below.
    "After each claim, cite the section in square brackets exactly as shown, "  # Require exact square-bracket citations after each claim.
    "e.g. [Policies > Returns]. If the excerpts do not answer the question, say: "  # Give a citation example and introduce the fallback rule.
    "'The handbook doesn't cover this — route to General Support.'"  # Specify the required reply when excerpts lack the answer.
)  # Close the preceding expression.
logger = logging.getLogger(__name__)  # Create a logger named after this module.


def docx_to_markdown(path: str | Path) -> str:  # Accept a path string or Path and return markdown text.
    """Convert body paragraphs and tables to markdown in reading order."""  # Describe the function for help() and readers.
    document = Document(path)  # Open the Word file at the supplied path.
    blocks = []  # Collect converted text blocks in reading order.
    for item in document.iter_inner_content():  # Visit body paragraphs and tables in their original order.
        if isinstance(item, Table):  # Handle a table separately from a paragraph.
            # Preserve each table's position, with one readable line per row.
            blocks.append("\n".join(" | ".join(c.text for c in r.cells) for r in item.rows))  # Join cells with pipes and rows with newlines.
        elif item.text.strip():  # Handle only paragraphs containing non-whitespace text.
            # Word styles become the headings that will supply citations.
            prefix = {"Heading 1": "# ", "Heading 2": "## "}.get(item.style.name, "")  # Map heading styles; use no prefix for other styles.
            blocks.append(prefix + item.text.strip())  # Add the heading marker and trimmed paragraph text.
    return "\n\n".join(blocks)  # Separate blocks with a blank line and return one string.


def split_handbook(path: str | Path) -> list[Piece]:  # Accept a file path; return text-and-metadata pieces.
    """Split markdown into pieces with section and topic metadata."""  # Summarize this helper for readers and help().
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[("#", "section"), ("##", "topic")])  # Map markdown heading levels to citation metadata keys.
    return splitter.split_text(docx_to_markdown(path))  # Load Word text, then split it into heading-based documents.


def citation_label(piece: Piece) -> str:  # Accept one document; return its bracketed source label.
    """Build a citation from heading metadata, including front matter."""  # Describe the helper without changing the input document.
    section = piece.metadata.get("section", "Front matter")  # Read its section, defaulting to Front matter if absent.
    topic = piece.metadata.get("topic")  # Read its topic; get() returns None when it is absent.
    return f"[{section} > {topic}]" if topic else f"[{section}]"  # Include the topic only when one exists.


def format_docs(pieces: list[Piece]) -> str:  # Accept retrieved documents and return prompt-ready text.
    """Prefix every retrieved passage with its exact citation label."""  # Document why labels accompany the passage text.
    return "\n\n".join(f"{citation_label(p)}\n{p.page_content}" for p in pieces)  # Separate labeled passages with blank lines.


def get_or_create_index(pc: Pinecone, emb: Embeddings) -> Any:  # Accept database and encoder clients; return a ready index.
    """Create a matching serverless index if missing and wait until ready."""  # Describe the creation and readiness contract.
    dimension = len(emb.embed_query("test"))  # Measure a real embedding rather than guessing its length.
    logger.info("Embedding dimension: %d", dimension)  # Insert the measured vector length into the progress message.
    if not pc.has_index(INDEX_NAME):  # Create an index only when this name is absent.
        pc.create_index(name=INDEX_NAME, dimension=dimension, metric="cosine",  # Set the index name, required vector length, and similarity metric.
                        spec=ServerlessSpec(cloud="aws", region="us-east-1"))  # Use serverless AWS hosting in the chosen region.
    deadline = time.monotonic() + 120  # Set a deadline 120 seconds from the monotonic clock.
    while not (description := pc.describe_index(INDEX_NAME)).status["ready"]:  # Fetch status and keep polling while ready is False.
        if time.monotonic() >= deadline:  # Stop polling if the two-minute deadline has passed.
            raise TimeoutError("Index is not ready; wait and retry.")  # Stop after two minutes instead of polling forever.
        time.sleep(1)  # Pause for one second between status requests.
    if description.dimension != dimension or description.metric != "cosine":  # Reject an existing index with incompatible settings.
        raise ValueError("Index mismatch; delete and recreate the lab index, then ingest again.")  # Reject incompatible vector size or metric before writing.
    return pc.Index(host=description.host)  # Return a client connected to the ready data endpoint.


def ingest(pc: Pinecone, emb: Embeddings) -> PineconeVectorStore:  # Accept service clients; return the populated store wrapper.
    """Load and upsert pieces with stable IDs, then allow search visibility."""  # Summarize ingestion and its search-visibility pause.
    pieces = split_handbook(HANDBOOK_PATH)  # Read the handbook and retain heading metadata in each piece.
    store = PineconeVectorStore(index=get_or_create_index(pc, emb), embedding=emb)  # Bind the ready index to the Gemini encoder.
    store.add_documents(pieces, ids=[f"piece-{i}" for i in range(len(pieces))])  # Embed and upsert with one repeatable ID per piece.
    time.sleep(5)  # Pinecone writes take a moment to become searchable.
    logger.info("Stored %d handbook pieces.", len(pieces))  # Log the number of pieces submitted for storage.
    return store  # Let the caller reuse this store for a question.


def build_chain(store: PineconeVectorStore) -> Runnable:  # Accept the store and return an invokable answer workflow.
    """Connect retrieval, cited excerpts, a Gemini prompt and text output."""  # Describe the chain components in execution order.
    retriever = store.as_retriever(search_kwargs={"k": K})  # Return at most K documents whenever the retriever is invoked.
    prompt = ChatPromptTemplate.from_messages([  # Build a reusable template with two message roles.
        ("system", SYSTEM_MESSAGE),  # Set answer scope, citation rules, and the fallback reply.
        ("human", "Handbook excerpts:\n{excerpts}\n\nQuestion: {question}"),  # Fill these named placeholders when the chain runs.
    ])  # Finish the prompt template.
    model = init_chat_model(CHAT_MODEL)  # Initialize the Gemini chat client for writing answers.
    return (  # Return the composed chain without invoking it yet.
        {"excerpts": retriever | format_docs, "question": RunnablePassthrough()}  # Build excerpts by retrieval, and preserve the question text.
        | prompt | model | StrOutputParser()  # Fill the prompt, call Gemini, then extract answer text.
    )  # Close the preceding expression.


def main() -> int:  # Return an exit status: zero for success, one for failure.
    """Run ingestion, a question, or both with friendly failure messages."""  # Summarize the command-line entry point.
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # Show INFO and higher messages using only their text.
    parser = argparse.ArgumentParser(description=__doc__)  # Use the module overview in --help output.
    parser.add_argument("--ingest", action="store_true", help="Load and store the handbook")  # Set ingest=True only when this flag is supplied.
    parser.add_argument("question", nargs="?", help="A question about the handbook")  # Accept zero or one positional question; quote multiword input.
    args = parser.parse_args()  # Read command-line values into args.ingest and args.question.
    if not args.ingest and not args.question:  # Reject a request that asks for neither supported action.
        parser.error("Use --ingest, a quoted question, or both.")  # Show CLI usage and exit with an argument error.
    load_dotenv(Path(__file__).parent / ".env")  # Read .env beside this script; preserve existing environment values.
    missing = [key for key in ("GOOGLE_API_KEY", "PINECONE_API_KEY") if not os.getenv(key)]  # Collect names of missing or empty keys, never their values.
    if missing:  # Run this branch if the missing-key list is not empty.
        logger.error("Missing %s. Copy .env.example to .env and add your keys.", ", ".join(missing))  # Show which configuration names must be filled in.
        return 1  # Signal failure to the terminal without a raw traceback.
    try:  # Catch initialization, model, and database request failures below.
        emb = init_embeddings(EMBEDDING_MODEL)  # Initialize the Gemini embedding client using our constant.
        pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])  # Authenticate the Pinecone client using the stored key.
        if args.ingest:  # Run ingestion when the flag is present.
            store = ingest(pc, emb)  # Load, split, upsert, and reuse the resulting vector store.
        elif pc.has_index(INDEX_NAME):  # For question-only mode, require an existing lab index.
            store = PineconeVectorStore(index=pc.Index(INDEX_NAME), embedding=emb)  # Attach to the existing index without ingesting again.
        else:  # Explain how to recover when the question-only index is absent.
            logger.error("Index missing. Run python pipeline.py --ingest first.")  # Explain how to prepare the index before asking questions.
            return 1  # Signal failure to the terminal without a raw traceback.
        if args.question:  # Generate an answer only when a question was provided.
            logger.info("%s", build_chain(store).invoke(args.question))  # Retrieve evidence, generate an answer, and log its text.
    except Exception as error:  # Convert service or setup exceptions into a friendly failure.
        # Avoid printing exception payloads, which can contain credentials.
        logger.error("Request failed (%s). Check keys and model access (%s, %s); "  # Start a helpful message without exposing exception payloads.
                     "for 429/rate limits, wait and retry. Check index dimensions and readiness.",  # Suggest likely configuration and quota fixes.
                     type(error).__name__, EMBEDDING_MODEL, CHAT_MODEL)  # Include only the exception class and configured model names.
        return 1  # Signal failure to the terminal without a raw traceback.
    return 0  # Signal that the requested work finished successfully.


if __name__ == "__main__":  # Run the CLI only for direct execution, not imports.
    raise SystemExit(main())  # Pass main()'s return code back to the terminal.
