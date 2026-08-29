import React, { useState, useEffect, useCallback } from "react";

/* ============================================================
   黑鑽飆股策略 — 持股追蹤（比照使用者「黑鑽飆股策略紀錄表」Excel重建）
   自動比對7種出場規則，觸發時發Telegram通知，實際出場仍由使用者手動記錄。
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

const EXIT_REASONS = [
  "收盤價跌破止損線，全部出場",
  "1個月未漲至停利線，出場1/2",
  "2個月未漲至停利線，全部出場",
  "盤中跌破SMA20，出場1/4",
  "盤中跌破EMA60，出場1/2",
  "盤中跌破EMA120，出場1/4",
  "漲達停利線後跌回進場價，全部出場",
];

const fmt = {
  money: (n) =>
    n === null || n === undefined || isNaN(n)
      ? "—"
      : new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(n),
  pct: (n) => (n === null || n === undefined || isNaN(n) ? "—" : `${(n * 100).toFixed(2)}%`),
};

function stripExchangePrefix(symbol) {
  if (!symbol) return symbol;
  const idx = symbol.indexOf(":");
  return idx >= 0 ? symbol.slice(idx + 1).trim().toUpperCase() : symbol.trim().toUpperCase();
}

function profitTarget(entryPrice) {
  return entryPrice ? Math.round(entryPrice * 1.2 * 10000) / 10000 : null;
}

export default function DiamondHoldingsTab({ openChart }) {
  const [positions, setPositions] = useState([]);
  const [summary, setSummary] = useState(null);
  const [settings, setSettings] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [list, s] = await Promise.all([apiGet("/diamond-holdings"), apiGet("/diamond-holdings/settings")]);
      setPositions(list.positions || []);
      setSummary(list.summary || null);
      setSettings(s);
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

  const openPositions = positions.filter((p) => p.status !== "closed");
  const closedPositions = positions.filter((p) => p.status === "closed");

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

      {!loaded ? (
        <div style={{ padding: 60, textAlign: "center", color: "#8B90A0" }}>載入中…</div>
      ) : (
        <>
          <PortfolioSummaryPanel summary={summary} onRefresh={refresh} />
          <SettingsPanel settings={settings} onSaved={refresh} />
          <AddPositionPanel settings={settings} onAdded={refresh} />

          <Panel eyebrow={`持有中 ${openPositions.length} 筆`} title="持股追蹤">
            {openPositions.length === 0 ? (
              <EmptyState text="尚無持有中的部位，可以在上方「新增持股」加入，或從「選股清單」的候選股按「加入追蹤」。" />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                {openPositions.map((p) => (
                  <PositionCard key={p._id} position={p} onChanged={refresh} openChart={openChart} />
                ))}
              </div>
            )}
          </Panel>

          {closedPositions.length > 0 && (
            <Panel eyebrow={`已平倉 ${closedPositions.length} 筆`} title="平倉紀錄">
              <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                {closedPositions.map((p) => (
                  <PositionCard key={p._id} position={p} onChanged={refresh} openChart={openChart} closed />
                ))}
              </div>
            </Panel>
          )}

          <Panel eyebrow="規則對照" title="7種出場規則">
            <ChecklistStatic
              items={[
                "收盤價跌破止損線 → 全部出場（最高優先，只要沒平倉會持續提醒）",
                "持有滿1個月未達停利線 → 出場1/2",
                "持有滿2個月仍未達停利線 → 全部出場",
                "曾觸及停利線後，盤中跌破SMA20 → 出場1/4",
                "曾觸及停利線後，盤中跌破EMA60 → 出場1/2",
                "曾觸及停利線後，盤中跌破EMA120 → 出場1/4",
                "曾觸及停利線後，價格跌回進場價 → 全部出場",
              ]}
            />
            <p style={{ color: "#5c6070", fontSize: 12, lineHeight: 1.7, marginTop: 14, marginBottom: 0 }}>
              停利線 = 進場價 × 1.2。系統只負責提醒符合規則的時機，不會自動下單，
              實際出場價格/股數/日期仍需要你自己執行後回填紀錄。
            </p>
          </Panel>
        </>
      )}
    </div>
  );
}

/* ---------- 組合帳戶總覽 ---------- */
function PortfolioSummaryPanel({ summary, onRefresh }) {
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState(null);
  const [checkError, setCheckError] = useState("");

  async function checkNow() {
    setChecking(true);
    setCheckError("");
    setCheckResult(null);
    try {
      const result = await apiSend("POST", "/diamond-holdings/check-now");
      setCheckResult(result);
      await onRefresh();
    } catch (e) {
      setCheckError(e.message);
    } finally {
      setChecking(false);
    }
  }

  if (!summary) return null;

  return (
    <Panel eyebrow="黑鑽飆股策略" title="帳戶總覽">
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
          gap: 14,
        }}
      >
        <StatCard label="策略本金" value={`$${fmt.money(summary.strategy_capital)}`} />
        <StatCard label="總買入金額" value={`$${fmt.money(summary.total_buy_amount)}`} />
        <StatCard label="總賣出金額" value={`$${fmt.money(summary.total_sell_amount)}`} />
        <StatCard label="總持有市值" value={`$${fmt.money(summary.total_market_value)}`} />
        <StatCard
          label="總未實現損益"
          value={`$${fmt.money(summary.total_unrealized_pnl)}`}
          tone={summary.total_unrealized_pnl < 0 ? "red" : "green"}
        />
        <StatCard label="帳上流動資金" value={`$${fmt.money(summary.cash_on_hand)}`} />
        <StatCard label="帳上淨值" value={`$${fmt.money(summary.net_worth)}`} highlight />
        <StatCard
          label="整體帳戶報酬率"
          value={summary.total_return !== null ? fmt.pct(summary.total_return) : "—"}
          tone={summary.total_return < 0 ? "red" : "green"}
        />
      </div>

      <div style={{ marginTop: 20, paddingTop: 20, borderTop: "1px solid #23262f" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <GoldButton onClick={checkNow} disabled={checking}>
            {checking ? "檢查中…" : "立即檢查所有持股"}
          </GoldButton>
        </div>
        {checkError && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 10 }}>{checkError}</div>}
        {checkResult && (
          <div style={{ marginTop: 10, fontSize: 13, color: "#8B90A0" }}>
            {checkResult.alerts.length === 0 ? (
              <span>本次檢查沒有任何持股觸發出場規則。</span>
            ) : (
              <div>
                <div style={{ color: "#E8C275", marginBottom: 4 }}>本次觸發 {checkResult.alerts.length} 筆提醒：</div>
                {checkResult.alerts.map((a, i) => (
                  <div key={i}>
                    {a.symbol}：{a.reason}（建議約 {a.suggested_shares} 股）
                  </div>
                ))}
              </div>
            )}
            {checkResult.errors.length > 0 && (
              <div style={{ color: "#E5484D", marginTop: 6 }}>
                {checkResult.errors.map((e, i) => (
                  <div key={i}>⚠ {e}</div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}

/* ---------- 設定 ---------- */
function SettingsPanel({ settings, onSaved }) {
  const [draft, setDraft] = useState(settings);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    setDraft(settings);
  }, [settings]);

  if (!draft) return null;

  async function save() {
    setSaving(true);
    setSaveError("");
    try {
      await apiSend("PUT", "/diamond-holdings/settings", draft);
      await onSaved();
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Panel eyebrow="設定" title="風險參數與自動檢查">
      <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
        <Field label="策略本金 (USD)">
          <input
            type="number"
            value={draft.strategy_capital}
            onChange={(e) => setDraft({ ...draft, strategy_capital: parseFloat(e.target.value) || 0 })}
            style={inputStyle}
          />
        </Field>
        <Field label="每筆風險比例 (%)">
          <input
            type="number"
            step="0.1"
            value={draft.risk_pct}
            onChange={(e) => setDraft({ ...draft, risk_pct: parseFloat(e.target.value) || 0 })}
            style={inputStyle}
          />
        </Field>
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#C7CAD4", cursor: "pointer", marginTop: 16 }}>
        <input
          type="checkbox"
          checked={draft.auto_check_enabled}
          onChange={(e) => setDraft({ ...draft, auto_check_enabled: e.target.checked })}
        />
        啟用自動排程（伺服器每天自動比對所有持股的出場規則，觸發時發 Telegram 通知）
      </label>
      <p style={{ color: "#5c6070", fontSize: 12, marginTop: 8, marginBottom: 0 }}>
        Telegram 設定跟「超速大盤」分頁共用同一組 Bot Token / Chat ID。
      </p>
      {saveError && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 12 }}>{saveError}</div>}
      <div style={{ marginTop: 14 }}>
        <GoldButton onClick={save} disabled={saving}>
          {saving ? "儲存中…" : "儲存設定"}
        </GoldButton>
      </div>
    </Panel>
  );
}

/* ---------- 新增持股 ---------- */
function AddPositionPanel({ settings, onAdded }) {
  const [open, setOpen] = useState(false);
  const [symbol, setSymbol] = useState("");
  const [entryDate, setEntryDate] = useState(new Date().toISOString().slice(0, 10));
  const [entryPrice, setEntryPrice] = useState(0);
  const [stopLoss, setStopLoss] = useState(0);
  const [shares, setShares] = useState(0);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const riskAmount = settings ? (settings.strategy_capital * settings.risk_pct) / 100 : 0;
  const perShareRisk = entryPrice - stopLoss;
  const suggestedShares = perShareRisk > 0 ? Math.round(riskAmount / perShareRisk) : 0;
  const ref20 = entryPrice ? +(entryPrice * 0.8).toFixed(4) : null;
  const ref25 = entryPrice ? +(entryPrice * 0.75).toFixed(4) : null;

  async function submit() {
    if (!symbol.trim()) {
      setErr("請輸入股號");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      await apiSend("POST", "/diamond-holdings", {
        symbol: symbol.trim(),
        entry_date: entryDate,
        shares: Number(shares) || 0,
        entry_price: Number(entryPrice) || 0,
        stop_loss: Number(stopLoss) || 0,
        note: note || null,
      });
      setSymbol("");
      setShares(0);
      setEntryPrice(0);
      setStopLoss(0);
      setNote("");
      setOpen(false);
      await onAdded();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Panel eyebrow="新增" title="新增持股">
        <GoldButton onClick={() => setOpen(true)}>+ 新增一筆持股</GoldButton>
      </Panel>
    );
  }

  return (
    <Panel eyebrow="新增" title="新增持股">
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <Field label="股號（可含交易所，例如 NASDAQ:OESX）">
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} style={{ ...inputStyle, width: 180 }} placeholder="NASDAQ:OESX" />
        </Field>
        <Field label="進場日期">
          <input type="date" value={entryDate} onChange={(e) => setEntryDate(e.target.value)} style={inputStyle} />
        </Field>
        <Field label="進場價">
          <input type="number" value={entryPrice} onChange={(e) => setEntryPrice(parseFloat(e.target.value) || 0)} style={inputStyle} />
        </Field>
        <Field label="止損線">
          <input type="number" value={stopLoss} onChange={(e) => setStopLoss(parseFloat(e.target.value) || 0)} style={inputStyle} />
        </Field>
        <Field label="股數">
          <input type="number" value={shares} onChange={(e) => setShares(parseFloat(e.target.value) || 0)} style={inputStyle} />
        </Field>
      </div>

      {entryPrice > 0 && (
        <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginTop: 16, fontSize: 12, color: "#8B90A0" }}>
          <span>-20%參考：${fmt.money(ref20)}</span>
          <span>-25%參考：${fmt.money(ref25)}</span>
          <span>停利線（+20%）：${fmt.money(profitTarget(entryPrice))}</span>
          {perShareRisk > 0 && (
            <span style={{ color: "#E8C275" }}>
              依風險設定建議應買股數：<strong>{suggestedShares}</strong> 股
            </span>
          )}
        </div>
      )}

      <div style={{ marginTop: 14 }}>
        <Field label="備註（選填）">
          <input value={note} onChange={(e) => setNote(e.target.value)} style={{ ...inputStyle, width: "100%" }} />
        </Field>
      </div>

      {err && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 10 }}>{err}</div>}
      <div style={{ marginTop: 14, display: "flex", gap: 10 }}>
        <GoldButton onClick={submit} disabled={busy}>
          {busy ? "新增中…" : "新增"}
        </GoldButton>
        <GhostButton small onClick={() => setOpen(false)}>
          取消
        </GhostButton>
      </div>
    </Panel>
  );
}

