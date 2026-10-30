import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { X, Plus } from "lucide-react";
import { UNIVERSITIES } from "@/data/universities";
import type { DraftField, ProfileDraft } from "@/services/onboardingProgress";
import { mergeEdited } from "@/utils/onboardingPreferences";
import { OB, obFieldLabel, obInput, obPrimaryButton, obFocus } from "./onboardingTheme";

// Quick confirm/correct of everything onboarding inferred from the resume or
// LinkedIn. Only the school is required (or "not in school"), so a student
// with a perfect parse can confirm in one click.

type ListKey = "roles" | "industries" | "targetCompanies" | "interests";

const LISTS: { key: ListKey; label: string; placeholder: string; hint: string }[] = [
  { key: "roles", label: "Roles you're aiming for", placeholder: "Add a role", hint: "Shapes who we suggest and how emails are framed." },
  { key: "industries", label: "Industries", placeholder: "Add an industry", hint: "" },
  { key: "targetCompanies", label: "Target companies", placeholder: "Add a company", hint: "We'll find people at these firms first." },
  { key: "interests", label: "Activities and interests", placeholder: "Add an activity", hint: "Shared interests make better openers." },
];

interface ChipListProps {
  label: string;
  hint: string;
  placeholder: string;
  values: string[];
  onChange: (values: string[]) => void;
}

