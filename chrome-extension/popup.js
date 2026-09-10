// Popup script for Offerloop Chrome Extension
console.log('[Offerloop Popup] Loaded');

// API Configuration
const API_BASE_URL = 'https://www.offerloop.ai';

// Dev only: skips the sign-in gate so the screens can be worked on without
// logging in. Must stay false in any shipped build.
const DEV_BYPASS_AUTH = false;

// Shared job URL patterns — used by detectMode() and isJobUrl()
const JOB_URL_PATTERNS = [
  /linkedin\.com\/jobs\//,
  /boards\.greenhouse\.io\//,
  /jobs\.lever\.co\//,
  /\.myworkdayjobs\.com\//,
  /indeed\.com\/(viewjob|jobs)/,
  /handshake\.com\/.*jobs/,
  /joinhandshake\.com\/.*jobs/,
  /app\.joinhandshake\.com\/.*jobs/,
  /glassdoor\.com\/job-listing/,
  /ziprecruiter\.com\/jobs/,
  /wellfound\.com\/jobs/,
  /\/careers\//,
  /\/jobs\//
];

// Sanitize a string for use in filenames
function sanitizeFilename(str) {
  if (!str) return 'unknown';
  return str.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

// Fetch wrapper with timeout support
async function fetchWithTimeout(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(timeoutId);
    return response;
  } catch (err) {
    clearTimeout(timeoutId);
    if (err.name === 'AbortError') throw new Error(`Request timed out after ${timeoutMs}ms`);
    throw err;
  }
}

// Authenticated calls go through the background, which owns the token and
// retries once after a refresh on 401. Resolves {ok, status, data}; never
// throws on HTTP errors, only when the extension itself cannot reach the
// background.
async function apiCall(path, method = 'POST', body = undefined, timeoutMs = 90000) {
  const resp = await chrome.runtime.sendMessage({ action: 'apiRequest', path, method, body, timeoutMs });
  if (!resp) return { ok: false, status: 0, data: { error: 'No response from extension' } };
  return resp;
}

// Tab Detection and Switching
function detectMode(url) {
  // Contact mode - LinkedIn profiles only
  if (url && url.match(/linkedin\.com\/in\//)) {
    return 'contact';
  }

  // Company mode - a LinkedIn company page lists its people first
  if (url && url.match(/linkedin\.com\/company\//i)) {
    return 'company';
  }

  // Job mode - job posting URLs
  if (url) {
    for (const pattern of JOB_URL_PATTERNS) {
      if (url.match(pattern)) return 'job';
    }
  }
  
  return 'contact'; // Default to contact
}

function switchTab(tabName) {
  // Update tab buttons
  document.querySelectorAll('.tab').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.tab === tabName);
  });
  
  // Update tab content
  document.querySelectorAll('.tab-content').forEach(content => {
    content.classList.toggle('hidden', content.id !== `${tabName}-tab`);
  });
  
  // Initialize Job tab when switched to
  if (tabName === 'job') {
    initJobTab();
  }

  // Company tab: the list when we are on a company page, the hint otherwise.
  if (tabName === 'company') {
    updateCompanyHint();
  }
}

// The Company tab shows the people list only on a company page. Anywhere
// else (or signed out) it shows the "open a company page" hint instead.
function updateCompanyHint() {
  const hint = document.getElementById('companyHint');
  const list = document.getElementById('companySection');
  const onCompanyPage = !!currentState.companyPage && currentState.isLoggedIn;
  hint?.classList.toggle('hidden', onCompanyPage);
  list?.classList.toggle('hidden', !onCompanyPage);
}

function initTabSwitcher() {
  // Add click handlers for tabs
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });
}

// ============================================
// JOB TAB FUNCTIONALITY (URL-FIRST APPROACH)
// ============================================

let currentJobUrl = null;
let manualInputRequired = false;
let _actionInProgress = false;

// After the extension is reloaded in chrome://extensions, a side panel that
// was already open keeps its pixels but loses its extension context: every
// chrome.* call throws, the first click leaves the in-progress flag stuck,
// and every click after that silently does nothing. Say so instead.
function _panelAlive(showFn) {
  if (chrome.runtime && chrome.runtime.id) return true;
  try { showFn('Offerloop was updated. Close this panel and open it again.'); } catch (e) {}
  return false;
}

// Credits cache — avoid redundant API calls when popup reopens quickly
let _creditsCacheTime = 0;
const CREDITS_CACHE_TTL = 120000; // 2 minutes

// Initialize Job Tab when switched to
async function initJobTab() {
  console.log('[Offerloop Popup] Initializing Job tab...');

  // Get current tab URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const companyCtx = parseCompanyFromTab(tab);
  // A role picked from a company's open-roles list stays selected while the
  // tab is still sitting on that company's LinkedIn page: the tab's own URL
  // never becomes the posting's URL (that would need an actual navigation),
  // so it cannot drive the "same job" check the way a real job-board tab does.
  const keepingCompanyPick = !!(currentJob && currentJob.source === 'company-jobs'
    && companyCtx && currentJob._companySlug === companyCtx.slug);
  // Results belong to the posting. Same posting: leave them on screen.
  const sameJob = keepingCompanyPick || (!!currentJobUrl && tab.url === currentJobUrl);
  if (!sameJob) {
    hideAllJobResults();
    hideJobError();
    hideManualForm();
    hideJobLoading();
    currentJob = null;
  }
  if (!keepingCompanyPick) {
    currentJobUrl = tab.url;
    // What the job is, from the page or the tab title, without blocking.
    readJobHeading(tab);
  }
  
  // Update URL display
  const urlTextEl = document.getElementById('job-url-text');
  if (urlTextEl) {
    urlTextEl.textContent = truncateUrl(currentJobUrl);
  }
  
  // Check if we're on a supported job board
  if (isJobUrl(currentJobUrl)) {
    hideCompanyJobsSection();
    showJobStatus('Job URL detected. Click a button to proceed.');
    enableJobButtons();
    manualInputRequired = false;
  } else if (companyCtx) {
    hideManualForm();
    manualInputRequired = false;
    if (keepingCompanyPick) {
      hideCompanyJobsSection();
      showJobStatus(`Using "${currentJob.title || 'this role'}" at ${companyCtx.name}.`);
      showJobActionsBlock();
      enableJobButtons();
    } else {
      hideJobActionsBlock();
      showJobStatus(`Open roles at ${companyCtx.name}.`);
      loadCompanyJobs(companyCtx);
    }
  } else {
    hideCompanyJobsSection();
    showJobActionsBlock();
    showJobStatus('Not on a recognized job page. Please enter details manually.');
    showManualForm();
    manualInputRequired = true;
    updateJobButtonState();
  }
}

// ============================================
// JOB TAB: a company page's open roles
// ============================================
// On a LinkedIn company page with no specific posting open, the Job tab
// lists that company's roles from the jobs catalog (same one the app's
// Feed searches), the same idea as the Company tab's people list: browse
// free, then pick one to run Find recruiters or Generate cover letter on.

const COMPANY_JOBS_FIRST = 5;
const companyJobsState = { key: null, rows: [] };

function _relativeDate(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return '';
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return '1 day ago';
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return months === 1 ? '1 month ago' : `${months} months ago`;
}

function showJobActionsBlock() {
  document.getElementById('job-actions')?.classList.remove('hidden');
}
function hideJobActionsBlock() {
  document.getElementById('job-actions')?.classList.add('hidden');
}
function hideCompanyJobsSection() {
  document.getElementById('company-jobs-section')?.classList.add('hidden');
  document.getElementById('company-jobs-back')?.classList.add('hidden');
}

function buildCompanyJobRowEl(job) {
  const el = document.createElement('div');
  el.className = 'similar-row';
  el.setAttribute('role', 'listitem');

  const avatar = document.createElement('span');
  avatar.className = 'similar-row__avatar';
  avatar.textContent = (job.company || '?').trim().slice(0, 1).toUpperCase();

  const who = document.createElement('div');
  who.className = 'similar-row__who';
  const nameEl = document.createElement('span');
  nameEl.className = 'similar-row__name';
  nameEl.textContent = (job.title || 'Open role').toLowerCase(); // CSS capitalizes
  const metaEl = document.createElement('span');
  metaEl.className = 'similar-row__meta';
  let loc = job.location || '';
  if (loc && typeof loc === 'object') loc = Object.values(loc).filter(Boolean).join(', ');
  const metaParts = [loc, _relativeDate(job.posted_at)].filter(Boolean);
  metaEl.textContent = metaParts.join(' · ');
  who.append(nameEl, metaEl);
  if (job.auto_apply_eligible) {
    const cap = document.createElement('span');
    cap.className = 'similar-row__category';
    cap.textContent = 'Auto-apply eligible';
    who.append(cap);
  }
  el.append(avatar, who);

  // The posting itself, one tap away, same pattern as opening a person's
  // LinkedIn profile from a people row.
  const applyUrl = job.apply_url || job.url || '';
  if (applyUrl) {
    el.classList.add('similar-row--openable');
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'similar-row__open';
    open.title = 'View the posting';
    open.setAttribute('aria-label', 'View the posting');
    open.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"></polyline></svg>';
    const go = (ev) => { ev.preventDefault(); ev.stopPropagation(); chrome.tabs.create({ url: applyUrl }); };
    open.addEventListener('click', go);
    avatar.addEventListener('click', go);
    el.appendChild(open);
  }

  const use = document.createElement('button');
  use.type = 'button';
  use.className = 'similar-row__action';
  use.textContent = 'Use this posting';
  use.addEventListener('click', () => selectCompanyJob(job));
  const actions = document.createElement('div');
  actions.className = 'similar-row__actions';
  actions.appendChild(use);
  el.appendChild(actions);

  return el;
}

function renderCompanyJobRows(jobs) {
  const results = document.getElementById('company-jobs-rows');
  const empty = document.getElementById('company-jobs-empty');
  const details = document.getElementById('company-jobs-details');
  if (!results) return;
  results.innerHTML = '';
  if (!jobs.length) {
    empty?.classList.remove('hidden');
    if (details) details.textContent = '';
    return;
  }
  empty?.classList.add('hidden');
  if (details) details.textContent = `${jobs.length} open role${jobs.length === 1 ? '' : 's'} on file`;
  jobs.forEach((job, i) => {
    const el = buildCompanyJobRowEl(job);
    if (i >= COMPANY_JOBS_FIRST) el.classList.add('hidden', 'similar-row--folded');
    results.appendChild(el);
  });
  if (jobs.length > COMPANY_JOBS_FIRST) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'similar-more';
    more.textContent = `Show ${jobs.length - COMPANY_JOBS_FIRST} more`;
    more.addEventListener('click', () => {
      results.querySelectorAll('.similar-row--folded').forEach((el) => el.classList.remove('hidden', 'similar-row--folded'));
      more.remove();
    });
    results.appendChild(more);
  }
}

async function loadCompanyJobs(company) {
  const key = company.slug || company.name;
  companyJobsState.companyCtx = company;
  const section = document.getElementById('company-jobs-section');
  const loading = document.getElementById('company-jobs-loading');
  const loadingText = document.getElementById('company-jobs-loading-text');
  section?.classList.remove('hidden');
  document.getElementById('company-jobs-back')?.classList.add('hidden');

  // Same company as last time this session: the list does not change
  // often enough to justify a second call every time the student flips
  // back to this tab.
  if (companyJobsState.key === key && companyJobsState.rows.length) {
    renderCompanyJobRows(companyJobsState.rows);
    return;
  }
  companyJobsState.key = key;
  companyJobsState.rows = [];
  document.getElementById('company-jobs-rows').innerHTML = '';
  document.getElementById('company-jobs-empty')?.classList.add('hidden');
  loading?.classList.remove('hidden');
  const stopStages = startLoadingStages(loadingText, [
    'Checking the job catalog',
    'Matching postings to this company',
    'Almost there',
  ]);
  try {
    const response = await apiCall(`/api/jobs/search?company=${encodeURIComponent(company.apiName || company.name)}&limit=15`, 'GET', undefined, 20000);
    stopStages();
    loading?.classList.add('hidden');
    if (companyJobsState.key !== key) return; // the student moved on
    const rows = (response.ok && response.data && response.data.results) || [];
    companyJobsState.rows = rows;
    renderCompanyJobRows(rows);
  } catch (e) {
    stopStages();
    loading?.classList.add('hidden');
    if (companyJobsState.key === key) renderCompanyJobRows([]);
  }
}

function backToCompanyJobsList() {
  const company = companyJobsState.companyCtx;
  if (!company) return;
  currentJob = null;
  currentJobUrl = '';
  const urlTextEl = document.getElementById('job-url-text');
  if (urlTextEl) urlTextEl.textContent = '';
  hideJobActionsBlock();
  hideJobError();
  hideAllJobResults();
  document.getElementById('company-jobs-back')?.classList.add('hidden');
  document.getElementById('company-jobs-section')?.classList.remove('hidden');
  showJobStatus(`Open roles at ${company.name}.`);
  renderCompanyJobRows(companyJobsState.rows);
}

function selectCompanyJob(job) {
  const company = companyJobsState.companyCtx || null;
  currentJob = {
    title: job.title || '',
    company: job.company || (company && company.name) || '',
    description: '',
    source: 'company-jobs',
    _companySlug: company ? company.slug : null,
  };
  currentJobUrl = job.apply_url || job.url || '';
  const urlTextEl = document.getElementById('job-url-text');
  if (urlTextEl) urlTextEl.textContent = truncateUrl(currentJobUrl);
  _renderJobHeading();
  hideCompanyJobsSection();
  hideJobError();
  hideAllJobResults();
  showJobActionsBlock();
  enableJobButtons();
  manualInputRequired = false;
  showJobStatus(`Using "${job.title || 'this role'}" at ${currentJob.company || 'this company'}.`);
  document.getElementById('company-jobs-back')?.classList.remove('hidden');
}

function isJobUrl(url) {
  if (!url) return false;
  return JOB_URL_PATTERNS.some(pattern => pattern.test(url));
}

// ============================================
// JOB TAB UI HELPERS
// ============================================

function showJobStatus(message) {
  const statusEl = document.getElementById('job-status');
  const textEl = document.getElementById('job-status-text');
  if (textEl) textEl.textContent = message;
  if (statusEl) statusEl.classList.remove('hidden');
}

function hideJobStatus() {
  const statusEl = document.getElementById('job-status');
  if (statusEl) statusEl.classList.add('hidden');
}

function showManualForm(partialData = null) {
  const form = document.getElementById('manual-form');
  if (form) form.classList.remove('hidden');
  manualInputRequired = true;
  
  // Pre-fill with any partial data
  if (partialData) {
    const companyInput = document.getElementById('manual-company');
    const titleInput = document.getElementById('manual-job-title');
    const descInput = document.getElementById('manual-description');
    
    if (companyInput && partialData.company) companyInput.value = partialData.company;
    if (titleInput && partialData.jobTitle) titleInput.value = partialData.jobTitle;
    if (descInput && partialData.description) descInput.value = partialData.description;
  }
  
  updateJobButtonState();
}

function hideManualForm() {
  const form = document.getElementById('manual-form');
  if (form) form.classList.add('hidden');
  manualInputRequired = false;
}

function enableJobButtons() {
  const findBtn = document.getElementById('find-recruiters-btn');
  const coverBtn = document.getElementById('cover-letter-btn');

  if (findBtn) findBtn.disabled = false;
  if (coverBtn) coverBtn.disabled = false;
}

function updateJobButtonState() {
  const company = document.getElementById('manual-company')?.value.trim() || '';
  const jobTitle = document.getElementById('manual-job-title')?.value.trim() || '';
  const description = document.getElementById('manual-description')?.value.trim() || '';
  
  const findBtn = document.getElementById('find-recruiters-btn');
  const coverBtn = document.getElementById('cover-letter-btn');

  // Find Recruiters needs company
  if (findBtn) findBtn.disabled = !company;

  // Cover Letter needs description
  if (coverBtn) coverBtn.disabled = !description;
}

function getManualInputData() {
  return {
    company: document.getElementById('manual-company')?.value.trim() || '',
    jobTitle: document.getElementById('manual-job-title')?.value.trim() || '',
    jobDescription: document.getElementById('manual-description')?.value.trim() || '',
    jobUrl: currentJobUrl
  };
}

