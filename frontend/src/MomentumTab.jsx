import React, { useState, useEffect, useCallback } from "react";

/* ============================================================
   超速大盤策略 (Momentum Overdrive) — 頁面元件
   ============================================================ */

const API = "/api";

async function apiGet(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(await _errText(r));
  return r.json();
}
async function apiSend(method, path, body) {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(await _errText(r));
  return r.json();
}
async function _errText(r) {
  const text = await r.text().catch(() => "");
  try {
    return JSON.parse(text).detail || text || `發生錯誤 (${r.status})`;
  } catch {
    return text || `發生錯誤 (${r.status})`;
  }
}

const fmt = {
  money: (n) =>
    n === null || n === undefined || isNaN(n)
      ? "—"
      : new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(n),
  pct: (n) => (n === null || n === undefined || isNaN(n) ? "—" : `${(n * 100).toFixed(2)}%`),
};

// 使用者附件Excel裡的真實歷史讀值，供一鍵匯入種子資料（只需匯入一次）
const SEED_IMPORT_PAYLOAD = {
  history: [
    { date: "2025-05-12", price: 506.69 },
    { date: "2025-06-11", price: 531.2 },
    { date: "2025-07-11", price: 553.56 },
    { date: "2025-08-11", price: 572.19 },
    { date: "2025-09-11", price: 583.4 },
    { date: "2025-10-10", price: 589.5 },
    { date: "2025-11-11", price: 621.57 },
    { date: "2025-12-11", price: 625.58 },
    { date: "2026-01-09", price: 626.25 },
    { date: "2026-02-11", price: 613.11 },
    { date: "2026-03-11", price: 607.69 },
    { date: "2026-04-13", price: 617.39 },
    { date: "2026-05-12", price: 707.24 },
    { date: "2026-06-11", price: 716.33 },
    { date: "2026-07-10", price: 725.51 },
    { date: "2026-08-17", price: 729.87 },
  ],
  position: "cash",
  shares: 0,
  cash: 730,
  initial_capital: 580,
};

