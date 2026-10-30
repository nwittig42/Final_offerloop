/**
 * ProfilePreview — visual mockup of the proposed Profile page.
 * Lives at /dev/profile-preview. Static design preview, no live data.
 *
 * Visual language matches the marketing site: soft-blue field, serif section
 * headings (Instrument Serif, NOT italic), blue accents, white cards.
 *
 * Sample data is intentionally fictional (Alex Chen at Stanford CS).
 * Spec: docs/PROFILE_ONBOARDING_SPEC.md
 */

import React, { useState, useEffect, useRef, useContext, createContext, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { doc, getDoc, updateDoc } from 'firebase/firestore';
import { ref as storageRef, uploadBytes, getDownloadURL, deleteObject, getStorage } from 'firebase/storage';
import { auth, db } from '@/lib/firebase';
import { useFirebaseAuth } from '@/contexts/FirebaseAuthContext';
import { onAuthStateChanged } from 'firebase/auth';
import { BACKEND_URL } from '@/services/api';
import { useToast } from '@/hooks/use-toast';
import { SidebarProvider } from '@/components/ui/sidebar';
import { AppSidebar } from '@/components/AppSidebar';
import { AppHeader } from '@/components/AppHeader';
import { ConnectAiAssistantsCard } from '@/components/ConnectAiAssistantsCard';
import { MainContentWrapper } from '@/components/MainContentWrapper';
import { companies as COMPANIES_TAXONOMY } from '@/data/companies';
import { industries as INDUSTRIES_TAXONOMY } from '@/data/industries';
import { roles as ROLES_TAXONOMY } from '@/data/roles';
import {
  FileText,
  Linkedin,
  ChevronDown,
  ChevronRight,
  Plus,
  X,
  Paperclip,
  Pencil,
  ArrowRight,
} from 'lucide-react';

// ── Tokens (mirroring the landing page) ─────────────────────────────────────

const C = {
  bg: '#F2F6FE',
  bgGradTop: '#FFFFFF',
  bgGradBottom: '#E8EFFE',
  cardBg: '#FFFFFF',
  cardBorder: 'rgba(59, 130, 246, 0.10)',
  cardShadow: '0 1px 2px rgba(15, 23, 42, 0.03), 0 8px 32px rgba(15, 23, 42, 0.04)',
  ink: '#0B1A3A',
  ink2: '#3B4B6B',
  ink3: '#7B8AA3',
  hairline: '#E2E8F5',
  blue: '#2563EB',
  blueSoft: '#3B82F6',
  blueTint: 'rgba(37, 99, 235, 0.06)',
  blueBorder: 'rgba(37, 99, 235, 0.22)',
  blueDashed: 'rgba(37, 99, 235, 0.40)',
  serif: "'Instrument Serif', Georgia, serif",
  mono: "'JetBrains Mono', monospace",
};

// ── Profile data shape + context ────────────────────────────────────────────

interface ProfileData {
  uid: string | null;
  name: string;
  email: string;
  avatarUrl: string | null;
  university: string;
  universityShort: string;
  school: string;        // e.g. "Marshall School of Business"
  degree: string;        // e.g. "BS"
  major: string;
  classYear: number | null;
  graduation: string;    // raw graduation string (e.g. "May 2027")

  resume: {
    connected: boolean;
    fileName: string;
    fileUrl: string | null;
    parsedAt: string | null;
    gpa: number | null;
    coursework: string[];
    skills: string[];
    experiences: Array<{ company: string; title: string; dates: string; location?: string; bullets?: string[] }>;
    projects: Array<{ name: string; description: string; dates?: string; link?: string; technologies?: string[] }>;
    honors: string[];
    awards: string[];
    extracurriculars: string[];
    certifications: string[];
    publications: string[];
    volunteer: string[];
  };

  linkedin: {
    connected: boolean;
    url: string;
    headline: string;
    refreshedAt: string | null;
    source: string;
    currentRole: string;
    currentCompany: string;
    industry: string;
    location: string;
    summary: string;
    education: Array<{ school: string; degree?: string; major?: string; dates?: string }>;
    experiences: Array<{ company: string; title: string; dates: string; location?: string; bullets?: string[] }>;
    skills: string[];
    interests: string[];
    connections: {
      total: number;
      byCompany: Array<{ company: string; count: number }>;
    };
  };

  suggestedIndustries: string[];
  userIndustries: string[];

  career: {
    targetFirms: string[];
    cycle: string;
    locations: string[];
    directionNarrative: string;
    extractedRoles: string[];
    suggestedRoles: string[];
    narrative: string; // anything-we-missed (personalContext)
    hardNos: string; // filter list — roles/companies/locations the user wants suppressed
  };

  personalContextFiles: Array<{
    name: string;
    size: number;
    contentType: string;
    url: string;
    storagePath: string;
    uploadedAt: string;
  }>;
}

interface ProfileContextValue {
  profile: ProfileData;
  loading: boolean;
  setLocal: (updater: (p: ProfileData) => ProfileData) => void;
  persist: (patch: Record<string, unknown>) => Promise<void>;
  reload: () => Promise<void>;
  uploadResume: (file: File) => Promise<void>;
  pickResumeFile: () => void;
  refreshLinkedIn: () => Promise<void>;
  disconnectLinkedIn: () => Promise<void>;
}

const ProfileContext = createContext<ProfileContextValue | null>(null);
const useProfile = (): ProfileContextValue => {
  const ctx = useContext(ProfileContext);
  if (!ctx) throw new Error('useProfile must be used within ProfileContext.Provider');
  return ctx;
};

// ── Default empty profile (shape preserved when nothing is loaded) ──────────

const EMPTY: ProfileData = {
  uid: null,
  name: '',
  email: '',
  avatarUrl: null,
  university: '',
  universityShort: '',
  school: '',
  degree: '',
  major: '',
  classYear: null,
  graduation: '',

  resume: {
    connected: false,
    fileName: '',
    fileUrl: null,
    parsedAt: null,
    gpa: null,
    coursework: [],
    skills: [],
    experiences: [],
    projects: [],
    honors: [],
    awards: [],
    extracurriculars: [],
    certifications: [],
    publications: [],
    volunteer: [],
  },

  linkedin: {
    connected: false,
    url: '',
    headline: '',
    refreshedAt: null,
    source: '',
    currentRole: '',
    currentCompany: '',
    industry: '',
    location: '',
    summary: '',
    education: [],
    experiences: [],
    skills: [],
    interests: [],
    connections: { total: 0, byCompany: [] },
  },

  suggestedIndustries: [],
  userIndustries: [],

  career: {
    targetFirms: [],
    cycle: '',
    locations: [],
    directionNarrative: '',
    extractedRoles: [],
    suggestedRoles: [],
    narrative: '',
    hardNos: '',
  },

  personalContextFiles: [],
};

// ── Firestore → ProfileData mapper ──────────────────────────────────────────

function shortenUniversity(full: string): string {
  if (!full) return '';
  const acronyms: Record<string, string> = {
    'university of southern california': 'USC',
    'university of california, los angeles': 'UCLA',
    'new york university': 'NYU',
    'university of pennsylvania': 'UPenn',
    'university of michigan': 'Michigan',
    'georgetown university': 'Georgetown',
    'stanford university': 'Stanford',
  };
  const key = full.trim().toLowerCase();
  if (acronyms[key]) return acronyms[key];
  const m = full.match(/^University of (.+)$/i);
  if (m) return m[1];
  return full.replace(/\s+University$/i, '').trim();
}

function parseYear(v: any): number | null {
  if (v == null) return null;
  const m = String(v).match(/(20\d{2})/);
  return m ? parseInt(m[1], 10) : null;
}

function pickEducation(rp: any): {
  university: string;
  school: string;
  degree: string;
  major: string;
  graduation: string;
  gpa: number | null;
  coursework: string[];
} {
  // resumeParsed.education can be a list (parser_v2) or a single object (legacy).
  const eduCandidate = Array.isArray(rp?.education) ? rp.education[0] || {} : rp?.education || {};
  const gpaRaw = eduCandidate.gpa ?? rp?.gpa ?? null;
  const gpa = gpaRaw == null || gpaRaw === '' ? null : Number(gpaRaw) || null;
  return {
    university: eduCandidate.university || rp?.university || '',
    school: eduCandidate.school || eduCandidate.college || eduCandidate.department || '',
    degree: eduCandidate.degree || '',
    major: eduCandidate.major || rp?.major || '',
    graduation: eduCandidate.graduation || rp?.year || '',
    gpa,
    coursework: Array.isArray(eduCandidate.coursework)
      ? eduCandidate.coursework
      : Array.isArray(rp?.coursework)
      ? rp.coursework
      : [],
  };
}

function mapExperiences(
  source: any
): Array<{ company: string; title: string; dates: string; location?: string; bullets?: string[] }> {
  const list = Array.isArray(source?.experiences)
    ? source.experiences
    : Array.isArray(source?.experience)
    ? source.experience
    : [];
  return list.map((e: any) => ({
    company: e.company || e.organization || e.employer || '',
    title: e.title || e.role || e.position || '',
    dates:
      e.dates ||
      [e.startDate || e.start, e.endDate || e.end].filter(Boolean).join(' – ') ||
      e.duration ||
      '',
    location: e.location || undefined,
    bullets: Array.isArray(e.bullets)
      ? e.bullets
      : Array.isArray(e.description)
      ? e.description
      : typeof e.description === 'string'
      ? [e.description]
      : undefined,
  }));
}

function mapProjects(rp: any) {
  const list = Array.isArray(rp?.projects) ? rp.projects : [];
  return list.map((p: any) => ({
    name: p.name || p.title || '',
    description: p.description || '',
    dates: p.date || p.dates,
    link: p.link || p.url,
    technologies: Array.isArray(p.technologies) ? p.technologies : undefined,
  }));
}

function asStringArray(v: any): string[] {
  if (!Array.isArray(v)) return [];
  return v
    .map((x) => (typeof x === 'string' ? x : x?.name || x?.title || x?.description || ''))
    .filter(Boolean);
}

/** Splits "Business Administration - STEM Designated" → { primary: "Business Administration", modifier: "STEM Designated" } */
export function cleanMajor(s: string): { primary: string; modifier: string } {
  if (!s) return { primary: '', modifier: '' };
  const m = s.match(/^(.+?)\s+[-–—]\s*(.+?)\)?$/) || s.match(/^(.+?)\s*\(([^)]+)\)\s*$/);
  if (m) return { primary: m[1].trim(), modifier: m[2].trim() };
  return { primary: s.trim(), modifier: '' };
}