function showJobLoading(message, stages) {
  const loading = document.getElementById('job-loading');
  const text = document.getElementById('job-loading-text');
  if (loading) loading.classList.remove('hidden');
  if (text) {
    if (Array.isArray(stages) && stages.length) startLoadingStages(text, stages, 4000);
    else text.textContent = message || 'Working on it';
  }
}

function hideJobLoading() {
  const loading = document.getElementById('job-loading');
  if (loading) loading.classList.add('hidden');
}

function showJobError(message) {
  const errorDiv = document.getElementById('job-error');
  const errorMsg = document.getElementById('job-error-message');
  if (errorMsg) errorMsg.textContent = message;
  if (errorDiv) errorDiv.classList.remove('hidden');
}

function showJobErrorWithSignin(message) {
  const errorDiv = document.getElementById('job-error');
  const errorMsg = document.getElementById('job-error-message');
  if (errorMsg) {
    errorMsg.textContent = message + ' ';
    const link = document.createElement('a');
    link.href = '#';
    link.textContent = 'Sign in';
    link.style.cssText = 'color:#2563EB;text-decoration:underline;cursor:pointer;';
    link.addEventListener('click', (e) => { e.preventDefault(); handleLogin(); });
    errorMsg.appendChild(link);
  }
  if (errorDiv) errorDiv.classList.remove('hidden');
}

function hideJobError() {
  const errorDiv = document.getElementById('job-error');
  if (errorDiv) errorDiv.classList.add('hidden');
}

function hideAllJobResults() {
  document.getElementById('job-results')?.classList.add('hidden');
  document.getElementById('cover-letter-results')?.classList.add('hidden');
}

function truncateUrl(url) {
  try {
    const parsed = new URL(url);
    let path = parsed.pathname;
    if (path.length > 30) {
      path = path.substring(0, 30) + '...';
    }
    return parsed.hostname + path;
  } catch {
    return url ? url.substring(0, 40) + '...' : 'Unknown URL';
  }
}

// ============================================
// FIND & EMAIL RECRUITERS (URL-FIRST)
// ============================================

// ============================================
// GENERATE COVER LETTER (URL-FIRST + DOWNLOAD)
// ============================================

function _coverLetterText(cl) {
  if (!cl) return '';
  if (typeof cl === 'string') return cl.trim();
  if (typeof cl === 'object') {
    const direct = cl.content || cl.text || cl.letter || cl.body || cl.coverLetter || cl.cover_letter || cl.message;
    if (typeof direct === 'string' && direct.trim()) return direct.trim();
    for (const key of Object.keys(cl)) {
      if (typeof cl[key] === 'string' && cl[key].length > 100) return cl[key].trim();
    }
  }
  return '';
}

function showCoverLetterResults(data, company, jobTitle) {
  const resultsDiv = document.getElementById('cover-letter-results');
  const detailsDiv = document.getElementById('cover-letter-details');
  const downloadLink = document.getElementById('cover-letter-download-link');
  
  // Extract company/title from various possible locations
  let companyName = company || data.company || data.companyName;
  let title = jobTitle || data.jobTitle || data.job_title || data.title;
  // Remembered for the Company tab's chips: arriving at a company page from
  // a posting is the common path, and the team you would join is the ask.
  if (title) currentState.lastJobTitle = title;
  
  // Also check inside coverLetter object if it exists
  if (data.coverLetter && typeof data.coverLetter === 'object') {
    companyName = companyName || data.coverLetter.company || data.coverLetter.companyName;
    title = title || data.coverLetter.jobTitle || data.coverLetter.job_title;
  }
  
  if (detailsDiv) {
    detailsDiv.textContent = `Cover letter for ${title || 'Position'} at ${companyName || 'Company'}`;
  }
  // The letter itself, readable in place (2026-09-08). The PDF stays one tap away.
  const preview = document.getElementById('cover-letter-preview');
  if (preview) {
    const text = _coverLetterText(data.coverLetter);
    preview.textContent = text;
    preview.classList.toggle('hidden', !text);
  }
  
  // Store cover letter data for re-download
  if (downloadLink && data.coverLetter) {
    downloadLink.onclick = (e) => {
      e.preventDefault();
      downloadCoverLetterAsPDF(data.coverLetter, companyName, title);
    };
  } else if (downloadLink && data.pdfUrl) {
    downloadLink.href = data.pdfUrl;
    downloadLink.onclick = null;
  }
  
  if (resultsDiv) resultsDiv.classList.remove('hidden');
}

function hideCoverLetterResults() {
  const resultsDiv = document.getElementById('cover-letter-results');
  if (resultsDiv) resultsDiv.classList.add('hidden');
}

async function downloadCoverLetterAsPDF(coverLetterData, company, jobTitle) {
  // Handle different response formats
  let text = '';
  
  if (typeof coverLetterData === 'string') {
    // Already a string
    text = coverLetterData;
  } else if (typeof coverLetterData === 'object' && coverLetterData !== null) {
    // It's an object - extract the text content
    // Try common field names
    text = coverLetterData.content 
        || coverLetterData.text 
        || coverLetterData.letter 
        || coverLetterData.body
        || coverLetterData.coverLetter
        || coverLetterData.cover_letter
        || coverLetterData.message
        || '';
    
    // If still empty, try to find any long string value in the object
    if (!text) {
      for (const key of Object.keys(coverLetterData)) {
        if (typeof coverLetterData[key] === 'string' && coverLetterData[key].length > 100) {
          text = coverLetterData[key];
          console.log('[Offerloop Popup] Found cover letter text in field:', key);
          break;
        }
      }
    }
    
    // Last resort - JSON stringify (but this shouldn't happen)
    if (!text) {
      console.error('[Offerloop Popup] Could not extract cover letter text from:', coverLetterData);
      text = JSON.stringify(coverLetterData, null, 2);
    }
    
    // Also extract company/title from response if not provided
    if (!company) {
      company = coverLetterData.company || coverLetterData.companyName || 'unknown';
    }
    if (!jobTitle) {
      jobTitle = coverLetterData.jobTitle || coverLetterData.job_title || coverLetterData.title || 'unknown';
    }
  }
  
  if (!text) {
    console.error('[Offerloop Popup] No cover letter text found');
    showJobError('Cover letter generated but could not extract text');
    return;
  }
  
  console.log('[Offerloop Popup] Generating PDF for cover letter, text length:', text.length);
  
  // Get auth token
  const authData = await chrome.storage.local.get(['authToken']);
  if (!DEV_BYPASS_AUTH && !authData.authToken) {
    console.error('[Offerloop Popup] No auth token found');
    showJobError('Please log in to download cover letter');
    return;
  }
  
  try {
    // Call backend to generate PDF
    // This one stays a direct fetch because the reply is a PDF blob, which
    // cannot travel over the message bridge. A 401 refreshes and retries once.
    const pdfFetch = (token) => fetchWithTimeout(`${API_BASE_URL}/api/job-board/cover-letter-pdf`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`
      },
      body: JSON.stringify({ content: text, company: company })
    }, 60000);
    let response = await pdfFetch(authData.authToken);
    if (response.status === 401) {
      const fresh = await chrome.runtime.sendMessage({ action: 'refreshToken' }).catch(() => null);
      if (fresh && fresh.token) response = await pdfFetch(fresh.token);
    }
    
    if (!response.ok) {
      throw new Error(`PDF generation failed: ${response.status}`);
    }
    
    // Get PDF blob
    const pdfBlob = await response.blob();
    
    // Create filename: companyname_cover_letter.pdf
    const filename = company ? `${sanitizeFilename(company)}_cover_letter.pdf` : 'cover_letter.pdf';
    
    // Create object URL and download
    const url = URL.createObjectURL(pdfBlob);
    
    chrome.downloads.download({
      url: url,
      filename: filename,
      saveAs: false
    }, (downloadId) => {
      if (chrome.runtime.lastError) {
        console.error('[Offerloop Popup] Cover letter PDF download error:', chrome.runtime.lastError);
        // Fallback: copy text to clipboard
        navigator.clipboard.writeText(text).then(() => {
          showJobError('PDF download failed, but cover letter copied to clipboard!');
        }).catch(() => {
          // Last resort: open PDF in new tab
          chrome.tabs.create({ url: url });
        });
      } else {
        console.log('[Offerloop Popup] Cover letter PDF downloaded:', filename);
      }
      // Revoke the blob URL after a delay
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  } catch (error) {
    console.error('[Offerloop Popup] Error generating PDF:', error);
    // Fallback: download as text file
    const filename = `cover-letter-${sanitizeFilename(company)}-${sanitizeFilename(jobTitle)}.txt`;
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    chrome.downloads.download({
      url: url,
      filename: filename,
      saveAs: false
    });
    showJobError('PDF generation failed, downloaded as text file instead');
  }
}

function triggerCoverLetterDownload(pdfUrl, company, jobTitle) {
  // For future PDF support
  const filename = company ? `${sanitizeFilename(company)}_cover_letter.pdf` : 'cover_letter.pdf';
  
  chrome.downloads.download({
    url: pdfUrl,
    filename: filename,
    saveAs: false
  }, (downloadId) => {
    if (chrome.runtime.lastError) {
      console.error('[Offerloop Popup] Cover letter PDF download error:', chrome.runtime.lastError);
      chrome.tabs.create({ url: pdfUrl });
    } else {
      console.log('[Offerloop Popup] Cover letter PDF download started:', downloadId);
    }
  });
}

// ============================================
// JOB TAB EVENT LISTENERS
// ============================================

// ============================================
// JOB TAB, 2026-09-08 rebuild: everything happens in the panel.
// The page tells us what the job is (content script, else the tab title),
// the server re-reads the posting from its URL, recruiters render as rows
// with the same email and draft actions as everywhere else, and the cover
// letter shows in place with a Download button. One footnote says it is
// all tracked in Offerloop; nothing bounces the student to the website.
// ============================================

let currentJob = null; // { title, company, description, source }

function _tabTitleJob(tab) {
  // LinkedIn: "(25) Compensation Business Partner | Snap Inc. | LinkedIn"
  const t = (tab && tab.title || '').replace(/^\(\d+\)\s*/, '').trim();
  const parts = t.split(/\s*\|\s*/).map((s) => s.trim()).filter(Boolean);
  if (parts.length >= 3 && /linkedin/i.test(parts[parts.length - 1])) {
    return { title: parts[0], company: parts[1] };
  }
  // Greenhouse / Lever / Ashby: "Job Title - Company" or "Company - Job Title"
  const dash = t.split(/\s+[-–]\s+/).map((s) => s.trim()).filter(Boolean);
  if (dash.length === 2) return { title: dash[0], company: dash[1] };
  return { title: '', company: '' };
}

function _renderJobHeading() {
  const wrap = document.getElementById('job-heading');
  const t = document.getElementById('job-heading-title');
  const c = document.getElementById('job-heading-company');
  if (!wrap || !t || !c) return;
  const has = currentJob && (currentJob.title || currentJob.company);
  wrap.classList.toggle('hidden', !has);
  if (!has) return;
  t.textContent = currentJob.title || 'This posting';
  c.textContent = currentJob.company ? `at ${currentJob.company}` : '';
  if (currentJob.title) currentState.lastJobTitle = currentJob.title;
}

async function readJobHeading(tab) {
  const fromTitle = _tabTitleJob(tab);
  currentJob = { ...(currentJob || {}), ...fromTitle, source: 'title' };
  _renderJobHeading();
  // The page itself, when the content script is there. Never blocks.
  try {
    const res = await Promise.race([
      chrome.tabs.sendMessage(tab.id, { action: 'scrapeJob' }),
      new Promise((resolve) => setTimeout(() => resolve(null), 3000)),
    ]);
    const adopt = (r) => {
      if (!r || !(r.jobTitle || r.title || r.company)) return;
      currentJob = {
        title: r.jobTitle || r.title || currentJob.title || '',
        company: r.company || currentJob.company || '',
        description: r.description || r.jobDescription || '',
        source: 'page',
      };
      _renderJobHeading();
    };
    adopt(res);
    // LinkedIn's job list swaps the open posting in a beat after the URL
    // changes; a second read catches the one the student actually clicked.
    const url = currentJobUrl;
    setTimeout(async () => {
      if (currentJobUrl !== url) return;
      try { adopt(await chrome.tabs.sendMessage(tab.id, { action: 'scrapeJob' })); } catch (e) { /* same as above */ }
    }, 1800);
  } catch (e) { /* content script not on this page yet; the title carries it */ }
}

// LinkedIn's search page carries the open posting as ?currentJobId=. The
// server fetches the URL it is given and reads the posting's own JSON-LD;
// handed the search page it gets the search page. Send the posting.
function _canonicalJobUrl(url) {
  try {
    const u = new URL(url || '');
    if (/linkedin\.com$/i.test(u.hostname) && /\/jobs\//.test(u.pathname)) {
      const id = u.searchParams.get('currentJobId');
      if (id && /^\d+$/.test(id)) return `https://www.linkedin.com/jobs/view/${id}/`;
      const m = u.pathname.match(/\/jobs\/view\/(\d+)/);
      if (m) return `https://www.linkedin.com/jobs/view/${m[1]}/`;
    }
  } catch (e) { /* not a URL */ }
  return url;
}

// After a server call, the heading shows the posting the server actually
// read (companyCleaned, jobTypeDetected / jobTitle), so the card above and
// the people below never disagree.
function _adoptServerJob(result) {
  if (!result) return;
  const company = result.companyCleaned || result.company || '';
  const title = result.jobTitle || result.jobTitleDetected || '';
  if (!company && !title) return;
  currentJob = { ...(currentJob || {}), company: company || (currentJob && currentJob.company) || '',
                 title: title || (currentJob && currentJob.title) || '', source: 'server' };
  _renderJobHeading();
}

function _jobRequestBody() {
  // Manual form wins when the page is not a posting we recognise.
  if (manualInputRequired) {
    const data = getManualInputData();
    return {
      company: data.company || undefined,
      jobTitle: data.jobTitle || undefined,
      jobDescription: data.jobDescription || undefined,
      jobUrl: data.jobUrl || undefined,
    };
  }
  const j = currentJob || {};
  // The URL is what the server parses (it reads LinkedIn postings fine);
  // the page's own words ride along so nothing is lost if parsing is thin.
  // Title and company ride along only when the page itself said them. The
  // tab title lags a click in LinkedIn's job list, and a stale company
  // name from it beat the server's own parse of the posting (a Favored
  // heading over Pink Sparrow recruiters, 2026-09-08).
  const fromPage = j.source === 'page' || j.source === 'company-jobs';
  return {
    jobUrl: _canonicalJobUrl(currentJobUrl),
    jobTitle: (fromPage && j.title) || undefined,
    company: (fromPage && j.company) || undefined,
    jobDescription: (j.description && j.description.length > 100) ? j.description : undefined,
  };
}

async function handleFindRecruiters() {
  if (_actionInProgress) return;
  if (!_panelAlive(showJobError)) return;
  _actionInProgress = true;
  const btn = document.getElementById('find-recruiters-btn');
  const authData = await chrome.storage.local.get(['authToken']);
  if (!DEV_BYPASS_AUTH && !authData.authToken) {
    _actionInProgress = false;
    showJobErrorWithSignin('Please sign in to use this feature.');
    return;
  }
  const requestBody = _jobRequestBody();
  if (manualInputRequired && !requestBody.company) {
    _actionInProgress = false;
    showJobError('Company name is required');
    return;
  }
  btn?.classList.add('loading');
  if (btn) btn.disabled = true;
  hideJobError();
  hideAllJobResults();
  showJobLoading('', [
    'Reading the posting',
    'Finding who recruits for this team',
    'Checking who is already on file',
    'Looking up their emails',
    'Writing your drafts',
    'Almost there',
  ]);
  try {
    const response = await apiCall('/api/job-board/find-recruiter', 'POST', requestBody, 120000);
    const result = response.data || {};
    if (response.ok && (result.success || result.recruiters)) {
      const recruiters = result.recruiters || [];
      _adoptServerJob(result);
      if (recruiters.length) {
        // Tracker save is a side effect; it never blocks the rows.
        apiCall('/api/job-board/save-recruiters', 'POST', {
          recruiters,
          draftsCreated: result.draftsCreated || [],
          companyCleaned: result.companyCleaned || '',
          associatedJobUrl: currentJobUrl || '',
          associatedJobTitle: (currentJob && currentJob.title) || result.jobTypeDetected || '',
          jobTitle: (currentJob && currentJob.title) || result.jobTypeDetected || '',
        }, 30000).catch(() => {});
      }
      renderRecruiterResults(result);
      if (result.creditsRemaining !== undefined) {
        updateCredits(result.creditsRemaining);
        _creditsCacheTime = Date.now();
      }
    } else if (result.needsManualInput || (result.error || '').match(/extract|company/i)) {
      showJobError('Could not read this posting. Add the company below.');
      showManualForm(result.partialData);
    } else {
      showJobError(result.error || 'Could not find recruiters. Please try again.');
    }
  } catch (error) {
    console.error('[Offerloop Popup] Find recruiters error:', error);
    showJobError('Something went wrong. Please try again.');
  } finally {
    hideJobLoading();
    btn?.classList.remove('loading');
    if (btn) btn.disabled = false;
    _actionInProgress = false;
  }
}

