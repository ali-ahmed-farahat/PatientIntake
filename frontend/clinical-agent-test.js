const form = document.getElementById("agent-form");
    const result = document.getElementById("result");
    const rawJson = document.getElementById("raw-json");

    // Escapes dynamic values before inserting them into HTML.
    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    // Renders an array as list items, or shows fallback text when the array is empty.
    function listItems(items, fallback) {
      if (!items || !items.length) {
        return `<p class="muted">${escapeHtml(fallback)}</p>`;
      }
      return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
    }

    // Builds the readable result panel from the clinical-agent JSON response.
    function renderClinicalResult(payload) {
      const agent = payload.clinical_agent || {};
      const report = agent.report || {};
      const checks = payload.medication_checks || {};
      const rag = payload.rag || {};
      const sources = rag.sources || [];
      const flags = checks.label_flags || [];
      const openfda = checks.openfda || [];
      const notes = payload.notes || [];
      const flagHtml = flags.length
        ? flags.map((flag) => `
            <div class="flag">
              <strong>${escapeHtml(flag.drug || "Medication flag")}</strong><br>
              ${escapeHtml(flag.message || "")}
            </div>
          `).join("")
        : `<div class="ok">No label interaction text flags were found for the parsed medication list.</div>`;
      const labelHtml = openfda.length
        ? `<ul>${openfda.map((item) => `
            <li>
              <strong>${escapeHtml(item.query)}</strong>:
              ${item.found ? "openFDA label found" : escapeHtml(item.message || item.error || "No label found")}
            </li>
          `).join("")}</ul>`
        : `<p class="muted">No medication names were parsed.</p>`;
      const sourceHtml = sources.length
        ? `<ul>${sources.map((source) => `
            <li>${escapeHtml(source.citation)} <span class="muted">score ${escapeHtml(source.score)}</span></li>
          `).join("")}</ul>`
        : `<p class="muted">No RAG sources returned.</p>`;

      result.innerHTML = `
        <div class="section">
          <h2>CrewAI Clinical Agent Review</h2>
          <p>${escapeHtml(report.clinical_summary || payload.message || "Clinical agent response received.")}</p>
          <p class="muted">Engine: ${escapeHtml(agent.engine || "gemini")} | Model: ${escapeHtml(agent.model || "")} | Confidence: ${escapeHtml(report.confidence || "not stated")}</p>
          ${agent.error ? `<div class="flag">${escapeHtml(agent.error)}</div>` : ""}
        </div>
        <div class="section">
          <h3>Safety Flags</h3>
          ${flagHtml}
        </div>
        <div class="section">
          <h3>CrewAI Key Findings</h3>
          ${listItems(report.key_findings || [], "No key findings returned.")}
        </div>
        <div class="section">
          <h3>CrewAI Medication Safety</h3>
          ${listItems(report.medication_safety || [], "No medication-safety summary returned.")}
        </div>
        <div class="section">
          <h3>CrewAI Guideline Context</h3>
          ${listItems(report.guideline_context || [], "No guideline-context summary returned.")}
        </div>
        <div class="section">
          <h3>Red Flags</h3>
          ${listItems(report.red_flags || [], "No red flags returned.")}
        </div>
        <div class="section">
          <h3>Missing Information</h3>
          ${listItems(report.missing_information || [], "No missing-information list returned.")}
        </div>
        <div class="section">
          <h3>Recommended Follow-up Questions</h3>
          ${listItems(report.recommended_next_questions || [], "No follow-up questions returned.")}
        </div>
        <div class="section">
          <h3>Medications Checked</h3>
          ${listItems(checks.drug_candidates || [], "No medication names were parsed.")}
        </div>
        <div class="section">
          <h3>openFDA Labels</h3>
          ${labelHtml}
        </div>
        <div class="section">
          <h3>Guideline Sources</h3>
          ${sourceHtml}
        </div>
        <div class="section">
          <h3>Retrieved Context</h3>
          <div class="passage">${escapeHtml(rag.context || "No context returned.")}</div>
        </div>
        <div class="section">
          <h3>Notes</h3>
          ${listItems(notes, "No notes returned.")}
        </div>
      `;
    }

    // Sends the form values to the clinical-agent endpoint and renders the response.
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      result.innerHTML = '<div class="muted">Running...</div>';
      rawJson.textContent = "Running...";
      const body = {
        query: document.getElementById("query").value,
        current_medications: document.getElementById("current-medications").value,
        medical_history: document.getElementById("medical-history").value,
        top_k: Number(document.getElementById("top-k").value || 1)
      };
      try {
        const response = await fetch("/clinical-agent", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const payload = await response.json();
        rawJson.textContent = JSON.stringify(payload, null, 2);
        renderClinicalResult(payload);
      } catch (error) {
        result.innerHTML = `<div class="flag">${escapeHtml(error)}</div>`;
        rawJson.textContent = String(error);
      }
    });