export default function MomentumTab() {
  const [settings, setSettings] = useState(null);
  const [state, setState] = useState(null);
  const [history, setHistory] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [s, st, h] = await Promise.all([
        apiGet("/momentum/settings"),
        apiGet("/momentum/state"),
        apiGet("/momentum/history"),
      ]);
      setSettings(s);
      setState(st);
      setHistory(h || []);
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!loaded) {
    return <div style={{ padding: 60, textAlign: "center", color: "#8B90A0" }}>載入中…</div>;
  }

  return (
    <div>
      {error && (
        <div
          style={{
            marginTop: 20,
            padding: "10px 14px",
            background: "rgba(229,72,77,0.1)",
            border: "1px solid rgba(229,72,77,0.4)",
            color: "#E5484D",
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}

      <StatusPanel state={state} history={history} settings={settings} onRefresh={refresh} />

      {history.length === 0 && (
        <ImportPanel onImported={refresh} />
      )}

      <ManualOverridePanel state={state} onOverridden={refresh} />

      <SettingsPanel settings={settings} onSaved={refresh} />

      <HistoryPanel history={history} onRefresh={refresh} />

      {history.length > 0 && <ImportPanel collapsedByDefault onImported={refresh} />}
    </div>
  );
}

/* ---------- 狀態卡片 ---------- */
function StatusPanel({ state, history, settings, onRefresh }) {
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const [checkResult, setCheckResult] = useState(null);

  const latestWithValue = [...history].reverse().find((h) => h.total_value !== null && h.total_value !== undefined);

  async function handleCheckNow(force) {
    setChecking(true);
    setCheckError("");
    setCheckResult(null);
    try {
      const row = await apiSend("POST", `/momentum/check-now${force ? "?force=true" : ""}`);
      setCheckResult(row);
      await onRefresh();
    } catch (e) {
      setCheckError(e.message);
    } finally {
      setChecking(false);
    }
  }

  const positionLabel = state?.position === "stock" ? "持有股票" : "持有現金";
  const positionTone = state?.position === "stock" ? "gold" : "muted";

  return (
    <Panel eyebrow="超速大盤策略" title="目前狀態">
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", marginBottom: 16 }}>
        <Badge tone={positionTone}>{positionLabel}</Badge>
        {state?.pending_trade && (
          <Badge tone="red">
            待換倉：{state.pending_trade.execution_date} 開盤執行「
            {state.pending_trade.target_action === "hold_stock" ? "買進" : "賣出"}」
          </Badge>
        )}
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          gap: 14,
        }}
      >
        <StatCard label="持有股數" value={fmt.money(state?.shares)} />
        <StatCard label="持有現金" value={`$${fmt.money(state?.cash)}`} />
        <StatCard label="資產總額（上次成交後）" value={latestWithValue ? `$${fmt.money(latestWithValue.total_value)}` : "—"} highlight />
        <StatCard
          label="累計報酬（上次成交後）"
          value={latestWithValue ? fmt.pct(latestWithValue.cumulative_return) : "—"}
          tone={latestWithValue && latestWithValue.cumulative_return < 0 ? "red" : "green"}
        />
        <StatCard label="最近讀值日" value={state?.last_check_date || "—"} />
        <StatCard label="交易標的 / 基準" value={settings ? `${settings.trade_symbol} / ${settings.benchmark_symbol}` : "—"} />
      </div>

      <div style={{ marginTop: 20, paddingTop: 20, borderTop: "1px solid #23262f" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <GoldButton onClick={() => handleCheckNow(false)} disabled={checking}>
            {checking ? "檢查中…" : "立即檢查一次"}
          </GoldButton>
          <GhostButton small onClick={() => handleCheckNow(true)} disabled={checking}>
            強制重新檢查本月
          </GhostButton>
          {settings?.auto_schedule_enabled ? (
            <Badge tone="green">已啟用自動排程（每月 {settings.check_day_of_month} 號檢查）</Badge>
          ) : (
            <Badge tone="muted">自動排程尚未啟用</Badge>
          )}
        </div>
        {checkError && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 10 }}>{checkError}</div>}
        {checkResult && (
          <div style={{ color: "#8B90A0", fontSize: 13, marginTop: 10 }}>
            本次讀值：{checkResult.date} 價格 ${fmt.money(checkResult.price)}，綜合動能指標{" "}
            {checkResult.momentum !== null ? fmt.pct(checkResult.momentum) : "資料不足"}，
            建議「{checkResult.action === "hold_stock" ? "持有股票" : checkResult.action === "hold_cash" ? "持有現金" : "無法判斷"}」
            {checkResult.execution_date && `，將於 ${checkResult.execution_date} 開盤換倉`}
          </div>
        )}
        <p style={{ color: "#5c6070", fontSize: 12, lineHeight: 1.7, marginTop: 14, marginBottom: 0 }}>
          每日排程會自動判斷「今天要不要換倉」與「是否輪到本月讀值」，這裡的按鈕只是方便手動測試或補跑。
          換倉訊號在讀值當天判斷，實際換倉會等到隔一個交易日用開盤價執行（跟你原本Excel的「隔天日期」邏輯一致）。
        </p>
      </div>
    </Panel>
  );
}