function renderRecruiterResults(result) {
  const card = document.getElementById('job-results');
  const details = document.getElementById('job-result-details');
  const list = document.getElementById('job-recruiter-rows');
  if (!card || !list) return;
  const recruiters = result.recruiters || [];
  const company = result.companyCleaned || (currentJob && currentJob.company) || '';
  const drafts = new Map();
  (result.draftsCreated || []).forEach((d) => {
    const key = (d.recruiter_email || d.email || '').toLowerCase();
    if (key) drafts.set(key, d.draft_url || d.draftUrl || '');
  });
  if (details) {
    const n = recruiters.length;
    details.textContent = n
      ? `${n} ${n === 1 ? 'person' : 'people'} who recruit${n === 1 ? 's' : ''} for this team${company ? ` at ${company}` : ''}`
      : 'Nobody on file for this team yet.';
  }
  list.innerHTML = '';
  recruiters.forEach((r) => {
    const email = (r.Email || r.email || '').trim();
    const row = {
      firstName: r.FirstName || r.firstName || '',
      lastName: r.LastName || r.lastName || '',
      title: r.Title || r.title || 'Recruiter',
      company: r.Company || r.company || company,
      linkedinUrl: r.LinkedIn || r.linkedin || r.linkedinUrl || '',
      school: r.College || r.college || '',
      email: email && email.toLowerCase() !== 'not available' ? email : '',
      caption: '',
      category: '',
    };
    const el = buildSimilarRowEl(row);
    const draftUrl = row.email ? drafts.get(row.email.toLowerCase()) : '';
    if (draftUrl) {
      // A draft already exists for this person: swap the Draft button for Open email.
      const btn = el.querySelector('.similar-row__action--draft');
      if (btn) {
        btn.textContent = 'Open email';
        const fresh = btn.cloneNode(true);
        btn.replaceWith(fresh);
        fresh.addEventListener('click', () => chrome.tabs.create({ url: draftUrl }));
      }
    }
    list.appendChild(el);
    // Into the tray as well (2026-09-08). The next action on this posting
    // (a cover letter, say) clears the result card, and the people you just
    // paid to find were gone from view. In the tray they stay reachable
    // with the same Write email / Open email actions.
    const displayName = `${row.firstName} ${row.lastName}`.trim() || row.title;
    row.trayId = `recruiter:${(row.email || row.linkedinUrl || displayName).toLowerCase()}`;
    trayUpsert({
      id: row.trayId,
      kind: 'email',
      status: row.email ? 'found' : 'miss',
      statusText: row.email ? '' : 'No email on file',
      name: displayName,
      company: row.company || '',
      linkedInUrl: row.linkedinUrl || '',
      email: row.email || '',
      draftUrl: draftUrl || '',
      person: { firstName: row.firstName, lastName: row.lastName, jobTitle: row.title,
                company: row.company, linkedinUrl: row.linkedinUrl, email: row.email },
    });
  });
  card.classList.remove('hidden');
}

async function handleGenerateCoverLetter() {
  if (_actionInProgress) return;
  if (!_panelAlive(showJobError)) return;
  _actionInProgress = true;
  const btn = document.getElementById('cover-letter-btn');
  const authData = await chrome.storage.local.get(['authToken']);
  if (!DEV_BYPASS_AUTH && !authData.authToken) {
    _actionInProgress = false;
    showJobErrorWithSignin('Please sign in to use this feature.');
    return;
  }
  const requestBody = _jobRequestBody();
  if (manualInputRequired && !requestBody.jobDescription) {
    _actionInProgress = false;
    showJobError('Paste the job description below so the letter has something to say.');
    return;
  }
  const company = requestBody.company || '';
  const jobTitle = requestBody.jobTitle || '';
  btn?.classList.add('loading');
  if (btn) btn.disabled = true;
  hideJobError();
  hideAllJobResults();
  showJobLoading('', [
    'Reading the posting',
    'Matching it against your resume',
    'Writing the letter',
    'Polishing the opening',
    'Almost there',
  ]);
  try {
    const response = await apiCall('/api/job-board/generate-cover-letter', 'POST', requestBody, 120000);
    const result = response.data || {};
    if (response.ok && (result.coverLetter || result.pdfUrl)) {
      _adoptServerJob(result);
      showCoverLetterResults(result, result.company || company, result.jobTitle || jobTitle);
      if (result.creditsRemaining !== undefined) {
        updateCredits(result.creditsRemaining);
        _creditsCacheTime = Date.now();
      }
      // Into the tray, so it is reachable after you move on.
      const t = jobTitle || (currentJob && currentJob.title) || 'this role';
      const c = company || (currentJob && currentJob.company) || '';
      trayUpsert({
        id: `cover:${currentJobUrl || t}`, kind: 'prep', status: 'found',
        name: `Cover letter · ${t}`, company: c,
        pdfUrl: result.pdfUrl || '', linkedInUrl: currentJobUrl || '',
        coverLetter: result.coverLetter || null,
      });
    } else if ((result.error || '').match(/description|required/i)) {
      showJobError('We could not read the posting. Paste the description below.');
      showManualForm({ company, jobTitle });
    } else if ((result.error || '').match(/resume/i)) {
      showJobError('Add a resume to your Offerloop profile first, then try again.');
    } else {
      showJobError(result.error || 'Could not write the letter. Please try again.');
    }
  } catch (error) {
    console.error('[Offerloop Popup] Cover letter error:', error);
    showJobError('Something went wrong. Please try again.');
  } finally {
    hideJobLoading();
    btn?.classList.remove('loading');
    if (btn) btn.disabled = false;
    _actionInProgress = false;
  }
}

function initJobTabListeners() {
  // Find & Email Recruiters button
  document.getElementById('find-recruiters-btn')?.addEventListener('click', handleFindRecruiters);
  
  // Generate Cover Letter button
  document.getElementById('cover-letter-btn')?.addEventListener('click', handleGenerateCoverLetter);
  document.getElementById('company-jobs-back')?.addEventListener('click', backToCompanyJobsList);
  
  // Manual form input listeners
  document.getElementById('manual-company')?.addEventListener('input', updateJobButtonState);
  document.getElementById('manual-job-title')?.addEventListener('input', updateJobButtonState);
  document.getElementById('manual-description')?.addEventListener('input', updateJobButtonState);
}


// Authentication is handled via Chrome Identity API + Backend

// DOM Elements
const elements = {
  loginSection: null,
  noProfileSection: null,
  companySection: null,
  profileSection: null,
  loginBtn: null,
  signOutBtn: null,
  findEmailBtn: null,
  draftEmailBtn: null,
  retryBtn: null,
  profileUrl: null,
  resultsSection: null,
  loadingSection: null,
  errorSection: null,
  loadingText: null,
  errorText: null,
  resultName: null,
  resultEmail: null,
  resultStatus: null,
  successLinksSection: null,
  openDraftLink: null,
  creditsCount: null,
};

// State
let currentState = {
  isLoggedIn: false,
  authToken: null,
  linkedInUrl: null,
  isProfilePage: false,
  credits: null,
  coffeeChatCost: null,
  user: null,
};

// Initialize DOM elements
function initElements() {
  elements.loginSection = document.getElementById('loginSection');
  elements.noProfileSection = document.getElementById('noProfileSection');
  elements.companySection = document.getElementById('companySection');
  elements.profileSection = document.getElementById('profileSection');
  elements.loginBtn = document.getElementById('loginBtn');
  elements.signOutBtn = document.getElementById('signOutBtn');
  elements.findEmailBtn = document.getElementById('findEmailBtn');
  elements.draftEmailBtn = document.getElementById('draftEmailBtn');
  elements.retryBtn = document.getElementById('retryBtn');
  elements.profileUrl = document.getElementById('profileUrl');
  elements.resultsSection = document.getElementById('resultsSection');
  elements.loadingSection = document.getElementById('loadingSection');
  elements.errorSection = document.getElementById('errorSection');
  elements.loadingText = document.getElementById('loadingText');
  elements.errorText = document.getElementById('errorText');
  elements.resultName = document.getElementById('resultName');
  elements.resultEmail = document.getElementById('resultEmail');
  elements.resultStatus = document.getElementById('resultStatus');
  elements.successLinksSection = document.getElementById('successLinksSection');
  elements.openDraftLink = document.getElementById('openDraftLink');
  elements.creditsCount = document.getElementById('creditsCount');
}

// ============================================
// COFFEE CHAT PREP WORKFLOW (aligned with website)
// ============================================

// Fallback used only until /check-credits returns the live cost from the server.
const COFFEE_CHAT_CREDITS_FALLBACK = 15;
let coffeeChatPollInterval = null;

function getCoffeeChatCost() {
  return currentState.coffeeChatCost || COFFEE_CHAT_CREDITS_FALLBACK;
}

function updateCoffeeChatButtonState() {
  const btn = document.getElementById('coffeeChatBtn');
  const hint = document.getElementById('coffeeChatHint');
  const credits = currentState.credits;
  const cost = getCoffeeChatCost();
  const hasEnough = credits !== null && credits !== undefined && credits >= cost;
  if (btn) {
    btn.disabled = !hasEnough;
    btn.title = hasEnough ? '' : `Need ${cost} credits. Check Account Settings for resume.`;
  }
  if (hint) {
    hint.textContent = hasEnough
      ? `Uses ${cost} credits • PDF saved to Library`
      : `Need ${cost} credits • Upload resume in Account Settings`;
    hint.className = hasEnough ? 'coffee-chat-hint' : 'coffee-chat-hint coffee-chat-hint-disabled';
  }
}

