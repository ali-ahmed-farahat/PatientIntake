from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import sqlite3
import json
import os
import hmac
import base64
import importlib.util
import re
import uuid
from datetime import datetime
from html import escape
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from werkzeug.utils import secure_filename
from agent_utils import compact_text as first_text
from agent_utils import load_secret as read_secret
from agent_utils import parse_json_object, request_json
from evidence_reviewer_agent import run_evidence_reviewer_agent
from lifestyle_agent import run_lifestyle_agent
from rag_store import build_clinical_context, index_rag_files, rag_status, search_rag
from report_agent import build_arabic_pdf_report, run_report_agent, save_report_pdf
from research_agent import run_research_agent

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "intake.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

def load_clinical_agent_module():
    """Load the separate clinical agent module from the local file with a space in its name."""
    module_path = os.path.join(BASE_DIR, "clinical_agent.py")
    spec = importlib.util.spec_from_file_location("clinical_agent_module", module_path)
    if not spec or not spec.loader:
        raise RuntimeError("Could not load clinical agent module.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

clinical_agent_module = load_clinical_agent_module()

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    """Convert a saved form answer into safe HTML for the submissions page."""
    value = safe_json_loads(value, value)

    if isinstance(value, list) and value and all(isinstance(item, dict) and item.get("url") for item in value):
        links = []
        for item in value:
            url = str(item.get("url", ""))
            name = escape(str(item.get("original_name") or item.get("stored_name") or "Uploaded file"))
            if url.startswith("/uploads/"):
                links.append(f'<li><a href="{escape(url)}">{name}</a></li>')
            else:
                links.append(f"<li>{name}</li>")
        return f"<ul>{''.join(links)}</ul>"

    if isinstance(value, list):
        return escape(", ".join(str(item) for item in value))

    if isinstance(value, dict):
        return escape(json.dumps(value, ensure_ascii=False, indent=2))

    return escape("" if value is None else str(value))

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
        "build_clinical_context": build_clinical_context,
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

def default_clinical_query(data):
    parts = [
        data.get("chiefComplaint") or data.get("chief_complaint"),
        data.get("medicalHistory") or data.get("medical_history"),
        data.get("currentMedications") or data.get("current_medications"),
        data.get("investigationResults") or data.get("investigation_results"),
    ]
    context = " ".join(str(part) for part in parts if part).strip()
    if context:
        return f"Clinical review for men's sexual health symptoms: {context}"
    return "Clinical review for men's sexual health symptoms, medication safety, and guideline context."

def attach_report_agent_output(pipeline, data=None):
    """Run the final report agent and save its structured report as a PDF."""
    data = data or {}
    report_result = run_report_agent(
        pipeline,
        api_key=GEMINI_API_KEY,
        model_name=GEMINI_REPORT_MODEL,
    )
    pipeline["report_agent"] = report_result
    pipeline["final_report"] = report_result.get("report")
    arabic_pdf_report, translation_error = build_arabic_pdf_report(
        report_result.get("report") or {},
        api_key=GEMINI_API_KEY,
        model_name=GEMINI_REPORT_MODEL,
    )
    if translation_error:
        pipeline["report_pdf_translation_error"] = translation_error

    try:
        pipeline["report_pdf"] = save_report_pdf(
            arabic_pdf_report,
            upload_dir=UPLOAD_DIR,
            submission_id=pipeline.get("submission_id"),
            patient_name=data.get("fullName") or data.get("full_name"),
            code_no=data.get("codeNo") or data.get("code_no"),
            arabic=True,
        )
    except (OSError, RuntimeError) as exc:
        pipeline["report_pdf"] = {"error": str(exc)}

    pipeline["stopped_after"] = "report_agent"
    return pipeline

def run_full_clinical_pipeline(data, submission_id=None):
    """Run the diagrammed workflow: lifestyle triage, then clinical and research agents if needed."""
    pipeline = {
        "workflow": [
            "lifestyle_agent",
            "medication_check_and_vector_rag",
            "clinical_agent",
            "research_agent",
            "evidence_reviewer_agent",
            "report_agent",
        ],
        "submission_id": submission_id,
        "status": "started",
    }

    if not GEMINI_API_KEY:
        pipeline.update({
            "status": "error",
            "error": "GEMINI_API_KEY is not configured.",
        })
        return attach_report_agent_output(pipeline, data)

    lifestyle_result = run_lifestyle_agent(data, GEMINI_API_KEY)
    pipeline["lifestyle_agent"] = lifestyle_result

    if not lifestyle_result.get("proceed_to_pipeline"):
        pipeline.update({
            "status": "completed",
            "stopped_after": "lifestyle_agent",
            "final_report": {
                "type": "lifestyle_triage",
                "summary": lifestyle_result.get("reasoning", ""),
                "recommendations": lifestyle_result.get("lifestyle_recommendations", []),
                "flags": lifestyle_result.get("flags", []),
            },
        })
        return pipeline

    clinical_input = dict(data)
    clinical_input.setdefault("query", default_clinical_query(data))
    if submission_id is not None:
        clinical_input["submission_id"] = submission_id

    clinical_result = clinical_agent_module.build_clinical_agent_response(
        clinical_input,
        clinical_agent_dependencies(),
    )
    pipeline["clinical_agent"] = clinical_result

    research_result = run_research_agent(
        clinical_result,
        api_key=GEMINI_API_KEY,
        model_name=GEMINI_RESEARCH_MODEL,
        max_pubmed_results=5,
    )
    pipeline["research_agent"] = research_result

    evidence_review_result = run_evidence_reviewer_agent(
        research_result,
        api_key=GEMINI_API_KEY,
        model_name=GEMINI_EVIDENCE_REVIEWER_MODEL,
    )
    pipeline["evidence_reviewer_agent"] = evidence_review_result

    pipeline.update({
        "status": "completed",
        "stopped_after": "evidence_reviewer_agent",
        "final_report": {
            "research": research_result.get("report"),
            "evidence_review": evidence_review_result.get("report"),
        },
    })
    return attach_report_agent_output(pipeline, data)

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

@app.route("/")
def website():
    """Serve the main intake form page."""
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/style.css")
def css():
    """Serve the frontend stylesheet."""
    return send_from_directory(BASE_DIR, "style.css")

@app.route("/script.js")
def js():
    """Serve the frontend JavaScript file."""
    return send_from_directory(BASE_DIR, "script.js")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """Serve a protected uploaded file after validating the requested path is safe."""
    if not submissions_authorized():
        return password_required_response()

    normalized = os.path.normpath(filename)
    if normalized.startswith("..") or os.path.isabs(normalized):
        return Response("Invalid upload path.", 400)

    directory = os.path.join(UPLOAD_DIR, os.path.dirname(normalized))
    response = send_from_directory(directory, os.path.basename(normalized))
    if normalized.replace("\\", "/").startswith("reports/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@app.route("/scan-drugs", methods=["POST"])
def scan_drugs():
    """Handle medication image uploads, OCR them, and run openFDA/DrugBank checks."""
    saved_files = []
    errors = []

    upload_groups = [
        ("drugImages", "drug-images", ALLOWED_IMAGE_EXTENSIONS),
        ("investigationFiles", "investigations", ALLOWED_INVESTIGATION_EXTENSIONS),
    ]

    for field_name, category, allowed_extensions in upload_groups:
        for file_obj in request.files.getlist(field_name)[:MAX_UPLOAD_FILES]:
            try:
                saved = save_uploaded_file(file_obj, category, allowed_extensions)
                if saved:
                    saved_files.append(saved)
            except ValueError as exc:
                errors.append(str(exc))

    current_medications = request.form.get("currentMedications", "")
    medical_history = request.form.get("medicalHistory", "")

    has_drug_images = any(file_info.get("category") == "drug-images" for file_info in saved_files)
    ai_note = None
    ocr_note = None
    ai_scan = None

    if has_drug_images:
        ai_scan, ai_note = extract_text_with_openai(saved_files)
        if ai_scan is None:
            ai_scan, ocr_note = extract_text_with_tesseract(saved_files)

    extracted_text = ""
    extracted_names = []
    scan_source = "manual_text"

    if ai_scan:
        scan_source = "openai_vision" if not ai_note else "local_ocr"
        extracted_text = first_text(ai_scan.get("observed_text"), 2000)
        extracted_names = [
            str(name).strip()
            for name in ai_scan.get("drug_names", [])
            if str(name).strip()
        ]

    drug_candidates = []
    for name in extracted_names + parse_possible_drug_names(current_medications, extracted_text):
        key = name.lower()
        if key not in {candidate.lower() for candidate in drug_candidates}:
            drug_candidates.append(name)
        if len(drug_candidates) >= MAX_LOOKUP_NAMES:
            break

    openfda_results = [lookup_openfda_label(name) for name in drug_candidates]
    drugbank_result = lookup_drugbank(drug_candidates)
    label_flags = build_label_flags(openfda_results, current_medications, medical_history)

    notes = [
        "This scan supports intake review only and is not a diagnosis, prescription, or medication-safety decision. / هذا الفحص لمراجعة بيانات الاستبيان فقط وليس تشخيصًا أو وصفة أو قرارًا علاجيًا.",
        "Confirm all detected medication names, strengths, and warnings with a licensed clinician. / يجب تأكيد أسماء الأدوية والجرعات والتحذيرات مع طبيب مختص.",
    ]
    for note in (ai_note, ocr_note):
        if note:
            notes.append(note)
    notes.extend(errors)

    return jsonify({
        "message": "Upload received and medication lookup completed. / تم استلام الملفات وإكمال البحث عن الأدوية.",
        "files": saved_files,
        "scan_source": scan_source,
        "extracted_text": extracted_text,
        "drug_candidates": drug_candidates,
        "openfda": openfda_results,
        "drugbank": drugbank_result,
        "label_flags": label_flags,
        "notes": notes,
    })

def render_ai_report(pipeline):
    """Render the clinical pipeline result as HTML for the submissions page."""
    if not pipeline:
        return '<p class="ai-missing">No AI report available for this submission.</p>'

    def safe_escape(value):
        return escape("" if value is None else str(value))

    status = pipeline.get("status", "")
    stopped_after = pipeline.get("stopped_after", "")
    html_parts = []

    # Purpose: show overall pipeline status first.
    badge_color = "#2e7d32" if status == "completed" else "#b71c1c"
    html_parts.append(
        f'<div class="ai-status" style="background:{badge_color}">'
        f'Pipeline: {escape(status.upper())} - stopped after: {escape(stopped_after)}'
        f'</div>'
    )

    if pipeline.get("error"):
        html_parts.append(f'<div class="ai-flag">{escape(pipeline["error"])}</div>')
        return "\n".join(html_parts)

    # Purpose: render the lifestyle triage stage.
    lifestyle = pipeline.get("lifestyle_agent", {})
    if lifestyle:
        decision = lifestyle.get("decision", "")
        confidence = lifestyle.get("confidence", "")
        decision_color = "#2e7d32" if decision == "YES" else "#e65100"
        html_parts.append(f'''
        <div class="ai-section">
          <div class="ai-section-title">Lifestyle Triage</div>
          <div class="ai-decision" style="border-left-color:{decision_color}">
            <strong>Decision:</strong> {escape(decision)} &nbsp;|&nbsp;
            <strong>Confidence:</strong> {escape(confidence)}<br>
            <em>{escape(lifestyle.get("reasoning", ""))}</em>
          </div>
          {"".join(f'<div class="ai-flag">{escape(f)}</div>' for f in lifestyle.get("flags", []))}
          {render_list("Lifestyle Recommendations", lifestyle.get("lifestyle_recommendations", []))}
        </div>''')

    if stopped_after == "lifestyle_agent":
        return "\n".join(html_parts)

    # Purpose: render medication checks, RAG sources, and CrewAI clinical memo.
    clinical = pipeline.get("clinical_agent", {})
    ca_report = clinical.get("clinical_agent", {}).get("report", {}) if clinical else {}
    med_checks = clinical.get("medication_checks", {}) if clinical else {}
    rag = clinical.get("rag", {}) if clinical else {}

    if ca_report:
        confidence = ca_report.get("confidence", "")
        conf_color = {"high": "#2e7d32", "moderate": "#e65100", "low": "#b71c1c"}.get(confidence, "#555")
        html_parts.append(f'''
        <div class="ai-section">
          <div class="ai-section-title">Clinical Agent - CrewAI</div>
          <div class="ai-summary-box">
            <p>{escape(ca_report.get("clinical_summary", ""))}</p>
            <span class="ai-badge" style="background:{conf_color}">Confidence: {escape(confidence)}</span>
          </div>
          {render_flags(med_checks.get("label_flags", []))}
          {render_list("Key Findings", ca_report.get("key_findings", []))}
          {render_list("Medication Safety", ca_report.get("medication_safety", []), warn=True)}
          {render_list("Red Flags", ca_report.get("red_flags", []), danger=True)}
          {render_list("Guideline Context", ca_report.get("guideline_context", []))}
          {render_list("Missing Information", ca_report.get("missing_information", []))}
          {render_list("Recommended Follow-up Questions", ca_report.get("recommended_next_questions", []))}
          {render_list("Medications Checked", med_checks.get("drug_candidates", []))}
          {render_rag_sources(rag)}
          {render_list("Citations", ca_report.get("source_citations", []))}
          {render_list("Limitations", ca_report.get("limitations", []))}
        </div>''')

    # Purpose: render PubMed/CrewAI research synthesis.
    research = pipeline.get("research_agent", {})
    ra_report = research.get("report", {}) if research else {}

    if ra_report:
        pubmed_papers = research.get("pubmed_papers", [])
        pubmed_error = research.get("pubmed_error")
        html_parts.append(f'''
        <div class="ai-section">
          <div class="ai-section-title">Research Agent - PubMed + CrewAI</div>
          <div class="ai-summary-box">
            <p>{escape(ra_report.get("research_summary", ""))}</p>
          </div>
          {render_list("Evidence Points", ra_report.get("evidence_points", []))}
          {render_list("Clinical Relevance", ra_report.get("clinical_relevance", []))}
          {render_list("Conflicts or Uncertainties", ra_report.get("conflicts_or_uncertainties", []))}
          {render_list("Suggested Clinician Review", ra_report.get("suggested_clinician_review", []))}
          {render_pubmed_papers(pubmed_papers, pubmed_error)}
          {render_list("Citations", ra_report.get("citations", []))}
          {render_list("Limitations", ra_report.get("limitations", []))}
        </div>''')

    # Purpose: render CrewAI evidence quality control after research synthesis.
    evidence_review = pipeline.get("evidence_reviewer_agent", {})
    er_report = evidence_review.get("report", {}) if evidence_review else {}

    if er_report:
        quality = str(er_report.get("overall_evidence_quality") or "not stated")
        readiness = str(er_report.get("final_report_readiness") or "not stated")
        html_parts.append(f'''
        <div class="ai-section">
          <div class="ai-section-title">Evidence Reviewer Agent - CrewAI</div>
          <div class="ai-summary-box">
            <p>{escape(er_report.get("reviewer_summary", ""))}</p>
            <span class="ai-badge" style="background:#1f4e79">Quality: {escape(quality)}</span>
            <span class="ai-badge" style="background:#607d8b">Readiness: {escape(readiness)}</span>
          </div>
          {render_list("High Confidence Claims", format_evidence_claims(er_report.get("high_confidence_claims", [])))}
          {render_list("Moderate Confidence Claims", format_evidence_claims(er_report.get("moderate_confidence_claims", [])), warn=True)}
          {render_list("Low Confidence or Unsupported Claims", format_evidence_claims(er_report.get("low_confidence_or_unsupported_claims", [])), danger=True)}
          {render_list("Citation Quality Issues", er_report.get("citation_quality_issues", []), warn=True)}
          {render_list("Missing Evidence", er_report.get("missing_evidence", []), warn=True)}
          {render_list("Overstatement Risks", er_report.get("overstatement_risks", []), warn=True)}
          {render_list("Evidence Conflicts", er_report.get("evidence_conflicts", []), warn=True)}
          {render_list("Clinician Review Priorities", er_report.get("clinician_review_priorities", []))}
          {render_list("Limitations", er_report.get("limitations", []))}
        </div>''')

    # Purpose: render the final structured report and PDF link.
    report_agent = pipeline.get("report_agent", {})
    final_report = report_agent.get("report") or pipeline.get("final_report") or {}
    if final_report and not any(key in final_report for key in ("executive_summary", "patient_snapshot", "report_type")):
        final_report = {}
    report_pdf = pipeline.get("report_pdf") or {}

    if final_report:
        snapshot = final_report.get("patient_snapshot") or {}
        pdf_error_html = ""
        if report_pdf.get("error"):
            pdf_error_html = f'<div class="ai-flag">PDF generation failed: {escape(report_pdf["error"])}</div>'

        html_parts.append(f'''
        <div class="ai-section">
          <div class="ai-section-title">Final Report Agent - Structured Output</div>
          <div class="ai-summary-box">
            <p>{safe_escape(final_report.get("executive_summary", ""))}</p>
            <span class="ai-badge" style="background:#1f4e79">Type: {safe_escape(final_report.get("report_type", ""))}</span>
            <span class="ai-badge" style="background:#607d8b">Confidence: {safe_escape(final_report.get("confidence", ""))}</span>
          </div>
          {pdf_error_html}
          {render_list("Clinical Summary", [final_report.get("clinical_summary", "")])}
          {render_list("Findings", final_report.get("findings", []))}
          {render_list("Citations", final_report.get("citations") or final_report.get("source_citations", []))}
          <div class="ai-list"><strong style="color:#1f4e79">Patient Snapshot</strong>
            <ul>
              <li>Submission ID: {safe_escape(snapshot.get("submission_id", ""))}</li>
              <li>Age: {safe_escape(snapshot.get("age", ""))}</li>
              <li>Sex: {safe_escape(snapshot.get("sex", ""))}</li>
              <li>Question: {safe_escape(snapshot.get("presenting_question", ""))}</li>
            </ul>
          </div>
          {render_list("Urgent Safety Alerts", final_report.get("urgent_safety_alerts", []), danger=True)}
          {render_list("Clinical Findings", final_report.get("clinical_findings", []))}
          {render_list("Medication Safety", final_report.get("medication_safety", []), warn=True)}
          {render_list("Evidence Summary", final_report.get("evidence_summary", []))}
          {render_list("Clinician Actions", final_report.get("clinician_actions", []))}
          {render_list("Missing Information", final_report.get("missing_information", []), warn=True)}
          {render_list("Limitations", final_report.get("limitations", []))}
        </div>''')

    return "\n".join(html_parts)


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


def render_list(title, items, warn=False, danger=False):
    if not items:
        return ""
    color = "#b71c1c" if danger else ("#e65100" if warn else "#1f4e79")
    lis = "".join(f"<li>{escape(str(item))}</li>" for item in items)
    return f'<div class="ai-list"><strong style="color:{color}">{escape(title)}</strong><ul>{lis}</ul></div>'


def render_flags(flags):
    if not flags:
        return '<div class="ai-ok">No medication interaction flags detected.</div>'
    items = "".join(
        f'<div class="ai-flag"><strong>{escape(f.get("drug",""))}</strong>: {escape(f.get("message",""))}</div>'
        for f in flags
    )
    return items


def render_rag_sources(rag):
    sources = rag.get("sources", []) if rag else []
    if not sources:
        return ""
    lis = "".join(
        f'<li>{escape(s.get("citation",""))} <span class="ai-score">score {escape(str(s.get("score","")))}</span></li>'
        for s in sources
    )
    return f'<div class="ai-list"><strong style="color:#1f4e79">Guideline Sources</strong><ul>{lis}</ul></div>'


def render_pubmed_papers(papers, error):
    if error and not papers:
        return f'<div class="ai-flag">PubMed error: {escape(error)}</div>'
    if not papers:
        return ""
    cards = []
    for p in papers:
        pmid = escape(p.get("pmid", ""))
        url = escape(p.get("url", ""))
        title = escape(p.get("title", "No title"))
        journal = escape(p.get("journal", ""))
        year = escape(p.get("year", ""))
        abstract = escape(p.get("abstract", ""))
        link = f'<a href="{url}" target="_blank">PMID {pmid}</a>' if url else f"PMID {pmid}"
        cards.append(
            f'<div class="pubmed-card">'
            f'<div class="pubmed-title">{title}</div>'
            f'<div class="pubmed-meta">{journal} {year} &mdash; {link}</div>'
            f'<div class="pubmed-abstract">{abstract}</div>'
            f'</div>'
        )
    return (
        '<div class="ai-list"><strong style="color:#1f4e79">PubMed Papers Retrieved</strong>'
        + "".join(cards)
        + '</div>'
    )


# Purpose: style the password-protected submissions page without burying the route in CSS.
SUBMISSIONS_PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:30px;background:#f4f7fb;color:#172033;font-family:Arial,sans-serif}
main{max-width:1100px;margin:auto}
h1{margin:0 0 22px;color:#1f4e79}
.toolbar{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap}
a{color:#1f4e79;font-weight:700;text-decoration:none}
.ai-download-button{display:inline-block;margin:4px 0 10px;padding:10px 14px;border-radius:6px;background:#1f4e79;color:#fff;font-weight:700;text-decoration:none}
.ai-download-button:hover{background:#163a5f}
.submission,.empty{background:#fff;border-radius:8px}
.submission{margin-bottom:28px;padding:24px;box-shadow:0 8px 24px rgba(15,23,42,.09)}
.submission h2{margin:0 0 14px;color:#1f4e79;font-size:20px}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:18px;padding:12px;border-radius:6px;background:#f0f6ff}
.submission-tabs{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.submission-tab{display:inline-flex;align-items:center;min-height:38px;padding:8px 14px;border:1px solid #c9d7e6;border-radius:6px;background:#eef4fb;color:#1f4e79;font:inherit;font-weight:700;font-size:13px;cursor:pointer}
.submission-download{border-color:#1f4e79;background:#1f4e79;color:#fff;text-decoration:none}
.submission-download:hover{background:#163a5f}
.submission-tab-active{border-color:#1f4e79;background:#dcecff}
.submission-tab-muted{color:#6b7280;background:#f5f7fa;cursor:not-allowed}
.submission-panel[hidden]{display:none}
.form-details{margin-bottom:20px;border:1px solid #d4dce7;border-radius:6px;overflow:hidden}
.form-details summary{padding:10px 14px;background:#eef4fb;color:#1f4e79;font-weight:700;cursor:pointer}
table{width:100%;border-collapse:collapse;table-layout:fixed}
th,td{padding:9px;border:1px solid #d4dce7;text-align:left;vertical-align:top;word-break:break-word}
th{width:260px;background:#eef4fb;color:#1f4e79}
.ai-report{border:2px solid #1f4e79;border-radius:8px;overflow:hidden}
.ai-report-title{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 16px;background:#1f4e79;color:#fff;font-weight:700;font-size:15px;letter-spacing:0;cursor:pointer;list-style:none}
.ai-report-title::-webkit-details-marker{display:none}
.ai-report-title::after{content:"+";width:22px;height:22px;border:1px solid rgba(255,255,255,.55);border-radius:4px;text-align:center;line-height:20px;font-size:18px}
.ai-report[open] .ai-report-title::after{content:"-"}
.ai-report-content{background:#fff}
.ai-section{padding:16px;border-bottom:1px solid #d4dce7}
.ai-section:last-child{border-bottom:0}
.ai-section-title{margin-bottom:10px;color:#1f4e79;font-weight:700;font-size:15px}
.ai-status{display:inline-block;margin-bottom:12px;padding:5px 12px;border-radius:4px;color:#fff;font-size:12px;font-weight:700}
.ai-missing{padding:12px;color:#888;font-style:italic}
.ai-summary-box,.ai-decision,.ai-flag,.ai-ok{margin-bottom:10px;padding:10px 14px;border-radius:0 6px 6px 0}
.ai-summary-box{border-left:4px solid #1f4e79;background:#f7fbff}
.ai-summary-box p{margin:0 0 8px;line-height:1.6}
.ai-badge{display:inline-block;padding:3px 10px;border-radius:8px;color:#fff;font-size:12px;font-weight:700}
.ai-decision{border-left:4px solid #e65100;background:#fffdf0;line-height:1.7}
.ai-flag{border-left:4px solid #b71c1c;background:#fff3f3;color:#7f0000;font-size:14px}
.ai-ok{border-left:4px solid #2e7d32;background:#f0fff4;color:#1b5e20;font-size:14px}
.ai-list{margin-bottom:12px}
.ai-list ul{margin:6px 0 0;padding-left:20px}
.ai-list li{margin-bottom:4px;font-size:14px;line-height:1.55}
.ai-score,.pubmed-meta{color:#666;font-size:12px}
.pubmed-card{margin-top:8px;padding:10px 14px;border:1px solid #c9dff5;border-radius:6px;background:#f7fbff}
.pubmed-title{margin-bottom:4px;font-weight:700;font-size:14px}
.pubmed-abstract{color:#333;font-size:13px;line-height:1.55}
.empty{padding:22px}
@media(max-width:760px){
  body{padding:12px}
  h1{margin-bottom:0;font-size:22px}
  .submission{padding:14px}
  .summary{grid-template-columns:1fr}
  table{display:block;overflow-x:auto;table-layout:auto}
  th,td{min-width:160px;word-break:normal}
  th{width:auto}
}
""".strip()


@app.route("/submissions")
def submissions():
    """Render a password-protected HTML page listing all submitted intake forms."""
    if not submissions_authorized():
        return password_required_response()

    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, full_name, age, mobile, email, form_data
        FROM intake_forms
        ORDER BY id DESC
    """).fetchall()
    conn.close()

    if not rows:
        submissions_html = "<p class=\"empty\">No forms submitted yet.</p>"
    else:
        cards = []

        for row in rows:
            try:
                form_data = json.loads(row["form_data"] or "{}")
            except json.JSONDecodeError:
                form_data = {}

            pipeline = form_data.pop("clinical_pipeline", None)

            # Purpose: render saved patient answers without the pipeline blob.
            answers = "\n".join(
                f"<tr><th>{escape(str(key))}</th><td>{format_answer(value)}</td></tr>"
                for key, value in form_data.items()
                if key != "clinical_pipeline"
            )

            ai_html = render_ai_report(pipeline)
            report_pdf = (pipeline or {}).get("report_pdf") or {}
            form_panel_id = f"form-panel-{row['id']}"
            ai_panel_id = f"ai-panel-{row['id']}"
            pdf_button = '<span class="submission-tab submission-tab-muted">Download PDF Report</span>'
            if report_pdf.get("url"):
                pdf_button = (
                    f'<a class="submission-tab submission-download" href="{escape(report_pdf["url"])}" '
                    f'target="_blank" download>Download PDF Report</a>'
                )
            elif report_pdf.get("error"):
                pdf_button = f'<span class="submission-tab submission-tab-muted">PDF unavailable</span>'

            cards.append(f"""
              <article class="submission">
                <h2>Submission #{escape(str(row["id"]))}</h2>
                <div class="summary">
                  <span><strong>Name:</strong> {escape(str(row["full_name"] or ""))}</span>
                  <span><strong>Age:</strong> {escape(str(row["age"] or ""))}</span>
                  <span><strong>Mobile:</strong> {escape(str(row["mobile"] or ""))}</span>
                  <span><strong>Email:</strong> {escape(str(row["email"] or ""))}</span>
                </div>

                <div class="submission-tabs">
                  {pdf_button}
                  <button class="submission-tab" type="button" data-panel-target="{form_panel_id}" aria-controls="{form_panel_id}" aria-expanded="false">Patient Form Answers</button>
                  <button class="submission-tab" type="button" data-panel-target="{ai_panel_id}" aria-controls="{ai_panel_id}" aria-expanded="false">AI Clinical Report</button>
                </div>

                <div id="{form_panel_id}" class="submission-panel form-details" hidden>
                  <table><tbody>{answers}</tbody></table>
                </div>

                <div id="{ai_panel_id}" class="submission-panel ai-report" hidden>
                  <div class="ai-report-content">
                  {ai_html}
                  </div>
                </div>
              </article>
            """)

        submissions_html = "\n".join(cards)

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Submitted Forms</title>
  <style>{SUBMISSIONS_PAGE_CSS}</style>
</head>
<body>
  <main>
    <div class="toolbar">
      <h1>Submitted Forms</h1>
      <a href="/">Back to form</a>
    </div>
    {submissions_html}
  </main>
  <script>
    document.addEventListener("click", function (event) {{
      const button = event.target.closest("[data-panel-target]");
      if (!button) return;
      const card = button.closest(".submission");
      const panel = document.getElementById(button.dataset.panelTarget);
      if (!card || !panel) return;

      const shouldOpen = panel.hidden;
      card.querySelectorAll(".submission-panel").forEach(function (item) {{
        item.hidden = true;
      }});
      card.querySelectorAll("[data-panel-target]").forEach(function (item) {{
        item.setAttribute("aria-expanded", "false");
        item.classList.remove("submission-tab-active");
      }});

      panel.hidden = !shouldOpen;
      button.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
      button.classList.toggle("submission-tab-active", shouldOpen);
    }});
  </script>
</body>
</html>
"""

@app.route("/submit", methods=["POST"])
def submit_form():
    """Save a submitted intake form, then run the configured clinical agent workflow."""
    data = request.json or {}

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO intake_forms 
        (full_name, age, mobile, email, form_data)
        VALUES (?, ?, ?, ?, ?)
    """, (
        data.get("fullName"),
        data.get("age"),
        data.get("mobile"),
        data.get("email"),
        json.dumps(data)
    ))

    submission_id = cur.lastrowid
    conn.commit()
    conn.close()

    try:
        pipeline_result = run_full_clinical_pipeline(data, submission_id=submission_id)
    except Exception as exc:
        pipeline_result = {
            "status": "error",
            "submission_id": submission_id,
            "error": str(exc),
        }

    enriched_data = dict(data)
    enriched_data["clinical_pipeline"] = pipeline_result
    conn = get_db_connection()
    conn.execute(
        "UPDATE intake_forms SET form_data = ? WHERE id = ?",
        (json.dumps(enriched_data, ensure_ascii=False), submission_id),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "message": "Form submitted successfully and clinical workflow completed.",
        "submission_id": submission_id,
        "report_pdf": pipeline_result.get("report_pdf"),
        "pipeline": pipeline_result,
    })

@app.route("/rag/status")
def rag_status_route():
    """Return the current indexing/search status for the local RAG document store."""
    if not submissions_authorized():
        return password_required_response()

    return jsonify(rag_status())

@app.route("/rag/index", methods=["POST"])
def rag_index_route():
    """Index uploaded/reference files into the local RAG store."""
    if not submissions_authorized():
        return password_required_response()

    options = request.get_json(silent=True) or {}
    try:
        result = index_rag_files(
            force=bool(options.get("force")),
            limit=options.get("limit"),
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result)

@app.route("/rag/search", methods=["POST"])
def rag_search_route():
    """Search the local RAG store and return the most relevant source passages."""
    if not submissions_authorized():
        return password_required_response()

    data = request.get_json(silent=True) or {}
    query = str(data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    top_k = clamp_int(data.get("top_k"), 6, 1, 12)
    try:
        result = search_rag(query, top_k=top_k)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result)

@app.route("/rag/context", methods=["POST"])
def rag_context_route():
    """Build a combined clinical context block from the top RAG search results."""
    if not submissions_authorized():
        return password_required_response()

    data = request.get_json(silent=True) or {}
    query = str(data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    top_k = clamp_int(data.get("top_k"), 6, 1, 12)
    try:
        result = build_clinical_context(query, top_k=top_k)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result)


# Purpose: style the protected browser page used to test the clinical-agent endpoint.
CLINICAL_TEST_PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#f6f8fb;color:#172033;font-family:Arial,sans-serif}
main{max-width:980px;margin:auto;display:grid;gap:18px}
h1,h2,h3{margin:0 0 8px;color:#1f4e79}
h1{font-size:28px}
h2{font-size:21px}
h3{font-size:17px}
form,.result-panel,details{padding:18px;border:1px solid #d8e0ea;border-radius:8px;background:#fff}
label{display:grid;gap:6px;margin-bottom:14px;font-weight:700}
input,textarea{width:100%;padding:10px;border:1px solid #bac7d5;border-radius:6px;font:inherit}
textarea{min-height:84px;resize:vertical}
button{padding:10px 14px;border:0;border-radius:6px;background:#1f4e79;color:#fff;font:inherit;font-weight:700;cursor:pointer}
.result-panel{display:grid;gap:14px}
.section{padding-top:12px;border-top:1px solid #e3e9f0}
.section:first-child{padding-top:0;border-top:0}
ul{margin:8px 0 0 20px;padding:0}
li{margin-bottom:6px}
.flag,.ok{padding:10px;border-left:4px solid}
.flag{border-color:#b42318;background:#fff3f0}
.ok{border-color:#18794e;background:#eefaf3}
.muted{color:#5f6b7a}
.passage,pre{overflow:auto;white-space:pre-wrap}
.passage{max-height:220px;padding:10px;border:1px solid #e3e9f0;border-radius:6px;background:#fbfcfe}
pre{min-height:220px}
""".strip()


@app.route("/clinical-agent-test")
def clinical_agent_test_page():
    """Serve a protected browser test page for calling the clinical agent endpoint."""
    if not submissions_authorized():
        return password_required_response()

    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clinical Agent Test</title>
  <style>{CLINICAL_TEST_PAGE_CSS}</style>
</head>
<body>
  <main>
    <h1>Clinical Agent Test</h1>
    <form id="agent-form">
      <label>
        Query
        <textarea id="query">erectile dysfunction medication safety</textarea>
      </label>
      <label>
        Current medications
        <textarea id="current-medications">sildenafil; nitroglycerin</textarea>
      </label>
      <label>
        Medical history
        <textarea id="medical-history">ischemic heart disease</textarea>
      </label>
      <label>
        Top results
        <input id="top-k" type="number" min="1" max="12" value="1">
      </label>
      <button type="submit">Run Agent</button>
    </form>
    <section id="result" class="result-panel">
      <div class="muted">Ready.</div>
    </section>
    <details>
      <summary>Raw JSON</summary>
      <pre id="raw-json">No result yet.</pre>
    </details>
  </main>
  <script>
    const form = document.getElementById("agent-form");
    const result = document.getElementById("result");
    const rawJson = document.getElementById("raw-json");

    // Escapes dynamic values before inserting them into HTML.
    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    // Renders an array as list items, or shows fallback text when the array is empty.
    function listItems(items, fallback) {
      if (!items || !items.length) {
        return `<p class="muted">${escapeHtml(fallback)}</p>`;
      }
      return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
    }

    // Builds the readable result panel from the clinical-agent JSON response.
    function renderClinicalResult(payload) {
      const agent = payload.clinical_agent || {};
      const report = agent.report || {};
      const checks = payload.medication_checks || {};
      const rag = payload.rag || {};
      const sources = rag.sources || [];
      const flags = checks.label_flags || [];
      const openfda = checks.openfda || [];
      const notes = payload.notes || [];
      const flagHtml = flags.length
        ? flags.map((flag) => `
            <div class="flag">
              <strong>${escapeHtml(flag.drug || "Medication flag")}</strong><br>
              ${escapeHtml(flag.message || "")}
            </div>
          `).join("")
        : `<div class="ok">No label interaction text flags were found for the parsed medication list.</div>`;
      const labelHtml = openfda.length
        ? `<ul>${openfda.map((item) => `
            <li>
              <strong>${escapeHtml(item.query)}</strong>:
              ${item.found ? "openFDA label found" : escapeHtml(item.message || item.error || "No label found")}
            </li>
          `).join("")}</ul>`
        : `<p class="muted">No medication names were parsed.</p>`;
      const sourceHtml = sources.length
        ? `<ul>${sources.map((source) => `
            <li>${escapeHtml(source.citation)} <span class="muted">score ${escapeHtml(source.score)}</span></li>
          `).join("")}</ul>`
        : `<p class="muted">No RAG sources returned.</p>`;

      result.innerHTML = `
        <div class="section">
          <h2>CrewAI Clinical Agent Review</h2>
          <p>${escapeHtml(report.clinical_summary || payload.message || "Clinical agent response received.")}</p>
          <p class="muted">Engine: ${escapeHtml(agent.engine || "gemini")} | Model: ${escapeHtml(agent.model || "")} | Confidence: ${escapeHtml(report.confidence || "not stated")}</p>
          ${agent.error ? `<div class="flag">${escapeHtml(agent.error)}</div>` : ""}
        </div>
        <div class="section">
          <h3>Safety Flags</h3>
          ${flagHtml}
        </div>
        <div class="section">
          <h3>CrewAI Key Findings</h3>
          ${listItems(report.key_findings || [], "No key findings returned.")}
        </div>
        <div class="section">
          <h3>CrewAI Medication Safety</h3>
          ${listItems(report.medication_safety || [], "No medication-safety summary returned.")}
        </div>
        <div class="section">
          <h3>CrewAI Guideline Context</h3>
          ${listItems(report.guideline_context || [], "No guideline-context summary returned.")}
        </div>
        <div class="section">
          <h3>Red Flags</h3>
          ${listItems(report.red_flags || [], "No red flags returned.")}
        </div>
        <div class="section">
          <h3>Missing Information</h3>
          ${listItems(report.missing_information || [], "No missing-information list returned.")}
        </div>
        <div class="section">
          <h3>Recommended Follow-up Questions</h3>
          ${listItems(report.recommended_next_questions || [], "No follow-up questions returned.")}
        </div>
        <div class="section">
          <h3>Medications Checked</h3>
          ${listItems(checks.drug_candidates || [], "No medication names were parsed.")}
        </div>
        <div class="section">
          <h3>openFDA Labels</h3>
          ${labelHtml}
        </div>
        <div class="section">
          <h3>Guideline Sources</h3>
          ${sourceHtml}
        </div>
        <div class="section">
          <h3>Retrieved Context</h3>
          <div class="passage">${escapeHtml(rag.context || "No context returned.")}</div>
        </div>
        <div class="section">
          <h3>Notes</h3>
          ${listItems(notes, "No notes returned.")}
        </div>
      `;
    }

    // Sends the form values to the clinical-agent endpoint and renders the response.
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      result.innerHTML = '<div class="muted">Running...</div>';
      rawJson.textContent = "Running...";
      const body = {
        query: document.getElementById("query").value,
        current_medications: document.getElementById("current-medications").value,
        medical_history: document.getElementById("medical-history").value,
        top_k: Number(document.getElementById("top-k").value || 1)
      };
      try {
        const response = await fetch("/clinical-agent", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const payload = await response.json();
        rawJson.textContent = JSON.stringify(payload, null, 2);
        renderClinicalResult(payload);
      } catch (error) {
        result.innerHTML = `<div class="flag">${escapeHtml(error)}</div>`;
        rawJson.textContent = String(error);
      }
    });
  </script>
</body>
</html>
"""

@app.route("/clinical-agent", methods=["POST"])
def clinical_agent_route():
    """Build and return the clinical agent packet for the supplied patient/query data."""
    if not submissions_authorized():
        return password_required_response()

    data = request.get_json(silent=True) or {}
    try:
        result = clinical_agent_module.build_clinical_agent_response(
            data,
            clinical_agent_dependencies(),
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result)

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
