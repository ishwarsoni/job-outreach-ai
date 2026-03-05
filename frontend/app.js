/**
 * app.js — Client-side logic for the Job Search Agent UI.
 *
 * Handles:
 *   - Form submission → SSE stream from /api/search
 *   - Pipeline stepper progress updates
 *   - Results table rendering (real-time row additions)
 *   - Email preview panel
 *   - CSV download
 *   - Toast notifications
 */

/* ═══════════════════════════════════════════════════════════════════════════
   DOM References
   ═══════════════════════════════════════════════════════════════════════════ */
const dom = {
    // Form
    searchForm:     document.getElementById('search-form'),
    inputCompany:   document.getElementById('input-company'),
    inputTitle:     document.getElementById('input-title'),
    inputDomain:    document.getElementById('input-domain'),
    inputMaxResults:document.getElementById('input-max-results'),
    inputDryRun:    document.getElementById('input-dry-run'),
    btnSearch:      document.getElementById('btn-search'),

    // Pipeline stepper
    pipelineSection: document.getElementById('pipeline-section'),
    pipelineStepper: document.getElementById('pipeline-stepper'),

    // Results
    resultsSection: document.getElementById('results-section'),
    resultsBody:    document.getElementById('results-body'),
    resultsCount:   document.getElementById('results-count'),

    // Empty state
    emptyState:     document.getElementById('empty-state'),

    // Email panel
    emailOverlay:   document.getElementById('email-overlay'),
    panelTo:        document.getElementById('panel-to'),
    panelSubject:   document.getElementById('panel-subject'),
    panelBody:      document.getElementById('panel-body'),
    btnClosePanel:  document.getElementById('btn-close-panel'),
    btnCopyEmail:   document.getElementById('btn-copy-email'),
    copyText:       document.getElementById('copy-text'),

    // Download
    btnDownload:    document.getElementById('btn-download'),

    // Toast
    toast:          document.getElementById('toast'),
    toastMessage:   document.getElementById('toast-message'),
};


/* ═══════════════════════════════════════════════════════════════════════════
   State
   ═══════════════════════════════════════════════════════════════════════════ */
let currentProfiles = [];
let activeEventSource = null;


/* ═══════════════════════════════════════════════════════════════════════════
   Toast Notifications
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Show a brief toast message at the bottom of the screen.
 * @param {string} message - Text to display
 * @param {number} duration - How long to show in ms (default 2500)
 */
function showToast(message, duration = 2500) {
    dom.toastMessage.textContent = message;
    dom.toast.classList.add('show');
    setTimeout(() => dom.toast.classList.remove('show'), duration);
}


/* ═══════════════════════════════════════════════════════════════════════════
   Pipeline Stepper
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Reset all pipeline steps to their initial "waiting" state.
 */
function resetStepper() {
    for (let i = 1; i <= 4; i++) {
        const stepEl = dom.pipelineStepper.querySelector(`[data-step="${i}"]`);
        stepEl.classList.remove('running', 'done');
        document.getElementById(`step-msg-${i}`).textContent = 'Waiting...';
    }
}

/**
 * Update a pipeline step's visual state and message.
 * @param {number} stepNum - Step number (1–4)
 * @param {'running'|'done'} status - Current status
 * @param {string} message - Status message to display
 */
function updateStep(stepNum, status, message) {
    const stepEl = dom.pipelineStepper.querySelector(`[data-step="${stepNum}"]`);
    if (!stepEl) return;

    stepEl.classList.remove('running', 'done');
    stepEl.classList.add(status);

    const msgEl = document.getElementById(`step-msg-${stepNum}`);
    if (msgEl) msgEl.textContent = message;
}


/* ═══════════════════════════════════════════════════════════════════════════
   Results Table
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Render all profiles into the results table.
 * @param {Array<Object>} profiles - Array of profile objects
 */
