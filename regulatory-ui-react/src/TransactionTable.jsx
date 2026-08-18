import React, { useState } from 'react';

const CHANNEL_COLOR = {
  UPI: "#1f9d63", // green
  NEFT: "#2f7fd6", // blue
  RTGS: "#cf861b", // amber/orange
  CARD: "#7c5bef", // purple
};

export function TransactionTable({ rows, selected, onSelect, totalCount }) {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const rowsPerPage = 20;

  // Filter rows based on search
  const filteredRows = rows.map((tx, index) => ({ tx, index })).filter(({ tx }) => {
    if (!search) return true;
    const s = search.toLowerCase();
    const rec = tx.record || {};
    return (
      (rec.tx_id && rec.tx_id.toLowerCase().includes(s)) ||
      (tx.sender && tx.sender.toLowerCase().includes(s)) ||
      (tx.receiver && tx.receiver.toLowerCase().includes(s)) ||
      (tx.amount && tx.amount.toLowerCase().includes(s)) ||
      (rec.purpose_code && rec.purpose_code.toLowerCase().includes(s)) ||
      (rec.channel && rec.channel.toLowerCase().includes(s))
    );
  });

  const totalPages = Math.ceil(filteredRows.length / rowsPerPage);
  const paginatedRows = filteredRows.slice(page * rowsPerPage, (page + 1) * rowsPerPage);

  const handleSearch = (e) => {
    setSearch(e.target.value);
    setPage(0);
  };

  return (
    <div style={{ border: "1px solid rgba(0,0,0,.08)", borderRadius: 13, overflow: "hidden", marginTop: "20px", background: "#fffdfa", boxShadow: "0 1px 3px rgba(0,0,0,.05)" }}>
      <div
        style={{
          padding: "12px 16px",
          background: "#fffdfa",
          borderBottom: "1px solid rgba(0,0,0,.08)",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: "10px"
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px", fontFamily: "'IBM Plex Mono', monospace", fontSize: "10.5px", letterSpacing: "1.5px", color: "#8a8478" }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#1f9d63" strokeWidth="2"><path d="M4 4h9l3 3v13H4z" /><path d="M8 13h6M8 16h6" /></svg>
          UPLOADED TRANSACTIONS
        </div>
        <input
          type="text"
          placeholder="Search ID, user, amount..."
          value={search}
          onChange={handleSearch}
          style={{
            padding: "8px 12px",
            borderRadius: "8px",
            border: "1px solid rgba(0,0,0,.15)",
            fontSize: "13px",
            fontFamily: "'Space Grotesk', sans-serif",
            outline: "none",
            width: "260px",
            background: "#ffffff",
            color: "#1a1712"
          }}
        />
      </div>
      
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13.5px", fontFamily: "'Space Grotesk', sans-serif", color: "#1a1712" }}>
          <thead>
            <tr>
              {["tx_id", "channel", "amount", "sender", "receiver", "purpose"].map((h) => (
                <th
                  key={h}
                  style={{
                    padding: "10px 16px",
                    textAlign: "left",
                    color: "#8a8478",
                    fontWeight: 600,
                    fontSize: "11px",
                    textTransform: "uppercase",
                    letterSpacing: "0.5px",
                    borderBottom: "1px solid rgba(0,0,0,.08)",
                    whiteSpace: "nowrap",
                    background: "rgba(0,0,0,.02)"
                  }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paginatedRows.length === 0 ? (
              <tr>
                <td colSpan="6" style={{ padding: "30px", textAlign: "center", color: "#8a8478" }}>No transactions found matching your search.</td>
              </tr>
            ) : paginatedRows.map(({ tx, index }) => {
              const isSel = index === selected;
              const rec = tx.record || {};
              const channel = rec.channel || "—";
              const cColor = CHANNEL_COLOR[channel.toUpperCase()] || "#a9a297";
              
              return (
              <tr
                key={rec.tx_id || index}
                onClick={() => onSelect(index)}
                style={{
                  cursor: "pointer",
                  background: isSel ? "rgba(31,157,99,.08)" : "transparent",
                  borderBottom: "1px solid rgba(0,0,0,.05)",
                  borderLeft: isSel ? `3px solid #1f9d63` : `3px solid transparent`,
                  transition: "background 0.15s",
                }}
              >
                <td style={{ padding: "10px 16px", fontFamily: "'IBM Plex Mono', monospace", color: isSel ? "#178a55" : "#3a352e", fontWeight: isSel ? 600 : 500 }}>
                  {rec.tx_id || "—"}
                </td>
                <td style={{ padding: "10px 16px" }}>
                  <span
                    style={{
                      fontSize: "10.5px",
                      padding: "3.5px 8px",
                      borderRadius: "6px",
                      fontWeight: 600,
                      color: cColor,
                      background: `${cColor}18`,
                      border: `1px solid ${cColor}40`,
                    }}
                  >
                    {channel}
                  </span>
                </td>
                <td style={{ padding: "10px 16px", fontWeight: 600 }}>
                  {tx.amount}
                </td>
                <td style={{ padding: "10px 16px" }}>{tx.sender}</td>
                <td style={{ padding: "10px 16px" }}>{tx.receiver}</td>
                <td style={{ padding: "10px 16px", fontFamily: "'IBM Plex Mono', monospace", color: "#8a8478", fontSize: "12px" }}>
                  {rec.purpose_code || "—"}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      
      {totalPages > 1 && (
        <div style={{ 
          padding: "12px 16px", 
          borderTop: "1px solid rgba(0,0,0,.08)", 
          display: "flex", 
          justifyContent: "space-between", 
          alignItems: "center",
          background: "#fffdfa"
        }}>
          <span style={{ fontSize: "12.5px", color: "#8a8478", fontFamily: "'Space Grotesk', sans-serif" }}>
            Showing {page * rowsPerPage + 1} - {Math.min((page + 1) * rowsPerPage, filteredRows.length)} of {filteredRows.length} results
          </span>
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              style={{
                padding: "7px 14px",
                borderRadius: "8px",
                border: "1px solid rgba(0,0,0,.15)",
                background: page === 0 ? "rgba(0,0,0,.02)" : "#ffffff",
                color: page === 0 ? "#a9a297" : "#1a1712",
                cursor: page === 0 ? "not-allowed" : "pointer",
                fontFamily: "'Space Grotesk', sans-serif",
                fontSize: "13px",
                fontWeight: 600,
                boxShadow: page === 0 ? "none" : "0 1px 2px rgba(0,0,0,.04)"
              }}
            >
              Previous
            </button>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              style={{
                padding: "7px 14px",
                borderRadius: "8px",
                border: "1px solid rgba(0,0,0,.15)",
                background: page >= totalPages - 1 ? "rgba(0,0,0,.02)" : "#ffffff",
                color: page >= totalPages - 1 ? "#a9a297" : "#1a1712",
                cursor: page >= totalPages - 1 ? "not-allowed" : "pointer",
                fontFamily: "'Space Grotesk', sans-serif",
                fontSize: "13px",
                fontWeight: 600,
                boxShadow: page >= totalPages - 1 ? "none" : "0 1px 2px rgba(0,0,0,.04)"
              }}
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
