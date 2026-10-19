import { stageStyle, type ContactRow } from "./contactsModel";

// Colored dot + label for a contact's pipeline stage.
export function StagePill({ contact, size = "sm" }: { contact: Pick<ContactRow, "stage" | "stageLabel">; size?: "sm" | "md" }) {
  const style = stageStyle(contact.stage);
  const dims = size === "md" ? "px-2.5 py-1 text-[12.5px] gap-1.5" : "px-2 py-0.5 text-[11px] gap-1";
  return (
    <span className={`inline-flex shrink-0 items-center rounded-full font-medium ${dims} ${style.pill}`}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: style.dot }} />
      {contact.stageLabel}
    </span>
  );
}
