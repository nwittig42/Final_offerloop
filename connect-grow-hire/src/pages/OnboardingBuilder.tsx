import { useEffect, useState } from "react";
import { Loader2, Sparkles, FileText, Download } from "lucide-react";
import {
  generateResumeBuilder,
  finalizeResumeBuilder,
  downloadResumeBuilderPdf,
  downloadPdfBlob,
} from "@/services/api";
import { ResumePrefill, resumePrefillFromParse } from "@/utils/onboardingPrefill";
import { BUILDER_DRAFT_VERSION, type BuilderDraft } from "@/services/onboardingProgress";
import { OB, obFieldLabel, obPrimaryButton } from "./onboardingTheme";

// Empty Harvard outline shown before the first generation.
const GHOST_SECTIONS = ["Education", "Experience", "Projects", "Leadership", "Skills"];

// Guided sections: labeled inputs with concrete examples produce far better
// resumes than one empty box. Values are composed into a labeled prompt for
// the backend, which researches the organizations mentioned and writes
// Harvard-style bullets.
const BUILDER_SECTIONS = [
  {
    key: "education",
    label: "Education",
    placeholder: "USC, Business Administration, Class of 2027. Add your GPA if you want it shown.",
  },
  {
    key: "experience",
    label: "Work experience",
    placeholder: "Sales associate at Men's Wearhouse, summer 2024. List each job, even part time. Anything you did or are proud of.",
  },
  {
    key: "projects",
    label: "Projects",
    placeholder: "Case competitions, class projects, a personal site. Example: microfinance analysis project with Kiva.",
  },
  {
    key: "leadership",
    label: "Leadership and activities",
    placeholder: "Clubs, teams, volunteering. Example: events lead for a business club, intramural captain.",
  },
  {
    key: "skills",
    label: "Skills and interests",
    placeholder: "Excel, SQL, Spanish, Figma. Interests are welcome too: chess, cooking, marathon running.",
  },
] as const;

type SectionKey = (typeof BUILDER_SECTIONS)[number]["key"];

const EMPTY_SECTIONS: Record<SectionKey, string> = {
  education: "",
  experience: "",
  projects: "",
  leadership: "",
  skills: "",
};

interface OnboardingBuilderProps {
  onComplete: (prefill: ResumePrefill) => void;
  submitting: boolean;
  /** Reads work saved before a refresh (or before leaving this step). */
  loadDraft?: () => BuilderDraft | null;
  /** Called on every change with the work so far (null once there's nothing to keep). */
  onDraftChange?: (draft: BuilderDraft | null) => void;
}

