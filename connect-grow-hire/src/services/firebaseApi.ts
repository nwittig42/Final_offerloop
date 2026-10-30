// src/services/firebaseApi.ts
import { db } from '../lib/firebase';
import { API_BASE_URL } from './api';
import {
  collection,
  doc,
  getDoc,
  getDocs,
  setDoc,
  updateDoc,
  deleteDoc,
  query,
  where,
  orderBy,
  limit,
  Timestamp,
  writeBatch,
} from 'firebase/firestore';

// ================================
// TYPES
// ================================
export interface Contact {
  id?: string;
  firstName: string;
  lastName: string;
  linkedinUrl: string;
  email: string;
  company: string;
  jobTitle: string;
  college: string;
  location: string;
  firstContactDate: string;
  status: string;
  lastContactDate: string;
  emailSubject?: string;
  emailBody?: string;
  createdAt?: string;
  updatedAt?: string;

  // ================================
  // NEW: Gmail tracking fields
  // ================================
  gmailThreadId?: string;
  gmailMessageId?: string;
  gmailDraftId?: string;
  gmailDraftUrl?: string;
  hasUnreadReply?: boolean;
  notificationsMuted?: boolean;
  draftCreatedAt?: string;
  lastChecked?: string;
  mutedAt?: string;

  // Warmth & personalization
  warmthScore?: number;
  warmthTier?: string;
  warmthSignals?: string[];
  personalizationLabel?: string;
  personalizationType?: string;
  briefing?: string;
  qualityRegenerated?: boolean;

  // Free-form note attached by the user from My Network. Persisted via
  // updateContact; surfaced inline under the row in the network table.
  notes?: string;
}

export interface ProfessionalInfo {
  firstName: string;
  lastName: string;
  university: string;
  currentDegree: string;
  fieldOfStudy: string;
  graduationYear: string;
  targetIndustries: string[];
  linkedinUrl?: string;
  careerGoals?: string;
}

export interface UserData {
  email: string;
  name: string;
  credits: number;
  maxCredits: number;
  tier: 'free' | 'pro';
  createdAt: string;
  lastLogin?: string;
  professionalInfo?: ProfessionalInfo;
  needsOnboarding?: boolean;
}

