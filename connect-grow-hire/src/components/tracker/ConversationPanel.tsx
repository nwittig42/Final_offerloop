import { ExternalLink, Linkedin, Copy, Check, FileText, Loader2 } from "lucide-react";
import { useState } from "react";
import type { OutboxThread, PipelineStage, AutoPrepStatus } from "@/services/api";
import { ActionBar } from "./ActionBar";
import { formatTimeAgo, formatDate, decodeHtmlEntities } from "@/lib/formatters";

const STAGE_OPTIONS: { value: PipelineStage; label: string }[] = [
  { value: "new", label: "New" },
  { value: "draft_created", label: "Draft Created" },
  { value: "draft_deleted", label: "Draft Deleted" },
  { value: "email_sent", label: "Email Sent" },
  { value: "waiting_on_reply", label: "Waiting on Reply" },
  { value: "replied", label: "They Replied" },
  { value: "meeting_scheduled", label: "Meeting Scheduled" },
  { value: "connected", label: "Connected" },
  { value: "no_response", label: "No Response" },
  { value: "bounced", label: "Bounced" },
  { value: "closed", label: "Closed" },
];

// --- component ---

interface ConversationPanelProps {
  contact: OutboxThread;
  onStageChange: (contactId: string, stage: string) => void;
  onArchive: (contactId: string) => void;
  onUnarchive: (contactId: string) => void;
  onSnooze: (contactId: string, until: string) => void;
  onMarkWon: (contactId: string) => void;
  onMarkRead: (contactId: string) => void;
  onRefresh: (contactId: string) => void;
  isSyncing: boolean;
  isMutating?: boolean;
  autoPrep?: AutoPrepStatus | null;
  onViewAutoPrep?: (prepId: string) => void;
}