/* ---------- 設定面板 ---------- */
/* ---------- 手動調整目前部位 ---------- */
function ManualOverridePanel({ state, onOverridden }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState(state?.position || "cash");
  const [shares, setShares] = useState(state?.shares || 0);
  const [cash, setCash] = useState(state?.cash || 0);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (state) {
      setPosition(state.position);
      setShares(state.shares);
      setCash(state.cash);
    }
  }, [state]);

  async function submit() {
    setBusy(true);
    setMsg("");
    try {
      await apiSend("PUT", "/momentum/state", { position, shares, cash, note: note || null });
      setMsg("✓ 已更新目前部位");
      setNote("");
      await onOverridden();
    } catch (e) {
      setMsg("✗ " + e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Panel eyebrow="人工覆寫" title="手動調整目前部位">
        <p style={{ color: "#8B90A0", fontSize: 13, marginTop: 0, marginBottom: 12, lineHeight: 1.7 }}>
          如果你像之前一樣，主觀判斷要偏離系統建議自行進出場，用這裡直接告訴系統你實際的部位，
          不用整批重新匯入歷史資料。
        </p>
        <GhostButton small onClick={() => setOpen(true)}>
          展開手動調整
        </GhostButton>
      </Panel>
    );
  }

  return (
    <Panel eyebrow="人工覆寫 · 會清除待執行的自動換倉" title="手動調整目前部位">
      <p style={{ color: "#8B90A0", fontSize: 13, marginTop: 0, marginBottom: 14, lineHeight: 1.7 }}>
        只在你主觀決定偏離系統建議、自行進出場時使用；一般情況請讓系統依規則自動判斷。
        送出後會覆蓋目前的股數/現金/部位狀態，並清除任何待執行的自動換倉。
      </p>
      <div style={{ display: "flex", gap: 20, flexWrap: "wrap", alignItems: "flex-end" }}>
        <Field label="部位">
          <select value={position} onChange={(e) => setPosition(e.target.value)} style={{ ...inputStyle, cursor: "pointer" }}>
            <option value="cash">持有現金</option>
            <option value="stock">持有股票</option>
          </select>
        </Field>
        <Field label="股數">
          <input type="number" value={shares} onChange={(e) => setShares(parseFloat(e.target.value) || 0)} style={inputStyle} />
        </Field>
        <Field label="現金 (USD)">
          <input type="number" value={cash} onChange={(e) => setCash(parseFloat(e.target.value) || 0)} style={inputStyle} />
        </Field>
      </div>
      <div style={{ marginTop: 14 }}>
        <Field label="備註（為什麼要手動override，會附加在最新一筆歷史紀錄上）">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            style={{ ...inputStyle, width: "100%" }}
            placeholder="例：雖然動能指標為正，但主觀判斷市場過熱，選擇手動出場"
          />
        </Field>
      </div>
      {msg && <div style={{ fontSize: 13, marginTop: 10, color: msg.startsWith("✓") ? "#4CC98A" : "#E5484D" }}>{msg}</div>}
      <div style={{ marginTop: 14, display: "flex", gap: 10 }}>
        <GoldButton onClick={submit} disabled={busy}>
          {busy ? "更新中…" : "更新部位"}
        </GoldButton>
        <GhostButton small onClick={() => setOpen(false)}>
          收合
        </GhostButton>
      </div>
    </Panel>
  );
}

