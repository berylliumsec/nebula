const finiteRect = (value) => value && ["x", "y", "width", "height"].every(
  (key) => Number.isFinite(value[key]),
) && value.width >= 0 && value.height >= 0;

const contained = (inner, outer, tolerance = 1) =>
  inner.x >= outer.x - tolerance && inner.y >= outer.y - tolerance &&
  inner.x + inner.width <= outer.x + outer.width + tolerance &&
  inner.y + inner.height <= outer.y + outer.height + tolerance;

/** Return bounds in Electron content coordinates, or null to hide the page. */
export function pageBounds(layout, contentBounds) {
  const { viewport, panel, controls, page, dialogOpen } = layout ?? {};
  if (dialogOpen || ![viewport, panel, controls, page].every(finiteRect) ||
      !finiteRect(contentBounds) || viewport.width < 1 || viewport.height < 1 ||
      page.width < 1 || page.height < 1 ||
      !contained(panel, viewport) || !contained(controls, panel) ||
      !contained(page, panel) || page.y < controls.y + controls.height - 1) return null;

  // The renderer's viewport is expressed in CSS pixels. Native view bounds use
  // content DIPs; this ratio handles both 90% Electron zoom and display scaling.
  const scaleX = contentBounds.width / viewport.width;
  const scaleY = contentBounds.height / viewport.height;
  if (!Number.isFinite(scaleX) || !Number.isFinite(scaleY) ||
      scaleX < 0.4 || scaleX > 2.5 || scaleY < 0.4 || scaleY > 2.5 ||
      Math.abs(scaleX - scaleY) > 0.05) return null;
  const x = Math.ceil((page.x - viewport.x) * scaleX);
  const y = Math.ceil((page.y - viewport.y) * scaleY);
  const right = Math.floor((page.x + page.width - viewport.x) * scaleX);
  const bottom = Math.floor((page.y + page.height - viewport.y) * scaleY);
  const bounds = { x, y, width: right - x, height: bottom - y };
  return bounds.width > 0 && bounds.height > 0 &&
    contained(bounds, { x: 0, y: 0, width: contentBounds.width, height: contentBounds.height }, 0)
    ? bounds : null;
}
