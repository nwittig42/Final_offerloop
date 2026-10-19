// Org alumni directories (OFF-42): which orgs exist and which the student
// has joined. Join (with the org's password) and leave write through the
// backend and refresh this query.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiService, type Org } from "@/services/api";
import { getUniversityShortName } from "@/lib/universityUtils";

export const ORGS_KEY = ["orgs"] as const;

export function useOrgs(enabled = true) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ORGS_KEY,
    queryFn: () => apiService.getOrgs(),
    enabled,
    // Membership changes what Find shows, so don't trust a cached (or cached
    // failed) answer when a page that needs it mounts.
    refetchOnMount: "always",
    retry: 2,
  });
  const onSuccess = (res: { memberships: string[] }) =>
    queryClient.setQueryData(ORGS_KEY, (prev: { orgs: Org[]; memberships: string[] } | undefined) =>
      prev ? { ...prev, memberships: res.memberships } : prev,
    );
  const join = useMutation({
    mutationFn: ({ id, password }: { id: string; password: string }) => apiService.joinOrg(id, password),
    onSuccess,
  });
  const leave = useMutation({ mutationFn: (id: string) => apiService.leaveOrg(id), onSuccess });

  const orgs = query.data?.orgs ?? [];
  const memberships = query.data?.memberships ?? [];
  return {
    orgs,
    memberships,
    joined: orgs.filter((o) => memberships.includes(o.id)),
    loading: query.isLoading,
    isError: query.isError,
    refetch: query.refetch,
    join,
    leave,
  };
}

// Orgs at the student's school first; everything else after (school unknown
// or spelled differently shouldn't hide an org they belong to).
export function sortOrgsForSchool(orgs: Org[], school: string | null | undefined): Org[] {
  if (!school) return orgs;
  const short = getUniversityShortName(school);
  const mine = (s: string) =>
    s.toLowerCase() === school.toLowerCase() || (!!short && getUniversityShortName(s) === short);
  return [...orgs].sort((a, b) => Number(mine(b.school)) - Number(mine(a.school)));
}

const DEFAULT_ACCENT = "#3A4E8C";

// The org's color, or the app's indigo when it has none.
export function orgAccent(org: Pick<Org, "accentColor"> | null | undefined): string {
  return org?.accentColor && /^#[0-9a-fA-F]{6}$/.test(org.accentColor) ? org.accentColor : DEFAULT_ACCENT;
}

// "USC TKE" -> "TKE": the org's own letters for its monogram.
export function orgMonogram(org: Pick<Org, "shortName" | "name">): string {
  const words = (org.shortName || org.name).split(/\s+/).filter(Boolean);
  const last = words[words.length - 1] || "";
  return (last.length <= 4 ? last : words.map((w) => w[0]).join("")).toUpperCase().slice(0, 4);
}
