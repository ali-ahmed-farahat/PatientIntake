import json
import os
import re
import textwrap
from datetime import datetime

from agent_utils import compact_text
from crewai_agent_tools import run_crewai_json_agent


GEMINI_REPORT_MODEL = "gemini-2.5-flash"


REPORT_SYSTEM_PROMPT = """
You are a final report agent.

You receive the complete pipeline output from multiple CrewAI agents and deterministic tools.
Your task is to produce one structured, clinician-facing final report.

Return ONLY a valid JSON object. No markdown and no text outside JSON.

Response format:
{
  "report_title": "short title",
  "report_type": "lifestyle_triage or full_clinical_evidence_review",
  "patient_snapshot": {
    "submission_id": "id if supplied",
    "age": "age if supplied",
    "sex": "sex or gender if supplied",
    "presenting_question": "short question or complaint"
  },
  "clinical_summary": "short clinician-facing clinical summary",
  "executive_summary": "short summary of the whole case",
  "findings": ["important final report findings"],
  "urgent_safety_alerts": ["urgent medication, clinical, or uncertainty alerts"],
  "clinical_findings": ["key clinical findings from the pipeline"],
  "medication_safety": ["medication safety findings"],
  "evidence_summary": ["evidence quality and source-supported points"],
  "clinician_actions": ["specific items for clinician review"],
  "missing_information": ["missing data needed before decisions"],
  "citations": ["citations supplied by earlier agents only"],
  "source_citations": ["citations supplied by earlier agents only"],
  "confidence": "high or moderate or low",
  "limitations": ["limits of the report"],
  "structured_sections": [
    {
      "heading": "section heading",
      "items": ["plain text item"]
    }
  ]
}

Rules:
- Do not diagnose.
- Do not prescribe.
- Do not invent facts, citations, PMIDs, medication facts, guideline statements, or lab values.
- Base the report only on the supplied pipeline output.
- Preserve urgent safety alerts prominently.
- Include citation quality concerns when the evidence reviewer flagged them.
- Keep the output structured and concise enough for a PDF report.
- Do not use Markdown, bold markers, bullets, headings, or asterisks inside string values. Use plain text only.
""".strip()


ARABIC_PDF_SYSTEM_PROMPT = """
You are an Arabic medical report translator.

You receive a structured clinical report JSON. Translate the report content into clear Modern Standard Arabic for the PDF only.

Return ONLY a valid JSON object. No markdown and no text outside JSON.

Response format:
{
  "report_title": "Arabic report title",
  "patient_snapshot": {
    "submission_id": "id if supplied",
    "age": "age if supplied",
    "sex": "sex or gender if supplied"
  },
  "clinical_summary": "Arabic clinical summary",
  "findings": ["Arabic finding"],
  "urgent_safety_alerts": ["Arabic urgent alert"],
  "medication_safety": ["Arabic medication safety point"],
  "evidence_summary": ["Arabic evidence summary point"],
  "clinician_actions": ["Arabic clinician action"],
  "missing_information": ["Arabic missing information item"],
  "citations": ["Keep citations, file names, page numbers, PMIDs, medication names, units, and lab values readable"],
  "limitations": ["Arabic limitation"]
}

Rules:
- Translate only the report prose into Arabic.
- Keep medication names, lab values, units, file names, page numbers, guideline names, and PMIDs unchanged when needed.
- Do not add new facts, citations, PMIDs, medication facts, guideline statements, diagnoses, or prescriptions.
- Base the Arabic report only on the supplied structured report.
- Use concise clinician-facing Arabic.
- Do not use Markdown, bullets, asterisks, or headings inside string values. Use plain text only.
""".strip()