function SettingsPanel({ settings, onSaved }) {
  const [draft, setDraft] = useState(settings);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [testResult, setTestResult] = useState("");
  const [testing, setTesting] = useState(false);
  const [fetchingChatId, setFetchingChatId] = useState(false);
  const [fetchChatIdMsg, setFetchChatIdMsg] = useState("");

  useEffect(() => {
    setDraft(settings);
  }, [settings]);

  if (!draft) return null;

  async function save() {
    setSaving(true);
    setSaveError("");
    try {
      await apiSend("PUT", "/momentum/settings", draft);
      await onSaved();
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function testTelegram() {
    setTesting(true);
    setTestResult("");
    try {
      await apiSend("POST", "/momentum/telegram-test");
      setTestResult("✓ 測試訊息已送出，請檢查 Telegram");
    } catch (e) {
      setTestResult("✗ " + e.message);
    } finally {
      setTesting(false);
    }
  }

  async function fetchChatId() {
    setFetchingChatId(true);
    setFetchChatIdMsg("");
    try {
      const result = await apiSend("POST", "/momentum/telegram-fetch-chat-id", { bot_token: draft.telegram_bot_token });
      setDraft({ ...draft, telegram_chat_id: result.chat_id });
      setFetchChatIdMsg(`✓ 已自動填入，偵測到帳號：${result.display_name}`);
    } catch (e) {
      setFetchChatIdMsg("✗ " + e.message);
    } finally {
      setFetchingChatId(false);
    }
  }

  return (
    <Panel eyebrow="設定" title="策略參數與 Telegram 通知">
      <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
        <Field label="基準ETF（算動能指標用）">
          <input
            value={draft.benchmark_symbol}
            onChange={(e) => setDraft({ ...draft, benchmark_symbol: e.target.value.toUpperCase() })}
            style={inputStyle}
          />
        </Field>
        <Field label="交易標的">
          <input
            value={draft.trade_symbol}
            onChange={(e) => setDraft({ ...draft, trade_symbol: e.target.value.toUpperCase() })}
            style={inputStyle}
          />
        </Field>
        <Field label="本金 (USD)">
          <input
            type="number"
            value={draft.initial_capital}
            onChange={(e) => setDraft({ ...draft, initial_capital: parseFloat(e.target.value) || 0 })}
            style={inputStyle}
          />
        </Field>
        <Field label="每月檢查日（幾號）">
          <input
            type="number"
            min="1"
            max="28"
            value={draft.check_day_of_month}
            onChange={(e) => setDraft({ ...draft, check_day_of_month: parseInt(e.target.value) || 1 })}
            style={inputStyle}
          />
        </Field>
      </div>

      <div style={{ marginTop: 16 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#C7CAD4", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={draft.auto_schedule_enabled}
            onChange={(e) => setDraft({ ...draft, auto_schedule_enabled: e.target.checked })}
          />
          啟用自動排程（伺服器每天自動檢查，輪到檢查日/有待執行換倉時會自動處理）
        </label>
      </div>

      <div style={{ marginTop: 20, paddingTop: 20, borderTop: "1px solid #23262f" }}>
        <MiniLabel>Telegram 通知</MiniLabel>
        <p style={{ color: "#8B90A0", fontSize: 13, lineHeight: 1.7, margin: "4px 0 12px" }}>
          需要出場或進場時（訊號轉換）、以及實際換倉完成時會發送通知。設定步驟：
        </p>
        <ol style={{ color: "#8B90A0", fontSize: 13, lineHeight: 1.9, margin: "0 0 14px", paddingLeft: 20 }}>
          <li>
            在 Telegram 搜尋 <span style={{ color: "#E8C275" }}>@BotFather</span>，傳 <code style={{ color: "#E8C275" }}>/newbot</code>{" "}
            建立一個新 Bot，取得 <strong style={{ color: "#EDEFF3" }}>Bot Token</strong>，貼到下方欄位
          </li>
          <li>
            在 Telegram 搜尋你剛建立的 Bot，傳一則訊息給它（例如輸入 <code style={{ color: "#E8C275" }}>/start</code>）
          </li>
          <li>
            回到這裡按「<strong style={{ color: "#EDEFF3" }}>自動取得 Chat ID</strong>」，會自動幫你填好，不用自己組API網址查
          </li>
        </ol>
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap", alignItems: "flex-end" }}>
          <Field label="Bot Token">
            <input
              type="password"
              value={draft.telegram_bot_token}
              onChange={(e) => setDraft({ ...draft, telegram_bot_token: e.target.value })}
              style={{ ...inputStyle, width: 260 }}
              placeholder="123456:ABC-DEF..."
            />
          </Field>
          <Field label="Chat ID">
            <input
              value={draft.telegram_chat_id}
              onChange={(e) => setDraft({ ...draft, telegram_chat_id: e.target.value })}
              style={inputStyle}
              placeholder="123456789"
            />
          </Field>
          <GhostButton small onClick={fetchChatId} disabled={fetchingChatId || !draft.telegram_bot_token}>
            {fetchingChatId ? "偵測中…" : "自動取得 Chat ID"}
          </GhostButton>
        </div>
        {fetchChatIdMsg && (
          <div style={{ fontSize: 12, marginTop: 8, color: fetchChatIdMsg.startsWith("✓") ? "#4CC98A" : "#E5484D" }}>
            {fetchChatIdMsg}
          </div>
        )}
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#C7CAD4", cursor: "pointer", marginTop: 14 }}>
          <input
            type="checkbox"
            checked={draft.telegram_enabled}
            onChange={(e) => setDraft({ ...draft, telegram_enabled: e.target.checked })}
          />
          啟用 Telegram 通知
        </label>
        <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 12 }}>
          <GhostButton small onClick={testTelegram} disabled={testing}>
            {testing ? "傳送中…" : "傳送測試訊息"}
          </GhostButton>
          {testResult && (
            <span style={{ fontSize: 12, color: testResult.startsWith("✓") ? "#4CC98A" : "#E5484D" }}>{testResult}</span>
          )}
        </div>
      </div>

      {saveError && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 14 }}>{saveError}</div>}
      <div style={{ marginTop: 16 }}>
        <GoldButton onClick={save} disabled={saving}>
          {saving ? "儲存中…" : "儲存設定"}
        </GoldButton>
      </div>
    </Panel>
  );
}

