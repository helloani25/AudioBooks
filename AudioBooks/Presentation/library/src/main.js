import "./style.css";

document.querySelector("#app").innerHTML = `
  <main class="page">
    <div class="panel">
      <div class="brand">
        <span class="brand-mark"></span>
        <span class="brand-name">AudioBooks</span>
      </div>
      <h1>Welcome back</h1>
      <p class="lead">
        Pick up right where you left off. Sync your library, track progress, and
        keep listening across devices.
      </p>
      <div class="stats">
        <div class="stat">
          <span class="stat-num">120k+</span>
          <span class="stat-label">hours streamed</span>
        </div>
        <div class="stat">
          <span class="stat-num">4.9</span>
          <span class="stat-label">listener rating</span>
        </div>
        <div class="stat">
          <span class="stat-num">24/7</span>
          <span class="stat-label">offline access</span>
        </div>
      </div>
    </div>

    <section class="card" aria-label="Sign in">
      <header class="card-header">
        <span class="eyebrow">Sign in</span>
        <h2>Continue your story</h2>
        <p>Use your account to keep your queue and bookmarks in sync.</p>
      </header>

      <form class="form" action="/home.html" method="get">
        <label class="field">
          <span>Email</span>
          <input type="email" placeholder="you@audiobooks.com" required />
        </label>

        <label class="field">
          <span>Password</span>
          <input type="password" placeholder="••••••••••" required />
        </label>

        <div class="row">
          <label class="check">
            <input type="checkbox" checked />
            <span>Remember this device</span>
          </label>
          <button type="button" class="link">Forgot password?</button>
        </div>

        <button class="primary" type="submit">Sign in</button>

        <div id="error-message" style="color: red; margin-top: 1rem; display: none;"></div>

        <div class="divider">
          <span>or</span>
        </div>

        <button type="button" class="ghost">Continue with Apple</button>

        <p class="fine">
          New here? <a class="link" href="/signup.html">Create an account</a>
        </p>
      </form>
    </section>
  </main>
`;

const loginForm = document.querySelector(".form");
const errorMessage = document.getElementById("error-message");

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorMessage.style.display = "none";

  const email = loginForm.querySelector('input[type="email"]').value;
  const password = loginForm.querySelector('input[type="password"]').value;

  try {
    // 1. Fetch CSRF token
    const csrfResponse = await fetch('/api/csrf-token');
    const { csrf_token } = await csrfResponse.json();

    // 2. Perform login
    // User requested "basic auth", so we'll use Authorization header
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf_token,
        "Authorization": "Basic " + btoa(email + ":" + password)
      }
    });

    const result = await response.json();
    if (response.ok) {
      window.location.href = result.redirect || "/home.html";
    } else {
      errorMessage.textContent = result.error || "Login failed";
      errorMessage.style.display = "block";
    }
  } catch (error) {
    errorMessage.textContent = "An error occurred. Please check if the backend is running.";
    errorMessage.style.display = "block";
    console.error("Login error:", error);
  }
});
