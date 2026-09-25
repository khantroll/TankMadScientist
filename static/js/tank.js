/**
 * tank.js — client-side helpers for the Tank dashboard.
 *
 * 1. Scroll preservation — keeps open run-output panels at their current
 *    scroll position when HTMX refreshes any container that can reattach
 *    them: the top-level run list, a crew's own children-list poll, or a
 *    direct self-poll on the slot itself. hx-preserve keeps a slot's
 *    content/attributes intact across a container-level swap, but the
 *    detach+reattach involved still resets scrollTop in most browsers —
 *    so every container that can do that needs explicit capture/restore,
 *    not just the outermost one.
 *
 * 2. Run-output accordion — toggleRunOutput() / openRunOutput() collapse
 *    or reveal the output slot of any run card.  The slot is hidden by
 *    default and its display state is preserved across HTMX swaps via
 *    hx-preserve, so expanding a log and then letting the page poll never
 *    collapses it again.
 *
 * 3. Crew-children tree — toggleCrewChildren() shows/hides the nested
 *    sub-agent panel for crew-type runs.
 *
 * 4. After-swap sync — htmx:afterSwap re-syncs chevron icons to match the
 *    (preserved) display states and auto-expands any run that has just
 *    reached awaiting_approval with fresh output ready.
 */

/* ── 1. Scroll preservation ─────────────────────────────────────────────── */
(function () {
  var saved = {};
  // Saved window-level scroll Y position; captured before #run-list swaps so
  // that content-height changes during the swap don't shift the page scroll.
  var savedWindowY = null;

  function scrollContainer(slot) {
    if (!slot) return null;
    return slot.querySelector(".run-output-scroll, .run-output, .run-output-formatted");
  }

  function captureScroll(slotId, el) {
    if (!el || el.clientHeight === 0) return; // skip hidden slots
    saved[slotId] = {
      top: el.scrollTop,
      atBottom: el.scrollHeight - el.scrollTop - el.clientHeight < 48,
    };
  }

  function restoreScroll(slotId, slot) {
    var state = saved[slotId];
    var el = scrollContainer(slot);
    if (!el || !state) return;
    if (state.atBottom) {
      el.scrollTop = el.scrollHeight;
    } else {
      el.scrollTop = state.top;
    }
    delete saved[slotId];
  }

  function isRunOutputTarget(target) {
    return target && target.id && target.id.indexOf("run-output-") === 0;
  }

  function isContainerTarget(target) {
    // Containers whose polled swap can re-insert (and, via hx-preserve,
    // reattach) nested #run-output-* slots: the top-level run list, and
    // each crew's own children-list poll. Reattaching a preserved node
    // resets its scrollTop in most browsers even though its content and
    // attributes survive — so every container that can do this needs the
    // same capture-before / restore-after treatment, not just #run-list.
    return (
      target &&
      target.id &&
      (target.id === "run-list" || target.id.indexOf("crew-children-inner-") === 0)
    );
  }

  function captureContainerSlots(container) {
    container.querySelectorAll('[id^="run-output-"]').forEach(function (slot) {
      if (slot.innerHTML.trim() && slot.style.display !== "none") {
        captureScroll(slot.id, scrollContainer(slot));
      }
    });
  }

  function restoreContainerSlots(container) {
    container.querySelectorAll('[id^="run-output-"]').forEach(function (slot) {
      restoreScroll(slot.id, slot);
    });
  }

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail.target;
    if (!target) return;

    if (target.id === "run-list") {
      // Capture the window scroll Y so we can restore it after the swap if
      // the new content has a different height (avoids page-level jitter).
      savedWindowY = window.scrollY;
    }

    if (isContainerTarget(target)) {
      captureContainerSlots(target);
      return;
    }

    if (isRunOutputTarget(target)) {
      captureScroll(target.id, scrollContainer(target));
    }
  });

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail.target;
    if (!target) return;

    if (window.syncProviderKeyHints) window.syncProviderKeyHints(target);

    if (isContainerTarget(target)) {
      // 0. Restore the page scroll position first (run-list only) so any
      //    layout shift from the new content doesn't move the viewport.
      if (target.id === "run-list" && savedWindowY !== null) {
        window.scrollTo(0, savedWindowY);
        savedWindowY = null;
      }

      // 1. Restore scroll positions in visible output slots.
      restoreContainerSlots(target);

      // 2. Sync output chevrons — the row markup is freshly rendered but
      //    the output slot is hx-preserved, so re-read its display state.
      target.querySelectorAll(".output-chevron").forEach(function (chevron) {
        var runId = chevron.id.replace("output-chevron-", "");
        _syncOutputChevron(runId);
      });

      // 3. Sync crew chevrons the same way (run-list only — crew chevrons
      //    don't appear inside a crew's own children list).
      if (target.id === "run-list") {
        target.querySelectorAll(".crew-chevron[id^='crew-chevron-']").forEach(function (chevron) {
          var runId = chevron.id.replace("crew-chevron-", "");
          var container = document.getElementById("crew-children-" + runId);
          if (container) {
            chevron.classList.toggle("crew-chevron--open", container.style.display !== "none");
          }
        });
      }

      // 4. Auto-expand any awaiting_approval run whose output slot now has
      //    content but is still hidden. Covers both top-level run cards
      //    (id="run-card-N") and crew child rows (id="crew-child-N").
      //    Uses _autoExpandedRuns to ensure we only auto-expand once per run
      //    so the user can re-collapse freely.
      target.querySelectorAll(".status-dot--awaiting_approval").forEach(function (dot) {
        var card = dot.closest(".run-card") || dot.closest(".crew-child-run");
        if (!card) return;
        var runId = card.id.replace(/^run-card-|^crew-child-/, "");
        if (_autoExpandedRuns.has(runId)) return;
        var slot = document.getElementById("run-output-" + runId);
        if (slot && slot.innerHTML.trim() && slot.style.display === "none") {
          _setRunOutputOpen(runId, true);
          _autoExpandedRuns.add(runId);
        }
      });

      return;
    }

    if (isRunOutputTarget(target)) {
      restoreScroll(target.id, target);

      // When an approve or cancel POST swaps new content into the slot,
      // force the slot open. Polling swaps use GET so they don't reach here.
      var verb =
        evt.detail.requestConfig && evt.detail.requestConfig.verb;
      if (verb === "post") {
        var runId = target.id.replace("run-output-", "");
        _setRunOutputOpen(runId, true);
      }
    }
  });
})();

