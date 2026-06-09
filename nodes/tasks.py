"""Consolidated task prompt builders used by the CrewAI agent layer."""

import json


TASK_DEFINITIONS = {
    "clinical": {
        "description": "Review this evidence packet and produce the required JSON clinical research memo.",
        "expected_output": "A valid JSON object matching the requested clinical memo response format.",
    },
    "evidence_reviewer": {
        "description": "Review this research result and judge the quality of its supplied evidence.",
        "expected_output": "A valid JSON object matching the requested evidence quality review response format.",
    },
    "lifestyle": {
        "description": "Here is the raw patient form submission. Review lifestyle and psychological fields and make your triage decision.",
        "expected_output": "A valid JSON object matching the requested lifestyle triage response format.",
    },
    "research": {
        "description": "Review this clinical/research packet and produce the required JSON research memo.",
        "expected_output": "A valid JSON object matching the requested research memo response format.",
    },
    "report": {
        "description": "Create the final structured JSON report from this pipeline output.",
        "expected_output": "A valid JSON object matching the requested final report response format.",
    },
    "arabic_pdf": {
        "description": "Translate this structured report into the requested Arabic JSON shape for the PDF only.",
        "expected_output": "A valid JSON object matching the requested Arabic PDF report response format.",
    },
}


def build_json_task_prompt(task_name, payload):
    """Return the existing two-part task prompt for a JSON payload."""
    task = TASK_DEFINITIONS[task_name]
    return f"{task['description']}\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