def _compact_json(value, max_chars=9000):
    """Return compact JSON text for report-agent context."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value or "")
    return compact_text(text, max_chars)


def build_report_packet(pipeline_result):
    """Extract the pipeline fields that the final report agent needs."""
    pipeline_result = pipeline_result or {}
    lifestyle = pipeline_result.get("lifestyle_agent") or {}
    clinical = pipeline_result.get("clinical_agent") or {}
    research = pipeline_result.get("research_agent") or {}
    evidence = pipeline_result.get("evidence_reviewer_agent") or {}

    return {
        "submission_id": pipeline_result.get("submission_id"),
        "status": pipeline_result.get("status"),
        "stopped_after": pipeline_result.get("stopped_after"),
        "lifestyle_agent": lifestyle,
        "clinical_agent": {
            "input": clinical.get("input", {}),
            "clinical_report": (clinical.get("clinical_agent") or {}).get("report", {}),
            "medication_checks": clinical.get("medication_checks", {}),
            "rag_sources": (clinical.get("rag") or {}).get("sources", []),
            "notes": clinical.get("notes", []),
        },
        "research_agent": {
            "pubmed_query": research.get("pubmed_query"),
            "pubmed_error": research.get("pubmed_error"),
            "pubmed_papers": research.get("pubmed_papers", []),
            "report": research.get("report", {}),
        },
        "evidence_reviewer_agent": {
            "report": evidence.get("report", {}),
            "error": evidence.get("error"),
        },
        "existing_final_report": pipeline_result.get("final_report"),
    }


def call_report_agent(report_packet, *, api_key, model_name=GEMINI_REPORT_MODEL, timeout=60):
    """Run the CrewAI final report agent and parse the structured JSON report."""
    return run_crewai_json_agent(
        role="Final Structured Report Agent",
        goal="Create a structured final report from the completed pipeline output.",
        backstory=REPORT_SYSTEM_PROMPT,
        task_prompt=(
            "Create the final structured JSON report from this pipeline output.\n\n"
            f"{json.dumps(report_packet, ensure_ascii=False, indent=2)}"
        ),
        expected_output="A valid JSON object matching the requested final report response format.",
        api_key=api_key,
        model_name=model_name,
        max_tokens=8192,
        timeout=timeout,
        label="CrewAI report agent",
    )


def _as_list(value):
    """Normalize report fields into lists of strings."""
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def _collect_citations(*reports):
    """Collect unique citations from earlier structured reports."""
    citations = []
    seen = set()
    for report in reports:
        for citation in _as_list((report or {}).get("citations") or (report or {}).get("source_citations")):
            key = citation.lower()
            if key in seen:
                continue
            seen.add(key)
            citations.append(citation)
    return citations


def build_fallback_report(pipeline_result, error=None):
    """Build a structured report when the CrewAI report agent is unavailable."""
    pipeline_result = pipeline_result or {}
    lifestyle = pipeline_result.get("lifestyle_agent") or {}
    clinical_outer = pipeline_result.get("clinical_agent") or {}
    clinical_report = (clinical_outer.get("clinical_agent") or {}).get("report", {})
    research_report = (pipeline_result.get("research_agent") or {}).get("report", {})
    evidence_report = (pipeline_result.get("evidence_reviewer_agent") or {}).get("report", {})
    inputs = clinical_outer.get("input", {})

    safety_alerts = []
    safety_alerts.extend(_as_list(lifestyle.get("flags")))
    safety_alerts.extend(_as_list(clinical_report.get("red_flags")))
    safety_alerts.extend(_as_list(evidence_report.get("clinician_review_priorities"))[:3])

    report_type = "full_clinical_evidence_review" if clinical_report or research_report else "lifestyle_triage"
    limitations = []
    limitations.extend(_as_list(clinical_report.get("limitations")))
    limitations.extend(_as_list(research_report.get("limitations")))
    limitations.extend(_as_list(evidence_report.get("limitations")))
    if error:
        limitations.append(f"Report agent unavailable: {error}")

    return {
        "report_title": "AI Clinical Evidence Report",
        "report_type": report_type,
        "patient_snapshot": {
            "submission_id": str(pipeline_result.get("submission_id") or inputs.get("submission_id") or ""),
            "age": str(inputs.get("age") or ""),
            "sex": str(inputs.get("sex") or inputs.get("gender") or ""),
            "presenting_question": inputs.get("query") or lifestyle.get("reasoning") or "",
        },
        "executive_summary": (
            clinical_report.get("clinical_summary")
            or research_report.get("research_summary")
            or lifestyle.get("reasoning")
            or "Structured report generated from available pipeline output."
        ),
        "clinical_summary": clinical_report.get("clinical_summary") or lifestyle.get("reasoning") or "",
        "findings": _as_list(clinical_report.get("key_findings")) or _as_list(research_report.get("evidence_points")),
        "urgent_safety_alerts": safety_alerts,
        "clinical_findings": _as_list(clinical_report.get("key_findings")),
        "medication_safety": _as_list(clinical_report.get("medication_safety")),
        "evidence_summary": _as_list(research_report.get("evidence_points")),
        "clinician_actions": _as_list(research_report.get("suggested_clinician_review")) or _as_list(evidence_report.get("clinician_review_priorities")),
        "missing_information": _as_list(clinical_report.get("missing_information")) or _as_list(evidence_report.get("missing_evidence")),
        "citations": _collect_citations(clinical_report, research_report),
        "source_citations": _collect_citations(clinical_report, research_report),
        "confidence": evidence_report.get("overall_evidence_quality") or research_report.get("confidence") or clinical_report.get("confidence") or lifestyle.get("confidence") or "low",
        "limitations": limitations,
        "structured_sections": [
            {"heading": "Urgent Safety Alerts", "items": safety_alerts},
            {"heading": "Clinical Findings", "items": _as_list(clinical_report.get("key_findings"))},
            {"heading": "Evidence Summary", "items": _as_list(research_report.get("evidence_points"))},
            {"heading": "Clinician Actions", "items": _as_list(research_report.get("suggested_clinician_review"))},
            {"heading": "Missing Information", "items": _as_list(clinical_report.get("missing_information"))},
        ],
    }


def run_report_agent(pipeline_result, *, api_key, model_name=GEMINI_REPORT_MODEL):
    """Return a structured final report generated by the CrewAI report agent."""
    report_packet = build_report_packet(pipeline_result)
    try:
        report = call_report_agent(
            report_packet,
            api_key=api_key,
            model_name=model_name,
        )
        llm_error = None
    except RuntimeError as exc:
        report = build_fallback_report(pipeline_result, error=str(exc))
        llm_error = str(exc)

    return {
        "engine": "crewai",
        "model": model_name,
        "report_packet": report_packet,
        "report": report,
        "error": llm_error,
    }


def call_arabic_pdf_report(report, *, api_key, model_name=GEMINI_REPORT_MODEL, timeout=60):
    """Run the CrewAI Arabic PDF translator and parse the structured Arabic report."""
    return run_crewai_json_agent(
        role="Arabic PDF Report Translator",
        goal="Translate the final structured clinical report into Arabic for PDF generation only.",
        backstory=ARABIC_PDF_SYSTEM_PROMPT,
        task_prompt=(
            "Translate this structured report into the requested Arabic JSON shape for the PDF only.\n\n"
            f"{json.dumps(report or {}, ensure_ascii=False, indent=2)}"
        ),
        expected_output="A valid JSON object matching the requested Arabic PDF report response format.",
        api_key=api_key,
        model_name=model_name,
        max_tokens=8192,
        timeout=timeout,
        label="CrewAI Arabic PDF translator",
    )


def build_fallback_arabic_pdf_report(report, error=None):
    """Build an Arabic-labeled PDF report if translation is unavailable."""
    report = report or {}
    limitations = []
    if error:
        limitations.append(f"تعذر توليد الترجمة العربية آلياً: {error}")
    return {
        "report_title": "تقرير المراجعة السريرية",
        "patient_snapshot": report.get("patient_snapshot") or {},
        "clinical_summary": "تعذرت ترجمة التقرير السريري إلى العربية. يرجى مراجعة التقرير المنظم في صفحة النظام أو إعادة توليد التقرير بعد توفر خدمة الترجمة.",
        "findings": [],
        "urgent_safety_alerts": [],
        "medication_safety": [],
        "evidence_summary": [],
        "clinician_actions": [],
        "missing_information": [],
        "citations": _as_list(report.get("citations") or report.get("source_citations")),
        "limitations": limitations,
    }


def _contains_arabic(value):
    """Return True when a nested report value contains Arabic characters."""
    if isinstance(value, dict):
        return any(_contains_arabic(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_arabic(item) for item in value)
    return bool(re.search(r"[\u0600-\u06FF]", str(value or "")))


def build_arabic_pdf_report(report, *, api_key, model_name=GEMINI_REPORT_MODEL):
    """Return an Arabic report object for PDF generation only."""
    try:
        arabic_report = call_arabic_pdf_report(report, api_key=api_key, model_name=model_name)
        if not _contains_arabic(arabic_report):
            raise RuntimeError("Arabic translator did not return Arabic content.")
        return arabic_report, None
    except RuntimeError as exc:
        return build_fallback_arabic_pdf_report(report, error=str(exc)), str(exc)


def _pdf_escape(text):
    """Escape a text string for a simple PDF content stream."""
    text = str(text or "")
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text.encode("cp1252", "replace").decode("cp1252")


def _clean_pdf_text(text):
    """Normalize whitespace and characters unsupported by the built-in PDF font."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.encode("cp1252", "replace").decode("cp1252")