function mapToProfile(uid: string, d: Record<string, any>, authPhotoUrl: string | null): ProfileData {
  const fullName =
    d.fullName || d.name || [d.firstName, d.lastName].filter(Boolean).join(' ') || '';

  // Resume-derived education (rich) — prefer parsed values over legacy top-level fields,
  // since the most recent resume upload is the freshest source of truth.
  const rp = d.resumeParsed || {};
  const edu = pickEducation(rp);
  const universityFull = edu.university || d.university || '';
  const universityShort = d.universityShort || shortenUniversity(universityFull);
  const majorRaw = edu.major || d.major || '';
  const majorPrimary = cleanMajor(majorRaw).primary || majorRaw;
  const classYear = parseYear(edu.graduation || d.graduationYear);

  const resumeUrl = d.resumeUrl || null;
  const resumeFileName = d.resumeFileName || (resumeUrl ? 'Resume' : '');
  const parsedAtRaw = d.resumeUpdatedAt || d.resumeParsedAt || null;
  const parsedAt = parsedAtRaw
    ? new Date(parsedAtRaw).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : null;

  const experiences = mapExperiences(rp);

  // LinkedIn — backend writes:
  //   linkedinResumeParsed   ← LLM-structured (resume-parser shape)
  //   linkedinEnrichmentData ← raw PDL/Bright Data response
  //   linkedinEnrichmentSource ← "pdl" | "bright_data" | "jina"
  //   linkedinEnrichedAt
  const linkedinUrl = d.linkedinUrl || '';
  const linkedinParsed = d.linkedinResumeParsed || {};
  const linkedinRaw = d.linkedinEnrichmentData || {};
  const linkedinSource = d.linkedinEnrichmentSource || '';

  // Experiences: prefer LLM structured shape, fallback to raw PDL experience list
  const linkedinExperiences =
    mapExperiences(linkedinParsed).length > 0
      ? mapExperiences(linkedinParsed)
      : mapExperiences(linkedinRaw);

  // Education: build a list — LLM education is sometimes single-object
  const liEduRaw = linkedinParsed.education;
  const liEduList: Array<{ school: string; degree?: string; major?: string; dates?: string }> = (
    Array.isArray(liEduRaw)
      ? liEduRaw
      : liEduRaw && typeof liEduRaw === 'object'
      ? [liEduRaw]
      : Array.isArray(linkedinRaw.education)
      ? linkedinRaw.education
      : []
  ).map((e: any) => ({
    school:
      e.school?.name || e.school || e.university || e.college || e.institution || '',
    degree: Array.isArray(e.degrees) ? e.degrees.join(', ') : e.degree || undefined,
    major: Array.isArray(e.majors) ? e.majors.join(', ') : e.major || undefined,
    dates:
      e.dates ||
      [e.start_date || e.startDate || e.start, e.end_date || e.endDate || e.graduation || e.end]
        .filter(Boolean)
        .join(' – ') ||
      undefined,
  }));

  // Skills: prefer LLM, fallback to PDL raw
  const linkedinSkills = Array.isArray(linkedinParsed?.skills) && linkedinParsed.skills.length > 0
    ? linkedinParsed.skills
    : Array.isArray(linkedinRaw.skills)
    ? linkedinRaw.skills
    : [];

  const linkedinInterests = Array.isArray(linkedinRaw.interests)
    ? linkedinRaw.interests
    : Array.isArray(linkedinParsed.interests)
    ? linkedinParsed.interests
    : [];

  // Current role / company / industry / location from PDL raw
  const liCurrentRole =
    linkedinRaw.job_title ||
    linkedinParsed.experience?.[0]?.title ||
    linkedinExperiences[0]?.title ||
    '';
  const liCurrentCompany =
    linkedinRaw.job_company_name ||
    linkedinParsed.experience?.[0]?.company ||
    linkedinExperiences[0]?.company ||
    '';
  const liIndustry =
    linkedinRaw.job_company_industry || linkedinRaw.industry || '';
  const liLocation =
    linkedinRaw.location_name ||
    [linkedinRaw.location_locality, linkedinRaw.location_region].filter(Boolean).join(', ') ||
    linkedinRaw.location_metro ||
    '';
  const liSummary =
    linkedinRaw.summary || linkedinParsed.summary || linkedinParsed.objective || '';

  const linkedinHeadline =
    linkedinRaw.headline ||
    (liCurrentRole && liCurrentCompany ? `${liCurrentRole} at ${liCurrentCompany}` : liCurrentRole || liSummary || '');

  const linkedinConnectionsTotal =
    typeof linkedinRaw.num_connections === 'number'
      ? linkedinRaw.num_connections
      : typeof linkedinRaw.connections === 'number'
      ? linkedinRaw.connections
      : linkedinRaw.connections?.total || 0;
  const linkedinByCompany = Array.isArray(linkedinRaw.connections?.byCompany)
    ? linkedinRaw.connections.byCompany
    : [];
  const linkedinRefreshedAt = d.linkedinEnrichedAt || null;

  // Use LinkedIn experiences/skills as a fallback for the resume display when the user has no resume.
  const effectiveExperiences = experiences.length > 0 ? experiences : linkedinExperiences;
  const effectiveSkills =
    Array.isArray(rp.skills) && rp.skills.length > 0 ? rp.skills : linkedinSkills;

  return {
    uid,
    name: fullName,
    email: d.email || '',
    avatarUrl: d.profilePhoto || authPhotoUrl,
    university: universityFull,
    universityShort,
    school: edu.school,
    degree: edu.degree,
    major: majorPrimary,
    classYear,
    graduation: edu.graduation,

    resume: {
      connected: !!resumeUrl,
      fileName: resumeFileName,
      fileUrl: resumeUrl,
      parsedAt,
      gpa: edu.gpa,
      coursework: edu.coursework,
      skills: effectiveSkills,
      experiences: effectiveExperiences,
      projects: mapProjects(rp),
      honors: asStringArray((rp.education && (Array.isArray(rp.education) ? rp.education[0] : rp.education))?.honors),
      awards: asStringArray(rp.awards),
      extracurriculars: asStringArray(rp.extracurriculars),
      certifications: asStringArray(rp.certifications),
      publications: asStringArray(rp.publications),
      volunteer: asStringArray(rp.volunteer),
    },

    linkedin: {
      connected: !!linkedinUrl,
      url: linkedinUrl.replace(/^https?:\/\//, ''),
      headline: linkedinHeadline,
      refreshedAt: linkedinRefreshedAt
        ? new Date(linkedinRefreshedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
        : null,
      source: linkedinSource,
      currentRole: liCurrentRole,
      currentCompany: liCurrentCompany,
      industry: liIndustry,
      location: liLocation,
      summary: liSummary,
      education: liEduList,
      experiences: linkedinExperiences,
      skills: linkedinSkills,
      interests: linkedinInterests,
      connections: {
        total: linkedinConnectionsTotal,
        byCompany: linkedinByCompany,
      },
    },

    suggestedIndustries: d.suggestedIndustries || [],
    userIndustries: d.targetIndustries || d.userIndustries || [],

    career: {
      targetFirms: d.targetFirms || [],
      cycle: d.recruitingCycle || (classYear ? `Summer ${classYear}` : ''),
      locations: d.preferredLocations || d.targetLocations || [],
      directionNarrative: d.directionNarrative || '',
      extractedRoles: d.extractedRoles || d.targetRoles || [],
      suggestedRoles: d.suggestedRoles || [],
      narrative: d.personalContext || d.careerGoals || '',
      hardNos: d.hardNos || '',
    },

    personalContextFiles: Array.isArray(d.personalContextFiles)
      ? d.personalContextFiles.filter((f: any) => f && typeof f === 'object')
      : [],
  };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── Bits ─────────────────────────────────────────────────────────────────────

const Mono: React.FC<{ children: React.ReactNode; style?: React.CSSProperties }> = ({ children, style }) => (
  <span
    style={{
      fontFamily: C.mono,
      fontSize: 10,
      letterSpacing: '0.10em',
      textTransform: 'uppercase',
      color: C.ink3,
      fontWeight: 500,
      ...style,
    }}
  >
    {children}
  </span>
);

const Serif: React.FC<{ children: React.ReactNode; size?: number; style?: React.CSSProperties }> = ({
  children,
  size = 22,
  style,
}) => (
  <span
    style={{
      fontFamily: C.serif,
      fontSize: size,
      fontWeight: 400,
      fontStyle: 'normal',
      color: C.ink,
      letterSpacing: '-0.005em',
      lineHeight: 1.15,
      ...style,
    }}
  >
    {children}
  </span>
);

const TitleRule: React.FC<{ width?: number; style?: React.CSSProperties }> = ({ width = 28, style }) => (
  <div
    style={{
      width,
      height: 2.5,
      background: C.blue,
      borderRadius: 2,
      marginTop: 6,
      opacity: 0.85,
      ...style,
    }}
  />
);

interface ChipProps {
  children: React.ReactNode;
  variant?: 'user' | 'suggested' | 'muted';
  removable?: boolean;
  onClick?: () => void;
}

const Chip: React.FC<ChipProps> = ({ children, variant = 'user', removable, onClick }) => {
  const styles: Record<NonNullable<ChipProps['variant']>, React.CSSProperties> = {
    user: {
      background: 'rgba(15, 23, 42, 0.04)',
      border: '1px solid transparent',
      color: C.ink,
    },
    suggested: {
      background: '#FFFFFF',
      border: `1px dashed ${C.blueDashed}`,
      color: C.blue,
    },
    muted: {
      background: 'transparent',
      border: `1px dashed ${C.hairline}`,
      color: C.ink3,
    },
  };
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        padding: '3px 10px',
        borderRadius: 100,
        fontSize: 12,
        fontWeight: 500,
        whiteSpace: 'nowrap',
        cursor: onClick ? 'pointer' : 'default',
        fontFamily: 'inherit',
        ...styles[variant],
      }}
    >
      {children}
      {removable && <X style={{ width: 10, height: 10, opacity: 0.28 }} />}
    </button>
  );
};

// ── Card wrapper ─────────────────────────────────────────────────────────────

const Card: React.FC<{ children: React.ReactNode; style?: React.CSSProperties }> = ({ children, style }) => (
  <div
    style={{
      background: C.cardBg,
      border: `1px solid ${C.cardBorder}`,
      borderRadius: 14,
      boxShadow: C.cardShadow,
      padding: '22px 26px',
      ...style,
    }}
  >
    {children}
  </div>
);

// ── Header ───────────────────────────────────────────────────────────────────

function Avatar() {
  const { profile } = useProfile();
  const initials =
    (profile.name || profile.email || 'U')
      .split(' ')
      .map((p) => p[0])
      .join('')
      .slice(0, 2)
      .toUpperCase();
  return (
    <div style={{ position: 'relative', flexShrink: 0 }}>
      <div
        style={{
          width: 64,
          height: 64,
          borderRadius: '50%',
          background: profile.avatarUrl
            ? `url(${profile.avatarUrl}) center/cover no-repeat`
            : 'linear-gradient(135deg, #93C5FD 0%, #2563EB 100%)',
          color: '#FFFFFF',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: 'inherit',
          fontSize: 22,
          fontWeight: 600,
          letterSpacing: '-0.01em',
          boxShadow: '0 1px 3px rgba(15,23,42,0.08), inset 0 1px 0 rgba(255,255,255,0.18)',
        }}
        title="Pulled from your Google account · click to replace"
      >
        {!profile.avatarUrl && initials}
      </div>
      <button
        type="button"
        aria-label="Change profile photo"
        style={{
          position: 'absolute',
          bottom: -2,
          right: -2,
          width: 22,
          height: 22,
          borderRadius: '50%',
          background: '#FFFFFF',
          border: `1px solid ${C.blueBorder}`,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          padding: 0,
          color: C.blue,
          boxShadow: '0 1px 2px rgba(15,23,42,0.08)',
        }}
      >
        <Pencil style={{ width: 10, height: 10 }} />
      </button>
    </div>
  );
}

