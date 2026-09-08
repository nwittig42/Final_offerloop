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
    
    const result = await importLinkedInContact(linkedInUrl, authToken, false, request.createDraft !== false);
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleImportLinkedIn:', error);
    sendResponse({ error: error.message || 'Failed to import contact' });
  }
}

// Import LinkedIn contact via API (with automatic token refresh on 401)
async function importLinkedInContact(linkedInUrl, authToken, isRetry = false, createDraft = true) {
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
      }),
    }, 90000);

    // Auto-refresh on 401 and retry once
    if (response.status === 401 && !isRetry) {
      console.log('[Offerloop Background] Got 401, attempting token refresh...');
      const newToken = await refreshAuthToken();
      if (newToken) {
        return importLinkedInContact(linkedInUrl, newToken, true, createDraft);
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
      message: data.message,
      credits_remaining: data.credits_remaining,
      draft_created: data.draft_created,
      email_found: data.email_found,
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
  const { company, authToken } = request;
  try {
    if (!authToken) {
      sendResponse({ error: 'Not authenticated' });
      return;
    }
    if (!company) {
      sendResponse({ error: 'Missing company' });
      return;
    }
    const result = await fetchCompanyPeople(company, authToken);
    sendResponse(result);
  } catch (error) {
    console.error('[Offerloop Background] Error in handleCompanyPeople:', error);
    sendResponse({ error: error.message || 'Failed to load people at this company' });
  }
}

// Call /api/contacts/company-people (with automatic token refresh on 401).
// Zero credits: this is the browse step, the email step charges per row.
async function fetchCompanyPeople(company, authToken, isRetry = false) {
  console.log('[Offerloop Background] Loading people at:', company);
  try {
    const response = await fetchWithTimeout(`${API_BASE_URL}/api/contacts/company-people`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${authToken}`,
      },
      body: JSON.stringify({ company, limit: 10 }),
    }, 45000);

    if (response.status === 401 && !isRetry) {
      console.log('[Offerloop Background] company-people 401, refreshing token...');
      const newToken = await refreshAuthToken();
      if (newToken) return fetchCompanyPeople(company, newToken, true);
    }

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.message || data.error || `API error: ${response.status}`);
    }
    return {
      success: true,
      contacts: data.contacts || [],
      cap: data.cap,
      credits_remaining: data.credits_remaining,
    };
  } catch (error) {
    console.error('[Offerloop Background] company-people API error:', error);
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