/* ── 2. Run-output accordion ────────────────────────────────────────────── */

/**
 * Tracks run IDs that were auto-expanded to awaiting_approval so we don't
 * fight the user if they manually collapse the card.
 * @type {Set<string>}
 */
var _autoExpandedRuns = new Set();

/**
 * Set or clear the open state for a run output slot, updating chevron and
 * card class in one place.
 * @param {string|number} runId
 * @param {boolean} open
 */
function _setRunOutputOpen(runId, open) {
  var slot    = document.getElementById("run-output-"    + runId);
  var chevron = document.getElementById("output-chevron-" + runId);
  var card    = document.getElementById("run-card-"      + runId);
  if (!slot) return;

  if (open) {
    slot.style.display = "block";
    // If the slot is empty, fire a one-shot load regardless of whether the
    // slot already has an hx-trigger polling interval.  Active runs have
    // hx-trigger="every 2s" but waiting for the next tick (up to 2 s) makes
    // the panel appear broken on first open.
    if (!slot.innerHTML.trim()) {
      htmx.ajax("GET", "/runs/" + runId + "/output", {
        target: "#run-output-" + runId,
        swap:   "innerHTML",
      });
    }
  } else {
    slot.style.display = "none";
  }

  if (chevron) chevron.classList.toggle("crew-chevron--open", !!open);
  if (card)    card.classList.toggle("output-open",           !!open);
}

/** Sync chevron direction from the current slot display state (no side effects). */
function _syncOutputChevron(runId) {
  var slot    = document.getElementById("run-output-"    + runId);
  var chevron = document.getElementById("output-chevron-" + runId);
  if (!slot || !chevron) return;
  chevron.classList.toggle("crew-chevron--open", slot.style.display !== "none");
}

/**
 * Toggle a run's output panel open or closed.
 * Called via onclick on the run-head row in run_list.html / run_children.html.
 */
function toggleRunOutput(runId) {
  var slot = document.getElementById("run-output-" + runId);
  if (!slot) return;
  _setRunOutputOpen(runId, slot.style.display === "none");
}

/**
 * Ensure a run's output panel is visible (used by cancel/approve callbacks).
 */
function openRunOutput(runId) {
  _setRunOutputOpen(runId, true);
}

/* ── 3. Crew-children tree ──────────────────────────────────────────────── */

/**
 * Toggle the visibility of a crew run's nested child-steps panel.
 * Called from the amber chevron button in the run header.
 */
function toggleCrewChildren(runId) {
  var container = document.getElementById("crew-children-" + runId);
  var chevron   = document.getElementById("crew-chevron-"  + runId);
  if (!container) return;

  var isOpen = container.style.display !== "none";
  container.style.display = isOpen ? "none" : "block";
  if (chevron) chevron.classList.toggle("crew-chevron--open", !isOpen);
}

/* ── 4. Provider key hints ──────────────────────────────────────────────── */

function syncProviderKeyHint(select) {
  if (!select) return;
  var hint = select.nextElementSibling;
  if (!hint || !hint.classList.contains("provider-key-hint")) return;
  var option = select.options[select.selectedIndex];
  var text = "";
  if (option && option.getAttribute("data-key-missing") === "1") {
    var env = option.getAttribute("data-key-env") || "The API key env var";
    if (option.getAttribute("data-key-fallback") === "1") {
      text = env + " is not set. A local default is configured, so calls can still authenticate.";
    } else {
      text = env + " is not set. This provider will fail until you set that environment variable.";
    }
  }
  hint.textContent = text;
  hint.hidden = !text;
}

function syncProviderKeyHints(root) {
  var scope = root && root.querySelectorAll ? root : document;
  scope.querySelectorAll(".provider-select").forEach(syncProviderKeyHint);
}

window.syncProviderKeyHints = syncProviderKeyHints;

document.addEventListener("change", function (evt) {
  var target = evt.target;
  if (target && target.classList && target.classList.contains("provider-select")) {
    syncProviderKeyHint(target);
  }
});

document.addEventListener("DOMContentLoaded", function () {
  syncProviderKeyHints(document);
});
