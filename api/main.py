from flask import Flask, request, jsonify, render_template, send_from_directory, Response
from flask_cors import CORS
import json
import os

from api.utils import (
    ALLOWED_IMAGE_EXTENSIONS,
    ALLOWED_INVESTIGATION_EXTENSIONS,
    BASE_DIR,
    FRONTEND_DIR,
    MAX_UPLOAD_FILES,
    UPLOAD_DIR,
    build_clinical_context,
    build_label_flags,
    clamp_int,
    clinical_agent_dependencies,
    clinical_agent_module,
    deployment_info,
    extract_text_with_openai,
    extract_text_with_tesseract,
    format_answer,
    get_db_connection,
    init_db,
    lookup_drugbank,
    lookup_openfda_label,
    parse_possible_drug_names,
    password_required_response,
    render_ai_report,
    run_full_clinical_pipeline,
    save_uploaded_file,
    submissions_authorized,
)
from core.rag_store import index_rag_files, rag_status, search_rag

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "frontend"))
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

@app.route("/")
def website():
    """Serve the main intake form page."""
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/style.css")
def css():
    """Serve the frontend stylesheet."""
    return send_from_directory(FRONTEND_DIR, "style.css")

@app.route("/script.js")
def js():
    """Serve the frontend JavaScript file."""
    return send_from_directory(FRONTEND_DIR, "script.js")

@app.route("/submissions.css")
def submissions_css():
    """Serve the submissions page stylesheet."""
    return send_from_directory(FRONTEND_DIR, "submissions.css")

@app.route("/submissions.js")
def submissions_js():
    """Serve the submissions page JavaScript file."""
    return send_from_directory(FRONTEND_DIR, "submissions.js")

@app.route("/clinical-agent-test.css")
def clinical_agent_test_css():
    """Serve the clinical agent test page stylesheet."""
    return send_from_directory(FRONTEND_DIR, "clinical-agent-test.css")

@app.route("/clinical-agent-test.js")
def clinical_agent_test_js():
    """Serve the clinical agent test page JavaScript file."""
    return send_from_directory(FRONTEND_DIR, "clinical-agent-test.js")

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
        "deployment": deployment_info(),
    })


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

    submissions = []
    for row in rows:
        try:
            form_data = json.loads(row["form_data"] or "{}")
        except json.JSONDecodeError:
            form_data = {}

        pipeline = form_data.pop("clinical_pipeline", None)
        report_pdf = (pipeline or {}).get("report_pdf") or {}
        submission_id = row["id"]
        submissions.append({
            "id": submission_id,
            "full_name": row["full_name"] or "",
            "age": row["age"] or "",
            "mobile": row["mobile"] or "",
            "email": row["email"] or "",
            "form_panel_id": f"form-panel-{submission_id}",
            "ai_panel_id": f"ai-panel-{submission_id}",
            "report_pdf_url": report_pdf.get("url"),
            "report_pdf_error": report_pdf.get("error"),
            "answers": [
                {"key": str(key), "value": format_answer(value)}
                for key, value in form_data.items()
                if key != "clinical_pipeline"
            ],
            "ai_html": render_ai_report(pipeline),
        })

    return render_template("submissions.html", submissions=submissions)

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
        "deployment": deployment_info(),
    })

@app.route("/rag/status")
def rag_status_route():
    """Return the current indexing/search status for the local RAG document store."""
    if not submissions_authorized():
        return password_required_response()

    status = rag_status()
    status["deployment"] = deployment_info()
    return jsonify(status)

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

    result["deployment"] = deployment_info()
    if STORAGE_IS_EPHEMERAL:
        result["warning"] = "RAG indexes created on Vercel are stored in /tmp and can disappear between function instances."
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

    result["deployment"] = deployment_info()
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

    result["deployment"] = deployment_info()
    return jsonify(result)



@app.route("/clinical-agent-test")
def clinical_agent_test_page():
    """Serve a protected browser test page for calling the clinical agent endpoint."""
    if not submissions_authorized():
        return password_required_response()

    return render_template("clinical-agent-test.html")

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

    result["deployment"] = deployment_info()
    return jsonify(result)

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
