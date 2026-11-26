import { REQUIRED_ATTRIBUTIONS, escapeHtml } from "../presets";

export function renderAttributionFooter(container: HTMLElement): void {
  container.className = "tf-footer";
  container.setAttribute("aria-label", "Data attributions and license");

  const itemsHtml = REQUIRED_ATTRIBUTIONS.map(
    (item) =>
      `<span class="tf-attrib-item" title="${escapeHtml(item.fullText)}">${escapeHtml(item.label)} <span class="tf-attrib-lic">(${escapeHtml(item.license)})</span></span>`
  ).join(`<span class="tf-attrib-sep" aria-hidden="true">·</span>`);

  container.innerHTML = `
    <div class="tf-attrib-list">
      ${itemsHtml}
    </div>
    <div class="tf-footer-license mono" title="TrailForge source code license">
      GPL-3.0-or-later
    </div>
  `;
}
