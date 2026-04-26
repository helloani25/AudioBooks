import "./style.css";

const app = document.querySelector("#app");
const currentUrl = new URL(window.location.href);
const bookIdParam = currentUrl.searchParams.get("book");
const isRecentView = currentUrl.searchParams.get("view") === "recent";
const initialCategoryParam = currentUrl.searchParams.get("category");
const returnTarget = `${window.location.pathname}${window.location.search}${window.location.hash}`;
if (bookIdParam) {
  window.location.replace(`/book.html?book=${encodeURIComponent(bookIdParam)}`);
}
const CACHE_VERSION = "v4";
const BOOK_CACHE_PREFIX = `audiobooks:${CACHE_VERSION}:book:`;
const COVER_ART_CACHE_PREFIX = `audiobooks:${CACHE_VERSION}:cover-art:`;
const REQUEST_TIMEOUT_MS = 15000;
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

const excerpt = (value, length = 320) => {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (text.length <= length) return text;
  return `${text.slice(0, length - 1).trim()}…`;
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

const saveCoverArtSnapshot = (bookId, covers) => {
  try {
    sessionStorage.setItem(`${COVER_ART_CACHE_PREFIX}${bookId}`, JSON.stringify(covers || []));
  } catch (error) {
    console.warn("Could not cache cover art snapshot:", error);
  }
};

const loadCoverArtSnapshot = (bookId) => {
  try {
    const raw = sessionStorage.getItem(`${COVER_ART_CACHE_PREFIX}${bookId}`);
    return raw ? JSON.parse(raw) : [];
  } catch (error) {
    console.warn("Could not read cached cover art:", error);
    return [];
  }
};

const pickCoverImageUrl = (covers) => {
  if (!Array.isArray(covers) || covers.length === 0) return DEFAULT_COVER_ART;
  const preferred = covers.find((cover) => String(cover.size_label || "").toLowerCase() === "medium")
    || covers.find((cover) => String(cover.size_label || "").toLowerCase() === "large")
    || covers[0];
  return preferred?.image_url || DEFAULT_COVER_ART;
};

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

const fetchJson = async (path) => {
  const response = await fetchWithTimeout(path);
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(`Request failed for ${path}: ${response.status}`);
  }
  return response.json();
};

const fetchCoverArt = async (bookId) => {
  const response = await fetchWithTimeout(`/api/books/${bookId}/cover-art`);
  if (response.status === 404) return [];
  if (!response.ok) {
    throw new Error(`Request failed for /api/books/${bookId}/cover-art: ${response.status}`);
  }
  const data = await response.json();
  return Array.isArray(data?.covers) ? data.covers : [];
};

const formatElapsed = (seconds) => {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "";
  const rounded = Math.round(total);
  const minutes = Math.floor(rounded / 60);
  const secs = rounded % 60;
  return `${minutes}:${String(secs).padStart(2, "0")}`;
};

const getResumeCopy = (item) => {
  const position = item?.position || {};
  if (item?.media_type === "text") {
    const chapterIndex = Number.isFinite(Number(position.selected_text_index))
      ? Number(position.selected_text_index)
      : Number.isFinite(Number(position.chapter_index))
        ? Number(position.chapter_index)
        : 0;
    const chapterLabel = `Chapter ${chapterIndex + 1}`;
    return {
      kicker: "Continue reading",
      detail: `${chapterLabel} · Reading progress saved`,
      progress: null,
    };
  }

  const trackOrder = Number.isFinite(Number(position.track_order))
    ? Number(position.track_order)
    : Number.isFinite(Number(position.selected_audio_index))
      ? Number(position.selected_audio_index)
      : 0;
  const timeLabel = formatElapsed(position.current_time);
  return {
    kicker: "Continue listening",
    detail: timeLabel ? `Track ${trackOrder + 1} · ${timeLabel}` : `Track ${trackOrder + 1}`,
    progress: null,
  };
};