function Header() {
  const { profile } = useProfile();
  const navigate = useNavigate();
  const universityForHeader = profile.universityShort || profile.university;
  const metaParts: string[] = [];
  if (universityForHeader) metaParts.push(universityForHeader);
  if (profile.school) metaParts.push(profile.school);
  if (profile.major) metaParts.push(profile.major);
  if (profile.classYear) metaParts.push(`Class of ${profile.classYear}`);
  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 18 }}>
        <Avatar />
        <div style={{ flex: 1, minWidth: 0 }}>
          <Serif size={42} style={{ display: 'block', lineHeight: 1.05 }}>
            {profile.name || 'Your profile'}
          </Serif>
          <div style={{ fontSize: 14, color: C.ink2, marginTop: 6, lineHeight: 1.5 }}>
            {metaParts.length > 0 ? metaParts.join(' · ') : (profile.email || 'Add your school and major to personalize matches')}
          </div>
        </div>
        {/* Cross-link to account settings. Sibling surfaces — profile drives
            personalization, settings holds billing / integrations / admin. */}
        <button
          type="button"
          onClick={() => navigate('/account-settings')}
          style={{
            flexShrink: 0,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            padding: '7px 12px',
            borderRadius: 999,
            background: 'transparent',
            border: `1px solid ${C.hairline}`,
            color: C.ink2,
            fontFamily: 'inherit',
            fontSize: 12.5,
            fontWeight: 500,
            cursor: 'pointer',
            transition: 'background .15s, border-color .15s, color .15s',
            whiteSpace: 'nowrap',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = '#FAFBFE';
            e.currentTarget.style.color = C.blue;
            e.currentTarget.style.borderColor = C.blueDashed;
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'transparent';
            e.currentTarget.style.color = C.ink2;
            e.currentTarget.style.borderColor = C.hairline;
          }}
        >
          Account settings →
        </button>
      </div>
    </div>
  );
}

// ── Collapsible source ───────────────────────────────────────────────────────

interface CollapsibleSourceProps {
  icon: React.ComponentType<{ style?: React.CSSProperties }>;
  title: string;
  meta: string;
  summary: string;
  rightActions?: Array<{ label: string; onClick?: () => void; variant?: 'default' | 'destructive' }>;
  defaultOpen?: boolean;
  emptyState?: boolean;
  children: React.ReactNode;
}

const CollapsibleSource: React.FC<CollapsibleSourceProps> = ({
  icon: Icon,
  title,
  meta,
  summary,
  rightActions,
  defaultOpen = false,
  emptyState = false,
  children,
}) => {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Card style={{ padding: 0, overflow: 'hidden' }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          width: '100%',
          background: 'transparent',
          border: 'none',
          padding: '18px 22px',
          cursor: 'pointer',
          textAlign: 'left',
          fontFamily: 'inherit',
          color: 'inherit',
        }}
      >
        <span style={{ marginTop: 4, marginRight: 12, color: C.ink3 }}>
          {open ? (
            <ChevronDown style={{ width: 14, height: 14 }} />
          ) : (
            <ChevronRight style={{ width: 14, height: 14 }} />
          )}
        </span>
        <span
          style={{
            width: 28,
            height: 28,
            borderRadius: 8,
            background: emptyState ? 'rgba(15, 23, 42, 0.04)' : C.blueTint,
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginRight: 14,
            flexShrink: 0,
          }}
        >
          <Icon style={{ width: 14, height: 14, color: emptyState ? C.ink3 : C.blue }} />
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
            <Serif size={17}>{title}</Serif>
            <Mono>{meta}</Mono>
          </span>
          <span
            style={{
              display: 'block',
              fontSize: 13,
              color: C.ink2,
              marginTop: 3,
              lineHeight: 1.55,
            }}
          >
            {summary}
          </span>
        </span>
        {rightActions && rightActions.length > 0 && (
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              marginLeft: 12,
            }}
          >
            {rightActions.map((a, i) => (
              <React.Fragment key={a.label}>
                {i > 0 && <span style={{ color: C.hairline, fontSize: 12 }}>·</span>}
                <span
                  onClick={(e) => {
                    e.stopPropagation();
                    a.onClick?.();
                  }}
                  style={{
                    fontSize: 12,
                    color: a.variant === 'destructive' ? '#B91C1C' : C.blue,
                    padding: '4px 4px',
                    cursor: a.onClick ? 'pointer' : 'default',
                    fontWeight: 500,
                  }}
                >
                  {a.label}
                </span>
              </React.Fragment>
            ))}
          </span>
        )}
      </button>

      {open && (
        <div
          style={{
            padding: '4px 26px 22px',
            marginLeft: 54,
            borderTop: `1px solid ${C.hairline}`,
            paddingTop: 18,
          }}
        >
          {children}
        </div>
      )}
    </Card>
  );
};

// ── Resume detail ────────────────────────────────────────────────────────────

const DetailField: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div style={{ marginBottom: 14 }}>
    <Mono style={{ display: 'block', marginBottom: 4 }}>{label}</Mono>
    <div style={{ fontSize: 13, color: C.ink, lineHeight: 1.55 }}>{children}</div>
  </div>
);

