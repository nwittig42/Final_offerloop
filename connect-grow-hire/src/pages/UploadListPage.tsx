import { useState, useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/AppSidebar";
import { AppHeader } from "@/components/AppHeader";
import { MainContentWrapper } from "@/components/MainContentWrapper";
import ContactImport from "@/components/ContactImport";
import { EliteGate } from "@/components/EliteGate";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Upload,
  Users,
  Building2,
  FileSpreadsheet,
  Link2,
  CheckCircle2,
  XCircle,
  Loader2,
  ArrowLeft,
} from "lucide-react";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { auth, db } from "@/lib/firebase";
import { doc, updateDoc } from "firebase/firestore";
import { BACKEND_URL } from "@/services/api";

// ---------------------------------------------------------------------------
// RocketReach-style multi-step "Upload a List" wizard.
//
// Step 1  — landing: pick data type (People / Companies — Companies disabled)
// Step 2  — name the list + choose CSV vs pasted LinkedIn URLs
// Step 3a — CSV branch: existing ContactImport component does the heavy lifting
// Step 3b — URL branch: paste LinkedIn URLs, imported sequentially
// Step 4  — done: summary + (optional) batch-apply list name as `group`
// ---------------------------------------------------------------------------

type WizardStep = 1 | 2 | "csv" | "url" | 4;
type ImportFormat = "csv" | "url";

const MAX_URLS = 25;
const LINKEDIN_MARKER = "linkedin.com/in/";
// Flat, unconditional charge per import-linkedin call. The backend deducts
// this on every lookup regardless of whether a contact/email is found — see
// backend/app/routes/linkedin_import.py:803-818 (`credits_to_deduct = 5`).
const CREDITS_PER_URL_LOOKUP = 5;

interface UrlImportOutcome {
  url: string;
  success: boolean;
  message: string;
  contactId?: string;
}

interface WizardSummary {
  imported: number;
  failed: number;
  creditsUsed?: number;
  creditsRemaining?: number;
  contactIds: string[];
}

