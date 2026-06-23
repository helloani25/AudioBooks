// ── book.js ───────────────────────────────────────────────────────────────────
//
// Entry point for book.html. Loads a single book by ?book=<id> and renders
// a multi-tab detail page. The view is driven by `bookState.data` — a plain
// object created by createBookState() and populated by loadBookDetail().
//
// ── Page layout ───────────────────────────────────────────────────────────────
//
//  ┌──────────────────────────── topbar ─────────────────────────────────────┐
//  │ logo │ Home | Categories ▼ | Recently Played │ [search]    [Logout]    │
//  └─────────────────────────────────────────────────────────────────────────┘
//  ┌──────────────── book-layout ────────────────────────────────────────────┐
//  │ ┌──── book-hero ──────────────────────────────────────────────────────┐ │
//  │ │  [cover img]  │  eyebrow · h1 title · byline (author · year · lang)│ │
//  │ └─────────────────────────────────────────────────────────────────────┘ │
//  │ ┌──── book-tab-bar ───────────────────────────────────────────────────┐ │
//  │ │  [About]  [Read]  [Listen]  [Video]                                │ │
//  │ └─────────────────────────────────────────────────────────────────────┘ │
//  │                                                                         │
//  │  ── tab-about (About tab) ──────────────────────────────────────────── │
//  │  │  summary paragraph                                                  │ │
//  │  │  book-meta-grid  (Author | Translator | Narrator | Language | ...)  │ │
//  │  │  media-tags      (Text · Audio · Video availability badges)        │ │
//  │                                                                         │
//  │  ── tab-read (Read tab) ────────────────────────────────────────────── │
//  │  ┌── book-sidebar ────┐  ┌── read-main ──────────────────────────────┐ │
//  │  │ chapter-panel-title│  │ #reader-title   (chapter heading)         │ │
//  │  │ #book-chapters     │  │ #reader-subtitle                          │ │
//  │  │  (chapter list)    │  │ #reader-content                           │ │
//  │  │ #chapter-pagination│  │   plain text → pre-wrap paragraphs        │ │
//  │  └────────────────────┘  │   HTML edition → DOMParser + inline images│ │
//  │                          └───────────────────────────────────────────┘ │
//  │                                                                         │
//  │  ── tab-listen (Listen tab) ────────────────────────────────────────── │
//  │  ┌── listen-player-panel ──────────────────────────────────────────┐   │
//  │  │  now-playing header · <audio> element · format/narrator meta   │   │
//  │  └────────────────────────────────────────────────────────────────┘   │
//  │  ┌── listen-playlist ──────────────────────────────────────────────┐   │
//  │  │  [track 1] [track 2] [track 3] ...                             │   │
//  │  └────────────────────────────────────────────────────────────────┘   │
//  └─────────────────────────────────────────────────────────────────────────┘
//
// ── State and rendering model ─────────────────────────────────────────────────
//
//  createBookState(bookId) → initial snapshot (all fields default/empty)
//  loadBookDetail(bookId)  → fills snapshot from 4 parallel API calls:
//                              /api/books/<id>/description  (summary, author, genres)
//                              /api/books/<id>/content      (text or HTML, content_type)
//                              /api/books/<id>/audio        (tracks, narrator)
//                              /api/media-history/books/<id>(resume position)
//
//  renderBookState()       → top-level render driven by data.mode:
//    mode = "about"  → About tab:  summary + metadata grid + media tags
//    mode = "read"   → Read tab:   renderSidebarChapters() + renderReadPanel()
//    mode = "listen" → Listen tab: renderListenPanel() → createAudioElement()
//
//  Tab switches call setActiveTab(tab) → sets data.mode → renderBookState()
//
// ── Audio playback ────────────────────────────────────────────────────────────
//
//  createAudioElement() creates the <audio> element and attaches it to the DOM.
//  loadTrack(index) swaps the src and seeks to the saved position.
//  Progress is saved to localStorage and to /api/media-history/books/<id>
//  every 5 seconds via the timeupdate event.
//  autoplayOnLoad is set to true when returning to a book that has an audio
//  resume position, so the track starts playing on the first render.
//
// ── Read tab: plain text vs HTML edition ──────────────────────────────────────
//
//  content_type = "text"  → splitIntoChapters() parses heading lines into
//                           chapter blocks; each block rendered as pre-wrap text
//  content_type = "html"  → HTML from the -h.zip archive is sanitized,
//                           Gutenberg boilerplate is removed, and chapter
//                           anchors are built from HTML headings; images served through
//                           /api/books/<id>/images/<filename> → signed GCS URL
//
import "./style.css";

const app = document.querySelector("#app");
const currentUrl = new URL(window.location.href);
const bookIdParam = currentUrl.searchParams.get("book");
const activeBookId = bookIdParam ? Number.parseInt(bookIdParam, 10) : null;
const isBookRoute = Number.isFinite(activeBookId) && activeBookId > 0;
const shouldAutoplayResume = currentUrl.searchParams.get("autoplay") === "1";
const returnTarget = `${window.location.pathname}${window.location.search}${window.location.hash}`;
const REQUEST_TIMEOUT_MS = 15000;
const CHAPTERS_PER_PAGE = 10;
const DEFAULT_COVER_ART = `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 420" role="img" aria-label="Default book cover">
  <rect width="320" height="420" rx="24" fill="#f8f5ef"/>
  <rect x="24" y="24" width="272" height="372" rx="18" fill="#ffffff" stroke="#e7e0d2"/>
  <rect x="48" y="56" width="120" height="12" rx="6" fill="#1a7f6e"/>
  <rect x="48" y="92" width="200" height="20" rx="10" fill="#151616" opacity="0.88"/>
  <rect x="48" y="126" width="166" height="14" rx="7" fill="#3a3f43" opacity="0.68"/>
  <rect x="48" y="168" width="224" height="128" rx="16" fill="#efe3cf"/>
  <path d="M76 260l40-58 32 38 30-22 52 42H76z" fill="#b6e2d3"/>
  <circle cx="106" cy="198" r="18" fill="#f1b972"/>
  <rect x="48" y="314" width="104" height="10" rx="5" fill="#1a7f6e"/>
  <rect x="48" y="338" width="190" height="12" rx="6" fill="#3a3f43" opacity="0.5"/>
</svg>
`)}`;

// ── Utilities ─────────────────────────────────────────────────────────────────

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

const formatNumber = (value) => {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString() : String(value ?? "0");
};

const fetchWithTimeout = async (input, init = {}, timeoutMs = REQUEST_TIMEOUT_MS) => {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeoutId);
  }
};

const parseYearFromPublication = (value) => {
  const match = String(value ?? "").match(/\b(1[89]\d{2}|20\d{2})\b/);
  return match ? Number.parseInt(match[1], 10) : null;
};

const splitSubjects = (value) =>
  String(value ?? "")
    .split(/[|;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);

const pickCoverImageUrl = (covers) => {
  if (!Array.isArray(covers) || covers.length === 0) return DEFAULT_COVER_ART;
  const preferred =
    covers.find((cover) => String(cover.size_label || "").toLowerCase() === "medium") ||
    covers.find((cover) => String(cover.size_label || "").toLowerCase() === "large") ||
    covers[0];
  return preferred?.image_url || DEFAULT_COVER_ART;
};

const fetchJson = async (path) => {
  const response = await fetchWithTimeout(path);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Request failed for ${path}: ${response.status}`);
  return response.json();
};

const fetchCoverArt = async (bookId) => {
  const response = await fetchWithTimeout(`/api/books/${bookId}/cover-art`);
  if (response.status === 404) return [];
  if (!response.ok) throw new Error(`Request failed for cover-art: ${response.status}`);
  const data = await response.json();
  return Array.isArray(data?.covers) ? data.covers : [];
};

const initAuthRedirect = async () => {
  try {
    const response = await fetchWithTimeout("/api/me");
    if (!response.ok) {
      window.location.href = `/index.html?next=${encodeURIComponent(returnTarget)}`;
      return false;
    }
    return true;
  } catch (error) {
    console.error("Auth check failed:", error);
    window.location.href = `/index.html?next=${encodeURIComponent(returnTarget)}`;
    return false;
  }
};

// ── localStorage helpers ──────────────────────────────────────────────────────

const savePlaybackPosition = (bookId, trackOrder, seconds) => {
  try {
    localStorage.setItem(`ab:pos:${bookId}:${trackOrder}`, String(seconds));
    localStorage.setItem(`ab:track:${bookId}`, String(trackOrder));
  } catch (_) {}
};

