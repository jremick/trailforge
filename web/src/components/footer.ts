import { uiIcon } from "../icons";
import { REQUIRED_ATTRIBUTIONS, escapeHtml } from "../presets";

export const TRAILFORGE_GITHUB_URL = "https://github.com/jremick/trailforge";

export function renderAttributionFooter(container: HTMLElement): void {
  container.className = "tf-footer";
  container.setAttribute("aria-label", "Data attributions and license");

  const fullListHtml = REQUIRED_ATTRIBUTIONS.map(
    (item) => `
      <div class="tf-attrib-popover-row">
        <div class="tf-attrib-popover-label">${escapeHtml(item.label)}</div>
        <div class="tf-attrib-popover-meta">${escapeHtml(item.license)} · ${escapeHtml(item.fullText)}</div>
      </div>
    `
  ).join("");

  // Compact single-line desktop credits grouping datasets cleanly without multi-line wrap
  const compactCredits = [
    {
      label: "© OpenStreetMap",
      license: "ODbL",
      title: REQUIRED_ATTRIBUTIONS[0]?.fullText ?? "OpenStreetMap contributors (ODbL)",
    },
    {
      label: "GA / DEA / CAPAD / AU States",
      license: "CC BY 4.0",
      title: REQUIRED_ATTRIBUTIONS.slice(1, 5)
        .map((a) => `${a.label} (${a.license})`)
        .join(" · "),
    },
    {
      label: "Open-Meteo · OpenFreeMap · GeoNames AU",
      license: "CC BY 4.0 / ODbL",
      title: REQUIRED_ATTRIBUTIONS.slice(5)
        .map((a) => `${a.label} (${a.license})`)
        .join(" · "),
    },
  ];

  const desktopItemsHtml = compactCredits
    .map(
      (item) =>
        `<span class="tf-attrib-item" title="${escapeHtml(item.title)}">${escapeHtml(item.label)} <span class="tf-attrib-lic">(${escapeHtml(item.license)})</span></span>`
    )
    .join(`<span class="tf-attrib-sep" aria-hidden="true">·</span>`);

  container.innerHTML = `
    <div class="tf-footer-desktop">
      <div class="tf-attrib-list">
        ${desktopItemsHtml}
      </div>
      <div class="tf-footer-right">
        <button
          type="button"
          class="tf-attrib-more-btn"
          data-role="attrib-trigger"
          aria-haspopup="dialog"
          aria-expanded="false"
          title="View full data attributions and license"
        >All credits</button>
        <a
          class="tf-footer-github"
          href="${TRAILFORGE_GITHUB_URL}"
          target="_blank"
          rel="noopener noreferrer"
          title="View TrailForge source code on GitHub"
        >${uiIcon("github", 13)}<span>GitHub</span></a>
        <span class="tf-footer-license mono" title="TrailForge source code license">GPL-3.0-or-later</span>
      </div>
    </div>

    <button
      type="button"
      class="tf-mobile-attrib-btn"
      data-role="attrib-mobile-trigger"
      aria-haspopup="dialog"
      aria-expanded="false"
      title="Data attributions and license"
    >© Credits</button>

    <div
      class="tf-attrib-popover hidden"
      data-role="attrib-popover"
      role="dialog"
      aria-label="Data sources and licensing"
    >
      <div class="tf-attrib-popover-head">
        <strong>Data sources &amp; licensing</strong>
        <button
          type="button"
          class="tf-attrib-popover-close"
          data-role="attrib-close"
          aria-label="Close attributions"
        >${uiIcon("close", 14)}</button>
      </div>
      <div class="tf-attrib-popover-body">
        ${fullListHtml}
      </div>
      <div class="tf-attrib-popover-foot">
        <a
          class="tf-footer-github"
          href="${TRAILFORGE_GITHUB_URL}"
          target="_blank"
          rel="noopener noreferrer"
          title="View TrailForge source code on GitHub"
        >${uiIcon("github", 13)}<span>TrailForge on GitHub</span></a>
        <span class="tf-footer-license mono">GPL-3.0-or-later</span>
      </div>
    </div>
  `;

  const popover = container.querySelector<HTMLElement>("[data-role='attrib-popover']");
  const triggers = container.querySelectorAll<HTMLButtonElement>(
    "[data-role='attrib-trigger'], [data-role='attrib-mobile-trigger']"
  );
  const closeBtn = container.querySelector<HTMLButtonElement>("[data-role='attrib-close']");

  const setOpen = (open: boolean): void => {
    popover?.classList.toggle("hidden", !open);
    triggers.forEach((btn) => btn.setAttribute("aria-expanded", String(open)));
  };

  triggers.forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = popover?.classList.contains("hidden") ?? false;
      setOpen(willOpen);
    });
  });

  closeBtn?.addEventListener("click", () => setOpen(false));

  document.addEventListener("click", (e) => {
    if (!popover || popover.classList.contains("hidden")) return;
    const target = e.target as Node;
    if (!container.contains(target)) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && popover && !popover.classList.contains("hidden")) {
      setOpen(false);
    }
  });
}
