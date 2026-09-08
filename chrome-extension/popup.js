// Popup script for Offerloop Chrome Extension
console.log('[Offerloop Popup] Loaded');

// API Configuration
const API_BASE_URL = 'https://www.offerloop.ai';

// ⚠️ DEV ONLY — skips the sign-in gate so you can work on the UI/UX
// without logging in. Set to false before shipping. API calls that need a
// real Firebase token will still fail; this only unlocks the screens.
const DEV_BYPASS_AUTH = false;

// ⚠️ DEV ONLY — paste a fresh Firebase ID token here to authenticate without OAuth.
// Grab it from www.offerloop.ai (Network tab → any /api request → Authorization: Bearer <token>).
// Expires ~1 hour; replace it when API calls start returning 401. Set to '' to disable.
const DEV_TOKEN = 'eyJhbGciOiJSUzI1NiIsImtpZCI6ImIyNDg2Mzc0OTVjYjM4N2U0OWViNmRlMThkZjk5N2VlOGU1YWUyOTciLCJ0eXAiOiJKV1QifQ.eyJuYW1lIjoiU2lkIEJhbmRpIiwicGljdHVyZSI6Imh0dHBzOi8vbGgzLmdvb2dsZXVzZXJjb250ZW50LmNvbS9hL0FDZzhvY0xGWXVmZEt3ZXRDZGxIcnhzbm5jMjcxNllfUUVhb3Z4T1p4R1oxQUI4QWgtaHVEUT1zOTYtYyIsImlzcyI6Imh0dHBzOi8vc2VjdXJldG9rZW4uZ29vZ2xlLmNvbS9vZmZlcmxvb3AtbmF0aXZlIiwiYXVkIjoib2ZmZXJsb29wLW5hdGl2ZSIsImF1dGhfdGltZSI6MTc4NDE1NTkwMSwidXNlcl9pZCI6IlFBSzRIcHZiZ05TV3lPMHhhZDRDYUhrNUZrWDIiLCJzdWIiOiJRQUs0SHB2YmdOU1d5TzB4YWQ0Q2FIazVGa1gyIiwiaWF0IjoxNzg0NzU2NzYyLCJleHAiOjE3ODQ3NjAzNjIsImVtYWlsIjoic2lkQG9mZmVybG9vcC5haSIsImVtYWlsX3ZlcmlmaWVkIjp0cnVlLCJmaXJlYmFzZSI6eyJpZGVudGl0aWVzIjp7Imdvb2dsZS5jb20iOlsiMTExMTE4Mzc0NTAyOTU2OTc0MDkxIl0sImVtYWlsIjpbInNpZEBvZmZlcmxvb3AuYWkiXX0sInNpZ25faW5fcHJvdmlkZXIiOiJnb29nbGUuY29tIn19.gjf45o7FvLSMcccik8jzqat8LnRW74MiGl68x4xDQv6NXy3xdYvakP0yrpS991tPgqN233k4KaM1Nh7crZ7yg9tl_ZPypJIlKZTV-S8cy0U80tTGBU1DzHFMhdjh26c8AuEcmMO4NUAdiPrsottebQvHSZbQ3XXtX3-7ENqTraPIDktXShHKSJbtCCl0dcz3SQPRI8h55XIa6l62CQUhVoiRNveiAa8088p0DtXrjJgXNsVo5mNSd_yCeZCVa7pSY2ngI6xNk2cTwjs9i-gyGhWzkq_m29baeyiIFQ-YmLP5WvG7zuV5IrC2bXuSjPRLW4HxabKRbeK50CzlQ7Jvmg';

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

// Credits cache — avoid redundant API calls when popup reopens quickly
let _creditsCacheTime = 0;
const CREDITS_CACHE_TTL = 120000; // 2 minutes

