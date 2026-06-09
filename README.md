# Patient Intake Questionnaire

A bilingual medical intake web app for collecting patient questionnaire data, medication details, medical history, investigation results, and uploaded medical files.

The app is built with Flask, SQLite, HTML, CSS, and JavaScript. It includes optional medication lookup through openFDA, optional AI image scanning through OpenAI Vision, and optional DrugBank support when a DrugBank API key is available.
It also includes a local RAG index for clinical books and guidelines using Gemini embeddings.

## Features

- Bilingual English / Arabic intake questionnaire
- Client-side validation for required form fields
- SQLite storage for submitted intake forms
- Protected submissions page for reviewing saved forms
- Drug and package photo uploads
- Investigation image/PDF uploads
- Medication text entry for current medications
- Medical history and investigation result summaries
- openFDA drug label lookup
- Optional OpenAI Vision extraction from medication images
- Optional local OCR fallback with Pillow and pytesseract
- Optional DrugBank lookup when configured
- Local PDF RAG index for clinical guideline retrieval and citations

## Project Structure

```text
.
|-- app.py
|-- clinical agent.py   # Clinical Agent orchestration for RAG and medication checks
|-- index.html
|-- script.js
|-- style.css
|-- requirements.txt
|-- README.md
|-- rag_store.py         # PDF extraction, embedding, vector storage, and retrieval
|-- .gitignore
|-- APIkey              # local secrets file, ignored by git
|-- intake.db           # local SQLite database, ignored by git
|-- rag_vectors.db      # local RAG vector database, ignored by git
|-- RAG Files/          # local clinical source PDFs, ignored by git
`-- uploads/            # uploaded files, ignored by git
```

## Requirements

- Python 3.10 or newer
- pip
- Optional: Tesseract OCR installed on the operating system for local OCR fallback

Python packages are listed in `requirements.txt`.

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the app:

```powershell
python app.py
```

Open the app:

```text
http://127.0.0.1:5000/
```

## API Keys

The app reads API keys from environment variables first. If they are not set, it reads them from a local `APIkey` file in the project folder.

Supported keys:

```text
OPENFDA_API_KEY=your_openfda_key
OPENAI_API_KEY=your_openai_key
DRUGBANK_API_KEY=your_drugbank_key
GEMINI_API_KEY=your_gemini_key
```

For openFDA only, `APIkey` may also contain just the bare key:

```text
your_openfda_key
```

`APIkey` is listed in `.gitignore` and should not be committed.

## Clinical Books RAG

Place source PDFs in:

```text
RAG Files/
```

Index the PDFs into the local SQLite vector store:

```powershell
python rag_store.py index
```

The indexer:

1. Extracts text from PDFs with `pypdf`.
2. Splits each page into overlapping chunks.
3. Creates Gemini embeddings with `gemini-embedding-001`.
4. Stores vectors and source metadata in `rag_vectors.db`.
5. Preserves filename and page number for citations.

Check index status:

```powershell
python rag_store.py status
```

Search the indexed books:

```powershell
python rag_store.py search "erectile dysfunction initial evaluation" --top-k 6
```

The Flask API also exposes protected RAG routes:

```text
GET  /rag/status    RAG index status
POST /rag/index     Build or refresh the local vector index
POST /rag/search    Search books and return passages with citations
POST /rag/context   Return prompt-ready context for a Clinical Agent
POST /clinical-agent Build a clinician-review packet from RAG plus medication checks
```

These routes use the same HTTP Basic Auth password as `/submissions`.

## Clinical Agent

The protected `/clinical-agent` endpoint uses Gemini as the reasoning layer over an evidence packet that combines:

1. Local RAG guideline retrieval from `rag_vectors.db`.
2. Medication name parsing from supplied medication/history text.
3. openFDA drug label checks.
4. Optional DrugBank checks when `DRUGBANK_API_KEY` is configured.
5. Gemini-generated structured clinical review with citations, medication flags, and safety notes.

The Gemini model defaults to `gemini-2.5-flash` and can be changed with:

```text
GEMINI_CLINICAL_MODEL=your_model_name
```

Example request body:

```json
{
  "query": "erectile dysfunction initial evaluation and medication safety",
  "current_medications": "sildenafil, nitroglycerin",
  "medical_history": "ischemic heart disease",
  "top_k": 4
}
```

You can also pass a saved intake form:

```json
{
  "submission_id": 1,
  "query": "review this case for guideline context and medication safety"
}
```

The endpoint supports clinical review only. It does not diagnose, prescribe, or replace clinician judgment.

## Submit Workflow

When a patient submits the main questionnaire, the app now follows the clinical workflow:

1. Save the form to `intake.db`.
2. Run the Gemini Lifestyle Agent.
3. If lifestyle is sufficient to explain symptoms, stop and return the lifestyle report.
4. If lifestyle is not sufficient, run medication checks through openFDA and optional DrugBank.
5. Retrieve guideline context from the local vector database.
6. Send medication and RAG evidence to the Gemini Clinical Agent.
7. Search PubMed for relevant research papers.
8. Send the Clinical Agent packet and PubMed evidence to the Gemini Research Agent.
9. Store the workflow result inside the saved submission under `clinical_pipeline`.

The browser submit modal shows a short workflow summary after submission. Full details are saved with the submission record.

## Medication Scanning

The medication upload section supports:

- Drug/package photos
- Current medication text
- Medical history summary
- Investigation/lab result summary
- Investigation files or photos

When the user clicks `Scan Uploads`, the server:

1. Saves uploaded files under `uploads/`.
2. Extracts medication text from images if OpenAI Vision or local OCR is configured.
3. Parses possible medication names from image text and manual medication text.
4. Looks up drug label data through openFDA.
5. Checks optional DrugBank data when `DRUGBANK_API_KEY` is set.
6. Stores the scan result with the normal form submission.

The scan is for intake documentation support only. Clinicians must confirm all medication names, doses, warnings, allergies, and interactions before using them for care decisions.

## Routes

```text
GET  /              Main intake form
GET  /style.css     Stylesheet
GET  /script.js     Browser logic
POST /submit        Save completed intake form
POST /scan-drugs    Upload files and run medication lookup
GET  /submissions   Password-protected submitted forms
GET  /uploads/...   Password-protected uploaded files
GET  /rag/status    Password-protected RAG index status
POST /rag/index     Password-protected RAG indexing
POST /rag/search    Password-protected RAG retrieval
POST /rag/context   Password-protected Clinical Agent context
POST /clinical-agent Password-protected RAG plus medication-check agent
```

## Deploying To Vercel

The project includes a Vercel Python Function entry point at `api/index.py`.

Before deploying, configure these environment variables in Vercel Project Settings:

```text
SUBMISSIONS_PASSWORD=use-a-strong-password
GEMINI_API_KEY=your_gemini_key
OPENAI_API_KEY=your_openai_key_optional
OPENFDA_API_KEY=your_openfda_key_optional
DRUGBANK_API_KEY=your_drugbank_key_optional
```

Vercel-specific behavior:

- `vercel.json` routes all requests to the Flask app through `api/index.py`.
- The Python function is configured with `maxDuration: 300` and `memory: 1024`.
- Local-only folders and files are excluded from the serverless bundle, including `RAG Files/`, `uploads/`, local SQLite files, logs, and virtualenvs.
- On Vercel, `DB_PATH`, `UPLOAD_DIR`, and `RAG_DB_PATH` default to `/tmp`.

Important serverless constraint: Vercel's function filesystem is read-only except for `/tmp`, and `/tmp` is temporary. This means submitted forms, uploaded files, generated PDFs, and RAG indexes are not durable on Vercel unless you configure external storage.

For production use, move persistent data to managed services:

- Submissions: Vercel Postgres, Neon, Supabase, or another SQL database.
- Uploads and PDFs: Vercel Blob, S3, Cloudinary, or another object store.
- RAG vectors: a persistent database or vector store instead of the local SQLite file.

## Submitted Forms

Submitted forms are stored in `intake.db`.

View submissions:

```text
http://127.0.0.1:5000/submissions
```

The page uses HTTP Basic Auth. The password is controlled by:

```text
SUBMISSIONS_PASSWORD=your_password
```

If not set, the default password is:

```text
Doctor
```

## Local Data And Privacy

This app stores sensitive medical information locally:

- `intake.db`
- `uploads/`
- `APIkey`
- `rag_vectors.db`
- `RAG Files/`
- generated logs

These files are ignored by git. Do not deploy this app publicly without adding proper production security, HTTPS, authentication, authorization, access logging, backups, and clinical data privacy controls.

## Optional OCR Notes

`pytesseract` is a Python wrapper. For OCR to work, the Tesseract executable must also be installed on the machine and available on the system path.

If OCR is not installed, the app still supports manual medication text and openFDA lookup.

## Development Notes

Useful checks:

```powershell
python -m py_compile app.py
```

Quick Flask smoke test:

```powershell
python -c "from app import app; c=app.test_client(); assert c.get('/').status_code == 200; print('ok')"
```
