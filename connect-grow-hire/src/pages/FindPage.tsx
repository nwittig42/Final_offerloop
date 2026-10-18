import React, { Suspense, useState, useEffect, useRef } from "react";
import { useSearchParams, useLocation, useNavigate } from "react-router-dom";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { MainContentWrapper } from "@/components/MainContentWrapper";
import { Search, Building2, UserCheck } from "lucide-react";
import { PageTitle } from "@/components/PageTitle";
import { LoadingSkeleton } from "@/components/LoadingSkeleton";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { apiService, type EmailTemplate } from "@/services/api";
import { firebaseApi } from "@/services/firebaseApi";
import { EliteGateModal } from "@/components/EliteGateModal";
import { NoSchoolEmptyState } from "@/components/NoSchoolEmptyState";
import { GoalsPromptBanner } from "@/components/find/GoalsPromptBanner";
import MountainsLake from "@/assets/for-students/mountains-lake.png";
import { IS_DEV_PREVIEW, DEV_MOCK_USER } from "@/lib/devPreview";
import { getUniversityShortName } from "@/lib/universityUtils";
import { PersonalizationStrip } from "@/components/personalization/PersonalizationStrip";
import { FindFilterRail } from "@/components/find/FindFilterRail";
import {
  FindTab, PeopleFilters, CompanyFilters, EMPTY_PEOPLE_FILTERS, EMPTY_COMPANY_FILTERS,
  peopleFiltersActive, companyFiltersActive,
} from "@/types/findFilters";

const ContactSearchPage = React.lazy(() => import("./ContactSearchPage"));
const FirmSearchPage = React.lazy(() => import("./FirmSearchPage"));
const RecruiterSpreadsheetPage = React.lazy(() => import("./RecruiterSpreadsheetPage"));
const TABS = [
  { id: "people", label: "People", mobileLabel: "People", icon: Search },
  { id: "companies", label: "Companies", mobileLabel: "Companies", icon: Building2 },
  { id: "hiring-managers", label: "Hiring Managers", mobileLabel: "Hiring", icon: UserCheck },
] as const;

function resolveTab(raw: string | null): FindTab {
  if (raw === "people") return "people";
  if (raw === "companies") return "companies";
  if (raw === "hiring-managers") return "hiring-managers";
  return "people";
}

