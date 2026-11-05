// The studio's page in Home Assistant's sidebar. A "custom" panel gets the whole area, so HA draws
// no title bar above it (the old "iframe" panel always had one). The studio stays in its own frame.
class HadashStudioPanel extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.innerHTML = `<style>:host{display:block;height:100vh;height:100dvh;background:#1c1c1e}iframe{display:block;width:100%;height:100%;border:0}</style>`;
    this.frame = document.createElement("iframe");
    // A name for screen readers. Not a title: Safari shows an iframe's title as a tooltip over everything inside it.
    this.frame.setAttribute("aria-label", "Dashboard Studio");
    this.frame.allow = "clipboard-write; fullscreen";
    this.frame.src = "/hadash/studio.html?panel";
    this.frame.addEventListener("load", () => this.tell());
    root.appendChild(this.frame);
  }
  // HA hides its sidebar on narrow screens or when the person chose "always hidden".
  // The studio then shows a ☰ button that opens it.
  set narrow(v) { this._narrow = v; this.tell(); }
  set hass(h) {
    const hidden = h && h.dockedSidebar === "always_hidden";
    if (hidden !== this._hidden) { this._hidden = hidden; this.tell(); }
  }
  tell() {
    const menu = !!(this._narrow || this._hidden);
    this.frame.dataset.menu = menu ? "1" : "0";
    try { this.frame.contentWindow && this.frame.contentWindow.dispatchEvent(new CustomEvent("hadash-menu", { detail: menu })); } catch (e) { /* not loaded yet */ }
  }
}
if (!customElements.get("hadash-studio-panel")) customElements.define("hadash-studio-panel", HadashStudioPanel);