function ResumeDetail() {
  const { profile } = useProfile();
  const r = profile.resume;
  const majorClean = cleanMajor(profile.major);
  const eduParts: string[] = [];
  if (profile.university) eduParts.push(profile.university);
  if (profile.school) eduParts.push(profile.school);
  if (profile.degree) eduParts.push(profile.degree);
  if (majorClean.primary) {
    eduParts.push(majorClean.modifier ? `${majorClean.primary} (${majorClean.modifier})` : majorClean.primary);
  }
  if (profile.classYear) eduParts.push(`Class of ${profile.classYear}`);
  else if (profile.graduation) eduParts.push(profile.graduation);
  if (r.gpa != null) eduParts.push(`GPA ${r.gpa}`);

  return (
    <>
      {eduParts.length > 0 && (
        <DetailField label="Education">{eduParts.join(' · ')}</DetailField>
      )}

      {r.honors.length > 0 && (
        <DetailField label="Honors">
          <span style={{ color: C.ink2 }}>{r.honors.join(' · ')}</span>
        </DetailField>
      )}

      {r.experiences.length > 0 && (
        <DetailField label="Experience">
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {r.experiences.map((e, i) => (
              <li key={`${e.company}-${i}`} style={{ marginBottom: 10 }}>
                <div>
                  <span style={{ fontWeight: 500 }}>{e.company}</span>
                  {e.title && <span style={{ color: C.ink2 }}> · {e.title}</span>}
                  {e.dates && <span style={{ color: C.ink3 }}> · {e.dates}</span>}
                  {e.location && <span style={{ color: C.ink3 }}> · {e.location}</span>}
                </div>
                {e.bullets && e.bullets.length > 0 && (
                  <ul style={{ margin: '4px 0 0 16px', padding: 0, color: C.ink2, fontSize: 12.5, lineHeight: 1.55 }}>
                    {e.bullets.map((b, j) => (
                      <li key={j} style={{ marginBottom: 2 }}>
                        {b}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </DetailField>
      )}

      {r.projects.length > 0 && (
        <DetailField label="Projects">
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {r.projects.map((p, i) => (
              <li key={`${p.name}-${i}`} style={{ marginBottom: 6 }}>
                <span style={{ fontWeight: 500 }}>{p.name}</span>
                {p.description && <span style={{ color: C.ink2 }}> · {p.description}</span>}
                {p.dates && <span style={{ color: C.ink3 }}> · {p.dates}</span>}
              </li>
            ))}
          </ul>
        </DetailField>
      )}

      {r.extracurriculars.length > 0 && (
        <DetailField label="Leadership & Activities">
          <span style={{ color: C.ink2 }}>{r.extracurriculars.join(' · ')}</span>
        </DetailField>
      )}

      {r.skills.length > 0 && (
        <DetailField label="Skills">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
            {r.skills.map((s) => (
              <Chip key={s} variant="muted">
                {s}
              </Chip>
            ))}
          </div>
        </DetailField>
      )}

      {r.coursework.length > 0 && (
        <DetailField label="Coursework">
          <span style={{ color: C.ink2 }}>{r.coursework.join(' · ')}</span>
        </DetailField>
      )}

      {r.awards.length > 0 && (
        <DetailField label="Awards">
          <span style={{ color: C.ink2 }}>{r.awards.join(' · ')}</span>
        </DetailField>
      )}

      {r.certifications.length > 0 && (
        <DetailField label="Certifications">
          <span style={{ color: C.ink2 }}>{r.certifications.join(' · ')}</span>
        </DetailField>
      )}

      {r.volunteer.length > 0 && (
        <DetailField label="Volunteer">
          <span style={{ color: C.ink2 }}>{r.volunteer.join(' · ')}</span>
        </DetailField>
      )}

      {r.publications.length > 0 && (
        <DetailField label="Publications">
          <span style={{ color: C.ink2 }}>{r.publications.join(' · ')}</span>
        </DetailField>
      )}
    </>
  );
}

function LinkedInConnect() {
  const { profile, persist, reload } = useProfile();
  const { toast } = useToast();
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);

  const submitUrl = async () => {
    const trimmed = url.trim();
    if (!trimmed.includes('linkedin.com/in/')) {
      toast({ title: 'Invalid LinkedIn URL', description: 'Paste a URL like linkedin.com/in/your-handle', variant: 'destructive' });
      return;
    }
    if (!profile.uid) return;
    setBusy(true);
    try {
      const normalized = trimmed.startsWith('http') ? trimmed : `https://${trimmed}`;
      await persist({ linkedinUrl: normalized });

      const token = auth.currentUser ? await auth.currentUser.getIdToken() : null;
      let enriched = false;
      let errorMsg = '';
      try {
        const res = await fetch(`${BACKEND_URL}/api/enrich-linkedin-onboarding`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({ linkedin_url: normalized }),
        });
        if (res.ok) {
          const data = await res.json();
          enriched = !!data?.enriched;
          errorMsg = data?.error || '';
        }
      } catch (e) {
        console.warn('LinkedIn enrichment failed (URL still saved):', e);
      }

      if (enriched) {
        toast({ title: 'LinkedIn connected and enriched', description: 'We pulled your headline, experience, and skills.' });
      } else {
        toast({
          title: 'LinkedIn URL saved',
          description: errorMsg || 'We saved the URL but couldn\'t pull data this time. Try again in a moment.',
          variant: 'destructive',
        });
      }
      await reload();
    } catch (e: any) {
      toast({ title: 'Could not save LinkedIn', description: e?.message || 'Please try again.', variant: 'destructive' });
    } finally {
      setBusy(false);
    }
  };

  // Serif used for the intro line, matching the home-page "what we know about
  // you" voice. URL-only flow (no PDF) — Firecrawl handles the data pull from
  // the live LinkedIn page, removing the multi-step PDF download workflow.
  const SERIF: React.CSSProperties = {
    fontFamily: "'Libre Baskerville', Georgia, serif",
    color: 'var(--heading, #1E2D4D)',
  };

  return (
    <div>
      <p
        style={{
          ...SERIF,
          fontSize: 15,
          lineHeight: 1.65,
          marginBottom: 16,
          marginTop: 0,
        }}
      >
        Drop in your LinkedIn URL — we'll pull your headline, experience, and
        skills automatically.
      </p>

      <div style={{ display: 'flex', gap: 8, alignItems: 'stretch' }}>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !busy) submitUrl(); }}
          placeholder="linkedin.com/in/your-handle"
          disabled={busy}
          style={{
            flex: 1,
            background: '#FAFBFE',
            border: `1px solid ${C.hairline}`,
            borderRadius: 8,
            padding: '11px 14px',
            fontSize: 13,
            fontFamily: C.mono,
            color: C.ink,
            outline: 'none',
          }}
        />
        <button
          type="button"
          onClick={submitUrl}
          disabled={busy || !url.trim()}
          style={{
            background: busy ? C.ink3 : C.blue,
            color: '#FFFFFF',
            border: 'none',
            borderRadius: 8,
            padding: '0 22px',
            fontSize: 13.5,
            fontWeight: 600,
            fontFamily: 'inherit',
            cursor: busy ? 'wait' : 'pointer',
            whiteSpace: 'nowrap',
            opacity: !url.trim() ? 0.6 : 1,
          }}
        >
          {busy ? 'Connecting…' : 'Connect'}
        </button>
      </div>

      <p
        style={{
          marginTop: 10,
          marginBottom: 0,
          fontSize: 11.5,
          color: C.ink3,
          lineHeight: 1.5,
        }}
      >
        Stays private to you. We only read your public profile to personalize
        matches and emails — nothing is posted or messaged.
      </p>
    </div>
  );
}

function ResumeUpload() {
  const { uploadResume, pickResumeFile } = useProfile();
  const [busy, setBusy] = useState(false);

  const handleFile = async (file: File) => {
    setBusy(true);
    try {
      await uploadResume(file);
    } finally {
      setBusy(false);
    }
  };

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  };

  return (
    <div>
      <div style={{ fontSize: 12.5, color: C.ink3, marginBottom: 10, lineHeight: 1.55 }}>
        Drop a PDF or DOCX here — we extract experience, skills, GPA, and coursework.
      </div>
      <div
        onClick={busy ? undefined : pickResumeFile}
        onDragOver={(e) => e.preventDefault()}
        onDrop={busy ? undefined : onDrop}
        style={{
          border: `1px dashed ${C.blueDashed}`,
          borderRadius: 10,
          padding: '24px 18px',
          background: C.blueTint,
          textAlign: 'center',
          color: C.ink2,
          fontSize: 13,
          cursor: busy ? 'wait' : 'pointer',
          opacity: busy ? 0.7 : 1,
        }}
      >
        <div style={{ fontWeight: 500, color: C.blue, marginBottom: 4 }}>
          {busy ? 'Parsing your resume…' : 'Drop resume here'}
        </div>
        <div style={{ fontSize: 12, color: C.ink3 }}>or click to browse · PDF / DOCX · 10MB max</div>
      </div>
    </div>
  );
}

function LinkedInDetail() {
  const { profile } = useProfile();
  const li = profile.linkedin;

  const sourceLabel =
    li.source === 'pdl'
      ? 'People Data Labs'
      : li.source === 'bright_data'
      ? 'Bright Data (LinkedIn scrape)'
      : li.source === 'jina'
      ? 'Jina Reader'
      : li.source || '';

  const headerMeta: string[] = [];
  if (li.industry) headerMeta.push(li.industry);
  if (li.location) headerMeta.push(li.location);

  return (
    <>
      <DetailField label="Profile URL">
        <a
          href={li.url.startsWith('http') ? li.url : `https://${li.url}`}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: C.blue, fontFamily: C.mono, fontSize: 12, textDecoration: 'none' }}
        >
          {li.url}
        </a>
      </DetailField>

      {li.headline && <DetailField label="Headline">{li.headline}</DetailField>}

      {(li.currentRole || li.currentCompany) && (
        <DetailField label="Current">
          {li.currentRole && <span style={{ fontWeight: 500 }}>{li.currentRole}</span>}
          {li.currentRole && li.currentCompany && <span style={{ color: C.ink2 }}> · </span>}
          {li.currentCompany && <span style={{ color: C.ink2 }}>{li.currentCompany}</span>}
        </DetailField>
      )}

      {headerMeta.length > 0 && (
        <DetailField label="Industry & Location">
          <span style={{ color: C.ink2 }}>{headerMeta.join(' · ')}</span>
        </DetailField>
      )}

      {li.summary && (
        <DetailField label="About">
          <span style={{ color: C.ink2, lineHeight: 1.6 }}>{li.summary}</span>
        </DetailField>
      )}

      {li.education.length > 0 && (
        <DetailField label="Education">
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {li.education.map((e, i) => (
              <li key={`${e.school}-${i}`} style={{ marginBottom: 4 }}>
                <span style={{ fontWeight: 500 }}>{e.school}</span>
                {e.degree && <span style={{ color: C.ink2 }}> · {e.degree}</span>}
                {e.major && <span style={{ color: C.ink2 }}> · {e.major}</span>}
                {e.dates && <span style={{ color: C.ink3 }}> · {e.dates}</span>}
              </li>
            ))}
          </ul>
        </DetailField>
      )}

      {li.experiences.length > 0 && (
        <DetailField label="Experience">
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {li.experiences.map((e, i) => (
              <li key={`${e.company}-${i}`} style={{ marginBottom: 8 }}>
                <div>
                  <span style={{ fontWeight: 500 }}>{e.company}</span>
                  {e.title && <span style={{ color: C.ink2 }}> · {e.title}</span>}
                  {e.dates && <span style={{ color: C.ink3 }}> · {e.dates}</span>}
                  {e.location && <span style={{ color: C.ink3 }}> · {e.location}</span>}
                </div>
                {e.bullets && e.bullets.length > 0 && (
                  <ul style={{ margin: '4px 0 0 16px', padding: 0, color: C.ink2, fontSize: 12.5, lineHeight: 1.55 }}>
                    {e.bullets.map((b, j) => (
                      <li key={j} style={{ marginBottom: 2 }}>{b}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </DetailField>
      )}

      {li.skills.length > 0 && (
        <DetailField label="Skills">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
            {li.skills.map((s) => (
              <Chip key={s} variant="muted">{s}</Chip>
            ))}
          </div>
        </DetailField>
      )}

      {li.interests.length > 0 && (
        <DetailField label="Interests">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
            {li.interests.map((s) => (
              <Chip key={s} variant="muted">{s}</Chip>
            ))}
          </div>
        </DetailField>
      )}

      {li.connections.total > 0 && (
        <DetailField label="Connections">
          <span style={{ fontWeight: 500 }}>{li.connections.total.toLocaleString()}</span> total
          {li.connections.byCompany.length > 0 && (
            <ul style={{ margin: '4px 0 0', padding: 0, listStyle: 'none' }}>
              {li.connections.byCompany.slice(0, 4).map((c) => (
                <li key={c.company} style={{ fontSize: 12.5, color: C.ink2, marginBottom: 2 }}>
                  {c.count} at <span style={{ color: C.ink, fontWeight: 500 }}>{c.company}</span>
                </li>
              ))}
            </ul>
          )}
        </DetailField>
      )}

      {sourceLabel && (
        <div style={{ fontSize: 11, color: C.ink3, marginTop: 8, fontStyle: 'normal' }}>
          Source: {sourceLabel}
          {li.source === 'pdl' && (
            <span style={{ color: C.ink3 }}>
              {' '}— PDL has limited coverage for college students; switch to Bright Data / Jina for richer data.
            </span>
          )}
        </div>
      )}
    </>
  );
}

// ── Direction (career interest) ──────────────────────────────────────────────

// ── Editable chip row with autocomplete ─────────────────────────────────────

interface EditableChipRowProps {
  label: string;
  values: string[];
  taxonomy?: string[];
  placeholder?: string;
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
  rowLabelWidth: number;
  rowGap: number;
}

const EditableChipRow: React.FC<EditableChipRowProps> = ({
  label,
  values,
  taxonomy,
  placeholder,
  onAdd,
  onRemove,
  rowLabelWidth,
  rowGap,
}) => {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const inputRef = React.useRef<HTMLInputElement>(null);

  const valueSet = new Set(values.map((v) => v.toLowerCase()));
  const suggestions = (() => {
    const q = draft.trim().toLowerCase();
    if (!taxonomy) return [];
    if (q.length === 0) return taxonomy.slice(0, 6).filter((t) => !valueSet.has(t.toLowerCase()));
    return taxonomy
      .filter((t) => t.toLowerCase().includes(q) && !valueSet.has(t.toLowerCase()))
      .slice(0, 6);
  })();

  const commit = (raw: string) => {
    const v = raw.trim();
    if (!v) return;
    if (valueSet.has(v.toLowerCase())) {
      setDraft('');
      return;
    }
    onAdd(v);
    setDraft('');
    // Keep editing on so user can add multiple in a row
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: rowGap }}>
      <Mono style={{ minWidth: rowLabelWidth, marginTop: 4 }}>{label}</Mono>
      <div style={{ flex: 1, position: 'relative' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, alignItems: 'center' }}>
          {values.map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => onRemove(v)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                padding: '3px 10px',
                borderRadius: 100,
                background: 'rgba(15, 23, 42, 0.04)',
                border: '1px solid transparent',
                color: C.ink,
                fontSize: 12,
                fontWeight: 500,
                whiteSpace: 'nowrap',
                cursor: 'pointer',
                fontFamily: 'inherit',
              }}
              title="Click to remove"
            >
              {v}
              <X style={{ width: 10, height: 10, opacity: 0.28 }} />
            </button>
          ))}

          {editing ? (
            <input
              ref={inputRef}
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  commit(draft);
                } else if (e.key === 'Escape') {
                  setEditing(false);
                  setDraft('');
                }
              }}
              onBlur={() => {
                // Defer so click on a suggestion still fires
                setTimeout(() => {
                  if (draft.trim()) commit(draft);
                  setEditing(false);
                }, 150);
              }}
              placeholder={placeholder || 'Type and press Enter…'}
              style={{
                background: '#FAFBFE',
                border: `1px solid ${C.blueBorder}`,
                borderRadius: 100,
                padding: '3px 10px',
                fontSize: 12,
                fontFamily: 'inherit',
                color: C.ink,
                outline: 'none',
                minWidth: 160,
              }}
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                padding: '3px 10px',
                borderRadius: 100,
                background: 'transparent',
                border: `1px dashed ${C.hairline}`,
                color: C.ink3,
                fontSize: 12,
                fontWeight: 500,
                cursor: 'pointer',
                fontFamily: 'inherit',
              }}
            >
              <Plus style={{ width: 10, height: 10 }} />
              Add
            </button>
          )}
        </div>

        {/* Autocomplete suggestion dropdown */}
        {editing && suggestions.length > 0 && (
          <div
            style={{
              position: 'absolute',
              left: 0,
              top: 'calc(100% + 4px)',
              zIndex: 10,
              background: '#FFFFFF',
              border: `1px solid ${C.hairline}`,
              borderRadius: 8,
              boxShadow: '0 6px 20px rgba(15,23,42,0.08)',
              padding: 4,
              minWidth: 220,
              maxHeight: 220,
              overflowY: 'auto',
            }}
          >
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onMouseDown={(e) => {
                  // mousedown fires before input blur — prevents blur dismissing dropdown
                  e.preventDefault();
                  commit(s);
                }}
                style={{
                  display: 'block',
                  width: '100%',
                  textAlign: 'left',
                  background: 'transparent',
                  border: 'none',
                  padding: '6px 10px',
                  fontSize: 13,
                  color: C.ink,
                  cursor: 'pointer',
                  borderRadius: 4,
                  fontFamily: 'inherit',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'rgba(37, 99, 235, 0.06)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                {s}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

// ── When picker (cycle + year) ──────────────────────────────────────────────

interface WhenPickerProps {
  cycle: string;
  cycleYear: number | null;
  onChange: (cycle: string, year: number | null) => void;
}

const WhenPicker: React.FC<WhenPickerProps> = ({ cycle, cycleYear, onChange }) => {
  const [open, setOpen] = useState(false);
  const cycles: Array<{ key: string; label: string; takesYear: boolean }> = [
    { key: 'summer-sa', label: 'Summer', takesYear: true },
    { key: 'fulltime', label: 'Full-time', takesYear: true },
    { key: 'off-cycle', label: 'Off-cycle', takesYear: false },
    { key: 'exploring', label: 'Exploring', takesYear: false },
  ];
  const matched = cycles.find((c) => c.key === cycle);
  const display = matched
    ? matched.takesYear && cycleYear
      ? `${matched.label} ${cycleYear}`
      : matched.label
    : cycle || 'Set timing';

  return (
    <div style={{ position: 'relative', display: 'inline-block' }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          background: 'rgba(15,23,42,0.04)',
          border: '1px solid transparent',
          borderRadius: 100,
          padding: '3px 12px',
          fontSize: 12,
          fontWeight: 500,
          color: C.ink,
          cursor: 'pointer',
          fontFamily: 'inherit',
        }}
      >
        {display}
      </button>
      {open && (
        <div
          style={{
            position: 'absolute',
            left: 0,
            top: 'calc(100% + 4px)',
            zIndex: 10,
            background: '#FFFFFF',
            border: `1px solid ${C.hairline}`,
            borderRadius: 8,
            boxShadow: '0 6px 20px rgba(15,23,42,0.08)',
            padding: 8,
            minWidth: 220,
          }}
        >
          {cycles.map((c) => {
            const isSelected = c.key === cycle;
            return (
              <div key={c.key} style={{ marginBottom: 4 }}>
                <button
                  type="button"
                  onClick={() => {
                    onChange(c.key, c.takesYear ? cycleYear || new Date().getFullYear() + 1 : null);
                    if (!c.takesYear) setOpen(false);
                  }}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    width: '100%',
                    gap: 8,
                    padding: '6px 8px',
                    background: isSelected ? 'rgba(37, 99, 235, 0.06)' : 'transparent',
                    border: 'none',
                    fontSize: 13,
                    color: C.ink,
                    cursor: 'pointer',
                    borderRadius: 6,
                    fontFamily: 'inherit',
                    textAlign: 'left',
                  }}
                >
                  <span
                    style={{
                      width: 12,
                      height: 12,
                      borderRadius: '50%',
                      border: `1.5px solid ${isSelected ? C.blue : C.ink3}`,
                      background: isSelected ? C.blue : 'transparent',
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ flex: 1, fontWeight: isSelected ? 500 : 400 }}>{c.label}</span>
                  {c.takesYear && isSelected && (
                    <input
                      type="number"
                      value={cycleYear || ''}
                      onChange={(e) => {
                        const y = parseInt(e.target.value, 10);
                        onChange(c.key, isNaN(y) ? null : y);
                      }}
                      onClick={(e) => e.stopPropagation()}
                      style={{
                        width: 64,
                        padding: '2px 6px',
                        fontSize: 12,
                        border: `1px solid ${C.hairline}`,
                        borderRadius: 6,
                        fontFamily: 'inherit',
                      }}
                    />
                  )}
                </button>
              </div>
            );
          })}
          <div style={{ paddingTop: 6, marginTop: 4, borderTop: `1px solid ${C.hairline}` }}>
            <button
              type="button"
              onClick={() => setOpen(false)}
              style={{
                width: '100%',
                background: C.blue,
                border: 'none',
                color: '#FFFFFF',
                padding: '6px 10px',
                fontSize: 12,
                fontWeight: 500,
                borderRadius: 6,
                cursor: 'pointer',
                fontFamily: 'inherit',
              }}
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

// ── Direction ────────────────────────────────────────────────────────────────

const INDUSTRIES_LIST: string[] = INDUSTRIES_TAXONOMY.map((i: any) => i.name);
const ROLES_LIST: string[] = ROLES_TAXONOMY.map((r: any) => r.name);

// Mirror of AccountSettings.tsx careerTrackOptions. Single source of truth
// for the enum the GoalsPromptBanner watches; setting it from the Goals
// section hides the banner immediately.
const CAREER_TRACK_OPTIONS = [
  'Investment Banking',
  'Management Consulting',
  'Private Equity',
  'Venture Capital',
  'Tech / Software Engineering',
  'Product Management',
  'Data Science / Analytics',
  'Marketing',
  'Finance / Corporate Finance',
  'Accounting',
  'Operations',
  'Other',
];

// Rotates through the Career Track input placeholder while idle.
const CAREER_TRACK_PLACEHOLDERS = [
  'What field are you targeting?',
  'e.g. Investment Banking',
  'e.g. Management Consulting',
  'e.g. Tech / Software Engineering',
  'e.g. Product Management',
  'e.g. Private Equity',
];
const COMPANIES_LIST: string[] = COMPANIES_TAXONOMY.map((c: any) => c.name);
const COMMON_LOCATIONS = [
  'New York, NY',
  'San Francisco, CA',
  'Los Angeles, CA',
  'Chicago, IL',
  'Boston, MA',
  'Washington, DC',
  'Seattle, WA',
  'Austin, TX',
  'Atlanta, GA',
  'Miami, FL',
  'London, UK',
  'Hong Kong',
  'Singapore',
];

function Direction() {
  const { profile, persist, reload } = useProfile();
  const { toast } = useToast();
  const { user, updateUser } = useFirebaseAuth();
  const c = profile.career;

  const [narrative, setNarrative] = useState(c.directionNarrative);
  const [receiptOpen, setReceiptOpen] = useState(true);
  const [extracting, setExtracting] = useState(false);

  // Career Tracks: multi-select. The first entry is mirrored to the legacy
  // single-string `careerTrack` field so AccountSettings stays in sync and the
  // GoalsPromptBanner auto-hides as soon as one track is picked.
  const [careerTracks, setCareerTracks] = useState<string[]>(
    user?.careerTrack ? [user.careerTrack] : []
  );
  useEffect(() => {
    // Initialize from auth context on first load. Preserve any local multi-
    // select once the user has started picking (don't clobber on later user
    // doc refreshes that only carry the single careerTrack field).
    setCareerTracks((prev) => {
      if (prev.length > 0) return prev;
      return user?.careerTrack ? [user.careerTrack] : [];
    });
  }, [user?.careerTrack]);

  const persistCareerTracks = async (next: string[]) => {
    setCareerTracks(next);
    try {
      await persist({ careerTracks: next, careerTrack: next[0] || '' });
      if (updateUser) {
        await updateUser({ careerTrack: next[0] || '' });
      }
    } catch (e) {
      console.warn('careerTracks persist failed', e);
    }
  };

  // UI state for the Career Track type-ahead input
  const [careerTrackInput, setCareerTrackInput] = useState('');
  const [careerTrackOpen, setCareerTrackOpen] = useState(false);

  // Rotate the placeholder text while the input is empty + unfocused.
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  useEffect(() => {
    if (careerTrackInput || careerTrackOpen) return;
    const id = setInterval(() => {
      setPlaceholderIdx((i) => (i + 1) % CAREER_TRACK_PLACEHOLDERS.length);
    }, 2500);
    return () => clearInterval(id);
  }, [careerTrackInput, careerTrackOpen]);

  const careerTrackSuggestions = (() => {
    const q = careerTrackInput.trim().toLowerCase();
    const selectedSet = new Set(careerTracks.map((t) => t.toLowerCase()));
    const available = CAREER_TRACK_OPTIONS.filter((opt) => !selectedSet.has(opt.toLowerCase()));
    if (!q) return available;
    return available.filter((opt) => opt.toLowerCase().includes(q));
  })();

  const addCareerTrack = (value: string) => {
    const v = value.trim();
    if (!v) return;
    if (!careerTracks.some((t) => t.toLowerCase() === v.toLowerCase())) {
      persistCareerTracks([...careerTracks, v]);
    }
    setCareerTrackInput('');
    setCareerTrackOpen(false);
  };

  // Sync local textarea state when profile reloads
  useEffect(() => { setNarrative(c.directionNarrative); }, [c.directionNarrative]);

  // Smooth-scroll into view when entered via /profile#goals (e.g. from the
  // "Add goals" banner on Find).
  const goalsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (typeof window !== 'undefined' && window.location.hash === '#goals') {
      requestAnimationFrame(() => {
        goalsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }, []);

  // Cycle parsing — Firestore stores `recruitingCycle` (key) and `cycleYear` separately.
  // The mapped `c.cycle` is a display string ("Summer 2027"); for the picker we want the key.
  // Read raw Firestore values via a side channel — for now derive from display string.
  const [cycleKey, setCycleKey] = useState<string>('summer-sa');
  const [cycleYear, setCycleYear] = useState<number | null>(profile.classYear);
  useEffect(() => {
    // best-effort parse from the display cycle
    if (/full[-\s]?time/i.test(c.cycle)) setCycleKey('fulltime');
    else if (/off[-\s]?cycle/i.test(c.cycle)) setCycleKey('off-cycle');
    else if (/explor/i.test(c.cycle)) setCycleKey('exploring');
    else setCycleKey('summer-sa');
    const y = c.cycle.match(/(20\d{2})/);
    if (y) setCycleYear(parseInt(y[1], 10));
  }, [c.cycle]);

  const showReceipt = narrative.trim().length > 0 || profile.userIndustries.length > 0 || c.extractedRoles.length > 0;
  const industryUserSet = new Set(profile.userIndustries);
  const remainingIndustries = profile.suggestedIndustries.filter((s) => !industryUserSet.has(s));
  const roleUserSet = new Set(c.extractedRoles);
  const remainingRoles = c.suggestedRoles.filter((s) => !roleUserSet.has(s));
  const receiptSummary = `${profile.userIndustries.length} industries · ${c.extractedRoles.length} roles · ${c.targetFirms.length} firms · ${c.locations.length} locations`;

  const persistNarrative = async () => {
    if (narrative !== c.directionNarrative) {
      try { await persist({ directionNarrative: narrative }); } catch (e) { console.warn(e); }
    }
  };

  // Persistent updaters per row — each updates Firestore and reloads
  const persistChips = async (
    field: 'targetIndustries' | 'extractedRoles' | 'targetFirms' | 'preferredLocations',
    next: string[],
  ) => {
    try {
      await persist({ [field]: next });
      await reload();
    } catch (e) {
      console.warn('persistChips failed', e);
    }
  };

  const addToList = (current: string[], value: string) => {
    const set = new Set(current.map((s) => s.toLowerCase()));
    if (set.has(value.toLowerCase())) return current;
    return [...current, value];
  };
  const removeFromList = (current: string[], value: string) =>
    current.filter((s) => s.toLowerCase() !== value.toLowerCase());

  const handleExtract = async () => {
    const text = narrative.trim();
    if (text.length < 8) {
      toast({ title: 'Write a sentence first', description: 'Tell us what you\'re after, even a couple lines.', variant: 'destructive' });
      return;
    }
    setExtracting(true);
    try {
      // Persist narrative before extraction
      await persist({ directionNarrative: text });
      const token = auth.currentUser ? await auth.currentUser.getIdToken() : null;
      const res = await fetch(`${BACKEND_URL}/api/extract-direction`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({ narrative: text }),
      });
      const data = await res.json();
      if (!res.ok || !data?.success) {
        throw new Error(data?.error || 'Extraction failed');
      }
      const ex = data.extracted || {};

      // Merge extracted into existing chips (don't blow away what user already added)
      const nextIndustries = Array.from(new Set([...profile.userIndustries, ...(ex.industries || [])]));
      const nextRoles = Array.from(new Set([...c.extractedRoles, ...(ex.roles || [])]));
      const nextFirms = Array.from(new Set([...c.targetFirms, ...(ex.firms || [])]));
      const nextLocations = Array.from(new Set([...c.locations, ...(ex.locations || [])]));

      const update: Record<string, unknown> = {
        targetIndustries: nextIndustries,
        extractedRoles: nextRoles,
        targetFirms: nextFirms,
        preferredLocations: nextLocations,
      };
      if (ex.recruitingCycle) update.recruitingCycle = ex.recruitingCycle;
      if (ex.cycleYear) update.cycleYear = ex.cycleYear;

      await persist(update);
      toast({ title: 'Picked up your direction', description: `${(ex.industries||[]).length} industries · ${(ex.roles||[]).length} roles · ${(ex.firms||[]).length} firms` });
      await reload();
    } catch (e: any) {
      toast({ title: 'Could not extract', description: e?.message || 'Try again.', variant: 'destructive' });
    } finally {
      setExtracting(false);
    }
  };

  const ROW_LABEL_WIDTH = 84;
  const ROW_GAP = 18;

  const TryLine: React.FC<{ items: string[]; onAccept: (s: string) => void }> = ({ items, onAccept }) => (
    <div style={{ display: 'flex', gap: ROW_GAP, marginTop: 4, marginBottom: 10 }}>
      <div style={{ minWidth: ROW_LABEL_WIDTH }} />
      <div
        style={{
          fontSize: 11.5,
          color: C.ink3,
          display: 'inline-flex',
          alignItems: 'baseline',
          gap: 6,
          flexWrap: 'wrap',
        }}
      >
        <span>or try</span>
        {items.map((s, i) => (
          <React.Fragment key={s}>
            <button
              type="button"
              onClick={() => onAccept(s)}
              style={{
                background: 'transparent',
                border: 'none',
                padding: 0,
                color: C.ink2,
                fontFamily: 'inherit',
                fontSize: 11.5,
                cursor: 'pointer',
                fontWeight: 500,
                textDecoration: 'underline',
                textDecorationColor: C.hairline,
                textUnderlineOffset: 3,
              }}
            >
              {s}
            </button>
            {i < items.length - 1 && <span>·</span>}
          </React.Fragment>
        ))}
      </div>
    </div>
  );

  return (
    <div id="goals" ref={goalsRef} style={{ scrollMarginTop: 80 }}>
    <Card>
      <div style={{ marginBottom: 14 }}>
        <Serif size={20} style={{ display: 'block' }}>
          Goals
        </Serif>
        <TitleRule />
        <div style={{ fontSize: 13, color: C.ink2, marginTop: 12, lineHeight: 1.55 }}>
          Help us personalize your outreach and contact recommendations.
        </div>
      </div>

      {/* Career Track — single text input with autocomplete dropdown.
          Selected tracks render as removable chips below the input. Banner
          watches the legacy single `careerTrack`, kept in sync as
          `careerTracks[0]` via persistCareerTracks. */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: C.ink, marginBottom: 6 }}>
          Career Track
        </div>

        <div style={{ position: 'relative' }}>
          <input
            type="text"
            value={careerTrackInput}
            onChange={(e) => {
              setCareerTrackInput(e.target.value);
              setCareerTrackOpen(true);
            }}
            onFocus={() => setCareerTrackOpen(true)}
            onBlur={() => {
              // delay so a click on a suggestion fires before the dropdown closes
              setTimeout(() => setCareerTrackOpen(false), 150);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                const match = careerTrackSuggestions[0];
                addCareerTrack(match ?? careerTrackInput);
              } else if (e.key === 'Escape') {
                setCareerTrackOpen(false);
              }
            }}
            placeholder={CAREER_TRACK_PLACEHOLDERS[placeholderIdx]}
            style={{
              width: '100%',
              padding: '10px 14px',
              background: '#FAFBFE',
              border: `1px solid ${C.hairline}`,
              borderRadius: 8,
              fontSize: 14,
              color: C.ink,
              fontFamily: 'inherit',
              outline: 'none',
            }}
          />

          {careerTrackOpen && careerTrackSuggestions.length > 0 && (
            <div
              style={{
                position: 'absolute',
                top: 'calc(100% + 4px)',
                left: 0,
                right: 0,
                background: '#FFFFFF',
                border: `1px solid ${C.hairline}`,
                borderRadius: 8,
                boxShadow: '0 4px 14px rgba(15, 23, 42, 0.10)',
                zIndex: 10,
                maxHeight: 220,
                overflowY: 'auto',
              }}
            >
              {careerTrackSuggestions.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onMouseDown={(e) => {
                    // mouseDown fires before input blur, so we can add before
                    // the dropdown closes.
                    e.preventDefault();
                    addCareerTrack(opt);
                  }}
                  style={{
                    display: 'block',
                    width: '100%',
                    padding: '10px 14px',
                    background: 'transparent',
                    border: 'none',
                    textAlign: 'left',
                    fontSize: 14,
                    color: C.ink,
                    cursor: 'pointer',
                    fontFamily: 'inherit',
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = C.blueTint; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
                >
                  {opt}
                </button>
              ))}
            </div>
          )}
        </div>

        {careerTracks.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
            {careerTracks.map((track) => (
              <span
                key={track}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  padding: '4px 6px 4px 12px',
                  background: C.blueTint,
                  border: `1px solid ${C.blueBorder}`,
                  color: C.blue,
                  borderRadius: 999,
                  fontSize: 13,
                  fontWeight: 500,
                }}
              >
                {track}
                <button
                  type="button"
                  onClick={() => persistCareerTracks(careerTracks.filter((t) => t !== track))}
                  aria-label={`Remove ${track}`}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 18,
                    height: 18,
                    padding: 0,
                    border: 'none',
                    background: 'transparent',
                    color: C.blue,
                    cursor: 'pointer',
                    opacity: 0.7,
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.opacity = '1'; }}
                  onMouseLeave={(e) => { e.currentTarget.style.opacity = '0.7'; }}
                >
                  <X style={{ width: 12, height: 12 }} />
                </button>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Free-text narrative below: "what kind of work are you after?" */}
      <div style={{ fontSize: 13, color: C.ink2, marginBottom: 8, lineHeight: 1.55 }}>
        What kind of work are you after? Plain English: strengths, what you like, what you don't.
      </div>

      <div style={{ position: 'relative' }}>
        <textarea
          value={narrative}
          onChange={(e) => setNarrative(e.target.value)}
          onBlur={persistNarrative}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              handleExtract();
            }
          }}
          rows={3}
          maxLength={800}
          placeholder="Good with people, like talking to a room. Decent with numbers when I have to be. Want a small team that ships fast and cares about the work."
          style={{
            width: '100%',
            background: '#FAFBFE',
            border: `1px solid ${C.hairline}`,
            borderRadius: 8,
            padding: '12px 50px 12px 14px',
            fontSize: 13,
            fontFamily: 'inherit',
            color: C.ink,
            resize: 'vertical',
            outline: 'none',
            lineHeight: 1.6,
          }}
        />
        {/* Inline action — quiet circular arrow, bottom-right inside textarea */}
        <button
          type="button"
          onClick={handleExtract}
          disabled={extracting || narrative.trim().length < 8}
          aria-label="Pick out my direction"
          title={extracting ? 'Reading your direction…' : 'Pick out my direction'}
          style={{
            position: 'absolute',
            bottom: 8,
            right: 8,
            width: 24,
            height: 24,
            borderRadius: '50%',
            background:
              extracting || narrative.trim().length < 8
                ? 'transparent'
                : 'rgba(37, 99, 235, 0.10)',
            color:
              extracting || narrative.trim().length < 8
                ? C.ink3
                : C.blue,
            border: 'none',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor:
              extracting || narrative.trim().length < 8 ? 'not-allowed' : 'pointer',
            transition: 'background .15s ease, color .15s ease',
            padding: 0,
          }}
          onMouseEnter={(e) => {
            if (!extracting && narrative.trim().length >= 8) {
              e.currentTarget.style.background = 'rgba(37, 99, 235, 0.18)';
            }
          }}
          onMouseLeave={(e) => {
            if (!extracting && narrative.trim().length >= 8) {
              e.currentTarget.style.background = 'rgba(37, 99, 235, 0.10)';
            }
          }}
        >
          {extracting ? (
            <span
              style={{
                width: 11,
                height: 11,
                borderRadius: '50%',
                border: `1.5px solid ${C.blueBorder}`,
                borderTopColor: C.blue,
                animation: 'spin360 0.8s linear infinite',
              }}
            />
          ) : (
            <ArrowRight style={{ width: 12, height: 12 }} strokeWidth={2.25} />
          )}
        </button>
      </div>
      <div style={{ marginTop: 6, fontSize: 11.5, color: C.ink3 }}>
        {narrative.length}/800
      </div>

      {showReceipt && (
        <>
          {/* Quiet toggle — chevron + summary, right-aligned, no pill, no glow */}
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
            <button
              type="button"
              onClick={() => setReceiptOpen((v) => !v)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                background: 'transparent',
                border: 'none',
                padding: '4px 6px',
                cursor: 'pointer',
                fontFamily: 'inherit',
                color: C.ink3,
                fontSize: 12,
                transition: 'color .15s ease',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.color = C.blue)}
              onMouseLeave={(e) => (e.currentTarget.style.color = C.ink3)}
            >
              <span>{receiptOpen ? 'Hide details' : `Show details · ${receiptSummary}`}</span>
              {receiptOpen ? (
                <ChevronDown style={{ width: 13, height: 13 }} />
              ) : (
                <ChevronRight style={{ width: 13, height: 13 }} />
              )}
            </button>
          </div>

          {receiptOpen && (
            <div
              style={{
                marginTop: 12,
                padding: '20px 22px',
                background:
                  'linear-gradient(180deg, rgba(37, 99, 235, 0.035) 0%, rgba(37, 99, 235, 0.015) 100%)',
                border: `1px solid ${C.cardBorder}`,
                borderRadius: 12,
              }}
            >
              <EditableChipRow
                label="Industries"
                values={profile.userIndustries}
                taxonomy={INDUSTRIES_LIST}
                placeholder="e.g. Investment Banking"
                onAdd={(v) => persistChips('targetIndustries', addToList(profile.userIndustries, v))}
                onRemove={(v) => persistChips('targetIndustries', removeFromList(profile.userIndustries, v))}
                rowLabelWidth={ROW_LABEL_WIDTH}
                rowGap={ROW_GAP}
              />
              {remainingIndustries.length > 0 ? (
                <TryLine
                  items={remainingIndustries}
                  onAccept={(s) => persistChips('targetIndustries', addToList(profile.userIndustries, s))}
                />
              ) : (
                <div style={{ height: 14 }} />
              )}

              <EditableChipRow
                label="Roles"
                values={c.extractedRoles}
                taxonomy={ROLES_LIST}
                placeholder="e.g. Software Engineer"
                onAdd={(v) => persistChips('extractedRoles', addToList(c.extractedRoles, v))}
                onRemove={(v) => persistChips('extractedRoles', removeFromList(c.extractedRoles, v))}
                rowLabelWidth={ROW_LABEL_WIDTH}
                rowGap={ROW_GAP}
              />
              {remainingRoles.length > 0 ? (
                <TryLine
                  items={remainingRoles}
                  onAccept={(s) => persistChips('extractedRoles', addToList(c.extractedRoles, s))}
                />
              ) : (
                <div style={{ height: 14 }} />
              )}

              <EditableChipRow
                label="Firms"
                values={c.targetFirms}
                taxonomy={COMPANIES_LIST}
                placeholder="e.g. Goldman Sachs"
                onAdd={(v) => persistChips('targetFirms', addToList(c.targetFirms, v))}
                onRemove={(v) => persistChips('targetFirms', removeFromList(c.targetFirms, v))}
                rowLabelWidth={ROW_LABEL_WIDTH}
                rowGap={ROW_GAP}
              />
              <div style={{ height: 14 }} />

              <div style={{ display: 'flex', alignItems: 'center', gap: ROW_GAP }}>
                <Mono style={{ minWidth: ROW_LABEL_WIDTH }}>When</Mono>
                <WhenPicker
                  cycle={cycleKey}
                  cycleYear={cycleYear}
                  onChange={(key, year) => {
                    setCycleKey(key);
                    setCycleYear(year);
                    persist({ recruitingCycle: key, cycleYear: year }).then(() => reload());
                  }}
                />
              </div>
              <div style={{ height: 14 }} />

              <EditableChipRow
                label="Where"
                values={c.locations}
                taxonomy={COMMON_LOCATIONS}
                placeholder="e.g. New York, NY"
                onAdd={(v) => persistChips('preferredLocations', addToList(c.locations, v))}
                onRemove={(v) => persistChips('preferredLocations', removeFromList(c.locations, v))}
                rowLabelWidth={ROW_LABEL_WIDTH}
                rowGap={ROW_GAP}
              />
            </div>
          )}
        </>
      )}
    </Card>
    </div>
  );
}

