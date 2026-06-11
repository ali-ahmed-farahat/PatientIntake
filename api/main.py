from flask import Flask, request, jsonify, render_template, send_from_directory, Response
from flask_cors import CORS
import json
import os
import queue
import threading
import time

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
    generate_next_patient_code,
    get_db_connection,
    get_patient_by_code,
    init_db,
    build_ai_summary_points,
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
_notification_lock = threading.Lock()
_notification_listeners = []

def _broadcast_notification(payload):
    data = json.dumps(payload, ensure_ascii=False)
    with _notification_lock:
        dead = []
        for q in _notification_listeners:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _notification_listeners.remove(q)
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

@app.route("/notifications.js")
def notifications_js():
    return send_from_directory(FRONTEND_DIR, "notifications.js")

@app.route("/notifications.css")  
def notifications_css():
    return send_from_directory(FRONTEND_DIR, "notifications.css")

@app.route("/submissions.css")
def submissions_css():
    """Serve the submissions page stylesheet."""
    return send_from_directory(FRONTEND_DIR, "submissions.css")

@app.route("/submissions.js")
def submissions_js():
    """Serve the submissions page JavaScript file."""
    return send_from_directory(FRONTEND_DIR, "submissions.js")

@app.route("/pedt")
def pedt_page():
    return send_from_directory(FRONTEND_DIR, "pedt.html")

@app.route("/pedt.css")
def pedt_css():
    return send_from_directory(FRONTEND_DIR, "pedt.css")

@app.route("/pedt.js")
def pedt_js():
    return send_from_directory(FRONTEND_DIR, "pedt.js")

@app.route("/ehs")
def ehs_page():
    """Serve the Erection Hardness Scale page."""
    return send_from_directory(FRONTEND_DIR, "ehs.html")

@app.route("/ehs.css")
def ehs_css():
    """Serve the Erection Hardness Scale stylesheet."""
    return send_from_directory(FRONTEND_DIR, "ehs.css")

@app.route("/ehs.js")
def ehs_js():
    """Serve the Erection Hardness Scale JavaScript logic."""
    return send_from_directory(FRONTEND_DIR, "ehs.js")

@app.route("/low-libido")
def low_libido_page():
    """Serve the Low Libido Questionnaire page."""
    return send_from_directory(FRONTEND_DIR, "low-libido.html")

@app.route("/low-libido.css")
def low_libido_css():
    """Serve the Low Libido Questionnaire stylesheet."""
    return send_from_directory(FRONTEND_DIR, "low-libido.css")

@app.route("/low-libido.js")
def low_libido_js():
    """Serve the Low Libido Questionnaire JavaScript logic."""
    return send_from_directory(FRONTEND_DIR, "low-libido.js")

@app.route("/iief")
def iief_page():
    """Serve the IIEF Questionnaire page."""
    return send_from_directory(FRONTEND_DIR, "iief.html")

@app.route("/iief.css")
def iief_css():
    """Serve the IIEF Questionnaire stylesheet."""
    return send_from_directory(FRONTEND_DIR, "iief.css")

@app.route("/iief.js")
def iief_js():
    """Serve the IIEF Questionnaire JavaScript logic."""
    return send_from_directory(FRONTEND_DIR, "iief.js")

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

@app.route("/patient-code/next")
def generate_patient_code():
    """Generate and return the next available patient code."""
    code_no = generate_next_patient_code()
    return jsonify({
        "codeNo": code_no,
        "message": f"Generated new patient code: {code_no}",
    })

@app.route("/patient-code/<code>")
def lookup_patient_code(code):
    """Look up an existing patient by their code."""
    patient = get_patient_by_code(code)
    
    if not patient:
        return jsonify({
            "found": False,
            "error": f"Patient code '{code}' not found.",
        }), 404
    
    return jsonify({
        "found": True,
        "codeNo": patient["codeNo"],
        "full_name": patient["full_name"],
        "age": patient["age"],
        "mobile": patient["mobile"],
        "email": patient["email"],
        "form_data": patient["form_data"],
    })

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
        iief_data = form_data.pop("iief_data", None)
        pedt_data = form_data.pop("pedt_data", None)
        ehs_data = form_data.pop("ehs_data", None)
        low_libido_data = form_data.pop("low_libido_data", None)
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
            "ai_summary_panel_id": f"ai-summary-panel-{submission_id}",
            "iief_panel_id": f"iief-panel-{submission_id}",
            "pedt_panel_id": f"pedt-panel-{submission_id}",
            "ehs_panel_id": f"ehs-panel-{submission_id}",
            "low_libido_panel_id": f"low-libido-panel-{submission_id}",
            "report_pdf_url": report_pdf.get("url"),
            "report_pdf_error": report_pdf.get("error"),
            "ai_summary_points": build_ai_summary_points(pipeline),
            "iief_data": iief_data,
            "pedt_data": pedt_data,
            "ehs_data": ehs_data,
            "low_libido_data": low_libido_data,
            "answers": [
                {"key": str(key), "value": format_answer(value)}
                for key, value in form_data.items()
                if key not in ("clinical_pipeline", "iief_data", "pedt_data", "ehs_data", "low_libido_data")
            ],
            "ai_html": render_ai_report(pipeline),
        })

    return render_template("submissions.html", submissions=submissions)

