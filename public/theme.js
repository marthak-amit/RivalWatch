// Light / dark mode. Default follows the system; the toggle stores an explicit choice on this device.
// Loaded in <head> so the saved theme is applied before first paint (no flash).
(function () {
  const KEY = 'rv-theme', root = document.documentElement;
  const read = () => { try { const v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; } catch { return null; } };
  const systemDark = () => matchMedia('(prefers-color-scheme: dark)').matches;
  const current = () => read() || (systemDark() ? 'dark' : 'light');
  const apply = (t) => { if (t) root.setAttribute('data-theme', t); else root.removeAttribute('data-theme'); };
  apply(read());

  const SUN = '<svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  const MOON = '<svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

  function label(btn) {
    const next = current() === 'dark' ? 'light' : 'dark'; btn.title = btn.ariaLabel = 'Switch to ' + next + ' mode';
    const t = btn.querySelector('.tlabel'); if (t) t.textContent = (next === 'dark' ? 'Dark' : 'Light') + ' mode';
  }
  function set(t) {
    apply(t);
    try { localStorage.setItem(KEY, t); } catch { /* private mode: the choice lasts for this page only */ }
    document.querySelectorAll('.themebtn, .themerow').forEach(label);
    dispatchEvent(new Event('rv-theme'));
  }

  function mount() {
    // a full-width, labelled row (the dashboard sidebar): easier to find than the round icon
    for (const slot of document.querySelectorAll('[data-theme-row]')) {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'themerow'; btn.innerHTML = SUN + MOON + '<span class="tlabel"></span>'; label(btn);
      btn.addEventListener('click', () => set(current() === 'dark' ? 'light' : 'dark'));
      slot.appendChild(btn);
    }
    const slots = document.querySelectorAll('[data-theme-slot]');
    const targets = slots.length ? [...slots] : [null];
    for (const slot of targets) {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'themebtn' + (slot ? '' : ' fixed'); btn.innerHTML = SUN + MOON; label(btn);
      btn.addEventListener('click', () => set(current() === 'dark' ? 'light' : 'dark'));
      if (slot) slot.appendChild(btn); else document.body.appendChild(btn);
    }
  }
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => { if (!read()) { document.querySelectorAll('.themebtn, .themerow').forEach(label); dispatchEvent(new Event('rv-theme')); } });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
  window.rvTheme = { get: current, set };
})();
