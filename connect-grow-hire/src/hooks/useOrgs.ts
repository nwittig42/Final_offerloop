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