function renderResults(profiles) {
    currentProfiles = profiles;
    dom.resultsBody.innerHTML = '';

    profiles.forEach((profile, index) => {
        const row = document.createElement('tr');
        row.classList.add('fade-in');
        row.style.animationDelay = `${index * 50}ms`;

        // Name
        const nameCell = document.createElement('td');
        nameCell.classList.add('cell-name');
        nameCell.textContent = profile.full_name || '—';

        // Title
        const titleCell = document.createElement('td');
        titleCell.textContent = profile.job_title || '—';

        // Company
        const companyCell = document.createElement('td');
        companyCell.classList.add('td-company');
        companyCell.textContent = profile.company || '—';

        // Email
        const emailCell = document.createElement('td');
        if (profile.validated_email) {
            emailCell.classList.add('cell-email');
            emailCell.textContent = profile.validated_email;
        } else {
            emailCell.classList.add('cell-email', 'placeholder');
            emailCell.textContent = 'Pending...';
        }

        // LinkedIn
        const linkCell = document.createElement('td');
        if (profile.profile_url) {
            const link = document.createElement('a');
            link.href = profile.profile_url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.classList.add('cell-link');
            link.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                    <polyline points="15 3 21 3 21 9"></polyline>
                    <line x1="10" y1="14" x2="21" y2="3"></line>
                </svg>
                Profile
            `;
            linkCell.appendChild(link);
        } else {
            linkCell.textContent = '—';
        }

        // Draft button
        const draftCell = document.createElement('td');
        const draftBtn = document.createElement('button');
        draftBtn.classList.add('btn-view-draft');
        draftBtn.dataset.index = index;

        if (profile.email_body) {
            draftBtn.innerHTML = `
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                    <circle cx="12" cy="12" r="3"></circle>
                </svg>
                View
            `;
            draftBtn.addEventListener('click', () => openEmailPanel(index));
        } else {
            draftBtn.innerHTML = '—';
            draftBtn.disabled = true;
        }
        draftCell.appendChild(draftBtn);

        // Assemble row
        row.appendChild(nameCell);
        row.appendChild(titleCell);
        row.appendChild(companyCell);
        row.appendChild(emailCell);
        row.appendChild(linkCell);
        row.appendChild(draftCell);

        dom.resultsBody.appendChild(row);
    });

    // Update count badge
    dom.resultsCount.textContent = `${profiles.length} found`;
}


/* ═══════════════════════════════════════════════════════════════════════════
   Email Preview Panel
   ═══════════════════════════════════════════════════════════════════════════ */

/** Profile index currently shown in the email panel. */
let currentEmailIndex = -1;

/**
 * Open the email preview overlay for a given profile index.
 * @param {number} index - Index into currentProfiles
 */
function openEmailPanel(index) {
    const profile = currentProfiles[index];
    if (!profile || !profile.email_body) return;

    currentEmailIndex = index;
    const body = profile.email_body;

    // Parse subject line (first line) from the email body
    const lines = body.split('\n');
    let subject = '';
    let emailContent = body;

    // If the first line looks like "Subject: ..." extract it
    if (lines[0] && lines[0].toLowerCase().startsWith('subject:')) {
        subject = lines[0].replace(/^subject:\s*/i, '').trim();
        emailContent = lines.slice(1).join('\n').trim();
    } else {
        subject = lines[0] || 'Cold Outreach';
        emailContent = lines.slice(1).join('\n').trim();
    }

    dom.panelTo.textContent = profile.validated_email || profile.full_name;
    dom.panelSubject.textContent = subject;
    dom.panelBody.textContent = emailContent;
    dom.copyText.textContent = 'Copy to Clipboard';

    dom.emailOverlay.classList.add('open');
}

/** Close the email preview overlay. */
function closeEmailPanel() {
    dom.emailOverlay.classList.remove('open');
    currentEmailIndex = -1;
}

/** Copy the current email body to the clipboard. */
async function copyEmailToClipboard() {
    if (currentEmailIndex < 0) return;
    const profile = currentProfiles[currentEmailIndex];
    if (!profile || !profile.email_body) return;

    try {
        await navigator.clipboard.writeText(profile.email_body);
        dom.copyText.textContent = 'Copied!';
        showToast('Email copied to clipboard');
        setTimeout(() => {
            dom.copyText.textContent = 'Copy to Clipboard';
        }, 2000);
    } catch (err) {
        showToast('Failed to copy — check browser permissions');
    }
}


/* ═══════════════════════════════════════════════════════════════════════════
   Search Pipeline (SSE)
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Submit the search form and open an SSE connection to stream results.
 * @param {Event} event - Form submit event
 */
async function handleSearch(event) {
    event.preventDefault();

    const company    = dom.inputCompany.value.trim();
    const title      = dom.inputTitle.value.trim();
    const domain     = dom.inputDomain.value.trim();
    const maxResults = parseInt(dom.inputMaxResults.value, 10) || 10;
    const dryRun     = dom.inputDryRun.checked;

    // Validate
    if (!company || !title) {
        showToast('Please enter both a company and job title');
        return;
    }

    // Abort any in-flight stream
    if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
    }

    // Reset UI
    setSearchLoading(true);
    resetStepper();
    dom.pipelineSection.style.display = '';
    dom.pipelineSection.classList.add('fade-in');
    dom.resultsSection.style.display = 'none';
    dom.emptyState.style.display = 'none';
    dom.btnDownload.disabled = true;
    dom.resultsBody.innerHTML = '';
    currentProfiles = [];

    try {
        // We use fetch + ReadableStream because SSE with POST requires it
        const response = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company, title, domain, max_results: maxResults, dry_run: dryRun }),
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({ error: 'Unknown error' }));
            throw new Error(err.error || `HTTP ${response.status}`);
        }

        // Read the SSE stream
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            // Parse SSE messages (format: "event: type\ndata: json\n\n")
            const messages = buffer.split('\n\n');
            buffer = messages.pop(); // Keep incomplete message in buffer

            for (const msg of messages) {
                if (!msg.trim()) continue;
                const eventMatch = msg.match(/^event:\s*(.+)/m);
                const dataMatch  = msg.match(/^data:\s*(.+)/m);

                if (!eventMatch || !dataMatch) continue;

                const eventType = eventMatch[1].trim();
                let eventData;
                try {
                    eventData = JSON.parse(dataMatch[1]);
                } catch {
                    continue;
                }

                handleSSEEvent(eventType, eventData);
            }
        }

    } catch (error) {
        showToast(`Error: ${error.message}`);
        console.error('Pipeline error:', error);
    } finally {
        setSearchLoading(false);
    }
}

/**
 * Handle a single SSE event from the pipeline.
 * @param {string} type - Event type (step, profiles, complete, error, etc.)
 * @param {Object} data - Parsed event data
 */
function handleSSEEvent(type, data) {
    switch (type) {
        case 'step':
            updateStep(data.step, data.status, data.message);
            break;

        case 'profiles':
            // Show the results section and render profiles
            dom.resultsSection.style.display = '';
            dom.resultsSection.classList.add('fade-in');
            renderResults(data.profiles);
            break;

        case 'validation_progress':
            updateStep(2, 'running',
                `Validating ${data.index + 1}/${data.total}: ${data.name}`);
            break;

        case 'draft_progress':
            updateStep(3, 'running',
                `Drafting ${data.index + 1}/${data.total}: ${data.name}`);
            break;

        case 'complete':
            dom.btnDownload.disabled = false;
            showToast(data.message || 'Pipeline complete!');

            // Re-render with final data if available
            if (data.dry_run) {
                dom.btnDownload.disabled = true;
            }
            break;

        case 'error':
            showToast(`Error: ${data.message}`, 5000);
            break;

        default:
            break;
    }
}

/**
 * Toggle the search button between normal and loading states.
 * @param {boolean} loading - Whether the pipeline is running
 */
function setSearchLoading(loading) {
    if (loading) {
        dom.btnSearch.classList.add('loading');
        dom.btnSearch.disabled = true;
    } else {
        dom.btnSearch.classList.remove('loading');
        dom.btnSearch.disabled = false;
    }
}


/* ═══════════════════════════════════════════════════════════════════════════
   CSV Download
   ═══════════════════════════════════════════════════════════════════════════ */

/** Trigger a download of the outreach CSV. */
function downloadCSV() {
    window.open('/api/download', '_blank');
}


/* ═══════════════════════════════════════════════════════════════════════════
   Event Listeners
   ═══════════════════════════════════════════════════════════════════════════ */
dom.searchForm.addEventListener('submit', handleSearch);
dom.btnClosePanel.addEventListener('click', closeEmailPanel);
dom.btnCopyEmail.addEventListener('click', copyEmailToClipboard);
dom.btnDownload.addEventListener('click', downloadCSV);

// Close overlay on backdrop click
dom.emailOverlay.addEventListener('click', (e) => {
    if (e.target === dom.emailOverlay) closeEmailPanel();
});

// Close overlay on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeEmailPanel();
});
