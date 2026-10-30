// Org logos (OFF-42), keyed by org id, the same way universityLogos keys
// schools. An org without one falls back to its letters on its color
// (orgMonogram / orgAccent in hooks/useOrgs).
import uscTkeLogo from "@/assets/orgs/usc_tke.png";

const ORG_LOGOS: Record<string, string> = {
  usc_tke: uscTkeLogo,
};

export function orgLogo(orgId: string | null | undefined): string | null {
  return (orgId && ORG_LOGOS[orgId]) || null;
}