// Initialize Job Tab when switched to
async function initJobTab() {
  console.log('[Offerloop Popup] Initializing Job tab...');
  
  // Reset state
  hideAllJobResults();
  hideJobError();
  hideManualForm();
  hideJobLoading();
  
  // Get current tab URL
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentJobUrl = tab.url;
  
  // Update URL display
  const urlTextEl = document.getElementById('job-url-text');
  if (urlTextEl) {
    urlTextEl.textContent = truncateUrl(currentJobUrl);
  }
  
  // Check if we're on a supported job board
  if (isJobUrl(currentJobUrl)) {
    showJobStatus('Job URL detected. Click a button to proceed.');
    enableJobButtons();
    manualInputRequired = false;
  } else {
    showJobStatus('Not on a recognized job page. Please enter details manually.');
    showManualForm();
    manualInputRequired = true;
    updateJobButtonState();
  }
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

function showJobLoading(message) {
  const loading = document.getElementById('job-loading');
  const text = document.getElementById('job-loading-text');
  if (text) text.textContent = message || 'Processing...';
  if (loading) loading.classList.remove('hidden');
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

function showJobResults(result) {
  const resultsDiv = document.getElementById('job-results');
  const detailsDiv = document.getElementById('job-result-details');
  const linksDiv = document.querySelector('#job-results .result-links');

  if (detailsDiv) {
    if (result.recruiters && result.recruiters.length > 0) {
      let text = `Found ${result.recruiters.length} recruiter${result.recruiters.length > 1 ? 's' : ''}`;
      if (result._savedToTracker) {
        text += ` • ${result._savedToTracker} saved to Hiring Manager Tracker`;
      }
      detailsDiv.textContent = text;
    } else {
      detailsDiv.textContent = 'Contact saved to your library';
    }
  }

  if (linksDiv && result._savedToTracker) {
    const trackerP = document.createElement('p');
    trackerP.textContent = 'Saved to Hiring Manager Tracker. ';
    const trackerLink = document.createElement('a');
    trackerLink.href = 'https://www.offerloop.ai/hiring-manager-tracker';
    trackerLink.target = '_blank';
    trackerLink.rel = 'noopener';
    trackerLink.textContent = 'View in Tracker';
    trackerP.appendChild(trackerLink);
    if (!linksDiv.querySelector('a[href*="hiring-manager-tracker"]')) {
      linksDiv.insertBefore(trackerP, linksDiv.firstChild);
    }
  }

  if (resultsDiv) resultsDiv.classList.remove('hidden');
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

async function handleFindRecruiters() {
  if (_actionInProgress) return;
  _actionInProgress = true;
  const btn = document.getElementById('find-recruiters-btn');

  // Get auth token
  const authData = await chrome.storage.local.get(['authToken']);
  if (!DEV_BYPASS_AUTH && !authData.authToken) {
    showJobErrorWithSignin('Please sign in to use this feature.');
    return;
  }

  // Build request body
  let requestBody = {};

  if (manualInputRequired) {
    // Use manual input
    const data = getManualInputData();
    if (!data.company) {
      showJobError('Company name is required');
      return;
    }
    requestBody = {
      company: data.company,
      jobTitle: data.jobTitle || undefined,
      jobDescription: data.jobDescription || undefined,
      jobUrl: data.jobUrl || undefined
    };
  } else {
    // Use URL - backend will parse
    requestBody = {
      jobUrl: currentJobUrl
    };
  }
  
  // Show loading
  btn.classList.add('loading');
  btn.disabled = true;
  hideJobError();
  hideAllJobResults();
  showJobLoading('Finding recruiters...');
  
  try {
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/job-board/find-recruiter`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authData.authToken}`
      },
      body: JSON.stringify(requestBody)
    });
    
    const result = await response.json();
    
    if (response.ok && (result.success || result.recruiters)) {
      // Save recruiters to Hiring Manager Tracker (same as website)
      if (result.recruiters && result.recruiters.length > 0) {
        try {
          const jobTitleForSave = (manualInputRequired ? getManualInputData().jobTitle : null) || result.jobTypeDetected || '';
          const saveBody = {
            recruiters: result.recruiters,
            draftsCreated: result.draftsCreated || [],
            companyCleaned: result.companyCleaned || '',
            associatedJobUrl: currentJobUrl || '',
            associatedJobTitle: jobTitleForSave,
            jobTitle: jobTitleForSave,
          };
          const saveRes = await fetchWithTimeout(`${API_BASE_URL}/api/job-board/save-recruiters`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Authorization': `Bearer ${authData.authToken}`,
            },
            body: JSON.stringify(saveBody),
          });
          const saveData = await saveRes.json().catch(() => ({}));
          if (saveData.saved > 0) {
            result._savedToTracker = saveData.saved;
          }
        } catch (e) {
          console.warn('[Offerloop Popup] Save to tracker failed:', e);
        }
      }
      showJobResults(result);
      if (result.creditsRemaining !== undefined) {
        updateCredits(result.creditsRemaining);
        _creditsCacheTime = Date.now();
      }
    } else if (result.needsManualInput || result.error?.includes('extract') || result.error?.includes('company')) {
      // Backend couldn't parse URL - show manual form
      showJobError('Could not extract job details from URL. Please enter manually.');
      showManualForm(result.partialData);
    } else {
      showJobError(result.error || 'Failed to find recruiters. Please try again.');
    }
  } catch (error) {
    console.error('[Offerloop Popup] Find recruiters error:', error);
    showJobError('Something went wrong. Please try again.');
  } finally {
    hideJobLoading();
    btn.classList.remove('loading');
    btn.disabled = false;
    _actionInProgress = false;
  }
}