const loadPlaybackPosition = (bookId, trackOrder) => {
  try {
    const v = parseFloat(localStorage.getItem(`ab:pos:${bookId}:${trackOrder}`));
    return Number.isFinite(v) && v > 0 ? v : 0;
  } catch (_) {
    return 0;
  }
};

const loadLastTrackOrder = (bookId) => {
  try {
    const v = parseInt(localStorage.getItem(`ab:track:${bookId}`), 10);
    return Number.isFinite(v) ? v : null;
  } catch (_) {
    return null;
  }
};

let csrfTokenPromise = null;

const getCsrfToken = async () => {
  if (!csrfTokenPromise) {
    csrfTokenPromise = fetchWithTimeout("/api/csrf-token")
      .then((response) => response.json())
      .then((data) => data?.csrf_token || null)
      .catch(() => null);
  }
  return csrfTokenPromise;
};

const fetchJsonWithAuth = async (path) => {
  const response = await fetchWithTimeout(path);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`Request failed for ${path}: ${response.status}`);
  return response.json();
};

let bookCategoriesLoaded = false;
let bookCategoriesLoading = null;
let bookCategoriesButtonEl = null;
let bookCategoriesMenuEl = null;
let bookCategoriesListEl = null;
let bookCategoriesWrapEl = null;

const buildBookUrl = (book, extraParams = {}) => {
  const nextUrl = new URL("/book.html", window.location.origin);
  nextUrl.searchParams.set("book", String(book.id));
  Object.entries(extraParams).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") {
      nextUrl.searchParams.set(key, String(value));
    }
  });
  return `${nextUrl.pathname}${nextUrl.search}`;
};

