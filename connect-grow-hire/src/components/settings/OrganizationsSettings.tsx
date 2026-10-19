// Account Settings → Organizations (OFF-42). Joining or leaving saves right
// away (it's a backend write, not part of the profile Save button).
import { useState } from "react";
import { OrgPicker } from "@/components/OrgPicker";
import { sortOrgsForSchool, useOrgs } from "@/hooks/useOrgs";
import { toast } from "@/hooks/use-toast";

export default function OrganizationsSettings({ school }: { school: string | null }) {
  const { orgs, memberships, loading, join, leave } = useOrgs();
  const [busyId, setBusyId] = useState<string | null>(null);

  const toggle = async (id: string) => {
    const org = orgs.find((o) => o.id === id);
    const leaving = memberships.includes(id);
    setBusyId(id);
    try {
      await (leaving ? leave : join).mutateAsync(id);
      toast({
        title: leaving ? `Left ${org?.name ?? "organization"}` : `You're in ${org?.name ?? "the organization"}`,
        description: leaving ? undefined : `Search "${org?.shortName} alumni" in Find to see the directory.`,
      });
    } catch {
      toast({ title: "Couldn't update your organizations", description: "Try again in a moment.", variant: "destructive" });
    } finally {
      setBusyId(null);
    }
  };

  if (loading) return <p className="text-[14px] text-[#64748B]">Loading organizations…</p>;
  return <OrgPicker orgs={sortOrgsForSchool(orgs, school)} selected={memberships} onToggle={toggle} busyId={busyId} />;
}