export const OnboardingBuilder = ({ onComplete, submitting, loadDraft, onDraftChange }: OnboardingBuilderProps) => {
  const [initialDraft] = useState(() => loadDraft?.() ?? null);
  const [sections, setSections] = useState<Record<SectionKey, string>>(() => ({
    ...EMPTY_SECTIONS,
    ...(initialDraft?.sections || {}),
  }));
  const [prompt, setPrompt] = useState(initialDraft?.prompt || "");
  const [history, setHistory] = useState<string[]>(initialDraft?.history || []);
  const [resume, setResume] = useState<object | null>(initialDraft?.resume || null);
  const [html, setHtml] = useState(initialDraft?.html || "");

  // Save as the user types, so a refresh doesn't lose anything.
  useEffect(() => {
    if (!onDraftChange) return;
    const empty = !resume && !prompt.trim() && !Object.values(sections).some((v) => v.trim());
    onDraftChange(empty ? null : { version: BUILDER_DRAFT_VERSION, sections, prompt, history, resume, html });
  }, [sections, prompt, history, resume, html, onDraftChange]);
  const [generating, setGenerating] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");

  const sectionsValid = Object.values(sections).some((v) => v.trim());

  const composePrompt = () =>
    BUILDER_SECTIONS.filter((s) => sections[s.key].trim())
      .map((s) => `${s.label.toUpperCase()}:\n${sections[s.key].trim()}`)
      .join("\n\n");

  const handleGenerate = async () => {
    const p = resume ? prompt.trim() : composePrompt();
    if (!p || generating) return;
    setError("");
    setGenerating(true);
    try {
      const res = await generateResumeBuilder(p, resume);
      setResume(res.resume as object);
      setHtml(res.html);
      if (resume) {
        // Only refine instructions go in the visible history; the initial
        // sectioned input is already on screen.
        setHistory((h) => [...h, prompt.trim()]);
        setPrompt("");
      }
    } catch (e) {
      setError(
        e instanceof Error && e.message === "generation_limit_reached"
          ? "You've used all free generations. Upload a resume instead, or edit this one after onboarding."
          : "Couldn't generate right now. Your details are saved, try again."
      );
    } finally {
      setGenerating(false);
    }
  };

  const handleUse = async () => {
    if (!resume || finalizing) return;
    setError("");
    setFinalizing(true);
    try {
      const res = await finalizeResumeBuilder(resume);
      onComplete(resumePrefillFromParse(res.parsed));
    } catch {
      setError("Couldn't save your resume. Try again.");
      setFinalizing(false);
    }
  };

  const handleDownload = async () => {
    if (!resume || downloading || generating) return;
    setError("");
    setDownloading(true);
    try {
      const blob = await downloadResumeBuilderPdf(resume);
      downloadPdfBlob(blob, "Offerloop_Resume.pdf");
    } catch {
      setError("Couldn't download the PDF. Try again.");
    } finally {
      setDownloading(false);
    }
  };

  const busy = generating || finalizing || submitting;
  const canGenerate = resume ? !!prompt.trim() : sectionsValid;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 380px) minmax(0, 1fr)", gap: 28 }}>
      {/* Left: sectioned inputs before first generation, refine box after */}
      <div>
        {!resume ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {BUILDER_SECTIONS.map((s) => (
              <div key={s.key}>
                <label style={obFieldLabel} htmlFor={`builder-${s.key}`}>
                  {s.label}
                </label>
                <textarea
                  id={`builder-${s.key}`}
                  value={sections[s.key]}
                  onChange={(e) => setSections((prev) => ({ ...prev, [s.key]: e.target.value }))}
                  placeholder={s.placeholder}
                  rows={2}
                  style={{
                    width: "100%",
                    border: `1px solid ${OB.border}`,
                    borderRadius: 10,
                    padding: "10px 12px",
                    fontFamily: OB.fontBody,
                    fontSize: 14,
                    color: OB.ink,
                    resize: "vertical",
                    outline: "none",
                  }}
                />
              </div>
            ))}
            <p style={{ fontSize: 12.5, color: OB.ink4, margin: 0, lineHeight: 1.5 }}>
              Plain words are fine, fill in what you have. We research the places you mention and
              write the bullet points for you.
            </p>
          </div>
        ) : (
          <div>
            <label
              style={{ fontWeight: 600, fontSize: 14, color: OB.heading, display: "block", marginBottom: 8 }}
            >
              Refine it
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Example: add my SQL project from last summer, and make the club bullets stronger."
              rows={5}
              style={{
                width: "100%",
                border: `1px solid ${OB.border}`,
                borderRadius: 10,
                padding: "12px 14px",
                fontFamily: OB.fontBody,
                fontSize: 15,
                color: OB.ink,
                resize: "vertical",
                outline: "none",
              }}
            />
            {history.length > 0 && (
              <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
                {history.map((h, i) => (
                  <div
                    key={i}
                    style={{
                      fontSize: 12.5,
                      color: OB.ink4,
                      background: OB.primary50,
                      borderRadius: 8,
                      padding: "7px 10px",
                    }}
                  >
                    {h}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {error && <p style={{ fontSize: 13, color: "#DC2626", margin: "10px 0 0" }}>{error}</p>}

        <button
          type="button"
          onClick={handleGenerate}
          disabled={!canGenerate || busy}
          style={{
            ...obPrimaryButton,
            marginTop: 14,
            opacity: canGenerate && !busy ? 1 : 0.5,
            cursor: canGenerate && !busy ? "pointer" : "default",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
          }}
        >
          {generating ? (
            <Loader2 size={17} className="animate-spin" />
          ) : (
            <Sparkles size={16} strokeWidth={1.7} />
          )}
          {generating ? "Writing your resume…" : resume ? "Refine resume" : "Generate my resume"}
        </button>
        {resume && (
          <>
            <button
              type="button"
              onClick={handleUse}
              disabled={busy}
              style={{
                ...obPrimaryButton,
                marginTop: 10,
                background: "#fff",
                color: OB.primary,
                border: `1.5px solid ${OB.primary}`,
                boxShadow: "none",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 8,
                opacity: busy ? 0.5 : 1,
                cursor: busy ? "default" : "pointer",
              }}
            >
              {finalizing || submitting ? (
                <Loader2 size={17} className="animate-spin" />
              ) : (
                <FileText size={16} strokeWidth={1.7} />
              )}
              Use this resume
            </button>
            <button
              type="button"
              onClick={handleDownload}
              disabled={downloading || generating}
              style={{
                marginTop: 10,
                width: "100%",
                background: "none",
                border: "none",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 7,
                fontFamily: OB.fontBody,
                fontSize: 13.5,
                fontWeight: 600,
                color: OB.ink3,
                padding: "6px 0",
                opacity: downloading || generating ? 0.5 : 1,
                cursor: downloading || generating ? "default" : "pointer",
              }}
            >
              {downloading ? (
                <Loader2 size={15} className="animate-spin" />
              ) : (
                <Download size={15} strokeWidth={1.8} />
              )}
              {downloading ? "Preparing PDF..." : "Download PDF"}
            </button>
            <p style={{ fontSize: 12.5, color: OB.ink4, margin: "8px 0 0", textAlign: "center" }}>
              Saved to your account as a PDF.
            </p>
          </>
        )}
      </div>

      {/* Right: live Harvard outline / preview */}
      <div
        style={{
          border: `1px solid ${OB.border}`,
          borderRadius: 14,
          background: "#fff",
          minHeight: 480,
          boxShadow: OB.shadowLg,
          overflow: "hidden",
        }}
      >
        {html ? (
          <iframe
            title="Resume preview"
            srcDoc={html}
            sandbox=""
            style={{ width: "100%", height: 620, border: "none" }}
          />
        ) : (
          <div style={{ padding: "36px 40px" }}>
            <div style={{ height: 22, width: 180, borderRadius: 6, background: OB.primary100, marginBottom: 6 }} />
            <div style={{ height: 12, width: 260, borderRadius: 6, background: OB.primary50, marginBottom: 28 }} />
            {GHOST_SECTIONS.map((s) => (
              <div key={s} style={{ marginBottom: 26 }}>
                <div
                  style={{
                    fontFamily: OB.fontDisplay,
                    fontSize: 13,
                    letterSpacing: ".08em",
                    textTransform: "uppercase",
                    color: OB.ink4,
                    borderBottom: `1px solid ${OB.border}`,
                    paddingBottom: 5,
                    marginBottom: 12,
                  }}
                >
                  {s}
                </div>
                <div style={{ height: 10, width: "82%", borderRadius: 5, background: OB.primary50, marginBottom: 8 }} />
                <div style={{ height: 10, width: "64%", borderRadius: 5, background: OB.primary50 }} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
