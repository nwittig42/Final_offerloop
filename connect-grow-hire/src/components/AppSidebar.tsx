import { useState } from "react";
import {
  ChevronDown,
  Zap,
  LogOut,
  Settings,
  Info,
  MessageSquare,
  Shield,
  ScrollText,
  PanelLeft,
  Tag,
  FileText,
  Users,
  Search,
  Coffee,
  Inbox,
  Briefcase,
  Upload,
  BookOpen,
  Bell,
  User,
  Gift,
  Plug,
  Server,
  ClipboardList,
} from "lucide-react";

import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useFirebaseAuth } from "../contexts/FirebaseAuthContext";
import { trackNavClick, trackUpgradeClick } from "../lib/analytics";
import { useTour } from "@/contexts/TourContext";
import { useNotifications } from "@/hooks/useNotifications";
import { useApplicationsAttention } from "@/hooks/useApplicationsAttention";
import { CreditsPanel } from "./sidebar/CreditsPanel";
import { useCreditsView } from "@/hooks/useCreditsView";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  useSidebar,
} from "@/components/ui/sidebar";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

// Design tokens — deep-navy rail per AppSidebar.reference.jsx
const T = {
  rail:        "#0F172A",
  wordmark:    "#FFFFFF",
  accent:      "#7B8FC9",                  // periwinkle
  activeBg:    "rgba(123,143,201,0.20)",
  activeText:  "#9CA8CD",
  hoverBg:     "rgba(255,255,255,0.05)",
  idleText:    "#94A3B8",
  hoverText:   "#E8EAF0",
  sectionText: "#5B677E",
  hairline:    "rgba(255,255,255,0.07)",
  popoverBg:   "#1A2438",
  action:      "var(--action, #E07A3E)",
  actionHover: "var(--action-dark, #C9652C)",
  upgradeBg:   "#4C62A8",                   // proto slate-blue for Upgrade Plan button
  upgradeIcon: "#FACC15",                   // proto yellow for the lightning bolt
  fontDisplay: "var(--font-display, 'Lora', Georgia, serif)",
  fontBody:    "var(--font-body, 'Inter', system-ui, sans-serif)",
};

type NavItemDef = {
  title: string;
  url: string;
  LucideIcon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>;
  dataTour?: string;
};

// RocketReach-style grouping: Discover = find/collect data, Engage = act on it.
const discoverNavItems: NavItemDef[] = [
  { title: "Search",      url: "/find",        LucideIcon: Search },
  { title: "Job Board",   url: "/job-board",   LucideIcon: Briefcase },
  { title: "My Network",  url: "/my-network",  LucideIcon: Users },
  { title: "Upload List", url: "/upload-list", LucideIcon: Upload },
];

const engageNavItems: NavItemDef[] = [
  { title: "Inbox",        url: "/outbox",           LucideIcon: Inbox,  dataTour: "tour-track-email" },
  { title: "Meeting Prep", url: "/coffee-chat-prep", LucideIcon: Coffee, dataTour: "tour-coffee-chat-prep" },
];

const applicationsNavItems: NavItemDef[] = [
  { title: "Applications", url: "/applications", LucideIcon: ClipboardList },
];

const utilityNavItems: NavItemDef[] = [
  { title: "Integrations",  url: "/integrations",  LucideIcon: Plug },
  { title: "MCP Server",    url: "/mcp-server",    LucideIcon: Server },
  { title: "Pricing",       url: "/pricing",       LucideIcon: Tag },
];

const userMenuItems = [
  { title: "Profile",           url: "/profile",            icon: User },
  { title: "Refer & Earn",      url: "/refer",              icon: Gift },
  { title: "Account Settings",  url: "/account-settings",   icon: Settings },
  { title: "Documentation",     url: "/documentation",      icon: FileText },
  { title: "About Us",          url: "/about",              icon: Info },
  { title: "Contact Us",        url: "/contact-us",         icon: MessageSquare },
  { title: "Privacy Policy",    url: "/privacy",            icon: Shield },
  { title: "Terms of Service",  url: "/terms-of-service",   icon: ScrollText },
];

