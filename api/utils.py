import base64
import hmac
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from flask import Response, render_template, request
from werkzeug.utils import secure_filename

from core.agent_utils import compact_text as first_text
from core.agent_utils import load_secret as read_secret
from core.agent_utils import parse_json_object, request_json
from core.crew_orchestrator import run_full_clinical_pipeline as orchestrate_full_clinical_pipeline
from core.rag_store import build_clinical_context
import nodes.agents as clinical_agent_module
from nodes.agents import (
    build_arabic_pdf_report,
    run_evidence_reviewer_agent,
    run_lifestyle_agent,
    run_report_agent,
    run_research_agent,
    save_report_pdf,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
IS_VERCEL = bool(os.environ.get("VERCEL"))
DB_PATH = os.environ.get("DB_PATH") or (
    os.path.join("/tmp", "intake.db") if IS_VERCEL else os.path.join(BASE_DIR, "intake.db")
)
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or (
    os.path.join("/tmp", "uploads") if IS_VERCEL else os.path.join(BASE_DIR, "uploads")
)
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND") or ("vercel_tmp" if IS_VERCEL else "local_filesystem")
STORAGE_IS_EPHEMERAL = STORAGE_BACKEND == "vercel_tmp"


def deployment_info():
    """Return storage/runtime metadata useful for deployed clients and admin routes."""
    return {
        "platform": "vercel" if IS_VERCEL else "local",
        "storage_backend": STORAGE_BACKEND,
        "storage_ephemeral": STORAGE_IS_EPHEMERAL,
        "database_path": DB_PATH,
        "upload_dir": UPLOAD_DIR,
    }

# Purpose: map the local APIkey file names accepted by the app.
SECRET_ALIASES = {
    "DRUGBANK_API_KEY": {"DRUGBANK_API_KEY", "DRUGBANK"},
    "GEMINI_API_KEY": {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI", "GOOGLE"},
    "OPENAI_API_KEY": {"OPENAI_API_KEY", "OPENAI"},
    "OPENFDA_API_KEY": {"OPENFDA_API_KEY", "OPEN_FDA_API_KEY", "OPENFDA", "FDA_API_KEY"},
}


def load_secret(name):
    """Read a named secret from env vars or the local APIkey file."""
    return read_secret(
        name,
        base_dir=BASE_DIR,
        aliases=SECRET_ALIASES.get(name, {name}),
        bare_value=lambda line: name == "OPENFDA_API_KEY" or (
            name == "GEMINI_API_KEY" and line.startswith("AIza")
        ),
    )

SUBMISSIONS_PASSWORD = os.environ.get("SUBMISSIONS_PASSWORD", "Doctor")
OPENFDA_API_KEY = load_secret("OPENFDA_API_KEY")
OPENAI_API_KEY = load_secret("OPENAI_API_KEY")
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini")
DRUGBANK_API_KEY = load_secret("DRUGBANK_API_KEY")
DRUGBANK_REGION = os.environ.get("DRUGBANK_REGION", "us")
GEMINI_API_KEY = load_secret("GEMINI_API_KEY")
GEMINI_CLINICAL_MODEL = os.environ.get("GEMINI_CLINICAL_MODEL", "gemini-2.5-flash")
GEMINI_RESEARCH_MODEL = os.environ.get("GEMINI_RESEARCH_MODEL", "gemini-2.5-flash")
GEMINI_EVIDENCE_REVIEWER_MODEL = os.environ.get("GEMINI_EVIDENCE_REVIEWER_MODEL", "gemini-2.5-flash")
GEMINI_REPORT_MODEL = os.environ.get("GEMINI_REPORT_MODEL", "gemini-2.5-flash")

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}
ALLOWED_INVESTIGATION_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | {"pdf"}
OPENAI_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_UPLOAD_FILES = 12
MAX_LOOKUP_NAMES = 8

STOP_MEDICATION_WORDS = {
    "after", "as", "before", "bid", "box", "cap", "capsule", "capsules", "current",
    "currently", "daily", "bmp", "dose", "drug", "each", "every", "for", "former",
    "gif", "image", "img", "in", "injection",
    "jpeg", "jpg",
    "last", "medicine", "medication", "medications", "month", "months", "morning",
    "needed", "night", "nightly", "once", "oral", "pack", "patient", "pdf",
    "photo", "pill", "png", "previous", "previously", "prn", "qid", "scan",
    "started", "stopped", "sublingual", "tablet", "tablets", "take", "takes",
    "taking", "the", "tid", "tif", "tiff", "tried", "twice", "use", "used", "uses",
    "using", "webp", "with", "year", "years",
}

DOSE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|kg|ml|iu|units?|%|mmol|meq)\b",
    re.IGNORECASE,
)