def _clean_unicode_text(text):
    """Normalize whitespace while preserving Unicode text for Arabic PDFs."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _add_wrapped_rows(rows, style, text, *, width=88, prefix=""):
    """Append wrapped styled rows while preserving indentation for continuation lines."""
    cleaned = _clean_pdf_text(text)
    if not cleaned:
        return
    initial_indent = prefix
    subsequent_indent = " " * len(prefix)
    wrapped = textwrap.wrap(
        cleaned,
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
    ) or [prefix]
    rows.extend({"style": style, "text": line} for line in wrapped)


def _report_lines(report):
    """Flatten structured report JSON into styled rows for the PDF writer."""
    rows = []
    title_parts = textwrap.wrap(
        _clean_pdf_text(report.get("report_title") or "AI Clinical Evidence Report"),
        width=58,
    ) or ["AI Clinical Evidence Report"]
    rows.append({"style": "title", "text": title_parts[0]})
    rows.extend({"style": "title_cont", "text": part} for part in title_parts[1:])
    rows.extend([
        {"style": "meta", "text": f"Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z"},
        {"style": "spacer", "text": ""},
    ])
    snapshot = report.get("patient_snapshot") or {}
    snapshot_parts = [
        f"Submission ID: {snapshot.get('submission_id', '')}",
        f"Age: {snapshot.get('age', '')}",
        f"Sex: {snapshot.get('sex', '')}",
    ]
    rows.append({"style": "section", "text": "Patient Snapshot"})
    for part in snapshot_parts:
        _add_wrapped_rows(rows, "body", part, width=84)
    rows.append({"style": "spacer", "text": ""})

    section_map = [
        ("Clinical Summary", [report.get("clinical_summary", "")]),
        ("Findings", report.get("findings", [])),
        ("Urgent Safety Alerts", report.get("urgent_safety_alerts", [])),
        ("Clinical Findings", report.get("clinical_findings", [])),
        ("Medication Safety", report.get("medication_safety", [])),
        ("Evidence Summary", report.get("evidence_summary", [])),
        ("Clinician Actions", report.get("clinician_actions", [])),
        ("Missing Information", report.get("missing_information", [])),
        ("Citations", report.get("citations") or report.get("source_citations", [])),
        ("Limitations", report.get("limitations", [])),
    ]

    for heading, items in section_map:
        rows.append({"style": "section", "text": heading})
        normalized = _as_list(items)
        if not normalized:
            rows.append({"style": "muted", "text": "No items reported."})
        for item in normalized:
            _add_wrapped_rows(rows, "item", item, width=82, prefix="- ")
        rows.append({"style": "spacer", "text": ""})
    return rows


PDF_STYLES = {
    "title": {"font": "F2", "size": 17, "height": 24, "color": (0.10, 0.27, 0.43)},
    "title_cont": {"font": "F2", "size": 15, "height": 21, "color": (0.10, 0.27, 0.43)},
    "section": {"font": "F2", "size": 12, "height": 25, "color": (0.10, 0.27, 0.43), "section_band": True},
    "meta": {"font": "F1", "size": 9, "height": 13, "color": (0.34, 0.40, 0.47)},
    "item": {"font": "F1", "size": 9.5, "height": 14, "color": (0.10, 0.13, 0.20)},
    "body": {"font": "F1", "size": 9.5, "height": 14, "color": (0.10, 0.13, 0.20)},
    "muted": {"font": "F1", "size": 10, "height": 14, "color": (0.42, 0.45, 0.50)},
    "spacer": {"font": "F1", "size": 4, "height": 10, "color": (1, 1, 1)},
}


def _pdf_text_command(row, x, y):
    """Return PDF drawing commands for one styled row."""
    style = PDF_STYLES.get(row.get("style"), PDF_STYLES["body"])
    text = _pdf_escape(row.get("text", ""))
    r, g, b = style["color"]
    commands = []
    if style.get("section_band"):
        commands.append("0.91 0.95 0.99 rg")
        commands.append(f"{x - 10} {y - 6} 502 21 re f")
        commands.append("0.12 0.31 0.47 rg")
        commands.append(f"{x - 10} {y - 6} 4 21 re f")
    commands.extend([
        f"{r} {g} {b} rg",
        "BT",
        f"/{style['font']} {style['size']} Tf",
        f"{x + (5 if style.get('section_band') else 0)} {y} Td",
        f"({text}) Tj",
        "ET",
    ])
    return commands


def _pdf_page_stream(rows):
    """Paginate styled rows into PDF page content streams."""
    pages = []
    page_commands = []
    margin_x = 50
    y = 710

    def start_page(page_index):
        commands = [
            "0.95 0.97 0.99 rg",
            "0 0 612 792 re f",
            "1 1 1 rg",
            "38 38 536 706 re f",
        ]
        if page_index == 0:
            commands.extend([
                "0.12 0.31 0.47 rg",
                "38 730 536 7 re f",
                "0.84 0.90 0.96 rg",
                "38 724 536 1 re f",
            ])
        return commands

    page_index = 0
    page_commands = start_page(page_index)
    for row in rows:
        style = PDF_STYLES.get(row.get("style"), PDF_STYLES["body"])
        if y - style["height"] < 56:
            pages.append(page_commands)
            page_index += 1
            page_commands = start_page(page_index)
            y = 710
        if row.get("style") != "spacer":
            page_commands.extend(_pdf_text_command(row, margin_x, y))
        y -= style["height"]

    pages.append(page_commands)
    return ["\n".join(commands).encode("cp1252", "replace") for commands in pages]


def write_simple_pdf(rows, output_path):
    """Write a styled text PDF using only the Python standard library."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if rows and isinstance(rows[0], str):
        rows = [{"style": "body", "text": line} for line in rows]

    objects = []

    def add_object(payload):
        objects.append(payload)
        return len(objects)

    font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold_font_id = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    page_ids = []
    content_ids = []
    page_streams = _pdf_page_stream(rows or [])

    for stream in page_streams:
        content_id = add_object(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        )
        content_ids.append(content_id)
        page_ids.append(None)

    pages_id = len(objects) + len(content_ids) + 1
    for index, content_id in enumerate(content_ids):
        page_ids[index] = add_object(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R /F2 {bold_font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    pages_payload = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
    actual_pages_id = add_object(pages_payload)
    catalog_id = add_object(f"<< /Type /Catalog /Pages {actual_pages_id} 0 R >>".encode("ascii"))

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, payload in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(payload)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )

    with open(output_path, "wb") as file_obj:
        file_obj.write(pdf)


def _load_arabic_pdf_libs():
    """Load Arabic PDF dependencies only when Arabic PDF generation is requested."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "Arabic PDF generation requires reportlab, arabic-reshaper, and python-bidi."
        ) from exc
    return arabic_reshaper, get_display, colors, letter, pdfmetrics, TTFont, canvas


def _arabic_font_paths():
    """Return regular/bold Arabic-capable font paths available on Windows."""
    candidates = [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\tahomabd.ttf"),
        (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            return regular, bold
    raise RuntimeError("No Arabic-capable Windows font was found.")


def _shape_arabic(text, arabic_reshaper, get_display):
    """Shape Arabic text for drawing with a PDF canvas."""
    text = _clean_unicode_text(text)
    return get_display(arabic_reshaper.reshape(text))


def _arabic_wrapped_lines(text, width=72, prefix=""):
    """Wrap Arabic text before bidi shaping."""
    cleaned = _clean_unicode_text(text)
    if not cleaned:
        return []
    return textwrap.wrap(
        cleaned,
        width=width,
        initial_indent=prefix,
        subsequent_indent=" " * len(prefix),
    ) or [prefix]


def _arabic_sections(report):
    """Return Arabic PDF sections from an Arabic structured report."""
    snapshot = report.get("patient_snapshot") or {}
    return [
        ("بيانات المريض", [
            f"رقم الملف: {snapshot.get('submission_id', '')}",
            f"العمر: {snapshot.get('age', '')}",
            f"النوع: {snapshot.get('sex', '')}",
        ]),
        ("الملخص السريري", [report.get("clinical_summary", "")]),
        ("النتائج", report.get("findings", [])),
        ("تنبيهات السلامة العاجلة", report.get("urgent_safety_alerts", [])),
        ("سلامة الأدوية", report.get("medication_safety", [])),
        ("ملخص الأدلة", report.get("evidence_summary", [])),
        ("إجراءات مقترحة للطبيب", report.get("clinician_actions", [])),
        ("المعلومات الناقصة", report.get("missing_information", [])),
        ("المراجع والاستشهادات", report.get("citations") or report.get("source_citations", [])),
        ("القيود", report.get("limitations", [])),
    ]


def write_arabic_pdf(report, output_path):
    """Write an Arabic RTL PDF using ReportLab with embedded Arabic fonts."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    arabic_reshaper, get_display, colors, letter, pdfmetrics, TTFont, canvas = _load_arabic_pdf_libs()
    regular_font, bold_font = _arabic_font_paths()
    pdfmetrics.registerFont(TTFont("ArabicRegular", regular_font))
    pdfmetrics.registerFont(TTFont("ArabicBold", bold_font))

    page_width, page_height = letter
    pdf = canvas.Canvas(output_path, pagesize=letter)
    left = 44
    right = page_width - 50
    y = page_height - 82
    page_index = 0

    def draw_page_background():
        pdf.setFillColor(colors.HexColor("#f2f6fb"))
        pdf.rect(0, 0, page_width, page_height, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.rect(38, 38, page_width - 76, page_height - 86, stroke=0, fill=1)
        if page_index == 0:
            pdf.setFillColor(colors.HexColor("#1f4e79"))
            pdf.rect(38, page_height - 60, page_width - 76, 7, stroke=0, fill=1)
            pdf.setFillColor(colors.HexColor("#d5e3f2"))
            pdf.rect(38, page_height - 66, page_width - 76, 1, stroke=0, fill=1)

    def new_page():
        nonlocal y, page_index
        pdf.showPage()
        page_index += 1
        draw_page_background()
        y = page_height - 92

    def ensure_space(height):
        if y - height < 58:
            new_page()

    def draw_rtl_line(text, *, font="ArabicRegular", size=10, color="#172033", x=None):
        nonlocal y
        if not text:
            return
        pdf.setFont(font, size)
        pdf.setFillColor(colors.HexColor(color))
        pdf.drawRightString(x or right, y, _shape_arabic(text, arabic_reshaper, get_display))

    def draw_wrapped(text, *, prefix="", font="ArabicRegular", size=10, color="#172033", width=74):
        nonlocal y
        for line in _arabic_wrapped_lines(text, width=width, prefix=prefix):
            ensure_space(16)
            draw_rtl_line(line, font=font, size=size, color=color)
            y -= 15

    def draw_section(title):
        nonlocal y
        ensure_space(34)
        pdf.setFillColor(colors.HexColor("#e8f2fb"))
        pdf.rect(left, y - 9, right - left, 24, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#1f4e79"))
        pdf.rect(right - 4, y - 9, 4, 24, stroke=0, fill=1)
        draw_rtl_line(title, font="ArabicBold", size=12, color="#1f4e79", x=right - 12)
        y -= 32

    draw_page_background()
    title_lines = _arabic_wrapped_lines(report.get("report_title") or "تقرير المراجعة السريرية", width=46)
    for index, line in enumerate(title_lines):
        ensure_space(24)
        draw_rtl_line(line, font="ArabicBold", size=17 if index == 0 else 15, color="#1f4e79")
        y -= 24 if index == 0 else 20

    draw_rtl_line(f"تاريخ الإصدار: {datetime.utcnow().isoformat(timespec='seconds')}Z", size=9, color="#56677a")
    y -= 24

    for heading, items in _arabic_sections(report):
        draw_section(heading)
        normalized = _as_list(items)
        if not normalized:
            draw_wrapped("لا توجد عناصر مسجلة.", font="ArabicRegular", size=10, color="#6b7280")
        for item in normalized:
            draw_wrapped(item, prefix="- ", font="ArabicRegular", size=10, width=72)
        y -= 10

    pdf.save()


def save_report_pdf(report, *, upload_dir, submission_id=None, patient_name=None, code_no=None, arabic=False):
    """Save the structured report as a PDF under uploads/reports and return metadata."""
    date_folder = datetime.utcnow().strftime("%Y%m%d")
    fallback_id = str(submission_id or "unsaved")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(patient_name or "").strip()).strip("-")
    safe_code = re.sub(r"[^A-Za-z0-9_-]+", "-", str(code_no or "").strip()).strip("-")
    if safe_name and safe_code:
        filename_base = f"{safe_name}-({safe_code})"
    elif safe_name:
        filename_base = safe_name
    elif safe_code:
        filename_base = f"patient-({safe_code})"
    else:
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", fallback_id).strip("-")
        filename_base = f"clinical-report-{safe_id}"

    filename = f"{filename_base}.pdf"
    relative_path = os.path.join("reports", date_folder, filename)
    absolute_path = os.path.join(upload_dir, relative_path)
    if arabic:
        write_arabic_pdf(report or {}, absolute_path)
    else:
        write_simple_pdf(_report_lines(report or {}), absolute_path)
    return {
        "relative_path": relative_path.replace(os.sep, "/"),
        "url": f"/uploads/{relative_path.replace(os.sep, '/')}",
        "filename": filename,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