// ── Personal context ─────────────────────────────────────────────────────────

function AnythingMissed() {
  const { profile, persist, reload } = useProfile();
  const { toast } = useToast();
  const [text, setText] = useState(profile.career.narrative);
  useEffect(() => { setText(profile.career.narrative); }, [profile.career.narrative]);

  // Typewriter prompts to prime the user's thinking when the textarea is empty.
  // Stagger: AnythingMissed types from the start with no delay; the attach
  // subtext starts on a different item with a small initial delay so the two
  // surfaces aren't typing in lockstep.
  const rotatingPlaceholder = useTypewriter(ANYTHING_MISSED_PLACEHOLDERS, { startOffset: 0, startDelayMs: 0 });
  const attachSubtext = useRotation(ATTACH_SUBTEXT_ROTATION, { startOffset: 1, startDelayMs: 900 });

  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const files = profile.personalContextFiles;

  const persistText = async () => {
    if (text !== profile.career.narrative) {
      try { await persist({ personalContext: text }); } catch (e) { console.warn(e); }
    }
  };

  const handleAttachClick = () => fileInputRef.current?.click();

  const handleFile = async (file: File) => {
    if (!profile.uid) return;
    if (file.size > 10 * 1024 * 1024) {
      toast({ title: 'File too large', description: 'Max 10MB.', variant: 'destructive' });
      return;
    }
    if (!/\.(pdf|docx?|doc|jpe?g|png)$/i.test(file.name)) {
      toast({ title: 'Unsupported file', description: 'PDF, DOC, DOCX, or images only.', variant: 'destructive' });
      return;
    }
    setUploading(true);
    try {
      // Reuse the resumes/{uid}/** storage path so existing storage rules apply.
      const storage = getStorage();
      const path = `resumes/${profile.uid}/personal-context/${Date.now()}-${file.name}`;
      const fileRef = storageRef(storage, path);
      await uploadBytes(fileRef, file, { contentType: file.type || 'application/octet-stream' });
      const url = await getDownloadURL(fileRef);

      const entry = {
        name: file.name,
        size: file.size,
        contentType: file.type || '',
        url,
        storagePath: path,
        uploadedAt: new Date().toISOString(),
      };
      const next = [...files, entry];
      await persist({ personalContextFiles: next });
      toast({ title: 'File attached', description: file.name });
      await reload();
    } catch (e: any) {
      toast({ title: 'Could not attach file', description: e?.message || 'Try again.', variant: 'destructive' });
    } finally {
      setUploading(false);
    }
  };

  const handleRemove = async (storagePath: string) => {
    try {
      try {
        await deleteObject(storageRef(getStorage(), storagePath));
      } catch (e) {
        // Continue even if storage delete fails (file may already be gone).
        console.warn('Storage delete failed:', e);
      }
      const remaining = files.filter((f) => f.storagePath !== storagePath);
      await persist({ personalContextFiles: remaining });
      await reload();
    } catch (e: any) {
      toast({ title: 'Could not remove file', description: e?.message || 'Try again.', variant: 'destructive' });
    }
  };

  const removeFile = (n: string) => setFiles((f) => f.filter((x) => x.name !== n));

  return (
    <Card>
      <Serif size={20} style={{ display: 'block' }}>
        Anything we missed about you?
      </Serif>
      <TitleRule />
      <div style={{ fontSize: 12.5, color: C.ink3, marginTop: 10, marginBottom: 12, lineHeight: 1.55 }}>
        Hobbies, hometown, work that didn't fit on a resume, side projects, anything specific or weird.
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={persistText}
        rows={3}
        maxLength={600}
        placeholder={rotatingPlaceholder}
        style={{
          width: '100%',
          background: '#FAFBFE',
          border: `1px solid ${C.hairline}`,
          borderRadius: 8,
          padding: '12px 14px',
          fontSize: 13,
          fontFamily: 'inherit',
          color: C.ink,
          resize: 'vertical',
          outline: 'none',
          lineHeight: 1.6,
        }}
      />

      {files.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
          {files.map((f) => (
            <div
              key={f.storagePath}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 8,
                padding: '5px 8px 5px 10px',
                background: C.blueTint,
                border: `1px solid ${C.blueBorder}`,
                borderRadius: 6,
                fontSize: 12,
                color: C.ink2,
                fontFamily: C.mono,
              }}
            >
              <Paperclip style={{ width: 11, height: 11, color: C.blue }} />
              <a
                href={f.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: C.ink2, textDecoration: 'none' }}
                title={f.name}
              >
                {f.name}
              </a>
              <span style={{ color: C.ink3 }}>·</span>
              <span style={{ color: C.ink3 }}>{formatBytes(f.size)}</span>
              <button
                type="button"
                onClick={() => handleRemove(f.storagePath)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  padding: 2,
                  cursor: 'pointer',
                  color: C.ink3,
                  display: 'inline-flex',
                  alignItems: 'center',
                }}
                aria-label={`Remove ${f.name}`}
              >
                <X style={{ width: 11, height: 11 }} />
              </button>
            </div>
          ))}
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept=".pdf,.doc,.docx,image/*"
        style={{ display: 'none' }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) handleFile(f);
          if (fileInputRef.current) fileInputRef.current.value = '';
        }}
      />
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 12,
          marginTop: 10,
          fontSize: 12,
          color: C.ink3,
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5, flex: 1, minWidth: 0 }}>
          <button
            type="button"
            onClick={uploading ? undefined : handleAttachClick}
            disabled={uploading}
            style={{
              alignSelf: 'flex-start',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              padding: '5px 10px',
              background: 'transparent',
              border: `1px dashed ${C.hairline}`,
              borderRadius: 6,
              color: C.ink2,
              fontSize: 12,
              fontFamily: 'inherit',
              cursor: uploading ? 'wait' : 'pointer',
              opacity: uploading ? 0.6 : 1,
            }}
          >
            <Paperclip style={{ width: 11, height: 11 }} />
            {uploading ? 'Uploading…' : 'Attach file'}
          </button>
          <span
            style={{
              fontSize: 11.5,
              color: C.ink3,
              fontStyle: 'italic',
              lineHeight: 1.4,
              transition: 'opacity .3s',
            }}
          >
            {attachSubtext}
          </span>
        </div>
        <span style={{ flexShrink: 0 }}>{text.length}/600</span>
      </div>
    </Card>
  );
}

