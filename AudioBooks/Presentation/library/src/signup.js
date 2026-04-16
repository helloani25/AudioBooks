import "./style.css";

document.querySelector("#app").innerHTML = `
  <main class="page">
    <div class="panel">
      <div class="brand">
        <span class="brand-mark"></span>
        <span class="brand-name">AudioBooks</span>
      </div>
      <h1>Start listening</h1>
      <p class="lead">
        Build your library in minutes. Track progress, save highlights, and
        keep your favorites synced across devices.
      </p>
      <div class="stats">
        <div class="stat">
          <span class="stat-num">1M+</span>
          <span class="stat-label">titles available</span>
        </div>
        <div class="stat">
          <span class="stat-num">15 min</span>
          <span class="stat-label">average setup</span>
        </div>
        <div class="stat">
          <span class="stat-num">30 day</span>
          <span class="stat-label">free trial</span>
        </div>
      </div>
    </div>

    <section class="card" aria-label="Sign up">
      <header class="card-header">
        <span class="eyebrow">Sign up</span>
        <h2>Create your account</h2>
        <p>Join your next listen in one step.</p>
      </header>

      <form class="form">
        <label class="field">
          <span>Full name</span>
          <input type="text" placeholder="Alex Morgan" required />
        </label>

        <label class="field">
          <span>Email</span>
          <input type="email" placeholder="you@audiobooks.com" required />
        </label>

        <label class="field">
          <span>Password</span>
          <input type="password" placeholder="••••••••••" required />
        </label>

        <label class="field">
          <span>Confirm password</span>
          <input type="password" placeholder="••••••••••" required />
        </label>

        <label class="check">
          <input type="checkbox" required />
          <span>I agree to the Terms and Privacy Policy</span>
        </label>

        <button class="primary" type="submit">Create account</button>

        <div id="error-message" style="color: red; margin-top: 1rem; display: none;"></div>

        <p class="fine">
          Already have an account? <a class="link" href="/index.html">Sign in</a>
        </p>
      </form>
    </section>
  </main>
`;

// Add submission logic
const signupForm = document.querySelector(".form");
const errorMessage = document.getElementById("error-message");

signupForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorMessage.style.display = "none";

  const fullName = signupForm.querySelector('input[type="text"]').value;
  const email = signupForm.querySelector('input[type="email"]').value;
  const password = signupForm.querySelectorAll('input[type="password"]')[0].value;
  const confirmPassword = signupForm.querySelectorAll('input[type="password"]')[1].value;

  if (password !== confirmPassword) {
    errorMessage.textContent = "Passwords do not match";
    errorMessage.style.display = "block";
    return;
  }

  try {
    // 1. Fetch CSRF token first
    const csrfResponse = await fetch('/api/csrf-token');
    const { csrf_token } = await csrfResponse.json();

    // 2. Perform signup
    const response = await fetch("/api/signup", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf_token,
      },
      body: JSON.stringify({ email, password, confirm_password: confirmPassword, full_name: fullName }),
    });

    const result = await response.json();
    if (response.ok) {
      alert("Account created! Please sign in.");
      window.location.href = "/index.html";
    } else {
      errorMessage.textContent = result.error || "Signup failed";
      errorMessage.style.display = "block";
    }
  } catch (error) {
    errorMessage.textContent = "An error occurred. Please check if the backend is running.";
    errorMessage.style.display = "block";
    console.error("Signup error:", error);
  }
});
