import React, { useState, useEffect, useMemo, useCallback } from "react";

/* ============================================================
   黑鑽選股儀表板 — Black Diamond Screener Dashboard
   設計語彙：切割寶石的刻面（facet）作為視覺簽名元素。
   深曜石背景 + 琥珀金強調色（代表被發掘的價值），
   漲跌採台灣紅漲綠跌慣例。

   資料透過後端 FastAPI (/api/*) 讀寫，非瀏覽器端 localStorage。
   ============================================================ */

const API = "/api";

async function apiGet(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`GET ${path} 失敗 (${r.status})`);
  return r.json();
}
async function apiSend(method, path, body) {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`${method} ${path} 失敗 (${r.status}) ${text}`);
  }
  return r.json();
}

/* ---------- 刻面圖形元件 (Signature element) ---------- */
function Facet({ className = "", size = 10, tone = "gold" }) {
  const colors = {
    gold: "#C9A15A",
    red: "#E5484D",
    green: "#2FA36B",
    muted: "#4A4E5A",
  };
  return (
    <span
      className={className}
      style={{
        display: "inline-block",
        width: size,
        height: size,
        background: colors[tone] || colors.gold,
        clipPath: "polygon(50% 0%, 100% 38%, 82% 100%, 18% 100%, 0% 38%)",
        flexShrink: 0,
      }}
    />
  );
}

/* ---------- 小工具 ---------- */
const fmt = {
  money: (n) =>
    n === null || n === undefined || isNaN(n)
      ? "—"
      : new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n),
  price: (n) =>
    n === null || n === undefined || isNaN(n) ? "—" : Number(n).toFixed(2),
  pct: (n) =>
    n === null || n === undefined || isNaN(n) ? "—" : `${Number(n).toFixed(1)}%`,
};

/* ============================================================
   主元件
   ============================================================ */
