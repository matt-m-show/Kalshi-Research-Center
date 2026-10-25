const form = document.getElementById("login");
const err = document.getElementById("err");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.classList.add("hidden");
  const resp = await fetch("/api/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "kalshi-dashboard" },
    body: JSON.stringify({ password: form.password.value }),
  });
  if (resp.ok) { location.href = "/"; return; }
  const data = await resp.json().catch(() => ({}));
  err.textContent = data.error || "Sign-in failed.";
  err.classList.remove("hidden");
});
