import "./style.css";

// Check authentication
(async () => {
  try {
    const response = await fetch('/api/me');
    if (!response.ok) {
      window.location.href = '/index.html';
    }
  } catch (error) {
    console.error('Auth check failed:', error);
    window.location.href = '/index.html';
  }
})();

document.querySelector("#app").innerHTML = `
  <main class="home">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark"></span>
        <span class="brand-name">AudioBooks</span>
      </div>
      <label class="search">
        <span class="sr-only">Search</span>
        <input type="search" placeholder="Search by title, author, or narrator" />
        <span class="search-icon">Go</span>
      </label>
      <button class="account logout-btn">Logout</button>
    </header>

    <div class="home-body">
      <aside class="menu">
        <h2>Categories</h2>
        <nav id="categories-nav">
          <div class="loading-mini">Loading...</div>
        </nav>
      </aside>

      <section class="content">
        <div class="hero">
          <div>
            <span class="eyebrow">Now listening</span>
            <h1>Build a queue that fits your mood</h1>
            <p>
              Jump into a curated mix or search the entire catalog to find your
              next listen.
            </p>
          </div>
          <div class="hero-card">
            <h3>Continue listening</h3>
            <p>The Last Lecture</p>
            <div class="progress">
              <span style="width: 62%"></span>
            </div>
            <button class="primary">Resume</button>
          </div>
        </div>

        <div class="category-grid">
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

        <section class="downloads">
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

const downloadsEl = document.querySelector("#top-downloads");
const categoriesEl = document.querySelector("#categories-nav");
const paginationEl = document.querySelector("#downloads-pagination");

let allSubjects = [];
let selectedCategory = "Featured";
let currentOffset = 0;
const LIMIT = 15;
let currentPage = 1;
let totalPages = 1;
let currentSearch = "";

const renderSidebar = () => {
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

const resetPagination = () => {
  currentPage = 1;
  currentOffset = 0;
  totalPages = 1;
};

const renderDownloads = (books, clear = false) => {
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
    card.className = "download-card";

    const title = document.createElement("h3");
    title.textContent = item.title || "Untitled";

    const meta = document.createElement("p");
    meta.className = "download-meta";
    const year = item.year ? ` • ${item.year}` : "";
    meta.textContent = `${item.authors || "Unknown"}${year} • ${item.language || "Unknown"}`;

    const subjectsDiv = document.createElement("div");
    subjectsDiv.className = "download-subjects";
    item.subjects.slice(0, 3).forEach(s => {
      const tag = document.createElement("span");
      tag.className = "subject-tag";
      tag.textContent = s;
      tag.onclick = async (e) => {
        e.stopPropagation();
        const searchInput = document.querySelector(".search input");
        if (searchInput) searchInput.value = "";
        currentSearch = "";
        selectedCategory = s;
        resetPagination();
        await loadPage();
        renderSidebar();
      };
      subjectsDiv.appendChild(tag);
    });

    const foot = document.createElement("p");
    foot.className = "download-foot";
    const downloads = item.downloads?.toLocaleString?.() ?? item.downloads ?? "0";
    foot.textContent = `${downloads} downloads`;

    card.append(title, meta, subjectsDiv, foot);
    downloadsEl.appendChild(card);
  });
};

const renderPagination = () => {
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

  paginationEl.appendChild(
    makeButton("Prev", Math.max(1, currentPage - 1), currentPage === 1)
  );

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

    paginationEl.appendChild(
      makeButton(String(totalPages), totalPages, false, currentPage === totalPages)
    );
  }

  paginationEl.appendChild(
    makeButton("Next", Math.min(totalPages, currentPage + 1), currentPage === totalPages)
  );
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
    const res = await fetch(url);
    const data = await res.json();
    if (Array.isArray(data)) {
      let countUrl = "/api/books/count";
      const params = [];
      if (subject) params.push(`subject=${encodeURIComponent(subject)}`);
      if (search) params.push(`search=${encodeURIComponent(search)}`);
      if (params.length > 0) countUrl += `?${params.join("&")}`;
      try {
        const countRes = await fetch(countUrl);
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
      total: typeof data.total === "number" ? data.total : (data.books || []).length
    };
  } catch (err) {
    console.error("Error fetching books:", err);
    return { books: [], total: 0 };
  }
};

const loadPage = async () => {
  downloadsEl.innerHTML = '<div class="loading">Loading titles...</div>';
  currentOffset = (currentPage - 1) * LIMIT;
  const subject = selectedCategory === "Featured" ? null : selectedCategory;
  const { books, total } = await fetchBooks(subject, currentSearch);
  totalPages = Math.max(1, Math.ceil(total / LIMIT));
  renderDownloads(books, true);
  renderPagination();
  updateSectionTitle();
};

const init = async () => {
  try {
    const subjectsRes = await fetch("/api/subjects?limit=50");
    if (!subjectsRes.ok) throw new Error("Failed to fetch subjects");
    allSubjects = await subjectsRes.json();
    
    await loadPage();
    renderSidebar();

    // Handle tile clicks
    const tileTrending = document.getElementById("tile-trending");
    const tileScifi = document.getElementById("tile-scifi");
    const tileFiction = document.getElementById("tile-fiction");

    if (tileTrending) {
      tileTrending.onclick = async () => {
        selectedCategory = "Featured";
        currentSearch = "";
        const searchInput = document.querySelector(".search input");
        if (searchInput) searchInput.value = "";
        resetPagination();
        await loadPage();
        renderSidebar();
      };
    }
    if (tileScifi) {
      tileScifi.onclick = async () => {
        selectedCategory = "Science fiction";
        currentSearch = "";
        const searchInput = document.querySelector(".search input");
        if (searchInput) searchInput.value = "";
        resetPagination();
        await loadPage();
        renderSidebar();
      };
    }
    if (tileFiction) {
      tileFiction.onclick = async () => {
        selectedCategory = "Fiction";
        currentSearch = "";
        const searchInput = document.querySelector(".search input");
        if (searchInput) searchInput.value = "";
        resetPagination();
        await loadPage();
        renderSidebar();
      };
    }

    // Search logic
    const searchInput = document.querySelector(".search input");
    const searchIcon = document.querySelector(".search-icon");
    
    const performSearch = async () => {
      const query = searchInput.value.trim();
      currentSearch = query;
      // Search is global, so we reset category to Featured if searching
      if (query) {
        selectedCategory = "Featured";
      }
      resetPagination();
      await loadPage();
      renderSidebar();
    };

    if (searchInput) {
      searchInput.addEventListener("keypress", async (e) => {
        if (e.key === "Enter") {
          performSearch();
        }
      });
    }
    if (searchIcon) {
      searchIcon.addEventListener("click", performSearch);
    }
  } catch (err) {
    downloadsEl.textContent = "Unable to load Gutenberg data.";
  }
};

init();

// Logout logic
const logoutBtn = document.querySelector(".logout-btn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", async () => {
    try {
      const csrfResponse = await fetch('/api/csrf-token');
      const { csrf_token } = await csrfResponse.json();

      await fetch("/api/logout", { 
        method: "POST",
        headers: {
          "X-CSRFToken": csrf_token,
        }
      });
      window.location.href = "/index.html";
    } catch (error) {
      console.error("Logout error:", error);
      window.location.href = "/index.html";
    }
  });
}