// HardNos — the suppression-list blurb. Free-form text the user fills with
// roles, companies, locations, or anything they want the system to filter
// AWAY from their feed. Feeds the same downstream rankers as the positive
// fields, but inverted. Mirrors the AnythingMissed pattern (textarea +
// blur-persist) without the file attachment complexity.
function HardNos() {
  const { profile, persist } = useProfile();
  const [text, setText] = useState(profile.career.hardNos);
  useEffect(() => { setText(profile.career.hardNos); }, [profile.career.hardNos]);

  // Different start offset + extra delay so HardNos types at a different
  // moment than the AnythingMissed placeholder above it.
  const rotatingPlaceholder = useTypewriter(HARD_NOS_PLACEHOLDERS, { startOffset: 2, startDelayMs: 1800 });

  const persistText = async () => {
    if (text !== profile.career.hardNos) {
      try { await persist({ hardNos: text }); } catch (e) { console.warn(e); }
    }
  };

  return (
    <Card>
      <Serif size={20} style={{ display: 'block' }}>
        Tell us what to filter out
      </Serif>
      <TitleRule />
      <div style={{ fontSize: 12.5, color: C.ink3, marginTop: 10, marginBottom: 12, lineHeight: 1.55 }}>
        Roles, companies, locations, or anything else you've ruled out. We'll
        suppress these from job recommendations, contact suggestions, and Scout's advice.
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={persistText}
        rows={3}
        maxLength={600}
        placeholder={rotatingPlaceholder}
        style={{
          width: '100%',
          background: '#FAFBFE',
          border: `1px solid ${C.hairline}`,
          borderRadius: 8,
          padding: '12px 14px',
          fontSize: 13,
          fontFamily: 'inherit',
          color: C.ink,
          resize: 'vertical',
          outline: 'none',
          lineHeight: 1.6,
        }}
      />
      <div
        style={{
          display: 'flex',
          justifyContent: 'flex-end',
          alignItems: 'center',
          marginTop: 10,
          fontSize: 12,
          color: C.ink3,
        }}
      >
        <span>{text.length}/600</span>
      </div>
    </Card>
  );
}

