interface AppHeaderProps {
  title?: string;
  titleIcon?: React.ReactNode;
  centerContent?: React.ReactNode;
  rightContent?: React.ReactNode;
  onJobTitleSuggestion?: (title: string, company?: string, location?: string) => void;
}

/**
 * AppHeader — visually removed. The 3 header icons moved to the sidebar and
 * Scout opens via a floating button. Pop-up notifications (new reply, login
 * summary, Loop finished) were removed on purpose; unread replies show as the
 * dot on the sidebar avatar. Kept as a null-rendering no-op because many pages still
 * import and render it; props are accepted but ignored for backward
 * compatibility.
 */
export function AppHeader(_props: AppHeaderProps) {
  return null;
}
