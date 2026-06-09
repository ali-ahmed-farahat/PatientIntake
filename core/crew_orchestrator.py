"""CrewAI workflow orchestration for the clinical pipeline."""


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


def attach_report_agent_output(
    pipeline,
    data=None,
    *,
    run_report_agent,
    build_arabic_pdf_report,
    save_report_pdf,
    gemini_api_key,
    gemini_report_model,
    upload_dir,
    storage_backend,
    storage_is_ephemeral,
):
    """Run the final report agent and save its structured report as a PDF."""
    data = data or {}
    report_result = run_report_agent(
        pipeline,
        api_key=gemini_api_key,
        model_name=gemini_report_model,
    )
    pipeline["report_agent"] = report_result
    pipeline["final_report"] = report_result.get("report")
    arabic_pdf_report, translation_error = build_arabic_pdf_report(
        report_result.get("report") or {},
        api_key=gemini_api_key,
        model_name=gemini_report_model,
    )
    if translation_error:
        pipeline["report_pdf_translation_error"] = translation_error

    try:
        pipeline["report_pdf"] = save_report_pdf(
            arabic_pdf_report,
            upload_dir=upload_dir,
            submission_id=pipeline.get("submission_id"),
            patient_name=data.get("fullName") or data.get("full_name"),
            code_no=data.get("codeNo") or data.get("code_no"),
            arabic=True,
        )
        pipeline["report_pdf"]["storage_backend"] = storage_backend
        pipeline["report_pdf"]["ephemeral"] = storage_is_ephemeral
    except (OSError, RuntimeError) as exc:
        pipeline["report_pdf"] = {"error": str(exc)}

    pipeline["stopped_after"] = "report_agent"
    return pipeline


def run_full_clinical_pipeline(
    data,
    submission_id=None,
    *,
    gemini_api_key,
    gemini_research_model,
    gemini_evidence_reviewer_model,
    gemini_report_model,
    clinical_agent_module,
    clinical_agent_dependencies,
    run_lifestyle_agent,
    run_research_agent,
    run_evidence_reviewer_agent,
    run_report_agent,
    build_arabic_pdf_report,
    save_report_pdf,
    upload_dir,
    storage_backend,
    storage_is_ephemeral,
):
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

    if not gemini_api_key:
        pipeline.update({
            "status": "error",
            "error": "GEMINI_API_KEY is not configured.",
        })
        return attach_report_agent_output(
            pipeline,
            data,
            run_report_agent=run_report_agent,
            build_arabic_pdf_report=build_arabic_pdf_report,
            save_report_pdf=save_report_pdf,
            gemini_api_key=gemini_api_key,
            gemini_report_model=gemini_report_model,
            upload_dir=upload_dir,
            storage_backend=storage_backend,
            storage_is_ephemeral=storage_is_ephemeral,
        )

    lifestyle_result = run_lifestyle_agent(data, gemini_api_key)
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
        api_key=gemini_api_key,
        model_name=gemini_research_model,
        max_pubmed_results=5,
    )
    pipeline["research_agent"] = research_result

    evidence_review_result = run_evidence_reviewer_agent(
        research_result,
        api_key=gemini_api_key,
        model_name=gemini_evidence_reviewer_model,
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
    return attach_report_agent_output(
        pipeline,
        data,
        run_report_agent=run_report_agent,
        build_arabic_pdf_report=build_arabic_pdf_report,
        save_report_pdf=save_report_pdf,
        gemini_api_key=gemini_api_key,
        gemini_report_model=gemini_report_model,
        upload_dir=upload_dir,
        storage_backend=storage_backend,
        storage_is_ephemeral=storage_is_ephemeral,
    )
