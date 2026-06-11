import json
import os
from xml.etree import ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from core.agent_utils import compact_text, load_secret as read_secret
from tools.crewai_agent_tools import run_crewai_json_agent


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEMINI_RESEARCH_MODEL = "gemini-2.5-flash"
PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_TOOL = os.environ.get("PUBMED_TOOL", "PatientIntake")
PUBMED_EMAIL = os.environ.get("PUBMED_EMAIL", "")


# Purpose: tell Gemini how to synthesize RAG, drug-label, and PubMed evidence.
RESEARCH_SYSTEM_PROMPT = """
You are a medical research assistant supporting a licensed clinician.

You will receive:
- Clinical Agent output
- RAG guideline citations
- Medication safety evidence
- PubMed abstracts when available

Return ONLY a valid JSON object. No markdown and no text outside JSON.

Response format:
{
  "research_summary": "short clinician-facing research synthesis",
  "evidence_points": ["key evidence points from RAG, drug labels, and PubMed"],
  "clinical_relevance": ["why the evidence matters for this case"],
  "conflicts_or_uncertainties": ["conflicting evidence or uncertainty"],
  "suggested_clinician_review": ["specific items for clinician review"],
  "citations": ["source citations, PMID citations, or drug-label sources supplied in the packet"],
  "confidence": "high or moderate or low",
  "limitations": ["limits of the search and analysis"]
}

Rules:
- Do not diagnose.
- Do not prescribe.
- Do not invent articles, PMIDs, citations, contraindications, or guideline statements.
- Base conclusions only on supplied evidence.
- Clearly separate medication-label evidence from PubMed/guideline evidence.
- Only cite PMID values that appear in the supplied pubmed_papers list.
- If pubmed_papers is empty, do not include PMID citations and state that PubMed retrieval returned no papers.
- If PubMed retrieval failed or returned no papers, say so in limitations.
- Do not use Markdown, bold markers, bullets, headings, or asterisks inside string values. Use plain text only.
""".strip()


def load_pubmed_api_key():
    """Read the PubMed API key from env vars or the local APIkey file."""
    return read_secret(
        "PUBMED_API_KEY",
        base_dir=BASE_DIR,
        aliases={"PUBMED_API_KEY", "NCBI_API_KEY", "PUBMED", "NCBI"},
    )


def pubmed_request_params(**params):
    """Attach optional PubMed identity fields and API key to one eutils request."""
    merged = dict(params)
    api_key = load_pubmed_api_key()
    if api_key:
        merged["api_key"] = api_key
    if PUBMED_TOOL:
        merged["tool"] = PUBMED_TOOL
    if PUBMED_EMAIL:
        merged["email"] = PUBMED_EMAIL
    return merged


# Purpose: turn the clinical packet into a compact PubMed search query.
def build_pubmed_query(clinical_packet):
    inputs = clinical_packet.get("input", {})
    meds = clinical_packet.get("medication_checks", {}).get("drug_candidates", [])
    query_parts = [
        inputs.get("query"),
        inputs.get("medical_history"),
        " ".join(meds),
        "erectile dysfunction",
    ]
    query = " ".join(str(part) for part in query_parts if part).strip()
    return query or "erectile dysfunction medication safety"


# Purpose: retrieve IDs, then abstracts, from PubMed eutils.
def fetch_pubmed_papers(query, *, max_results=5, timeout=20):
    search_params = pubmed_request_params(
        db="pubmed",
        term=query,
        retmode="json",
        retmax=str(max_results),
        sort="relevance",
    )
    search_url = PUBMED_SEARCH_URL + "?" + urlencode(search_params)
    with urlopen(search_url, timeout=timeout) as response:
        search_payload = json.loads(response.read().decode("utf-8"))

    pmids = search_payload.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        return []

    fetch_params = pubmed_request_params(
        db="pubmed",
        id=",".join(pmids),
        retmode="xml",
        rettype="abstract",
    )
    fetch_url = PUBMED_FETCH_URL + "?" + urlencode(fetch_params)
    with urlopen(fetch_url, timeout=timeout) as response:
        xml_text = response.read().decode("utf-8", "replace")

    return parse_pubmed_xml(xml_text, pmids)


def xml_text(node, path):
    """Return normalized text for one XML child path."""
    found = node.find(path)
    return " ".join(" ".join(found.itertext()).split()) if found is not None else ""


def parse_pubmed_xml(xml_payload, pmids):
    """Convert PubMed XML into the paper cards used by the research report."""
    papers = []
    root = ET.fromstring(xml_payload)
    for article in root.findall(".//PubmedArticle"):
        pmid = xml_text(article, ".//PMID") or (pmids[len(papers)] if len(papers) < len(pmids) else "")
        abstract = " ".join(
            " ".join(part.itertext()).strip()
            for part in article.findall(".//AbstractText")
        )
        papers.append({
            "pmid": pmid,
            "title": xml_text(article, ".//ArticleTitle"),
            "journal": xml_text(article, ".//Journal/Title"),
            "year": (xml_text(article, ".//PubDate/Year") or xml_text(article, ".//MedlineDate"))[:4],
            "abstract": compact_text(abstract, 1800),
            "citation": f"PMID {pmid}" if pmid else "",
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        })
    return papers


def call_gemini_research_agent(research_packet, *, api_key, model_name=GEMINI_RESEARCH_MODEL, timeout=60):
    """Run the CrewAI research synthesis agent and parse its JSON memo."""
    return run_crewai_json_agent(
        role="Research Synthesis Agent",
        goal="Synthesize supplied clinical, guideline, medication-label, and PubMed evidence without inventing sources.",
        backstory=RESEARCH_SYSTEM_PROMPT,
        task_prompt=(
            "Review this clinical/research packet and produce the required JSON research memo.\n\n"
            f"{json.dumps(research_packet, ensure_ascii=False, indent=2)}"
        ),
        expected_output="A valid JSON object matching the requested research memo response format.",
        api_key=api_key,
        model_name=model_name,
        max_tokens=8192,
        timeout=timeout,
        label="CrewAI research agent",
    )


def run_research_agent(clinical_packet, *, api_key, model_name=GEMINI_RESEARCH_MODEL, max_pubmed_results=5):
    query = build_pubmed_query(clinical_packet)
    pubmed_error = None
    try:
        papers = fetch_pubmed_papers(query, max_results=max_pubmed_results)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ET.ParseError) as exc:
        papers = []
        pubmed_error = str(exc)

    research_packet = {
        "pubmed_query": query,
        "pubmed_error": pubmed_error,
        "pubmed_papers": papers,
        "clinical_packet": clinical_packet,
    }

    try:
        report = call_gemini_research_agent(
            research_packet,
            api_key=api_key,
            model_name=model_name,
        )
        llm_error = None
    except RuntimeError as exc:
        report = {
            "research_summary": "CrewAI research reasoning was unavailable.",
            "evidence_points": [],
            "clinical_relevance": [],
            "conflicts_or_uncertainties": [],
            "suggested_clinician_review": [],
            "citations": [],
            "confidence": "low",
            "limitations": [str(exc)],
        }
        llm_error = str(exc)

    return {
        "engine": "crewai",
        "model": model_name,
        "pubmed_query": query,
        "pubmed_api_key_configured": bool(load_pubmed_api_key()),
        "research_packet": research_packet,
        "pubmed_papers": papers,
        "pubmed_error": pubmed_error,
        "report": report,
        "error": llm_error,
    }
