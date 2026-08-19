import React, { useEffect, useState } from "react";
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
} from "recharts";

/* ============================================================
   線圖 Modal — 顯示個股近5年週K走勢，供人工判斷盤整/起漲型態
   ============================================================ */

const API = "/api";

async function apiGet(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    let detail = text;
    try {
      detail = JSON.parse(text).detail || text;
    } catch {
      /* ignore */
    }
    throw new Error(detail || `讀取失敗 (${r.status})`);
  }
  return r.json();
}

function fmtPrice(n) {
  return n === null || n === undefined || isNaN(n) ? "—" : Number(n).toFixed(2);
}

function fmtDateLabel(dateStr) {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  return `${d.getFullYear()}`;
}

export default function ChartModal({ symbol, markers = [], onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setData(null);
    apiGet(`/chart/${encodeURIComponent(symbol)}?period=5y&interval=1wk`)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  useEffect(() => {
    function onKey(e) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const points = data?.points || [];
  const chartData = points.map((p) => ({
    date: p.date,
    close: p.close,
    range: p.high !== null && p.low !== null ? [p.low, p.high] : null,
  }));

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(5,6,9,0.78)",
        backdropFilter: "blur(3px)",
        zIndex: 100,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
      }}
    >
      <div
        className="app-modal-box"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxWidth: 780,
          maxHeight: "88vh",
          overflowY: "auto",
          background: "#14161c",
          border: "1px solid #2a2e38",
          clipPath:
            "polygon(0 0, calc(100% - 18px) 0, 100% 18px, 100% 100%, 18px 100%, 0 calc(100% - 18px))",
          padding: 24,
          boxSizing: "border-box",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <div
              style={{
                fontSize: 11,
                letterSpacing: 2,
                color: "#8B7A46",
                fontFamily: "'JetBrains Mono', monospace",
                marginBottom: 4,
              }}
            >
              近 5 年週線
            </div>
            <h2
              style={{
                fontFamily: "'Noto Serif TC', serif",
                fontWeight: 900,
                fontSize: 26,
                margin: 0,
                color: "#F1E4C6",
              }}
            >
              {symbol}
            </h2>
          </div>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "1px solid #3a3e4a",
              color: "#C7CAD4",
              width: 32,
              height: 32,
              cursor: "pointer",
              fontSize: 16,
              lineHeight: "1",
            }}
            aria-label="關閉"
          >
            ✕
          </button>
        </div>

        {markers.length > 0 && (
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 14 }}>
            {markers
              .filter((m) => m.value !== null && m.value !== undefined && !isNaN(m.value))
              .map((m, i) => (
                <span
                  key={i}
                  style={{
                    fontSize: 12,
                    fontFamily: "'JetBrains Mono', monospace",
                    color: m.color || "#8B90A0",
                  }}
                >
                  {m.label} ${fmtPrice(m.value)}
                </span>
              ))}
          </div>
        )}

        <div style={{ marginTop: 20, height: 360 }}>
          {loading && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#5c6070", fontSize: 13 }}>
              載入線圖中…
            </div>
          )}
          {!loading && error && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                height: "100%",
                color: "#E5484D",
                fontSize: 13,
                textAlign: "center",
                padding: "0 20px",
              }}
            >
              {error}
            </div>
          )}
          {!loading && !error && chartData.length > 0 && (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={chartData} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#20232c" vertical={false} />
                <XAxis
                  dataKey="date"
                  tickFormatter={fmtDateLabel}
                  stroke="#4A4E5A"
                  tick={{ fill: "#6B7080", fontSize: 11, fontFamily: "'JetBrains Mono', monospace" }}
                  minTickGap={40}
                />
                <YAxis
                  domain={["auto", "auto"]}
                  stroke="#4A4E5A"
                  tick={{ fill: "#6B7080", fontSize: 11, fontFamily: "'JetBrains Mono', monospace" }}
                  width={54}
                  tickFormatter={(v) => `$${v}`}
                />
                <Tooltip
                  contentStyle={{
                    background: "#0d0e12",
                    border: "1px solid #2a2e38",
                    fontSize: 12,
                    fontFamily: "'JetBrains Mono', monospace",
                  }}
                  labelStyle={{ color: "#8B90A0" }}
                  formatter={(value, name) => {
                    if (name === "range" && Array.isArray(value)) {
                      return [`${fmtPrice(value[0])} ~ ${fmtPrice(value[1])}`, "高低區間"];
                    }
                    return [`$${fmtPrice(value)}`, "收盤"];
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="range"
                  stroke="none"
                  fill="#C9A15A"
                  fillOpacity={0.08}
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="close"
                  stroke="#E8C275"
                  strokeWidth={1.75}
                  dot={false}
                  isAnimationActive={false}
                />
                {markers
                  .filter((m) => m.value !== null && m.value !== undefined && !isNaN(m.value))
                  .map((m, i) => (
                    <ReferenceLine
                      key={i}
                      y={m.value}
                      stroke={m.color || "#4A4E5A"}
                      strokeDasharray="4 4"
                      label={{
                        value: m.label,
                        position: "insideTopLeft",
                        fill: m.color || "#8B90A0",
                        fontSize: 10,
                        fontFamily: "'JetBrains Mono', monospace",
                      }}
                    />
                  ))}
              </ComposedChart>
            </ResponsiveContainer>
          )}
          {!loading && !error && chartData.length === 0 && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#5c6070", fontSize: 13 }}>
              查無歷史資料
            </div>
          )}
        </div>

        <p style={{ color: "#5c6070", fontSize: 12, lineHeight: 1.7, marginTop: 16, marginBottom: 0 }}>
          資料來源 Yahoo Finance（週K，近5年），僅供參考。判斷重點：低位盤整時間 ≥ 2 年、是否剛突破盤整區間起漲。
        </p>
      </div>
    </div>
  );
}
