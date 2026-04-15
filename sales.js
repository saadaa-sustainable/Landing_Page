// ─────────────────────────────────────────────
//  sales.js  —  Saadaa Ops Dashboard
//  Calls the local Python server at /api/sales
//  Token is kept securely in server.py — not here
// ─────────────────────────────────────────────

async function shopifySalesFetch() {
    const resp = await fetch('/api/sales');

    if (!resp.ok) {
        const txt = await resp.text().catch(() => '');
        throw new Error(`Server error ${resp.status}: ${txt.slice(0, 200)}`);
    }

    const rows = await resp.json();

    if (!Array.isArray(rows) || !rows.length) {
        throw new Error('No sales data returned from server.');
    }

    return rows.map(r => ({
        product_title: String(r.product_title || '').trim(),
        product_vendor: String(r.product_vendor || '').trim(),
        product_type: String(r.product_type || '').trim(),
        net_items_sold: Number(r.net_items_sold || 0),
        gross_sales: Number(r.gross_sales || 0),
        discounts: Number(r.discounts || 0),
        returns: Number(r.returns || 0),
        net_sales: Number(r.net_sales || 0),
        taxes: Number(r.taxes || 0),
        total_sales: Number(r.total_sales || 0),
    })).filter(r => r.product_title && r.product_title !== 'Totals');
}