/* ---------- 匯入歷史資料 ---------- */
function ImportPanel({ onImported, collapsedByDefault }) {
  const [open, setOpen] = useState(!collapsedByDefault);
  const [text, setText] = useState(JSON.stringify(SEED_IMPORT_PAYLOAD, null, 2));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  async function doImport() {
    setBusy(true);
    setMsg("");
    try {
      const payload = JSON.parse(text);
      const result = await apiSend("POST", "/momentum/import", payload);
      setMsg(`✓ 已匯入 ${result.imported_rows} 筆歷史紀錄`);
      await onImported();
    } catch (e) {
      setMsg("✗ " + e.message);
    } finally {
      setBusy(false);
    }
  }

  if (collapsedByDefault && !open) {
    return (
      <Panel eyebrow="進階" title="重新匯入歷史資料">
        <p style={{ color: "#8B90A0", fontSize: 13, marginTop: 0, marginBottom: 12 }}>
          已有歷史紀錄。如果需要用新的資料整個覆蓋重來，可以在這裡重新匯入。
        </p>
        <GhostButton small onClick={() => setOpen(true)}>
          展開匯入工具
        </GhostButton>
      </Panel>
    );
  }

  return (
    <Panel eyebrow={collapsedByDefault ? "進階 · 會覆蓋現有紀錄" : "STEP 01 · 匯入歷史資料"} title="匯入歷史讀值與目前部位">
      <p style={{ color: "#8B90A0", fontSize: 13, lineHeight: 1.7, marginTop: 0 }}>
        {collapsedByDefault
          ? "下方已預填你原本Excel的紀錄格式，若要重新匯入請自行編輯後送出，會整個覆蓋現有的歷史紀錄與目前部位。"
          : "已經幫你把附件Excel裡的歷史讀值與目前部位（持有現金 $730，本金 $580）整理成下方JSON，確認無誤後按「匯入」即可，之後系統就會自動接手，不用再手動維護Excel。"}
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        style={{
          width: "100%",
          minHeight: 220,
          background: "#0d0e12",
          border: "1px solid #2a2e38",
          color: "#EDEFF3",
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: 12,
          padding: 12,
          boxSizing: "border-box",
          resize: "vertical",
        }}
      />
      {msg && <div style={{ fontSize: 13, marginTop: 10, color: msg.startsWith("✓") ? "#4CC98A" : "#E5484D" }}>{msg}</div>}
      <div style={{ marginTop: 12 }}>
        <GoldButton onClick={doImport} disabled={busy}>
          {busy ? "匯入中…" : "匯入"}
        </GoldButton>
      </div>
    </Panel>
  );
}

