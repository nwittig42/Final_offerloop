import { useState, useRef } from "react";
import { Upload, FileText, Loader2, Check } from "lucide-react";
import { ACCEPTED_RESUME_TYPES, isValidResumeFile } from "@/utils/resumeFileTypes";
import { enrichLinkedInOnboarding, resumeFromLinkedIn, BACKEND_URL } from "@/services/api";
import { auth } from "@/lib/firebase";
import {
  ResumePrefill,
  resumePrefillFromParse,
  prefillFromLinkedin,
  resolvePrefill,
  EMPTY_PREFILL,
} from "@/utils/onboardingPrefill";
import { OB, obFieldLabel, obInput, obPrimaryButton, obFocus } from "./onboardingTheme";

export type EntryPath = "resume" | "linkedin";

export interface SourceResult {
  resumePrefill: ResumePrefill | null;
  linkedinPrefill: ResumePrefill | null;
  resolved: ResumePrefill;
  resumeFile: File | null;
  linkedinUrl: string;
  entryPath: EntryPath;
}

interface OnboardingSourceProps {
  onNext: (data: SourceResult) => void;
  onBuild: () => void;
  initialLinkedinUrl?: string;
  submitting?: boolean;
  // Onboarding dry run: parse without saving the resume to the account.
  preview?: boolean;
}

const LINKEDIN_RE = /linkedin\.com\/in\//i;