const loadRecentResumeCard = async () => {
  const card = document.querySelector("#resume-card");
  const kickerEl = document.querySelector("#resume-kicker");
  const titleEl = document.querySelector("#resume-title");
  const metaEl = document.querySelector("#resume-meta");
  const progressEl = document.querySelector("#resume-progress");
  const buttonEl = document.querySelector("#resume-button");
  if (!card || !kickerEl || !titleEl || !metaEl || !progressEl || !buttonEl) return;

  try {
    const response = await fetchWithTimeout("/api/media-history/last");
    if (response.status === 404) {
      kickerEl.textContent = "Continue listening";
      titleEl.textContent = "Nothing resumed yet";
      metaEl.textContent = "Your most recent book will appear here after you start reading or listening.";
      progressEl.style.width = "12%";
      buttonEl.disabled = true;
      return;
    }
    if (!response.ok) throw new Error(`Request failed for /api/media-history/last: ${response.status}`);

    const { item } = await response.json();
    if (!item) {
      kickerEl.textContent = "Continue listening";
      titleEl.textContent = "Nothing resumed yet";
      metaEl.textContent = "Your most recent book will appear here after you start reading or listening.";
      progressEl.style.width = "12%";
      buttonEl.disabled = true;
      return;
    }

    const [descriptionResult, coverResult] = await Promise.allSettled([
      fetchJson(`/api/books/${item.book_id}/description`),
      fetchCoverArt(item.book_id),
    ]);
    const description = descriptionResult.status === "fulfilled" ? descriptionResult.value : null;
    const covers = coverResult.status === "fulfilled" ? coverResult.value : [];
    const resumeCopy = getResumeCopy(item);
    kickerEl.textContent = resumeCopy.kicker;
    titleEl.textContent = description?.source_title || `Book ${item.book_id}`;
    metaEl.textContent = resumeCopy.detail;
    progressEl.style.width = item.media_type === "text" ? "52%" : "68%";
    if (covers.length > 0) {
      card.style.setProperty("--resume-cover", `url(${pickCoverImageUrl(covers)})`);
    }
    buttonEl.disabled = false;
    const openResume = () => {
      window.location.href = buildBookUrl(
        { id: item.book_id },
        item.media_type === "audio" ? { autoplay: "1" } : {},
      );
    };
    buttonEl.onclick = (e) => {
      e.stopPropagation();
      openResume();
    };
    card.onclick = openResume;
  } catch (error) {
    console.warn("Could not load recent media history:", error);
    kickerEl.textContent = "Continue listening";
    titleEl.textContent = "Nothing resumed yet";
    metaEl.textContent = "Your most recent book will appear here after you start reading or listening.";
    progressEl.style.width = "12%";
    buttonEl.disabled = true;
  }
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

const loadRecentCategories = async () => {
  if (!recentCategoriesLoading) {
    recentCategoriesLoading = (async () => {
      try {
        const response = await fetchWithTimeout("/api/subjects?limit=50");
        if (!response.ok) throw new Error(`Request failed for /api/subjects: ${response.status}`);
        const data = await response.json();
        return normalizeCategoryNames(data);
      } catch (error) {
        recentCategoriesLoading = null;
        throw error;
      }
    })();
  }
  return recentCategoriesLoading;
};

const closeRecentCategoriesDropdown = () => {
  if (!recentCategoriesMenuEl || !recentCategoriesButtonEl) return;
  recentCategoriesMenuEl.hidden = true;
  recentCategoriesButtonEl.setAttribute("aria-expanded", "false");
};

const renderRecentCategoriesDropdown = async () => {
  if (!recentCategoriesListEl) return;
  recentCategoriesListEl.innerHTML = '<div class="loading-mini">Loading categories...</div>';
  try {
    const categories = await loadRecentCategories();
    recentCategoriesListEl.innerHTML = "";
    categories.forEach((category, index) => {
      const item = document.createElement("a");
      item.className = "nav-dropdown-item";
      item.href = buildHomeCategoryUrl(category);
      item.textContent = category;
      if (index === 0) {
        item.classList.add("active");
      }
      recentCategoriesListEl.appendChild(item);
    });
    recentCategoriesLoaded = true;
  } catch (error) {
    console.warn("Could not load categories for dropdown:", error);
    recentCategoriesListEl.innerHTML = '<div class="empty-state">Unable to load categories right now.</div>';
  }
};

const toggleRecentCategoriesDropdown = async () => {
  if (!recentCategoriesMenuEl || !recentCategoriesButtonEl) return;
  const willOpen = recentCategoriesMenuEl.hidden;
  recentCategoriesMenuEl.hidden = !willOpen;
  recentCategoriesButtonEl.setAttribute("aria-expanded", String(willOpen));
  if (willOpen && !recentCategoriesLoaded) {
    await renderRecentCategoriesDropdown();
  }
};

const setupRecentCategoriesDropdown = () => {
  recentCategoriesWrapEl = document.querySelector(".nav-dropdown-wrap");
  recentCategoriesButtonEl = document.querySelector(".categories-nav-btn");
  recentCategoriesMenuEl = document.querySelector(".categories-dropdown");
  recentCategoriesListEl = document.querySelector("#recent-categories-list");

  if (recentCategoriesButtonEl && !recentCategoriesButtonEl.dataset.bound) {
    recentCategoriesButtonEl.dataset.bound = "1";
    recentCategoriesButtonEl.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleRecentCategoriesDropdown().catch((error) => {
        console.warn("Could not toggle categories dropdown:", error);
      });
    });
  }

  if (!document.body.dataset.recentCategoriesDropdownBound) {
    document.body.dataset.recentCategoriesDropdownBound = "1";
    document.addEventListener("click", (e) => {
      if (!recentCategoriesWrapEl || !recentCategoriesWrapEl.contains(e.target)) {
        closeRecentCategoriesDropdown();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeRecentCategoriesDropdown();
    });
  }
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

const renderRecentHomeShell = () => `
  <main class="home recent-page">
    <header class="topbar book-topbar">
      <div class="book-nav-left">
        <a class="brand book-brand brand-link" href="/home.html" aria-label="Go to homepage">
          <span class="brand-mark"></span>
          <span class="brand-name">AudioBooks</span>
        </a>
        <nav class="book-nav-links">
          <button class="nav-link home-nav-btn" type="button">Home</button>
          <div class="nav-dropdown-wrap">
            <button class="nav-link categories-nav-btn" type="button" aria-expanded="false">Categories</button>
            <div class="nav-dropdown categories-dropdown" hidden>
              <div class="nav-dropdown-title">Categories</div>
              <div id="recent-categories-list" class="nav-dropdown-list">
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
          <input type="search" placeholder="Search by title, author, or narrator" />
          <span class="search-icon">Go</span>
        </label>
        <button class="account logout-btn" type="button">Logout</button>
      </div>
    </header>

    <div class="home-body recent-home-body">
      <section class="content recent-content">
        <section class="downloads recent-last-played-panel">
          <div class="section-header">
            <h2>Last played</h2>
            <p>Pick up where you left off.</p>
          </div>
          <div class="hero-card resume-card" id="resume-card">
            <span class="eyebrow" id="resume-kicker">Continue listening</span>
            <h3 id="resume-title">The Last Lecture</h3>
            <p id="resume-meta">Pick up where you left off.</p>
            <div class="progress">
              <span id="resume-progress" style="width: 62%"></span>
            </div>
            <button class="primary" type="button" id="resume-button">Resume</button>
          </div>
        </section>

        <section id="recently-played-panel" class="downloads recently-played-panel">
          <div class="section-header">
            <h2>Recently Played</h2>
            <p>Books you last listened to or read, newest first.</p>
          </div>
          <div id="recently-played-list" class="downloads-list">
            <div class="loading-mini">Loading recent books...</div>
          </div>
        </section>
      </section>
    </div>
  </main>
`;

const renderHomeShell = () => isRecentView ? renderRecentHomeShell() : `
  <main class="home">
    <header class="topbar book-topbar">
      <div class="book-nav-left">
        <a class="brand book-brand brand-link" href="/home.html" aria-label="Go to homepage">
          <span class="brand-mark"></span>
          <span class="brand-name">AudioBooks</span>
        </a>
        <nav class="book-nav-links">
          <button class="nav-link home-nav-btn" type="button">Home</button>
          <div class="nav-dropdown-wrap">
            <button class="nav-link categories-nav-btn" type="button" aria-expanded="false">Categories</button>
            <div class="nav-dropdown categories-dropdown" hidden>
              <div class="nav-dropdown-title">Categories</div>
              <div id="recent-categories-list" class="nav-dropdown-list">
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
          <input type="search" placeholder="Search by title, author, or narrator" />
          <span class="search-icon">Go</span>
        </label>
        <button class="account logout-btn" type="button">Logout</button>
      </div>
    </header>

    <div class="home-body">
      <aside class="menu" id="home-menu">
        <h2>Categories</h2>
        <nav id="categories-nav">
          <div class="loading-mini">Loading...</div>
        </nav>
      </aside>

      <section class="content">
        <div class="hero" id="home-hero">
          <div>
            <span class="eyebrow">Now listening</span>
            <h1>Build a queue that fits your mood</h1>
            <p>
              Jump into a curated mix or search the entire catalog to find your
              next listen.
            </p>
          </div>
          <div class="hero-card resume-card" id="resume-card">
            <span class="eyebrow" id="resume-kicker">Continue listening</span>
            <h3 id="resume-title">The Last Lecture</h3>
            <p id="resume-meta">Pick up where you left off.</p>
            <div class="progress">
              <span id="resume-progress" style="width: 62%"></span>
            </div>
            <button class="primary" type="button" id="resume-button">Resume</button>
          </div>
        </div>

        <section id="recently-played-panel" class="downloads recently-played-panel" hidden>
          <div class="section-header">
            <h2>Recently Played</h2>
            <p>Books you last listened to or read, newest first.</p>
          </div>
          <div id="recently-played-list" class="downloads-list">
            <div class="loading-mini">Loading recent books...</div>
          </div>
        </section>

        <div class="category-grid" id="home-category-grid">
          <div class="tile clickable" id="tile-trending">
            <h3>Trending titles</h3>
            <p>Listeners are bingeing these right now.</p>
          </div>
          <div class="tile clickable" id="tile-scifi">
            <h3>Science fiction</h3>
            <p>Explore the wonders of future and space.</p>
          </div>
          <div class="tile clickable" id="tile-fiction">
            <h3>Classic fiction</h3>
            <p>Timeless stories from historical masters.</p>
          </div>
        </div>

        <section class="downloads" id="most-downloaded-panel">
          <div class="section-header">
            <h2>Most downloaded from Gutenberg</h2>
            <p>Top titles based on total downloads in the index.</p>
          </div>
          <div id="top-downloads" class="downloads-list">Loading titles...</div>
          <div id="downloads-pagination" class="pagination" aria-label="Pagination"></div>
        </section>
      </section>
    </div>
  </main>
`;

app.innerHTML = renderHomeShell();

let downloadsEl = null;
let categoriesEl = null;
let paginationEl = null;
let allSubjects = [];
let selectedCategory = "Featured";
let currentOffset = 0;
const LIMIT = 15;
let currentPage = 1;
let totalPages = 1;
let currentSearch = "";
let recentlyPlayedLoaded = false;
let recentCategoriesLoaded = false;
let recentCategoriesLoading = null;
let recentCategoriesButtonEl = null;
let recentCategoriesMenuEl = null;
let recentCategoriesListEl = null;
let recentCategoriesWrapEl = null;
let recentlyPlayedPanelEl = null;
let recentlyPlayedListEl = null;

const resetPagination = () => {
  currentPage = 1;
  currentOffset = 0;
  totalPages = 1;
};

const openBookPage = (book) => {
  window.location.href = buildBookUrl(book);
};

const buildRecentBookUrl = (item) => buildBookUrl(
  { id: item.book_id },
  item.media_type === "audio" ? { autoplay: "1" } : {},
);

const buildHomeCategoryUrl = (category) => {
  const nextUrl = new URL("/home.html", window.location.origin);
  nextUrl.searchParams.set("category", category);
  return `${nextUrl.pathname}${nextUrl.search}`;
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

const renderRecentlyPlayedPanel = async () => {
  if (!recentlyPlayedListEl) return;
  recentlyPlayedListEl.innerHTML = '<div class="loading-mini">Loading recent books...</div>';

  try {
    const response = await fetchWithTimeout("/api/media-history/recent?limit=10");
    if (response.status === 404) {
      recentlyPlayedListEl.innerHTML = '<div class="empty-state">No recent books yet.</div>';
      recentlyPlayedLoaded = true;
      return;
    }
    if (!response.ok) throw new Error(`Request failed for /api/media-history/recent: ${response.status}`);

    const data = await response.json();
    const items = Array.isArray(data?.items) ? data.items : [];
    if (items.length === 0) {
      recentlyPlayedListEl.innerHTML = '<div class="empty-state">No recent books yet.</div>';
      recentlyPlayedLoaded = true;
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

    recentlyPlayedListEl.innerHTML = "";
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
      recentlyPlayedListEl.appendChild(card);
    });

    recentlyPlayedLoaded = true;
  } catch (error) {
    console.warn("Could not load recent media history list:", error);
    recentlyPlayedListEl.innerHTML = '<div class="empty-state">Unable to load recent books right now.</div>';
  }
};

const renderSidebar = () => {
  if (!categoriesEl) return;

  categoriesEl.innerHTML = "";

  const featuredLink = document.createElement("a");
  featuredLink.href = "#";
  featuredLink.textContent = "Featured";
  featuredLink.className = selectedCategory === "Featured" ? "active" : "";
  featuredLink.onclick = async (e) => {
    e.preventDefault();
    if (selectedCategory === "Featured" && !currentSearch) return;

    const searchInput = document.querySelector(".search input");
    if (searchInput) searchInput.value = "";
    currentSearch = "";
    selectedCategory = "Featured";
    resetPagination();
    await loadPage();
    renderSidebar();
  };
  categoriesEl.appendChild(featuredLink);

  allSubjects.forEach((subjectObj) => {
    const subject = subjectObj.name;
    const link = document.createElement("a");
    link.href = "#";
    link.textContent = subject;
    link.className = selectedCategory === subject ? "active" : "";
    link.onclick = async (e) => {
      e.preventDefault();
      if (selectedCategory === subject && !currentSearch) return;

      const searchInput = document.querySelector(".search input");
      if (searchInput) searchInput.value = "";
      currentSearch = "";
      selectedCategory = subject;
      resetPagination();
      await loadPage();
      renderSidebar();
    };
    categoriesEl.appendChild(link);
  });
};

const renderDownloads = (books, clear = false) => {
  if (!downloadsEl) return;

  if (clear) {
    downloadsEl.innerHTML = "";
  }

  if (books.length === 0) {
    if (clear) {
      downloadsEl.textContent = "No titles found for this category.";
    }
    return;
  }

  books.forEach((item) => {
    const card = document.createElement("article");
    card.className = "download-card clickable";
    card.setAttribute("role", "button");
    card.tabIndex = 0;

    const title = document.createElement("h3");
    title.textContent = item.title || "Untitled";

    const cover = document.createElement("img");
    cover.className = "download-cover";
    cover.alt = `${item.title || "Untitled"} cover art`;
    cover.loading = "lazy";
    cover.decoding = "async";
    cover.src = DEFAULT_COVER_ART;
    cover.onerror = () => {
      cover.onerror = null;
      cover.src = DEFAULT_COVER_ART;
    };
    const cachedCovers = loadCoverArtSnapshot(item.id);
    if (cachedCovers.length > 0) {
      cover.src = pickCoverImageUrl(cachedCovers);
    } else {
      (async () => {
        try {
          const covers = await fetchCoverArt(item.id);
          saveCoverArtSnapshot(item.id, covers);
          const selectedCover = pickCoverImageUrl(covers);
          if (selectedCover) {
            cover.src = selectedCover;
          }
        } catch (error) {
          console.warn(`Failed to load cover art for ${item.id}:`, error);
        }
      })();
    }

    const meta = document.createElement("p");
    meta.className = "download-meta";
    const year = item.year ? ` • ${item.year}` : "";
    meta.textContent = `${item.authors || "Unknown"}${year} • ${item.language || "Unknown"}`;

    const subjectsDiv = document.createElement("div");
    subjectsDiv.className = "download-subjects";
    const subjects = Array.isArray(item.subjects) ? item.subjects : [];
    subjects.slice(0, 3).forEach((subject) => {
      const tag = document.createElement("span");
      tag.className = "subject-tag";
      tag.textContent = subject;
      tag.onclick = async (e) => {
        e.stopPropagation();
        const searchInput = document.querySelector(".search input");
        if (searchInput) searchInput.value = "";
        currentSearch = "";
        selectedCategory = subject;
        resetPagination();
        await loadPage();
        renderSidebar();
      };
      subjectsDiv.appendChild(tag);
    });

    const foot = document.createElement("p");
    foot.className = "download-foot";
    const downloads = item.downloads?.toLocaleString?.() ?? formatNumber(item.downloads ?? 0);
    foot.textContent = `${downloads} downloads`;

    const cta = document.createElement("span");
    cta.className = "card-cta";
    cta.textContent = "Open book";

    const open = () => openBookPage(item);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });

    card.append(cover, title, meta, subjectsDiv, foot, cta);
    downloadsEl.appendChild(card);
  });
};

