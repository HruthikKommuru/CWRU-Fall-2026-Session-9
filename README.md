# Session 9 · Embeddings & Vector Databases

Build a support assistant that searches one Word handbook by meaning and answers with section citations: **Load → Split → Store → Ask**. For example, “Customer smells gas from their stove — what do I do?” should produce an answer such as “Tell the customer to stop using the product immediately and route the ticket to the Safety Team. [Support teams > Safety escalations]” This lab builds on Session 8's prompt templates, `|` pipes, `invoke`, and parallel runnable branches. TrailMix Outdoors is a fictional company; the handbook is teaching material.

| Job | Service / model |
| --- | --- |
| Embeddings | Gemini `gemini-embedding-001` |
| Answers | Gemini `gemini-3.7-flash` |
| Vector database | Pinecone, serverless AWS `us-east-1`, cosine similarity |

## Setup

Use **Python 3.13**; the pinned Pinecone integration requires Python below 3.14. The dependencies were installed together and verified on Python 3.13. One Google key powers both Gemini models; Pinecone needs its own key. API usage and index hosting may incur charges depending on your accounts.

```bash
git clone https://github.com/HruthikKommuru/CWRU-Fall-2026-Session-9.git
cd CWRU-Fall-2026-Session-9
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On Windows, create the environment with `py -3.13 -m venv .venv`, activate with `.venv\Scripts\Activate.ps1` in PowerShell, and use `Copy-Item .env.example .env`.

Edit `.env` with your `GOOGLE_API_KEY` from [Google AI Studio](https://aistudio.google.com/apikey) and `PINECONE_API_KEY` from the [Pinecone console](https://app.pinecone.io/). Keep `.env` private; Git ignores it. Model names are constants at the top of both implementations so your instructor can change them if access changes; neither implementation switches providers automatically.

## Two ways to run

**Learn step by step:** start Jupyter from the repository folder, open `session9_lab.ipynb`, select your environment's Python kernel, and run from the top. The notebook is deliberately saved with no outputs; its embedding, storage, search, evaluation, and answer cells make live API calls when you run them.

```bash
jupyter notebook session9_lab.ipynb
```

**Run the whole pipeline:** ingest once, then ask questions. You can also ingest and ask in one command.

```bash
python pipeline.py --ingest
python pipeline.py "Customer smells gas from their stove — what do I do?"
python pipeline.py --ingest "Can someone exchange boots worn on a hike?"
```

The loader preserves paragraphs and tables in reading order. The splitter uses Word's section and topic headings, giving 59 pieces from 7 sections and 58 topics, including one front-matter piece. Search returns the top three pieces; the chain prefixes each excerpt with its exact `[section > topic]`, `[section]`, or `[Front matter]` label.

The shared lab index is `trailmix-handbook`. Stable IDs (`piece-0`, `piece-1`, …) make repeated ingestion of this unchanged handbook overwrite the same records. Keep the index dedicated to this lab; changed content or chunking can leave obsolete IDs behind and requires a fresh index.

## Repository structure

```text
CWRU-Fall-2026-Session-9/
├── README.md                    # Setup, usage, and completion checks
├── requirements.txt             # Verified, pinned direct dependencies
├── .env.example                 # Empty key names to copy into .env
├── .gitignore                   # Excludes keys and local working files
├── data/
│   ├── support_handbook.docx     # Original handbook, unchanged
│   └── eval_questions.csv        # Five retrieval checks with expected headings
├── session9_lab.ipynb            # Guided Load → Split → Store → Ask lab
└── pipeline.py                  # End-to-end command-line version
```

## Done means

- Retrieval hit rate @3 is at least **4/5** in the notebook evaluation.
- Every factual claim cites a real handbook section; check its cited passage.
- The CEO hiking-trail question gets “The handbook doesn't cover this — route to General Support.”
- No API keys are committed.

Local checks verify imports, extraction, splitting, notebook structure, and missing-key behavior. The source has **173 paragraphs, 8 tables, 7 sections, and 58 topics**; extraction yields **30,605 characters and 59 pieces**. Live model access, retrieval hit rate, and generated-answer quality must be checked with your keys; the notebook has not been executed.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| Missing key | Copy `.env.example` to `.env`, fill both values, and rerun setup. |
| Index dimension mismatch | For this dedicated lab index, delete `trailmix-handbook` in the Pinecone console and rerun ingestion to recreate it using the measured embedding dimension. Deleting it removes its stored vectors. |
| 429 / rate limit | Wait and retry; check your Google API quota and billing. |
| Empty results just after upsert | Wait a few more seconds and rerun search; writes are eventually searchable. |
| `iter_inner_content` missing | Upgrade `python-docx` with `pip install --upgrade 'python-docx>=1.1'`. The pinned version is 1.2.0. |
| Model unavailable | Check model access in AI Studio and update the constants with your instructor; no provider fallback is configured. |
| Dependency install fails on Python 3.14 | Recreate the virtual environment with Python 3.13, then reinstall. |
| Index not ready after two minutes | Wait and rerun; check index status in the Pinecone console. |
