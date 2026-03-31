/**
 * app.js — Client-side logic for the Job Search Agent UI.
 *
 * Handles:
 *   - Form submission → SSE stream from /api/search
 *   - Pipeline stepper progress updates
 *   - Results card grid rendering (real-time)
 *   - Gmail compose integration (opens pre-filled drafts in Gmail)
 *   - CSV download
 *   - Toast notifications
 */

/* ═══════════════════════════════════════════════════════════════════════════
   DOM References
   ═══════════════════════════════════════════════════════════════════════════ */
const dom = {
    // Form
    searchForm: document.getElementById('search-form'),
    inputCompany: document.getElementById('input-company'),
    inputTitle: document.getElementById('input-title'),
    inputDomain: document.getElementById('input-domain'),
    inputMaxResults: document.getElementById('input-max-results'),
    inputDryRun: document.getElementById('input-dry-run'),
    btnSearch: document.getElementById('btn-search'),

    // Pipeline stepper
    pipelineSection: document.getElementById('pipeline-section'),
    pipelineStepper: document.getElementById('pipeline-stepper'),

    // Results
    resultsSection: document.getElementById('results-section'),
    resultsBody: document.getElementById('results-body'),
    resultsCount: document.getElementById('results-count'),

    // Empty state
    emptyState: document.getElementById('empty-state'),

    // Email panel (removed — Gmail compose opens directly)

    // Download
    btnDownload: document.getElementById('btn-download'),

    // Toast
    toast: document.getElementById('toast'),
    toastMessage: document.getElementById('toast-message'),
};


/* ═══════════════════════════════════════════════════════════════════════════
   State
   ═══════════════════════════════════════════════════════════════════════════ */
let currentProfiles = [];
let activeEventSource = null;


/* ═══════════════════════════════════════════════════════════════════════════
   Toast Notifications
   ═══════════════════════════════════════════════════════════════════════════ */

function showToast(message, duration = 2500) {
    dom.toastMessage.textContent = message;
    dom.toast.classList.add('show');
    setTimeout(() => dom.toast.classList.remove('show'), duration);
}


/* ═══════════════════════════════════════════════════════════════════════════
   Pipeline Stepper
   ═══════════════════════════════════════════════════════════════════════════ */

function resetStepper() {
    for (let i = 1; i <= 4; i++) {
        const stepEl = dom.pipelineStepper.querySelector(`[data-step="${i}"]`);
        stepEl.classList.remove('running', 'done');
        document.getElementById(`step-msg-${i}`).textContent = 'Waiting...';
    }
}

function updateStep(stepNum, status, message) {
    const stepEl = dom.pipelineStepper.querySelector(`[data-step="${stepNum}"]`);
    if (!stepEl) return;

    stepEl.classList.remove('running', 'done');
    stepEl.classList.add(status);

    const msgEl = document.getElementById(`step-msg-${stepNum}`);
    if (msgEl) msgEl.textContent = message;
}


/* ═══════════════════════════════════════════════════════════════════════════
   Results — Card Grid
   ═══════════════════════════════════════════════════════════════════════════ */

function renderResults(profiles) {
    currentProfiles = profiles;
    dom.resultsBody.innerHTML = '';

    profiles.forEach((profile, index) => {
        const card = document.createElement('div');
        card.classList.add('profile-card', 'fade-in');
        card.style.animationDelay = `${index * 60}ms`;

        // Initials avatar
        const initials = ((profile.first_name?.[0] || '') + (profile.last_name?.[0] || '')).toUpperCase() || '?';

        // Email display with confidence badge
        const confidence = profile.email_confidence || '';
        let confidenceBadge = '';
        if (confidence === 'found') {
            confidenceBadge = '<span class="conf-badge conf-badge--found" title="Found on the public web">✓ Found</span>';
        } else if (confidence === 'verified') {
            confidenceBadge = '<span class="conf-badge conf-badge--verified" title="SMTP verified">✓ Verified</span>';
        } else if (confidence === 'likely') {
            confidenceBadge = '<span class="conf-badge conf-badge--likely" title="Catch-all domain — likely correct">~ Likely</span>';
        } else if (confidence === 'guessed') {
            confidenceBadge = '<span class="conf-badge conf-badge--guessed" title="Best guess — not verified">? Guessed</span>';
        }

        const emailHtml = profile.validated_email
            ? `<span class="card-email">${profile.validated_email}</span>${confidenceBadge}`
            : `<span class="card-email card-email--pending">Pending…</span>`;

        // LinkedIn link
        const linkedinHtml = profile.profile_url
            ? `<a href="${profile.profile_url}" target="_blank" rel="noopener noreferrer" class="card-link" title="View LinkedIn">
                   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                       <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                       <polyline points="15 3 21 3 21 9"></polyline>
                       <line x1="10" y1="14" x2="21" y2="3"></line>
                   </svg>
                   LinkedIn
               </a>`
            : '';

        // Gmail button — opens Gmail compose with pre-filled email
        const gmailHtml = (profile.email_body && profile.validated_email)
            ? `<button class="card-btn card-btn--gmail" data-index="${index}">
                   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                       <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path>
                       <polyline points="22,6 12,13 2,6"></polyline>
                   </svg>
                   Email
               </button>`
            : `<button class="card-btn card-btn--gmail" disabled>No Email</button>`;

        // Copy button — copies drafted email to clipboard
        const copyHtml = profile.email_body
            ? `<button class="card-btn card-btn--copy" data-index="${index}">
                   <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                       <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                       <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                   </svg>
                   Copy
               </button>`
            : '';

        card.innerHTML = `
            <div class="card-top">
                <div class="card-avatar">${initials}</div>
                <div class="card-info">
                    <p class="card-name">${profile.full_name || '—'}</p>
                    <p class="card-role">${profile.job_title || '—'} <span class="card-at">at</span> ${profile.company || '—'}</p>
                </div>
            </div>
            <div class="card-details">
                ${emailHtml}
            </div>
            <div class="card-actions">
                ${linkedinHtml}
                ${gmailHtml}
                ${copyHtml}
            </div>
        `;

        // Wire up Gmail button click
        const gmailBtn = card.querySelector('.card-btn--gmail:not([disabled])');
        if (gmailBtn) {
            gmailBtn.addEventListener('click', () => openGmailCompose(index));
        }

        // Wire up Copy button click
        const copyBtn = card.querySelector('.card-btn--copy');
        if (copyBtn) {
            copyBtn.addEventListener('click', () => copyEmailToClipboard(index, copyBtn));
        }

        dom.resultsBody.appendChild(card);
    });

    dom.resultsCount.textContent = `${profiles.length} found`;
}


