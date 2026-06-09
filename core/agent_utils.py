import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Purpose: shared low-level helpers used by the Flask app and agent modules.
TRANSIENT_HTTP_ERRORS = (HTTPError, URLError, TimeoutError)


def load_secret(name, *, base_dir, aliases=None, bare_value=None):
    """Read a secret from env vars or the local APIkey file."""
    valid_names = {key.upper() for key in (aliases or {name})}
    for key in valid_names:
        value = os.environ.get(key)
        if value:
            return value.strip()

    try:
        with open(os.path.join(base_dir, "APIkey"), "r", encoding="utf-8") as file_obj:
            lines = [
                line.strip()
                for line in file_obj
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        return None

    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().upper() in valid_names:
            return value.strip().strip("\"'")

    if bare_value and len(lines) == 1 and "=" not in lines[0] and bare_value(lines[0]):
        return lines[0].strip().strip("\"'")
    return None


def request_json(url, *, method="GET", headers=None, body=None, timeout=12):
    """Send JSON in/out over HTTP with the standard request shape."""
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=request_headers, method=method)
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def compact_text(value, max_chars=700):
    """Normalize scalar/list text and optionally shorten it."""
    text = " ".join(str(item) for item in value if item) if isinstance(value, list) else str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].rstrip() + "..." if len(text) > max_chars else text


def parse_json_object(text, *, fallback=None, error_label="Model"):
    """Parse a JSON object, including model replies wrapped in extra text."""
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        pass

    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            if fallback is None:
                raise RuntimeError(f"{error_label} returned invalid JSON: {exc}") from exc
    if fallback is not None:
        return fallback
    raise RuntimeError(f"{error_label} did not return a JSON object.")


def gemini_model_resource(model_name):
    """Return the API resource path for a Gemini model name."""
    model_name = str(model_name)
    return model_name if model_name.startswith("models/") else f"models/{model_name}"


def gemini_text(payload):
    """Extract text from the Gemini generateContent response shape."""
    parts = (payload.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "\n".join(part.get("text", "") for part in parts if isinstance(part.get("text"), str)).strip()


def call_gemini_json(*, api_key, model_name, system_prompt, prompt, max_tokens=2048, timeout=45, label="Gemini"):
    """Call Gemini and parse its JSON-only response."""
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"{gemini_model_resource(model_name)}:generateContent?key={api_key}"
    )

    last_error = None
    for _ in range(3):
        try:
            return parse_json_object(
                gemini_text(request_json(url, method="POST", body=body, timeout=timeout)),
                error_label=label,
            )
        except TRANSIENT_HTTP_ERRORS as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{label} returned invalid JSON transport: {exc}") from exc
    raise RuntimeError(f"{label} failed after retries: {last_error}")