def get_db_connection():
    """Open the SQLite intake database and return rows that can be accessed by column name."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intake_forms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT,
            age INTEGER,
            mobile TEXT,
            email TEXT,
            form_data TEXT
        )
    """)
    conn.commit()
    return conn

def safe_json_loads(value, fallback=None):
    """Parse JSON strings safely while returning a fallback for empty or invalid values."""
    if fallback is None:
        fallback = {}
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback

def format_answer(value):
    """Convert a saved form answer into structured data for the submissions page."""
    value = safe_json_loads(value, value)

    if isinstance(value, list) and value and all(isinstance(item, dict) and item.get("url") for item in value):
        links = []
        for item in value:
            url = str(item.get("url", ""))
            links.append({
                "url": url if url.startswith("/uploads/") else "",
                "name": str(item.get("original_name") or item.get("stored_name") or "Uploaded file"),
            })
        return {"type": "links", "links": links}

    if isinstance(value, list):
        return {"type": "text", "text": ", ".join(str(item) for item in value)}

    if isinstance(value, dict):
        return {"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}

    return {"type": "text", "text": "" if value is None else str(value)}

def submissions_authorized():
    """Check whether the current request supplied the correct Basic Auth password."""
    auth = request.authorization
    return bool(auth and hmac.compare_digest(auth.password or "", SUBMISSIONS_PASSWORD))

def password_required_response():
    """Return the 401 response that asks the browser to show a password prompt."""
    return Response(
        "Password required to view submitted forms.",
        401,
        {"WWW-Authenticate": 'Basic realm="Submitted Forms"'}
    )

def clamp_int(value, default, minimum, maximum):
    """Convert a value to an integer and keep it inside the supplied inclusive range."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))

def allowed_extension(filename, allowed_extensions):
    """Return True when the filename has one of the allowed extensions."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in allowed_extensions

def save_uploaded_file(file_obj, category, allowed_extensions):
    """Validate and save one uploaded file, then return metadata used by later routes."""
    if not file_obj or not file_obj.filename:
        return None

    if not allowed_extension(file_obj.filename, allowed_extensions):
        raise ValueError(f"{file_obj.filename} has an unsupported file type.")

    original_name = file_obj.filename
    filename = secure_filename(original_name) or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    date_folder = datetime.utcnow().strftime("%Y%m%d")
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    relative_path = os.path.join(category, date_folder, stored_name)
    destination_dir = os.path.join(UPLOAD_DIR, category, date_folder)
    os.makedirs(destination_dir, exist_ok=True)
    destination_path = os.path.join(destination_dir, stored_name)
    file_obj.save(destination_path)

    return {
        "category": category,
        "original_name": original_name,
        "stored_name": stored_name,
        "relative_path": relative_path.replace(os.sep, "/"),
        "url": f"/uploads/{relative_path.replace(os.sep, '/')}",
        "size": os.path.getsize(destination_path),
        "content_type": file_obj.mimetype or "",
        "extension": ext,
        "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "storage_backend": STORAGE_BACKEND,
        "ephemeral": STORAGE_IS_EPHEMERAL,
    }

def list_value(value):
    """Normalize a scalar or list from an API response into a list of non-empty strings."""
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if value:
        return [str(value)]
    return []

def openfda_quote(value):
    """Escape a search term so openFDA treats it as a quoted exact-value query."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

def summarize_openfda_label(record):
    """Extract the useful medication label fields from one raw openFDA label record."""
    openfda = record.get("openfda") or {}
    return {
        "brand_names": list_value(openfda.get("brand_name")),
        "generic_names": list_value(openfda.get("generic_name")),
        "substance_names": list_value(openfda.get("substance_name")),
        "manufacturer_names": list_value(openfda.get("manufacturer_name")),
        "routes": list_value(openfda.get("route")),
        "dosage_forms": list_value(openfda.get("dosage_form")),
        "product_ndcs": list_value(openfda.get("product_ndc")),
        "rxnorm_ids": list_value(openfda.get("rxcui")),
        "purpose": first_text(record.get("purpose"), 600),
        "indications": first_text(record.get("indications_and_usage"), 800),
        "warnings": first_text(record.get("warnings"), 900),
        "contraindications": first_text(record.get("contraindications"), 700),
        "drug_interactions": first_text(record.get("drug_interactions"), 900),
        "adverse_reactions": first_text(record.get("adverse_reactions"), 700),
    }

def lookup_openfda_label(drug_name):
    """Look up one drug name in openFDA and return a compact label summary or error."""
    params = {"limit": 1}
    if OPENFDA_API_KEY:
        params["api_key"] = OPENFDA_API_KEY

    searches = [
        f"openfda.brand_name:{openfda_quote(drug_name)}",
        f"openfda.generic_name:{openfda_quote(drug_name)}",
        f"openfda.substance_name:{openfda_quote(drug_name)}",
        openfda_quote(drug_name),
    ]

    for search in searches:
        params["search"] = search
        url = "https://api.fda.gov/drug/label.json?" + urlencode(params)
        try:
            payload = request_json(url)
            results = payload.get("results") or []
            if results:
                return {
                    "query": drug_name,
                    "found": True,
                    "source": "openFDA drug label",
                    "search": search,
                    "label": summarize_openfda_label(results[0]),
                }
        except HTTPError as exc:
            if exc.code == 404:
                continue
            return {"query": drug_name, "found": False, "error": f"openFDA returned HTTP {exc.code}"}
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {"query": drug_name, "found": False, "error": f"openFDA lookup failed: {exc}"}

    return {"query": drug_name, "found": False, "message": "No matching openFDA label found."}

def parse_possible_drug_names(*texts):
    """Find likely medication names in free text by removing doses and common filler words."""
    candidates = []
    seen = set()

    for text in texts:
        if not text:
            continue
        parts = re.split(r"[\n,;|/]+", str(text))
        for part in parts:
            cleaned = DOSE_PATTERN.sub(" ", part)
            cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
            cleaned = re.sub(r"[^A-Za-z0-9+.\- ]+", " ", cleaned)
            words = [
                word for word in cleaned.split()
                if word.lower() not in STOP_MEDICATION_WORDS and not word.isdigit()
            ]
            if not words:
                continue
            candidate = " ".join(words[:4]).strip(" .-")
            key = candidate.lower()
            if len(candidate) < 3 or key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if len(candidates) >= MAX_LOOKUP_NAMES:
                return candidates

    return candidates

def output_text_from_openai_response(payload):
    """Collect text output from the OpenAI Responses API payload shape."""
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    chunks = []
    for output_item in payload.get("output", []):
        for content in output_item.get("content", []):
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()

def extract_text_with_openai(saved_files):
    """Use OpenAI vision to read uploaded drug images and return medication text as JSON."""
    if not OPENAI_API_KEY:
        return None, "OpenAI vision is not configured. Set OPENAI_API_KEY to enable image scanning. / لم يتم إعداد OpenAI Vision. أضف OPENAI_API_KEY لتفعيل فحص الصور."

    image_contents = []
    for file_info in saved_files:
        if file_info.get("category") != "drug-images":
            continue
        ext = file_info.get("extension", "").lower()
        if ext not in OPENAI_IMAGE_EXTENSIONS:
            continue
        file_path = os.path.join(UPLOAD_DIR, file_info["relative_path"])
        with open(file_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")
        mime_ext = "jpeg" if ext == "jpg" else ext
        image_contents.append({
            "type": "input_image",
            "image_url": f"data:image/{mime_ext};base64,{encoded}",
            "detail": "high",
        })

    if not image_contents:
        return None, "No OpenAI-compatible drug image was uploaded. / لم يتم رفع صورة دواء متوافقة مع OpenAI."

    prompt = (
        "Extract visible medication information from these drug package or pill images. "
        "Return JSON only with keys: drug_names (array), observed_text (string), "
        "strengths (array), dosage_forms (array), manufacturer_or_ndc (array), confidence_notes (string). "
        "Do not diagnose, prescribe, or infer beyond visible label text."
    )
    body = {
        "model": OPENAI_VISION_MODEL,
        "input": [{
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}] + image_contents,
        }],
    }

    try:
        payload = request_json(
            "https://api.openai.com/v1/responses",
            method="POST",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            body=body,
            timeout=45,
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"OpenAI vision scan failed / فشل فحص الصور باستخدام OpenAI: {exc}"

    text = output_text_from_openai_response(payload)
    return parse_json_object(text, fallback={}) or {"observed_text": text}, None

def extract_text_with_tesseract(saved_files):
    """Use local Tesseract OCR as a fallback to read text from uploaded drug images."""
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return None, "Local OCR is not installed. Install Pillow and pytesseract to enable fallback OCR. / لم يتم تثبيت OCR المحلي. ثبّت Pillow و pytesseract لتفعيل القراءة الاحتياطية."

    text_parts = []
    for file_info in saved_files:
        if file_info.get("category") != "drug-images":
            continue
        if file_info.get("extension", "").lower() not in ALLOWED_IMAGE_EXTENSIONS:
            continue
        try:
            file_path = os.path.join(UPLOAD_DIR, file_info["relative_path"])
            with Image.open(file_path) as image:
                text_parts.append(pytesseract.image_to_string(image))
        except Exception as exc:
            text_parts.append(f"[OCR failed for {file_info.get('original_name')}: {exc}]")

    text = "\n".join(part for part in text_parts if part.strip()).strip()
    if not text:
        return None, "Local OCR did not extract text from the uploaded drug images. / لم يستخرج OCR المحلي نصًا من صور الأدوية المرفوعة."
    return {"observed_text": text, "drug_names": parse_possible_drug_names(text)}, None

def lookup_drugbank(drug_names):
    """Query DrugBank for product matches and possible interactions between parsed drugs."""
    if not DRUGBANK_API_KEY:
        return {
            "configured": False,
            "message": "DrugBank is not configured. Set DRUGBANK_API_KEY on the server to enable DrugBank lookups. / لم يتم إعداد DrugBank. أضف DRUGBANK_API_KEY على الخادم لتفعيل البحث.",
        }

    matches = []
    product_concept_ids = []

    for name in drug_names[:MAX_LOOKUP_NAMES]:
        params = {"q": name, "region": DRUGBANK_REGION, "per_page": 3}
        url = "https://api.drugbank.com/v1/product_concepts?" + urlencode(params)
        try:
            payload = request_json(url, headers={"Authorization": DRUGBANK_API_KEY})
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            matches.append({"query": name, "error": f"DrugBank lookup failed: {exc}"})
            continue

        items = payload if isinstance(payload, list) else payload.get("results", [])
        simplified = []
        for item in items[:3]:
            product_id = item.get("drugbank_pcid") or item.get("id")
            if product_id and product_id not in product_concept_ids:
                product_concept_ids.append(product_id)
            simplified.append({
                "name": item.get("name") or item.get("display_name"),
                "drugbank_pcid": product_id,
                "brand": item.get("brand"),
                "route": item.get("route"),
                "form": item.get("form"),
                "standing": item.get("standing"),
            })
        matches.append({"query": name, "matches": simplified})

    interactions = []
    if len(product_concept_ids) >= 2:
        try:
            interactions_payload = request_json(
                "https://api.drugbank.com/v1/ddi",
                method="POST",
                headers={"Authorization": DRUGBANK_API_KEY},
                body={"product_concept_id": product_concept_ids},
            )
            interactions = interactions_payload if isinstance(interactions_payload, list) else interactions_payload.get("interactions", [])
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            interactions = [{"error": f"DrugBank interaction lookup failed: {exc}"}]

    return {"configured": True, "matches": matches, "interactions": interactions[:20]}

def build_label_flags(openfda_results, current_medications, medical_history):
    """Create review flags when openFDA label text mentions patient meds or history terms."""
    combined_context = f"{current_medications}\n{medical_history}".lower()
    context_names = [name.lower() for name in parse_possible_drug_names(current_medications)]
    history_words = {
        word.lower()
        for word in re.findall(r"[A-Za-z][A-Za-z\-]{4,}", medical_history or "")
        if word.lower() not in STOP_MEDICATION_WORDS
    }

    flags = []
    for result in openfda_results:
        if not result.get("found"):
            continue
        label = result.get("label") or {}
        interaction_text = (label.get("drug_interactions") or "").lower()
        warning_text = " ".join([
            label.get("warnings") or "",
            label.get("contraindications") or "",
        ]).lower()

        for name in context_names:
            if name and name != result.get("query", "").lower() and name in interaction_text:
                flags.append({
                    "type": "label_interaction_text_match",
                    "drug": result.get("query"),
                    "matched_context": name,
                    "message": f"The openFDA label interaction section mentions {name}.",
                })

        for word in list(history_words)[:20]:
            if word in warning_text and word in combined_context:
                flags.append({
                    "type": "label_warning_history_text_match",
                    "drug": result.get("query"),
                    "matched_context": word,
                    "message": f"The openFDA warning or contraindication text mentions {word}.",
                })

    return flags[:20]

def clinical_agent_dependencies():
    """Package local helper functions so the external clinical agent module can call them."""
    return {
        "build_clinical_context": safe_build_clinical_context,
        "build_label_flags": build_label_flags,
        "clamp_int": clamp_int,
        "get_db_connection": get_db_connection,
        "gemini_api_key": GEMINI_API_KEY,
        "gemini_clinical_model": GEMINI_CLINICAL_MODEL,
        "lookup_drugbank": lookup_drugbank,
        "lookup_openfda_label": lookup_openfda_label,
        "max_lookup_names": MAX_LOOKUP_NAMES,
        "parse_possible_drug_names": parse_possible_drug_names,
        "safe_json_loads": safe_json_loads,
    }

def safe_build_clinical_context(query, top_k=6):
    """Return RAG context when available, or an empty context when deployment lacks a local index."""
    try:
        return build_clinical_context(query, top_k=top_k)
    except RuntimeError as exc:
        return {
            "query": query,
            "context": "",
            "sources": [],
            "error": str(exc),
        }

def run_full_clinical_pipeline(data, submission_id=None):
    """Run the diagrammed workflow through the core orchestrator."""
    return orchestrate_full_clinical_pipeline(
        data,
        submission_id=submission_id,
        gemini_api_key=GEMINI_API_KEY,
        gemini_research_model=GEMINI_RESEARCH_MODEL,
        gemini_evidence_reviewer_model=GEMINI_EVIDENCE_REVIEWER_MODEL,
        gemini_report_model=GEMINI_REPORT_MODEL,
        clinical_agent_module=clinical_agent_module,
        clinical_agent_dependencies=clinical_agent_dependencies,
        run_lifestyle_agent=run_lifestyle_agent,
        run_research_agent=run_research_agent,
        run_evidence_reviewer_agent=run_evidence_reviewer_agent,
        run_report_agent=run_report_agent,
        build_arabic_pdf_report=build_arabic_pdf_report,
        save_report_pdf=save_report_pdf,
        upload_dir=UPLOAD_DIR,
        storage_backend=STORAGE_BACKEND,
        storage_is_ephemeral=STORAGE_IS_EPHEMERAL,
    )

def init_db():
    """Create the intake_forms SQLite table if it does not already exist."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS intake_forms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT,
            age INTEGER,
            mobile TEXT,
            email TEXT,
            form_data TEXT
        )
    """)
    conn.commit()
    conn.close()

def render_ai_report(pipeline):
    """Render the clinical pipeline result with the frontend AI report partial."""
    final_report = {}
    report_pdf = {}
    if pipeline:
        report_agent = pipeline.get("report_agent", {})
        final_report = report_agent.get("report") or pipeline.get("final_report") or {}
        if final_report and not any(key in final_report for key in ("executive_summary", "patient_snapshot", "report_type")):
            final_report = {}
        report_pdf = pipeline.get("report_pdf") or {}

    return render_template(
        "ai_report.html",
        pipeline=pipeline,
        final_report=final_report,
        report_pdf=report_pdf,
        format_evidence_claims=format_evidence_claims,
    )

def format_evidence_claims(items):
    """Convert evidence reviewer claim dictionaries into readable list rows."""
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            rows.append(str(item))
            continue
        parts = [
            item.get("claim"),
            item.get("support"),
            item.get("quality_reason") or item.get("quality_issue"),
        ]
        rows.append(" | ".join(str(part) for part in parts if part))
    return rows