// SearchSurface: just the tabs + tab content, no page shell.
// Used by DashboardPage to embed search below the agent snapshot.
export const SearchSurface: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = resolveTab(searchParams.get("tab"));
  const routerLocation = useLocation();
  const { user: authUser } = useFirebaseAuth();
  const user = IS_DEV_PREVIEW ? DEV_MOCK_USER : authUser;

  const [savedEmailTemplate, setSavedEmailTemplate] = useState<EmailTemplate | null>(null);
  const [sessionEmailTemplate, setSessionEmailTemplate] = useState<EmailTemplate | null>(null);
  const activeEmailTemplate = sessionEmailTemplate ?? savedEmailTemplate;
  const [userUniversity, setUserUniversity] = useState<string | null>(null);
  const [userFirstName, setUserFirstName] = useState<string | null>(null);

  useEffect(() => {
    if (!user?.uid) return;
    if (IS_DEV_PREVIEW) {
      setUserUniversity((user as any)?.university || "");
      setUserFirstName("Demo");
      return;
    }
    firebaseApi.getUserOnboardingData(user.uid).then((data) => {
      setUserUniversity(data.university || null);
      setUserFirstName(data.firstName || null);
    }).catch(() => {});
  }, [user?.uid]);

  useEffect(() => {
    if (!user?.uid || IS_DEV_PREVIEW) return;
    apiService.getEmailTemplate().then((t) => {
      setSavedEmailTemplate({
        purpose: t.purpose, stylePreset: t.stylePreset, customInstructions: t.customInstructions,
        signoffPhrase: t.signoffPhrase, signatureBlock: t.signatureBlock, name: t.name,
        subject: t.subject, savedTemplateId: t.savedTemplateId,
      });
    }).catch(() => {});
  }, [user?.uid]);

  useEffect(() => {
    const state = (routerLocation.state as { appliedEmailTemplate?: EmailTemplate } | undefined)?.appliedEmailTemplate;
    if (state) {
      setSessionEmailTemplate(state);
      sessionStorage.removeItem("offerloop_applied_email_template");
      return;
    }
    try {
      const raw = sessionStorage.getItem("offerloop_applied_email_template");
      if (raw) {
        const parsed = JSON.parse(raw) as EmailTemplate;
        if (parsed && typeof parsed === "object") setSessionEmailTemplate(parsed);
        sessionStorage.removeItem("offerloop_applied_email_template");
      }
    } catch {}
  }, [routerLocation.state]);

  // Flash effect: when a tab changes, briefly tint the active tab vibrant
  // blue then fade back to the slate accent. Adds visible feedback to the
  // segmented-control slide.
  const [tabFlashing, setTabFlashing] = useState(false);
  const tabFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flashTab = () => {
    setTabFlashing(true);
    if (tabFlashTimer.current) clearTimeout(tabFlashTimer.current);
    tabFlashTimer.current = setTimeout(() => setTabFlashing(false), 700);
  };
  useEffect(() => () => {
    if (tabFlashTimer.current) clearTimeout(tabFlashTimer.current);
  }, []);

  const setActiveTab = (tab: FindTab) => {
    setSearchParams({ tab }, { replace: true });
    flashTab();
  };

  const isCompaniesTab = activeTab === "companies";

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {/* Page title */}
      <div>
        <div style={{ maxWidth: 800, margin: "0 auto", padding: "24px 0 8px" }}>
          {isCompaniesTab && userUniversity && (
            <PersonalizationStrip firstName={userFirstName} university={userUniversity} />
          )}
          <PageTitle
            lead={
              isCompaniesTab
                ? "Discover"
                : activeTab === "hiring-managers"
                ? "Connect with"
                : undefined
            }
            accent={
              isCompaniesTab
                ? "Companies"
                : activeTab === "hiring-managers"
                ? "Hiring Managers"
                : "meet?"
            }
          >
            {isCompaniesTab || activeTab === "hiring-managers"
              ? undefined
              : "Who do you want to"}
          </PageTitle>
        </div>
      </div>

      {/* Tab bar */}
      <div style={{ borderBottom: "1px solid var(--line)" }}>
        <div style={{ maxWidth: 800, margin: "0 auto" }}>
          <div style={{ display: "flex", gap: 0 }}>
            {TABS.map((tab) => {
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className="max-sm:!px-4 max-sm:!text-[13px]"
                  style={{
                    display: "flex", alignItems: "center", gap: 7,
                    padding: "12px 20px 10px", fontSize: 13,
                    fontWeight: isActive ? 500 : 400, cursor: "pointer",
                    border: "none",
                    borderBottom: isActive ? "2px solid var(--accent)" : "2px solid transparent",
                    marginBottom: -1, background: "transparent",
                    color: isActive ? "var(--ink)" : "var(--ink-3)",
                    transition: "all .15s", fontFamily: "inherit",
                  }}
                  onMouseEnter={(e) => { if (!isActive) (e.currentTarget as HTMLButtonElement).style.color = "var(--ink-2)"; }}
                  onMouseLeave={(e) => { if (!isActive) (e.currentTarget as HTMLButtonElement).style.color = "var(--ink-3)"; }}
                >
                  <tab.icon style={{ width: 14, height: 14, flexShrink: 0 }} />
                  <span className="hidden sm:inline">{tab.label}</span>
                  <span className="sm:hidden">{tab.mobileLabel}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Stats bar */}
      {(() => {
        const uniShort = getUniversityShortName(userUniversity);
        const statsItems = [
          uniShort ? `3,200+ ${uniShort} alumni tracked` : null,
          "13,700+ jobs indexed",
          "Updated today",
        ].filter(Boolean) as string[];
        return (
          <div style={{
            display: "flex", justifyContent: "center", alignItems: "center", gap: 0,
            padding: "10px 0 14px", fontSize: 11, fontWeight: 400, color: "var(--ink-2)",
            fontFamily: "'JetBrains Mono', monospace",
          }}>
            {statsItems.map((item, i) => (
              <React.Fragment key={item}>
                {i > 0 && <span style={{ margin: "0 8px", fontSize: 9 }}>·</span>}
                <span>{item}</span>
              </React.Fragment>
            ))}
          </div>
        );
      })()}

      {/* Tab body */}
      <div style={{ borderTop: "none" }}>
        <div style={{ maxWidth: 800, margin: "0 auto", padding: "0 0 44px" }}>
          <Suspense
            fallback={
              <div className="flex items-center justify-center py-20">
                <LoadingSkeleton />
              </div>
            }
          >
            <div style={{ display: activeTab === "people" ? "block" : "none" }}>
              <ContactSearchPage embedded hideSubTabs parentEmailTemplate={activeEmailTemplate} isDevPreview={IS_DEV_PREVIEW} />
            </div>
            <div data-tour="tour-find-companies" style={{ display: activeTab === "companies" ? "block" : "none" }}>
              <FirmSearchPage embedded isDevPreview={IS_DEV_PREVIEW} />
            </div>
            <div data-tour="tour-find-hiring-managers" style={{ display: activeTab === "hiring-managers" ? "block" : "none" }}>
              <RecruiterSpreadsheetPage embedded isDevPreview={IS_DEV_PREVIEW} />
            </div>
          </Suspense>
        </div>
      </div>
    </div>
  );
};

const FindPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = resolveTab(searchParams.get("tab"));
  const routerLocation = useLocation();
  const navigate = useNavigate();
  const { user: authUser } = useFirebaseAuth();
  const user = IS_DEV_PREVIEW ? DEV_MOCK_USER : authUser;

  const [showEliteGate, setShowEliteGate] = useState(false);
  const [savedEmailTemplate, setSavedEmailTemplate] = useState<EmailTemplate | null>(null);
  const [sessionEmailTemplate, setSessionEmailTemplate] = useState<EmailTemplate | null>(null);
  const activeEmailTemplate = sessionEmailTemplate ?? savedEmailTemplate;
  const [userUniversity, setUserUniversity] = useState<string | null>(null);
  const [userFirstName, setUserFirstName] = useState<string | null>(null);
  // Explicit "I don't have a school" opt-out: skips the school wall and renders
  // Find without alumni personalization.
  const [universityOptOut, setUniversityOptOut] = useState(false);
  const [schoolLoaded, setSchoolLoaded] = useState(false);
  const [forceRefresh, setForceRefresh] = useState(0);

  // Filter-rail state. Nonce bumps ONLY on user edits (not on parse-populate),
  // signalling the embedded search page to re-run with overrides. An edit that
  // clears every dimension resets instead of re-searching (backend would 400).
  const [peopleFilters, setPeopleFilters] = useState<PeopleFilters>(EMPTY_PEOPLE_FILTERS);
  const [peopleFiltersNonce, setPeopleFiltersNonce] = useState(0);
  const [companyFilters, setCompanyFilters] = useState<CompanyFilters>(EMPTY_COMPANY_FILTERS);
  const [companyFiltersNonce, setCompanyFiltersNonce] = useState(0);

  const handlePeopleFiltersChange = (f: PeopleFilters) => {
    setPeopleFilters(f);
    if (peopleFiltersActive(f)) setPeopleFiltersNonce((n) => n + 1);
    else setPeopleFiltersNonce(0); // cleared → fresh state, no re-search
  };
  const handleCompanyFiltersChange = (f: CompanyFilters) => {
    setCompanyFilters(f);
    if (companyFiltersActive(f)) setCompanyFiltersNonce((n) => n + 1);
    else setCompanyFiltersNonce(0);
  };

  // Load user university + first name
  useEffect(() => {
    if (!user?.uid) return;
    if (IS_DEV_PREVIEW) {
      setUserUniversity((user as any)?.university || "");
      setUserFirstName("Demo");
      setSchoolLoaded(true);
      return;
    }
    firebaseApi.getUserOnboardingData(user.uid).then((data) => {
      setUserUniversity(data.university || null);
      setUserFirstName(data.firstName || null);
      setUniversityOptOut(data.universityOptOut === true);
      setSchoolLoaded(true);
    }).catch(() => setSchoolLoaded(true));
  }, [user?.uid, forceRefresh]);

  // Load saved email template on mount
  useEffect(() => {
    if (!user?.uid || IS_DEV_PREVIEW) return;
    apiService.getEmailTemplate().then((t) => {
      setSavedEmailTemplate({
        purpose: t.purpose,
        stylePreset: t.stylePreset,
        customInstructions: t.customInstructions,
        signoffPhrase: t.signoffPhrase,
        signatureBlock: t.signatureBlock,
        name: t.name,
        subject: t.subject,
        savedTemplateId: t.savedTemplateId,
      });
    }).catch(() => {});
  }, [user?.uid]);

  // Pick up template applied from EmailTemplatesPage via router state or sessionStorage
  useEffect(() => {
    const state = (routerLocation.state as { appliedEmailTemplate?: EmailTemplate } | undefined)?.appliedEmailTemplate;
    if (state) {
      setSessionEmailTemplate(state);
      sessionStorage.removeItem("offerloop_applied_email_template");
      return;
    }
    try {
      const raw = sessionStorage.getItem("offerloop_applied_email_template");
      if (raw) {
        const parsed = JSON.parse(raw) as EmailTemplate;
        if (parsed && typeof parsed === "object") setSessionEmailTemplate(parsed);
        sessionStorage.removeItem("offerloop_applied_email_template");
      }
    } catch {}
  }, [routerLocation.state]);

  // Flash effect: when a tab changes, briefly tint the active tab vibrant
  // blue then fade back to the slate accent. Adds visible feedback to the
  // segmented-control slide.
  const [tabFlashing, setTabFlashing] = useState(false);
  const tabFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flashTab = () => {
    setTabFlashing(true);
    if (tabFlashTimer.current) clearTimeout(tabFlashTimer.current);
    tabFlashTimer.current = setTimeout(() => setTabFlashing(false), 700);
  };
  useEffect(() => () => {
    if (tabFlashTimer.current) clearTimeout(tabFlashTimer.current);
  }, []);

  const setActiveTab = (tab: FindTab) => {
    setSearchParams({ tab }, { replace: true });
    flashTab();
  };

  // Query handed off from the Getting Started launcher (/find?tab=..&q=..).
  // Captured once, then stripped from the URL so it does not re-fire. Because
  // both search pages stay mounted behind the toggle, we pass it only to the
  // tab it targets so the hidden page never consumes it.
  const [launchQuery, setLaunchQuery] = useState<{ tab: FindTab; q: string } | null>(null);
  useEffect(() => {
    const q = searchParams.get("q");
    if (q && q.trim()) {
      setLaunchQuery({ tab: resolveTab(searchParams.get("tab")), q });
      const next = new URLSearchParams(searchParams);
      next.delete("q");
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const peopleInitialQuery =
    launchQuery && launchQuery.tab === "people" ? launchQuery.q : undefined;
  const companiesInitialQuery =
    launchQuery && launchQuery.tab === "companies" ? launchQuery.q : undefined;

  const isCompaniesTab = activeTab === "companies";

  // No-school empty state. Users who explicitly opted out of having a school
  // (universityOptOut) get the normal Find experience, minus alumni personalization.
  if (schoolLoaded && !userUniversity && !universityOptOut && user?.uid) {
    return (
      <SidebarProvider>
        <div className="flex min-h-screen w-full font-sans" style={{ color: "var(--ink)" }}>
          <AppSidebar />
          <MainContentWrapper>
            <AppHeader title="" />
            <NoSchoolEmptyState
              uid={user.uid}
              onSchoolSet={() => setForceRefresh((n) => n + 1)}
            />
          </MainContentWrapper>
        </div>
      </SidebarProvider>
    );
  }

  return (
    <>
    <SidebarProvider>
      <div className="flex min-h-screen w-full font-sans" style={{
        color: 'var(--ink)',
      }}>
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="" />
          <GoalsPromptBanner />

          {/* Scrollable page body */}
          <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column", position: "relative" }}>
            {/* Mountains as a top-anchored backdrop behind the hero/search only.
                Sits as an upper band that fades in at the top and fades out before
                the results area, so a successful search renders on clean page bg
                rather than over the mountain ridge. */}
            <div
              aria-hidden
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                right: 0,
                height: "88vh",
                pointerEvents: "none",
                zIndex: 0,
                backgroundImage: `url(${MountainsLake})`,
                backgroundSize: "120% auto",
                backgroundPosition: "center bottom",
                backgroundRepeat: "no-repeat",
                opacity: 0.5,
                maskImage:
                  "linear-gradient(180deg, transparent 0%, rgba(0,0,0,0.4) 18%, #000 50%, rgba(0,0,0,0.45) 82%, transparent 100%)",
                WebkitMaskImage:
                  "linear-gradient(180deg, transparent 0%, rgba(0,0,0,0.4) 18%, #000 50%, rgba(0,0,0,0.45) 82%, transparent 100%)",
              }}
            />
            {/* Page title — wrapped in a z-index:1 layer so it sits above
                the mountain backdrop. */}
            <div style={{ flexShrink: 0, position: "relative", zIndex: 1 }}>
              <div style={{ maxWidth: 1000, margin: '0 auto', padding: '36px 40px 6px' }}>
                <PageTitle
                  align="center"
                  noScribble
                  size={isCompaniesTab ? "lg" : "md"}
                  lead={
                    isCompaniesTab
                      ? "Discover"
                      : activeTab === "hiring-managers"
                      ? "Connect with"
                      : "Who do you want to"
                  }
                  accent={
                    isCompaniesTab
                      ? "Companies"
                      : activeTab === "hiring-managers"
                      ? "Hiring Managers"
                      : "meet?"
                  }
                  subtitle={
                    isCompaniesTab
                      ? "Find companies that match your interests."
                      : activeTab === "hiring-managers"
                      ? "Find the hiring manager behind a role, and draft a message that gets noticed."
                      : "Find the right people, get their contact info, and draft outreach in one step."
                  }
                />
              </div>
            </div>

            {/* Body — left vertical toggle + tab content */}
            <div style={{ flex: 1, overflowY: "auto", borderTop: "none", position: "relative", zIndex: 1 }}>
              <div
                className="flex flex-col sm:flex-row"
                style={{ maxWidth: 1120, margin: "0 auto", padding: "0 40px 44px", gap: 28 }}
              >
                {/* Left rail — tab toggle + filter panel (FindFilterRail) */}
                <div className="flex-shrink-0 sm:w-[236px]">
                  <FindFilterRail
                    activeTab={activeTab}
                    onTabChange={setActiveTab}
                    tabFlashing={tabFlashing}
                    peopleFilters={peopleFilters}
                    onPeopleFiltersChange={handlePeopleFiltersChange}
                    companyFilters={companyFilters}
                    onCompanyFiltersChange={handleCompanyFiltersChange}
                  />
                </div>

                {/* Tab content */}
                <div className="min-w-0 flex-1">
                  <Suspense
                    fallback={
                      <div className="flex items-center justify-center py-20">
                        <LoadingSkeleton />
                      </div>
                    }
                  >
                    <div style={{ display: activeTab === "people" ? "block" : "none" }}>
                      <ContactSearchPage embedded hideSubTabs parentEmailTemplate={activeEmailTemplate} isDevPreview={IS_DEV_PREVIEW} initialQuery={peopleInitialQuery}
                        railFilters={peopleFilters} railFiltersNonce={peopleFiltersNonce} onParsedQuery={setPeopleFilters} />
                    </div>
                    <div data-tour="tour-find-companies" style={{ display: activeTab === "companies" ? "block" : "none" }}>
                      <FirmSearchPage embedded isDevPreview={IS_DEV_PREVIEW} initialQuery={companiesInitialQuery}
                        railFilters={companyFilters} railFiltersNonce={companyFiltersNonce} onParsedFilters={setCompanyFilters} />
                    </div>
                    <div data-tour="tour-find-hiring-managers" style={{ display: activeTab === "hiring-managers" ? "block" : "none" }}>
                      <RecruiterSpreadsheetPage embedded isDevPreview={IS_DEV_PREVIEW} />
                    </div>
                  </Suspense>
                </div>
              </div>
            </div>
          </div>
        </MainContentWrapper>
      </div>
    </SidebarProvider>
    <EliteGateModal open={showEliteGate} onClose={() => setShowEliteGate(false)} />
    </>
  );
};

export default FindPage;
