// The official LinkedIn mark (blue rounded square, white "in") as a link to
// someone's profile, styled as one of the card's action buttons.

// LinkedIn's brand mark, from their official logo (24×24 viewBox).
function LinkedInMark({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden className="shrink-0">
      <rect width="24" height="24" rx="4" fill="#0A66C2" />
      <path
        fill="#fff"
        d="M7.12 20.45H3.56V9h3.56v11.45ZM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13ZM20.45 20.45h-3.55v-5.57c0-1.33-.03-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.46v6.28Z"
      />
    </svg>
  );
}

// A real button in the card's action row: same height, grey fill, hover and
// press states as Info, with the LinkedIn mark inside.
export function LinkedInLink({ href, name }: { href: string; name: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open ${name}'s LinkedIn profile`}
      title={`Open ${name}'s LinkedIn`}
      className="inline-flex h-8 w-11 shrink-0 items-center justify-center rounded-full bg-[#F5F5F7] transition-colors hover:bg-[#E8E8ED] active:bg-[#DCDCE0]"
    >
      <LinkedInMark size={16} />
    </a>
  );
}