// Coffee Chat Prep handler - Full workflow (matches website behavior)
async function handleCoffeeChatPrep() {
  if (!_panelAlive(showCoffeeChatError)) return;
  const btn = document.getElementById('coffeeChatBtn');
  
  // Get current LinkedIn URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const linkedinUrl = tab?.url;
  
  // Validate we're on a LinkedIn profile
  if (!linkedinUrl || !linkedinUrl.match(/linkedin\.com\/in\//)) {
    showCoffeeChatError('Please navigate to a LinkedIn profile first');
    return;
  }
  
  // Get auth token
  const authData = await chrome.storage.local.get(['authToken']);
  if (!DEV_BYPASS_AUTH && !authData.authToken) {
    showCoffeeChatError('Please sign in to use this feature');
    return;
  }
  
  // Pre-flight: check credits (match website)
  const credits = currentState.credits;
  const cost = getCoffeeChatCost();
  if (credits === null || credits === undefined || credits < cost) {
    showCoffeeChatError(
      `You need ${cost} credits to generate a coffee chat prep. ` +
      (credits != null ? `You have ${credits} credits.` : 'Check your balance in Account Settings.')
    );
    return;
  }
  
  // Show loading state
  btn.classList.add('loading');
  btn.disabled = true;
  hideCoffeeChatResults();
  hideCoffeeChatError();
  showCoffeeChatLoading('Starting Coffee Chat Prep...');
  
  try {
    // Step 1: Start the prep
    console.log('[Offerloop Popup] Starting Coffee Chat Prep for:', linkedinUrl);
    
    const startResponse = await apiCall('/api/coffee-chat-prep', 'POST', { linkedinUrl }, 60000);
    const errorData = startResponse.data || {};
    
    if (!startResponse.ok) {
      // Map backend errors to friendly messages (same as website)
      let message = errorData.error || 'Failed to start Coffee Chat Prep';
      if (errorData.needsResume) {
        message = 'Please upload your resume in Account Settings first.';
      } else if (errorData.credits_needed != null) {
        message = `Insufficient credits. You need ${errorData.credits_needed} credits. You have ${errorData.current_credits || 0}.`;
      } else if (errorData.details?.reason || (errorData.details && typeof errorData.details === 'object')) {
        message = errorData.error || message;
      }
      throw new Error(message);
    }
    
    const responseData = errorData; // already parsed
    const prepId = responseData.prepId || responseData.id;
    
    console.log('[Offerloop Popup] Prep started with ID:', prepId);
    
    if (!prepId) {
      throw new Error('No prep ID returned from server');
    }
    
    // Step 2: Poll for completion
    pollCoffeeChatStatus(prepId, authData.authToken, btn);
    
  } catch (error) {
    console.error('[Offerloop Popup] Coffee Chat Prep error:', error);
    showCoffeeChatError(error.message || 'Something went wrong. Please try again.');
    hideCoffeeChatLoading();
    btn.classList.remove('loading');
    updateCoffeeChatButtonState();
  }
}

function pollCoffeeChatStatus(prepId, authToken, btn) {
  let attempts = 0;
  const maxAttempts = 60; // Fewer attempts needed with backoff
  const maxTotalMs = 10 * 60 * 1000; // 10 minute absolute timeout
  const startTime = Date.now();

  // Clear any existing poll
  if (coffeeChatPollInterval) {
    clearTimeout(coffeeChatPollInterval);
    coffeeChatPollInterval = null;
  }

  const statusMessages = {
    'processing': 'Initializing...',
    'enriching_profile': 'Enriching profile data...',
    'fetching_news': 'Fetching recent news...',
    'building_context': 'Building user context...',
    'extracting_hometown': 'Extracting location...',
    'generating_content': 'Generating content...',
    'generating_pdf': 'Generating PDF...',
    'completed': 'Coffee Chat Prep ready!',
    'failed': 'Generation failed'
  };

  function getBackoffDelay(attempt) {
    // Start at 2s, double each time, cap at 15s
    return Math.min(2000 * Math.pow(1.5, attempt), 15000);
  }

  async function poll() {
    attempts++;

    if (attempts > maxAttempts || (Date.now() - startTime) > maxTotalMs) {
      coffeeChatPollInterval = null;
      hideCoffeeChatLoading();
      showCoffeeChatError('Prep is taking longer than expected. Check the Coffee Chat Library for results.');
      btn.classList.remove('loading');
      updateCoffeeChatButtonState();
      return;
    }

    try {
      const statusResponse = await apiCall(`/api/coffee-chat-prep/${prepId}`, 'GET', undefined, 30000);

      if (!statusResponse.ok) {
        throw new Error('Failed to check status');
      }

      const statusData = statusResponse.data || {};
      console.log('[Offerloop Popup] Poll status:', statusData.status, `(attempt ${attempts})`);

      // Update loading message based on status
      const loadingMessage = statusMessages[statusData.status] || 'Processing...';
      updateCoffeeChatLoadingText(loadingMessage);

      if (statusData.status === 'completed' && statusData.pdfUrl) {
        // Success!
        coffeeChatPollInterval = null;

        hideCoffeeChatLoading();
        showCoffeeChatResults(statusData);
        triggerPdfDownload(statusData.pdfUrl, statusData.contactData);

        btn.classList.remove('loading');
        updateCoffeeChatButtonState();

        // Refresh credits
        try {
          const creditsResponse = await chrome.runtime.sendMessage({
            action: 'getCredits',
            authToken: authToken,
          });
          if (creditsResponse.credits !== undefined) {
            updateCredits(creditsResponse.credits);
            _creditsCacheTime = Date.now();
          }
        } catch (e) {
          console.log('[Offerloop Popup] Could not refresh credits:', e);
        }
        return;

      } else if (statusData.status === 'failed') {
        // Failed
        coffeeChatPollInterval = null;

        hideCoffeeChatLoading();
        showCoffeeChatError(statusData.error || 'Coffee Chat Prep failed. Please try again.');

        btn.classList.remove('loading');
        updateCoffeeChatButtonState();
        return;
      }

      // Still processing — schedule next poll with backoff
      coffeeChatPollInterval = setTimeout(poll, getBackoffDelay(attempts));

    } catch (error) {
      console.error('[Offerloop Popup] Polling error:', error);
      // Don't stop polling on transient errors, schedule next with backoff
      coffeeChatPollInterval = setTimeout(poll, getBackoffDelay(attempts));
    }
  }

  // Start first poll
  coffeeChatPollInterval = setTimeout(poll, 2000);
}

function showCoffeeChatLoading(message) {
  const loadingDiv = document.getElementById('coffeeChatLoading');
  const loadingText = document.getElementById('coffeeChatLoadingText');
  
  if (loadingText) loadingText.textContent = message || 'Generating Coffee Chat Prep...';
  if (loadingDiv) loadingDiv.classList.remove('hidden');
}

function hideCoffeeChatLoading() {
  const loadingDiv = document.getElementById('coffeeChatLoading');
  if (loadingDiv) loadingDiv.classList.add('hidden');
}

function updateCoffeeChatLoadingText(message) {
  const loadingText = document.getElementById('coffeeChatLoadingText');
  if (loadingText) loadingText.textContent = message;
}

function showCoffeeChatResults(data) {
  const resultsDiv = document.getElementById('coffeeChatResults');
  const contactDiv = document.getElementById('coffeeChatContact');
  const downloadLink = document.getElementById('coffeeChatDownloadLink');
  
  // Show contact name if available
  if (data.contactData) {
    const firstName = data.contactData.firstName || '';
    const lastName = data.contactData.lastName || '';
    const fullName = data.contactData.name || `${firstName} ${lastName}`.trim();
    const company = data.contactData.company || '';
    
    if (fullName) {
      const displayText = company ? `${fullName} at ${company}` : fullName;
      contactDiv.textContent = displayText;
      contactDiv.style.display = 'block';
    } else {
      contactDiv.style.display = 'none';
    }
  } else {
    contactDiv.style.display = 'none';
  }
  
  // Set download link
  if (data.pdfUrl && downloadLink) {
    downloadLink.href = data.pdfUrl;
  }

  if (resultsDiv) resultsDiv.style.display = 'block';

  // Into the tray as well, so the prep stays reachable after you move on
  // to the next person and this card leaves the main area.
  try {
    const cd = data.contactData || {};
    const fullName = cd.name || `${cd.firstName || ''} ${cd.lastName || ''}`.trim() || 'Meeting prep';
    trayUpsert({
      id: `prep:${data.pdfUrl || fullName}`, kind: 'prep', status: 'found',
      name: fullName, company: cd.company || '', pdfUrl: data.pdfUrl || '',
      linkedInUrl: currentState.linkedInUrl || '',
    });
  } catch (e) { /* tray is a convenience */ }
}

function hideCoffeeChatResults() {
  const resultsDiv = document.getElementById('coffeeChatResults');
  if (resultsDiv) resultsDiv.style.display = 'none';
}

function showCoffeeChatError(message) {
  const errorDiv = document.getElementById('coffeeChatError');
  const errorMsg = document.getElementById('coffeeChatErrorMessage');
  
  if (errorMsg) errorMsg.textContent = message;
  if (errorDiv) errorDiv.style.display = 'block';
}

function hideCoffeeChatError() {
  const errorDiv = document.getElementById('coffeeChatError');
  if (errorDiv) errorDiv.style.display = 'none';
}

function triggerPdfDownload(pdfUrl, contactData) {
  // Create a sanitized filename
  let contactName = 'contact';
  if (contactData) {
    const firstName = contactData.firstName || '';
    const lastName = contactData.lastName || '';
    contactName = contactData.name || `${firstName} ${lastName}`.trim() || 'contact';
  }
  
  const sanitizedName = contactName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
  
  const filename = `coffee-chat-prep-${sanitizedName}.pdf`;
  
  // Use Chrome downloads API
  chrome.downloads.download({
    url: pdfUrl,
    filename: filename,
    saveAs: false // Auto-save to downloads folder
  }, (downloadId) => {
    if (chrome.runtime.lastError) {
      console.error('[Offerloop Popup] Download error:', chrome.runtime.lastError);
      // Fallback: open PDF in new tab
      chrome.tabs.create({ url: pdfUrl });
    } else {
      console.log('[Offerloop Popup] PDF download started, ID:', downloadId);
    }
  });
}

// Clean up polling if popup closes
window.addEventListener('unload', () => {
  if (coffeeChatPollInterval) {
    clearTimeout(coffeeChatPollInterval);
    coffeeChatPollInterval = null;
  }
});

// Event Listeners
function initEventListeners() {
  elements.loginBtn?.addEventListener('click', handleLogin);
  elements.signOutBtn?.addEventListener('click', handleSignOut);
  elements.findEmailBtn?.addEventListener('click', handleFindEmail);
  elements.draftEmailBtn?.addEventListener('click', handleDraftEmail);
  elements.retryBtn?.addEventListener('click', handleRetry);

  // The empty states' "Browse ... on LinkedIn" buttons open a search page
  // in a new tab and turn the card into a nudge ("now click any person or
  // company"), so the next step is obvious while the panel waits. The
  // panel switches tabs on its own once the URL becomes a person or a
  // company (reactToActiveTabChange).
  document.querySelectorAll('.js-browse-linkedin').forEach((link) => {
    link.addEventListener('click', () => {
      const card = link.closest('.state-card');
      if (!card) return;
      card.querySelector('.js-follow-body')?.classList.add('hidden');
      card.querySelector('.js-follow-nudge')?.classList.remove('hidden');
      card.classList.add('state-card--waiting');
    });
  });
  
  // Coffee Chat Prep button
  document.getElementById('coffeeChatBtn')?.addEventListener('click', handleCoffeeChatPrep);
}

// Handle retry - refresh token first, then retry the action
async function handleRetry() {
  console.log('[Offerloop Popup] Retry clicked, attempting token refresh first...');
  
  // Try silent refresh first
  const newToken = await refreshAuthToken();
  
  if (!newToken) {
    // Silent refresh failed, force interactive login
    console.log('[Offerloop Popup] Silent refresh failed, forcing re-login...');
    await handleLogin();
    
    // If login succeeded, retry the action
    if (currentState.authToken && currentState.isLoggedIn) {
      await handleFindEmail();
    }
    return;
  }
  
  // Token refreshed, retry the action
  await handleFindEmail();
}

// Show a specific section, hide others
function showSection(sectionName) {
  elements.loginSection?.classList.add('hidden');
  elements.noProfileSection?.classList.add('hidden');
  elements.companySection?.classList.add('hidden');
  elements.profileSection?.classList.add('hidden');

  switch (sectionName) {
    case 'login':
      elements.loginSection?.classList.remove('hidden');
      elements.signOutBtn?.classList.add('hidden');
      break;
    case 'noProfile':
      elements.noProfileSection?.classList.remove('hidden');
      elements.signOutBtn?.classList.remove('hidden');
      break;
    case 'company':
      // The list lives in the Company tab; the Contact tab keeps its hint
      // so switching tabs on a company page never shows an empty panel.
      elements.companySection?.classList.remove('hidden');
      elements.noProfileSection?.classList.remove('hidden');
      elements.signOutBtn?.classList.remove('hidden');
      break;
    case 'profile':
      elements.profileSection?.classList.remove('hidden');
      elements.signOutBtn?.classList.remove('hidden');
      break;
  }
}

// Show loading state
function showLoading(text = 'Finding email...') {
  elements.loadingSection?.classList.remove('hidden');
  elements.resultsSection?.classList.add('hidden');
  elements.errorSection?.classList.add('hidden');
  if (elements.findEmailBtn) elements.findEmailBtn.disabled = true;
  if (elements.loadingText) {
    elements.loadingText.textContent = text;
  }
}

// Hide loading state
function hideLoading() {
  elements.loadingSection?.classList.add('hidden');
  if (elements.findEmailBtn) elements.findEmailBtn.disabled = false;
}

// Show error
function showError(message) {
  hideLoading();
  elements.errorSection?.classList.remove('hidden');
  elements.resultsSection?.classList.add('hidden');
  if (elements.errorText) {
    elements.errorText.textContent = message;
  }
}

// Show results
function showResults(contact) {
  hideLoading();
  elements.errorSection?.classList.add('hidden');
  elements.resultsSection?.classList.remove('hidden');

  // Parse name
  const fullName = contact.full_name || `${contact.firstName || ''} ${contact.lastName || ''}`.trim() || 'Unknown';
  elements.resultName.textContent = fullName;

  // Email, or the waiting state while FullEnrich finishes in the background:
  // the app's green bar with filler copy that advances every few seconds.
  const progressEl = document.getElementById('resultProgress');
  if (_pendingStagesStop) { _pendingStagesStop(); _pendingStagesStop = null; }
  progressEl?.classList.toggle('hidden', !contact.pending);
  applyResultAvatar();
  if (contact.pending) {
    _pendingStagesStop = startLoadingStages(elements.resultEmail, [
      'Checking who already has this address',
      'Asking our email partners',
      'Verifying the address is live',
      'Still working, this one is taking a moment',
      'Almost there',
    ], 6000);
  } else if (contact.email) {
    elements.resultEmail.textContent = contact.email;
  } else {
    elements.resultEmail.textContent = contact.refunded
      ? 'No email found. Your credits were refunded.'
      : 'Not found';
  }

  // Status badge, card state, and the line under the name
  elements.resultStatus.classList.remove('no-email', 'pending');
  elements.resultsSection?.classList.toggle('result-card--pending', !!contact.pending);
  const metaEl = elements.resultsSection?.querySelector('.result-card__meta');
  if (contact.pending) {
    elements.resultStatus.textContent = 'Searching';
    elements.resultStatus.classList.add('pending');
    if (metaEl) metaEl.textContent = 'Finding their email';
  } else if (contact.email) {
    // Catch-all domains (Apple, Qualcomm, most of big tech) accept any
    // address, so nobody can verify one. When the server serves those, say
    // "Likely" instead of "Found" so the student knows what they have.
    const likely = /catch_all/i.test(contact.email_source || '');
    elements.resultStatus.textContent = likely ? 'Likely' : 'Found';
    if (metaEl) metaEl.textContent = likely ? 'Best match, not verified' : 'Ready to reach out';
  } else {
    elements.resultStatus.textContent = 'No Email';
    elements.resultStatus.classList.add('no-email');
    if (metaEl) metaEl.textContent = contact.refunded ? 'Credits refunded' : 'No email on file';
  }

  // Show success links section
  elements.successLinksSection?.classList.remove('hidden');

  // Step two. With an email and no draft yet: the Draft button. With a
  // draft URL (a draft was made this session): the Open link. No email:
  // neither.
  currentState.lastFound = contact;
  const draftUrl = contact.gmail_draft_url || contact.gmailDraftUrl || contact.draft_url;
  if (elements.openDraftLink && draftUrl) {
    elements.openDraftLink.href = draftUrl;
  }
  elements.openDraftLink?.classList.toggle('hidden', !draftUrl);
  if (elements.draftEmailBtn) {
    elements.draftEmailBtn.classList.toggle('hidden', !(contact.email && !draftUrl));
    elements.draftEmailBtn.disabled = false;
    const label = elements.draftEmailBtn.querySelector('.result-action__label');
    if (label) label.textContent = 'Draft an email to them';
  }

  // Capture source for the "Find similar" disclosure and reset its state on
  // every new result so an old company's rows never bleed across contacts.
  primeSimilarSection(contact);
}

// The company logo lives on the "Find similar at X" row (gold frame, gold
// arrow), the eye-catcher under the found email. Scout keeps the avatar.
let _pendingStagesStop = null;

function applyResultAvatar() {
  const wrap = document.getElementById('similarToggleLogo');
  const img = wrap ? wrap.querySelector('img') : null;
  if (!wrap || !img) return;
  const logo = (currentState.profileCompanyLogo || '').trim();
  if (logo) {
    if (img.getAttribute('src') !== logo) {
      img.onerror = () => {
        img.onerror = null;
        wrap.classList.add('hidden');
      };
      img.src = logo;
    }
    wrap.classList.remove('hidden');
  } else {
    img.onerror = null;
    img.removeAttribute('src');
    wrap.classList.add('hidden');
  }
}

// Update credits display
function updateCredits(credits) {
  if (elements.creditsCount && credits !== null && credits !== undefined) {
    elements.creditsCount.textContent = credits;
  }
  currentState.credits = credits;
  updateCoffeeChatButtonState();
}

// Silently refresh the Firebase token by delegating to the background service worker.
// The canonical refreshAuthToken() lives in background.js; this avoids duplicating that logic.
// Returns the new token, or null if silent refresh fails.
async function refreshAuthToken() {
  console.log('[Offerloop Popup] Requesting token refresh from background...');

  try {
    const response = await new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ action: 'refreshToken' }, (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve(resp);
        }
      });
    });

    if (!response || !response.success) {
      console.warn('[Offerloop Popup] Background token refresh failed:', response?.error);
      return null;
    }

    // Sync the popup's in-memory state with what the background stored
    currentState.authToken = response.token;
    currentState.isLoggedIn = true;
    currentState.user = {
      email: response.userEmail,
      name: response.userName,
      picture: response.userPhoto,
    };
    currentState.credits = response.credits;

    if (response.credits !== undefined) {
      updateCredits(response.credits);
    }

    console.log('[Offerloop Popup] Token refreshed successfully via background');
    return response.token;

  } catch (error) {
    console.warn('[Offerloop Popup] Silent refresh failed:', error.message);
    return null;
  }
}

