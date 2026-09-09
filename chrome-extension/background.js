// Background service worker for Offerloop Chrome Extension
console.log('[Offerloop Background] Service worker started');

// Configuration
const API_BASE_URL = 'https://www.offerloop.ai';

// Clicking the toolbar icon opens the side panel (instead of a popup)
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.error('[Offerloop Background] setPanelBehavior failed:', err));

// Fetch wrapper with AbortController timeout (default 30s)
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

// Refresh the Firebase auth token using Chrome Identity API
async function refreshAuthToken() {
  console.log('[Offerloop Background] Refreshing auth token...');
  
  try {
    // Remove cached Google token to force a fresh one
    const oldToken = await new Promise((resolve) => {
      chrome.identity.getAuthToken({ interactive: false }, (token) => {
        resolve(token || null);
      });
    });
    
    if (oldToken) {
      await new Promise((resolve) => {
        chrome.identity.removeCachedAuthToken({ token: oldToken }, resolve);
      });
    }
    
    // Get fresh Google token (non-interactive in background)
    const googleToken = await new Promise((resolve, reject) => {
      chrome.identity.getAuthToken({ interactive: false }, (token) => {
        if (chrome.runtime.lastError || !token) {
          reject(new Error(chrome.runtime.lastError?.message || 'No token'));
        } else {
          resolve(token);
        }
      });
    });
    
    // Exchange for Firebase token
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/auth/google-extension`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ googleToken }),
    });
    
    if (!response.ok) throw new Error('Token exchange failed');
    
    const data = await response.json();
    if (!data.success || !data.token) throw new Error('No token in response');
    
    // Update storage
    await chrome.storage.local.set({
      authToken: data.token,
      isLoggedIn: true,
      userEmail: data.user.email,
      userName: data.user.name,
      userPhoto: data.user.picture,
      credits: data.credits,
    });
    
    console.log('[Offerloop Background] Token refreshed successfully');
    return data.token;
  } catch (error) {
    console.error('[Offerloop Background] Token refresh failed:', error);
    return null;
  }
}

// Listen for extension installation
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install') {
    console.log('[Offerloop Background] Extension installed');
    // Initialize storage
    chrome.storage.local.set({
      isLoggedIn: false,
      authToken: null,
      credits: null,
    });
  }
  
  // Create context menu
  chrome.contextMenus.create({
    id: 'offerloop-add',
    title: 'Add to Offerloop',
    contexts: ['link'],
    targetUrlPatterns: ['*://www.linkedin.com/in/*', '*://linkedin.com/in/*'],
  });
});

// Listen for messages from popup or content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  console.log('[Offerloop Background] Received message:', request.action);
  
  switch (request.action) {
    case 'addToOfferloop':
      handleAddToOfferloop(request, sendResponse);
      return true; // Keep channel open for async response
      
    case 'importLinkedIn':
      handleImportLinkedIn(request, sendResponse);
      return true;

    case 'findSimilarContacts':
      handleFindSimilarContacts(request, sendResponse);
      return true;

    case 'companyPeople':
      handleCompanyPeople(request, sendResponse);
      return true;

    case 'draftContact':
      handleDraftContact(request, sendResponse);
      return true;

    case 'revealEmail':
      handleRevealEmail(request, sendResponse);
      return true;

    case 'emailStatus':
      handleEmailStatus(request, sendResponse);
      return true;

    case 'getCredits':
      handleGetCredits(request, sendResponse);
      return true;
      
    case 'setAuthToken':
      handleSetAuthToken(request, sendResponse);
      return true;
      
    case 'getStatus':
      handleGetStatus(sendResponse);
      return true;

    case 'refreshToken':
      handleRefreshToken(sendResponse);
      return true;

    case 'logScraperResult':
      handleLogScraperResult(request);
      // Fire-and-forget, no response needed
      break;

    default:
      console.log('[Offerloop Background] Unknown action:', request.action);
      sendResponse({ error: 'Unknown action' });
  }
  
  return true;
});

// Handle add to Offerloop from content script
async function handleAddToOfferloop(request, sendResponse) {
  const { linkedInUrl } = request;
  
  try {
    // Get auth token from storage
    const { authToken, isLoggedIn } = await chrome.storage.local.get(['authToken', 'isLoggedIn']);
    
    if (!isLoggedIn || !authToken) {
      sendResponse({
        success: false,
        error: 'Please sign in to Offerloop first. Click the extension icon to sign in.',
      });
      return;
    }
    
    // Call the LinkedIn import API
    const result = await importLinkedInContact(linkedInUrl, authToken);
    
    if (result.error) {
      sendResponse({
        success: false,
        error: result.error,
      });
    } else {
      sendResponse({
        success: true,
        message: result.message || 'Contact added successfully!',
        contact: result.contact,
        credits_remaining: result.credits_remaining,
      });
    }
  } catch (error) {
    console.error('[Offerloop Background] Error in handleAddToOfferloop:', error);
    sendResponse({
      success: false,
      error: error.message || 'Failed to add contact',
    });
  }
}

// Handle import LinkedIn from popup
async function handleImportLinkedIn(request, sendResponse) {
  const { linkedInUrl, authToken } = request;
  
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    
    const result = await importLinkedInContact(linkedInUrl, authToken, false, request.createDraft !== false, request.emailMode || '');
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleImportLinkedIn:', error);
    sendResponse({ error: error.message || 'Failed to import contact' });
  }
}

// Import LinkedIn contact via API (with automatic token refresh on 401)
async function importLinkedInContact(linkedInUrl, authToken, isRetry = false, createDraft = true, emailMode = '') {
  console.log('[Offerloop Background] Importing LinkedIn contact:', linkedInUrl, 'draft:', createDraft);

  try {
    // 90s timeout: with create_draft the import-linkedin pipeline runs the
    // engine lookup + resume extraction + Perplexity + Apify +
    // batch_generate_emails + quality gate + Gmail draft, which can take
    // 30-60s. Without it (the panel's default since 2026-09-07) the route
    // stops after the contact + email and returns in a few seconds.
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/contacts/import-linkedin`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authToken}`,
      },
      body: JSON.stringify({
        linkedin_url: linkedInUrl,
        create_draft: createDraft,
        // fast: shelf or Hunter in seconds, FullEnrich in the background
        email_mode: emailMode || undefined,
      }),
    }, 90000);

    // Auto-refresh on 401 and retry once
    if (response.status === 401 && !isRetry) {
      console.log('[Offerloop Background] Got 401, attempting token refresh...');
      const newToken = await refreshAuthToken();
      if (newToken) {
        return importLinkedInContact(linkedInUrl, newToken, true, createDraft, emailMode);
      }
    }
    
    const data = await response.json();
    console.log('[Offerloop Background] API Response:', data);
    
    if (!response.ok) {
      throw new Error(data.message || data.error || `API error: ${response.status}`);
    }
    
    return {
      success: true,
      contact: data.contact,
      contact_id: data.contact_id,
      message: data.message,
      credits_remaining: data.credits_remaining,
      draft_created: data.draft_created,
      email_found: data.email_found,
      email_pending: data.email_pending === true,
      gmail_draft_url: data.gmail_draft_url,
    };
  } catch (error) {
    console.error('[Offerloop Background] API Error:', error);
    throw error;
  }
}

// Handle find similar contacts from popup
async function handleFindSimilarContacts(request, sendResponse) {
  const { authToken, source } = request;
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    if (!source || !source.company) {
      sendResponse({ error: 'Missing source company' });
      return;
    }
    const result = await findSimilarContacts(source, authToken);
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleFindSimilarContacts:', error);
    sendResponse({ error: error.message || 'Failed to find similar contacts' });
  }
}

// Call /api/contacts/find-similar (with automatic token refresh on 401)
async function findSimilarContacts(source, authToken, isRetry = false) {
  console.log('[Offerloop Background] Finding similar contacts for:', source);
  try {
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/contacts/find-similar`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authToken}`,
      },
      body: JSON.stringify({ source }),
    }, 45000);

    if (response.status === 401 && !isRetry) {
      console.log('[Offerloop Background] find-similar 401, refreshing token...');
      const newToken = await refreshAuthToken();
      if (newToken) return findSimilarContacts(source, newToken, true);
    }

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.message || data.error || `API error: ${response.status}`);
    }
    return {
      success: true,
      contacts: data.contacts || [],
      cap: data.cap,
      credits_used: data.credits_used,
      credits_remaining: data.credits_remaining,
    };
  } catch (error) {
    console.error('[Offerloop Background] find-similar API error:', error);
    throw error;
  }
}

// Handle draft for a contact whose email is already known (step two)
async function handleDraftContact(request, sendResponse) {
  const { contact, authToken } = request;
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    if (!contact || !contact.Email) {
      sendResponse({ error: 'No email to draft to' });
      return;
    }
    const result = await draftContactViaApi(contact, authToken);
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleDraftContact:', error);
    sendResponse({ error: error.message || 'Failed to create the draft' });
  }
}

// Call /api/emails/generate-and-draft for one contact (with automatic token
// refresh on 401). Same route the website's Find page uses after preview;
// the resume comes from the user's profile server-side.
async function draftContactViaApi(contact, authToken, isRetry = false) {
  console.log('[Offerloop Background] Drafting for:', contact.Email);
  try {
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/emails/generate-and-draft`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authToken}`,
      },
      body: JSON.stringify({ contacts: [contact] }),
    }, 90000);

    if (response.status === 401 && !isRetry) {
      console.log('[Offerloop Background] generate-and-draft 401, refreshing token...');
      const newToken = await refreshAuthToken();
      if (newToken) return draftContactViaApi(contact, newToken, true);
    }

    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.message || data.error || `API error: ${response.status}`);
    }
    const first = (data.drafts || [])[0] || {};
    return {
      success: true,
      gmail_draft_url: first.gmailUrl || '',
      delivery_mode: first.deliveryMode || data.deliveryMode || '',
      credits_remaining: data.credits_remaining,
    };
  } catch (error) {
    console.error('[Offerloop Background] generate-and-draft API error:', error);
    throw error;
  }
}

// Handle company people (side panel on a LinkedIn company page)
async function handleCompanyPeople(request, sendResponse) {
  const { company, authToken, role } = request;
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    if (!company) {
      sendResponse({ error: 'Missing company' });
      return;
    }
    const result = await fetchCompanyPeople(company, authToken, false, (role || '').trim(), {
      limit: request.limit || 10,
      prefetch: request.prefetch !== false,
      focus: (request.focus || '').trim(),
      dig: request.dig,
    });
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleCompanyPeople:', error);
    sendResponse({ error: error.message || 'Failed to load people at this company' });
  }
}

// The company deck (2026-09-07): the same ranker the app's People deck
// runs, narrowed to one firm. Ranked against the student (school, target
// roles, tenure), each row carrying the ranker's reason. Zero credits to
// browse; the top rows get a FullEnrich prefetch so a reveal is instant.
// Reason types the ranker emits, in plain words, used when it gives no sentence.
const REASON_LABELS = {
  alumni: 'Went to your school',
  dream_company: 'At one of your target companies',
  shared_company: 'Worked where you worked',
  career_path: 'On the path you want',
  shared_major: 'Studied what you studied',
  recent_transition: 'Recently changed roles',
};

async function fetchCompanyPeople(company, authToken, isRetry = false, role = '', opts = {}) {
  console.log('[Offerloop Background] Loading company deck:', company, role ? `role=${role}` : '');
  try {
    const params = new URLSearchParams({
      company,
      limit: String(opts.limit || 10),
      surface: 'extension',
      prefetch: opts.prefetch === false ? '0' : '1',
    });
    if (role) params.set('role', role);
    // dig: '1' asks the server to pull new people from LinkedIn for a dry
    // company (the student pressed the button); '0' forbids it (glance
    // lists). Unset means the server decides, which for the panel is no.
    if (opts.dig === '1' || opts.dig === '0') params.set('dig', opts.dig);
    if (opts.focus) params.set('focus', opts.focus);
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/ranker/candidates?${params.toString()}`, {
      method: 'GET',
      headers: { 'Authorization': `Bearer ${authToken}` },
    }, 60000);

    if (response.status === 401 && !isRetry) {
      console.log('[Offerloop Background] company deck 401, refreshing token...');
      const newToken = await refreshAuthToken();
      if (newToken) return fetchCompanyPeople(company, newToken, true, role, opts);
    }

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.message || data.error || `API error: ${response.status}`);
    }
    const rows = (data.candidates || []).map((c) => {
      const p = c.person || {};
      // Every reason in plain words, for the expanded card; the first one
      // (or the ranker's briefing sentence) is the row caption.
      const reasonLabels = (Array.isArray(c.reasons) ? c.reasons : [])
        .map((r) => (r && typeof r === 'object')
          ? (r.label || r.text || REASON_LABELS[r.type] || '') + (r.detail ? ` (${r.detail})` : '')
          : (typeof r === 'string' ? r : ''))
        .map((s) => s.trim())
        .filter(Boolean);
      const reasonTypes = (Array.isArray(c.reasons) ? c.reasons : [])
        .map((r) => (r && typeof r === 'object' ? (r.type || '') : ''))
        .filter(Boolean);
      return {
        reasonTypes,
        candidateId: p.id || '',
        firstName: p.firstName || '',
        lastName: p.lastName || '',
        title: p.title || '',
        company: p.company || company,
        linkedinUrl: p.linkedinUrl || '',
        school: p.college || '',
        city: p.city || '',
        state: p.state || '',
        joinedYear: p.joinedYear || null,
        photoUrl: p.photo_url || '',
        email: '',
        caption: c.briefing || reasonLabels[0] || '',
        briefing: c.briefing || '',
        reasons: reasonLabels,
        tier: c.tier || '',
        roleMatch: c.role_match === true,
        category: '',
      };
    });
    return {
      success: true,
      contacts: rows,
      deck_id: data.deck_id || '',
      reason: data.reason || '',
      chips: Array.isArray(data.chips) ? data.chips : [],
      can_dig: data.can_dig === true,
    };
  } catch (error) {
    console.error('[Offerloop Background] company deck API error:', error);
    throw error;
  }
}