const renderPagination = () => {
  if (!paginationEl) return;

  paginationEl.innerHTML = "";
  if (totalPages <= 1) {
    if (totalPages === 1) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "page-btn active";
      btn.textContent = "1";
      btn.disabled = true;
      paginationEl.appendChild(btn);
    }
    return;
  }

  const makeButton = (label, page, disabled = false, active = false) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "page-btn";
    btn.textContent = label;
    btn.disabled = disabled;
    if (active) {
      btn.classList.add("active");
    }
    btn.addEventListener("click", () => {
      if (page === currentPage) return;
      currentPage = page;
      currentOffset = (currentPage - 1) * LIMIT;
      loadPage();
    });
    return btn;
  };

  const makeEllipsis = () => {
    const span = document.createElement("span");
    span.className = "page-ellipsis";
    span.textContent = "...";
    return span;
  };

  paginationEl.appendChild(makeButton("Prev", Math.max(1, currentPage - 1), currentPage === 1));

  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i += 1) {
      paginationEl.appendChild(makeButton(String(i), i, false, i === currentPage));
    }
  } else {
    paginationEl.appendChild(makeButton("1", 1, false, currentPage === 1));

    if (currentPage > 3) {
      paginationEl.appendChild(makeEllipsis());
    }

    const start = Math.max(2, currentPage - 1);
    const end = Math.min(totalPages - 1, currentPage + 1);
    for (let i = start; i <= end; i += 1) {
      paginationEl.appendChild(makeButton(String(i), i, false, i === currentPage));
    }

    if (currentPage < totalPages - 2) {
      paginationEl.appendChild(makeEllipsis());
    }

    paginationEl.appendChild(makeButton(String(totalPages), totalPages, false, currentPage === totalPages));
  }

  paginationEl.appendChild(makeButton("Next", Math.min(totalPages, currentPage + 1), currentPage === totalPages));
};

