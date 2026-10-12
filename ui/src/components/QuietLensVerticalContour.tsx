/** Keep the navigation handle and collapsed Session rail on the same contour. */
export function QuietLensVerticalContour({ className }: { className?: string }) {
  return <svg className={className} viewBox="0 0 44 600" preserveAspectRatio="none" aria-hidden="true" focusable="false">
    <ellipse cx="22" cy="300" rx="6" ry="290" />
  </svg>;
}
