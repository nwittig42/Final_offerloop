/**
 * Turn the named nested maps of an updateDoc payload into dotted field paths
 * ("goals.careerTrack"), so the update merges into each map instead of
 * replacing it and wiping the fields it doesn't mention.
 */
export function mergeNestedMaps(payload: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = { ...payload };
  for (const key of keys) {
    const value = out[key];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      delete out[key];
      for (const [k, v] of Object.entries(value)) out[`${key}.${k}`] = v;
    }
  }
  return out;
}