// Handle login with Chrome Identity API + Backend
async function handleLogin() {
  console.log('[Offerloop Popup] Login clicked');
  
  try {
    // Use Chrome Identity API to get Google auth token
    const googleToken = await new Promise((resolve, reject) => {
      chrome.identity.getAuthToken({ interactive: true }, (token) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve(token);
        }
      });
    });
    
    console.log('[Offerloop Popup] Got Google auth token');
    
    // Send Google token to backend to exchange for Firebase token
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/auth/google-extension`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ googleToken }),
    });
    
    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || errorData.message || 'Authentication failed');
    }
    
    const data = await response.json();
    
    if (!data.success || !data.token) {
      throw new Error(data.error || 'Authentication failed');
    }
    
    console.log('[Offerloop Popup] Sign-in successful:', data.user.email);
    
    // Save to Chrome storage
    await chrome.storage.local.set({
      authToken: data.token,
      isLoggedIn: true,
      userEmail: data.user.email,
      userName: data.user.name,
      userPhoto: data.user.picture,
      credits: data.credits,
    });
    
    currentState.authToken = data.token;
    currentState.isLoggedIn = true;
    currentState.user = data.user;
    currentState.credits = data.credits;
    
    // Update credits display
    if (data.credits !== undefined) {
      updateCredits(data.credits);
    }
    
    await checkAndShowContent();
    
  } catch (error) {
    console.error('[Offerloop Popup] Login error:', error);
    showError(error.message || 'Sign-in failed. Please try again.');
  }
}

// Handle sign out
async function handleSignOut() {
  console.log('[Offerloop Popup] Sign out clicked');
  
  try {
    // Revoke Chrome identity token
    chrome.identity.getAuthToken({ interactive: false }, (token) => {
      if (token) {
        chrome.identity.removeCachedAuthToken({ token }, () => {
          console.log('[Offerloop Popup] Chrome auth token revoked');
        });
      }
    });
    
    // Clear Chrome storage
    await chrome.storage.local.set({
      authToken: null,
      isLoggedIn: false,
      userEmail: null,
      userName: null,
      userPhoto: null,
      credits: null,
    });
    
    // Reset state
    currentState.authToken = null;
    currentState.isLoggedIn = false;
    currentState.user = null;
    currentState.credits = null;
    
    // Show login section
    showSection('login');
    updateCredits('--');
    
  } catch (error) {
    console.error('[Offerloop Popup] Sign out error:', error);
  }
}

// Handle Find Email button click
async function handleFindEmail() {
  if (_actionInProgress) return;
  if (!_panelAlive(showError)) return;
  _actionInProgress = true;
  if (!currentState.linkedInUrl) {
    _actionInProgress = false;
    showError('No LinkedIn URL detected. Please navigate to a profile page.');
    return;
  }
  
  if (!currentState.authToken) {
    showError('Please sign in to Offerloop first.');
    return;
  }
  
  // Two steps (2026-09-07): the email first, in seconds, then drafting is a
  // separate click. The old single call ran enrichment + writing + Gmail
  // before showing anything, 30 to 60 seconds of spinner for one address.
  showLoading('Finding email...');

  try {
    // Call backend via background script, without the draft. Fast mode:
    // the shelf or Hunter answer in seconds; if only FullEnrich's waterfall
    // is left, the server starts it and we poll while the person is shown.
    const response = await chrome.runtime.sendMessage({
      action: 'importLinkedIn',
      linkedInUrl: currentState.linkedInUrl,
      authToken: currentState.authToken,
      createDraft: false,
      emailMode: 'fast',
    });

    console.log('[Offerloop Popup] API Response:', response);

    if (response.error) {
      throw new Error(response.error);
    }

    // Update credits
    if (response.credits_remaining !== undefined) {
      updateCredits(response.credits_remaining);
      _creditsCacheTime = Date.now();
      currentState.credits = response.credits_remaining;
      chrome.storage.local.set({ credits: response.credits_remaining });
    }

    // Show the email; the draft button appears next to it. Pending: show
    // the person now and let the poll fill the address in.
    const contact = response.contact || response;
    // Keyed by the panel's own URL for this page, not the server's
    // normalized one: the two can differ by a trailing dash or case, and
    // a mismatch left cards stuck on "Searching" after the email landed.
    const pageUrl = currentState.linkedInUrl || '';
    const shown = {
      full_name: contact.full_name,
      firstName: contact.firstName,
      lastName: contact.lastName,
      email: contact.email,
      email_source: contact.email_source || '',
      company: contact.company,
      jobTitle: contact.jobTitle || contact.title,
      linkedinUrl: pageUrl || contact.linkedinUrl || contact.linkedin_url,
      profileUrl: contact.linkedinUrl || contact.linkedin_url || pageUrl,
      draft_url: response.draft_url || contact.draft_url,
      gmail_draft_url: response.gmail_draft_url || contact.gmail_draft_url,
      gmailDraftUrl: response.gmailDraftUrl,
      pending: response.email_pending === true && !contact.email,
      contactId: response.contact_id || '',
    };
    showResults(shown);
    cardsByUrl[shown.linkedinUrl || currentState.linkedInUrl || ''] = shown;
    if (shown.pending && shown.contactId) {
      startEmailPolling(shown);
    } else if (shown.email && shown.contactId) {
      // Instant finds go in the tray too, so a queue of finds reads as one list.
      trayUpsert({
        id: shown.contactId, kind: 'email', status: 'found',
        name: shown.full_name || `${shown.firstName || ''} ${shown.lastName || ''}`.trim(),
        company: shown.company || '', linkedInUrl: shown.linkedinUrl,
        email: shown.email, source: shown.email_source || '', person: shown,
      });
    }

  } catch (error) {
    console.error('[Offerloop Popup] Error:', error);
    showError(error.message || 'Failed to find email. Please try again.');
  } finally {
    _actionInProgress = false;
    settleTabChange();
  }
}

// Get current tab info
async function getCurrentTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab;
  } catch (error) {
    console.error('[Offerloop Popup] Error getting current tab:', error);
    return null;
  }
}

// Check if on LinkedIn profile
async function checkLinkedInProfile() {
  const tab = await getCurrentTab();
  
  if (!tab?.url) {
    return { isProfilePage: false, linkedInUrl: null };
  }
  
  const url = tab.url;
  const isProfilePage = url.includes('linkedin.com/in/');
  
  if (isProfilePage) {
    // Clean the URL
    const match = url.match(/https?:\/\/(www\.)?linkedin\.com\/in\/[\w-]+/);
    const linkedInUrl = match ? match[0] : url.split('?')[0].split('#')[0];
    return { isProfilePage: true, linkedInUrl };
  }
  
  return { isProfilePage: false, linkedInUrl: null };
}

// A LinkedIn company page (linkedin.com/company/<slug>/...). The display
// name comes from the tab title ("Hallmark Media: Overview | LinkedIn",
// sometimes prefixed with a "(3)" notification count); the slug is the
// fallback when the title has not settled yet.
function parseCompanyFromTab(tab) {
  const url = (tab && tab.url) || '';
  const m = url.match(/linkedin\.com\/company\/([^/?#]+)/i);
  if (!m) return null;
  const slug = decodeURIComponent(m[1]);
  // The slug is the truth for the search: "hallmark-media" keys the same
  // shelf row as "Hallmark Media", and it is right the instant the URL
  // changes. The tab title lags a navigation (Chrome reports the URL first,
  // the title later), so it is only used for display, and only when it
  // agrees with the slug; otherwise the slug is shown title-cased.
  const slugWords = slug.replace(/-+/g, ' ').replace(/\s+/g, ' ').trim();
  const slugName = slugWords.replace(/\b\w/g, (c) => c.toUpperCase());
  let title = (tab.title || '')
    .replace(/^\(\d+\)\s*/, '')
    .replace(/\s*\|\s*LinkedIn\s*$/i, '')
    .split(':')[0]
    .trim();
  const firstSlugWord = (slugWords.split(' ')[0] || '').toLowerCase();
  const titleAgrees = title && firstSlugWord.length > 2 && title.toLowerCase().includes(firstSlugWord);
  const name = titleAgrees ? title : slugName;
  return { slug, name, apiName: slugWords };
}

// Check auth state and show appropriate content
async function checkAndShowContent() {
  // Check if on LinkedIn profile
  const { isProfilePage, linkedInUrl } = await checkLinkedInProfile();
  currentState.isProfilePage = isProfilePage;
  currentState.linkedInUrl = linkedInUrl;

  // Credits on every page (2026-09-07). This used to run only on profile
  // pages, so the pill showed a dash everywhere else.
  await refreshCredits();

  if (!isProfilePage) {
    // Company page: list the people first, email on demand per row.
    const company = parseCompanyFromTab(await getCurrentTab());
    currentState.companyPage = company;
    if (company) {
      console.log('[Offerloop Popup] On LinkedIn company page:', company.name);
      showSection('company');
      switchTab('company');
      await loadCompanyPeople(company);
      return;
    }
    console.log('[Offerloop Popup] Not on LinkedIn profile');
    showSection('noProfile');
    updateCompanyHint();
    return;
  }
  currentState.companyPage = null;
  updateCompanyHint();
  
  console.log('[Offerloop Popup] On LinkedIn profile:', linkedInUrl);
  showSection('profile');

  // A new person: the previous person's result card and prep card leave
  // the main area (they live on in the tray). Coming back to a person whose
  // card we still have restores it.
  const priorUrl = currentState.lastFound && currentState.lastFound.linkedinUrl;
  if (priorUrl && priorUrl !== linkedInUrl) {
    elements.resultsSection?.classList.add('hidden');
    elements.errorSection?.classList.add('hidden');
    hideCoffeeChatResults();
    currentState.lastFound = null;
  }
  const savedCard = cardsByUrl[linkedInUrl];
  if (savedCard && (!currentState.lastFound || currentState.lastFound.linkedinUrl !== linkedInUrl)) {
    showResults(savedCard);
  }

  // Update UI with LinkedIn URL
  if (elements.profileUrl && linkedInUrl) {
    const displayUrl = linkedInUrl.replace('https://www.linkedin.com', 'linkedin.com').replace('https://linkedin.com', 'linkedin.com');
    elements.profileUrl.textContent = displayUrl;
  }

  // People at this person's company, listed before any email is bought.
  // Fire and forget: nothing else waits on it. The logo resets per profile
  // so one company's mark never sits on the next person's card.
  currentState.profileCompanyLogo = '';
  applyResultAvatar();
  getCurrentTab().then((tab) => loadProfileCompanyPeople(tab)).catch(() => {});
}

// Fetch credits from backend (with cache to avoid redundant calls). Runs for
// every signed-in state, not just profile pages.
async function refreshCredits() {
  if (currentState.authToken) {
    const now = Date.now();
    if (currentState.credits !== null && (now - _creditsCacheTime) < CREDITS_CACHE_TTL) {
      // Use cached credits
      updateCredits(currentState.credits);
    } else {
      try {
        const creditsResponse = await chrome.runtime.sendMessage({
          action: 'getCredits',
          authToken: currentState.authToken,
        });

        if (creditsResponse.credits !== undefined) {
          updateCredits(creditsResponse.credits);
          currentState.credits = creditsResponse.credits;
          _creditsCacheTime = now;
          chrome.storage.local.set({ credits: creditsResponse.credits });
        }
        if (creditsResponse.creditCosts) {
          const cost = creditsResponse.creditCosts.coffee_chat_prep;
          if (typeof cost === 'number' && cost > 0) {
            currentState.coffeeChatCost = cost;
            chrome.storage.local.set({ coffeeChatCost: cost });
            updateCoffeeChatButtonState();
          }
        }
      } catch (error) {
        console.error('[Offerloop Popup] Error fetching credits:', error);
      }
    }
  }
}

// Load auth state from Chrome storage
async function loadAuthState() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['authToken', 'isLoggedIn', 'credits', 'userEmail', 'userName', 'userPhoto', 'coffeeChatCost'], (result) => {
      currentState.authToken = result.authToken || null;
      currentState.isLoggedIn = result.isLoggedIn || false;
      currentState.credits = result.credits || null;
      currentState.coffeeChatCost = result.coffeeChatCost || null;
      currentState.userEmail = result.userEmail || null;
      currentState.userName = result.userName || null;
      currentState.userPhoto = result.userPhoto || null;
      // Reconstruct user object if we have the data
      if (result.userEmail) {
        currentState.user = {
          email: result.userEmail,
          name: result.userName,
          picture: result.userPhoto,
        };
      }
      resolve(result);
    });
  });
}

// Auth state is managed via Chrome storage, no Firebase listener needed

// Initialize popup
async function init() {
  console.log('[Offerloop Popup] Initializing...');
  
  initElements();
  initEventListeners();
  initTabSwitcher();
  initJobTabListeners();
  await trayRestore();
  
  // Get current tab URL and detect mode
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.url) {
      const mode = detectMode(tab.url);
      switchTab(mode);
      console.log('[Offerloop Popup] Detected mode:', mode, 'for URL:', tab.url);
    }
  } catch (error) {
    console.error('[Offerloop Popup] Error detecting mode:', error);
  }
  
  // Load stored auth state
  await loadAuthState();
  
  // Update credits display and coffee chat button state
  if (currentState.credits !== null) {
    updateCredits(currentState.credits);
  } else {
    updateCoffeeChatButtonState();
  }

  // ⚠️ DEV bypass — skip the sign-in gate entirely and go straight to content
  if (DEV_BYPASS_AUTH) {
    console.log('[Offerloop Popup] DEV_BYPASS_AUTH on — skipping sign-in');
    currentState.isLoggedIn = true;
    currentState.authToken = currentState.authToken || 'dev-bypass-token';
    currentState.user = currentState.user || { email: 'dev@offerloop.ai', name: 'Dev User' };
    if (currentState.credits === null) updateCredits(9999);
    await checkAndShowContent();
    return;
  }

  // ⚠️ DEV token bypass — when a real Firebase ID token is manually injected via
  // the popup's DevTools console (chrome.storage.local.set({ devBypassToken: '<token>' })),
  // use it directly for all authenticated API calls and SKIP the OAuth refresh,
  // which fails locally because the manifest oauth2 client id is invalid for this
  // unpacked extension. Token expires ~1h — re-paste a fresh one when calls 401.
  // Only an explicitly injected devBypassToken counts. The hardcoded DEV_TOKEN
  // that used to sit here (a real July 2026 Firebase token) ran on every
  // start for every user and overwrote their sign-in; calls that bypassed
  // the background then failed with "Invalid or expired token".
  const devAuth = await chrome.storage.local.get(['devBypassToken']);
  const devToken = devAuth.devBypassToken || '';
  if (devToken) {
    console.log('[Offerloop Popup] Using dev token — skipping OAuth refresh');
    currentState.authToken = devToken;
    currentState.isLoggedIn = true;
    // Mirror it into authToken so the Job-tab handlers (which read authToken
    // straight from storage) authenticate with the same token.
    await chrome.storage.local.set({ authToken: devToken, isLoggedIn: true });
    await checkAndShowContent();
    return;
  }

  // Check if already logged in
  if (currentState.isLoggedIn && currentState.authToken) {
    console.log('[Offerloop Popup] Already signed in:', currentState.userEmail);

    // Proactively refresh token to avoid expired token errors
    const freshToken = await refreshAuthToken();
    if (!freshToken) {
      console.log('[Offerloop Popup] Token refresh failed, showing login');
      // Clear stale auth state
      await chrome.storage.local.set({
        authToken: null,
        isLoggedIn: false,
      });
      currentState.authToken = null;
      currentState.isLoggedIn = false;
      showSection('login');
      return;
    }

    await checkAndShowContent();
  } else {
    console.log('[Offerloop Popup] Not logged in');
    showSection('login');
  }
}

// ============================================================
// Find Similar Contacts — disclosure inside the email-found card
// ============================================================
//
// State machine: closed → opened → loading → (results | empty | error).
// Fetch fires once on first open; subsequent toggles just show/hide the
// already-rendered rows. A new found contact (showResults() → primeSimilarSection)
// resets state so stale rows don't bleed across LinkedIn profiles.

const similarState = {
  source: null,        // { firstName, lastName, company, title, location, linkedinUrl }
  fetched: false,      // have we already called /find-similar for this source?
  rows: [],            // raw contact dicts from the backend
};

function _getSimilarEl(id) {
  return document.getElementById(id);
}

function primeSimilarSection(contact) {
  const section = _getSimilarEl('similarToggleBtn');
  if (!section) return;

  const company = (contact.company || '').trim();
  const linkedInUrl = currentState.linkedInUrl || contact.linkedinUrl || '';

  similarState.source = {
    firstName: contact.firstName || (contact.full_name || '').split(' ')[0] || '',
    lastName: contact.lastName || (contact.full_name || '').split(' ').slice(1).join(' ') || '',
    company: company,
    title: contact.jobTitle || contact.title || '',
    location: contact.location || '',
    linkedinUrl: linkedInUrl,
  };
  similarState.fetched = false;
  similarState.rows = [];

  // Reset DOM
  const toggle = _getSimilarEl('similarToggleBtn');
  const body = _getSimilarEl('similarBody');
  const results = _getSimilarEl('similarResults');
  const loading = _getSimilarEl('similarLoading');
  const empty = _getSimilarEl('similarEmpty');
  const error = _getSimilarEl('similarError');
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
  body?.classList.add('hidden');
  results?.classList.add('hidden');
  loading?.classList.add('hidden');
  empty?.classList.add('hidden');
  error?.classList.add('hidden');
  if (results) results.innerHTML = '';

  // Update labels + footer deep-link with the source company
  const companyLabel = company || 'this company';
  const toggleCompanyEl = _getSimilarEl('similarToggleCompany');
  if (toggleCompanyEl) toggleCompanyEl.textContent = companyLabel;
  const emptyCompanyEl = empty?.querySelector('span');
  if (emptyCompanyEl) emptyCompanyEl.textContent = companyLabel;
  const footerLink = _getSimilarEl('similarFooterLink');
  if (footerLink) {
    const companyParam = encodeURIComponent(company);
    const roleParam = encodeURIComponent(similarState.source.title || '');
    footerLink.href = company
      ? `https://www.offerloop.ai/find?tab=people&company=${companyParam}${roleParam ? `&role=${roleParam}` : ''}`
      : 'https://www.offerloop.ai/find?tab=people';
    const footerCompanyEl = footerLink.querySelector('.similar-footer-link__company');
    if (footerCompanyEl) footerCompanyEl.textContent = companyLabel;
  }

  // Hide toggle entirely if there's no company to search against — the
  // backend would reject the request anyway.
  toggle.style.display = company ? '' : 'none';
}

