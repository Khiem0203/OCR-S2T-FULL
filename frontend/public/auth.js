// Đăng nhập API bằng tên + mật khẩu (HTTP Basic), dùng chung cho các trang
// Tên đăng nhập nhớ trên trình duyệt; mật khẩu chỉ nhớ trong phiên (đóng tab là mất)
"use strict";

(function () {
  const user = document.getElementById("apiUser");
  const pass = document.getElementById("apiPass");
  const get = (store, key) => { try { return store.getItem(key) || ""; } catch { return ""; } };
  const set = (store, key, value) => {
    try { if (value) store.setItem(key, value); else store.removeItem(key); } catch { /* bị chặn */ }
  };

  // Bỏ API key cũ còn lưu từ phiên bản trước
  set(localStorage, "apiKey", "");

  user.value = get(localStorage, "apiUser");
  pass.value = get(sessionStorage, "apiPass");
  user.addEventListener("change", () => set(localStorage, "apiUser", user.value.trim()));
  pass.addEventListener("change", () => set(sessionStorage, "apiPass", pass.value));

  window.authHeaders = function () {
    const name = user.value.trim();
    if (!name && !pass.value) return {};
    const bytes = new TextEncoder().encode(`${name}:${pass.value}`);
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    return { Authorization: "Basic " + btoa(bin) };
  };
})();