export function ConversationPanel({
  contact,
  onStageChange,
  onArchive,
  onUnarchive,
  onSnooze,
  onMarkWon,
  onMarkRead,
  onRefresh,
  isSyncing,
  isMutating,
  autoPrep,
  onViewAutoPrep,
}: ConversationPanelProps) {
  const [emailCopied, setEmailCopied] = useState(false);

  const copyEmail = async () => {
    try {
      await navigator.clipboard.writeText(contact.email);
      setEmailCopied(true);
      setTimeout(() => setEmailCopied(false), 2000);
    } catch {
      // Clipboard permission denied or not available
    }
  };

  const gmailThreadUrl = contact.gmailThreadId
    ? `https://mail.google.com/mail/u/0/#inbox/${contact.gmailThreadId}`
    : null;

  const followUpOverdue =
    contact.nextFollowUpAt && new Date(contact.nextFollowUpAt) <= new Date();

  return (
    <div className="h-full overflow-y-auto">
      <div className="p-5 space-y-5">
        {/* 1. Contact header */}
        <div>
          <h2 className="text-lg font-bold text-gray-900">{contact.name || contact.email}</h2>
          {(contact.title || contact.company) && (
            <p className="text-sm text-gray-500 mt-0.5">
              {[contact.title, contact.company].filter(Boolean).join(" at ")}
            </p>
          )}

          <div className="flex items-center gap-3 mt-2 flex-wrap">
            {/* email */}
            <button
              onClick={copyEmail}
              className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 transition-colors"
            >
              {emailCopied ? <Check className="w-3 h-3 text-green-500" /> : <Copy className="w-3 h-3" />}
              {contact.email}
            </button>

            {/* linkedin */}
            {contact.linkedinUrl && (
              <a
                href={contact.linkedinUrl.startsWith('http') ? contact.linkedinUrl : `https://${contact.linkedinUrl}`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-[#3B82F6] hover:text-[#2563EB]"
              >
                <Linkedin className="w-3 h-3" />
                LinkedIn
              </a>
            )}

            {/* gmail thread */}
            {gmailThreadUrl && (
              <a
                href={gmailThreadUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700"
              >
                <ExternalLink className="w-3 h-3" />
                View in Gmail
              </a>
            )}
          </div>
        </div>

        {/* 1b. Sent timestamp */}
        {contact.emailSentAt && (
          <p className="text-xs text-gray-500">
            Sent on {formatDate(contact.emailSentAt)}
            {contact.emailOpenedAt && ` · Opened ${formatDate(contact.emailOpenedAt)}`}
          </p>
        )}

        {/* 2. AI Summary */}
        {contact.conversationSummary && (
          <div className="bg-gray-50 border border-gray-100 rounded-[3px] p-3">
            <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1">
              AI Summary
            </p>
            <p className="text-sm text-gray-700 leading-relaxed">{decodeHtmlEntities(contact.conversationSummary)}</p>
          </div>
        )}

        {/* 3. Message preview */}
        {contact.lastMessageSnippet && (
          <div>
            <div className="flex items-center gap-2 mb-1.5">
              <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">
                Last message
              </p>
              <span className="text-[10px] text-gray-400">
                {formatTimeAgo(contact.lastActivityAt)}
              </span>
              {contact.lastMessageFrom === "contact" && (
                <span className="text-[10px] font-medium text-[#3B82F6]">They replied</span>
              )}
              {contact.lastMessageFrom === "user" && (
                <span className="text-[10px] font-medium text-gray-500">You sent</span>
              )}
            </div>
            <blockquote className="border-l-2 border-gray-200 pl-3 py-1 text-sm text-gray-600 italic">
              {decodeHtmlEntities(contact.lastMessageSnippet)}
            </blockquote>
          </div>
        )}

        {/* 4. Follow-up info */}
        {contact.nextFollowUpAt && (
          <div className={`text-sm ${followUpOverdue ? "text-red-600" : "text-gray-600"}`}>
            {followUpOverdue
              ? `Follow-up overdue since ${formatDate(contact.nextFollowUpAt)}`
              : `Follow-up scheduled for ${formatDate(contact.nextFollowUpAt)}`}
            {contact.followUpCount > 0 && (
              <span className="text-gray-400 ml-2">
                ({contact.followUpCount} follow-up{contact.followUpCount !== 1 ? "s" : ""} sent)
              </span>
            )}
          </div>
        )}

        {/* 4a. Auto-Prep card */}
        {contact.pipelineStage === "meeting_scheduled" && autoPrep && (
          <div className="bg-green-50 border border-green-200/60 rounded-[3px] p-3">
            <p className="text-[10px] font-semibold text-green-600 uppercase tracking-wide mb-1">
              Coffee Chat Prep
            </p>
            {autoPrep.status === "generating" ? (
              <div className="flex items-center gap-2 text-sm text-green-700">
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Preparing your coffee chat brief...
              </div>
            ) : autoPrep.status === "ready" && autoPrep.prepId ? (
              <button
                onClick={() => onViewAutoPrep?.(autoPrep.prepId!)}
                className="inline-flex items-center gap-1.5 text-sm font-medium text-green-700 hover:text-green-800"
              >
                <FileText className="w-3.5 h-3.5" />
                View coffee chat prep
              </button>
            ) : null}
          </div>
        )}

        {/* 4b. Resolution */}
        {contact.resolution && (
          <div className="text-sm text-gray-600">
            <span className="font-medium">Resolution:</span>{" "}
            {contact.resolution.replace(/_/g, " ")}
            {contact.resolutionDetails && (
              <span className="text-gray-400 ml-1">— {contact.resolutionDetails}</span>
            )}
          </div>
        )}

        {/* Sync error */}
        {contact.lastSyncError && (
          <div className="text-xs text-red-500 bg-red-50 border border-red-100 rounded-[3px] px-3 py-2">
            {contact.lastSyncError.message}
          </div>
        )}

        {/* 5. Action bar */}
        <ActionBar
          contact={contact}
          onMarkRead={() => onMarkRead(contact.id)}
          onMarkWon={() => onMarkWon(contact.id)}
          onArchive={() => onArchive(contact.id)}
          onUnarchive={() => onUnarchive(contact.id)}
          onSnooze={(until) => onSnooze(contact.id, until)}
          onRefresh={() => onRefresh(contact.id)}
          isSyncing={isSyncing}
        />

        {/* 6. Stage selector */}
        <div>
          <label className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide block mb-1">
            Pipeline Stage
          </label>
          <select
            value={contact.pipelineStage || ""}
            onChange={(e) => {
              if (e.target.value) onStageChange(contact.id, e.target.value);
            }}
            disabled={isMutating}
            className="w-full max-w-[220px] text-sm bg-white border border-gray-200 rounded-[3px] px-2.5 py-1.5 text-gray-700 focus:outline-none focus:ring-2 focus:ring-[#3B82F6]/20 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <option value="" disabled>Select stage...</option>
            {STAGE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}
