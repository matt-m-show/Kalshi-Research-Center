// Thin fetch wrapper. Every request carries the header the server requires on
// state-changing calls; a 401 sends the user to the login page.

const HEADERS = { "X-Requested-With": "kalshi-dashboard" };

async function request(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: { ...HEADERS } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    location.href = "/login";
    throw new Error("Login required");
  }
  const text = await resp.text();
  let data;
  try { data = JSON.parse(text); } catch { throw new Error(`Server error (${resp.status}): ${text.slice(0, 160)}`); }
  if (!resp.ok && data && data.error) throw new Error(data.error);
  return data;
}

export const get = (path) => request("GET", path);
export const post = (path, body) => request("POST", path, body);
