/** Keep the top menu handle and resting composer on the same contour. */
export function QuietLensHorizontalContour({ className }: { className?: string }) {
  return <svg className={className} viewBox="0 0 800 44" preserveAspectRatio="none" aria-hidden="true" focusable="false">
    <ellipse cx="400" cy="22" rx="390" ry="6" />
  </svg>;
}