@app.route("/submit", methods=["POST"])
def submit_form():
    """Save a submitted intake form, then run the configured clinical agent workflow."""
    data = request.json or {}
    initial_payload = dict(data)
    
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
        json.dumps(initial_payload, ensure_ascii=False)
    ))

    submission_id = cur.lastrowid
    initial_payload["clinical_pipeline"] = {
        "status": "running",
        "submission_id": submission_id,
        "stopped_after": None,
        "message": "Clinical workflow is running in the background.",
    }
    cur.execute(
        "UPDATE intake_forms SET form_data = ? WHERE id = ?",
        (json.dumps(initial_payload, ensure_ascii=False), submission_id),
    )
    conn.commit()
    conn.close()

    # Spawn background thread to run clinical pipeline
    def run_pipeline_bg(data_copy, sub_id):
        print(f"[pipeline] submission #{sub_id} started")
        try:
            pipeline_result = run_full_clinical_pipeline(data_copy, submission_id=sub_id)
        except Exception as exc:
            pipeline_result = {
                "status": "error",
                "submission_id": sub_id,
                "error": str(exc),
            }
            print(f"[pipeline] submission #{sub_id} failed: {exc}")
        else:
            print(
                f"[pipeline] submission #{sub_id} finished: "
                f"status={pipeline_result.get('status')} "
                f"stopped_after={pipeline_result.get('stopped_after')}"
            )

        conn_bg = get_db_connection()
        row = conn_bg.execute("SELECT form_data FROM intake_forms WHERE id = ?", (sub_id,)).fetchone()
        if row:
            try:
                current_data = json.loads(row["form_data"] or "{}")
            except json.JSONDecodeError:
                current_data = dict(data_copy)
        else:
            current_data = dict(data_copy)

        current_data["clinical_pipeline"] = pipeline_result
        conn_bg.execute(
            "UPDATE intake_forms SET form_data = ? WHERE id = ?",
            (json.dumps(current_data, ensure_ascii=False), sub_id),
        )
        conn_bg.commit()
        conn_bg.close()

        _broadcast_notification({
            "type": "pipeline_completed",
            "submission_id": sub_id,
            "status": pipeline_result.get("status"),
            "stopped_after": pipeline_result.get("stopped_after"),
            "timestamp": time.strftime("%H:%M"),
        })

    threading.Thread(target=run_pipeline_bg, args=(dict(data), submission_id), daemon=False).start()

    _broadcast_notification({
        "submission_id": submission_id,
        "full_name": data.get("fullName") or "Unknown patient",
        "visit_type": data.get("visitType") or "",
        "age": str(data.get("age") or ""),
        "timestamp": time.strftime("%H:%M"),
    })

    return jsonify({
        "message": "Form submitted successfully. Clinical workflow is running in the background.",
        "submission_id": submission_id,
        "codeNo": f"INT-{submission_id}",
        "deployment": deployment_info(),
    })


@app.route("/submit-iief", methods=["POST"])
def submit_iief():
    """Merge the IIEF scores and answers into the patient's submission record."""
    data = request.json or {}
    submission_id = data.get("submission_id")
    iief_data = data.get("iief_data")

    if not submission_id:
        return jsonify({"error": "submission_id is required"}), 400
    if not iief_data:
        return jsonify({"error": "iief_data is required"}), 400

    try:
        # Extract integer ID from patient code if passed as a string (e.g. INT-123 -> 123)
        if isinstance(submission_id, str) and submission_id.startswith("INT-"):
            submission_id = int(submission_id.split("-")[1])
        else:
            submission_id = int(submission_id)
    except (ValueError, IndexError):
        return jsonify({"error": "Invalid submission_id format"}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT form_data FROM intake_forms WHERE id = ?", (submission_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": f"Submission #{submission_id} not found"}), 404

    try:
        form_data = json.loads(row["form_data"] or "{}")
    except json.JSONDecodeError:
        form_data = {}

    # Merge IIEF data
    form_data["iief_data"] = iief_data

    conn.execute(
        "UPDATE intake_forms SET form_data = ? WHERE id = ?",
        (json.dumps(form_data, ensure_ascii=False), submission_id),
    )
    conn.commit()
    conn.close()

    # Broadcast a notification to refresh submissions in the dashboard
    _broadcast_notification({
        "submission_id": submission_id,
        "type": "iief_submitted",
        "timestamp": time.strftime("%H:%M"),
    })

    return jsonify({
        "message": "IIEF Questionnaire answers submitted successfully.",
        "submission_id": submission_id,
    })