// ============================================
// GENERATE COVER LETTER (URL-FIRST + DOWNLOAD)
// ============================================

async function handleGenerateCoverLetter() {
  const btn = document.getElementById('cover-letter-btn');

  // Get auth token
  const authData = await chrome.storage.local.get(['authToken']);
  if (!DEV_BYPASS_AUTH && !authData.authToken) {
    showJobErrorWithSignin('Please sign in to use this feature.');
    return;
  }

  // Build request body
  let requestBody = {
    jobUrl: currentJobUrl
  };
  let company = '';
  let jobTitle = '';
  
  if (manualInputRequired) {
    // Use manual input - job description is required
    const data = getManualInputData();
    if (!data.jobDescription) {
      showJobError('Job description is required for cover letter generation');
      return;
    }
    requestBody = {
      jobDescription: data.jobDescription,
      company: data.company || undefined,
      jobTitle: data.jobTitle || undefined,
      jobUrl: data.jobUrl || undefined
    };
    company = data.company || '';
    jobTitle = data.jobTitle || '';
  } else {
    // Try to scrape job description from the page
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const response = await Promise.race([
        chrome.tabs.sendMessage(tab.id, { action: 'scrapeJobDescription' }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('Content script timeout')), 5000))
      ]);
      
      if (response && response.description && response.description.length > 100) {
        requestBody = {
          jobDescription: response.description,
          jobUrl: currentJobUrl
        };
      } else {
        // No description scraped - need manual input
        showJobError('Could not extract job description. Please paste it manually.');
        showManualForm();
        return;
      }
    } catch (e) {
      console.error('[Offerloop Popup] Scraping failed:', e);
      // Scraping failed - need manual input
      showJobError('Could not extract job description. Please paste it manually.');
      showManualForm();
      return;
    }
  }
  
  // Show loading
  btn.classList.add('loading');
  btn.disabled = true;
  hideJobError();
  hideAllJobResults();
  showJobLoading('Generating cover letter...');
  
  try {
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/job-board/generate-cover-letter`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authData.authToken}`
      },
      body: JSON.stringify(requestBody)
    });
    
    const result = await response.json();
    console.log('[Offerloop Popup] Cover letter API response:', result);
    console.log('[Offerloop Popup] coverLetter type:', typeof result.coverLetter);
    
    if (response.ok && result.coverLetter) {
      // Success - download the cover letter as a PDF
      hideJobLoading();
      showCoverLetterResults(result, company, jobTitle);
      downloadCoverLetterAsPDF(result.coverLetter, company, jobTitle);

      if (result.creditsRemaining !== undefined) {
        updateCredits(result.creditsRemaining);
        _creditsCacheTime = Date.now();
      }
    } else if (response.ok && result.pdfUrl) {
      // If backend returns PDF URL (future support)
      hideJobLoading();
      showCoverLetterResults(result, company, jobTitle);
      triggerCoverLetterDownload(result.pdfUrl, company, jobTitle);

      if (result.creditsRemaining !== undefined) {
        updateCredits(result.creditsRemaining);
        _creditsCacheTime = Date.now();
      }
    } else if (result.error?.includes('description') || result.error?.includes('required')) {
      // Need job description
      showJobError('Please paste the job description below');
      showManualForm();
    } else {
      showJobError(result.error || 'Failed to generate cover letter. Please try again.');
    }
  } catch (error) {
    console.error('[Offerloop Popup] Cover letter error:', error);
    showJobError('Something went wrong. Please try again.');
  } finally {
    hideJobLoading();
    btn.classList.remove('loading');
    btn.disabled = false;
  }
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
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/job-board/cover-letter-pdf`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authData.authToken}`
      },
      body: JSON.stringify({ content: text, company: company })
    });
    
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