export default function App() {
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [tab, setTab] = useState("screen");

  const [settings, setSettings] = useState({ totalCapital: 500000, riskPct: 1 });
  const [candidates, setCandidates] = useState([]);
  const [holdings, setHoldings] = useState([]);
  const [sortKey, setSortKey] = useState("range_position_pct");
  const [sortDir, setSortDir] = useState("desc");

  const refreshAll = useCallback(async () => {
    try {
      const [s, c, h] = await Promise.all([
        apiGet("/settings"),
        apiGet("/candidates"),
        apiGet("/holdings"),
      ]);
      setSettings(s);
      setCandidates((c.candidates || []).map((x, i) => ({ ...x, _id: x._id || `${x.symbol}-${i}` })));
      setHoldings(h || []);
      setLoadError("");
    } catch (e) {
      setLoadError("無法連線到後端 API：" + e.message);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  async function saveSettings(next) {
    setSettings(next);
    try {
      await apiSend("PUT", "/settings", next);
    } catch (e) {
      setLoadError(e.message);
    }
  }

  async function importCandidates(payload) {
    const data = await apiSend("POST", "/candidates/import", payload);
    setCandidates((data.candidates || []).map((x, i) => ({ ...x, _id: x._id || `${x.symbol}-${i}` })));
  }

  async function clearCandidates() {
    const data = await apiSend("DELETE", "/candidates");
    setCandidates(data.candidates || []);
  }

  async function addHolding(payload) {
    const created = await apiSend("POST", "/holdings", payload);
    setHoldings((h) => [...h, created]);
  }
  async function patchHolding(id, patch) {
    const updated = await apiSend("PUT", `/holdings/${id}`, patch);
    setHoldings((hs) => hs.map((h) => (h._id === id ? updated : h)));
  }
  async function removeHolding(id) {
    await apiSend("DELETE", `/holdings/${id}`);
    setHoldings((hs) => hs.filter((h) => h._id !== id));
  }

  const sortedCandidates = useMemo(() => {
    const arr = [...candidates];
    arr.sort((a, b) => {
      const av = a[sortKey] ?? -Infinity;
      const bv = b[sortKey] ?? -Infinity;
      if (av === bv) return 0;
      const cmp = av > bv ? 1 : -1;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [candidates, sortKey, sortDir]);

  function toggleSort(key) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function addCandidateToHoldings(candidate) {
    const entryPrice = candidate.price ?? 0;
    const stop = +(entryPrice * 0.775).toFixed(2);
    addHolding({
      symbol: candidate.symbol,
      name: candidate.name || "",
      entryPrice,
      stopPrice: stop,
      shares: 0,
      entryDate: new Date().toISOString().slice(0, 10),
      stage: "watching",
      note: "",
    });
    setTab("holdings");
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "radial-gradient(ellipse at top, #14161d 0%, #0a0b0f 55%)",
        color: "#EDEFF3",
        fontFamily: "'Noto Sans TC', sans-serif",
        paddingBottom: 60,
      }}
    >
      <Header tab={tab} setTab={setTab} />
      <main style={{ maxWidth: 1100, margin: "0 auto", padding: "0 20px" }}>
        {loadError && (
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
            {loadError}（請確認後端服務是否已啟動）
          </div>
        )}
        {!loaded ? (
          <div style={{ padding: 60, textAlign: "center", color: "#8B90A0" }}>載入中…</div>
        ) : (
          <>
            {tab === "screen" && (
              <ScreenTab
                candidates={sortedCandidates}
                importCandidates={importCandidates}
                clearCandidates={clearCandidates}
                sortKey={sortKey}
                sortDir={sortDir}
                toggleSort={toggleSort}
                addToHoldings={addCandidateToHoldings}
              />
            )}
            {tab === "calc" && <CalcTab settings={settings} setSettings={saveSettings} />}
            {tab === "holdings" && (
              <HoldingsTab
                holdings={holdings}
                addHolding={addHolding}
                patchHolding={patchHolding}
                removeHolding={removeHolding}
              />
            )}
            {tab === "sop" && <SopTab />}
          </>
        )}
      </main>
    </div>
  );
}

/* ============================================================
   Header
   ============================================================ */
function Header({ tab, setTab }) {
  const tabs = [
    { key: "screen", label: "選股清單" },
    { key: "calc", label: "停損停利計算" },
    { key: "holdings", label: "持股追蹤" },
    { key: "sop", label: "SOP速查" },
  ];
  return (
    <header
      style={{
        borderBottom: "1px solid #23262f",
        background: "rgba(10,11,15,0.85)",
        backdropFilter: "blur(6px)",
        position: "sticky",
        top: 0,
        zIndex: 10,
      }}
    >
      <div style={{ maxWidth: 1100, margin: "0 auto", padding: "20px 20px 0" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Facet size={22} tone="gold" />
          <h1
            style={{
              fontFamily: "'Noto Serif TC', serif",
              fontWeight: 900,
              fontSize: 26,
              letterSpacing: 1,
              margin: 0,
              color: "#F1E4C6",
            }}
          >
            黑鑽選股
          </h1>
          <span style={{ color: "#6B7080", fontSize: 13, marginLeft: 4 }}>
            Black Diamond Screener
          </span>
        </div>
        <nav style={{ display: "flex", gap: 4, marginTop: 18 }}>
          {tabs.map((t) => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                style={{
                  background: "none",
                  border: "none",
                  cursor: "pointer",
                  padding: "10px 16px",
                  fontSize: 14,
                  fontWeight: 500,
                  color: active ? "#F1E4C6" : "#8B90A0",
                  borderBottom: active ? "2px solid #C9A15A" : "2px solid transparent",
                  fontFamily: "'Noto Sans TC', sans-serif",
                  transition: "color .15s",
                }}
              >
                {t.label}
              </button>
            );
          })}
        </nav>
      </div>
    </header>
  );
}

/* ============================================================
   Panel 共用容器
   ============================================================ */
function Panel({ children, style = {}, title, eyebrow }) {
  return (
    <section
      style={{
        background: "#14161c",
        border: "1px solid #23262f",
        clipPath:
          "polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px))",
        padding: 24,
        marginTop: 20,
        ...style,
      }}
    >
      {(title || eyebrow) && (
        <div style={{ marginBottom: 16 }}>
          {eyebrow && (
            <div
              style={{
                fontSize: 11,
                letterSpacing: 2,
                color: "#8B7A46",
                fontFamily: "'JetBrains Mono', monospace",
                marginBottom: 4,
              }}
            >
              {eyebrow}
            </div>
          )}
          {title && (
            <h2
              style={{
                fontFamily: "'Noto Serif TC', serif",
                fontWeight: 700,
                fontSize: 19,
                margin: 0,
                color: "#EDEFF3",
              }}
            >
              {title}
            </h2>
          )}
        </div>
      )}
      {children}
    </section>
  );
}

/* ============================================================
   Tab 1: 選股清單
   ============================================================ */
function ScreenTab({ candidates, importCandidates, clearCandidates, sortKey, sortDir, toggleSort, addToHoldings }) {
  const [importText, setImportText] = useState("");
  const [importError, setImportError] = useState("");

  const [symbolsInput, setSymbolsInput] = useState("");
  const [jobStatus, setJobStatus] = useState(null);
  const [jobError, setJobError] = useState("");
  const [polling, setPolling] = useState(false);

  const cols = [
    { key: "symbol", label: "代碼" },
    { key: "market_cap", label: "市值" },
    { key: "price", label: "股價" },
    { key: "price_to_sales", label: "P/S" },
    { key: "range_position_pct", label: "52週位置" },
    { key: "avg_volume", label: "平均量" },
    { key: "exchange", label: "交易所" },
  ];

  async function handlePasteImport() {
    setImportError("");
    try {
      const parsed = JSON.parse(importText);
      await importCandidates(parsed);
      setImportText("");
    } catch (e) {
      setImportError("匯入失敗：" + e.message);
    }
  }

  async function handleRunScreen() {
    setJobError("");
    const symbols = symbolsInput
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (symbols.length === 0) {
      setJobError("請輸入至少一檔股票代碼");
      return;
    }
    try {
      await apiSend("POST", "/screen/run", { source: "symbols", symbols });
      setPolling(true);
    } catch (e) {
      setJobError(e.message);
    }
  }

  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(async () => {
      try {
        const status = await apiGet("/screen/status");
        setJobStatus(status);
        if (!status.running) {
          setPolling(false);
          if (!status.error) {
            const data = await apiGet("/candidates");
            await importCandidates(data);
          } else {
            setJobError(status.error);
          }
        }
      } catch (e) {
        setJobError(e.message);
        setPolling(false);
      }
    }, 1500);
    return () => clearInterval(timer);
  }, [polling, importCandidates]);

  return (
    <div>
      <Panel eyebrow="STEP 01 · 執行篩選" title="從後端跑一次篩選">
        <p style={{ color: "#8B90A0", fontSize: 13, lineHeight: 1.7, marginTop: 0 }}>
          輸入想篩選的股票代碼（空白或逗號分隔），後端會即時用 6 道濾網逐檔檢查。
          若要對整個美股市場篩選，建議改用 <code style={{ color: "#C9A15A" }}>engine.py --fetch-nasdaq</code> 在本機或排程執行，較不受網頁逾時限制。
        </p>
        <textarea
          value={symbolsInput}
          onChange={(e) => setSymbolsInput(e.target.value)}
          placeholder="AAPL, SOUN, IONQ, XYZ ..."
          style={{
            width: "100%",
            minHeight: 70,
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
        {jobError && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 8 }}>{jobError}</div>}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12 }}>
          <GoldButton onClick={handleRunScreen} disabled={polling}>
            {polling ? "篩選中…" : "開始篩選"}
          </GoldButton>
          {jobStatus && polling && (
            <span style={{ fontSize: 12, color: "#8B90A0", fontFamily: "'JetBrains Mono', monospace" }}>
              {jobStatus.processed}/{jobStatus.total} · 目前：{jobStatus.current_symbol || "—"} · 已通過 {jobStatus.passed_so_far} 檔
            </span>
          )}
        </div>
      </Panel>

      <Panel eyebrow="或者" title="貼上 candidates.json 匯入">
        <textarea
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          placeholder='{"candidates": [...]}'
          style={{
            width: "100%",
            minHeight: 90,
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
        {importError && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 8 }}>{importError}</div>}
        <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
          <GhostButton onClick={handlePasteImport}>匯入清單</GhostButton>
          {candidates.length > 0 && <GhostButton onClick={clearCandidates}>清空清單</GhostButton>}
        </div>
      </Panel>

      <Panel eyebrow={`STEP 02 · 人工複核 (${candidates.length} 檔通過濾網)`} title="候選股一覽">
        {candidates.length === 0 ? (
          <EmptyState text="尚無候選股，請先執行篩選或匯入 candidates.json。" />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr>
                  {cols.map((c) => (
                    <th
                      key={c.key}
                      onClick={() => toggleSort(c.key)}
                      style={{
                        textAlign: "left",
                        padding: "8px 10px",
                        color: "#8B90A0",
                        fontWeight: 500,
                        borderBottom: "1px solid #23262f",
                        cursor: "pointer",
                        whiteSpace: "nowrap",
                        fontFamily: "'JetBrains Mono', monospace",
                        fontSize: 11,
                      }}
                    >
                      {c.label} {sortKey === c.key ? (sortDir === "asc" ? "▲" : "▼") : ""}
                    </th>
                  ))}
                  <th style={{ borderBottom: "1px solid #23262f" }}></th>
                </tr>
              </thead>
              <tbody>
                {candidates.map((c) => (
                  <tr key={c._id || c.symbol} style={{ borderBottom: "1px solid #1c1f27" }}>
                    <td style={cellStyle}>
                      <div style={{ fontWeight: 700, color: "#F1E4C6" }}>{c.symbol}</div>
                      {c.name && <div style={{ fontSize: 11, color: "#6B7080" }}>{c.name}</div>}
                      {c.needs_manual_review && (
                        <div style={{ fontSize: 11, color: "#E5484D", marginTop: 2 }}>
                          ⚠ {c.needs_manual_review}
                        </div>
                      )}
                    </td>
                    <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>{fmt.money(c.market_cap)}</td>
                    <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>${fmt.price(c.price)}</td>
                    <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>
                      {c.price_to_sales !== undefined && c.price_to_sales !== null ? Number(c.price_to_sales).toFixed(2) : "—"}
                    </td>
                    <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace", color: "#E5484D" }}>
                      {fmt.pct(c.range_position_pct)}
                    </td>
                    <td style={{ ...cellStyle, fontFamily: "'JetBrains Mono', monospace" }}>{fmt.money(c.avg_volume)}</td>
                    <td style={cellStyle}>{c.exchange || "—"}</td>
                    <td style={cellStyle}>
                      <GhostButton small onClick={() => addToHoldings(c)}>
                        加入追蹤
                      </GhostButton>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel eyebrow="STEP 03 · 型態判斷" title="人工複核提醒">
        <ChecklistStatic
          items={[
            "打開 5 年線圖，確認低位盤整時間 ≥ 2 年",
            "確認最近剛突破盤整區間（起漲狀態）",
            "排除 AMEX 交易所的股票",
            "從候選清單中，最終可能只留下 1～2 檔適合進場",
          ]}
        />
      </Panel>
    </div>
  );
}

const cellStyle = { padding: "10px", verticalAlign: "top" };

/* ============================================================
   Tab 2: 停損停利計算機
   ============================================================ */
function CalcTab({ settings, setSettings }) {
  const [entryPrice, setEntryPrice] = useState(50);
  const [stopPrice, setStopPrice] = useState(45);

  const riskAmount = settings.totalCapital * (settings.riskPct / 100);
  const perShareLoss = entryPrice - stopPrice;
  const shares = perShareLoss > 0 ? Math.floor(riskAmount / perShareLoss) : 0;
  const stopPctFromEntry = entryPrice > 0 ? ((entryPrice - stopPrice) / entryPrice) * 100 : 0;
  const profitTarget = entryPrice * 1.2;
  const positionCost = shares * entryPrice;
  const stopTooWide = stopPctFromEntry > 25;

  return (
    <div>
      <Panel eyebrow="資金與風險設定" title="策略配置">
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <FieldNumber
            label="投入黑鑽策略總資金 (USD)"
            value={settings.totalCapital}
            onChange={(v) => setSettings({ ...settings, totalCapital: v })}
          />
          <FieldNumber
            label="每筆交易風險比例 (%)"
            value={settings.riskPct}
            step={0.1}
            onChange={(v) => setSettings({ ...settings, riskPct: v })}
          />
        </div>
        <div style={{ marginTop: 14, fontSize: 13, color: "#8B90A0" }}>
          建議風險比例落在本金的 0.5%～2%，高手通常控制在 1% 以內。
        </div>
      </Panel>

      <Panel eyebrow="單筆交易試算" title="應買股數計算">
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <FieldNumber label="進場價" value={entryPrice} onChange={setEntryPrice} />
          <FieldNumber label="停損價" value={stopPrice} onChange={setStopPrice} />
        </div>

        {stopTooWide && (
          <div
            style={{
              marginTop: 14,
              padding: "10px 14px",
              background: "rgba(229,72,77,0.1)",
              border: "1px solid rgba(229,72,77,0.4)",
              color: "#E5484D",
              fontSize: 13,
            }}
          >
            停損幅度 {stopPctFromEntry.toFixed(1)}% 超過建議上限 25%，請重新檢視支撐位。
          </div>
        )}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
            gap: 14,
            marginTop: 20,
          }}
        >
          <StatCard label="總資金最大損失" value={`$${fmt.money(riskAmount)}`} />
          <StatCard label="每股最大損失" value={`$${fmt.price(perShareLoss)}`} />
          <StatCard label="應買股數" value={fmt.money(shares)} highlight />
          <StatCard label="持倉成本" value={`$${fmt.money(positionCost)}`} />
          <StatCard label="停損幅度" value={fmt.pct(stopPctFromEntry)} tone={stopTooWide ? "red" : undefined} />
          <StatCard label="停利參考線 (+20%)" value={`$${fmt.price(profitTarget)}`} tone="green" />
        </div>
      </Panel>

      <Panel eyebrow="下單提醒" title="停損單設定">
        <ChecklistStatic
          items={[
            "進場下單後，立刻設定停損單，非常重要",
            "市價停損單：觸價後快速成交，但可能有滑價風險",
            "限價停損單：可控制成交價，但極端下跌時可能無法成交（例如跳空）",
            "無論哪種方式，仍需每日關注持股狀況",
          ]}
        />
      </Panel>
    </div>
  );
}

/* ============================================================
   Tab 3: 持股追蹤
   ============================================================ */
const STAGE_LABELS = {
  watching: { label: "觀察中", tone: "muted" },
  entered: { label: "已進場", tone: "gold" },
  profit_zone: { label: "已達停利線", tone: "green" },
  closed: { label: "已出場", tone: "muted" },
};

function HoldingsTab({ holdings, addHolding, patchHolding, removeHolding }) {
  function addBlank() {
    addHolding({
      symbol: "",
      name: "",
      entryPrice: 0,
      stopPrice: 0,
      shares: 0,
      entryDate: new Date().toISOString().slice(0, 10),
      stage: "watching",
      note: "",
    });
  }

  return (
    <div>
      <Panel eyebrow={`共 ${holdings.length} 筆`} title="持股追蹤表">
        {holdings.length === 0 ? (
          <EmptyState text="尚無追蹤中的持股，可從「選股清單」加入候選股，或直接新增一筆。" />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {holdings.map((h) => (
              <HoldingCard
                key={h._id}
                holding={h}
                onChange={(patch) => patchHolding(h._id, patch)}
                onRemove={() => removeHolding(h._id)}
              />
            ))}
          </div>
        )}
        <div style={{ marginTop: 16 }}>
          <GhostButton onClick={addBlank}>+ 手動新增持股</GhostButton>
        </div>
      </Panel>

      <Panel eyebrow="出場邏輯提醒" title="漁夫式出場法（達停利線後）">
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <ExitRow tone="gold" label="短期出場" detail="股價跌破 SMA20（收盤）→ 賣出 1/4" />
          <ExitRow tone="gold" label="中期出場" detail="股價跌破 EMA60（高點）→ 賣出 1/2" />
          <ExitRow tone="gold" label="長期出場" detail="股價跌破 EMA120（高點）→ 賣出剩餘 1/4" />
        </div>
        <p style={{ color: "#8B90A0", fontSize: 13, marginTop: 14, lineHeight: 1.7 }}>
          停損線～停利線之間為「真空區」：價格未觸及上下限之前，不操作、持續觀察。
          一旦盤中觸及 +20% 停利線，記得把停損線拉高至進場價，鎖住本金。
        </p>
      </Panel>
    </div>
  );
}

function HoldingCard({ holding, onChange, onRemove }) {
  const stage = STAGE_LABELS[holding.stage] || STAGE_LABELS.watching;
  return (
    <div
      style={{
        border: "1px solid #23262f",
        background: "#181a21",
        padding: 16,
        display: "flex",
        flexWrap: "wrap",
        gap: 14,
        alignItems: "flex-end",
      }}
    >
      <div style={{ minWidth: 90 }}>
        <MiniLabel>代碼</MiniLabel>
        <input
          value={holding.symbol}
          onChange={(e) => onChange({ symbol: e.target.value.toUpperCase() })}
          style={inputStyle}
          placeholder="AAPL"
        />
      </div>
      <div style={{ minWidth: 100 }}>
        <MiniLabel>進場價</MiniLabel>
        <input
          type="number"
          value={holding.entryPrice}
          onChange={(e) => onChange({ entryPrice: parseFloat(e.target.value) || 0 })}
          style={inputStyle}
        />
      </div>
      <div style={{ minWidth: 100 }}>
        <MiniLabel>停損價</MiniLabel>
        <input
          type="number"
          value={holding.stopPrice}
          onChange={(e) => onChange({ stopPrice: parseFloat(e.target.value) || 0 })}
          style={inputStyle}
        />
      </div>
      <div style={{ minWidth: 90 }}>
        <MiniLabel>股數</MiniLabel>
        <input
          type="number"
          value={holding.shares}
          onChange={(e) => onChange({ shares: parseInt(e.target.value) || 0 })}
          style={inputStyle}
        />
      </div>
      <div style={{ minWidth: 130 }}>
        <MiniLabel>進場日期</MiniLabel>
        <input
          type="date"
          value={holding.entryDate}
          onChange={(e) => onChange({ entryDate: e.target.value })}
          style={inputStyle}
        />
      </div>
      <div style={{ minWidth: 140 }}>
        <MiniLabel>階段</MiniLabel>
        <select
          value={holding.stage}
          onChange={(e) => onChange({ stage: e.target.value })}
          style={{ ...inputStyle, cursor: "pointer" }}
        >
          {Object.entries(STAGE_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v.label}
            </option>
          ))}
        </select>
      </div>
      <div style={{ flex: 1, minWidth: 160 }}>
        <MiniLabel>備註</MiniLabel>
        <input
          value={holding.note}
          onChange={(e) => onChange({ note: e.target.value })}
          style={inputStyle}
          placeholder="例：已拉高停損至進場價"
        />
      </div>
      <Badge tone={stage.tone}>{stage.label}</Badge>
      <button
        onClick={onRemove}
        style={{
          background: "none",
          border: "1px solid #3a2020",
          color: "#E5484D",
          padding: "6px 10px",
          cursor: "pointer",
          fontSize: 12,
        }}
      >
        刪除
      </button>
    </div>
  );
}

function ExitRow({ label, detail, tone }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <Facet size={10} tone={tone} />
      <span style={{ fontWeight: 700, fontSize: 13, minWidth: 80, color: "#F1E4C6" }}>{label}</span>
      <span style={{ fontSize: 13, color: "#C7CAD4" }}>{detail}</span>
    </div>
  );
}