/* ---------- 歷史紀錄表 ---------- */
function HistoryPanel({ history, onRefresh }) {
  const rows = [...history].reverse();
  return (
    <Panel eyebrow={`共 ${history.length} 筆讀值`} title="歷史紀錄">
      {rows.length === 0 ? (
        <EmptyState text="尚無歷史紀錄，請先匯入資料。" />
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr>
                {["讀值日", "基準價", "1個月", "3個月", "6個月", "綜合動能", "建議動作", "換倉日", "換倉價", "股數", "現金", "資產總額", "累計報酬", "備註"].map((h) => (
                  <th
                    key={h}
                    style={{
                      textAlign: "left",
                      padding: "8px 10px",
                      color: "#8B90A0",
                      fontWeight: 500,
                      borderBottom: "1px solid #23262f",
                      whiteSpace: "nowrap",
                      fontFamily: "'JetBrains Mono', monospace",
                      fontSize: 11,
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((h, i) => (
                <tr key={i} style={{ borderBottom: "1px solid #1c1f27" }}>
                  <td style={cellStyle}>{h.date}</td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>${fmt.money(h.price)}</td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>{h.perf_1m !== null ? fmt.pct(h.perf_1m) : "—"}</td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>{h.perf_3m !== null ? fmt.pct(h.perf_3m) : "—"}</td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>{h.perf_6m !== null ? fmt.pct(h.perf_6m) : "—"}</td>
                  <td
                    style={{
                      ...cellStyle,
                      fontFamily: "'JetBrains Mono', monospace",
                      color: h.momentum === null ? "#5c6070" : h.momentum >= 0 ? "#4CC98A" : "#F0787C",
                      fontWeight: 700,
                    }}
                  >
                    {h.momentum !== null ? fmt.pct(h.momentum) : "—"}
                  </td>
                  <td style={cellStyle}>
                    {h.action === "hold_stock" ? (
                      <Badge tone="gold">持有股票</Badge>
                    ) : h.action === "hold_cash" ? (
                      <Badge tone="muted">持有現金</Badge>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td style={cellStyle}>{h.execution_date || "—"}</td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>
                    {h.execution_price !== null && h.execution_price !== undefined ? `$${fmt.money(h.execution_price)}` : "—"}
                  </td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>
                    {h.shares_after !== null && h.shares_after !== undefined ? fmt.money(h.shares_after) : "—"}
                  </td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>
                    {h.cash_after !== null && h.cash_after !== undefined ? `$${fmt.money(h.cash_after)}` : "—"}
                  </td>
                  <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>
                    {h.total_value !== null && h.total_value !== undefined ? `$${fmt.money(h.total_value)}` : "—"}
                  </td>
                  <td
                    style={{
                      ...cellStyle,
                      fontFamily: "'JetBrains Mono', monospace",
                      color: h.cumulative_return === null ? "#5c6070" : h.cumulative_return >= 0 ? "#4CC98A" : "#F0787C",
                    }}
                  >
                    {h.cumulative_return !== null && h.cumulative_return !== undefined ? fmt.pct(h.cumulative_return) : "—"}
                  </td>
                  <td style={{ ...cellStyle, minWidth: 160 }}>
                    <NoteCell date={h.date} note={h.note} onSaved={onRefresh} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function NoteCell({ date, note, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(note || "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(note || "");
  }, [note]);

  async function save() {
    setSaving(true);
    try {
      await apiSend("PUT", `/momentum/history/${date}/note`, { note: draft });
      setEditing(false);
      await onSaved();
    } catch {
      /* 忽略，維持編輯狀態讓使用者重試 */
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <div style={{ display: "flex", gap: 6 }}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          style={{ ...inputStyle, width: 160, fontSize: 11 }}
          autoFocus
        />
        <GhostButton small onClick={save} disabled={saving}>
          {saving ? "…" : "存"}
        </GhostButton>
      </div>
    );
  }

  return (
    <div
      onClick={() => setEditing(true)}
      style={{ cursor: "pointer", color: note ? "#C7CAD4" : "#5c6070", fontSize: 12, minHeight: 18 }}
      title="點擊編輯備註"
    >
      {note || "＋加註記"}
    </div>
  );
}

/* ---------- 共用小元件（風格與主 App 一致） ---------- */
function Panel({ children, title, eyebrow }) {
  return (
    <section
      style={{
        background: "#14161c",
        border: "1px solid #23262f",
        clipPath: "polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px))",
        padding: 24,
        marginTop: 20,
      }}
    >
      {(title || eyebrow) && (
        <div style={{ marginBottom: 16 }}>
          {eyebrow && (
            <div style={{ fontSize: 11, letterSpacing: 2, color: "#8B7A46", fontFamily: "'JetBrains Mono', monospace", marginBottom: 4 }}>
              {eyebrow}
            </div>
          )}
          {title && (
            <h2 style={{ fontFamily: "'Noto Serif TC', serif", fontWeight: 700, fontSize: 19, margin: 0, color: "#EDEFF3" }}>{title}</h2>
          )}
        </div>
      )}
      {children}
    </section>
  );
}

function StatCard({ label, value, tone, highlight }) {
  const toneColor = tone === "red" ? "#E5484D" : tone === "green" ? "#2FA36B" : highlight ? "#F1E4C6" : "#EDEFF3";
  return (
    <div style={{ border: `1px solid ${highlight ? "#C9A15A" : "#23262f"}`, background: "#0d0e12", padding: 16 }}>
      <div style={{ fontSize: 11, color: "#8B90A0", marginBottom: 8 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: toneColor }}>{value}</div>
    </div>
  );
}

function Badge({ children, tone }) {
  const colors = {
    gold: { bg: "rgba(201,161,90,0.15)", fg: "#E8C275" },
    green: { bg: "rgba(47,163,107,0.15)", fg: "#4CC98A" },
    red: { bg: "rgba(229,72,77,0.15)", fg: "#F0787C" },
    muted: { bg: "rgba(139,144,160,0.12)", fg: "#8B90A0" },
  };
  const c = colors[tone] || colors.muted;
  return (
    <span style={{ background: c.bg, color: c.fg, fontSize: 11, fontWeight: 700, padding: "6px 10px", whiteSpace: "nowrap" }}>
      {children}
    </span>
  );
}

function GoldButton({ children, onClick, disabled }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        background: disabled ? "#7a6a45" : "#C9A15A",
        color: "#14161c",
        border: "none",
        padding: "10px 20px",
        fontWeight: 700,
        fontSize: 13,
        cursor: disabled ? "not-allowed" : "pointer",
        clipPath: "polygon(0 0, calc(100% - 8px) 0, 100% 100%, 8px 100%)",
      }}
    >
      {children}
    </button>
  );
}

function GhostButton({ children, onClick, small, disabled }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        background: "none",
        border: "1px solid #3a3e4a",
        color: disabled ? "#5c6070" : "#C7CAD4",
        padding: small ? "5px 10px" : "10px 18px",
        fontWeight: 500,
        fontSize: small ? 12 : 13,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {children}
    </button>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <MiniLabel>{label}</MiniLabel>
      {children}
    </div>
  );
}

function MiniLabel({ children }) {
  return (
    <div style={{ fontSize: 11, color: "#8B90A0", marginBottom: 6, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 0.5 }}>
      {children}
    </div>
  );
}

const inputStyle = {
  background: "#0d0e12",
  border: "1px solid #2a2e38",
  color: "#EDEFF3",
  padding: "8px 10px",
  fontSize: 13,
  fontFamily: "'JetBrains Mono', monospace",
  boxSizing: "border-box",
  width: 140,
};

const cellStyle = { padding: "8px 10px", verticalAlign: "top" };

function EmptyState({ text }) {
  return (
    <div style={{ border: "1px dashed #2a2e38", padding: "36px 20px", textAlign: "center", color: "#5c6070", fontSize: 13 }}>
      {text}
    </div>
  );
}
