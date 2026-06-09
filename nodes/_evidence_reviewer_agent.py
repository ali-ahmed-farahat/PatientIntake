import json

from core.agent_utils import compact_text
from tools.crewai_agent_tools import run_crewai_json_agent


GEMINI_EVIDENCE_REVIEWER_MODEL = "gemini-2.5-flash"


EVIDENCE_REVIEWER_SYSTEM_PROMPT = """
You are an evidence quality reviewer supporting a licensed clinician.

You receive the output of a research agent. The research result may include:
- the research agent synthesis
- PubMed retrieval results
- the full research packet used by the research agent
- the clinical packet nested inside the research packet
- RAG guideline sources
- medication label checks and safety flags

Your task is evidence quality control only.

Return ONLY a valid JSON object. No markdown and no text outside JSON.

Response format:
{
  "overall_evidence_quality": "high or moderate or low or insufficient",
  "final_report_readiness": "ready or ready_with_cautions or not_ready",
  "reviewer_summary": "short clinician-facing evidence quality summary",
  "high_confidence_claims": [
    {
      "claim": "claim that is strongly supported",
      "support": "source or reason",
      "quality_reason": "why this is high confidence"
    }
  ],
  "moderate_confidence_claims": [
    {
      "claim": "claim that is plausible but needs caution",
      "support": "source or reason",
      "quality_reason": "why this is moderate confidence"
    }
  ],
  "low_confidence_or_unsupported_claims": [
    {
      "claim": "claim that is weak, inferred, overstated, or unsupported",
      "quality_issue": "why the support is weak or absent"
    }
  ],
  "citation_quality_issues": ["claims with missing, weak, indirect, or mismatched citations"],
  "missing_evidence": ["specific patient data, tests, source passages, or citations still needed"],
  "overstatement_risks": ["places where the research or clinical synthesis may overstate certainty"],
  "evidence_conflicts": ["conflicts or tensions between supplied evidence sources"],
  "clinician_review_priorities": ["highest-priority evidence items for clinician verification"],
  "limitations": ["limits of this evidence review"]
}

Evidence quality scale:
- high: directly supported by a supplied guideline passage, drug label, contraindication, safety flag, or strong cited source.
- moderate: supported by relevant but indirect evidence, common clinical association, or incomplete patient data.
- low: plausible but weakly supported, inferred, or missing a direct citation.
- insufficient: not enough supplied evidence to support the claim.

Rules:
- Do not diagnose.
- Do not prescribe.
- Do not create new clinical claims.
- Do not add new citations or sources.
- Do not rewrite the clinical or research report.
- Only judge whether supplied claims are supported by supplied evidence.
- Flag claims that rely on incomplete patient data.
- Flag claims that use citations which do not directly support the claim.
- If PubMed returned no papers, include that in limitations or missing evidence.
- If a PMID is cited but does not appear in the supplied pubmed_papers list, flag it as a citation quality issue.
- Do not use Markdown, bold markers, bullets, headings, or asterisks inside string values. Use plain text only.
""".strip()