/* ============================================================
   Tab 4: SOP 速查
   ============================================================ */
function SopTab() {
  return (
    <div>
      <Panel eyebrow="濾網邏輯" title="6 道電腦濾網">
        <SopTable
          rows={[
            ["市值", "25M ~ 250M（可調整為 250~1000 改抓中型股）"],
            ["價值因子", "Price/Sales ≤ 1"],
            ["動能因子", "股價位於52週高低區間 ≥ 90%"],
            ["交易量", "平均日成交量 ≥ 10,000（資金大可提高到 100k~200k）"],
            ["交易所", "排除 OTC；AMEX 需人工複核排除"],
            ["股價", "≥ 1（排除低價股）"],
          ]}
        />
      </Panel>

      <Panel eyebrow="進場" title="停損與股數計算">
        <ChecklistStatic
          items={[
            "停損設在進場價下方 25% 以內，優先找前一波盤整低點或前一根長紅K棒低點作支撐",
            "應買股數 = (總資金 × 風險比例) / 每股最大損失（進場價－停損價）",
            "下單後立即設定停損單",
          ]}
        />
      </Panel>

      <Panel eyebrow="出場" title="三種出場情境">
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <SopScenario tone="red" title="情況一：進場後下跌" detail="收盤價跌破停損線 → 全部出場" />
          <SopScenario
            tone="muted"
            title="情況二：持續盤整未達停利線"
            detail="持股滿1個月未達停利線 → 賣出一半；滿2個月仍未達 → 全部出場"
          />
          <SopScenario
            tone="green"
            title="情況三：盤中觸及 +20% 停利線"
            detail="停損線拉高至進場價鎖住本金，改用 SMA20 / EMA60 / EMA120 分批出場（漁夫出場法）"
          />
        </div>
      </Panel>

      <Panel eyebrow="心態" title="重要提醒">
        <ChecklistStatic
          items={[
            "濾網是「低標」，不是保證獲利的完美策略",
            "停損價永遠優先於任何均線，跌破就出場",
            "所有出場行為都必須有 SOP 規則依據，不能憑感覺",
            "電腦選股只是初篩，最終仍要靠人工判斷型態",
          ]}
        />
      </Panel>
    </div>
  );
}