const ChipList = ({ label, hint, placeholder, values, onChange }: ChipListProps) => {
  const [text, setText] = useState("");
  const inputId = useId();
  const add = () => {
    const v = text.trim();
    if (v && !values.some((x) => x.toLowerCase() === v.toLowerCase())) onChange([...values, v]);
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    }
  };
  return (
    <div style={{ marginBottom: 18 }}>
      <label htmlFor={inputId} style={obFieldLabel}>
        {label}
      </label>
      {hint && <p style={{ fontSize: 12.5, color: OB.ink3, margin: "-4px 0 8px" }}>{hint}</p>}
      {values.length > 0 && (
        <ul aria-label={`Selected: ${label}`} style={{ listStyle: "none", padding: 0, margin: "0 0 8px", display: "flex", flexWrap: "wrap", gap: 6 }}>
          {values.map((v) => (
            <li
              key={v}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                background: OB.primary50,
                border: `1px solid ${OB.primary100}`,
                color: OB.heading,
                borderRadius: 999,
                padding: "4px 6px 4px 11px",
                fontSize: 13.5,
              }}
            >
              {v}
              <button
                type="button"
                aria-label={`Remove ${v}`}
                onClick={() => onChange(values.filter((x) => x !== v))}
                style={{ background: "none", border: "none", cursor: "pointer", color: OB.ink3, display: "flex", padding: 2 }}
              >
                <X size={13} strokeWidth={2.4} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div style={{ display: "flex", gap: 8 }}>
        <input
          id={inputId}
          style={{ ...obInput, height: 42 }}
          {...obFocus}
          placeholder={placeholder}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
        />
        <button
          type="button"
          onClick={add}
          aria-label={`Add to ${label}`}
          disabled={!text.trim()}
          style={{
            flexShrink: 0,
            width: 42,
            height: 42,
            borderRadius: 10,
            border: `1px solid ${OB.primary200}`,
            background: "#fff",
            color: OB.primary,
            cursor: text.trim() ? "pointer" : "default",
            opacity: text.trim() ? 1 : 0.5,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Plus size={17} />
        </button>
      </div>
    </div>
  );
};

interface OnboardingConfirmProps {
  initial: ProfileDraft;
  onConfirm: (draft: ProfileDraft) => void;
  submitting?: boolean;
  /** True when the resume couldn't be read, so fields start empty. */
  sparse?: boolean;
  /** Every edit, so the flow can save it and a refresh keeps it. */
  onChange?: (draft: ProfileDraft) => void;
}

export const OnboardingConfirm = ({ initial, onConfirm, submitting, sparse, onChange }: OnboardingConfirmProps) => {
  const [draft, setDraft] = useState<ProfileDraft>(initial);
  const [touched, setTouched] = useState(false);
  // Anything the user changes is theirs: marked in `edited` so re-running
  // inference later never replaces it.
  const set = <K extends DraftField>(key: K, value: ProfileDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value, edited: mergeEdited(d.edited, [key]) }));

  const mounted = useRef(false);
  useEffect(() => {
    // Skip the initial render: only report real edits.
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    onChange?.(draft);
  }, [draft, onChange]);

  const schoolMissing = !draft.notInSchool && !draft.university.trim();
  const yearInvalid = !!draft.graduationYear && !/^(19|20)\d{2}$/.test(draft.graduationYear.trim());
  const canContinue = !schoolMissing && !yearInvalid && !submitting;

  const submit = () => {
    setTouched(true);
    if (!canContinue) return;
    onConfirm({
      ...draft,
      firstName: draft.firstName.trim(),
      lastName: draft.lastName.trim(),
      university: draft.notInSchool ? "" : draft.university.trim(),
      major: draft.major.trim(),
      graduationYear: draft.graduationYear.trim(),
    });
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      noValidate
    >
      {sparse && (
        <p
          role="status"
          style={{ fontSize: 13.5, color: OB.ink2, background: OB.primary50, borderRadius: 10, padding: "10px 14px", margin: "0 0 18px" }}
        >
          We couldn't read much from that, so fill in what you can. You can change any of this later in Settings.
        </p>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 18 }}>
        <div>
          <label htmlFor="ob-first" style={obFieldLabel}>First name</label>
          <input id="ob-first" style={obInput} {...obFocus} value={draft.firstName} onChange={(e) => set("firstName", e.target.value)} />
        </div>
        <div>
          <label htmlFor="ob-last" style={obFieldLabel}>Last name</label>
          <input id="ob-last" style={obInput} {...obFocus} value={draft.lastName} onChange={(e) => set("lastName", e.target.value)} />
        </div>
      </div>

      <div style={{ marginBottom: 8 }}>
        <label htmlFor="ob-school" style={obFieldLabel}>School</label>
        <input
          id="ob-school"
          list="ob-school-options"
          style={{ ...obInput, opacity: draft.notInSchool ? 0.5 : 1 }}
          {...obFocus}
          disabled={draft.notInSchool}
          placeholder="Start typing your university"
          value={draft.university}
          aria-invalid={touched && schoolMissing}
          aria-describedby={touched && schoolMissing ? "ob-school-error" : undefined}
          onChange={(e) => set("university", e.target.value)}
        />
        <datalist id="ob-school-options">
          {UNIVERSITIES.map((u) => (
            <option key={u} value={u} />
          ))}
        </datalist>
        {touched && schoolMissing && (
          <p id="ob-school-error" style={{ fontSize: 12, color: "#DC2626", margin: "6px 0 0" }}>
            Add your school, or tick "I'm not in school".
          </p>
        )}
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13.5, color: OB.ink2, marginBottom: 18, cursor: "pointer" }}>
        <input type="checkbox" checked={draft.notInSchool} onChange={(e) => set("notInSchool", e.target.checked)} />
        I'm not in school, or mine isn't listed
      </label>

      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12, marginBottom: 22 }}>
        <div>
          <label htmlFor="ob-major" style={obFieldLabel}>Major</label>
          <input id="ob-major" style={obInput} {...obFocus} value={draft.major} onChange={(e) => set("major", e.target.value)} />
        </div>
        <div>
          <label htmlFor="ob-year" style={obFieldLabel}>Grad year</label>
          <input
            id="ob-year"
            inputMode="numeric"
            maxLength={4}
            style={obInput}
            {...obFocus}
            placeholder="2027"
            value={draft.graduationYear}
            aria-invalid={yearInvalid}
            onChange={(e) => set("graduationYear", e.target.value.replace(/\D/g, ""))}
          />
          {yearInvalid && (
            <p style={{ fontSize: 12, color: "#DC2626", margin: "6px 0 0" }}>Use a 4-digit year.</p>
          )}
        </div>
      </div>

      {LISTS.map((l) => (
        <ChipList
          key={l.key}
          label={l.label}
          hint={l.hint}
          placeholder={l.placeholder}
          values={draft[l.key]}
          onChange={(v) => set(l.key, v)}
        />
      ))}

      <button
        type="submit"
        disabled={submitting}
        style={{ ...obPrimaryButton, marginTop: 8, opacity: submitting ? 0.6 : 1 }}
      >
        {submitting ? "Saving…" : "Looks good"}
      </button>
    </form>
  );
};