const updateSectionTitle = () => {
  const sectionTitle = document.querySelector(".downloads h2");
  const heroEl = document.querySelector(".hero");
  const categoryGridEl = document.querySelector(".category-grid");

  const isFeatured = selectedCategory === "Featured" && !currentSearch;

  if (heroEl) heroEl.style.display = isFeatured ? "grid" : "none";
  if (categoryGridEl) categoryGridEl.style.display = isFeatured ? "grid" : "none";

  if (sectionTitle) {
    sectionTitle.textContent = selectedCategory === "Featured"
      ? (currentSearch ? `Search results for "${currentSearch}"` : "Most downloaded from Gutenberg")
      : `Books in ${selectedCategory}`;
  }
};

const fetchBooks = async (subject = null, search = null) => {
  let url = `/api/books?limit=${LIMIT}&offset=${currentOffset}`;
  if (subject) {
    url += `&subject=${encodeURIComponent(subject)}`;
  }
  if (search) {
    url += `&search=${encodeURIComponent(search)}`;
  }

  try {
    const res = await fetchWithTimeout(url);
    const data = await res.json();
    if (Array.isArray(data)) {
      let countUrl = "/api/books/count";
      const params = [];
      if (subject) params.push(`subject=${encodeURIComponent(subject)}`);
      if (search) params.push(`search=${encodeURIComponent(search)}`);
      if (params.length > 0) countUrl += `?${params.join("&")}`;

      try {
        const countRes = await fetchWithTimeout(countUrl);
        const countData = await countRes.json();
        const total =
          typeof countData === "number"
            ? countData
            : typeof countData.total === "number"
              ? countData.total
              : data.length;
        return { books: data, total };
      } catch (err) {
        console.warn("Count fetch failed:", err);
        return { books: data, total: data.length };
      }
    }
    return {
      books: data.books || [],
      total: typeof data.total === "number" ? data.total : (data.books || []).length,
    };
  } catch (err) {
    console.error("Error fetching books:", err);
    return { books: [], total: 0 };
  }
};