/* ---------- 單筆持股卡片 ---------- */
function PositionCard({ position: p, onChanged, openChart, closed }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(p);
  const [showExitForm, setShowExitForm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    setDraft(p);
  }, [p]);

  async function saveEdit() {
    setBusy(true);
    setErr("");
    try {
      await apiSend("PUT", `/diamond-holdings/${p._id}`, {
        symbol: draft.symbol,
        entry_date: draft.entry_date,
        shares: Number(draft.shares),
        entry_price: Number(draft.entry_price),
        stop_loss: Number(draft.stop_loss),
        note: draft.note,
      });
      setEditing(false);
      await onChanged();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function removePosition() {
    setBusy(true);
    try {
      await apiSend("DELETE", `/diamond-holdings/${p._id}`);
      await onChanged();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const pnlColor = p.total_pnl === null || p.total_pnl === undefined ? "#8B90A0" : p.total_pnl >= 0 ? "#4CC98A" : "#F0787C";

  return (
    <div
      style={{
        border: "1px solid #23262f",
        background: "#181a21",
        padding: 16,
        opacity: closed ? 0.75 : 1,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <button
            onClick={() =>
              openChart(stripExchangePrefix(p.symbol), [
                { label: "進場價", value: p.entry_price, color: "#E8C275" },
                { label: "止損線", value: p.stop_loss, color: "#E5484D" },
                { label: "停利線", value: p.profit_target, color: "#2FA36B" },
              ])
            }
            style={{
              background: "none",
              border: "none",
              padding: 0,
              cursor: "pointer",
              fontWeight: 700,
              fontSize: 16,
              color: "#F1E4C6",
              textDecoration: "underline",
              textDecorationColor: "#3a3e4a",
            }}
          >
            {p.symbol}
          </button>
          {closed && <Badge tone="muted">已平倉</Badge>}
          {p.ever_hit_target && <Badge tone="green">已觸及停利線</Badge>}
          {p.last_alert && (
            <Badge tone="gold">上次提醒：{p.last_alert.reason}</Badge>
          )}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {!closed && (
            <GhostButton small onClick={() => setEditing((v) => !v)}>
              {editing ? "取消編輯" : "編輯"}
            </GhostButton>
          )}
          {!closed && (
            <GhostButton small onClick={() => setShowExitForm((v) => !v)}>
              記錄出場
            </GhostButton>
          )}
          <button
            onClick={removePosition}
            disabled={busy}
            style={{
              background: "none",
              border: "1px solid #3a2020",
              color: "#E5484D",
              padding: "5px 10px",
              cursor: "pointer",
              fontSize: 12,
              whiteSpace: "nowrap",
            }}
          >
            刪除
          </button>
        </div>
      </div>

      {editing ? (
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 14 }}>
          <Field label="進場日期">
            <input type="date" value={draft.entry_date} onChange={(e) => setDraft({ ...draft, entry_date: e.target.value })} style={inputStyle} />
          </Field>
          <Field label="進場價">
            <input type="number" value={draft.entry_price} onChange={(e) => setDraft({ ...draft, entry_price: e.target.value })} style={inputStyle} />
          </Field>
          <Field label="止損線">
            <input type="number" value={draft.stop_loss} onChange={(e) => setDraft({ ...draft, stop_loss: e.target.value })} style={inputStyle} />
          </Field>
          <Field label="股數">
            <input type="number" value={draft.shares} onChange={(e) => setDraft({ ...draft, shares: e.target.value })} style={inputStyle} />
          </Field>
          <Field label="備註">
            <input value={draft.note || ""} onChange={(e) => setDraft({ ...draft, note: e.target.value })} style={{ ...inputStyle, width: 200 }} />
          </Field>
          <div style={{ alignSelf: "flex-end" }}>
            <GoldButton onClick={saveEdit} disabled={busy}>
              {busy ? "儲存中…" : "儲存"}
            </GoldButton>
          </div>
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))",
            gap: 10,
            marginTop: 14,
            fontSize: 12,
          }}
        >
          <MiniStat label="進場日" value={p.entry_date} />
          <MiniStat label="進場價" value={`$${fmt.money(p.entry_price)}`} />
          <MiniStat label="止損線" value={`$${fmt.money(p.stop_loss)}`} />
          <MiniStat label="停利線" value={`$${fmt.money(p.profit_target)}`} />
          <MiniStat label="原始股數" value={fmt.money(p.shares)} />
          <MiniStat label="未出清股數" value={fmt.money(p.remaining_shares)} />
          <MiniStat label="最新價" value={p.latest_price !== null && p.latest_price !== undefined ? `$${fmt.money(p.latest_price)}` : "—"} />
          <MiniStat label="持有市值" value={p.market_value !== null && p.market_value !== undefined ? `$${fmt.money(p.market_value)}` : "—"} />
          <MiniStat
            label="未實現損益"
            value={p.unrealized_pnl !== null && p.unrealized_pnl !== undefined ? `$${fmt.money(p.unrealized_pnl)}` : "—"}
          />
          <MiniStat label="已實現損益" value={`$${fmt.money(p.realized_pnl)}`} />
          <MiniStat label="損益比例" value={p.pnl_ratio !== null ? fmt.pct(p.pnl_ratio) : "—"} valueColor={pnlColor} />
          <MiniStat label="未實現+已實現" value={`$${fmt.money(p.total_pnl)}`} valueColor={pnlColor} />
        </div>
      )}

      {p.note && !editing && <div style={{ marginTop: 10, fontSize: 12, color: "#8B90A0" }}>備註：{p.note}</div>}

      {p.exits && p.exits.length > 0 && (
        <div style={{ marginTop: 14, borderTop: "1px solid #23262f", paddingTop: 12 }}>
          <MiniLabel>出場紀錄（{p.exits.length}/4）</MiniLabel>
          <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
            {p.exits.map((e, i) => (
              <div key={i} style={{ fontSize: 12, color: "#C7CAD4" }}>
                第{i + 1}次：{e.date} · ${fmt.money(e.price)} × {e.shares}股 · {e.reason}
              </div>
            ))}
          </div>
        </div>
      )}

      {showExitForm && <ExitForm positionId={p._id} onDone={() => { setShowExitForm(false); onChanged(); }} />}

      {err && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 10 }}>{err}</div>}
    </div>
  );
}