const saveMediaHistory = async (bookId, mediaType, position) => {
  try {
    const csrfToken = await getCsrfToken();
    if (!csrfToken) return;
    await fetchWithTimeout(`/api/media-history/books/${bookId}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify({
        media_type: mediaType,
        position,
      }),
    });
  } catch (error) {
    console.warn("Could not persist media history:", error);
  }
};

const loadMediaHistory = async (bookId) => {
  try {
    return await fetchJsonWithAuth(`/api/media-history/books/${bookId}`);
  } catch (error) {
    console.warn("Could not load media history:", error);
    return null;
  }
};

const formatElapsed = (seconds) => {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "";
  const rounded = Math.round(total);
  const minutes = Math.floor(rounded / 60);
  const secs = rounded % 60;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
};

const describeRecentPosition = (item) => {
  const position = item?.position || {};
  if (item?.media_type === "text") {
    const chapterIndex = Number.isFinite(Number(position.selected_text_index))
      ? Number(position.selected_text_index)
      : Number.isFinite(Number(position.chapter_index))
        ? Number(position.chapter_index)
        : 0;
    return `Continue reading · Chapter ${chapterIndex + 1}`;
  }

  const trackOrder = Number.isFinite(Number(position.track_order))
    ? Number(position.track_order)
    : Number.isFinite(Number(position.selected_audio_index))
      ? Number(position.selected_audio_index)
      : 0;
  const timeLabel = formatElapsed(position.current_time);
  return timeLabel ? `Continue listening · Track ${trackOrder + 1} · ${timeLabel}` : `Continue listening · Track ${trackOrder + 1}`;
};

const buildRecentBookUrl = (item) => buildBookUrl({ id: item.book_id });

const buildHomeCategoryUrl = (category) => {
  const nextUrl = new URL("/home.html", window.location.origin);
  nextUrl.searchParams.set("category", category);
  return `${nextUrl.pathname}${nextUrl.search}`;
};

const normalizeCategoryNames = (subjects) => {
  const names = ["Featured"];
  (Array.isArray(subjects) ? subjects : []).forEach((subjectObj) => {
    const name = String(subjectObj?.name ?? "").trim();
    if (name && !names.includes(name)) {
      names.push(name);
    }
  });
  return names;
};

const loadBookCategories = async () => {
  if (!bookCategoriesLoading) {
    bookCategoriesLoading = (async () => {
      try {
        const response = await fetchWithTimeout("/api/subjects?limit=50");
        if (!response.ok) throw new Error(`Request failed for /api/subjects: ${response.status}`);
        const data = await response.json();
        return normalizeCategoryNames(data);
      } catch (error) {
        bookCategoriesLoading = null;
        throw error;
      }
    })();
  }
  return bookCategoriesLoading;
};

const closeBookCategoriesDropdown = () => {
  if (!bookCategoriesMenuEl || !bookCategoriesButtonEl) return;
  bookCategoriesMenuEl.hidden = true;
  bookCategoriesButtonEl.setAttribute("aria-expanded", "false");
};

const renderBookCategoriesDropdown = async () => {
  if (!bookCategoriesListEl) return;
  bookCategoriesListEl.innerHTML = '<div class="loading-mini">Loading categories...</div>';
  try {
    const categories = await loadBookCategories();
    bookCategoriesListEl.innerHTML = "";
    categories.forEach((category, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "nav-dropdown-item";
      item.textContent = category;
      if (index === 0) {
        item.classList.add("active");
      }
      item.addEventListener("click", () => {
        window.location.href = buildHomeCategoryUrl(category);
      });
      bookCategoriesListEl.appendChild(item);
    });
    bookCategoriesLoaded = true;
  } catch (error) {
    console.warn("Could not load categories for dropdown:", error);
    bookCategoriesListEl.innerHTML = '<div class="empty-state">Unable to load categories right now.</div>';
  }
};

const toggleBookCategoriesDropdown = async () => {
  if (!bookCategoriesMenuEl || !bookCategoriesButtonEl) return;
  const willOpen = bookCategoriesMenuEl.hidden;
  bookCategoriesMenuEl.hidden = !willOpen;
  bookCategoriesButtonEl.setAttribute("aria-expanded", String(willOpen));
  if (willOpen && !bookCategoriesLoaded) {
    await renderBookCategoriesDropdown();
  }
};

const setupBookCategoriesDropdown = () => {
  bookCategoriesWrapEl = document.querySelector(".nav-dropdown-wrap");
  bookCategoriesButtonEl = document.querySelector(".categories-btn");
  bookCategoriesMenuEl = document.querySelector(".categories-dropdown");
  bookCategoriesListEl = document.querySelector("#book-categories-list");

  if (bookCategoriesButtonEl && !bookCategoriesButtonEl.dataset.bound) {
    bookCategoriesButtonEl.dataset.bound = "1";
    bookCategoriesButtonEl.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleBookCategoriesDropdown().catch((error) => {
        console.warn("Could not toggle categories dropdown:", error);
      });
    });
  }

  if (!document.body.dataset.bookCategoriesDropdownBound) {
    document.body.dataset.bookCategoriesDropdownBound = "1";
    document.addEventListener("click", (e) => {
      if (!bookCategoriesWrapEl || !bookCategoriesWrapEl.contains(e.target)) {
        closeBookCategoriesDropdown();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeBookCategoriesDropdown();
    });
  }
};

const stopAudioPlayback = async (data) => {
  if (!data?.audioEl) return;
  if (!data.audioEl.paused) {
    data.audioEl.pause();
  }
  if (Number(data.audioEl.currentTime) > 0) {
    await saveAudioProgress(data);
  }
};

const navigateWithPlaybackShutdown = async (data, targetUrl) => {
  await stopAudioPlayback(data);
  window.location.href = targetUrl;
};

const formatTrackDuration = (seconds) => {
  const totalSeconds = Number(seconds);
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return null;
  const rounded = Math.round(totalSeconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
};

// ── Shell ─────────────────────────────────────────────────────────────────────

const renderBookShell = () => `
  <main class="home book-view">
    <header class="topbar book-topbar">
      <div class="book-nav-left">
        <a class="brand book-brand brand-link" href="/home.html" aria-label="Go to homepage">
          <span class="brand-mark"></span>
          <span class="brand-name">AudioBooks</span>
        </a>
        <nav class="book-nav-links">
          <button class="nav-link home-btn" type="button">Home</button>
          <div class="nav-dropdown-wrap">
            <button class="nav-link categories-btn" type="button" aria-expanded="false">Categories</button>
            <div class="nav-dropdown categories-dropdown" hidden>
              <div class="nav-dropdown-title">Categories</div>
              <div id="book-categories-list" class="nav-dropdown-list">
                <div class="loading-mini">Loading categories...</div>
              </div>
            </div>
          </div>
          <a class="nav-link recent-nav-btn" href="/home.html?view=recent">Recently Played</a>
        </nav>
      </div>
      <div class="book-nav-right">
        <label class="search book-search">
          <span class="sr-only">Search</span>
          <input type="search" class="book-search-input" placeholder="Search by title, author, or narrator" />
          <span class="search-icon book-search-go">Go</span>
        </label>
        <button class="account logout-btn" type="button">Logout</button>
      </div>
    </header>

    <div class="home-body book-layout" id="book-layout">
      <section class="content book-content">
        <section class="downloads book-hero">
          <img id="book-cover" class="book-cover" alt="" />
          <div class="section-header">
            <span class="eyebrow">Book page</span>
            <h1 id="book-title">Loading...</h1>
            <p id="book-byline"></p>
          </div>
        </section>

        <div class="book-tab-bar" id="book-tab-bar" role="tablist" aria-label="Book sections"></div>

        <section id="tab-about" class="book-tab-panel" role="tabpanel">
          <p id="book-summary" class="book-summary"></p>
          <div id="book-meta" class="book-meta"></div>
          <div id="book-media-tags" class="book-tags"></div>
        </section>

        <section id="tab-read" class="book-tab-panel read-layout" role="tabpanel" hidden>
          <aside class="menu book-sidebar" id="book-sidebar">
            <div>
              <h3 id="chapter-panel-title" class="book-panel-title">Chapters</h3>
              <nav id="book-chapters" class="chapter-list" aria-label="Chapter navigation"></nav>
              <div id="chapter-pagination" class="chapter-pagination"></div>
            </div>
          </aside>
          <div class="read-main" id="read-main">
            <div class="section-header">
              <h2 id="reader-title">Loading...</h2>
              <p id="reader-subtitle"></p>
            </div>
            <div id="reader-content" class="reader-content"></div>
          </div>
        </section>

        <section id="tab-listen" class="book-tab-panel" role="tabpanel" hidden>
          <div id="listen-content"></div>
        </section>

        <section id="tab-video" class="book-tab-panel" role="tabpanel" hidden>
          <div class="empty-state">Video editions are not yet available for this title.</div>
        </section>
      </section>
    </div>
  </main>
`;

app.innerHTML = renderBookShell();

// ── Chapter parsing (Read tab — plain text only) ─────────────────────────────
// splitIntoChapters() splits a plain-text book into chapter blocks by detecting
// heading lines ("CHAPTER I", "PART TWO", etc.). selectChapterBlocks() dedupes
// TOC lines from body headings by keeping the block with the most text.
// These functions are only called for content_type='text'; HTML editions skip
// splitting and are processed separately by buildHtmlReadingModel().

const CHAPTER_HEADING_PATTERN = /^(?:chapter|book|part|section|act)\s+(?:[ivxlcdm]+|\d+)\b/i;
const STRUCTURAL_PARENT_PATTERN = /^(?:book|part|volume)\s+(?:the\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\b/i;
const HTML_CHAPTER_HEADING_PATTERN = /^(?:chapter|book|part|section|act|canto|scene)\b(?:\s+(?:[ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth))?/i;
const TOC_MARKER_PATTERN = /^(?:table of\s+)?contents\.?$/i;
const TOC_PAGE_HEADER_PATTERN = /^page\.?$/i;
const TOC_ENTRY_PATTERN = /^(.*?\S)\s{2,}(\d{1,4})\s*$/;
const GUTENBERG_HEADER_LINE_PATTERN = /^the project gutenberg ebook of\b/i;
const GUTENBERG_TRAILER_LINE_PATTERN = /^(?:\*\*\*\s*)?end of (?:the|this) project gutenberg/i;
const ROMAN_VALUES = Object.freeze({ I: 1, V: 5, X: 10, L: 50, C: 100, D: 500, M: 1000 });

const isChapterHeading = (line) => {
  const stripped = String(line ?? "").trim();
  if (!stripped || stripped.length > 180) return false;
  return CHAPTER_HEADING_PATTERN.test(stripped);
};

const isStructuralParent = (line) => {
  const stripped = String(line ?? "").trim();
  if (!stripped || stripped.length > 200) return false;
  return STRUCTURAL_PARENT_PATTERN.test(stripped);
};

const romanToInt = (value) => {
  const token = String(value ?? "").trim().toUpperCase();
  if (!token || /[^IVXLCDM]/.test(token)) return null;
  let total = 0;
  let prev = 0;
  for (let i = token.length - 1; i >= 0; i -= 1) {
    const curr = ROMAN_VALUES[token[i]];
    if (!curr) return null;
    if (curr < prev) total -= curr;
    else {
      total += curr;
      prev = curr;
    }
  }
  return total > 0 ? total : null;
};

const normalizeChapterTitle = (title) => {
  const source = String(title ?? "");
  let normalized = source
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.]+$/g, "")
    .toUpperCase();

  // Canonicalize heading keys so TOC lines like
  // "CHAPTER I.--The Exterior" and body lines "CHAPTER I" dedupe together.
  const headingMatch = source.trim().match(/^(CHAPTER|BOOK|PART|SECTION|ACT)\s+([IVXLCDM]+|\d+)\b/i);
  if (headingMatch) {
    const kind = headingMatch[1].toUpperCase();
    const numToken = headingMatch[2];
    const number = /^\d+$/.test(numToken) ? Number(numToken) : romanToInt(numToken);
    if (Number.isFinite(number) && number > 0) {
      normalized = `${kind} ${number}`;
    } else {
      normalized = `${kind} ${numToken.toUpperCase()}`;
    }
  }
  return normalized;
};

const selectChapterBlocks = (blocks) => {
  if (blocks.length <= 1) return blocks;
  const bestBlocks = new Map();
  const orderedKeys = [];
  blocks.forEach((block, index) => {
    const titleKey = normalizeChapterTitle(block.title);
    if (!titleKey) return;
    const dedupKey = block.parentContext
      ? `${normalizeChapterTitle(block.parentContext)}\x00${titleKey}`
      : titleKey;
    if (!bestBlocks.has(dedupKey)) orderedKeys.push(dedupKey);
    const existing = bestBlocks.get(dedupKey);
    if (!existing || block.text.length > existing.text.length) {
      bestBlocks.set(dedupKey, { ...block, _index: index });
    }
  });
  return orderedKeys
    .map((key) => bestBlocks.get(key))
    .filter(Boolean)
    .sort((l, r) => l._index - r._index)
    .map(({ _index, ...block }) => block)
    .filter((block, index, array) => !(index === 0 && block.title === "Front Matter" && array.length > 1));
};

const normalizeTocHeading = (value) =>
  String(value ?? "")
    .normalize("NFKD")
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

const extractTocEntries = (rawText) => {
  const text = String(rawText ?? "").replace(/\r\n/g, "\n");
  if (!text) return [];

  const lines = text.split("\n");
  const maxScan = Math.min(lines.length, 5000);
  let markerIdx = -1;
  for (let i = 0; i < maxScan; i += 1) {
    if (TOC_MARKER_PATTERN.test(lines[i].trim())) {
      markerIdx = i;
      break;
    }
  }
  if (markerIdx < 0) return [];

  const titles = [];
  let pendingPrefix = "";
  for (let i = markerIdx + 1; i < Math.min(lines.length, markerIdx + 2600); i += 1) {
    const stripped = lines[i].trim();
    if (!stripped) continue;
    if (TOC_PAGE_HEADER_PATTERN.test(stripped)) continue;

    const entryMatch = stripped.match(TOC_ENTRY_PATTERN);
    if (entryMatch) {
      const fullTitle = pendingPrefix ? `${pendingPrefix} ${entryMatch[1].trim()}` : entryMatch[1].trim();
      pendingPrefix = "";
      titles.push(fullTitle);
      continue;
    }

    if (titles.length >= 8 && /^[A-Z][A-Z\s,'.\-&;:()]+$/.test(stripped)) break;

    const normalized = normalizeTocHeading(stripped);
    if (normalized && stripped.length <= 140) {
      pendingPrefix = pendingPrefix ? `${pendingPrefix} ${stripped}` : stripped;
      continue;
    }

    if (titles.length >= 8) break;
  }
  return titles;
};

const splitByTocHeadings = (cleanText, rawText) => {
  const tocTitles = extractTocEntries(rawText);
  if (tocTitles.length < 8) return null;

  const lines = cleanText.split("\n");
  const headingCandidates = [];
  for (let index = 0; index < lines.length; index += 1) {
    const stripped = lines[index].trim();
    if (!stripped || stripped.length > 220) continue;
    const normalized = normalizeTocHeading(stripped);
    if (!normalized) continue;
    headingCandidates.push({ index, normalized });
  }

  const matched = [];
  let cursor = 0;
  for (const title of tocTitles) {
    const wanted = normalizeTocHeading(title);
    if (!wanted) continue;
    let foundAt = -1;
    for (let i = cursor; i < headingCandidates.length; i += 1) {
      if (headingCandidates[i].normalized === wanted) {
        foundAt = i;
        break;
      }
    }
    if (foundAt < 0) continue;
    matched.push({ title, lineIndex: headingCandidates[foundAt].index });
    cursor = foundAt + 1;
  }

  if (matched.length < 6) return null;

  const blocks = [];
  for (let i = 0; i < matched.length; i += 1) {
    const start = matched[i].lineIndex;
    const end = i + 1 < matched.length ? matched[i + 1].lineIndex : lines.length;
    const text = lines.slice(start, end).join("\n").trim();
    if (!text) continue;
    blocks.push({ title: matched[i].title, text });
  }

  return blocks.length >= 2 ? blocks : null;
};

const splitIntoChapters = (text, rawText = "") => {
  const cleanText = String(text ?? "").replace(/\r\n/g, "\n").trim();
  if (!cleanText) return [];
  const lines = cleanText.split("\n");
  if (!lines.some(isChapterHeading)) {
    const tocBlocks = splitByTocHeadings(cleanText, rawText);
    if (tocBlocks) return tocBlocks;
    return [{ title: "Full Text", text: cleanText }];
  }
  const blocks = [];
  let currentTitle = "Front Matter";
  let currentParent = null;
  let currentLines = [];
  let sawHeading = false;
  for (const line of lines) {
    if (isChapterHeading(line)) {
      sawHeading = true;
      if (currentLines.length > 0) {
        blocks.push({ title: currentTitle, parentContext: currentParent, text: currentLines.join("\n").trim() });
      }
      currentTitle = line.trim();
      currentLines = [];
      continue;
    }
    if (isStructuralParent(line)) currentParent = line.trim();
    currentLines.push(line);
  }
  if (currentLines.length > 0) {
    blocks.push({ title: currentTitle, parentContext: currentParent, text: currentLines.join("\n").trim() });
  }
  const cleaned = blocks.filter((b) => b.text);
  if (!sawHeading || cleaned.length === 0) return [{ title: "Full Text", text: cleanText }];
  return selectChapterBlocks(cleaned);
};

const normalizeWhitespace = (value) => String(value ?? "").replace(/\s+/g, " ").trim();

const sanitizeHtmlDocument = (doc) => {
  doc.querySelectorAll("script").forEach((el) => el.remove());
  doc.querySelectorAll("*").forEach((el) => {
    [...el.attributes].forEach((attr) => {
      if (attr.name.startsWith("on")) {
        el.removeAttribute(attr.name);
      }
    });
  });
};

const stripGutenbergBoilerplate = (doc) => {
  // Gutenberg HTML commonly wraps legal boilerplate in these containers.
  [
    "#pg-header",
    "#pg-footer",
    ".pg-header",
    ".pg-footer",
    ".pg-boilerplate",
    ".x-ebookmaker-drop",
  ].forEach((selector) => {
    doc.querySelectorAll(selector).forEach((el) => el.remove());
  });

  // Fallback when explicit Gutenberg wrappers are absent.
  const topBlocks = Array.from(doc.body.children).slice(0, 12);
  topBlocks.forEach((el) => {
    const text = normalizeWhitespace(el.textContent).toLowerCase();
    if (!text) return;
    if (GUTENBERG_HEADER_LINE_PATTERN.test(text)) {
      el.remove();
    }
  });

  // Remove trailing Gutenberg license/footer if present inline.
  const candidates = Array.from(doc.body.querySelectorAll("p,div,section,article,footer"));
  const trailerNode = candidates.find((el) => GUTENBERG_TRAILER_LINE_PATTERN.test(normalizeWhitespace(el.textContent)));
  if (trailerNode && trailerNode.parentNode) {
    let cursor = trailerNode;
    while (cursor) {
      const next = cursor.nextSibling;
      cursor.remove();
      cursor = next;
    }
  }
};

const isLikelyHtmlChapterHeading = (line) => {
  const text = normalizeWhitespace(line);
  if (!text || text.length > 180) return false;
  if (/^(contents|table of contents)\b/i.test(text)) return false;
  return HTML_CHAPTER_HEADING_PATTERN.test(text);
};

const getHeadingCandidates = (doc) => {
  const headingEls = Array.from(doc.body.querySelectorAll("h1,h2,h3,h4,h5,h6"));
  const semanticEls = Array.from(
    doc.body.querySelectorAll(
      [
        "[class*='chapter']",
        "[id*='chapter']",
        "[class*='book'][class*='title']",
        "[class*='part'][class*='title']",
      ].join(",")
    )
  );
  const seen = new Set();
  return [...headingEls, ...semanticEls].filter((el) => {
    if (seen.has(el)) return false;
    seen.add(el);
    return true;
  });
};

const buildHtmlReadingModel = (htmlText, fallbackTitle) => {
  const parser = new DOMParser();
  const doc = parser.parseFromString(String(htmlText ?? ""), "text/html");
  sanitizeHtmlDocument(doc);
  stripGutenbergBoilerplate(doc);

  const candidates = getHeadingCandidates(doc).filter((el) => {
    const text = normalizeWhitespace(el.textContent);
    if (!isLikelyHtmlChapterHeading(text)) return false;
    // Keep heading-like elements, not large containers.
    if (!/^H[1-6]$/.test(el.tagName) && el.querySelector("p,div,section,article,table,ul,ol")) return false;
    return true;
  });

  const chapters = [];
  candidates.forEach((el, i) => {
    const anchorId = `ab-chapter-${i + 1}`;
    el.id = anchorId;
    chapters.push({
      title: normalizeWhitespace(el.textContent),
      isHtml: true,
      anchorId,
    });
  });

  if (chapters.length === 0) {
    chapters.push({
      title: fallbackTitle || "Full Text",
      isHtml: true,
      anchorId: null,
    });
  }

  return {
    fullHtml: doc.body.innerHTML,
    chapters,
  };
};

const extractHtmlSummaryText = (htmlText, maxLength = 400) => {
  const parser = new DOMParser();
  const doc = parser.parseFromString(String(htmlText ?? ""), "text/html");
  sanitizeHtmlDocument(doc);
  stripGutenbergBoilerplate(doc);
  return normalizeWhitespace(doc.body.textContent).slice(0, maxLength);
};

const renderDefinitionList = (items) => `
  <dl class="book-meta-grid">
    ${items.map(([label, value]) => `
      <div class="book-meta-item">
        <dt>${escapeHtml(label)}</dt>
        <dd>${escapeHtml(value ?? "Unknown")}</dd>
      </div>`).join("")}
  </dl>
`;

// ── Book state ────────────────────────────────────────────────────────────────
// createBookState() defines the full shape of the page's mutable state.
// bookState.data holds the single live instance; all render functions read from
// it rather than keeping their own local copies.
// getBookRefs() resolves all stable DOM IDs after the shell has been injected.

const createBookState = (bookId) => ({
  id: bookId,
  title: `Book ${bookId}`,
  authors: "",
  translator: "",
  year: "",
  language: "",
  downloads: "",
  subjects: [],
  summary: "",
  publicationDate: "",
  descriptionSource: "",
  textChapters: [],
  audioChapters: [],
  audio: null,
  content: null,
  htmlFullContent: "",
  hasAudio: false,
  hasVideo: false,
  mode: "about",
  selectedTextIndex: 0,
  selectedAudioIndex: 0,
  textChapterPage: 0,
  audioChapterPage: 0,
  readScrollTop: 0,
  readScrollRestored: false,
  readScrollListenerBound: false,
  readScrollSaveTimer: null,
  resume: null,
  coverArt: [],
  audioEl: null,
  readMainEl: null,
  autoplayOnLoad: false,
});

const bookState = { data: null };

const getBookRefs = () => ({
  cover:            document.querySelector("#book-cover"),
  title:            document.querySelector("#book-title"),
  byline:           document.querySelector("#book-byline"),
  tabBar:           document.querySelector("#book-tab-bar"),
  tabAbout:         document.querySelector("#tab-about"),
  tabRead:          document.querySelector("#tab-read"),
  tabListen:        document.querySelector("#tab-listen"),
  tabVideo:         document.querySelector("#tab-video"),
  summary:          document.querySelector("#book-summary"),
  bookMeta:         document.querySelector("#book-meta"),
  mediaTags:        document.querySelector("#book-media-tags"),
  chapterPanelTitle: document.querySelector("#chapter-panel-title"),
  chapters:         document.querySelector("#book-chapters"),
  chapterPagination: document.querySelector("#chapter-pagination"),
  readerTitle:      document.querySelector("#reader-title"),
  readerSubtitle:   document.querySelector("#reader-subtitle"),
  readMain:         document.querySelector("#read-main"),
  readerContent:    document.querySelector("#reader-content"),
  listenContent:    document.querySelector("#listen-content"),
});

// ── Tab navigation ────────────────────────────────────────────────────────────
// setActiveTab() is the only way to change the visible panel. It guards against
// switching to Listen/Video when those tabs are not available for this book,
// then triggers a full renderBookState() to repaint the page.

const setActiveTab = (tab) => {
  if (!bookState.data) return;
  if (tab === "listen" && !bookState.data.hasAudio) return;
  if (tab === "video" && !bookState.data.hasVideo) return;
  bookState.data.mode = tab;
  renderBookState();
};

// ── Audio player (Listen tab) ─────────────────────────────────────────────────
// The <audio> element is created once by createAudioElement() and reused for
// the lifetime of the page. renderListenPanel() builds the surrounding UI
// (now-playing header, playlist, narrator label) and mounts the audio element.
// loadTrack(index) swaps the src and seeks; timeupdate saves progress every 5 s.
// Track durations are probed lazily via ensureTrackDurations() to avoid loading
// metadata for all tracks on initial render.

const syncAudioPlayerUI = (data, refs) => {
  if (!refs.listenContent) return;
  const chapter = data.audioChapters[data.selectedAudioIndex];
  const titleEl = refs.listenContent.querySelector(".now-playing-title");
  const numEl = refs.listenContent.querySelector(".now-playing-track-num");
  const durationEl = refs.listenContent.querySelector(".np-duration");
  if (titleEl) titleEl.textContent = chapter?.chapter_title || `Track ${(chapter?.track_order ?? data.selectedAudioIndex) + 1}`;
  if (numEl) numEl.textContent = `Track ${data.selectedAudioIndex + 1} of ${data.audioChapters.length}`;
  if (durationEl) durationEl.textContent = chapter?.duration || "—";
  refs.listenContent.querySelectorAll(".playlist-item").forEach((btn, i) => {
    btn.classList.toggle("active", i === data.selectedAudioIndex);
    btn.setAttribute("aria-selected", String(i === data.selectedAudioIndex));
  });
};

const getAudioResumeTime = (data, chapter) => {
  const resume = data.resume;
  const resumePosition = resume?.media_type === "audio" ? resume.position || {} : null;
  if (resumePosition && Number(resumePosition.track_order) === Number(chapter?.track_order)) {
    const currentTime = Number(resumePosition.current_time);
    if (Number.isFinite(currentTime) && currentTime > 0) return currentTime;
  }
  const saved = loadPlaybackPosition(data.id, chapter?.track_order);
  return saved > 0 ? saved : 0;
};

const saveAudioProgress = async (data) => {
  const chapter = data.audioChapters[data.selectedAudioIndex];
  if (!chapter || !data.audioEl || data.audioEl.currentTime <= 0) return;
  const currentTime = Number(data.audioEl.currentTime);
  if (!Number.isFinite(currentTime) || currentTime <= 0) return;
  localStorage.setItem(`ab:pos:${data.id}:${chapter.track_order}`, String(currentTime));
  localStorage.setItem(`ab:track:${data.id}`, String(chapter.track_order));
  await saveMediaHistory(data.id, "audio", {
    selected_audio_index: data.selectedAudioIndex,
    track_order: chapter.track_order,
    current_time: currentTime,
    chapter_title: chapter.chapter_title || "",
  });
};

const getTextResumeState = (data) => {
  const resume = data.resume;
  const resumePosition = resume?.media_type === "text" ? resume.position || {} : null;
  if (!resumePosition) return null;
  return {
    selectedTextIndex: Number.isFinite(Number(resumePosition.selected_text_index))
      ? Number(resumePosition.selected_text_index)
      : Number.isFinite(Number(resumePosition.chapter_index))
        ? Number(resumePosition.chapter_index)
        : 0,
    scrollTop: Number.isFinite(Number(resumePosition.scroll_top)) ? Number(resumePosition.scroll_top) : 0,
  };
};

const saveTextProgress = async (data, refs, scrollTop) => {
  const chapter = data.textChapters[data.selectedTextIndex];
  const nextScrollTop = Number(scrollTop);
  if (!Number.isFinite(nextScrollTop) || nextScrollTop < 0) return;
  data.readScrollTop = nextScrollTop;
  await saveMediaHistory(data.id, "text", {
    selected_text_index: data.selectedTextIndex,
    scroll_top: nextScrollTop,
    chapter_title: chapter?.title || `Chapter ${data.selectedTextIndex + 1}`,
  });
};

const renderRecentlyPlayedPanel = async (data, refs) => {
  if (!refs.recentlyPlayedList) return;
  refs.recentlyPlayedList.innerHTML = '<div class="loading-mini">Loading recent books...</div>';

  try {
    const response = await fetchJsonWithAuth("/api/media-history/recent?limit=10");
    if (!response) {
      refs.recentlyPlayedList.innerHTML = '<div class="empty-state">No recent books yet.</div>';
      data.recentBooksLoaded = true;
      return;
    }

    const items = Array.isArray(response?.items) ? response.items : [];
    if (items.length === 0) {
      refs.recentlyPlayedList.innerHTML = '<div class="empty-state">No recent books yet.</div>';
      data.recentBooksLoaded = true;
      return;
    }

    const decorated = await Promise.all(items.map(async (item) => {
      const [descriptionResult, coverResult] = await Promise.allSettled([
        fetchJson(`/api/books/${item.book_id}/description`),
        fetchCoverArt(item.book_id),
      ]);
      const description = descriptionResult.status === "fulfilled" ? descriptionResult.value : null;
      const covers = coverResult.status === "fulfilled" ? coverResult.value : [];
      return {
        item,
        title: description?.source_title || `Book ${item.book_id}`,
        cover: pickCoverImageUrl(covers),
        meta: describeRecentPosition(item),
      };
    }));

    refs.recentlyPlayedList.innerHTML = "";
    decorated.forEach((entry) => {
      const card = document.createElement("article");
      card.className = "download-card clickable";
      card.setAttribute("role", "button");
      card.tabIndex = 0;

      const cover = document.createElement("img");
      cover.className = "download-cover";
      cover.alt = `${entry.title} cover art`;
      cover.loading = "lazy";
      cover.decoding = "async";
      cover.src = entry.cover || DEFAULT_COVER_ART;
      cover.onerror = () => {
        cover.onerror = null;
        cover.src = DEFAULT_COVER_ART;
      };

      const kicker = document.createElement("span");
      kicker.className = "card-cta";
      kicker.textContent = entry.item.media_type === "text" ? "Continue reading" : "Continue listening";

      const title = document.createElement("h3");
      title.textContent = entry.title;

      const meta = document.createElement("p");
      meta.className = "download-meta";
      meta.textContent = entry.meta;

      const foot = document.createElement("p");
      foot.className = "download-foot";
      foot.textContent = "Open book";

      const open = () => {
        window.location.href = buildRecentBookUrl(entry.item);
      };
      card.addEventListener("click", open);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });

      card.append(cover, kicker, title, meta, foot);
      refs.recentlyPlayedList.appendChild(card);
    });

    data.recentBooksLoaded = true;
  } catch (error) {
    console.warn("Could not load recent books:", error);
    refs.recentlyPlayedList.innerHTML = '<div class="empty-state">Unable to load recent books right now.</div>';
  }
};

const toggleRecentlyPlayedPanel = async (data, refs) => {
  if (!refs.recentlyPlayedPanel || !refs.recentNavBtn) return;
  const willShow = refs.recentlyPlayedPanel.hidden;
  refs.recentlyPlayedPanel.hidden = !willShow;
  refs.recentNavBtn.classList.toggle("active", willShow);
  refs.recentNavBtn.setAttribute("aria-expanded", String(willShow));
  if (willShow && !data.recentBooksLoaded) {
    await renderRecentlyPlayedPanel(data, refs);
  }
  if (willShow) {
    refs.recentlyPlayedPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
};

const updateTrackDurationLabel = (refs, index, duration) => {
  if (!refs.listenContent) return;
  const btn = refs.listenContent.querySelector(`.playlist-item[data-index="${index}"]`);
  const durationEl = btn?.querySelector(".pl-duration");
  if (durationEl && duration) {
    durationEl.textContent = duration;
  }
};

const ensureTrackDurations = async (data, refs) => {
  if (!Array.isArray(data.audioChapters) || data.audioChapters.length === 0) return;
  if (data.trackDurationLoading) return;
  data.trackDurationLoading = true;

  try {
    for (let i = 0; i < data.audioChapters.length; i += 1) {
      const chapter = data.audioChapters[i];
      if (!chapter || chapter.duration) continue;
      const duration = await new Promise((resolve) => {
        const probe = new Audio();
        probe.preload = "metadata";
        probe.addEventListener("loadedmetadata", () => {
          resolve(formatTrackDuration(probe.duration));
          probe.removeAttribute("src");
          probe.load();
        }, { once: true });
        probe.addEventListener("error", () => resolve(null), { once: true });
        probe.src = chapter.track_url;
      });
      if (duration) {
        chapter.duration = duration;
        updateTrackDurationLabel(refs, i, duration);
        if (data.mode === "listen" && i === data.selectedAudioIndex) {
          syncAudioPlayerUI(data, refs);
        }
      }
    }
  } finally {
    data.trackDurationLoading = false;
  }
};

const renderSidebarChapters = (data, refs) => {
  const textChapters = Array.isArray(data.textChapters) ? data.textChapters : [];
  const audioChapters = Array.isArray(data.audioChapters) ? data.audioChapters : [];
  const isListenMode = data.mode === "listen";
  const sidebarChapters = isListenMode ? audioChapters : textChapters;
  const sidebarSelectedIndex = isListenMode ? data.selectedAudioIndex : data.selectedTextIndex;
  const currentPage = isListenMode ? data.audioChapterPage : data.textChapterPage;
  const totalPages = Math.ceil(sidebarChapters.length / CHAPTERS_PER_PAGE);
  const pageStart = currentPage * CHAPTERS_PER_PAGE;
  const pageEnd = Math.min(pageStart + CHAPTERS_PER_PAGE, sidebarChapters.length);

  if (refs.chapterPanelTitle) {
    refs.chapterPanelTitle.textContent = isListenMode ? "Tracks" : "Chapters";
  }

  if (refs.chapters) {
    refs.chapters.innerHTML = "";
    if (sidebarChapters.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = isListenMode ? "No audio tracks found." : "No chapters detected.";
      refs.chapters.appendChild(empty);
    } else {
      sidebarChapters.slice(pageStart, pageEnd).forEach((chapter, pageIndex) => {
        const index = pageStart + pageIndex;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `chapter-item${isListenMode ? " audio-track-item" : ""}${index === sidebarSelectedIndex ? " active" : ""}`;
        if (isListenMode) {
          const label = chapter.chapter_title || `Track ${chapter.track_order ?? index + 1}`;
          const duration = chapter.duration || "—";
          btn.innerHTML = `
            <span class="chapter-item-label">${escapeHtml(label)}</span>
            <span class="chapter-item-duration">${escapeHtml(duration)}</span>
          `;
          btn.addEventListener("click", async () => {
            await saveAudioProgress(data);
            loadTrack(index);
          });
        } else {
          const label = chapter?.title || `Chapter ${index + 1}`;
          btn.textContent = label;
          btn.title = label;
          btn.addEventListener("click", async () => {
            if (refs.readMain) {
              await saveTextProgress(data, refs, refs.readMain.scrollTop);
            }
            data.selectedTextIndex = index;
            data.readScrollTop = 0;
            data.readScrollRestored = false;
            renderBookState();
          });
        }
        refs.chapters.appendChild(btn);
      });
    }
  }

  if (refs.chapterPagination) {
    refs.chapterPagination.innerHTML = "";
    if (totalPages > 1) {
      const prevBtn = document.createElement("button");
      prevBtn.type = "button";
      prevBtn.className = "chapter-page-btn";
      prevBtn.textContent = "← Prev";
      prevBtn.disabled = currentPage === 0;
      prevBtn.addEventListener("click", () => {
        if (isListenMode) data.audioChapterPage = Math.max(0, currentPage - 1);
        else data.textChapterPage = Math.max(0, currentPage - 1);
        renderBookState();
      });
      const pageInfo = document.createElement("span");
      pageInfo.className = "chapter-page-info";
      pageInfo.textContent = `${pageStart + 1}–${pageEnd} of ${sidebarChapters.length}`;
      const nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "chapter-page-btn";
      nextBtn.textContent = "Next →";
      nextBtn.disabled = currentPage >= totalPages - 1;
      nextBtn.addEventListener("click", () => {
        if (isListenMode) data.audioChapterPage = Math.min(totalPages - 1, currentPage + 1);
        else data.textChapterPage = Math.min(totalPages - 1, currentPage + 1);
        renderBookState();
      });
      refs.chapterPagination.append(prevBtn, pageInfo, nextBtn);
    }
  }
};

const loadTrack = (index) => {
  const data = bookState.data;
  if (!data || !data.audioChapters[index]) return;
  data.selectedAudioIndex = index;
  const chapter = data.audioChapters[index];
  if (data.audioEl) {
    data.audioEl.src = chapter.track_url;
    const saved = getAudioResumeTime(data, chapter);
    if (saved > 0) {
      data.audioEl.addEventListener("loadedmetadata", () => {
        if (data.audioEl) data.audioEl.currentTime = saved;
      }, { once: true });
    }
    data.audioEl.play().catch(() => {});
  }
  const refs = getBookRefs();
  syncAudioPlayerUI(data, refs);
  renderSidebarChapters(data, refs);
};

const createAudioElement = (data, refs, shouldAutoplay = false) => {
  const audio = document.createElement("audio");
  audio.className = "audio-player-el";
  audio.controls = true;
  audio.preload = "metadata";

  const chapter = data.audioChapters[data.selectedAudioIndex];
  if (chapter) {
    const saved = getAudioResumeTime(data, chapter);
    const playOnLoad = data.autoplayOnLoad || (shouldAutoplay && data.resume?.media_type === "audio");
    data.autoplayOnLoad = false;
    if (saved > 0 && !playOnLoad) {
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = saved; }, { once: true });
    }
    if (playOnLoad) {
      audio.addEventListener("loadedmetadata", () => {
        if (saved > 0) audio.currentTime = saved;
        audio.play().catch(() => {});
      }, { once: true });
    }
    audio.src = chapter.track_url;
  }

  audio.addEventListener("ended", () => {
    saveAudioProgress(data).catch(() => {});
    const next = data.selectedAudioIndex + 1;
    if (next < data.audioChapters.length) loadTrack(next);
  });

  let lastSave = 0;
  audio.addEventListener("timeupdate", () => {
    const now = Date.now();
    if (now - lastSave < 5000) return;
    lastSave = now;
    saveAudioProgress(data).catch(() => {});
  });

  data.audioEl = audio;
  const slot = refs.listenContent?.querySelector(".audio-player-el-slot");
  if (slot) slot.appendChild(audio);
};

const renderListenPanel = (data, refs) => {
  if (!refs.listenContent) return;
  const audioChapters = Array.isArray(data.audioChapters) ? data.audioChapters : [];

  if (audioChapters.length === 0) {
    refs.listenContent.innerHTML = '<div class="empty-state">No audio tracks available for this title.</div>';
    return;
  }

  if (data.audioEl) {
    syncAudioPlayerUI(data, refs);
    return;
  }

  const chapter = audioChapters[data.selectedAudioIndex] || audioChapters[0];
  const narrator = data.audio?.narrator;
  const narratorSource = data.audio?.narrator_source;
  const narratorText = narrator
    ? `Narrated by ${escapeHtml(narrator)}`
    : narratorSource === "synthesized"
    ? "Computer-generated audio"
    : "";

  refs.listenContent.innerHTML = `
    <div class="listen-layout">
      <div class="listen-player-panel">
        <div class="now-playing-header">
          <span class="now-playing-track-num">Track ${data.selectedAudioIndex + 1} of ${audioChapters.length}</span>
          <p class="now-playing-title">${escapeHtml(chapter?.chapter_title || `Track ${(chapter?.track_order ?? 0) + 1}`)}</p>
        </div>
        <div class="audio-player-el-slot"></div>
        <div class="now-playing-meta">
          <span class="np-duration">${escapeHtml(chapter?.duration || "—")}</span>
          <span class="np-format">${escapeHtml(data.audio?.audio_format?.toUpperCase() || "MP3")}</span>
          ${narratorText ? `<span class="np-narrator">${narratorText}</span>` : ""}
        </div>
      </div>
      <div class="listen-playlist">
        ${audioChapters.map((ch, i) => `
          <button type="button" class="playlist-item${i === data.selectedAudioIndex ? " active" : ""}"
                  data-index="${i}" aria-selected="${i === data.selectedAudioIndex}">
            <span class="pl-track-num">${i + 1}</span>
            <span class="pl-title">${escapeHtml(ch.chapter_title || `Track ${ch.track_order ?? i + 1}`)}</span>
            <span class="pl-duration">${escapeHtml(ch.duration || "—")}</span>
          </button>`).join("")}
      </div>
    </div>`;

  refs.listenContent.querySelectorAll(".playlist-item").forEach((btn, i) => {
    btn.addEventListener("click", () => loadTrack(i));
  });

  createAudioElement(data, refs, shouldAutoplayResume);
  ensureTrackDurations(data, refs);
};

const reflowChapterText = (rawText) => {
  const source = String(rawText ?? "").replace(/\r\n/g, "\n").trim();
  if (!source) return "";
  const paragraphs = source.split(/\n{2,}/);
  const normalized = paragraphs
    .map((paragraph) => {
      const lines = paragraph
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      if (lines.length === 0) return "";
      return lines.join(" ");
    })
    .filter(Boolean);
  return normalized.join("\n\n");
};

const renderReadPanel = (data, refs) => {
  const textChapters = Array.isArray(data.textChapters) ? data.textChapters : [];
  const chapter = textChapters[data.selectedTextIndex] || textChapters[0];
  const isHtmlContent = chapter?.isHtml === true;

  // Hide sidebar only when HTML has no chapter navigation.
  const sidebar = document.querySelector("#book-sidebar");
  if (sidebar) sidebar.hidden = isHtmlContent && textChapters.length <= 1;

  if (refs.readerTitle) {
    refs.readerTitle.textContent = isHtmlContent
      ? (textChapters.length > 1 ? (chapter?.title || data.title || "Full Text") : (data.title || "Full Text"))
      : chapter
        ? chapter.title || `Chapter ${data.selectedTextIndex + 1}`
        : "Text edition";
  }
  if (refs.readerSubtitle) refs.readerSubtitle.textContent = "";
  if (!refs.readerContent) return;
  refs.readerContent.innerHTML = "";
  refs.readerContent.style.width = "100%";
  refs.readerContent.style.maxWidth = "100%";

  if (!chapter) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No text content is available for this title.";
    refs.readerContent.appendChild(empty);
    return;
  }

  const panel = document.createElement("article");
  panel.className = "chapter-panel";
  panel.style.width = "100%";
  panel.style.maxWidth = "100%";
  panel.style.boxSizing = "border-box";
  const body = document.createElement("div");
  body.style.width = "100%";
  body.style.maxWidth = "100%";

  if (chapter.isHtml) {
    // Parse the Gutenberg HTML edition so the browser renders the book as it was
    // originally typeset — with inline illustrations (scanned engravings, maps,
    // photographs) that do not exist in the plain-text edition.
    // Image src attributes in clean_content already point to /api/books/{id}/images/
    // which redirects through a signed GCS URL to the self-hosted copy we uploaded
    // during backfill. We strip <script> tags and on* attributes as a precaution
    // even though the source is trusted public-domain Gutenberg content.
    body.className = "book-text book-html-content";
    const parser = new DOMParser();
    const doc = parser.parseFromString(data.htmlFullContent || chapter.text, "text/html");
    sanitizeHtmlDocument(doc);
    stripGutenbergBoilerplate(doc);
    while (doc.body.firstChild) {
      body.appendChild(doc.body.firstChild);
    }
  } else {
    body.className = "book-text";
    body.textContent = reflowChapterText(chapter.text) || "No chapter text available.";
  }

  panel.append(body);
  refs.readerContent.appendChild(panel);

  // For reconstructed HTML books, chapter clicks jump to heading anchors.
  if (chapter?.isHtml && chapter?.anchorId && refs.readMain) {
    window.requestAnimationFrame(() => {
      const anchor = body.querySelector(`[id="${chapter.anchorId}"]`);
      if (anchor) {
        anchor.scrollIntoView({ block: "start", behavior: "auto" });
      }
    });
    data.readScrollRestored = true;
    return;
  }

  if (refs.readMain && !data.readScrollListenerBound) {
    refs.readMain.addEventListener("scroll", () => {
      if (data.readScrollSaveTimer) {
        window.clearTimeout(data.readScrollSaveTimer);
      }
      data.readScrollSaveTimer = window.setTimeout(() => {
        saveTextProgress(data, refs, refs.readMain?.scrollTop || 0).catch(() => {});
      }, 400);
    }, { passive: true });
    data.readScrollListenerBound = true;
  }

  if (refs.readMain && !data.readScrollRestored) {
    const resumeState = getTextResumeState(data);
    const scrollTop = resumeState?.scrollTop ?? data.readScrollTop ?? 0;
    if (scrollTop > 0) {
      window.requestAnimationFrame(() => {
        if (refs.readMain) refs.readMain.scrollTop = scrollTop;
      });
    }
    data.readScrollRestored = true;
  }
};

// ── Main render cycle ─────────────────────────────────────────────────────────
// renderBookState() is the single re-render entry point. It always repaints the
// hero strip and tab bar, then delegates to the active panel's render function:
//   About  → summary text + metadata grid (author/narrator/translator/language)
//            + media-availability badges (Text / Audio / Video)
//   Read   → renderSidebarChapters() fills the left column chapter list
//             renderReadPanel() fills the right column with text or HTML content
//   Listen → renderListenPanel() builds the player UI on first call;
//             subsequent calls just sync the active track highlight via syncAudioPlayerUI()

const renderBookState = () => {
  const data = bookState.data;
  if (!data) return;

  const refs = getBookRefs();

  // Hero strip — always visible
  if (refs.title) refs.title.textContent = data.title || `Book ${data.id}`;
  if (refs.cover) {
    refs.cover.src = pickCoverImageUrl(data.coverArt);
    refs.cover.alt = `${data.title || `Book ${data.id}`} cover art`;
    refs.cover.onerror = () => { refs.cover.onerror = null; refs.cover.src = DEFAULT_COVER_ART; };
  }
  if (refs.byline) {
    const yearLabel = data.publicationDate || (data.year ? String(data.year) : "Unknown publication");
    refs.byline.textContent = `${data.authors || "Unknown author"} • ${yearLabel} • ${data.language || "Unknown language"}`;
  }

  // Tab bar
  if (refs.tabBar) {
    refs.tabBar.innerHTML = "";
    const textChapters = Array.isArray(data.textChapters) ? data.textChapters : [];
    const tabs = [
      { id: "about",  label: "About",  available: true },
      { id: "read",   label: "Read",   available: textChapters.length > 0 },
      { id: "listen", label: "Listen", available: data.hasAudio },
      { id: "video",  label: "Video",  available: data.hasVideo },
    ];
    tabs.forEach((tab) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.id = `tab-btn-${tab.id}`;
      btn.className = `book-tab-btn${data.mode === tab.id ? " active" : ""}`;
      btn.textContent = tab.label;
      btn.disabled = !tab.available;
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(data.mode === tab.id));
      btn.setAttribute("aria-controls", `tab-${tab.id}`);
      btn.addEventListener("click", () => setActiveTab(tab.id));
      refs.tabBar.appendChild(btn);
    });
  }

  // Show/hide panels
  if (refs.tabAbout)  refs.tabAbout.hidden  = data.mode !== "about";
  if (refs.tabRead)   refs.tabRead.hidden   = data.mode !== "read";
  if (refs.tabListen) refs.tabListen.hidden = data.mode !== "listen";
  if (refs.tabVideo)  refs.tabVideo.hidden  = data.mode !== "video";

  // CSS uses body[data-mode] to show/hide sidebar
  document.body.dataset.mode = data.mode;

  // About tab content
  if (data.mode === "about") {
    if (refs.summary) refs.summary.textContent = data.summary || "Description not available for this title.";

    if (refs.bookMeta) {
      const textChapters = Array.isArray(data.textChapters) ? data.textChapters : [];
      const audioChapters = Array.isArray(data.audioChapters) ? data.audioChapters : [];
      const narrator = data.audio?.narrator;
      const metaItems = [["Author", data.authors || "Unknown"]];
      if (data.translator) metaItems.push(["Translator", data.translator]);
      if (narrator) metaItems.push(["Narrator", narrator]);
      if (data.language && data.language !== "Unknown") metaItems.push(["Language", data.language]);
      if (data.publicationDate || data.year) metaItems.push(["Published", data.publicationDate || String(data.year)]);
      if (textChapters.length > 0) metaItems.push(["Chapters", String(textChapters.length)]);
      if (audioChapters.length > 0) metaItems.push(["Tracks", String(audioChapters.length)]);
      refs.bookMeta.innerHTML = renderDefinitionList(metaItems);
    }

    if (refs.mediaTags) {
      const textChapters = Array.isArray(data.textChapters) ? data.textChapters : [];
      const tags = [
        { label: "Text",  available: textChapters.length > 0 },
        { label: "Audio", available: data.hasAudio },
        { label: "Video", available: data.hasVideo },
      ];
      refs.mediaTags.innerHTML = tags.map((tag) =>
        `<span class="media-tag${tag.available ? "" : " disabled"}">${escapeHtml(tag.label)}</span>`
      ).join("");
    }
  }

  if (data.mode === "read")   { renderSidebarChapters(data, refs); renderReadPanel(data, refs); }
  if (data.mode === "listen") renderListenPanel(data, refs);
};

// ── Data loading ──────────────────────────────────────────────────────────────
// loadBookDetail() fires four API requests in parallel and assembles the result
// into a complete bookState snapshot. It also determines the initial tab:
//   - audio resume position  → mode="listen", autoplayOnLoad=true
//   - text resume position   → mode="read", selectedTextIndex restored
//   - last played track      → mode="listen" (no autoplay)
//   - otherwise              → mode="about"

const loadBookDetail = async (bookId) => {
  const snapshot = createBookState(bookId);
  const [descriptionResult, contentResult, audioResult, historyResult] = await Promise.allSettled([
    fetchJson(`/api/books/${bookId}/description`),
    fetchJson(`/api/books/${bookId}/content`),
    fetchJson(`/api/books/${bookId}/audio`),
    loadMediaHistory(bookId),
  ]);
  let coverArt = [];
  try { coverArt = await fetchCoverArt(bookId); } catch (_) {}

  const description = descriptionResult.status === "fulfilled" ? descriptionResult.value : null;
  const content     = contentResult.status  === "fulfilled" ? contentResult.value  : null;
  const audio       = audioResult.status    === "fulfilled" ? audioResult.value    : null;
  const history     = historyResult.status  === "fulfilled" ? historyResult.value  : null;

  snapshot.title            = description?.source_title    || `Book ${bookId}`;
  snapshot.authors          = description?.source_author   || "Unknown";
  snapshot.translator       = description?.translator      || "";
  snapshot.year             = parseYearFromPublication(description?.publication_date) || "";
  snapshot.language         = snapshot.language            || "Unknown";
  snapshot.summary          = description?.summary         || "";
  snapshot.publicationDate  = description?.publication_date || "";
  snapshot.descriptionSource = description?.source         || "Catalog";
  snapshot.content          = content;
  snapshot.audio            = audio;
  snapshot.coverArt         = coverArt;
  snapshot.contentType      = content?.content_type || "text";
  // Prefer html_content (server-cleaned HTML with GCS image paths) when the book
  // has inline illustrations.  Falls back to clean_content/raw_content for books
  // that only have a plain-text edition, or when html_content hasn't been backfilled yet.
  const htmlSource = content?.has_images && content?.html_content
    ? content.html_content
    : (snapshot.contentType === "html" ? (content?.clean_content || content?.raw_content) : null);
  if (htmlSource) {
    snapshot.contentType = "html";
    const htmlModel = buildHtmlReadingModel(
      htmlSource,
      description?.source_title || `Book ${bookId}`
    );
    snapshot.htmlFullContent = htmlModel.fullHtml;
    snapshot.textChapters = htmlModel.chapters;
  } else {
    snapshot.textChapters = splitIntoChapters(
      content?.clean_content || content?.raw_content || "",
      content?.raw_content || ""
    );
  }
  snapshot.audioChapters    = Array.isArray(audio?.chapters) ? audio.chapters : [];
  snapshot.hasAudio         = Boolean(audio && (audio.package_url || snapshot.audioChapters.length > 0));
  snapshot.hasVideo         = false;
  snapshot.mode             = "about";
  snapshot.resume           = history?.item || null;

  const savedPosition = snapshot.resume?.position || {};
  if (snapshot.resume?.media_type === "text" && snapshot.textChapters.length > 0) {
    const textIndex = Number.isFinite(Number(savedPosition.selected_text_index))
      ? Number(savedPosition.selected_text_index)
      : Number.isFinite(Number(savedPosition.chapter_index))
        ? Number(savedPosition.chapter_index)
        : 0;
    snapshot.selectedTextIndex = Math.max(0, Math.min(textIndex, snapshot.textChapters.length - 1));
    snapshot.readScrollTop = Number.isFinite(Number(savedPosition.scroll_top)) ? Number(savedPosition.scroll_top) : 0;
    snapshot.mode = "read";
  } else if (snapshot.resume?.media_type === "audio" && snapshot.audioChapters.length > 0) {
    const resumeTrackOrder = Number.isFinite(Number(savedPosition.track_order))
      ? Number(savedPosition.track_order)
      : NaN;
    const resumeAudioIndex = Number.isFinite(Number(savedPosition.selected_audio_index))
      ? Number(savedPosition.selected_audio_index)
      : -1;
    const resumeIndex = Number.isFinite(resumeTrackOrder)
      ? snapshot.audioChapters.findIndex((ch) => Number(ch.track_order) === resumeTrackOrder)
      : -1;
    snapshot.selectedAudioIndex = resumeIndex >= 0
      ? resumeIndex
      : Math.max(0, Math.min(resumeAudioIndex, snapshot.audioChapters.length - 1));
    snapshot.mode = "listen";
    snapshot.autoplayOnLoad = true;
  }

  if (!snapshot.summary && snapshot.textChapters.length > 0) {
    if (snapshot.contentType === "html" && snapshot.htmlFullContent) {
      snapshot.summary = extractHtmlSummaryText(snapshot.htmlFullContent, 400);
    } else {
      snapshot.summary = String(snapshot.textChapters[0].text || "").replace(/\s+/g, " ").trim().slice(0, 400);
    }
  }

  if (snapshot.mode === "about" && snapshot.audioChapters.length > 0) {
    const lastTrackOrder = loadLastTrackOrder(bookId);
    if (lastTrackOrder !== null) {
      const resumeIndex = snapshot.audioChapters.findIndex((ch) => ch.track_order === lastTrackOrder);
      if (resumeIndex >= 0) {
        snapshot.selectedAudioIndex = resumeIndex;
        snapshot.mode = "listen";
      }
    }
  }

  if (snapshot.mode === "about" && snapshot.resume?.media_type === "text") {
    snapshot.mode = "read";
  }

  return snapshot;
};

const setupNavSearch = (data) => {
  const input = document.querySelector(".book-search-input");
  const go = document.querySelector(".book-search-go");
  const brandLink = document.querySelector(".book-brand");
  const homeBtn = document.querySelector(".home-btn");
  const catBtn = document.querySelector(".categories-btn");
  const recentBtn = document.querySelector(".recent-nav-btn");

  const navigate = async (targetUrl) => {
    await navigateWithPlaybackShutdown(data, targetUrl);
  };

  const searchNavigate = async () => {
    const q = input ? input.value.trim() : "";
    await navigate(q ? `/home.html?search=${encodeURIComponent(q)}` : "/home.html");
  };
  if (input) {
    input.addEventListener("dblclick", () => {
      window.setTimeout(() => {
        input.focus();
        input.setSelectionRange(0, input.value.length);
      }, 0);
    });
    input.addEventListener("keydown", async (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        await searchNavigate();
      }
    });
  }
  if (go) go.addEventListener("click", searchNavigate);
  if (brandLink) {
    brandLink.addEventListener("click", async (e) => {
      e.preventDefault();
      await navigate("/home.html");
    });
  }
  if (homeBtn) {
    homeBtn.addEventListener("click", async () => {
      await navigate("/home.html");
    });
  }
  if (catBtn) {
    catBtn.addEventListener("click", (e) => {
      e.stopPropagation();
    });
  }
  if (recentBtn) {
    recentBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      await navigate("/home.html?view=recent");
    });
  }
};

const initBookView = async (bookId) => {

  try {
    bookState.data = await loadBookDetail(bookId);
    renderBookState();
    setupBookCategoriesDropdown();
  } catch (error) {
    console.error("Failed to load book detail:", error);
    const content = document.querySelector("#tab-about");
    if (content) content.innerHTML = '<div class="empty-state">Unable to load this book right now.</div>';
  }
};

const setupLogout = () => {
  const logoutBtn = document.querySelector(".logout-btn");
  if (!logoutBtn) return;
  logoutBtn.addEventListener("click", async () => {
    try {
      const csrfResponse = await fetch("/api/csrf-token");
      const { csrf_token } = await csrfResponse.json();
      await fetch("/api/logout", { method: "POST", headers: { "X-CSRFToken": csrf_token } });
      window.location.href = "/index.html";
    } catch (_) {
      window.location.href = "/index.html";
    }
  });
};

const init = async () => {
  if (!isBookRoute) { window.location.replace("/home.html"); return; }
  await initBookView(activeBookId);
  setupNavSearch(bookState.data);
  setupLogout();
};

(async () => {
  const authAllowed = await initAuthRedirect();
  if (authAllowed) await init();
})();