export const OnboardingSource = ({ onNext, onBuild, initialLinkedinUrl, submitting, preview = false }: OnboardingSourceProps) => {
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [resumePrefill, setResumePrefill] = useState<ResumePrefill | null>(null);
  const [resumeParsing, setResumeParsing] = useState(false);
  const [linkedinUrl, setLinkedinUrl] = useState(initialLinkedinUrl || "");
  const [internalSubmitting, setInternalSubmitting] = useState(false);
  // Phased status for the LinkedIn path: enrich, then best-effort resume build.
  const [phase, setPhase] = useState<"" | "enriching" | "writing">("");
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const linkedinValid = LINKEDIN_RE.test(linkedinUrl);
  // Step 2 gate: any ONE of {resume parsed, valid LinkedIn URL, manual choice}.
  // Manual has its own link, so the Continue gate is resume OR LinkedIn.
  const canContinue = !!resumePrefill || linkedinValid;

  const ingestFile = async (file: File) => {
    if (!isValidResumeFile(file)) {
      setError("Please upload a PDF, DOCX, or DOC file.");
      return;
    }
    setError("");
    setResumeFile(file);
    setResumeParsing(true);
    try {
      const formData = new FormData();
      formData.append("resume", file);
      // Signed in, parse-resume also saves the file as their resume; the
      // preview sends it anonymously so it's only parsed.
      const token = auth.currentUser && !preview ? await auth.currentUser.getIdToken() : null;
      const res = await fetch(`${BACKEND_URL}/api/parse-resume`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      });
      const result = await res.json();
      if (res.ok && result.data) {
        setResumePrefill(resumePrefillFromParse(result.data));
      } else {
        // Keep the file (we still attach it later); user can confirm manually.
        setResumePrefill(EMPTY_PREFILL);
      }
    } catch {
      setResumePrefill(EMPTY_PREFILL);
    } finally {
      setResumeParsing(false);
    }
  };

  const handleResumeUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void ingestFile(file);
  };

  const proceed = (entryPath: EntryPath, linkedinPrefill: ResumePrefill | null) => {
    const resolved = resolvePrefill(resumePrefill, linkedinPrefill);
    onNext({
      resumePrefill,
      linkedinPrefill,
      resolved,
      resumeFile,
      linkedinUrl: linkedinValid ? linkedinUrl.trim() : "",
      entryPath,
    });
  };

  const handleContinue = async () => {
    if (internalSubmitting || submitting) return;
    setError("");
    // Resume is primary when present.
    if (resumePrefill) {
      // If a LinkedIn URL was also supplied, enrich it to fill resume gaps.
      if (linkedinValid) {
        setInternalSubmitting(true);
        setPhase("enriching");
        try {
          const result = await enrichLinkedInOnboarding(linkedinUrl.trim(), preview);
          proceed("resume", result ? prefillFromLinkedin(result) : null);
          return;
        } catch {
          proceed("resume", null);
          return;
        } finally {
          setInternalSubmitting(false);
          setPhase("");
        }
      }
      proceed("resume", null);
      return;
    }
    // LinkedIn-only path: enrich, then build them a Harvard one-pager from the
    // enriched profile. The build is best-effort and never blocks onboarding;
    // failures (including the generation cap) are ignored.
    if (linkedinValid) {
      setInternalSubmitting(true);
      setPhase("enriching");
      let linkedinPrefill: ResumePrefill = EMPTY_PREFILL;
      try {
        const result = await enrichLinkedInOnboarding(linkedinUrl.trim(), preview);
        if (result) linkedinPrefill = prefillFromLinkedin(result);
        setPhase("writing");
        try {
          if (!preview) await resumeFromLinkedIn();
        } catch {
          /* best-effort */
        }
      } catch {
        /* enrichment failed; proceed with empty prefill */
      }
      setInternalSubmitting(false);
      setPhase("");
      proceed("linkedin", linkedinPrefill);
    }
  };

  return (
    <div>
      {/* Resume dropzone */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => fileInputRef.current?.click()}
        onKeyDown={(e) => e.key === "Enter" && fileInputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          const file = e.dataTransfer.files?.[0];
          if (file) void ingestFile(file);
        }}
        style={{
          border: `1.5px dashed ${dragOver || resumeFile ? OB.primary : OB.primary200}`,
          borderRadius: 14,
          padding: 26,
          textAlign: "center",
          marginBottom: 14,
          background: OB.primary50,
          cursor: "pointer",
          transition: "border-color .2s",
        }}
      >
        <span
          style={{
            display: "inline-flex",
            width: 46,
            height: 46,
            borderRadius: 12,
            background: "#fff",
            color: OB.primary,
            alignItems: "center",
            justifyContent: "center",
            marginBottom: 12,
          }}
        >
          {resumeParsing ? (
            <Loader2 size={22} className="animate-spin" />
          ) : resumeFile ? (
            <FileText size={22} strokeWidth={1.6} />
          ) : (
            <Upload size={22} strokeWidth={1.6} />
          )}
        </span>
        {resumeFile ? (
          <>
            <div style={{ fontWeight: 600, fontSize: 15, color: OB.heading }}>{resumeFile.name}</div>
            <div style={{ fontSize: 13, color: OB.ink3, marginTop: 2, display: "flex", alignItems: "center", justifyContent: "center", gap: 5 }}>
              {resumeParsing ? (
                "Reading your resume…"
              ) : (
                <>
                  {resumePrefill?.name ? (
                    <>
                      <Check size={13} strokeWidth={3} style={{ color: OB.primary }} /> Parsed. Click to
                      replace.
                    </>
                  ) : (
                    "Attached. Click to replace."
                  )}
                </>
              )}
            </div>
          </>
        ) : (
          <>
            <div style={{ fontWeight: 600, fontSize: 15, color: OB.heading }}>
              Drop your resume or <span style={{ color: OB.primary }}>browse</span>
            </div>
            <div style={{ fontSize: 13, color: OB.ink3, marginTop: 2 }}>PDF, DOCX or DOC</div>
          </>
        )}
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPTED_RESUME_TYPES.accept}
          className="hidden"
          onChange={handleResumeUpload}
        />
      </div>

      {/* LinkedIn URL */}
      <div style={{ marginBottom: 24 }}>
        <label style={{ ...obFieldLabel, display: "flex", alignItems: "center", gap: 6 }} htmlFor="ob-linkedin">
          LinkedIn URL <span style={{ color: OB.ink4, fontWeight: 400 }}>(optional)</span>
        </label>
        <input
          id="ob-linkedin"
          type="url"
          style={obInput}
          {...obFocus}
          placeholder="linkedin.com/in/your-handle"
          value={linkedinUrl}
          onChange={(e) => setLinkedinUrl(e.target.value)}
        />
        {linkedinUrl && !linkedinValid && (
          <p style={{ fontSize: 12, color: "#DC2626", margin: "6px 0 0" }}>
            Enter a full linkedin.com/in/… profile URL.
          </p>
        )}
      </div>

      {error && <p style={{ fontSize: 12, color: "#DC2626", margin: "0 0 12px" }}>{error}</p>}

      <button
        type="button"
        onClick={handleContinue}
        disabled={!canContinue || internalSubmitting || submitting}
        style={{
          ...obPrimaryButton,
          marginBottom: 14,
          opacity: canContinue && !internalSubmitting && !submitting ? 1 : 0.5,
          cursor: canContinue && !internalSubmitting && !submitting ? "pointer" : "default",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
        onMouseEnter={(e) =>
          canContinue && !internalSubmitting && !submitting && (e.currentTarget.style.background = OB.primaryDark)
        }
        onMouseLeave={(e) => (e.currentTarget.style.background = OB.primary)}
      >
        {internalSubmitting || submitting ? (
          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Loader2 size={17} className="animate-spin" />
            {phase === "enriching"
              ? "Reading your LinkedIn…"
              : phase === "writing"
                ? "Writing your resume…"
                : "Finishing up…"}
          </span>
        ) : (
          "Continue"
        )}
      </button>
      <div style={{ textAlign: "center" }}>
        <button
          type="button"
          onClick={onBuild}
          style={{
            fontWeight: 600,
            fontSize: 14,
            color: OB.primary,
            background: "none",
            border: "none",
            cursor: "pointer",
            fontFamily: OB.fontBody,
          }}
        >
          No resume? We'll build you one
        </button>
      </div>
    </div>
  );
};