function ExitForm({ positionId, onDone }) {
  const [reason, setReason] = useState(EXIT_REASONS[0]);
  const [price, setPrice] = useState(0);
  const [shares, setShares] = useState(0);
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    setBusy(true);
    setErr("");
    try {
      await apiSend("POST", `/diamond-holdings/${positionId}/exit`, {
        reason,
        price: Number(price) || 0,
        shares: Number(shares) || 0,
        date,
      });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ marginTop: 14, borderTop: "1px solid #23262f", paddingTop: 14 }}>
      <MiniLabel>記錄這次出場</MiniLabel>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8, alignItems: "flex-end" }}>
        <Field label="原因">
          <select value={reason} onChange={(e) => setReason(e.target.value)} style={{ ...inputStyle, width: 240, cursor: "pointer" }}>
            {EXIT_REASONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </Field>
        <Field label="成交價">
          <input type="number" value={price} onChange={(e) => setPrice(e.target.value)} style={inputStyle} />
        </Field>
        <Field label="股數">
          <input type="number" value={shares} onChange={(e) => setShares(e.target.value)} style={inputStyle} />
        </Field>
        <Field label="日期">
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} style={inputStyle} />
        </Field>
        <GoldButton onClick={submit} disabled={busy}>
          {busy ? "送出中…" : "確認記錄"}
        </GoldButton>
      </div>
      {err && <div style={{ color: "#E5484D", fontSize: 13, marginTop: 8 }}>{err}</div>}
    </div>
  );
}