// ================================
// CALENDAR TYPES
// ================================
export interface CalendarEvent {
  id?: string;
  title: string;
  contactId?: string;
  contactName: string;
  firm: string;
  date: string; // ISO date format like "2025-12-05"
  time: string; // like "14:00"
  duration: number; // minutes, default 30
  type: 'video' | 'phone' | 'in-person';
  status: 'pending' | 'confirmed' | 'completed' | 'cancelled';
  meetingLink?: string;
  notes?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface FollowUpReminder {
  id: string;
  contactId: string;
  contactName: string;
  firm: string;
  daysSinceContact: number;
  lastContactDate: string;
}

export interface ManualFirm {
  id?: string;
  name: string;
  industry?: string;
  hq?: string;
  createdAt?: string;
  updatedAt?: string;
}

// ================================
// RECRUITER TYPES
// ================================
export interface Recruiter {
  id?: string;
  firstName: string;
  lastName: string;
  linkedinUrl: string;
  email: string;
  company: string;
  jobTitle: string;
  location: string;
  phone?: string;
  workEmail?: string;
  personalEmail?: string;
  associatedJobId?: string;
  associatedJobTitle?: string;
  associatedJobUrl?: string;
  dateAdded: string;
  status: string;
  createdAt?: string;
  updatedAt?: string;
  // Gmail draft tracking fields
  gmailMessageId?: string;
  gmailDraftId?: string;
  gmailDraftUrl?: string;
  // Sent tracking fields (set when an email is sent in send mode)
  gmailThreadId?: string;
  pipelineStage?: string;
  emailSentAt?: string;
}

// ================================
// API
// ================================
export const firebaseApi = {
  // ----------------
  // USER MANAGEMENT
  // ----------------
  async createUser(uid: string, userData: Partial<UserData>): Promise<void> {
    const userRef = doc(db, 'users', uid);
    await setDoc(
      userRef,
      {
        ...userData,
        createdAt: userData.createdAt || new Date().toISOString(),
        lastLogin: new Date().toISOString(),
      },
      { merge: true }
    );
  },

  async getUser(uid: string): Promise<UserData | null> {
    const userRef = doc(db, 'users', uid);
    const userSnap = await getDoc(userRef);
    if (!userSnap.exists()) return null;
    return userSnap.data() as UserData;
  },

  async updateUser(uid: string, updates: Partial<UserData>): Promise<void> {
    const userRef = doc(db, 'users', uid);
    await updateDoc(userRef, {
      ...updates,
      lastLogin: new Date().toISOString(),
    });
  },

  async updateCredits(uid: string, credits: number): Promise<void> {
    const userRef = doc(db, 'users', uid);
    await updateDoc(userRef, { credits });
  },

  // ----------------
  // PROFESSIONAL INFO
  // ----------------
  async saveProfessionalInfo(uid: string, info: ProfessionalInfo): Promise<void> {
    const userRef = doc(db, 'users', uid);
    await updateDoc(userRef, {
      professionalInfo: info,
      needsOnboarding: false,
    });
  },

  async getProfessionalInfo(uid: string): Promise<ProfessionalInfo | null> {
    const userRef = doc(db, 'users', uid);
    const userSnap = await getDoc(userRef);
    if (!userSnap.exists()) return null;

    const userData = userSnap.data() as UserData;
    return userData.professionalInfo || null;
  },

  async getUserOnboardingData(uid: string): Promise<{
    firstName: string;
    university: string;
    universityOptOut: boolean;
    graduationYear: string;
    targetIndustries: string[];
    preferredLocations: string[];
    dreamCompanies: string[];
    careerTrack: string;
    preferredJobRole: string;
    targetFirms: string[];
    extractedRoles: string[];
    directionNarrative: string;
    personalContext: string;
  }> {
    const userRef = doc(db, 'users', uid);
    const userSnap = await getDoc(userRef);
    const d = userSnap.exists() ? userSnap.data() : {} as Record<string, unknown>;
    const pi = (d.professionalInfo || {}) as Record<string, unknown>;
    const academics = (d.academics || {}) as Record<string, unknown>;
    const loc = (d.location || {}) as Record<string, unknown>;
    const profile = (d.profile || {}) as Record<string, unknown>;

    // Check resumeParsed in Firestore (from uploaded resume)
    const resumeParsed = (d.resumeParsed || {}) as Record<string, unknown>;
    const resumeEducation = (resumeParsed.education || {}) as Record<string, unknown>;
    // Onboarding v2 has the user confirm (or correct) the school parsed from
    // their resume. Once confirmed, their answer beats the parse — the same
    // order the backend's get_user_school uses.
    const onboarding = (d.onboarding || {}) as Record<string, unknown>;
    const confirmedUniversity = onboarding.profileConfirmedAt ? ((academics.university as string) || '') : '';
    // A confirmed "I'm not in school" also beats the parse: only a school the
    // user set afterwards (Settings, or Find's school prompt) counts. Mirrors
    // the backend's get_user_school.
    const confirmedNoSchool = !!onboarding.profileConfirmedAt && d.universityOptOut === true;
    // Also check localStorage resume data for university
    let resumeUniversity = '';
    try {
      const rd = JSON.parse(localStorage.getItem('resumeData') || '{}');
      resumeUniversity = rd.university || '';
    } catch { /* ignore */ }

    return {
      firstName: (pi.firstName as string) || (d.firstName as string) || (profile.firstName as string) || (d.name as string) || '',
      // Prefer the most recently parsed resume value first — that's the freshest source of truth.
      // Then resumeEducation (parser_v2 list shape), then top-level overrides, then legacy
      // onboarding paths. This prevents stale "Alabama State University" defaults from old
      // onboarding test entries shadowing the real resume-parsed value.
      university: confirmedNoSchool
        ? confirmedUniversity || (d.university as string) || ''
        : confirmedUniversity ||
          (resumeParsed.university as string) ||
          ((resumeEducation as any)?.university as string) ||
          (Array.isArray(resumeParsed.education) && (resumeParsed.education as any[])[0]?.university) ||
          (d.university as string) ||
          (pi.university as string) ||
          (academics.university as string) ||
          (d.college as string) ||
          resumeUniversity ||
          '',
      // Explicit "I don't have a school" opt-out. Lets Find render without the
      // school wall for users with no university on file.
      universityOptOut: d.universityOptOut === true,
      graduationYear: (pi.graduationYear as string) || (academics.graduationYear as string) || (d.graduationYear as string) || '',
      // Profile-page Direction extractor saves chips at the top level (`targetIndustries`, `targetFirms`, `extractedRoles`, `preferredLocations`).
      // Read those first, then fall through to legacy onboarding fields.
      targetIndustries: (d.targetIndustries as string[]) || (pi.targetIndustries as string[]) || (d.industriesOfInterest as string[]) || (d.interests as string[]) || (loc.interests as string[]) || (loc.careerInterests as string[]) || [],
      preferredLocations: (d.preferredLocations as string[]) || (d.targetLocations as string[]) || (d.preferredLocation as string[]) || (loc.preferredLocation as string[]) || [],
      dreamCompanies: (d.dreamCompanies as string[]) || [],
      careerTrack: (d.careerTrack as string) || '',
      preferredJobRole: (d.preferredJobRole as string) || (d.preferredJobRolesOrTitles as string) || '',
      targetFirms: (d.targetFirms as string[]) || (d.dreamCompanies as string[]) || [],
      extractedRoles: (d.extractedRoles as string[]) || (d.targetRoles as string[]) || [],
      directionNarrative: (d.directionNarrative as string) || '',
      personalContext: (d.personalContext as string) || (d.careerGoals as string) || '',
    };
  },

  // ----------------
  // CONTACT MANAGEMENT
  // ----------------
  async createContact(uid: string, contact: Omit<Contact, 'id'>): Promise<string> {
    const contactsRef = collection(db, 'users', uid, 'contacts');
    const newContactRef = doc(contactsRef);

    const contactData = {
      ...contact,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };

    await setDoc(newContactRef, contactData);
    return newContactRef.id;
  },

  async bulkCreateContacts(uid: string, contacts: Omit<Contact, 'id'>[]): Promise<void> {
    // Use backend API endpoint which has proper deduplication logic
    // This prevents duplicate contacts from being created
    try {
      // Get auth token for API call
      const { getAuth } = await import('firebase/auth');
      const auth = getAuth();
      const user = auth.currentUser;
      if (!user) {
        throw new Error('User not authenticated');
      }
      const idToken = await user.getIdToken();


      // Convert contacts to backend format (camelCase to match backend expectations)
      const backendContacts = contacts.map((c) => ({
        FirstName: c.firstName,
        LastName: c.lastName,
        Email: c.email,
        LinkedIn: c.linkedinUrl,
        Company: c.company,
        Title: c.jobTitle,
        College: c.college,
        location: c.location,
        emailSubject: c.emailSubject,
        emailBody: c.emailBody,
        gmailDraftId: c.gmailDraftId,
        gmailDraftUrl: c.gmailDraftUrl,
        gmailThreadId: c.gmailThreadId,
        gmailMessageId: c.gmailMessageId,
      }));

      // DEBUG: Log first backend contact to see email fields being sent
      if (backendContacts.length > 0) {
        console.log('[DEBUG] bulkCreateContacts - First backend contact being sent:', {
          emailSubject: backendContacts[0].emailSubject || 'MISSING',
          emailBody: backendContacts[0].emailBody ? `${backendContacts[0].emailBody.substring(0, 100)}...` : 'MISSING',
          allKeys: Object.keys(backendContacts[0]),
        });
      }

      // Call backend API with deduplication (endpoint is /contacts/bulk, API_BASE_URL already includes /api)
      const response = await fetch(`${API_BASE_URL}/contacts/bulk`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${idToken}`,
        },
        body: JSON.stringify({ contacts: backendContacts }),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(error.error || `Failed to bulk create contacts: ${response.statusText}`);
      }

      const result = await response.json();
      console.log(`Bulk create contacts: ${result.created} created, ${result.skipped} skipped (duplicates)`);
    } catch (error) {
      console.error('Error in bulkCreateContacts:', error);
      throw error;
    }
  },

  async getContacts(uid: string): Promise<Contact[]> {
    const contactsRef = collection(db, 'users', uid, 'contacts');
    const snapshot = await getDocs(contactsRef);
    return snapshot.docs.map((d) => ({ id: d.id, ...d.data() })) as Contact[];
  },

  async getContact(uid: string, contactId: string): Promise<Contact | null> {
    const contactRef = doc(db, 'users', uid, 'contacts', contactId);
    const contactSnap = await getDoc(contactRef);
    if (!contactSnap.exists()) return null;

    return { id: contactSnap.id, ...contactSnap.data() } as Contact;
  },

  async updateContact(uid: string, contactId: string, updates: Partial<Contact>): Promise<void> {
    const contactRef = doc(db, 'users', uid, 'contacts', contactId);
    await updateDoc(contactRef, {
      ...updates,
      updatedAt: new Date().toISOString(),
    });
  },

  async deleteContact(uid: string, contactId: string): Promise<void> {
    try {
      // Get auth token for API call
      const { getAuth } = await import('firebase/auth');
      const auth = getAuth();
      const user = auth.currentUser;
      if (!user) {
        throw new Error('User not authenticated');
      }
      const idToken = await user.getIdToken();


      const response = await fetch(`${API_BASE_URL}/contacts/${contactId}`, {
        method: 'DELETE',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${idToken}`,
        },
      });

      if (!response.ok) {
        throw new Error(`Failed to delete contact: ${response.statusText}`);
      }
    } catch (error) {
      console.error('Error deleting contact:', error);
      throw error;
    }
  },

  async clearAllContacts(uid: string): Promise<void> {
    const contactsRef = collection(db, 'users', uid, 'contacts');
    const snapshot = await getDocs(contactsRef);
    const batch = writeBatch(db);

    snapshot.docs.forEach((d) => batch.delete(d.ref));
    await batch.commit();
  },

  async findContactByEmail(uid: string, email: string): Promise<Contact | null> {
    const contactsRef = collection(db, 'users', uid, 'contacts');
    const q = query(contactsRef, where('email', '==', email));
    const snapshot = await getDocs(q);

    if (snapshot.empty) return null;

    const d = snapshot.docs[0];
    return { id: d.id, ...d.data() } as Contact;
  },

  // ================================
  // ACTIVITY LOGGING
  // ================================
  async logActivity(
    uid: string,
    type: 'firmSearch' | 'contactSearch' | 'coffeePrep',
    summary: string,
    metadata?: any
  ): Promise<void> {
    try {
      console.log('📝 Logging activity:', { uid, type, summary, metadata });
      const activitiesRef = collection(db, 'users', uid, 'activity');
      const activityDoc = doc(activitiesRef);
      await setDoc(activityDoc, {
        type,
        summary,
        timestamp: Timestamp.now(),
        metadata: metadata || {},
      });
      console.log('✅ Activity logged successfully');
    } catch (error) {
      console.error('❌ Failed to log activity:', error);
      throw error; // Re-throw so caller knows it failed
    }
  },

  async getActivities(uid: string, limitCount: number = 10): Promise<Array<{
    id: string;
    type: string;
    summary: string;
    timestamp: any;
    metadata?: any;
  }>> {
    try {
      const activitiesRef = collection(db, 'users', uid, 'activity');
      
      // Try with orderBy first, fallback to getting all and sorting client-side if index doesn't exist
      let snapshot;
      try {
        const q = query(activitiesRef, orderBy('timestamp', 'desc'), limit(limitCount));
        snapshot = await getDocs(q);
      } catch (error: any) {
        // If orderBy fails (likely missing index), get all and sort client-side
        console.warn('Firestore index may be missing, fetching all activities and sorting client-side:', error);
        const allSnapshot = await getDocs(activitiesRef);
        const allActivities = allSnapshot.docs.map((d) => ({
          id: d.id,
          ...d.data(),
        })) as Array<{
          id: string;
          type: string;
          summary: string;
          timestamp: any;
          metadata?: any;
        }>;
        
        // Sort by timestamp desc and limit
        return allActivities
          .filter(a => a.timestamp)
          .sort((a, b) => {
            const aTime = a.timestamp?.toMillis?.() || 0;
            const bTime = b.timestamp?.toMillis?.() || 0;
            return bTime - aTime;
          })
          .slice(0, limitCount);
      }
      
      return snapshot.docs.map((d) => ({
        id: d.id,
        ...d.data(),
      })) as Array<{
        id: string;
        type: string;
        summary: string;
        timestamp: any;
        metadata?: any;
      }>;
    } catch (error) {
      console.error('Error fetching activities:', error);
      return [];
    }
  },

  // ================================
  // GOALS MANAGEMENT
  // ================================
  async getGoals(uid: string): Promise<Array<{
    id: string;
    type: string;
    target: number;
    period: string;
    startDate: any;
    endDate: any;
  }>> {
    try {
      const goalsRef = collection(db, 'users', uid, 'goals');
      const snapshot = await getDocs(goalsRef);
      
      return snapshot.docs.map((d) => ({
        id: d.id,
        ...d.data(),
      })) as Array<{
        id: string;
        type: string;
        target: number;
        period: string;
        startDate: any;
        endDate: any;
      }>;
    } catch (error) {
      console.error('Error fetching goals:', error);
      return [];
    }
  },

  async createGoal(
    uid: string,
    goal: {
      type: 'contacts' | 'firms' | 'coffeeChats' | 'outreach';
      target: number;
      period: 'month' | 'week' | 'year';
      startDate: Timestamp;
      endDate: Timestamp;
    }
  ): Promise<string> {
    try {
      const goalsRef = collection(db, 'users', uid, 'goals');
      const goalDoc = doc(goalsRef);
      await setDoc(goalDoc, goal);
      return goalDoc.id;
    } catch (error) {
      console.error('Error creating goal:', error);
      throw error;
    }
  },

  async updateUserStreak(uid: string, streakData: {
    currentStreak: number;
    longestStreak: number;
    lastActivityDate: string;
  }): Promise<void> {
    try {
      const userRef = doc(db, 'users', uid);
      await updateDoc(userRef, {
        currentStreak: streakData.currentStreak,
        longestStreak: streakData.longestStreak,
        lastActivityDate: streakData.lastActivityDate,
      });
    } catch (error) {
      console.error('Error updating user streak:', error);
      // Don't throw - streak update shouldn't break activity logging
    }
  },

  async getUserStreak(uid: string): Promise<{
    currentStreak: number;
    longestStreak: number;
    lastActivityDate: string | null;
  } | null> {
    try {
      const userRef = doc(db, 'users', uid);
      const userDoc = await getDoc(userRef);
      
      if (!userDoc.exists()) {
        return null;
      }
      
      const data = userDoc.data();
      return {
        currentStreak: data.currentStreak || 0,
        longestStreak: data.longestStreak || 0,
        lastActivityDate: data.lastActivityDate || null,
      };
    } catch (error) {
      console.error('Error fetching user streak:', error);
      return null;
    }
  },

  // ================================
  // CALENDAR MANAGEMENT
  // ================================
  async createCalendarEvent(uid: string, event: Omit<CalendarEvent, 'id' | 'createdAt' | 'updatedAt'>): Promise<string> {
    try {
      const eventsRef = collection(db, 'users', uid, 'calendar_events');
      const newEventRef = doc(eventsRef);
      
      // Remove undefined values (Firestore doesn't accept undefined)
      const eventData: Record<string, any> = {
        title: event.title,
        contactName: event.contactName,
        firm: event.firm,
        date: event.date,
        time: event.time,
        duration: event.duration,
        type: event.type,
        status: event.status,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      
      // Only add optional fields if they have values
      if (event.contactId) {
        eventData.contactId = event.contactId;
      }
      if (event.meetingLink) {
        eventData.meetingLink = event.meetingLink;
      }
      if (event.notes) {
        eventData.notes = event.notes;
      }
      
      await setDoc(newEventRef, eventData);
      return newEventRef.id;
    } catch (error) {
      console.error('Error creating calendar event:', error);
      throw error;
    }
  },

  /**
   * Fetches calendar events for a user.
   * 
   * REQUIRED FIRESTORE INDEX: calendar_events collection needs composite index on (date ASC, time ASC)
   * Deploy with: firebase deploy --only firestore:indexes
   * 
   * @param uid - User ID
   * @param month - Optional month (0-indexed, 0 = January). If not provided, returns all events.
   * @param year - Optional year. If not provided, returns all events.
   */
  async getCalendarEvents(uid: string, month?: number, year?: number): Promise<CalendarEvent[]> {
    try {
      const eventsRef = collection(db, 'users', uid, 'calendar_events');
      
      // Try to query with orderBy, but fallback if index doesn't exist
      let snapshot;
      try {
        const q = query(eventsRef, orderBy('date', 'asc'), orderBy('time', 'asc'));
        snapshot = await getDocs(q);
      } catch (error: any) {
        // If orderBy fails (likely missing index), get all and sort client-side
        console.warn('Firestore index may be missing, fetching all events and sorting client-side:', error);
        snapshot = await getDocs(eventsRef);
      }
      
      let events = snapshot.docs.map((d) => ({ id: d.id, ...d.data() })) as CalendarEvent[];
      
      // Sort client-side if we didn't use orderBy
      events.sort((a, b) => {
        const dateCompare = a.date.localeCompare(b.date);
        if (dateCompare !== 0) return dateCompare;
        return a.time.localeCompare(b.time);
      });
      
      // Filter by month/year if provided
      if (month !== undefined && year !== undefined) {
        events = events.filter(event => {
          if (!event.date) return false;
          // Parse date string (format: "yyyy-MM-dd")
          const [eventYear, eventMonth] = event.date.split('-').map(Number);
          // Note: month is 0-indexed in JS Date, but our filter uses 0-indexed too
          return eventMonth - 1 === month && eventYear === year;
        });
        console.log(`📅 Fetched ${events.length} calendar events for month ${month + 1}/${year}`);
      } else {
        console.log(`📅 Fetched ${events.length} calendar events (all events, no month/year filter)`);
      }
      
      return events;
    } catch (error) {
      console.error('Error fetching calendar events:', error);
      return [];
    }
  },

  async updateCalendarEvent(uid: string, eventId: string, updates: Partial<CalendarEvent>): Promise<void> {
    try {
      const eventRef = doc(db, 'users', uid, 'calendar_events', eventId);
      
      // Remove undefined values (Firestore doesn't accept undefined)
      const updateData: Record<string, any> = {
        updatedAt: new Date().toISOString(),
      };
      
      // Only include defined fields
      Object.keys(updates).forEach(key => {
        const value = updates[key as keyof CalendarEvent];
        if (value !== undefined) {
          updateData[key] = value;
        }
      });
      
      await updateDoc(eventRef, updateData);
    } catch (error) {
      console.error('Error updating calendar event:', error);
      throw error;
    }
  },

  async deleteCalendarEvent(uid: string, eventId: string): Promise<void> {
    try {
      const eventRef = doc(db, 'users', uid, 'calendar_events', eventId);
      await deleteDoc(eventRef);
    } catch (error) {
      console.error('Error deleting calendar event:', error);
      throw error;
    }
  },

  async getFollowUpReminders(uid: string): Promise<FollowUpReminder[]> {
    try {
      const contacts = await firebaseApi.getContacts(uid);
      const now = new Date();
      const threeDaysAgo = new Date(now);
      threeDaysAgo.setDate(threeDaysAgo.getDate() - 3);
      
      // Get upcoming events to exclude contacts with scheduled meetings
      const upcomingEvents = await firebaseApi.getCalendarEvents(uid);
      const upcomingEventContactIds = new Set(
        upcomingEvents
          .filter(e => {
            const eventDate = new Date(`${e.date}T${e.time}`);
            return eventDate > now;
          })
          .map(e => e.contactId)
          .filter(Boolean) as string[]
      );
      
      const reminders: FollowUpReminder[] = [];
      
      for (const contact of contacts) {
        // Skip if contact has upcoming event
        if (contact.id && upcomingEventContactIds.has(contact.id)) {
          continue;
        }
        
        // Skip if status is 'Replied' or 'Meeting Scheduled'
        if (contact.status === 'Replied' || contact.status === 'Meeting Scheduled') {
          continue;
        }
        
        // Check if firstContactDate is more than 3 days ago
        if (contact.firstContactDate) {
          const contactDate = new Date(contact.firstContactDate);
          if (contactDate < threeDaysAgo) {
            const daysSince = Math.floor((now.getTime() - contactDate.getTime()) / (1000 * 60 * 60 * 24));
            
            reminders.push({
              id: contact.id || '',
              contactId: contact.id || '',
              contactName: `${contact.firstName} ${contact.lastName}`.trim() || contact.email,
              firm: contact.company || '',
              daysSinceContact: daysSince,
              lastContactDate: contact.firstContactDate,
            });
          }
        }
      }
      
      // Sort by daysSinceContact descending
      reminders.sort((a, b) => b.daysSinceContact - a.daysSinceContact);
      
      return reminders;
    } catch (error) {
      console.error('Error fetching follow-up reminders:', error);
      return [];
    }
  },

  // ================================
  // CONTACT SEARCH (for autocomplete)
  // ================================
  async searchContacts(uid: string, searchQuery: string, limitCount: number = 10): Promise<Contact[]> {
    try {
      const contacts = await firebaseApi.getContacts(uid);
      const queryLower = searchQuery.toLowerCase();
      
      return contacts
        .filter(contact => {
          const fullName = `${contact.firstName} ${contact.lastName}`.toLowerCase();
          const email = contact.email.toLowerCase();
          const company = (contact.company || '').toLowerCase();
          
          return fullName.includes(queryLower) || 
                 email.includes(queryLower) || 
                 company.includes(queryLower);
        })
        .slice(0, limitCount);
    } catch (error) {
      console.error('Error searching contacts:', error);
      return [];
    }
  },

  // ================================
  // TIMELINE MANAGEMENT
  // ================================
  async saveTimeline(uid: string, timelineData: {
    phases: Array<{
      name: string;
      startMonth: string;
      endMonth: string;
      goals: string[];
      description: string;
    }>;
    startDate: string;
    targetDeadline: string;
    lastPrompt?: string;
    updatedAt?: string;
  }): Promise<void> {
    const userRef = doc(db, 'users', uid);
    await updateDoc(userRef, {
      timeline: {
        ...timelineData,
        updatedAt: new Date().toISOString(),
      },
    });
  },

  async getTimeline(uid: string): Promise<{
    phases: Array<{
      name: string;
      startMonth: string;
      endMonth: string;
      goals: string[];
      description: string;
    }>;
    startDate: string;
    targetDeadline: string;
    lastPrompt?: string;
    updatedAt?: string;
  } | null> {
    try {
      const userRef = doc(db, 'users', uid);
      const userSnap = await getDoc(userRef);
      if (!userSnap.exists()) {
        console.log('❌ User document does not exist');
        return null;
      }
      const data = userSnap.data();
      const timeline = data.timeline;
      if (!timeline || !timeline.phases || timeline.phases.length === 0) {
        console.log('❌ No timeline data found in user document');
        return null;
      }
      console.log('✅ Found timeline in Firestore:', timeline);
      // Ensure lastPrompt is included if it exists
      return {
        ...timeline,
        lastPrompt: timeline.lastPrompt || '',
      };
    } catch (error) {
      console.error('❌ Error getting timeline:', error);
      return null;
    }
  },

  // ================================
  // RECRUITER MANAGEMENT
  // ================================
  async getRecruiters(uid: string): Promise<Recruiter[]> {
    try {
      const recruitersRef = collection(db, 'users', uid, 'recruiters');
      const snapshot = await getDocs(recruitersRef);
      return snapshot.docs.map((d) => ({ id: d.id, ...d.data() })) as Recruiter[];
    } catch (error) {
      console.error('Error fetching recruiters:', error);
      return [];
    }
  },

  async getRecruiter(uid: string, recruiterId: string): Promise<Recruiter | null> {
    try {
      const recruiterRef = doc(db, 'users', uid, 'recruiters', recruiterId);
      const recruiterSnap = await getDoc(recruiterRef);
      if (!recruiterSnap.exists()) return null;
      return { id: recruiterSnap.id, ...recruiterSnap.data() } as Recruiter;
    } catch (error) {
      console.error('Error fetching recruiter:', error);
      return null;
    }
  },

  async updateRecruiter(uid: string, recruiterId: string, updates: Partial<Recruiter>): Promise<void> {
    try {
      const recruiterRef = doc(db, 'users', uid, 'recruiters', recruiterId);
      await updateDoc(recruiterRef, {
        ...updates,
        updatedAt: new Date().toISOString(),
      });
    } catch (error) {
      console.error('Error updating recruiter:', error);
      throw error;
    }
  },

  async bulkCreateRecruiters(uid: string, recruiters: Omit<Recruiter, 'id'>[]): Promise<void> {
    try {
      const batch = writeBatch(db);
      const recruitersRef = collection(db, 'users', uid, 'recruiters');

      for (const recruiter of recruiters) {
        const newRecruiterRef = doc(recruitersRef);
        const recruiterData = {
          ...recruiter,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        };
        batch.set(newRecruiterRef, recruiterData);
      }

      await batch.commit();
    } catch (error) {
      console.error('Error bulk creating recruiters:', error);
      throw error;
    }
  },

  async deleteRecruiter(uid: string, recruiterId: string): Promise<void> {
    try {
      const recruiterRef = doc(db, 'users', uid, 'recruiters', recruiterId);
      await deleteDoc(recruiterRef);
    } catch (error) {
      console.error('Error deleting recruiter:', error);
      throw error;
    }
  },

  // ================================
  // MANUAL FIRMS (user-added companies on My Network)
  // ================================
  async getManualFirms(uid: string): Promise<ManualFirm[]> {
    try {
      const firmsRef = collection(db, 'users', uid, 'manual_firms');
      const snapshot = await getDocs(firmsRef);
      return snapshot.docs.map((d) => ({ id: d.id, ...d.data() })) as ManualFirm[];
    } catch (error) {
      console.error('Error fetching manual firms:', error);
      return [];
    }
  },

  async createManualFirm(uid: string, firm: Omit<ManualFirm, 'id'>): Promise<string> {
    const firmsRef = collection(db, 'users', uid, 'manual_firms');
    const newRef = doc(firmsRef);
    await setDoc(newRef, {
      ...firm,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
    return newRef.id;
  },

  async deleteManualFirm(uid: string, firmId: string): Promise<void> {
    try {
      const firmRef = doc(db, 'users', uid, 'manual_firms', firmId);
      await deleteDoc(firmRef);
    } catch (error) {
      console.error('Error deleting manual firm:', error);
      throw error;
    }
  },

  async clearAllRecruiters(uid: string): Promise<void> {
    try {
      const recruitersRef = collection(db, 'users', uid, 'recruiters');
      const snapshot = await getDocs(recruitersRef);
      const batch = writeBatch(db);

      snapshot.docs.forEach((d) => batch.delete(d.ref));
      await batch.commit();
    } catch (error) {
      console.error('Error clearing recruiters:', error);
      throw error;
    }
  },
  // ================================
  // FEATURE FLAGS
  // ================================
  async getFeatureFlags(): Promise<{ suggestions_enabled?: boolean }> {
    try {
      const flagDoc = await getDoc(doc(db, 'system', 'feature_flags'));
      return flagDoc.exists() ? (flagDoc.data() as { suggestions_enabled?: boolean }) : {};
    } catch {
      return {};
    }
  },
};

export default firebaseApi;
