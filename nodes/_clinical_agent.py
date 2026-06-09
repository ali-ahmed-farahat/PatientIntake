import json

from tools.crewai_agent_tools import run_crewai_json_agent


# Purpose: tell Gemini exactly what structured clinical memo to return.
CLINICAL_AGENT_SYSTEM_PROMPT = """
You are a clinical research assistant supporting a licensed clinician.

You will receive a structured evidence packet containing patient context, local RAG guideline
passages with citations, medication label checks, and safety flags.

Return ONLY a valid JSON object. No markdown and no text outside the JSON.

Response format:
{
  "clinical_summary": "short clinician-facing summary",
  "key_findings": ["important case findings from the provided evidence"],
  "medication_safety": ["medication safety issues or reassuring findings"],
  "guideline_context": ["what the retrieved sources support, with citations"],
  "red_flags": ["urgent or high-risk issues to review"],
  "missing_information": ["history, exam, labs, timing, dose, or context still needed"],
  "recommended_next_questions": ["specific follow-up questions for the clinician to ask"],
  "source_citations": ["citations from the provided RAG sources or drug labels"],
  "confidence": "high or moderate or low",
  "limitations": ["important limits of the analysis"]
}

Rules:
- Do not diagnose.
- Do not prescribe.
- Do not invent citations, guidelines, medication facts, doses, contraindications, or lab values.
- Base conclusions only on the supplied evidence packet.
- If the packet contains a serious medication interaction or contraindication, make it prominent.
- State when a finding requires clinician confirmation.
- If evidence is insufficient, say what is missing.
- Do not use Markdown, bold markers, bullets, headings, or asterisks inside string values. Use plain text only.
""".strip()


# Purpose: shrink API lookups before they are sent to Gemini.
def compact_openfda_results(openfda_results):
    return [
        {
            "query": item.get("query"),
            "found": item.get("found"),
            "source": item.get("source"),
            "message": item.get("message") or item.get("error"),
            "brand_names": label.get("brand_names", []),
            "generic_names": label.get("generic_names", []),
            "routes": label.get("routes", []),
            "indications": label.get("indications", ""),
            "contraindications": label.get("contraindications", ""),
            "drug_interactions": label.get("drug_interactions", ""),
            "warnings": label.get("warnings", ""),
        }
        for item in openfda_results
        for label in [item.get("label") or {}]
    ]


# Purpose: combine patient context, local RAG passages, and medication checks.
def build_evidence_packet(inputs, rag_context, medication_checks):
    return {
        "patient_context": inputs,
        "rag": {
            "query": rag_context.get("query"),
            "context": rag_context.get("context"),
            "sources": rag_context.get("sources", []),
        },
        "medication_checks": {
            "drug_candidates": medication_checks.get("drug_candidates", []),
            "openfda": compact_openfda_results(medication_checks.get("openfda", [])),
            "drugbank": medication_checks.get("drugbank", {}),
            "label_flags": medication_checks.get("label_flags", []),
        },
    }


def call_gemini_clinical_agent(evidence_packet, *, api_key, model_name, timeout=45):
    """Run the CrewAI clinical review agent and parse the JSON memo."""
    return run_crewai_json_agent(
        role="Clinical Evidence Review Agent",
        goal="Produce a clinician-facing structured memo using only the supplied evidence packet.",
        backstory=CLINICAL_AGENT_SYSTEM_PROMPT,
        task_prompt=(
            "Review this evidence packet and produce the required JSON clinical research memo.\n\n"
            f"{json.dumps(evidence_packet, ensure_ascii=False, indent=2)}"
        ),
        expected_output="A valid JSON object matching the requested clinical memo response format.",
        api_key=api_key,
        model_name=model_name,
        max_tokens=8192,
        timeout=timeout,
        label="CrewAI clinical agent",
    )