@app.route("/submit-pedt", methods=["POST"])
def submit_pedt():
    """Merge the PEDT scores and answers into the patient's submission record."""
    data = request.json or {}
    submission_id = data.get("submission_id")
    pedt_data = data.get("pedt_data")

    if not submission_id:
        return jsonify({"error": "submission_id is required"}), 400
    if not pedt_data:
        return jsonify({"error": "pedt_data is required"}), 400

    try:
        # Extract integer ID from patient code if passed as a string (e.g. INT-123 -> 123)
        if isinstance(submission_id, str) and submission_id.startswith("INT-"):
            submission_id = int(submission_id.split("-")[1])
        else:
            submission_id = int(submission_id)
    except (ValueError, IndexError):
        return jsonify({"error": "Invalid submission_id format"}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT form_data FROM intake_forms WHERE id = ?", (submission_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": f"Submission #{submission_id} not found"}), 404

    try:
        form_data = json.loads(row["form_data"] or "{}")
    except json.JSONDecodeError:
        form_data = {}

    # Merge PEDT data
    form_data["pedt_data"] = pedt_data

    conn.execute(
        "UPDATE intake_forms SET form_data = ? WHERE id = ?",
        (json.dumps(form_data, ensure_ascii=False), submission_id),
    )
    conn.commit()
    conn.close()

    # Broadcast a notification to refresh submissions in the dashboard
    _broadcast_notification({
        "submission_id": submission_id,
        "type": "pedt_submitted",
        "timestamp": time.strftime("%H:%M"),
    })

    return jsonify({
        "message": "PEDT Questionnaire answers submitted successfully.",
        "submission_id": submission_id,
    })


@app.route("/submit-ehs", methods=["POST"])
def submit_ehs():
    """Merge the Erection Hardness Scale answers into the patient's submission record."""
    data = request.json or {}
    submission_id = data.get("submission_id")
    ehs_data = data.get("ehs_data")

    if not submission_id:
        return jsonify({"error": "submission_id is required"}), 400
    if not ehs_data:
        return jsonify({"error": "ehs_data is required"}), 400

    try:
        if isinstance(submission_id, str) and submission_id.startswith("INT-"):
            submission_id = int(submission_id.split("-")[1])
        else:
            submission_id = int(submission_id)
    except (ValueError, IndexError):
        return jsonify({"error": "Invalid submission_id format"}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT form_data FROM intake_forms WHERE id = ?", (submission_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": f"Submission #{submission_id} not found"}), 404

    try:
        form_data = json.loads(row["form_data"] or "{}")
    except json.JSONDecodeError:
        form_data = {}

    form_data["ehs_data"] = ehs_data

    conn.execute(
        "UPDATE intake_forms SET form_data = ? WHERE id = ?",
        (json.dumps(form_data, ensure_ascii=False), submission_id),
    )
    conn.commit()
    conn.close()

    _broadcast_notification({
        "submission_id": submission_id,
        "type": "ehs_submitted",
        "timestamp": time.strftime("%H:%M"),
    })

    return jsonify({
        "message": "Erection Hardness Scale answers submitted successfully.",
        "submission_id": submission_id,
    })


@app.route("/submit-low-libido", methods=["POST"])
def submit_low_libido():
    """Merge the Low Libido questionnaire scores into the patient's submission record."""
    data = request.json or {}
    submission_id = data.get("submission_id")
    low_libido_data = data.get("low_libido_data")

    if not submission_id:
        return jsonify({"error": "submission_id is required"}), 400
    if not low_libido_data:
        return jsonify({"error": "low_libido_data is required"}), 400

    try:
        if isinstance(submission_id, str) and submission_id.startswith("INT-"):
            submission_id = int(submission_id.split("-")[1])
        else:
            submission_id = int(submission_id)
    except (ValueError, IndexError):
        return jsonify({"error": "Invalid submission_id format"}), 400

    conn = get_db_connection()
    row = conn.execute("SELECT form_data FROM intake_forms WHERE id = ?", (submission_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": f"Submission #{submission_id} not found"}), 404

    try:
        form_data = json.loads(row["form_data"] or "{}")
    except json.JSONDecodeError:
        form_data = {}

    form_data["low_libido_data"] = low_libido_data

    conn.execute(
        "UPDATE intake_forms SET form_data = ? WHERE id = ?",
        (json.dumps(form_data, ensure_ascii=False), submission_id),
    )
    conn.commit()
    conn.close()

    _broadcast_notification({
        "submission_id": submission_id,
        "type": "low_libido_submitted",
        "timestamp": time.strftime("%H:%M"),
    })

    return jsonify({
        "message": "Low Libido questionnaire answers submitted successfully.",
        "submission_id": submission_id,
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

@app.route("/events")
def sse_events():
    """
    Server-Sent Events stream for real-time doctor notifications.
    The doctor's page connects once; we push a 'new_submission' event
    each time a patient completes and submits the intake form.
    """
    if not submissions_authorized():
        return password_required_response()
 
    def stream():
        q: queue.Queue = queue.Queue(maxsize=20)
        with _notification_lock:
            _notification_listeners.append(q)
        try:
            # Initial ping confirms the connection is live.
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"event: new_submission\ndata: {data}\n\n"
                except queue.Empty:
                    # Keepalive comment — prevents proxies from killing idle connections.
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _notification_lock:
                try:
                    _notification_listeners.remove(q)
                except ValueError:
                    pass
 
    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
 
 

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
    debug_mode = os.environ.get("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host="127.0.0.1", port=5000, debug=debug_mode, use_reloader=False)