/* ---------- 共用小元件 ---------- */
function Panel({ children, title, eyebrow }) {
  return (
    <section
      className="app-panel"
      style={{
        background: "#14161c",
        border: "1px solid #23262f",
        clipPath: "polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 14px 100%, 0 calc(100% - 14px))",
        padding: 24,
        marginTop: 20,
        maxWidth: "100%",
        boxSizing: "border-box",
        overflow: "hidden",
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
    <div style={{ border: `1px solid ${highlight ? "#C9A15A" : "#23262f"}`, background: "#0d0e12", padding: 14 }}>
      <div style={{ fontSize: 11, color: "#8B90A0", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 17, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: toneColor }}>{value}</div>
    </div>
  );
}

function MiniStat({ label, value, valueColor }) {
  return (
    <div>
      <div style={{ color: "#5c6070", fontSize: 10, marginBottom: 2 }}>{label}</div>
      <div style={{ color: valueColor || "#EDEFF3", fontFamily: "'JetBrains Mono', monospace", fontWeight: 600 }}>{value}</div>
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
    <span style={{ background: c.bg, color: c.fg, fontSize: 11, fontWeight: 700, padding: "5px 9px", whiteSpace: "nowrap" }}>{children}</span>
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
        whiteSpace: "nowrap",
        flexShrink: 0,
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
        whiteSpace: "nowrap",
        flexShrink: 0,
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
  width: 130,
};

function ChecklistStatic({ items }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {items.map((it, i) => (
        <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
          <span
            style={{
              display: "inline-block",
              width: 8,
              height: 8,
              background: "#C9A15A",
              clipPath: "polygon(50% 0%, 100% 38%, 82% 100%, 18% 100%, 0% 38%)",
              flexShrink: 0,
              marginTop: 4,
            }}
          />
          <span style={{ fontSize: 13, color: "#C7CAD4", lineHeight: 1.6 }}>{it}</span>
        </div>
      ))}
    </div>
  );
}

function EmptyState({ text }) {
  return (
    <div style={{ border: "1px dashed #2a2e38", padding: "36px 20px", textAlign: "center", color: "#5c6070", fontSize: 13 }}>{text}</div>
  );
}
