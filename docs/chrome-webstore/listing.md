# Hush — Chrome Web Store Listing Copy

Copy-paste ready. Every section maps to a field in the Developer Dashboard.

---

## Item Name (max 45 chars)
```
Hush — Media Pause for Voice Assistants
```
(39 chars)

## Summary (max 132 chars, shown under title in search)
```
Instantly pause YouTube, Spotify Web, and other browser media when your voice assistant speaks or listens.
```
(106 chars)

## Category
**Productivity**

## Language
English

---

## Detailed Description (up to 16,000 chars — use as much as needed)

```
Hush pauses whatever's playing in your browser — YouTube, Spotify Web, Netflix, Apple Music, podcasts — the moment your voice assistant needs to listen or speak. When it's done, Hush resumes playback exactly where you left off.

WHAT PROBLEM DOES IT SOLVE?

Voice assistants talk over your music. Music drowns out your voice commands. Hush fixes both — instantly, without fiddling with volume controls or media keys.

HOW IT WORKS

Hush is the browser half of a two-part system. It listens on a local Unix socket for pause/resume events from a companion desktop app (for example, HeyVox at https://heyvox.dev). When the desktop app sends a pause event, Hush finds any audible tab and mutes or pauses it. When the event clears, playback resumes.

Nothing leaves your machine. Hush never sends audio, metadata, or browsing history anywhere. All communication is local-only, between the extension and a desktop companion running on the same computer.

FEATURES

• Automatic tab detection — pauses whichever tab is playing audio, not just the focused one
• Resume on release — playback continues from exactly where it stopped
• Site-specific media control — uses each site's native pause/play where possible (YouTube, Spotify, Netflix), falls back to tab muting otherwise
• All-frame support — works even when media is embedded in iframes
• Zero configuration — install, confirm native messaging connection, done

REQUIREMENTS

• A companion desktop app that speaks the Hush protocol (HeyVox includes one)
• macOS 14+ (the current desktop companion is macOS-only; Windows/Linux companions welcome)
• Chrome 116 or later

PRIVACY

Hush is privacy-first by design:

• No cloud services. Ever.
• No analytics, telemetry, or tracking.
• No ads.
• No user accounts.
• No data collection of any kind.

The "<all_urls>" permission is required because media can play on any site, and Hush needs to be able to pause tabs wherever audio is coming from. The "nativeMessaging" permission is how the extension talks to the local desktop companion. See the full privacy policy: https://heyvox.dev/privacy.html

OPEN SOURCE

Hush is part of the HeyVox project and is MIT-licensed. Source code, issue tracker, and contribution guide:
https://github.com/heyvox-dev/heyvox

QUESTIONS? ISSUES?

File an issue at https://github.com/heyvox-dev/heyvox/issues or email hello@heyvox.dev.
```

---

## Permission Justifications

Each of these maps to a field in the "Privacy practices" tab. Paste verbatim.

### Single Purpose (required)
```
Hush pauses and resumes in-browser media playback on behalf of a local desktop voice assistant, so the assistant can be heard and can hear the user without competing audio.
```

### Justification — `host_permissions` ("<all_urls>")
```
Media plays on any website — YouTube, Spotify Web, Netflix, Apple Music, podcasts, news sites, countless others. The extension has to be able to pause a tab regardless of which site is playing audio, which requires access to all URLs. The extension never reads or transmits page content; it only issues pause/play commands to the site's media elements.
```

### Justification — `tabs`
```
Required to enumerate tabs to find which one is producing audio (the "audible" property), and to send pause/resume messages to the correct tab. No tab content, URLs, titles, or history is collected or transmitted off-device.
```

### Justification — `scripting`
```
Required to execute a small script in the target tab that pauses or resumes the page's native media element. Site-specific handling (for example, clicking YouTube's play button versus calling .pause() on a generic <video> tag) is more reliable than a blanket mute, and keeps the tab's media state in sync. The script does not exfiltrate any data.
```

### Justification — `nativeMessaging`
```
Hush is controlled by a local desktop voice assistant (such as HeyVox). nativeMessaging is the Chrome-approved way for the extension to receive pause/resume events from that locally-installed companion application. No remote servers are involved; the channel is a local process pipe between Chrome and the companion on the same machine.
```

### Justification — Remote Code
```
No remote code is used. The extension ships all of its JavaScript bundled. No eval, no remote script loading, no Content-Security-Policy bypass.
```

### Data Usage Disclosures (check exactly these, uncheck the rest)

- Personally identifiable information: **No**
- Health information: **No**
- Financial and payment information: **No**
- Authentication information: **No**
- Personal communications: **No**
- Location: **No**
- Web history: **No**
- User activity: **No** (Hush reads `tab.audible` in-memory only, never stores it)
- Website content: **No**

And the three certifications at the bottom:

- [x] I do not sell or transfer user data to third parties, apart from approved use cases
- [x] I do not use or transfer user data for purposes unrelated to the item's single purpose
- [x] I do not use or transfer user data to determine creditworthiness or for lending purposes

---

## Support Email
```
hello@heyvox.dev
```
(Or any address on a domain you control.)

## Website
```
https://heyvox.dev
```

## Privacy Policy URL (required because of `<all_urls>` + `nativeMessaging`)
```
https://heyvox.dev/privacy.html
```

---

## Screenshots (required: at least 1, up to 5, 1280×800 or 640×400 PNG/JPEG)

Planned shots — take these at 1280×800 against a neutral background:

1. **Hero** — the Hush popup open next to a paused YouTube tab, with HeyVox's mic indicator visible in the menu bar. Caption: "Voice assistant speaks. Music pauses. Automatic."
2. **Multi-tab** — three audible tabs (YouTube, Spotify Web, a podcast), Hush popup showing them all. Caption: "Handles every audible tab, not just the focused one."
3. **Site support** — YouTube with native pause engaged (play button visible). Caption: "Native pause on YouTube, Spotify, Netflix, and more."
4. **Privacy** — popup or a graphic with "No cloud. No telemetry. No accounts." Caption: "100% local. Nothing leaves your computer."
5. **Integration** — HeyVox menu bar icon + Hush popup side by side. Caption: "Pairs with any desktop assistant that speaks the Hush protocol."

## Promotional Tile (440×280, required for the category page)

Simple flat tile:
- HeyVox/Hush wordmark top-left
- Tagline: "Media pause for voice assistants"
- Muted note glyph (🔇 or similar stylised icon) right-aligned

## Small Marquee (optional, 920×680)
Skip for v1 — you can add it later if you want featured placement.
