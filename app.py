from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import sqlite3
import json
import os
import hmac
import base64
import re
import uuid
from datetime import datetime
from html import escape
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "intake.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

def load_secret(name):
    value = os.environ.get(name)
    if value:
        return value.strip()

    secret_path = os.path.join(BASE_DIR, "APIkey")
    aliases = {
        "OPENFDA_API_KEY": {"OPENFDA_API_KEY", "OPEN_FDA_API_KEY", "OPENFDA", "FDA_API_KEY"},
        "OPENAI_API_KEY": {"OPENAI_API_KEY", "OPENAI"},
        "DRUGBANK_API_KEY": {"DRUGBANK_API_KEY", "DRUGBANK"},
    }

    try:
        with open(secret_path, "r", encoding="utf-8") as secret_file:
            lines = [
                line.strip()
                for line in secret_file
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        return None

    valid_names = aliases.get(name, {name})
    for line in lines:
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip().upper() in valid_names:
            return raw_value.strip().strip("\"'")

    if name == "OPENFDA_API_KEY" and len(lines) == 1 and "=" not in lines[0]:
        return lines[0].strip().strip("\"'")

    return None

SUBMISSIONS_PASSWORD = os.environ.get("SUBMISSIONS_PASSWORD", "Doctor")
OPENFDA_API_KEY = load_secret("OPENFDA_API_KEY")
OPENAI_API_KEY = load_secret("OPENAI_API_KEY")
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini")
DRUGBANK_API_KEY = load_secret("DRUGBANK_API_KEY")
DRUGBANK_REGION = os.environ.get("DRUGBANK_REGION", "us")

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}
ALLOWED_INVESTIGATION_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | {"pdf"}
OPENAI_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_UPLOAD_FILES = 12
MAX_LOOKUP_NAMES = 8

STOP_MEDICATION_WORDS = {
    "after", "before", "bid", "box", "cap", "capsule", "capsules", "daily",
    "bmp", "dose", "drug", "each", "every", "for", "gif", "image", "img", "injection",
    "jpeg", "jpg",
    "medicine", "medication", "medications", "morning", "night", "once",
    "oral", "pack", "pdf", "photo", "pill", "png", "prn", "qid", "scan",
    "tablet", "tablets", "the", "tid", "tif", "tiff", "twice", "webp", "with",
}

DOSE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|kg|ml|iu|units?|%|mmol|meq)\b",
    re.IGNORECASE,
)

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def safe_json_loads(value, fallback=None):
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
    auth = request.authorization
    return bool(auth and hmac.compare_digest(auth.password or "", SUBMISSIONS_PASSWORD))

def password_required_response():
    return Response(
        "Password required to view submitted forms.",
        401,
        {"WWW-Authenticate": 'Basic realm="Submitted Forms"'}
    )

def allowed_extension(filename, allowed_extensions):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in allowed_extensions

def save_uploaded_file(file_obj, category, allowed_extensions):
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

def request_json(url, *, method="GET", headers=None, body=None, timeout=12):
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=request_headers, method=method)
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def first_text(value, max_chars=700):
    if isinstance(value, list):
        text = " ".join(str(item) for item in value if item)
    else:
        text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text

def list_value(value):
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if value:
        return [str(value)]
    return []

def openfda_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

def summarize_openfda_label(record):
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

def extract_json_object(text):
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}

def output_text_from_openai_response(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    chunks = []
    for output_item in payload.get("output", []):
        for content in output_item.get("content", []):
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()

def extract_text_with_openai(saved_files):
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
    return extract_json_object(text) or {"observed_text": text}, None

def extract_text_with_tesseract(saved_files):
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

def init_db():
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
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/style.css")
def css():
    return send_from_directory(BASE_DIR, "style.css")

@app.route("/script.js")
def js():
    return send_from_directory(BASE_DIR, "script.js")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    if not submissions_authorized():
        return password_required_response()

    normalized = os.path.normpath(filename)
    if normalized.startswith("..") or os.path.isabs(normalized):
        return Response("Invalid upload path.", 400)

    directory = os.path.join(UPLOAD_DIR, os.path.dirname(normalized))
    return send_from_directory(directory, os.path.basename(normalized))

@app.route("/scan-drugs", methods=["POST"])
def scan_drugs():
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
    investigation_results = request.form.get("investigationResults", "")
    filename_text = "\n".join(file_info["original_name"] for file_info in saved_files)

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
    for name in extracted_names + parse_possible_drug_names(current_medications, extracted_text, filename_text):
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

@app.route("/submissions")
def submissions():
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

            answers = "\n".join(
                f"<tr><th>{escape(str(key))}</th><td>{format_answer(value)}</td></tr>"
                for key, value in form_data.items()
            )

            cards.append(f"""
              <article class="submission">
                <h2>Submission #{row["id"]}</h2>
                <div class="summary">
                  <span><strong>Name:</strong> {escape(str(row["full_name"] or ""))}</span>
                  <span><strong>Age:</strong> {escape(str(row["age"] or ""))}</span>
                  <span><strong>Mobile:</strong> {escape(str(row["mobile"] or ""))}</span>
                  <span><strong>Email:</strong> {escape(str(row["email"] or ""))}</span>
                </div>
                <table>
                  <tbody>{answers}</tbody>
                </table>
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
  <style>
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 30px;
      background: #f4f7fb;
      color: #172033;
      font-family: Arial, sans-serif;
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
    }}
    h1 {{
      color: #1f4e79;
      margin: 0 0 22px;
    }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }}
    a {{
      color: #1f4e79;
      font-weight: bold;
      text-decoration: none;
    }}
    .submission {{
      background: #fff;
      border-radius: 8px;
      margin-bottom: 22px;
      padding: 22px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
    }}
    .submission h2 {{
      margin: 0 0 14px;
      color: #1f4e79;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    th, td {{
      border: 1px solid #d4dce7;
      padding: 9px;
      text-align: left;
      vertical-align: top;
      word-break: break-word;
    }}
    th {{
      width: 260px;
      background: #eef4fb;
      color: #1f4e79;
    }}
    .empty {{
      background: #fff;
      border-radius: 8px;
      padding: 22px;
    }}
    @media (max-width: 760px) {{
      body {{
        padding: 12px;
      }}
      h1 {{
        font-size: 24px;
        margin-bottom: 0;
      }}
      .toolbar {{
        align-items: flex-start;
      }}
      .submission {{
        padding: 16px;
      }}
      .summary {{
        grid-template-columns: 1fr;
      }}
      table {{
        display: block;
        overflow-x: auto;
        table-layout: auto;
      }}
      th, td {{
        min-width: 180px;
        word-break: normal;
      }}
      th {{
        width: auto;
      }}
    }}
    @media (max-width: 420px) {{
      body {{
        padding: 8px;
      }}
      .submission,
      .empty {{
        padding: 14px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="toolbar">
      <h1>Submitted Forms</h1>
      <a href="/">Back to form</a>
    </div>
    {submissions_html}
  </main>
</body>
</html>
"""

@app.route("/submit", methods=["POST"])
def submit_form():
    data = request.json

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

    conn.commit()
    conn.close()

    return jsonify({"message": "Form submitted successfully"})

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)