/* ═══════════════════════════════════════════════════════════════════════════
   Gmail Compose — opens Gmail with pre-filled To, Subject, and Body
   ═══════════════════════════════════════════════════════════════════════════ */

function _parseEmailParts(rawBody) {
    const lines = rawBody.split('\n');
    let subject = '';
    let body = rawBody;

    if (lines[0] && lines[0].toLowerCase().startsWith('subject:')) {
        subject = lines[0].replace(/^subject:\s*/i, '').trim();
        body = lines.slice(1).join('\n').trim();
    } else {
        subject = lines[0] || 'Quick question';
        body = lines.slice(1).join('\n').trim();
    }
    return { subject, body };
}

function openGmailCompose(index) {
    const profile = currentProfiles[index];
    if (!profile || !profile.email_body || !profile.validated_email) return;

    const { subject, body } = _parseEmailParts(profile.email_body);

    // Build Gmail compose URL
    const gmailUrl = 'https://mail.google.com/mail/?view=cm'
        + '&to='  + encodeURIComponent(profile.validated_email)
        + '&su='  + encodeURIComponent(subject)
        + '&body=' + encodeURIComponent(body);

    window.open(gmailUrl, '_blank');
    showToast(`Opening Gmail for ${profile.full_name}`);
}

async function copyEmailToClipboard(index, btnEl) {
    const profile = currentProfiles[index];
    if (!profile || !profile.email_body) return;

    try {
        await navigator.clipboard.writeText(profile.email_body);
        const origHtml = btnEl.innerHTML;
        btnEl.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg> Copied`;
        showToast('Email copied to clipboard');
        setTimeout(() => { btnEl.innerHTML = origHtml; }, 2000);
    } catch {
        showToast('Failed to copy — check browser permissions');
    }
}


/* ═══════════════════════════════════════════════════════════════════════════
   Search Pipeline (SSE)
   ═══════════════════════════════════════════════════════════════════════════ */

async function handleSearch(event) {
    event.preventDefault();

    const company = dom.inputCompany.value.trim();
    const title = dom.inputTitle.value.trim();
    const domain = dom.inputDomain.value.trim();
    const maxResults = parseInt(dom.inputMaxResults.value, 10) || 10;
    const dryRun = dom.inputDryRun.checked;

    if (!company || !title) {
        showToast('Please enter both a company and job title');
        return;
    }

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
        const response = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ company, title, domain, max_results: maxResults, dry_run: dryRun }),
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({ error: 'Unknown error' }));
            throw new Error(err.error || `HTTP ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            const messages = buffer.split('\n\n');
            buffer = messages.pop();

            for (const msg of messages) {
                if (!msg.trim()) continue;
                const eventMatch = msg.match(/^event:\s*(.+)/m);
                const dataMatch = msg.match(/^data:\s*(.+)/m);
                if (!eventMatch || !dataMatch) continue;

                const eventType = eventMatch[1].trim();
                let eventData;
                try { eventData = JSON.parse(dataMatch[1]); } catch { continue; }
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

function handleSSEEvent(type, data) {
    switch (type) {
        case 'step':
            updateStep(data.step, data.status, data.message);
            break;

        case 'profiles':
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

function downloadCSV() {
    window.open('/api/download', '_blank');
}


/* ═══════════════════════════════════════════════════════════════════════════
   Event Listeners
   ═══════════════════════════════════════════════════════════════════════════ */
dom.searchForm.addEventListener('submit', handleSearch);
dom.btnDownload.addEventListener('click', downloadCSV);