def _compact_json(value, max_chars=2500):
    """Return compact text for nested JSON-like values without sending huge payloads."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value or "")
    return compact_text(text, max_chars)


def compact_openfda_results(openfda_results, limit=10):
    """Keep the medication-label evidence small enough for reviewer context."""
    compacted = []
    for item in (openfda_results or [])[:limit]:
        label = item.get("label") or {}
        compacted.append({
            "query": item.get("query"),
            "found": item.get("found"),
            "source": item.get("source"),
            "message": item.get("message") or item.get("error"),
            "brand_names": label.get("brand_names", []),
            "generic_names": label.get("generic_names", []),
            "contraindications": compact_text(label.get("contraindications"), 900),
            "drug_interactions": compact_text(label.get("drug_interactions"), 900),
            "warnings": compact_text(label.get("warnings"), 900),
        })
    return compacted


def compact_pubmed_papers(papers, limit=8):
    """Keep PubMed metadata and abstracts relevant but bounded."""
    compacted = []
    for paper in (papers or [])[:limit]:
        compacted.append({
            "pmid": paper.get("pmid"),
            "title": paper.get("title"),
            "journal": paper.get("journal"),
            "year": paper.get("year"),
            "citation": paper.get("citation"),
            "url": paper.get("url"),
            "abstract": compact_text(paper.get("abstract"), 1200),
        })
    return compacted


def compact_medication_checks(medication_checks):
    """Extract reviewer-relevant medication evidence from the clinical packet."""
    return {
        "drug_candidates": medication_checks.get("drug_candidates", []),
        "label_flags": medication_checks.get("label_flags", []),
        "openfda": compact_openfda_results(medication_checks.get("openfda", [])),
        "drugbank_summary": _compact_json(medication_checks.get("drugbank", {}), 2500),
    }


def build_evidence_review_packet(research_result):
    """Build the only packet the reviewer needs from the research result."""
    research_packet = research_result.get("research_packet") or {}
    clinical_packet = research_packet.get("clinical_packet") or {}
    clinical_agent = clinical_packet.get("clinical_agent") or {}
    clinical_report = clinical_agent.get("report") or {}
    medication_checks = clinical_packet.get("medication_checks") or {}
    rag = clinical_packet.get("rag") or {}

    return {
        "research_agent": {
            "engine": research_result.get("engine"),
            "model": research_result.get("model"),
            "pubmed_query": research_result.get("pubmed_query"),
            "pubmed_error": research_result.get("pubmed_error"),
            "error": research_result.get("error"),
            "report": research_result.get("report") or {},
        },
        "pubmed_papers": compact_pubmed_papers(research_result.get("pubmed_papers", [])),
        "clinical_packet": {
            "input": clinical_packet.get("input", {}),
            "clinical_agent": {
                "engine": clinical_agent.get("engine"),
                "model": clinical_agent.get("model"),
                "error": clinical_agent.get("error"),
                "report": clinical_report,
            },
            "rag": {
                "query": rag.get("query"),
                "sources": rag.get("sources", []),
                "context": compact_text(rag.get("context"), 5000),
            },
            "medication_checks": compact_medication_checks(medication_checks),
            "notes": clinical_packet.get("notes", []),
        },
        "research_packet_meta": {
            "pubmed_query": research_packet.get("pubmed_query"),
            "pubmed_error": research_packet.get("pubmed_error"),
            "has_nested_clinical_packet": bool(clinical_packet),
        },
    }


def call_gemini_evidence_reviewer(review_packet, *, api_key, model_name=GEMINI_EVIDENCE_REVIEWER_MODEL, timeout=60):
    """Run the CrewAI evidence quality reviewer and parse the JSON review."""
    return run_crewai_json_agent(
        role="Evidence Quality Reviewer Agent",
        goal="Grade supplied claims against supplied evidence and flag support, citation, and overstatement risks.",
        backstory=EVIDENCE_REVIEWER_SYSTEM_PROMPT,
        task_prompt=(
            "Review this research result and judge the quality of its supplied evidence.\n\n"
            f"{json.dumps(review_packet, ensure_ascii=False, indent=2)}"
        ),
        expected_output="A valid JSON object matching the requested evidence quality review response format.",
        api_key=api_key,
        model_name=model_name,
        max_tokens=8192,
        timeout=timeout,
        label="CrewAI evidence reviewer",
    )


def run_evidence_reviewer_agent(research_result, *, api_key, model_name=GEMINI_EVIDENCE_REVIEWER_MODEL):
    """Return an evidence quality review using only the research agent result."""
    review_packet = build_evidence_review_packet(research_result or {})

    try:
        report = call_gemini_evidence_reviewer(
            review_packet,
            api_key=api_key,
            model_name=model_name,
        )
        llm_error = None
    except RuntimeError as exc:
        report = {
            "overall_evidence_quality": "insufficient",
            "final_report_readiness": "not_ready",
            "reviewer_summary": "Evidence review was unavailable.",
            "high_confidence_claims": [],
            "moderate_confidence_claims": [],
            "low_confidence_or_unsupported_claims": [],
            "citation_quality_issues": [],
            "missing_evidence": [],
            "overstatement_risks": [],
            "evidence_conflicts": [],
            "clinician_review_priorities": [],
            "limitations": [str(exc)],
        }
        llm_error = str(exc)

    return {
        "engine": "crewai",
        "model": model_name,
        "review_packet": review_packet,
        "report": report,
        "error": llm_error,
    }