const StepIndicator = ({ current }: { current: 1 | 2 | 3 }) => {
  const steps = [1, 2, 3];
  return (
    <div className="flex items-center justify-center gap-2 mb-8">
      {steps.map((s, idx) => (
        <div key={s} className="flex items-center gap-2">
          <div
            className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-semibold border transition-colors ${
              s === current
                ? "bg-[#0F172A] text-white border-[#0F172A]"
                : s < current
                ? "bg-[#3B82F6]/10 text-[#3B82F6] border-[#3B82F6]/30"
                : "bg-white text-gray-400 border-gray-200"
            }`}
          >
            {s < current ? <CheckCircle2 className="w-4 h-4" /> : s}
          </div>
          {idx < steps.length - 1 && (
            <div
              className={`w-10 h-px ${s < current ? "bg-[#3B82F6]/40" : "bg-gray-200"}`}
            />
          )}
        </div>
      ))}
    </div>
  );
};

const UploadListPage = () => {
  const navigate = useNavigate();
  const { user } = useFirebaseAuth();

  const [step, setStep] = useState<WizardStep>(1);
  const [dataType, setDataType] = useState<"people" | "companies">("people");
  const [listName, setListName] = useState("");
  const [format, setFormat] = useState<ImportFormat | null>(null);

  // URL-branch state
  const [urlText, setUrlText] = useState("");
  const [urlImporting, setUrlImporting] = useState(false);
  const [urlProgress, setUrlProgress] = useState<{ current: number; total: number } | null>(null);
  const [urlResults, setUrlResults] = useState<UrlImportOutcome[]>([]);
  const [urlStopError, setUrlStopError] = useState<string | null>(null);

  // Final summary shown on Step 4
  const [summary, setSummary] = useState<WizardSummary | null>(null);
  const [tagging, setTagging] = useState(false);

  const stepIndicatorValue: 1 | 2 | 3 =
    step === 1 ? 1 : step === 2 ? 2 : step === "csv" || step === "url" ? 2 : 3;

  const parsedUrls = useMemo(() => {
    const lines = urlText
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    const valid = lines.filter((l) => l.toLowerCase().includes(LINKEDIN_MARKER));
    return { lines, valid, capped: valid.slice(0, MAX_URLS) };
  }, [urlText]);

  const getIdToken = async (): Promise<string> => {
    const firebaseUser = auth.currentUser;
    if (!firebaseUser) throw new Error("Not authenticated");
    return await firebaseUser.getIdToken(true);
  };

  // Batch-apply the list name as `group` on every contact id we collected,
  // via direct client Firestore writes (users/{uid}/contacts/{id} is owner
  // read/write per firestore.rules, same as other in-app contact edits).
  // Note: `group` is written in camelCase to match every other contact field
  // in this schema (firstName, jobTitle, linkedinUrl, ...) — there is no
  // existing reader for it yet in My Network; it's forward-looking metadata
  // for finding this list later, per the future "find list" filter.
  const tagContactsWithGroup = useCallback(
    async (ids: string[], name: string) => {
      if (!user || !name.trim() || ids.length === 0) return;
      setTagging(true);
      try {
        await Promise.all(
          ids.map((id) =>
            updateDoc(doc(db, "users", user.uid, "contacts", id), {
              group: name.trim(),
            }).catch((err) => {
              console.error(`Failed to tag contact ${id} with group`, err);
            })
          )
        );
      } finally {
        setTagging(false);
      }
    },
    [user]
  );

  // ---------------- CSV branch completion ----------------
  const handleCsvComplete = useCallback(
    (result?: any) => {
      const contacts: Array<{ id?: string }> = Array.isArray(result?.contacts) ? result.contacts : [];
      const ids = contacts.map((c) => c.id).filter((id): id is string => !!id);

      setSummary({
        imported: result?.created ?? 0,
        failed: result?.skipped?.total ?? 0,
        creditsUsed: result?.credits?.spent,
        creditsRemaining: result?.credits?.remaining,
        contactIds: ids,
      });
      setStep(4);

      if (listName.trim() && ids.length > 0) {
        tagContactsWithGroup(ids, listName);
      }
    },
    [listName, tagContactsWithGroup]
  );

  // ---------------- URL branch import loop ----------------
  const runUrlImport = useCallback(async () => {
    const urls = parsedUrls.capped;
    if (urls.length === 0) return;

    setUrlImporting(true);
    setUrlStopError(null);
    setUrlResults([]);
    setUrlProgress({ current: 0, total: urls.length });

    const outcomes: UrlImportOutcome[] = [];
    let stoppedEarly = false;
    // Honest credit accounting: the backend deducts CREDITS_PER_URL_LOOKUP on
    // every call that imports a contact (even when no email is found) and only
    // those responses carry `credits_remaining` — so charged calls are exactly
    // the responses that include it. Early-return failures (invalid URL,
    // not-found 404, PDL quota 503) never reach the deduction.
    let chargedCalls = 0;
    let lastCreditsRemaining: number | undefined;

    try {
      // One token for the whole loop. This only holds because every call stays
      // on the fast path (create_draft: false, seconds per lookup); if the loop
      // ever grows slow per-call work, per-iteration token refresh is needed
      // (Firebase ID tokens expire after ~1 hour).
      const token = await getIdToken();

      for (let i = 0; i < urls.length; i++) {
        const url = urls[i];
        setUrlProgress({ current: i + 1, total: urls.length });

        try {
          const response = await fetch(`${BACKEND_URL}/api/contacts/import-linkedin`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${token}`,
            },
            body: JSON.stringify({
              linkedin_url: url,
              create_draft: false,
            }),
          });

          const data = await response.json().catch(() => ({}));

          if (typeof data?.credits_remaining === "number") {
            chargedCalls++;
            lastCreditsRemaining = data.credits_remaining;
          }

          if (response.status === 402 || data?.error_code === "PDL_QUOTA_EXCEEDED" || data?.error_code === "INSUFFICIENT_CREDITS") {
            outcomes.push({ url, success: false, message: data?.message || "Out of credits" });
            setUrlStopError(data?.message || "Ran out of credits — stopping import.");
            stoppedEarly = true;
            break;
          }

          if (!response.ok || data?.status !== "ok") {
            outcomes.push({ url, success: false, message: data?.message || data?.error || "Not found" });
            continue;
          }

          const fullName = data?.contact?.full_name || "Contact";
          outcomes.push({
            url,
            success: true,
            message: data?.message || `${fullName} found`,
            contactId: data?.contact_id,
          });
        } catch (err: any) {
          outcomes.push({ url, success: false, message: err?.message || "Import failed" });
        }

        // Keep the UI live as results come in.
        setUrlResults([...outcomes]);
      }
    } catch (err: any) {
      setUrlStopError(err?.message || "Failed to import — please sign in again and retry.");
    } finally {
      setUrlImporting(false);
    }

    const succeeded = outcomes.filter((o) => o.success);
    const ids = succeeded.map((o) => o.contactId).filter((id): id is string => !!id);

    setSummary({
      imported: succeeded.length,
      failed: outcomes.length - succeeded.length,
      creditsUsed: chargedCalls * CREDITS_PER_URL_LOOKUP,
      creditsRemaining: lastCreditsRemaining,
      contactIds: ids,
    });
    setStep(4);

    if (listName.trim() && ids.length > 0) {
      await tagContactsWithGroup(ids, listName);
    }

    void stoppedEarly;
  }, [parsedUrls.capped, listName, tagContactsWithGroup]);

  const resetWizard = () => {
    setStep(1);
    setDataType("people");
    setListName("");
    setFormat(null);
    setUrlText("");
    setUrlImporting(false);
    setUrlProgress(null);
    setUrlResults([]);
    setUrlStopError(null);
    setSummary(null);
  };

  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-paper font-sans text-ink">
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="Upload a List" />
          <EliteGate
            feature="Upload List"
            description="Upload a CSV or paste LinkedIn URLs and we'll find everyone's email and add them to My Network. Upgrade to Elite to unlock it."
          >
          <div className="flex-1 overflow-y-auto">
            <div className="max-w-[760px] mx-auto px-6 py-10">
              {step !== 1 && <StepIndicator current={stepIndicatorValue} />}

              {/* ---------------- STEP 1: LANDING ---------------- */}
              {step === 1 && (
                <div className="text-center max-w-[560px] mx-auto">
                  <div className="w-12 h-12 bg-white/70 backdrop-blur-sm rounded-[3px] flex items-center justify-center mx-auto mb-5 border border-black/[0.07] shadow-sm">
                    <Upload className="w-5 h-5 text-[#3B82F6]" />
                  </div>
                  <h1
                    className="text-[32px] font-normal text-gray-900 mb-3"
                    style={{ fontFamily: "'Instrument Serif', Georgia, serif", letterSpacing: "-0.025em", lineHeight: 1.1 }}
                  >
                    Upload a List
                  </h1>
                  <p className="text-gray-500 mb-10 max-w-md mx-auto">
                    Upload a list of contacts and we&apos;ll find their emails and add
                    them to My Network. Lists can then be used anywhere in Offerloop.
                  </p>

                  <p className="text-sm font-medium text-gray-700 mb-4">
                    What type of data do you want to upload?
                  </p>

                  <div className="inline-flex items-center bg-[#F1F5F9] rounded-full p-1 mb-2">
                    <button
                      type="button"
                      onClick={() => setDataType("people")}
                      className={`flex items-center gap-2 px-5 py-2.5 rounded-full text-sm font-medium transition-all ${
                        dataType === "people"
                          ? "bg-white text-[#0F172A] shadow-sm"
                          : "text-gray-500 hover:text-gray-700"
                      }`}
                    >
                      <Users className="w-4 h-4" />
                      People
                    </button>
                    <button
                      type="button"
                      disabled
                      title="Coming soon"
                      className="flex items-center gap-2 px-5 py-2.5 rounded-full text-sm font-medium text-gray-300 cursor-not-allowed"
                    >
                      <Building2 className="w-4 h-4" />
                      Companies
                    </button>
                  </div>
                  <p className="text-xs text-gray-400 mb-8">Companies — coming soon</p>

                  <Button
                    onClick={() => setStep(2)}
                    className="h-12 px-8 rounded-[3px] bg-[#0F172A] hover:bg-[#1E293B] text-white font-medium shadow-md hover:shadow-lg transition-all"
                  >
                    Begin Uploading
                  </Button>
                </div>
              )}

              {/* ---------------- STEP 2: NAME + FORMAT ---------------- */}
              {step === 2 && (
                <div className="max-w-[560px] mx-auto">
                  <h2
                    className="text-2xl font-normal text-gray-900 mb-1"
                    style={{ fontFamily: "'Instrument Serif', Georgia, serif" }}
                  >
                    Name your list
                  </h2>
                  <p className="text-sm text-gray-500 mb-3">
                    Find this list later in My Network
                  </p>
                  <Input
                    value={listName}
                    onChange={(e) => setListName(e.target.value)}
                    placeholder="e.g. Goldman Sachs alumni (optional)"
                    className="mb-10 rounded-[3px] border-gray-300 h-11"
                  />

                  <h2
                    className="text-2xl font-normal text-gray-900 mb-4"
                    style={{ fontFamily: "'Instrument Serif', Georgia, serif" }}
                  >
                    What format is your contact list in?
                  </h2>

                  <div className="space-y-3 mb-8">
                    <button
                      type="button"
                      onClick={() => setFormat("csv")}
                      className={`w-full text-left rounded-[3px] border p-5 flex items-start gap-4 transition-all ${
                        format === "csv"
                          ? "border-[#0F172A] bg-[#0F172A] text-white shadow-md"
                          : "border-gray-200 bg-white hover:border-[#3B82F6]/50 hover:bg-[#FAFBFF]"
                      }`}
                    >
                      <FileSpreadsheet
                        className={`w-5 h-5 mt-0.5 flex-shrink-0 ${format === "csv" ? "text-white" : "text-[#3B82F6]"}`}
                      />
                      <div>
                        <p className={`font-medium mb-1 ${format === "csv" ? "text-white" : "text-gray-900"}`}>
                          I have a CSV to upload
                        </p>
                        <p className={`text-sm ${format === "csv" ? "text-white/70" : "text-gray-500"}`}>
                          Upload your own list of contacts and we&apos;ll match each
                          person with verified info.
                        </p>
                      </div>
                    </button>

                    <button
                      type="button"
                      onClick={() => setFormat("url")}
                      className={`w-full text-left rounded-[3px] border p-5 flex items-start gap-4 transition-all ${
                        format === "url"
                          ? "border-[#0F172A] bg-[#0F172A] text-white shadow-md"
                          : "border-gray-200 bg-white hover:border-[#3B82F6]/50 hover:bg-[#FAFBFF]"
                      }`}
                    >
                      <Link2
                        className={`w-5 h-5 mt-0.5 flex-shrink-0 ${format === "url" ? "text-white" : "text-[#3B82F6]"}`}
                      />
                      <div>
                        <p className={`font-medium mb-1 ${format === "url" ? "text-white" : "text-gray-900"}`}>
                          I&apos;d like to paste a list of LinkedIn URLs
                        </p>
                        <p className={`text-sm ${format === "url" ? "text-white/70" : "text-gray-500"}`}>
                          Give us LinkedIn profile URLs and we&apos;ll find accurate
                          info for each.
                        </p>
                      </div>
                    </button>
                  </div>

                  {/* Truthful per-branch pricing note: CSV only charges for
                      created contacts; the URL branch charges per profile
                      lookup that imports a contact (flat 5 credits each). */}
                  <p className="text-xs text-gray-400 text-center mb-6">
                    {format === "url"
                      ? `Each profile lookup uses ${CREDITS_PER_URL_LOOKUP} credits.`
                      : "You're only charged for contacts we actually find."}
                  </p>

                  <div className="flex items-center justify-between">
                    <Button
                      variant="outline"
                      onClick={() => setStep(1)}
                      className="rounded-[3px] border-gray-300"
                    >
                      Previous
                    </Button>
                    <Button
                      disabled={!format}
                      onClick={() => setStep(format === "csv" ? "csv" : "url")}
                      className="h-11 px-8 rounded-[3px] bg-[#0F172A] hover:bg-[#1E293B] text-white font-medium disabled:opacity-40"
                    >
                      Next
                    </Button>
                  </div>
                </div>
              )}

              {/* ---------------- STEP 3a: CSV ---------------- */}
              {step === "csv" && (
                <div>
                  <div className="mb-4">
                    <button
                      type="button"
                      onClick={() => setStep(2)}
                      className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
                    >
                      <ArrowLeft className="w-3.5 h-3.5" />
                      Previous
                    </button>
                  </div>
                  <ContactImport onImportComplete={handleCsvComplete} />
                </div>
              )}

              {/* ---------------- STEP 3b: PASTE LINKEDIN URLS ---------------- */}
              {step === "url" && (
                <div className="max-w-[600px] mx-auto">
                  <div className="mb-4">
                    <button
                      type="button"
                      onClick={() => !urlImporting && setStep(2)}
                      disabled={urlImporting}
                      className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700 disabled:opacity-40"
                    >
                      <ArrowLeft className="w-3.5 h-3.5" />
                      Previous
                    </button>
                  </div>

                  <h2
                    className="text-2xl font-normal text-gray-900 mb-2"
                    style={{ fontFamily: "'Instrument Serif', Georgia, serif" }}
                  >
                    Paste a list of LinkedIn profiles
                  </h2>
                  <p className="text-sm text-gray-500 mb-4">
                    One URL per line. We&apos;ll look each one up and find verified
                    contact info.
                  </p>

                  <textarea
                    value={urlText}
                    onChange={(e) => setUrlText(e.target.value)}
                    disabled={urlImporting}
                    rows={8}
                    placeholder={
                      "https://www.linkedin.com/in/jane-doe\nhttps://www.linkedin.com/in/john-smith"
                    }
                    className="w-full rounded-[3px] border border-gray-300 p-4 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#3B82F6]/30 focus:border-[#3B82F6] disabled:bg-gray-50 disabled:text-gray-400"
                  />

                  <div className="flex items-center justify-between mt-2 mb-6 text-xs text-gray-500">
                    <span>
                      {parsedUrls.valid.length} profile URL{parsedUrls.valid.length === 1 ? "" : "s"}
                    </span>
                    {parsedUrls.valid.length > MAX_URLS && (
                      <span className="text-amber-600 font-medium">
                        Only the first {MAX_URLS} will be imported
                      </span>
                    )}
                  </div>

                  {!urlImporting && urlResults.length === 0 && (
                    <div className="flex items-center justify-between">
                      <Button
                        variant="outline"
                        onClick={() => setStep(2)}
                        className="rounded-[3px] border-gray-300"
                      >
                        Previous
                      </Button>
                      <Button
                        disabled={parsedUrls.valid.length === 0}
                        onClick={runUrlImport}
                        className="h-11 px-8 rounded-[3px] bg-[#0F172A] hover:bg-[#1E293B] text-white font-medium disabled:opacity-40"
                      >
                        Import {Math.min(parsedUrls.valid.length, MAX_URLS) || ""} contacts
                      </Button>
                    </div>
                  )}

                  {(urlImporting || urlResults.length > 0) && (
                    <div className="mt-2">
                      {urlProgress && urlImporting && (
                        <p className="text-sm text-gray-600 mb-4 flex items-center gap-2">
                          <Loader2 className="w-4 h-4 animate-spin text-[#3B82F6]" />
                          Importing {urlProgress.current} of {urlProgress.total}…
                        </p>
                      )}

                      {urlStopError && (
                        <div className="mb-4 p-3 bg-red-50 text-red-600 text-sm rounded-[3px]">
                          {urlStopError}
                        </div>
                      )}

                      <div className="border border-gray-200 rounded-[3px] divide-y divide-gray-100 max-h-80 overflow-y-auto">
                        {urlResults.map((r, idx) => (
                          <div key={idx} className="flex items-start gap-3 px-4 py-3 text-sm">
                            {r.success ? (
                              <CheckCircle2 className="w-4 h-4 text-green-600 mt-0.5 flex-shrink-0" />
                            ) : (
                              <XCircle className="w-4 h-4 text-red-500 mt-0.5 flex-shrink-0" />
                            )}
                            <div className="min-w-0">
                              <p className="text-gray-700 truncate">{r.url}</p>
                              <p className={r.success ? "text-gray-500" : "text-red-500"}>{r.message}</p>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* ---------------- STEP 4: DONE ---------------- */}
              {step === 4 && summary && (
                <div className="max-w-[520px] mx-auto text-center">
                  <div className="inline-flex items-center gap-2 bg-[#F0FDF4] rounded-full px-4 py-1.5 mb-5">
                    <CheckCircle2 className="w-3.5 h-3.5 text-green-700" />
                    <span className="text-sm font-medium text-green-700">Upload complete</span>
                  </div>

                  <div className="text-[56px] font-bold text-gray-900 tracking-tight leading-none mb-2">
                    {summary.imported}
                  </div>
                  <p className="text-gray-500 mb-8">contacts added to My Network</p>

                  <div className="border-t border-gray-100 divide-y divide-gray-100 text-left mb-2">
                    <div className="flex justify-between items-center py-3">
                      <span className="text-sm text-gray-500">Imported</span>
                      <span className="text-sm font-semibold text-[#3B82F6]">{summary.imported}</span>
                    </div>
                    {summary.failed > 0 && (
                      <div className="flex justify-between items-center py-3">
                        <span className="text-sm text-gray-500">Not found / skipped</span>
                        <span className="text-sm font-semibold text-gray-400">{summary.failed}</span>
                      </div>
                    )}
                    {summary.creditsUsed !== undefined && (
                      <div className="flex justify-between items-center py-3">
                        <span className="text-sm text-gray-500">Credits used</span>
                        <span className="text-sm font-semibold text-gray-900">
                          {summary.creditsUsed}
                          {summary.creditsRemaining !== undefined && (
                            <span className="text-gray-400 font-normal"> · {summary.creditsRemaining} remaining</span>
                          )}
                        </span>
                      </div>
                    )}
                    {listName.trim() && (
                      <div className="flex justify-between items-center py-3">
                        <span className="text-sm text-gray-500">List name</span>
                        <span className="text-sm font-semibold text-gray-900">
                          {tagging ? "Tagging…" : listName}
                        </span>
                      </div>
                    )}
                  </div>

                  <div className="flex gap-3 pt-6">
                    <Button
                      variant="outline"
                      onClick={resetWizard}
                      className="flex-1 h-11 rounded-[3px] border-gray-300"
                    >
                      Upload another list
                    </Button>
                    <Button
                      onClick={() => navigate("/contacts")}
                      className="flex-1 h-11 rounded-[3px] bg-[#0F172A] hover:bg-[#1E293B] text-white"
                    >
                      View in My Network
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </div>
          </EliteGate>
        </MainContentWrapper>
      </div>
    </SidebarProvider>
  );
};

export default UploadListPage;
