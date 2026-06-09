import json
import os

from core.agent_utils import parse_json_object


def _load_crewai():
    """Import CrewAI lazily so app startup can report a clear dependency error."""
    try:
        from crewai import Agent, Crew, LLM, Process, Task
    except ImportError as exc:
        raise RuntimeError(
            "CrewAI is not installed. Install the project requirements, including crewai[google-genai]."
        ) from exc
    return Agent, Crew, LLM, Process, Task


def _configure_google_key(api_key):
    """Expose the Gemini key through the env names CrewAI/LiteLLM integrations expect."""
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    os.environ.setdefault("GEMINI_API_KEY", api_key)
    os.environ.setdefault("GOOGLE_API_KEY", api_key)


def _crew_output_text(result, task):
    """Return the raw text from a CrewAI result across supported output shapes."""
    for value in (
        getattr(result, "raw", None),
        getattr(getattr(task, "output", None), "raw", None),
        result,
    ):
        if value:
            return str(value)
    return ""


def run_crewai_json_agent(
    *,
    role,
    goal,
    backstory,
    task_prompt,
    expected_output,
    api_key,
    model_name,
    max_tokens=4096,
    timeout=45,
    label="CrewAI agent",
):
    """Run one CrewAI agent/task and parse the JSON object it returns."""
    _configure_google_key(api_key)
    Agent, Crew, LLM, Process, Task = _load_crewai()

    llm = LLM(
        model=model_name,
        temperature=0.2,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    agent = Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=task_prompt,
        expected_output=expected_output,
        agent=agent,
    )
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
    try:
        result = crew.kickoff()
        return parse_json_object(_crew_output_text(result, task), error_label=label)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"{label} failed: {exc}") from exc
