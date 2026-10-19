// Account Settings → Organizations (OFF-42). Joining (with the org's
// password) or leaving saves right away; it isn't part of the Save button.
import { useNavigate } from "react-router-dom";
import { OrgPicker } from "@/components/OrgPicker";
import { sortOrgsForSchool, useOrgs } from "@/hooks/useOrgs";
import { toast } from "@/hooks/use-toast";

export default function OrganizationsSettings({ school }: { school: string | null }) {
  const { orgs, memberships, loading, join, leave } = useOrgs();
  const navigate = useNavigate();
  const nameOf = (id: string) => orgs.find((o) => o.id === id)?.name ?? "the organization";

  const onJoin = async (id: string, password: string) => {
    await join.mutateAsync({ id, password });
    toast({ title: `You're in ${nameOf(id)}`, description: "Open the directory here or from Find to browse every alum." });
  };
  const onLeave = async (id: string) => {
    try {
      await leave.mutateAsync(id);
      toast({ title: `Left ${nameOf(id)}`, description: "You'll need the password to rejoin." });
    } catch {
      toast({ title: "Couldn't leave right now", description: "Try again in a moment.", variant: "destructive" });
    }
  };

  if (loading) return <p className="text-[14px] text-[#64748B]">Loading organizations…</p>;
  return (
    <OrgPicker
      orgs={sortOrgsForSchool(orgs, school)}
      joined={memberships}
      onJoin={onJoin}
      onLeave={onLeave}
      onOpen={(id) => navigate(`/directory/${id}`)}
    />
  );
}