// Rotating placeholder sets — give the user concrete examples to ride on
// when their textarea is empty. Each list rotates every ~4 seconds, so a user
// who pauses to think gets a fresh nudge. Pure brain-priming, no AI required.
const ANYTHING_MISSED_PLACEHOLDERS: string[] = [
  "I led a supply-chain group project last fall · I tutor freshmen in econ · Mock trial team",
  "Speak Mandarin · Competitive sailor · Listen to Acquired and Odd Lots religiously",
  "From Atlanta · GA Tech transfer · Co-founded a finance club my sophomore year",
  "Worked retail in HS · Read SEC filings for fun · Big fan of behavioral econ",
  "Wrote a class paper on dark pools · Run a sneaker reselling side gig · Watched the Wirecard doc 3x",
];

const HARD_NOS_PLACEHOLDERS: string[] = [
  "No crypto roles · Avoid LA · Not interested in sales positions",
  "Skip series A startups · No remote-only roles · Avoid commute > 30 min",
  "No defense industry · Skip oil/gas · Not interested in pure research roles",
  "Pass on retail banking · Avoid roles that need a PhD · Skip non-tier-1 consulting",
];

const ATTACH_SUBTEXT_ROTATION: string[] = [
  "Drop a class essay — Scout learns how you write",
  "A personal statement is the best material for prep sheets",
  "Cover letters help us match your voice in outreach emails",
  "Any writing sample — papers, project docs, anything that captures how you think",
  "Resume PDF, a paper you wrote, even a deck — all fair game",
];

/**
 * Typewriter rotation — types each item out character-by-character, holds,
 * erases, advances to the next. Per-instance `startOffset` lets multiple
 * surfaces start at different points in the cycle so they don't all type in
 * lockstep on first mount.
 *
 *  typeMs:  ms per character while typing forward
 *  holdMs:  ms to keep the fully-typed item visible
 *  eraseMs: ms per character while backspacing
 *  startOffset: index in `items` to start at (0..items.length-1)
 *  startDelayMs: delay before any typing begins (lets other surfaces lead)
 */
function useTypewriter(
  items: string[],
  opts?: { typeMs?: number; holdMs?: number; eraseMs?: number; startOffset?: number; startDelayMs?: number },
): string {
  const typeMs = opts?.typeMs ?? 38;
  const holdMs = opts?.holdMs ?? 2400;
  const eraseMs = opts?.eraseMs ?? 18;
  const startDelayMs = opts?.startDelayMs ?? 0;
  const initialIdx = ((opts?.startOffset ?? 0) % Math.max(1, items.length) + items.length) % Math.max(1, items.length);

  const [idx, setIdx] = useState(initialIdx);
  const [phase, setPhase] = useState<'idle' | 'typing' | 'holding' | 'erasing'>(startDelayMs > 0 ? 'idle' : 'typing');
  const [displayed, setDisplayed] = useState('');

  // Initial delay before the very first character types out.
  useEffect(() => {
    if (phase !== 'idle') return;
    const t = setTimeout(() => setPhase('typing'), startDelayMs);
    return () => clearTimeout(t);
  }, [phase, startDelayMs]);

  useEffect(() => {
    if (items.length === 0) return;
    const target = items[idx % items.length] || '';

    if (phase === 'typing') {
      if (displayed.length < target.length) {
        const t = setTimeout(() => setDisplayed(target.slice(0, displayed.length + 1)), typeMs);
        return () => clearTimeout(t);
      }
      const t = setTimeout(() => setPhase('erasing'), holdMs);
      return () => clearTimeout(t);
    }

    if (phase === 'erasing') {
      if (displayed.length > 0) {
        const t = setTimeout(() => setDisplayed(target.slice(0, displayed.length - 1)), eraseMs);
        return () => clearTimeout(t);
      }
      setIdx((i) => (i + 1) % items.length);
      setPhase('typing');
    }
  }, [displayed, phase, idx, items, typeMs, holdMs, eraseMs]);

  return displayed;
}