function SopTable({ rows }) {
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i} style={{ borderBottom: "1px solid #1c1f27" }}>
            <td style={{ padding: "10px 10px 10px 0", width: 120, color: "#C9A15A", fontWeight: 700, verticalAlign: "top" }}>
              {k}
            </td>
            <td style={{ padding: "10px 0", color: "#C7CAD4" }}>{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SopScenario({ title, detail, tone }) {
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
      <div style={{ marginTop: 4 }}>
        <Facet size={11} tone={tone} />
      </div>
      <div>
        <div style={{ fontWeight: 700, fontSize: 14, color: "#EDEFF3" }}>{title}</div>
        <div style={{ fontSize: 13, color: "#8B90A0", marginTop: 2 }}>{detail}</div>
      </div>
    </div>
  );
}

/* ============================================================
   小型共用元件
   ============================================================ */
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

function GhostButton({ children, onClick, small }) {
  return (
    <button
      onClick={onClick}
      style={{
        background: "none",
        border: "1px solid #3a3e4a",
        color: "#C7CAD4",
        padding: small ? "5px 10px" : "10px 18px",
        fontWeight: 500,
        fontSize: small ? 12 : 13,
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

function FieldNumber({ label, value, onChange, step = 1 }) {
  return (
    <div>
      <MiniLabel>{label}</MiniLabel>
      <input
        type="number"
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
        style={{ ...inputStyle, width: 180, fontSize: 16, fontWeight: 700 }}
      />
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
  width: "100%",
  background: "#0d0e12",
  border: "1px solid #2a2e38",
  color: "#EDEFF3",
  padding: "8px 10px",
  fontSize: 13,
  fontFamily: "'JetBrains Mono', monospace",
  boxSizing: "border-box",
};

function StatCard({ label, value, tone, highlight }) {
  const toneColor = tone === "red" ? "#E5484D" : tone === "green" ? "#2FA36B" : highlight ? "#F1E4C6" : "#EDEFF3";
  return (
    <div style={{ border: `1px solid ${highlight ? "#C9A15A" : "#23262f"}`, background: "#0d0e12", padding: 16 }}>
      <div style={{ fontSize: 11, color: "#8B90A0", marginBottom: 8 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: toneColor }}>{value}</div>
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
    <span style={{ background: c.bg, color: c.fg, fontSize: 11, fontWeight: 700, padding: "6px 10px", whiteSpace: "nowrap", alignSelf: "center" }}>
      {children}
    </span>
  );
}

function ChecklistStatic({ items }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it, i) => (
        <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          <Facet size={8} tone="gold" />
          <span style={{ fontSize: 13, color: "#C7CAD4", lineHeight: 1.6 }}>{it}</span>
        </div>
      ))}
    </div>
  );
}

function EmptyState({ text }) {
  return (
    <div style={{ border: "1px dashed #2a2e38", padding: "36px 20px", textAlign: "center", color: "#5c6070", fontSize: 13 }}>
      {text}
    </div>
  );
}