async function toggleSimilarSection() {
  const toggle = _getSimilarEl('similarToggleBtn');
  const body = _getSimilarEl('similarBody');
  if (!toggle || !body) return;

  const isOpen = toggle.getAttribute('aria-expanded') === 'true';
  if (isOpen) {
    toggle.setAttribute('aria-expanded', 'false');
    body.classList.add('hidden');
    return;
  }

  toggle.setAttribute('aria-expanded', 'true');
  body.classList.remove('hidden');

  if (!similarState.fetched) {
    await fetchSimilarContacts();
  }
}

async function fetchSimilarContacts() {
  const loading = _getSimilarEl('similarLoading');
  const results = _getSimilarEl('similarResults');
  const empty = _getSimilarEl('similarEmpty');
  const error = _getSimilarEl('similarError');

  if (!similarState.source || !similarState.source.company) {
    error.textContent = 'Need a company to find similar contacts.';
    error.classList.remove('hidden');
    return;
  }

  loading?.classList.remove('hidden');
  results?.classList.add('hidden');
  empty?.classList.add('hidden');
  error?.classList.add('hidden');
  startLoadingStages(_getSimilarEl('similarLoadingText'), [
    'Checking who is already on file',
    'Looking for people like them',
    'Ranking them for you',
    'Almost there',
  ]);

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'findSimilarContacts',
      authToken: currentState.authToken,
      source: similarState.source,
    });

    similarState.fetched = true;
    loading?.classList.add('hidden');

    if (!response || response.error) {
      error.textContent = (response && response.error) || 'Couldn’t fetch similar contacts. Try again.';
      error.classList.remove('hidden');
      return;
    }

    if (typeof response.credits_remaining === 'number') {
      updateCredits(response.credits_remaining);
      currentState.credits = response.credits_remaining;
      chrome.storage.local.set({ credits: response.credits_remaining });
    }

    similarState.rows = response.contacts || [];
    if (similarState.rows.length === 0) {
      empty?.classList.remove('hidden');
      return;
    }

    renderSimilarRows(similarState.rows);
    results?.classList.remove('hidden');
  } catch (e) {
    console.error('[Offerloop Popup] find-similar error:', e);
    similarState.fetched = true;
    loading?.classList.add('hidden');
    error.textContent = e.message || 'Couldn’t fetch similar contacts. Try again.';
    error.classList.remove('hidden');
  }
}

function renderSimilarRows(rows) {
  const results = _getSimilarEl('similarResults');
  if (!results) return;
  results.innerHTML = '';
  rows.forEach((row) => results.appendChild(buildSimilarRowEl(row)));
}

function _categoryCaption(category, sourceCompany, sourceTitle) {
  // Human caption from the backend's bucket category.
  const co = sourceCompany || 'this company';
  const t = sourceTitle || 'this role';
  switch (category) {
    case 'same_role_same_co':   return `Same role at ${co}`;
    case 'any_role_same_co':    return `Also at ${co}`;
    case 'same_role_other_co':  return `${t} at other firms`;
    default:                    return '';
  }
}

function buildSimilarRowEl(row) {
  const el = document.createElement('div');
  el.className = 'similar-row';
  el.setAttribute('role', 'listitem');

  const initials = `${(row.firstName || ' ')[0]}${(row.lastName || ' ')[0]}`.toUpperCase().trim() || '?';
  const fullName = `${row.firstName || ''} ${row.lastName || ''}`.trim() || 'Unknown';

  // Meta line: for same-company buckets (A/B) we show school since the
  // company is implicit; for cross-company (C) we show the contact's
  // actual company so the user knows where they work.
  const metaParts = [];
  if (row.title) metaParts.push(row.title);
  if (row.category === 'same_role_other_co') {
    if (row.company) metaParts.push(row.company);
  } else {
    if (row.school) metaParts.push(row.school);
  }
  const meta = metaParts.join(' · ');

  const avatar = document.createElement('span');
  avatar.className = 'similar-row__avatar';
  avatar.textContent = initials;
  // A face when the shelf has one. LinkedIn's picture links expire after
  // weeks and its grey "no photo" silhouette is not a photo (the app's rule
  // too), so both fall back to the initials underneath.
  const photo = (row.photoUrl || row.photo_url || '').trim();
  if (photo && !/static\.licdn\.com|\/aero-v1\//i.test(photo)) {
    const img = document.createElement('img');
    img.className = 'similar-row__photo';
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.addEventListener('error', () => img.remove());
    img.src = photo;
    avatar.appendChild(img);
  }

  const who = document.createElement('div');
  who.className = 'similar-row__who';
  const nameEl = document.createElement('span');
  nameEl.className = 'similar-row__name';
  nameEl.textContent = fullName.toLowerCase();  // CSS capitalize handles display
  const metaEl = document.createElement('span');
  metaEl.className = 'similar-row__meta';
  metaEl.textContent = meta || (row.company || '');
  who.append(nameEl, metaEl);

  // Caption explains *why* this row was suggested: the ranker's own
  // sentence for deck rows, the bucket caption for find-similar rows.
  const captionText = (row.caption || '').trim() || _categoryCaption(
    row.category,
    similarState.source && similarState.source.company,
    similarState.source && similarState.source.title,
  );
  if (captionText) {
    const captionEl = document.createElement('span');
    captionEl.className = 'similar-row__category';
    captionEl.textContent = captionText;
    who.append(captionEl);
  }

  el.append(avatar, who);

  // Open the person (2026-09-07): clicking the name or the arrow sends the
  // LinkedIn tab to their profile, and the panel follows it into the
  // Contact tab, exactly as if the user had clicked them on LinkedIn.
  const profileUrl = normalizeLinkedInUrl(row.linkedinUrl || row.linkedin_url || '');
  if (profileUrl && /linkedin\.com\/in\//i.test(profileUrl)) {
    el.classList.add('similar-row--openable');
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'similar-row__open';
    open.title = `Open ${fullName} on LinkedIn`;
    open.setAttribute('aria-label', `Open ${fullName} on LinkedIn`);
    open.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"></polyline></svg>';
    const go = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openPersonOnLinkedIn(profileUrl);
    };
    open.addEventListener('click', go);
    avatar.addEventListener('click', go);
    el.appendChild(open);
  }

  // Expand on the name (2026-09-07): a little more about the person from
  // data the deck already sent, so it costs nothing. Location, school, the
  // year they joined, every reason the ranker had, and a LinkedIn link.
  // Concise (2026-09-08): the full title on one line, then place, school
  // (with a cap mark) and tenure on a second, then up to three reason
  // tags. The ranker's sentence is dropped here; the row caption already
  // carries it.
  const facts = [];
  const place = [row.city, row.state].filter(Boolean).join(', ');
  if (place) facts.push({ text: place });
  if (row.school) facts.push({ text: row.school, icon: 'school' });
  if (row.joinedYear) facts.push({ text: `Since ${row.joinedYear}` });
  const reasonLines = (Array.isArray(row.reasons) ? row.reasons.filter(Boolean) : []).slice(0, 3);
  if (row.title || facts.length || reasonLines.length) {
    el.classList.add('similar-row--expandable');
    const details = document.createElement('div');
    details.className = 'similar-row__details hidden';
    if (row.title) {
      const t = document.createElement('p');
      t.className = 'similar-row__fact-title';
      t.textContent = row.title;
      details.appendChild(t);
    }
    if (facts.length) {
      const line = document.createElement('p');
      line.className = 'similar-row__facts';
      facts.forEach((f, i) => {
        if (i) line.appendChild(document.createTextNode('  ·  '));
        if (f.icon === 'school') {
          const cap = document.createElement('span');
          cap.className = 'similar-row__cap';
          cap.setAttribute('aria-hidden', 'true');
          cap.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 10L12 5 2 10l10 5 10-5z"></path><path d="M6 12v5c3 3 9 3 12 0v-5"></path></svg>';
          line.appendChild(cap);
        }
        line.appendChild(document.createTextNode(f.text));
      });
      details.appendChild(line);
    }
    if (reasonLines.length) {
      const why = document.createElement('div');
      why.className = 'similar-row__why';
      reasonLines.forEach((r) => {
        const tag = document.createElement('span');
        tag.className = 'similar-row__why-tag';
        tag.textContent = r;
        why.appendChild(tag);
      });
      details.appendChild(why);
    }
    if (profileUrl) {
      const link = document.createElement('a');
      link.className = 'similar-row__link';
      link.href = profileUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'View on LinkedIn';
      details.appendChild(link);
    }
    el.appendChild(details);
    who.setAttribute('role', 'button');
    who.setAttribute('aria-expanded', 'false');
    who.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const open = details.classList.toggle('hidden');
      el.classList.toggle('similar-row--open', !open);
      who.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
  }

  // Action: existing email → pill link + draft button; otherwise lazy "Find email"
  if (row.email) {
    const actions = document.createElement('div');
    actions.className = 'similar-row__actions';
    actions.appendChild(buildEmailPill(row.email));
    actions.appendChild(buildDraftBtn(row));
    el.appendChild(actions);
  } else {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'similar-row__action';
    btn.textContent = 'Find email';
    btn.addEventListener('click', () => findEmailForRow(row, btn, el));
    el.appendChild(btn);
  }

  return el;
}

// Send the active LinkedIn tab to a profile. The tab-change listener then
// switches the panel to Contact for that person. Falls back to a new tab
// when the active tab is not LinkedIn (the side panel can be open anywhere).
async function openPersonOnLinkedIn(profileUrl) {
  try {
    const tab = await getCurrentTab();
    if (tab?.id && /linkedin\.com/i.test(tab.url || '')) {
      await chrome.tabs.update(tab.id, { url: profileUrl });
    } else {
      await chrome.tabs.create({ url: profileUrl });
    }
  } catch (e) {
    console.error('[Offerloop Popup] open person failed:', e);
    chrome.tabs.create({ url: profileUrl });
  }
}

function buildDraftBtn(row) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'similar-row__action similar-row__action--draft';
  btn.textContent = 'Draft';
  btn.addEventListener('click', () => draftForRow(row, btn));
  return btn;
}