// Sibling of useTypewriter that just swaps full strings on an interval — no
// character-by-character typing. Use for places where the text should rotate
// quietly without animation.
function useRotation(
  items: string[],
  opts?: { intervalMs?: number; startOffset?: number; startDelayMs?: number },
): string {
  const intervalMs = opts?.intervalMs ?? 4500;
  const startDelayMs = opts?.startDelayMs ?? 0;
  const initialIdx =
    ((opts?.startOffset ?? 0) % Math.max(1, items.length) + items.length) %
    Math.max(1, items.length);
  const [idx, setIdx] = useState(initialIdx);

  useEffect(() => {
    if (items.length <= 1) return;
    let intervalId: number | undefined;
    const delayId = window.setTimeout(() => {
      intervalId = window.setInterval(() => {
        setIdx((i) => (i + 1) % items.length);
      }, intervalMs);
    }, startDelayMs);
    return () => {
      clearTimeout(delayId);
      if (intervalId !== undefined) clearInterval(intervalId);
    };
  }, [items.length, intervalMs, startDelayMs]);

  return items[idx] || '';
}

// ── Page ─────────────────────────────────────────────────────────────────────

const ProfilePreview: React.FC = () => {
  const [profile, setProfile] = useState<ProfileData>(EMPTY);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  const load = useCallback(async () => {
    try {
      const u = auth.currentUser;
      if (!u) {
        setProfile(EMPTY);
        return;
      }
      const ref = doc(db, 'users', u.uid);
      const snap = await getDoc(ref);
      const data = snap.exists() ? (snap.data() as Record<string, any>) : {};
      setProfile(mapToProfile(u.uid, data, u.photoURL || null));
    } catch (e) {
      console.error('Failed to load profile:', e);
      toast({ title: 'Could not load profile', description: 'Try refreshing the page.', variant: 'destructive' });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    // Wait for auth to settle, then load
    const unsub = onAuthStateChanged(auth, (u) => {
      if (u) load();
      else { setProfile(EMPTY); setLoading(false); }
    });
    return () => unsub();
  }, [load]);

  const persist = useCallback(async (patch: Record<string, unknown>) => {
    const u = auth.currentUser;
    if (!u) return;
    const ref = doc(db, 'users', u.uid);
    await updateDoc(ref, { ...patch, lastLogin: new Date().toISOString() });
  }, []);

  const setLocal = useCallback((updater: (p: ProfileData) => ProfileData) => {
    setProfile((prev) => updater(prev));
  }, []);

  // Shared file input for both initial upload and Replace
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const pickResumeFile = useCallback(() => fileInputRef.current?.click(), []);

  const uploadResume = useCallback(async (file: File) => {
    if (!auth.currentUser) return;
    if (file.size > 10 * 1024 * 1024) {
      toast({ title: 'File too large', description: 'Max 10MB. Try a smaller PDF or DOCX.', variant: 'destructive' });
      return;
    }
    if (!/\.(pdf|docx?|doc)$/i.test(file.name)) {
      toast({ title: 'Unsupported file', description: 'Please upload a PDF, DOCX, or DOC.', variant: 'destructive' });
      return;
    }

    try {
      const formData = new FormData();
      formData.append('resume', file);
      const token = await auth.currentUser.getIdToken();
      const res = await fetch(`${BACKEND_URL}/api/parse-resume`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      const result = await res.json();
      if (!res.ok) throw new Error(result.error || 'Failed to parse resume');

      await persist({
        resumeFileName: file.name,
        resumeUrl: result.resumeUrl || result.fileUrl || `local://${file.name}`,
        resumeUpdatedAt: new Date().toISOString(),
        resumeParsed: result.parsed || result.data || result,
      });

      toast({ title: 'Resume updated', description: 'Latest experience, skills, and education extracted.' });
      await load();
    } catch (e: any) {
      toast({ title: 'Could not parse resume', description: e?.message || 'Please try again.', variant: 'destructive' });
    }
  }, [persist, load, toast]);

  const refreshLinkedIn = useCallback(async () => {
    if (!auth.currentUser || !profile.linkedin.url) return;
    const url = profile.linkedin.url.startsWith('http')
      ? profile.linkedin.url
      : `https://${profile.linkedin.url}`;
    try {
      const token = await auth.currentUser.getIdToken();
      const res = await fetch(`${BACKEND_URL}/api/enrich-linkedin-onboarding`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ linkedin_url: url }),
      });
      const data = await res.json();
      if (data?.enriched) {
        toast({ title: 'LinkedIn refreshed', description: 'Latest data pulled.' });
        await load();
      } else {
        toast({
          title: 'Could not refresh',
          description: data?.error || 'Try again in a few minutes.',
          variant: 'destructive',
        });
      }
    } catch (e: any) {
      toast({ title: 'Refresh failed', description: e?.message || 'Try again.', variant: 'destructive' });
    }
  }, [profile.linkedin.url, load, toast]);

  const disconnectLinkedIn = useCallback(async () => {
    if (!auth.currentUser) return;
    if (!window.confirm('Disconnect LinkedIn? This removes your URL and the enriched data. You can reconnect any time.')) {
      return;
    }
    try {
      await persist({
        linkedinUrl: '',
        linkedinEnrichmentData: null,
        linkedinResumeParsed: null,
        linkedinEnrichmentSource: null,
        linkedinEnrichedAt: null,
      });
      toast({ title: 'LinkedIn disconnected' });
      await load();
    } catch (e: any) {
      toast({ title: 'Could not disconnect', description: e?.message || 'Try again.', variant: 'destructive' });
    }
  }, [persist, load, toast]);

  const ctx: ProfileContextValue = {
    profile,
    loading,
    setLocal,
    persist,
    reload: load,
    uploadResume,
    pickResumeFile,
    refreshLinkedIn,
    disconnectLinkedIn,
  };

  // Compute summaries for the source-card collapsed states
  const r = profile.resume;
  const li = profile.linkedin;
  const briefStats: string[] = [];
  if (r.experiences.length > 0) briefStats.push(`${r.experiences.length} role${r.experiences.length === 1 ? '' : 's'}`);
  if (r.projects.length > 0) briefStats.push(`${r.projects.length} project${r.projects.length === 1 ? '' : 's'}`);
  if (r.skills.length > 0) briefStats.push(`${r.skills.length} skills`);
  if (r.gpa != null) briefStats.push(`GPA ${r.gpa}`);
  const resumeSummary = r.connected
    ? r.fileName
      ? `${r.fileName}${briefStats.length ? ` · ${briefStats.join(' · ')}` : ''}`
      : briefStats.length
      ? briefStats.join(' · ')
      : 'Resume on file.'
    : 'Drop a PDF or DOCX to extract your experience, skills, and education.';
  const linkedinSummary = li.connected
    ? [
        li.headline,
        li.connections.total > 0
          ? `${li.connections.total.toLocaleString()} connections${
              li.connections.byCompany[0]
                ? ` — ${li.connections.byCompany[0].count} at ${li.connections.byCompany[0].company}`
                : ''
            }`
          : null,
      ]
        .filter(Boolean)
        .join(' · ') || li.url
    : 'Add your LinkedIn to enrich the profile with your network.';

  return (
    <ProfileContext.Provider value={ctx}>
      <SidebarProvider>
        <style>{`
          @keyframes spin360 { to { transform: rotate(360deg); } }
          .btn-spin {
            width: 14px;
            height: 14px;
            border: 2px solid rgba(255,255,255,0.4);
            border-top-color: #FFFFFF;
            border-radius: 50%;
            animation: spin360 0.8s linear infinite;
          }
        `}</style>
        <div className="flex min-h-screen w-full font-sans" style={{ color: C.ink }}>
          <AppSidebar />
          <MainContentWrapper>
            <AppHeader title="Profile" />
            <div
              className="relative"
              style={{
                flex: 1,
                overflow: 'auto',
                background: `linear-gradient(180deg, ${C.bgGradTop} 0%, ${C.bgGradBottom} 100%)`,
                padding: '40px 48px 80px',
              }}
            >
              {/* Watercolor mountain backdrop — mirrors the home page so the
                  profile feels like an extension of the same environment, not
                  a separate destination. Same /mountains-lake.png asset, same
                  fixed-bottom positioning + 70vh height. */}
              <img
                src="/mountains-lake.png"
                alt=""
                aria-hidden
                draggable={false}
                style={{
                  position: 'fixed',
                  bottom: 0,
                  left: 0,
                  width: '100%',
                  height: '70vh',
                  objectFit: 'cover',
                  objectPosition: 'bottom center',
                  opacity: 0.9,
                  zIndex: 0,
                  pointerEvents: 'none',
                  userSelect: 'none',
                }}
              />
              <div className="relative" style={{ maxWidth: 760, margin: '0 auto', zIndex: 1 }}>
                {loading ? (
                  <div style={{ fontSize: 13, color: C.ink3, padding: '32px 0' }}>Loading your profile…</div>
                ) : (
                  <>
                    {/* Hidden file input — triggered by Replace action and ResumeUpload zone */}
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,.docx,.doc"
                      style={{ display: 'none' }}
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) uploadResume(f);
                        if (fileInputRef.current) fileInputRef.current.value = '';
                      }}
                    />

                    <Header />

                    <ConnectAiAssistantsCard style={{ marginBottom: 14 }} />

                    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                      <CollapsibleSource
                        icon={FileText}
                        title="Resume"
                        meta={r.connected ? (r.parsedAt ? `Parsed ${r.parsedAt}` : 'On file') : 'Not added'}
                        summary={resumeSummary}
                        rightActions={r.connected ? [{ label: 'Replace', onClick: pickResumeFile }] : undefined}
                        defaultOpen={!r.connected}
                        emptyState={!r.connected}
                      >
                        {r.connected ? <ResumeDetail /> : <ResumeUpload />}
                      </CollapsibleSource>

                      <CollapsibleSource
                        icon={Linkedin}
                        title="LinkedIn"
                        meta={li.connected ? (li.refreshedAt ? `Synced ${li.refreshedAt}` : 'On file') : 'Not connected'}
                        summary={linkedinSummary}
                        rightActions={
                          li.connected
                            ? [
                                { label: 'Refresh', onClick: refreshLinkedIn },
                                { label: 'Disconnect', onClick: disconnectLinkedIn, variant: 'destructive' },
                              ]
                            : undefined
                        }
                        defaultOpen={!li.connected}
                        emptyState={!li.connected}
                      >
                        {li.connected ? <LinkedInDetail /> : <LinkedInConnect />}
                      </CollapsibleSource>

                      <Direction />

                      <AnythingMissed />

                      <HardNos />
                    </div>
                  </>
                )}
              </div>
            </div>
          </MainContentWrapper>
        </div>
      </SidebarProvider>
    </ProfileContext.Provider>
  );
};

export default ProfilePreview;