// Handle the pending-email poll for a profile Find Email that went to the
// background. GET /api/contacts/<id>/email-status: found | miss | pending.
async function handleEmailStatus(request, sendResponse) {
  const { contactId, authToken } = request;
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    if (!contactId) {
      sendResponse({ error: 'Missing contact' });
      return;
    }
    const result = await fetchEmailStatus(contactId, authToken);
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleEmailStatus:', error);
    sendResponse({ error: error.message || 'Could not check the email' });
  }
}

async function fetchEmailStatus(contactId, authToken, isRetry = false) {
  const response = await fetchWithTimeout(
    `${API_BASE_URL}/api/contacts/${encodeURIComponent(contactId)}/email-status`, {
      method: 'GET',
      headers: { 'Authorization': `Bearer ${authToken}` },
    }, 20000);
  if (response.status === 401 && !isRetry) {
    const newToken = await refreshAuthToken();
    if (newToken) return fetchEmailStatus(contactId, newToken, true);
  }
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || data.error || `API error: ${response.status}`);
  }
  return {
    success: true,
    status: data.status || 'pending',
    email: data.email || '',
    source: data.source || '',
    refunded: !!data.refunded,
  };
}

// Handle reveal for a company-deck row (8 credits, refunded on a miss)
async function handleRevealEmail(request, sendResponse) {
  const { candidateId, deckId, authToken } = request;
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    if (!candidateId) {
      sendResponse({ error: 'Missing candidate' });
      return;
    }
    const result = await revealCandidateEmail(candidateId, deckId || '', authToken);
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleRevealEmail:', error);
    sendResponse({ error: error.message || 'Failed to find the email' });
  }
}

