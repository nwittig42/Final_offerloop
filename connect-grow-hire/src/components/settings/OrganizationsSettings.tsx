// Account Settings → Organizations (OFF-42): add the orgs you're in (search,
// pick, enter its password). Joining or removing saves right away; it isn't
// part of the profile Save button. Find picks the membership up from here.
import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { OrgMembershipEditor } from "@/components/orgs/OrgMembershipEditor";
import { toast } from "@/hooks/use-toast";

export default function OrganizationsSettings() {
  const navigate = useNavigate();
  const location = useLocation();

  // Links from Find land on /account-settings#organizations.
  useEffect(() => {
    if (location.hash === "#organizations") {
      document.getElementById("organizations")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [location.hash]);

  return (
    <OrgMembershipEditor
      onOpenDirectory={(id) => navigate(`/directory/${id}`)}
      onJoined={(org) => toast({
        title: `You're in ${org.name}`,
        description: `Find now shows ${org.shortName} alumni: use the pill under the search bar or open the directory.`,
      })}
      onLeft={(org) => toast({ title: `Removed ${org.name}`, description: "You'll need the password to add it again." })}
    />
  );
}