// Compact scale: the labeled sections must fit without
// scrolling, so nav rows run tighter than the old single-list layout.
const NAV_FONT_SIZE = "12.5px";
const NAV_PY = "7.5px";
const NAV_GAP = "10px";
const NAV_RADIUS = "8px";

export function AppSidebar() {
  const { state, toggleSidebar } = useSidebar();
  const location = useLocation();
  const navigate = useNavigate();
  const currentPath = location.pathname;
  const { user, signOut } = useFirebaseAuth();
  const [userDropdownOpen, setUserDropdownOpen] = useState(false);

  const isActive = (url: string) => {
    const basePath = url.split("?")[0];
    return currentPath === basePath || currentPath.startsWith(basePath + "/");
  };

  const handleSignOut = async () => {
    try {
      await signOut();
      navigate("/");
    } catch (error) {
      console.error("Sign out error:", error);
    }
  };

  // Trial-aware single source of truth for the credits widget. During a Pro
  // trial this returns the daily budget (300/day) instead of the frozen monthly
  // pool; for free/paid users it returns the normal balance + tier cap (derived
  // from TIER_CONFIGS, so a stale Firestore maxCredits can't drift the display).
  const creditsView = useCreditsView();
  const isCollapsed = state === "collapsed";

  const { startTour } = useTour();
  const { notifications } = useNotifications();
  const unreadCount = notifications.unreadReplyCount;

  const applicationsAttention = useApplicationsAttention();

  const initials =
    user?.name
      ?.split(" ")
      .map((n) => n[0])
      .join("")
      .toUpperCase()
      .slice(0, 2) ||
    user?.email?.[0]?.toUpperCase() ||
    "U";

  const sectionLabel = (text: string) => (
    <div
      style={{
        fontFamily: T.fontBody,
        fontSize: "10px",
        fontWeight: 600,
        letterSpacing: "0.08em",
        textTransform: "uppercase",
        color: T.sectionText,
        padding: "3px 12px 6px",
      }}
    >
      {text}
    </div>
  );

  const renderNavItem = (item: NavItemDef) => {
    const active = isActive(item.url);
    const Icon = item.LucideIcon;

    const iconEl = (
      <Icon
        className="h-4 w-4 flex-shrink-0"
        style={{
          color: active ? T.activeText : T.idleText,
          opacity: active ? 1 : 0.85,
        }}
      />
    );

    if (isCollapsed) {
      return (
        <Tooltip key={item.title}>
          <TooltipTrigger asChild>
            <NavLink
              to={item.url}
              data-tour={item.dataTour}
              onClick={() => trackNavClick(item.title, "sidebar")}
              className="flex items-center justify-center transition-colors"
              style={{
                padding: NAV_PY,
                background: active ? T.activeBg : "transparent",
                borderRadius: NAV_RADIUS,
                textDecoration: "none",
              }}
              onMouseEnter={(e) => {
                if (!active) e.currentTarget.style.background = T.hoverBg;
              }}
              onMouseLeave={(e) => {
                if (!active) e.currentTarget.style.background = "transparent";
              }}
            >
              {iconEl}
            </NavLink>
          </TooltipTrigger>
          <TooltipContent side="right">{item.title}</TooltipContent>
        </Tooltip>
      );
    }

    return (
      <NavLink
        key={item.title}
        to={item.url}
        data-tour={item.dataTour}
        onClick={() => trackNavClick(item.title, "sidebar")}
        className="flex items-center transition-colors"
        style={{
          gap: NAV_GAP,
          paddingTop: NAV_PY,
          paddingBottom: NAV_PY,
          paddingLeft: "12px",
          paddingRight: "12px",
          borderRadius: NAV_RADIUS,
          fontSize: NAV_FONT_SIZE,
          fontWeight: active ? 600 : 500,
          fontFamily: T.fontBody,
          background: active ? T.activeBg : "transparent",
          color: active ? T.activeText : T.idleText,
          textDecoration: "none",
        }}
        onMouseEnter={(e) => {
          if (!active) {
            e.currentTarget.style.background = T.hoverBg;
            e.currentTarget.style.color = T.hoverText;
          }
        }}
        onMouseLeave={(e) => {
          if (!active) {
            e.currentTarget.style.background = "transparent";
            e.currentTarget.style.color = T.idleText;
          }
        }}
      >
        <span className="relative">
          {iconEl}
        </span>
        <span className="flex-1">{item.title}</span>
        {item.title === "Applications" && applicationsAttention > 0 && (
          <span
            className="ml-auto text-[10px] font-medium rounded-full px-1.5 py-0.5 leading-none"
            style={{
              background: "rgba(252,211,77,0.15)",
              color: "#FCD34D",
            }}
          >
            {applicationsAttention}
          </span>
        )}
      </NavLink>
    );
  };

  return (
    <TooltipProvider>
      {/* Width comes from ui/sidebar's SIDEBAR_WIDTH / SIDEBAR_WIDTH_ICON so
          the layout spacer and the visible rail always agree; a className
          width here only resizes the rail and leaves a white gap. */}
      <Sidebar collapsible="icon">
        <SidebarContent
          className="flex flex-col h-full overflow-hidden"
          style={{
            background: T.rail,
            borderRight: `1px solid ${T.hairline}`,
          }}
        >
          {/* User row + collapse toggle (dropdown opens downward) */}
          <div
            style={{
              padding: isCollapsed ? "16px 8px" : "14px 12px 10px",
              position: "relative",
            }}
          >
            {isCollapsed ? (
              <div className="flex justify-center">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      onClick={toggleSidebar}
                      className="p-2 rounded-lg transition-colors"
                      style={{ color: T.idleText, background: "transparent", border: "none", cursor: "pointer" }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.background = T.hoverBg;
                        e.currentTarget.style.color = T.hoverText;
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.background = "transparent";
                        e.currentTarget.style.color = T.idleText;
                      }}
                      aria-label="Expand sidebar"
                    >
                      <PanelLeft className="h-5 w-5" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="right">Expand sidebar</TooltipContent>
                </Tooltip>
              </div>
            ) : (
              <div className="flex items-center" style={{ gap: 4 }}>
                <button
                  onClick={() => setUserDropdownOpen(!userDropdownOpen)}
                  className="flex items-center transition-colors rounded-lg flex-1"
                  style={{
                    gap: 10,
                    padding: "4px 6px",
                    background: userDropdownOpen ? T.hoverBg : "transparent",
                    border: "none",
                    cursor: "pointer",
                    textAlign: "left",
                    minWidth: 0,
                  }}
                  onMouseEnter={(e) => {
                    if (!userDropdownOpen) e.currentTarget.style.background = T.hoverBg;
                  }}
                  onMouseLeave={(e) => {
                    if (!userDropdownOpen) e.currentTarget.style.background = "transparent";
                  }}
                >
                  <Avatar
                    className="h-8 w-8 flex-shrink-0"
                    style={{
                      background: "rgba(123,143,201,0.22)",
                      boxShadow: "0 0 0 2px rgba(123,143,201,0.35)",
                    }}
                  >
                    {user?.picture && <AvatarImage src={user.picture} alt={user.name} />}
                    <AvatarFallback
                      style={{
                        background: "rgba(123,143,201,0.22)",
                        color: T.activeText,
                        fontFamily: T.fontBody,
                        fontWeight: 700,
                        fontSize: 12,
                      }}
                    >
                      {initials}
                    </AvatarFallback>
                  </Avatar>
                  <span
                    className="truncate flex-1"
                    style={{
                      fontFamily: T.fontBody,
                      fontSize: 13.5,
                      fontWeight: 500,
                      color: T.hoverText,
                      position: "relative",
                    }}
                  >
                    {user?.name || "User"}
                    {unreadCount > 0 && (
                      <span
                        style={{
                          position: "absolute",
                          top: -4,
                          right: 18,
                          minWidth: 8,
                          height: 8,
                          borderRadius: 999,
                          background: T.action as string,
                        }}
                      />
                    )}
                  </span>
                  <ChevronDown
                    className={cn(
                      "h-3.5 w-3.5 transition-transform flex-shrink-0",
                      userDropdownOpen && "rotate-180"
                    )}
                    style={{ color: T.sectionText }}
                  />
                </button>

                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      onClick={toggleSidebar}
                      className="p-1.5 rounded-lg transition-colors flex-shrink-0"
                      style={{ color: T.idleText, background: "transparent", border: "none", cursor: "pointer" }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.background = T.hoverBg;
                        e.currentTarget.style.color = T.hoverText;
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.background = "transparent";
                        e.currentTarget.style.color = T.idleText;
                      }}
                      aria-label="Collapse sidebar"
                    >
                      <PanelLeft className="h-4 w-4" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="right">Collapse sidebar</TooltipContent>
                </Tooltip>
              </div>
            )}

            {/* Dropdown — opens downward, contains Tour + Notifications + existing menu + Sign out */}
            {!isCollapsed && userDropdownOpen && (
              <div
                style={{
                  position: "absolute",
                  top: "calc(100% - 2px)",
                  left: 12,
                  right: 12,
                  background: T.popoverBg,
                  border: `1px solid ${T.hairline}`,
                  borderRadius: 10,
                  padding: 6,
                  boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
                  zIndex: 50,
                }}
              >
                <button
                  onClick={() => {
                    setUserDropdownOpen(false);
                    startTour();
                  }}
                  className="flex items-center gap-3 transition-colors w-full"
                  style={{
                    padding: "8px 10px",
                    borderRadius: 6,
                    fontFamily: T.fontBody,
                    fontSize: 13,
                    color: T.idleText,
                    background: "transparent",
                    border: "none",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = T.hoverBg;
                    e.currentTarget.style.color = T.hoverText;
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "transparent";
                    e.currentTarget.style.color = T.idleText;
                  }}
                >
                  <BookOpen className="h-4 w-4" />
                  <span>Take the tour</span>
                </button>
                <NavLink
                  to="/tracker"
                  onClick={() => {
                    setUserDropdownOpen(false);
                    trackNavClick("Notifications", "sidebar_dropdown");
                  }}
                  className="flex items-center gap-3 transition-colors"
                  style={{
                    padding: "8px 10px",
                    borderRadius: 6,
                    fontFamily: T.fontBody,
                    fontSize: 13,
                    color: T.idleText,
                    textDecoration: "none",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = T.hoverBg;
                    e.currentTarget.style.color = T.hoverText;
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "transparent";
                    e.currentTarget.style.color = T.idleText;
                  }}
                >
                  <Bell className="h-4 w-4" />
                  <span className="flex-1">Notifications</span>
                  {unreadCount > 0 && (
                    <span
                      style={{
                        minWidth: 18,
                        height: 16,
                        padding: "0 5px",
                        borderRadius: 999,
                        background: T.action as string,
                        color: "#fff",
                        fontSize: 10,
                        fontWeight: 700,
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        lineHeight: 1,
                      }}
                    >
                      {unreadCount > 9 ? "9+" : unreadCount}
                    </span>
                  )}
                </NavLink>
                <div style={{ margin: "4px 0", borderTop: `1px solid ${T.hairline}` }} />
                {userMenuItems.map((item) => (
                  <NavLink
                    key={item.title}
                    to={item.url}
                    onClick={() => {
                      setUserDropdownOpen(false);
                      trackNavClick(item.title, "sidebar_dropdown");
                    }}
                    className="flex items-center gap-3 transition-colors"
                    style={{
                      padding: "8px 10px",
                      borderRadius: 6,
                      fontFamily: T.fontBody,
                      fontSize: 13,
                      color: T.idleText,
                      textDecoration: "none",
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = T.hoverBg;
                      e.currentTarget.style.color = T.hoverText;
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = "transparent";
                      e.currentTarget.style.color = T.idleText;
                    }}
                  >
                    <item.icon className="h-4 w-4" />
                    <span>{item.title}</span>
                  </NavLink>
                ))}
                <div style={{ margin: "4px 0", borderTop: `1px solid ${T.hairline}` }} />
                <button
                  onClick={handleSignOut}
                  className="flex items-center gap-3 transition-colors w-full"
                  style={{
                    padding: "8px 10px",
                    borderRadius: 6,
                    fontFamily: T.fontBody,
                    fontSize: 13,
                    color: T.idleText,
                    background: "transparent",
                    border: "none",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = T.hoverBg;
                    e.currentTarget.style.color = T.hoverText;
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "transparent";
                    e.currentTarget.style.color = T.idleText;
                  }}
                >
                  <LogOut className="h-4 w-4" />
                  <span>Sign out</span>
                </button>
              </div>
            )}
          </div>

          {/* Navigation */}
          <nav
            className="flex-1 overflow-y-auto flex flex-col"
            style={{
              padding: isCollapsed ? "0 8px" : "0 14px",
            }}
          >
            {!isCollapsed && sectionLabel("Discover")}
            {isCollapsed && (
              <div style={{ borderTop: `1px solid ${T.hairline}`, margin: "0 6px 6px" }} />
            )}
            <div className="flex flex-col" style={{ gap: 2 }}>
              {discoverNavItems.map(renderNavItem)}
            </div>
            <div style={{ height: 12 }} />
            {!isCollapsed && sectionLabel("Engage")}
            {isCollapsed && (
              <div style={{ borderTop: `1px solid ${T.hairline}`, margin: "0 6px 6px" }} />
            )}
            <div className="flex flex-col" style={{ gap: 2 }}>
              {engageNavItems.map(renderNavItem)}
            </div>
            <div style={{ height: 12 }} />
            {!isCollapsed && sectionLabel("Applications")}
            {isCollapsed && (
              <div style={{ borderTop: `1px solid ${T.hairline}`, margin: "0 6px 6px" }} />
            )}
            <div className="flex flex-col" style={{ gap: 2 }}>
              {applicationsNavItems.map(renderNavItem)}
            </div>
            <div className="flex-1" />
            {!isCollapsed && sectionLabel("Resources")}
            <div className="flex flex-col" style={{ gap: 2, paddingBottom: 8 }}>
              {utilityNavItems.map(renderNavItem)}
            </div>
          </nav>
        </SidebarContent>

        {/* Footer — credits + upgrade + user row */}
        <SidebarFooter
          style={{
            background: T.rail,
            borderTop: `1px solid ${T.hairline}`,
            padding: 14,
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          {!isCollapsed ? (
            <CreditsPanel
              remaining={creditsView.balance}
              total={creditsView.total}
              isTrialing={creditsView.isTrialing}
              daysRemaining={creditsView.daysRemaining}
              onUpgrade={() => {
                trackUpgradeClick("sidebar", {
                  from_location: creditsView.isTrialing ? "sidebar_trial" : "sidebar",
                });
                navigate("/pricing");
              }}
            />
          ) : (
            <>
              {/* Collapsed footer: upgrade + avatar */}
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    onClick={() => {
                      trackUpgradeClick("sidebar", { from_location: "sidebar" });
                      navigate("/pricing");
                    }}
                    className="w-full flex items-center justify-center p-2.5 transition-all"
                    style={{
                      background: T.upgradeBg,
                      color: "#FFFFFF",
                      borderRadius: 3,
                      border: "none",
                      cursor: "pointer",
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.filter = "brightness(1.05)";
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.filter = "none";
                    }}
                  >
                    <Zap className="h-5 w-5" style={{ color: T.upgradeIcon, fill: T.upgradeIcon }} />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="right">
                  <div className="text-xs">
                    <p className="font-medium">
                      {creditsView.isTrialing
                        ? `Trial: ${creditsView.balance}/${creditsView.total} today`
                        : `Credits: ${creditsView.balance}/${creditsView.total}`}
                    </p>
                    <p className="text-gray-400 mt-0.5">Click to upgrade</p>
                  </div>
                </TooltipContent>
              </Tooltip>
            </>
          )}
        </SidebarFooter>
      </Sidebar>
    </TooltipProvider>
  );
}