// POST /api/ranker/candidates/<id>/reveal-email: warehouse first, then the
// deck's prefetch, then one live FullEnrich lookup. Charged only when a
// verified address lands; the backend refunds a miss on its own.
async function revealCandidateEmail(candidateId, deckId, authToken, isRetry = false) {
  try {
    const response = await fetchWithTimeout(
      `${API_BASE_URL}/api/ranker/candidates/${encodeURIComponent(candidateId)}/reveal-email`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${authToken}`,
        },
        body: JSON.stringify({ deck_id: deckId, surface: 'extension' }),
      }, 90000);

    if (response.status === 401 && !isRetry) {
      const newToken = await refreshAuthToken();
      if (newToken) return revealCandidateEmail(candidateId, deckId, newToken, true);
    }

    const data = await response.json();
    if (response.status === 402) {
      throw new Error(`Not enough credits (need ${data.required}, have ${data.have})`);
    }
    if (!response.ok) {
      throw new Error(data.message || data.error || `API error: ${response.status}`);
    }
    return {
      success: true,
      email: data.email || '',
      verified: !!data.verified,
      charged: !!data.charged,
      cached: !!data.cached,
      reason: data.reason || '',
    };
  } catch (error) {
    console.error('[Offerloop Background] reveal-email API error:', error);
    throw error;
  }
}

// Handle get credits
async function handleGetCredits(request, sendResponse) {
  const { authToken } = request;
  
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/check-credits`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${authToken}`,
      },
    });
    
    const data = await response.json();
    
    if (!response.ok) {
      sendResponse({ error: data.message || 'Failed to get credits' });
      return;
    }
    
    sendResponse({
      credits: data.credits,
      maxCredits: data.max_credits,
      tier: data.tier,
      creditCosts: data.credit_costs || null,
    });
  } catch (error) {
    console.error('[Offerloop Background] Error getting credits:', error);
    sendResponse({ error: error.message });
  }
}

// Handle set auth token (can be called from popup after authentication)
async function handleSetAuthToken(request, sendResponse) {
  const { authToken } = request;
  
  try {
    await chrome.storage.local.set({
      authToken: authToken,
      isLoggedIn: !!authToken,
    });
    
    console.log('[Offerloop Background] Auth token updated');
    sendResponse({ success: true });
  } catch (error) {
    console.error('[Offerloop Background] Error setting auth token:', error);
    sendResponse({ error: error.message });
  }
}

// Handle get status
async function handleGetStatus(sendResponse) {
  try {
    const { authToken, isLoggedIn, credits } = await chrome.storage.local.get(['authToken', 'isLoggedIn', 'credits']);
    
    sendResponse({
      isLoggedIn: isLoggedIn || false,
      hasToken: !!authToken,
      credits: credits,
    });
  } catch (error) {
    sendResponse({ error: error.message });
  }
}

// Handle token refresh request from popup
async function handleRefreshToken(sendResponse) {
  try {
    const newToken = await refreshAuthToken();
    if (newToken) {
      // Read back the full stored state so the popup can sync its local state
      const stored = await chrome.storage.local.get(['authToken', 'isLoggedIn', 'userEmail', 'userName', 'userPhoto', 'credits']);
      sendResponse({ success: true, token: newToken, ...stored });
    } else {
      sendResponse({ success: false, error: 'Token refresh failed' });
    }
  } catch (error) {
    console.error('[Offerloop Background] Error in handleRefreshToken:', error);
    sendResponse({ success: false, error: error.message });
  }
}

// Log scraper result to backend (fire-and-forget, best-effort)
async function handleLogScraperResult(request) {
  try {
    const { authToken } = await chrome.storage.local.get(['authToken']);
    if (!authToken) return;

    await fetchWithTimeout(`${API_BASE_URL}/api/extension/scraper-log`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authToken}`,
      },
      body: JSON.stringify({
        platform: request.platform || 'unknown',
        success: request.success,
        fields_found: request.fieldsFound || [],
        url_pattern: request.urlPattern || '',
        timestamp: new Date().toISOString(),
      }),
    }, 5000); // Short 5s timeout — this is best-effort
  } catch (e) {
    // Silently ignore — this is telemetry, not critical
    console.log('[Offerloop Background] Scraper log failed (non-critical):', e.message);
  }
}

// Handle context menu click
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'offerloop-add' && info.linkUrl) {
    // Send message to handle the import
    handleAddToOfferloop({ linkedInUrl: info.linkUrl }, (response) => {
      // Show notification with result
      const message = response.success 
        ? response.message || 'Contact added to Offerloop!'
        : response.error || 'Failed to add contact';
      
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon128.png',
        title: 'Offerloop',
        message: message,
      });
    });
  }
});