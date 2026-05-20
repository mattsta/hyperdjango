// HyperDjango docs: hx-boost wiring on top of mkdocs-material.
//
// mkdocs-material renders three panels per page:
//   LEFT   .md-sidebar--primary   global site nav (same on every page)
//   CENTER .md-content            article body (changes per page)
//   RIGHT  .md-sidebar--secondary current page's TOC (changes per page)
//
// On nav click we swap CENTER + the RIGHT TOC's inner nav, and leave LEFT
// completely untouched. The left sidebar's scroll listener (and every other
// mkdocs-material binding on it) stays alive because we never touch its
// DOM nodes.
//
// Why not default hx-boost behavior?
// hx-boost defaults to (target=body, swap=innerHTML). That preserves the
// <body> element but wipes every child — INCLUDING the left sidebar's
// scroll-container children that mkdocs-material has bound listeners to.
// The visible bug: after one nav click, the left sidebar refuses to scroll
// because the elements its scroll handler observed are no longer in the DOM.

(function () {
  // Set hx-boost on <html> BEFORE htmx loads. htmx scans on DOMContentLoaded
  // and picks up the inherited attributes on every descendant link.
  const root = document.documentElement;
  root.setAttribute("hx-boost", "true");
  // Target the article frame only — leaves both sidebars intact.
  root.setAttribute("hx-target", ".md-content");
  // Pull just the .md-content element OUT of the response (don't parse the
  // entire body for nothing).
  root.setAttribute("hx-select", ".md-content");
  root.setAttribute("hx-swap", "outerHTML show:window:top");

  // After a successful swap, do the bookkeeping that a full page load would
  // have done for free: update <title>, refresh sidebar active highlight,
  // and scroll to top or hash anchor.
  document.addEventListener("htmx:afterSettle", function (evt) {
    const xhr = evt.detail && evt.detail.xhr;
    if (xhr && xhr.responseText) {
      try {
        const newDoc = new DOMParser().parseFromString(
          xhr.responseText,
          "text/html"
        );
        if (newDoc.title) {
          document.title = newDoc.title;
        }
        // RIGHT panel (.md-sidebar--secondary): replace ONLY the inner
        // .md-nav--secondary so the new page's TOC shows up. We leave the
        // outer .md-sidebar--secondary wrapper + .md-sidebar__scrollwrap
        // intact so their scroll listeners survive.
        const newToc = newDoc.querySelector(
          ".md-sidebar--secondary .md-nav--secondary"
        );
        const currentToc = document.querySelector(
          ".md-sidebar--secondary .md-nav--secondary"
        );
        if (newToc && currentToc) {
          currentToc.replaceWith(newToc);
        }
        // LEFT panel (.md-sidebar--primary): NEVER touch DOM nodes. Sync
        // the .md-nav__link--active class only, by diffing class attributes
        // against the response's left nav. Existing nodes (and their bound
        // scroll listener) stay put.
        const newActiveLinks = newDoc.querySelectorAll(
          ".md-sidebar--primary .md-nav__link--active"
        );
        const newActiveHrefs = new Set(
          Array.from(newActiveLinks).map((a) => a.getAttribute("href"))
        );
        document
          .querySelectorAll(".md-sidebar--primary .md-nav__link--active")
          .forEach((el) => el.classList.remove("md-nav__link--active"));
        document
          .querySelectorAll(".md-sidebar--primary .md-nav__link")
          .forEach((el) => {
            const href = el.getAttribute("href");
            if (href && newActiveHrefs.has(href)) {
              el.classList.add("md-nav__link--active");
            }
          });
      } catch (e) {
        // Best-effort post-swap polish — never block navigation on it.
      }
    }
    if (window.location.hash) {
      const anchor = document.getElementById(window.location.hash.slice(1));
      if (anchor) {
        anchor.scrollIntoView();
        return;
      }
    }
    window.scrollTo(0, 0);
  });

  // Skip hx-boost for external links, downloads, and anchors with target=_blank.
  document.addEventListener("htmx:beforeRequest", function (evt) {
    const a = evt.detail.elt;
    if (!(a instanceof HTMLAnchorElement)) return;
    if (a.target === "_blank") {
      evt.preventDefault();
      return;
    }
    if (a.hasAttribute("download")) {
      evt.preventDefault();
      return;
    }
    if (a.host && a.host !== window.location.host) {
      evt.preventDefault();
    }
  });
})();
