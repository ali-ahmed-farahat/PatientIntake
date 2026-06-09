import json

from tools.crewai_agent_tools import run_crewai_json_agent

GEMINI_MODEL = "gemini-2.5-flash"

# Purpose: ask Gemini whether lifestyle factors can explain the case alone.
SYSTEM_PROMPT = """
You are a clinical decision support assistant working in a men's sexual health clinic.

Your task is to review a patient's lifestyle and psychological data and decide whether
their reported symptoms can be sufficiently explained by lifestyle factors alone —
or whether an organic, vascular, hormonal, or medication-related cause is likely involved
and requires further clinical investigation.

You will receive raw form data. Field names may vary (camelCase, snake_case, Arabic labels).
Extract what you can and reason holistically. Do not penalize missing fields.

You must respond ONLY with a valid JSON object. No preamble, no explanation outside the JSON.

Response format:
{
  "decision": "YES" or "NO",
  "confidence": "high" or "moderate" or "low",
  "dominant_factors": ["list of the main lifestyle factors driving your decision"],
  "reasoning": "2-3 sentences explaining your clinical reasoning",
  "lifestyle_recommendations": ["list of specific lifestyle changes relevant to this patient"],
  "flags": ["any serious findings the doctor should be aware of immediately"],
  "proceed_to_pipeline": true or false
}

Rules:
- decision YES means: lifestyle is the primary likely cause → report goes directly to doctor
- decision NO means: organic/medication cause likely → continue to full clinical pipeline
- proceed_to_pipeline is always the opposite of YES/NO (YES → false, NO → true)
- Be conservative: when in doubt, say NO and send to the pipeline
- Do not use Markdown, bold markers, bullets, headings, or asterisks inside string values. Use plain text only.
- Never diagnose. Never prescribe. This is a triage decision only.
- Base your reasoning on established clinical knowledge of men's sexual health
""".strip()


def _call_gemini(raw_data: dict, api_key: str) -> dict:
    """Run the CrewAI lifestyle triage agent on raw form data."""
    return run_crewai_json_agent(
        role="Lifestyle Triage Agent",
        goal="Decide whether lifestyle and psychological factors sufficiently explain the submission or the full pipeline should continue.",
        backstory=SYSTEM_PROMPT,
        task_prompt=(
            "Here is the raw patient form submission. Review lifestyle and psychological "
            "fields and make your triage decision.\n\n"
            f"{json.dumps(raw_data, ensure_ascii=False, indent=2)}"
        ),
        expected_output="A valid JSON object matching the requested lifestyle triage response format.",
        api_key=api_key,
        model_name=GEMINI_MODEL,
        max_tokens=4096,
        timeout=30,
        label="CrewAI lifestyle agent",
    )


def run_lifestyle_agent(raw_data: dict, api_key: str) -> dict:
    """Return lifestyle-only triage or fall through to the full pipeline."""
    try:
        result = _call_gemini(raw_data, api_key)
    except RuntimeError as exc:
        # Purpose: on model failure, keep the safer full clinical pipeline.
        return {
            "decision": "NO",
            "confidence": "low",
            "dominant_factors": [],
            "reasoning": "Lifestyle agent could not complete analysis due to an error. Defaulting to full pipeline for safety.",
            "lifestyle_recommendations": [],
            "flags": [f"Lifestyle agent error: {exc}"],
            "proceed_to_pipeline": True,
            "error": str(exc),
        }

    if "decision" not in result:
        result["decision"] = "NO"

    # Purpose: keep the routing flag consistent with the YES/NO decision.
    result["proceed_to_pipeline"] = result["decision"].upper() != "YES"
    return result
