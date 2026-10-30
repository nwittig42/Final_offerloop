// Profile photo from the people source (Coresignal's LinkedIn picture),
// falling back to initials when there is no photo or it fails to load.
// LinkedIn CDN links are signed and expire, so a broken image is expected
// for older records, not an error.
import { useEffect, useState } from "react";
import { initials } from "./peopleModel";

// Gold ring for top connections: a metallic gradient ring with a thin white
// gap between it and the photo, like an Apple Watch activity ring.
export const GOLD_RING = "conic-gradient(from 210deg, #F5D27A, #C9962E, #F9E3A3, #B8862A, #F5D27A)";

export function PersonAvatar({ name, photoUrl, size = 56, ring = false }: { name: string; photoUrl?: string; size?: number; ring?: boolean }) {
  if (ring) {
    return (
      <span className="shrink-0 rounded-full p-[2.5px]" style={{ background: GOLD_RING }}>
        <span className="block rounded-full bg-white p-[2px]">
          <PersonAvatar name={name} photoUrl={photoUrl} size={size - 9} />
        </span>
      </span>
    );
  }
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [photoUrl]);

  const box = { width: size, height: size };
  if (photoUrl && !failed) {
    return (
      <img
        src={photoUrl}
        alt=""
        style={box}
        className="shrink-0 rounded-full bg-[#F5F5F7] object-cover"
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <div
      style={{
        ...box,
        fontSize: Math.round(size * 0.36),
        // Apple Contacts monogram: soft grey gradient, white letters.
        background: "linear-gradient(180deg, #A9A9AE 0%, #8B8B90 100%)",
      }}
      className="flex shrink-0 items-center justify-center rounded-full font-medium tracking-[0.02em] text-white"
    >
      {initials(name)}
    </div>
  );
}