function normalizeLinkedInUrl(raw) {
  if (!raw) return '';
  let u = String(raw).trim();
  // Bare slug like "michael-foster" → wrap into full URL
  if (!u.includes('/') && !u.includes('.')) {
    u = 'https://www.linkedin.com/in/' + u;
  }
  // Strip protocol-relative "//"
  u = u.replace(/^\/\//, 'https://');
  // Add protocol if missing
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
  // Ensure www. prefix on linkedin.com
  u = u.replace(/^(https?:\/\/)(?!www\.)linkedin\.com/i, '$1www.linkedin.com');
  // Drop query + fragment
  u = u.split('?')[0].split('#')[0];
  // Drop trailing slash
  u = u.replace(/\/+$/, '');
  return u;
}

async function draftForRow(row, btn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Drafting…';
  const rawUrl = row.linkedinUrl || row.linkedin_url || row.profileUrl || row.profile_url || '';
  const linkedInUrl = normalizeLinkedInUrl(rawUrl);
  console.log('[Offerloop Popup] Draft click — row:', row, 'raw URL:', rawUrl, 'normalized:', linkedInUrl);
  if (!linkedInUrl || !/linkedin\.com\/in\//i.test(linkedInUrl)) {
    console.warn('[Offerloop Popup] Draft click — no usable LinkedIn URL:', row);
    btn.disabled = false;
    btn.textContent = 'No URL';
    btn.title = `Row has no usable LinkedIn URL (raw: ${JSON.stringify(rawUrl)})`;
    return;
  }
  try {
    // The row already has an email, so this is only the writing step: no
    // second lookup, no second charge for the address.
    const draftUrl = await draftForContact({
      firstName: row.firstName,
      lastName: row.lastName,
      email: row.email,
      company: row.company,
      jobTitle: row.title,
      linkedinUrl: linkedInUrl,
    });
    chrome.tabs.create({ url: draftUrl });
    btn.textContent = 'Opened ✓';
    setTimeout(() => { btn.disabled = false; btn.textContent = original; }, 2000);
  } catch (e) {
    console.error('[Offerloop Popup] per-row draft error:', e);
    btn.disabled = false;
    btn.textContent = 'Failed';
    btn.title = e.message || String(e);
    setTimeout(() => { btn.textContent = original; btn.removeAttribute('title'); }, 5000);
  }
}

// Step two for any person whose email is already on screen: one call to the
// website's generate-and-draft route (resume attached server-side), which
// returns the Gmail draft URL. Throws on failure so callers can show it.
async function draftForContact(person) {
  const response = await chrome.runtime.sendMessage({
    action: 'draftContact',
    contact: {
      Name: `${person.firstName || ''} ${person.lastName || ''}`.trim() || person.full_name || '',
      FirstName: person.firstName || '',
      LastName: person.lastName || '',
      Email: person.email,
      Company: person.company || '',
      Title: person.jobTitle || person.title || '',
      LinkedIn: person.linkedinUrl || '',
    },
    authToken: currentState.authToken,
  });
  console.log('[Offerloop Popup] Draft response:', response);
  if (!response || response.error) {
    throw new Error((response && response.error) || 'Draft failed (empty response)');
  }
  if (response.credits_remaining !== undefined && response.credits_remaining !== null) {
    updateCredits(response.credits_remaining);
    currentState.credits = response.credits_remaining;
    chrome.storage.local.set({ credits: response.credits_remaining });
  }
  return response.gmail_draft_url || 'https://mail.google.com/mail/u/0/#drafts';
}

// ── Pending emails: poll until FullEnrich lands, as many at once as you
// like (2026-09-08). Each search is its own job on the server and its own
// poll here. The card is refreshed only when its profile is on screen; the
// tray shows every job wherever you are, so you can queue a few and keep
// browsing.
const emailPolls = new Map(); // contactId -> { timer, tries, shown }
const cardsByUrl = {};        // profile URL -> the shown card, restored on return
const EMAIL_POLL_EVERY_MS = 5000;
const EMAIL_POLL_MAX_TRIES = 84; // seven minutes, the server's own ceiling

function stopEmailPolling(contactId) {
  const job = emailPolls.get(contactId);
  if (job && job.timer) clearInterval(job.timer);
  emailPolls.delete(contactId);
}

function startEmailPolling(shown) {
  const contactId = shown.contactId;
  if (!contactId) return;
  stopEmailPolling(contactId);
  const linkedInUrl = shown.linkedinUrl || currentState.linkedInUrl || '';
  shown.linkedinUrl = linkedInUrl;
  cardsByUrl[linkedInUrl] = shown;
  trayUpsert({
    id: contactId, kind: 'email', status: 'pending',
    name: shown.full_name || `${shown.firstName || ''} ${shown.lastName || ''}`.trim() || 'Someone',
    company: shown.company || '', linkedInUrl, person: shown,
  });
  const job = { timer: null, tries: 0, shown };
  const tick = async () => {
    if (!emailPolls.has(contactId)) return;
    job.tries += 1;
    let res = null;
    try {
      res = await chrome.runtime.sendMessage({
        action: 'emailStatus', contactId, authToken: currentState.authToken,
      });
    } catch (e) {
      res = null;
    }
    if (!res || res.error || res.status === 'pending') {
      if (job.tries >= EMAIL_POLL_MAX_TRIES) {
        stopEmailPolling(contactId);
        trayUpsert({ id: contactId, status: 'miss', statusText: 'Took too long. Try again later.' });
      }
      return;
    }
    stopEmailPolling(contactId);
    const stillHere = (currentState.linkedInUrl || '') === linkedInUrl;
    shown.pending = false;
    if (res.status === 'found' && res.email) {
      shown.email = res.email;
      shown.email_source = res.source || '';
      trayUpsert({ id: contactId, status: 'found', email: res.email, source: res.source || '' });
    } else {
      shown.email = '';
      shown.refunded = !!res.refunded;
      trayUpsert({ id: contactId, status: 'miss', statusText: res.refunded ? 'No email. Credits refunded.' : 'No email found.' });
      _creditsCacheTime = 0;
      refreshCredits().catch(() => {});
    }
    if (stillHere) showResults(shown);
    if (typeof shown.onDone === 'function') { try { shown.onDone(res); } catch (e) {} }
  };
  job.timer = setInterval(tick, EMAIL_POLL_EVERY_MS);
  emailPolls.set(contactId, job);
  setTimeout(tick, 2500);
}

// ── The tray ───────────────────────────────────────────────────────────
// Pills for background work: email searches (pending / found / miss) and
// finished meeting preps. Lives above the tab content, so it is visible on
// every tab. Session-only; pills go when dismissed or on Clear done.
const trayItems = new Map();

// The tray outlives the panel (2026-09-08): closing the side panel used to
// drop every queued search from view while the server kept working. Items
// are mirrored to chrome.storage.session (cleared when Chrome quits) and
// restored on open; pending ones resume polling.
const _traySave = () => {
  try {
    const plain = Array.from(trayItems.values()).map((it) => {
      const { onDone, writing, open, ...rest } = it;
      if (rest.person && typeof rest.person === 'object') {
        const { onDone: _od, ...person } = rest.person;
        rest.person = person;
      }
      return rest;
    });
    chrome.storage.session?.set({ trayItems: plain });
  } catch (e) { /* storage.session missing on very old Chrome */ }
};

async function trayRestore() {
  try {
    const { trayItems: saved } = await chrome.storage.session.get(['trayItems']);
    (saved || []).forEach((it) => trayItems.set(it.id, it));
    renderTray();
    (saved || []).forEach((it) => {
      if (it.kind === 'email' && it.status === 'pending' && it.person && it.person.contactId) {
        startEmailPolling({ ...it.person, pending: true, contactId: it.person.contactId });
      }
    });
  } catch (e) { /* nothing saved */ }
}

function trayUpsert(patch) {
  if (!patch || !patch.id) return;
  const cur = trayItems.get(patch.id) || {};
  trayItems.set(patch.id, { ...cur, ...patch });
  renderTray();
  _traySave();
}

function trayRemove(id) {
  trayItems.delete(id);
  renderTray();
  _traySave();
}

function _initialsOf(name) {
  const parts = (name || '').trim().split(/\s+/).filter(Boolean);
  const s = ((parts[0] || ' ')[0] + (parts[1] || ' ')[0]).toUpperCase().trim();
  return s || '?';
}

function renderTray() {
  const tray = document.getElementById('tray');
  const list = document.getElementById('trayItems');
  if (!tray || !list) return;
  const items = Array.from(trayItems.values());
  tray.classList.toggle('hidden', items.length === 0);
  list.innerHTML = '';
  items.forEach((it) => {
    const pill = document.createElement('div');
    pill.className = `tray-pill tray-pill--${it.status || 'pending'}`;
    pill.setAttribute('role', 'listitem');

    const avatar = document.createElement('span');
    avatar.className = 'tray-pill__avatar';
    avatar.textContent = it.kind === 'prep' ? 'MP' : _initialsOf(it.name);

    const who = document.createElement('div');
    who.className = 'tray-pill__who';
    const name = document.createElement('span');
    name.className = 'tray-pill__name';
    name.textContent = it.name || 'Someone';
    const status = document.createElement('span');
    status.className = 'tray-pill__status';
    if (it.kind === 'prep') {
      status.textContent = it.company ? `Meeting prep ready · ${it.company}` : 'Meeting prep ready';
    } else if (it.status === 'pending') {
      status.textContent = 'Finding their email';
    } else if (it.status === 'found') {
      status.textContent = it.email + (/catch_all/i.test(it.source || '') ? ' · likely' : '');
    } else {
      status.textContent = it.statusText || 'No email found.';
    }
    who.append(name, status);
    // Tapping the name opens a small detail strip in place (company, the
    // address, a link to the profile) instead of leaving the page. The
    // tray is for staying where you are.
    who.title = 'Details';
    who.addEventListener('click', () => {
      trayUpsert({ id: it.id, open: !it.open });
    });

    const actions = document.createElement('div');
    actions.className = 'tray-pill__actions';
    if (it.kind === 'prep' && it.pdfUrl) {
      const dl = document.createElement('a');
      dl.className = 'tray-pill__btn';
      dl.href = it.pdfUrl;
      dl.target = '_blank';
      dl.rel = 'noopener';
      dl.textContent = 'Download PDF';
      actions.appendChild(dl);
    } else if (it.kind === 'prep' && it.coverLetter) {
      // A cover letter the server returned as text: build the PDF on tap.
      const dl = document.createElement('button');
      dl.type = 'button';
      dl.className = 'tray-pill__btn';
      dl.textContent = 'Download PDF';
      dl.addEventListener('click', () => downloadCoverLetterAsPDF(it.coverLetter, it.company, it.name));
      actions.appendChild(dl);
    }
    if (it.kind === 'email' && it.status === 'found' && it.person) {
      // Two states, said plainly: "Write email" runs the draft in the
      // background right here; "Open email" opens the Gmail draft.
      const draft = document.createElement('button');
      draft.type = 'button';
      draft.className = 'tray-pill__btn';
      draft.textContent = it.draftUrl ? 'Open email' : (it.writing ? 'Writing…' : 'Write email');
      draft.disabled = !!it.writing;
      draft.addEventListener('click', async () => {
        if (it.draftUrl) { chrome.tabs.create({ url: it.draftUrl }); return; }
        trayUpsert({ id: it.id, writing: true });
        try {
          const url = await draftForContact({ ...it.person, email: it.email, linkedinUrl: it.person.profileUrl || it.person.linkedinUrl });
          trayUpsert({ id: it.id, writing: false, draftUrl: url });
          chrome.tabs.create({ url });
        } catch (e) {
          trayUpsert({ id: it.id, writing: false });
        }
      });
      actions.appendChild(draft);
    }
    if (it.status !== 'pending') {
      const x = document.createElement('button');
      x.type = 'button';
      x.className = 'tray-pill__x';
      x.setAttribute('aria-label', 'Dismiss');
      x.textContent = '×';
      x.addEventListener('click', () => trayRemove(it.id));
      actions.appendChild(x);
    }

    pill.append(avatar, who, actions);
    if (it.open) {
      const more = document.createElement('div');
      more.className = 'tray-pill__more';
      const lines = [];
      if (it.person && (it.person.jobTitle || it.company)) {
        lines.push([it.person.jobTitle, it.company].filter(Boolean).join(' · '));
      } else if (it.company) {
        lines.push(it.company);
      }
      if (it.kind === 'email' && it.status === 'found' && it.email) {
        lines.push(/catch_all/i.test(it.source || '') ? 'Best match at this company, not verified' : 'Verified address');
      }
      lines.forEach((t) => {
        const p = document.createElement('span');
        p.textContent = t;
        more.appendChild(p);
      });
      if (it.linkedInUrl) {
        const a = document.createElement('a');
        a.href = it.linkedInUrl;
        a.textContent = 'Open their profile';
        a.addEventListener('click', (ev) => { ev.preventDefault(); openPersonOnLinkedIn(it.linkedInUrl); });
        more.appendChild(a);
      }
      pill.appendChild(more);
    }
    list.appendChild(pill);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('trayClear')?.addEventListener('click', () => {
    Array.from(trayItems.values())
      .filter((it) => it.status !== 'pending')
      .forEach((it) => trayItems.delete(it.id));
    renderTray();
    _traySave();
  });
});

// Profile page, step two: the Draft button under the found email.
async function handleDraftEmail() {
  const person = currentState.lastFound;
  const btn = elements.draftEmailBtn;
  if (!person || !person.email || !btn || _actionInProgress) return;
  _actionInProgress = true;
  btn.disabled = true;
  const label = btn.querySelector('.result-action__label');
  if (label) label.textContent = 'Writing your draft...';
  try {
    const draftUrl = await draftForContact(person);
    person.gmail_draft_url = draftUrl;
    if (elements.openDraftLink) elements.openDraftLink.href = draftUrl;
    btn.classList.add('hidden');
    elements.openDraftLink?.classList.remove('hidden');
    chrome.tabs.create({ url: draftUrl });
  } catch (e) {
    console.error('[Offerloop Popup] draft error:', e);
    btn.disabled = false;
    if (label) label.textContent = 'Draft failed, try again';
  } finally {
    _actionInProgress = false;
    settleTabChange();
  }
}

function buildEmailPill(email) {
  const pill = document.createElement('a');
  pill.className = 'similar-row__email-pill';
  pill.href = 'https://mail.google.com/mail/u/0/#drafts';
  pill.target = '_blank';
  pill.title = email;
  pill.textContent = email;
  return pill;
}

async function findEmailForRow(row, btn, rowEl) {
  if (!row.linkedinUrl) {
    rowEl.classList.add('similar-row--no-email');
    btn.textContent = 'No URL';
    btn.disabled = true;
    return;
  }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Finding…';
  try {
    // Email only; drafting is the row's next button, not an automatic side
    // effect of finding the address. Deck rows reveal through the ranker
    // (warehouse, then the deck's prefetch, then one live lookup; 8
    // credits, refunded on a miss). Other rows go through import-linkedin.
    let email = '';
    if (row.candidateId) {
      const response = await chrome.runtime.sendMessage({
        action: 'revealEmail',
        candidateId: row.candidateId,
        deckId: row.deckId || '',
        authToken: currentState.authToken,
      });
      if (!response || response.error) {
        throw new Error((response && response.error) || 'Find-email failed');
      }
      email = (response.email || '').trim();
      // The reveal route does not return the balance; refetch it.
      _creditsCacheTime = 0;
      refreshCredits().catch(() => {});
    } else {
      // Fast ladder (2026-09-08): shelf or Hunter answer in seconds. If only
      // FullEnrich's slow waterfall is left, the server starts it and the
      // tray pill polls for the address while you keep going. The old call
      // waited on the whole waterfall and outlived the panel's 90-second
      // limit, so the button just snapped back to "Find email" with no word.
      const trayId = row.trayId || '';
      if (trayId) trayUpsert({ id: trayId, status: 'pending' });
      const response = await chrome.runtime.sendMessage({
        action: 'importLinkedIn',
        linkedInUrl: row.linkedinUrl,
        authToken: currentState.authToken,
        createDraft: false,
        emailMode: 'fast',
      });
      if (!response || response.error) {
        if (trayId) trayUpsert({ id: trayId, status: 'miss', statusText: 'No email on file' });
        throw new Error((response && response.error) || 'Find-email failed');
      }
      if (response.credits_remaining !== undefined) {
        updateCredits(response.credits_remaining);
        currentState.credits = response.credits_remaining;
        chrome.storage.local.set({ credits: response.credits_remaining });
      }
      const found = response.contact || response;
      email = (found.email || '').trim();
      const contactId = response.contact_id || '';
      if (!email && response.email_pending === true && contactId) {
        // The pill for this person becomes the pending pill (one pill, not two).
        if (trayId) trayRemove(trayId);
        const shown = {
          full_name: found.full_name || `${row.firstName || ''} ${row.lastName || ''}`.trim(),
          firstName: found.firstName || row.firstName || '',
          lastName: found.lastName || row.lastName || '',
          company: found.company || row.company || '',
          jobTitle: found.jobTitle || found.title || row.title || '',
          linkedinUrl: row.linkedinUrl,
          profileUrl: row.linkedinUrl,
          pending: true,
          contactId,
          onDone: (res) => {
            if (res.status === 'found' && res.email) {
              row.email = res.email;
              if (rowEl.isConnected) {
                const actions = document.createElement('div');
                actions.className = 'similar-row__actions';
                actions.appendChild(buildEmailPill(res.email));
                actions.appendChild(buildDraftBtn(row));
                btn.replaceWith(actions);
              }
            } else if (rowEl.isConnected) {
              rowEl.classList.add('similar-row--no-email');
              btn.textContent = 'No email';
              btn.disabled = true;
            }
          },
        };
        startEmailPolling(shown);
        btn.textContent = 'Searching…';
        btn.disabled = true;
        return;
      }
      if (trayId) {
        trayUpsert(email
          ? { id: trayId, status: 'found', email, source: found.email_source || '', person: { ...row, email } }
          : { id: trayId, status: 'miss', statusText: response.refunded ? 'No email. Credits refunded.' : 'No email found.' });
      }
    }
    if (email) {
      row.email = email;
      const actions = document.createElement('div');
      actions.className = 'similar-row__actions';
      actions.appendChild(buildEmailPill(email));
      actions.appendChild(buildDraftBtn(row));
      btn.replaceWith(actions);
    } else {
      rowEl.classList.add('similar-row--no-email');
      btn.textContent = 'No email';
      btn.disabled = true;
    }
  } catch (e) {
    console.error('[Offerloop Popup] per-row find-email error:', e);
    btn.disabled = false;
    btn.textContent = original;
    rowEl.classList.add('similar-row--no-email');
  }
}

// Wire up the toggle once on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
  const toggle = _getSimilarEl('similarToggleBtn');
  toggle?.addEventListener('click', toggleSimilarSection);
});

// ============================================
// COMPANY MODE: people first, email on demand
// ============================================
// On a LinkedIn company page the panel lists people at that company before
// anyone pays for an email. Browsing is free (the backend serves the
// warehouse first); each row's Find email button runs the same
// import-linkedin path the profile page uses, charged only when an address
// lands. Rows reuse the similar-row renderer, so Draft appears as soon as a
// row has an email.

// Two lists share this code: the company page ("company" prefix, ids
// companyName / companyLoading / ...) and the profile page's "Also at"
// block ("profileCompany" prefix). State is kept per prefix.
const companyState = {
  company: { key: null, rows: [] },
  profileCompany: { key: null, rows: [] },
};

function _companyEl(prefix, suffix) {
  return document.getElementById(prefix + suffix);
}

async function loadCompanyPeople(company, prefix = 'company', opts = {}) {
  const state = companyState[prefix] || (companyState[prefix] = { key: null, rows: [] });
  const nameEl = _companyEl(prefix, 'Name');
  const loading = _companyEl(prefix, 'Loading');
  const results = _companyEl(prefix, 'Results');
  const empty = _companyEl(prefix, 'Empty');
  const error = _companyEl(prefix, 'Error');
  const footer = _companyEl(prefix, 'FooterLink');
  // The typed role or chip (Company tab only). Part of the cache key, so a
  // new filter always refetches and the same filter never does.
  if (prefix === 'company' && companyFilter.company !== (company.slug || company.name)) {
    // A new company: the previous company's filter does not carry over.
    companyFilter.company = company.slug || company.name;
    companyFilter.role = '';
    companyFilter.focus = '';
  }
  const role = prefix === 'company' ? (companyFilter.role || '') : '';
  const focus = prefix === 'company' ? (companyFilter.focus || '') : '';
  // Glance lists never pull new people; the Company tab only does when the
  // student presses the button (opts.dig === '1').
  const dig = prefix === 'profileCompany' ? '0' : (opts.dig === '1' ? '1' : '');
  const key = `${company.slug || company.name}|${role}|${focus}|${dig}`;
  if (prefix === 'company') renderCompanyChips(company);

  if (nameEl) nameEl.textContent = company.name;
  const emptyCompanyEl = empty?.querySelector('span');
  if (emptyCompanyEl) emptyCompanyEl.textContent = company.name;
  if (footer) {
    footer.href = `https://www.offerloop.ai/find?tab=people&company=${encodeURIComponent(company.name)}`;
    const footerCompanyEl = footer.querySelector('.similar-footer-link__company');
    if (footerCompanyEl) footerCompanyEl.textContent = company.name;
  }

  // Same company, rows already loaded (the user moved between the About,
  // Posts and People tabs): keep what we have, no second request.
  if (state.key === key && state.rows.length) {
    renderCompanyRows(state.rows, prefix);
    return;
  }

  state.key = key;
  state.rows = [];
  results?.classList.add('hidden');
  empty?.classList.add('hidden');
  error?.classList.add('hidden');
  if (results) results.innerHTML = '';
  loading?.classList.remove('hidden');
  const stopStages = startLoadingStages(_companyEl(prefix, 'LoadingText'), [
    'Checking who is already on file',
    'Ranking them for you',
    'Looking deeper on LinkedIn',
    'Almost there',
  ]);

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'companyPeople',
      company: company.apiName || company.name,
      role,
      focus,
      // The profile page's "Also at" block is a glance, not a hunt: five
      // rows and no FullEnrich prefetch, so browsing profiles spends nothing.
      limit: prefix === 'profileCompany' ? 5 : 10,
      prefetch: prefix !== 'profileCompany',
      dig,
      authToken: currentState.authToken,
    });
    stopStages();
    loading?.classList.add('hidden');
    if (state.key !== key) return; // the user moved on

    if (!response || response.error) {
      if (error) {
        error.textContent = (response && response.error) || 'Could not load people here. Try again.';
        error.classList.remove('hidden');
      }
      return;
    }
    if (response.credits_remaining !== undefined && response.credits_remaining !== null) {
      updateCredits(response.credits_remaining);
      chrome.storage.local.set({ credits: response.credits_remaining });
    }
    state.rows = (response.contacts || []).map((r) => ({ ...r, deckId: response.deck_id || '' }));
    if (prefix === 'company' && Array.isArray(response.chips) && response.chips.length) {
      companyFilter.chips = response.chips;
    }
    if (prefix === 'company') {
      // LinkedIn's "N from your school work here", as Coresignal counts it.
      companyFilter.alumniCount = (typeof response.alumni_count === 'number') ? response.alumni_count : null;
      renderCompanyChips(company);
    }
    if (state.rows.length === 0) {
      renderCompanyEmpty(prefix, company, response, dig);
      empty?.classList.remove('hidden');
      return;
    }
    renderCompanyRows(state.rows, prefix);
  } catch (e) {
    console.error('[Offerloop Popup] company-people error:', e);
    stopStages();
    loading?.classList.add('hidden');
    if (error) {
      error.textContent = e.message || 'Could not load people here. Try again.';
      error.classList.remove('hidden');
    }
  }
}