function initJobTabListeners() {
  // Find & Email Recruiters button
  document.getElementById('find-recruiters-btn')?.addEventListener('click', handleFindRecruiters);
  
  // Generate Cover Letter button
  document.getElementById('cover-letter-btn')?.addEventListener('click', handleGenerateCoverLetter);
  
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
    
    const startResponse = await fetchWithTimeout(`${API_BASE_URL}/api/coffee-chat-prep`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authData.authToken}`
      },
      body: JSON.stringify({ linkedinUrl })
    });
    
    const errorData = await startResponse.json().catch(() => ({}));
    
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
      const statusResponse = await fetchWithTimeout(`${API_BASE_URL}/api/coffee-chat-prep/${prepId}`, {
        headers: {
          'Authorization': `Bearer ${authToken}`
        }
      });

      if (!statusResponse.ok) {
        throw new Error('Failed to check status');
      }

      const statusData = await statusResponse.json();
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

  // Email, or the waiting state while FullEnrich finishes in the background
  if (contact.pending) {
    elements.resultEmail.textContent = 'Locking down their email. This can take a minute or two.';
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
    elements.resultStatus.textContent = 'Found';
    if (metaEl) metaEl.textContent = 'Ready to reach out';
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
  _actionInProgress = true;
  if (!currentState.linkedInUrl) {
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
    const shown = {
      full_name: contact.full_name,
      firstName: contact.firstName,
      lastName: contact.lastName,
      email: contact.email,
      company: contact.company,
      jobTitle: contact.jobTitle || contact.title,
      linkedinUrl: contact.linkedinUrl || contact.linkedin_url || currentState.linkedInUrl,
      draft_url: response.draft_url || contact.draft_url,
      gmail_draft_url: response.gmail_draft_url || contact.gmail_draft_url,
      gmailDraftUrl: response.gmailDraftUrl,
      pending: response.email_pending === true && !contact.email,
      contactId: response.contact_id || '',
    };
    showResults(shown);
    if (shown.pending && shown.contactId) {
      startEmailPolling(shown);
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
  const slugWords = slug.replace(/-/g, ' ').trim();
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

  // Update UI with LinkedIn URL
  if (elements.profileUrl && linkedInUrl) {
    const displayUrl = linkedInUrl.replace('https://www.linkedin.com', 'linkedin.com').replace('https://linkedin.com', 'linkedin.com');
    elements.profileUrl.textContent = displayUrl;
  }

  // People at this person's company, listed before any email is bought.
  // Fire and forget: nothing else waits on it.
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
  const devAuth = await chrome.storage.local.get(['devBypassToken']);
  const devToken = devAuth.devBypassToken || DEV_TOKEN;
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
  const detailLines = [];
  if (row.title) detailLines.push(row.title);
  const place = [row.city, row.state].filter(Boolean).join(', ');
  if (place) detailLines.push(place);
  if (row.school) detailLines.push(row.school);
  if (row.joinedYear) detailLines.push(`At ${row.company || 'this company'} since ${row.joinedYear}`);
  const reasonLines = Array.isArray(row.reasons) ? row.reasons.filter(Boolean) : [];
  if (detailLines.length || reasonLines.length || row.briefing) {
    el.classList.add('similar-row--expandable');
    const details = document.createElement('div');
    details.className = 'similar-row__details hidden';
    if (row.briefing) {
      const b = document.createElement('p');
      b.className = 'similar-row__briefing';
      b.textContent = row.briefing;
      details.appendChild(b);
    }
    if (detailLines.length) {
      const ul = document.createElement('ul');
      ul.className = 'similar-row__facts';
      detailLines.forEach((line) => {
        const li = document.createElement('li');
        li.textContent = line;
        ul.appendChild(li);
      });
      details.appendChild(ul);
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

// ── Pending email: poll until FullEnrich lands, then fill the card in ──
// One poll at a time. Survives tab changes (the result belongs to the
// profile it was started on); the card is only refreshed when that profile
// is still the one on screen.
const emailPoll = { timer: null, contactId: '', linkedInUrl: '', tries: 0 };
const EMAIL_POLL_EVERY_MS = 5000;
const EMAIL_POLL_MAX_TRIES = 84; // seven minutes, the server's own ceiling

function stopEmailPolling() {
  if (emailPoll.timer) clearInterval(emailPoll.timer);
  emailPoll.timer = null;
  emailPoll.contactId = '';
  emailPoll.tries = 0;
}

function startEmailPolling(shown) {
  stopEmailPolling();
  emailPoll.contactId = shown.contactId;
  emailPoll.linkedInUrl = shown.linkedinUrl || currentState.linkedInUrl || '';
  emailPoll.tries = 0;
  const tick = async () => {
    if (!emailPoll.contactId) return;
    emailPoll.tries += 1;
    let res = null;
    try {
      res = await chrome.runtime.sendMessage({
        action: 'emailStatus',
        contactId: emailPoll.contactId,
        authToken: currentState.authToken,
      });
    } catch (e) {
      res = null;
    }
    if (!res || res.error) {
      if (emailPoll.tries >= EMAIL_POLL_MAX_TRIES) stopEmailPolling();
      return;
    }
    if (res.status === 'pending') {
      if (emailPoll.tries >= EMAIL_POLL_MAX_TRIES) stopEmailPolling();
      return;
    }
    stopEmailPolling();
    const stillHere = (currentState.linkedInUrl || '') === emailPoll.linkedInUrl;
    if (res.status === 'found' && res.email) {
      shown.pending = false;
      shown.email = res.email;
      if (stillHere) showResults(shown);
    } else {
      shown.pending = false;
      shown.email = '';
      shown.refunded = !!res.refunded;
      if (stillHere) showResults(shown);
      _creditsCacheTime = 0;
      refreshCredits().catch(() => {});
    }
  };
  emailPoll.timer = setInterval(tick, EMAIL_POLL_EVERY_MS);
  setTimeout(tick, 2500);
}

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
      const response = await chrome.runtime.sendMessage({
        action: 'importLinkedIn',
        linkedInUrl: row.linkedinUrl,
        authToken: currentState.authToken,
        createDraft: false,
      });
      if (!response || response.error) {
        throw new Error((response && response.error) || 'Find-email failed');
      }
      if (response.credits_remaining !== undefined) {
        updateCredits(response.credits_remaining);
        currentState.credits = response.credits_remaining;
        chrome.storage.local.set({ credits: response.credits_remaining });
      }
      const found = response.contact || response;
      email = (found.email || '').trim();
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

async function loadCompanyPeople(company, prefix = 'company') {
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
  }
  const role = prefix === 'company' ? (companyFilter.role || '') : '';
  const key = `${company.slug || company.name}|${role}`;
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
      // The profile page's "Also at" block is a glance, not a hunt: five
      // rows and no FullEnrich prefetch, so browsing profiles spends nothing.
      limit: prefix === 'profileCompany' ? 5 : 10,
      prefetch: prefix !== 'profileCompany',
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
    if (state.rows.length === 0) {
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
    i = Math.min(i + 1, stages.length - 1);
    textEl.textContent = stages[i];
  }, everyMs);
  return () => clearInterval(timer);
}

function renderCompanyRows(rows, prefix = 'company') {
  const results = _companyEl(prefix, 'Results');
  if (!results) return;
  results.innerHTML = '';
  // Deck rows carry the ranker's reason as their caption; no bucket caption.
  rows.forEach((row) => results.appendChild(buildSimilarRowEl({ ...row, category: '' })));
  results.classList.remove('hidden');
}

// ── Company tab filter: a typed role plus a couple of chips ──────────
// The role rides to the ranker as ?role=, which puts matching titles first
// and asks Coresignal for that role when the shelf is dry. Chips: Recruiters
// always, plus the last job title the Job tab saw (arriving at a company
// from a posting is the common path).
const companyFilter = { role: '', company: '' };

function renderCompanyChips(company) {
  const wrap = document.getElementById('companyChips');
  const input = document.getElementById('companyRoleInput');
  if (!wrap) return;
  if (input && input.value !== companyFilter.role) input.value = companyFilter.role;
  const chips = [];
  if (companyFilter.role) chips.push({ label: 'Everyone', role: '' });
  chips.push({ label: 'Recruiters', role: 'recruiter' });
  const lastJob = (currentState.lastJobTitle || '').trim();
  if (lastJob && lastJob.toLowerCase() !== companyFilter.role.toLowerCase()) {
    chips.push({ label: lastJob.length > 28 ? `${lastJob.slice(0, 27)}…` : lastJob, role: lastJob });
  }
  wrap.innerHTML = '';
  chips.forEach((chip) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip' + (chip.role.toLowerCase() === companyFilter.role.toLowerCase() && chip.role ? ' chip--active' : '');
    b.textContent = chip.label;
    b.setAttribute('role', 'listitem');
    b.addEventListener('click', () => applyCompanyRole(chip.role, company));
    wrap.appendChild(b);
  });
}

function applyCompanyRole(role, company) {
  companyFilter.role = (role || '').trim();
  const input = document.getElementById('companyRoleInput');
  if (input) input.value = companyFilter.role;
  const target = company || currentState.companyPage;
  if (target) loadCompanyPeople(target, 'company');
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('companyRoleForm');
  const input = document.getElementById('companyRoleInput');
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
  } catch (e) {
    // Content script not ready on this page yet; nothing to show.
    return;
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