const loadPage = async () => {
  if (!downloadsEl) return;
  downloadsEl.innerHTML = '<div class="loading">Loading titles...</div>';
  currentOffset = (currentPage - 1) * LIMIT;
  const subject = selectedCategory === "Featured" ? null : selectedCategory;
  try {
    const { books, total } = await fetchBooks(subject, currentSearch);
    totalPages = Math.max(1, Math.ceil(total / LIMIT));
    renderDownloads(books, true);
    renderPagination();
    updateSectionTitle();
  } catch (error) {
    console.error("Failed to load homepage titles:", error);
    downloadsEl.innerHTML = '<div class="empty-state">Unable to load Gutenberg titles right now.</div>';
  }
};

const initHomeView = async () => {
  downloadsEl = document.querySelector("#top-downloads");
  categoriesEl = document.querySelector("#categories-nav");
  paginationEl = document.querySelector("#downloads-pagination");
  recentlyPlayedPanelEl = document.querySelector("#recently-played-panel");
  recentlyPlayedListEl = document.querySelector("#recently-played-list");
  const searchInput = document.querySelector(".search input");
  const searchIcon = document.querySelector(".search-icon");

  try {
    await renderRecentlyPlayedPanel();
    await loadRecentResumeCard();
    setupRecentCategoriesDropdown();
    if (isRecentView) {
      const navigateSearch = async () => {
        const query = searchInput ? searchInput.value.trim() : "";
        window.location.href = query ? `/home.html?search=${encodeURIComponent(query)}` : "/home.html";
      };
      if (searchInput) {
        searchInput.addEventListener("keydown", async (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            await navigateSearch();
          }
        });
      }
      if (searchIcon) {
        searchIcon.addEventListener("click", navigateSearch);
      }
      return;
    }

    const subjectsRes = await fetchWithTimeout("/api/subjects?limit=50");
    if (!subjectsRes.ok) throw new Error("Failed to fetch subjects");
    allSubjects = await subjectsRes.json();
    if (initialCategoryParam) {
      selectedCategory = initialCategoryParam;
    }

    await loadPage();
    renderSidebar();

    const tileTrending = document.getElementById("tile-trending");
    const tileScifi = document.getElementById("tile-scifi");
    const tileFiction = document.getElementById("tile-fiction");

    const searchInput = document.querySelector(".search input");
    const searchIcon = document.querySelector(".search-icon");

    const openCategory = async (category) => {
      selectedCategory = category;
      currentSearch = "";
      if (searchInput) searchInput.value = "";
      resetPagination();
      await loadPage();
      renderSidebar();
    };

    if (tileTrending) {
      tileTrending.onclick = () => openCategory("Featured");
    }
    if (tileScifi) {
      tileScifi.onclick = () => openCategory("Science fiction");
    }
    if (tileFiction) {
      tileFiction.onclick = () => openCategory("Fiction");
    }

    const performSearch = async () => {
      const query = searchInput ? searchInput.value.trim() : "";
      currentSearch = query;
      if (query) {
        selectedCategory = "Featured";
      }
      resetPagination();
      await loadPage();
      renderSidebar();
    };

    if (searchInput) {
      searchInput.addEventListener("keydown", async (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          await performSearch();
        }
      });
    }
    if (searchIcon) {
      searchIcon.addEventListener("click", performSearch);
    }
  } catch (err) {
    if (downloadsEl) {
      downloadsEl.textContent = "Unable to load Gutenberg data.";
    }
    console.error("Home initialization failed:", err);
  }
};

const setupLogout = () => {
  const logoutBtn = document.querySelector(".logout-btn");
  if (!logoutBtn) return;

  logoutBtn.addEventListener("click", async () => {
    try {
      const csrfResponse = await fetch("/api/csrf-token");
      const { csrf_token } = await csrfResponse.json();

      await fetch("/api/logout", {
        method: "POST",
        headers: {
          "X-CSRFToken": csrf_token,
        },
      });
      window.location.href = "/index.html";
    } catch (error) {
      console.error("Logout error:", error);
      window.location.href = "/index.html";
    }
  });
};

const setupHomeNav = () => {
  const homeNavBtn = document.querySelector(".home-nav-btn");
  if (homeNavBtn) homeNavBtn.addEventListener("click", () => {
    window.location.href = "/home.html";
  });
};

const init = async () => {
  await initHomeView();
  setupHomeNav();
  setupLogout();
};

(async () => {
  const authAllowed = await initAuthRedirect();
  if (authAllowed) {
    await init();
  }
})();