// Staged loading copy (the app's green-bar move): the message advances
// every few seconds so a 15 second dig reads as progress, not a hang.
// Returns a stop function; safe to call twice.
function startLoadingStages(textEl, stages, everyMs = 3500) {
  if (!textEl || !stages || !stages.length) return () => {};
  let i = 0;
  textEl.textContent = stages[0];
  const timer = setInterval(() => {
    // Self-stopping: once the loader is hidden, the copy stops advancing.
    if (textEl.closest('.hidden')) { clearInterval(timer); return; }
    i = Math.min(i + 1, stages.length - 1);
    textEl.textContent = stages[i];
  }, everyMs);
  return () => clearInterval(timer);
}

// Empty states for a company list. The panel never pulls new people on
// its own; when the shelf is dry the student gets a button and decides.
// Digs are rationed per day server-side (reason "dig_cap").
function renderCompanyEmpty(prefix, company, response, dig) {
  const text = _companyEl(prefix, 'EmptyText') || _companyEl(prefix, 'Empty');
  const btn = _companyEl(prefix, 'DigBtn');
  const hint = _companyEl(prefix, 'EmptyHint');
  const name = company.name || 'this company';
  const reason = (response && response.reason) || '';
  const filtered = !!(companyFilter.role || companyFilter.focus) && prefix === 'company';
  let copy = '';
  let showDig = false;
  if (reason === 'shelf_empty' && response.can_dig && prefix === 'company') {
    const alumniFocus = companyFilter.focus === 'alumni';
    const n = companyFilter.alumniCount;
    copy = alumniFocus
      ? (typeof n === 'number' && n > 0
          ? `None of the ${n >= 50 ? '50+' : n} alumni at ${name} are on file yet.`
          : `No alumni on file yet at ${name}.`)
      : `No one on file yet at ${name}.`;
    showDig = true;
    const btnLabel = btn ? btn.querySelector('span') : null;
    if (btnLabel) btnLabel.textContent = alumniFocus ? 'Look for alumni here' : 'Look for people here';
  } else if (reason === 'dig_cap') {
    copy = `You have pulled new people enough for today. ${name} will be here tomorrow, or open any profile there and we start from that person.`;
  } else if (filtered) {
    copy = `No one on file for this filter at ${name}. Try another chip, or clear the filter.`;
  } else if (dig === '1') {
    copy = `We looked on LinkedIn and found no one we could place at ${name} yet. Open any profile there and we start from that person.`;
  } else {
    copy = `No one on file yet at ${name}.`;
  }
  if (text) {
    // Keep the <span> the tab-title catch-up writes into, but the copy above
    // already names the company, so it can stay empty.
    text.textContent = copy;
  }
  if (btn) {
    btn.classList.toggle('hidden', !showDig);
    btn.disabled = false;
    btn.onclick = showDig ? () => {
      btn.disabled = true;
      loadCompanyPeople(company, prefix, { dig: '1' });
    } : null;
  }
  if (hint) hint.classList.toggle('hidden', !showDig);
}

const COMPANY_ROWS_FIRST = 5;

function renderCompanyRows(rows, prefix = 'company') {
  const results = _companyEl(prefix, 'Results');
  if (!results) return;
  results.innerHTML = '';
  // Deck rows carry the ranker's reason as their caption; no bucket caption.
  // Five up front, the rest behind one line: shorter list, same data, no
  // extra request (rows are already in hand).
  // Tier labels (Company tab, unfiltered view): alumni first, then people
  // in recruiting, then everyone else, each group in ranked order. A
  // typed role or chip already says what the list is, so no labels then.
  const recruiterRe = /recruit|talent|people (ops|operations|partner)|human resources|\bhr\b|campus|university relations|early career|staffing/i;
  const tierOf = (row) => {
    if ((row.reasonTypes || []).includes('alumni')) return 'alumni';
    if (recruiterRe.test(row.title || '')) return 'recruiting';
    return 'other';
  };
  const filtered = prefix === 'company' && !!(companyFilter.role || companyFilter.focus);
  let ordered = rows;
  let groupLabels = null;
  if (prefix === 'company' && !filtered && rows.length > 1) {
    const groups = { alumni: [], recruiting: [], other: [] };
    rows.forEach((r) => groups[tierOf(r)].push(r));
    ordered = [...groups.alumni, ...groups.recruiting, ...groups.other];
    groupLabels = { alumni: 'Alumni', recruiting: 'Recruiting', other: (groups.alumni.length || groups.recruiting.length) ? 'Also here' : '' };
    // One real-data line at the top; parts with a zero are left out.
    const parts = [`${rows.length} on file`];
    if (groups.alumni.length) parts.push(`${groups.alumni.length} alumni`);
    if (groups.recruiting.length) parts.push(`${groups.recruiting.length} in recruiting`);
    const count = document.createElement('p');
    count.className = 'similar-count';
    count.textContent = parts.join(' · ');
    results.appendChild(count);
  }
  const els = ordered.map((row) => buildSimilarRowEl({ ...row, category: '' }));
  let lastTier = null;
  els.forEach((el, i) => {
    if (groupLabels) {
      const t = tierOf(ordered[i]);
      if (t !== lastTier && groupLabels[t]) {
        const h = document.createElement('div');
        h.className = 'similar-group';
        h.textContent = groupLabels[t];
        if (prefix === 'company' && i >= COMPANY_ROWS_FIRST) h.classList.add('hidden', 'similar-row--folded');
        results.appendChild(h);
      }
      lastTier = t;
    }
    if (prefix === 'company' && i >= COMPANY_ROWS_FIRST) el.classList.add('hidden', 'similar-row--folded');
    results.appendChild(el);
  });
  if (prefix === 'company' && els.length > COMPANY_ROWS_FIRST) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'similar-more';
    const rest = els.length - COMPANY_ROWS_FIRST;
    more.textContent = `Show ${rest} more`;
    more.addEventListener('click', () => {
      results.querySelectorAll('.similar-row--folded').forEach((el) => el.classList.remove('hidden', 'similar-row--folded'));
      more.remove();
    });
    results.appendChild(more);
  }
  results.classList.remove('hidden');
}

// ── Company tab filter: a typed role plus a couple of chips ──────────
// The role rides to the ranker as ?role=, which puts matching titles first
// and asks Coresignal for that role when the shelf is dry. Chips: Recruiters
// always, plus the last job title the Job tab saw (arriving at a company
// from a posting is the common path).
// role: typed text or a role chip. focus: alumni | recent | managers, the
// predicted hunts. chips: what the server suggested for this student
// (year, school, saved roles); a plain default until the first response.
const companyFilter = { role: '', focus: '', company: '', chips: null };

const DEFAULT_CHIPS = [
  { label: 'Alumni', focus: 'alumni' },
  { label: 'Recent hires', focus: 'recent' },
  { label: 'Recruiters', role: 'recruiter' },
  { label: 'Hiring managers', focus: 'managers' },
];

function renderCompanyChips(company) {
  const wrap = document.getElementById('companyChips');
  const input = document.getElementById('companyRoleInput');
  if (!wrap) return;
  if (input && input.value !== companyFilter.role) input.value = companyFilter.role;
  const chips = [];
  const active = companyFilter.role || companyFilter.focus;
  if (active) chips.push({ label: 'Everyone', role: '', focus: '' });
  (companyFilter.chips || DEFAULT_CHIPS).forEach((c) => {
    let label = c.label || '';
    // The alumni chip carries the live count when we have one: "USC alumni · 24".
    if ((c.focus || '') === 'alumni' && typeof companyFilter.alumniCount === 'number') {
      label = `${label} · ${companyFilter.alumniCount >= 50 ? '50+' : companyFilter.alumniCount}`;
    }
    chips.push({ label, role: (c.role || '').trim(), focus: (c.focus || '').trim() });
  });
  const lastJob = (currentState.lastJobTitle || '').trim();
  if (lastJob && !chips.some((c) => c.role.toLowerCase() === lastJob.toLowerCase())) {
    chips.push({ label: lastJob.length > 28 ? `${lastJob.slice(0, 27)}…` : lastJob, role: lastJob, focus: '' });
  }
  // The collapsed control shows what is applied, so the list stays the
  // hero and the filter is one tap away.
  const toggleLabel = document.querySelector('#companyFilterToggle .company-filter-toggle__label');
  if (toggleLabel) {
    const activeChip = (companyFilter.chips || DEFAULT_CHIPS).find((c) =>
      ((c.role || '').toLowerCase() === (companyFilter.role || '').toLowerCase())
      && ((c.focus || '') === (companyFilter.focus || '')) && (c.role || c.focus));
    toggleLabel.textContent = active
      ? `Showing: ${activeChip ? activeChip.label : companyFilter.role}`
      : 'Who are you looking for?';
  }
  wrap.innerHTML = '';
  chips.forEach((chip) => {
    if (!chip.label) return;
    const isActive = (chip.role || chip.focus)
      && chip.role.toLowerCase() === (companyFilter.role || '').toLowerCase()
      && chip.focus === (companyFilter.focus || '');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip' + (isActive ? ' chip--active' : '');
    b.textContent = chip.label;
    b.setAttribute('role', 'listitem');
    b.addEventListener('click', () => applyCompanyFilter({ role: chip.role, focus: chip.focus }, company));
    wrap.appendChild(b);
  });
}

function applyCompanyFilter(filter, company) {
  companyFilter.role = ((filter && filter.role) || '').trim();
  companyFilter.focus = ((filter && filter.focus) || '').trim();
  const input = document.getElementById('companyRoleInput');
  if (input) input.value = companyFilter.role;
  const target = company || currentState.companyPage;
  if (target) loadCompanyPeople(target, 'company');
}

// Typing a role clears any focus chip; the words are the filter now.
function applyCompanyRole(role, company) {
  applyCompanyFilter({ role, focus: '' }, company);
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('companyRoleForm');
  const input = document.getElementById('companyRoleInput');
  // The filter lives behind one control; the list is the hero.
  const toggle = document.getElementById('companyFilterToggle');
  const panel = document.getElementById('companyFilterPanel');
  toggle?.addEventListener('click', () => {
    const open = panel?.classList.toggle('hidden') === false;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.classList.toggle('company-filter-toggle--open', open);
    if (open) input?.focus();
  });
  form?.addEventListener('submit', (ev) => {
    ev.preventDefault();
    applyCompanyRole(input ? input.value : '');
  });
  // Clearing the box (the search input's own x, or backspacing to empty)
  // goes back to everyone.
  input?.addEventListener('search', () => {
    if (!input.value.trim() && companyFilter.role) applyCompanyRole('');
  });
});

// Profile page: ask the content script which company the person works at,
// then list their colleagues under the action buttons. The person on screen
// is excluded server-side once they are in the tracker; before that they may
// appear in their own list, which is harmless. Quiet on any failure.
async function loadProfileCompanyPeople(tab) {
  const block = document.getElementById('profileCompanySection');
  if (!block) return;
  block.classList.add('hidden');
  if (!tab?.id || !currentState.authToken) return;
  let company = '';
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { action: 'getProfileCompany' });
    company = ((res && res.company) || '').trim();
    // The company's logo, for the result card's avatar (gold ring). The
    // page often has not painted its logos when we first ask, so retry a
    // couple of times before settling for Scout.
    currentState.profileCompanyLogo = ((res && res.logoUrl) || '').trim();
    applyResultAvatar();
    if (!currentState.profileCompanyLogo) {
      const wantedUrl = currentState.linkedInUrl;
      for (const delay of [2500, 6000]) {
        setTimeout(async () => {
          if (currentState.profileCompanyLogo || currentState.linkedInUrl !== wantedUrl) return;
          try {
            const again = await chrome.tabs.sendMessage(tab.id, { action: 'getProfileCompany' });
            const logo = ((again && again.logoUrl) || '').trim();
            if (logo && currentState.linkedInUrl === wantedUrl) {
              currentState.profileCompanyLogo = logo;
              applyResultAvatar();
            }
          } catch (e) { /* content script gone; nothing to do */ }
        }, delay);
      }
    }
  } catch (e) {
    // Content script not ready on this page yet; the shelf may still know.
  }
  if (!company && currentState.linkedInUrl) {
    // The shelf (free, no vendor call): anyone a student found before
    // carries company, title and photo. Covers profile layouts the page
    // reader cannot parse.
    try {
      const glance = await apiCall(`/api/contacts/profile-glance?url=${encodeURIComponent(currentState.linkedInUrl)}`, 'GET', undefined, 15000);
      if (glance.ok && glance.data && glance.data.company) {
        company = String(glance.data.company).trim();
        if (!currentState.profileCompanyLogo && glance.data.logo_url) {
          currentState.profileCompanyLogo = glance.data.logo_url;
          applyResultAvatar();
        }
      }
    } catch (e) { /* not on the shelf */ }
  }
  if (!company) return;
  block.classList.remove('hidden');
  await loadCompanyPeople({ slug: company.toLowerCase(), name: company }, 'profileCompany');
}

// Side panel stays open across tab switches — unlike a popup which reinits every
// open — so we have to actively re-detect the active tab when the user navigates.
let _tabChangePending = false;

async function reactToActiveTabChange() {
  // Never re-render mid-action (2026-09-07). A click elsewhere while Find
  // Email or Draft is running used to wipe the result the moment it landed.
  // Remember that something changed and catch up when the action finishes.
  if (_actionInProgress) {
    _tabChangePending = true;
    return;
  }
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url) return;
    // Same person as the result on screen: leave the panel alone (LinkedIn
    // rewrites the URL with tracking suffixes and section anchors as you
    // scroll a profile).
    const sameProfile = currentState.linkedInUrl
      && tab.url.includes('linkedin.com/in/')
      && tab.url.startsWith(currentState.linkedInUrl);
    if (sameProfile) return;
    switchTab(detectMode(tab.url));
    if (currentState.isLoggedIn) {
      await checkAndShowContent();
    }
  } catch (error) {
    console.error('[Offerloop Popup] Tab change handler error:', error);
  }
}

// Called by the long actions when they finish, so a navigation that
// happened mid-action is honoured afterwards instead of dropped.
function settleTabChange() {
  if (!_tabChangePending) return;
  _tabChangePending = false;
  reactToActiveTabChange();
}

chrome.tabs.onActivated.addListener(reactToActiveTabChange);
chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.url && tab.active) reactToActiveTabChange();
  // The title settles after the URL. On a company page, catch up the
  // display name only; the search already ran on the slug.
  if (changeInfo.title && tab.active && currentState.companyPage) {
    const c = parseCompanyFromTab(tab);
    if (c && c.slug === currentState.companyPage.slug && c.name !== currentState.companyPage.name) {
      currentState.companyPage = c;
      const nameEl = document.getElementById('companyName');
      if (nameEl) nameEl.textContent = c.name;
      const emptyCompanyEl = document.querySelector('#companyEmpty span');
      if (emptyCompanyEl) emptyCompanyEl.textContent = c.name;
      const footerCompanyEl = document.querySelector('#companyFooterLink .similar-footer-link__company');
      if (footerCompanyEl) footerCompanyEl.textContent = c.name;
    }
  }
});

// Start initialization when DOM is ready
document.addEventListener('DOMContentLoaded', init);