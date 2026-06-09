"""Consolidated public entry points for the CrewAI agents.

The original agent modules were moved under ``nodes`` as private helpers so their
packet-building and fallback behavior stays intact. Import from this module in
application code.
"""

from nodes._clinical_agent import (
    build_agent_inputs,
    build_clinical_agent_response,
    build_evidence_packet,
    call_gemini_clinical_agent,
)
from nodes._evidence_reviewer_agent import (
    build_evidence_review_packet,
    call_gemini_evidence_reviewer,
    run_evidence_reviewer_agent,
)
from nodes._lifestyle_agent import run_lifestyle_agent
from nodes._report_agent import (
    build_arabic_pdf_report,
    build_fallback_arabic_pdf_report,
    build_fallback_report,
    build_report_packet,
    call_arabic_pdf_report,
    call_report_agent,
    run_report_agent,
    save_report_pdf,
)
from nodes._research_agent import (
    build_pubmed_query,
    call_gemini_research_agent,
    fetch_pubmed_papers,
    run_research_agent,
)


AGENT_NAMES = {
    "clinical": "Clinical Evidence Review Agent",
    "evidence_reviewer": "Evidence Quality Reviewer Agent",
    "lifestyle": "Lifestyle Triage Agent",
    "research": "Research Synthesis Agent",
    "report": "Final Structured Report Agent",
    "arabic_pdf": "Arabic PDF Report Translator",
}
