const API_BASE = "https://wispy-butterfly-2a2d.m-oka-newdaysys.workers.dev";

const el = (id) => document.getElementById(id);

async function loadSlots() {
  const loading = el("loading");
  const select = el("slotSelect");

  try {
    const r = await fetch(`${API_BASE}/slots`);
    const data = await r.json();

    if (!r.ok) {
      throw new Error(data?.error || JSON.stringify(data));
    }

    select.innerHTML =
      `<option value="">選択してください</option>` +
      data.map(s => `<option value="${s.id}">${s.label}</option>`).join("");

    select.disabled = false;
    loading.textContent = data.length ? "空き枠を選択してください。" : "現在、空き枠がありません。";
  } catch (e) {
    loading.textContent = "空き枠の取得に失敗しました。";
    el("result").textContent = String(e);
  }
}

async function submitBooking() {
  const consent = el("consent").checked;
  const name = el("name").value.trim();
  const email = el("email").value.trim();
  const slotId = el("slotSelect").value;

  if (!consent) return alert("同意が必要です。");
  if (!name) return alert("氏名が必要です。");
  if (!email) return alert("メールが必要です。");
  if (!slotId) return alert("面談枠を選択してください。");

  const payload = { name, email, slotId };

  el("submitBtn").disabled = true;
  el("result").textContent = "送信中…";

  try {
    const r = await fetch(`${API_BASE}/book`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const data = await r.json();

    // ★重要：HTTPが200でも ok=false は失敗扱い
    if (!r.ok) {
      throw new Error(data?.error || data?.message || JSON.stringify(data));
    }
    if (data?.ok !== true) {
      throw new Error(data?.message || "予約に失敗しました（詳細不明）");
    }

    el("result").textContent = `予約完了しました。\n予約ID: ${data.bookingId}`;
    await loadSlots();
  } catch (e) {
    el("result").textContent = "送信に失敗しました。\n" + String(e);
  } finally {
    el("submitBtn").disabled = false;
  }
}

window.addEventListener("load", () => {
  loadSlots();
  el("submitBtn").addEventListener("click", submitBooking);
});