def first_present(data, *keys, default=""):
    """Return the first non-empty value found for the given keys in a dictionary."""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def unique_names(names, limit):
    """Keep medication names unique, preserving order and stopping at the requested limit."""
    result = []
    seen = set()
    for name in names:
        name = str(name or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= limit:
            break
    return result


def load_submission_payload(submission_id, *, get_db_connection, safe_json_loads):
    """Load one saved intake submission and merge database columns into its form data."""
    try:
        submission_id = int(submission_id)
    except (TypeError, ValueError):
        raise RuntimeError("submission_id must be an integer.")

    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT id, full_name, age, mobile, email, form_data
        FROM intake_forms
        WHERE id = ?
        """,
        (submission_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError(f"Submission #{submission_id} was not found.")

    form_data = safe_json_loads(row["form_data"], {})
    form_data.update({
        "submission_id": row["id"],
        "fullName": row["full_name"],
        "age": row["age"],
        "mobile": row["mobile"],
        "email": row["email"],
    })
    return form_data


def build_agent_inputs(data, dependencies):
    """Normalize request or submission data into the fields the clinical agent needs."""
    merged = {}
    submission_id = data.get("submission_id") or data.get("submissionId")
    if submission_id:
        merged.update(load_submission_payload(
            submission_id,
            get_db_connection=dependencies["get_db_connection"],
            safe_json_loads=dependencies["safe_json_loads"],
        ))
    merged.update(data)

    current_medications = first_present(
        merged,
        "current_medications",
        "currentMedications",
        "medications",
        "medicationText",
    )
    medical_history = first_present(
        merged,
        "medical_history",
        "medicalHistory",
        "history",
        "pastMedicalHistory",
    )
    investigation_summary = first_present(
        merged,
        "investigation_summary",
        "investigationSummary",
        "investigation_results",
        "investigationResults",
        "labs",
    )
    clinical_question = str(first_present(
        merged,
        "query",
        "question",
        "clinical_question",
        "clinicalQuestion",
    )).strip()

    patient_parts = [
        f"Age: {first_present(merged, 'age')}",
        f"Sex: {first_present(merged, 'sex', 'gender')}",
        f"Medical history: {medical_history}",
        f"Current medications: {current_medications}",
        f"Investigations: {investigation_summary}",
    ]
    patient_context = "\n".join(
        part for part in patient_parts
        if not part.endswith(": ") and not part.endswith(": None")
    )

    if not clinical_question:
        clinical_question = "clinical guidance for this patient context"
        if medical_history or current_medications or investigation_summary:
            clinical_question += f": {patient_context}"

    return {
        "submission_id": submission_id,
        "query": clinical_question,
        "patient_context": patient_context,
        "current_medications": str(current_medications or ""),
        "medical_history": str(medical_history or ""),
        "investigation_summary": str(investigation_summary or ""),
    }


def build_clinical_agent_response(data, dependencies):
    """Assemble the full clinical agent response from RAG context and medication checks."""
    inputs = build_agent_inputs(data, dependencies)
    top_k = dependencies["clamp_int"](data.get("top_k"), 6, 1, 12)

    rag_query = inputs["query"]
    if inputs["patient_context"]:
        rag_query = f"{rag_query}\n\nPatient context:\n{inputs['patient_context']}"

    rag_context = dependencies["build_clinical_context"](rag_query, top_k=top_k)

    explicit_drugs = data.get("drug_names") or data.get("drugNames") or []
    if isinstance(explicit_drugs, str):
        explicit_drugs = dependencies["parse_possible_drug_names"](explicit_drugs)
    drug_candidates = unique_names(
        list(explicit_drugs)
        + dependencies["parse_possible_drug_names"](inputs["current_medications"]),
        dependencies["max_lookup_names"],
    )

    openfda_results = [
        dependencies["lookup_openfda_label"](name)
        for name in drug_candidates
    ]
    drugbank_result = dependencies["lookup_drugbank"](drug_candidates)
    label_flags = dependencies["build_label_flags"](
        openfda_results,
        inputs["current_medications"],
        inputs["medical_history"],
    )

    medication_checks = {
        "drug_candidates": drug_candidates,
        "openfda": openfda_results,
        "drugbank": drugbank_result,
        "label_flags": label_flags,
    }
    evidence_packet = build_evidence_packet(inputs, rag_context, medication_checks)

    try:
        llm_report = call_gemini_clinical_agent(
            evidence_packet,
            api_key=dependencies.get("gemini_api_key"),
            model_name=dependencies.get("gemini_clinical_model", "gemini-2.5-flash"),
        )
        llm_error = None
    except RuntimeError as exc:
        llm_report = {
            "clinical_summary": "CrewAI clinical reasoning was unavailable. Evidence packet was still assembled.",
            "key_findings": [],
            "medication_safety": [],
            "guideline_context": [],
            "red_flags": [],
            "missing_information": [],
            "recommended_next_questions": [],
            "source_citations": [],
            "confidence": "low",
            "limitations": [str(exc)],
        }
        llm_error = str(exc)

    return {
        "message": "CrewAI clinical agent packet created for clinician review.",
        "input": inputs,
        "rag": rag_context,
        "medication_checks": medication_checks,
        "evidence_packet": evidence_packet,
        "clinical_agent": {
            "engine": "crewai",
            "model": dependencies.get("gemini_clinical_model", "gemini-2.5-flash"),
            "report": llm_report,
            "error": llm_error,
        },
        "notes": [
            "CrewAI reviewed the assembled RAG and medication evidence packet.",
            "Use retrieved source passages and citations to verify guideline support.",
            "Confirm medication names, doses, allergies, contraindications, and interactions with a licensed clinician.",
            "openFDA and DrugBank results may be incomplete; absence of a flag is not proof of safety.",
        ],
    }
